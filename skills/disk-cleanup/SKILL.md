---
name: disk-cleanup
description: Frees disk space on a developer's machine without losing work - finds the ignored build output (node_modules, build, target, .venv, .gradle…) of git checkouts nobody touched for two weeks, stale Xcode DerivedData and device symbols, unavailable simulators, Gradle versions no project uses, package-manager caches (npm, yarn, pnpm, pip, Homebrew, CocoaPods, Docker), idle ~/Library/Caches entries and huge files in Downloads and Desktop. Prints a numbered report with sizes and deletes only what the user picks, re-checking each item right before. macOS first, works on Linux. Use for "/disk-cleanup", "disk full", "free up space", "what is taking up space", "clean the disk", "disco pieno", "libera spazio", "pulisci il disco", "cosa occupa spazio", "non ho più spazio". Not for cleaning a single project's build (run its own clean task) or deleting personal files the user has not looked at.
---

# disk-cleanup

`scan` only reads. `clean` deletes only the numbers the user picked, re-checks each one right before and skips,
with the reason, anything that changed since the scan. Approval is per run: never reuse a previous answer, never
widen the rules (more categories, other folders, `rm` by hand) to free more space.

## 1. Scan

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/disk_cleanup.py" scan
```

It takes a minute or two (it measures every candidate) and works inside the sandbox. Options: `--roots ~/work,~/oss`
(folders under home holding the checkouts; default the existing ones of ~/Documents, ~/Developer, ~/Projects,
~/code, ~/src, ~/dev), `--days 14` (how long a checkout must be untouched), `--derived-days 7`, `--cache-hours 24`.
The last line names the plan file: keep it for step 3. Each scan writes a new plan, named with its time. A plan
older than 24 hours is refused.

What it proposes, section by section:

| Section | What | Safe? |
|---|---|---|
| checkout-build | ignored `node_modules`, `build`, `dist`, `.next`, `.nuxt`, `.turbo`, `target`, `.gradle`, `.kotlin`, `.dart_tool`, `.venv`, `venv`, `__pycache__`, `.parcel-cache`, `Pods`, `DerivedData` of git checkouts untouched for `--days`. Only folders git ignores and that hold no tracked file; sources, `.git` and the checkout the session runs in are never touched | yes |
| xcode | DerivedData whose project is gone or unused for `--derived-days`; DeviceSupport except the newest per platform; simulators whose runtime is gone (`xcrun simctl delete unavailable`) | yes |
| gradle | wrapper distributions and per-version caches no scanned project uses; the build cache | yes |
| packages | npm, yarn, pnpm, pip, Homebrew, CocoaPods caches, emptied by the tool's own command. Docker images and build cache (`docker system df`), never volumes | yes; Docker ⚠ by number only |
| cache | `~/Library/Caches` entries with nothing touched for `--cache-hours`. Apple's own (except Xcode's `com.apple.dt.*`), browser installs (Playwright, Cypress), Google, JetBrains and pypoetry are left out; a cache in use stays whole | yes |
| look | files and folders of 1 GB or more at the top of Downloads and Desktop; Xcode archives | ⚠ by number only |

It also prints heavy folders it only measures (Android emulators and system images, `~/.cache`, CoreSimulator) and
warns when Time Machine local snapshots will hold the freed space for a while. It never reads
`~/Library/Containers`, `~/Library/Group Containers` or `~/Pictures`, so macOS asks for no permission.

## 2. Show and ask

Show the report as it is: the sections with their totals, the numbered rows, the `--all-safe` total. Then ask what
to delete, offering:

- **all safe** (`--all-safe`): every row of the safe sections;
- **some sections** (`--cat checkout-build,xcode`);
- **some numbers** (`--ids 1,4,7-12`): the only way to delete a ⚠ row (Docker, Downloads, Desktop, archives).

For a ⚠ row, say what is lost: an Xcode archive is needed to symbolicate that build's crashes; a file in Downloads
may be the only copy. Never pick for the user, and never turn "clean it up" into ⚠ rows.

The numbers belong to the report just shown, with its plan file. After any new scan, show the new report and ask
again: never apply numbers the user picked from an earlier one. A range (`7-12`) that covers ⚠ rows: name each of
them and get a yes for each before cleaning.

To protect something for good, the user puts a `.disk-cleanup-keep` file in the folder: empty, it protects the
folder and everything under it; with patterns (one per line, `#` for comments, e.g. `*.mov`), only the matching
entries below it. The scan never proposes them, and clean refuses them and any folder that holds a keep file
deeper down. A keep file cannot protect the package caches or Docker: their own commands empty them.

## 3. Clean

Clean must run **outside the Claude Code sandbox** (Bash with `dangerouslyDisableSandbox: true`): it deletes outside
the working directory, and it reads the running processes so it can skip what a build or a Gradle daemon is using.
Inside the sandbox those rows are skipped with "cannot read the running processes", so `--dry-run` runs outside it
too. That call asks the user: declined, stop and say nothing was deleted. Never fall back to `rm` or another route.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/disk_cleanup.py" clean --plan <plan file> --ids 1,4,7-12
python3 "${CLAUDE_SKILL_DIR}/scripts/disk_cleanup.py" clean --plan <plan file> --all-safe --dry-run
```

Use `--dry-run` first when the user wants to see what would go. Each row prints `✓` (deleted), `–` (skipped, with
why: modified after the scan, the checkout was used again, a process uses it, protected, gone, outside its
category's folders, path through a symlink) or `✗` (failed, with the error).

## 4. Report back

Say how much was freed and how much is free now (the script's last line), list the skipped rows with their reason
in a few words, and the next steps when they apply: a new scan if the plan expired, the Time Machine note if the
free space did not grow.

Left out on purpose: a scheduled nightly run. Cleaning stays a choice the user makes each time.
