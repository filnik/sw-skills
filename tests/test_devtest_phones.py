#!/usr/bin/env python3
"""Packs, phones, leases and install receipts of device-test. Run: python3 tests/test_devtest_phones.py (exit 0 = green)."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ["DEVTEST_HOME"] = tempfile.mkdtemp()
os.environ["DEVTEST_PACK"] = "none"
os.environ["DEVTEST_WORKTREE"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "device-test" / "scripts"))

import devtest_common as C  # noqa: E402
import devtest_phones as Ph  # noqa: E402

UDID = "00008140-000000000000001E"


def raises(exc, fn, *args):
    try:
        fn(*args)
    except exc as e:
        return str(e)
    raise AssertionError("%s not raised" % exc.__name__)


def packs(**named):
    root = Path(tempfile.mkdtemp())
    for name, conf in named.items():
        (root / name).mkdir()
        (root / name / "pack.json").write_text(conf if isinstance(conf, str) else json.dumps(conf))
    return root


def test_the_only_pack_is_loaded_and_the_project_block_wins_key_by_key():
    root = packs(shop={"android": {"package": "com.example.shop", "db": "databases/shop.db"}, "default_env": "test"})
    folder, conf = C.load_pack({}, root)
    assert folder == root / "shop" and conf["android"]["package"] == "com.example.shop"
    folder, conf = C.load_pack({"android": {"db": "databases/other.db"}, "default_env": "staging"}, root)
    assert conf["android"] == {"package": "com.example.shop", "db": "databases/other.db"}
    assert conf["default_env"] == "staging"


def test_a_pack_is_chosen_by_name_and_two_packs_without_a_name_load_none():
    root = packs(shop={"default_env": "a"}, bank={"default_env": "b"})
    assert C.load_pack({}, root) == (None, {})
    assert C.load_pack({"pack": "bank"}, root) == (root / "bank", {"default_env": "b"})
    assert C.load_pack({"pack": "bank"}, root, named="shop")[0] == root / "shop"  # DEVTEST_PACK over the config
    assert C.load_pack({"pack": str(root / "shop")}, Path("/nowhere"))[0] == root / "shop"  # a path, not a name
    assert C.load_pack({"pack": "bank", "notifier": "say"}, root, named="none") == (None, {"notifier": "say"})
    assert "missing" in raises(C.Blocked, C.load_pack, {"pack": "missing"}, root)
    assert "broken" in raises(C.Blocked, C.load_pack, {}, packs(broken="{not json"))


def test_phones_as_objects_or_ids_and_resolve_by_alias_or_id():
    conf = {"phones": {"pixel": {"platform": "android", "id": "4A15SERIAL", "name": "Pixel 9"}, "iphone": UDID}}
    with mock.patch.object(C, "CONFIG", conf):
        devices, names = C._phones()
    assert devices == {"pixel": ("android", "4A15SERIAL"), "iphone": ("ios", UDID)}
    assert names == {"pixel": "Pixel 9", "iphone": "iphone"}
    with mock.patch.object(C, "DEVICES", devices), mock.patch.dict(os.environ, {"DEVTEST_DEVICE": ""}):
        assert C.resolve("pixel") == ("android", "4A15SERIAL", "pixel")
        assert C.resolve(UDID) == ("ios", UDID, "iphone")
        assert C.resolve("emulator-5554") == ("android", "emulator-5554", "emulator-5554")
        assert "pixel|iphone" in raises(C.Blocked, C.resolve, None)


def test_session_state_and_holder():
    d = Path(tempfile.mkdtemp())
    (d / "10.json").write_text(json.dumps({"status": "idle", "statusUpdatedAt": 1700000000000}))
    (d / "11.json").write_text(json.dumps({"status": "busy"}))
    (d / "30.json").write_text(json.dumps({"status": "busy"}))
    alive = lambda pid: pid != 12  # noqa: E731
    assert Ph.session_state({"pid": 10, "agent": "claude"}, alive, [d]) == ("idle", 1700000000.0)
    assert Ph.session_state({"pid": 11, "agent": "claude"}, alive, [d]) == ("running", None)
    assert Ph.session_state({"pid": 12, "agent": "codex"}, alive, [d]) == ("gone", None)
    assert Ph.session_state({"owner": "no holder"}, alive, [d]) == ("unknown", None)
    chain = {os.getppid(): (20, "/bin/zsh"), 20: (30, "zsh"), 30: (1, "claude")}
    assert Ph.holder(lambda pid: chain[pid], [d])["pid"] == 30
    chain[20] = (30, "/opt/homebrew/lib/codex/codex")
    h = Ph.holder(lambda pid: chain[pid], [d])
    assert (h["pid"], h["agent"]) == (20, "codex")


def wait(check, env="60s", every=120):
    t, sent, wanted = [0.0], [], Path(tempfile.mkdtemp())

    def sleep(s):
        t[0] += s
    with mock.patch.dict(os.environ, {"DEVTEST_PLUG_WAIT": env, "DEVTEST_OWNER": "LOGIN-1"}), \
            mock.patch.object(Ph, "WANTED", wanted), mock.patch.object(Ph, "NAMES", {"pixel": "Pixel 9"}), \
            mock.patch.object(Ph, "holder", lambda: {"cwd": "/x/shop-3", "iterm": "U"}), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            got = Ph.wait_connected("S1", "pixel", lambda i: check(t[0]), sent.append, sleep, lambda: t[0], every)
        except C.Blocked as e:
            got = e
    assert list(wanted.glob("*.json")) == [], "the request file is gone afterwards"
    return got, sent


def test_wait_connected_asks_to_plug_the_phone_in_and_gives_up_after_the_wait():
    got, sent = wait(lambda t: True)
    assert got is False and sent == []  # already connected: no notification
    got, sent = wait(lambda t: t >= 9)
    assert got is True and sent == ["Plug in the Pixel 9: LOGIN-1 is waiting for it · shop-3"]
    with mock.patch.object(C, "CONFIG", {"plug_in_text": "Attacca il %(phone)s (%(who)s)%(where)s"}):
        assert wait(lambda t: t >= 9)[1] == ["Attacca il Pixel 9 (LOGIN-1) · shop-3"]
    got, sent = wait(lambda t: False, every=30)
    assert isinstance(got, C.Blocked) and len(sent) == 2  # at 0 s and 30 s of 60 s
    got, sent = wait(lambda t: False, env="0")
    assert got is False and sent == []  # the wait is off: the command fails on its own


def test_leases_between_owners():
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"DEVTEST_OWNER": "a"}), \
            mock.patch.object(Ph, "holder", lambda: {"pid": 1, "agent": "claude"}):
        C.lease_acquire("S1", "pixel", "a", "30m", "LOGIN-1")
        C.check_lease("S1", "pixel")  # the owner itself passes
    lease = json.loads((C.LEASES / "S1.json").read_text())
    assert {"owner", "alias", "expires", "note", "since", "pid", "agent"} <= lease.keys(), lease  # the format other tools read
    with mock.patch.dict(os.environ, {"DEVTEST_OWNER": "b"}):
        assert "leased by a" in raises(C.Blocked, C.check_lease, "S1", "pixel")
        assert "leased by a" in raises(C.Blocked, C.lease_acquire, "S1", "pixel", "b", "30m", "")
        assert "--force" in raises(C.Blocked, C.lease_release, "S1", "pixel", "b", False)
        with contextlib.redirect_stdout(out):
            C.lease_release("S1", "pixel", "b", True)
        C.check_lease("S1", "pixel")
    assert C.parse_ttl("90m") == 5400 and C.parse_ttl("2h") == 7200 and C.parse_ttl("15") == 900


def test_install_receipt_says_when_the_build_is_stale():
    top = Path("/work/app")
    rec = {"worktree": "/work/app", "apk_mtime": 100.0, "build_started_at": 150.0, "commit": "abc"}
    assert C.stale_reason(rec, top, 140.0) == ""  # an up-to-date build keeps its old file: its start counts
    assert "older than the last change" in C.stale_reason(rec, top, 160.0)
    assert C.stale_reason({"worktree": "/work/app", "app_mtime": 200.0}, top, 160.0) == ""  # an iOS receipt
    assert "not of app" in C.stale_reason(dict(rec, worktree="/work/other"), top, 0)
    assert "run `deploy`" in C.stale_reason(None, top, 0)
    for path in ("app/src/test/FooTest.kt", "README.md", "iosAppTests/A.swift", ".claude/config/project.json"):
        assert C.NOT_IN_BUILD.search(path), path
    for path in ("app/src/main/Foo.kt", "iosApp/ContentView.swift", "contest/Main.kt"):
        assert not C.NOT_IN_BUILD.search(path), path


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
