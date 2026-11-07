"""Shared pieces of devtest: project config, output contract, leases, log marks, crash verdict, prefs codecs, builds.

Exit codes: 0 ok · 1 FAIL (the app did not do what was expected) · 2 BLOCKED (device, tooling or setup) ·
3 SCENARIO ERROR (the scenario itself is wrong).
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

STATE = Path(os.environ.get("DEVTEST_HOME") or Path.home() / ".cache" / "devtest")
LEASES = STATE / "leases"
RUNS = STATE / "runs"
BUILDS = STATE / "builds"
UDID_RE = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9a-f]{40}"
CRASH = r"FATAL EXCEPTION|AndroidRuntime: |SIGSEGV|SIGABRT|EXC_BAD_ACCESS|Terminating app due to|Fatal error: "


class Fail(Exception):
    """The app did not behave as expected."""


class Blocked(Exception):
    """The check could not run: device, tooling, lease or setup problem."""


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, timeout: float = 120, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        raise Blocked("timed out after %ds: %s" % (timeout, " ".join(map(str, cmd))[:200]))
    except FileNotFoundError:
        raise Blocked("%s is not installed or not on PATH" % cmd[0])


def text(r: subprocess.CompletedProcess) -> str:
    out = r.stdout if isinstance(r.stdout, str) else (r.stdout or b"").decode(errors="replace")
    err = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode(errors="replace")
    return (out + err).strip()


def short(s: str, n: int = 200) -> str:
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


# ---------------------------------------------------------------------------------------- config

def project_config(start=None) -> tuple:
    """(project root, device_test block) from the nearest .claude/config/project.json at or above `start`."""
    here = Path(start or Path.cwd()).expanduser().resolve()
    for d in [here] + list(here.parents):
        f = d / ".claude" / "config" / "project.json"
        if f.is_file():
            try:
                return d, json.loads(f.read_text()).get("device_test", {})
            except ValueError as e:
                raise Blocked("%s is not valid JSON: %s" % (f, e))
    return here, {}


def need(cfg: dict, platform: str, key: str):
    value = (cfg.get(platform) or {}).get(key)
    if not value:
        raise Blocked("set device_test.%s.%s in .claude/config/project.json" % (platform, key))
    return value


def platform_of(ident: str) -> str:
    return "ios" if re.fullmatch(UDID_RE, ident) else "android"


def resolve(name, phones: dict) -> tuple:
    """(platform, id, alias) for an alias of device_test.phones, an adb serial or an iPhone UDID."""
    name = name or os.environ.get("DEVTEST_DEVICE")
    if not name:
        raise Blocked("no device: pass --device ALIAS|SERIAL|UDID (configured: %s)" % (", ".join(phones) or "none"))
    ident = phones.get(name, name)
    alias = name if name in phones else next((a for a, i in phones.items() if i == name), name)
    return platform_of(ident), ident, alias


def default_phone(platform: str, phones: dict) -> str:
    for alias, ident in phones.items():
        if platform_of(ident) == platform:
            return alias
    raise Blocked("no %s phone in device_test.phones: pass --device" % platform)


def device_dir(ident: str) -> Path:
    d = STATE / "dev" / ident
    d.mkdir(parents=True, exist_ok=True)
    return d


def fresh_db_dir(device: Path) -> Path:
    """A new folder for a copy of the app's DB, the earlier copies deleted: each copy is only good until the next."""
    base = device / "db"
    if base.is_dir():
        shutil.rmtree(base, ignore_errors=True)
    dest = base / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    return dest


# ---------------------------------------------------------------------------------------- leases

def owner() -> str:
    return os.environ.get("DEVTEST_OWNER", "")


