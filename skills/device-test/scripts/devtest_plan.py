"""From a test plan to a scenario skeleton (`new`), and from the ledger to the latest verdicts (`report`).

A plan, as the skill writes it:

    Ticket: SHOP-12 · Cart keeps items after a restart
    Scenario: new device-tests/SHOP-12.py
    Setup: logged in, empty cart
    Checks:
      AC1 add two items and restart the app → the cart shows 2 (tag cart.count)
      AC2 remove one → log "cart saved" and pref cart_size = 1
    Not to test: payment

`new` writes one step per check, covering it, whose body raises NotImplementedError: until it is written, the run
says SCENARIO ERROR instead of passing an empty step.
"""

from __future__ import annotations

import re

CHECK = re.compile(r"^([A-Za-z][\w-]*\d[\w-]*)\s+(.+)$")
HEADER = re.compile(r"^[A-Z][\w /-]*:")


def parse_plan(plan: str) -> dict:
    """{title, out, setup, checks: [(id, action, expected)]} of a test plan."""
    title, out, setup, checks, in_checks = "", None, "", [], False
    for line in plan.splitlines():
        s = line.strip()
        if not s:
            continue
        if in_checks and not line[:1].isspace() and HEADER.match(s):
            in_checks = False
        if not in_checks:
            m = re.match(r"(?:Ticket|Title):\s*(.+)", s)
            if m:
                title = re.sub(r"\s*·\s*", " ", m.group(1)).strip()
            m = re.match(r"Scenario:\s*new\s+(\S+\.py)", s)
            if m:
                out = m.group(1)
            m = re.match(r"Setup:\s*(.+)", s)
            if m:
                setup = m.group(1)
            in_checks = s.lower().startswith("checks")
            continue
        m = CHECK.match(s)
        if m:
            parts = re.split(r"\s*(?:→|->)\s*", m.group(2), 1)
            checks.append((m.group(1), parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    return {"title": title, "out": out, "setup": setup, "checks": checks}


def scaffold(plan: str) -> tuple:
    """(suggested path or None, scenario source) for a test plan."""
    p = parse_plan(plan)
    if not p["checks"]:
        raise ValueError("no checks found: list them under 'Checks:', one per line, each starting with its ID")
    title = p["title"] or "untitled scenario"
    lines = ['"""%s' % title.replace('"""', "'''"), "",
             "Written by `devtest new` from the test plan. Each step raises NotImplementedError until it is written.",
             '"""', "", "from devtest_scenario import Scenario", "", "S = Scenario(%r, criteria={" % title]
    lines += ["    %r: %r," % (cid, ("%s → %s" % (a, e)) if e else a) for cid, a, e in p["checks"]]
    lines += ["})", "", "", "@S.pre", "def setup(d):"]
    if p["setup"]:
        lines.append("    # setup: %s" % p["setup"])
    lines += ["    d.launch(restart=True)", ""]
    for cid, action, expected in p["checks"]:
        lines += ["", "@S.step(%r, covers=[%r])" % (action, cid), "def _(d):"]
        if expected:
            lines.append("    # expect: %s" % expected)
        lines += ["    # m = d.mark() before the action; d.expect_log(r'...', since=m) after it",
                  "    raise NotImplementedError(%r)" % ("%s: %s" % (cid, action)), ""]
    lines += ["", "@S.post", "def restore(d):", "    # undo what pre and the steps changed (prefs, network, data)",
              "    pass", ""]
    return p["out"], "\n".join(lines)


def report(entries: list, current_build: str, scenario=None) -> list:
    """The latest verdict per scenario × phone, one line each, marked when it is from another build."""
    latest = {}
    for e in entries:
        if scenario and e.get("scenario") != scenario:
            continue
        latest[(e.get("scenario"), e.get("phone"))] = e
    out = []
    for (name, phone), e in sorted(latest.items()):
        crit = e.get("criteria") or {}
        passed = sum(1 for v in crit.values() if v == "PASS")
        others = " · ".join("%s %s" % (c, v.split(" (")[0]) for c, v in crit.items() if v != "PASS")
        out.append("%s · %s · %s · %s %s · %d PASS%s%s" % (
            name, phone, e.get("verdict"), e.get("at", "?").replace("T", " "), e.get("build", "?"), passed,
            " · " + others if others else "", "" if e.get("build") == current_build else " (older build)"))
    return out
