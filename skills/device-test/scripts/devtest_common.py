"""Shared pieces of devtest: output contract, the pack and its config, device registry, leases, log marks, the
prefs codec (androidx DataStore and plist), install receipts.

Exit codes: 0 ok, 1 FAIL (the app did not do what was expected), 2 BLOCKED (device, tooling or setup).

Everything about the app under test comes from a pack: packs/<name>/pack.json plus its flows/, scenarios/, suites/,
hooks.py and runner.md. The pack is the one DEVTEST_PACK names, otherwise `device_test.pack` in the project's
.claude/config/project.json, otherwise the only folder under packs/. The other keys of that `device_test` block
override the pack's. Without a pack the engine still runs (leases, raw UI, logs by pattern), and a command that
needs an app constant stops with BLOCKED and names the key.
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
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKS = HERE.parent / "packs"
UDID_RE = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9a-f]{40}"


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


# ---------------------------------------------------------------------------------------- pack and config

def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _project_block() -> dict:
    sys.path.insert(0, str(HERE.parents[2] / "shared"))
    try:
        from project_config import project_config
    except ImportError:
        return {}
    return project_config(os.environ.get("DEVTEST_WORKTREE") or None).get("device_test") or {}


def load_pack(block: dict, packs: Path = PACKS, named=None) -> tuple:
    """(the pack's folder or None, its pack.json with the project's device_test block on top)."""
    block = dict(block)
    name = named or block.get("pack")
    block.pop("pack", None)
    if name == "none":
        folder = None
    elif name:
        folder = Path(name).expanduser() if os.sep in name else packs / name
    else:
        found = sorted(packs.glob("*/pack.json"))
        folder = found[0].parent if len(found) == 1 else None
    conf = {}
    if folder:
        try:
            conf = json.loads((folder / "pack.json").read_text())
        except (OSError, ValueError) as e:
            raise Blocked("device-test pack %s: %s" % (folder, e))
    return folder, _merge(conf, block)


try:
    PACK, CONFIG = load_pack(_project_block(), named=os.environ.get("DEVTEST_PACK"))
except Blocked as e:
    print("BLOCKED: %s" % e, file=sys.stderr)
    raise SystemExit(2)


def cfg(path: str, default=None):
    """A config value by dotted path: cfg("android.package")."""
    node = CONFIG
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def need(path: str):
    value = cfg(path)
    if value in (None, "", [], {}):
        raise Blocked("no `%s`: set it in %s or in device_test of .claude/config/project.json" % (
            path, PACK / "pack.json" if PACK else "a pack's pack.json (no pack is loaded)"))
    return value


def app_id(platform: str) -> str:
    return need("android.package" if platform == "android" else "ios.bundle_id")


def pack_dir(name: str):
    """The pack's scenarios/ or suites/ folder (or the one its `<name>_dir` key gives); None without a pack."""
    if cfg(name + "_dir"):
        return Path(cfg(name + "_dir")).expanduser()
    return PACK / name if PACK else None


def launch_rules(platform: str) -> dict:
    """The pack's `launch` block for one platform: its shared keys with launch.<platform> on top."""
    block = cfg("launch") or {}
    return _merge({k: v for k, v in block.items() if k not in ("android", "ios")}, block.get(platform) or {})


_hooks = []


