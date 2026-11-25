---
name: disk-cleanup
description: Frees disk space on a developer's machine without losing work - finds old screen recordings (Kapture, OBS, Kap's temp files), the ignored build output (node_modules, build, target, .venv, .gradle…) of git checkouts nobody touched for a while, clean idle checkouts of a configured pool, stale Xcode DerivedData and device symbols, unavailable simulators, Gradle versions no project uses, package-manager caches (npm, yarn, pnpm, pip, Homebrew, CocoaPods, Docker), idle ~/Library/Caches entries and huge files in Downloads and Desktop. Prints a numbered report with sizes and deletes only what the user picks, re-checking each item right before; an optional nightly mode removes only regenerable build output. macOS first, works on Linux. Use for "/disk-cleanup", "disk full", "free up space", "what is taking up space", "clean the disk", "delete the Kapture recordings", "disco pieno", "libera spazio", "pulisci il disco", "cosa occupa spazio", "non ho più spazio". Not for cleaning a single project's build (run its own clean task) or deleting personal files the user has not looked at.
---

# disk-cleanup

```
scan (reads only) → report → the user picks → clean (re-checks, deletes) → freed space
```

`scan` only reads. `clean` deletes only the numbers the user picked, re-checks each one right before and skips,
with the reason, anything that changed since the scan. Approval is per run: never reuse a previous answer, never
widen the rules (more categories, other folders, `rm` by hand) to free more space.

The script does the measuring and the safety checks; you show the report and ask. Do not run your own `du`/`find`
sweeps around it: a home folder holds hundreds of GB, they take minutes, and the scan already covers the places that
matter. Never read inside `~/Library/Containers`, `~/Library/Group Containers` or `~/Pictures` (the Photos library):
macOS asks "… would like to access data from other apps" every time. If the user wants those measured, the fix is
theirs: Full Disk Access for the terminal (System Settings → Privacy & Security → Full Disk Access); otherwise quote
their size from System Settings → General → Storage.

## 0. Config

The script reads the `disk_cleanup` block of `.claude/config/project.json`, the first one found from the current
folder upward that has it, and remembers where it found it for runs that start elsewhere (the nightly one). All keys
are optional; without a config the pool and device sections are left out.

```json
{"disk_cleanup": {
  "pools": [{"root": "~/work", "checkouts": ["app-[0-9]*", "ws-*/*"], "claims": ".pick/claims.json",
             "build_dirs": ["build", ".gradle"]}],
  "video_dirs": [{"path": "~/work/product", "skip": ["assets"]}],
  "signed_apps": "~/.cache/signed-apps",
  "phone_db_copies": "~/.cache/phones/*/db",
  "measure": ["~/work/.app.git"],
  "defaults": {"derived_days": 2}}}
```

- `pools`: folders of checkouts a script creates and reuses, as glob patterns under `root`. `claims` is a JSON file
  `{"<checkout path>": {"pid": N}}`: a live pid reserves that checkout. `build_dirs`, when set, are the only build
  folder names its checkouts lose (default: every build folder name the scan knows).
- `video_dirs`: folders whose videos are offered too, only by number; `skip` names subfolders left out.
- `signed_apps`: one folder per signed app copy, named like its checkout or holding a `checkout` file with its path.
- `phone_db_copies`: folders of copies of a phone's database, one folder per copy: all but the newest go.
- `measure`: more folders measured in INFO. `defaults`: option defaults, such as `{"derived_days": 2}`.

`python3 "${CLAUDE_SKILL_DIR}/../../shared/project_config.py" section disk-cleanup` prints this skill's section of
the playbook the config names, and nothing else of it: follow it. Where the section and this skill both speak, the
section wins.

