#!/usr/bin/env python3
"""What of a mobile PR was tested on the real phones, from the device-test ledger.

Prints what the PR changes per platform, then one block per scenario × phone run on the PR's builds or named after
its ticket: each check's latest verdict (devtest suite rules) and the build it ran on, measured against HEAD.

    tested.py --repo <clone> [--base origin/main] [--ticket ABC-123]

Reads the `tested` block of the project's .claude/config/project.json, found from the clone upward (all optional):
`phones`, the device-test aliases to report, in that order (default: the pack's phones); `paths`, per bucket (off,
both, android, ios, build) a list of regexes for the project's own folders, added to that bucket's defaults below.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "device-test/scripts"))
sys.path.insert(0, str(HERE.parents[3] / "shared"))
from devtest_common import DEVICES  # noqa: E402
from devtest_scenario import load  # noqa: E402
from devtest_suite import SCENARIOS, current_version, entries, verdicts  # noqa: E402
from project_config import project_config  # noqa: E402

SERIALS = {ident: alias for alias, (_, ident) in DEVICES.items()}
NOT_JUDGED = ("SKIP", "NOT RUN")
OLDER = " [older scenario version]"
# Kotlin Multiplatform source sets, Swift and Gradle files. Checked in this order, the first match wins.
DEFAULT_PATHS = {
    "off": [r"/src/\w*Test/", r"/src/test/", r"Tests?/"],
    "both": [r"/src/commonMain/"],
    "android": [r"/src/androidMain/"],
    "ios": [r"/src/(ios\w*|apple\w*|native\w*)Main/", r"/src/nativeInterop/", r"\.swift$"],
    "build": [r"\.gradle(\.kts)?$", r"gradle\.properties$", r"\.versions\.toml$"],
}


def path_rules(extra=None) -> list:
    """(bucket, regex) in checking order: each bucket's defaults plus the project's own patterns for it."""
    extra = extra or {}
    return [(name, re.compile("|".join(pats + list(extra.get(name) or [])))) for name, pats in DEFAULT_PATHS.items()]


def bucket(path: str, rules: list) -> str:
    """Where a changed file runs: both (shared code), android, ios, build (gradle), off (never on a phone)."""
    return next((name for name, rx in rules if rx.search(path)), "off")


def summary(paths: list, rules: list) -> str:
    groups = {}
    for p in paths:
        groups.setdefault(bucket(p, rules), []).append(p)
    return " · ".join("%s %d" % (b, len(groups[b])) for b in ("both", "android", "ios", "build", "off") if b in groups) \
        or "nothing", groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--ticket")
    a = ap.parse_args()
    repo = a.repo
    conf = project_config(Path(repo).expanduser()).get("tested") or {}
    rules = path_rules(conf.get("paths"))
    phones = conf.get("phones") or list(DEVICES)

    def git(*args):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout.strip()

    branch = git("branch", "--show-current") or "(detached)"
    ticket = a.ticket or (re.search(r"[A-Z][A-Z0-9]+-\d+", branch.upper()) or [None])[0]
    head = git("rev-parse", "HEAD")
    if not head:
        sys.exit("not a git checkout: %s" % repo)
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    print("PR %s · branch %s · ticket %s · HEAD %s%s · base %s"
          % (repo, branch, ticket or "?", head[:10], " +uncommitted changes" if dirty else "", a.base))

    line, groups = summary(git("diff", "--name-only", "%s...HEAD" % a.base).splitlines(), rules)
    print("changed vs base (files): %s" % line)
    for b in ("android", "ios", "build"):
        if b in groups:
            names = [Path(p).name for p in groups[b]]
            print("  %s: %s%s" % (b, ", ".join(names[:6]), " …" if len(names) > 6 else ""))

    own = set(git("rev-list", "%s..HEAD" % a.base).splitlines())
    labels = {}

    def label(build: str) -> str:
        """A short name for the build (HEAD, or B1, B2… described at the end)."""
        if build not in labels:
            sha = build.split("+")[0]
            extra = " +uncommitted" if "+dirty" in build else ""
            if head.startswith(sha):
                labels[build] = ("HEAD" + extra, [])
            elif subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", sha, "HEAD"],
                                capture_output=True).returncode == 0:
                after = git("log", "--first-parent", "--reverse", "--format=%H\t%ad\t%P\t%s",
                            "--date=format:%d/%m %H:%M", "%s..HEAD" % sha).splitlines()
                lines, from_base = [], 0
                for c in after:
                    full, when, parents, subject = c.split("\t", 3)
                    if full not in own:
                        from_base += 1
                        continue
                    files = "merge" if " " in parents else summary(
                        git("diff-tree", "--no-commit-id", "--name-only", "-r", full).splitlines(), rules)[0]
                    lines.append("%s %s %s (%s)" % (full[:10], when, subject[:70], files))
                labels[build] = ("B%d %s%s, %d commits before HEAD%s" % (
                    len(labels) + 1, sha, extra, len(after),
                    " (%d of them already on the base, not listed)" % from_base if from_base else ""), lines)
            else:
                labels[build] = ("B%d %s%s, not in HEAD's history (rebased, amended or another branch)"
                                 % (len(labels) + 1, sha, extra), [])
        return labels[build][0].split(",")[0]

    def ours(r) -> bool:
        sha = r.get("build", "").split("+")[0]
        named = ticket and re.search(r"(^|[^\w-])%s($|[^\d])" % re.escape(ticket), r["scenario"])
        return bool(named) or (len(sha) >= 7 and any(c.startswith(sha) for c in own))

    rows = []
    for r in entries():
        if ours(r):
            r["phone"] = SERIALS.get(r["phone"], r["phone"])
            rows.append(r)
    if not rows:
        print("ledger: no device-test run on this PR's builds or named after %s" % (ticket or "its ticket"))
        print("phones with no run: %s" % ", ".join(phones))
        return 0

    reds = [r for r in rows if r["verdict"].startswith("RED")]
    rows = [r for r in rows if not r["verdict"].startswith("RED")]
    for r in reds:
        print("red proof: %s %s %s on %s (%s)" % (r["scenario"], r["phone"], r["verdict"], r["build"], r["at"][:16]))

    for scenario in dict.fromkeys(r["scenario"] for r in rows):
        try:
            texts = load(SCENARIOS / (scenario + ".py")).criteria
        except Exception:
            texts = {}
        is_now = current_version(scenario)
        print("\n%s" % scenario)
        for c, text in texts.items():
            print("  %s %s" % (c, text[:110]))
        for phone in dict.fromkeys(r["phone"] for r in rows if r["scenario"] == scenario):
            mine = [r for r in rows if r["scenario"] == scenario and r["phone"] == phone]
            new = [r for r in mine if is_now(r)]
            crit = verdicts(new)[0]
            for c, v in verdicts([r for r in mine if not is_now(r)])[0].items():
                if c not in crit or crit[c].split(" (")[0] in NOT_JUDGED:
                    crit[c] = v + (OLDER if new else "")
            last = mine[-1]
            print("  %s · %d runs · last %s %s" % (phone, len(mine), last["at"][:16], last["verdict"]))
            if not crit:
                print("    no check judged: %s · build %s" % (last.get("reason", "")[:140], label(last["build"])))
            by_build = {}
            for c, v in crit.items():
                word = v[:-len(OLDER)] if v.endswith(OLDER) else v
                src = next((r for r in reversed(mine) if r.get("criteria", {}).get(c) == word), last)
                # Only failures keep their reason: N/A and SKIP reasons are noise here.
                short = word[:120] if word.startswith(("FAIL", "BLOCKED", "PARTIAL")) else word.split(" (")[0]
                by_build.setdefault(label(src["build"]), []).append("%s %s%s" % (c, short, OLDER if word != v else ""))
            for build, checks in by_build.items():
                print("    build %s: %s" % (build, " · ".join(checks)))

    if any(lines or text != "HEAD" for text, lines in labels.values()):
        print("\nbuilds tested (commits after each, oldest first):")
        for text, lines in labels.values():
            print("  %s" % text)
            for line in lines:
                print("    %s" % line)

    tested = {r["phone"] for r in rows}
    print("\nphones with no run: %s" % (", ".join(p for p in phones if p not in tested) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
