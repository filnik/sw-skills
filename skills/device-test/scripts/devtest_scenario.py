"""Scenarios as code: one line per step, fail fast with evidence, one verdict line.

    from devtest_scenario import Scenario
    S = Scenario("TICKET-12 short title", criteria={"AC1": "…", "AC2": "…"})

    @S.pre
    def setup(d):
        d.launch(restart=True, wait_screen="screen.home")

    @S.step("Saving the form logs the event and stores the name", covers=["AC1"])
    def _(d):
        m = d.mark()
        d.tap("form.save")
        d.expect_log(r"form saved", since=m)
        d.expect_pref("display_name", equals="Ann")

    S.skip("AC2", "needs a second account")

Rules the runner enforces, so a scenario cannot pass without looking:
  - log expectations read only the lines written after their mark;
  - every expect_absent / expect_no_log / expect_pref(absent or lacks) / expect_file(exists=False) needs the same
    target asserted present somewhere in the run, otherwise the run ends as SCENARIO ERROR (expect_no_log can name
    an observed positive-control pattern instead);
  - after every step the app must still be running (unless the step stopped it on purpose);
  - every criterion is covered by a step or skipped with a reason; a step left as NotImplementedError is an error;
  - there is no sleep: wait on a condition with d.wait(...).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

from devtest_common import (RUNS, STATE, Blocked, Fail, decode_prefs, encode_pref, read_since, short, sqlite_query,
                            wait_log)

LEDGER = STATE / "ledger.jsonl"


class ScenarioError(Exception):
    """The scenario itself is wrong (uncovered criterion, absence never paired with presence, unwritten step)."""


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
        """Runs first: brings the phone and the app to the state the steps need, whatever the last run left."""
        self.pre_fn = fn
        return fn

    def post(self, fn):
        """Always runs after the steps, pass or fail: restores the phone. Its errors are reported, not judged."""
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
        """Keeps the steps covering a criterion in `only` (or whose number is in it) and drops those covering one in
        `skip`; numbers count the platform's steps as the run prints them. A step that covers nothing is kept unless
        its number is skipped. Later steps may need what a dropped one leaves."""
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
    """The `d` handed to every step: actions plus expectations on one phone."""

    def __init__(self, dev, top: Path, evidence: Path, ledger: dict):
        self.dev, self.top, self.evidence, self.ledger = dev, top, evidence, ledger
        self.platform = dev.platform
        self.stopped_on_purpose = False
        self.notes = []
        self.unverifiable = {}

    def unverified(self, criterion: str, reason: str) -> None:
        """This criterion cannot be observed on this platform: reported as N/A with the reason, never as PASS."""
        self.unverifiable[criterion] = reason

    def _seen(self, kind: str, target: str, present: bool) -> None:
        self.ledger.setdefault((kind, target), set()).add(present)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def mark(self) -> Mark:
        log = self.dev.log
        return Mark(log.stat().st_size if log.exists() else 0, time.time())

    def wait(self, cond, timeout: float, label: str, poll: float = 1.0):
        deadline = time.time() + timeout
        while True:
            last = cond()
            if last:
                return last
            if time.time() >= deadline:
                raise Fail("%s: not true within %ds" % (label, timeout))
            time.sleep(poll)

    # ------------------------------------------------------------------------------------ actions

    def launch(self, wait_screen=None, restart: bool = False, timeout: float = 90) -> None:
        self.stopped_on_purpose = False
        self.note(self.dev.launch(wait_screen=wait_screen, restart=restart, timeout=timeout))

    def restart(self, wait_screen=None) -> None:
        self.launch(wait_screen=wait_screen, restart=True)

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
        """Android only (iOS: BLOCKED). Refused while adb itself runs over Wi-Fi."""
        self.note(self.dev.wifi(on))

    def airplane(self, on: bool, confirm=None) -> None:
        """iOS: a person toggles it on request; `confirm` is the regex of the app's connectivity log line."""
        self.note(self.dev.airplane(on, confirm))

    def isolate(self, on: bool) -> None:
        """Android 13+: the app alone cut off the network, the phone still online."""
        self.note(self.dev.isolate(on))

    def set_pref(self, key: str, value) -> None:
        """Writes one prefs key with the app stopped (value None removes it); relaunch afterwards."""
        self.stop()
        value = None if value is None else (value if isinstance(value, str) else json.dumps(value))
        self.dev.write_prefs(encode_pref(self.dev.prefs_bytes(), key, value))
        got = decode_prefs(self.dev.prefs_bytes()).get(key)
        if got != value:
            raise Blocked("prefs write of %s did not stick: read back %r" % (key, got))

    def put_media(self, relative: str, local=None) -> str:
        """Pushes a local file into the app's files folder (a tiny placeholder JPEG when local is omitted)."""
        if local is not None:
            local = Path(local)
            if not local.is_file():
                raise Blocked("media source missing: %s" % local)
            return self.dev.put_file(relative, local)
        local = self.evidence.parent / ("seed-%s" % relative.replace("/", "_"))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"\xff\xd8\xff\xe0devtest seed\xff\xd9")
        return self.dev.put_file(relative, local)

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
            return n is not None and ((checked is None or n.checked == checked)
                                      and (selected is None or n.selected == selected)
                                      and (text is None or text in (n.text, n.desc, n.subtree_text())))
        try:
            self.wait(ok, timeout, sel, poll=0.8)
        except Fail:
            n = self.dev.node(sel)
            got = "missing" if n is None else "checked=%s selected=%s text=%r" % (n.checked, n.selected, n.text)
            want = ", ".join("%s=%r" % kv for kv in (("checked", checked), ("selected", selected), ("text", text))
                             if kv[1] is not None)
            raise Fail("%s: wanted %s, got %s" % (sel, want, got))
        self._seen("ui", sel, True)

    def _capture_silent(self, since: Mark, what: str) -> None:
        """BLOCKED when the log capture recorded no line at all for 20 s or more after the mark: the capture is down,
        so a missing or absent line proves nothing about the app."""
        quiet = time.time() - since.at
        if quiet >= 20 and not read_since(self.dev.log, since.offset):
            raise Blocked("%s: the log capture recorded no line in the %ds since the mark, so it is down; not judged "
                          "against the app" % (what, quiet))

    def expect_log(self, pattern: str, since: Mark, timeout: float = 15) -> str:
        line = wait_log(self.dev.log, since.offset, pattern, timeout)
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

    def expect_no_log(self, pattern: str, since: Mark, within: float = 5, observed=None) -> None:
        """No matching line in this window. `observed` is a positive control seen in the SAME window (asserted with
        expect_log earlier), for an event that must never occur in the run."""
        line = wait_log(self.dev.log, since.offset, pattern, within)
        if line:
            raise Fail("unexpected log /%s/: %s" % (pattern, short(line, 300)))
        self._capture_silent(since, "absence of /%s/" % short(pattern, 80))
        if observed is None:
            self._seen("log", pattern, False)
            return
        if True not in self.ledger.get(("log", observed), set()):
            raise ScenarioError("expect_no_log observed= needs a preceding expect_log of /%s/" % observed)
        if not any(re.search(observed, l) for l in read_since(self.dev.log, since.offset)):
            raise ScenarioError("positive control /%s/ absent from the forbidden-log window" % observed)

    def prefs(self) -> dict:
        return decode_prefs(self.dev.prefs_bytes())

    def expect_pref(self, key: str, equals=None, contains=None, lacks=None, absent: bool = False,
                    timeout: float = 10) -> None:
        """`lacks` passes when the key is missing or does not contain the text; like `absent`, it needs a
        `contains`/`equals` on the same key somewhere in the run."""
        def check():
            v = self.prefs().get(key)
            if absent:
                return v is None
            if lacks is not None:
                return v is None or lacks not in str(v)
            return v is not None and (equals is None or v == equals) and (contains is None or contains in str(v))
        try:
            self.wait(check, timeout, key, poll=1.5)
        except Fail:
            want = ("absent" if absent else "without %r" % lacks if lacks is not None else
                    "== %r" % equals if equals is not None else "containing %r" % contains)
            raise Fail("pref %s = %r, wanted %s" % (key, self.prefs().get(key), want))
        self._seen("pref", key, not absent and lacks is None)

    def query(self, sql: str, limit: int = 40) -> list:
        for attempt in (1, 2, 3):
            try:
                return sqlite_query(self.dev.pull_db(), sql, limit=limit)
            except Blocked as e:
                # A copy torn by the app's own writes: a new copy is consistent again.
                if attempt == 3 or ("malformed" not in str(e) and "not a database" not in str(e)):
                    raise
                time.sleep(2)

    def expect_db(self, sql: str, equals=None, contains=None) -> None:
        body = "\n".join(self.query(sql)[1:])
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
        """Screenshot, UI tree and the log since the step began: saved only for a FAIL."""
        self.evidence.mkdir(parents=True, exist_ok=True)
        saved, missing = [], []
        try:
            self.dev.shot(self.evidence / ("%s.png" % name))
            saved.append("png")
        except Exception as e:  # noqa: BLE001 - evidence must never hide the failure itself
            missing.append("no screenshot: %s" % short(str(e), 80))
        try:
            (self.evidence / ("%s.ui.txt" % name)).write_text("\n".join(self.dev.src()))
            saved.append("ui.txt")
        except Exception as e:  # noqa: BLE001
            missing.append("no ui tree: %s" % short(str(e), 80))
        (self.evidence / ("%s.log" % name)).write_text("\n".join(read_since(self.dev.log, since)[-400:]))
        saved.append("log")
        return "%s/%s.{%s}%s" % (self.evidence, name, ",".join(saved), " (%s)" % "; ".join(missing) if missing else "")


