#!/usr/bin/env python3
"""memguard: kill agent-launched processes whose memory explodes.

A process counts as agent-launched when Claude Code or Codex is one of its
ancestors (it stays counted if the agent exits and it gets orphaned).
Processes started by hand in a terminal are never touched, and neither are
the agent processes themselves. Claude Code's helpers that re-run its own
binary (its embedded ripgrep, argv0 "rg") are not agents: they are watched.

Rule: kill (SIGKILL) when the memory footprint (the "Memory" column of
Activity Monitor, compressed memory included) is at least MIN_GB and grew by
GROWTH_GB or more within the last WINDOW_S seconds: big and still growing
fast. Also kill at HARD_GB or more whatever the speed: no ordinary dev process
gets there (a JVM such as the Gradle daemon is capped by its -Xmx). A process
that grows fast but stays below MIN_GB, or that sits between MIN_GB and
HARD_GB without growing fast, is left alone. Change the rules here and run
install.sh again.

macOS only. install.sh runs it as a user LaunchAgent; it logs each kill to
~/Library/Logs/memguard.log and shows a notification.
`memguard.py --list` prints the agents and their processes once, then exits.
"""
import collections
import ctypes
import logging
import os
import signal
import subprocess
import sys
import time

GB = 1024 ** 3
INTERVAL_S = 1.0
WINDOW_S = 5.0
GROWTH_GB = 6.0
MIN_GB = 20.0
HARD_GB = 30.0

LOG_PATH = os.path.expanduser("~/Library/Logs/memguard.log")
log = logging.getLogger("memguard")

_lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
PROC_PIDTBSDINFO = 3
RUSAGE_INFO_V0 = 0


class _BsdInfo(ctypes.Structure):  # struct proc_bsdinfo, <sys/proc_info.h>
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid",
        "ruid", "rgid", "svuid", "svgid", "rfu_1")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (n, ctypes.c_uint32) for n in (
            "nfiles", "pgid", "pjobc", "e_tdev", "e_tpgid", "nice")] + [
        ("start_tvsec", ctypes.c_uint64), ("start_tvusec", ctypes.c_uint64)]


class _RusageV0(ctypes.Structure):  # struct rusage_info_v0, <sys/resource.h>
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
        "pageins", "wired_size", "resident_size", "phys_footprint",
        "proc_start_abstime", "proc_exit_abstime")]


assert ctypes.sizeof(_BsdInfo) == 136

Proc = collections.namedtuple("Proc", "pid ppid key comm")


def list_procs():
    """This user's processes as {pid: Proc}; key is (pid, start time)."""
    n = _lib.proc_listallpids(None, 0)
    if n <= 0:
        return {}
    pids = (ctypes.c_int * (n + 256))()
    n = _lib.proc_listallpids(pids, ctypes.sizeof(pids))
    uid = os.getuid()
    info = _BsdInfo()
    procs = {}
    for pid in pids[:max(n, 0)]:
        if pid <= 0:
            continue
        got = _lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, ctypes.c_uint64(0),
                                ctypes.byref(info), ctypes.sizeof(info))
        if got != ctypes.sizeof(info) or info.uid != uid:
            continue
        key = (pid, info.start_tvsec, info.start_tvusec)
        procs[pid] = Proc(pid, info.ppid, key, info.comm.decode(errors="replace"))
    return procs


def footprint(pid):
    """Physical footprint in bytes, or None if the process is gone."""
    ru = _RusageV0()
    if _lib.proc_pid_rusage(pid, RUSAGE_INFO_V0, ctypes.byref(ru)) != 0:
        return None
    return ru.phys_footprint


def exe_path(pid):
    buf = ctypes.create_string_buffer(4096)
    n = _lib.proc_pidpath(pid, buf, ctypes.sizeof(buf))
    return buf.value.decode(errors="replace") if n > 0 else ""


def is_agent_path(path):
    """Claude Code (native install) or Codex."""
    return "/.local/share/claude/versions/" in path or os.path.basename(path) == "codex"


def is_agent_root(path, parent_path):
    """An agent, unless started directly by an agent binary: that is a helper
    such as Claude Code's embedded ripgrep, which re-executes Claude's binary."""
    return is_agent_path(path) and not is_agent_path(parent_path)


def default_is_agent(proc, procs):
    return is_agent_root(exe_path(proc.pid), exe_path(proc.ppid))


def descendants(parent_of, roots):
    """Pids with an ancestor in roots (roots themselves excluded)."""
    memo = {}
    for pid in parent_of:
        chain, seen, cur = [], set(), pid
        while cur not in memo:
            chain.append(cur)
            seen.add(cur)
            ppid = parent_of.get(cur, 0)
            if ppid in roots:
                memo[cur] = True
            elif ppid <= 1 or ppid not in parent_of or ppid in seen:
                memo[cur] = False
            else:
                cur = ppid
        for c in chain:
            memo[c] = memo[cur]
    return {p for p, under in memo.items() if under and p not in roots}


