# pr-review: rules for every Codex reviewer

You are one of several reviewers of the same PR, each on one stream; the others cover what yours does not, so stay
inside your stream. The brief gives the target, the ticket's checks, the risks, the decided choices and your output
dir.

## Allowed

- Read anything: the checkout, other repos, the knowledge base the brief names (start from the file it gives,
  read-only), `gh` read commands, the project's tracker (through your connector, when you have one) for tickets the
  brief or the code names.
- Write only `<output dir>/<stream>.md` and scratch files in `<output dir>/<stream>-work/`.
- No change to the checkout, no git command that writes, no `gh` command that writes (comment, review, label,
  rerun), never touch a phone. Gradle or xcodebuild only when the brief allows it.

## Seeing the change

- `git diff <base>...HEAD` for the PR, plus `git diff` when the brief says the working tree counts. Read the whole
  changed file where the hunk alone does not explain it.
- The repo's rules for the touched areas: the root and nested `CLAUDE.md` or `AGENTS.md` files (and their routing
  tables), plus the rule files the brief lists (risky areas, past lessons: read the matching sections).

## Evidence bar

Each finding has: `file:line`; severity; a one-line summary; the concrete scenario (inputs or state → wrong result,
or what it costs for a non-bug); the evidence (code excerpt with `file:line`, or a command and its output); and
`confirmed` (shown by a command, a test or a reproduction) or `reasoned` (read from the code only).

Severity: `blocker` the PR should not merge (data loss, crash, a required check missing, a broken flow) · `bug`
wrong behaviour in a real case · `risk` could break under conditions you could not confirm · `nit` small cleanup.

A `blocker`, `bug` or `risk` also gets three fields, so Claude can weigh the fix against the harm:

- `likelihood`, from who reaches the path and how often, with that reason in a few words:
  `almost impossible` (needs a state the app does not produce today) · `rare` (a narrow combination: a feature few
  use, a timeline, a millisecond race) · `possible` (a normal flow under a not-everyday condition: offline, retry,
  process death, a second device) · `likely` (the feature's normal flow, for many users) · `certain` (every time
  the path runs). Do not inflate it: a scenario you had to build by hand is rarely `likely`.
- `impact`, the worst of: `data loss` · `wrong data on the server` (what the web or other devices see) ·
  `crash` · `blocked flow` (the user cannot finish without a workaround) · `wrong UI or message` (toast,
  badge, text) · `app recovers` (a retry or the next pass fixes it, at most a delay) · `no visible effect`
  (logs, analytics, performance); then what the user sees, in a few words.
- `fix cost`: the minimal fix in one line and its size, `1–2 lines` · `small` (one file, about 10 lines, test
  included) · `medium` (several files, or a new branch in a flow other features use) · `large` (changes the
  approach); plus what the fix itself could break, if anything.

Do not report what the brief lists as decided, or style the repo's rules do not state. A problem that predates the
PR goes under "Pre-existing", one line each, unless the PR makes it worse or relies on it. No finding is a valid
outcome: then say what you checked.

## Output

`<output dir>/<stream>.md`: start with `## Findings` in the preamble's compact format, then `## Evidence`, one
`### <id>` per finding. Put the detailed stream sections, commands and unchecked items under Evidence too.

Final message, in the language the brief names (English when it names none), under 15 lines: the report path, the
findings by id and count by severity, one line per blocker or bug with its likelihood, impact and fix cost.
