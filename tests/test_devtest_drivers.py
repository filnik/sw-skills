#!/usr/bin/env python3
"""The device-test drivers, codecs, guards and generic flows without a phone or a pack.
Run: python3 tests/test_devtest_drivers.py (exit 0 = green)."""

import os
import plistlib
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["DEVTEST_HOME"] = tempfile.mkdtemp()
os.environ["DEVTEST_PACK"] = "none"
os.environ["DEVTEST_WORKTREE"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "device-test" / "scripts"))

import devtest_android as A  # noqa: E402
import devtest_common as C  # noqa: E402
import devtest_guard as G  # noqa: E402
import devtest_ios as I  # noqa: E402
from flows import pair, phone  # noqa: E402

DUMP = """<hierarchy rotation="0">
<node class="android.widget.FrameLayout" resource-id="screen.settings" bounds="[0,0][1080,2400]" enabled="true">
  <node class="android.view.View" resource-id="settings.wifi_only" bounds="[0,200][1080,320]" enabled="true">
    <node class="android.widget.TextView" text="Upload on Wi-Fi only" bounds="[40,220][800,300]" enabled="true"/>
    <node class="android.widget.Switch" checkable="true" checked="true" bounds="[900,220][1040,300]" enabled="true"/>
  </node>
  <node class="android.widget.Button" resource-id="form.save" text="Save" bounds="[0,400][1080,500]" enabled="false"/>
  <node class="android.widget.ImageView" content-desc="Back" bounds="[0,40][100,140]" enabled="true"/>
</node></hierarchy>"""


def test_android_nodes_and_selectors_and_listing():
    nodes = [A.Node(el, i) for i, el in enumerate(ET.fromstring(DUMP).iter("node"))]
    by = {n.id or n.desc or n.text: n for n in nodes}
    assert by["settings.wifi_only"].checked, "the state of a switch row is read from its checkable child"
    assert "Upload on Wi-Fi only" in by["settings.wifi_only"].subtree_text()
    match = A.Android.matches
    assert match(by["form.save"], "form.save") and match(by["form.save"], "text:Save")
    assert match(by["Back"], "desc:Back") and not match(by["Back"], "text:Back")
    assert match(by["Upload on Wi-Fi only"], "text~wi-fi")
    assert not match(by["Upload on Wi-Fi only"], "text~xpload"), "text~ keeps the first character of the wanted text"
    dev = A.Android.__new__(A.Android)
    dev.dump = lambda: nodes
    lines = dev.src()
    assert lines[0] == "SCREENS: screen.settings"
    assert any("settings.wifi_only" in l and "[on]" in l for l in lines)
    assert any("Save" in l and "[off]" in l for l in lines)
    assert dev.src("save")[1:] == [l for l in lines[1:] if "save" in l.lower()]


def test_ios_locators():
    assert I.IOS.query("login.submit") == ("accessibility id", "login.submit")
    assert I.IOS.query("text:Sign in") == ("predicate string", "label == 'Sign in' OR value == 'Sign in'")
    assert I.IOS.query("text~it's") == ("predicate string", "label CONTAINS[c] 'it\\'s' OR value CONTAINS[c] 'it\\'s'")


def test_prefs_codec_reads_and_writes_datastore_and_plist():
    buf = C.encode_string_pref(b"", "token", "abc")
    buf = C.encode_string_pref(buf, "name", "Ann")
    assert C.decode_prefs(buf) == {"token": "abc", "name": "Ann"}
    assert C.decode_prefs(C.encode_string_pref(buf, "token", None)) == {"name": "Ann"}
    assert C.decode_prefs(C.encode_string_pref(buf, "name", "Bob")) == {"token": "abc", "name": "Bob"}
    for fmt in (plistlib.FMT_BINARY, plistlib.FMT_XML):
        plist = plistlib.dumps({"count": 3, "name": "Ann"}, fmt=fmt)
        out = C.encode_string_pref(plist, "name", "Bob")
        assert out[:6] == plist[:6]  # the format is kept
        assert C.decode_prefs(out) == {"count": 3, "name": "Bob"}
        assert C.decode_prefs(C.encode_string_pref(out, "count", None)) == {"name": "Bob"}


