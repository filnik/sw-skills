#!/usr/bin/env python3
"""The device-test runner without a phone or a pack. Run: python3 tests/test_devtest_run.py (exit 0 = green)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DEVTEST_HOME"] = tempfile.mkdtemp()
os.environ["DEVTEST_PACK"] = "none"
os.environ["DEVTEST_WORKTREE"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "device-test" / "scripts"))

import devtest_android as A  # noqa: E402
import devtest_plan as P  # noqa: E402
import devtest_scenario as S  # noqa: E402


class FakePhone:
    app = "com.example.app"

    def __init__(self, platform="android"):
        self.alias, self.platform = "fake", platform
        self.log = Path(tempfile.mkdtemp()) / "app.log"
        self.log.write_text("")
        self.on_screen = {"screen.home"}
        self.front, self.online = self.app, True

    def say(self, line):
        with open(self.log, "a") as f:
            f.write(line + "\n")

    def screens(self, nodes=None):
        return sorted(s for s in self.on_screen if s.startswith("screen."))

    def visible(self, sel, timeout=0):
        return sel in self.on_screen

    def node(self, sel, timeout=0):
        return None

    def launch(self, env=None, wait_screen=None, restart=False, timeout=90):
        return "launched"

    def crash_check(self, since):
        pass

    def ensure_capture(self):
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


def run(path, phone=None, expect_fail=None, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = S.run_scenario(path, [phone or FakePhone()], Path(tempfile.mkdtemp()), 1, expect_fail, False, False,
                              tags_check=False, **kw)
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


def test_verdict_and_criteria_and_evidence_on_fail():
    code, out = run(scenario(VERDICTS))
    assert code == 1, out
    verdict = next(l for l in out.splitlines() if l.startswith("VERDICT"))
    assert "VERDICT fake FAIL" in verdict and "step 3" in verdict, verdict
    assert "AC1 PASS" in verdict and "AC2 N/A on android (debug-only line)" in verdict, verdict
    assert "AC3 SKIP (needs hardware)" in verdict and "AC4 FAIL" in verdict, verdict
    evidence = next(l for l in out.splitlines() if "evidence:" in l).split("evidence: ")[1]
    folder = Path(evidence.split("/step-3")[0])
    assert {"step-3.log", "step-3.png", "step-3.ui.txt"} <= {p.name for p in folder.iterdir()}, evidence
    entry = json.loads(S.LEDGER.read_text().splitlines()[-1])
    assert (entry["verdict"], entry["failed_step"], entry["criteria"]["AC1"]) == ("FAIL", "3", "PASS")
    assert (entry["scenario"], entry["phone"], entry["platform"]) == ("demo", "fake", "android")


def test_absence_needs_presence():
    lonely = 'S = Scenario("x")\n@S.step("no popup")\ndef _(d):\n    d.expect_absent("popup", settle=0)\n'
    code, out = run(scenario(lonely))
    assert code == 3 and "never seen present" in out, out
    paired = '''S = Scenario("x")
@S.step("no error yet")
def _(d):
    m = d.mark()
    d.dev.say("I/app: alive")
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
    assert [bool(s["off"]) for s in sc.steps] == [True, False, False]  # a step that covers nothing stays
    sc.select(skip=["3"])
    assert [bool(s["off"]) for s in sc.steps] == [True, False, True]


def test_scaffold_from_plan_is_an_error_until_written():
    plan = """Ticket: SHOP-12 · Cart keeps items
Scenario: new SHOP-12.py
Setup: logged in, empty cart
Checks (one per line, keep the IDs):
  AC1 add two items and restart → the cart shows 2 (tag cart.count)
  AC2 remove one -> log "cart saved"
Not to test: payment (no test card)
"""
    parsed = P.parse_plan(plan)
    assert parsed["out"] == "SHOP-12.py" and parsed["title"] == "SHOP-12 Cart keeps items"
    assert parsed["checks"] == [("AC1", "add two items and restart", "the cart shows 2 (tag cart.count)"),
                                ("AC2", "remove one", 'log "cart saved"')]
    out, source = P.scaffold(plan)
    path = Path(tempfile.mkdtemp()) / "SHOP-12.py"
    path.write_text(source)
    S.load(path).validate()  # every check is covered by its step
    code, text = run(path)
    assert code == 3 and "step not written yet: AC1" in text, text


def test_dump_caller_names_the_outer_and_inner_flow_and_else_the_driver_call():
    ns, flows = {}, os.path.join(tempfile.mkdtemp(), "flows")
    exec(compile("def open_item(f):\n    return f()\n", os.path.join(flows, "nav.py"), "exec"), ns)
    exec(compile("def create(f):\n    return open_item(f)\n", os.path.join(flows, "items.py"), "exec"), ns)
    exec(compile("class Driver:\n    def tap(self, f):\n        return f()\n", "/x/devtest_scenario.py", "exec"), ns)
    assert ns["create"](A.dump_caller) == "items.create > nav.open_item"
    assert ns["open_item"](lambda: ns["Driver"]().tap(A.dump_caller)) == "nav.open_item"
    assert ns["Driver"]().tap(A.dump_caller) == "x.tap"
    assert A.dump_caller() == "runner"


def test_side_by_side_runs_at_once_and_the_worst_exit_wins():
    def child(code):
        return [sys.executable, "-c", "import time; time.sleep(1); print('done'); raise SystemExit(%d)" % code]
    started = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        worst = S.run_side_by_side({"a": child(0), "b": child(2)}, Path(tempfile.mkdtemp()))
    assert time.time() - started < 1.8
    assert worst == 2


def test_a_two_phone_scenario_is_not_split():
    folder = Path(tempfile.mkdtemp())
    (folder / "pair.py").write_text("other = pair.colleague(d)\n")
    (folder / "single.py").write_text("d.tap('settings.reminders.switch')\n")
    assert S.uses_pair(folder / "pair.py") and not S.uses_pair(folder / "single.py")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
