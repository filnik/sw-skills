#!/usr/bin/env python3
"""Pure-logic tests for skills/pick/scripts/clickup.py. Run: python3 tests/test_clickup.py (exit 0 = green)."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "skills" / "pick" / "scripts" / "clickup.py"
spec = importlib.util.spec_from_file_location("clickup", path)
cu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cu)


def test_parse_ref():
    assert cu.parse_ref("https://app.clickup.com/t/86c7e4q8r") == ("86c7e4q8r", {})
    assert cu.parse_ref("86c7e4q8r") == ("86c7e4q8r", {})
    assert cu.parse_ref("https://app.clickup.com/t/9012345/DEV-123") == \
        ("DEV-123", {"custom_task_ids": "true", "team_id": "9012345"})
    assert cu.parse_ref("DEV-123", workspace_id="777") == ("DEV-123", {"custom_task_ids": "true", "team_id": "777"})
    try:
        cu.parse_ref("DEV-123")
    except ValueError as e:
        assert "workspace_id" in str(e)
    else:
        raise AssertionError("a custom id without a workspace must fail")


def test_in_progress_status():
    statuses = [
        {"status": "complete", "type": "closed", "orderindex": 3},
        {"status": "to do", "type": "open", "orderindex": 0},
        {"status": "in review", "type": "custom", "orderindex": 2},
        {"status": "in progress", "type": "custom", "orderindex": 1},
    ]
    assert cu.in_progress_status(statuses) == "in progress"
    assert cu.in_progress_status([{"status": "doing", "type": "custom", "orderindex": "5"}]) == "doing"
    assert cu.in_progress_status([{"status": "to do", "type": "open"}]) is None


def test_format_task():
    task = {"id": "abc", "custom_id": "DEV-1", "name": "Fix cart", "status": {"status": "to do"},
            "priority": {"priority": "high"}, "tags": [{"name": "bug"}], "assignees": [{"username": "ann"}],
            "list": {"id": "9", "name": "Sprint"}, "url": "https://app.clickup.com/t/abc",
            "markdown_description": "Steps\n1. open", "attachments": [{"id": "a1", "title": "shot.png",
                                                                       "mimetype": "image/png", "url": "https://x/y"}]}
    comments = [{"date": "1730000000000", "user": {"username": "bob"}, "comment_text": "second"},
                {"date": "1720000000000", "user": {"username": "ann"}, "comment_text": "first"}]
    out = cu.format_task(task, comments, {"a1": "/tmp/01-shot.png"})
    assert "task      abc (DEV-1) · Fix cart" in out
    assert "priority high · tags bug" in out
    assert "- shot.png (image/png) → /tmp/01-shot.png" in out
    assert out.index("ann, 2024-07-03: first") < out.index("bob, 2024-10-27: second"), out
    assert cu.task_line(task) == "abc · to do · Fix cart #bug · ann"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
