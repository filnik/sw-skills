"""The iPhone tooling under devtest_ios: usbmuxd port forwards, the unsigned Xcode build of the app, its re-sign with a
local provisioning profile, the install, and WebDriverAgent (built for testing once, then run with xcodebuild).

Why an unsigned build and a manual re-sign: with no Apple account in Xcode `-allowProvisioningUpdates` fails with
"No Accounts", and a local profile often lacks entitlements the app declares (an App Group, say). So the build is
unsigned, the entitlements the profile does not grant are dropped, and so are the bundle parts the pack lists in
`ios.drop_from_bundle` (an extension that would need its own profile).

The app's values come from the pack: ios.bundle_id, team_id, scheme, project, target, entitlements, state_dir.
Standard library only, python 3.9 compatible. Needs the sandbox off (keychain, usbmuxd, devicectl).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import devtest_common as common
from devtest_common import cfg, need

BUNDLE = cfg("ios.bundle_id") or ""
LOCAL_PORT = 18100
DEVICE_PORT = 8100
WDA_URL = "http://127.0.0.1:%d" % LOCAL_PORT

STATE = Path(cfg("ios.state_dir")).expanduser() if cfg("ios.state_dir") else common.STATE / "ios"
LOGS = STATE / "logs"
APPS = STATE / "apps"
WDA_DD = STATE / "wda-dd"
PIDS = STATE / "pids.json"
CONSOLE_LOG = LOGS / "app-console.log"
PROFILE_DIRS = [
    Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles",
    Path.home() / "Library/MobileDevice/Provisioning Profiles",
]
SIGNED_WDA_GLOB = str(Path.home() / ".cache/appium-mcp/wda-real/*/signed/*/Payload-resigned.ipa")
WDA_PROJECT_GLOB = str(Path.home() / ".npm/_npx/*/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj")
WDA_LOCAL_PROJECT = STATE / "wda-source/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"
USBMUXD = "/var/run/usbmuxd"


class Fail(Exception):
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


# ---------------------------------------------------------------------------------------- usbmux

def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("usbmuxd closed the connection")
        buf += chunk
    return buf


def _usbmux(message: dict):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(USBMUXD)
    body = plistlib.dumps(dict(message, ClientVersionString="devtest", ProgName="devtest"))
    s.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)
    length = struct.unpack("<IIII", _recv_exact(s, 16))[0]
    return s, plistlib.loads(_recv_exact(s, length - 16))


def _norm(udid: str) -> str:
    return udid.replace("-", "").upper()


def usb_devices() -> list:
    s, reply = _usbmux({"MessageType": "ListDevices"})
    s.close()
    out = []
    for d in reply.get("DeviceList", []):
        p = d.get("Properties", {})
        out.append({"udid": p.get("SerialNumber", ""), "id": d.get("DeviceID"), "conn": p.get("ConnectionType", "")})
    return out


def usbmux_connect(udid: str, port: int) -> socket.socket:
    dev = next((d for d in usb_devices() if d["conn"] == "USB" and _norm(d["udid"]) == _norm(udid)), None)
    if dev is None:
        raise ConnectionError("device %s is not on USB" % udid)
    s, reply = _usbmux({"MessageType": "Connect", "DeviceID": dev["id"], "PortNumber": socket.htons(port)})
    if reply.get("Number", 1) != 0:
        s.close()
        raise ConnectionError("usbmuxd refused port %d: %s" % (port, reply))
    return s


def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


FORWARD_IDLE = float(os.environ.get("DEVTEST_FORWARD_IDLE", "1800"))


def forward(local: int, remote: int, udid: str, idle: float = FORWARD_IDLE) -> None:
    """Serves 127.0.0.1:local, tunnelling each connection to the device's port over usbmuxd.

    Exits after `idle` seconds with no new connection and none open: a session that ends without `wda stop` or
    `lease release` (lease expired, crash, phone unplugged) would otherwise leave forwards serving nothing for days.
    The next command that needs one starts it again: ensure_wda and baa_port check that it is alive."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", local))
    srv.listen(32)
    srv.settimeout(idle)
    pipes = []
    while True:
        try:
            client, _ = srv.accept()
        except socket.timeout:
            pipes = [t for t in pipes if t.is_alive()]
            if not pipes:
                srv.close()
                return
            continue
        client.settimeout(None)
        try:
            device = usbmux_connect(udid, remote)
        except Exception as e:  # keep serving: the device may come back
            sys.stderr.write("forward: %s\n" % e)
            client.close()
            continue
        for a, b in ((client, device), (device, client)):
            t = threading.Thread(target=_pipe, args=(a, b), daemon=True)
            t.start()
            pipes.append(t)


