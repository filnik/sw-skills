#!/usr/bin/env python3
"""devtest — compact, scriptable testing of a mobile app on real phones (Android over adb, iPhone over WebDriverAgent).

    devtest [--device ALIAS|SERIAL|UDID] [--owner NAME] [--worktree W] <command> ...

    devices [--json]                          the pack's phones, connected or not, and their leases
    lease acquire|release|status [--ttl 90m] [--note N] [--force]
    build | install | deploy [--worktree W]   Debug build of the worktree (default: git top of cwd)
    build-info [--worktree W]                 is the installed build the worktree's latest? (BLOCKED if not)
    launch [--env E] [--wait-screen S] [--restart]
    stop | crash-check [--since MARK]
    mark NAME                                 remember the current end of the app log
    logs PRESET|REGEX [--since MARK] [--tail 20]   presets: crash, baa and the pack's log_presets
    wait-log REGEX [--since MARK] [--timeout 30]
    prefs [FILTER] | prefs set KEY VALUE | prefs rm KEY      the app's prefs file: DataStore or plist (set/rm stop the app)
    flag NAME on|off|default                  a feature-flag override written to the prefs (stops the app; pack hook)
    db "SQL"                                  query a fresh copy of the app's SQLite DB (+wal, shm)
    file PATH                                 does an app file exist (files/…, Documents/…, /sdcard/…)
    net wifi on|off | net airplane on|off
    shot NAME                                 downscaled screenshot, prints only its path
    ui src [FILTER] | tap SEL | set-text SEL VALUE | wait-screen TAG | visible SEL | back | swipe up|down
       | refresh | hide-keyboard                SEL: test tag, text:Exact, text~part, desc:…, desc~…
    baa ARGS...                               the app's own automation CLI (pack key `baa`) with this device's port
    run SCENARIO.py [--platform android|ios|both] [--repeat N] [--expect-fail STEP] [--no-build-check] [-v]
        several phones (--platform both, --device a,b) run side by side, except a two-phone scenario
        [--only O9,5] [--skip A6]             steps covering these criteria (or with these numbers) only / not
        [--no-tags-check]                     skip the preflight that finds every selector in the worktree
    new PLAN.md|- [--out PATH]                a scenario skeleton from a test plan: one step per check, not yet written
    clean --prefix P                          the pack's cleanup of the test data named with that prefix (pack hook)
    suite run NAME [--phones a,b] [--tiers vital,data]   the pack's suites/NAME.py tier by tier, in parallel per phone
    suite report NAME [--build SHA]           verdicts per scenario and phone for a build, from the ledger
    tags check [SCENARIO.py] [-v]             selectors of flows (and the scenario) present in the worktree
    lint [SCENARIO.py ...]                    no flow re-implemented in a scenario, no helper copied between two
    api                                       regenerate the flows' API.md from the docstrings

The app's constants, phones, flows, scenarios and suites come from a pack (see devtest_common). Scenario paths that
do not exist as given are looked up in the pack's scenarios/.
Output: one line on success; on failure a few lines plus evidence paths.
Exit: 0 ok · 1 FAIL (the app misbehaved) · 2 BLOCKED (device/tooling/setup) · 3 SCENARIO ERROR.
Needs the sandbox off (adb, usbmuxd, devicectl).
Shell state does not survive between tool calls, so pass --owner on every call instead of exporting DEVTEST_OWNER.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if "--worktree" in sys.argv[1:-1]:  # the project config is looked up from the worktree, before the pack loads
    os.environ["DEVTEST_WORKTREE"] = sys.argv[sys.argv.index("--worktree") + 1]

from devtest_common import (DEFAULT_FOR, DEVICES, Blocked, Fail, check_lease, decode_prefs, encode_string_pref,  # noqa: E402
                            format_prefs, grep_lines, hook, lease_acquire, lease_release, lease_status, mark_offset, need,
                            owner, pack_dir, read_since, resolve, run, say, set_mark, short, sqlite_query, current_lease,
                            wait_log, worktree)


def plugged(dev) -> None:
    """A phone that is not connected: ask to plug it in (macOS notification) and wait, see devtest_phones."""
    from devtest_phones import wait_connected
    if wait_connected(getattr(dev, "serial", None) or dev.udid, dev.alias) and dev.platform == "android":
        dev.reconnect()


def make_device(name):
    platform, ident, alias = resolve(name)
    if platform == "android":
        from devtest_android import Android
        return Android(ident, alias)
    from devtest_ios import IOS
    return IOS(ident, alias)


def override_key(top: Path, name: str) -> str:
    """The prefs key that stores a feature flag's local override: the pack's hook knows the app's naming."""
    resolve_key = hook("override_key")
    if not resolve_key:
        raise Blocked("`flag` needs override_key(top, name) in the pack's hooks.py: it maps a flag to its prefs key")
    return resolve_key(top, name)


