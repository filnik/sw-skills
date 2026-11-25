#!/usr/bin/env python3
# Where the medium tasks Claude would delegate on its own go: to Codex, except outside the folders Codex may work in
# or with Codex used up; over budget on the slower tier; from 80% and over budget, while Claude has margin, to Claude
# agents (sub-claude), with Codex kept in reserve for what only it has.
# Prints one line for the context (UserPromptSubmit hook). --codex-tier prints Codex's weekly usage, the sub-codex
# tier (fast or default) and the model of --important runs (astra or sol); --sub-route the way for sub-codex.sh
# (codex, claude reserve or claude usedup); --claude-pick hard|judgement|gathering the Claude agent's model and
# effort, which the line also gives for all three classes; --folder DIR exits 4 when Codex may not work in DIR;
# --language the language of the final message of Codex runs and of sub-claude.
#
# spare = 100 - used - pace x weighted hours until the reset: the quota that, at this pace, expires unused.
# pace = the larger of the one measured in this window and 100 / the weighted hours of a week.
# In the first quarter of the window the projection grows with the hours elapsed: the early pace is unreliable.
# Weights from the author's transcripts (70% Mon-Fri 9-18:30, 19% evening, 11% weekend): weekday 1,
# evening and weekend 0.25, 3:00-9:00 zero.
import glob, json, os, sys, time
from datetime import datetime
from pathlib import Path

WEEK = 7 * 86400
CODEX_MAX = 100       # Codex weekly usage from which it counts as used up
FAST_ALWAYS = 70      # Codex weekly usage up to which sub-codex always runs on the fast tier
FAST_SLACK = 1.5      # points the fast tier may overshoot at the reset (~one hour of work): Claude covers the rest
ASTRA_DAYS = 3        # days before the reset from which sub-codex's --important runs may go to Astra
ASTRA_PER_DAY = 10    # minimum spare per day left until the reset: 30 points at three days, 10 at one
CODEX_RESERVE = 80    # Codex weekly usage from which, over budget, the work goes to Claude if Claude has margin
CLAUDE_WEEK_MAX = 60  # Claude's margin: week at most 60% used (the usage, not the spare: early in the week the
CLAUDE_FIVE_MAX = 50  # projection is wrong) and the 5-hour window under half, which also stops the chat

# Model and effort of the Claude agents (sub-claude) by task class and Claude quota. The user's floors (25/11):
# Sonnet and Opus never below high, Fable never below medium. Price per token: Sonnet 1, Opus 2, Fable 4.
LADDER = {
    "hard":      {"ample": ("fable", "high"),  "normal": ("fable", "medium"), "tight": ("opus", "high")},
    "judgement": {"ample": ("opus", "xhigh"),  "normal": ("opus", "high"),    "tight": ("sonnet", "high")},
    "gathering": {"ample": ("sonnet", "high"), "normal": ("sonnet", "high"),  "tight": ("sonnet", "high")},
}


# Claude agents in parallel at most, per quota band (the user, 25/11: "balanced on how many tokens you have").
CLAUDE_PARALLEL = {"ample": 3, "normal": 2, "tight": 1}

# The sub_codex block of the user-global config: where Codex may work (every folder when empty), how the route line
# names those folders, what only Codex has, the skills built on sub-codex, and the language of the final messages.
DEFAULTS = {"folders": [], "label": "Codex", "codex_only": [], "skills": "the skills built on it",
            "language": "English"}


