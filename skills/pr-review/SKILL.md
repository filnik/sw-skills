---
name: pr-review
description: Full review of a PR through parallel Codex runs, steered and checked by Claude - whether the PR does what its ticket asks, duplicates or code that should reuse existing structures, test coverage (untested or badly tested parts), whether the change fits the app's flow, a code review for bugs, and a run of the ticket's checks on the user's phones (device-test). Claude reads the ticket, writes one brief and the device plan, starts the Codex runs together, verifies every finding against the code and reports one table. Use for "/pr-review 4801", "review the PR", "review a colleague's PR", "does the PR meet the ticket?", "full review with a phone test", "fai la review della PR", "revisiona la PR di un collega", "la PR soddisfa il ticket?", "review completa con test sul telefono". Not for answering comments on a PR (babysit), a code-only review, or an overview of open PRs (my-prs).
model: opus
---

# pr-review

Claude spends its tokens where judgment is: the ticket, where the risk is, the device plan, checking every finding.
Three Codex runs and a device-test runner, started together, do the reading, the grepping and the phones.

```
PR → checkout, ticket, risks (Claude) → brief + device plan
   → in parallel: intent · tests · code (Codex) · device (device-test's runner)
   → verify each stream as it lands (Claude) → at most one follow-up round → report → fixes or pending review
```

| Stream | Answers | Effort |
|---|---|---|
| intent | the ticket's requirements are met, and the change fits the app's flow | xhigh |
| tests | coverage: untested or badly tested parts | high |
| code | bugs, duplicated logic and missed reuse of existing structures | xhigh |
| device | the ticket's checks on the phones | xhigh |

Requirements and the macro analysis share a stream: same input (ticket, the knowledge base, the flows the change
reaches), and "fits the flow" is judged against the intended behaviour. The Codex instructions are in `codex/`:
Claude does not read them. `$S` is the Claude session's scratchpad, `<n>` the PR number: write both out in full.

## 0. Config and playbook

From the current folder, `python3 "${CLAUDE_SKILL_DIR}/../../shared/project_config.py" pr_review` prints the
`pr_review` block of the project's `.claude/config/project.json` (all keys optional):

```json
{"pr_review": {"knowledge_base": "~/work/product-docs"}}
```

- `knowledge_base`: the folder that holds the product's intended behaviour. The `knowledge_base` block of that
  folder's own `.claude/config/project.json` names the file to start from (`index`, under `root`); `INDEX.md`
  otherwise. Without the key, the intended behaviour comes from the ticket and the PR body only.

Then print this skill's section of the project's playbook, and nothing else of it, with
`python3 "${CLAUDE_SKILL_DIR}/../../shared/project_config.py" section pr-review`, and for the device stream the
`device-test` one (`… section device-test`): where a section and this skill both speak, the section wins. That is where a project names its tracker, its default repo and checkouts, its rule
files, the runner and the environment of the device tests, and the report's words in the user's language.

## 1. PR and checkout

- `gh pr view <n> [--repo <repo>] --json number,title,url,author,headRefName,headRefOid,baseRefName,body,isDraft,additions,deletions,changedFiles`.
  `gh` needs network access: under a Bash sandbox that blocks it, run it with the sandbox off. No number: the PR of
  the current checkout's branch. No repo: the current checkout's, or the playbook's default. Mine = `author.login`
  equals `gh api user --jq .login`.
