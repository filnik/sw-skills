"""iOS half of devtest: a physical iPhone on USB, driven through WebDriverAgent (WDA) over a usbmux port forward, with
`xcrun devicectl` for install, launch with the console attached, and app-container file copies. Standard library only.

WDA comes from the appium-webdriveragent npm package (`npm install --prefix ~/.cache/devtest/ios/wda-src
appium-webdriveragent`, or an Appium xcuitest driver install). It is built once for testing, signed with
device_test.ios.team_id, and served on 127.0.0.1:<18100 + n> per phone. Selectors: accessibility identifier,
`text:Exact` / `desc:Exact` (label or value), `text~part` / `desc~part`.

`python3 devtest_ios.py _forward UDID LOCAL REMOTE` is the port forward process that `wda` starts.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import plistlib
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from devtest_common import (STATE, Blocked, Fail, build, crash_verdict, device_dir, fresh_db_dir, install_receipt,
                            load_json, need, read_since, run, save_json, set_mark, short, stale_reason, text, wait_log,
                            worktree_changed_at)

IOS_STATE = STATE / "ios"
LOGS = IOS_STATE / "logs"
PIDS = IOS_STATE / "pids.json"
WDA_DD = IOS_STATE / "wda-dd"
USBMUXD = "/var/run/usbmuxd"
DEVICE_PORT = 8100
WDA_PROJECTS = [
    str(IOS_STATE / "wda-src/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"),
    str(Path.home() / ".appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/"
                      "WebDriverAgent.xcodeproj"),
    str(Path.home() / ".npm/_npx/*/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"),
]


# ---------------------------------------------------------------------------------------- usbmux

def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("usbmuxd closed the connection")
        buf += chunk
    return buf


def _usbmux(message: dict):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(USBMUXD)
    body = plistlib.dumps(dict(message, ClientVersionString="devtest", ProgName="devtest"))
    s.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)
    length = struct.unpack("<IIII", _recv_exact(s, 16))[0]
    return s, plistlib.loads(_recv_exact(s, length - 16))


def norm(udid: str) -> str:
    return udid.replace("-", "").upper()


def usb_devices() -> list:
    s, reply = _usbmux({"MessageType": "ListDevices"})
    s.close()
    return [{"udid": d.get("Properties", {}).get("SerialNumber", ""), "id": d.get("DeviceID"),
             "conn": d.get("Properties", {}).get("ConnectionType", "")} for d in reply.get("DeviceList", [])]


def usbmux_connect(udid: str, port: int) -> socket.socket:
    dev = next((d for d in usb_devices() if d["conn"] == "USB" and norm(d["udid"]) == norm(udid)), None)
    if dev is None:
        raise ConnectionError("device %s is not on USB" % udid)
    s, reply = _usbmux({"MessageType": "Connect", "DeviceID": dev["id"], "PortNumber": socket.htons(port)})
    if reply.get("Number", 1) != 0:
        s.close()
        raise ConnectionError("usbmuxd refused port %d: %s" % (port, reply))
    return s


def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def forward(udid: str, local: int, remote: int) -> None:
    """Serves 127.0.0.1:local, tunnelling each connection to the device's port over usbmuxd."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", local))
    srv.listen(32)
    while True:
        client, _ = srv.accept()
        try:
            device = usbmux_connect(udid, remote)
        except Exception as e:  # noqa: BLE001 - keep serving: the device may come back
            sys.stderr.write("forward: %s\n" % e)
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, device), daemon=True).start()
        threading.Thread(target=_pipe, args=(device, client), daemon=True).start()


def local_port(udid: str) -> int:
    """A stable local port per phone, so two iPhones can run side by side."""
    return 18100 + int(hashlib.sha1(norm(udid).encode()).hexdigest(), 16) % 100


# ---------------------------------------------------------------------------------------- background processes

def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def stop_proc(name: str) -> None:
    pids = load_json(PIDS, {})
    pid = pids.pop(name, None)
    if pid and alive(pid):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except OSError:
            pass
    save_json(PIDS, pids)


def spawn(name: str, cmd: list, log: Path) -> int:
    stop_proc(name)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write("==== %s %s\n" % (name, time.strftime("%Y-%m-%d %H:%M:%S")))
        f.flush()
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    pids = load_json(PIDS, {})
    pids[name] = p.pid
    save_json(PIDS, pids)
    return p.pid


