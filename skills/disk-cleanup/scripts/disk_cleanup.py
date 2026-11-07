#!/usr/bin/env python3
"""disk-cleanup: find what fills a developer's disk and free it without losing work.

    disk_cleanup.py scan [--roots DIR,DIR] [--days 14] [--derived-days 7] [--cache-hours 24] [--plan FILE]
    disk_cleanup.py clean --plan FILE (--all-safe | --cat CAT,CAT | --ids 1,4,7-12) [--dry-run]

scan only reads: it measures, writes a numbered plan and prints the report. clean deletes only what was picked and
re-checks each item right before: anything that changed since the scan is skipped, with the reason.
Categories: checkout-build, xcode, gradle, packages, cache (regenerable, in --all-safe) and look (only by --ids).
A folder holding a .disk-cleanup-keep file is never proposed nor deleted, nor is anything under it; when the file
lists names (patterns, one per line, # for comments), only those are protected. Nothing behind a symlink is deleted.
Never reads ~/Library/Containers, ~/Library/Group Containers or ~/Pictures (macOS asks for permission there).
Stdlib only, Python 3.9+.
"""
import argparse
import calendar
import fnmatch
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home()
LIB = HOME / "Library"
XCODE = LIB / "Developer/Xcode"
DERIVED = XCODE / "DerivedData"
ARCHIVES = XCODE / "Archives"
SIM_DEVICES = LIB / "Developer/CoreSimulator/Devices"
CACHES = LIB / "Caches"
GRADLE = HOME / ".gradle"
LOOSE = [HOME / "Downloads", HOME / "Desktop"]
PRIVATE = [LIB / "Containers", LIB / "Group Containers", HOME / "Pictures"]
DEFAULT_ROOTS = ["Documents", "Developer", "Projects", "code", "src", "dev"]
STATE = HOME / ".cache/disk-cleanup/last-run.json"
KEEP = ".disk-cleanup-keep"
INFO = [".android/avd", "Library/Android/sdk/system-images", "Android/Sdk/system-images", ".cache",
        "Library/Developer/CoreSimulator"]
# Ignored folders a build or an install brings back: the only thing deleted inside a checkout.
BUILD_DIRS = {"node_modules", "build", "dist", ".next", ".nuxt", ".turbo", "target", ".gradle", ".kotlin",
              ".dart_tool", ".venv", "venv", "__pycache__", ".parcel-cache", "Pods", "DerivedData"}
BUNDLES = (".app", ".photoslibrary", ".imovielibrary", ".fcpbundle", ".tvlibrary", ".musiclibrary", ".xcarchive")
SIMCTL = ["xcrun", "simctl", "delete", "unavailable"]
# Package caches are emptied by their own tool, never with rm: (command, folders measured, what it is).
TOOLS = {
    "npm": (["npm", "cache", "clean", "--force"], [".npm/_cacache"], "npm cache"),
    "yarn": (["yarn", "cache", "clean"], ["Library/Caches/Yarn", ".cache/yarn"], "Yarn cache"),
    "pnpm": (["pnpm", "store", "prune"], ["Library/pnpm/store", ".local/share/pnpm/store"],
             "pnpm store · frees only packages no project uses, so less than shown"),
    "pip": (["python3", "-m", "pip", "cache", "purge"], ["Library/Caches/pip", ".cache/pip"], "pip cache"),
    "brew": (["brew", "cleanup", "-s"], ["Library/Caches/Homebrew"], "Homebrew downloads and old versions"),
    "pod": (["pod", "cache", "clean", "--all"], ["Library/Caches/CocoaPods"], "CocoaPods cache"),
}
DOCKER = {
    "docker-images": (["docker", "image", "prune", "-a", "-f"], "Images", "Docker images no container uses"),
    "docker-build": (["docker", "builder", "prune", "-f"], "Build Cache", "Docker build cache"),
}
COMMANDS = {**{k: v[0] for k, v in TOOLS.items()}, **{k: v[0] for k, v in DOCKER.items()}, "simctl": SIMCTL}
ALLOWED_COMMANDS = {"packages": set(TOOLS) | set(DOCKER), "xcode": {"simctl"}}
# ~/Library/Caches entries left alone: installs living there, IDE local history, caches packages empties by tool.
CACHE_SKIP = {"ms-playwright", "ms-playwright-mcp", "Cypress", "pypoetry", "Google", "JetBrains",
              "Yarn", "pip", "Homebrew", "CocoaPods", "pnpm"}