@contextmanager
def _lease_lock():
    LEASES.mkdir(parents=True, exist_ok=True)
    with open(LEASES / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _lease_file(ident: str) -> Path:
    return LEASES / ("%s.json" % ident)


def current_lease(ident: str):
    lease = load_json(_lease_file(ident), None)
    return lease if lease and lease.get("expires", 0) > time.time() else None


def parse_ttl(ttl: str) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", ttl)
    if not m:
        raise Blocked("bad duration %s: use 90m, 2h, 600s" % ttl)
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def until(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def lease_acquire(ident: str, alias: str, who: str, ttl: str, note: str) -> None:
    if not who:
        raise Blocked("pass --owner <session name> on every call")
    from devtest_phones import holder
    with _lease_lock():
        lease = current_lease(ident)
        if lease and lease["owner"] != who:
            raise Blocked("%s is leased by %s until %s (%s)" % (alias, lease["owner"], until(lease["expires"]),
                                                                lease.get("note", "")))
        expires = time.time() + parse_ttl(ttl)
        save_json(_lease_file(ident), dict(holder(), owner=who, alias=alias, expires=expires, note=note,
                                           since=(lease or {}).get("since", time.time())))
    say("lease %s -> %s until %s" % (alias, who, until(expires)))


def lease_release(ident: str, alias: str, who: str, force: bool) -> None:
    with _lease_lock():
        lease = current_lease(ident)
        if lease and lease["owner"] != who and not force:
            raise Blocked("%s is leased by %s, not by %s (--force only if that session is gone)" % (
                alias, lease["owner"], who or "you"))
        _lease_file(ident).unlink(missing_ok=True)
    say("lease %s released" % alias)


def check_lease(ident: str, alias: str) -> None:
    """Device-touching commands refuse a phone that another owner holds."""
    lease = current_lease(ident)
    if lease and lease["owner"] != owner():
        raise Blocked("%s is leased by %s until %s: wait, or ask that session to release it" % (
            alias, lease["owner"], until(lease["expires"])))


# ---------------------------------------------------------------------------------------- marks and logs

def marks_file(ident: str) -> Path:
    return device_dir(ident) / "marks.json"


def set_mark(ident: str, log: Path, name: str) -> int:
    offset = log.stat().st_size if log.exists() else 0
    marks = load_json(marks_file(ident), {})
    marks[name] = {"offset": offset, "log": str(log), "at": time.time()}
    save_json(marks_file(ident), marks)
    return offset


def mark_offset(ident: str, log: Path, name) -> int:
    if not name:
        return 0
    m = load_json(marks_file(ident), {}).get(name)
    if m is None:
        raise Blocked("unknown mark %s: set it with `mark %s` before the action" % (name, name))
    if m["log"] != str(log):
        raise Blocked("mark %s belongs to another log (%s)" % (name, m["log"]))
    return m["offset"]


def read_since(log: Path, offset: int) -> list:
    if not log.exists():
        return []
    with open(log, "rb") as f:
        f.seek(offset)
        return f.read().decode(errors="replace").splitlines()


def grep_lines(lines: list, pattern: str) -> list:
    rx = re.compile(CRASH if pattern == "crash" else pattern, re.I)
    return [l for l in lines if rx.search(l)]


def wait_log(log: Path, offset: int, pattern: str, timeout: float, poll: float = 1.0) -> str:
    deadline = time.time() + timeout
    while True:
        hits = grep_lines(read_since(log, offset), pattern)
        if hits:
            return hits[-1]
        if time.time() >= deadline:
            return ""
        time.sleep(poll)


def crash_verdict(lines: list, alive: bool) -> None:
    """Raises for a crash in these log lines; BLOCKED when the build is older than the data on the phone."""
    m = re.search(r"A migration from (\d+) to (\d+) was required", "\n".join(lines))
    if m and int(m.group(1)) > int(m.group(2)):
        raise Blocked("the app crashed on a database newer than this build (v%s -> v%s): install a build at least as "
                      "new as the data, or reinstall clean" % (m.group(1), m.group(2)))
    hits = grep_lines(lines, "crash")
    if hits:
        reason = next((l for l in hits if "reason:" in l or "Exception" in l), hits[0])
        raise Fail("app crashed: %s" % short(reason.strip(), 400))
    if not alive:
        raise Fail("app process is gone (no crash line in the log)")


# ---------------------------------------------------------------------------------------- prefs
# androidx DataStore Preferences (protobuf) or a property list (NSUserDefaults), told apart by their first bytes.

def _varint(buf: bytes, i: int) -> tuple:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _put_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _fields(buf: bytes):
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 2:
            n, i = _varint(buf, i)
            val, i = buf[i:i + n], i + n
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise Blocked("unsupported protobuf wire type %d in the prefs file" % wire)
        yield num, wire, val


def _is_plist(buf: bytes) -> bool:
    return buf.startswith(b"bplist") or buf.lstrip().startswith(b"<?xml")


def decode_prefs(buf: bytes) -> dict:
    if _is_plist(buf):
        return plistlib.loads(buf)
    out = {}  # PreferenceMap { map<string, Value> preferences = 1 }
    for num, _, entry in _fields(buf):
        if num != 1:
            continue
        key, value = None, None
        for n, _, v in _fields(entry):
            if n == 1:
                key = v.decode()
            elif n == 2:
                for vn, _, vv in _fields(v):
                    if vn == 1:
                        value = bool(vv)
                    elif vn == 2:
                        value = struct.unpack("<f", vv)[0]
                    elif vn in (3, 4):
                        value = vv
                    elif vn == 5:
                        value = vv.decode(errors="replace")
                    elif vn == 6:
                        value = sorted(s.decode() for sn, _, s in _fields(vv) if sn == 1)
                    elif vn == 7:
                        value = struct.unpack("<d", vv)[0]
                    elif vn == 8:
                        value = "<%d bytes>" % len(vv)
        if key is not None:
            out[key] = value
    return out


def _len_field(num: int, payload: bytes) -> bytes:
    return _put_varint((num << 3) | 2) + _put_varint(len(payload)) + payload


def encode_pref(buf: bytes, key: str, value) -> bytes:
    """The prefs file with `key` set to a string value, or removed when value is None."""
    if _is_plist(buf):
        data = plistlib.loads(buf)
        data.pop(key, None)
        if value is not None:
            data[key] = value
        return plistlib.dumps(data, fmt=plistlib.FMT_BINARY if buf.startswith(b"bplist") else plistlib.FMT_XML)
    kept = b""
    for num, wire, entry in _fields(buf):
        if num == 1 and any(n == 1 and v.decode() == key for n, _, v in _fields(entry)):
            continue
        if wire != 2:
            raise Blocked("unexpected top-level field %d in the DataStore file" % num)
        kept += _len_field(num, entry)
    if value is None:
        return kept
    entry = _len_field(1, key.encode()) + _len_field(2, _len_field(5, str(value).encode()))
    return kept + _len_field(1, entry)


def format_prefs(prefs: dict, flt) -> list:
    return ["%s = %s" % (k, short(v if isinstance(v, str) else json.dumps(v, default=str), 160))
            for k, v in sorted(prefs.items()) if not flt or flt.lower() in k.lower()]


def sqlite_query(db: Path, sql: str, limit: int = 40) -> list:
    r = run(["sqlite3", "-header", "-separator", " | ", str(db), sql], timeout=60, text=True)
    if r.returncode != 0:
        raise Blocked("sqlite3: %s" % short(text(r), 300))
    rows = r.stdout.splitlines()
    if len(rows) > limit + 1:
        rows = rows[:limit + 1] + ["… %d more rows" % (len(rows) - limit - 1)]
    return rows


# ---------------------------------------------------------------------------------------- worktree and builds

# Changes that do not reach the app build: scenarios, tests, docs, agent config.
NOT_IN_BUILD = re.compile(r"(^|/)(device-tests|\.claude|docs|tests?|[A-Za-z]*Tests?|\w+[_-]tests?)/|\.md$")


def worktree(arg=None) -> Path:
    cwd = Path(arg).expanduser().resolve() if arg else Path.cwd()
    r = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], text=True)
    if r.returncode != 0:
        raise Blocked("%s is not inside a git checkout: pass --worktree" % cwd)
    return Path(r.stdout.strip())


