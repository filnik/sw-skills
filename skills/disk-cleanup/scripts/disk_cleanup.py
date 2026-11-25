#!/usr/bin/env python3
"""disk-cleanup: find what fills a developer's disk and free it without losing work.

    disk_cleanup.py scan [--roots DIR,DIR] [--days 14] [--derived-days 7] [--cache-hours 24] [--plan FILE] […]
    disk_cleanup.py clean --plan FILE (--all-safe | --cat CAT,CAT | --ids 1,4,7-12) [--dry-run]
    disk_cleanup.py nightly [--idle-hours 8] [--dry-run]

scan only reads: it measures, writes a numbered plan and prints the report. clean deletes only what was picked and
re-checks each item right before: anything that changed since the scan is skipped, with the reason. nightly runs
unattended (launchd, cron): with no plan and nobody to ask, it deletes only regenerable output - the build folders of
idle pool checkouts, DerivedData, the configured device leftovers - and Kap's old temp recordings, each re-checked
like in clean.
Categories: recordings, checkouts, checkout-build, xcode, gradle, devices, packages, cache (in --all-safe unless
marked ⚠) and look (only by --ids).
A folder holding a .disk-cleanup-keep file is never proposed nor deleted, nor is anything under it; when the file
lists names (patterns, one per line, # for comments), only those are protected. Nothing behind a symlink is deleted.
Never reads ~/Library/Containers, ~/Library/Group Containers or ~/Pictures (macOS asks for permission there).

Config: the "disk_cleanup" block of .claude/config/project.json, the first one found from the current folder upward
that has it; with none (the nightly run starts in /), the one the last run found. All keys optional:
    {"disk_cleanup": {
      "pools": [{"root": "~/work", "checkouts": ["app-[0-9]*", "ws-*/*"], "claims": ".pick/claims.json",
                 "build_dirs": ["build", ".gradle"]}],
      "video_dirs": [{"path": "~/work/product", "skip": ["assets"]}],
      "signed_apps": "~/.cache/signed-apps", "phone_db_copies": "~/.cache/phones/*/db",
      "measure": ["~/work/.app.git"], "defaults": {"derived_days": 2}}}
pools: folders of checkouts a script creates and reuses (glob patterns under root). An idle clean one is offered
whole; its build output goes sooner than other checkouts' (not touched today; 7 days with uncommitted changes).
claims: a JSON file {"<checkout path>": {"pid": N}}; a live pid reserves that checkout. build_dirs: the only build
folder names its checkouts lose (default: every name in BUILD_DIRS).
video_dirs: videos there are offered by number only, `skip` names subfolders left out.
signed_apps: one folder per signed app copy, named like its checkout or holding a `checkout` file with its path.
phone_db_copies: folders of DB copies (one folder per copy): all but the newest go.
measure: more folders measured in INFO. defaults: option defaults, e.g. {"derived_days": 2, "days": 20}.
Stdlib only, Python 3.9+.
"""
import argparse
import calendar
import datetime
import fnmatch
import functools
import glob
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))
import project_config  # noqa: E402

HOME = Path.home()
LIB = HOME / "Library"
XCODE = LIB / "Developer/Xcode"
DERIVED = XCODE / "DerivedData"
ARCHIVES = XCODE / "Archives"
SIM_DEVICES = LIB / "Developer/CoreSimulator/Devices"
CACHES = LIB / "Caches"
GRADLE = HOME / ".gradle"
MOVIES = HOME / "Movies"  # OBS records here: every video in it counts as a recording
LOOSE = [HOME / "Downloads", HOME / "Desktop"]
DOCUMENTS = HOME / "Documents"  # only its top-level files are offered
# Reading these makes macOS ask "… would like to access data from other apps" every time: never read nor measured.
PRIVATE = [LIB / "Containers", LIB / "Group Containers", HOME / "Pictures"]
DEFAULT_ROOTS = ["Documents", "Developer", "Projects", "code", "src", "dev"]
STATE = HOME / ".cache/disk-cleanup/last-run.json"
NIGHTLY = HOME / ".cache/disk-cleanup/last-nightly.json"
REMEMBERED = HOME / ".cache/disk-cleanup/config-path"
KEEP = ".disk-cleanup-keep"
INFO = [".android/avd", "Library/Android/sdk/system-images", "Android/Sdk/system-images", ".cache", ".gradle",
        ".konan", ".npm", ".colima", ".codex", "Library/Developer/CoreSimulator", "Library/Developer/Xcode/DerivedData",
        "Library/Developer/Xcode/Archives"]
# Ignored folders a build or an install brings back: the only thing deleted inside a checkout.
BUILD_DIRS = {"node_modules", "build", "dist", ".next", ".nuxt", ".turbo", "target", ".gradle", ".kotlin",
              ".dart_tool", ".venv", "venv", "__pycache__", ".parcel-cache", "Pods", "DerivedData"}
