#!/usr/bin/env python3
# Checks quota-route.py's route: Codex up to 70%, then fast or slow; from 80% and over budget to Claude only if
# Claude has margin (week at most 60%, 5 hours under 50%); Codex used up always to Claude. Then the ladder, the
# parallel runs and the route line's wording from the sub_codex config.
import importlib.util, json, os, subprocess, sys, tempfile, time

spec = importlib.util.spec_from_file_location("qr", os.path.join(os.path.dirname(__file__), "quota-route.py"))
qr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qr)

now, fail = 1_800_000_000, 0
for name, codex_used, codex_spare, claude, expected in [
    ("Codex 50%: Codex", 50, -20, {"used": 20.0, "five": 10}, "codex"),
    ("Codex 85% within budget: Codex", 85, 5, {"used": 20.0, "five": 10}, "codex"),
    ("Codex 75% over budget: slow Codex", 75, -10, {"used": 20.0, "five": 10}, "codex"),
    ("Codex 82% over budget and Claude 20%: Claude", 82, -10, {"used": 20.0, "five": 10}, "claude"),
    ("Codex 82% over budget and Claude 5 hours at 55%: slow Codex", 82, -10, {"used": 20.0, "five": 55}, "codex"),
    ("Codex 82% over budget and Claude week at 65%: slow Codex", 82, -10, {"used": 65.0, "five": 10}, "codex"),
    ("Codex 82% over budget and Claude limits unreadable: slow Codex", 82, -10, None, "codex"),
    ("Codex 100%: Claude", 100, -30, {"used": 90.0, "five": 90}, "claude"),
]:
    qr.spare = lambda w, now, s=codex_spare: s
    got, _ = qr.route({"used": float(codex_used), "reset": now + 86400}, claude, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected

# Claude agents' ladder: never Sonnet or Opus below high, never Fable below medium.
floors = {"sonnet": "high", "opus": "high", "fable": "medium"}
order = ["low", "medium", "high", "xhigh", "max"]
for kind, bands in qr.LADDER.items():
    for band, (m, e) in bands.items():
        ok = order.index(e) >= order.index(floors[m])
        print(f"{'ok  ' if ok else 'FAIL'} floor kept: {kind} {band} {m} {e}")
        fail |= not ok
for name, claude_spare, claude, expected in [
    ("Claude 11% and 5 hours 16% and spare 40: ample", 40, {"used": 11.0, "five": 16}, "ample"),
    ("Claude 30% and 5 hours 35%: normal", 40, {"used": 30.0, "five": 35}, "normal"),
    ("Claude 60% and spare 15: normal", 15, {"used": 60.0, "five": 10}, "normal"),
    ("Claude 40% and negative spare: tight", -5, {"used": 40.0, "five": 10}, "tight"),
    ("Claude 80%: tight", 15, {"used": 80.0, "five": 10}, "tight"),
    ("Claude 5 hours at 65%: tight", 40, {"used": 20.0, "five": 65}, "tight"),
    ("Claude limits unreadable: normal", 40, None, "normal"),
]:
    qr.spare = lambda w, now, s=claude_spare: s
    got = qr.claude_band(claude and {**claude, "reset": now + 86400}, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
# How many agents in parallel: Claude 3/2/1 per band; Codex 3 under 70%, 2 above it within budget, 1 over budget.
for name, got, expected in [
    ("Claude ample: 3 in parallel", qr.CLAUDE_PARALLEL["ample"], 3),
    ("Claude normal: 2 in parallel", qr.CLAUDE_PARALLEL["normal"], 2),
    ("Claude tight: 1 at a time", qr.CLAUDE_PARALLEL["tight"], 1),
]:
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
for name, codex_used, codex_spare, expected in [
    ("Codex 40%: 3 runs in parallel", 40, -30, 3),
    ("Codex 75% within budget: 2 runs", 75, 5, 2),
    ("Codex 75% over budget: 1 run", 75, -20, 1),
]:
    qr.spare = lambda w, now, s=codex_spare: s
    got = qr.codex_parallel({"used": float(codex_used), "reset": now + 86400}, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
print(f"{'ok  ' if qr.codex_parallel(None, now) == 2 else 'FAIL'} Codex usage unreadable: 2 runs")
fail |= qr.codex_parallel(None, now) != 2

# The route line names what only Codex has from the config, and says it generically without one.
named = {**qr.DEFAULTS, "codex_only": ["Tracker", "Logs", "Traces"]}
outside = {**qr.DEFAULTS, "folders": ["/no-such-codex-root"], "label": "Work", "skills": "the *-x skills"}
claude = {"used": 20.0, "five": 10, "reset": now + 86400}
qr.spare = lambda w, now: -10
for name, codex_used, cfg, cwd, expected in [
    ("reserve and connectors named", 82, named, "/", " Only Tracker, Logs and Traces runs (--needs-codex) still go to Codex."),
    ("reserve and no connectors named", 82, qr.DEFAULTS, "/", " Only runs that need Codex (--needs-codex) still go to Codex."),
    ("used up and connectors named", 100, named, "/", " Tracker, Logs and Traces cannot run until Codex resets: say so."),
    ("used up and no connectors named", 100, qr.DEFAULTS, "/",
     " Runs that need Codex (--needs-codex) cannot run until Codex resets: say so."),
    ("outside the folders", 20, outside, "/",
     "quota route: claude (not a Work folder: sub-codex and the *-x skills run only there). Do every task with Claude"),
]:
    line = qr.route_line(cwd, {"used": float(codex_used), "reset": now + 86400}, claude, now, cfg)
    print(f"{'ok  ' if expected in line else 'FAIL'} route line: {name}")
    fail |= expected not in line

# The statusline example writes the limits file read_claude reads.
with tempfile.TemporaryDirectory() as home:
    env = {**os.environ, "HOME": home}
    reset = int(time.time()) + 86400
    status = {"model": {"display_name": "Opus"}, "rate_limits": {"five_hour": {"used_percentage": 12.4, "resets_at": reset},
                                                                "seven_day": {"used_percentage": 40, "resets_at": reset}}}
    example = os.path.join(os.path.dirname(__file__), "..", "examples", "statusline-limits.py")
    shown = subprocess.run([sys.executable, example], input=json.dumps(status), env=env, capture_output=True,
                           text=True).stdout.strip()
    saved_home, os.environ["HOME"] = os.environ.get("HOME"), home
    try:
        got = qr.read_claude(time.time())
    finally:
        os.environ["HOME"] = saved_home
    ok = got is not None and got["used"] == 40 and got["five"] == 12.4 and shown == "Opus · 5h 12% · 7d 40%"
    print(f"{'ok  ' if ok else 'FAIL'} statusline example: read_claude reads its file ({got}, {shown!r})")
    fail |= not ok
sys.exit(fail)
