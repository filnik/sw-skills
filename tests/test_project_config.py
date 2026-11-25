#!/usr/bin/env python3
"""Tests for shared/project_config.py. Run: python3 tests/test_project_config.py (exit 0 = green)."""
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "shared" / "project_config.py"
spec = importlib.util.spec_from_file_location("project_config", path)
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


def test_a_nested_claude_folder_without_config_and_the_parent_config_is_found():
    tmp = Path(tempfile.mkdtemp()).resolve()
    try:
        (tmp / ".claude/config").mkdir(parents=True)
        (tmp / ".claude/config/project.json").write_text(json.dumps({"locale": "en", "playbook": "book.md"}))
        (tmp / "book.md").write_text("# book")
        repo = tmp / "repo"
        (repo / ".claude").mkdir(parents=True)  # the repo's own .claude/, no config in it
        (repo / "src").mkdir()
        found, cfg = pc.find(repo / "src")
        assert found == tmp / ".claude/config/project.json", found
        assert cfg["locale"] == "en"
        assert pc.playbook(repo / "src") == tmp / "book.md"
    finally:
        shutil.rmtree(tmp)


def test_a_repo_config_adds_to_the_parent_config_and_wins_on_shared_keys():
    tmp = Path(tempfile.mkdtemp()).resolve()
    try:
        (tmp / ".claude/config").mkdir(parents=True)
        (tmp / ".claude/config/project.json").write_text(
            json.dumps({"locale": "it", "playbook": "book.md", "babysit": {"required": ["CI"]}}))
        (tmp / "book.md").write_text("# book")
        repo = tmp / "kb"
        (repo / ".claude/config").mkdir(parents=True)
        (repo / ".claude/config/project.json").write_text(json.dumps({"locale": "en", "knowledge_base": {"root": "."}}))
        found, cfg = pc.find(repo)
        assert found == repo / ".claude/config/project.json", found
        assert cfg["locale"] == "en" and cfg["babysit"] == {"required": ["CI"]} and "knowledge_base" in cfg, cfg
        assert pc.playbook(repo) == tmp / "book.md"
    finally:
        shutil.rmtree(tmp)


def test_section_returns_only_the_named_section_and_its_pieces_and_nothing_without_a_playbook():
    tmp = Path(tempfile.mkdtemp()).resolve()
    try:
        (tmp / ".claude/config").mkdir(parents=True)
        (tmp / ".claude/config/project.json").write_text(json.dumps({"playbook": "book.md"}))
        (tmp / "book.md").write_text(
            "# book\n\n## pick\n\nShared.\n\n### gates\n- estimated\n\n## pickle\n\nnot pick\n\n## tested\n- galaxy\n")
        assert pc.section("pick", tmp) == "## pick\n\nShared.\n\n### gates\n- estimated"
        assert pc.section("tested", tmp) == "## tested\n- galaxy"
        assert pc.section("babysit", tmp) == ""
        assert pc.section("pick", Path("/")) == ""  # no config, so no playbook
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
