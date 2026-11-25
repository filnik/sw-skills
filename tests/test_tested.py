#!/usr/bin/env python3
"""Tests for skills/tested/scripts/tested.py. Run: python3 tests/test_tested.py (exit 0 = green)."""
import importlib.util
import os
import tempfile
from pathlib import Path

os.environ["DEVTEST_HOME"] = tempfile.mkdtemp()
os.environ["DEVTEST_PACK"] = "none"
os.environ["DEVTEST_WORKTREE"] = tempfile.mkdtemp()
path = Path(__file__).resolve().parents[1] / "skills" / "tested" / "scripts" / "tested.py"
spec = importlib.util.spec_from_file_location("tested", path)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def test_source_sets_and_build_files_land_in_their_bucket_and_anything_else_is_off():
    rules = t.path_rules()
    assert t.bucket("core/src/commonMain/kotlin/A.kt", rules) == "both"
    assert t.bucket("core/src/androidMain/kotlin/A.kt", rules) == "android"
    assert t.bucket("core/src/iosArm64Main/kotlin/A.kt", rules) == "ios"
    assert t.bucket("app/App.swift", rules) == "ios"
    assert t.bucket("app/build.gradle.kts", rules) == "build"
    assert t.bucket("legacy/src/main/A.kt", rules) == "off"


def test_project_paths_add_to_a_bucket_and_tests_stay_off():
    rules = t.path_rules({"android": [r"^legacy/"], "off": [r"^tools/"]})
    assert t.bucket("legacy/src/main/A.kt", rules) == "android"
    assert t.bucket("legacy/src/test/ATest.kt", rules) == "off"
    assert t.bucket("tools/src/commonMain/A.kt", rules) == "off"
    assert t.bucket("core/src/commonMain/A.kt", rules) == "both"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