WRAPPER = re.compile(r"distributionUrl=.*gradle-(\d[\w.]*?(?:-rc-\d+)?)-(?:bin|all)\.zip")
SECTIONS = [
    ("checkout-build", "BUILD IN IDLE CHECKOUTS · ignored build output of git checkouts untouched for {days} days "
                       "(sources and .git never touched)"),
    ("xcode", "XCODE · DerivedData orphaned or unused for {derived_days} days, old device symbols, "
              "unavailable simulators"),
    ("gradle", "GRADLE · versions no scanned project uses, build cache idle for {days} days"),
    ("packages", "PACKAGES · package-manager caches, emptied by the tool's own command"),
    ("cache", "CACHES · ~/Library/Caches entries unused for {cache_hours} h (Apple's and installs left out)"),
    ("look", "TO LOOK AT · never in --all-safe, only by number"),
]
BIG, MIN_SIZE, DAY, PLAN_TTL = 1 << 30, 1 << 20, 86400, 86400
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}  # status must not rewrite the index


# ---------------------------------------------------------------- helpers

def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            break
        n /= 1024
    else:
        unit = "TB"
    return f"{n:.1f} {unit}" if unit in ("GB", "TB") else f"{n:.0f} {unit}"


def short(p):
    return str(p).replace(str(HOME), "~", 1)


def mtime(p):
    try:
        return os.lstat(p).st_mtime
    except OSError:
        return 0.0


def idle_days(ts):
    return (time.time() - ts) / DAY


def inside(path, roots):
    return any(str(path) == str(r) or str(path).startswith(str(r) + os.sep) for r in roots)


def strictly_inside(path, roots):
    return any(str(path).startswith(str(r) + os.sep) for r in roots)


def plain(p):
    """No symlink anywhere in the path, so the delete hits exactly what was inspected."""
    return os.path.realpath(p) == str(p)


def kept(p):
    """Protected by a KEEP file in it (deleting it would take what the file protects) or in a folder above it,
    up to HOME; a KEEP file that lists patterns protects only the entries below it that match."""
    p = Path(p)
    for d in [p, *p.parents]:
        marker = d / KEEP
        if marker.is_file():
            if d == p:
                return True
            try:
                names = [ln.strip() for ln in marker.read_text(errors="replace").splitlines()
                         if ln.strip() and not ln.lstrip().startswith("#")]
            except OSError:
                return True
            if not names or any(fnmatch.fnmatch(p.name, n) or fnmatch.fnmatch(str(p.relative_to(d)), n)
                                for n in names):
                return True
        if d in (HOME, Path(p.anchor)):
            break
    return False


