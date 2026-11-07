#!/usr/bin/env python3
"""Scenario engine of skills/device-test with a fake phone: verdicts, evidence, honesty rules, red proof, side by side,
the plan scaffold and the ledger report. No phone needed. Run: python3 tests/test_devtest_run.py (exit 0 = green)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["DEVTEST_HOME"] = tempfile.mkdtemp(prefix="devtest-home-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "device-test" / "scripts"))

import devtest_plan as P  # noqa: E402
import devtest_scenario as S  # noqa: E402


class FakePhone:
    app_id = "com.example.app"

    def __init__(self, platform="android"):
        self.alias, self.platform = "fake", platform
        self.log = Path(tempfile.mkdtemp()) / "app.log"
        self.log.write_text("")
        self.on_screen = {"screen.home"}
        self.front, self.online = self.app_id, True

    def say(self, line):
        with open(self.log, "a") as f:
            f.write(line + "\n")

    def screens(self, nodes=None):
        return sorted(s for s in self.on_screen if s.startswith("screen."))

    def visible(self, sel, timeout=0):
        return sel in self.on_screen

    def node(self, sel, timeout=0):
        return None

    def launch(self, wait_screen=None, restart=False, timeout=90):
        return "launched"

    def crash_check(self, since):
        pass

    def shot(self, path):
        path.write_bytes(b"png")
        return path

    def src(self, flt=None):
        return ["SCREENS: screen.home"]

    def foreground(self):
        return self.front

    def reachable(self):
        return self.online

    def build_info(self, top):
        return "build ok"

    def stop(self):
        pass


def scenario(body):
    path = Path(tempfile.mkdtemp()) / "demo.py"
    path.write_text("from devtest_scenario import Scenario\n" + body)
    return path


def run(path, phone=None, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = S.run_scenario(path, [phone or FakePhone()], Path(tempfile.mkdtemp()), tags_check=False, **kw)
    return code, out.getvalue()


VERDICTS = '''
S = Scenario("DEMO-1 demo", criteria={"AC1": "save logs it", "AC2": "debug only", "AC3": "hardware", "AC4": "button"})

@S.step("save logs it", covers=["AC1"])
def _(d):
    m = d.mark()
    d.dev.say("I/app: saved ok")
    d.expect_log(r"saved ok", since=m, timeout=2)

@S.step("cannot be seen here", covers=["AC2"])
def _(d):
    d.unverified("AC2", "debug-only line")

S.skip("AC3", "needs hardware")

@S.step("the button shows", covers=["AC4"])
def _(d):
    d.expect_visible("form.button", timeout=0)
'''


def test_verdict_criteria_and_evidence_on_fail():
    code, out = run(scenario(VERDICTS))
    assert code == 1, out
    verdict = next(l for l in out.splitlines() if l.startswith("VERDICT"))
    assert "VERDICT fake FAIL" in verdict and "step 3" in verdict, verdict
    assert "AC1 PASS" in verdict and "AC2 N/A on android (debug-only line)" in verdict, verdict
    assert "AC3 SKIP (needs hardware)" in verdict and "AC4 FAIL" in verdict, verdict
    evidence = next(l for l in out.splitlines() if "evidence:" in l).split("evidence: ")[1]
    folder = Path(evidence.split("/step-3")[0])
    assert sorted(p.name for p in folder.iterdir()) == ["step-3.log", "step-3.png", "step-3.ui.txt"], evidence
    entry = json.loads(S.LEDGER.read_text().splitlines()[-1])
    assert (entry["verdict"], entry["failed_step"], entry["criteria"]["AC1"]) == ("FAIL", "3", "PASS")


def test_absence_needs_presence():
    lonely = 'S = Scenario("x")\n@S.step("no popup")\ndef _(d):\n    d.expect_absent("popup", settle=0)\n'
    code, out = run(scenario(lonely))
    assert code == 3 and "never seen present" in out, out
    paired = '''S = Scenario("x")
@S.step("no error yet")
def _(d):
    m = d.mark()
    d.expect_no_log(r"boom", since=m, within=0)
@S.step("then the error")
def _(d):
    m = d.mark()
    d.dev.say("E/app: boom")
    d.expect_log(r"boom", since=m, timeout=1)
'''
    code, out = run(scenario(paired))
    assert code == 0 and "VERDICT fake PASS" in out, out


def test_red_proof():
    red = '''S = Scenario("x", criteria={"AC1": "fixed"})
@S.step("setup")
def _(d):
    pass
@S.step("the bug", covers=["AC1"])
def _(d):
    assert False, "still broken"
'''
    code, out = run(scenario(red), expect_fail="2")
    assert code == 0 and "RED OK" in out, out
    code, out = run(scenario(red), expect_fail="1")
    assert code == 1 and "RED MISSING" in out, out


def test_fail_turns_blocked_when_the_phone_is_at_fault():
    failing = 'S = Scenario("x")\n@S.step("tap")\ndef _(d):\n    d.expect_visible("gone", timeout=0)\n'
    phone = FakePhone()
    phone.online = False
    code, out = run(scenario(failing), phone)
    assert code == 2 and "disconnected during the step" in out, out
    phone = FakePhone()
    phone.front = "com.android.systemui"
    code, out = run(scenario(failing), phone)
    assert code == 2 and "in front of the app" in out, out


def test_validate_and_select():
    sc = S.Scenario("x", criteria={"AC1": "a", "AC2": "b"})
    sc.step("one", covers=["AC1"])(lambda d: None)
    try:
        sc.validate()
        raise AssertionError("AC2 uncovered must be an error")
    except S.ScenarioError as e:
        assert "AC2" in str(e)
    sc.step("two", covers=["AC2"])(lambda d: None)
    sc.step("three")(lambda d: None)
    sc.validate()
    sc.select(only=["AC2"])
    assert [s["off"] for s in sc.steps] == [{"android", "ios"}, set(), set()]


def test_scaffold_from_plan_is_an_error_until_written():
    plan = """Ticket: SHOP-12 · Cart keeps items