def scenario_path(name: str) -> Path:
    path = Path(name).expanduser()
    packed = pack_dir("scenarios")
    if not path.exists() and packed and (packed / path.name).exists():
        return (packed / path.name).resolve()
    return path.resolve()


def default_phones(platform) -> list:
    wanted = ("android", "ios") if platform == "both" else (platform,)
    missing = [p for p in wanted if p not in DEFAULT_FOR]
    if missing:
        raise Blocked("no %s phone in the pack: pass --device" % " or ".join(missing))
    return [DEFAULT_FOR[p] for p in wanted]


def cmd_devices(_a) -> None:
    from devtest_common import PACK, PACKS
    print("(pack: %s)" % PACK if PACK else "(no pack loaded, so no app constants and no phone aliases: %s)" % (
        "name one of the packs in device_test.pack of .claude/config/project.json" if any(PACKS.glob("*/pack.json"))
        else "add packs/<name>/pack.json, see SKILL.md"), file=sys.stderr)
    lines = run(["adb", "devices"], text=True).stdout.splitlines()[1:]
    serials = {l.split()[0] for l in lines if l.endswith("device")}
    try:
        from devtest_ios import idev
        udids = {d["udid"] for d in idev.usb_devices() if d["conn"] == "USB"}
    except Exception:
        udids = set()
    # Phones on adb over Wi-Fi (`adb tcpip` + `adb connect`) show as ip:port: map them back to their serial.
    wifi = {run(["adb", "-s", s, "shell", "getprop", "ro.serialno"], timeout=10, text=True).stdout.strip(): s
            for s in serials if ":" in s}
    for alias, (platform, ident) in DEVICES.items():
        connected = ident in serials or any(ident.replace("-", "") == u.replace("-", "") for u in udids)
        state = "wifi %s" % wifi[ident] if ident in wifi else "connected" if connected else "not connected"
        lease = current_lease(ident)
        say("%-7s %-8s %-26s %-13s %s" % (alias, platform, ident, state,
                                          "free" if not lease else "leased by %s" % lease["owner"]))
    # A phone with no alias is leased by its serial: list it, or it looks free while a session holds it.
    for s in sorted(serials - {i for _, i in DEVICES.values()} - set(wifi.values())):
        lease = current_lease(s)
        say("%-7s %-8s %-26s %-13s %s" % ("-", "android", s, "connected",
                                          "free" if not lease else "leased by %s" % lease["owner"]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", help="a pack alias (%s), a serial or a UDID (default $DEVTEST_DEVICE)" % (
        ", ".join(DEVICES) or "none configured"))
    p.add_argument("--worktree", help="the app's worktree (default: git top of cwd)")
    p.add_argument("--owner", help="who holds the lease (default $DEVTEST_OWNER); pass it on every call")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("devices")
    sp.add_argument("--json", action="store_true", help="phones, leases, holder sessions and plug-in requests")
    sp = sub.add_parser("lease")
    sp.add_argument("action", choices=["acquire", "release", "status"])
    sp.add_argument("--ttl", default="90m")
    sp.add_argument("--note", default="")
    sp.add_argument("--force", action="store_true")
    for name in ("build", "install", "deploy", "build-info", "stop", "wda"):
        sub.add_parser(name)
    sp = sub.add_parser("launch")
    sp.add_argument("--env", help="the app's environment, when it asks at launch (default: the pack's default_env)")
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
    sp = sub.add_parser("flag")
    sp.add_argument("name")
    sp.add_argument("state", choices=["on", "off", "default"])
    sp = sub.add_parser("db")
    sp.add_argument("sql")
    sp = sub.add_parser("file")
    sp.add_argument("path")
    sp = sub.add_parser("net")
    sp.add_argument("what", choices=["wifi", "airplane"])
    sp.add_argument("state", choices=["on", "off"])
    sp = sub.add_parser("shot")
    sp.add_argument("name")
    sp = sub.add_parser("ui")
    sp.add_argument("action")
    sp.add_argument("args", nargs="*")
    sp.add_argument("--timeout", type=float, default=10)
    sp = sub.add_parser("baa")
    sp.add_argument("args", nargs=argparse.REMAINDER)
    sp = sub.add_parser("run")
    sp.add_argument("scenario")
    sp.add_argument("--platform", choices=["android", "ios", "both"])
    sp.add_argument("--repeat", type=int, default=1)
    sp.add_argument("--expect-fail", help="step number that must FAIL (red proof on the base build)")
    sp.add_argument("--no-build-check", action="store_true")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.add_argument("--only", default="", help="criteria or step numbers, comma separated")
    sp.add_argument("--skip", default="", help="criteria or step numbers, comma separated")
    sp.add_argument("--no-tags-check", action="store_true")
    sp = sub.add_parser("new")
    sp.add_argument("plan", help="a test plan file, or - to read it from stdin")
    sp.add_argument("--out", help="where to write it (default: the plan's 'Scenario: new PATH', in the pack's scenarios/)")
    sp = sub.add_parser("clean")
    sp.add_argument("--prefix", required=True)
    sp = sub.add_parser("suite")
    sp.add_argument("action", choices=["run", "report"])
    sp.add_argument("name")
    sp.add_argument("--phones", default=",".join(DEFAULT_FOR[p] for p in ("android", "ios") if p in DEFAULT_FOR))
    sp.add_argument("--tiers", default="")
    sp.add_argument("--build")
    sp = sub.add_parser("tags")
    sp.add_argument("action", choices=["check"])
    sp.add_argument("scenario", nargs="?")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp = sub.add_parser("lint")
    sp.add_argument("files", nargs="*")
    sub.add_parser("api")

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


