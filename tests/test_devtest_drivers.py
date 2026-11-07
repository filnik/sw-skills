#!/usr/bin/env python3
"""The phone-free parts of the device-test drivers: Android UI dump parsing and selectors, iOS locators and source
listing, the prefs codec (DataStore and plist), crash lines, the per-iPhone port, and the guards (tags check, lint,
flows listing). Run: python3 tests/test_devtest_drivers.py (exit 0 = green)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["DEVTEST_HOME"] = tempfile.mkdtemp(prefix="devtest-home-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "device-test" / "scripts"))

import devtest_android as A  # noqa: E402
import devtest_common as C  # noqa: E402
import devtest_guard as G  # noqa: E402
import devtest_ios as I  # noqa: E402

DUMP = """noise before
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node class="android.widget.FrameLayout" resource-id="screen.settings" bounds="[0,0][1080,2400]" enabled="true">
  <node class="android.view.View" resource-id="settings.wifi_only" bounds="[0,200][1080,320]" enabled="true">
    <node class="android.widget.TextView" text="Upload on Wi-Fi only" bounds="[40,220][800,300]" enabled="true"/>
    <node class="android.widget.Switch" checkable="true" checked="true" bounds="[900,220][1040,300]" enabled="true"/>
  </node>
  <node class="android.widget.Button" resource-id="com.example.app:id/save" text="Save" bounds="[0,400][1080,500]"
        enabled="false"/>
  <node class="android.widget.ImageView" content-desc="Back" bounds="[0,40][100,140]" enabled="true"/>
</node></hierarchy>
UI hierchary dumped to: /dev/tty"""


def test_android_dump_selectors_and_listing():
    nodes = A.parse_dump(DUMP)
    by = {n.id or n.desc or n.text: n for n in nodes}
    assert by["settings.wifi_only"].checked, "the state of a switch row is read from its checkable child"
    assert "Upload on Wi-Fi only" in by["settings.wifi_only"].subtree_text()
    assert A.matches(by["com.example.app:id/save"], "save")  # a View id matches without its package
    assert A.matches(by["Back"], "desc:Back") and A.matches(by["Back"], "desc~bac")
    assert A.matches(by["Upload on Wi-Fi only"], "text~wi-fi") and not A.matches(by["Back"], "text:Back")
    lines = A.src_lines(nodes)
    assert lines[0] == "SCREENS: screen.settings"
    assert any("settings.wifi_only" in l and "[on]" in l for l in lines)
    assert any("Save" in l and "[off]" in l for l in lines)
    assert A.src_lines(nodes, "save")[1:] == [l for l in lines[1:] if "save" in l.lower()]
    assert A.parse_dump("ERROR: could not get idle state") == []


def test_ios_locators_and_listing():
    assert I.query("login.submit") == ("accessibility id", "login.submit")
    assert I.query("text:Sign in") == ("predicate string", "label == 'Sign in' OR value == 'Sign in'")
    assert I.query("text~it's") == ("predicate string", "label CONTAINS[c] 'it\\'s' OR value CONTAINS[c] 'it\\'s'")
    xml = """<?xml version="1.0"?><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App">
<XCUIElementTypeOther type="XCUIElementTypeOther" name="screen.login" visible="true" x="0" y="0" width="390" height="844"/>
<XCUIElementTypeButton type="XCUIElementTypeButton" name="login.submit" label="Sign in" visible="true" x="20" y="700"
 width="350" height="50"/>
<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Hidden" visible="false" x="0" y="0" width="1"
 height="1"/></XCUIElementTypeApplication>"""
    assert I.ui_src_lines(xml) == ["SCREENS: screen.login", "Button     (195,725) login.submit | Sign in"]
    udid = "00008140-001A5D801469401C"
    assert I.local_port(udid) == I.local_port(udid.lower()) and 18100 <= I.local_port(udid) < 18200


def test_prefs_codec_datastore_and_plist():
    buf = C.encode_pref(b"", "token", "abc")
    buf = C.encode_pref(buf, "name", "Ann")
    assert C.decode_prefs(buf) == {"token": "abc", "name": "Ann"}
    assert C.decode_prefs(C.encode_pref(buf, "token", None)) == {"name": "Ann"}
    assert C.decode_prefs(C.encode_pref(buf, "name", "Bob")) == {"token": "abc", "name": "Bob"}
    import plistlib
    for fmt in (plistlib.FMT_BINARY, plistlib.FMT_XML):
        plist = plistlib.dumps({"count": 3, "name": "Ann"}, fmt=fmt)
        out = C.encode_pref(plist, "name", "Bob")
        assert out[:6] == plist[:6]  # the format is kept
        assert C.decode_prefs(out) == {"count": 3, "name": "Bob"}
        assert C.decode_prefs(C.encode_pref(out, "count", None)) == {"name": "Bob"}
    assert C.format_prefs({"name": "Ann", "flags": ["a"]}, "fla") == ['flags = ["a"]']


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


def project_with_scenarios():
    top = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(top)], check=True)
    (top / "app").mkdir()
    (top / "app" / "Login.kt").write_text('Modifier.testTag("login.submit")\nval t = "home." + name\n')
    tests = top / "device-tests"
    (tests / "flows").mkdir(parents=True)
    (tests / "flows" / "nav.py").write_text('def sign_in(d, user):\n    """Signs in from the login screen. Then…"""\n'
                                             '    d.tap("login.submit")\n')
    helper = 'def open_cart(d):\n    d.tap("cart.open")\n    d.expect_screen("screen.cart")\n'
    (tests / "a.py").write_text("from devtest_scenario import Scenario\n" + helper +
                                'def sign_in(d):\n    pass\nS = Scenario("x")\n@S.step("go")\ndef _(d):\n'
                                '    open_cart(d)\n    frobnicate(d)\n    d.tap("home.tab")\n')
    (tests / "b.py").write_text(helper)
    return top, tests


def test_guards():
    top, tests = project_with_scenarios()
    errors, warnings = G.check_tags(top, tests / "a.py")
    assert [e.split(" ")[1].rstrip(":") for e in errors] == ["cart.open", "screen.cart"], errors  # in a.py and absent from app/
    assert [w.split(" ")[1].rstrip(":") for w in warnings] == ["home.tab"], warnings  # built at runtime from "home." + name
    problems = G.lint([tests / "a.py"])
    assert any("'frobnicate' is used but never defined" in p for p in problems), problems
    assert any("sign_in() exists in flows as nav.sign_in" in p for p in problems), problems
    assert any("open_cart() has the same body as b.py:open_cart" in p for p in problems), problems
    assert G.flows_api(tests / "flows") == ["nav.sign_in(d, user) Signs in from the login screen."]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