def hook(name: str):
    """The function of that name in the pack's hooks.py, or None: what the app needs done in code, not in config."""
    if not _hooks:
        module = None
        if PACK and (PACK / "hooks.py").is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("devtest_pack_hooks", PACK / "hooks.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        _hooks.append(module)
    return getattr(_hooks[0], name, None)


def _phones() -> tuple:
    """phones: alias -> {"platform", "id", "name"}, or alias -> id (the platform is read from the id's shape)."""
    devices, names = {}, {}
    for alias, phone in (cfg("phones") or {}).items():
        phone = phone if isinstance(phone, dict) else {"id": phone}
        ident = phone["id"]
        devices[alias] = (phone.get("platform") or ("ios" if re.fullmatch(UDID_RE, ident) else "android"), ident)
        names[alias] = phone.get("name", alias)
    return devices, names


PKG = cfg("android.package") or cfg("ios.bundle_id") or ""
STATE = Path(os.environ.get("DEVTEST_HOME") or cfg("state_dir") or "~/.cache/devtest").expanduser()
LEASES = STATE / "leases"
RUNS = STATE / "runs"
# The pack's test phones. Any other adb serial or iPhone UDID works too, passed as is.
DEVICES, NAMES = _phones()
DEFAULT_FOR = {**{platform: alias for alias, (platform, _) in reversed(list(DEVICES.items()))},
               **(cfg("default_for") or {})}
SOURCE_ARCHIVE = cfg("source_archive_marker") or ".devtest-source-archive.json"


def notify(text: str) -> None:
    """Runs the pack's `notifier` with the text, for the person at the Mac; does nothing when there is none."""
    notifier = cfg("notifier")
    if notifier:
        run([str(Path(notifier).expanduser()), text], timeout=15)


def text(r: subprocess.CompletedProcess) -> str:
    out = r.stdout if isinstance(r.stdout, str) else r.stdout.decode(errors="replace")
    err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
    return (out + err).strip()


def short(s: str, n: int = 200) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def resolve(name) -> tuple:
    """Maps an alias, serial or UDID to (platform, id, alias)."""
    name = name or os.environ.get("DEVTEST_DEVICE")
    if not name:
        raise Blocked("no device: pass --device %s (or set DEVTEST_DEVICE)" % ("|".join(DEVICES) or "SERIAL|UDID"))
    if name in DEVICES:
        platform, ident = DEVICES[name]
        return platform, ident, name
    for alias, (platform, ident) in DEVICES.items():
        if ident == name:
            return platform, ident, alias
    platform = "ios" if re.fullmatch(UDID_RE, name) else "android"
    return platform, name, name


def device_dir(ident: str) -> Path:
    d = STATE / "dev" / ident
    d.mkdir(parents=True, exist_ok=True)
    return d


def fresh_db_dir(device: Path) -> Path:
    """A new folder for a copy of the app's DB, the earlier copies deleted: each read is a whole copy, used only until
    the next read, and kept they fill the disk in days."""
    base = device / "db"
    if base.is_dir():
        for old in base.iterdir():
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink(missing_ok=True)
    dest = base / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    return dest


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
    if lease and lease.get("expires", 0) > time.time():
        return lease
    return None


def parse_ttl(ttl: str) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", ttl)
    if not m:
        raise Blocked("bad --ttl %s: use 90m, 2h, 600s" % ttl)
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def lease_acquire(ident: str, alias: str, who: str, ttl: str, note: str) -> None:
    if not who:
        raise Blocked("pass --owner <session name> (or set DEVTEST_OWNER)")
    with _lease_lock():
        lease = current_lease(ident)
        if lease and lease["owner"] != who:
            raise Blocked("%s is leased by %s until %s (%s)" % (
                alias, lease["owner"], datetime.fromtimestamp(lease["expires"]).strftime("%H:%M"), lease.get("note", "")))
        expires = time.time() + parse_ttl(ttl)
        from devtest_phones import holder  # which session holds it: `devices --json` shows whether it still works
        save_json(_lease_file(ident), {"owner": who, "alias": alias, "expires": expires, "note": note,
                                       "since": (lease or {}).get("since", time.time()), **holder()})
    say("lease %s -> %s until %s" % (alias, who, datetime.fromtimestamp(expires).strftime("%H:%M")))


def lease_release(ident: str, alias: str, who: str, force: bool) -> None:
    with _lease_lock():
        lease = current_lease(ident)
        if lease and lease["owner"] != who and not force:
            raise Blocked("%s is leased by %s, not by %s (use --force only if that session is gone)" % (
                alias, lease["owner"], who or "you"))
        _lease_file(ident).unlink(missing_ok=True)
    say("lease %s released" % alias)


def lease_status() -> None:
    rows = []
    # Phones with no alias are leased by their serial or UDID: list those too.
    known = {ident for _, ident in DEVICES.values()}
    others = sorted(p.stem for p in LEASES.glob("*.json") if p.stem not in known)
    for alias, ident in [(a, i) for a, (_, i) in DEVICES.items()] + [("-", s) for s in others]:
        lease = current_lease(ident)
        if alias == "-" and not lease:
            continue
        rows.append("%-7s %-26s %s" % (alias, ident, "free" if not lease else "%s until %s %s" % (
            lease["owner"], datetime.fromtimestamp(lease["expires"]).strftime("%H:%M"), lease.get("note", ""))))
    say("\n".join(rows))


def check_lease(ident: str, alias: str) -> None:
    """Device-touching commands refuse a device that another owner holds."""
    lease = current_lease(ident)
    if lease and lease["owner"] != owner():
        raise Blocked("%s is leased by %s until %s: wait, or ask that session to release it" % (
            alias, lease["owner"], datetime.fromtimestamp(lease["expires"]).strftime("%H:%M")))


# ---------------------------------------------------------------------------------------- marks

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
    if isinstance(name, int):
        return name
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


LOG_PRESETS = {
    "crash": r"FATAL EXCEPTION|AndroidRuntime|SIGSEGV|EXC_BAD_ACCESS|Terminating app",
    "baa": r"Baa started on ws://",
    **(cfg("log_presets") or {}),
}


def crash_verdict(lines: list, alive: bool) -> None:
    """Raises for a crash in these log lines: Blocked when the build is older than the data on the phone."""
    text_ = "\n".join(lines)
    m = re.search(r"A migration from (\d+) to (\d+) was required", text_)
    if m and int(m.group(1)) > int(m.group(2)):
        raise Blocked("the app crashed on a database newer than this build (v%s -> v%s): install a build at least as "
                      "new as the data, or reinstall clean (that logs out)" % (m.group(1), m.group(2)))
    hits = grep_lines(lines, "crash")
    if hits:
        reason = next((l for l in hits if "reason:" in l or "Exception" in l), hits[0])
        raise Fail("app crashed: %s" % short(reason.strip(), 400))
    if not alive:
        raise Fail("app process is gone (no crash line in the log)")


def grep_lines(lines: list, pattern: str) -> list:
    rx = re.compile(LOG_PRESETS.get(pattern, pattern), re.I)
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


# ---------------------------------------------------------------------------------------- DataStore

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
            raise Blocked("unsupported protobuf wire type %d in the DataStore file" % wire)
        yield num, wire, val


def _is_plist(buf: bytes) -> bool:
    return buf.startswith(b"bplist") or buf.lstrip().startswith(b"<?xml")


def decode_prefs(buf: bytes) -> dict:
    """A plist, or PreferenceMap { map<string, Value> preferences = 1 } of androidx DataStore."""
    if _is_plist(buf):
        return plistlib.loads(buf)
    out = {}
    for num, _, entry in _fields(buf):
        if num != 1:
            continue
        key, value = None, None
        for n, _, v in _fields(entry):
            if n == 1:
                key = v.decode()
            elif n == 2:
                for vn, vw, vv in _fields(v):
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


def encode_string_pref(buf: bytes, key: str, value) -> bytes:
    """Rewrites the prefs with `key` set to a string value, or removed when value is None. An app that stores its
    keys as JSON strings takes a Boolean as the string "true"."""
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
        if wire == 2:
            kept += _len_field(num, entry)
        else:
            raise Blocked("unexpected top-level field %d in the DataStore file" % num)
    if value is None:
        return kept
    val = _len_field(5, value.encode())
    entry = _len_field(1, key.encode()) + _len_field(2, val)
    return kept + _len_field(1, entry)


def format_prefs(prefs: dict, flt) -> list:
    rows = []
    for k in sorted(prefs):
        if flt and flt.lower() not in k.lower():
            continue
        v = prefs[k]
        rows.append("%s = %s" % (k, short(v if isinstance(v, str) else json.dumps(v), 160)))
    return rows


# ---------------------------------------------------------------------------------------- sqlite

def sqlite_query(db: Path, sql: str, limit: int = 40) -> list:
    r = run(["sqlite3", "-header", "-separator", " | ", str(db), sql], timeout=60, text=True)
    if r.returncode != 0:
        raise Blocked("sqlite3: %s" % short(text(r), 300))
    rows = r.stdout.splitlines()
    if len(rows) > limit + 1:
        rows = rows[:limit + 1] + ["… %d more rows" % (len(rows) - limit - 1)]
    return rows


# ---------------------------------------------------------------------------------------- worktree

NOT_IN_BUILD = re.compile(cfg("not_in_build") or r"(^|/)(\.claude|docs|tests?|[A-Za-z]*Tests?|\w+[_-]tests?)/|\.md$")


def source_archive(top: Path):
    """A folder of sources made with `git archive`, marked by a manifest, stands in for a clone of the repo."""
    marker = top / SOURCE_ARCHIVE
    if not marker.exists():
        return None
    rec = load_json(marker, {})
    if (rec.get("kind") != "git-archive" or not re.fullmatch(r"[0-9a-f]{40}", rec.get("commit", ""))
            or not isinstance(rec.get("prepared_at"), (int, float)) or not (top / (cfg("source_dir") or ".")).is_dir()):
        raise Blocked("invalid source archive manifest: %s" % marker)
    return rec


def worktree(arg=None) -> Path:
    cwd = Path(arg).expanduser().resolve() if arg else Path.cwd()
    if source_archive(cwd):
        return cwd
    r = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], text=True)
    if r.returncode != 0:
        raise Blocked("%s is not inside a clone of the app's repo: pass --worktree" % cwd)
    top = Path(r.stdout.strip())
    if cfg("source_dir") and not (top / cfg("source_dir")).is_dir():
        raise Blocked("%s has no %s/ folder" % (top, cfg("source_dir")))
    return top


