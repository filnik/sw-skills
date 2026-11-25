---
name: memguard
description: Keeps a runaway command started by an AI agent from taking the Mac down - installs a small background guard (a user LaunchAgent) that watches only the processes launched by Claude Code or Codex and kills one whose memory explodes, before macOS runs out of memory; then tells what it killed and why. macOS only. Use for "/memguard", "install memguard", "is memguard running", "what did memguard kill", "a command ate all the RAM", "the Mac froze from memory", "stop runaway processes", "il Mac si è bloccato per la RAM", "un comando si è mangiato la memoria", "cosa ha killato memguard". Not for memory used by apps you opened yourself (it never touches them) or for disk space (disk-cleanup).
---

# memguard

Agents run many commands at once, unattended. One bad one is enough: a regex `git grep` over a big repo, started by
an agent, grew about 5 GB per second and took a 48 GB Mac to a restart in about 35 seconds. macOS closed hundreds
of its own background services first and never touched the culprit. memguard watches every second and kills it
within a few seconds instead.

**What it watches**: processes that have Claude Code (native install, `~/.local/share/claude/versions/`) or Codex
(a binary named `codex`) among their ancestors, including Claude Code's embedded ripgrep. A process stays watched if
its agent exits. Commands the user types in a terminal, other apps and the agents themselves are never touched.

**When it kills** (SIGKILL), on the memory macOS charges to the process (Activity Monitor's "Memory" column):

- 20 GB or more **and** at least 6 GB more than 5 seconds earlier: big and still growing fast;
- 30 GB or more, whatever the speed.

Below 20 GB nothing is killed, however fast it grows: a Gradle or Kotlin daemon bursting toward its `-Xmx` is safe.

## Install, update

Outside the Claude Code sandbox (it writes `~/Library/LaunchAgents` and talks to launchd), so the call asks the user:

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/install.sh"
```

It copies the script to `~/.local/share/memguard/`, writes `~/Library/LaunchAgents/local.memguard.plist` and starts
it; it starts again at every login and after a crash. Running it again updates the copy. Check the first line of
`~/Library/Logs/memguard.log`: `started: kill agent processes above 20 GB …`.

## Status and what it killed

```bash
launchctl print gui/$(id -u)/local.memguard | grep -E '^\s*(state|pid) ='
grep KILLED ~/Library/Logs/memguard.log | tail
python3 ~/.local/share/memguard/memguard.py --list
```

`--list` shows the agents found and the biggest processes watched; run it outside the sandbox, which hides other
processes. Each `KILLED` line has the size, the growth, the full command and the parent chain up to the agent: say
which command it was and from which session, and look for the cause, since memguard only stops the damage (the
`git grep` above stays under 1 GB with `LC_ALL=C` in front). The user also got a macOS notification.

## Change the rules

The thresholds are the constants at the top of `scripts/memguard.py` (`MIN_GB`, `GROWTH_GB`, `WINDOW_S`, `HARD_GB`);
edit them there and run `install.sh` again. Keep `MIN_GB` a few GB above the largest `-Xmx` the user's builds set
(`org.gradle.jvmargs` in `gradle.properties`, Android Studio's `studio.vmoptions`), and `HARD_GB` above anything
they run on purpose, such as a local AI model.

## Remove

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/install.sh" --uninstall
```

## Limits

- Claude Code installed with npm runs as `node` and is not recognized; the native installer is.
- A job started detached (`nohup cmd &`) whose shell exits before the next check, within a second, is not watched.
- It sees only the user's own processes, not root's.
