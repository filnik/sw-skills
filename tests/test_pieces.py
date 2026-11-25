#!/usr/bin/env python3
"""Tests for the "project pieces" command the skills run. Run: python3 tests/test_pieces.py (exit 0 = green).

Each skill that takes pieces prints its own section of the project's playbook with one shell line in its SKILL.md;
these tests run that exact line against a sample playbook.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"
PIECES = {"pick": ["gates", "pr-shaping"], "ci-why": ["known-causes"], "crashes": ["gates"], "my-prs": ["base-lag"]}
BOOK = """# Team playbook

Intro, read by no skill.

## pick

Shared text.

### gates
Replaces the default.
- the issue must be estimated

## pickle

Not pick's.

## my-prs-extra

Not my-prs's either.

## ci-why
### known-causes
- a runner named bob: rerun
"""


def command(skill):
    text = (SKILLS / skill / "SKILL.md").read_text()
    lines = [l for l in text.splitlines() if '/shared/project_config.py" section ' in l]
    assert len(lines) == 1, f"{skill}: one pieces command expected, found {len(lines)}"
    return lines[0]


def run(skill, cwd):
    env = dict(os.environ, CLAUDE_SKILL_DIR=str(SKILLS / skill))
    p = subprocess.run(["bash", "-c", command(skill)], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"{skill}: exit {p.returncode} {p.stderr}"
    return p.stdout


def test_each_skill_prints_only_its_own_section_and_nothing_without_a_playbook():
    tmp = Path(tempfile.mkdtemp()).resolve()
    try:
        bare = tmp / "bare"
        bare.mkdir()
        project = tmp / "project"
        (project / ".claude/config").mkdir(parents=True)
        (project / ".claude/config/project.json").write_text(json.dumps({"playbook": "docs/book.md"}))
        (project / "docs").mkdir()
        (project / "docs/book.md").write_text(BOOK)
        (project / "src").mkdir()
        assert run("pick", project / "src") == \
            "## pick\n\nShared text.\n\n### gates\nReplaces the default.\n- the issue must be estimated\n"
        assert run("ci-why", project) == "## ci-why\n### known-causes\n- a runner named bob: rerun\n"
        assert run("my-prs", project) == ""
        assert run("crashes", project) == ""
        for skill in PIECES:
            assert run(skill, bare) == "", skill
    finally:
        shutil.rmtree(tmp)


def test_each_skill_names_its_section_and_marks_the_default_of_each_piece():
    for skill, pieces in PIECES.items():
        assert command(skill).endswith(f" section {skill}"), skill
        text = (SKILLS / skill / "SKILL.md").read_text()
        for piece in pieces:
            assert text.count(f"Piece `{piece}`.") == 1, f"{skill}: the default of {piece} is not marked once"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