BUNDLES = (".app", ".photoslibrary", ".imovielibrary", ".fcpbundle", ".tvlibrary", ".musiclibrary", ".xcarchive")
WALK_SKIP = {"node_modules", ".git", "build", ".gradle", "Pods", "DerivedData", ".venv", "venv"}
# Kap records into the per-user temp dir under a random 32-hex name and exports ("copy to clipboard") into a 32-hex
# folder as "Kapture <date> at <time>.mp4", named after the second the recording ended.
KAP_RAW = re.compile(r"^[0-9a-f]{32}\.mp4$")
KAP_DIR = re.compile(r"^[0-9a-f]{32}$")
KAP_EXPORT = re.compile(r"^Kapture (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})\.\w+$")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".flv", ".gif", ".webm"}
RECORDING_EXT = VIDEO_EXT - {".gif"}  # a gif next to documents is usually embedded in one
# OBS's default "%CCYY-%MM-%DD %hh-%mm-%ss", the whole name: "2026-10-22 13-08-02 - edit.mp4" is someone's edit
OBS_NAME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.\w+$")
# Ignored entries a fresh clone plus a build brings back. Anything else ignored keeps a whole checkout out of --all-safe.
REGEN = {
    "node_modules", "build", "dist", "out", "target", ".gradle", ".kotlin", ".idea", ".vscode", "Pods",
    "DerivedData", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".coverage", ".jest-cache", ".next", "coverage", ".cxx", ".build", ".swiftpm",
    "xcuserdata", ".cc-writes", ".DS_Store", ".terraform", ".tox", ".cache",
}  # local.properties is judged by its content: a project may keep hand-written credentials there
REGEN_FILE = re.compile(r"\.(log|iml|pyc)$|\.log\.[\w.-]+$")
NETWORK_URL = re.compile(r"^((https?|ssh|git)://|[\w.-]+@[\w.-]+:)")
SPOTLIGHT = {"mdworker_shared", "mdworker", "mds_stores", "mds"}  # Spotlight reading a folder is not someone working
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
    ("recordings", "RECORDINGS · Kapture videos, videos in ~/Movies (OBS) and in the video folders of the config, "
                   "older than {recording_days} days; Kap's temp recordings after {kap_days} days "
                   "({kap_exported_days} once exported)"),
    ("checkouts", "CHECKOUTS · pool checkouts clean and idle for {pool_days} days, deleted whole"),
    ("checkout-build", "BUILD IN IDLE CHECKOUTS · ignored build output of pool checkouts not touched {when} (with "
                       "uncommitted changes: {dirty_checkout_days} days) and of other git checkouts untouched for "
                       "{days} days (sources and .git never touched)"),
    ("xcode", "XCODE · DerivedData orphaned or unused for {derived_days} days, old device symbols, "
              "unavailable simulators"),
    ("gradle", "GRADLE · versions no scanned project uses, build cache idle for {days} days"),
    ("devices", "DEVICES · signed app copies whose checkout is gone or idle for {days} days, phone DB copies "
                "but the newest"),
    ("packages", "PACKAGES · package-manager caches, emptied by the tool's own command"),
    ("cache", "CACHES · ~/Library/Caches entries unused for {cache_hours} h (Apple's and installs left out)"),
    ("look", "TO LOOK AT · never in --all-safe, only by number"),
]
BIG, MIN_SIZE, DAY, PLAN_TTL = 1 << 30, 1 << 20, 86400, 86400
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}  # status must not rewrite the index
CFG = {}  # the disk_cleanup block in use
CONFIG_PATH = None


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


def fsize(p):
    try:
        return os.lstat(p).st_size
    except OSError:
        return 0


def idle_days(ts):
    return (time.time() - ts) / DAY


def midnight(days_back=0):
    return time.mktime((datetime.date.today() - datetime.timedelta(days=days_back)).timetuple())


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


# ---------------------------------------------------------------- config

def read_block(path):
    """The disk_cleanup block of a config file, or {}."""
    try:
        data = json.loads(Path(path).read_text())
    except (TypeError, OSError, ValueError):
        return {}
    block = data.get("disk_cleanup") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def load_config():
    """(config file, disk_cleanup block): the first config from the current folder upward that has the block,
    else the one an earlier run found (the nightly run starts in /, where there is none)."""
    path, cfg = project_config.find()
    block = cfg.get("disk_cleanup")
    if path and isinstance(block, dict):
        try:
            REMEMBERED.parent.mkdir(parents=True, exist_ok=True)
            REMEMBERED.write_text(str(path))
        except OSError:
            pass
        return path, block
    try:
        path = Path(REMEMBERED.read_text().strip())
    except OSError:
        return None, {}
    block = read_block(path)
    return (path, block) if block else (None, {})


def configure(path, block):
    global CFG, CONFIG_PATH
    CFG, CONFIG_PATH = (block if isinstance(block, dict) else {}), path


def expand(p):
    return Path(os.path.expanduser(str(p)))


def pools():
    """(root, checkout patterns, claims file or None) of each configured pool."""
    out = []
    for pool in CFG.get("pools", []):
        root = expand(pool["root"])
        claims = pool.get("claims")
        out.append((root, pool.get("checkouts", []), root / claims if claims else None))
    return out


def pool_build_dirs(d):
    """The build folder names a pool checkout may lose: its pool's "build_dirs", or every BUILD_DIRS name."""
    for pool in CFG.get("pools", []):
        if strictly_inside(d, [expand(pool["root"])]):
            return set(pool.get("build_dirs", BUILD_DIRS))
    return BUILD_DIRS


def pool_checkouts():
    """The configured pools' checkout folders, real folders with no symlink on the way."""
    found = []
    for root, patterns, _ in pools():
        for pattern in patterns:
            for d in sorted(root.glob(pattern)):
                if d.is_dir() and plain(d) and d not in found:
                    found.append(d)
    return found


def video_dirs():
    """(folder, names of subfolders left out) for each configured video folder."""
    out = []
    for v in CFG.get("video_dirs", []):
        path, skip = (v, []) if isinstance(v, str) else (v["path"], v.get("skip", []))
        out.append((expand(path), set(skip)))
    return out


def signed_apps_root():
    v = CFG.get("signed_apps")
    return expand(v) if v else None


def db_copy_dirs():
    v = CFG.get("phone_db_copies")
    return [Path(p) for p in sorted(glob.glob(os.path.expanduser(v)))] if v else []


def measured():
    return [Path(p) for g in CFG.get("measure", []) for p in sorted(glob.glob(os.path.expanduser(g)))]


# ---------------------------------------------------------------- recordings (Kapture, OBS, Kap's temp files)

def is_video(name):
    return Path(name).suffix.lower() in VIDEO_EXT


def is_recording(name):
    return Path(name).suffix.lower() in RECORDING_EXT


def is_kapture(name):
    return name.lower().startswith("kapture") and is_video(name)


def tracked(f):
    return git(f.parent, "ls-files", "--error-unmatch", f.name) is not None


def recording_roots():
    return [*LOOSE, MOVIES, *(v for v, _ in video_dirs())]


def recording_files():
    """Kapture videos in the recording roots, and every recording in ~/Movies and in the video folders."""
    skips = dict(video_dirs())
    seen = set()
    for root in recording_roots():
        if not root.is_dir() or inside(root, PRIVATE):
            continue
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = [d for d in dirnames if d not in WALK_SKIP and not d.endswith(BUNDLES)
                           and not (here == MOVIES and d == "TV") and d not in skips.get(here, ())]
            if len(here.parts) - base >= 4:
                dirnames[:] = []
            for f in filenames:
                p = here / f
                if p not in seen and (is_kapture(f) or (root == MOVIES or root in skips) and is_recording(f)):
                    seen.add(p)
                    yield p


@functools.lru_cache(maxsize=1)
def user_temp():
    """The per-user temp dir Kap works in ($TMPDIR can be another one, inside a sandbox)."""
    try:
        out = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
    return Path(os.path.realpath(out)) if out else None


