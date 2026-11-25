---
name: sub-codex
description: Hands a well-scoped sub-task to Codex (codex exec, gpt-6.2-sol at xhigh by default) and brings back only its final summary, so the work is paid from the Codex quota and its logs stay out of Claude's context. Use for "/sub-codex", "have Codex do it", "delegate to Codex", "run it on Codex", "fallo fare a codex", "delega a codex", "lancia su codex", and on your own for medium tasks with a clear goal and a way to check the result (well-specified app code, mechanical refactors, tests, data queries and evidence gathering). DocuSketch folders only (~/Documents/projects/docusketch): never for personal projects. Not for design decisions, unclear requirements, implementing CI workflows, shell/build scripts or tooling, subtle diagnoses or critical fixes (unless the quota line says Astra is on), or work that needs the user's input midway.
---

# sub-codex

Claude's weekly quota is usually the scarce one (Team Premium = Max 5x limits). Codex has its own, so a medium task
done there costs Claude one call to start it and one short summary to read.

## Only DocuSketch

Codex is for DocuSketch work only: its working directory (`--cd`, or the current directory) must be inside
`~/Documents/projects/docusketch`. In any other folder (personal projects) do
every task with Claude, also an explicit "have Codex do it": say that Codex is reserved for DocuSketch. The script
refuses any other folder with exit 4, and the quota hook routes those prompts to Claude.

## Which quota

Every prompt carries a `quota route:` line (UserPromptSubmit hook, `scripts/quota-route.py`). Inside DocuSketch it
routes to Codex: fast below 70% of Codex's weekly usage, and from 70% fast while within budget, the slower tier when
over it (negative Codex spare); sub-codex picks the tier by itself. From 80% and over budget, while Claude has margin
(week at most 60% used, 5h window under 50%), it routes to `claude`: Codex is kept for what only it has (PostHog,
Crashlytics, SigNoz stag and sand). The user (25/11): "better to run out of Codex before Claude… if Codex gets too
strained, let's send as much as we can to Claude".

- `codex`: delegate as below, and so do the skills built on Codex (`/pr-review`, `/device-test`, `/merge-codex`,
  `/build`, the `*-codex` ones). On the slower tier only an urgent task may go to Claude instead; if unsure it is
  urgent, ask the user.
- `claude` inside DocuSketch (Codex in reserve or used up): use this skill and the `*-codex` skills as usual. The
  script does not start Codex: it writes the task to a file, prints one Agent call (`sub-claude` with the model and
  effort to use, or the Claude agent the calling skill names, e.g. `device-tester`) and exits 5. Exit 5 is not a Codex
  failure: make that call as printed, tell the user in one line that the task runs on Claude and why, then go on with
  the calling skill's steps. Runs that need Codex pass `--needs-codex` and Luna runs stay on Codex while it is in
  reserve; once it is used up, everything goes to Claude. The user's "have Codex do it" while Codex is in reserve
  means "delegate": it goes to sub-claude unless they insist on Codex.
- `claude` outside DocuSketch: there is only Claude, on the user's own paid plan. Do every task with Claude, also
  what they ask to hand to Codex, and say why.
- Decide once per task: a task started on one side finishes there.
- Claude agents' model and effort: the quota line ends with the pick for each class, from Claude's quota (ample,
  normal, tight). Pass both `model` and `effort` in the Agent call. `hard`: critical fixes, subtle diagnoses,
  complex implementations (`--important` runs). `judgement`: diagnoses, plans, reviews, medium implementations
  (default `xhigh` runs). `gathering`: reading, data, re-runs, mechanical edits (`--effort high`). The user (25/11):
  "I would not go below high for Sonnet and Opus, nor below medium for Fable"; the table is `LADDER` in `quota-route.py`.

## One agent or several

Before every delegation, to Codex or to Claude agents, decide whether the work is one task or several in parallel.
The user (25/11): "always ask yourself whether to do it all with one agent or several in parallel, balanced on how
many tokens you have, without waste, and balancing model and effort to the task".

- **Split only when** the parts are independent (different files, no decision that waits on another part), each
  is large enough to be worth an agent, and each can be checked on its own. Otherwise use one agent. Every agent
  pays its own start-up (rules, files: 20-40k tokens), and the merge falls on the caller.
- **How many at most:** from the quota line.
  - Claude agents: 3 when the quota is ample, 2 when normal, 1 when tight (`CLAUDE_PARALLEL`).
  - Codex runs: 3 below 70% of its week, 2 above it but within budget, 1 when over budget (`codex_parallel`).
- **Model and effort per part, not per task.** The gathering parts go on `gathering` (Sonnet high, or Luna on
  Codex), the reasoning on `judgement`, the hard part on `hard`. Splitting by class keeps the mechanical parts off
  the expensive model.
- **Never:**
  - two agents writing the same files;
  - a verifier agent, unless the conclusion is important and an independent reading is worth it;
  - an agent for a check that takes two commands.

Not for implementing CI workflows, shell/build scripts or tooling, subtle diagnoses (concurrency, lifecycle,
state machines, Compose/CMP internals, caches and races), or critical fixes (release-blocking or user data).
These stay with Claude, or go to the `sub-claude` agent (class `hard`, model and effort from the quota line) with a
narrow question when they need a lot of reading. Codex keeps well-specified app code, mechanical refactors, tests, data queries and evidence,
and, while Astra is on, also complex implementations, subtle diagnoses and critical fixes (below).

## Astra