# ---------------------------------------------------------------------------------------- device

def pick_udid(explicit) -> str:
    wanted = explicit or os.environ.get("IOS_DEVICE_UDID")
    try:
        usb = [d for d in usb_devices() if d["conn"] == "USB"]
    except OSError as e:
        raise Fail("usbmuxd unreachable (%s): run with the sandbox off" % e)
    if wanted:
        if not any(_norm(d["udid"]) == _norm(wanted) for d in usb):
            raise Fail("device %s is not connected over USB. Plug in the cable: over Wi-Fi only the "
                       "CoreDevice tunnel sees it, usbmuxd does not." % wanted)
        return wanted
    udids = sorted({d["udid"] for d in usb})
    if not udids:
        raise Fail("no iPhone on USB. Plug in the cable (Wi-Fi is not enough for usbmuxd), "
                   "unlock it and trust this Mac.")
    if len(udids) > 1:
        raise Fail("several devices on USB, pass --udid: %s" % ", ".join(udids))
    return udids[0]


# ---------------------------------------------------------------------------------------- worktree

def worktree(arg) -> Path:
    try:
        top = common.worktree(arg)
    except common.Blocked as e:
        raise Fail(str(e))
    if not (top / need("ios.project")).is_dir():
        raise Fail("%s has no %s" % (top, cfg("ios.project")))
    return top


def xcodebuild_args(top: Path) -> list:
    return ["xcodebuild", "-project", str(top / need("ios.project")), "-scheme", need("ios.scheme"),
            "-configuration", "Debug", "-destination", "generic/platform=iOS", *(cfg("ios.xcodebuild_args") or []),
            "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO"]


def project_dir(top: Path) -> Path:
    return (top / need("ios.project")).parent


def built_app(top: Path) -> Path:
    r = run(xcodebuild_args(top) + ["-showBuildSettings"], cwd=str(project_dir(top)))
    wanted = cfg("ios.target") or need("ios.scheme")
    target, settings = None, {}
    for line in r.stdout.splitlines():
        m = re.match(r"Build settings for action \w+ and target (.+):", line)
        if m:
            target = m.group(1)
            continue
        if target == wanted and " = " in line:
            k, v = line.split(" = ", 1)  # split before stripping: an empty value leaves "KEY ="
            settings[k.strip()] = v.strip()
    if "BUILT_PRODUCTS_DIR" not in settings or "FULL_PRODUCT_NAME" not in settings:
        raise Fail("could not read the build settings of target %s:\n%s" % (wanted, r.stderr[-2000:]))
    return Path(settings["BUILT_PRODUCTS_DIR"]) / settings["FULL_PRODUCT_NAME"]


def signed_app(top: Path) -> Path:
    """The re-signed copy of this worktree's build: the .app under apps/<worktree>/."""
    folder = APPS / top.name
    return next(iter(sorted(folder.glob("*.app"))), folder / (cfg("ios.app_name") or "App.app"))


# ---------------------------------------------------------------------------------------- build