def kap_export_files(d):
    """The Kapture files of a Kap export folder, or [] when it holds anything else."""
    try:
        files = [f for f in d.iterdir() if f.name != ".DS_Store"]  # Finder leaves one when the folder is opened
    except OSError:
        return []
    ok = files and all(KAP_EXPORT.match(f.name) and f.is_file() and not f.is_symlink() for f in files)
    return files if ok else []


def kap_temp(days, exported_days):
    """Kap's leftovers in the user temp dir as (path, kind, size, reason): export folders idle for exported_days,
    raw recordings idle for exported_days once exported, for days otherwise."""
    tmp = user_temp()
    found = sorted(tmp.iterdir()) if tmp and tmp.is_dir() else []
    out, ended = [], []
    for d in found:
        files = kap_export_files(d) if KAP_DIR.match(d.name) and d.is_dir() and not d.is_symlink() else []
        if not files:
            continue
        # local wall-clock fields, compared as such: mktime would guess wrong in the repeated DST hour
        ended += [tuple(map(int, KAP_EXPORT.match(f.name).groups())) for f in files]
        age = idle_days(max(mtime(p) for p in [d, *files]))
        if age >= exported_days:
            out.append((d, "dir", sum(fsize(f) for f in files), f"Kap export · {age:.0f} days"))
    for f in found:
        if KAP_RAW.match(f.name) and f.is_file() and not f.is_symlink():
            t = int(mtime(f))
            age, done = idle_days(t), any(tuple(time.localtime(t + k))[:6] in ended for k in range(-2, 3))
            if age >= (exported_days if done else days):
                out.append((f, "file", fsize(f), f"Kap raw recording · {age:.0f} days · "
                            + ("exported" if done else "never exported")))
    return out


def recording_items(args):
    """The recordings to offer, and the ones a keep file protects."""
    items, protected = [], []
    vids = [v for v, _ in video_dirs()]
    for f in recording_files():
        try:
            st = f.lstat()
        except OSError:
            continue
        age = idle_days(st.st_mtime)
        if age < args.recording_days or not plain(f) or tracked(f):
            continue
        if kept(f):
            protected.append(f)
            continue
        done = any(f.with_suffix(s).exists() for s in (".srt", ".txt"))
        if inside(f, vids):
            kind, auto = "video folder", False
        elif is_kapture(f.name):
            kind, auto = "Kapture", True
        else:
            kind, auto = ("OBS", True) if OBS_NAME.match(f.name) else ("video in Movies", False)
        reason = f"{kind} · {age:.0f} days" + (" · transcribed" if done else "")
        if kind == "video folder":
            reason += " · ⚠ only by number: it may still be needed"
        elif not auto:
            reason += " · ⚠ not named like OBS's own recordings: look at it"
        items.append({"cat": "recordings", "path": str(f), "kind": "file", "size": st.st_size, "mtime": st.st_mtime,
                      "measure": [], "auto": auto, "reason": reason})
    for p, kind, size, reason in kap_temp(args.kap_days, args.kap_exported_days):
        if plain(p) and not kept(p):
            items.append({"cat": "recordings", "path": str(p), "kind": kind, "size": size, "mtime": mtime(p),
                          "measure": [], "auto": True, "reason": reason})
    return items, protected


# ---------------------------------------------------------------- pool checkouts

def repo_state(path):
    """What deleting this checkout would lose. None when it is not a git checkout."""
    path = Path(path)
    if not (path / ".git").exists():
        return None
    gitdir = git(path, "rev-parse", "--absolute-git-dir")
    if gitdir is None:
        return {"blockers": ["git does not answer"], "last": time.time(), "worktree": False,
                "main": str(path), "linked": [], "dirty": True, "status_ok": False}
    gitdir = Path(gitdir)
    worktree = (path / ".git").is_file()
    # .git itself is left out: an IDE's background fetches touch it without anyone working here
    top = [mtime(e.path) for e in entries(path) if e.name not in (".DS_Store", ".git")]
    # when the reflog entry was written (%gd with unix dates), not the date of the commit HEAD sits on
    reflog = re.search(r"\{(\d+)\}", git(path, "reflog", "-1", "--date=unix", "--format=%gd") or "")
    last = max(mtime(gitdir / "index"), mtime(gitdir / "HEAD"), float(reflog.group(1) if reflog else 0), *top)

    # Any git failure below is a blocker: "cannot tell" must never read as "nothing to lose".
    blockers = []
    status = git(path, "status", "--porcelain")
    if status is None:
        blockers.append("git status failed")
    elif status:
        blockers.append(f"{len(status.splitlines())} modified or untracked files")
    if git(path, "symbolic-ref", "-q", "HEAD") is None:
        # a detached HEAD's commits sit on no branch, in a worktree and in a main checkout alike
        n = git(path, "rev-list", "--count", "HEAD", "--not", "--branches", "--remotes")
        if n is None:
            blockers.append("cannot count the commits on the detached HEAD")
        elif int(n):
            blockers.append(f"{n} commits on a detached HEAD, saved nowhere else")
    linked = []
    if worktree:
        # Branches and stashes live in the main repo and survive.
        common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        # a worktree of a bare repo has the bare repo itself as common dir, not a .git inside a checkout
        main = (str(Path(common).parent) if Path(common).name == ".git" else common) if common else str(path)
    else:
        urls = [ln.split(" ", 1)[-1] for ln in (git(path, "config", "--get-regexp", r"^remote\..*\.url$") or "").splitlines()]
        local = [u for u in urls if not NETWORK_URL.match(u)]
        if local:
            # "already on a remote" means nothing when the remote is another folder on this disk
            blockers.append(f"remote on this disk ({local[0]})")
        n = git(path, "rev-list", "--count", "--branches", "--not", "--remotes")
        if n is None:
            blockers.append("cannot count the local commits")
        elif int(n):
            blockers.append(f"{n} commits only here")
        stash = git(path, "stash", "list")
        if stash is None:
            blockers.append("cannot read the stashes")
        elif stash:
            blockers.append(f"{len(stash.splitlines())} stashes")
        listing = git(path, "worktree", "list", "--porcelain")
        if listing is None:
            blockers.append("cannot list the worktrees")
        linked = [ln[9:] for ln in (listing or "").splitlines() if ln.startswith("worktree ")][1:]
        linked = [w for w in linked if Path(w).exists()]
        main = str(path)
    return {"blockers": blockers, "last": last, "worktree": worktree, "main": main, "linked": linked,
            "dirty": status is None or bool(status), "status_ok": status is not None}


