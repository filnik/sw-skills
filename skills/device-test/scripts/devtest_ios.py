"""iOS half of devtest: a thin layer over devtest_wda.py (WDA over usbmux, devicectl, console log)."""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

import devtest_wda as idev
from devtest_common import (Blocked, Fail, app_id, cfg, crash_verdict, device_dir, fresh_db_dir, hhmm, hook, launch_rules,
                            load_json, mark_offset, need, read_since, run, save_json, set_mark, short, source_archive,
                            stale_reason, text, wait_log, worktree_changed_at)

STATE_RUNNING = {1: "not running", 2: "suspended", 3: "background", 4: "foreground"}


class Stale(Blocked):
    """WDA 404 'stale element reference': the element changed under a find or a read, or a restarted WDA no longer
    knows its id. Seen on POST /elements ('"…" Button is not present in the current view anymore') while a menu
    closed, and on /displayed ('expired from the internal cache') after a restart."""


class Node:
    def __init__(self, el_id: str, attrs: dict):
        self.el_id = el_id
        self.id = attrs.get("name") or ""
        self.desc = attrs.get("label") or ""
        self.text = attrs.get("value") or attrs.get("label") or ""
        self.checked = attrs.get("value") in ("1", "true")
        self.selected = attrs.get("selected") in (True, "true", "1") or "not_selected" not in self.desc and "selected" in self.desc
        self.enabled = attrs.get("enabled", True) not in (False, "false")
        self.focused = False

    def subtree_text(self) -> str:
        return self.text


