---
name: pr-review
description: Full review of a DocuSketch PR through parallel Codex runs, steered and checked by Claude - whether the PR does what its Jira ticket asks, duplicates or code that should reuse existing structures, test coverage (untested or badly tested parts), whether the change fits the app's flow, a code review for bugs, and a run of the ticket's checks on the user's phones (device-test). Claude reads the ticket, writes one brief and the device plan, starts the Codex runs together, verifies every finding against the code and reports one table. Use for "/pr-review 4801", "review the PR", "review a colleague's PR", "does the PR meet the ticket?", "full review with a phone test", "fai la review della PR", "revisiona la PR di un collega", "la PR soddisfa il ticket?", "review completa con test sul telefono". Not for answering comments on a PR (babysit), a code-only review (code-review-codex), or an overview of open PRs (check-prs).
model: opus
---

# pr-review

Claude spends its tokens where judgment is: the ticket, where the risk is, the device plan, checking every finding.
Four Codex runs, started together, do the reading, the grepping and the phones.

```
PR → checkout, ticket, risks (Claude) → brief + device plan
   → Codex in parallel: intent · tests · code · device
   → verify each stream as it lands (Claude) → at most one follow-up round → report → fixes or pending review
```

| Stream | Answers | Effort |
|---|---|---|
| intent | the ticket's requirements are met, and the change fits the app's flow | xhigh |
| tests | coverage: untested or badly tested parts | high |
| code | bugs, duplicated logic and missed reuse of existing structures | xhigh |
| device | the ticket's checks on the phones | xhigh |

Requirements and the macro analysis share a stream: same input (ticket, docusketch-product, the flows the change
reaches), and "fits the flow" is judged against the intended behaviour. The Codex instructions are in `codex/`:
Claude does not read them. `$S` is the Claude session's scratchpad, `<n>` the PR number: write both out in full.

## 1. PR and checkout

- `gh pr view <n> --repo <repo> --json number,title,url,author,headRefName,headRefOid,baseRefName,body,isDraft,additions,deletions,changedFiles`
  with the sandbox off (`gh-tls-outside-sandbox`). No number: the PR of the current checkout's branch. Repo default
  `docusketch/docusketch-mobile`. Mine = `author.login` equals `gh api user --jq .login`.
- Checkout: the `~/Documents/projects/docusketch/mobile-*` clone whose `git rev-parse HEAD` is `headRefOid`. The user's
  PR in the current checkout: review it there, working tree included (say so). None at the PR head:
  `python3 ~/.claude/skills/ds-pick/scripts/ds_pick.py free --base <headRefName> --ticket <KEY>` from the pool root,
  sandbox off, and give the user its `update_command`; go on only once that clone's HEAD is the PR head. Never
  switch, pull or reset a clone yourself.
- Snapshot: `git -C <checkout> rev-parse HEAD > $S/pr<n>-before.txt`, then
  `git -C <checkout> status --porcelain >> $S/pr<n>-before.txt` and
  `git -C <checkout> diff HEAD | shasum >> $S/pr<n>-before.txt`.

## 2. Ticket and risks (Claude)

- The key from the branch, title or body (`PHX-\d+`). Read the ticket yourself, never through a Codex summary:
  `.ds-pick/<KEY>/issue.md` under the pool root when it exists, otherwise Jira (description, status, fix version,
  links, and the comments: scope often changes there). Write the acceptance criteria as checks `AC1…`, in the
  ticket's words; none in the ticket: derive them from the description and mark them `derived`. No ticket: say so,
  and the intent stream judges against the PR body and docusketch-product.
- `git -C <checkout> diff --stat origin/<baseRefName>...HEAD`, then the hunks of the risky files only (the whole
  diff under ~300 lines).
- Risks, 2–6 lines: where this change can break (callers, sync and offline, migrations and existing installs,
  platform differences, a flag that must gate it), from the diff, the root `CLAUDE.md` routing table,
  `.github/critical-areas.md` and the matching `PR_LESSONS.md` categories.
- Decided: choices the ticket, the PR body or this conversation already made, so no stream reopens them.

## 3. Brief and device plan

`$S/pr<n>-brief.md`, in English, self-contained, one file for the three analysis streams:

1. "You are one reviewer of a PR review run in parallel. Read
   ~/.claude/skills/pr-review/codex/common.md, then codex/<stream>.md for the stream named at the end
   of this prompt, and follow them."
2. Target: repo, PR number and URL, checkout path, head SHA, base ref; whether the working tree counts.
3. Ticket: key, title, the checks `AC1…`, the scope and out-of-scope lines.
4. Risks and decided, from §2.
5. Builds: "A device run builds in this checkout: do not run Gradle or xcodebuild." Without a device stream: "You
   may run the unit tests of the touched modules."
6. Output dir: `$S/pr<n>/`.

`$S/pr<n>-device.md` is the device-test plan (its "Test plan" template), plus "Follow
~/.claude/agents/device-tester.md; Codex runs without a sandbox, so its sandbox notes do not apply":

- checks: the `AC`s a phone can observe, same IDs, each with its evidence; then 1–3 regression checks `R1…` on the
  flows the diff reaches;
- phones: the ones the user named; otherwise one Bash call, alone,
  `python3 ~/.claude/skills/device-test/scripts/dsdev.py --owner pr-review devices`: the Galaxy, plus the iPhone
  when connected, unless the diff touches one platform only. A phone leased by another owner: ask;
- worktree = the checkout, owner `codex-pr<n>`, red proof: none unless the user asks (it needs a base build).

No device stream when the repo is not docusketch-mobile, the change cannot show on a phone (CI, build scripts,
tests or docs only), no phone is connected, or the user said so. Say which.