def settings(strict=False):
    """The sub_codex block of ~/.claude/config/project.json over DEFAULTS. Only that file: where Codex may work and
    what only it has belong to the user's Codex account, not to a project. Unreadable: DEFAULTS, or raise if strict."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))
        from project_config import project_config
        block = project_config(Path.home()).get("sub_codex")
    except (Exception, SystemExit):
        if strict:
            raise
        block = None
    cfg = {**DEFAULTS, **(block if isinstance(block, dict) else {})}
    if isinstance(cfg["folders"], str):
        cfg["folders"] = [cfg["folders"]]
    return cfg


def codex_allowed(path, cfg):
    """True when Codex may work in path: anywhere without folders, otherwise inside one of them."""
    roots = [os.path.realpath(os.path.expanduser(f)) for f in cfg["folders"]]
    p = os.path.realpath(os.path.expanduser(path))
    return not roots or any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def check_folder(path):
    """0 when Codex may work in path, otherwise 4 with the reason on stderr (sub-codex.sh exits with it)."""
    cfg = settings(strict=True)
    if not os.path.isdir(os.path.expanduser(path)):
        print(f"sub-codex: {path} is not a folder.", file=sys.stderr)
        return 4
    if codex_allowed(path, cfg):
        return 0
    print(f"sub-codex: Codex works only in the {cfg['label']} folders ({', '.join(cfg['folders'])});"
          f" {os.path.realpath(path)} is not one. Do the task with Claude.", file=sys.stderr)
    return 4


def and_list(items):
    items = [str(i) for i in items]
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else "".join(items)


def weight(ts):
    t = datetime.fromtimestamp(ts)
    h = t.hour + t.minute / 60
    if 3 <= h < 9:
        return 0.0
    return 1.0 if t.weekday() < 5 and 9 <= h < 18.5 else 0.25


def hours(a, b):
    """Weighted hours between two epochs, in quarter-hour steps."""
    total, t = 0.0, a
    while t < b:
        total += weight(t) * min(900, b - t) / 3600
        t += 900
    return total


def window(used, reset, now):
    """Usage and reset of the weekly window; past the reset the window starts again from zero."""
    if used is None or not reset:
        return None
    if reset <= now:
        used = 0
        while reset <= now:
            reset += WEEK
    return {"used": float(used), "reset": float(reset)}


def read_claude(now):
    # written by the statusline at every refresh: rate_limits reach only the statusline
    name = os.path.basename((os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")).rstrip("/"))
    try:
        with open(os.path.expanduser(f"~/.cache/claude-statusline/limits-{name}.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    w = window(d.get("week_pct"), d.get("week_at"), now)
    if w:
        w["five"] = (d.get("five_pct") or 0) if (d.get("five_at") or 0) > now else 0
    return w


def read_codex(now):
    """Codex's latest weekly limit in its sessions (tail of the latest files)."""
    files = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")), key=os.path.getmtime)
    for path in reversed(files[-5:]):
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 512 * 1024))
            lines = f.read().decode(errors="replace").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                limits = json.loads(line)["payload"]["rate_limits"] or {}
            except (ValueError, KeyError, TypeError):
                continue
            if (limits.get("limit_id") or "codex") != "codex":
                continue
            for w in (limits.get("primary"), limits.get("secondary")):
                if w and w.get("window_minutes") == 10080:
                    return window(w.get("used_percent"), w.get("resets_at"), now)
    return None


def spare(w, now):
    nominal = 100 / hours(now, now + WEEK)
    elapsed = hours(w["reset"] - WEEK, now)
    rate = max(nominal, w["used"] / elapsed) if elapsed >= 1 else nominal
    remaining = hours(now, w["reset"])
    rate *= min(1, 4 * elapsed / (elapsed + remaining))
    return 100 - w["used"] - rate * remaining


def fast_tier(w, now):
    # decided at every run: if the fast tier spends too much the spare drops and runs go back to the default tier
    return w["used"] <= FAST_ALWAYS or spare(w, now) >= -FAST_SLACK


def astra(w, now):
    # decided at every run: every Astra run lowers the spare, and below the threshold runs go back to Sol
    days = (w["reset"] - now) / 86400
    return days <= ASTRA_DAYS and spare(w, now) >= ASTRA_PER_DAY * days


def claude_margin(claude):
    return bool(claude) and claude["used"] <= CLAUDE_WEEK_MAX and claude.get("five", 0) < CLAUDE_FIVE_MAX


def claude_band(claude, now):
    """ample, normal or tight: how much the Claude agents may spend. Early in the week the spare is optimistic and
    the usage decides; late in the week the usage is high even with free quota and the spare decides."""
    if not claude:
        return "normal"
    used, five, left = claude["used"], claude.get("five", 0), spare(claude, now)
    if used > 75 or five >= 60 or left < 0:
        return "tight"
    if used <= 50 and five < 30 and left >= 10:
        return "ample"
    return "normal"


def codex_parallel(w, now):
    """Codex runs in parallel: 3 under 70%, 2 above it but within budget (or usage unreadable), 1 over budget."""
    if not w:
        return 2
    if w["used"] < FAST_ALWAYS:
        return 3
    return 2 if fast_tier(w, now) else 1


def ladder_line(claude, now):
    band = claude_band(claude, now)
    picks = " · ".join(f"{c} {m} {e}" for c, (m, e) in ((c, LADDER[c][band]) for c in LADDER))
    return f"Claude agents' model and effort (Claude quota {band}, at most {CLAUDE_PARALLEL[band]} in parallel): {picks}."


