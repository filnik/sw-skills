"""Android half of devtest: adb only, no Appium (a running Appium UiAutomator2 session breaks `uiautomator dump`).

Selectors match the node's resource-id (a Compose testTag needs `testTagsAsResourceId = true`; for a View id,
`name` matches `<package>:id/name`), `text:Exact`, `text~part`, `desc:Exact`, `desc~part` (content description).
"""

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

from devtest_common import (Blocked, Fail, build, crash_verdict, device_dir, fresh_db_dir, grep_lines, install_receipt,
                            load_json, need, read_since, run, save_json, set_mark, short, stale_reason, text,
                            worktree_changed_at)

ALLOW_BUTTONS = ("com.android.permissioncontroller:id/permission_allow_foreground_only_button",
                 "com.android.permissioncontroller:id/permission_allow_button")


class Node:
    def __init__(self, el, order: int = 0):
        self.id = el.get("resource-id", "")
        self.text = el.get("text", "")
        self.desc = el.get("content-desc", "")
        self.cls = el.get("class", "").rsplit(".", 1)[-1]
        self.checked = el.get("checked") == "true"
        if el.get("checkable") != "true":
            # A tagged wrapper holds its untagged checkable child (a switch row): read the state there.
            inner = next((e for e in el.iter() if e.get("checkable") == "true"), None)
            if inner is not None:
                self.checked = inner.get("checked") == "true"
        self.selected = el.get("selected") == "true"
        self.enabled = el.get("enabled") == "true"
        x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", el.get("bounds", "[0,0][0,0]")))
        self.bounds = (x1, y1, x2, y2)
        self.order = order
        self.el = el

    def label(self) -> str:
        return " | ".join(dict.fromkeys(v for v in (self.id, self.text, self.desc) if v))

    def subtree_text(self) -> str:
        return " ".join(v for e in self.el.iter() for v in (e.get("text", ""),) if v)


def parse_dump(xml: str) -> list:
    start, end = xml.find("<?xml"), xml.rfind("</hierarchy>")
    if start < 0 or end < start:
        return []
    root = ET.fromstring(xml[start:end + len("</hierarchy>")])
    return [Node(el, i) for i, el in enumerate(root.iter("node"))]


def matches(n: Node, sel: str) -> bool:
    if sel.startswith("text:"):
        return n.text == sel[5:]
    if sel.startswith("text~"):
        return sel[5:].lower() in n.text.lower()
    if sel.startswith("desc:"):
        return n.desc == sel[5:]
    if sel.startswith("desc~"):
        return sel[5:].lower() in n.desc.lower()
    return n.id == sel or n.id.endswith(":id/" + sel)