def regenerable(entry):
    parts = [p for p in entry.rstrip("/").split("/") if p not in ("", ".")]
    return (any(p in REGEN for p in parts) or bool(parts and REGEN_FILE.search(parts[-1]))
            or entry.startswith(".dvc/tmp"))


def only_regenerable(d, limit=2000):
    seen = 0
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [x for x in dirnames if x not in REGEN]
        rel = os.path.relpath(dirpath, d)
        for f in filenames:
            seen += 1
            if seen > limit or not regenerable(os.path.join(rel, f)):
                return False
    return True


def sdk_only(f):
    """A local.properties the IDE writes by itself (sdk/ndk/cmake paths, comments); a hand-written one (debug
    credentials, say) is something no build brings back."""
    try:
        lines = [ln.strip() for ln in Path(f).read_text(errors="replace").splitlines()]
    except OSError:
        return False
    return all(not ln or ln.startswith(("#", "!", "sdk.dir", "ndk.dir", "cmake.dir")) for ln in lines)


def ignored_extras(path):
    """Ignored entries a delete would take with it and that no clone or build brings back
    (.env, keystores, a DVC cache, generated config…)."""
    out = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory")
    if out is None:
        return ["(git ls-files failed)"]
    return [e for e in out.splitlines()
            if not (sdk_only(Path(path) / e) if Path(e).name == "local.properties" else regenerable(e))
            and not (e.endswith("/") and only_regenerable(Path(path) / e))]


def pool_items(states, in_use, days, cwd):
    """Pool checkouts that are clean and idle for `days`, offered whole, and the ones kept with the reason."""
    kept_dirs, ok = [], {}
    for d, s in states.items():
        if s is None:
            continue
        if inside(cwd, [d]):
            kept_dirs.append((d, "the folder this session runs in"))
        elif kept(d):
            kept_dirs.append((d, f"protected by {KEEP}"))
        elif str(d) in in_use:
            kept_dirs.append((d, "a running process uses it, or a session claimed it"))
        elif s["blockers"]:
            kept_dirs.append((d, ", ".join(s["blockers"]) + f" · idle for {idle_days(s['last']):.0f} days"))
        elif idle_days(s["last"]) < days:
            kept_dirs.append((d, f"clean, but touched {idle_days(s['last']):.0f} days ago"))
        else:
            ok[str(d)] = (d, s)
    repos = []
    for key, (d, s) in ok.items():
        # a main checkout goes only together with every worktree that hangs off it
        alive = [w for w in s["linked"] if os.path.realpath(w) not in ok]
        if alive:
            kept_dirs.append((d, "has worktrees in use: " + ", ".join(Path(w).name for w in alive)))
            continue
        extras = ignored_extras(d)
        reason = f"{'worktree' if s['worktree'] else 'repo'} clean · idle for {idle_days(s['last']):.0f} days"
        if s["linked"]:
            reason += " · together with its worktrees"
        if extras:
            more = f" (+{len(extras) - 4})" if len(extras) > 4 else ""
            reason += " · ⚠ ignored files no build brings back: " + ", ".join(extras[:4]) + more
        repos.append({"cat": "checkouts", "path": key, "kind": "dir", "auto": not extras, "reason": reason,
                      "measure": [key], "days": days,
                      "repo": {"worktree": s["worktree"], "main": s["main"], "extras": extras}})
    return repos, kept_dirs


def pool_builds(states, doomed, cwd, since, since_dirty, clean_label, dirty_label, busy=()):
    """Build output of pool checkouts nobody touched after `since` (`since_dirty` for those with uncommitted
    changes), builds included, and not in `busy`. Only regenerable output goes, never a source file, so a checkout
    with uncommitted changes qualifies too: it just has to be idle for longer."""
    quiet = [(d, s["dirty"], limit) for d, s in states.items()
             if s and s["status_ok"] and str(d) not in doomed and not inside(cwd, [d]) and str(d) not in busy
             and not kept(d) for limit in [since_dirty if s["dirty"] else since] if s["last"] < limit]
    with ThreadPoolExecutor(8) as pool:
        checks = list(pool.map(lambda q: (touched_since(q[0], q[2]), [o for o in build_outputs(q[0])
                                                                      if Path(o).name in pool_build_dirs(q[0])]),
                               quiet))
    items = []
    for (d, dirty, limit), (touched, outs) in zip(quiet, checks):
        if outs and not touched:
            items.append({"cat": "checkout-build", "path": str(d), "kind": "dirs", "paths": outs, "measure": outs,
                          "since": limit, "pool": True, "dirty_ok": dirty, "auto": True,
                          "reason": f"{len(outs)} build folders · {dirty_label if dirty else clean_label}"})
    return items


# ---------------------------------------------------------------- build output in other checkouts

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


# ---------------------------------------------------------------- Xcode, Gradle, devices, packages, caches

def derived_info(d):
    """The project a DerivedData folder belongs to, and when Xcode last used it."""
    try:
        info = plistlib.loads((Path(d) / "info.plist").read_bytes())
    except (OSError, ValueError):
        info = {}
    last = info.get("LastAccessedDate")
    return info.get("WorkspacePath"), (calendar.timegm(last.utctimetuple()) if hasattr(last, "utctimetuple")
                                       else mtime(d))


def derived_items(derived_days, doomed=(), keep=(), doomed_why="its checkout is in the checkouts list"):
    """DerivedData whose project is gone, belongs to a doomed checkout or is unused; none of a checkout in `keep`."""
    for d in subdirs(DERIVED):
        if kept(d) or not plain(d):
            continue
        ws, last = derived_info(d)
        if ws and inside(os.path.realpath(ws), keep):
            continue
        if ws and not Path(ws).exists():
            why = "its project is gone"
        elif ws and inside(os.path.realpath(ws), doomed):
            why = doomed_why
        elif idle_days(last) >= derived_days:
            why = f"unused for {idle_days(last):.0f} days"
        else:
            continue
        yield dir_item("xcode", d, f"DerivedData · {why} · the next build makes it again", last_access=last)


