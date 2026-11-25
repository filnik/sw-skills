"""Scenarios as code: one line per step, fail fast with evidence, one verdict line.

    from devtest_scenario import Scenario
    S = Scenario("SHOP-12 short title", criteria={"AC1": "…", "AC2": "…"})

    @S.pre
    def setup(d):
        d.flag("newCart", "on")
        d.launch(wait_screen="screen.cart")

    @S.step("Switch on sends the event and stores the choice", covers=["AC1"])
    def _(d):
        m = d.mark()
        d.tap("settings.reminders.switch")
        d.expect_log(r"track: reminders_toggle_switched", since=m)
        d.expect_pref("reminders_user_ids", contains="42")

    S.skip("AC2", "needs LiDAR, the test iPhone has none")

Rules the runner enforces, so a scenario cannot pass without looking:
  - log expectations read only the lines written after their mark;
  - every expect_absent / expect_no_log / expect_pref(absent) / expect_file(exists=False) needs the same
    target asserted present somewhere in the run, otherwise the run ends as SCENARIO ERROR; expect_no_log can
    instead name an observed positive-control pattern, asserted present in the same log window;
  - after every step the app must still be running (unless the step stopped it on purpose);
  - every criterion is covered by a step or skipped with a reason;
  - there is no sleep: wait on a condition with d.wait(...).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

from devtest_common import (RUNS, Blocked, Fail, decode_prefs, encode_string_pref, format_prefs, grep_lines, hook, need,
                            read_since, short, source_archive, sqlite_query, wait_log)


class ScenarioError(Exception):
    """The scenario itself is wrong (uncovered criterion, absence never paired with presence)."""


class Mark:
    def __init__(self, offset: int, at: float):
        self.offset, self.at = offset, at


class Scenario:
    def __init__(self, title: str, criteria=None, platforms=("android", "ios")):
        self.title = title
        self.criteria = dict(criteria or {})
        self.platforms = tuple(platforms)
        self.pre_fn = None
        self.post_fn = None
        self.steps = []
        self.skipped = {}

    def pre(self, fn):
        self.pre_fn = fn
        return fn

    def post(self, fn):
        """Always runs after the steps, pass or fail: restore the device. Its errors are reported, not judged."""
        self.post_fn = fn
        return fn

    def step(self, title: str, covers=(), platforms=None):
        def deco(fn):
            self.steps.append({"title": title, "fn": fn, "covers": tuple(covers),
                               "platforms": tuple(platforms) if platforms else self.platforms})
            return fn
        return deco

    def skip(self, criterion: str, reason: str) -> None:
        self.skipped[criterion] = reason

    def select(self, only=(), skip=()) -> None:
        """Keeps the steps that cover a criterion in `only` (or whose number is in it) and drops those covering one
        in `skip`; numbers count the steps of the platform, as the run prints them. A step that covers nothing is kept
        unless its number is skipped. Later steps may need what a dropped one leaves: choose with care."""
        only, skip = set(only), set(skip)
        for p in ("android", "ios"):
            n = 0
            for s in self.steps:
                if p not in s["platforms"]:
                    continue
                n += 1
                keys = set(s["covers"]) | {str(n)}
                s.setdefault("off", set())
                if keys & skip or (only and s["covers"] and not keys & only):
                    s["off"].add(p)

    def validate(self) -> None:
        unknown = sorted({c for s in self.steps for c in s["covers"]} - set(self.criteria))
        if unknown:
            raise ScenarioError("steps cover unknown criteria: %s" % ", ".join(unknown))
        covered = {c for s in self.steps for c in s["covers"]} | set(self.skipped)
        missing = [c for c in self.criteria if c not in covered]
        if missing:
            raise ScenarioError("criteria with no step and no skip reason: %s" % ", ".join(missing))


class Driver:
    """The `d` handed to every step: actions plus expectations on one device."""

    def __init__(self, dev, top: Path, evidence: Path, ledger: dict):
        self.dev, self.top, self.evidence, self.ledger = dev, top, evidence, ledger
        self.platform = dev.platform
        self.stopped_on_purpose = False
        self.notes = []
        self.unverifiable = {}

    def unverified(self, criterion: str, reason: str) -> None:
        """This criterion cannot be observed on this platform: reported as N/A with the reason, never as PASS."""
        self.unverifiable[criterion] = reason

    # ------------------------------------------------------------------------------------ helpers

    def _seen(self, kind: str, target: str, present: bool) -> None:
        self.ledger.setdefault((kind, target), set()).add(present)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def mark(self) -> Mark:
        log = self.dev.log
        return Mark(log.stat().st_size if log.exists() else 0, time.time())

    def wait(self, cond, timeout: float, label: str, poll: float = 1.0):
        deadline = time.time() + timeout
        last = None
        while True:
            last = cond()
            if last:
                return last
            if time.time() >= deadline:
                raise Fail("%s: not true within %ds" % (label, timeout))
            time.sleep(poll)

    # ------------------------------------------------------------------------------------ actions

    def launch(self, env=None, wait_screen=None, restart: bool = False, timeout: float = 90) -> None:
        """env: the app's environment when it asks at launch (default: the pack's default_env)."""
        self.stopped_on_purpose = False
        self.dev.last_launch = self.mark()  # a flow can wait on what this launch starts (a startup sync, say)
        self.note(self.dev.launch(env=env, wait_screen=wait_screen, restart=restart, timeout=timeout))

    def restart(self, env=None, wait_screen=None) -> None:
        self.launch(env=env, wait_screen=wait_screen, restart=True)

    def stop(self) -> None:
        self.dev.stop()
        self.stopped_on_purpose = True

    def tap(self, sel: str, timeout: float = 10) -> None:
        self.dev.tap(sel, timeout)

    def set_text(self, sel: str, value: str, timeout: float = 10) -> None:
        self.dev.set_text(sel, value, timeout)

    def back(self) -> None:
        self.dev.back()

    def swipe(self, direction: str) -> None:
        self.dev.swipe(direction)

    def refresh(self) -> None:
        self.dev.refresh()

    def hide_keyboard(self) -> None:
        self.dev.hide_keyboard()

    def wifi(self, on: bool) -> None:
        self.note(self.dev.wifi(on))

    def airplane(self, on: bool) -> None:
        self.note(self.dev.airplane(on))

    def metered(self, on: bool) -> None:
        """Android: the current Wi-Fi marked metered (as cellular or a hotspot looks to the app), or put back. iOS:
        BLOCKED (no scriptable metered Wi-Fi)."""
        self.note(self.dev.metered(on))

    def isolate(self, on: bool) -> None:
        """Android: the app alone cut off the network (per-app firewall), the phone still online, adb over Wi-Fi
        intact. The app sees a network and its calls fail: isolation from the server, not the app's offline state."""
        if self.platform != "android":
            raise Blocked("isolate is Android only (per-app firewall)")
        self.note(self.dev.isolate(on))

    def set_pref(self, key: str, value) -> None:
        """Writes one key of the app's prefs with the app stopped; value None removes it. Relaunch afterwards."""
        self.stop()
        data = encode_string_pref(self.dev.prefs_bytes(), key, None if value is None else
                                  (value if isinstance(value, str) else json.dumps(value)))
        self.dev.write_prefs(data)
        got = decode_prefs(self.dev.prefs_bytes()).get(key)
        want = None if value is None else (value if isinstance(value, str) else json.dumps(value))
        if got != want:
            raise Blocked("prefs write of %s did not stick: read back %r" % (key, got))

    def flag(self, name: str, state: str) -> None:
        """A feature flag's local override through the prefs: on, off or default (removed). Stops the app. The
        pack's override_key hook maps the flag's name to its key."""
        from devtest import override_key
        key = override_key(self.top, name)
        self.set_pref(key, {"on": "true", "off": "false", "default": None}[state])
        self.note("%s=%s" % (key, state))

    def baa(self, *args, timeout: float = 120) -> str:
        port = self.dev.baa_port()
        r = subprocess.run([str(self.top / need("baa")), "--port", str(port), *args], capture_output=True,
                           text=True, timeout=timeout, cwd=str(self.top))
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            raise Fail("baa %s: %s" % (" ".join(args)[:80], short(out, 500)))
        return out

    def baa_run(self, name: str, params=None, timeout: int = 180):
        port = self.dev.baa_port()
        cli = str(self.top / need("baa"))
        try:
            # Keep parameters private (0600) and remove them even when the CLI fails.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", prefix="devtest-baa-") as f:
                json.dump({} if params is None else params, f)
                f.flush()
                r = subprocess.run([cli, "--port", str(port), "run", str(name), "--params", f.name,
                                    "--timeout", str(timeout), "--json"], capture_output=True, text=True,
                                   timeout=timeout + 90, cwd=str(self.top))
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Fail("baa_run %s: %s" % (type(e).__name__, short(str(getattr(e, "stderr", None) or e), 300))) from None
        try:
            report = json.loads(r.stdout)
        except ValueError:
            raise Fail("baa_run no JSON report (exit %d): %s" % (r.returncode, short(r.stderr, 300))) from None
        if r.returncode == 0 and report.get("status") == "success":
            return report["result"]
        evidence_id = report.get("evidenceId")
        folder = self.evidence / ("baa-" + str(evidence_id or time.time_ns()))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        if evidence_id:
            for kind in ("trace", "tree", "screenshot"):
                saved = subprocess.run([cli, "evidence", evidence_id, kind], capture_output=True,
                                       text=True, timeout=30, cwd=str(self.top))
                if saved.returncode != 0:
                    continue  # an evicted payload must not hide the script's own failure
                if kind == "screenshot":
                    shutil.copyfile(saved.stdout.strip(), folder / "screenshot.png")
                else:
                    (folder / (kind + (".json" if kind == "tree" else ".txt"))).write_text(saved.stdout)
        raise Fail("baa_run %s: %s; evidence: %s" % (report.get("status", "failed"), report.get("error") or "", folder))

    # ------------------------------------------------------------------------------------ expectations

    def expect_screen(self, tag: str, timeout: float = 10) -> None:
        self.wait(lambda: tag in self.dev.screens(), timeout, "screen %s" % tag, poll=0.8)
        self._seen("ui", tag, True)

    def expect_visible(self, sel: str, timeout: float = 10) -> None:
        if not self.dev.visible(sel, timeout):
            raise Fail("%s not visible within %ds (screens: %s)" % (sel, timeout, ", ".join(self.dev.screens()) or "-"))
        self._seen("ui", sel, True)

    def expect_absent(self, sel: str, settle: float = 3) -> None:
        """Absent for `settle` seconds. Assert the screen first, so absence is not just 'not loaded yet'."""
        deadline = time.time() + settle
        while time.time() < deadline:
            if self.dev.visible(sel):
                raise Fail("%s is visible but should not be (screens: %s)" % (sel, ", ".join(self.dev.screens()) or "-"))
            time.sleep(0.8)
        self._seen("ui", sel, False)

    def expect_state(self, sel: str, checked=None, selected=None, text=None, timeout: float = 10) -> None:
        def ok():
            n = self.dev.node(sel)
            if n is None:
                return False
            return ((checked is None or n.checked == checked) and (selected is None or n.selected == selected)
                    and (text is None or text in (n.text, n.desc, n.subtree_text())))
        try:
            self.wait(ok, timeout, sel, poll=0.8)
        except Fail:
            n = self.dev.node(sel)
            got = "missing" if n is None else "checked=%s selected=%s text=%r desc=%r" % (n.checked, n.selected, n.text, n.desc)
            raise Fail("%s: wanted %s, got %s" % (sel, ", ".join("%s=%r" % kv for kv in (
                ("checked", checked), ("selected", selected), ("text", text)) if kv[1] is not None), got))
        self._seen("ui", sel, True)

    def _capture_silent(self, since: Mark, what: str) -> None:
        """BLOCKED when the capture recorded no line at all for 20s or more after the mark: the capture is down (iOS:
        the console detaches when something other than devtest relaunches the app), so a missing or absent line proves
        nothing about the app."""
        quiet = time.time() - since.at
        if quiet >= 20 and not read_since(self.dev.log, since.offset):
            raise Blocked("%s: the log capture recorded no line in the %ds since the mark, so the capture is down, "
                          "not judged against the app" % (what, quiet))

    def expect_log(self, pattern: str, since: Mark, timeout: float = 15) -> str:
        # USB can come back with logcat dead.
        # Reopen with its last device timestamp so a complete ring-buffer backfill can be read.
        deadline = time.time() + timeout
        while True:
            if self.platform == 'android':
                self.dev.ensure_capture()
            remaining = max(0, deadline - time.time())
            line = wait_log(self.dev.log, since.offset, pattern, min(5, remaining))
            if line or time.time() >= deadline:
                break
        if not line:
            self._capture_silent(since, "no log /%s/" % short(pattern, 80))
            tail = read_since(self.dev.log, since.offset)
            word = max(re.findall(r"[A-Za-z_]{4,}", pattern) or [""], key=len)
            near = [l for l in tail if word and word.lower() in l.lower()]
            seen = ("lines with '%s': %s" % (word, short(" ⏎ ".join(l[-160:] for l in near[-3:]), 500)) if near
                    else "no new line mentions '%s'" % word)
            raise Fail("no log /%s/ within %ds after the mark (%d new lines; %s)" % (pattern, timeout, len(tail), seen))
        self._seen("log", pattern, True)
        return line

    def expect_no_log(self, pattern: str, since: Mark, within: float = 5, observed: str = None) -> None:
        """No matching line in this window. `observed` supplies a positive control in the SAME window when the
        forbidden event must never occur in the run (a sample input forbidden, the real input observed).
        Without it the usual same-target presence guard still applies."""
        line = wait_log(self.dev.log, since.offset, pattern, within)
        if line:
            raise Fail("unexpected log /%s/: %s" % (pattern, short(line, 300)))
        self._capture_silent(since, "absence of /%s/" % short(pattern, 80))
        if observed is not None:
            if True not in self.ledger.get(("log", observed), set()):
                raise ScenarioError("expect_no_log observed= needs a preceding expect_log of /%s/" % observed)
            if not any(re.search(observed, line) for line in read_since(self.dev.log, since.offset)):
                raise ScenarioError("positive control /%s/ absent from the forbidden-log window" % observed)
        else:
            self._seen("log", pattern, False)

    def prefs(self) -> dict:
        return decode_prefs(self.dev.prefs_bytes())

    def expect_pref(self, key: str, equals=None, contains=None, lacks=None, absent: bool = False, timeout: float = 10) -> None:
        """`lacks` passes when the key is missing or does not contain the text; like `absent`, it needs a
        `contains`/`equals` on the same key somewhere in the run. The pack's pref_key hook can map a key to the
        name this build stores it under (a renamed key); prefs() stays raw."""
        if hook("pref_key"):
            key = hook("pref_key")(self, key)
        def check():
            v = self.prefs().get(key)
            if absent:
                return v is None
            if lacks is not None:
                return v is None or lacks not in str(v)
            if v is None:
                return False
            return (equals is None or v == equals) and (contains is None or contains in str(v))
        try:
            self.wait(check, timeout, key, poll=1.5)
        except Fail:
            want = ("absent" if absent else "without %r" % lacks if lacks is not None else
                    "== %r" % equals if equals is not None else "containing %r" % contains)
            raise Fail("pref %s = %r, wanted %s" % (key, self.prefs().get(key), want))
        self._seen("pref", key, not absent and lacks is None)

    def put_media(self, relative: str, local: Path = None) -> str:
        """Pushes a real local media file, or creates a small placeholder when local is omitted. Returns the path
        expect_file takes. A test of real media processing must supply its own vetted source."""
        if local is not None:
            local = Path(local)
            if not local.is_file():
                raise Blocked("media source missing: %s" % local)
            return self.dev.put_file(relative, local)
        local = self.evidence.parent / ("seed-%s" % relative.replace("/", "_"))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"\xff\xd8\xff\xe0devtest seed\xff\xd9")
        return self.dev.put_file(relative, local)

    def query(self, sql: str, limit: int = 40) -> list:
        for attempt in (1, 2, 3):
            try:
                return sqlite_query(self.dev.pull_db(), sql, limit=limit)
            except Blocked as e:
                # A copy of the live DB torn by the app's writes: a new copy is consistent again.
                if attempt == 3 or "malformed" not in str(e) and "not a database" not in str(e):
                    raise
                time.sleep(2)

    def expect_db(self, sql: str, equals=None, contains=None) -> None:
        rows = self.query(sql)
        body = "\n".join(rows[1:])
        if equals is not None and body.strip() != str(equals).strip():
            raise Fail("%s -> %r, wanted %r" % (short(sql, 120), short(body, 200), equals))
        if contains is not None and str(contains) not in body:
            raise Fail("%s -> %r, wanted it to contain %r" % (short(sql, 120), short(body, 200), contains))

    def expect_file(self, path: str, exists: bool = True, timeout: float = 10) -> None:
        try:
            self.wait(lambda: self.dev.file_exists(path) == exists, timeout, path, poll=2)
        except Fail:
            raise Fail("%s %s" % (path, "is missing" if exists else "still exists"))
        self._seen("file", path, exists)

    # ------------------------------------------------------------------------------------ evidence

    def collect(self, name: str, since: int) -> str:
        self.evidence.mkdir(parents=True, exist_ok=True)
        saved, missing = [], []
        try:
            self.dev.shot(self.evidence / ("%s.png" % name))
            saved.append("png")
        except Exception as e:  # evidence must never hide the failure itself
            missing.append("no screenshot: %s" % short(str(e), 80))
        try:
            (self.evidence / ("%s.ui.txt" % name)).write_text("\n".join(self.dev.src()))
            saved.append("ui.txt")
        except Exception as e:
            missing.append("no ui tree: %s" % short(str(e), 80))
        (self.evidence / ("%s.log" % name)).write_text("\n".join(read_since(self.dev.log, since)[-400:]))
        saved.append("log")
        return "%s/%s.{%s}%s" % (self.evidence, name, ",".join(saved), " (%s)" % "; ".join(missing) if missing else "")


