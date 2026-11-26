"""The pull request of the tested branch: its device-test section and the template boxes of the phones tested.

    devtest pr                                    the section as it is now, or "none"
    devtest pr --file NOTE.md --phones a,b        NOTE.md becomes the section; the phones' boxes get ticked

Config, `pr` in the pack's pack.json or in the project's device_test block:
    {"pr": {"section": "**On device.**", "boxes": {"pixel": ["Android", "Android device"]}}}
`section` is the text that starts the section (default "### Device tests"); `boxes` lists per phone alias the labels
of the PR template's checkboxes to tick. Boxes are only ever ticked, never cleared.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from devtest_common import Blocked, cfg, short

START, END = "<!-- device-test -->", "<!-- /device-test -->"
# What ends a section written before the markers existed: a heading, a rule or the next bold paragraph label.
NEXT = re.compile(r"^(#|---|\*\*)")


def _span(lines: list, label: str):
    """(first, after last) line of the section, or None."""
    marks = [i for i, line in enumerate(lines) if line.strip() in (START, END)]
    if len(marks) >= 2 and lines[marks[0]].strip() == START and lines[marks[1]].strip() == END:
        return marks[0], marks[1] + 1
    start = next((i for i, line in enumerate(lines) if line.startswith(label)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if NEXT.match(lines[i])), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def section(body: str, label: str):
    span = _span(body.splitlines(), label)
    if not span:
        return None
    return "\n".join(x for x in body.splitlines()[span[0]:span[1]] if x.strip() not in (START, END)).strip()


def update_body(body: str, label: str, note: str, labels: list) -> tuple:
    """(new body, "replaced" or "added", box labels ticked now, box labels with no box in the body)."""
    lines = body.splitlines()
    text = label + ("\n\n" if label.startswith("#") else " ") + note.strip()
    block = [START, "", *text.splitlines(), "", END]
    span = _span(lines, label)
    if span:
        how, (s, e) = "replaced", span
    else:  # before the last rule (the template's checklist), else at the end
        rules = [i for i, line in enumerate(lines) if line.strip() == "---"]
        how, s = "added", rules[-1] if rules else len(lines)
        e = s
        block = ([""] if s and lines[s - 1].strip() else []) + block + ([""] if rules else [])
    lines[s:e] = block
    ticked, missing = [], []
    for box in dict.fromkeys(labels):
        rx = re.compile(r"^\s*[-*+] \[([ xX])\] %s\s*$" % re.escape(box))
        hits = [(i, m) for i, m in ((i, rx.match(line)) for i, line in enumerate(lines)) if m]
        if not hits:
            missing.append(box)
        for i, m in hits:
            if m.group(1) == " ":
                lines[i] = lines[i][:m.start(1)] + "x" + lines[i][m.end(1):]
                ticked.append(box)
    return "\n".join(lines) + "\n", how, list(dict.fromkeys(ticked)), missing


def pr(top: Path, phones: list, note_file) -> int:
    label = cfg("pr.section") or "### Device tests"

    def gh(*args):
        return subprocess.run(["gh", *args], cwd=top, capture_output=True, text=True)

    r = gh("pr", "view", "--json", "number,state,body")
    if r.returncode:
        if "no pull requests found" in r.stderr:
            print("no PR for this branch: nothing to update")
            return 0
        raise Blocked("gh pr view: %s" % short(r.stderr.strip()))
    info = json.loads(r.stdout)
    if info["state"] != "OPEN":
        print("PR #%d is %s: not updated" % (info["number"], info["state"].lower()))
        return 0
    if not note_file:
        print(section(info["body"], label) or "none")
        return 0
    boxes = cfg("pr.boxes") or {}
    body, how, ticked, missing = update_body(info["body"], label, Path(note_file).read_text(),
                                             [b for p in phones for b in boxes.get(p, [])])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
    r = gh("pr", "edit", str(info["number"]), "--body-file", f.name)
    Path(f.name).unlink()
    if r.returncode:
        raise Blocked("gh pr edit: %s" % short(r.stderr.strip()))
    notes = ["no box configured for %s (pr.boxes)" % ", ".join(p for p in phones if p not in boxes)] \
        if any(p not in boxes for p in phones) else []
    notes += ["no such box in the PR: %s" % ", ".join(missing)] if missing else []
    print("PR #%d: section %s · ticked %s%s" % (info["number"], how, ", ".join(ticked) or "nothing new",
                                                "".join(" · " + n for n in notes)))
    return 0