def route(codex, claude, now):
    """('claude' | 'codex', reason) for the medium tasks Claude would delegate on its own."""
    if codex and codex["used"] >= CODEX_MAX:
        return "claude", "Codex is used up"
    if not codex:
        return "codex", "Codex usage unknown, default tier"
    if codex["used"] < FAST_ALWAYS:
        return "codex", "Codex under 70% of its week, fast tier"
    if fast_tier(codex, now):
        return "codex", "fast tier, within budget"
    if codex["used"] >= CODEX_RESERVE and claude_margin(claude):
        return "claude", "Codex kept for its connectors, Claude has margin"
    return "codex", "slower tier, over budget"


def describe(name, w, now):
    if not w:
        return f"{name} unknown"
    s = f"{name} {w['used']:.0f}% weekly, spare {spare(w, now):.0f} pts until {datetime.fromtimestamp(w['reset']):%a %H:%M}"
    return s + (f", 5h {w['five']:.0f}%" if "five" in w else "")


def route_line(cwd, codex, claude, now, cfg):
    """The hook's line for a prompt in cwd."""
    if not codex_allowed(cwd, cfg):
        return (f"quota route: claude (not a {cfg['label']} folder: sub-codex and {cfg['skills']} run only there)."
                f" Do every task with Claude, also when the user asks for Codex. {ladder_line(claude, now)}")
    side, why = route(codex, claude, now)
    only = and_list(cfg["codex_only"])
    if side == "claude":
        action = (f"Medium tasks go to Claude agents. Use sub-codex and {cfg['skills']} as usual: sub-codex.sh does"
                  " not start Codex, exits 5 and prints one Agent call (sub-claude, or the Claude agent the calling"
                  " skill names, e.g. device-tester). Exit 5 is not a Codex failure: make that call as printed, then"
                  " go on with the calling skill's steps; tell the user in one line that the task runs on Claude and why.")
        action += (f" Only {only + ' runs' if only else 'runs that need Codex'} (--needs-codex) still go to Codex."
                   if why.startswith("Codex kept") else
                   f" {only or 'Runs that need Codex (--needs-codex)'} cannot run until Codex resets: say so.")
    elif why.startswith("slower"):
        action = ("Hand medium tasks to Codex as usual: sub-codex picks the slower tier by itself. Only an urgent task"
                  " may go to Claude instead; if unsure it is urgent, ask the user.")
    else:
        action = "Hand medium tasks to Codex as usual."
    if side == "codex":
        action += f" At most {codex_parallel(codex, now)} Codex runs in parallel."
    if side == "codex" and codex and astra(codex, now):
        action += (" Astra is on (checked again at every run): pass --important to implementations, code reviews and"
                   " merges; complex implementations, subtle diagnoses and critical fixes go to Codex too, and Claude"
                   " reviews the result.")
    return (f"quota route: {side} ({why}) - {describe('Claude', claude, now)} · {describe('Codex', codex, now)}. {action}"
            f" {ladder_line(claude, now)}")


def main():
    now = time.time()
    if sys.argv[1:] == ["--language"]:
        print(settings()["language"])
        return
    codex = read_codex(now)
    if sys.argv[1:] == ["--codex-tier"]:
        if codex:
            print(f"{codex['used']:g} {'fast' if fast_tier(codex, now) else 'default'}"
                  f" {'astra' if astra(codex, now) else 'sol'}")
        return
    if sys.argv[1:] == ["--sub-route"]:
        side, why = route(codex, read_claude(now), now)
        print("codex" if side == "codex" else "claude usedup" if why.endswith("used up") else "claude reserve")
        return
    if sys.argv[1:2] == ["--claude-pick"]:
        kind = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in LADDER else "judgement"
        print(*LADDER[kind][claude_band(read_claude(now), now)])
        return
    try:
        cwd = json.load(sys.stdin).get("cwd") or os.getcwd() if not sys.stdin.isatty() else os.getcwd()
    except Exception:
        cwd = os.getcwd()
    print(route_line(cwd, codex, read_claude(now), now, settings()))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--folder"]:
        # fails closed: an error here exits nonzero, and sub-codex.sh refuses the folder
        sys.exit(check_folder(sys.argv[2] if len(sys.argv) > 2 else os.getcwd()))
    try:
        main()
    except Exception:
        pass  # the hook must never disturb the prompt