def keep_below(p):
    """A KEEP file anywhere inside folder p: deleting p would take what it protects. True when that cannot be checked."""
    if not os.path.isdir(p):
        return False
    try:
        r = subprocess.run(["find", str(p), "-name", KEEP, "-print", "-quit"], capture_output=True, text=True,
                           errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return bool(r.stdout.strip())


def parse_ids(spec):
    out = []
    for part in spec.replace(" ", "").split(","):
        if part:
            a, _, b = part.partition("-")
            out.extend(range(int(a), int(b or a) + 1))
    return list(dict.fromkeys(out))


def step(msg):
    print(f"· {msg}…", file=sys.stderr, flush=True)


def run(cmd, cwd=None, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace", env=GIT_ENV,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git(repo, *args):
    return run(["git", "-C", str(repo), *args])


def du(path):
    out = run(["du", "-sk", str(path)], timeout=600)
    try:
        return int(out.split()[0]) * 1024
    except (AttributeError, IndexError, ValueError):
        return 0


def measure(paths):
    paths = list(dict.fromkeys(map(str, paths)))
    with ThreadPoolExecutor(8) as pool:
        return dict(zip(paths, pool.map(du, paths)))


def touched_since(path, ts, skip=()):
    """The first file under path modified after ts (.git and `skip` left out), or None."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    prune = []
    for s in [os.path.join(str(path), ".git"), *map(str, skip)]:
        prune += ["-path", s, "-prune", "-o"]
    r = subprocess.run(["find", str(path), *prune, "-newermt", stamp, "-print", "-quit"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode and not r.stdout:
        return f"(find failed: {r.stderr.strip()[-80:]})"  # cannot tell: treat as touched
    return r.stdout.strip() or None


def disk():
    u = shutil.disk_usage(str(HOME))
    return u.total, u.free


# ---------------------------------------------------------------- build output in checkouts

def find_checkouts(roots, depth=4):
    """Git checkouts under roots, at most `depth` folders down; a checkout's own subfolders are not walked."""
    found = []

    def walk(d, level):
        listing = entries(d)
        if any(e.name == ".git" for e in listing):
            found.append(Path(d))
            return
        for e in listing if level < depth else []:
            if (e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in BUILD_DIRS
                    and not e.name.endswith(BUNDLES) and not inside(e.path, PRIVATE)):
                walk(e.path, level + 1)

    for r in roots:
        walk(str(r), 0)
    return found


def repo_last(repo):
    """When someone last worked here: index, HEAD, the reflog's own time, the top-level entries (.git left out)."""
    gitdir = git(repo, "rev-parse", "--absolute-git-dir")
    if gitdir is None:
        return time.time()  # cannot tell: treat as in use
    reflog = re.search(r"\{(\d+)\}", git(repo, "reflog", "-1", "--date=unix", "--format=%gd") or "")
    top = [mtime(e.path) for e in entries(repo) if e.name not in (".git", ".DS_Store")]
    return max(mtime(Path(gitdir) / "index"), mtime(Path(gitdir) / "HEAD"),
               float(reflog.group(1) if reflog else 0), *top)


def build_outputs(repo):
    """Ignored folders of a checkout named like build output, at any depth, with no tracked file inside."""
    out = git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory")
    found = []
    for e in (out or "").splitlines():
        q = Path(repo) / e.rstrip("/")
        if (e.endswith("/") and q.name in BUILD_DIRS and plain(q) and not kept(q)
                and git(repo, "ls-files", "--", e.rstrip("/")) == ""):
            found.append(str(q))
    return found


def deletable_outputs(item):
    """The plan's folders that are still ignored build output, with nothing tracked inside."""
    current = set(build_outputs(item["path"]))
    return [q for q in item["paths"] if q in current]


def checkout_item(repo, days, cwd):
    if kept(repo) or not plain(repo) or inside(cwd, [repo]):
        return None
    outs = build_outputs(repo)
    if not outs:
        return None
    cutoff = time.time() - days * DAY
    last = repo_last(repo)
    if last >= cutoff or touched_since(repo, cutoff, skip=outs):
        return "active"
    return {"cat": "checkout-build", "path": str(repo), "kind": "dirs", "paths": outs, "since": cutoff, "auto": True,
            "measure": outs, "reason": f"{', '.join(os.path.relpath(q, repo) for q in outs)} · untouched for "
                                       f"{idle_days(last):.0f} days · the next build or install brings them back"}


# ---------------------------------------------------------------- Xcode, Gradle, packages, caches

def derived_info(d):
    """The project a DerivedData folder belongs to, and when Xcode last used it."""
    try:
        info = plistlib.loads((Path(d) / "info.plist").read_bytes())
    except (OSError, ValueError):
        info = {}
    last = info.get("LastAccessedDate")
    return info.get("WorkspacePath"), (calendar.timegm(last.utctimetuple()) if hasattr(last, "utctimetuple")
                                       else mtime(d))


def entries(root):
    """The entries of a folder by name; none when it is missing or macOS refuses it."""
    try:
        return sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return []


def subdirs(root):
    return [Path(e.path) for e in entries(root) if e.is_dir(follow_symlinks=False)]


def dir_item(cat, p, reason, **extra):
    return {"cat": cat, "path": str(p), "kind": "dir", "mtime": mtime(p), "auto": True, "measure": [str(p)],
            "reason": reason, **extra}


def cmd_item(cat, tool, measured, reason, auto=True, size=None):
    item = {"cat": cat, "path": " ".join(COMMANDS[tool]), "kind": "cmd", "tool": tool, "auto": auto,
            "measure": [str(p) for p in measured], "reason": reason}
    if size is not None:
        item["size"] = size
    return item


def xcode_items(derived_days):
    for d in subdirs(DERIVED):
        ws, last = derived_info(d)
        if ws and not Path(ws).exists():
            why = "its project is gone"
        elif idle_days(last) >= derived_days:
            why = f"unused for {idle_days(last):.0f} days"
        else:
            continue
        yield dir_item("xcode", d, f"DerivedData · {why} · the next build makes it again", last_access=last)
    for kind in sorted(XCODE.glob("*DeviceSupport")) if XCODE.is_dir() else []:
        for d in sorted(subdirs(kind), key=mtime, reverse=True)[1:]:  # the newest is the device in use
            yield dir_item("xcode", d, f"{kind.name} · older OS · Xcode copies it again when such a device connects")
    try:
        r = subprocess.run(["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True, timeout=30)
        devices = json.loads(r.stdout).get("devices", {})
    except (OSError, subprocess.TimeoutExpired, ValueError):
        devices = {}
    sims = [SIM_DEVICES / d["udid"] for devs in devices.values() for d in devs if not d.get("isAvailable", True)]
    if sims:
        yield cmd_item("xcode", "simctl", sims, f"{len(sims)} simulators whose runtime is no longer installed")


def gradle_items(checkouts, days):
    used = set()
    for repo in checkouts:
        for p in [repo / "gradle/wrapper/gradle-wrapper.properties",
                  *repo.glob("*/gradle/wrapper/gradle-wrapper.properties")]:
            m = WRAPPER.search(p.read_text(errors="ignore")) if p.is_file() else None
            if m:
                used.add(m.group(1))
    if not used:  # nothing to compare with: touch nothing
        return
    for d in subdirs(GRADLE / "wrapper/dists"):
        v = re.sub(r"^gradle-|-(bin|all)$", "", d.name)
        if d.name.startswith("gradle-") and v not in used:
            yield dir_item("gradle", d, f"Gradle {v} · no scanned project uses it · downloaded again if needed")
    for parent in ("caches", "daemon"):
        for d in subdirs(GRADLE / parent):
            if d.name[:1].isdigit() and d.name not in used:
                yield dir_item("gradle", d, f"{parent} of Gradle {d.name} · no scanned project uses it")
    cache = GRADLE / "caches/build-cache-1"
    if cache.is_dir() and idle_days(mtime(cache)) >= days:
        yield dir_item("gradle", cache, "Gradle build cache · made again by the next builds")


def parse_docker(out):
    """{type: reclaimable bytes} from `docker system df --format '{{.Type}}|{{.Reclaimable}}'`."""
    units = {"": 1, "k": 10 ** 3, "m": 10 ** 6, "g": 10 ** 9, "t": 10 ** 12}  # Docker counts in powers of 1000
    sizes = {}
    for ln in (out or "").splitlines():
        kind, _, rest = ln.partition("|")
        m = re.match(r"([\d.]+)\s*([kKMGT]?)B", rest.strip())
        if m:
            sizes[kind] = int(float(m.group(1)) * units[m.group(2).lower()])
    return sizes


def docker_reclaimable():
    return parse_docker(run(["docker", "system", "df", "--format", "{{.Type}}|{{.Reclaimable}}"], timeout=20))


def package_items():
    for tool, (cmd, rels, what) in TOOLS.items():
        dirs = [HOME / r for r in rels if (HOME / r).is_dir() and plain(HOME / r)]
        if dirs and shutil.which(cmd[0]):
            yield cmd_item("packages", tool, dirs, f"{what} · downloaded again when needed")
    if shutil.which("docker"):
        sizes = docker_reclaimable()
        for tool, (_, kind, what) in DOCKER.items():
            if sizes.get(kind):
                yield cmd_item("packages", tool, [], f"{what} · pulled or built again when needed · ⚠ only by number",
                               auto=False, size=sizes[kind])


def cache_entries():
    """Top-level entries of ~/Library/Caches the cleanup may touch: Apple's own are left out (system managed)
    except Xcode's com.apple.dt.*, and so are CACHE_SKIP."""
    return [Path(e.path) for e in entries(CACHES)
            if not e.is_symlink() and e.name not in CACHE_SKIP
            and (not e.name.startswith("com.apple.") or e.name.startswith("com.apple.dt."))]


def cache_items(hours):
    entries = [p for p in cache_entries() if not kept(p)]
    with ThreadPoolExecutor(8) as pool:
        touched = list(pool.map(lambda p: touched_since(p, time.time() - hours * 3600), entries))
    for p, t in zip(entries, touched):
        if not t:  # a cache in use stays whole: half of a live cache is worse than all of it
            yield {**dir_item("cache", p, f"unused for {hours} h · the app makes it again"), "hours": hours,
                   "kind": "dir" if p.is_dir() else "file"}


def look_items():
    for root in LOOSE:
        for e in entries(root):
            if not e.is_symlink() and not kept(e.path):
                kind = "dir" if e.is_dir() else "file"
                yield {"cat": "look", "path": e.path, "kind": kind, "mtime": mtime(e.path), "auto": False,
                       "measure": [e.path], "min": BIG,
                       "reason": f"{kind} · modified {idle_days(mtime(e.path)):.0f} days ago"}
    for a in sorted(ARCHIVES.glob("*/*.xcarchive")) if ARCHIVES.is_dir() else []:
        if plain(a) and not kept(a):
            yield {**dir_item("look", a, "Xcode archive · needed to symbolicate that build's crashes: keep the ones "
                                         "users still run"), "auto": False, "min": 100 << 20}


# ---------------------------------------------------------------- scan

def local_snapshots():
    out = run(["tmutil", "listlocalsnapshots", "/"], timeout=20)
    return sum(1 for ln in (out or "").splitlines() if "com.apple.TimeMachine" in ln)


def last_run():
    try:
        s = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return "never"
    return f"{idle_days(s['at']):.0f} days ago (freed {human(s['freed'])})"


def number(items):
    """Sizes filled in, small ones dropped, ids assigned section by section, biggest first."""
    sizes = measure(p for i in items if "size" not in i for p in i["measure"])
    for i in items:
        i.setdefault("size", sum(sizes.get(p, 0) for p in i["measure"]))
    items = [i for i in items if i["size"] >= i.get("min", MIN_SIZE)]
    ordered = [i for cat, _ in SECTIONS for i in sorted((x for x in items if x["cat"] == cat), key=lambda x: -x["size"])]
    return [{"id": n, **i} for n, i in enumerate(ordered, 1)]


def save_plan(path, items, args):
    Path(path).write_text(json.dumps({"created": time.time(), "args": vars(args), "items": items}, indent=1))


def scan(args):
    roots = [Path(os.path.expanduser(r)).resolve() for r in args.roots.split(",")] if args.roots else \
        [HOME / r for r in DEFAULT_ROOTS if (HOME / r).is_dir()]
    roots = [r for r in roots if r.is_dir() and strictly_inside(r, [HOME]) and not inside(r, PRIVATE)]
    cwd = os.path.realpath(os.getcwd())
    step("git checkouts under " + ", ".join(short(r) for r in roots))
    checkouts = find_checkouts(roots)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda r: checkout_item(r, args.days, cwd), checkouts))
    items = [r for r in results if isinstance(r, dict)]
    active = sum(1 for r in results if r == "active")
    step("Xcode, Gradle, package managers, caches")
    items += [*xcode_items(args.derived_days), *gradle_items(checkouts, args.days), *package_items(),
              *cache_items(args.cache_hours), *look_items()]
    step("measuring (a minute or two)")
    items = number(items)
    info = measure(HOME / p for p in INFO if (HOME / p).is_dir())
    save_plan(args.plan, items, args)

    total, free = disk()
    print(f"disk-cleanup · {time.strftime('%Y-%m-%d')} · {human(free)} free of {human(total)} · last cleanup: {last_run()}")
    snaps = local_snapshots()
    if snaps:
        print(f"⚠ {snaps} Time Machine local snapshots: deleted files free their space only when those expire")
    print(f"{len(checkouts)} checkouts scanned, {active} in use (touched in the last {args.days} days) left alone")
    for cat, title in SECTIONS:
        rows = [i for i in items if i["cat"] == cat]
        print(f"\n{title.format(**vars(args))} — {len(rows)} items, {human(sum(i['size'] for i in rows))}")
        for i in rows:
            print(f"  [{i['id']:>3}] {human(i['size']):>9}  {short(i['path'])} — {i['reason']}")
    heavy = sorted(((p, n) for p, n in info.items() if n >= BIG), key=lambda x: -x[1])
    if heavy:
        print("\nINFO · other heavy folders, measured only")
        for p, n in heavy:
            print(f"        {human(n):>9}  {short(p)}")
    safe = [i for i in items if i["auto"]]
    print(f"\n--all-safe frees ~{human(sum(i['size'] for i in safe))} ({len(safe)} items)")
    print(f"plan: {args.plan}")


# ---------------------------------------------------------------- clean

def fenced(item):
    """Each category deletes only inside its own fence, whatever the plan file says."""
    p, cat, kind = item["path"], item["cat"], item["kind"]
    if kind == "cmd":
        return item.get("tool") in ALLOWED_COMMANDS.get(cat, ())
    if not strictly_inside(p, [HOME]) or inside(p, PRIVATE):
        return False
    path = Path(p)
    parent = path.parent
    if cat == "checkout-build":
        return (kind == "dirs" and (path / ".git").exists() and bool(item["paths"])
                and all(strictly_inside(q, [p]) and Path(q).name in BUILD_DIRS for q in item["paths"]))
    if cat == "xcode":
        return kind == "dir" and (parent == DERIVED or parent.parent == XCODE and parent.name.endswith("DeviceSupport"))
    if cat == "gradle":
        return kind == "dir" and (parent == GRADLE / "wrapper/dists" and path.name.startswith("gradle-")
                                  or parent in (GRADLE / "caches", GRADLE / "daemon") and path.name[:1].isdigit()
                                  or path == GRADLE / "caches/build-cache-1")
    if cat == "cache":
        return parent == CACHES and path in set(cache_entries())
    if cat == "look":
        return parent in LOOSE or path.suffix == ".xcarchive" and parent.parent == ARCHIVES
    return False


_PROCESSES = None


def processes():
    """Every path a running process has open (cwd included) and every command line; None when lsof or ps cannot
    run (the Claude Code sandbox refuses ps). Kept for 20 s: one lsof over the system takes a second or two."""
    global _PROCESSES
    if _PROCESSES and time.time() - _PROCESSES[0] < 20:
        return _PROCESSES[1]
    files = run(["lsof", "-w", "-Fn"], timeout=120)
    ps = run(["ps", "-axww", "-o", "command="])
    if not ps:
        return None
    _PROCESSES = (time.time(), "\n".join([ln[1:] for ln in (files or "").splitlines() if ln.startswith("n/")]
                                        + ps.splitlines()))
    return _PROCESSES[1]


def busy_now(p):
    blob = processes()
    if blob is None:
        return "cannot read the running processes (run clean outside the sandbox)"
    return "a running process uses it" if re.search(re.escape(str(p)) + r"(?=/|\s|$)", blob) else None


def recheck(item):
    """Why this item must not be deleted now, or None."""
    if not fenced(item):
        return "outside its category's fence"
    if item["kind"] == "cmd":
        return None if shutil.which(COMMANDS[item["tool"]][0]) else "command not found"
    p = Path(item["path"])
    if not os.path.lexists(p):
        return "gone already"
    if not plain(p):
        return "the path contains a symlink"
    if kept(p):
        return f"protected by {KEEP}"
    inner = [q for q in (item["paths"] if item["kind"] == "dirs" else [str(p)]) if keep_below(q)]
    if inner:
        return f"a {KEEP} file inside {short(inner[0])}"
    if item["kind"] == "file" and not p.is_file() or item["kind"] in ("dir", "dirs") and not p.is_dir():
        return "changed type after the scan"
    if "mtime" in item and abs(mtime(p) - item["mtime"]) > 1:
        return "modified after the scan"
    if item["cat"] == "cache":
        t = touched_since(p, time.time() - item["hours"] * 3600)
        return f"used recently ({short(t)})" if t else None
    if "last_access" in item and derived_info(p)[1] > item["last_access"] + 1:
        return "used by Xcode after the scan"
    if item["cat"] in ("xcode", "gradle"):  # a build or a Gradle daemon running from it
        return busy_now(p)
    if item["cat"] == "checkout-build":
        if repo_last(p) >= item["since"]:
            return "the checkout was used after the scan"
        t = touched_since(p, item["since"], skip=item["paths"])
        if t:
            return f"touched recently ({short(t)})"
        return busy_now(p) or (None if deletable_outputs(item) else "nothing left to delete")
    return None


def remove(p):
    def force(func, path, _):
        parent = os.path.dirname(path)
        if parent != str(p) and not parent.startswith(str(p) + os.sep):
            return  # never change the permissions of a folder outside the one being deleted
        try:
            os.chmod(os.path.dirname(path), 0o700)
            func(path)
        except OSError:
            pass

    error = ""
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, onerror=force)
        else:
            os.remove(p)
    except OSError as e:
        error = str(e)
    return f"not deleted {error}".strip() if os.path.lexists(p) else None


def delete(item):
    if item["kind"] == "cmd":
        try:
            r = subprocess.run(COMMANDS[item["tool"]], capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)
        return (r.stderr.strip() or "failed")[-300:] if r.returncode else None
    if item["kind"] == "dirs":
        left = [f"{short(q)}: {err}" for q in deletable_outputs(item) for err in [remove(q)] if err]
        return "; ".join(left[:3]) or None
    return remove(item["path"])


def clean(args):
    try:
        plan = json.loads(Path(args.plan).read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read the plan ({e}): scan again")
    if time.time() - plan["created"] > PLAN_TTL:
        sys.exit("the plan is older than 24 hours: scan again")
    items = plan["items"]
    if args.all_safe:
        chosen = [i for i in items if i["auto"]]
    elif args.cat:
        cats = set(args.cat.split(","))
        chosen = [i for i in items if i["cat"] in cats and i["auto"]]
    else:
        by_id = {i["id"]: i for i in items}
        wanted = parse_ids(args.ids)
        missing = [n for n in wanted if n not in by_id]
        if missing:
            sys.exit(f"not in the plan: {missing}")
        chosen = [by_id[n] for n in wanted]
    free_before, denied = disk()[1], False
    for item in sorted(chosen, key=lambda i: i["kind"] == "cmd"):  # commands last
        label = f"[{item['id']}] {short(item['path'])}"
        why = recheck(item)
        if why:
            print(f"  – skipped {label}: {why}")
        elif args.dry_run:
            print(f"  · would delete {label} ({human(item['size'])})")
        else:
            err = delete(item)
            denied |= bool(err and "not permitted" in err)
            print(f"  ✗ {label}: {err}" if err else f"  ✓ {label} ({human(item['size'])})")
    if args.dry_run:
        return
    total, free = disk()
    freed = max(0, free - free_before)
    print(f"\nfreed {human(freed)} · now {human(free)} free of {human(total)}")
    if denied:
        print("'Operation not permitted': run clean outside the sandbox.")
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"at": time.time(), "freed": freed}))
    except OSError:
        pass


