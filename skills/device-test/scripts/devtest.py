#!/usr/bin/env python3
"""devtest — scripted testing of your app on real phones: one call runs a whole scenario and prints one line per step
and one verdict. Android through adb (no Appium); a physical iPhone through WebDriverAgent over USB.

    devtest [--device ALIAS|SERIAL|UDID[,…]] [--owner NAME] [--worktree DIR] <command> ...

    devices                                 configured and connected phones, their leases and holders
    lease acquire|release|status [--ttl 90m] [--note N] [--force]
    build | install | deploy                build the worktree (device_test.<platform>.build) / install it / both
    build-info                              is the installed build the worktree's latest? (BLOCKED if not)
    wda                                     start WebDriverAgent on the iPhone (launch also starts it)
    launch [--wait-screen SEL] [--restart] | stop | crash-check [--since MARK]
    mark NAME                               remember the current end of the app log
    logs REGEX|crash [--since MARK] [--tail 20] | wait-log REGEX [--since MARK] [--timeout 30]
    prefs [FILTER] | prefs set KEY VALUE | prefs rm KEY      DataStore or plist prefs (set/rm stop the app)
    db "SQL" | file PATH | shot NAME        query a fresh DB copy · does an app file exist · screenshot path
    net wifi on|off | net airplane on|off --confirm REGEX
    ui src [FILTER] | tap SEL | set-text SEL VALUE | wait-screen TAG | visible SEL | back | swipe up|down
       | refresh | hide-keyboard           SEL: test tag, text:Exact, text~part, desc:Exact, desc~part
    run SCENARIO.py [--platform android|ios|both] [--repeat N] [--expect-fail STEP] [--only AC1,3] [--skip AC4]
        [--no-build-check] [--no-tags-check] [-v]       several phones run side by side
    report [SCENARIO]                       latest verdict per scenario and phone, from the ledger
    new PLAN|-                              scenario skeleton from a test plan (a file, or - for stdin)
    tags check SCENARIO.py | lint [SCENARIO.py ...] | flows [DIR]

Config: the "device_test" block of .claude/config/project.json, found from the worktree upward. Only the keys a
command uses are needed; activity, prefs and db are optional.

    "device_test": {
      "android": {"package": "com.example.app", "build": "./gradlew :app:assembleDebug",
                  "apk": "app/build/outputs/apk/debug/app-debug.apk", "activity": ".MainActivity",
                  "prefs": "files/datastore/settings.preferences_pb", "db": "app.db"},
      "ios": {"bundle_id": "com.example.app", "team_id": "ABCDE12345",
              "build": "xcodebuild -scheme App -destination generic/platform=iOS -derivedDataPath build build",
              "app": "build/Build/Products/Debug-iphoneos/App.app",
              "prefs": "Library/Preferences/com.example.app.plist", "db": "Library/Application Support/app.sqlite"},
      "phones": {"pixel": "<adb serial>", "iphone": "<UDID>"}}

Output: one line on success; on failure a few lines plus evidence paths. State: ~/.cache/devtest (leases, logs,
marks, runs/<time>-<scenario>/ evidence, ledger.jsonl).
Exit: 0 ok · 1 FAIL (the app misbehaved) · 2 BLOCKED (phone, tooling or setup) · 3 SCENARIO ERROR.
Pass --owner on every call: shell state does not survive between tool calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from devtest_common import (LEASES, Blocked, Fail, check_lease, current_lease, decode_prefs, default_phone,  # noqa: E402
                            encode_pref, format_prefs, grep_lines, lease_acquire, lease_release, mark_offset, owner,
                            platform_of, project_config, read_since, resolve, say, set_mark, short, sqlite_query,
                            until, wait_log, worktree)


def make_device(name, cfg: dict):
    platform, ident, alias = resolve(name, cfg.get("phones") or {})
    if platform == "android":
        from devtest_android import Android
        return Android(ident, alias, cfg)
    from devtest_ios import IOS
    return IOS(ident, alias, cfg)


def ident_of(dev) -> str:
    return getattr(dev, "serial", None) or dev.udid


def plugged(dev) -> None:
    """A phone that is not connected: ask to plug it in (macOS notification) and wait, see devtest_phones."""
    from devtest_phones import wait_connected
    if wait_connected(ident_of(dev), dev.alias) and dev.platform == "android":
        dev.reconnect()


def cmd_devices(cfg: dict, probe: bool = True) -> None:
    from devtest_phones import connected_sets, is_connected, session_state
    phones = cfg.get("phones") or {}
    serials, udids = connected_sets() if probe else (set(), set())
    known = set(phones.values())
    rows = list(phones.items()) + [("-", s) for s in sorted(serials - known) if ":" not in s]
    rows += [("-", p.stem) for p in sorted(LEASES.glob("*.json")) if p.stem not in known | serials]
    if not rows:
        say("no phones: list them in device_test.phones of .claude/config/project.json, or connect one")
    for alias, ident in rows:
        lease = current_lease(ident)
        if alias == "-" and not lease and ident not in serials:
            continue
        state = ("connected" if is_connected(ident, (serials, udids)) else "not connected") if probe else ""
        held = "free" if not lease else "leased by %s until %s (%s)%s" % (
            lease["owner"], until(lease["expires"]), session_state(lease),
            " · " + lease["note"] if lease.get("note") else "")
        say("%-8s %-8s %-26s %-13s %s" % (alias, platform_of(ident), ident, state, held))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", help="alias from device_test.phones, adb serial or UDID; a comma list for `run`")
    p.add_argument("--worktree", help="the checkout under test (default: git top of the cwd)")
    p.add_argument("--owner", help="who holds the lease; pass it on every call")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("devices", "build", "install", "deploy", "build-info", "stop", "wda"):
        sub.add_parser(name)
    sp = sub.add_parser("lease")
    sp.add_argument("action", choices=["acquire", "release", "status"])
    sp.add_argument("--ttl", default="90m")
    sp.add_argument("--note", default="")
    sp.add_argument("--force", action="store_true")
    sp = sub.add_parser("launch")
    sp.add_argument("--wait-screen")
    sp.add_argument("--restart", action="store_true")
    sp.add_argument("--timeout", type=float, default=90)
    sp = sub.add_parser("crash-check")
    sp.add_argument("--since")
    sp = sub.add_parser("mark")
    sp.add_argument("name")
    sp = sub.add_parser("logs")
    sp.add_argument("pattern")
    sp.add_argument("--since")
    sp.add_argument("--tail", type=int, default=20)
    sp = sub.add_parser("wait-log")
    sp.add_argument("pattern")
    sp.add_argument("--since")
    sp.add_argument("--timeout", type=float, default=30)
    sp = sub.add_parser("prefs")
    sp.add_argument("args", nargs="*")
    sp = sub.add_parser("db")
    sp.add_argument("sql")
    sp = sub.add_parser("file")
    sp.add_argument("path")
    sp = sub.add_parser("shot")
    sp.add_argument("name")
    sp = sub.add_parser("net")
    sp.add_argument("what", choices=["wifi", "airplane"])
    sp.add_argument("state", choices=["on", "off"])
    sp.add_argument("--confirm", help="iOS airplane: regex of the line the app logs when connectivity changes")
    sp = sub.add_parser("ui")
    sp.add_argument("action")
    sp.add_argument("args", nargs="*")
    sp.add_argument("--timeout", type=float, default=10)
    sp = sub.add_parser("run")
    sp.add_argument("scenario")
    sp.add_argument("--platform", choices=["android", "ios", "both"])
    sp.add_argument("--repeat", type=int, default=1)
    sp.add_argument("--expect-fail", help="step number that must FAIL (red proof on the build without the fix)")
    sp.add_argument("--only", default="", help="criteria or step numbers, comma separated")
    sp.add_argument("--skip", default="", help="criteria or step numbers, comma separated")
    sp.add_argument("--no-build-check", action="store_true")
    sp.add_argument("--no-tags-check", action="store_true")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp = sub.add_parser("report")
    sp.add_argument("scenario", nargs="?")
    sp = sub.add_parser("new")
    sp.add_argument("plan", help="a test plan file, or - to read it from stdin")
    sp.add_argument("--out", help="where to write it (default: the plan's 'Scenario: new PATH')")
    sp = sub.add_parser("tags")
    sp.add_argument("action", choices=["check"])
    sp.add_argument("scenario")
    sp = sub.add_parser("lint")
    sp.add_argument("files", nargs="*")
    sp = sub.add_parser("flows")
    sp.add_argument("dir", nargs="?")

    a = p.parse_args()
    if a.owner:
        os.environ["DEVTEST_OWNER"] = a.owner
    try:
        return dispatch(a)
    except Fail as e:
        print("FAIL: %s" % e)
        return 1
    except Blocked as e:
        print("BLOCKED: %s" % e)
        return 2
    except KeyboardInterrupt:
        print("BLOCKED: interrupted")
        return 2


def split(s: str) -> list:
    return [x.strip() for x in s.split(",") if x.strip()]


def dispatch(a) -> int:
    try:
        top = worktree(a.worktree)
    except Blocked:
        if a.cmd not in ("devices", "lease", "flows", "lint", "new"):
            raise
        top = None
    root, cfg = project_config(top)
    c = a.cmd
    if c == "devices" or (c == "lease" and a.action == "status"):
        cmd_devices(cfg, probe=c == "devices")
        return 0
    if c == "lease":
        _, ident, alias = resolve(a.device, cfg.get("phones") or {})
        if a.action == "acquire":
            lease_acquire(ident, alias, owner(), a.ttl, a.note)
            return 0
        lease_release(ident, alias, owner(), a.force)
        dev = make_device(a.device, cfg)
        if dev.platform == "android":
            try:
                note = dev.restore_screen()  # screen timeout, a leftover network isolation
            except Blocked:
                note = "phone not reachable: screen timeout not restored"
            if note:
                say(note)
        return 0
    if c in ("report", "new", "tags", "lint", "flows"):
        return cmd_offline(a, top or root, cfg)
    if c == "run":
        return cmd_run(a, top, cfg)

    dev = make_device(a.device, cfg)
    ident = ident_of(dev)
    check_lease(ident, dev.alias)
    if c != "build":  # a build alone does not touch the phone
        plugged(dev)
    if c in ("build", "deploy"):
        say(dev.build(top))
    if c in ("install", "deploy"):
        say(dev.install(top))
    elif c == "build-info":
        say(dev.build_info(top))
    elif c == "wda":
        if dev.platform != "ios":
            raise Blocked("wda is for an iPhone")
        say(dev.start_wda())
    elif c == "launch":
        say(dev.launch(wait_screen=a.wait_screen, restart=a.restart, timeout=a.timeout))
    elif c == "stop":
        dev.stop()
        say("stopped")
    elif c == "crash-check":
        dev.crash_check(mark_offset(ident, dev.log, a.since))
        say("app running")
    elif c == "mark":
        say("mark %s at %d" % (a.name, set_mark(ident, dev.log, a.name)))
    elif c == "logs":
        if dev.platform == "android":
            dev.ensure_capture()
        hits = grep_lines(read_since(dev.log, mark_offset(ident, dev.log, a.since)), a.pattern)
        for line in hits[-a.tail:]:
            print(short(line, 260))
        if not hits:
            say("no lines match %s" % a.pattern)
    elif c == "wait-log":
        line = wait_log(dev.log, mark_offset(ident, dev.log, a.since), a.pattern, a.timeout)
        if not line:
            raise Fail("no log /%s/ within %ds" % (a.pattern, a.timeout))
        say(short(line, 260))
    elif c == "prefs":
        return cmd_prefs(dev, a.args)
    elif c == "db":
        for row in sqlite_query(dev.pull_db(), a.sql):
            print(row)
    elif c == "file":
        say("%s %s" % (a.path, "exists" if dev.file_exists(a.path) else "missing"))
    elif c == "net":
        say(dev.wifi(a.state == "on") if a.what == "wifi" else dev.airplane(a.state == "on", a.confirm))
    elif c == "shot":
        shots = dev.dir / "shots"
        shots.mkdir(exist_ok=True)
        say(str(dev.shot(shots / ("%s.png" % a.name))))
    elif c == "ui":
        return cmd_ui(dev, a)
    return 0


def cmd_run(a, top: Path, cfg: dict) -> int:
    from devtest_scenario import ScenarioError, run_scenario, run_side_by_side
    phones = cfg.get("phones") or {}
    if a.platform:
        names = [default_phone(p, phones) for p in (["android", "ios"] if a.platform == "both" else [a.platform])]
    else:
        names = split(a.device or "") or [a.device]
    devices = [make_device(n, cfg) for n in names]
    for d in devices:
        check_lease(ident_of(d), d.alias)
    for d in devices:
        plugged(d)
    scenario = Path(a.scenario).expanduser().resolve()
    if not scenario.is_file():
        raise Blocked("no scenario at %s" % scenario)
    if len(devices) > 1:
        flags = ["--repeat", str(a.repeat)] + [f for f, on in (("--no-build-check", a.no_build_check),
                                                               ("--no-tags-check", a.no_tags_check),
                                                               ("-v", a.verbose)) if on]
        for opt, value in (("--expect-fail", a.expect_fail), ("--only", a.only), ("--skip", a.skip)):
            if value:
                flags += [opt, value]
        me = [sys.executable, str(Path(__file__).resolve())]
        return run_side_by_side({d.alias: me + ["--device", d.alias, "--worktree", str(top), "run", str(scenario)]
                                 + flags for d in devices})
    try:
        return run_scenario(scenario, devices, top, a.repeat, a.expect_fail, not a.no_build_check, a.verbose,
                            only=split(a.only), skip=split(a.skip), tags_check=not a.no_tags_check)
    except ScenarioError as e:
        print("SCENARIO ERROR: %s" % e)
        return 3


def cmd_offline(a, top: Path, cfg: dict) -> int:
    """Commands that read files only: no phone, no lease."""
    import devtest_guard
    import devtest_plan
    tests = top / "device-tests"
    if a.cmd == "report":
        from devtest_scenario import LEDGER, build_id
        entries = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()] if LEDGER.exists() else []
        lines = devtest_plan.report(entries, build_id(top), a.scenario)
        print("\n".join(lines) if lines else "no verdicts in %s" % LEDGER)
        return 0
    if a.cmd == "new":
        plan = sys.stdin.read() if a.plan == "-" else Path(a.plan).expanduser().read_text()
        try:
            suggested, source = devtest_plan.scaffold(plan)
        except ValueError as e:
            raise Blocked(str(e))
        out = Path(a.out or suggested or "device-tests/%s.py" % time.strftime("%Y%m%d-%H%M%S"))
        out = out if out.is_absolute() else top / out
        if out.exists():
            raise Blocked("%s exists: extend it by hand, or pass --out" % out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(source)
        say("wrote %s (%d steps to write)" % (out, source.count("NotImplementedError(")))
        return 0
    if a.cmd == "tags":
        from devtest_scenario import ScenarioError
        try:
            devtest_guard.preflight_tags(top, Path(a.scenario).expanduser().resolve(), strict=True)
        except ScenarioError as e:
            print("SCENARIO ERROR: %s" % e)
            return 3
        say("tags check: all present")
        return 0
    if a.cmd == "lint":
        files = [Path(f).expanduser().resolve() for f in a.files] or sorted(tests.glob("*.py"))
        errors = devtest_guard.lint(files)
        for e in errors:
            print(e)
        say("lint: %d file(s), %d problem(s)" % (len(files), len(errors)))
        return 1 if errors else 0
    lines = devtest_guard.flows_api(Path(a.dir).expanduser() if a.dir else tests / "flows")
    print("\n".join(lines) if lines else "no flows yet")
    return 0


def cmd_prefs(dev, args) -> int:
    if args and args[0] in ("set", "rm"):
        if (args[0] == "set" and len(args) != 3) or (args[0] == "rm" and len(args) != 2):
            raise Blocked("usage: prefs set KEY VALUE | prefs rm KEY")
        key, value = args[1], (args[2] if args[0] == "set" else None)
        dev.stop()
        dev.write_prefs(encode_pref(dev.prefs_bytes(), key, value))
        got = decode_prefs(dev.prefs_bytes()).get(key)
        if got != value:
            raise Blocked("write of %s did not stick (read back %r)" % (key, got))
        say("%s = %s (app stopped: launch it to apply)" % (key, value if value is not None else "<removed>"))
        return 0
    rows = format_prefs(decode_prefs(dev.prefs_bytes()), args[0] if args else None)
    print("\n".join(rows) if rows else "no keys match")
    return 0


def cmd_ui(dev, a) -> int:
    act, x = a.action, a.args
    if act == "src":
        print("\n".join(dev.src(x[0] if x else None)))
    elif act == "tap":
        say(dev.tap(x[0], a.timeout))
    elif act == "set-text":
        say(dev.set_text(x[0], x[1], a.timeout))
    elif act == "wait-screen":
        deadline = time.time() + a.timeout
        while True:
            screens = dev.screens()
            if x[0] in screens:
                say("on %s" % x[0])
                break
            if time.time() >= deadline:
                raise Fail("not on %s within %ds (screens: %s)" % (x[0], a.timeout, ", ".join(screens) or "-"))
            time.sleep(0.8)
    elif act == "visible":
        n = dev.node(x[0], a.timeout)
        if n is None:
            raise Fail("%s not visible" % x[0])
        say("%s visible%s%s%s" % (x[0], " checked" if n.checked else "", " selected" if n.selected else "",
                                  " text=%r" % short(n.text, 80) if n.text else ""))
    elif act == "back":
        dev.back()
        say("back")
    elif act in ("swipe", "refresh", "hide-keyboard"):
        {"swipe": lambda: dev.swipe(x[0]), "refresh": dev.refresh, "hide-keyboard": dev.hide_keyboard}[act]()
        say(act)
    else:
        raise Blocked("unknown ui action %s" % act)
    return 0


if __name__ == "__main__":
    sys.exit(main())