def cmd_build(a) -> None:
    top = worktree(a.worktree)
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / ("build-%s.log" % top.name)
    say("building %s (unsigned, generic iOS device) -> %s" % (top, log))
    started = time.time()
    with open(log, "w") as f:
        code = subprocess.call(xcodebuild_args(top) + ["build"], cwd=str(project_dir(top)),
                               stdout=f, stderr=subprocess.STDOUT)
    text = log.read_text(errors="replace")
    errors = [l for l in text.splitlines() if " error:" in l or l.startswith("error:")]
    minutes = (time.time() - started) / 60
    if code != 0 or "BUILD SUCCEEDED" not in text:
        say("BUILD FAILED after %.1f min" % minutes)
        for l in list(dict.fromkeys(errors))[:20]:
            say("  " + l[:400])
        raise Fail("see %s" % log)
    say("BUILD SUCCEEDED in %.1f min: %s" % (minutes, built_app(top)))


# ---------------------------------------------------------------------------------------- signing

def decode_profile(path: Path) -> dict:
    r = subprocess.run(["security", "cms", "-D", "-i", str(path)], capture_output=True)
    if r.returncode != 0:
        return {}
    try:
        return plistlib.loads(r.stdout)
    except Exception:
        return {}


def app_identifier() -> str:
    return "%s.%s" % (need("ios.team_id"), need("ios.bundle_id"))


def find_profile(udid: str):
    now = datetime.now(timezone.utc)
    best = None
    for d in PROFILE_DIRS:
        for p in d.glob("*.mobileprovision"):
            info = decode_profile(p)
            ent = info.get("Entitlements", {})
            if ent.get("application-identifier") != app_identifier():
                continue
            exp = info.get("ExpirationDate")
            if exp is not None and exp.replace(tzinfo=timezone.utc) < now:
                continue
            if _norm(udid) not in {_norm(x) for x in info.get("ProvisionedDevices", [])}:
                continue
            if best is None or exp > best[1].get("ExpirationDate"):
                best = (p, info)
    if best is None:
        raise Fail("no valid profile for %s that lists device %s. Open the project once in Xcode "
                   "with an account to create it." % (app_identifier(), udid))
    return best


def find_identity(profile: dict) -> str:
    certs = {hashlib.sha1(c).hexdigest().upper() for c in profile.get("DeveloperCertificates", [])}
    r = run(["security", "find-identity", "-v", "-p", "codesigning"])
    for line in r.stdout.splitlines():
        m = re.search(r"\b([0-9A-F]{40})\b", line)
        if m and m.group(1) in certs:
            return m.group(1)
    raise Fail("none of the profile's certificates is in the keychain:\n%s" % r.stdout)


def entitlements(top: Path, profile: dict) -> tuple:
    src = top / cfg("ios.entitlements", "")
    wanted = plistlib.loads(src.read_bytes()) if src.is_file() else {}
    team, bundle = need("ios.team_id"), need("ios.bundle_id")

    def sub(v):
        if isinstance(v, str):
            for name in ("APP_BUNDLE_ID", "PRODUCT_BUNDLE_IDENTIFIER"):
                v = v.replace("$(%s)" % name, bundle)
            return v.replace("$(AppIdentifierPrefix)", team + ".").replace("$(TeamIdentifierPrefix)", team + ".")
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    allowed = profile.get("Entitlements", {})
    kept, dropped = {}, []
    for k, v in wanted.items():
        if k in allowed:
            kept[k] = sub(v)
        else:
            dropped.append(k)
    kept.update({
        "application-identifier": app_identifier(),
        "com.apple.developer.team-identifier": team,
        "get-task-allow": True,
        "keychain-access-groups": [app_identifier()],
    })
    return kept, dropped