def load(path: Path) -> Scenario:
    spec = importlib.util.spec_from_file_location("scenario_%s" % re.sub(r"\W", "_", path.stem), str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found = [v for v in vars(mod).values() if isinstance(v, Scenario)]
    if len(found) != 1:
        raise ScenarioError("%s must define exactly one Scenario" % path)
    return found[0]


def dur(s: float) -> str:
    return "%.1fs" % s if s < 60 else "%dm%02ds" % (s // 60, s % 60)


def wda_restarts(dev) -> int:
    """WDA restarts so far on the phone and on its colleague (iOS; always 0 on Android)."""
    colleague = getattr(dev, "flow", {}).get("colleague")
    return sum(getattr(x, "wda_restarts", 0) for x in [dev] + ([colleague.dev] if colleague else []))


def run_once(sc: Scenario, dev, top: Path, run_dir: Path, verbose: bool) -> dict:
    """Runs pre + steps on one device. Returns {status, failed_step, lines, criteria}."""
    ledger, lines = {}, []
    dev.flow, dev.last_launch = {}, None  # flows' per-run state: every run starts clean
    dev.ui_dump_count, dev.ui_dump_seconds, dev.ui_dumps_by_helper = 0, 0.0, {}
    steps = [s for s in sc.steps if dev.platform in s["platforms"]]
    status, failed_step, reason = "PASS", None, ""
    results = {}
    started = time.time()
    plan = ([("pre", {"title": "preconditions", "fn": sc.pre_fn, "covers": ()})] if sc.pre_fn else []) + \
        [(str(i), s) for i, s in enumerate(steps, 1) if dev.platform not in s.get("off", ())]
    for label, s in plan:
        d = Driver(dev, top, run_dir / dev.alias, ledger)
        since = dev.log.stat().st_size if dev.log.exists() else 0
        t0, wda0 = time.time(), wda_restarts(dev)
        try:
            s["fn"](d)
            if not d.stopped_on_purpose:
                dev.crash_check(since)
            lines.append("  ✓ %s %s (%s)%s" % (label, s["title"], dur(time.time() - t0),
                                                " · " + "; ".join(d.notes) if d.notes and verbose else ""))
            for c in s["covers"]:
                if c in d.unverifiable:
                    results[c] = "N/A on %s (%s)" % (dev.platform, d.unverifiable[c])
                else:
                    results.setdefault(c, "PASS")
            s["ran"] = True
        except Exception as e:
            if isinstance(e, Blocked):
                kind = "BLOCKED"
            elif isinstance(e, (Fail, AssertionError)):
                kind = "FAIL"
                front = dev.foreground() if hasattr(dev, "foreground") else None
                if hasattr(dev, "reachable") and not dev.reachable():
                    # A phone that dropped off USB mid-step explains any missing log or state: not the app's fault.
                    kind = "BLOCKED"
                    e = Blocked("%s disconnected during the step, so this failure cannot be trusted: %s" % (dev.alias, e))
                elif front and front != dev.app and not d.stopped_on_purpose:
                    # Another app in front (a notification banner took a tap, a system prompt): not the app's fault.
                    kind = "BLOCKED"
                    e = Blocked("%s is in front of the app on %s (a notification or system prompt took over), so this "
                                "failure cannot be trusted: %s" % (front, dev.alias, e))
                elif wda_restarts(dev) > wda0:
                    # A new WDA session kills and relaunches the app.
                    kind = "BLOCKED"
                    e = Blocked("WebDriverAgent restarted during the step and its new session relaunched the app, so "
                                "this failure cannot be trusted: %s" % e)
            elif isinstance(e, NotImplementedError):  # a step `devtest new` wrote and nobody filled in
                kind = "SCENARIO ERROR"
                e = ScenarioError("step not written yet: %s" % e)
            else:
                kind = "BLOCKED"
                e = Blocked("scenario code raised %s" % short(traceback.format_exc().splitlines()[-1], 300))
            evidence = d.collect("step-%s" % label, since) if kind == "FAIL" else ""
            lines.append("  ✗ %s %s (%s) — %s: %s" % (label, s["title"], dur(time.time() - t0), kind, short(str(e), 700)))
            if evidence:
                lines.append("      evidence: %s" % evidence)
            for c in s["covers"]:
                results[c] = kind
            s["ran"] = True
            status, failed_step, reason = kind, label, short(str(e), 200)
            break
    if sc.post_fn:
        d = Driver(dev, top, run_dir / dev.alias, {})
        try:
            sc.post_fn(d)
        except Exception as e:
            lines.append("  ! post (restore) failed: %s" % short(str(e), 300))
    for _, s in plan:
        # A criterion passes only when every step covering it ran and passed.
        ran = s.pop("ran", False)
        for c in s["covers"]:
            if not ran and results.get(c) in (None, "PASS"):
                results[c] = "NOT RUN"
    applicable = {c for s in steps for c in s["covers"]}
    for s in sc.steps:
        for c in s["covers"]:
            if c not in applicable:
                results[c] = "N/A on %s (step for %s only)" % (dev.platform, "/".join(s["platforms"]))
    selected = {c for _, s in plan for c in s["covers"]}
    for i, s in enumerate(steps, 1):
        if dev.platform not in s.get("off", ()):
            continue
        for c in s["covers"]:
            if c not in selected:
                results[c] = "SKIP (not selected: --only/--skip)"
            elif results.get(c) == "PASS":
                results[c] = "PARTIAL (step %d covering it not selected)" % i
    if status == "PASS":
        vacuous = [t for (k, t), seen in ledger.items() if seen == {False}]
        if vacuous:
            status, reason = "SCENARIO ERROR", "absence asserted but never seen present in this run: %s" % ", ".join(vacuous)
            lines.append("  ✗ %s" % reason)
    for c, why in sc.skipped.items():
        results[c] = "SKIP (%s)" % why
    metrics = {"wall_seconds": round(time.time() - started, 3), "uiautomator_dumps": dev.ui_dump_count,
               "uiautomator_dump_seconds": round(dev.ui_dump_seconds, 1),
               "uiautomator_dumps_by_helper": {k: {"dumps": n, "seconds": round(s, 1)} for k, (n, s) in
                                               sorted(dev.ui_dumps_by_helper.items(), key=lambda kv: -kv[1][1])},
               "baa_navigation": dev.flow.get("baa_navigation", {}),
               "ui_navigation_forced": os.environ.get("DEVTEST_UI_NAV") == "1"}
    evidence = run_dir / dev.alias
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    nav_baa = metrics["baa_navigation"]
    lines.append("  metrics: %.1fs · %d UI dumps (%.0fs) · baa navigation %d/%d · %d baa→ui fallbacks" %
                 (metrics["wall_seconds"], metrics["uiautomator_dumps"], metrics["uiautomator_dump_seconds"],
                  nav_baa.get("successes", 0), nav_baa.get("calls", 0), len(nav_baa.get("fallbacks", []))))
    if metrics["uiautomator_dumps_by_helper"]:
        lines.append("  dumps by helper: " + " · ".join("%s %d (%.0fs)" % (k, v["dumps"], v["seconds"]) for k, v in
                                                       list(metrics["uiautomator_dumps_by_helper"].items())[:6]))
    return {"status": status, "failed_step": failed_step, "reason": reason, "lines": lines, "criteria": results,
            "steps": len(plan), "duration": time.time() - started}


def criteria_line(sc: Scenario, results: dict) -> str:
    return "criteria: " + " · ".join("%s %s" % (c, results.get(c, "NOT RUN")) for c in sc.criteria)


LEDGER = RUNS.parent / "ledger.jsonl"


def build_id(top: Path) -> str:
    """The worktree's HEAD, +dirty with uncommitted tracked changes: what `run` checked the installed build against."""
    archive = source_archive(top)
    if archive:
        return archive["commit"][:10]
    def git(*a):
        return subprocess.run(["git", "-C", str(top), *a], capture_output=True, text=True, timeout=20).stdout.strip()
    sha = git("rev-parse", "--short=10", "HEAD") or "unknown"
    return sha + ("+dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def record(entry: dict) -> None:
    """One line per scenario × phone verdict in the state folder's ledger.jsonl (devtest suite report reads it)."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")


def preflight_tags(top: Path, path: Path, strict: bool) -> None:
    """The selectors of flows and of the scenario exist in the worktree: SCENARIO ERROR naming the missing ones
    (a warning only when strict is False, e.g. a red proof on a base build that predates a tag)."""
    from devtest_guard import check_tags
    errors, warnings, _ = check_tags(top, path)
    for w in warnings:
        print("· warning: %s" % w)
    if errors and strict:
        raise ScenarioError("selectors missing from the worktree (renamed in the app?): %s" % "; ".join(errors))
    for e in errors:
        print("· warning: %s" % e)


def uses_pair(path: Path) -> bool:
    """A two-phone scenario drives its colleague itself, so its phones never run it side by side."""
    return re.search(r"\bpair\.(colleague|together)\(", path.read_text()) is not None


def run_side_by_side(argvs: dict, out_dir: Path = None) -> int:
    """One child `devtest run` per phone ({alias: argv}), all started at once; each child's output is printed whole,
    phone by phone, when it ends. Exit code: the worst child's."""
    out_dir = out_dir or RUNS / ("%s-side-by-side" % time.strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    procs = {}
    try:
        for alias, argv in argvs.items():
            with open(out_dir / ("%s.out" % alias), "w") as f:
                procs[alias] = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT)
        worst = 0
        for alias, proc in procs.items():
            code = proc.wait()
            print((out_dir / ("%s.out" % alias)).read_text(), end="", flush=True)
            worst = max(worst, code if code >= 0 else 2)
        return worst
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()


def run_scenario(path: Path, devices: list, top: Path, repeat: int, expect_fail, build_check: bool, verbose: bool,
                 only=(), skip=(), tags_check: bool = True) -> int:
    sc = load(path)
    sc.validate()
    if only or skip:
        sc.select(only, skip)
    if tags_check:
        preflight_tags(top, path, strict=expect_fail is None)
    run_dir = RUNS / ("%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), path.stem))
    build = build_id(top)
    # The scenario's version: a verdict from before a fix to the scenario is superseded in `suite report`.
    scenario_sha = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    worst = 0

    def ledger(dev, verdict, outcomes=(), **extra):
        last = outcomes[-1] if outcomes else {}
        record({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "scenario": path.stem, "phone": dev.alias,
                "scenario_sha": scenario_sha,
                "platform": dev.platform, "build": build, "worktree": str(top), "verdict": verdict,
                "criteria": last.get("criteria", {}), "runs": len(outcomes),
                "passed": sum(1 for r in outcomes if r["status"] == "PASS"),
                "duration": round(sum(r["duration"] for r in outcomes)), "failed_step": last.get("failed_step"),
                "reason": last.get("reason", ""), "evidence": str(run_dir), "only": sorted(only), "skip": sorted(skip),
                **extra})

    for dev in devices:
        if dev.platform not in sc.platforms:
            print("· %s: scenario not meant for %s" % (dev.alias, dev.platform))
            continue
        head = "scenario %s · %s (%s)" % (sc.title, dev.alias, dev.platform)
        if build_check:
            try:
                head += " · " + dev.build_info(top)
            except Blocked as e:
                print(head)
                print("VERDICT %s BLOCKED · %s" % (dev.alias, e))
                ledger(dev, "BLOCKED", reason=short(str(e), 200))
                worst = max(worst, 2)
                continue
        print(head, flush=True)
        outcomes = []
        for i in range(1, repeat + 1):
            res = run_once(sc, dev, top, run_dir / ("run%d" % i if repeat > 1 else ""), verbose)
            outcomes.append(res)
            if i == 1 or res["status"] != "PASS":
                if repeat > 1:
                    print(" run %d/%d" % (i, repeat))
                print("\n".join(res["lines"]), flush=True)
            elif repeat > 1:
                print(" run %d/%d PASS (%s)" % (i, repeat, dur(res["duration"])), flush=True)
        last = outcomes[-1]
        passed = sum(1 for r in outcomes if r["status"] == "PASS")
        if expect_fail is not None:
            hit = [r for r in outcomes if r["status"] == "FAIL" and r["failed_step"] == str(expect_fail)]
            if len(hit) == len(outcomes):
                print("VERDICT %s RED OK · fails at step %s as expected: the scenario catches the bug" % (dev.alias, expect_fail))
                ledger(dev, "RED OK", outcomes)
            else:
                where = ", ".join("%s%s" % (r["status"], " at %s" % r["failed_step"] if r["failed_step"] else "") for r in outcomes)
                print("VERDICT %s RED MISSING · wanted FAIL at step %s, got %s: the scenario would not catch the bug"
                      % (dev.alias, expect_fail, where))
                ledger(dev, "RED MISSING", outcomes)
                worst = max(worst, 1)
            continue
        if repeat > 1 and passed < repeat:
            agg = {}
            for r in outcomes:
                for c, v in r["criteria"].items():
                    agg.setdefault(c, []).append(v.split(" (")[0])
            line = "criteria over %d runs: " % repeat + " · ".join(
                "%s %s" % (c, " ".join("%s %d" % (s, n) for s, n in sorted({s: agg[c].count(s) for s in agg[c]}.items())))
                for c in sc.criteria)
            app_failures = [r for r in outcomes if r["status"] in ("FAIL", "SCENARIO ERROR")]
            if app_failures:
                first = app_failures[0]
                print("VERDICT %s %s %d/%d runs passed · first failure step %s: %s · %s" % (
                    dev.alias, "FLAKY" if passed else "FAIL", passed, repeat, first["failed_step"], first["reason"], line))
                ledger(dev, "FLAKY" if passed else "FAIL", outcomes, runs_criteria=agg)
                worst = max(worst, 1)
            else:
                first = next(r for r in outcomes if r["status"] == "BLOCKED")
                print("VERDICT %s BLOCKED · %d/%d runs passed, the others stopped on the device or tooling, not the app: "
                      "step %s: %s · %s" % (dev.alias, passed, repeat, first["failed_step"], first["reason"], line))
                ledger(dev, "BLOCKED", outcomes, runs_criteria=agg)
                worst = max(worst, 2)
            continue
        status = last["status"]
        summary = ("%d steps" % last["steps"] if status == "PASS" else last["reason"] if not last["failed_step"]
                   else "step %s: %s" % (last["failed_step"], last["reason"]))
        print("VERDICT %s %s%s · %s · %s (%s)" % (dev.alias, status, " %d/%d runs" % (passed, repeat) if repeat > 1 else "",
                                               summary, criteria_line(sc, last["criteria"]), dur(last["duration"])))
        ledger(dev, status, outcomes)
        worst = max(worst, {"PASS": 0, "FAIL": 1, "BLOCKED": 2, "SCENARIO ERROR": 3}[status])
    return worst
