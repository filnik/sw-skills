#!/usr/bin/env python3
"""Pure-logic tests for skills/release-notes/scripts/release_prs.py. Run: python3 tests/test_release_notes.py."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "skills" / "release-notes" / "scripts" / "release_prs.py"
spec = importlib.util.spec_from_file_location("release_prs", path)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def test_pr_number():
    assert rp.pr_number("Merge pull request #42 from ann/fix-cart") == 42
    assert rp.pr_number("Fix the cart total (#118)") == 118
    assert rp.pr_number("Fix #12 in the parser") is None
    assert rp.pr_number("Bump version") is None


def test_kind():
    assert rp.kind("feat!: drop Node 16", [], "", "ann") == "breaking"
    assert rp.kind("Rework config", ["breaking-change"], "", "ann") == "breaking"
    assert rp.kind("Rework config", [], "BREAKING CHANGE: the key moved", "ann") == "breaking"
    assert rp.kind("fix(cart): keep totals", [], "", "ann") == "fix"
    assert rp.kind("Keep totals", ["bug"], "", "ann") == "fix"
    assert rp.kind("Dark mode", ["enhancement"], "", "ann") == "feature"
    assert rp.kind("perf: faster search", [], "", "ann") == "feature"
    assert rp.kind("Bump lodash", [], "", "dependabot[bot]") == "internal"
    assert rp.kind("ci: cache gradle", [], "", "ann") == "internal"
    assert rp.kind("Update the onboarding copy", [], "", "ann") == "other"


def test_excerpt():
    body = "<!-- template -->\n## Why\n\nUsers lost their cart.\n![shot](x.png)\n- [ ] tests\n- [x] docs\n"
    assert rp.excerpt(body) == "Users lost their cart. - [x] docs"
    assert rp.excerpt(None) == ""
    long = rp.excerpt("word " * 200)
    assert long.endswith(" …") and len(long) <= rp.EXCERPT + 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