def cmd_sign(a) -> None:
    top = worktree(a.worktree)
    src = Path(a.app) if a.app else built_app(top)
    if not src.is_dir():
        raise Fail("%s does not exist: run `build` first" % src)
    udid = pick_udid(a.udid)
    profile_path, profile = find_profile(udid)
    identity = find_identity(profile)
    ents, dropped = entitlements(top, profile)

    out_dir = APPS / top.name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    app = out_dir / src.name
    shutil.copytree(src, app, symlinks=True)
    removed = [rel for rel in cfg("ios.drop_from_bundle") or [] if (app / rel).exists()]
    for rel in removed:
        shutil.rmtree(app / rel)
    shutil.copy(profile_path, app / "embedded.mobileprovision")
    ent_file = out_dir / "app.entitlements"
    ent_file.write_bytes(plistlib.dumps(ents))
    (out_dir / "checkout").write_text(str(top))  # lets a disk cleanup delete the copy once the worktree is gone

    nested = [p for p in app.rglob("*") if p.suffix in (".framework", ".dylib", ".appex")]
    for p in sorted(nested, key=lambda p: len(p.parts), reverse=True):
        r = run(["codesign", "-f", "-s", identity, "--timestamp=none", str(p)])
        if r.returncode != 0:
            raise Fail("codesign failed on %s:\n%s" % (p, r.stderr))
    r = run(["codesign", "-f", "-s", identity, "--timestamp=none", "--entitlements", str(ent_file), str(app)])
    if r.returncode != 0:
        raise Fail("codesign failed on %s:\n%s" % (app, r.stderr))
    r = run(["codesign", "-v", "--deep", str(app)])
    if r.returncode != 0:
        raise Fail("signature does not verify:\n%s" % r.stderr)
    say("signed %s with profile %s" % (app, profile.get("Name")))
    say("  dropped: %s (not in the profile)" % (", ".join(removed + dropped) or "nothing"))


def cmd_install(a) -> None:
    top = worktree(a.worktree) if not a.app else None
    app = Path(a.app) if a.app else signed_app(top)
    if not app.is_dir():
        raise Fail("%s does not exist: run `sign` first" % app)
    udid = pick_udid(a.udid)
    last = ""
    for attempt in range(1, 5):
        r = run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, str(app)])
        if r.returncode == 0:
            say("installed %s on %s (attempt %d)" % (app.name, udid, attempt))
            return
        last = (r.stdout + r.stderr).strip().splitlines()
        last = next((l for l in last if "ERROR" in l or "rror" in l), last[-1] if last else "")
        say("install attempt %d failed: %s" % (attempt, last))
        time.sleep(3)
    raise Fail("install failed: %s. Unlock the phone and check the cable." % last)


# ---------------------------------------------------------------------------------------- processes

def load_pids() -> dict:
    try:
        return json.loads(PIDS.read_text())
    except (OSError, ValueError):
        return {}