In the last 3 days before Codex's weekly reset, while the quota Codex would leave unused at this pace is at least
10 points per day left (30 at three days, 10 at one), the quota line ends with "Astra is on". The script checks it
again at every run, so it switches itself off as the Astra runs use up that spare. While it is on:

- Pass `--important` to implementations, code reviews and merge-conflict runs: they run on `gpt-6.2-astra`, about
  as good as Opus but much hungrier. Other runs stay on Sol or Luna.
- Complex implementations, subtle diagnoses and critical fixes go to Codex too, with `--important`; Claude reviews
  the result before calling it done. The user (24/11): "if we have Astra enabled you can give it more complex things and
  review them afterwards". Decisions (hotfix or not), design, unclear requirements, CI workflows and
  tooling, and the dialogue with the user stay with Claude.
- A task already started on Astra finishes on Codex even if Astra switches off meanwhile.

## Run

1. Write the prompt to a file in the scratchpad with Write. Codex starts from zero: give the goal, the repo or
   worktree, the files and the ticket, the constraints, how to verify the result and what to report. No "as
   discussed above".
2. One Bash call, written exactly like this and alone (no `cd … &&`, no pipes). Use the foreground with
   `timeout: 600000` only for normally short runs with measured p90 < 5 minutes and nothing for Claude to do in
   parallel; the 10-minute timeout kills the run. Use `run_in_background: true` for long runs, parallel work or
   an unmeasured duration; follow the caller skill's measured run step.

   `bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh [--full] [--cd DIR] [--name NAME] --file PROMPT.md`

   `settings.json` runs exactly that prefix outside Claude's sandbox: inside it, Codex fails with "Operation not
   permitted". The exclusion does not match a command holding backticks or `$(…)`, even inside quotes (five
   failures on 27/10, all with an inline prompt quoting a command in backticks): text written on the command line,
   after `--file` or alone, is plain words only, and anything else goes in the file. If the call still lands in
   the sandbox, the script stops before starting Codex, says so and exits 3.
3. The completion notice carries Codex's final message; its first line is the path of the full log, the second the
   service tier, the model and effort, and Codex's weekly usage before and after the run. Read the log only if the
   summary is not enough.

Options:

- `--cd DIR`: the repo or worktree the task works in (default: the current directory). Must be inside
  `~/Documents/projects/docusketch`, or the script exits 4.
- `--full`: no Codex sandbox. Needed for Gradle and xcodebuild (they write `~/.gradle`, `~/.konan` and
  DerivedData), for the phones (adb, usbmuxd) and for files outside `--cd`. Without it Codex runs workspace-write:
  it writes only in `--cd` and the temp dirs, with network.
- `--search`: live web search (`web_search=live`); without it Codex has only the cached index.
- Service tier, chosen by the script: it reads Codex's weekly usage from the last session in `~/.codex/sessions`
  and runs on the fast tier (`service_tier=fast`: quicker, but it uses up Codex's quota faster) up to
  `FAST_ALWAYS` (70%, in `quota-route.py`). Above that it stays fast while, at this week's pace, Codex reaches its
  reset going over by at most `FAST_SLACK` (about an hour of work), and uses the default tier otherwise or when the
  usage cannot be read. It is decided again at every run, so a fast tier that spends too much switches itself off.
  Callers pass nothing for it. `--fast` forces the fast tier: only when the user asks for speed.
- `--needs-codex`: the run uses a connector only Codex has (PostHog, Crashlytics, SigNoz stag or sand) or the user
  insists on Codex: it stays on Codex while Codex is in reserve.
- `--important`: an implementation, code review or merge-conflict run. On `gpt-6.2-astra` while Astra is on (§Astra),
  on Sol otherwise; an explicit `--model` wins. Run it in the background: Astra's duration is not measured yet.
- `--model`, `--effort`: default `gpt-6.2-sol`, `xhigh`.
  Effort floor: `high`; `medium` only for truly trivial steps, never `low`.
  Keep `xhigh` where reasoning really matters; callers set `--effort high` for ordinary bounded work.
  `--model gpt-6-luna` (a twentieth of Sol's credits) only for mechanical runs with nothing to judge: the callers
  open, open-pr, build, done and check-prs pass it.
- Luna workers: a prompt that is mostly gathering (queries, logs, searches, inventories) stays on Sol and adds
  "Follow ~/.claude/skills/sub-codex/luna-workers.md", then which steps go to Luna and which stay
  with Sol (planning, judgements, checks, the report). The gathering `*-codex` skills already say it in their
  instructions. Not for implementation, reviews, merges or device tests.
- `--name`: label for the log and output files.
- `--file PROMPT.md "text"`: the text is appended to the file, for a fixed prompt plus its parameters (plain
  words: no backticks, no `$`).

The script adds fixed rules to every prompt: no commit, push, rebase, merge, reset or branch switch; `rg` on the
working tree, git grep only for a commit not checked out and with `LC_ALL=C` unless it is plain `-F` without `-i`,
at most two repo-wide searches at once, both rules copied
into every spawned agent; final message in Italian under 25 lines. On "Selected model is at capacity" (OpenAI out of capacity, not our quota) it resumes the
same session at once, then after 15, 30, 60 and 120 s; the log marks each resume.

## After

For a report file, read only `sed -n '/^## Findings/,/^## Evidence/p' <report>`. Open one `### <id>` evidence
section only when its quoted lines are not enough. Never chain the `.output` read with `cat <report>` or `git diff`.
To check an implementation, `git diff --stat` first, then only the hunks needed to verify it; when Codex says the
tests pass, rerun them in one call. If Codex fails, or the log says the quota is over or the login is gone, do the
task with Claude and tell the user why.