def head(top: Path) -> str:
    return run(["git", "-C", str(top), "rev-parse", "--short", "HEAD"], text=True).stdout.strip() or "unknown"


def worktree_changed_at(top: Path) -> float:
    """Newest of the HEAD commit time and the mtime of every modified or untracked file that goes into the build."""
    newest = float(run(["git", "-C", str(top), "log", "-1", "--format=%ct"], text=True).stdout.strip() or 0)
    status = run(["git", "-C", str(top), "status", "--porcelain", "--untracked-files=all"], text=True).stdout
    for line in status.splitlines():
        rel = line[3:].split(" -> ")[-1].strip('"')
        path = top / rel
        if not NOT_IN_BUILD.search(rel) and path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return newest


def newest_mtime(path: Path) -> float:
    """A file's mtime; for a bundle folder (.app), the newest of its top-level entries (executable, Info.plist)."""
    if path.is_dir():
        return max([path.stat().st_mtime] + [p.stat().st_mtime for p in path.iterdir()])
    return path.stat().st_mtime


def build(top: Path, platform: str, cfg: dict) -> str:
    """Runs device_test.<platform>.build in the worktree; the artifact is device_test.android.apk / ios.app."""
    command = need(cfg, platform, "build")
    artifact = top / need(cfg, platform, "apk" if platform == "android" else "app")
    BUILDS.mkdir(parents=True, exist_ok=True)
    log = BUILDS / ("%s-%s.log" % (top.name, platform))
    started = time.time()
    with open(log, "w") as f:
        code = subprocess.call(command, shell=True, cwd=str(top), stdout=f, stderr=subprocess.STDOUT)
    minutes = (time.time() - started) / 60
    if code != 0 or not artifact.exists():
        lines = log.read_text(errors="replace").splitlines()
        errors = [l.strip() for l in lines if re.search(r"(^e: |\berror\b|FAILED|What went wrong)", l, re.I)]
        why = " / ".join(dict.fromkeys(errors[:6])) or "no %s after the build" % artifact
        raise Blocked("build failed (exit %d) after %.1f min: %s (log %s)" % (code, minutes, short(why, 600), log))
    # An up-to-date build keeps the artifact's old mtime: record when the build checked its inputs.
    save_json(BUILDS / ("%s-%s.json" % (top.name, platform)), {
        "worktree": str(top), "started": started, "artifact_mtime": newest_mtime(artifact), "commit": head(top)})
    return "built in %.1f min: %s" % (minutes, artifact)


def install_receipt(top: Path, platform: str, artifact: Path, **extra) -> dict:
    commit, mtime = head(top), newest_mtime(artifact)
    b = load_json(BUILDS / ("%s-%s.json" % (top.name, platform)), {})
    started = b.get("started", 0) if (b.get("worktree") == str(top) and b.get("commit") == commit
                                      and b.get("artifact_mtime") == mtime) else 0
    return dict(extra, worktree=str(top), commit=commit, artifact_mtime=mtime, build_started=started,
                installed_at=time.time())


def stale_reason(rec, top: Path, changed_at: float) -> str:
    """'' when the installed build is the worktree's latest, otherwise why it is not."""
    if not rec:
        return "no build installed by devtest: run `deploy` from the worktree under test"
    if Path(rec["worktree"]) != top:
        return "the phone runs a build of %s, not of %s" % (Path(rec["worktree"]).name, top.name)
    built = max(rec["artifact_mtime"], rec.get("build_started", 0))
    if built < changed_at:
        return "the installed build (%s) is older than the last change in %s (%s): deploy again" % (
            hhmm(built), top.name, hhmm(changed_at))
    return ""
