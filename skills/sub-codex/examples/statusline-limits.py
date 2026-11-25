#!/usr/bin/env python3
"""A minimal Claude Code statusline that saves Claude's rate limits for sub-codex's quota hook.

Claude Code passes `rate_limits` only to the statusline command, so quota-route.py reads them from the file this
writes: ~/.cache/claude-statusline/limits-<config dir name>.json, the config dir being $CLAUDE_CONFIG_DIR or
~/.claude. It prints "<model> · 5h N% · 7d N%".

    "statusLine": {"type": "command", "command": "python3 ~/.claude/skills/sub-codex/examples/statusline-limits.py"}

To keep a statusline of your own, call this from it with the same input and drop its output:
    printf '%s' "$input" | python3 ~/.claude/skills/sub-codex/examples/statusline-limits.py >/dev/null
"""
import json
import os
import sys

data = json.load(sys.stdin)
limits = data.get("rate_limits") or {}
five, week = limits.get("five_hour") or {}, limits.get("seven_day") or {}
if week.get("used_percentage") is not None:
    name = os.path.basename((os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")).rstrip("/"))
    path = os.path.expanduser(f"~/.cache/claude-statusline/limits-{name}.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"five_pct": five.get("used_percentage"), "five_at": five.get("resets_at"),
                       "week_pct": week.get("used_percentage"), "week_at": week.get("resets_at")}, f)
        os.replace(tmp, path)
    except OSError:
        pass

parts = [(data.get("model") or {}).get("display_name") or "?"]
for label, w in (("5h", five), ("7d", week)):
    if w.get("used_percentage") is not None:
        parts.append(f"{label} {w['used_percentage']:.0f}%")
print(" · ".join(parts))
