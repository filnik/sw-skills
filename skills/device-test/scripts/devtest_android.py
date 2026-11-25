"""Android half of devtest: adb only, no Appium (an Appium UiAutomator2 session must not be running)."""

from __future__ import annotations

import gzip
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from devtest_common import (Blocked, Fail, app_id, cfg, crash_verdict, device_dir, fresh_db_dir, grep_lines, hhmm,
                            launch_rules, load_json, need, read_since, run, save_json, set_mark, short, source_archive,
                            stale_reason, text, wait_log, worktree_changed_at)

ALLOW_BUTTONS = ("com.android.permissioncontroller:id/permission_allow_foreground_only_button",
                 "com.android.permissioncontroller:id/permission_allow_button")


class Node:
    def __init__(self, el, order: int):
        self.id = el.get("resource-id", "")
        self.text = el.get("text", "")
        self.desc = el.get("content-desc", "")
        self.cls = el.get("class", "").rsplit(".", 1)[-1]
        self.checked = el.get("checked") == "true"
        if el.get("checkable") != "true":
            # A tagged wrapper (e.g. switchModifier) holds its untagged checkable child: read the state there.
            inner = next((e for e in el.iter() if e.get("checkable") == "true"), None)
            if inner is not None:
                self.checked = inner.get("checked") == "true"
        self.selected = el.get("selected") == "true"
        self.enabled = el.get("enabled") == "true"
        self.focused = el.get("focused") == "true"
        x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", el.get("bounds", "[0,0][0,0]")))
        self.bounds = (x1, y1, x2, y2)
        self.order = order
        self.el = el

    def label(self) -> str:
        return " | ".join(dict.fromkeys(v for v in (self.id, self.text, self.desc) if v))

    def subtree_text(self) -> str:
        return " ".join(v for e in self.el.iter() for v in (e.get("text", ""),) if v)


def dump_caller() -> str:
    """Who asked for a UI dump, for metrics.json: the outermost and innermost public flows helpers, else the Driver
    method the scenario called, else the runner."""
    helpers, driver, f = [], None, sys._getframe(1)
    while f:
        path, name = f.f_code.co_filename, f.f_code.co_name
        if os.sep + "flows" + os.sep in path and not name.startswith(("_", "<")):
            helpers.append("%s.%s" % (Path(path).stem, name))
        elif path.endswith("devtest_scenario.py") and type(f.f_locals.get("self")).__name__ == "Driver":
            driver = "x." + name
        f = f.f_back
    if helpers:
        return helpers[-1] if len(helpers) == 1 or helpers[0] == helpers[-1] else "%s > %s" % (helpers[-1], helpers[0])
    return driver or "runner"


def adb_transport(serial: str) -> str:
    """The adb transport that reaches the phone with this serial. The ip:port of the same phone after
    `adb tcpip 5555` + `adb connect` wins over USB when both are up (a USB link that drops every few
    minutes while Wi-Fi holds); the phone is told apart by its ro.serialno. A phone on wireless debugging that adb has not
    connected yet is found through mDNS and connected: it picks a new port each time wireless debugging comes back on,
    e.g. after airplane mode, and mDNS can still list the old one or skip the phone on a single query
    (2 of 5 on a Xiaomi), so it is asked a few times. Otherwise the serial itself, so an absent phone still gives the
    usual BLOCKED.
    A host adb-daemon reset
    forgets tcpip:5555 connections, which have no mDNS service. Remember only an endpoint
    whose ro.serialno was verified, and reconnect/reverify that same phone on a later run.
    """
    remembered = device_dir(serial) / "wifi-transport.json"
    lines = run(["adb", "devices"], text=True).stdout.splitlines()[1:]
    online = [l.split()[0] for l in lines if l.endswith("device")]
    for t in online:
        if ":" in t and run(["adb", "-s", t, "shell", "getprop", "ro.serialno"], timeout=10,
                            text=True).stdout.strip() == serial:
            save_json(remembered, {"endpoint": t})
            return t
    if serial in online:
        return serial
    endpoint = load_json(remembered, {}).get("endpoint")
    if endpoint:
        try:
            connected = run(["adb", "connect", endpoint], timeout=8, text=True).stdout
            if re.match(r"(already )?connected to", connected) and run(
                ["adb", "-s", endpoint, "shell", "getprop", "ro.serialno"], timeout=10,
                text=True).stdout.strip() == serial:
                return endpoint
        except Blocked:
            pass
    try:
        for attempt in range(6):
            if attempt:
                time.sleep(1)
            for s in run(["adb", "mdns", "services"], timeout=10, text=True).stdout.splitlines():
                if "adb-%s-" % serial in s and "_adb-tls-connect._tcp" in s:
                    endpoint = s.split()[-1]
                    if re.match(r"(already )?connected to", run(["adb", "connect", endpoint], timeout=8,
                                                                 text=True).stdout):
                        return endpoint
    except Blocked:
        pass
    return serial


