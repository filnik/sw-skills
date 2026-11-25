#!/usr/bin/env python3
"""Tests for skills/memguard/scripts/memguard.py. Run: python3 tests/test_memguard.py (exit 0 = green).
LiveTest starts three throwaway processes that allocate up to ~2 GB and kills them at the end. macOS only."""
import importlib.util
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

if sys.platform != "darwin":
    print("skipped: memguard is macOS only")
    sys.exit(0)

spec = importlib.util.spec_from_file_location(
    "memguard", Path(__file__).resolve().parents[1] / "skills/memguard/scripts/memguard.py")
memguard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memguard)
GB, GrowthTracker, Proc = memguard.GB, memguard.GrowthTracker, memguard.Proc
descendants, is_agent_root = memguard.descendants, memguard.is_agent_root

CLAUDE = "/Users/me/.local/share/claude/versions/2.1.323"
CODEX = "/Users/me/.codex/packages/x/bin/codex"


class TrackerTest(unittest.TestCase):
    def run_samples(self, samples):
        t = GrowthTracker(window_s=5, growth_bytes=6 * GB, min_bytes=20 * GB, hard_bytes=30 * GB)
        return [t.observe("k", ts, gb * GB) is not None for ts, gb in samples]

    def test_explosive_growth_flagged_once_past_20gb(self):  # a regex git grep: ~5 GB/s
        self.assertEqual(self.run_samples([(s, 0.1 + 5 * s) for s in range(6)]),
                         [False, False, False, False, True, True])

    def test_fast_burst_capped_below_20gb_is_spared(self):  # a Gradle daemon with -Xmx14g
        self.assertFalse(any(self.run_samples([(s, min(2 + 4 * s, 16)) for s in range(30)])))

    def test_big_but_stable_under_30gb_is_spared(self):
        self.assertFalse(any(self.run_samples([(s, 25) for s in range(60)])))

    def test_slow_growth_flagged_only_at_30gb(self):
        flags = self.run_samples([(s, s * 0.5) for s in range(80)])
        self.assertEqual(flags.index(True), 60)  # 0.5 GB/s, reaches 30 GB at s=60


class AgentTest(unittest.TestCase):
    def test_agent_roots(self):
        self.assertTrue(is_agent_root(CLAUDE, "/bin/zsh"))
        self.assertTrue(is_agent_root(CODEX, "/bin/zsh"))
        self.assertFalse(is_agent_root(CLAUDE, CLAUDE))  # Claude's embedded ripgrep
        self.assertFalse(is_agent_root("/usr/bin/git", CODEX))

    def test_only_processes_under_an_agent(self):
        parent_of = {
            10: 1,   # terminal app
            11: 10,  # my shell
            12: 11,  # git I launched myself
            20: 11,  # claude
            21: 20,  # its shell
            22: 21,  # codex launched by claude
            23: 22,  # git grep launched by codex
            30: 1,   # unrelated daemon
        }
        self.assertEqual(descendants(parent_of, {20, 22}), {21, 23})


class GuardTest(unittest.TestCase):
    def test_orphan_stays_watched_and_is_killed_once(self):
        table, sizes, kills = {}, {}, []
        guard = memguard.Guard(is_agent=lambda p, procs: p.comm == "claude",
                               kill=lambda pid, *a: kills.append(pid),
                               list_procs=lambda: dict(table), footprint=sizes.get)
        table.update({
            12: Proc(12, 11, (12, 0, 0), "mine"),    # launched by me
            20: Proc(20, 11, (20, 0, 0), "claude"),
            21: Proc(21, 20, (21, 0, 0), "zsh"),
            23: Proc(23, 21, (23, 0, 0), "job"),     # background job of the agent
        })
        sizes.update({12: 1 * GB, 23: 1 * GB})
        guard.scan()
        del table[21]                                # the agent's shell exits...
        table[23] = Proc(23, 1, (23, 0, 0), "job")  # ...and the job is reparented
        sizes.update({12: 31 * GB, 23: 31 * GB})
        guard.scan()
        guard.scan()                                 # still listed while dying
        self.assertEqual(kills, [23])


def _child(chunk_mb, pause_s, max_mb):
    code = ("import time\nchunks=[]\n"
            "while len(chunks)*%d < %d:\n"
            "    chunks.append(b'x' * (%d << 20)); time.sleep(%r)\n"
            "time.sleep(60)\n") % (chunk_mb, max_mb, chunk_mb, pause_s)
    return subprocess.Popen([sys.executable, "-c", code])


class LiveTest(unittest.TestCase):
    """Real processes, real footprint and SIGKILL, thresholds scaled down."""

    def spawn(self, **kw):
        p = _child(**kw)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)  # cleanups run last-in first-out: kill, then wait
        return p

    def test_only_big_fast_grower_is_killed(self):
        fast_big = self.spawn(chunk_mb=100, pause_s=0.05, max_mb=3000)   # ~2 GB/s, no cap
        fast_small = self.spawn(chunk_mb=100, pause_s=0.05, max_mb=700)  # ~2 GB/s, stops under 1 GB
        slow = self.spawn(chunk_mb=50, pause_s=0.5, max_mb=1500)         # ~0.1 GB/s
        me = os.getpid()
        guard = memguard.Guard(window_s=2, growth_bytes=GB // 2, min_bytes=1 * GB,
                               is_agent=lambda p, procs: p.pid == me,
                               kill=lambda pid, *a: os.kill(pid, 9))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and fast_big.poll() is None:
            guard.scan()
            time.sleep(0.2)
        self.assertEqual(fast_big.wait(timeout=2), -9)
        for _ in range(15):  # keep watching the other two for 3 more seconds
            guard.scan()
            time.sleep(0.2)
        self.assertIsNone(fast_small.poll())
        self.assertIsNone(slow.poll())
        self.assertGreater(memguard.footprint(fast_small.pid), 600 << 20)


if __name__ == "__main__":
    unittest.main()
