"""Phone status for the person at the Mac: who holds each phone, whether that session is still working, and the
"plug it in" request.

- holder() goes into the lease, so whatever reads `devices --json` (a status panel, say) can tell a working session
  from a stopped one and focus its terminal tab.
- wait_connected(): phones are often left unplugged. A command that needs a phone that is not connected posts a
  notification (the pack's `notifier`) asking to plug it in, and goes on as soon as it is connected, instead of
  failing at once. DEVTEST_PLUG_WAIT (default 5m, 0 = fail at once) bounds the wait.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from devtest_common import (DEVICES, NAMES, STATE, Blocked, cfg, current_lease, load_json, notify, owner, parse_ttl,
                            run, save_json)

WANTED = STATE / "wanted"
# where Claude Code keeps one <pid>.json per live session: the pack's session_dirs, then the default config folder
SESSION_DIRS = [Path(d).expanduser() for d in cfg("session_dirs") or []] + [Path.home() / ".claude" / "sessions"]


def _parent(pid: int) -> tuple:
    out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.split(None, 1)
    return (int(out[0]), out[1].strip()) if len(out) == 2 else (0, "")


def _session_file(pid: int, dirs=None):
    return next((p for d in (dirs or SESSION_DIRS) for p in [d / ("%d.json" % pid)] if p.exists()), None)


def holder(parent=_parent, dirs=None) -> dict:
    """The nearest Claude Code session (it has a sessions/<pid>.json) or Codex process above this command."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    info = {"iterm": os.environ.get("ITERM_SESSION_ID", "").partition(":")[2], "cwd": top or os.getcwd()}
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        ppid, comm = parent(pid)
        if _session_file(pid, dirs):
            return {**info, "pid": pid, "agent": "claude"}
        if os.path.basename(comm).startswith("codex"):
            return {**info, "pid": pid, "agent": "codex"}
        pid = ppid
    return info


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def session_state(lease: dict, alive=_alive, dirs=None) -> tuple:
    """(state, since): running · idle (a Claude session waiting at its prompt, since when) · gone (the process that
    took the lease has ended) · unknown (a lease without a holder)."""
    pid = lease.get("pid")
    if not pid:
        return "unknown", None
    if not alive(pid):
        return "gone", None
    if lease.get("agent") == "claude":
        s = load_json(_session_file(pid, dirs) or Path("/nonexistent"), {})
        if s.get("status") == "idle":
            return "idle", (s.get("statusUpdatedAt") or 0) / 1000 or None
    return "running", None


def wanted(ident: str, alive=_alive):
    w = load_json(WANTED / ("%s.json" % ident), None)
    return w if w and alive(w.get("pid", 0)) else None


def connected_sets() -> tuple:
    lines = run(["adb", "devices"], text=True).stdout.splitlines()[1:]
    serials = {l.split()[0] for l in lines if l.endswith("device")}
    # a phone on adb over Wi-Fi shows as ip:port: count it under its serial too
    serials |= {run(["adb", "-s", s, "shell", "getprop", "ro.serialno"], timeout=10, text=True).stdout.strip()
                for s in list(serials) if ":" in s} - {""}
    try:
        from devtest_ios import idev
        udids = {d["udid"].replace("-", "") for d in idev.usb_devices() if d["conn"] == "USB"}
    except Exception:
        udids = set()
    return serials, udids


def is_connected(ident: str, sets=None) -> bool:
    serials, udids = sets or connected_sets()
    return ident in serials or ident.replace("-", "") in udids


def wait_connected(ident: str, alias: str, check=is_connected, notify=notify, sleep=time.sleep, clock=time.time,
                   every: float = 120) -> bool:
    """True once the phone is connected; False right away if it already was. Raises Blocked when nobody plugs it
    in within DEVTEST_PLUG_WAIT."""
    if check(ident):
        return False
    wait = parse_ttl(os.environ.get("DEVTEST_PLUG_WAIT", "5m"))
    if not wait:
        return False  # the command fails with its own BLOCKED
    lease = current_lease(ident) or {}
    who = lease.get("note") or owner() or "a session"
    h = holder()
    where = os.path.basename(h.get("cwd", ""))
    text = (cfg("plug_in_text") or "Plug in the %(phone)s: %(who)s is waiting for it%(where)s") % {
        "phone": NAMES.get(alias, alias), "who": who, "where": " · " + where if where else ""}
    WANTED.mkdir(parents=True, exist_ok=True)
    request = WANTED / ("%s.json" % ident)
    save_json(request, {"alias": alias, "who": who, "where": where, "since": clock(), "pid": os.getpid(),
                        "iterm": h.get("iterm", "")})
    print("(%s is not connected: asked to plug it in, waiting up to %ds)" % (alias, wait), file=sys.stderr, flush=True)
    try:
        deadline, last = clock() + wait, None
        while clock() < deadline:
            if last is None or clock() - last >= every:
                notify(text)
                last = clock()
            sleep(3)
            if check(ident):
                return True
    finally:
        request.unlink(missing_ok=True)
    raise Blocked("%s is not connected: a notification asked to plug it in, nobody did within %ds" % (alias, wait))


def phones() -> list:
    """One dict per known phone, plus any other connected or leased Android serial (for `devices --json`)."""
    serials, udids = connected_sets()
    rows = [(alias, platform, ident) for alias, (platform, ident) in DEVICES.items()]
    known = {ident for _, _, ident in rows}
    leased = {p.stem for p in (STATE / "leases").glob("*.json")}
    rows += [("-", "android", s) for s in sorted((serials | leased) - known) if ":" not in s]
    out = []
    for alias, platform, ident in rows:
        lease = current_lease(ident)
        state, since = session_state(lease) if lease else (None, None)
        if alias == "-" and not lease and ident not in serials:
            continue
        out.append({"alias": alias, "name": NAMES.get(alias, ident), "platform": platform, "ident": ident,
                    "connected": is_connected(ident, (serials, udids)), "lease": lease,
                    "session": state, "session_since": since, "wanted": wanted(ident)})
    return out
