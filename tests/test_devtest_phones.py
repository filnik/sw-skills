#!/usr/bin/env python3
"""Phones, leases and config of skills/device-test: session state, the plug-in wait, leases between owners, aliases,
the project config walk-up and the stale-build check. Run: python3 tests/test_devtest_phones.py (exit 0 = green)."""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
os.environ["DEVTEST_HOME"] = tempfile.mkdtemp(prefix="devtest-home-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "device-test" / "scripts"))

import devtest_common as C  # noqa: E402
import devtest_phones as Ph  # noqa: E402


def raises(exc, fn, *args):
    try:
        fn(*args)
    except exc as e:
        return str(e)
    raise AssertionError("%s not raised" % exc.__name__)


def test_session_state_and_holder():
    d = Path(tempfile.mkdtemp())
    (d / "10.json").write_text(json.dumps({"status": "idle"}))
    (d / "11.json").write_text(json.dumps({"status": "busy"}))
    alive = lambda pid: pid != 12  # noqa: E731
    assert Ph.session_state({"pid": 10}, alive, [d]) == "idle"
    assert Ph.session_state({"pid": 11}, alive, [d]) == "running"
    assert Ph.session_state({"pid": 12}, alive, [d]) == "gone"
    assert Ph.session_state({"owner": "no holder"}, alive, [d]) == "unknown"
    chain = {os.getppid(): (20, "zsh"), 20: (11, "node"), 11: (1, "claude")}
    assert Ph.holder(lambda pid: chain[pid], [d])["pid"] == 11


def wait(check, env="60s", every=120):
    t, sent = [0.0], []

    def sleep(s):
        t[0] += s
    with mock.patch.dict(os.environ, {"DEVTEST_PLUG_WAIT": env, "DEVTEST_OWNER": "LOGIN-1"}), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            got = Ph.wait_connected("S1", "pixel", lambda i: check(t[0]), sent.append, sleep, lambda: t[0], every)
        except C.Blocked as e:
            got = e
    return got, sent


def test_wait_connected():
    got, sent = wait(lambda t: True)
    assert got is False and sent == []  # already connected: no notification
    got, sent = wait(lambda t: t >= 9)
    assert got is True and sent == ["Plug in pixel: LOGIN-1 is waiting for it"]
    got, sent = wait(lambda t: False, every=30)
    assert isinstance(got, C.Blocked) and len(sent) == 2  # at 0 s and 30 s of 60 s
    got, sent = wait(lambda t: False, env="0")
    assert got is False and sent == []  # the wait is off: the command fails on its own


def test_leases_between_owners():
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"DEVTEST_OWNER": "a"}):
        C.lease_acquire("S1", "pixel", "a", "30m", "LOGIN-1")
        C.check_lease("S1", "pixel")  # the owner itself passes
    with mock.patch.dict(os.environ, {"DEVTEST_OWNER": "b"}):
        assert "leased by a" in raises(C.Blocked, C.check_lease, "S1", "pixel")
        assert "leased by a" in raises(C.Blocked, C.lease_acquire, "S1", "pixel", "b", "30m", "")
        assert "--force" in raises(C.Blocked, C.lease_release, "S1", "pixel", "b", False)
        with contextlib.redirect_stdout(out):
            C.lease_release("S1", "pixel", "b", True)
        C.check_lease("S1", "pixel")
    assert C.parse_ttl("90m") == 5400 and C.parse_ttl("2h") == 7200 and C.parse_ttl("15") == 900


def test_config_aliases_and_walk_up():
    root = Path(tempfile.mkdtemp())
    (root / ".claude" / "config").mkdir(parents=True)
    cfg = {"android": {"package": "com.example.app"},
           "phones": {"pixel": "4A15SERIAL", "iphone": "00008140-001A5D801469401C"}}
    (root / ".claude" / "config" / "project.json").write_text(json.dumps({"device_test": cfg, "other": {}}))
    (root / "app" / "src").mkdir(parents=True)
    found_root, found = C.project_config(root / "app" / "src")
    assert found_root == root.resolve() and found == cfg
    phones = found["phones"]
    assert C.resolve("pixel", phones) == ("android", "4A15SERIAL", "pixel")
    assert C.resolve("00008140-001A5D801469401C", phones) == ("ios", "00008140-001A5D801469401C", "iphone")
    assert C.resolve("emulator-5554", phones) == ("android", "emulator-5554", "emulator-5554")
    assert C.default_phone("ios", phones) == "iphone"
    assert "device_test.ios.bundle_id" in raises(C.Blocked, C.need, cfg, "ios", "bundle_id")
    with mock.patch.dict(os.environ, {"DEVTEST_DEVICE": ""}):
        assert "pixel, iphone" in raises(C.Blocked, C.resolve, None, phones)


def test_stale_build():
    top = Path("/work/app")
    rec = {"worktree": "/work/app", "artifact_mtime": 100.0, "build_started": 150.0, "commit": "abc"}
    assert C.stale_reason(rec, top, 140.0) == ""  # an up-to-date build: its start counts
    assert "older than the last change" in C.stale_reason(rec, top, 160.0)
    assert "not of app" in C.stale_reason(dict(rec, worktree="/work/other"), top, 0)
    assert "run `deploy`" in C.stale_reason(None, top, 0)
    for path in ("device-tests/login.py", "app/src/test/FooTest.kt", "README.md", "iosAppTests/A.swift",
                 ".claude/config/project.json"):
        assert C.NOT_IN_BUILD.search(path), path
    for path in ("app/src/main/Foo.kt", "iosApp/ContentView.swift", "contest/Main.kt"):
        assert not C.NOT_IN_BUILD.search(path), path


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