Scenario: new device-tests/SHOP-12.py
Setup: logged in, empty cart
Checks (one per line, keep the IDs):
  AC1 add two items and restart → the cart shows 2 (tag cart.count)
  AC2 remove one -> log "cart saved"
Not to test: payment (no test card)
"""
    parsed = P.parse_plan(plan)
    assert parsed["out"] == "device-tests/SHOP-12.py" and parsed["title"] == "SHOP-12 Cart keeps items"
    assert parsed["checks"] == [("AC1", "add two items and restart", "the cart shows 2 (tag cart.count)"),
                                ("AC2", "remove one", 'log "cart saved"')]
    out, source = P.scaffold(plan)
    path = Path(tempfile.mkdtemp()) / "SHOP-12.py"
    path.write_text(source)
    sc = S.load(path)
    sc.validate()  # every check is covered by its step
    code, text = run(path)
    assert code == 3 and "step not written yet: AC1" in text, text


def test_side_by_side_runs_at_once_and_the_worst_exit_wins():
    def child(code):
        return [sys.executable, "-c", "import time; time.sleep(1); print('done'); raise SystemExit(%d)" % code]
    started = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        worst = S.run_side_by_side({"a": child(0), "b": child(2)}, Path(tempfile.mkdtemp()))
    assert time.time() - started < 1.8
    assert worst == 2


def test_report_latest_per_scenario_and_phone():
    entries = [{"scenario": "login", "phone": "pixel", "verdict": "FAIL", "at": "2026-11-01T10:00:00", "build": "aaa",
                "criteria": {"AC1": "FAIL"}},
               {"scenario": "login", "phone": "pixel", "verdict": "PASS", "at": "2026-11-02T10:00:00", "build": "bbb",
                "criteria": {"AC1": "PASS", "AC2": "SKIP (no LiDAR)"}},
               {"scenario": "cart", "phone": "iphone", "verdict": "PASS", "at": "2026-11-01T09:00:00", "build": "aaa",
                "criteria": {}}]
    assert P.report(entries, "bbb") == [
        "cart · iphone · PASS · 2026-11-01 09:00:00 aaa · 0 PASS (older build)",
        "login · pixel · PASS · 2026-11-02 10:00:00 bbb · 1 PASS · AC2 SKIP"]
    assert len(P.report(entries, "bbb", "login")) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