def at_least_one(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="measure and write the plan; deletes nothing")
    s.add_argument("--roots", help="comma-separated folders under your home holding your checkouts (default: " +
                                   ", ".join("~/" + r for r in DEFAULT_ROOTS) + ", the ones that exist)")
    s.add_argument("--days", type=at_least_one, default=14, help="days a checkout must be untouched")
    s.add_argument("--derived-days", type=at_least_one, default=7, help="days a DerivedData folder must be unused")
    s.add_argument("--cache-hours", type=at_least_one, default=24, help="hours a ~/Library/Caches entry must be unused")
    s.add_argument("--plan", default=str(Path(os.environ.get("TMPDIR", "/tmp")) / "disk-cleanup-plan.json"))
    c = sub.add_parser("clean", help="delete items of the plan, re-checking each one")
    c.add_argument("--plan", required=True)
    pick = c.add_mutually_exclusive_group(required=True)
    pick.add_argument("--ids", help="e.g. 1,4,7-12: the only way to delete TO LOOK AT and ⚠ items")
    pick.add_argument("--all-safe", action="store_true", help="every safe item")
    pick.add_argument("--cat", help="checkout-build,xcode,gradle,packages,cache (their safe items)")
    c.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    {"scan": scan, "clean": clean}[args.command](args)


if __name__ == "__main__":
    main()