def dispatch(a) -> int:
    if a.cmd == "devices":
        if a.json:
            import json
            import time
            from devtest_phones import phones
            print(json.dumps({"now": time.time(), "phones": phones()}))
        else:
            cmd_devices(a)
        return 0
    if a.cmd == "lease":
        if a.action == "status":
            lease_status()
            return 0
        _, ident, alias = resolve(a.device)
        who = owner()
        if a.action == "acquire":
            lease_acquire(ident, alias, who, a.ttl, a.note)
        else:
            lease_release(ident, alias, who, a.force)
            dev = make_device(a.device)
            note = dev.restore_screen() if dev.platform == "android" else dev.stop_tools()
            if note:
                say(note)
        return 0
    if a.cmd == "run":
        from devtest_scenario import ScenarioError, run_scenario, run_side_by_side, uses_pair
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
        names = (default_phones(a.platform) if a.platform else None) or split(a.device or "") or [a.device]
        devices = [make_device(n) for n in names]
        for d in devices:
            check_lease(getattr(d, "serial", None) or d.udid, d.alias)
        for d in devices:
            plugged(d)
        top = worktree(a.worktree)
        scenario = scenario_path(a.scenario)
        if len(devices) > 1 and not uses_pair(scenario):
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
            return run_scenario(scenario, devices, top, a.repeat, a.expect_fail,
                                not a.no_build_check, a.verbose, only=split(a.only), skip=split(a.skip),
                                tags_check=not a.no_tags_check)
        except ScenarioError as e:
            print("SCENARIO ERROR: %s" % e)
            return 3
    if a.cmd == "new":
        import time
        import devtest_plan
        plan = sys.stdin.read() if a.plan == "-" else Path(a.plan).expanduser().read_text()
        try:
            suggested, source = devtest_plan.scaffold(plan)
        except ValueError as e:
            raise Blocked(str(e))
        out = Path(a.out or suggested or "%s.py" % time.strftime("%Y%m%d-%H%M%S")).expanduser()
        if not out.is_absolute() and not a.out and pack_dir("scenarios"):
            out = pack_dir("scenarios") / out.name
        if out.exists():
            raise Blocked("%s exists: extend it by hand, or pass --out" % out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(source)
        say("wrote %s (%d steps to write)" % (out.resolve(), source.count("NotImplementedError(")))
        return 0
    if a.cmd in ("tags", "lint", "api"):
        import devtest_guard
        if a.cmd == "api":
            say("wrote %s" % devtest_guard.api())
            return 0
        if a.cmd == "lint":
            errors, legacy = devtest_guard.lint(a.files)
            for e in errors:
                print(e)
            say("lint: %d problem(s); %d scenario(s) not on flows yet: %s" % (len(errors), len(legacy),
                                                                              ", ".join(legacy) or "-"))
            return 1 if errors else 0
        errors, warnings, report = devtest_guard.check_tags(worktree(a.worktree), a.scenario)
        for line in (report if a.verbose else []) + ["warning: " + w for w in warnings] + errors:
            print(line)
        say("tags check: %d missing, %d warning(s)" % (len(errors), len(warnings)))
        return 3 if errors else 0
    if a.cmd == "suite":
        import devtest_suite
        top = worktree(a.worktree)
        if a.action == "report":
            return devtest_suite.report(a.name, top, a.build)
        return devtest_suite.run(a.name, [x for x in a.phones.split(",") if x], top, owner(),
                               [t for t in a.tiers.split(",") if t] or None)

    dev = make_device(a.device)
    ident = getattr(dev, "serial", None) or dev.udid
    check_lease(ident, dev.alias)
    c = a.cmd
    if c != "build":  # a build alone does not touch the phone
        plugged(dev)
    if c in ("build", "install", "deploy", "build-info"):
        top = worktree(a.worktree)
        if c in ("build", "deploy"):
            say(dev.build(top))
        if c in ("install", "deploy"):
            say(dev.install(top))
        if c == "build-info":
            say(dev.build_info(top))
    elif c == "wda":
        dev.ensure_wda()
        say("WebDriverAgent ready")
    elif c == "clean":
        from devtest_common import device_dir
        from devtest_scenario import Driver
        clean = hook("clean")
        if not clean:
            raise Blocked("`clean` needs clean(x, prefix) in the pack's hooks.py")
        say(clean(Driver(dev, worktree(a.worktree), device_dir(ident) / "clean", {}), a.prefix))
    elif c == "launch":
        say(dev.launch(env=a.env, wait_screen=a.wait_screen, restart=a.restart, timeout=a.timeout))
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
        for l in hits[-a.tail:]:
            print(short(l, 260))
        if not hits:
            say("no lines match %s" % a.pattern)
    elif c == "wait-log":
        line = wait_log(dev.log, mark_offset(ident, dev.log, a.since), a.pattern, a.timeout)
        if not line:
            raise Fail("no log /%s/ within %ds" % (a.pattern, a.timeout))
        say(short(line, 260))
    elif c == "prefs":
        return cmd_prefs(dev, a.args)
    elif c == "flag":
        key = override_key(worktree(a.worktree), a.name)
        return cmd_prefs(dev, ["set", key, {"on": "true", "off": "false"}[a.state]] if a.state != "default" else ["rm", key])
    elif c == "db":
        for row in sqlite_query(dev.pull_db(), a.sql):
            print(row)
    elif c == "file":
        say("%s %s" % (a.path, "exists" if dev.file_exists(a.path) else "missing"))
    elif c == "net":
        say(dev.wifi(a.state == "on") if a.what == "wifi" else dev.airplane(a.state == "on"))
    elif c == "shot":
        from devtest_common import device_dir
        shots = device_dir(ident) / "shots"
        shots.mkdir(exist_ok=True)
        say(str(dev.shot(shots / ("%s.png" % a.name))))
    elif c == "ui":
        return cmd_ui(dev, a)
    elif c == "baa":
        import subprocess
        top = worktree(a.worktree)
        return subprocess.call([str(top / need("baa")), "--port", str(dev.baa_port()), *a.args], cwd=str(top))
    return 0


def cmd_prefs(dev, args) -> int:
    if args and args[0] in ("set", "rm"):
        if (args[0] == "set" and len(args) != 3) or (args[0] == "rm" and len(args) != 2):
            raise Blocked("usage: prefs set KEY VALUE | prefs rm KEY")
        key = args[1]
        value = args[2] if args[0] == "set" else None
        dev.stop()
        dev.write_prefs(encode_string_pref(dev.prefs_bytes(), key, value))
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
        import time
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
