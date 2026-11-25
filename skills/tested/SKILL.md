---
name: tested
description: Tells whether the current DocuSketch mobile PR was tested on the real test phones (Galaxy A71, Xiaomi Redmi Note 12S, Pixel 9, iPhone 16) - one table of which check passed on which phone and on which build, the phones still missing, and whether the change needs one phone, two at once (sync between phones) or both Android and iOS. Reads the device-test ledger and this conversation; read-only. Use for "/tested", "was the PR tested?", "which phones did we try it on?", "what is left to test?", "does it need a test on iOS too?", "la PR è stata testata?", "su che telefoni l'abbiamo provata?", "cosa manca da testare?", "serve provarla anche su iOS?". Not for running a test (device-test) or putting the build on a phone (build).
---

# tested

Runs in the main session, not through Codex or a subagent: one of its two sources is this conversation, which only
this session sees, and the other is a script that prints ~30 lines.

## 1. The PR

The mobile clone this conversation works on (e.g. `mobile-7`), or the clone or ticket given as argument. None in the
conversation and no argument: ask which one. For a stacked PR pass its base: `--base origin/<base branch>`.

## 2. The ledger

`python3 ~/.claude/skills/tested/scripts/tested.py --repo <clone> [--base origin/<base>] [--ticket PHX-…]`

It prints what the PR changes per platform (`both` = shared code, `android`, `ios`, `build` = gradle, `off` = tests,
desktop, web, CI), then each device-test scenario run on the PR's builds or named after its ticket: its checks with
their text, and per phone the latest verdict of each check and the build it ran on (`HEAD`, or `B1`, `B2`… listed at
the end with the PR commits made after it). Red proofs come on their own line: they are not tests of the PR.

## 3. This conversation

Add what the ledger does not hold: tests the user did by hand ("I tried it on the Pixel, it works"), phone-logs-codex timelines
of their tries, device-tester verdicts. Each gets a row with its phone and what was seen; its build is the one installed
at that moment if the conversation says so, otherwise `build ?`. Installing a build (`/build`) is not a test.
Use what is in context (or the summary, after a compaction); do not open the transcript file.

## 4. What the PR needs

- **Platforms**, from the changes: `android` only → Android; `ios` only → iOS; `both` or `build` → Android and iOS when
  the changed path goes through platform code (expect/actual, files, camera, background work, network, permissions,
  notifications, UI); pure shared logic (mapping, rules, sync decisions) → one phone of either platform is enough.
  `off` only → no phone test needed: say so and stop.
- **Two phones at once** when a check involves two devices: a change made on one seen on the other, sync or conflicts
  between devices, the same account on two phones, a scenario with a colleague phone.
- **Which Android phone**: the Galaxy is the default. Another one is needed only when the ticket ties the problem to a
  model, OEM or Android version.
- **Checks**: the scenario's criteria, or the ticket's acceptance criteria if they are in the conversation. Do not open
  Jira just for this; without either, judge from the changes and say so.

A check counts as tested on a phone when it PASSed on `HEAD`, or on a build whose later PR commits (listed under
"builds tested") do not touch code that runs on that platform. For a `+uncommitted` build, the first commit right
after it is usually the tested work itself (minutes later, same files): count it as tested and say it is assumed.
Otherwise mark it ⚠️ and name the commit that changed the code afterwards.

## 5. Answer (in the user's language)

```
**PHX-3555 · mobile-7 · HEAD be8014e** — Testata in parte: iPhone sì, Galaxy sì, manca la parte Android di C2 su HEAD.

| Check | Galaxy A71 | Xiaomi 12S | Pixel 9 | iPhone 16 |
|---|---|---|---|---|
| C1 upload manuale, Home e blocco | ➖ | — | — | ✅ |
| C2 passata automatica, ForegroundService | ✅ | — | — | ➖ |
| a mano: upload con rete assente | — | — | ✅ user | — |
| Build | ⚠️ 76cf8ca, poi 63bf942 tocca both | — | ⛔ non raggiungibile | ✅ 76cf8ca (+ commit subito dopo) |

✅ PASS · ❌ FAIL · ⛔ BLOCKED · ➖ N/A · — non provato · ⚠️ codice cambiato dopo il test

**Mancano:** Galaxy, C2 e C4 su HEAD (63bf942 cambia BackgroundManagerImpl dopo il test).
**Serve:** Android e iOS, un telefono per piattaforma (Galaxy + iPhone); nessun check tra due telefoni.
```

- The verdict word is one of: Testata, Testata in parte, Non testata.
- One column per phone, always all four, so the missing ones show. Check labels: a few words in the user's language.
- A FAIL or BLOCKED keeps its reason in a line under the table; regression scenarios (`REG-…`) are one row each with
  their PASS count.
- **Mancano** lists only phones and checks the PR needs; **Serve** says one phone, two at once, or Android and iOS,
  and why in half a line.
