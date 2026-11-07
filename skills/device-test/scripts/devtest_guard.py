"""Guards that keep scenarios honest as the app moves, and the flows listing a scenario writer reads.

    tags check  every test-tag literal of a scenario and of its flows/ exists in the worktree's source: a renamed tag
                is a SCENARIO ERROR before the run, not a FAIL that blames the app
    lint        no undefined name in a scenario, no helper that flows/ already has, no helper copied between two
                scenarios (shared steps belong in flows/)
    flows       one line per public flow: name, signature, first sentence of its docstring
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import re
import subprocess
import symtable
from pathlib import Path

TAG_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
NOT_TAGS = re.compile(r"^(com|org|net|io|android|java|kotlin|androidx|apple)\.|\.(jpe?g|png|db|sqlite|json|log|txt|py|"
                      r"kt|swift|xml|plist|mp4|pb|preferences_pb)$", re.I)


def tag_literals(path: Path) -> dict:
    """{file:line: literal} for the string literals of a Python file that look like test tags (`login.submit`)."""
    tree = ast.parse(path.read_text())
    return {"%s:%d" % (path.name, n.lineno): n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and TAG_RE.match(n.value)
            and not NOT_TAGS.search(n.value)}


def _grep(top: Path, patterns: list, exclude: list, only_matching: bool = True) -> list:
    if not patterns:
        return []
    args = ["git", "-C", str(top), "grep", "--untracked", "-I", "-h", "-F"] + (["-o"] if only_matching else ["-l"])
    for p in patterns:
        args += ["-e", p]
    args += ["--", "."] + [":(exclude)%s" % e for e in exclude]
    return subprocess.run(args, capture_output=True, text=True, timeout=120).stdout.splitlines()


def check_tags(top: Path, scenario: Path) -> tuple:
    """(errors, warnings) for the tags of the scenario and of the flows/ folder next to it."""
    folder = scenario.parent
    files = [scenario] + sorted((folder / "flows").glob("*.py"))
    tags = {}
    for f in files:
        for where, tag in tag_literals(f).items():
            tags.setdefault(tag, where)
    try:
        exclude = [str(folder.resolve().relative_to(top.resolve()))]
    except ValueError:
        exclude = []
    quoted = sorted({q % t for t in tags for q in ('"%s"', "'%s'")})
    found = set()
    for i in range(0, len(quoted), 80):
        found |= {s.strip("\"'") for s in _grep(top, quoted[i:i + 80], exclude)}
    errors, warnings = [], []
    for tag, where in sorted(tags.items(), key=lambda kv: kv[1]):
        if tag in found:
            continue
        head = tag[:tag.rindex(".") + 1]
        if _grep(top, ['"%s' % head, "'%s" % head], exclude, only_matching=False):
            warnings.append("%s %s: not in the source as written, only its head '%s' (built at runtime?)" % (
                where, tag, head))
        else:
            errors.append("%s %s: not in the worktree's source (renamed in the app?)" % (where, tag))
    return errors, warnings


def preflight_tags(top: Path, scenario: Path, strict: bool) -> None:
    """SCENARIO ERROR naming the missing tags; only warnings when not strict (a red proof on an older build)."""
    from devtest_scenario import ScenarioError
    errors, warnings = check_tags(top, scenario)
    for w in warnings + ([] if strict else errors):
        print("· warning: %s" % w)
    if errors and strict:
        raise ScenarioError("tags missing from the worktree (renamed? pass --no-tags-check if the app builds them at "
                            "runtime): %s" % "; ".join(errors))


# ------------------------------------------------------------------------------------------------ lint

def undefined_names(path: Path) -> list:
    """Names a module reads that nothing defines: a missing import shows on the phone only when that line runs."""
    top = symtable.symtable(path.read_text(), str(path), "exec")
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    defined |= set(dir(builtins)) | {"__file__", "__name__"}
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


def _helpers(tree) -> list:
    """Top-level helper functions: not steps (named _), not pre/post, not decorated."""
    return [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name != "_" and not n.decorator_list]


def _body_hash(fn) -> str:
    body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(
        getattr(fn.body[0], "value", None), ast.Constant) else fn.body
    return hashlib.sha1("".join(ast.dump(s) for s in body).encode()).hexdigest()


def flow_names(folder: Path) -> dict:
    out = {}
    for f in sorted(folder.glob("*.py")):
        for fn in ast.parse(f.read_text()).body:
            if isinstance(fn, ast.FunctionDef) and not fn.name.startswith("_"):
                out.setdefault(fn.name, "%s.%s" % (f.stem, fn.name))
    return out


def lint(files: list) -> list:
    """Problems of these scenarios, compared with the flows/ folder and the other scenarios next to each."""
    errors = []
    for p in files:
        flows = flow_names(p.parent / "flows")
        trees = {q: ast.parse(q.read_text()) for q in sorted(p.parent.glob("*.py"))}
        trees.setdefault(p, ast.parse(p.read_text()))
        bodies = {}
        for q, tree in trees.items():
            for fn in _helpers(tree):
                if len(fn.body) > 1 or not isinstance(fn.body[0], (ast.Return, ast.Expr)):
                    bodies.setdefault(_body_hash(fn), []).append((q, fn.name))
        for n in undefined_names(p):
            errors.append("%s: '%s' is used but never defined or imported" % (p.name, n))
        for fn in _helpers(trees[p]):
            if fn.name in flows:
                errors.append("%s:%d %s() exists in flows as %s: use it" % (p.name, fn.lineno, fn.name, flows[fn.name]))
            twins = [(q, n) for q, n in bodies.get(_body_hash(fn), []) if (q, n) != (p, fn.name)]
            if twins:
                errors.append("%s:%d %s() has the same body as %s: move it to flows/" % (
                    p.name, fn.lineno, fn.name, ", ".join("%s:%s" % (q.name, n) for q, n in twins)))
    return errors


def flows_api(folder: Path) -> list:
    """One line per public function of flows/*.py, read with ast (nothing is imported or run)."""
    lines = []
    for f in sorted(folder.glob("*.py")):
        for fn in ast.parse(f.read_text()).body:
            if isinstance(fn, ast.FunctionDef) and not fn.name.startswith("_"):
                doc = " ".join((ast.get_docstring(fn) or "").split())
                first = re.split(r"(?<=\.)\s", doc, 1)[0] if doc else ""
                lines.append("%s.%s(%s) %s" % (f.stem, fn.name, ast.unparse(fn.args), first[:200]))
    return lines