def xcode_items(derived_days, doomed=(), keep=()):
    yield from derived_items(derived_days, doomed, keep)
    for kind in sorted(XCODE.glob("*DeviceSupport")) if XCODE.is_dir() else []:
        for d in sorted(subdirs(kind), key=mtime, reverse=True)[1:]:  # the newest is the device in use
            if not kept(d):
                yield dir_item("xcode", d,
                               f"{kind.name} · older OS · Xcode copies it again when such a device connects")
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
        if d.name.startswith("gradle-") and v not in used and not kept(d):
            yield dir_item("gradle", d, f"Gradle {v} · no scanned project uses it · downloaded again if needed")
    for parent in ("caches", "daemon"):
        for d in subdirs(GRADLE / parent):
            if d.name[:1].isdigit() and d.name not in used and not kept(d):
                yield dir_item("gradle", d, f"{parent} of Gradle {d.name} · no scanned project uses it")
    cache = GRADLE / "caches/build-cache-1"
    if cache.is_dir() and idle_days(mtime(cache)) >= days and not kept(cache):
        yield dir_item("gradle", cache, "Gradle build cache · made again by the next builds")


def app_checkout(d):
    """The checkout a signed app copy was made for, when the copy records it in a `checkout` file."""
    try:
        return Path((Path(d) / "checkout").read_text().strip())
    except OSError:
        return None


def signed_app_items(doomed, days, pool_dirs):
    """Signed app copies whose checkout is gone, in the checkouts list, or signed `days` ago."""
    root = signed_apps_root()
    for d in subdirs(root) if root else []:
        if kept(d) or not plain(d):
            continue
        top = app_checkout(d)
        guesses = [top] if top else [*(r / d.name for r, _, _ in pools()), *(c for c in pool_dirs if c.name == d.name)]
        homes = [os.path.realpath(p) for p in guesses if p.is_dir()]
        if not homes:
            why = "its checkout is gone"
        elif all(inside(h, doomed) for h in homes):
            why = "its checkout is in the checkouts list"
        elif idle_days(mtime(d)) >= days:
            why = f"signed {idle_days(mtime(d)):.0f} days ago"
        else:
            continue
        yield dir_item("devices", d, "signed app copy · " + why)


def orphan_apps(idle_hours):
    """Signed copies whose recorded checkout is gone, none signed in the last idle_hours. A copy that records no
    checkout is only named like one: those are left to scan, where someone looks."""
    root = signed_apps_root()
    for d in subdirs(root) if root else []:
        top = app_checkout(d)
        if top and not top.is_dir() and idle_days(mtime(d)) * 24 >= idle_hours and plain(d) and not kept(d):
            yield dir_item("devices", d, "signed app copy · its checkout is gone")


def db_copies():
    """Copies of the phones' DB but the newest in each folder, and none from the last hour (a test may read it)."""
    for db in db_copy_dirs():
        for p in sorted(subdirs(db))[:-1]:
            if time.time() - mtime(p) >= 3600 and plain(p) and not kept(p):
                yield dir_item("devices", p, "copy of a phone's DB · only the newest stays")


def cmd_item(cat, tool, measured_paths, reason, auto=True, size=None):
    item = {"cat": cat, "path": " ".join(COMMANDS[tool]), "kind": "cmd", "tool": tool, "auto": auto,
            "measure": [str(p) for p in measured_paths], "reason": reason}
    if size is not None:
        item["size"] = size
    return item


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
    found = [p for p in cache_entries() if not kept(p)]
    with ThreadPoolExecutor(8) as pool:
        touched = list(pool.map(lambda p: touched_since(p, time.time() - hours * 3600), found))
    for p, t in zip(found, touched):
        if not t:  # a cache in use stays whole: half of a live cache is worse than all of it
            yield {**dir_item("cache", p, f"unused for {hours} h · the app makes it again"), "hours": hours,
                   "kind": "dir" if p.is_dir() else "file"}


def look_items(listed=()):
    """Big files and folders at the top of Downloads and Desktop, big files at the top of Documents, Xcode archives."""
    found = [e for root in LOOSE for e in entries(root)]
    found += [e for e in entries(DOCUMENTS) if e.is_file(follow_symlinks=False)]
    for e in found:
        if e.path not in listed and not e.name.startswith(".") and not e.is_symlink() and not kept(e.path):
            kind = "dir" if e.is_dir() else "file"
            yield {"cat": "look", "path": e.path, "kind": kind, "mtime": mtime(e.path), "auto": False,
                   "measure": [e.path], "min": BIG,
                   "reason": f"{kind} · modified {idle_days(mtime(e.path)):.0f} days ago"}
    for a in sorted(ARCHIVES.glob("*/*.xcarchive")) if ARCHIVES.is_dir() else []:
        if plain(a) and not kept(a):
            yield {**dir_item("look", a, "Xcode archive · needed to symbolicate that build's crashes: keep the ones "
                                         "users still run"), "auto": False, "min": 100 << 20}


# ---------------------------------------------------------------- running processes and claims

_PROCESSES = None


def processes():
    """Every path a running process has open (cwd included) and every command line; None when lsof or ps cannot
    run (the Claude Code sandbox refuses ps). Kept for 20 s: one lsof over the system takes a second or two."""
    global _PROCESSES
    if _PROCESSES and time.time() - _PROCESSES[0] < 20:
        return _PROCESSES[1]
    try:
        files = subprocess.run(["lsof", "-w", "-Fcn"], capture_output=True, text=True, errors="replace",
                               timeout=120).stdout
        ps = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True, errors="replace",
                            timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if ps.returncode or not ps.stdout:
        return None
    paths, command = [], ""
    for ln in files.splitlines():
        if ln.startswith("c"):
            command = ln[1:]
        elif ln.startswith("n/") and command not in SPOTLIGHT:
            paths.append(ln[1:])
    _PROCESSES = (time.time(), "\n".join(paths + ps.stdout.splitlines()))
    return _PROCESSES[1]


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def claimed():
    """Checkouts a live claim in a pool's claims file reserves: a session works there."""
    live = set()
    for _, _, claims in pools():
        try:
            data = json.loads(claims.read_text()) if claims else {}
        except (OSError, ValueError):
            data = {}
        live |= {p for p, c in data.items() if isinstance(c, dict) and alive(c.get("pid"))} if isinstance(data, dict) else set()
    return live


def held(dirs):
    """The folders a running process holds (an open file, its cwd, or its command line: a build, a daemon, an agent
    started inside) or a live claim reserves; None when the processes cannot be read."""
    blob = processes()
    if blob is None:
        return None
    live = claimed()
    return {str(d) for d in dirs if str(d) in live or re.search(re.escape(str(d)) + r"(?=/|\s|$)", blob)}