## 1. Scan

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/disk_cleanup.py" scan
```

Foreground, with a long timeout (`timeout: 600000`): one to three minutes, progress on stderr. It works inside the
sandbox, where it cannot read the running processes and says so; clean, run outside it, checks them before every
deletion. Options, every threshold at least 1:

- `--roots ~/work,~/oss`: folders under home holding the checkouts (default the existing ones of ~/Documents,
  ~/Developer, ~/Projects, ~/code, ~/src, ~/dev);
- `--days 14`: how long a checkout outside the pools must be untouched; also the Gradle build cache and signed apps;
- `--pool-days 20`: how long a pool checkout must be clean and idle to go whole;
- `--checkout-days 1` (not touched today) and `--dirty-checkout-days 7`: when a pool checkout's build output goes;
- `--derived-days 7`, `--cache-hours 24`, `--recording-days 7`, `--kap-days 5`, `--kap-exported-days 2`.

The last line names the plan file: keep it for step 3. Each scan writes a new plan, named with its time. A plan
older than 24 hours is refused.

What it proposes, section by section:

| Section | What | Safe? |
|---|---|---|
| recordings | older than `--recording-days` and not tracked by git: `Kapture*` videos in Downloads, Desktop, Movies and the video folders; every video in `~/Movies`, where OBS records (TV, iMovie and Photos libraries skipped). Kap's leftovers in the user temp dir (`getconf DARWIN_USER_TEMP_DIR`, not the sandbox `$TMPDIR`): raw recordings (`<32 hex>.mp4`) after `--kap-days`, after `--kap-exported-days` once exported, and export folders (`<32 hex>/Kapture … at H.MM.SS.mp4`, made by "copy to clipboard") after `--kap-exported-days`. "transcribed": a `.srt` or `.txt` sits next to it and stays | Kapture, Kap and OBS-named videos yes; other videos in Movies and every video of a video folder ⚠ by number only |
| checkouts | pool checkouts with empty `git status`, no stash, no commit missing from every remote, no local remote, idle for `--pool-days` (reflog, index, HEAD, top-level entries), not claimed, not held by a process, not the folder the session runs in. A worktree's branches live in its main repo, so for a worktree only a detached HEAD counts; a main checkout goes only with all its worktrees. Ignored files no build brings back (`.env`, keystores, a DVC cache, hand-written `local.properties`) make it ⚠ | yes, ⚠ ones by number |
| checkout-build | pool checkouts: ignored build folders (`build`, `.gradle`, `.kotlin`, `node_modules`…, any depth) of a checkout nothing touched today, builds included (`--checkout-days` widens it), or for `--dirty-checkout-days` when it has uncommitted changes: they stay where they are. Other git checkouts: the same folders after `--days` untouched. Only folders git ignores and that hold no tracked file; sources, `.git` and the checkout the session runs in are never touched | yes |
| xcode | DerivedData whose project is gone, belongs to a checkout above, or is unused for `--derived-days` (Xcode's `LastAccessedDate`); DeviceSupport except the newest per platform; simulators whose runtime is gone (`xcrun simctl delete unavailable`) | yes |
| gradle | wrapper distributions and per-version caches no scanned project uses; the build cache | yes |
| devices | signed app copies whose checkout is gone or listed above, or signed `--days` ago; copies of a phone's DB but the newest of each phone and any from the last hour | yes |
| packages | npm, yarn, pnpm, pip, Homebrew, CocoaPods caches, emptied by the tool's own command. Docker images and build cache (`docker system df`), never volumes | yes; Docker ⚠ by number only |
| cache | `~/Library/Caches` entries with nothing touched for `--cache-hours`. Apple's own (except Xcode's `com.apple.dt.*`), browser installs (Playwright, Cypress), Google, JetBrains and pypoetry are left out; a cache in use stays whole | yes |
| look | files and folders of 1 GB or more at the top of Downloads and Desktop, files of 1 GB or more at the top of Documents; Xcode archives | ⚠ by number only |

Pool checkouts it keeps are listed under "kept" with their size and reason ("3 modified or untracked files",
"2 commits only here", "remote on this disk", "clean, but touched 4 days ago"): that list is often where most of the
space sits, and freeing it is the user's call, checkout by checkout.

It also prints heavy folders it only measures (Android emulators and system images, `~/.cache`, `~/.gradle`,
`~/.konan`, CoreSimulator, DerivedData, the config's `measure`, non-git folders in a pool) and warns when Time
Machine local snapshots will hold the freed space for a while.

## 2. Show and ask

Show the report as it is: the sections with their totals, the numbered rows, the `--all-safe` total, then two or
three lines on the biggest items and anything in TO LOOK AT or INFO worth a decision. Then ask what to delete,
offering:

- **all safe** (`--all-safe`): every row not marked ⚠;
- **some sections** (`--cat recordings,checkout-build`);
- **some numbers** (`--ids 1,4,7-12`): the only way to delete a ⚠ row.

For a ⚠ row, say what is lost: an Xcode archive is needed to symbolicate that build's crashes; a file in Downloads
may be the only copy; a video in a video folder may still be needed. Never pick for the user, and never turn
"clean it up" into ⚠ rows. A checkout kept for untracked files stays kept even when they look like junk: say what is
in it and let the user decide; if they want it gone anyway, show what would be lost (`git status`,
`git log --branches --not --remotes`) and delete that one path only on their explicit yes.

The numbers belong to the report just shown, with its plan file. After any new scan, show the new report and ask
again: never apply numbers the user picked from an earlier one. A range (`7-12`) that covers ⚠ rows: name each of
them and get a yes for each before cleaning.

To protect something for good, the user puts a `.disk-cleanup-keep` file in the folder: empty, it protects the
folder and everything under it; with patterns (one per line, `#` for comments, e.g. `*.mov`), only the matching
entries below it. Scan, clean and the nightly run never touch them, nor any folder that holds a keep file deeper
down. A keep file cannot protect the package caches or Docker: their own commands empty them.

