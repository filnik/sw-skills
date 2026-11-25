#!/usr/bin/env python3
"""Pure-logic tests for skills/my-prs/scripts/my_prs.py. Run: python3 tests/test_my_prs.py (exit 0 = green)."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "skills" / "my-prs" / "scripts" / "my_prs.py"
spec = importlib.util.spec_from_file_location("my_prs", path)
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


def pr(ci="SUCCESS", merge="CLEAN", review=None, draft=False, mergeable="MERGEABLE", threads=(), asked=(),
       number=7, base="main", head="fix-cart"):
    return {"id": f"PR_{number}", "number": number, "title": "Fix cart", "createdAt": "2026-11-01T10:00:00Z",
            "baseRefName": base, "headRefName": head, "headRefOid": f"h{number}", "baseRef": {"target": {"oid": f"b{number}"}},
            "repository": {"nameWithOwner": "acme/shop", "defaultBranchRef": {"name": "main"}},
            "isDraft": draft, "mergeable": mergeable,
            "mergeStateStatus": merge, "reviewDecision": review,
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": a}} for a in asked]},
            "reviewThreads": {"nodes": [{"isResolved": r, "comments": {"nodes": [{"author": {"login": who}}]}}
                                        for r, who in threads]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": ci} if ci else None}}]}}


def texts(p):
    return [t for _, t in mp.steps(p, "me")]


def test_steps():
    assert texts(pr()) == ["ready to merge"]
    assert texts(pr(merge="DIRTY", mergeable="CONFLICTING", ci="FAILURE")) == \
        ["CI red → /dev:ci-why", "conflicts with main → /dev:merge"]
    # a thread whose last word is yours waits for the reviewer, not for you
    assert texts(pr(merge="BLOCKED", review="REVIEW_REQUIRED", asked=["ann"],
                    threads=[(False, "ann"), (False, "me"), (True, "bob")])) == \
        ["1 open thread to answer → /dev:babysit", "waiting for ann"]
    assert texts(pr(merge="BLOCKED", review="REVIEW_REQUIRED"))[0].startswith("no reviewer asked")
    assert texts(pr(merge="BEHIND", review="APPROVED")) == ["behind main → gh pr update-branch 7"]
    assert texts(pr(draft=True, ci="FAILURE", merge="DRAFT")) == ["CI red → /dev:ci-why", "draft"]
    assert texts(pr(ci="PENDING", merge="UNSTABLE")) == ["CI running"]
    assert texts(pr(ci=None, merge="UNKNOWN"))[0].startswith("merge state not computed")
    assert texts(pr(merge="BLOCKED", review="APPROVED")) == ["blocked by a branch rule (blocked)"]


def test_line():
    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    p = pr(ci="FAILURE")
    assert mp.line(p, mp.steps(p, "me"), now) == "acme/shop#7 · Fix cart · 3d · CI red → /dev:ci-why"


def test_lag_against_the_real_base_and_the_inherited_lag_and_the_stack():
    bottom = pr(number=1, base="sync-main", head="part-1")
    top = pr(number=2, base="part-1", head="part-2")
    plain = pr(number=3, merge="BEHIND")
    prs = [bottom, top, plain]
    stacks = mp.stacked_on(prs)
    assert stacks == {"PR_2": 1}
    query = mp.lag_query(prs, stacks)
    # only the bottom PR's base, not the default branch nor one of your PRs, is compared with the default branch
    assert query.count("defaultBranchRef") == 1 and 'compare(headRef:"b1")' in query
    assert 'p1:node(id:"PR_2"){... on PullRequest{baseRef{compare(headRef:"h2"){behindBy}}}}' in query
    mp.apply_lag(prs, {"p0": {"baseRef": {"compare": {"behindBy": 2}},
                              "repository": {"defaultBranchRef": {"compare": {"behindBy": 33}}}},
                       "p1": {"baseRef": {"compare": {"behindBy": 0}}},
                       "p2": {"baseRef": {"compare": {"behindBy": 12}}}})
    for p in prs:
        p["stackedOn"] = stacks.get(p["id"])
    assert mp.lag(bottom) == "2 behind sync-main, sync-main 33 behind main"
    assert mp.lag(top) == "on #1"
    # GitHub's BEHIND already names the lag in the step, with the count
    assert mp.lag(plain) == "" and texts(plain) == ["12 behind main → gh pr update-branch 3"]
    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    assert mp.line(bottom, mp.steps(bottom, "me"), now) == \
        "acme/shop#1 · Fix cart · 3d · 2 behind sync-main, sync-main 33 behind main · ready to merge"


def test_lag_unmeasured_leaves_the_line_as_before():
    p = pr()
    mp.apply_lag([p], None)
    assert p["behind"] is None and p["baseBehind"] is None and mp.lag(p) == ""
    assert mp.lag_query([pr(number=9) | {"baseRef": None}], {}) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