## 4. Launch

In one message, each one Bash call alone with `run_in_background: true` (plain words after `--file`: see `sub-codex`):

```
bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh --full --important --cd <checkout> --name pr<n>-intent --file $S/pr<n>-brief.md "Stream: intent"
bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh --full --effort high --cd <checkout> --name pr<n>-tests --file $S/pr<n>-brief.md "Stream: tests"
bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh --full --important --cd <checkout> --name pr<n>-code --file $S/pr<n>-brief.md "Stream: code"
bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh --full --cd <checkout> --name pr<n>-device --file $S/pr<n>-device.md
```

`--full`: `gh` needs the keychain, the reports land in `$S`, the device run needs the phones. Tell the user in one
line what started (PR, checkout, phones). While they run, do not review the PR yourself.

## 5. Verify, as each stream lands

1. Read the summary in the notice; read `$S/pr<n>/<stream>.md` by the findings-first rule in `sub-codex` §After.
   For each one open the cited
   lines yourself and check its scenario against the code, the ticket and the decided list. Drop what does not
   hold, what reopens a decision, and style the repo's rules do not state.
   For each bug also check its `probabilità`, `incidenza` and `costo del fix` (scales in `codex/common.md`):
   recompute the probabilità from who reaches the path, since Codex tends to overstate it, and look at the minimal
   fix yourself. Then weigh them into a `consiglio`: `fix nella PR` · `fix facoltativo` · `ticket a parte` ·
   `lascia stare`. The harm has to be worth the change: `perdita di dati` or `dati sbagliati sul server` gets fixed
   unless it is `quasi impossibile`; a `raro` bug where `l'app recupera` or with `nessun effetto visibile` does not
   earn a `medio` or `grande` fix (`lascia stare`, or `ticket a parte` if it can grow); a real bug with a `1–2 righe`
   fix is `fix nella PR`.
2. Device: Codex's verdict table. On a FAIL read the evidence only as far as needed to say app bug or
   scenario/setup. A BLOCKED is not a verdict on the PR.
3. Across streams: merge the same finding reported twice (keep the stronger evidence). An `AC` the intent stream
   calls met while the device run fails it, or the reverse, is a point to settle, not two answers.
4. When all have landed: **one follow-up round at most**, only for what would change the verdict and reading cannot
   settle (a `reasoned` bug, an `AC` contradicted by the phone, a gap only a test run proves): one short brief per
   point, same command, `--name pr<n>-followup-<k>`; a device recheck is a rerun plan with `--effort high` when
   it runs an existing scenario as is. What is still open after it
   goes in the report as open.
5. Snapshot again into `$S/pr<n>-after.txt` and compare: any difference, show it to the user and restore nothing
   yourself.

A stream that fails (quota, login, crash): say which. Device falls back to the `device-tester` agent with the same
plan. For the others ask the user whether to do that part with Claude (`/code-review`, `/simplify`, or by reading),
since it is Claude quota.

## 6. Report

In the user's language, for the head SHA reviewed:

1. Verdict, one line: `pronta` | `da sistemare: N blocker, M bug` | `da ripensare` (the approach does not fit).
   N and M count only the findings whose consiglio is `fix nella PR`; with none, the PR is `pronta`.
2. Requirements, one row per check: ID → met / partial / missing / not verifiable → code (`file:line`) → phone
   (PASS / FAIL / N/A / not run).
3. `ReportFindings` with the verified findings, most severe first; category `requirements`, `design`,
   `correctness`, `reuse` or `test-coverage`; `verdict: CONFIRMED` when a command, test or phone showed it,
   `PLAUSIBLE` when it rests on reading.
   Right after it, a table with one row per bug: ID (as in `ReportFindings`) → probabilità, with its reason →
   incidenza, with what the user sees → costo del fix, with the minimal fix → consiglio.
4. Phones, one line each (`galaxy PASS 6/6 · iphone FAIL AC3`), with the evidence path of each FAIL.
5. Dropped findings, one line each with why; what was not checked.
6. Recap, last in the message (after the §7 comment drafts on a colleague's PR). It opens with two links, the Jira
   ticket (`https://docusketch.atlassian.net/browse/<KEY>`, or "nessun ticket") and the PR's `url`, then these five
   headings, a few bullets each, summing up the above with no new evidence. Explain it simply, as to someone who has not read the
   code: what happens on screen and why, not the code's own terms (not "save point on the overview" but "Done
   brings you back to the project page").
   - **Cosa c'era da fare**: the problem the ticket solves and its checks, as the app's user would see them.
   - **Come è stato risolto**: the PR's approach, check by check, and what it changes beyond the ticket.
   - **Architettura**: whether it holds: fit with the app's flow, the layer the fix lives in, duplicates or missed
     reuse, test coverage; when it does not hold, the one thing that would change.
   - **Rilievi e cosa aprirei**: one line per finding with its severity, probabilità, incidenza and consiglio, and
     what to open for each: PR comment, Jira ticket, question to the author, fix by us.
   - **Dubbi e domande**: what is still open, and what only the author or the user can settle; "nessuno" if so.

## 7. After

- The user's own PR: apply the findings with consiglio `fix nella PR` whose fix is `1–2 righe` or `piccolo`, test
  included, and rerun the affected tests (through `sub-codex` when Gradle or iOS). The others go to them first, with
  their three fields and the minimal option. Commit and push stay theirs.
- A colleague's PR: never edit, push or comment on your own. Propose the comments as plain text ready to paste, none
  for a `lascia stare`, each saying in one clause how likely the case is and what the user sees; after their ok, add
  them to a PENDING review (their existing one if there is one), sent only when they say so.
