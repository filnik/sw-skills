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
import types
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
KAP = TMP / "kap-temp"  # stands for the per-user temp dir Kap records into
KAP.mkdir()
dc.user_temp = lambda: KAP


def age(root, ts=OLD):
    """Every file and folder under root (root included) last modified at ts."""
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            os.utime(os.path.join(dirpath, n), (ts, ts), follow_symlinks=False)
    os.utime(root, (ts, ts))


def write(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def repo(name, parent=None):
    """A checkout idle for 30 days: src.js and lib/a.js tracked, node_modules ignored."""
    r = (parent or H / "code") / name
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


def test_keep_file_in_xcode_and_gradle():
    support = dc.XCODE / "iOS DeviceSupport"
    write(support / "16.0" / dc.KEEP, "")
    for name, ts in (("18.0", time.time()), ("17.0", OLD), ("16.0", OLD - 86400)):
        write(support / name / "Symbols/x")
        age(support / name, ts)
    write(H / "code/droid/gradle/wrapper/gradle-wrapper.properties",
          "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.5-bin.zip\n")
    for name in ("gradle-8.5-bin", "gradle-7.0-bin", "gradle-6.0-bin"):
        (dc.GRADLE / "wrapper/dists" / name).mkdir(parents=True)
    write(dc.GRADLE / "wrapper/dists/gradle-6.0-bin" / dc.KEEP, "")

    real = dc.subprocess
    dc.subprocess = types.SimpleNamespace(run=lambda *a, **k: (_ for _ in ()).throw(OSError("no xcrun in tests")),
                                          TimeoutExpired=subprocess.TimeoutExpired)
    try:
        xcode = [Path(i["path"]).name for i in dc.xcode_items(7)]
    finally:
        dc.subprocess = real
    assert xcode == ["17.0"], xcode  # 18.0 is the newest, 16.0 is protected
    gradle = [Path(i["path"]).name for i in dc.gradle_items([H / "code/droid"], 14)]
    assert gradle == ["gradle-7.0-bin"], gradle


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


def configured(block):
    """The test body runs with this disk_cleanup config, and with none afterwards."""
    @contextlib.contextmanager
    def scope():
        dc.configure(None, block)
        try:
            yield
        finally:
            dc.configure(None, {})
    return scope()


def pushed(name, parent):
    """A pool checkout whose commits are on a remote too."""
    r, g = repo(name, parent)
    g("update-ref", "refs/remotes/origin/main", "HEAD")
    age(r)
    return r


def test_kap_exported_go_after_two_days_and_the_rest_after_five():
    ended = int(time.time() - 3 * dc.DAY)
    export = KAP / ("e" * 32)
    write(export / time.strftime("Kapture %Y-%m-%d at %H.%M.%S.mp4", time.localtime(ended)))
    write(export / ".DS_Store")
    age(export, time.time() - 3 * dc.DAY)
    for name, days in (("a", 3), ("b", 3.5), ("c", 6)):  # a ended when the export says: exported
        write(KAP / (name * 32 + ".mp4"))
        age(KAP / (name * 32 + ".mp4"), time.time() - days * dc.DAY)
    age(KAP / ("a" * 32 + ".mp4"), ended)
    write(KAP / "other.mp4")
    age(KAP / "other.mp4")
    got = sorted(p.name for p, *_ in dc.kap_temp(5, 2))
    assert got == ["a" * 32 + ".mp4", "c" * 32 + ".mp4", "e" * 32], got


def test_kap_fence_lets_through_only_kap_files_at_the_top_of_the_temp_dir():
    export, other = KAP / ("d" * 32), KAP / ("f" * 32)
    write(export / "Kapture 2026-11-08 at 21.29.50.mp4")
    write(other / "notes.txt")
    fenced = lambda p, kind: dc.fenced({"cat": "recordings", "path": str(p), "kind": kind})
    assert fenced(KAP / ("a" * 32 + ".mp4"), "file") and fenced(export, "dir")
    assert not fenced(other, "dir") and not fenced(KAP / "clip.mp4", "file")
    assert not fenced(KAP / "sub" / ("a" * 32 + ".mp4"), "file")


def test_recordings_by_kind_and_video_folders_only_by_number():
    vids = H / "product"
    for p in ("Downloads/Kapture 2026-10-01 at 10.00.00.mp4", "Downloads/clip.mp4", "Movies/2026-10-01 10-00-00.mkv",
              "Movies/2026-10-01 10-00-00 - edit.mp4", "Movies/TV/show.mp4", "product/calls/standup.mp4",
              "product/repos/app/demo.mp4"):
        write(H / p)
    for d in ("Downloads", "Movies", "product"):
        age(H / d)
    with configured({"video_dirs": [{"path": "~/product", "skip": ["repos"]}]}):
        items, _ = dc.recording_items(argparse.Namespace(recording_days=7, kap_days=5, kap_exported_days=2))
        got = {os.path.relpath(i["path"], H): i["auto"] for i in items if not i["path"].startswith(str(KAP))}
        assert got == {"Downloads/Kapture 2026-10-01 at 10.00.00.mp4": True, "Movies/2026-10-01 10-00-00.mkv": True,
                       "Movies/2026-10-01 10-00-00 - edit.mp4": False, "product/calls/standup.mp4": False}, got
        fenced = lambda p: dc.fenced({"cat": "recordings", "path": str(p), "kind": "file"})
        assert fenced(vids / "calls/standup.mp4")
        assert not fenced(vids / "repos/app/demo.mp4") and not fenced(H / "Downloads/clip.mp4")


def test_pool_build_output_goes_when_clean_and_untouched_today_or_dirty_and_untouched_for_a_week():
    pool = H / "pool"
    with configured({"pools": [{"root": "~/pool", "checkouts": ["slot-*"]}]}):
        old, _ = repo("slot-old", pool)
        fresh, _ = repo("slot-fresh", pool)
        age(fresh, time.time())
        for name, days in (("slot-dirty", 3), ("slot-stale", 30)):
            r, _ = repo(name, pool)
            write(r / "notes.txt")
            age(r, time.time() - days * dc.DAY)
        dirs = dc.pool_checkouts()
        assert [d.name for d in dirs] == ["slot-dirty", "slot-fresh", "slot-old", "slot-stale"], dirs
        items = dc.pool_builds({d: dc.repo_state(d) for d in dirs}, [], "/", dc.midnight(0), dc.midnight(6),
                               "clean", "dirty")
        assert {Path(i["path"]).name: i["dirty_ok"] for i in items} == {"slot-old": False, "slot-stale": True}, items
        dc.processes = lambda: ""
        item = next(i for i in items if i["path"] == str(old))
        assert dc.recheck(item) is None, dc.recheck(item)
        write(old / "notes.txt")
        age(old)
        assert dc.recheck(item) == "the checkout has uncommitted changes now"
    with configured({"pools": [{"root": "~/pool", "checkouts": ["slot-*"], "build_dirs": ["build"]}]}):
        dirs = dc.pool_checkouts()
        items = dc.pool_builds({d: dc.repo_state(d) for d in dirs}, [], "/", dc.midnight(0), dc.midnight(6), "", "")
        assert items == [], "node_modules is not in this pool's build_dirs"


def test_idle_clean_pool_checkout_goes_whole_and_dirty_or_claimed_ones_are_kept():
    pool = H / "pool2"
    with configured({"pools": [{"root": "~/pool2", "checkouts": ["slot-*"], "claims": "claims.json"}]}):
        clean, ended, taken = pushed("slot-clean", pool), pushed("slot-ended", pool), pushed("slot-taken", pool)
        dirty = pushed("slot-dirty", pool)
        write(dirty / "notes.txt")
        age(dirty)
        done = subprocess.Popen(["true"])
        done.wait()
        (pool / "claims.json").write_text(json.dumps({str(taken): {"pid": str(os.getpid())},
                                                      str(ended): {"pid": str(done.pid)}}))
        dirs = dc.pool_checkouts()
        dc.processes = lambda: ""
        in_use = dc.held(dirs)
        assert in_use == {str(taken)}, "a claim whose session ended reserves nothing"
        repos, kept = dc.pool_items({d: dc.repo_state(d) for d in dirs}, in_use, 20, "/")
        assert sorted(Path(r["path"]).name for r in repos) == ["slot-clean", "slot-ended"], repos
        assert all(r["auto"] for r in repos)
        why = {d.name: w for d, w in kept}
        assert why["slot-dirty"].startswith("1 modified or untracked files"), why
        assert why["slot-taken"].startswith("a running process uses it"), why
        assert dc.fenced({"cat": "checkouts", "path": str(clean), "kind": "dir"})
        assert not dc.fenced({"cat": "checkouts", "path": str(pool), "kind": "dir"})


def test_orphan_apps_only_when_the_recorded_checkout_is_gone_and_signed_before_idle_hours():
    apps, here = H / ".cache/apps", H / "work/checkout"
    here.mkdir(parents=True)
    for name, top, days in (("gone-old", H / "nowhere/a", 2), ("here", here, 2), ("gone-fresh", H / "nowhere/b", 0),
                            ("legacy", None, 2)):
        (apps / name).mkdir(parents=True)
        if top:
            write(apps / name / "checkout", str(top))
        age(apps / name, time.time() - days * dc.DAY)
    with configured({"signed_apps": "~/.cache/apps"}):
        assert [Path(i["path"]).name for i in dc.orphan_apps(8)] == ["gone-old"]
        assert dc.fenced({"cat": "devices", "path": str(apps / "gone-old"), "kind": "dir"})
        assert not dc.fenced({"cat": "devices", "path": str(here), "kind": "dir"})


def test_phone_db_copies_but_the_newest_and_the_last_hour_and_their_fence():
    phones = H / ".cache/phones"
    for phone in ("p1", "p2"):
        for stamp in ("20261101-000000", "20261102-000000", "20261103-000000"):
            write(phones / phone / "db" / stamp / "app.db")
    age(phones)
    age(phones / "p2/db/20261102-000000", time.time())  # a test may still read it
    with configured({"phone_db_copies": "~/.cache/phones/*/db"}):
        got = sorted(os.path.relpath(i["path"], phones) for i in dc.db_copies())
        assert got == ["p1/db/20261101-000000", "p1/db/20261102-000000", "p2/db/20261101-000000"], got
        assert dc.fenced({"cat": "devices", "path": str(phones / "p1/db/20261101-000000"), "kind": "dir"})
        assert not dc.fenced({"cat": "devices", "path": str(phones / "p1/db"), "kind": "dir"})


def test_config_from_the_folder_upward_and_else_the_one_found_last():
    proj, other = TMP / "proj", TMP / "other"
    write(proj / ".claude/config/project.json", json.dumps({"disk_cleanup": {"signed_apps": "~/apps"}}))
    write(other / ".claude/config/project.json", json.dumps({"locale": "en"}))
    (proj / "src").mkdir()
    here = os.getcwd()
    try:
        os.chdir(proj / "src")
        assert dc.load_config() == (proj / ".claude/config/project.json", {"signed_apps": "~/apps"})
        os.chdir(other)
        assert dc.load_config()[0] == proj / ".claude/config/project.json", "a config without the block falls back"
        os.chdir("/")
        assert dc.load_config()[1] == {"signed_apps": "~/apps"}
    finally:
        os.chdir(here)


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ok")
