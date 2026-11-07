#!/usr/bin/env python3
"""Pure-logic tests for skills/my-prs/scripts/my_prs.py. Run: python3 tests/test_my_prs.py (exit 0 = green)."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "skills" / "my-prs" / "scripts" / "my_prs.py"
spec = importlib.util.spec_from_file_location("my_prs", path)
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


def pr(ci="SUCCESS", merge="CLEAN", review=None, draft=False, mergeable="MERGEABLE", threads=(), asked=()):
    return {"number": 7, "title": "Fix cart", "createdAt": "2026-11-01T10:00:00Z", "baseRefName": "main",
            "repository": {"nameWithOwner": "acme/shop"}, "isDraft": draft, "mergeable": mergeable,
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
