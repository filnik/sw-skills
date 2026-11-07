"""Who holds each phone, whether that session still works, and the "plug it in" wait.

- holder() goes into the lease: `devices` then tells a working session from one that has ended, so a stale lease
  can be released with --force.
- wait_connected(): a command that needs a phone that is not connected posts a macOS notification asking to plug it
  in and goes on as soon as it is there, instead of failing at once. DEVTEST_PLUG_WAIT (default 5m, 0 = fail at
  once) bounds the wait.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from devtest_common import Blocked, current_lease, load_json, owner, parse_ttl, run

SESSION_DIRS = [Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"]


def _parent(pid: int) -> tuple:
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=10).stdout.split(None, 1)
    except (OSError, subprocess.SubprocessError):  # ps not allowed here: the lease just records no session
        return 0, ""
    return (int(out[0]), out[1].strip()) if len(out) == 2 else (0, "")


def _session_file(pid: int, dirs=None):
    return next((p for d in (dirs or SESSION_DIRS) for p in [d / ("%d.json" % pid)] if p.exists()), None)


def holder(parent=_parent, dirs=None) -> dict:
    """The nearest Claude Code session above this command (it has a sessions/<pid>.json), and the cwd."""
    info = {"cwd": os.getcwd()}
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        if _session_file(pid, dirs):
            return dict(info, pid=pid)
        pid = parent(pid)[0]
    return info


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def session_state(lease: dict, alive=_alive, dirs=None) -> str:
    """running · idle (the session waits at its prompt) · gone (the process that took the lease has ended) ·
    unknown (no session was found when the lease was taken)."""
    pid = lease.get("pid")
    if not pid:
        return "unknown"
    if not alive(pid):
        return "gone"
    if load_json(_session_file(pid, dirs) or Path("/nonexistent"), {}).get("status") == "idle":
        return "idle"
    return "running"


def connected_sets() -> tuple:
    """(adb serials, iPhone UDIDs without dashes) connected right now; a phone on adb over Wi-Fi counts by serial."""
    try:
        lines = run(["adb", "devices"], timeout=20, text=True).stdout.splitlines()[1:]
    except Blocked:
        lines = []
    serials = {l.split()[0] for l in lines if l.endswith("device")}
    serials |= {run(["adb", "-s", s, "shell", "getprop", "ro.serialno"], timeout=10, text=True).stdout.strip()
                for s in list(serials) if ":" in s} - {""}
    try:
        from devtest_ios import usb_devices
        udids = {d["udid"].replace("-", "").upper() for d in usb_devices() if d["conn"] == "USB"}
    except Exception:  # noqa: BLE001 - no usbmuxd, no iPhones
        udids = set()
    return serials, udids


def is_connected(ident: str, sets=None) -> bool:
    serials, udids = sets or connected_sets()
    return ident in serials or ident.replace("-", "").upper() in udids


def notify(message: str) -> None:
    message = message.replace('"', "'").replace("\\", "/")
    try:
        run(["osascript", "-e", 'display notification "%s" with title "devtest"' % message], timeout=10)
    except Blocked:
        pass  # not a Mac, or no notification permission: the wait goes on anyway


def wait_connected(ident: str, alias: str, check=is_connected, notify=notify, sleep=time.sleep, clock=time.time,
                   every: float = 120) -> bool:
    """True once the phone is connected; False right away if it already was (or the wait is off).
    BLOCKED when nobody plugs it in within DEVTEST_PLUG_WAIT."""
    if check(ident):
        return False
    wait = parse_ttl(os.environ.get("DEVTEST_PLUG_WAIT", "5m"))
    if not wait:
        return False
    who = (current_lease(ident) or {}).get("note") or owner() or "a test session"
    message = "Plug in %s: %s is waiting for it" % (alias, who)
    print("(%s is not connected: asked to plug it in, waiting up to %ds)" % (alias, wait), file=sys.stderr, flush=True)
    deadline, last = clock() + wait, None
    while clock() < deadline:
        if last is None or clock() - last >= every:
            notify(message)
            last = clock()
        sleep(3)
        if check(ident):
            return True
    raise Blocked("%s is not connected: a notification asked to plug it in, nobody did within %ds" % (alias, wait))