def latest(pattern: str):
    found = glob.glob(pattern)
    return Path(max(found, key=os.path.getmtime)) if found else None


# ---------------------------------------------------------------------------------------- WebDriverAgent

def wda_xctestrun(team: str) -> Path:
    """WebDriverAgentRunner built for testing once, signed automatically with the team."""
    products = WDA_DD / "Build/Products"
    runner = products / "Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    found = latest(str(products / "*.xctestrun"))
    if found and runner.is_dir() and load_json(IOS_STATE / "wda-team.json", {}).get("team") == team:
        return found
    project = next((p for p in (latest(g) for g in WDA_PROJECTS) if p), None)
    if project is None:
        raise Blocked("WebDriverAgent sources missing: run `npm install --prefix %s appium-webdriveragent`, then retry"
                      % (IOS_STATE / "wda-src"))
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / "wda-build.log"
    print("(building WebDriverAgent once from %s -> %s)" % (project, log), file=sys.stderr, flush=True)
    command = ["xcodebuild", "-project", str(project), "-scheme", "WebDriverAgentRunner",
               "-destination", "generic/platform=iOS", "-derivedDataPath", str(WDA_DD), "-allowProvisioningUpdates",
               "CODE_SIGN_STYLE=Automatic", "DEVELOPMENT_TEAM=%s" % team,
               # WDA's own xcconfig turns warnings into errors.
               "GCC_TREAT_WARNINGS_AS_ERRORS=NO", "RUN_CLANG_STATIC_ANALYZER=NO", "build-for-testing"]
    with open(log, "w") as f:
        code = subprocess.call(command, stdout=f, stderr=subprocess.STDOUT)
    found = latest(str(products / "*.xctestrun"))
    if code != 0 or found is None or not runner.is_dir():
        raise Blocked("WebDriverAgent did not build for team %s (log %s). Open %s in Xcode once, select your team for "
                      "WebDriverAgentRunner (give it your own bundle id if Xcode says it is taken), then retry"
                      % (team, log, project))
    save_json(IOS_STATE / "wda-team.json", {"team": team})
    return found


class Stale(Blocked):
    """WDA 'stale element reference': the element changed under a find or a read, or a restarted WDA forgot its id."""


class Node:
    def __init__(self, el_id: str, attrs: dict):
        self.el_id = el_id
        self.id = attrs.get("name") or ""
        self.desc = attrs.get("label") or ""
        self.text = attrs.get("value") or attrs.get("label") or ""
        self.checked = attrs.get("value") in ("1", "true")
        self.selected = attrs.get("selected") in (True, "true", "1")
        self.enabled = attrs.get("enabled", True) not in (False, "false")

    def subtree_text(self) -> str:
        return self.text


def query(sel: str) -> tuple:
    """A selector as a WDA locator (using, value)."""
    v = sel[5:].replace("\\", "\\\\").replace("'", "\\'")
    if sel[:5] in ("text:", "desc:"):
        return "predicate string", "label == '%s' OR value == '%s'" % (v, v)
    if sel[:5] in ("text~", "desc~"):
        return "predicate string", "label CONTAINS[c] '%s' OR value CONTAINS[c] '%s'" % (v, v)
    return "accessibility id", sel


