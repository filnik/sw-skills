#!/usr/bin/env python3
"""Tests for skills/disk-cleanup/scripts/disk_cleanup.py on a throwaway HOME. Run: python3 tests/test_disk_cleanup.py
(exit 0 = green). Nothing outside the temporary folder is read for deletion or deleted."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="disk-cleanup-test.")).resolve()
os.environ.update(HOME=str(TMP / "home"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(TMP / "gitconfig"))
(TMP / "home").mkdir()
spec = importlib.util.spec_from_file_location(
    "disk_cleanup", Path(__file__).resolve().parents[1] / "skills/disk-cleanup/scripts/disk_cleanup.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)
H = dc.HOME
assert H == TMP / "home", H
OLD = time.time() - 30 * 86400


def age(root, ts=OLD):
    """Every file and folder under root (root included) last modified at ts."""
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            os.utime(os.path.join(dirpath, n), (ts, ts), follow_symlinks=False)
    os.utime(root, (ts, ts))


def write(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def repo(name):
    """A checkout idle for 30 days: src.js and lib/a.js tracked, node_modules ignored."""
    r = H / "code" / name
    dates = "%d +0000" % OLD
    env = {**os.environ, "GIT_AUTHOR_DATE": dates, "GIT_COMMITTER_DATE": dates}

    def g(*args):
        subprocess.run(["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       check=True, capture_output=True, env=env)

    write(r / ".gitignore", "node_modules/\n")
    write(r / "src.js")
    write(r / "lib/a.js")
    write(r / "node_modules/pkg/index.js")
    g("init", "-q")
    g("add", ".")
    g("commit", "-q", "-m", "init")
    age(r)
    return r, g


def test_keep():
    write(H / "k/.disk-cleanup-keep", "# videos stay\n*.mov\n")
    write(H / "k/a.mov")
    write(H / "k/a.mp4")
    write(H / "k/sub/b.mov")
    write(H / "all/.disk-cleanup-keep", "")
    write(H / "all/x.bin")
    assert dc.kept(H / "k/a.mov") and dc.kept(H / "k/sub/b.mov")
    assert not dc.kept(H / "k/a.mp4")
    assert dc.kept(H / "k"), "a folder holding a keep file is protected itself"
    assert dc.kept(H / "all/x.bin")
    assert not dc.kept(H / "free.bin")


def test_parse():
    assert dc.parse_ids("1,4,7-9, 4") == [1, 4, 7, 8, 9]
    assert dc.parse_docker("Images|1.2GB (50%)\nBuild Cache|500MB\nLocal Volumes|0B") == \
        {"Images": 1200000000, "Build Cache": 500000000, "Local Volumes": 0}


def test_idle_checkout():
    r, g = repo("idle")
    item = dc.checkout_item(r, 14, "/")
    assert isinstance(item, dict) and item["paths"] == [str(r / "node_modules")], item
    assert dc.checkout_item(r, 14, str(r)) is None, "the session's own folder is never proposed"

    os.utime(r / "lib/a.js")  # a source changed today, deep enough that no top-level entry shows it
    assert dc.checkout_item(r, 14, "/") == "active"

    t, g = repo("tracked")
    g("add", "-f", "node_modules/pkg/index.js")
    g("commit", "-q", "-m", "vendored")
    age(t)
    assert dc.checkout_item(t, 14, "/") is None, "a folder with a tracked file inside is not build output"


def test_fence():
    write(H / "Library/Caches/ok/a")
    write(H / "elsewhere/a")
    (H / "Library/Caches/link").symlink_to(H / "elsewhere")
    cache = lambda p: {"cat": "cache", "path": str(p), "kind": "dir"}
    assert dc.fenced(cache(H / "Library/Caches/ok"))
    assert not dc.fenced(cache(H / "elsewhere")), "outside its category's root"
    assert not dc.fenced(cache(H / "Library/Caches/link")), "a symlink is never a cache entry"
    assert not dc.fenced({"cat": "packages", "path": "rm -rf ~", "kind": "cmd", "tool": "rm"})
    r, _ = repo("fenced")
    (H / "via").symlink_to(H / "code")
    item = {"cat": "checkout-build", "path": str(H / "via/fenced"), "kind": "dirs",
            "paths": [str(H / "via/fenced/node_modules")], "since": time.time()}
    assert "symlink" in dc.recheck(item)
    item = {**item, "path": str(r), "paths": [str(r / "src.js")]}
    assert not dc.fenced(item), "only build folders inside the checkout"


def test_plan_expiry():
    plan = TMP / "old-plan.json"
    dc.save_plan(plan, [], argparse.Namespace())
    data = json.loads(plan.read_text())
    plan.write_text(json.dumps({**data, "created": time.time() - 25 * 3600}))
    try:
        dc.clean(argparse.Namespace(plan=str(plan), all_safe=True, cat=None, ids=None, dry_run=False))
    except SystemExit as e:
        assert "24 hours" in str(e)
    else:
        raise AssertionError("an old plan must be refused")


def test_clean():
    for name in ("foo", "bar", "baz", "com.apple.thing"):
        write(H / f"Library/Caches/{name}/data.bin")
    age(H / "Library/Caches")
    app, _ = repo("app")
    dc.MIN_SIZE = 0
    items = dc.number([*dc.cache_items(24), dc.checkout_item(app, 14, "/")])
    by_path = {i["path"]: i["id"] for i in items}
    assert str(H / "Library/Caches/com.apple.thing") not in by_path, "Apple's caches are left alone"
    plan = TMP / "plan.json"
    dc.save_plan(plan, items, argparse.Namespace())

    write(H / "Library/Caches/bar/new.bin")  # bar changes after the scan
    dc.processes = lambda: ""  # nothing running holds the checkout
    ids = ",".join(str(by_path[str(p)]) for p in (H / "Library/Caches/foo", H / "Library/Caches/bar", app))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        dc.clean(argparse.Namespace(plan=str(plan), all_safe=False, cat=None, ids=ids, dry_run=False))
    out = out.getvalue()
    assert not (H / "Library/Caches/foo").exists()
    assert (H / "Library/Caches/bar/data.bin").exists() and "modified after the scan" in out, out
    assert (H / "Library/Caches/baz").exists(), "not picked, not deleted"
    assert not (app / "node_modules").exists()
    assert (app / "src.js").exists() and (app / ".git").is_dir() and (app / "lib/a.js").exists()


def test_keep_below_and_fence_of_remove():
    write(H / "Downloads/stuff/junk.bin")
    write(H / "Downloads/stuff/important" / dc.KEEP, "")
    item = {"cat": "look", "path": str(H / "Downloads/stuff"), "kind": "dir", "mtime": dc.mtime(H / "Downloads/stuff")}
    assert dc.recheck(item).startswith(f"a {dc.KEEP} file inside"), "a keep file deeper down protects its parents"
    write(H / "Downloads/top.bin")
    assert dc.recheck({**item, "path": str(H / "Downloads/top.bin"), "kind": "file",
                       "mtime": dc.mtime(H / "Downloads/top.bin")}) is None

    # a read-only folder inside the target still goes; the folder holding the target keeps its permissions
    write(H / "Downloads/ro/locked/f.bin")
    os.chmod(H / "Downloads/ro/locked", 0o555)
    assert dc.remove(str(H / "Downloads/ro")) is None and not (H / "Downloads/ro").exists()
    write(H / "Downloads/shut/target/f.bin")
    os.chmod(H / "Downloads/shut", 0o555)
    try:
        assert dc.remove(str(H / "Downloads/shut/target")), "the target's own folder is not writable: not deleted"
        assert (H / "Downloads/shut").stat().st_mode & 0o777 == 0o555, "the parent's permissions are untouched"
    finally:
        os.chmod(H / "Downloads/shut", 0o755)


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ok")