def save_pids(pids: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    PIDS.write_text(json.dumps(pids))


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def stop(name: str) -> None:
    pids = load_pids()
    pid = pids.pop(name, None)
    if pid and alive(pid):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except OSError:
            os.kill(int(pid), signal.SIGTERM)
    save_pids(pids)


def spawn(name: str, cmd: list, log: Path) -> int:
    stop(name)
    LOGS.mkdir(parents=True, exist_ok=True)
    f = open(log, "a")
    f.write("==== %s %s\n" % (name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    f.flush()
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    pids = load_pids()
    pids[name] = p.pid
    save_pids(pids)
    return p.pid


# ---------------------------------------------------------------------------------------- WDA

def http(method: str, path: str, body=None, timeout: float = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(WDA_URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def wda_up() -> bool:
    try:
        return http("GET", "/status", timeout=3).get("value", {}).get("ready", True) is not False
    except (OSError, ValueError, urllib.error.URLError):
        return False


def latest(pattern: str):
    found = glob.glob(pattern)
    return Path(max(found, key=os.path.getmtime)) if found else None


def wda_xctestrun() -> Path:
    products = WDA_DD / "Build/Products"
    run_file = latest(str(products / "*.xctestrun"))
    runner = products / "Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    ipa = latest(SIGNED_WDA_GLOB)
    if ipa is None and run_file is not None and runner.is_dir():
        if run(["codesign", "--verify", "--deep", str(runner)]).returncode == 0:
            return run_file
    if run_file is None or ipa is None:
        project = latest(WDA_PROJECT_GLOB) or (WDA_LOCAL_PROJECT if WDA_LOCAL_PROJECT.is_dir() else None)
        if project is None:
            command = ["npm", "install", "--prefix", str(WDA_LOCAL_PROJECT.parents[2]), "appium-webdriveragent"]
            raise Fail("BLOCKED: WebDriverAgent sources missing. Run: %s; then retry `wda start`." %
                       shlex.join(command))
        log = LOGS / "wda-build.log"
        LOGS.mkdir(parents=True, exist_ok=True)
        say("building WebDriverAgent for testing (once) from %s -> %s" % (project, log))
        command = ["xcodebuild", "-project", str(project), "-scheme", "WebDriverAgentRunner",
                   "-destination", "generic/platform=iOS", "-derivedDataPath", str(WDA_DD),
                   "CODE_SIGNING_ALLOWED=%s" % ("YES" if ipa is None else "NO"),
                   # WDA's own xcconfig turns warnings into errors, and a stray
                   # /usr/local/include warning then fails the runner.
                   "GCC_TREAT_WARNINGS_AS_ERRORS=NO", "RUN_CLANG_STATIC_ANALYZER=NO"]
        if ipa is None:
            command += ["CODE_SIGN_STYLE=Automatic", "DEVELOPMENT_TEAM=%s" % need("ios.team_id")]
        command.append("build-for-testing")
        with open(log, "w") as f:
            code = subprocess.call(command, stdout=f, stderr=subprocess.STDOUT)
        run_file = latest(str(products / "*.xctestrun"))
        if ipa is None:
            if code != 0 or run_file is None or not runner.is_dir() or run(
                    ["codesign", "--verify", "--deep", str(runner)]).returncode != 0:
                raise Fail("BLOCKED: WebDriverAgent needs local signing. Run: %s; select team %s and "
                           "prepare automatic signing, then run:\n%s\nBuild log: %s" % (
                               shlex.join(["open", "-a", "Xcode", str(project)]), cfg("ios.team_id"),
                               shlex.join(command), log))
            (STATE / "wda-runner.src").unlink(missing_ok=True)
            say("WebDriverAgent runner signed locally by Xcode")
            return run_file
        if code != 0 or run_file is None:
            raise Fail("WebDriverAgent build-for-testing failed, see %s" % log)

    marker = STATE / "wda-runner.src"
    stamp = "%s %d" % (ipa, int(ipa.stat().st_mtime))
    if not runner.exists() or not marker.exists() or marker.read_text() != stamp:
        unpacked = STATE / "wda-ipa"
        if unpacked.exists():
            shutil.rmtree(unpacked)
        with zipfile.ZipFile(ipa) as z:
            z.extractall(unpacked)
        signed_runner = next(unpacked.glob("Payload/*.app"))
        # zipfile drops the executable bits
        for p in signed_runner.rglob("*"):
            if p.is_file():
                p.chmod(0o755)
        if runner.exists():
            shutil.rmtree(runner)
        shutil.copytree(signed_runner, runner, symlinks=True)
        marker.write_text(stamp)
        say("WebDriverAgent runner replaced with the signed one from %s" % ipa)
    return run_file


def cmd_wda(a) -> None:
    if a.action == "stop":
        stop("wda")
        stop("forward")
        stop("baa-forward")
        say("WebDriverAgent and the port forwards stopped")
        return
    if a.action == "status":
        pids = load_pids()
        say("wda pid %s alive=%s, forward pid %s alive=%s, %s answers=%s" % (
            pids.get("wda"), alive(pids.get("wda")), pids.get("forward"), alive(pids.get("forward")), WDA_URL, wda_up()))
        return
    udid = pick_udid(a.udid)
    if wda_up() and alive(load_pids().get("wda")):
        say("WebDriverAgent already answers on %s" % WDA_URL)
        return
    if alive(load_pids().get("wda")) and not alive(load_pids().get("forward")):
        # The forward exits after FORWARD_IDLE with no client while WDA keeps running: restart only the forward.
        spawn("forward", [sys.executable, str(Path(__file__).resolve()), "--udid", udid, "_forward"], LOGS / "forward.log")
        for _ in range(10):
            if wda_up():
                say("WebDriverAgent answers again on %s (port forward restarted)" % WDA_URL)
                return
            time.sleep(0.5)
    xctestrun = wda_xctestrun()
    log = LOGS / "wda-run.log"
    spawn("wda", ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun), "-destination", "id=%s" % udid], log)
    spawn("forward", [sys.executable, str(Path(__file__).resolve()), "--udid", udid, "_forward"], LOGS / "forward.log")
    say("starting WebDriverAgent on %s (keep the phone unlocked)..." % udid)
    deadline = time.time() + 150
    while time.time() < deadline:
        if wda_up():
            say("WebDriverAgent ready on %s" % WDA_URL)
            say("Use `launch` or `ui src`: the script creates its WDA HTTP session automatically.")
            return
        if not alive(load_pids().get("wda")):
            break
        time.sleep(2)
    tail = log.read_text(errors="replace").splitlines()[-25:]
    raise Fail("WebDriverAgent did not come up. Unlock the phone; if it asks, trust the developer in "
               "Settings > General > VPN & Device Management. Log tail:\n" + "\n".join(tail))


# ---------------------------------------------------------------------------------------- ui

def sid() -> str:
    s = http("GET", "/status").get("sessionId")
    if not s:
        # forceAppLaunch false: after a WDA restart the new session attaches to the running app instead of relaunching it.
        caps = {"capabilities": {"alwaysMatch": {"bundleId": need("ios.bundle_id"), "shouldWaitForQuiescence": False,
                                                 "forceAppLaunch": False}}}
        s = http("POST", "/session", caps)["sessionId"]
    return s


def touch(actions: list) -> None:
    http("POST", "/session/%s/actions" % sid(), {"actions": [
        {"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": actions}]})


def ui_src(flt) -> None:
    root = ET.fromstring(http("GET", "/source?format=xml&excluded_attributes=", timeout=120)["value"])
    screens, rows = [], []
    for el in root.iter():
        name, label, value = el.get("name"), el.get("label"), el.get("value")
        if name and name.startswith("screen."):
            screens.append(name)
            continue
        if el.get("visible") != "true" or not (name or label or value):
            continue
        if el.get("type") in ("XCUIElementTypeApplication", "XCUIElementTypeWindow",
                              "XCUIElementTypeKey", "XCUIElementTypeKeyboard"):
            continue
        x, y, w, h = (int(float(el.get(k, 0))) for k in ("x", "y", "width", "height"))
        kind = el.get("type", "").replace("XCUIElementType", "")
        text = " | ".join(dict.fromkeys(v for v in (name, label, value) if v))
        line = "%-10.10s (%d,%d) %s" % (kind, x + w // 2, y + h // 2, text[:150])
        if not flt or flt.lower() in line.lower():
            rows.append(line)
    print("SCREENS:", ", ".join(dict.fromkeys(screens)) or "-")
    for r in dict.fromkeys(rows):
        print(r)


# ---------------------------------------------------------------------------------------- main

def main() -> int:
    """Only the port forward runs as a process of its own: `devtest_wda.py --udid UDID _forward [--local N --remote N]`."""
    p = argparse.ArgumentParser()
    p.add_argument("--udid", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("_forward")
    sp.add_argument("--local", type=int, default=LOCAL_PORT)
    sp.add_argument("--remote", type=int, default=DEVICE_PORT)
    a = p.parse_args()
    forward(a.local, a.remote, a.udid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
