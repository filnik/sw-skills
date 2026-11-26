#!/usr/bin/env python3
"""The PR section and boxes of devtest pr, without gh. Run: python3 tests/test_devtest_pr.py (exit 0 = green)."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DEVTEST_HOME"] = tempfile.mkdtemp()
os.environ["DEVTEST_PACK"] = "none"
os.environ["DEVTEST_WORKTREE"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "device-test" / "scripts"))

import devtest_pr as R  # noqa: E402

LABEL = "**On device.**"
BODY = """# Details

**Tests.** Unit tests passed.

**On device.** Pending on both phones.

---

# Author Checklist

- [ ] I have tested the build on BOTH platforms (check all that apply):
    - [ ] iOS
        - [ ] iOS 18 device
        - [ ] iOS 26 device
    - [x] Android
        - [ ] Android device
"""


def test_the_placeholder_paragraph_is_replaced_then_the_marked_section_and_boxes_are_only_ever_ticked():
    body, how, ticked, missing = R.update_body(BODY, LABEL, "Ran on Sandbox.\n\n- **Pixel 9:** C1 PASS.",
                                               ["Android", "Android device", "Tablet"])
    assert how == "replaced" and "Pending" not in body and "**Tests.** Unit tests passed." in body
    assert ticked == ["Android device"] and missing == ["Tablet"]
    assert "- [ ] iOS 26 device" in body and "        - [x] Android device" in body
    assert R.section(body, LABEL) == "**On device.** Ran on Sandbox.\n\n- **Pixel 9:** C1 PASS."
    again, how, ticked, _ = R.update_body(body, LABEL, "Ran again.", ["iOS", "iOS 26 device"])
    assert how == "replaced" and R.section(again, LABEL) == "**On device.** Ran again."
    assert ticked == ["iOS", "iOS 26 device"] and "[x] Android device" in again
    assert again.count(R.START) == 1 and again.index(R.END) < again.index("# Author Checklist")


def test_with_no_section_it_goes_before_the_checklist_rule_or_at_the_end():
    body, how, _, _ = R.update_body(BODY.replace("**On device.** Pending on both phones.\n\n", ""), LABEL, "Ran.", [])
    assert how == "added" and body.index("**On device.** Ran.") < body.index("---")
    body, how, _, _ = R.update_body("Some text.", "### Device tests", "Ran.", [])
    assert how == "added" and body.endswith("### Device tests\n\nRan.\n\n%s\n" % R.END)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