def load(path: Path) -> Scenario:
    """Imports the scenario file; its folder goes on sys.path, so `from flows import …` finds its shared flows."""
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("scenario_%s" % re.sub(r"\W", "_", path.stem), str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found = [v for v in vars(mod).values() if isinstance(v, Scenario)]
    if len(found) != 1:
        raise ScenarioError("%s must define exactly one Scenario" % path)
    return found[0]


def dur(s: float) -> str:
    return "%.1fs" % s if s < 60 else "%dm%02ds" % (s // 60, s % 60)


def _judge(e: Exception, dev, d: Driver, wda0: int) -> tuple:
    """(kind, exception): a FAIL turns BLOCKED when the phone dropped off, another app took the front, or WDA
    restarted (its new session relaunches the app); a step not written yet is a SCENARIO ERROR."""
    if isinstance(e, NotImplementedError):
        return "SCENARIO ERROR", ScenarioError("step not written yet: %s" % short(str(e), 200))
    if isinstance(e, ScenarioError):
        return "SCENARIO ERROR", e
    if isinstance(e, Blocked):
        return "BLOCKED", e
    if not isinstance(e, (Fail, AssertionError)):
        return "BLOCKED", Blocked("scenario code raised %s" % short(traceback.format_exc().splitlines()[-1], 300))
    if hasattr(dev, "reachable") and not dev.reachable():
        return "BLOCKED", Blocked("%s disconnected during the step, so this failure cannot be trusted: %s" % (
            dev.alias, e))
    front = dev.foreground() if hasattr(dev, "foreground") else None
    if front and front != dev.app_id and not d.stopped_on_purpose:
        return "BLOCKED", Blocked("%s is in front of the app on %s (a notification or system prompt took over), so "
                                  "this failure cannot be trusted: %s" % (front, dev.alias, e))
    if getattr(dev, "wda_restarts", 0) > wda0:
        return "BLOCKED", Blocked("WebDriverAgent restarted during the step and relaunched the app, so this failure "
                                  "cannot be trusted: %s" % e)
    return "FAIL", e


def run_once(sc: Scenario, dev, top: Path, run_dir: Path, verbose: bool) -> dict:
    """Runs pre + steps on one phone. Returns {status, failed_step, reason, lines, criteria, steps, duration}."""
    ledger, lines, results = {}, [], {}
    steps = [s for s in sc.steps if dev.platform in s["platforms"]]
    status, failed_step, reason = "PASS", None, ""
    started = time.time()
    plan = ([("pre", {"title": "preconditions", "fn": sc.pre_fn, "covers": ()})] if sc.pre_fn else []) + \
        [(str(i), s) for i, s in enumerate(steps, 1) if dev.platform not in s.get("off", ())]
    ran = set()
    for label, s in plan:
        d = Driver(dev, top, run_dir / dev.alias, ledger)
        since = dev.log.stat().st_size if dev.log.exists() else 0
        t0, wda0 = time.time(), getattr(dev, "wda_restarts", 0)
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
            ran.add(label)
        except Exception as e:  # noqa: BLE001 - every failure becomes a verdict
            kind, e = _judge(e, dev, d, wda0)
            evidence = d.collect("step-%s" % label, since) if kind == "FAIL" else ""
            lines.append("  ✗ %s %s (%s) — %s: %s" % (label, s["title"], dur(time.time() - t0), kind, short(str(e), 700)))
            if evidence:
                lines.append("      evidence: %s" % evidence)
            for c in s["covers"]:
                results[c] = kind
            ran.add(label)
            status, failed_step, reason = kind, label, short(str(e), 200)
            break
    if sc.post_fn:
        try:
            sc.post_fn(Driver(dev, top, run_dir / dev.alias, {}))
        except Exception as e:  # noqa: BLE001
            lines.append("  ! post (restore) failed: %s" % short(str(e), 300))
    for label, s in plan:  # a criterion passes only when every step covering it ran and passed
        for c in s["covers"]:
            if label not in ran and results.get(c) in (None, "PASS"):
                results[c] = "NOT RUN"
    applicable = {c for s in steps for c in s["covers"]}
    for s in sc.steps:
        for c in s["covers"]:
            if c not in applicable:
                results[c] = "N/A on %s (step for %s only)" % (dev.platform, "/".join(s["platforms"]))
    selected = {c for _, s in plan for c in s["covers"]}
    for i, s in enumerate(steps, 1):
        if dev.platform in s.get("off", ()):
            for c in s["covers"]:
                if c not in selected:
                    results[c] = "SKIP (not selected: --only/--skip)"
                elif results.get(c) == "PASS":
                    results[c] = "PARTIAL (step %d covering it not selected)" % i
    if status == "PASS":
        vacuous = sorted(t for (k, t), seen in ledger.items() if seen == {False})
        if vacuous:
            status, reason = "SCENARIO ERROR", "absence asserted but never seen present in this run: %s" % ", ".join(vacuous)
            lines.append("  ✗ %s" % reason)
    for c, why in sc.skipped.items():
        results[c] = "SKIP (%s)" % why
    return {"status": status, "failed_step": failed_step, "reason": reason, "lines": lines, "criteria": results,
            "steps": len(plan), "duration": time.time() - started}


def criteria_line(sc: Scenario, results: dict) -> str:
    return "criteria: " + " · ".join("%s %s" % (c, results.get(c, "NOT RUN")) for c in sc.criteria)


def build_id(top: Path) -> str:
    """The worktree's HEAD, +dirty with uncommitted tracked changes."""
    def git(*a):
        return subprocess.run(["git", "-C", str(top)] + list(a), capture_output=True, text=True, timeout=20).stdout.strip()
    return (git("rev-parse", "--short=10", "HEAD") or "unknown") + (
        "+dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def record(entry: dict) -> None:
    """One line per scenario × phone verdict in the ledger (`report` reads it)."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_side_by_side(argvs: dict, out_dir=None) -> int:
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


EXIT = {"PASS": 0, "FAIL": 1, "BLOCKED": 2, "SCENARIO ERROR": 3}


def run_scenario(path: Path, devices: list, top: Path, repeat: int = 1, expect_fail=None, build_check: bool = True,
                 verbose: bool = False, only=(), skip=(), tags_check: bool = True) -> int:
    sc = load(path)
    sc.validate()
    if only or skip:
        sc.select(only, skip)
    if tags_check:
        from devtest_guard import preflight_tags
        preflight_tags(top, path, strict=expect_fail is None)
    run_dir = RUNS / ("%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), path.stem))
    build = build_id(top)
    scenario_sha = hashlib.sha1(path.read_bytes()).hexdigest()[:10]  # a fix to the scenario supersedes old verdicts
    worst = 0

    def ledger(dev, verdict, outcomes=(), **extra):
        last = outcomes[-1] if outcomes else {}
        record(dict(extra, at=time.strftime("%Y-%m-%dT%H:%M:%S"), scenario=path.stem, scenario_sha=scenario_sha,
                    phone=dev.alias, platform=dev.platform, build=build, worktree=str(top), verdict=verdict,
                    criteria=last.get("criteria", {}), runs=len(outcomes),
                    passed=sum(1 for r in outcomes if r["status"] == "PASS"),
                    duration=round(sum(r["duration"] for r in outcomes)), failed_step=last.get("failed_step"),
                    reason=extra.get("reason", last.get("reason", "")), evidence=str(run_dir),
                    only=sorted(only), skip=sorted(skip)))

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
            if res["status"] == "SCENARIO ERROR":
                break  # the scenario is wrong: repeating it proves nothing
        last = outcomes[-1]
        passed = sum(1 for r in outcomes if r["status"] == "PASS")
        if expect_fail is not None and last["status"] != "SCENARIO ERROR":
            hit = [r for r in outcomes if r["status"] == "FAIL" and r["failed_step"] == str(expect_fail)]
            if len(hit) == len(outcomes):
                print("VERDICT %s RED OK · fails at step %s as expected: the scenario catches the bug" % (
                    dev.alias, expect_fail))
                ledger(dev, "RED OK", outcomes)
            else:
                where = ", ".join("%s%s" % (r["status"], " at %s" % r["failed_step"] if r["failed_step"] else "")
                                  for r in outcomes)
                print("VERDICT %s RED MISSING · wanted FAIL at step %s, got %s: the scenario would not catch the bug"
                      % (dev.alias, expect_fail, where))
                ledger(dev, "RED MISSING", outcomes)
                worst = max(worst, 1)
            continue
        if repeat > 1 and passed < len(outcomes) and last["status"] != "SCENARIO ERROR":
            agg = {}
            for r in outcomes:
                for c, v in r["criteria"].items():
                    agg.setdefault(c, []).append(v.split(" (")[0])
            line = "criteria over %d runs: " % repeat + " · ".join(
                "%s %s" % (c, " ".join("%s %d" % (s, agg.get(c, []).count(s)) for s in sorted(set(agg.get(c, [])))))
                for c in sc.criteria)
            app = [r for r in outcomes if r["status"] == "FAIL"]
            if app:
                verdict = "FLAKY" if passed else "FAIL"
                print("VERDICT %s %s %d/%d runs passed · first failure step %s: %s · %s" % (
                    dev.alias, verdict, passed, repeat, app[0]["failed_step"], app[0]["reason"], line))
                ledger(dev, verdict, outcomes, runs_criteria=agg)
                worst = max(worst, 1)
            else:
                first = next(r for r in outcomes if r["status"] == "BLOCKED")
                print("VERDICT %s BLOCKED · %d/%d runs passed, the others stopped on the phone or tooling, not the app: "
                      "step %s: %s · %s" % (dev.alias, passed, repeat, first["failed_step"], first["reason"], line))
                ledger(dev, "BLOCKED", outcomes, runs_criteria=agg)
                worst = max(worst, 2)
            continue
        status = last["status"]
        summary = ("%d steps" % last["steps"] if status == "PASS" else last["reason"] if not last["failed_step"]
                   else "step %s: %s" % (last["failed_step"], last["reason"]))
        print("VERDICT %s %s%s · %s · %s (%s)" % (dev.alias, status, " %d/%d runs" % (passed, repeat) if repeat > 1
                                               else "", summary, criteria_line(sc, last["criteria"]),
                                               dur(last["duration"])))
        ledger(dev, status, outcomes)
        worst = max(worst, EXIT[status])
    return worst