def ui_src_lines(xml: str, flt=None) -> list:
    """`SCREENS: …` then one line per visible element with a name, label or value: type, centre, text."""
    root = ET.fromstring(xml)
    screens, rows = [], []
    for el in root.iter():
        name, label, value = el.get("name"), el.get("label"), el.get("value")
        if name and name.startswith("screen."):
            screens.append(name)
            continue
        if el.get("visible") != "true" or not (name or label or value):
            continue
        if el.get("type") in ("XCUIElementTypeApplication", "XCUIElementTypeWindow", "XCUIElementTypeKey",
                              "XCUIElementTypeKeyboard"):
            continue
        x, y, w, h = (int(float(el.get(k, 0))) for k in ("x", "y", "width", "height"))
        kind = el.get("type", "").replace("XCUIElementType", "")
        label_text = " | ".join(dict.fromkeys(v for v in (name, label, value) if v))
        line = "%-10.10s (%d,%d) %s" % (kind, x + w // 2, y + h // 2, short(label_text, 150))
        if not flt or flt.lower() in line.lower():
            rows.append(line)
    return ["SCREENS: " + (", ".join(dict.fromkeys(screens)) or "-")] + list(dict.fromkeys(rows))


class IOS:
    platform = "ios"

    def __init__(self, udid: str, alias: str, cfg: dict):
        self.udid, self.alias, self.cfg = udid, alias, cfg
        self.dir = device_dir(udid)
        self.log = self.dir / "app.log"
        self.port = local_port(udid)
        self.wda_log = LOGS / ("wda-%s.log" % norm(udid))
        self.wda_restarts = 0
        self.ui_dumps = 0

    @property
    def app_id(self) -> str:
        return need(self.cfg, "ios", "bundle_id")

    # ------------------------------------------------------------------------------------ WDA

    def _raw(self, method: str, path: str, body=None, timeout: float = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def wda_up(self) -> bool:
        try:
            return self._raw("GET", "/status", timeout=3).get("value", {}).get("ready", True) is not False
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def start_wda(self) -> str:
        if self.wda_up() and alive(load_json(PIDS, {}).get("wda-" + norm(self.udid))):
            return "WebDriverAgent already answers on 127.0.0.1:%d" % self.port
        xctestrun = wda_xctestrun(need(self.cfg, "ios", "team_id"))
        spawn("wda-" + norm(self.udid), ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                                         "-destination", "id=%s" % self.udid], self.wda_log)
        spawn("forward-" + norm(self.udid), [sys.executable, str(Path(__file__).resolve()), "_forward", self.udid,
                                             str(self.port), str(DEVICE_PORT)], LOGS / "forward.log")
        deadline = time.time() + 150
        while time.time() < deadline:
            if self.wda_up():
                return "WebDriverAgent ready on 127.0.0.1:%d" % self.port
            if not alive(load_json(PIDS, {}).get("wda-" + norm(self.udid))):
                break
            time.sleep(2)
        tail = " / ".join(self.wda_log.read_text(errors="replace").splitlines()[-6:]) if self.wda_log.exists() else ""
        raise Blocked("WebDriverAgent did not come up on %s. Unlock the phone; trust the developer in Settings > "
                      "General > VPN & Device Management; a passcode prompt may need `xcrun devicectl manage pair "
                      "--device %s`. Log %s: %s" % (self.alias, self.udid, self.wda_log, short(tail, 500)))

    def ensure_wda(self) -> None:
        if not self.wda_up():
            self.start_wda()

    def restart_wda_if_dead(self, method: str = "GET", path: str = "") -> bool:
        """True when the caller may retry: WDA was down and is back, or it answers again and the request is a read.
        A WDA still writing its log is busy, not dead: restarting it would relaunch the app mid-step."""
        deadline = time.time() + 10
        while True:
            if self.wda_up():
                return method == "GET" or bool(re.search(r"/elements?$", path))
            busy = self.wda_log.exists() and time.time() - self.wda_log.stat().st_mtime < 10
            if time.time() >= deadline and not (busy and time.time() < deadline + 60):
                break
            time.sleep(2)
        sys.stderr.write("(WebDriverAgent stopped answering: restarting it)\n")
        self.wda_restarts += 1  # the runner reads it: a new session relaunches the app
        self.start_wda()
        return True

    def sid(self) -> str:
        for attempt in (1, 2):
            try:
                s = self._raw("GET", "/status").get("sessionId")
                if not s:
                    # forceAppLaunch false: a new session attaches to the running app instead of relaunching it.
                    caps = {"capabilities": {"alwaysMatch": {"bundleId": self.app_id, "shouldWaitForQuiescence": False,
                                                             "forceAppLaunch": False}}}
                    s = self._raw("POST", "/session", caps)["sessionId"]
                return s
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                if attempt == 2 or not self.restart_wda_if_dead():
                    raise Blocked("no WDA session (%s)" % short(str(e), 120))

    def http(self, method: str, path: str, body=None, timeout: float = 60):
        for attempt in (1, 2):
            try:
                return self._raw(method, path, body, timeout)
            except urllib.error.HTTPError as e:
                detail, error = "", ""
                try:
                    value = json.loads(e.read() or b"{}").get("value", {})
                    detail, error = value.get("message", ""), value.get("error", "")
                except (ValueError, AttributeError, OSError):
                    pass
                stale = error == "stale element reference" or (e.code == 404 and (
                    "not present in the current view" in detail or "expired from the internal cache" in detail))
                raise (Stale if stale else Blocked)("WDA %s %s -> HTTP %d %s" % (
                    method, re.sub(r"/session/[^/]+", "", path), e.code, short(detail, 200)))
            except (urllib.error.URLError, OSError, ValueError) as e:
                # WDA on the phone dies now and then: restart it once, then retry.
                if attempt == 2 or not self.restart_wda_if_dead(method, path):
                    raise Blocked("WebDriverAgent unreachable (%s): unlock the iPhone" % short(str(e), 120))
                path = re.sub(r"/session/[^/]+", "/session/%s" % self.sid(), path)

    def reachable(self) -> bool:
        try:
            return any(d["conn"] == "USB" and norm(d["udid"]) == norm(self.udid) for d in usb_devices())
        except OSError:
            return False

    def foreground(self):
        """Bundle id of the app in front (a notification banner can take a tap), None when WDA cannot tell."""
        try:
            return (self._raw("GET", "/wda/activeAppInfo", None, 10).get("value") or {}).get("bundleId")
        except Exception:  # noqa: BLE001 - only a hint for the verdict
            return None

    @staticmethod
    def _again(fn, *args):
        """Runs fn up to three times across Stale: a search racing a transition settles within a second."""
        for attempt in (1, 2, 3):
            try:
                return fn(*args)
            except Stale:
                if attempt == 3:
                    raise
                time.sleep(1)

    # ------------------------------------------------------------------------------------ UI

    def elements(self, using: str, value: str) -> list:
        def once():
            r = self.http("POST", "/session/%s/elements" % self.sid(), {"using": using, "value": value})
            return [e.get("ELEMENT") or next(iter(e.values())) for e in r.get("value", [])]
        return self._again(once)

    def attrs(self, el: str) -> dict:
        out = {}
        for a in ("name", "label", "value", "selected", "enabled"):
            try:
                out[a] = self.http("GET", "/session/%s/element/%s/attribute/%s" % (self.sid(), el, a), timeout=20
                                   ).get("value")
            except Blocked:
                out[a] = None
        return out

    def rect(self, el: str) -> dict:
        return self.http("GET", "/session/%s/element/%s/rect" % (self.sid(), el), timeout=20).get("value", {})

    def _native_switch(self, el: str):
        """A UISwitch hosted by a cross-platform UI (Compose) is a sibling of its tagged wrapper: match by position."""
        r = self.rect(el)
        for sw in self.elements("class name", "XCUIElementTypeSwitch"):
            s = self.rect(sw)
            cx, cy = s.get("x", 0) + s.get("width", 0) / 2, s.get("y", 0) + s.get("height", 0) / 2
            if (r.get("x", 0) <= cx <= r.get("x", 0) + r.get("width", 0)
                    and r.get("y", 0) <= cy <= r.get("y", 0) + r.get("height", 0)):
                return sw
        return None

    def _find_once(self, sel: str):
        els = self.elements(*query(sel))
        if not els:
            return None
        attrs = self.attrs(els[0])
        if attrs.get("value") is None and "switch" in sel:
            sw = self._native_switch(els[0])
            if sw:
                attrs["value"] = self.attrs(sw).get("value")
                return Node(sw, attrs)
        return Node(els[0], attrs)

    def find(self, sel: str, nodes=None):
        return self._again(self._find_once, sel)

    def screens(self, nodes=None) -> list:
        names = []
        for el in self.elements("predicate string", "name BEGINSWITH 'screen.'"):
            try:
                names.append(self.http("GET", "/session/%s/element/%s/attribute/name" % (self.sid(), el),
                                       timeout=20).get("value"))
            except Blocked:
                pass
        return [n for n in names if n]

    def wait_for(self, sel: str, timeout: float = 10):
        deadline = time.time() + timeout
        while True:
            n = self.find(sel)
            if n is not None or time.time() >= deadline:
                return n, None
            time.sleep(0.8)  # gentle polling: accessibility reads during a transition can upset some UI toolkits

    def _element_for(self, sel: str, timeout: float = 0):
        """An element id without its attributes (five requests fewer than find)."""
        deadline = time.time() + timeout
        while True:
            els = self.elements(*query(sel))
            if els or time.time() >= deadline:
                return els[0] if els else None
            time.sleep(0.8)

    def _tap_once(self, sel: str, timeout: float) -> str:
        el = self._element_for(sel, timeout)
        if el is None:
            raise Fail("%s not on screen within %ds (screens: %s)" % (sel, timeout, ", ".join(self.screens()) or "-"))
        try:
            self.http("POST", "/session/%s/element/%s/click" % (self.sid(), el))
        except Stale:
            raise
        except Blocked:
            # Off screen: scroll it into view and click again.
            self.http("POST", "/session/%s/wda/element/%s/scroll" % (self.sid(), el), {"toVisible": True})
            self.http("POST", "/session/%s/element/%s/click" % (self.sid(), el))
        return "tapped %s" % sel

    def tap(self, sel: str, timeout: float = 10) -> str:
        return self._again(self._tap_once, sel, timeout)

    def node(self, sel: str, timeout: float = 0):
        return self.wait_for(sel, timeout)[0]

    def visible(self, sel: str, timeout: float = 0) -> bool:
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

    def _set_text_once(self, sel: str, value: str, timeout: float) -> str:
        el = self._element_for(sel, timeout)
        if el is None:
            raise Fail("%s not on screen within %ds" % (sel, timeout))
        base = "/session/%s/element/%s" % (self.sid(), el)
        self.http("POST", base + "/click")
        self.http("POST", base + "/clear")
        if value:
            self.http("POST", base + "/value", {"value": list(value)})
        time.sleep(0.5)  # let the accessibility value settle before reading it back
        seen = self.attrs(el).get("value") or ""
        if seen != value:
            raise Fail("%s shows %r after typing %r" % (sel, seen, value))
        return "text of %s = %r" % (sel, value)

    def set_text(self, sel: str, value: str, timeout: float = 10) -> str:
        return self._again(self._set_text_once, sel, value, timeout)

    def _back_once(self) -> None:
        """A popover's dismiss region first, then a BackButton, then the first button of the navigation bar."""
        if self.elements(*query("PopoverDismissRegion")):
            self.tap("PopoverDismissRegion", 1)
            return
        if self.elements(*query("BackButton")):
            self.tap("BackButton", 1)
            return
        for el in self.elements("xpath", "//XCUIElementTypeNavigationBar/XCUIElementTypeButton[1]"):
            base = "/session/%s/element/%s" % (self.sid(), el)
            if self.http("GET", base + "/displayed").get("value"):
                self.http("POST", base + "/click", {})
                return
        raise Fail("no back button on screen: tap the app's own back by its selector")

    def back(self) -> None:
        self._again(self._back_once)

    def window(self) -> tuple:
        v = self.http("GET", "/session/%s/window/size" % self.sid()).get("value", {})
        return int(v.get("width", 390)), int(v.get("height", 844))

    def drag(self, x: int, y1: int, y2: int, ms: int) -> None:
        self.http("POST", "/session/%s/actions" % self.sid(), {"actions": [
            {"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": [
                {"type": "pointerMove", "duration": 0, "x": x, "y": y1}, {"type": "pointerDown"},
                {"type": "pause", "duration": 100}, {"type": "pointerMove", "duration": ms, "x": x, "y": y2},
                {"type": "pointerUp"}]}]})

    def swipe(self, direction: str) -> None:
        w, h = self.window()
        a, b = int(h * 0.75), int(h * 0.3)
        y1, y2 = {"up": (a, b), "down": (b, a)}[direction]
        self.drag(w // 2, y1, y2, 350)

    def refresh(self) -> None:
        """Pull to refresh, from inside the list (a drag started above the scroll view does nothing)."""
        w, h = self.window()
        self.drag(w // 2, int(h * 0.38), int(h * 0.85), 700)

    def src(self, flt=None) -> list:
        self.ui_dumps += 1
        return ui_src_lines(self.http("GET", "/source?format=xml&excluded_attributes=", timeout=120)["value"], flt)

    def shot(self, path: Path) -> Path:
        raw = path.with_suffix(".raw.png")
        raw.write_bytes(base64.b64decode(self.http("GET", "/screenshot").get("value", "")))
        try:
            run(["sips", "-Z", "900", str(raw), "--out", str(path)])
        except Blocked:
            pass
        if path.exists():
            raw.unlink()
        else:
            raw.replace(path)
        return path

    # ------------------------------------------------------------------------------------ app

    def app_state(self) -> int:
        """1 not running · 2 suspended · 3 background · 4 foreground."""
        return int(self.http("POST", "/session/%s/wda/apps/state" % self.sid(), {"bundleId": self.app_id}
                             ).get("value", 0))

    def running(self) -> bool:
        return self.app_state() >= 3

    def stop(self) -> None:
        self.http("POST", "/session/%s/wda/apps/terminate" % self.sid(), {"bundleId": self.app_id})

    def _alert(self) -> str:
        try:
            return self._raw("GET", "/session/%s/alert/text" % self.sid(), timeout=10).get("value") or ""
        except (urllib.error.URLError, OSError, ValueError):
            return ""

    def launch(self, wait_screen=None, restart: bool = False, timeout: float = 90) -> str:
        """Launches with devicectl and its console streamed into app.log (os_log at info level and above; debug lines
        do not show), accepts system alerts, and waits for `wait_screen` (any selector) or the app in front."""
        self.ensure_wda()
        self.sid()  # a session created after the launch would relaunch the app and detach the console
        since = set_mark(self.udid, self.log, "launch")
        started = time.time()
        if restart or self.app_state() < 3:
            spawn("console-" + norm(self.udid), [
                "xcrun", "devicectl", "device", "process", "launch", "--device", self.udid, "--terminate-existing",
                "--console", "--environment-variables", json.dumps({"OS_ACTIVITY_DT_MODE": "enable"}), self.app_id],
                self.log)
        else:
            self.http("POST", "/session/%s/wda/apps/activate" % self.sid(), {"bundleId": self.app_id})
        # devicectl launches asynchronously: WDA can still see the old process for a moment.
        while time.time() - started < min(15, timeout) and self.app_state() < 3:
            time.sleep(0.5)
        deadline, handled, screens = time.time() + timeout, [], []
        while time.time() < deadline:
            alert = self._alert()
            if alert:
                self.http("POST", "/session/%s/alert/accept" % self.sid(), {})
                handled.append("alert '%s'" % short(alert, 40))
                continue
            if (wait_screen and self._element_for(wait_screen)) or (not wait_screen and self.app_state() == 4):
                screens = self.screens()
                return "launched on %s%s" % (screens[-1] if screens else "the app",
                                             " (%s)" % ", ".join(handled) if handled else "")
            if time.time() - started > 8 and self.app_state() < 3:
                self.crash_check(since)
            time.sleep(1)
        raise Fail("app did not reach %s within %ds (screens: %s)" % (wait_screen or "the front", timeout,
                                                                      ", ".join(self.screens()) or "-"))

    def crash_check(self, since: int) -> None:
        if self.app_state() >= 3:
            return
        crash_verdict(read_since(self.log, since), alive=False)

    # ------------------------------------------------------------------------------------ app container files

    def _devicectl_copy(self, direction: str, source: str, destination: str):
        """devicectl copy, tried twice: it hangs now and then. When the second try hangs too, the Mac's
        CoreDeviceService is usually stuck."""
        cmd = ["xcrun", "devicectl", "device", "copy", direction, "--device", self.udid, "--domain-type",
               "appDataContainer", "--domain-identifier", self.app_id, "--source", source, "--destination", destination]
        try:
            r = run(cmd, timeout=60, text=True)
            if r.returncode == 0:
                return r
        except Blocked:
            pass
        sys.stderr.write("(devicectl copy failed or hung: trying once more)\n")
        try:
            return run(cmd, timeout=120, text=True)
        except Blocked as e:
            raise Blocked("%s. If `xcrun devicectl device info files` hangs too, restart the Mac's CoreDeviceService: "
                          "`pkill -9 -f CoreDeviceService.xpc` (launchd starts a fresh one)" % e)

    def _copy_from(self, source: str, dest: Path) -> bool:
        return self._devicectl_copy("from", source, str(dest)).returncode == 0 and dest.exists()

    def prefs_bytes(self) -> bytes:
        path = need(self.cfg, "ios", "prefs")
        with tempfile.TemporaryDirectory(prefix="prefs-read-", dir=str(self.dir)) as folder:
            dest = Path(folder) / "prefs"
            if not self._copy_from(path, dest):
                raise Blocked("cannot copy %s off %s" % (path, self.alias))
            return dest.read_bytes()

    def write_prefs(self, data: bytes) -> None:
        """devicectl skips a file it thinks unchanged: every write gets a newer modification time."""
        path = need(self.cfg, "ios", "prefs")
        with tempfile.TemporaryDirectory(prefix="prefs-write-", dir=str(self.dir)) as folder:
            src = Path(folder) / "prefs"
            src.write_bytes(data)
            modified = max(time.time() + 2, getattr(self, "_prefs_mtime", 0) + 2)
            os.utime(str(src), (modified, modified))
            self._prefs_mtime = modified
            r = self._devicectl_copy("to", str(src), path)
            if r.returncode != 0:
                raise Blocked("cannot write %s: %s" % (path, short(text(r), 300)))

    def pull_db(self) -> Path:
        """The WAL before the DB, and no -shm: a checkpoint between the two copies then leaves frames already in the
        DB, not missing ones, and SQLite rebuilds the index instead of trusting a stale -shm."""
        path = need(self.cfg, "ios", "db")
        name = path.rsplit("/", 1)[-1]
        dest = fresh_db_dir(self.dir)
        self._copy_from(path + "-wal", dest / (name + "-wal"))
        if not self._copy_from(path, dest / name):
            raise Blocked("%s not copied off %s" % (path, self.alias))
        return dest / name

    def media_path(self, relative: str) -> str:
        return "Documents/" + relative

    def put_file(self, relative: str, local: Path) -> str:
        target = self.media_path(relative)
        r = self._devicectl_copy("to", str(local), target)
        if r.returncode != 0:
            raise Blocked("cannot copy to %s: %s" % (target, short(text(r), 300)))
        return target

    def file_exists(self, path: str) -> bool:
        """A path inside the app container (Documents/…, Library/…)."""
        folder, _, name = path.rpartition("/")
        r = run(["xcrun", "devicectl", "device", "info", "files", "--device", self.udid, "--domain-type",
                 "appDataContainer", "--domain-identifier", self.app_id] + (["--subdirectory", folder] if folder else []),
                timeout=60, text=True)
        return name in r.stdout

    # ------------------------------------------------------------------------------------ network

    def wifi(self, on: bool, timeout: float = 40) -> str:
        raise Blocked("Wi-Fi cannot be switched from a script on iOS: use airplane(on, confirm=...)")

    def isolate(self, on: bool) -> str:
        raise Blocked("isolate is Android only (per-app firewall)")

    def airplane(self, on: bool, confirm=None, timeout: float = 300) -> str:
        """Not scriptable on iOS: a macOS notification asks the person at the Mac to toggle Airplane Mode, and the
        call waits for `confirm`, a regex of the line the app logs when its connectivity changes."""
        if not confirm:
            raise Blocked("airplane() on iOS needs confirm=<regex of the app's connectivity log line>: nothing else "
                          "proves the switch happened")
        from devtest_phones import notify
        offset = self.log.stat().st_size if self.log.exists() else 0
        deadline, line = time.time() + timeout, ""
        while not line and time.time() < deadline:
            notify("Turn Airplane Mode %s on %s from Control Center, then go back to the app" % (
                "on" if on else "off", self.alias))
            line = wait_log(self.log, offset, confirm, min(45, max(1, deadline - time.time())))
        if not line:
            raise Blocked("nobody turned Airplane Mode %s within %ds (no /%s/ in the app log)" % (
                "on" if on else "off", timeout, confirm))
        return "Airplane Mode %s by hand: %s" % ("on" if on else "off", short(line.strip(), 160))

    # ------------------------------------------------------------------------------------ build

    def build(self, top: Path) -> str:
        return build(top, "ios", self.cfg)

    def install(self, top: Path) -> str:
        app = top / need(self.cfg, "ios", "app")
        if not app.is_dir():
            raise Blocked("%s does not exist: run `build` first" % app)
        last = ""
        for attempt in range(1, 5):
            r = run(["xcrun", "devicectl", "device", "install", "app", "--device", self.udid, str(app)], timeout=600,
                    text=True)
            if r.returncode == 0:
                rec = install_receipt(top, "ios", app)
                save_json(self.dir / "installed.json", rec)
                return "installed %s @ %s on %s" % (top.name, rec["commit"], self.alias)
            lines = text(r).splitlines()
            last = next((l for l in lines if "rror" in l), lines[-1] if lines else "")
            time.sleep(3)
        raise Blocked("install failed 4 times: %s. Unlock the phone and check the cable" % short(last, 300))

    def build_info(self, top: Path) -> str:
        rec = load_json(self.dir / "installed.json", None)
        why = stale_reason(rec, top, worktree_changed_at(top))
        if why:
            raise Blocked(why)
        return "build ok: %s @ %s (installs done outside devtest are not detected on iOS)" % (top.name, rec["commit"])


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "_forward":
    forward(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