- Checkout: a local clone whose `git rev-parse HEAD` is `headRefOid`: the current checkout, one of its worktrees
  (`git worktree list --porcelain` prints each HEAD), or one of the clones the playbook names. The user's PR in the
  current checkout: review it there, working tree included (say so). None at the PR head: give the user the command
  that puts a clean clone there (`gh pr checkout <n>`, or the playbook's way) and go on only once that clone's HEAD
  is the PR head. Never switch, pull or reset a clone yourself.
- Snapshot: `git -C <checkout> rev-parse HEAD > $S/pr<n>-before.txt`, then
  `git -C <checkout> status --porcelain >> $S/pr<n>-before.txt` and
  `git -C <checkout> diff HEAD | shasum >> $S/pr<n>-before.txt`.

## 2. Ticket and risks (Claude)

- The ticket the PR names: an issue it closes or links (`Closes #12`), or a key or task id in the branch, title or
  body (`ABC-123`). Read it from the project's tracker the way `pick` does: GitHub Issues with
  `gh issue view <N> --json number,title,body,state,labels,url,comments`, ClickUp with
  `python3 "${CLAUDE_SKILL_DIR}/../pick/scripts/clickup.py" get <id>`, any other tracker as the playbook says. Read it
  yourself, never through a Codex summary: description, status, links, and the comments (scope often changes
  there). Write the acceptance criteria as checks `AC1…`, in the ticket's words; none in the ticket: derive them
  from the description and mark them `derived`. No ticket: say so, and the intent stream judges against the PR body
  and the knowledge base.
- `git -C <checkout> diff --stat origin/<baseRefName>...HEAD`, then the hunks of the risky files only (the whole
  diff under ~300 lines).
- Risks, 2–6 lines: where this change can break (callers, sync and offline, migrations and existing installs,
  platform differences, a flag that must gate it), from the diff and the repo's own rules: the root and nested
  `CLAUDE.md` or `AGENTS.md`, and the files they or the playbook name for risky areas and past lessons.
- Decided: choices the ticket, the PR body or this conversation already made, so no stream reopens them.

## 3. Brief and device plan

`$S/pr<n>-brief.md`, in English, self-contained, one file for the three analysis streams:

1. "You are one reviewer of a PR review run in parallel. Read
   ~/.claude/skills/pr-review/codex/common.md, then codex/<stream>.md for the stream named at the end
   of this prompt, and follow them."
2. Target: repo, PR number and URL, checkout path, head SHA, base ref; whether the working tree counts.
3. Ticket: key, title, the checks `AC1…`, the scope and out-of-scope lines.
4. Risks and decided, from §2.
5. Sources: the knowledge base's start file (read-only), or "none"; the repo's rule files from §2.
6. Builds: "A device run builds in this checkout: do not run Gradle or xcodebuild." Without a device stream: "You
   may run the unit tests of the touched modules."
7. Output dir: `$S/pr<n>/`. Final message: in the user's language.

`$S/pr<n>-device.md` is the device-test plan (its "Test plan" template), for the runner device-test names (its rule
2: the `device-tester` agent unless the playbook names another):

- checks: the `AC`s a phone can observe, same IDs, each with its evidence; then 1–3 regression checks `R1…` on the
  flows the diff reaches;
- phones: the ones the user named; otherwise one Bash call, alone,
  `python3 ~/.claude/skills/device-test/scripts/devtest.py --owner pr-review devices`: the pack's default Android
  phone, plus its default iPhone when connected (`default_for` in the `pack.json` of the folder `devices` prints),
  unless the diff touches one platform only. A phone leased by another owner: ask;
- worktree = the checkout, owner `<runner>-pr<n>`, red proof: none unless the user asks (it needs a base build).

No device stream when there is no device-test pack or the repo is not the app it tests (the playbook can name the
repos), the change cannot show on a phone (CI, build scripts, tests or docs only), no phone is connected, or the
user said so. Say which.

## 4. Launch

In one message, each one Bash call alone with `run_in_background: true` (plain words after `--file`: see `sub-codex`):

```
bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh --full --important --cd <checkout> --name pr<n>-intent --file $S/pr<n>-brief.md "Stream: intent"
bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh --full --effort high --cd <checkout> --name pr<n>-tests --file $S/pr<n>-brief.md "Stream: tests"
bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh --full --important --cd <checkout> --name pr<n>-code --file $S/pr<n>-brief.md "Stream: code"
```

`--full`: `gh` needs the keychain and the reports land in `$S`. In the same message the device stream starts on its
runner: the `device-tester` agent in the background with the plan as its prompt, or the runner and command the
playbook gives. Tell the user in one line what started (PR, checkout, phones). While they run, do not review the PR
yourself.

## 5. Verify, as each stream lands

1. Read the summary in the notice; read `$S/pr<n>/<stream>.md` by the findings-first rule in `sub-codex` §After.
   For each one open the cited
   lines yourself and check its scenario against the code, the ticket and the decided list. Drop what does not
   hold, what reopens a decision, and style the repo's rules do not state.
   For each bug also check its `likelihood`, `impact` and `fix cost` (scales in `codex/common.md`):
   recompute the likelihood from who reaches the path, since Codex tends to overstate it, and look at the minimal
   fix yourself. Then weigh them into an `advice`: `fix in the PR` · `optional fix` · `separate ticket` ·
   `leave it`. The harm has to be worth the change: `data loss` or `wrong data on the server` gets fixed unless it
   is `almost impossible`; a `rare` bug where the `app recovers` or with `no visible effect` does not earn a
   `medium` or `large` fix (`leave it`, or `separate ticket` if it can grow); a real bug with a `1–2 lines` fix is
   `fix in the PR`.
2. Device: the runner's verdict table. On a FAIL read the evidence only as far as needed to say app bug or
   scenario/setup. A BLOCKED is not a verdict on the PR.
3. Across streams: merge the same finding reported twice (keep the stronger evidence). An `AC` the intent stream
   calls met while the device run fails it, or the reverse, is a point to settle, not two answers.
4. When all have landed: **one follow-up round at most**, only for what would change the verdict and reading cannot
   settle (a `reasoned` bug, an `AC` contradicted by the phone, a gap only a test run proves): one short brief per
   point, same command, `--name pr<n>-followup-<k>`; a device recheck is a rerun plan for the same runner (on
   Codex, `--effort high` when it runs an existing scenario as is). What is still open after it
   goes in the report as open.
5. Snapshot again into `$S/pr<n>-after.txt` and compare: any difference, show it to the user and restore nothing
   yourself.

A stream that fails (quota, login, crash): say which. A device stream on another runner falls back to the
`device-tester` agent with the same plan. For the others ask the user whether to do that part with Claude
(`/code-review`, `/simplify`, or by reading), since it is Claude quota.

## 6. Report

In the user's language, for the head SHA reviewed. The words below are the English ones: the playbook can give the
user's own.

1. Verdict, one line: `ready` | `needs work: N blocker, M bug` | `rethink` (the approach does not fit).
   N and M count only the findings whose advice is `fix in the PR`; with none, the PR is `ready`.
2. Requirements, one row per check: ID → met / partial / missing / not verifiable → code (`file:line`) → phone
   (PASS / FAIL / N/A / not run).
3. `ReportFindings` with the verified findings, most severe first; category `requirements`, `design`,
   `correctness`, `reuse` or `test-coverage`; `verdict: CONFIRMED` when a command, test or phone showed it,
   `PLAUSIBLE` when it rests on reading.
   Right after it, a table with one row per bug: ID (as in `ReportFindings`) → likelihood, with its reason →
   impact, with what the user sees → fix cost, with the minimal fix → advice.
4. Phones, one line each (`pixel PASS 6/6 · iphone FAIL AC3`), with the evidence path of each FAIL.
5. Dropped findings, one line each with why; what was not checked.
6. Recap, last in the message (after the §7 comment drafts on a colleague's PR). It opens with two links, the
   ticket's URL in its tracker (or "no ticket") and the PR's `url`, then these five headings, a few bullets each,
   summing up the above with no new evidence. Explain it simply, as to someone who has not read the
   code: what happens on screen and why, not the code's own terms (not "save point on the overview" but "Done
   brings you back to the project page").
   - **What had to be done**: the problem the ticket solves and its checks, as the app's user would see them.
   - **How it was solved**: the PR's approach, check by check, and what it changes beyond the ticket.
   - **Architecture**: whether it holds: fit with the app's flow, the layer the fix lives in, duplicates or missed
     reuse, test coverage; when it does not hold, the one thing that would change.
   - **Findings and what I would open**: one line per finding with its severity, likelihood, impact and advice,
     and what to open for each: PR comment, ticket, question to the author, fix by us.
   - **Doubts and questions**: what is still open, and what only the author or the user can settle; "none" if so.

## 7. After

- The user's own PR: apply the findings with advice `fix in the PR` whose fix is `1–2 lines` or `small`, test
  included, and rerun the affected tests (through `sub-codex` when Gradle or iOS). The others go to them first, with
  their three fields and the minimal option. Commit and push stay theirs.
- A colleague's PR: never edit, push or comment on your own. Propose the comments as plain text ready to paste, none
  for a `leave it`, each saying in one clause how likely the case is and what the user sees; after their ok, add
  them to a PENDING review (their existing one if there is one), sent only when they say so.