def busy_now(p):
    h = held([p])
    if h is None:
        return "cannot read the running processes (run it outside the sandbox)"
    return "a running process uses it, or a session claimed it" if h else None


# ---------------------------------------------------------------- scan

def local_snapshots():
    out = run(["tmutil", "listlocalsnapshots", "/"], timeout=20)
    # only Time Machine snapshots pin deleted user files; com.apple.os.update-* live on the system volume
    return sum(1 for ln in (out or "").splitlines() if "com.apple.TimeMachine" in ln)


def last_run(state=STATE):
    try:
        s = json.loads(state.read_text())
    except (OSError, ValueError):
        return "never"
    return f"{idle_days(s['at']):.0f} days ago (freed {human(s['freed'])})"


def record_run(state, freed):
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"at": time.time(), "freed": freed}))
    except OSError:
        pass


def number(items):
    """Sizes filled in, small ones dropped, ids assigned section by section, biggest first."""
    sizes = measure(p for i in items if "size" not in i for p in i["measure"])
    for i in items:
        i.setdefault("size", sum(sizes.get(p, 0) for p in i["measure"]))
    items = [i for i in items if i["size"] >= i.get("min", MIN_SIZE)]
    ordered = [i for cat, _ in SECTIONS for i in sorted((x for x in items if x["cat"] == cat), key=lambda x: -x["size"])]
    return [{"id": n, **i} for n, i in enumerate(ordered, 1)]


def save_plan(path, items, args):
    Path(path).write_text(json.dumps({"created": time.time(), "config": str(CONFIG_PATH) if CONFIG_PATH else None,
                                      "args": vars(args), "items": items}, indent=1))


def scan(args):
    roots = [Path(os.path.expanduser(r)).resolve() for r in args.roots.split(",")] if args.roots else \
        [HOME / r for r in DEFAULT_ROOTS if (HOME / r).is_dir()]
    roots = [r for r in roots if r.is_dir() and strictly_inside(r, [HOME]) and not inside(r, PRIVATE)]
    cwd = os.path.realpath(os.getcwd())
    step("recordings")
    recordings, protected = recording_items(args)

    pool_dirs = pool_checkouts()
    states, in_use, repos, kept_dirs = {}, set(), [], []
    if pool_dirs:
        step("pool checkouts")
        with ThreadPoolExecutor(8) as pool:
            states = dict(zip(pool_dirs, pool.map(repo_state, pool_dirs)))
        in_use = held(pool_dirs)
        if in_use is None:
            print("· running processes not readable here: clean checks them before each delete", file=sys.stderr)
            in_use = set()
        repos, kept_dirs = pool_items(states, in_use, args.pool_days, cwd)
    # only what --all-safe really deletes: a ⚠ checkout stays, so its build output is still offered
    doomed = [r["path"] for r in repos if r["auto"]]
    when = "today" if args.checkout_days == 1 else f"for {args.checkout_days} days"
    builds = pool_builds(states, doomed, cwd, midnight(args.checkout_days - 1), midnight(args.dirty_checkout_days - 1),
                         f"clean, not touched {when}",
                         f"uncommitted changes (they stay), not touched for {args.dirty_checkout_days} days", in_use)

    step("git checkouts under " + ", ".join(short(r) for r in roots))
    in_pool = {str(d) for d in pool_dirs}
    checkouts = [c for c in find_checkouts(roots) if str(c) not in in_pool]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda r: checkout_item(r, args.days, cwd), checkouts))
    items = [*recordings, *repos, *builds, *(r for r in results if isinstance(r, dict))]
    active = sum(1 for r in results if r == "active")

    step("Xcode, Gradle, devices, package managers, caches")
    gradle_from = [c for c in [*checkouts, *pool_dirs] if str(c) not in doomed]
    items += [*xcode_items(args.derived_days, doomed, in_use), *gradle_items(gradle_from, args.days),
              *signed_app_items(doomed, args.days, pool_dirs), *db_copies(), *package_items(),
              *cache_items(args.cache_hours), *look_items({r["path"] for r in recordings})]
    step("measuring (a minute or two)")
    items = number(items)
    nongit = [d for d, s in states.items() if s is None]
    info_dirs = [p for p in [*(HOME / p for p in INFO), *measured()] if p.is_dir() and not inside(p, PRIVATE)]
    sizes = measure([*info_dirs, *nongit, *(d for d, _ in kept_dirs)])
    save_plan(args.plan, items, args)

    total, free = disk()
    nightly_note = f" · last nightly: {last_run(NIGHTLY)}" if NIGHTLY.is_file() else ""
    print(f"disk-cleanup · {time.strftime('%Y-%m-%d')} · {human(free)} free of {human(total)}"
          f" · last cleanup: {last_run()}{nightly_note}")
    if CONFIG_PATH:
        print(f"config: {short(CONFIG_PATH)}")
    snaps = local_snapshots()
    if snaps:
        print(f"⚠ {snaps} Time Machine local snapshots: deleted files free their space only when those expire")
    print(f"{len(checkouts)} checkouts scanned, {active} in use (touched in the last {args.days} days) left alone")
    configured = {"checkouts": bool(pool_dirs), "devices": bool(signed_apps_root() or db_copy_dirs())}
    for cat, title in SECTIONS:
        rows = [i for i in items if i["cat"] == cat]
        if not configured.get(cat, True) and not rows:
            continue
        print(f"\n{title.format(**vars(args), when=when)} — {len(rows)} items, {human(sum(i['size'] for i in rows))}")
        for i in rows:
            print(f"  [{i['id']:>3}] {human(i['size']):>9}  {short(i['path'])} — {i['reason']}")
        if cat == "recordings" and protected:
            print(f"  protected by {KEEP} ({len(protected)}): " + ", ".join(short(f) for f in protected[:3])
                  + (f" (+{len(protected) - 3})" if len(protected) > 3 else ""))
        if cat == "checkouts" and kept_dirs:
            print(f"  kept ({len(kept_dirs)}, {human(sum(sizes.get(str(d), 0) for d, _ in kept_dirs))}):")
            for d, why in sorted(kept_dirs, key=lambda k: -sizes.get(str(k[0]), 0)):
                print(f"        {human(sizes.get(str(d), 0)):>9}  {short(d)} — {why}")
    heavy = sorted(((str(p), sizes.get(str(p), 0), "") for p in info_dirs), key=lambda x: -x[1])
    heavy += [(str(p), sizes.get(str(p), 0), " · not git, in a pool") for p in nongit]
    heavy = [h for h in heavy if h[1] >= BIG]
    if heavy:
        print("\nINFO · other heavy folders, measured only")
        for p, n, note in heavy:
            print(f"        {human(n):>9}  {short(p)}{note}")
    safe = [i for i in items if i["auto"]]
    print(f"\n--all-safe frees ~{human(sum(i['size'] for i in safe))} ({len(safe)} items)")
    print(f"plan: {args.plan}")


