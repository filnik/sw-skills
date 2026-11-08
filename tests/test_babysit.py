#!/usr/bin/env python3
"""Pure-logic tests for skills/babysit/scripts/babysit.py. Run: python3 tests/test_babysit.py (exit 0 = green)."""
import importlib.machinery
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "skills" / "babysit" / "scripts" / "babysit.py"
loader = importlib.machinery.SourceFileLoader("dev_babysit", str(path))
spec = importlib.util.spec_from_loader("dev_babysit", loader)
bs = importlib.util.module_from_spec(spec)
loader.exec_module(bs)


def thread(first_id, authors, resolved=False):
    nodes = [{"databaseId": first_id + i, "author": {"login": a}, "body": f"body {i}"} for i, a in enumerate(authors)]
    return {"id": f"T{first_id}", "isResolved": resolved, "isOutdated": False, "path": "src/a.py", "line": 7,
            "originalLine": 7, "comments": {"nodes": nodes}}


def test_build_items():
    threads = [thread(10, ["copilot"]), thread(20, ["alice", "me"]), thread(30, ["bob"], resolved=True)]
    reviews = [{"databaseId": 1, "author": {"login": "bob"}, "state": "COMMENTED", "body": ""},
               {"databaseId": 2, "author": {"login": "bob"}, "state": "CHANGES_REQUESTED", "body": "<!-- x -->Fix it"}]
    comments = [{"databaseId": 5, "author": None, "body": "hello"}]
    items, resolved = bs.build_items(threads, reviews, comments, {"me"})
    assert resolved == 1
    assert [i[0] for i in items] == ["c10", "c20", "rv2", "ic5"], items
    assert items[1][2].endswith("answered, not resolved")
    assert not items[0][2].endswith("answered, not resolved")
    assert items[2][3] == "Fix it"
    assert items[3][1] == "ghost"


def test_clean():
    body = "<!-- hidden --><details><summary><strong>What changed in this PR</strong></summary>table</details>" \
           "Real <b>point</b>\n\n\nnext​"
    assert bs.clean(body) == "Real point\nnext"


def test_ci_lines():
    rollup = {"contexts": {"nodes": [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "test", "conclusion": "FAILURE", "isRequired": True,
         "detailsUrl": "https://github.com/o/r/actions/runs/1/job/42"},
        {"context": "deploy", "state": "PENDING"},
    ]}}
    lines = bs.ci_lines(rollup)
    assert lines[0] == "ci       1 ok · 1 failed · 1 pending (deploy)", lines[0]
    assert "required test (failure) → gh run view --job 42 --log-failed" in lines[1], lines[1]
    assert lines[1].endswith("· run 1"), "the run id is what gh run rerun needs"
    assert bs.ci_lines(None) == ["ci       no checks"]


def test_copilot_line():
    review = {"author": {"login": "copilot-pull-request-reviewer"}, "commit": {"oid": "abc"}}
    assert bs.copilot_line([], [], "abc") is None
    assert bs.copilot_line([review], [], "abc").endswith("= head")
    assert "not reviewed since" in bs.copilot_line([review], [], "def")


def test_reviewer_reply_reopens_an_answered_thread():
    ours, seen = {"me"}, {"c10", "rv2"}
    answered, = bs.build_items([thread(10, ["alice", "me"])], [], [], ours)[0]
    replied_again, = bs.build_items([thread(10, ["alice", "me", "alice"])], [], [], ours)[0]
    assert bs.handled(answered, seen, ours)
    assert not bs.handled(replied_again, seen, ours), "a reply after ours goes back to triage"
    assert bs.handled(("rv2", "bob", "review", "x", []), seen, ours)
    assert not bs.handled(("c99", "alice", "a.py:1", "x", []), seen, ours)


def test_answer_rerun_and_new_reply():
    """A rerun after a partial failure skips what was posted; a reviewer's new reply can be answered again."""
    import contextlib
    import io
    import json
    import tempfile
    from types import SimpleNamespace

    posted, resolved, saved = [], [], {}
    t = thread(10, ["alice", "me"])  # the earlier run's reply went out, then the run died
    state = {"seen": {"c10", "ic5"}}

    def graphql(query, **kw):
        if query == bs.OURS:
            return {"viewer": {"login": "me"}, "repository": {"pullRequest": {"author": {"login": "me"}}}}
        resolved.append(kw["t"])

    bs.target = lambda pr: ("o", "r", 7)
    bs.graphql = graphql
    bs.gh = lambda *a, **kw: posted.append(a)
    bs.review_threads = lambda *a: [t]
    bs.load_state = lambda *a: ("state.json", state["seen"], set())
    bs.save_state = lambda f, seen, shown: saved.update(seen=set(seen))

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([{"id": "c10", "body": "Fixed in abc"}, {"id": "ic5", "body": "Done"}], f)
    args = SimpleNamespace(pr="7", file=f.name, dry_run=False)

    with contextlib.redirect_stdout(io.StringIO()) as out:
        bs.answer(args)
    assert "c10  already answered, skipped · resolved now" in out.getvalue(), out.getvalue()
    assert posted == [], posted
    assert resolved == ["T10"], "the reply was there but the thread still open: resolve only"

    t["comments"]["nodes"].append({"databaseId": 12, "author": {"login": "alice"}, "body": "Not fixed"})
    t["isResolved"], resolved[:] = False, []
    with contextlib.redirect_stdout(io.StringIO()):
        bs.answer(args)
    assert len(posted) == 1 and "replies" in posted[0][3], posted
    assert resolved == ["T10"]
    Path(f.name).unlink()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
