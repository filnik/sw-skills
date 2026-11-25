"""Guards that keep scenarios honest as the app and the framework move:

    tags check   every selector of the pack's flows/tags.py and every tag literal of a scenario exists in the
                 worktree's sources (a literal, a "prefix.${x}_suffix" template, or pinned by a test); every `text:`
                 label of tags.py is one of the app's strings (pack key guard.strings); log patterns of flows/logs.py
                 whose literal part no longer appears in the source are warned about; the pack's tags_check hook adds
                 the app's own checks.
    lint         a scenario on flows defines no function flows already has, and no two scenarios hold the
                 same function body.
    api          writes the flows' API.md (one line per public flow) from the docstrings: what a writing agent reads.

tags.py may hold NOT_APP (names that are not selectors), NOT_IN_SOURCE (selectors built at runtime) and UNMERGED
({selector: where it is waiting to be merged}, reported as a warning).
"""

from __future__ import annotations

import ast
import hashlib
import html
import importlib
import inspect
import re
import os
import shutil
import subprocess
from pathlib import Path

from devtest_common import PACK, cfg, hook, pack_dir

HERE = Path(__file__).resolve().parent
FLOW_DIRS = [HERE / "flows"] + ([PACK / "flows"] if PACK and (PACK / "flows").is_dir() else [])
SCENARIOS = pack_dir("scenarios")
MODULES = tuple(cfg("guard.modules") or ("phone", "fields", "pair", "state", "storagefiles"))
TAG_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+[._]?$")
NOT_TAGS = re.compile(r"^(com|org|android|java|kotlin)\.|\.(jpe?g|png|db|json|log|txt|py|kt|xml|mp4|ui)$", re.I)
STRINGS = cfg("guard.strings") or ""
SOURCE_GLOBS = cfg("guard.source_globs") or ["*.kt", "*.swift"]


def _git_grep(top: Path, args: list) -> list:
    """Search the working tree's sources with rg, untracked files and source archives included. Never with a regex
    `git grep`: on a large repo it can take unbounded memory under UTF-8.
    """
    env, exe = dict(os.environ), shutil.which("rg")
    if not exe:  # inside Claude Code rg can be only a shell function over the claude binary (ARGV0=rg)
        exe = os.environ.get("CLAUDE_CODE_EXECPATH") or os.path.expanduser("~/.local/bin/claude")
        env["ARGV0"] = "rg"
    globs = [x for g in SOURCE_GLOBS for x in ("-g", g)]
    r = subprocess.run([exe, "--no-ignore", "--with-filename", *globs, "-g", "!**/build/**",
                        *[a for a in args if a != "-E"], cfg("source_dir") or "."], cwd=top,
                       capture_output=True, text=True, timeout=60, env=env)
    return r.stdout.splitlines()


def _is_test(path: str) -> bool:
    return bool(re.search(r"[Tt]est|/proto/", path))


def _tags_module():
    try:
        from flows import tags
    except ImportError:
        return None
    return tags


def flow_tags() -> dict:
    """{constant name: selector} of tags.py, tuples flattened as NAME[i]; NOT_APP and NOT_IN_SOURCE left out."""
    T = _tags_module()
    out = {}
    for name, v in vars(T).items() if T else ():
        if name.startswith("_") or name in getattr(T, "NOT_APP", ()):
            continue
        for i, s in enumerate(v if isinstance(v, tuple) else (v,)):
            if (isinstance(s, str) and s not in getattr(T, "NOT_IN_SOURCE", ())
                    and (s[:5] in ("text:", "text~") or TAG_RE.match(s))):
                out[name if not isinstance(v, tuple) else "%s[%d]" % (name, i)] = s
    return out


def scenario_tags(path: Path) -> dict:
    """{line: tag} for the string literals of a scenario that look like test tags."""
    tree = ast.parse(path.read_text())
    return {"%s:%d" % (path.name, n.lineno): n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and TAG_RE.match(n.value)
            and not NOT_TAGS.search(n.value)}


def _templates(top: Path) -> list:
    """String templates ("a.${x}_b") that build tags, as (regex, literal) from the app's main sources."""
    out = []
    for line in _git_grep(top, ["-o", "-E", r'"[a-z_]+(\.[a-z0-9_]+)*\.?[a-z0-9_]*\$[^"]*"']):
        path, _, lit = line.partition(":")
        if _is_test(path):
            continue
        body = lit.strip('"')
        parts = re.split(r"\$\{[^}]*\}?|\$[A-Za-z_][A-Za-z0-9_.]*", body)
        if "." not in parts[0]:
            continue  # "a${it.id}" would match anything: only templates with a dotted literal head count
        out.append((re.compile("^" + "[A-Za-z0-9_]+".join(re.escape(p) for p in parts) + "$"), lit))
    return out