class Android:
    @property
    def app(self) -> str:
        return app_id("android")

    platform = "android"

    def __init__(self, serial: str, alias: str):
        # The serial names the phone (leases, state dir, marks); the transport is how adb reaches it right now.
        self.serial, self.alias = serial, alias
        self.transport = adb_transport(serial)
        self.dir = device_dir(serial)
        self.log = self.dir / "app.log"
        self._geometry = None

    # ------------------------------------------------------------------------------------ adb

    def adb(self, *args, timeout: float = 60, binary: bool = False):
        """Retry read-only exec-out once after an ADB daemon reset; never replay input or writes.
        A prefs/DB read is lost during a daemon restart while the phone and its DB are readable right afterward.
        """
        command = ["adb", "-s", self.transport, *args]
        r = run(command, timeout=timeout, text=not binary)
        read_only = ((len(args) >= 4 and args[:3] == ("exec-out", "run-as", self.app)
                      and args[3] in ("cat", "gzip"))
                     or args[:3] == ("exec-out", "uiautomator", "dump")
                     or args[:2] == ("exec-out", "screencap"))
        if read_only and r.returncode != 0 and any(
            message in text(r).lower() for message in
            ("protocol fault", "connection reset", "daemon not running", "connection closed")
        ):
            print("(ADB transport reset on %s: retrying read-only exec-out once)" % self.alias,
                  file=sys.stderr, flush=True)
            r = run(command, timeout=timeout, text=not binary)
        err = ((r.stderr.decode(errors="replace") if binary else text(r)) if r.returncode != 0 else "")
        # A preflight may outlive the host's adb daemon. A package-info read
        # is safe to retry after reconnecting; never replay input, installs or writes.
        package_info = len(args) == 2 and args[0] == "shell" and args[1].startswith("dumpsys package ")
        if r.returncode != 0 and (read_only or package_info) and re.search(r"device .* not found|no devices", err):
            self.reconnect()
            r = run(["adb", "-s", self.transport, *args], timeout=timeout, text=not binary)
            err = ((r.stderr.decode(errors="replace") if binary else text(r)) if r.returncode != 0 else "")
        if r.returncode != 0 and re.search(r"device .* not found|unauthorized|offline|no devices", err):
            raise Blocked("%s (%s) is not reachable over adb: %s" % (self.alias, self.transport, short(err)))
        return r

    def reconnect(self) -> str:
        """Looks the transport up again after the adb link dropped (a USB cable, wireless debugging off and back on)."""
        self.transport = adb_transport(self.serial)
        return self.transport

    def reachable(self) -> bool:
        """On adb and still captured: a phone that dropped off and came back has lost its logcat capture."""
        on_adb = run(["adb", "-s", self.transport, "get-state"], timeout=15, text=True).stdout.strip() == "device"
        return on_adb and self._capture_alive()

    def sh(self, cmd: str, timeout: float = 60) -> str:
        return self.adb("shell", cmd, timeout=timeout).stdout.strip()

    def pid(self) -> str:
        return self.sh("pidof %s" % self.app)

    def uid(self) -> str:
        m = re.search(r"uid:(\d+)", self.sh("pm list packages -U %s" % self.app))
        if not m:
            raise Blocked("%s is not installed on %s" % (self.app, self.alias))
        return m.group(1)

    # ------------------------------------------------------------------------------------ log capture

    def _capture_alive(self) -> bool:
        pid = load_json(self.dir / "procs.json", {}).get("logcat")
        if not pid:
            return False
        r = run(["ps", "-p", str(pid), "-o", "command="], text=True)
        return "logcat" in r.stdout and self.transport in r.stdout

    def ensure_capture(self) -> None:
        """Stream by uid across app restarts; resume original device timestamps after capture death.
        A global adb-server restart kills this phone's capture
        while the app goes on. -T from the last captured timestamp backfills the device ring buffer.
        Scenarios still require complete step boundaries; ring-buffer loss is never synthesized.
        """
        if self._capture_alive():
            return
        procs = load_json(self.dir / "procs.json", {})
        uid = self.uid()
        since = "1"
        if procs.get("logcat") and self.log.exists():
            with self.log.open("rb") as handle:
                handle.seek(max(0, self.log.stat().st_size - 65536))
                tail = handle.read().decode(errors="replace").splitlines()
            for line in reversed(tail):
                stamp = re.match(r"\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}", line)
                if stamp:
                    since = stamp.group()
                    save_json(self.dir / "capture-resume.json", {"at": time.time(), "since": since, "uid": uid,
                                                               "offset": self.log.stat().st_size})
                    break
        with open(self.log, "a") as f:
            f.write("==== capture %s uid %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), uid))
            f.flush()
            p = subprocess.Popen(["adb", "-s", self.transport, "logcat", "-v", "threadtime", "-T", since, "--uid=%s" % uid],
                                 stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        procs["logcat"] = p.pid
        save_json(self.dir / "procs.json", procs)
        time.sleep(0.5)
        if p.poll() is not None:
            raise Blocked("logcat --uid did not start on %s: see %s" % (self.alias, self.log))

    def stop_capture(self) -> None:
        procs = load_json(self.dir / "procs.json", {})
        pid = procs.pop("logcat", None)
        if pid and self._capture_alive():
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except OSError:
                pass
        save_json(self.dir / "procs.json", procs)

    # ------------------------------------------------------------------------------------ UI

    def dump(self) -> list:
        last, woken = "", False
        caller = dump_caller()
        for _ in range(4):
            self.ui_dump_count = getattr(self, "ui_dump_count", 0) + 1
            t0 = time.time()
            r = self.adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=30)
            took = time.time() - t0
            self.ui_dump_seconds = getattr(self, "ui_dump_seconds", 0.0) + took
            spent = self.__dict__.setdefault("ui_dumps_by_helper", {}).setdefault(caller, [0, 0.0])
            spent[0], spent[1] = spent[0] + 1, spent[1] + took
            xml = r.stdout
            start, end = xml.find("<?xml"), xml.rfind("</hierarchy>")
            if start >= 0 and end > start:
                root = ET.fromstring(xml[start:end + len("</hierarchy>")])
                packages = {el.get("package") for el in root.iter("node")}
                if not woken and self.app not in packages and packages & {"com.android.systemui", "com.samsung.android.app.aodservice"}:
                    self.wake()  # the screen went off or locked under us
                    woken = True
                    continue
                if any("unintentional_" in (el.get("resource-id") or "") for el in root.iter("node")):
                    self._accidental_touch()
                    continue
                self._accidental_tries = 0
                return [Node(el, i) for i, el in enumerate(root.iter("node"))]
            last = short(text(r))
            time.sleep(0.7)
        raise Blocked("uiautomator dump failed on %s (%s): an Appium session or a never-idle animation "
                      "blocks it" % (self.alias, last))

    def _accidental_touch(self) -> None:
        """Samsung's Accidental touch protection covers the screen while the proximity sensor is covered, and a
        step times out under it. One swipe up dismisses it; when it is still up the sensor is
        covered, which only a person can fix."""
        tries = getattr(self, "_accidental_tries", 0)
        self._accidental_tries = tries + 1
        if tries >= 1:
            self._accidental_tries = 0
            raise Blocked("Samsung 'Accidental touch protection' covers the screen of the %s: its proximity sensor is "
                          "covered (face down, in a case or a pocket?). Uncover the top of the phone, or turn the "
                          "feature off in Settings > Display" % self.alias)
        w, h = self.geometry()[:2]
        self.sh("input swipe %d %d %d %d 300" % (w // 2, int(h * 0.75), w // 2, int(h * 0.25)))
        time.sleep(1)

    def geometry(self) -> tuple:
        """(width, height, safe_top, safe_bottom): taps outside the bars only."""
        if self._geometry:
            return self._geometry
        cached = load_json(self.dir / "geometry.json", None)
        if cached:
            self._geometry = tuple(cached)
            return self._geometry
        size = re.search(r"(\d+)x(\d+)", self.sh("wm size").splitlines()[-1])
        w, h = int(size.group(1)), int(size.group(2))
        top, bottom = int(h * 0.04), int(h * 0.94)
        win = self.sh("dumpsys window", timeout=30)
        m = re.search(r"(?:ITYPE_NAVIGATION_BAR|navigationBars)[^\n]*?frame=\[\d+,(\d+)\]\[\d+,(\d+)\]", win)
        if m and int(m.group(1)) > h // 2:
            bottom = int(m.group(1))
        m = re.search(r"(?:ITYPE_STATUS_BAR|statusBars)[^\n]*?frame=\[\d+,\d+\]\[\d+,(\d+)\]", win)
        if m:
            top = int(m.group(1))
        self._geometry = (w, h, top, bottom)
        save_json(self.dir / "geometry.json", list(self._geometry))
        return self._geometry

    @staticmethod
    def matches(n: Node, sel: str) -> bool:
        if sel.startswith("text:"):
            return n.text == sel[5:]
        if sel.startswith("text~"):
            return sel[5:].lower() in n.text.lower()
        if sel.startswith("desc:"):
            return n.desc == sel[5:]
        if sel.startswith("desc~"):
            return sel[5:].lower() in n.desc.lower()
        return n.id == sel

    def find(self, sel: str, nodes=None):
        nodes = nodes if nodes is not None else self.dump()
        return next((n for n in nodes if self.matches(n, sel)), None)

    def screens(self, nodes=None) -> list:
        nodes = nodes if nodes is not None else self.dump()
        return [n.id for n in nodes if n.id.startswith("screen.")]

    def wait_for(self, sel: str, timeout: float = 10):
        deadline = time.time() + timeout
        while True:
            nodes = self.dump()
            n = self.find(sel, nodes)
            if n is not None or time.time() >= deadline:
                return n, nodes
            time.sleep(0.5)

    def _tap_point(self, n: Node):
        w, h, top, bottom = self.geometry()
        x1, y1, x2, y2 = n.bounds
        vy1, vy2 = max(y1, top), min(y2, bottom)
        vx1, vx2 = max(x1, 0), min(x2, w)
        if vy2 - vy1 < 12 or vx2 - vx1 < 12:
            return None
        return (vx1 + vx2) // 2, (vy1 + vy2) // 2

    def tap(self, sel: str, timeout: float = 10) -> str:
        for attempt in range(4):
            n, nodes = self.wait_for(sel, timeout if attempt == 0 else 3)
            if n is None:
                raise Fail("%s not on screen within %ds (screens: %s)" % (sel, timeout, ", ".join(self.screens(nodes)) or "-"))
            point = self._tap_point(n)
            if point:
                self.sh("input tap %d %d" % point)
                return "tapped %s at %d,%d" % (sel, point[0], point[1])
            # Hidden under a system bar or off screen: bring it towards the middle.
            w, h, top, bottom = self.geometry()
            mid = (top + bottom) // 2
            cy = (n.bounds[1] + n.bounds[3]) // 2
            dy = max(min(cy - mid, int((bottom - top) * 0.6)), -int((bottom - top) * 0.6))
            self.sh("input swipe %d %d %d %d 400" % (w // 2, mid, w // 2, mid - dy))
            time.sleep(0.6)
        raise Fail("%s stays under a system bar or off screen after 3 scrolls" % sel)

    def tap_seen(self, sel: str, nodes) -> str:
        """Taps `sel` where a dump already taken shows it, without the new dump tap() takes (~2s over adb Wi-Fi): for
        a tap racing the app (a sheet's X that must land before the work behind it ends).
        tap() when that dump lacks it or it sits under a system bar."""
        n = self.find(sel, nodes)
        point = self._tap_point(n) if n is not None else None
        if not point:
            return self.tap(sel)
        self.sh("input tap %d %d" % point)
        return "tapped %s at %d,%d" % (sel, point[0], point[1])

    def node(self, sel: str, timeout: float = 0):
        n, _ = self.wait_for(sel, timeout)
        return n

    def visible(self, sel: str, timeout: float = 0) -> bool:
        return self.node(sel, timeout) is not None

    def keyboard_shown(self) -> bool:
        return "mInputShown=true" in self.sh("dumpsys input_method | grep mInputShown")

    def hide_keyboard(self) -> None:
        if self.keyboard_shown():
            self.sh("input keyevent KEYCODE_BACK")
            time.sleep(0.4)

    @staticmethod
    def field_value(n) -> str:
        """What a text field holds: its EditText. A tagged wrapper also carries the label and the clear button
        ('Name * Garage 1'), so the whole subtree is only the fallback when there is no EditText."""
        if n is None:
            return ""
        if n.cls == "EditText":
            return n.text
        edit = next((e for e in n.el.iter() if e.get("class", "").endswith("EditText")), None)
        return edit.get("text", "") if edit is not None else n.subtree_text()

    def focused(self, sel: str) -> bool:
        n = self.find(sel)
        return n is not None and any(e.get("focused") == "true" for e in n.el.iter())

    def type_keys(self, value: str, paced: bool) -> None:
        """Gboard (Pixel 9, Android 17) needs both ways. A burst loses the key before a space
        ('Garage 12 b' -> 'Garag12 b' three times); one `input` per key (~0.2 s each) types it right but
        is too slow for a search field that recomposes when the first key turns search mode on,
        so the next SPACE clicks the button beside it. So: a burst first, paced keys on the retries."""
        if not paced:
            self.sh("input text %s" % shlex.quote(value.replace(" ", "%s")))
            return
        keys = ["input keyevent KEYCODE_SPACE" if c == " " else "input text %s" % shlex.quote(c) for c in value]
        self.sh("; sleep 0.05; ".join(keys), timeout=30 + 2 * len(value))

    def paste_text(self, value: str) -> None:
        """Native clipboard paste into the focused input; deliberately does not assert the resulting value.
        A 2100-character adb input burst left only a 317-character
        prefix. Paste delivers one input transaction, so a field's truncation can be tested reliably.
        """
        import base64
        import hashlib
        source = Path(__file__).parent / "android" / "DevtestClipboard.java"
        sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
                   or Path.home() / "Library/Android/sdk")
        jars = sorted(sdk.glob("platforms/*/android.jar"))
        compilers = sorted(sdk.glob("build-tools/*/d8"))
        if not jars or not compilers:
            raise Blocked("Android SDK android.jar and d8 are required for native paste")
        cache = self.dir / ("clipboard-" + hashlib.sha256(source.read_bytes()).hexdigest()[:12])
        cache.mkdir(parents=True, exist_ok=True)
        dex = cache / "classes.dex"
        if not dex.exists():
            commands = (["javac", "-source", "8", "-target", "8", "-cp", str(jars[-1]),
                         "-d", str(cache), str(source)],
                        [str(compilers[-1]), "--lib", str(jars[-1]), "--output", str(cache),
                         str(cache / "DevtestClipboard.class")])
            for command in commands:
                result = run(command, timeout=60, text=True)
                if result.returncode:
                    raise Blocked("clipboard bridge build failed: %s" % short(text(result), 500))
        remote = "/data/local/tmp/devtest-clipboard.dex"
        result = self.adb("push", str(dex), remote)
        if result.returncode:
            raise Blocked("clipboard bridge transfer failed: %s" % short(text(result), 300))
        encoded = base64.b64encode(value.encode()).decode()
        result = self.adb("shell", "CLASSPATH=%s app_process / DevtestClipboard %s" %
                          (remote, shlex.quote(encoded)))
        if result.returncode or "clipboard set:" not in result.stdout:
            raise Blocked("native clipboard write failed: %s" % short(text(result), 300))
        self.sh("input keyevent KEYCODE_PASTE")

    def set_text(self, sel: str, value: str, timeout: float = 10, verify: bool = True, paste: bool = False) -> str:
        """Real keyboard input; verify=False defers value proof to the caller's persisted-state assertion.
        The post-input UI dump takes seconds, too long before a timed departure.
        Focus/keyboard checks still precede input; only the final dump can be deferred for timed departures.
        paste=True avoids Gboard changing repeated tokens in an input burst.
        """
        if not value.isascii():
            raise Blocked("adb input text only types ASCII: %r" % value)
        seen = ""
        for attempt in (1, 2, 3):
            # Gboard (Pixel 9, Android 17, on an auto-focused field): keys typed into a
            # field focused before our tap went to an inactive InputConnection ('commitText on inactive
            # InputConnection'): 'Garage 1' -> 'Garag1', and the retype changed nothing. Hide the keyboard and tap again
            # so the IME binds afresh, before the first try and after every miss.
            if attempt > 1 or self.keyboard_shown():
                self.hide_keyboard()
            self.tap(sel, timeout if attempt == 1 else 5)
            # Ctrl+A before the keyboard is connected selects nothing and DEL eats only the last character
            # ('NAME 2000a G' -> 'NAME 2000a NAME 2000a G e'): wait for it.
            # Type only into a focused field: a list reloading right after the tap took the focus away
            # (HIDE_SOFT_INPUT_BY_INSETS_API 130 ms after the tap) and the typed SPACE clicked the
            # button beside it. Checked again after the settle, since the focus went away after it was first seen.
            t0 = time.time()
            while not (self.keyboard_shown() and self.focused(sel)) and time.time() - t0 < 3:
                time.sleep(0.2)
            time.sleep(0.5)
            if not (self.keyboard_shown() and self.focused(sel)):
                seen = "(%s not focused with the keyboard up)" % sel
                continue
            self.sh("input keycombination KEYCODE_CTRL_LEFT KEYCODE_A")
            self.sh("input keyevent KEYCODE_DEL")
            if value:
                if paste:
                    self.paste_text(value)
                else:
                    self.type_keys(value, paced=attempt > 1)
            if not verify:
                return "typed %r into %s; caller must verify stored value after departure" % (value, sel)
            deadline = time.time() + 5
            while time.time() < deadline:
                n = self.find(sel)
                seen = self.field_value(n)
                if seen == value or (n and n.text == value):
                    return "text of %s = %r%s" % (sel, value, " (try %d)" % attempt if attempt > 1 else "")
                time.sleep(0.4)
        raise Fail("%s shows %r after typing %r (3 tries)" % (sel, seen, value))

    def back(self) -> None:
        self.sh("input keyevent KEYCODE_BACK")

    def swipe(self, direction: str) -> None:
        w, h, top, bottom = self.geometry()
        a, b = int(top + (bottom - top) * 0.75), int(top + (bottom - top) * 0.3)
        y1, y2 = {"up": (a, b), "down": (b, a)}[direction]
        self.sh("input swipe %d %d %d %d 350" % (w // 2, y1, w // 2, y2))

    def refresh(self) -> None:
        """Pull to refresh: a slow drag from just below the top bar."""
        w, h, top, bottom = self.geometry()
        self.sh("input swipe %d %d %d %d 600" % (w // 2, top + int(h * 0.25), w // 2, top + int(h * 0.7)))

    def src(self, flt=None) -> list:
        nodes = self.dump()
        rows = []
        for n in nodes:
            if n.id.startswith("screen.") or not (n.id or n.text or n.desc):
                continue
            x1, y1, x2, y2 = n.bounds
            state = "".join(s for s, on in ((" [on]", n.checked), (" [sel]", n.selected), (" [off]", not n.enabled)) if on)
            line = "%-10.10s (%d,%d) %s%s" % (n.cls, (x1 + x2) // 2, (y1 + y2) // 2, short(n.label(), 150), state)
            if not flt or flt.lower() in line.lower():
                rows.append(line)
        return ["SCREENS: " + (", ".join(dict.fromkeys(self.screens(nodes))) or "-")] + list(dict.fromkeys(rows))

    def shot(self, path: Path) -> Path:
        raw = path.with_suffix(".raw.png")
        r = self.adb("exec-out", "screencap", "-p", binary=True)
        raw.write_bytes(r.stdout)
        run(["sips", "-Z", "900", str(raw), "--out", str(path)])
        if path.exists():
            raw.unlink()
        else:
            raw.replace(path)
        return path

    # ------------------------------------------------------------------------------------ app

    def running(self) -> bool:
        return bool(self.pid())

    def stop(self) -> None:
        self.sh("am force-stop %s" % self.app)

    def keyguard_showing(self) -> bool:
        win = self.sh("dumpsys window policy | grep -iE 'showing=|isKeyguardShowing|mShowingLockscreen'")
        return bool(re.search(r"(?i)(isKeyguardShowing|mShowingLockscreen|showing)=true", win))

    def wake(self) -> None:
        """Screen on and kept on for long waits (a phone that is not powered over USB is not held by
        stayon alone), lock screen dismissed. `lease release` puts the original screen timeout back."""
        timeout = self.sh("settings get system screen_off_timeout")
        if timeout.isdigit() and int(timeout) < 600000:
            saved = self.dir / "screen_timeout.json"
            if not saved.exists():
                save_json(saved, {"screen_off_timeout": timeout})
            self.sh("settings put system screen_off_timeout 1800000")
        self.sh("svc power stayon usb")
        self.sh("input keyevent KEYCODE_WAKEUP")
        if self.keyguard_showing():
            self.sh("wm dismiss-keyguard")
            time.sleep(1)
        if self.keyguard_showing():
            raise Blocked("%s is locked with a PIN or pattern: unlock it by hand" % self.alias)

    def restore_screen(self) -> str:
        notes = []
        if (self.dir / "isolated.json").exists():  # a run that died while the app was cut off the network
            notes.append(self.isolate(False))
        if (self.dir / "metered.json").exists():  # a run that died with the Wi-Fi marked metered
            notes.append(self.metered(False))
        saved = self.dir / "screen_timeout.json"
        original = load_json(saved, {}).get("screen_off_timeout")
        if not original:
            return "; ".join(notes)
        self.sh("settings put system screen_off_timeout %s" % original)
        self.sh("svc power stayon false")
        saved.unlink()
        return "; ".join(notes + ["screen timeout back to %ss" % (int(original) // 1000)])

    def launch(self, env=None, wait_screen=None, restart: bool = False, timeout: float = 60) -> str:
        rules, env = launch_rules("android"), env or cfg("default_env")
        # System UI's notification shade can cover a healthy app while dumpsys still names the app as
        # foreground, and am start alone does not collapse the shade.
        self.wake()
        self.sh("cmd statusbar collapse")
        if restart:
            self.stop()
        self.ensure_capture()
        set_mark(self.serial, self.log, "launch")
        self.sh("am start -n %s/%s" % (self.app, need("android.activity")))
        deadline, handled = time.time() + timeout, []
        while time.time() < deadline:
            nodes = self.dump()
            screens = self.screens(nodes)
            ids = {n.id for n in nodes}
            # Android 17 warns on every launch of a build with a native library that is not
            # 16 KB aligned. Dismiss this exact page-size warning only;
            # keep the system's persistent "do not show again" setting unchanged.
            messages = [n.text for n in nodes if n.id == "android:id/message"]
            if ("android:id/alertTitle" in ids and "android:id/button2" in ids
                    and any("dimensioni pagina di 16 kB" in m or "16 KB page size" in m for m in messages)):
                try:
                    self.tap("android:id/button2", 3)
                    handled.append("16 KB compatibility warning: OK")
                except Fail:
                    pass  # warning disappeared while the launch's UI was settling
                continue
            if rules.get("env_screen") and rules["env_screen"] in screens:
                try:
                    self.tap(rules["env_option"] % env, 5)
                    self.tap(rules["env_continue"], 10)
                    handled.append("env=%s" % env)
                except Fail:
                    pass  # the screen moved on or is still settling: look again
                continue
            allow = next((b for b in ALLOW_BUTTONS if b in ids), None)
            if allow:
                self.tap(allow, 3)
                handled.append("permission")
                continue
            texts = {n.text for n in nodes}
            # a launch.dismiss rule: its `tap` target is on screen and, when it lists `texts`, one of them is too
            rule = next((r for r in rules.get("dismiss") or []
                         if (r["tap"][5:] in texts if r["tap"].startswith("text:") else r["tap"] in ids)
                         and (not r.get("texts") or texts & set(r["texts"]))), None)
            if rule:
                self.tap(rule["tap"], 3)
                handled.append(rule.get("note") or rule["tap"])
                continue
            if rules.get("login_screen") in screens and wait_screen != rules["login_screen"]:
                raise Blocked("the app is logged out on %s: log in by hand%s" % (
                    self.alias, " (%s)" % rules["login_hint"] if rules.get("login_hint") else ""))
            settled = [s for s in screens if s not in (rules.get("ignored_screens") or ())]
            if not wait_screen and rules.get("screen_tags") is False and self.foreground() == self.app:
                settled = ["the app"]  # an app with no screen.* tags: in front is as far as launch can tell
            if (wait_screen and wait_screen in screens) or (not wait_screen and settled):
                return "launched pid %s on %s%s" % (self.pid(), settled[-1] if settled else "?",
                                                   " (%s)" % ", ".join(handled) if handled else "")
            if not self.running():
                self.crash_check(0)
            time.sleep(0.8)
        raise Fail("app did not reach %s within %ds (screens: %s)" % (wait_screen or "a screen", timeout, ", ".join(screens) or "-"))

    def crash_check(self, since: int) -> None:
        alive = self.running()
        lines = read_since(self.log, since)
        if not alive:
            lines += self.sh("logcat -d -b crash -t 200").splitlines()
        if alive and not grep_lines(lines, "crash"):
            return
        crash_verdict(lines, alive)

    # ------------------------------------------------------------------------------------ state

    def prefs_bytes(self) -> bytes:
        r = self.adb("exec-out", "run-as", self.app, "cat", need("android.prefs"), binary=True)
        if r.returncode != 0 or r.stdout.startswith(b"run-as:") or r.stdout.startswith(b"cat:"):
            raise Blocked("cannot read %s: %s" % (cfg("android.prefs"), short(text(r))))
        return r.stdout

    def write_prefs(self, data: bytes) -> None:
        tmp = self.dir / "prefs-write.pb"
        tmp.write_bytes(data)
        self.adb("push", str(tmp), "/data/local/tmp/devtest.pb")
        self.sh("chmod 644 /data/local/tmp/devtest.pb")
        out = self.sh("run-as %s cp /data/local/tmp/devtest.pb %s" % (self.app, need("android.prefs")))
        if out:
            raise Blocked("could not write %s: %s" % (cfg("android.prefs"), short(out)))
        self.sh("rm -f /data/local/tmp/devtest.pb")

    def pull_db(self) -> Path:
        """A fresh copy of the app's DB, pulled once more when adb stalls: over adb on Wi-Fi a `cat` of the WAL hung
        until its 60s timeout once, right after an upload that had completed."""
        try:
            return self._pull_db()
        except Blocked as e:
            if "timed out" not in str(e):
                raise
            print("(adb stalled pulling the DB on %s: pulling it once more)" % self.alias, file=sys.stderr, flush=True)
            return self._pull_db()

    def _pull_db(self) -> Path:
        """A 12 MB DB over wireless adb stalls raw cat.
        Stream gzip when available (1.5 MB there), decompress locally; old phones retain raw cat.
        """
        db = need("android.db")
        base = db.rsplit("/", 1)[-1]
        dest = fresh_db_dir(self.dir)
        for suffix in ("", "-wal", "-shm"):
            name = base + suffix
            r = self.adb("exec-out", "run-as", self.app, "gzip", "-c", db + suffix, binary=True)
            if r.returncode == 0 and r.stdout.startswith(b"\x1f\x8b"):
                try:
                    (dest / name).write_bytes(gzip.decompress(r.stdout))
                except (OSError, EOFError) as exc:
                    raise Blocked("truncated compressed DB transfer on %s: %s" % (self.alias, name)) from exc
            elif r.returncode != 0 and b"not found" in r.stderr and b"gzip" in r.stderr:
                r = self.adb("exec-out", "run-as", self.app, "cat", db + suffix, binary=True)
                if r.returncode == 0 and not r.stdout.startswith(b"cat:"):
                    (dest / name).write_bytes(r.stdout)
        if not (dest / base).exists():
            raise Blocked("%s not readable on %s" % (base, self.alias))
        return dest / base

    def media_path(self, relative: str) -> str:
        """The app's external files folder, which it stores relative media paths against."""
        return "/sdcard/Android/data/%s/files/%s" % (self.app, relative)

    def put_file(self, relative: str, local: Path) -> str:
        target = self.media_path(relative)
        self.sh("mkdir -p %s" % shlex.quote(target.rsplit("/", 1)[0]))
        r = self.adb("push", str(local), target)
        if r.returncode != 0:
            raise Blocked("adb push to %s failed: %s" % (target, short(text(r))))
        return target

    def remove_file(self, path: str) -> None:
        self.sh("rm -f %s" % shlex.quote(path))

    def file_exists(self, path: str) -> bool:
        """`files/…` and `databases/…` are app-private; /sdcard paths are read directly.
        A media path the DB stores relative to the app's external files directory (the pack's media_dirs
        prefixes) is not relative to run-as's working directory.
        """
        if path.startswith(tuple(cfg("media_dirs") or ())):
            path = self.media_path(path)
        if path.startswith("/"):
            return self.sh("ls %s >/dev/null 2>&1 && echo yes" % shlex.quote(path)) == "yes"
        return self.sh("run-as %s ls %s >/dev/null 2>&1 && echo yes" % (self.app, shlex.quote(path))) == "yes"

    # ------------------------------------------------------------------------------------ device

    def wifi_enabled(self) -> bool:
        """Actual radio state, including airplane mode.
        Xiaomi reports wifi_on=3 with Wi-Fi disabled, so the setting is not a boolean.
        """
        status = self.sh("dumpsys wifi | grep -m1 '^Wi-Fi is '").strip()
        if status == "Wi-Fi is enabled":
            return True
        if status == "Wi-Fi is disabled":
            return False
        raise Blocked("cannot read actual Wi-Fi state: %s" % status)

    def wifi(self, on: bool, timeout: float = 40) -> str:
        if not on and ":" in self.transport:
            raise Blocked("adb reaches %s over Wi-Fi (%s): turning Wi-Fi off would cut it; connect the phone over USB "
                          "(`adb -s %s usb`) for offline steps" % (self.alias, self.transport, self.transport))
        self.sh("svc wifi %s" % ("enable" if on else "disable"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.sh("dumpsys wifi | grep -m1 'mWifiInfo SSID'")
            enabled = self.wifi_enabled()
            if on and "COMPLETED" in info and "<unknown ssid>" not in info:
                ssid = re.search(r'SSID: "?([^",]*)', info)
                return "wifi on, connected to %s" % (ssid.group(1) if ssid else "?")
            if not on and not enabled:
                return "wifi off"
            time.sleep(1)
        raise Blocked("wifi did not turn %s within %ds" % ("on" if on else "off", timeout))

    def default_net_caps(self) -> str:
        """Capabilities of the default network ("" when none): its NetworkAgentInfo in `dumpsys connectivity`."""
        out = self.sh("dumpsys connectivity", timeout=30)
        m = re.search(r"Active default network: (\d+)", out)
        if not m:
            return ""
        nai = re.search(r"NetworkAgentInfo\{network\{%s\}.*?Capabilities: ([A-Z_&]+)" % m.group(1), out)
        return nai.group(1) if nai else ""

    def metered(self, on: bool, timeout: float = 60) -> str:
        """The Wi-Fi the phone is on marked metered (`cmd netpolicy set metered-network SSID true`), or its override
        put back as it was: the default network then lacks NET_CAPABILITY_NOT_METERED, as cellular or another phone's
        hotspot does. Waits until the default network is validated with the wanted cost (the override reconnects the
        Wi-Fi briefly: Xiaomi HyperOS). The SSID and the original override live in metered.json, so
        `lease release` puts back one a dead run left."""
        flag = self.dir / "metered.json"
        if on:
            info = self.sh("dumpsys wifi | grep -m1 'mWifiInfo SSID'")
            m = re.search(r'SSID: "([^"]+)"', info)
            if not m:
                raise Blocked("%s is on no Wi-Fi to mark metered: %s" % (self.alias, short(info, 120)))
            ssid = m.group(1)
            if not flag.exists():
                rows = self.sh("cmd netpolicy list wifi-networks")
                orig = next((ln.split(";")[-1] for ln in rows.splitlines() if ln.split(";")[0] == ssid), "none")
                save_json(flag, {"ssid": ssid, "orig": "undefined" if orig in ("none", "") else orig})
            self.sh("cmd netpolicy set metered-network %s true" % shlex.quote(ssid))
        else:
            saved = load_json(flag, {})
            if not saved:
                return "no metered override set by devtest"
            self.sh("cmd netpolicy set metered-network %s %s" % (shlex.quote(saved["ssid"]), saved["orig"]))
            if saved["orig"] == "true":
                on = True  # it was metered before devtest: wait for that
        deadline = time.time() + timeout
        caps = ""
        while time.time() < deadline:
            caps = self.default_net_caps()
            if "VALIDATED" in caps.split("&") and ("NOT_METERED" not in caps.split("&")) == on:
                if not on and flag.exists():
                    flag.unlink()
                return "default network %s (%s)" % ("metered" if on else "not metered", caps)
            time.sleep(1)
        raise Blocked("the default network did not become %s within %ds: %s" % (
            "metered" if on else "unmetered", timeout, short(caps, 160)))

    def foreground(self):
        """Package that owns the focused window (a system dialog over the app shows here), None when it cannot be read.
        The owner comes from the window list's `package=`, not from the focus line's title: the app's own Compose
        Popup (its toast) is titled "Pop-Up Window" ("Finestra popup" on the Italian Pixel), which the title regex
        read as "Pop", turning a FAIL of the app into BLOCKED."""
        out = self.sh("dumpsys window windows")
        m = re.search(r"mCurrentFocus=Window\{(\w+) \S+ ([^}\n]*)\}", out)
        if not m:
            # Xiaomi Redmi Note 12S, Android 15: `windows` carries no focus line.
            out = self.sh("dumpsys window") + out
            m = re.search(r"mCurrentFocus=Window\{(\w+) \S+ ([^}\n]*)\}", out)
        if not m:
            return None
        owner = re.search(r"Window #\d+ Window\{%s [^\n]*\n\s*mOwnerUid=\d+[^\n]*?\bpackage=([\w.]+)" % m.group(1), out)
        if owner:
            return owner.group(1)
        title = re.match(r"([\w.]+)/", m.group(2))
        return title.group(1) if title else m.group(2)

    def isolate(self, on: bool) -> str:
        """Cuts the app alone off the network with Android's per-app firewall (chain OEM_DENY_3, Android 13+). The
        phone keeps Wi-Fi, so adb over Wi-Fi survives; the app still sees a network, so its calls fail rather than
        it going offline (for the app's offline state use wifi(False)). `lease release` lifts a leftover isolation."""
        flag = self.dir / "isolated.json"
        if on:
            out = self.sh("cmd connectivity set-chain3-enabled true") + " " + \
                self.sh("cmd connectivity set-package-networking-enabled false %s" % self.app)
            state = self.sh("cmd connectivity get-package-networking-enabled %s" % self.app)
            if not state.strip().endswith("deny"):
                raise Blocked("the per-app firewall is not available on %s: %s" % (self.alias, short(out, 160)))
            save_json(flag, {"package": self.app})
            return "app cut off the network (per-app firewall)"
        self.sh("cmd connectivity set-package-networking-enabled true %s" % self.app)
        self.sh("cmd connectivity set-chain3-enabled false")
        if flag.exists():
            flag.unlink()
        return "app network back"

    # ------------------------------------------------------------------------------------ baa

    def baa_port(self) -> int:
        from devtest_common import mark_offset
        line = wait_log(self.log, mark_offset(self.serial, self.log, "launch"), "baa", 10)
        m = re.search(r"ws://127\.0\.0\.1:(\d+)/baa", line)
        if not m:
            raise Blocked("no 'Baa started' line since the last launch: this build has no baa driver")
        cache = load_json(self.dir / "baa.json", {})
        if cache.get("device_port") == m.group(1) and cache.get("pid") == self.pid():
            return int(cache["host_port"])
        host = self.adb("forward", "tcp:0", "tcp:%s" % m.group(1)).stdout.strip()
        save_json(self.dir / "baa.json", {"device_port": m.group(1), "host_port": host, "pid": self.pid()})
        return int(host)

    # ------------------------------------------------------------------------------------ build

    @staticmethod
    def wait_other_gradle(top: Path, limit: float = 1200) -> None:
        """Two Gradle runs in one worktree race on build/ (\"Could not get file mode …\"): wait for the other one.
        Gradle 8.14's gradlew starts the wrapper with `-jar …/gradle/wrapper/gradle-wrapper.jar`, without
        GradleWrapperMain on the command line: matching only that class missed a running build, and two builds of
        one worktree both failed on build/ ("Failed to create MD5 hash")."""
        deadline, said = time.time() + limit, False
        while time.time() < deadline:
            ps = run(["ps", "-axo", "pid,command"], text=True).stdout.splitlines()
            others = [l for l in ps if ("GradleWrapperMain" in l or "gradle-wrapper.jar" in l) and str(top) in l]
            if not others:
                return
            if not said:
                sys.stderr.write("(another Gradle run is building %s: waiting for it)\n" % top.name)
                said = True
            time.sleep(5)
        raise Blocked("another Gradle run has been building %s for %d min" % (top.name, limit // 60))

    def build(self, top: Path) -> str:
        """Build with the SDK also visible to included builds: an included build misses the sdk.dir of
        the root's local.properties when ANDROID_HOME is unset.
        Respect an explicit SDK; otherwise use the installed platform default, without writing local.properties.
        """
        archive = source_archive(top)
        env = os.environ.copy()
        if not env.get("ANDROID_HOME"):
            sdk = Path(env.get("ANDROID_SDK_ROOT") or
                       (Path.home() / "Library/Android/sdk" if sys.platform == "darwin" else Path.home() / "Android/Sdk"))
            if sdk.is_dir():
                env["ANDROID_HOME"] = str(sdk)
        self.wait_other_gradle(top)
        log = self.dir / ("build-%s.log" % top.name)
        started = time.time()
        command, apk = need("android.build"), top / need("android.apk")
        with open(log, "w") as f:
            code = subprocess.call(command, shell=isinstance(command, str), cwd=str(top / (cfg("android.build_dir") or ".")),
                                   stdout=f, stderr=subprocess.STDOUT, env=env)
        body = log.read_text(errors="replace")
        if code != 0 or not apk.exists():
            lines = body.splitlines()
            errors = [l for l in lines if l.startswith("e: ") or "error:" in l]
            for i, l in enumerate(lines):
                if "What went wrong" in l:
                    errors += [x for x in lines[i + 1:i + 4] if x.strip()]
            raise Blocked("BUILD FAILED after %.1f min: %s (log %s)" % (
                (time.time() - started) / 60, short(" / ".join(dict.fromkeys(errors[:6])), 600), log))
        # An up-to-date Gradle build keeps the APK's old mtime,
        # even after checkout-only/doc changes. Record when Gradle checked its inputs; changes
        # during/after that build still make build_info stale. Keep the receipt outside the worktree.
        save_json(self.dir / ("build-%s.json" % top.name), {
            "worktree": str(top), "build_started_at": started, "apk_mtime": apk.stat().st_mtime,
            "commit": archive["commit"][:10] if archive else run(["git", "-C", str(top), "rev-parse", "--short", "HEAD"], text=True).stdout.strip()})
        return "BUILD SUCCESSFUL in %.1f min: %s" % ((time.time() - started) / 60, apk)

    def install(self, top: Path) -> str:
        apk = top / need("android.apk")
        if not apk.exists():
            raise Blocked("%s does not exist: run `build` first" % apk)
        # A 581 MB APK over Wi-Fi exceeded 300s.
        # Keep the USB budget; wireless streaming needs time without uninstalling the app.
        install_timeout = 1200 if ":" in self.transport else 300
        r = self.adb("install", "-r", "-t", str(apk), timeout=install_timeout)
        if "Success" not in r.stdout:
            raise Blocked("adb install failed: %s" % short(text(r), 300))
        for p in cfg("android.permissions") or []:
            self.sh("pm grant %s android.permission.%s 2>/dev/null" % (self.app, p))
        self.stop_capture()
        archive = source_archive(top)
        commit = archive["commit"][:10] if archive else run(["git", "-C", str(top), "rev-parse", "--short", "HEAD"], text=True).stdout.strip()
        receipt = load_json(self.dir / ("build-%s.json" % top.name), {})
        checked_at = (receipt.get("build_started_at", 0) if receipt.get("worktree") == str(top)
                      and receipt.get("commit") == commit and receipt.get("apk_mtime") == apk.stat().st_mtime else 0)
        save_json(self.dir / "installed.json", {"worktree": str(top), "commit": commit, "apk_mtime": apk.stat().st_mtime,
                                                "build_started_at": checked_at,
                                                "installed_at": time.time(), "update_time": self.update_time()})
        return "installed %s (%s, apk %s) on %s" % (top.name, commit, hhmm(apk.stat().st_mtime), self.alias)

    def update_time(self) -> str:
        m = re.search(r"lastUpdateTime=([^\n]+)", self.sh("dumpsys package %s | grep -m1 lastUpdateTime" % self.app))
        return m.group(1).strip() if m else ""

    def build_info(self, top: Path) -> str:
        rec = load_json(self.dir / "installed.json", None)
        actual = self.update_time()
        if not actual:
            raise Blocked("%s is not installed on %s" % (self.app, self.alias))
        if not rec or rec.get("update_time") != actual:
            raise Blocked("the build on %s (installed %s) was not installed by devtest: run `devtest install` "
                          "from the worktree under test" % (self.alias, actual))
        why = stale_reason(rec, top, worktree_changed_at(top))
        if why:
            raise Blocked("%s: %s" % (self.alias, why))
        return "build ok: %s @ %s, apk %s, installed %s" % (top.name, rec["commit"], hhmm(rec["apk_mtime"]), hhmm(rec["installed_at"]))