def worktree_changed_at(top: Path) -> float:
    """Newest of: HEAD commit time and mtime of every modified/untracked source file that goes into the build."""
    archive = source_archive(top)
    if archive:
        return max(archive["prepared_at"], (top / SOURCE_ARCHIVE).stat().st_mtime)
    r = run(["git", "-C", str(top), "log", "-1", "--format=%ct"], text=True)
    newest = float(r.stdout.strip() or 0)
    r = run(["git", "-C", str(top), "status", "--porcelain", "--", cfg("source_dir") or "."], text=True)
    suffixes = cfg("build_suffixes")
    for line in r.stdout.splitlines():
        rel = line[3:].split(" -> ")[-1].strip('"')
        if NOT_IN_BUILD.search(rel):
            continue
        path = top / rel
        if (not suffixes or path.suffix in suffixes) and path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return newest


def stale_reason(rec, top: Path, changed_at: float) -> str:
    """'' when the installed build (an install receipt) is the worktree's latest, otherwise why it is not."""
    if not rec:
        return "no build installed by devtest: run `deploy` from the worktree under test"
    if Path(rec["worktree"]) != top:
        return "the phone runs a build of %s, not of %s" % (Path(rec["worktree"]).name, top.name)
    built = max(rec.get("apk_mtime") or rec.get("app_mtime") or 0, rec.get("build_started_at") or 0)
    if built < changed_at:
        return "the installed build (%s) is older than the last change in %s (%s): deploy again" % (
            hhmm(built), top.name, hhmm(changed_at))
    return ""


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")