def _strings(top: Path) -> list:
    f = top / STRINGS
    if not STRINGS or not f.exists():
        return []
    raw = re.findall(r"<string name=\"[^\"]+\"[^>]*>(.*?)</string>", f.read_text(), re.S)
    return [html.unescape(s).replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n") for s in raw]


def check_tags(top: Path, scenario=None) -> tuple:
    """(errors, warnings, report lines) for the selectors in tags.py (+ the scenario's own tag literals)."""
    targets = {"tags.py " + k: v for k, v in flow_tags().items()}
    if scenario is not None:
        targets.update(scenario_tags(Path(scenario)))
    tags = {k: v for k, v in targets.items() if v[:5] not in ("text:", "text~")}
    labels = {k: v for k, v in targets.items() if v[:5] in ("text:", "text~")}
    pats = sorted({'"' + v if v[-1] in "._" else '"' + v + '"' for v in tags.values()})
    found_main, found_test = set(), set()
    for i in range(0, len(pats), 80):  # one search per 80 tags
        args = ["-o", "-F"]
        for p in pats[i:i + 80]:
            args += ["-e", p]
        for line in _git_grep(top, args):
            path, _, lit = line.rpartition(":")
            (found_test if _is_test(path) else found_main).add(lit.strip('"'))
    unmerged_at = getattr(_tags_module(), "UNMERGED", {})
    templates = None
    errors, report, unmerged = [], [], []
    for where, tag in sorted(tags.items()):
        if tag in found_main:
            continue
        if templates is None:
            templates = _templates(top)
        tpl = next((lit for rx, lit in templates if rx.match(tag)), None) if tag[-1] not in "._" else None
        if tpl:
            report.append("%s %s: template %s" % (where, tag, tpl))
        elif tag in found_test:
            report.append("%s %s: only in tests (built at runtime)" % (where, tag))
        else:
            (unmerged if tag in unmerged_at else errors).append("%s %s: not in the worktree's sources%s" % (
                where, tag, " (%s not merged here)" % unmerged_at[tag] if tag in unmerged_at else ""))
    strings = _strings(top)
    for where, sel in sorted(labels.items()):
        want = sel[5:]
        ok = (want in strings) if sel[4] == ":" else any(want.lower() in s.lower() for s in strings)
        if not ok:
            # A label can be a literal in the code instead of a translated string: look for it quoted, outside tests.
            ok = any(not _is_test(path) for path in _git_grep(top, ["-l", "-F", "-e", '"' + want + '"']))
        if strings and not ok and where.startswith("tags.py"):
            (unmerged if sel in unmerged_at else errors).append("%s %s: no such app string in %s%s" % (
                where, sel, STRINGS.rsplit("/", 3)[0] + "/…/" + STRINGS.rsplit("/", 1)[-1],
                " (%s not merged here)" % unmerged_at[sel] if sel in unmerged_at else ""))
    warnings = unmerged
    if hook("tags_check"):
        hook("tags_check")(top, errors, warnings)
    try:
        from flows import logs
    except ImportError:
        return errors, warnings, report
    for name, pat in vars(logs).items():
        if name.startswith("_") or not isinstance(pat, str):
            continue
        runs = [r.strip() for r in re.split(r"\\.|[()\[\]{}|*+?^$]|%s|\\[dswS]", pat) if len(r.strip()) >= 12]
        if not runs:
            continue
        run = max(runs, key=len)
        pieces = [run] + [q for q in re.split(r": |=", run) if len(q) >= 8]  # "track: $event": the event name alone
        if not any(_git_grep(top, ["-l", "-F", "-e", q]) for q in pieces):
            warnings.append("logs.%s: '%s' not found in the sources (renamed?)" % (name, run))
    return errors, warnings, report


# ------------------------------------------------------------------------------------------------ lint

def _uses_flows(tree) -> bool:
    return any(isinstance(n, (ast.Import, ast.ImportFrom)) and "flows" in (getattr(n, "module", "") or "")
               + " ".join(a.name for a in n.names) for n in ast.walk(tree))


def _functions(tree) -> list:
    """Top-level helper functions: not steps (named _), pre/post, nor decorated."""
    return [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name != "_" and not n.decorator_list]


def _body_hash(fn) -> str:
    body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(
        getattr(fn.body[0], "value", None), ast.Constant) else fn.body
    return hashlib.sha1("".join(ast.dump(s) for s in body).encode()).hexdigest()


def undefined_names(path: Path) -> list:
    """Names a module reads that nothing defines (a missing import shows on the phone only when that line runs)."""
    import builtins
    import symtable
    top = symtable.symtable(path.read_text(), str(path), "exec")
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    defined |= set(dir(builtins)) | {"__file__", "__name__", "__path__"}
    out = set()

    def walk(t):
        for s in t.get_symbols():
            implicit = t.get_type() == "module" or s.is_global()
            if s.is_referenced() and implicit and not s.is_assigned() and s.get_name() not in defined:
                out.add(s.get_name())
        for child in t.get_children():
            walk(child)
    walk(top)
    return sorted(out)


def flow_names() -> dict:
    out = {}
    for m in MODULES:
        mod = importlib.import_module("flows." + m)
        for name, f in vars(mod).items():
            if inspect.isfunction(f) and f.__module__ == mod.__name__ and not name.startswith("_"):
                out.setdefault(name, "%s.%s" % (m, name))
    return out


def lint(files=None) -> tuple:
    """(errors, legacy scenario names). Errors only for scenarios on flows."""
    everything = sorted(SCENARIOS.glob("*.py")) if SCENARIOS else []
    paths = [Path(f) for f in files] if files else everything
    flows = flow_names()
    trees = {p: ast.parse(p.read_text()) for p in set(paths) | set(everything)}
    migrated = {p for p in trees if _uses_flows(trees[p])}
    errors, bodies = [], {}
    for p in everything:
        for fn in _functions(trees[p]):
            if len(fn.body) > 1 or not isinstance(fn.body[0], (ast.Return, ast.Expr)):
                bodies.setdefault(_body_hash(fn), []).append((p, fn.name))
    for p in [f for d in FLOW_DIRS for f in sorted(d.glob("*.py"))] + sorted(HERE.glob("devtest*.py")):
        for n in undefined_names(p):
            errors.append("%s: '%s' is used but never defined or imported" % (p.name, n))
    for p in paths:
        if p not in migrated:
            continue
        for n in undefined_names(p):
            errors.append("%s: '%s' is used but never defined or imported" % (p.name, n))
        for fn in _functions(trees[p]):
            if fn.name in flows:
                errors.append("%s:%d %s() exists in flows as %s: use it" % (p.name, fn.lineno, fn.name, flows[fn.name]))
            twins = [(q, n) for q, n in bodies.get(_body_hash(fn), []) if (q, n) != (p, fn.name)]
            if twins:
                errors.append("%s:%d %s() has the same body as %s: move it to flows" % (
                    p.name, fn.lineno, fn.name, ", ".join("%s:%s" % (q.name, n) for q, n in twins)))
    legacy = [p.stem for p in everything if p not in migrated]
    return errors, legacy


# ------------------------------------------------------------------------------------------------ api

def api() -> Path:
    """Writes API.md from the docstrings into the pack's flows/ (the engine's without a pack); returns its path."""
    lines = ["# flows API (generated by `devtest api`: do not edit)", "",
             "`from flows import %s`. `x` is any Driver (`d`, or `pair.colleague(d)`).%s%s" % (
                 ", ".join(MODULES), " Selectors: `T.*` (tags.py)." if _tags_module() else "",
                 " Log patterns: `L.*` (logs.py)." if any((d / "logs.py").is_file() for d in FLOW_DIRS) else ""), ""]
    for m in MODULES:
        mod = importlib.import_module("flows." + m)
        lines += ["## %s" % m, "", (inspect.getdoc(mod) or "").split("\n\n")[0].replace("\n", " "), ""]
        for name, f in vars(mod).items():
            if inspect.isfunction(f) and f.__module__ == mod.__name__ and not name.startswith("_"):
                doc = (inspect.getdoc(f) or "").replace("\n", " ")
                first = re.split(r"(?<=\.)\s", doc, 1)[0] if doc else ""
                first = first if len(first) <= 200 else first[:197].rsplit(" ", 1)[0] + "…"
                sig = re.sub(r"=\([^)]{30,}\)", "=(…)", str(inspect.signature(f)))
                lines.append("- `%s%s` %s" % (name, sig.replace("flows.sheet.", ""), first))
        lines.append("")
    out = FLOW_DIRS[-1] / "API.md"
    out.write_text("\n".join(lines))
    return out