class IOS:
    platform = "ios"

    def __init__(self, udid: str, alias: str):
        self.udid, self.alias = udid, alias
        self.dir = device_dir(udid)
        self.log = idev.CONSOLE_LOG

    @property
    def app(self) -> str:
        return app_id("ios")

    # ------------------------------------------------------------------------------------ WDA

    def http(self, method: str, path: str, body=None, timeout: float = 60):
        for attempt in (1, 2):
            try:
                return idev.http(method, path, body, timeout)
            except urllib.error.HTTPError as e:
                detail, error = "", ""
                try:
                    value = json.loads(e.read() or b"{}").get("value", {})
                    detail, error = value.get("message", ""), value.get("error", "")
                except (ValueError, AttributeError, OSError):
                    pass
                # Activating Settings can restart WDA while a saved endpoint still names its dead session, and an
                # uninstall invalidates a WDA state read in flight: neither 404 acted, so both are asked again.
                deleted_read = 'was deleted while this request was still pending' in detail and (method == 'GET' or path.endswith('/wda/apps/state'))
                if attempt == 1 and e.code == 404 and ('Session does not exist' in detail or deleted_read) and '/session/' in path:
                    fresh = re.sub(r"/session/[^/]+", "/session/%s" % self.sid(), path)
                    if fresh != path:
                        path = fresh
                        continue
                stale = error == "stale element reference" or (
                    e.code == 404 and ("not present in the current view" in detail or "expired from the internal cache" in detail))
                raise (Stale if stale else Blocked)(
                    "WDA %s %s -> HTTP %d %s" % (method, re.sub(r"/session/[^/]+", "", path), e.code, short(detail, 200)))
            except (urllib.error.URLError, OSError, ValueError) as e:
                # WDA on the phone dies now and then (connection reset): restart it once, then retry.
                if attempt == 2 or not self.restart_wda_if_dead(method, path):
                    raise Blocked("WebDriverAgent unreachable (%s): unlock the iPhone" % short(str(e), 120))
                path = re.sub(r"/session/[^/]+", "/session/%s" % self.sid(), path)

    def sid(self) -> str:
        for attempt in (1, 2):
            try:
                return idev.sid()
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                if attempt == 2 or not self.restart_wda_if_dead():
                    raise Blocked("no WDA session (%s)" % short(str(e), 120))

    def reachable(self) -> bool:
        try:
            return any(d["conn"] == "USB" and idev._norm(d["udid"]) == idev._norm(self.udid) for d in idev.usb_devices())
        except OSError:
            return False

    def foreground(self):
        """Bundle id of the app in front, None when WDA cannot tell. A notification banner can take a tap meant for
        the app (TestFlight's 'new build' banner once opened TestFlight in the middle of a step)."""
        try:
            return (idev.http("GET", "/wda/activeAppInfo", None, 10).get("value") or {}).get("bundleId")
        except Exception:  # noqa: BLE001 - only a hint for the verdict
            return None

    def restart_wda_if_dead(self, method: str = "GET", path: str = "") -> bool:
        """True when the caller may retry: WDA was down and is back, or it answers again after a transient error and
        the request is a read. One /status of 3s used to decide: after a reset on a live WDA (busy, or a usbmux hiccup)
        devtest killed it, and the new session (bundleId) relaunched the app on the environment screen in the middle of
        a step ("BUILD INTERRUPTED" in wda-run.log)."""
        deadline = time.time() + 10
        while True:
            if idev.wda_up():
                return method == "GET" or bool(re.search(r"/elements?$", path))
            # Busy, not dead: WDA serves one request at a time, and while it is still writing its log it is alive.
            # Restarting it then killed a live WDA whose new session relaunched the app ("Connection reset by peer"
            # lines up to the kill, "BUILD INTERRUPTED" in wda-run.log).
            log = idev.LOGS / "wda-run.log"
            if time.time() >= deadline and not (log.exists() and time.time() - log.stat().st_mtime < 10
                                                and time.time() < deadline + 60):
                break
            time.sleep(2)
        sys.stderr.write("(WebDriverAgent stopped answering: restarting it)\n")
        self.wda_restarts = getattr(self, "wda_restarts", 0) + 1  # the runner reads it: a new session relaunches the app
        self.ensure_wda()
        return True

    def ensure_wda(self) -> None:
        if idev.wda_up():
            return
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                idev.cmd_wda(argparse.Namespace(action="start", udid=self.udid))
        except idev.Fail as e:
            raise Blocked("WebDriverAgent did not start: %s" % short(str(e), 300))

    def xml_source(self, full_text: bool = False) -> str:
        """XCTest's standard snapshot caps value at 512 bytes.
        Request custom snapshots for exact long-field evidence, restoring the session setting afterward.
        """
        if not full_text:
            return self.http("GET", "/source?format=xml", timeout=120)["value"]
        path = "/session/%s/appium/settings" % self.sid()
        settings = self.http("GET", path).get("value", {})
        if "enforceCustomSnapshots" not in settings:
            raise Blocked("WDA cannot expose full text: enforceCustomSnapshots setting unavailable")
        previous = settings["enforceCustomSnapshots"]
        self.http("POST", path, {"settings": {"enforceCustomSnapshots": True}})
        try:
            return self.http("GET", "/source?format=xml", timeout=120)["value"]
        finally:
            self.http("POST", path, {"settings": {"enforceCustomSnapshots": previous}})

    def elements(self, using: str, value: str) -> list:
        for attempt in (1, 2, 3):
            try:
                r = self.http("POST", "/session/%s/elements" % self.sid(), {"using": using, "value": value})
                return [e.get("ELEMENT") or next(iter(e.values())) for e in r.get("value", [])]
            except Stale:
                # A search racing a transition: the screen settles within a second, so search again.
                if attempt == 3:
                    raise
                time.sleep(1)

    def attrs(self, el: str) -> dict:
        out = {}
        for a in ("name", "label", "value", "selected", "enabled"):
            try:
                out[a] = self.http("GET", "/session/%s/element/%s/attribute/%s" % (self.sid(), el, a), timeout=20).get("value")
            except Blocked:
                out[a] = None
        return out

    @staticmethod
    def query(sel: str) -> tuple:
        def q(s):
            return s.replace("'", "\\'")
        if sel.startswith("text:") or sel.startswith("desc:"):
            return "predicate string", "label == '%s' OR value == '%s'" % (q(sel[5:]), q(sel[5:]))
        if sel.startswith("text~") or sel.startswith("desc~"):
            return "predicate string", "label CONTAINS[c] '%s' OR value CONTAINS[c] '%s'" % (q(sel[5:]), q(sel[5:]))
        return "accessibility id", sel

    def rect(self, el: str) -> dict:
        return self.http("GET", "/session/%s/element/%s/rect" % (self.sid(), el), timeout=20).get("value", {})

    def _native_switch(self, el: str):
        """A Compose-hosted UISwitch is a sibling of its tagged wrapper, not a child: match it by position."""
        r = self.rect(el)
        for sw in self.elements("class name", "XCUIElementTypeSwitch"):
            s = self.rect(sw)
            cx, cy = s.get("x", 0) + s.get("width", 0) / 2, s.get("y", 0) + s.get("height", 0) / 2
            if r.get("x", 0) <= cx <= r.get("x", 0) + r.get("width", 0) and r.get("y", 0) <= cy <= r.get("y", 0) + r.get("height", 0):
                return sw
        return None

    def find(self, sel: str, nodes=None):
        """A Stale between the find and the reads of its element (the switch re-rendered right after a tap) is looked
        for again after a second: GET /element/<id>/rect answered 404 'expired from the internal cache' right after a
        switch flipped."""
        for attempt in (1, 2, 3):
            try:
                return self._find_once(sel)
            except Stale:
                if attempt == 3:
                    raise
                time.sleep(1)

    def _find_once(self, sel: str):
        els = self.elements(*self.query(sel))
        if not els:
            return None
        attrs = self.attrs(els[0])
        if attrs.get("value") is None and "switch" in sel:
            sw = self._native_switch(els[0])
            if sw:
                attrs["value"] = self.attrs(sw).get("value")
                return Node(sw, dict(attrs, name=attrs.get("name")))
        return Node(els[0], attrs)

    def screens(self, nodes=None) -> list:
        names = []
        for el in self.elements("predicate string", "name BEGINSWITH 'screen.'"):
            try:
                names.append(self.http("GET", "/session/%s/element/%s/attribute/name" % (self.sid(), el), timeout=20).get("value"))
            except Blocked:
                pass
        return [n for n in names if n]

    def wait_for(self, sel: str, timeout: float = 10):
        deadline = time.time() + timeout
        while True:
            n = self.find(sel)
            if n is not None or time.time() >= deadline:
                return n, None
            time.sleep(0.8)  # gentle polling: CMP 1.11 crashes on accessibility reads during transitions

    def _element_for(self, sel: str, timeout: float = 0):
        """Find an element id without fetching unused attributes: with five extra attribute requests per lookup
        tap/visible missed short-lived UI and stretched timed steps. State reads still use find().
        """
        deadline = time.time() + timeout
        while True:
            els = self.elements(*self.query(sel))
            if els or time.time() >= deadline:
                return els[0] if els else None
            time.sleep(0.8)

    def tap(self, sel: str, timeout: float = 10) -> str:
        """After a WDA restart a click can still reference its expired element cache. Resolve the selector again
        after Stale, including
        a stale click after the existing scroll fallback; never scroll the expired reference again.
        """
        for attempt in (1, 2, 3):
            try:
                return self._tap_once(sel, timeout)
            except Stale:
                if attempt == 3:
                    raise
                time.sleep(1)

    def _tap_once(self, sel: str, timeout: float = 10) -> str:
        # A button's text can also occur in the instruction before it: native text taps prefer an actual button.
        # Tagged Compose containers retain their established selection behavior.
        el = None
        if sel.startswith(("text:", "text~", "desc:", "desc~")):
            using, predicate = self.query(sel)
            buttons = self.elements(using, "(%s) AND type == 'XCUIElementTypeButton'" % predicate)
            el = buttons[0] if buttons else None
        if el is None:
            el = self._element_for(sel, timeout)
        if el is None:
            raise Fail("%s not on screen within %ds (screens: %s)" % (sel, timeout, ", ".join(self.screens()) or "-"))
        path = "/session/%s/element/%s/click" % (self.sid(), el)
        try:
            self.http("POST", path)
        except Stale:
            raise
        except Blocked:
            time.sleep(1)
            el = self._element_for(sel, timeout)
            if el is None:
                raise Fail("%s gone from the screen before the tap (screens: %s)" % (sel, ", ".join(self.screens()) or "-"))
            path = "/session/%s/element/%s/click" % (self.sid(), el)
            try:
                self.http("POST", path)
            except Stale:
                raise
            except Blocked:
                self.http("POST", "/session/%s/wda/element/%s/scroll" % (self.sid(), el), {"toVisible": True})
                self.http("POST", path)
        return "tapped %s" % sel

    def node(self, sel: str, timeout: float = 0):
        n, _ = self.wait_for(sel, timeout)
        return n

    def visible(self, sel: str, timeout: float = 0) -> bool:
        """Read displayed directly; retry a disappearing element once.
        Like tap(), avoid fetching five unused attributes before the displayed request.
        """
        for attempt in (1, 2):
            el = self._element_for(sel, timeout if attempt == 1 else 0)
            if el is None:
                return False
            try:
                return bool(self.http("GET", "/session/%s/element/%s/displayed" % (self.sid(), el)).get("value"))
            except Stale:
                pass
        return False

    def hide_keyboard(self) -> None:
        try:
            self.http("POST", "/session/%s/wda/keyboard/dismiss" % self.sid(), {})
        except Blocked:
            pass

    def set_text(self, sel: str, value: str, timeout: float = 10) -> str:
        """A clear can reference an element expired by a WDA restart.
        Resolve the field again and replace its complete value after Stale, as tap/back already do.
        """
        for attempt in (1, 2, 3):
            try:
                return self._set_text_once(sel, value, timeout)
            except Stale:
                if attempt == 3:
                    raise
                time.sleep(1)

    def _set_text_once(self, sel: str, value: str, timeout: float = 10) -> str:
        el = self._element_for(sel, timeout)
        if el is None:
            raise Fail("%s not on screen within %ds" % (sel, timeout))
        base = "/session/%s/element/%s" % (self.sid(), el)
        self.http("POST", base + "/click")
        self.http("POST", base + "/clear")
        if value:
            self.http("POST", base + "/value", {"value": list(value)})
        # Sampling a Compose text field immediately after typing can hang WDA.
        # Let its accessibility value settle before the attribute snapshot (the save still uses the real UI).
        time.sleep(0.5)
        seen = self.attrs(el).get("value") or ""
        if seen != value:
            raise Fail("%s shows %r after typing %r" % (sel, seen, value))
        return "text of %s = %r" % (sel, value)

    def back(self) -> None:
        """A WDA reset can expire a native close button during a restore.
        Re-find the current top close/back action after Stale, as tap() does; do not reuse its id.
        """
        for attempt in (1, 2, 3):
            try:
                return self._back_once()
            except Stale:
                if attempt == 3:
                    raise
                time.sleep(1)

    def _back_once(self) -> None:
        """Dismiss a native popover before considering navigation beneath it: a popover has PopoverDismissRegion
        and no dismiss button of the app, and that native accessibility name is stable while the label is localized.
        Android keeps its own system Back. A sheet can have a native nav-bar close while the app's back button
        belongs to the covered screen: dismiss the visible sheet before navigating beneath it.
        """
        if self.elements(*self.query("PopoverDismissRegion")):
            self.tap("PopoverDismissRegion", 1)
            return
        close_buttons = self.elements("xpath", "//XCUIElementTypeNavigationBar//XCUIElementTypeButton[@name='close']")
        for element in close_buttons:
            base = "/session/%s/element/%s" % (self.sid(), element)
            if self.http("GET", base + "/displayed").get("value"):
                self.http("POST", base + "/click", {})
                return
        for sel in ("navigation.back_button", "navigation.dismiss_button", "BackButton"):
            if self.elements(*self.query(sel)):
                self.tap(sel, 1)
                return
        raise Fail("no back or dismiss button on screen")

    def window(self) -> tuple:
        v = self.http("GET", "/session/%s/window/size" % self.sid()).get("value", {})
        return int(v.get("width", 390)), int(v.get("height", 844))

    def swipe(self, direction: str) -> None:
        w, h = self.window()
        a, b = int(h * 0.75), int(h * 0.3)
        y1, y2 = {"up": (a, b), "down": (b, a)}[direction]
        idev.touch([{"type": "pointerMove", "duration": 0, "x": w // 2, "y": y1}, {"type": "pointerDown"},
                    {"type": "pause", "duration": 100}, {"type": "pointerMove", "duration": 350, "x": w // 2, "y": y2},
                    {"type": "pointerUp"}])

    def _drag(self, x: float, y0: float, y1: float, duration: float) -> None:
        self.http("POST", "/session/%s/wda/dragfromtoforduration" % self.sid(),
                  {"fromX": x, "fromY": y0, "toX": x, "toY": y1, "duration": duration})

    def open_switcher(self) -> None:
        """Home, then WDA's drag from the bottom edge to mid-screen (0.3s) opens the app switcher.
        The W3C-actions swipe never opened it (the home screen stayed), dragfromtoforduration did."""
        w, h = self.window()
        self.http("POST", "/wda/homescreen", {})
        time.sleep(1.5)
        self._drag(w / 2, h - 1, h * 0.5, 0.3)
        time.sleep(1.5)

    def card_centre(self):
        """(x, y) of the app's card in the open switcher, from the springboard tree (name 'card:<bundle>:...'), else None."""
        m = re.search(r'<[^>]*name="card:%s:[^"]*"[^>]*>' % re.escape(self.app), self.xml_source())
        if not m:
            return None
        g = {k: float(v) for k, v in re.findall(r'\b(x|y|width|height)="(-?[\d.]+)"', m.group(0))}
        return (g["x"] + g["width"] / 2, g["y"] + g["height"] / 2) if len(g) == 4 else None

    def drag_card(self, x_frac: float = 0.5, y_from: float = 0.45, duration: float = 0.05) -> bool:
        """One quick drag of the app's card (its centre from the tree, else the screen fraction) up and off the screen;
        returns whether the card was found in the tree."""
        w, h = self.window()
        c = self.card_centre()
        self._drag(c[0] if c else w * x_frac, c[1] if c else h * y_from, h * 0.03, duration)
        time.sleep(2)
        return c is not None

    def refresh(self) -> None:
        # From inside the list: a drag that starts in a gap above the scroll view (under a tab selector, say)
        # fires no pull-to-refresh.
        w, h = self.window()
        idev.touch([{"type": "pointerMove", "duration": 0, "x": w // 2, "y": int(h * 0.38)}, {"type": "pointerDown"},
                    {"type": "pause", "duration": 100}, {"type": "pointerMove", "duration": 700, "x": w // 2, "y": int(h * 0.85)},
                    {"type": "pointerUp"}])

    def src(self, flt=None) -> list:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                idev.ui_src(flt)
            except (urllib.error.URLError, OSError) as e:
                raise Blocked("WDA source failed: %s" % short(str(e), 120))
        return buf.getvalue().splitlines()

    def shot(self, path: Path) -> Path:
        raw = path.with_suffix(".raw.png")
        raw.write_bytes(base64.b64decode(self.http("GET", "/screenshot").get("value", "")))
        run(["sips", "-Z", "900", str(raw), "--out", str(path)])
        if path.exists():
            raw.unlink()
        else:
            raw.replace(path)
        return path

    # ------------------------------------------------------------------------------------ app

    def app_state(self) -> int:
        return int(self.http("POST", "/session/%s/wda/apps/state" % self.sid(), {"bundleId": self.app}).get("value", 0))

    def running(self) -> bool:
        return self.app_state() >= 3

    def stop(self) -> None:
        self.http("POST", "/session/%s/wda/apps/terminate" % self.sid(), {"bundleId": self.app})

    def launch(self, env=None, wait_screen=None, restart: bool = False, timeout: float = 90) -> str:
        rules, env = launch_rules("ios"), env or cfg("default_env")
        self.ensure_wda()
        self.sid()  # a session created after the launch would relaunch the app and detach the console
        since = set_mark(self.udid, self.log, "launch")
        started = time.time()
        if restart or self.app_state() < 3:
            idev.spawn("console", ["xcrun", "devicectl", "device", "process", "launch", "--device", self.udid,
                                   "--terminate-existing", "--console", "--environment-variables",
                                   # console_debug turns on debug-level os_log in the launched process only
                                   ('{"OS_ACTIVITY_DT_MODE": "enable", "OS_ACTIVITY_MODE": "debug"}'
                                    if getattr(self, "console_debug", False) else '{"OS_ACTIVITY_DT_MODE": "enable"}'),
                                   self.app, *(cfg("ios.launch_args") or [])], self.log)
        else:
            self.http("POST", "/session/%s/wda/apps/activate" % self.sid(), {"bundleId": self.app})
        deadline, handled, screens = time.time() + timeout, [], []
        # devicectl launches asynchronously: WDA can still see the old process
        while time.time() - started < min(15, timeout) and self.app_state() < 3:
            time.sleep(0.5)
        while time.time() < deadline:
            alert = self._alert()
            if alert and any(k in alert for k in getattr(self, "keep_alerts", ())):
                # keep_alerts: without it the launch accepts the app's own alert seconds after it shows
                return "launched with the alert '%s' left on screen" % short(alert, 40)
            if alert:
                self.http("POST", "/session/%s/alert/accept" % self.sid(), {})
                handled.append("alert '%s'" % short(alert, 40))
                continue
            try:
                screens = self.screens()
            except Blocked as e:
                if "not running" in str(e) and time.time() - started < min(15, timeout):
                    time.sleep(1)
                    continue
                raise
            if rules.get("env_screen") and rules["env_screen"] in screens:
                self.tap(rules["env_option"] % env, 5)
                self.tap(rules["env_continue"], 5)
                handled.append("env=%s" % env)
                continue
            rule = next((r for r in rules.get("dismiss") or [] if self._applies(r)), None)
            if rule:
                self.tap(rule["tap"], 3)
                handled.append(rule.get("note") or rule["tap"])
                continue
            if rules.get("login_screen") in screens and wait_screen != rules["login_screen"]:
                raise Blocked("the app is logged out on %s: log in by hand%s" % (
                    self.alias, " (%s)" % rules["login_hint"] if rules.get("login_hint") else ""))
            settled = [s for s in screens if s not in (rules.get("ignored_screens") or ())]
            if not wait_screen and rules.get("screen_tags") is False and self.app_state() == 4:
                settled = ["the app"]  # an app with no screen.* tags: in front is as far as launch can tell
            if (wait_screen and wait_screen in screens) or (not wait_screen and settled):
                return "launched on %s%s" % (settled[-1] if settled else "?", " (%s)" % ", ".join(handled) if handled else "")
            if time.time() - started > 8 and not screens:
                self.crash_check(since)
            time.sleep(1)
        raise Fail("app did not reach %s within %ds (screens: %s)" % (wait_screen or "a screen", timeout, ", ".join(screens) or "-"))

    def _applies(self, rule: dict) -> bool:
        """A launch.dismiss rule: its `tap` target is on screen and, when it lists `texts`, one of them is too."""
        texts = rule.get("texts")
        if texts and not any(self.elements(*self.query("text:" + t)) for t in texts):
            return False
        return bool(self.elements(*self.query(rule["tap"])))

    def _alert(self) -> str:
        try:
            return idev.http("GET", "/session/%s/alert/text" % self.sid(), timeout=10).get("value") or ""
        except (urllib.error.URLError, OSError, ValueError):
            return ""

    def crash_check(self, since: int) -> None:
        # XCTest state 2 is an alive, suspended background process (after a minute on Home, say).
        if self.app_state() >= 2:
            return
        crash_verdict(read_since(self.log, since), alive=False)

    # ------------------------------------------------------------------------------------ state

    def _devicectl_copy(self, direction: str, source: str, destination: str):
        """devicectl copy, tried twice: it hangs now and then (60s timeouts mid-run, fine a minute later).
        When the second try hangs too, every devicectl service usually does (`device info files`, `info apps`,
        `info lockState`) while WDA and the console stream still work: the Mac's CoreDeviceService is stuck.
        Seen once after 3 days up: it ignored SIGTERM (`devicectl` then failed with "Timed out waiting for
        CoreDeviceService to fully initialize"); `kill -9` on it made launchd start a fresh one and copies worked again.
        Replugging the cable did not help."""
        cmd = ["xcrun", "devicectl", "device", "copy", direction, "--device", self.udid, "--domain-type",
               "appDataContainer", "--domain-identifier", self.app, "--source", source, "--destination", destination]
        try:
            result = run(cmd, timeout=60, text=True)
            if result.returncode == 0:
                return result
            # A transient copy error clears on the next read.
            sys.stderr.write("(devicectl copy failed: trying once more)\n")
        except Blocked:
            sys.stderr.write("(devicectl copy hung: trying once more)\n")
        try:
            return run(cmd, timeout=120, text=True)
        except Blocked as e:
            raise Blocked("%s. If `xcrun devicectl device info files` hangs too, the Mac's CoreDeviceService is stuck: "
                          "`pkill -9 -f CoreDeviceService.xpc` (launchd restarts it), then run again" % e)

    def _copy_from(self, source: str, dest: Path) -> bool:
        r = self._devicectl_copy("from", source, str(dest))
        return r.returncode == 0 and dest.exists()

    def prefs_bytes(self) -> bytes:
        """Use a fresh transfer path for each settings snapshot."""
        with tempfile.TemporaryDirectory(prefix="prefs-read-", dir=self.dir) as folder:
            dest = Path(folder) / "settings.pb"
            if not self._copy_from(need("ios.prefs"), dest):
                raise Blocked("cannot copy %s off %s" % (cfg("ios.prefs"), self.alias))
            return dest.read_bytes()

    def write_prefs(self, data: bytes) -> None:
        """devicectl skips unmodified files: a same-size edit was read back unchanged until the source files got
        distinct modification times."""
        with tempfile.TemporaryDirectory(prefix="prefs-write-", dir=self.dir) as folder:
            src = Path(folder) / "settings.pb"
            src.write_bytes(data)
            modified = max(time.time() + 2, getattr(self, "_prefs_transfer_mtime", 0) + 2)
            os.utime(src, (modified, modified))
            self._prefs_transfer_mtime = modified
            r = self._devicectl_copy("to", str(src), need("ios.prefs"))
            if r.returncode != 0:
                raise Blocked("cannot write %s: %s" % (cfg("ios.prefs"), short(text(r), 300)))

    def pull_db(self) -> Path:
        """The WAL before the DB, and no -shm: a checkpoint between the copies of a live DB then leaves frames already
        in the DB, not missing ones, and SQLite rebuilds the index from the WAL instead of trusting a stale -shm.
        DB first with -shm gave "database disk image is malformed" mid-upload."""
        db = need("ios.db")
        name = db.rsplit("/", 1)[-1]
        dest = fresh_db_dir(self.dir)
        for suffix in ("-wal", ""):
            self._copy_from(db + suffix, dest / (name + suffix))
        if not (dest / name).exists():
            raise Blocked("%s not copied off %s" % (name, self.alias))
        return dest / name

    def push_db(self, folded: Path) -> None:
        """With the app stopped, `folded` (a pull_db copy, its WAL checkpointed in and edited) becomes the app's DB.
        devicectl cannot delete, so the phone's -wal is first overwritten empty and read back empty (left as it was,
        SQLite would replay the old WAL's frames over the new file at the next open), the -shm too (SQLite resets it
        anyway on a first open); only then the DB itself."""
        db = need("ios.db")
        empty = self.dir / "empty-file"
        empty.write_bytes(b"")
        r = self._devicectl_copy("to", str(empty), db + "-wal")
        back = self.dir / "wal-check"
        back.unlink(missing_ok=True)
        if r.returncode != 0 or not self._copy_from(db + "-wal", back) or back.stat().st_size:
            raise Blocked("the phone's %s-wal could not be emptied (%s): DB not written; if the WAL did "
                          "change, push %s to %s" % (db, short(text(r), 160), folded, db))
        self._devicectl_copy("to", str(empty), db + "-shm")
        r = self._devicectl_copy("to", str(folded), db)
        if r.returncode != 0:
            raise Blocked("%s not written (%s) after its WAL was emptied: push %s to it before launching the app" % (
                db, short(text(r), 160), folded))

    def media_path(self, relative: str) -> str:
        return "%s/%s" % (cfg("ios.media_root") or "Documents", relative)

    def put_file(self, relative: str, local: Path) -> str:
        target = self.media_path(relative)
        r = run(["xcrun", "devicectl", "device", "copy", "to", "--device", self.udid, "--domain-type", "appDataContainer",
                 "--domain-identifier", self.app, "--source", str(local), "--destination", target], timeout=60, text=True)
        if r.returncode != 0:
            raise Blocked("cannot copy to %s: %s" % (target, short(text(r), 300)))
        return target

    def remove_file(self, path: str) -> None:
        """devicectl cannot delete: left to the app's own cleanup."""

    def list_files(self, folder: str) -> str:
        """devicectl's listing of a container folder as text: one ~3s call for many names."""
        r = run(["xcrun", "devicectl", "device", "info", "files", "--device", self.udid, "--domain-type", "appDataContainer",
                 "--domain-identifier", self.app, "--subdirectory", folder], timeout=60, text=True)
        return r.stdout

    def file_exists(self, path: str) -> bool:
        folder, _, name = path.rpartition("/")
        return name in self.list_files(folder)

    # ------------------------------------------------------------------------------------ device

    def _by_hand(self, name: str, on: bool, timeout: float) -> str:
        """iOS has no scriptable network switch: the pack's hook asks the person at the Mac and waits for the app's
        own log line that confirms the change."""
        ask = hook(name)
        if not ask:
            raise Blocked("on iOS the network is switched by hand: the pack's hooks.py needs %s(dev, on, timeout), which "
                          "asks for it and waits for the app's log line" % name)
        return ask(self, on, timeout)

    def airplane(self, on: bool, timeout: float = 300) -> str:
        return self._by_hand("ios_airplane", on, timeout)

    def wifi(self, on: bool, timeout: float = 40) -> str:
        raise Blocked("Wi-Fi cannot be switched from a script on iOS: use `net airplane on|off`")

    def metered(self, on: bool, timeout: float = 300) -> str:
        return self._by_hand("ios_metered", on, timeout)

    # ------------------------------------------------------------------------------------ baa

    def baa_port(self) -> int:
        line = wait_log(self.log, mark_offset(self.udid, self.log, "launch"), "baa", 10)
        m = re.search(r"ws://127\.0\.0\.1:(\d+)/baa", line)
        if not m:
            raise Blocked("no 'Baa started' line since the last launch: is this a Debug build with baa?")
        cache = load_json(self.dir / "baa.json", {})
        pids = idev.load_pids()
        if cache.get("device_port") == m.group(1) and idev.alive(pids.get("baa-forward")):
            return int(cache["host_port"])
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        host = s.getsockname()[1]
        s.close()
        idev.spawn("baa-forward", [sys.executable, str(Path(idev.__file__).resolve()), "--udid", self.udid, "_forward",
                                   "--local", str(host), "--remote", m.group(1)], idev.LOGS / "baa-forward.log")
        time.sleep(0.5)
        save_json(self.dir / "baa.json", {"device_port": m.group(1), "host_port": host})
        return host

    def stop_tools(self) -> str:
        """`lease release`: the session is over, so WebDriverAgent, the console capture and both port forwards stop."""
        names = ("wda", "forward", "baa-forward", "console")
        running = [n for n in names if idev.alive(idev.load_pids().get(n))]
        for name in names:
            idev.stop(name)
        return "stopped %s" % ", ".join(running) if running else ""

    # ------------------------------------------------------------------------------------ build

    def _ns(self, top: Path):
        return argparse.Namespace(worktree=str(top), app=None, udid=self.udid)

    def build(self, top: Path) -> str:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                idev.cmd_build(self._ns(top))
                idev.cmd_sign(self._ns(top))
        except idev.Fail as e:
            raise Blocked("%s %s" % (short(buf.getvalue(), 600), e))
        return buf.getvalue().strip().splitlines()[0] if buf.getvalue().strip() else "built"

    def install(self, top: Path) -> str:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                idev.cmd_install(self._ns(top))
        except idev.Fail as e:
            raise Blocked(str(e))
        app = idev.signed_app(top)
        binary = app / (cfg("ios.debug_binary") or "-")  # a Debug build's code is in this file, not in the executable
        mtime = (binary if binary.exists() else app).stat().st_mtime
        archive = source_archive(top)
        commit = archive["commit"][:10] if archive else run(
            ["git", "-C", str(top), "rev-parse", "--short", "HEAD"], text=True).stdout.strip()
        save_json(self.dir / "installed.json", {"worktree": str(top), "commit": commit, "app_mtime": mtime,
                                                "installed_at": time.time()})
        return "installed %s (%s, built %s) on %s" % (top.name, commit, hhmm(mtime), self.alias)

    def build_info(self, top: Path) -> str:
        rec = load_json(self.dir / "installed.json", None)
        why = stale_reason(rec, top, worktree_changed_at(top))
        if why:
            raise Blocked("%s: %s" % (self.alias, why))
        return "build ok: %s @ %s, built %s, installed %s (iOS: installs done outside devtest are not detected)" % (
            top.name, rec["commit"], hhmm(rec["app_mtime"]), hhmm(rec["installed_at"]))
