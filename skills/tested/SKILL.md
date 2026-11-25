---
name: tested
description: Tells whether the current mobile PR was tested on the project's real test phones (the device-test pack's) - one table of which check passed on which phone and on which build, the phones still missing, and whether the change needs one phone, two at once (sync between phones) or both Android and iOS. Reads the device-test ledger and this conversation; read-only. Use for "/tested", "was the PR tested?", "which phones did we try it on?", "what is left to test?", "does it need a test on iOS too?", "la PR è stata testata?", "su che telefoni l'abbiamo provata?", "cosa manca da testare?", "serve provarla anche su iOS?". Not for running a test (device-test) or putting the build on a phone.
---

# tested

Runs in the main session, not through Codex or a subagent: one of its two sources is this conversation, which only
this session sees, and the other is a script that prints ~30 lines.

First print this skill's section of the project's playbook with
`python3 "${CLAUDE_SKILL_DIR}/../../shared/project_config.py" section tested`: where it and this skill both speak,
the section wins.

## 1. The PR

The checkout this conversation works on, or the checkout or ticket given as argument. None in the conversation and
no argument: ask which one. For a stacked PR pass its base: `--base origin/<base branch>`.

## 2. The ledger

`python3 ~/.claude/skills/tested/scripts/tested.py --repo <clone> [--base origin/<base>] [--ticket ABC-123]`

It prints what the PR changes per platform (`both` = shared code, `android`, `ios`, `build` = gradle, `off` = tests,
desktop, web, CI), then each device-test scenario run on the PR's builds or named after its ticket: its checks with
their text, and per phone the latest verdict of each check and the build it ran on (`HEAD`, or `B1`, `B2`… listed at
the end with the PR commits made after it). Red proofs come on their own line: they are not tests of the PR.

The phones are the device-test pack's aliases. The script reads the `tested` block of the project's
`.claude/config/project.json`, found from the clone upward (all keys optional):

```json
{"tested": {"phones": ["pixel", "tablet", "iphone"],
            "paths": {"android": ["^legacy-app/"], "ios": ["^iosApp/"], "off": ["^screenshots/"]}}}
```

- `phones`: the phones to report, in column order; default all the pack's phones, in the pack's order.
- `paths`: regexes for the project's own folders, added per bucket to the defaults (Kotlin Multiplatform source
  sets, Swift and Gradle files). Buckets are checked in the order off, both, android, ios, build; the first match
  wins, and a file nothing matches is `off`.

## 3. This conversation

Add what the ledger does not hold: tests the user did by hand ("I tried it on the Pixel, it works"), timelines read
from the phone's logs of their tries, device-tester verdicts. Each gets a row with its phone and what was seen; its
build is the one installed at that moment if the conversation says so, otherwise `build ?`. Installing a build is
not a test. Use what is in context (or the summary, after a compaction); do not open the transcript file.

## 4. What the PR needs

- **Platforms**, from the changes: `android` only → Android; `ios` only → iOS; `both` or `build` → Android and iOS when
  the changed path goes through platform code (expect/actual, files, camera, background work, network, permissions,
  notifications, UI); pure shared logic (mapping, rules, sync decisions) → one phone of either platform is enough.
  `off` only → no phone test needed: say so and stop.
- **Two phones at once** when a check involves two devices: a change made on one seen on the other, sync or conflicts
  between devices, the same account on two phones, a scenario with a colleague phone.
- **Which Android phone**: the pack's default (`default_for` in its `pack.json`). Another one is needed only when the
  ticket ties the problem to a model, OEM or Android version.
- **Checks**: the scenario's criteria, or the ticket's acceptance criteria if they are in the conversation. Do not open
  the tracker just for this; without either, judge from the changes and say so.

A check counts as tested on a phone when it PASSed on `HEAD`, or on a build whose later PR commits (listed under
"builds tested") do not touch code that runs on that platform. For a `+uncommitted` build, the first commit right
after it is usually the tested work itself (minutes later, same files): count it as tested and say it is assumed.
Otherwise mark it ⚠️ and name the commit that changed the code afterwards.

## 5. Answer (in the user's language)

```
**ABC-123 · app-2 · HEAD be8014e** — Partly tested: iPhone yes, Pixel yes, the Android part of C2 on HEAD is missing.

| Check | pixel | tablet | iphone |
|---|---|---|---|
| C1 manual upload, Home and lock | ➖ | — | ✅ |
| C2 automatic pass, ForegroundService | ✅ | — | ➖ |
| by hand: upload with no network | — | ✅ user | — |
| Build | ⚠️ 76cf8ca, then 63bf942 touches both | ⛔ unreachable | ✅ 76cf8ca (+ the commit right after) |

✅ PASS · ❌ FAIL · ⛔ BLOCKED · ➖ N/A · — not tried · ⚠️ code changed after the test

**Missing:** pixel, C2 and C4 on HEAD (63bf942 changes BackgroundManagerImpl after the test).
**Needs:** Android and iOS, one phone per platform (pixel + iphone); no check between two phones.
```

- The verdict word is one of: Tested, Partly tested, Not tested.
- One column per phone, always all of them (the ones in the run blocks plus `phones with no run`), so the missing
  ones show. Check labels: a few words in the user's language.
- A FAIL or BLOCKED keeps its reason in a line under the table; regression scenarios (`REG-…`) are one row each with
  their PASS count.
- **Missing** lists only phones and checks the PR needs; **Needs** says one phone, two at once, or Android and iOS,
  and why in half a line.