def src_lines(nodes: list, flt=None) -> list:
    """`SCREENS: …` then one line per node with an id, text or description: class, centre, label, state."""
    rows = []
    for n in nodes:
        if n.id.startswith("screen.") or not (n.id or n.text or n.desc):
            continue
        x1, y1, x2, y2 = n.bounds
        state = "".join(s for s, on in ((" [on]", n.checked), (" [sel]", n.selected), (" [off]", not n.enabled)) if on)
        line = "%-10.10s (%d,%d) %s%s" % (n.cls, (x1 + x2) // 2, (y1 + y2) // 2, short(n.label(), 150), state)
        if not flt or flt.lower() in line.lower():
            rows.append(line)
    screens = [n.id for n in nodes if n.id.startswith("screen.")]
    return ["SCREENS: " + (", ".join(dict.fromkeys(screens)) or "-")] + list(dict.fromkeys(rows))


def adb_transport(serial: str) -> str:
    """How adb reaches the phone with this serial right now. Its ip:port after `adb tcpip` + `adb connect` wins over
    USB when both are up (told apart by ro.serialno); then the last verified endpoint (an adb server restart forgets
    tcpip connections); then a phone on wireless debugging found through mDNS, asked a few times since mDNS can skip
    it or list an old port. Otherwise the serial itself, so an absent phone gives the usual BLOCKED."""
    remembered = device_dir(serial) / "wifi-transport.json"
    lines = run(["adb", "devices"], text=True).stdout.splitlines()[1:]
    online = [l.split()[0] for l in lines if l.endswith("device")]

    def is_it(t):
        return run(["adb", "-s", t, "shell", "getprop", "ro.serialno"], timeout=10, text=True).stdout.strip() == serial

    for t in online:
        if ":" in t and is_it(t):
            save_json(remembered, {"endpoint": t})
            return t
    if serial in online:
        return serial
    endpoint = load_json(remembered, {}).get("endpoint")
    try:
        if endpoint and re.match(r"(already )?connected to", run(["adb", "connect", endpoint], timeout=8,
                                                                 text=True).stdout) and is_it(endpoint):
            return endpoint
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
    platform = "android"

    def __init__(self, serial: str, alias: str, cfg: dict):
        # The serial names the phone (leases, state dir, marks); the transport is how adb reaches it now.
        self.serial, self.alias, self.cfg = serial, alias, cfg
        self.transport = adb_transport(serial)
        self.dir = device_dir(serial)
        self.log = self.dir / "app.log"
        self._geometry = None
        self.ui_dumps = 0

    @property
    def app_id(self) -> str:
        return need(self.cfg, "android", "package")

    # ------------------------------------------------------------------------------------ adb

    def adb(self, *args, timeout: float = 60, binary: bool = False):
        """Read-only exec-out is retried once after an adb transport reset; input and writes are never replayed."""
        command = ["adb", "-s", self.transport] + list(args)
        r = run(command, timeout=timeout, text=not binary)
        read_only = args[:1] == ("exec-out",) and (args[1:3] in (("uiautomator", "dump"),) or args[1:2] == ("screencap",)
                                                   or (args[1:2] == ("run-as",) and args[3:4] in (("cat",), ("gzip",))))
        err = text(r) if r.returncode != 0 else ""
        if read_only and r.returncode != 0 and re.search(
                r"protocol fault|connection reset|daemon not running|connection closed|device .* not found", err, re.I):
            print("(adb transport reset on %s: retrying a read once)" % self.alias, file=sys.stderr, flush=True)
            self.reconnect()
            r = run(["adb", "-s", self.transport] + list(args), timeout=timeout, text=not binary)
            err = text(r) if r.returncode != 0 else ""
        if r.returncode != 0 and re.search(r"device .* not found|unauthorized|offline|no devices", err):
            raise Blocked("%s (%s) is not reachable over adb: %s" % (self.alias, self.transport, short(err)))
        return r

    def reconnect(self) -> str:
        self.transport = adb_transport(self.serial)
        return self.transport

    def reachable(self) -> bool:
        """On adb and still captured: a phone that dropped off and came back has lost its logcat capture."""
        try:
            on_adb = run(["adb", "-s", self.transport, "get-state"], timeout=15, text=True).stdout.strip() == "device"
        except Blocked:
            return False
        return on_adb and self._capture_alive()

    def sh(self, cmd: str, timeout: float = 60) -> str:
        return self.adb("shell", cmd, timeout=timeout).stdout.strip()

    def pid(self) -> str:
        return self.sh("pidof %s" % self.app_id)

    def uid(self) -> str:
        m = re.search(r"uid:(\d+)", self.sh("pm list packages -U %s" % self.app_id))
        if not m:
            raise Blocked("%s is not installed on %s" % (self.app_id, self.alias))
        return m.group(1)

    # ------------------------------------------------------------------------------------ log capture

    def _capture_alive(self) -> bool:
        pid = load_json(self.dir / "procs.json", {}).get("logcat")
        if not pid:
            return False
        r = run(["ps", "-p", str(pid), "-o", "command="], text=True)
        return "logcat" in r.stdout and self.transport in r.stdout

    def ensure_capture(self) -> None:
        """logcat of the app's uid streamed into app.log across app restarts. A capture that died resumes from the
        last timestamp it wrote, so the device's ring buffer fills the gap."""
        if self._capture_alive():
            return
        procs = load_json(self.dir / "procs.json", {})
        uid = self.uid()
        since = "1"
        if procs.get("logcat") and self.log.exists():
            with self.log.open("rb") as f:
                f.seek(max(0, self.log.stat().st_size - 65536))
                tail = f.read().decode(errors="replace").splitlines()
            stamp = next((m.group() for m in (re.match(r"\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}", l) for l in reversed(tail))
                          if m), None)
            since = stamp or since
        with open(self.log, "a") as f:
            f.write("==== capture %s uid %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), uid))
            f.flush()
            p = subprocess.Popen(["adb", "-s", self.transport, "logcat", "-v", "threadtime", "-T", since,
                                  "--uid=%s" % uid], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
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
        last, woken, covered = "", False, 0
        for _ in range(4):
            self.ui_dumps += 1
            r = self.adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=30)
            nodes = parse_dump(r.stdout)
            if nodes:
                packages = {n.el.get("package") for n in nodes}
                if not woken and self.app_id not in packages and "com.android.systemui" in packages:
                    self.wake()  # the screen went off or locked under us
                    woken = True
                    continue
                if any("unintentional_" in n.id for n in nodes):
                    # Samsung's accidental touch protection: the proximity sensor is covered. One swipe dismisses it.
                    covered += 1
                    if covered > 1:
                        raise Blocked("'Accidental touch protection' covers the screen of %s: uncover the top of the "
                                      "phone, or turn the feature off in Settings > Display" % self.alias)
                    w, h = self.geometry()[:2]
                    self.sh("input swipe %d %d %d %d 300" % (w // 2, int(h * 0.75), w // 2, int(h * 0.25)))
                    continue
                return nodes
            last = short(text(r))
            time.sleep(0.7)
        raise Blocked("uiautomator dump failed on %s (%s): an Appium session or a never-idle animation blocks it"
                      % (self.alias, last))

    def geometry(self) -> tuple:
        """(width, height, safe_top, safe_bottom): taps land outside the status and navigation bars."""
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

    def find(self, sel: str, nodes=None):
        nodes = nodes if nodes is not None else self.dump()
        return next((n for n in nodes if matches(n, sel)), None)

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
        vy1, vy2, vx1, vx2 = max(y1, top), min(y2, bottom), max(x1, 0), min(x2, w)
        if vy2 - vy1 < 12 or vx2 - vx1 < 12:
            return None
        return (vx1 + vx2) // 2, (vy1 + vy2) // 2

    def tap(self, sel: str, timeout: float = 10) -> str:
        for attempt in range(4):
            n, nodes = self.wait_for(sel, timeout if attempt == 0 else 3)
            if n is None:
                raise Fail("%s not on screen within %ds (screens: %s)" % (sel, timeout,
                                                                          ", ".join(self.screens(nodes)) or "-"))
            point = self._tap_point(n)
            if point:
                self.sh("input tap %d %d" % point)
                return "tapped %s at %d,%d" % (sel, point[0], point[1])
            # Under a system bar or off screen: scroll it towards the middle.
            w, h, top, bottom = self.geometry()
            mid = (top + bottom) // 2
            dy = max(min((n.bounds[1] + n.bounds[3]) // 2 - mid, int((bottom - top) * 0.6)), -int((bottom - top) * 0.6))
            self.sh("input swipe %d %d %d %d 400" % (w // 2, mid, w // 2, mid - dy))
            time.sleep(0.6)
        raise Fail("%s stays under a system bar or off screen after 3 scrolls" % sel)

    def node(self, sel: str, timeout: float = 0):
        return self.wait_for(sel, timeout)[0]

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
        """What a text field holds: its EditText (a tagged wrapper also carries the label and a clear button)."""
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
        """A burst first; one key at a time on the retries (some keyboards drop the key before a space in a burst)."""
        if not paced:
            self.sh("input text %s" % shlex.quote(value.replace(" ", "%s")))
            return
        keys = ["input keyevent KEYCODE_SPACE" if c == " " else "input text %s" % shlex.quote(c) for c in value]
        self.sh("; sleep 0.05; ".join(keys), timeout=30 + 2 * len(value))

    def set_text(self, sel: str, value: str, timeout: float = 10) -> str:
        """Real keyboard input, three tries: the field must be focused with the keyboard up before Ctrl+A, DEL and
        the keys (typed into a field focused before the tap, keys can go to a stale input connection)."""
        if not value.isascii():
            raise Blocked("adb input text types ASCII only: %r" % value)
        seen = ""
        for attempt in (1, 2, 3):
            if attempt > 1 or self.keyboard_shown():
                self.hide_keyboard()
            self.tap(sel, timeout if attempt == 1 else 5)
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
                self.type_keys(value, paced=attempt > 1)
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
        return src_lines(self.dump(), flt)

    def shot(self, path: Path) -> Path:
        raw = path.with_suffix(".raw.png")
        raw.write_bytes(self.adb("exec-out", "screencap", "-p", binary=True).stdout)
        try:
            run(["sips", "-Z", "900", str(raw), "--out", str(path)])
        except Blocked:
            pass  # no sips (not a Mac): keep the full-size screenshot
        if path.exists():
            raw.unlink()
        else:
            raw.replace(path)
        return path

    # ------------------------------------------------------------------------------------ app

    def running(self) -> bool:
        return bool(self.pid())

    def stop(self) -> None:
        self.sh("am force-stop %s" % self.app_id)

    def foreground(self):
        """Package of the focused window (a system dialog over the app shows here), None when it cannot be read."""
        out = self.sh("dumpsys window windows")
        m = re.search(r"mCurrentFocus=Window\{(\w+) \S+ ([^}\n]*)\}", out)
        if not m:
            out = self.sh("dumpsys window") + out
            m = re.search(r"mCurrentFocus=Window\{(\w+) \S+ ([^}\n]*)\}", out)
        if not m:
            return None
        # The owner from the window list, not the title: an app's own popup is titled "Pop-Up Window".
        owner = re.search(r"Window #\d+ Window\{%s [^\n]*\n\s*mOwnerUid=\d+[^\n]*?\bpackage=([\w.]+)" % m.group(1), out)
        if owner:
            return owner.group(1)
        title = re.match(r"([\w.]+)/", m.group(2))
        return title.group(1) if title else m.group(2)

    def keyguard_showing(self) -> bool:
        win = self.sh("dumpsys window policy | grep -iE 'showing=|isKeyguardShowing|mShowingLockscreen'")
        return bool(re.search(r"(?i)(isKeyguardShowing|mShowingLockscreen|showing)=true", win))

    def wake(self) -> None:
        """Screen on and kept on for long waits, lock screen dismissed; `lease release` restores the timeout."""
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
        if (self.dir / "isolated.json").exists():  # a run that died with the app cut off the network
            notes.append(self.isolate(False))
        saved = self.dir / "screen_timeout.json"
        original = load_json(saved, {}).get("screen_off_timeout")
        if original:
            self.sh("settings put system screen_off_timeout %s" % original)
            self.sh("svc power stayon false")
            saved.unlink()
            notes.append("screen timeout back to %ss" % (int(original) // 1000))
        return "; ".join(notes)

    def launch(self, wait_screen=None, restart: bool = False, timeout: float = 60) -> str:
        """Starts the app (device_test.android.activity, else its launcher entry), accepts runtime permission prompts
        and waits for `wait_screen` (any selector), or for the app to be in front."""
        self.wake()
        self.sh("cmd statusbar collapse")
        if restart:
            self.stop()
        self.ensure_capture()
        set_mark(self.serial, self.log, "launch")
        activity = (self.cfg.get("android") or {}).get("activity")
        if activity:
            self.sh("am start -n %s/%s" % (self.app_id, activity))
        else:
            self.sh("monkey -p %s -c android.intent.category.LAUNCHER 1" % self.app_id)
        deadline, handled, screens = time.time() + timeout, [], []
        while time.time() < deadline:
            nodes = self.dump()
            screens = self.screens(nodes)
            allow = next((b for b in ALLOW_BUTTONS if self.find(b, nodes)), None)
            if allow:
                self.tap(allow, 3)
                handled.append("permission")
                continue
            ours = any(n.el.get("package") == self.app_id for n in nodes)
            if (wait_screen and self.find(wait_screen, nodes)) or (not wait_screen and ours):
                return "launched pid %s on %s%s" % (self.pid(), screens[-1] if screens else "the app",
                                                   " (%s)" % ", ".join(handled) if handled else "")
            if not self.running():
                self.crash_check(0)
            time.sleep(0.8)
        raise Fail("app did not reach %s within %ds (screens: %s)" % (wait_screen or "the front", timeout,
                                                                      ", ".join(screens) or "-"))

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
        path = need(self.cfg, "android", "prefs")
        r = self.adb("exec-out", "run-as", self.app_id, "cat", path, binary=True)
        if r.returncode != 0 or r.stdout.startswith(b"run-as:") or r.stdout.startswith(b"cat:"):
            raise Blocked("cannot read %s (a debuggable build is needed for run-as): %s" % (path, short(text(r))))
        return r.stdout

    def write_prefs(self, data: bytes) -> None:
        path = need(self.cfg, "android", "prefs")
        tmp = self.dir / "prefs-write.bin"
        tmp.write_bytes(data)
        self.adb("push", str(tmp), "/data/local/tmp/devtest-prefs")
        self.sh("chmod 644 /data/local/tmp/devtest-prefs")
        out = self.sh("run-as %s cp /data/local/tmp/devtest-prefs %s" % (self.app_id, shlex.quote(path)))
        self.sh("rm -f /data/local/tmp/devtest-prefs")
        if out:
            raise Blocked("could not write %s: %s" % (path, short(out)))

    def pull_db(self) -> Path:
        """A fresh copy of databases/<device_test.android.db> with its -wal and -shm, gzip-streamed when the phone
        has gzip (a large DB over adb on Wi-Fi stalls a raw cat)."""
        name = need(self.cfg, "android", "db")
        dest = fresh_db_dir(self.dir)
        for f in (name, name + "-wal", name + "-shm"):
            r = self.adb("exec-out", "run-as", self.app_id, "gzip", "-c", "databases/" + f, binary=True)
            if r.returncode == 0 and r.stdout.startswith(b"\x1f\x8b"):
                try:
                    (dest / f).write_bytes(gzip.decompress(r.stdout))
                except (OSError, EOFError):
                    raise Blocked("truncated compressed DB transfer on %s: %s" % (self.alias, f))
            elif r.returncode != 0 and b"gzip" in r.stderr:
                r = self.adb("exec-out", "run-as", self.app_id, "cat", "databases/" + f, binary=True)
                if r.returncode == 0 and not r.stdout.startswith(b"cat:"):
                    (dest / f).write_bytes(r.stdout)
        if not (dest / name).exists():
            raise Blocked("databases/%s not readable on %s" % (name, self.alias))
        return dest / name

    def media_path(self, relative: str) -> str:
        """The app's external files folder, where it can read files the test puts there."""
        return "/sdcard/Android/data/%s/files/%s" % (self.app_id, relative)

    def put_file(self, relative: str, local: Path) -> str:
        target = self.media_path(relative)
        self.sh("mkdir -p %s" % shlex.quote(target.rsplit("/", 1)[0]))
        r = self.adb("push", str(local), target)
        if r.returncode != 0:
            raise Blocked("adb push to %s failed: %s" % (target, short(text(r))))
        return target

    def file_exists(self, path: str) -> bool:
        """/sdcard/… and other absolute paths are read directly; relative ones inside the app's private folder."""
        if path.startswith("/"):
            return self.sh("ls %s >/dev/null 2>&1 && echo yes" % shlex.quote(path)) == "yes"
        return self.sh("run-as %s ls %s >/dev/null 2>&1 && echo yes" % (self.app_id, shlex.quote(path))) == "yes"

    # ------------------------------------------------------------------------------------ network

    def wifi_enabled(self) -> bool:
        status = self.sh("dumpsys wifi | grep -m1 '^Wi-Fi is '").strip()
        if status in ("Wi-Fi is enabled", "Wi-Fi is disabled"):
            return status.endswith("enabled")
        raise Blocked("cannot read the Wi-Fi state: %s" % status)

    def wifi(self, on: bool, timeout: float = 40) -> str:
        if not on and ":" in self.transport:
            raise Blocked("adb reaches %s over Wi-Fi (%s): turning Wi-Fi off would cut it; connect it over USB "
                          "(`adb -s %s usb`) for offline steps, or use isolate()" % (self.alias, self.transport,
                                                                                    self.transport))
        self.sh("svc wifi %s" % ("enable" if on else "disable"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.sh("dumpsys wifi | grep -m1 'mWifiInfo SSID'")
            if on and "COMPLETED" in info and "<unknown ssid>" not in info:
                return "wifi on"
            if not on and not self.wifi_enabled():
                return "wifi off"
            time.sleep(1)
        raise Blocked("wifi did not turn %s within %ds" % ("on" if on else "off", timeout))

    def airplane(self, on: bool, confirm=None) -> str:
        raise Blocked("airplane mode is not scriptable on recent Android without root: use wifi() or isolate()")

    def isolate(self, on: bool) -> str:
        """The app alone cut off the network with Android's per-app firewall (Android 13+): the phone keeps Wi-Fi
        and adb, the app sees a network whose calls fail. `lease release` lifts a leftover isolation."""
        flag = self.dir / "isolated.json"
        if on:
            out = self.sh("cmd connectivity set-chain3-enabled true") + " " + \
                self.sh("cmd connectivity set-package-networking-enabled false %s" % self.app_id)
            if not self.sh("cmd connectivity get-package-networking-enabled %s" % self.app_id).strip().endswith("deny"):
                raise Blocked("the per-app firewall is not available on %s: %s" % (self.alias, short(out, 160)))
            save_json(flag, {"package": self.app_id})
            return "app cut off the network"
        self.sh("cmd connectivity set-package-networking-enabled true %s" % self.app_id)
        self.sh("cmd connectivity set-chain3-enabled false")
        flag.unlink(missing_ok=True)
        return "app network back"

    # ------------------------------------------------------------------------------------ build

    def build(self, top: Path) -> str:
        return build(top, "android", self.cfg)

    def install(self, top: Path) -> str:
        apk = top / need(self.cfg, "android", "apk")
        if not apk.exists():
            raise Blocked("%s does not exist: run `build` first" % apk)
        r = self.adb("install", "-r", "-t", "-g", str(apk), timeout=300)
        if "Success" not in r.stdout:
            raise Blocked("adb install failed: %s" % short(text(r), 300))
        self.stop_capture()  # a reinstall can change the uid
        rec = install_receipt(top, "android", apk, update_time=self.update_time())
        save_json(self.dir / "installed.json", rec)
        return "installed %s @ %s on %s" % (top.name, rec["commit"], self.alias)

    def update_time(self) -> str:
        m = re.search(r"lastUpdateTime=([^\n]+)", self.sh("dumpsys package %s | grep -m1 lastUpdateTime" % self.app_id))
        return m.group(1).strip() if m else ""

    def build_info(self, top: Path) -> str:
        rec = load_json(self.dir / "installed.json", None)
        actual = self.update_time()
        if not actual:
            raise Blocked("%s is not installed on %s" % (self.app_id, self.alias))
        if rec and rec.get("update_time") != actual:
            raise Blocked("the build on %s (installed %s) was not installed by devtest: run `deploy`" % (self.alias,
                                                                                                         actual))
        why = stale_reason(rec, top, worktree_changed_at(top))
        if why:
            raise Blocked(why)
        return "build ok: %s @ %s" % (top.name, rec["commit"])