def test_crash_verdict():
    C.crash_verdict(["I/app: fine"], alive=True)
    for lines, alive, kind in ((["E AndroidRuntime: FATAL EXCEPTION: main"], True, C.Fail),
                               (["I/app: fine"], False, C.Fail),
                               (["java.lang.IllegalStateException: A migration from 9 to 8 was required"], True,
                                C.Blocked)):
        try:
            C.crash_verdict(lines, alive)
            raise AssertionError("no verdict for %s" % lines)
        except kind:
            pass


def test_launch_rules_put_the_platform_block_over_the_shared_one():
    launch = {"login_screen": "screen.login", "dismiss": [{"tap": "push.later"}],
              "android": {"dismiss": [{"tap": "text:Not now", "texts": ["Battery"]}], "login_hint": "by hand"}}
    with mock.patch.object(C, "CONFIG", {"launch": launch, "android": {"package": "com.example.app"}}):
        assert C.launch_rules("ios") == {"login_screen": "screen.login", "dismiss": [{"tap": "push.later"}]}
        android = C.launch_rules("android")
        assert android["dismiss"] == [{"tap": "text:Not now", "texts": ["Battery"]}]
        assert (android["login_screen"], android["login_hint"]) == ("screen.login", "by hand")
        assert C.app_id("android") == "com.example.app"
        try:
            C.app_id("ios")
            raise AssertionError("a missing key must be BLOCKED")
        except C.Blocked as e:
            assert "ios.bundle_id" in str(e)


def test_lint_finds_undefined_names_and_copied_helpers():
    tests = Path(tempfile.mkdtemp())
    helper = 'def open_cart(d):\n    d.tap("cart.open")\n    d.expect_screen("screen.cart")\n'
    (tests / "a.py").write_text("from devtest_scenario import Scenario\nfrom flows import phone\n" + helper +
                                'def settle(d):\n    pass\nS = Scenario("x")\n@S.step("go")\ndef _(d):\n'
                                "    open_cart(d)\n    frobnicate(d)\n")
    (tests / "b.py").write_text("from flows import phone\n" + helper)
    with mock.patch.object(G, "SCENARIOS", tests):
        problems = "\n".join(G.lint([tests / "a.py"])[0])
    assert "'frobnicate' is used but never defined" in problems, problems
    assert "settle() exists in flows as phone.settle" in problems, problems
    assert "open_cart() has the same body as b.py:open_cart" in problems, problems
    assert G.scenario_tags(tests / "a.py") == {"a.py:4": "cart.open", "a.py:5": "screen.cart"}


def test_ios_log_records_glued_on_one_line_are_split_and_wait_line_finds_the_first():
    log = Path(tempfile.mkdtemp()) / "console.log"
    stamp = "2026-11-25 10:00:0%d.000000+0100 App[12:34] %s"
    log.write_text(stamp % (1, "upload started") + stamp % (2, "upload done") + "\nplain line\n")
    x = SimpleNamespace(platform="ios", dev=SimpleNamespace(log=log))
    mark = SimpleNamespace(offset=0)
    assert [l[-11:] for l in phone.logged(x, "upload", mark)] == ["oad started", "upload done"]
    assert phone.wait_line(x, "done", mark, 1).endswith("upload done")
    assert phone.wait_line(x, "never", mark, 0.1) is None
    x.platform = "android"
    assert len(phone.logged(x, "upload", mark)) == 1


def test_pair_partners_come_from_the_pack_or_are_the_other_platform():
    phones = {"pixel": ("android", "S1"), "tablet": ("android", "S2"), "iphone": ("ios", "U1")}
    with mock.patch.object(pair, "DEVICES", phones):
        assert pair._partners("ios") == ("pixel", "tablet") and pair._partners("android") == ("iphone",)
        with mock.patch.object(C, "CONFIG", {"pair_partners": {"ios": ["tablet"]}}):
            assert pair._partners("ios") == ("tablet",) and pair._partners("android") == ("iphone",)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