# ---------------------------------------------------------------- clean

def fenced(item):
    """Each category deletes only inside its own fence, whatever the plan file says."""
    p, cat, kind = item["path"], item["cat"], item["kind"]
    if kind == "cmd":
        return item.get("tool") in ALLOWED_COMMANDS.get(cat, ())
    path = Path(p)
    parent = path.parent
    if cat == "recordings" and user_temp() and parent == user_temp():
        return (kind == "file" and bool(KAP_RAW.match(path.name))
                or kind == "dir" and bool(KAP_DIR.match(path.name)) and bool(kap_export_files(path)))
    if not strictly_inside(p, [HOME]) or inside(p, PRIVATE):
        return False
    if cat == "recordings":
        vids = video_dirs()
        skipped = [v / s for v, skip in vids for s in skip]
        return kind == "file" and not strictly_inside(p, skipped) and (
            is_kapture(path.name) and strictly_inside(p, recording_roots())
            or is_recording(path.name) and strictly_inside(p, [MOVIES, *(v for v, _ in vids)]))
    if cat == "checkouts":
        return kind == "dir" and path in pool_checkouts()
    if cat == "checkout-build":
        return (kind == "dirs" and (path / ".git").exists() and bool(item["paths"])
                and all(strictly_inside(q, [p]) and Path(q).name in BUILD_DIRS for q in item["paths"]))
    if cat == "xcode":
        return kind == "dir" and (parent == DERIVED or parent.parent == XCODE and parent.name.endswith("DeviceSupport"))
    if cat == "gradle":
        return kind == "dir" and (parent == GRADLE / "wrapper/dists" and path.name.startswith("gradle-")
                                  or parent in (GRADLE / "caches", GRADLE / "daemon") and path.name[:1].isdigit()
                                  or path == GRADLE / "caches/build-cache-1")
    if cat == "devices":
        return kind == "dir" and (parent == signed_apps_root() or parent in db_copy_dirs())
    if cat == "cache":
        return parent == CACHES and path in set(cache_entries())
    if cat == "look":
        return (parent in LOOSE or parent == DOCUMENTS and kind == "file"
                or path.suffix == ".xcarchive" and parent.parent == ARCHIVES)
    return False


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
    if item["cat"] == "recordings":
        return busy_now(p) if p.parent == user_temp() else None  # Kap's editor may still have it open
    if item["cat"] == "cache":
        t = touched_since(p, time.time() - item["hours"] * 3600)
        return f"used recently ({short(t)})" if t else None
    if "last_access" in item and derived_info(p)[1] > item["last_access"] + 1:
        return "used by Xcode after the scan"
    if item["cat"] in ("xcode", "gradle", "devices"):  # a build, a Gradle daemon or a test running from it
        return busy_now(p)
    if item["cat"] == "checkout-build":
        pool = item.get("pool")
        if pool and not item.get("dirty_ok"):
            s = repo_state(p)
            if s is None or not s["status_ok"]:
                return "git does not answer"
            if s["dirty"]:
                return "the checkout has uncommitted changes now"
        if repo_last(p) >= item["since"]:
            return "the checkout was used after the scan"
        t = touched_since(p, item["since"], skip=() if pool else item["paths"])
        if t:
            return f"touched recently ({short(t)})"
        return busy_now(p) or (None if deletable_outputs(item) else "nothing left to delete")
    if item["cat"] == "checkouts":
        if inside(os.path.realpath(os.getcwd()), [p]):
            return "the folder this session runs in"
        s = repo_state(p)
        if s is None:
            return "no longer a git checkout"
        if s["blockers"]:
            return ", ".join(s["blockers"])
        if idle_days(s["last"]) < item["days"]:
            return "touched after the scan"
        if s["linked"]:
            return "still has worktrees: " + ", ".join(Path(w).name for w in s["linked"])
        new = set(ignored_extras(p)) - set(item["repo"]["extras"])
        if new:
            return "new ignored files no build brings back: " + ", ".join(sorted(new)[:4])
        return busy_now(p)  # a pool checkout a session just claimed is clean and idle, and must stay
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


def order(item):
    repo = item.get("repo")
    if repo:
        return 2 if repo["worktree"] else 3  # worktrees before the main checkout that owns them
    return 4 if item["kind"] == "cmd" else 1