## 3. Clean

Clean must run **outside the Claude Code sandbox** (Bash with `dangerouslyDisableSandbox: true`): it deletes outside
the working directory, and it reads the running processes so it can skip what a build, a Gradle daemon or a session
is using. Inside the sandbox those rows are skipped with "cannot read the running processes", so `--dry-run` runs
outside it too. That call asks the user: declined, stop and say nothing was deleted. Never fall back to `rm` or
another route.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/disk_cleanup.py" clean --plan <plan file> --ids 1,4,7-12
python3 "${CLAUDE_SKILL_DIR}/scripts/disk_cleanup.py" clean --plan <plan file> --all-safe --dry-run
```

Use `--dry-run` first when the user wants to see what would go. Each row prints `✓` (deleted), `–` (skipped, with
why: modified after the scan, the checkout was used again or has uncommitted changes now, a process uses it or a
session claimed it, protected, gone, outside its category's folders, path through a symlink) or `✗` (failed, with
the error). Each category deletes only inside its own folders, whatever the plan says. Worktrees go before their
main checkout; `git worktree prune` then runs in the main repo.

## 4. Report back

Say how much was freed and how much is free now (the script's last line), list the skipped rows with their reason
in a few words, and the next steps when they apply: a new scan if the plan expired, the Time Machine note if the
free space did not grow.

## Nightly run (optional)

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/disk_cleanup.py" nightly [--idle-hours 8] [--dry-run]
```

For a scheduler (launchd, cron), with no plan and nobody to ask. It deletes only:

- the build output of pool checkouts untouched for `--idle-hours` (8: "not touched today" would hit last night's
  work at dawn), or for `--dirty-checkout-days` with uncommitted changes, that no running process holds and no live
  claim reserves;
- DerivedData whose checkout just lost its build, or that is orphaned or unused for `--derived-days`;
- copies of a phone's DB but the newest, and signed app copies whose recorded checkout is gone and that were not
  signed in the last `--idle-hours` (a copy that records no checkout is left to scan);
- Kap's temp recordings, as in recordings.

Each item gets the same re-check as clean. When the processes cannot be read it touches no checkout. It prints what
it did and records the run, so scan says when the last nightly ran. It finds the config the last interactive run
found: run scan once from the project folder before scheduling it. On macOS, a job launchd starts directly is
refused `~/Documents`; a small app that holds that permission and runs the script works, and the log line
"macOS denies access" means the permission is gone.
