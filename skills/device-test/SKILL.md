---
name: device-test
description: Tests your Android or iOS app on real phones through scripted scenarios that run whole in one call and print one line per step and one verdict (PASS, FAIL, BLOCKED, SCENARIO ERROR), instead of driving the phone tap by tap. The main session writes a test plan from the ticket and the diff; the device-tester agent writes or reruns the scenario, runs it and returns a short verdict table with evidence. Leases per phone, several phones side by side, red proof for bugfixes, evidence only on failure. Use for "/device-test", "test it on the phone", "verify on Android/iPhone", "write a scenario for …", "rerun the scenario", "red proof", "who is using the phone", "testa sul device", "prova sul telefono", "verifica su Android/iPhone", "scrivi uno scenario", "rilancia lo scenario", "prova del rosso", "chi sta usando il telefono". Not for simulators, emulators-only UI checks in CI, or unit tests.
---

# device-test

```
ticket + diff → test plan → device-tester agent → scenario runs whole → verdict table
```

Driving a phone tap by tap costs one round trip per tap, each re-reading the whole context. Here a scenario is a
Python file in your project that `devtest run` executes in one Bash call, printing ~10 lines. The engine refuses to
pass what it did not look at: every criterion is covered or skipped with a reason, an absence counts only if the same
thing is seen present in the run, a check the phone cannot observe is `N/A`, never PASS, and a failure caused by the
phone (it dropped off, another app took the front, the log capture died) is BLOCKED, never blamed on the app.

The tool is `python3 ${CLAUDE_SKILL_DIR}/scripts/devtest.py` (stdlib only; `--help` lists every command).

## Setup (once per project)

Add a `device_test` block to `.claude/config/project.json` in the repo (the same file `pick` reads). Only the keys a
command uses are needed:

```json
"device_test": {
  "android": {"package": "com.example.app", "build": "./gradlew :app:assembleDebug",
              "apk": "app/build/outputs/apk/debug/app-debug.apk",
              "activity": ".MainActivity", "prefs": "files/datastore/settings.preferences_pb", "db": "app.db"},
  "ios": {"bundle_id": "com.example.app", "team_id": "ABCDE12345",
          "build": "xcodebuild -scheme App -destination generic/platform=iOS -derivedDataPath build build",
          "app": "build/Build/Products/Debug-iphoneos/App.app",
          "prefs": "Library/Preferences/com.example.app.plist", "db": "Library/Application Support/app.sqlite"},
  "phones": {"pixel": "<adb serial>", "iphone": "<UDID>"}
}
```

- **Android**: `adb` on the PATH, USB debugging on. Give screens and controls test tags (`Modifier.testTag("login.submit")`,
  screens `screen.<name>`); in Compose set `testTagsAsResourceId = true` once at the root so uiautomator sees them.
- **iOS** (physical iPhone over USB): Xcode, the phone in Developer Mode and paired, your `team_id`, and
  WebDriverAgent sources: `npm install --prefix ~/.cache/devtest/ios/wda-src appium-webdriveragent`. devtest builds
  WDA once with your team. Tags are `accessibilityIdentifier`s.
- adb, usbmuxd and `devicectl` need the USB devices: if Claude Code isolates Bash commands, add the devtest command
  to `sandbox.excludedCommands`.
- Scenarios live in the project, `device-tests/<name>.py`; shared steps in `device-tests/flows/*.py`.
  A commented example: `${CLAUDE_SKILL_DIR}/examples/login.py`.

## Rules

1. **The main session does not drive phones.** Writing, running and rerunning scenarios, and the red proof, go to
   the `device-tester` agent (`dev:device-tester` once this plugin is installed) with the plan below. It runs on
   Sonnet with its own context, so the phone output never fills this one.
2. Claude may run the read-only commands itself: `devices`, `lease status`, `report`, `flows`, `build-info`.
   `build-info` waits for a phone that is not plugged in: give that Bash call a timeout above 300000.
3. A phone leased by another session: ask the user before taking it. When the user agrees, release it yourself,
   `python3 <tool> --device <alias> lease release --force`, then delegate: the agent never forces a lease. A phone
   that is not connected: devtest posts a macOS notification asking to plug it in and waits up to 5 min
   (`DEVTEST_PLUG_WAIT`, `0` fails at once).

## Test plan (main session → agent)

Write it from the ticket, the diff and the intended behaviour, to a scratchpad file. The agent tests these checks and
nothing else; it adds only the navigation and setup they need, and a check it cannot do as written comes back
BLOCKED with the reason, never swapped for another.

```
Ticket: <id> · <title>
Plan file: <absolute path of this file>
Tool: python3 <absolute path of ${CLAUDE_SKILL_DIR}/scripts/devtest.py>
Worktree: <path under test> · red proof on: <base build or worktree | none>
Phones: <aliases> · owner: device-tester-<id>
Scenario: new device-tests/<id>.py | existing device-tests/<file> (run as is | extend with AC4)
Setup: account (from env vars, never in the file), data to prepare, network state
Checks (one per line, keep the IDs):
  AC1 <action> → <expected, with its evidence: screen tag or text, log line, pref, db row, file>
  AC2 …
Not to test: <what and why>
```

- The `Tool:` line carries the substituted path: an agent body does not get `${CLAUDE_SKILL_DIR}`. The `Plan file:`
  line is the path the agent passes to `devtest new`.
- Every check names its evidence. One a phone cannot observe is `N/A on ios (why)` or goes under "Not to test".
- A rerun is a plan too: scenario, phones, worktree, and the checks (`--only AC2,AC3`) if not all.
- The same checks on several phones are one plan: the agent runs them side by side in one call.

Launch the agent with the plan as its prompt, in the background: runs often take several minutes.

## Report (agent → main session)

```
<id> · pixel PASS 4/4 · iphone FAIL step 3
AC1 PASS · AC2 FAIL (iphone: no log /session started/ within 15s) · AC3 N/A on ios (Keychain) · AC4 SKIP (no fingerprint)
evidence: ~/.cache/devtest/runs/…/iphone/step-3.{png,ui.txt,log}
not tested: … (why)
scenario: device-tests/<id>.py (new|changed|unchanged)
flows: <added or changed, or unchanged>
```

Relay it to the user. Open the evidence only for a FAIL whose line does not explain it. Exit codes of `run`:
0 PASS · 1 FAIL (the app) · 2 BLOCKED (phone, tooling, lease, stale build) · 3 SCENARIO ERROR (the scenario).