class GrowthTracker:
    """Flags a process at hard_bytes, or at min_bytes after rising growth_bytes within window_s."""

    def __init__(self, window_s, growth_bytes, min_bytes, hard_bytes):
        self.window_s = window_s
        self.growth_bytes = growth_bytes
        self.min_bytes = min_bytes
        self.hard_bytes = hard_bytes
        self.history = {}

    def observe(self, key, t, size):
        """The growth within the window if the process must be killed, else None."""
        h = self.history.setdefault(key, collections.deque())
        h.append((t, size))
        while t - h[0][0] > self.window_s:
            h.popleft()
        growth = size - min(s for _, s in h)
        if size >= self.hard_bytes or (size >= self.min_bytes and growth >= self.growth_bytes):
            return growth
        return None

    def retain(self, alive_keys):
        for k in [k for k in self.history if k not in alive_keys]:
            del self.history[k]


def _chain(procs, pid):
    names, seen = [], set()
    while pid in procs and pid not in seen and len(names) < 12:
        seen.add(pid)
        names.append("%s(%d)" % (procs[pid].comm, pid))
        pid = procs[pid].ppid
    return " <- ".join(names)


def kill_process(pid, procs, size, growth):
    try:
        os.kill(pid, signal.SIGSTOP)  # freeze it now: reading its command line takes time
    except ProcessLookupError:
        return
    except OSError as e:
        log.error("cannot stop pid=%d %s: %s", pid, procs[pid].comm, e)
        return
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=1).stdout.strip()
    except Exception:
        cmd = "?"
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as e:
        log.error("cannot kill pid=%d %s: %s", pid, procs[pid].comm, e)
        return
    msg = "%s killed: %.1f GB, +%.1f GB in %gs" % (
        procs[pid].comm, size / GB, growth / GB, WINDOW_S)
    log.warning("KILLED pid=%d %.1f GB (+%.1f GB) | %s | %s",
                pid, size / GB, growth / GB, cmd[:2000], _chain(procs, pid))
    try:
        subprocess.Popen(
            ["osascript", "-e", "on run argv", "-e",
             'display notification (item 1 of argv) with title "memguard"',
             "-e", "end run", msg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Guard:
    def __init__(self, window_s=WINDOW_S, growth_bytes=GROWTH_GB * GB,
                 min_bytes=MIN_GB * GB, hard_bytes=HARD_GB * GB,
                 is_agent=default_is_agent, kill=kill_process,
                 list_procs=list_procs, footprint=footprint):
        self.tracker = GrowthTracker(window_s, growth_bytes, min_bytes, hard_bytes)
        self.is_agent = is_agent
        self.kill = kill
        self.list_procs = list_procs
        self.footprint = footprint
        self.tagged = set()   # keys seen under an agent: stay watched once orphaned
        self.killed = set()   # keys already killed: a dying process is not killed twice

    def scan(self):
        procs = self.list_procs()
        alive = {p.key for p in procs.values()}
        self.tagged &= alive
        self.killed &= alive
        self.tracker.retain(alive)

        agents = {p.pid for p in procs.values()
                  if p.key not in self.tagged and self.is_agent(p, procs)}
        tagged_pids = {k[0] for k in self.tagged}
        watched = descendants({p.pid: p.ppid for p in procs.values()},
                              agents | tagged_pids) | tagged_pids

        killed = []
        for pid in watched:
            key = procs[pid].key
            self.tagged.add(key)
            size = None if key in self.killed else self.footprint(pid)
            if size is None:
                continue
            growth = self.tracker.observe(key, time.monotonic(), size)
            if growth is not None:
                self.killed.add(key)
                self.kill(pid, procs, size, growth)
                killed.append(pid)
        return agents, watched, killed


def list_once():
    procs = list_procs()
    agents, watched, _ = Guard(kill=lambda *a: None, list_procs=lambda: procs).scan()
    for pid in sorted(agents):
        print("AGENT %6d %s" % (pid, exe_path(pid)))
    rows = sorted(((footprint(p) or 0, p) for p in watched), reverse=True)
    for size, pid in rows[:25]:
        print("  %6d %7.2f GB  %s" % (pid, size / GB, procs[pid].comm))
    print("%d agents, %d processes watched" % (len(agents), len(watched)))


def main():
    if "--list" in sys.argv:
        list_once()
        return
    logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("started: kill agent processes above %g GB growing >= %g GB in %g s,"
             " or above %g GB", MIN_GB, GROWTH_GB, WINDOW_S, HARD_GB)
    guard = Guard()
    while True:
        start = time.monotonic()
        try:
            guard.scan()
        except Exception:
            log.exception("scan failed")
        time.sleep(max(0.05, INTERVAL_S - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