def clean(args):
    try:
        plan = json.loads(Path(args.plan).read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read the plan ({e}): scan again")
    if time.time() - plan["created"] > PLAN_TTL:
        sys.exit("the plan is older than 24 hours: scan again")
    configure(plan.get("config"), read_block(plan.get("config")))  # the fences of the config the scan used
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
    free_before, denied, mains = disk()[1], False, set()
    for item in sorted(chosen, key=order):
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
            if not err and item.get("repo", {}).get("worktree"):
                mains.add(item["repo"]["main"])
    for main in mains:
        if Path(main).is_dir():
            git(main, "worktree", "prune")
    if args.dry_run:
        return
    total, free = disk()
    freed = max(0, free - free_before)
    print(f"\nfreed {human(freed)} · now {human(free)} free of {human(total)}")
    if denied:
        print("'Operation not permitted': run clean outside the sandbox.")
    record_run(STATE, freed)


# ---------------------------------------------------------------- nightly

def run_item(item, dry_run):
    """Recheck, then delete (or say what would go). True when it went, or would have."""
    size = human(sum(measure(item.get("paths", [item["path"]])).values()))
    why = recheck(item)
    if why:
        print(f"  – skipped {short(item['path'])}: {why}")
        return False
    if dry_run:
        print(f"  · would delete {short(item['path'])} ({size}) — {item['reason']}")
        return True
    err = delete(item)
    print(f"  ✗ {short(item['path'])}: {err}" if err else f"  ✓ {short(item['path'])} ({size}) — {item['reason']}")
    return not err


def nightly(args):
    """No plan and nobody to ask: only what scan marks safe and no build fails to bring back - the build output of
    pool checkouts idle for --idle-hours that nothing holds, DerivedData that is orphaned, unused or whose checkout
    just lost its build, the configured device leftovers, and Kap's old temp recordings (exports are pasted
    elsewhere). Each item goes through the same recheck as clean, right before its delete."""
    print(f"— nightly {time.strftime('%Y-%m-%d %H:%M')}" + (f" · config {short(CONFIG_PATH)}" if CONFIG_PATH else ""),
          flush=True)
    for root, _, _ in pools():
        try:
            os.listdir(root)  # glob swallows a privacy denial and would report "nothing to clean"
        except PermissionError:
            sys.exit(f"{root}: macOS denies access (Privacy & Security → Files and Folders: allow the app that "
                     "runs this script)")
        except OSError:
            pass
    dirs = pool_checkouts()
    with ThreadPoolExecutor(8) as pool:
        states = dict(zip(dirs, pool.map(repo_state, dirs)))
    busy = held(dirs)
    if busy is None:  # nobody to ask and no way to tell who works where: touch no checkout
        print("  running processes not readable (lsof/ps): checkouts skipped")
        busy = {str(d) for d in dirs}
    busy |= {str(d) for d in dirs if kept(d)}
    builds = pool_builds(states, [], os.path.realpath(os.getcwd()), time.time() - args.idle_hours * 3600,
                         midnight(args.dirty_checkout_days - 1), f"clean, idle for {args.idle_hours} h",
                         f"uncommitted changes (they stay), idle for {args.dirty_checkout_days} days", busy)
    free_before = disk()[1]
    if dirs:
        repos = {str(d) for d, s in states.items() if s}
        print(f"  {len(repos)} checkouts ({len(busy & repos)} in use or protected), {len(builds)} with build output"
              " to remove", flush=True)
    # Builds first: only a checkout whose build really went loses its DerivedData too.
    cleared = [i["path"] for i in builds if run_item(i, args.dry_run)]
    derived = list(derived_items(args.derived_days, cleared, busy | {i["path"] for i in builds if i["path"] not in cleared},
                                 f"its checkout was idle for {args.idle_hours} h"))
    groups = [(derived, "DerivedData")]
    if db_copy_dirs():
        groups.append((list(db_copies()), "phone DB copies"))
    if signed_apps_root():
        groups.append((list(orphan_apps(args.idle_hours)), "signed app copies of vanished checkouts"))
    groups.append(([{"cat": "recordings", "path": str(p), "kind": kind, "mtime": mtime(p), "reason": why}
                    for p, kind, _, why in kap_temp(args.kap_days, args.kap_exported_days) if plain(p) and not kept(p)],
                   "Kap temp recordings"))
    for found, what in groups:
        print(f"  {len(found)} {what}", flush=True)
        for item in found:
            run_item(item, args.dry_run)
    if args.dry_run:
        return
    freed = max(0, disk()[1] - free_before)
    print(f"freed {human(freed)} · now {human(disk()[1])} free")
    record_run(NIGHTLY, freed)


# ---------------------------------------------------------------- main

def at_least_one(v):
    """0 would move a cut-off to now, and work in progress would qualify."""
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def kap_args(p):
    p.add_argument("--kap-days", type=at_least_one, default=5, help="days before Kap's temp recordings go")
    p.add_argument("--kap-exported-days", type=at_least_one, default=2, help="the same, once exported")


def main():
    path, block = load_config()
    configure(path, block)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="measure and write the plan; deletes nothing")
    s.add_argument("--roots", help="comma-separated folders under your home holding your checkouts (default: " +
                                   ", ".join("~/" + r for r in DEFAULT_ROOTS) + ", the ones that exist)")
    s.add_argument("--days", type=at_least_one, default=14,
                   help="days a checkout outside the pools must be untouched; also the Gradle build cache and signed apps")
    s.add_argument("--pool-days", type=at_least_one, default=20, help="days a pool checkout must be idle to go whole")
    s.add_argument("--checkout-days", type=at_least_one, default=1,
                   help="pool checkouts' build output: calendar days untouched (1 = not touched today)")
    s.add_argument("--dirty-checkout-days", type=at_least_one, default=7,
                   help="the same, for pool checkouts with uncommitted changes")
    s.add_argument("--derived-days", type=at_least_one, default=7, help="days a DerivedData folder must be unused")
    s.add_argument("--cache-hours", type=at_least_one, default=24, help="hours a ~/Library/Caches entry must be unused")
    s.add_argument("--recording-days", type=at_least_one, default=7, help="days before a recording is offered")
    kap_args(s)
    s.add_argument("--plan", default=str(Path(os.environ.get("TMPDIR", "/tmp")) /
                                         time.strftime("disk-cleanup-plan-%Y%m%d-%H%M%S.json")))
    c = sub.add_parser("clean", help="delete items of the plan, re-checking each one")
    c.add_argument("--plan", required=True)
    pick = c.add_mutually_exclusive_group(required=True)
    pick.add_argument("--ids", help="e.g. 1,4,7-12: the only way to delete TO LOOK AT and ⚠ items")
    pick.add_argument("--all-safe", action="store_true", help="every safe item")
    pick.add_argument("--cat", help="recordings,checkouts,checkout-build,xcode,gradle,devices,packages,cache "
                                    "(their safe items)")
    c.add_argument("--dry-run", action="store_true")
    n = sub.add_parser("nightly", help="unattended: regenerable build output and Kap's old temp recordings")
    n.add_argument("--idle-hours", type=at_least_one, default=8,
                   help="hours a clean pool checkout must be untouched before its build output goes")
    n.add_argument("--dirty-checkout-days", type=at_least_one, default=7, help="as in scan")
    n.add_argument("--derived-days", type=at_least_one, default=7, help="as in scan")
    kap_args(n)
    n.add_argument("--dry-run", action="store_true")
    defaults = CFG.get("defaults", {})
    if isinstance(defaults, dict):
        for p in (s, n):
            p.set_defaults(**{k: v for k, v in defaults.items() if any(a.dest == k for a in p._actions)})
    args = ap.parse_args()
    {"scan": scan, "clean": clean, "nightly": nightly}[args.command](args)


if __name__ == "__main__":
    main()
