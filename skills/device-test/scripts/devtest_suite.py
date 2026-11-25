"""Suites: a manifest of scenarios per phone, run tier by tier, and the report built from the ledger.

    devtest --owner X --worktree W suite run NAME --phones ALIAS,ALIAS [--tiers vital,data]
    devtest --worktree W suite report NAME [--build SHA]

A manifest (the pack's suites/<name>.py) holds TIERS = ((tier, ((scenario, "android ios" | "android" | "ios" | "pair"), ...)),
...), STOP_AFTER (tiers whose app FAIL stops the suite) and KNOWN ({(scenario, criterion): why}). In a tier the
single-phone scenarios run in parallel, one queue per phone; "pair" scenarios run after them with the Android
phone as `d` and the other as its colleague. Every verdict lands in the state folder's ledger.jsonl (devtest run
writes it), so a criterion run in parts (`run --only`) adds up in the report.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from devtest_common import RUNS, Blocked, check_lease, pack_dir, resolve, say
from devtest_scenario import LEDGER, build_id

HERE = Path(__file__).resolve().parent
SUITES = pack_dir("suites")
SCENARIOS = pack_dir("scenarios")
NOT_JUDGED = ("SKIP", "NOT RUN", "N/A")


def load(name: str):
    if not SUITES:
        raise Blocked("suites live in a pack's suites/ folder, and no pack is loaded")
    path = SUITES / ("%s.py" % name)
    if not path.exists():
        raise Blocked("no suite %s (have: %s)" % (path, ", ".join(p.stem for p in SUITES.glob("*.py")) or "none"))
    spec = importlib.util.spec_from_file_location("suite_" + name.replace("-", "_"), str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_one(cmd: list, out: Path) -> int:
    with open(out, "w") as f:
        return subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)


def run(name: str, phones: list, top: Path, owner: str, tiers=None) -> int:
    suite = load(name)
    by_platform = {}
    for p in phones:
        platform, ident, alias = resolve(p)
        check_lease(ident, alias)
        by_platform[platform] = alias
    out_dir = RUNS / ("%s-suite-%s" % (time.strftime("%Y%m%d-%H%M%S"), name))
    out_dir.mkdir(parents=True, exist_ok=True)
    devtest = str(HERE / "devtest.py")
    worst, lock = 0, threading.Lock()
    say("suite %s · build %s · phones %s · output in %s" % (name, build_id(top), ", ".join(phones), out_dir))

    def job(scenario: str, phone: str) -> int:
        cmd = [sys.executable, devtest, "--owner", owner, "--device", phone, "--worktree", str(top), "run",
               str(SCENARIOS / ("%s.py" % scenario))]
        out = out_dir / ("%s-%s.out" % (phone, scenario))
        t0 = time.time()
        code = _run_one(cmd, out)
        verdicts = [l for l in out.read_text().splitlines() if l.startswith(("VERDICT", "SCENARIO ERROR", "BLOCKED"))]
        with lock:
            for v in verdicts or ["(no verdict line, exit %d: see %s)" % (code, out)]:
                print("  %s · %s · %s (%dm)" % (scenario, phone, v[:300], (time.time() - t0) // 60), flush=True)
        return code

    for tier, items in suite.TIERS:
        if tiers and tier not in tiers:
            continue
        print("tier %s" % tier, flush=True)
        queues = {}
        for scenario, where in items:
            for platform in where.split():
                if platform != "pair" and platform in by_platform:
                    queues.setdefault(by_platform[platform], []).append(scenario)
        codes = []

        def drain(phone, names):
            for n in names:
                codes.append(job(n, phone))
        threads = [threading.Thread(target=drain, args=(ph, ns)) for ph, ns in queues.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        pairs = [s for s, where in items if "pair" in where.split()]
        for scenario in pairs:
            if len(by_platform) < 2 or "android" not in by_platform:
                print("  %s · skipped: needs an Android phone and an iPhone" % scenario)
                continue
            codes.append(job(scenario, by_platform["android"]))
        worst = max([worst] + codes)
        if 1 in codes and tier in getattr(suite, "STOP_AFTER", ()):
            print("stopped after tier %s: an app FAIL there (STOP_AFTER)" % tier)
            return 1
    print("report: devtest --worktree %s suite report %s" % (top, name))
    return worst


# ------------------------------------------------------------------------------------------------ report

def entries(build=None) -> list:
    if not LEDGER.exists():
        return []
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    return [r for r in rows if build is None or r.get("build", "").split("+")[0] == build.split("+")[0]]


def verdicts(entries) -> tuple:
    """({criterion: verdict}, {criterion: words seen}) over ledger entries in time order: the latest judged verdict
    wins, so runs split with --only add up; an N/A replaces only a SKIP or NOT RUN, never a PASS or FAIL."""
    crit, seen = {}, {}
    for r in entries:
        for c, v in r.get("criteria", {}).items():
            word = v.split(" (")[0]
            prev = crit.get(c, "").split(" (")[0]
            if c in crit and (word in ("SKIP", "NOT RUN") or (word.startswith("N/A") and prev not in ("SKIP", "NOT RUN"))):
                continue
            crit[c] = v
            seen.setdefault(c, set()).add(word)
    return crit, seen


def current_version(scenario: str):
    """A test on ledger entries: written by the scenario file as it is now (its sha1, or for entries from before the
    sha was recorded, a time after the file's last change)."""
    path = SCENARIOS / (scenario + ".py")
    if not path.exists():
        return lambda r: True
    sha = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    changed = path.stat().st_mtime

    def is_now(r):
        if r.get("scenario_sha"):
            return r["scenario_sha"] == sha
        return time.mktime(time.strptime(r["at"], "%Y-%m-%dT%H:%M:%S")) >= changed
    return is_now


def report(name: str, top: Path, build=None) -> int:
    suite = load(name)
    build = build or build_id(top)
    rows = entries(build)
    known = getattr(suite, "KNOWN", {})
    scenarios = [s for _, items in suite.TIERS for s, _ in items]
    phones = sorted({r["phone"] for r in rows if r["scenario"] in scenarios})
    print("suite %s · build %s · %d ledger entries" % (name, build, sum(r["scenario"] in scenarios for r in rows)))
    if not phones:
        print("no verdict for this build yet")
        return 0
    print("%-18s %s" % ("", " ".join("%-22s" % p for p in phones)))
    details, flaky = [], []
    for s in scenarios:
        cells = []
        for p in phones:
            mine = [r for r in rows if r["scenario"] == s and r["phone"] == p]
            if not mine:
                cells.append("-")
                continue
            # Verdicts of the scenario as it is now win; those of an older version only fill what it has not judged.
            is_now = current_version(s)
            new = [r for r in mine if is_now(r)]
            crit, seen = verdicts(new)
            older = set()
            for c, v in verdicts([r for r in mine if not is_now(r)])[0].items():
                if c not in crit or crit[c].split(" (")[0] in ("SKIP", "NOT RUN"):
                    crit[c] = v
                    older.add(c)
            if not new:
                older = set()  # every verdict is from one version: nothing to tell apart
                seen = verdicts(mine)[1]
            flaky += ["%s %s %s: %s" % (s, p, c, "/".join(sorted(w))) for c, w in seen.items()
                      if "PASS" in w and w & {"FAIL", "BLOCKED"}]
            bad = {c: v for c, v in crit.items() if v.split(" (")[0] in ("FAIL", "BLOCKED", "NOT RUN", "PARTIAL")}
            judged = [c for c, v in crit.items() if not v.split(" (")[0].startswith(NOT_JUDGED)]
            last = mine[-1]
            if crit:
                failed = [c for c, v in bad.items() if v.startswith(("FAIL", "BLOCKED"))]
                word = ("PASS" if not bad else "INCOMPLETE" if not failed else
                        "KNOWN" if all((s, c) in known for c in failed) and len(failed) == len(bad) else "FAIL")
                cells.append("%s %d/%d" % (word, len([c for c in judged if c not in bad]), len(judged)))
            else:
                cells.append(last["verdict"])
            for c, v in bad.items():
                why = known.get((s, c))
                details.append("  %s %s %s %s%s%s" % (s, p, c, v[:140], " · KNOWN: %s" % why if why else "",
                                                     " · from an older version of the scenario" if c in older else ""))
            if not crit and last["verdict"] != "PASS":
                details.append("  %s %s %s: %s" % (s, p, last["verdict"], last.get("reason", "")[:160]))
        print("%-18s %s" % (s, " ".join("%-22s" % c for c in cells)))
    if details:
        print("not passed:")
        print("\n".join(details))
    if flaky:
        print("mixed verdicts on this build (flaky?):")
        print("\n".join("  " + f for f in flaky))
    return 0
