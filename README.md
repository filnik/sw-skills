# Dev workflow skills for Claude Code

Skills for the part of software development around the code: picking up an issue, filing one, researching a
question, committing, opening the pull request, answering the review, merging the base, understanding a red CI,
reading the history, checking production crashes, and knowing where you are.

| Skill | What it does | Writes |
|---|---|---|
| `/dev:pick` | Takes a GitHub issue or ClickUp task, reads it with its attachments, asks what matters, proposes a plan; once approved, assigns it, creates the branch and implements. | after plan approval |
| `/dev:ticket` | Turns a problem into a well-written issue: Problem, Scope, Out of scope, Acceptance criteria. Checks for duplicates first. | the issue |
| `/dev:research` | Searches the web and checks every answer against the versions your project really uses, with dated sources. | nothing |
| `/dev:commit-push` | Stages only the files of the change, writes the message in the repo's style, pushes. | commit, push |
| `/dev:pr-create` | Opens the PR from the repo's template (or updates the open one), with only the tests that really ran. | push, PR |
| `/dev:babysit` | Reads the CI and **every** review comment (people and bots), proposes fix / decline / follow-up for each, waits for your answer, then fixes, pushes and replies. | after your answer |
| `/dev:merge` | Brings the base branch in and resolves the conflicts keeping both sides' intent, runs the tests, pushes. | merge commit, push |
| `/dev:ci-why` | Finds the first real error in each failed job and tells code failures from flaky or infra ones. | nothing |
| `/dev:recap` | Done, open, and what you have to decide, in plain words, plus the state of the branch and PR. | nothing |
| `/dev:my-prs` | All your open PRs across every repo, the ones that need you first, with the next step for each. | nothing |
| `/dev:history` | When and by which PR something was introduced, which release has a fix, what changed between two versions. | nothing |
| `/dev:standup` | Your standup from what really happened: commits, PRs opened, merged and reviewed, issues. Ready to paste. | nothing |
| `/dev:crashes` | The crashes of a release from Crashlytics or Sentry, new or worse than the previous version, with the code they point to. | nothing |
| `/dev:release-notes` | The release notes of a version from every PR merged since the last tag, written for users, in the repo's changelog format. | on your ok |
| `/dev:disk-cleanup` | Finds regenerable build output in idle projects, stale Xcode and Gradle data and caches, shows a numbered report, deletes only what you pick after re-checking each item. | what you pick |
| `/dev:memguard` | Installs a background guard that kills a command started by Claude Code or Codex when its memory explodes (20 GB and still growing fast, or 30 GB), before the Mac freezes; tells you what it killed. macOS. | a LaunchAgent, on install |
| `/dev:device-test` | Tests your app on real phones (Android via adb, iPhone via WebDriverAgent): a scenario runs whole in one call, one line per step, one verdict. Claude writes the plan, the `device-tester` agent runs it. | a pack under `skills/device-test/packs/` |
| `/dev:pr-review` | Reviews a PR end to end through parallel Codex runs: the ticket's requirements and the fit with the app's flow, test coverage, bugs and missed reuse, and the ticket's checks on real phones. Claude writes the brief, verifies every finding against the code and reports one table. | small fixes on your own PR; a pending review on a colleague's, on your ok |
| `/dev:tested` | Says whether the current PR was tested on the real phones: which check passed on which phone and build, the phones still missing, and whether it needs one phone, two at once or both Android and iOS. Reads the `device-test` ledger and the conversation. | nothing |
| `/dev:sub-codex` | Hands a well-scoped sub-task to the Codex CLI (`codex exec`) and brings back only its final summary, so the work is paid from the Codex quota. A prompt hook routes medium tasks between Codex and Claude agents by each side's weekly usage. | what the task asks, never git |

And three agents, which the skills spawn (`dev:<name>` once the plugin is installed):

| Agent | What it does |
|---|---|
| `device-tester` | Runs a `device-test` plan on the phones and returns only the verdict table (Sonnet). |
| `sub-claude` | A Claude worker for sub-tasks that would fill the main conversation (diagnoses, plans, reviews, larger implementations), and for the tasks `sub-codex` hands back to Claude when Codex is kept in reserve or used up. Returns a summary under 25 lines. |
| `artifact-maker` | Makes and updates one claude.ai Artifact (design mockups, report pages, dashboards, whiteboards) in its own context and returns the URL and at most 5 lines; feedback goes to the same agent, which republishes on the same URL. |

`pr-review`, `tested`, `sub-codex`, `sub-claude` and `artifact-maker` are the author's daily versions and still
assume his work setup: the DocuSketch folders under `~/Documents/projects/docusketch`, its mobile repo and Jira, the
Codex CLI, and the phones of `device-test`.

You don't have to type the commands: "pick the next bug", "open an issue for this", "search online how to…",
"commit and push", "open the PR", "what do the reviewers say", "fix the conflicts", "why is CI red", "where are we",
"which PRs need me", "when was this introduced", "write my standup", "is the new release healthy" trigger them too.

## A typical loop

```
/dev:ticket ──▶ /dev:pick ──▶ write code ──▶ /dev:commit-push ──▶ /dev:pr-create ──▶ /dev:babysit ◀──┐
                    │                                                                  │   │          │
              /dev:research                                            CI red? ◀───────┘   └──▶ conflicts?
                                                                       /dev:ci-why              /dev:merge
                                    lost the thread at any point? /dev:recap

morning: /dev:my-prs, /dev:standup    release: /dev:release-notes    after it: /dev:crashes ──▶ /dev:ticket
```

## Install

In Claude Code:

```
/plugin marketplace add filnik/sw-skills
/plugin install dev@sw-skills
```

Or, to install it for everyone who works on a project, commit this to the project's `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "sw-skills": { "source": { "source": "github", "repo": "filnik/sw-skills" } }
  },
  "enabledPlugins": { "dev@sw-skills": true }
}
```

Each skill is also a self-contained folder: copying `skills/<name>/` into `~/.claude/skills/` (or a project's
`.claude/skills/`) works too. `ticket` uses the ClickUp script in `pick`, so copy them together; `standup` uses the
script in `my-prs` when it is there.

To try the plugin from a clone without installing it: `claude --plugin-dir .`.

Requirements: `git`, the [GitHub CLI](https://cli.github.com) logged in (`gh auth login`), `python3` 3.9+, `bash`.
No other dependencies.

## Use the skills without installing

To run the skills straight from a clone, so that a `git pull` updates them, link the ones you want one by one:

```bash
repo=~/path/to/sw-skills
ln -s "$repo/skills/<name>" ~/.claude/skills/<name>
ln -s "$repo/agents/<name>.md" ~/.claude/agents/<name>.md
```

A linked skill keeps its bare name, `/<name>` (and the agent `<name>`). The plugin install namespaces them instead,
as `/dev:<name>` and `dev:<name>`; with both you get both.

`pr-review`, `tested` and `sub-codex` call their scripts, and each other, through `~/.claude/skills/<name>/…`
paths, so they need the link (or a copy) there. `sub-codex` also expects, in `settings.json`, its command
`bash ~/.claude/skills/sub-codex/scripts/sub-codex.sh` in `sandbox.excludedCommands` (Codex cannot start inside
Claude's sandbox) and `python3 ~/.claude/skills/sub-codex/scripts/quota-route.py` as a `UserPromptSubmit` hook,
which adds the `quota route:` line to every prompt.

## Config

Some skills read a per-project file, `.claude/config/project.json`. They look for it from the current folder upward
through every parent folder and merge what they find, the nearest file winning key by key, so a repo's own
config adds to one kept in a folder above it instead of hiding it. Every key is optional:

```json
{
  "locale": "en",
  "playbook": ".claude/config/playbook.md",
  "clickup": {"list_id": "901234567"},
  "babysit": {"ignore_checks": ["PR Safeness"], "skip_comments": ["<!-- preview-builds-hint -->"],
              "copilot": {"request": true, "auto_review_bases": ["main"]}},
  "disk_cleanup": {"pools": [{"root": "~/work", "checkouts": ["app-[0-9]*"]}], "defaults": {"derived_days": 2}}
}
```

- `locale`: the language of text the skills post for others (GitHub replies, PR comments); default `en`. The skills
  talk to you in your own language.
- `playbook`: a Markdown file with your own procedures, one section per skill, relative to the folder that holds
  `.claude/`. When it exists, babysit, commit-push and disk-cleanup read it, and where it and a skill both speak, the
  playbook wins.
- One block per skill: `clickup` (pick, ticket), `babysit`, `disk_cleanup`. Each skill's SKILL.md lists its keys.

Keep the file out of git when it holds private paths or team rules. `python3 shared/project_config.py [<key>]`
prints what the skills see from the current folder.

## Issue trackers

`pick` and `ticket` work with **GitHub Issues** out of the box, through `gh`.

For **ClickUp**, add this to the project's `.claude/config/project.json`:

```json
{
  "clickup": {
    "list_id": "901234567",
    "workspace_id": "9012345"
  }
}
```

- `list_id`: the list tasks are read from and created in (the number after `/li/` in the list's URL).
- `workspace_id`: needed only if your tasks use custom ids like `DEV-123`.
- `in_progress_status` (optional): the status `pick` moves a task to; by default the list's first status that reads
  like "in progress".

The token (ClickUp → Settings → Apps → API Token) goes in the `CLICKUP_TOKEN` environment variable, or in
`.claude/secrets/clickup-token`. **Gitignore that file.**

## Crash reporting

`crashes` reads crashes through the crash tool's MCP server. Add the one your app uses, once:

- **Firebase Crashlytics**: from the app's folder, `claude mcp add firebase npx -- -y firebase-tools@latest mcp`,
  signed in with `npx firebase-tools@latest login`.
- **Sentry**: `claude mcp add --transport http sentry https://mcp.sentry.dev/mcp`, then sign in from `/mcp`.

Both servers can also change issues (notes, state); the skill only reads.

## Testing on real phones

`device-test` is an engine plus a pack. The engine (`skills/device-test/scripts/devtest*.py`, Python, stdlib only)
knows phones: drivers for Android (adb) and iPhone (WebDriverAgent), the scenario DSL and its runner, leases so two
sessions never drive the same phone, the ledger of verdicts, suites, `lint`, `tags check` and `new`, which turns a
test plan into a scenario skeleton. It knows nothing about your app.

Everything about one app lives in a pack, a folder under `skills/device-test/packs/<name>/` that git ignores, so it
never leaves your machine:

    pack.json      package and bundle id, build command, paths of the built app, prefs and DB, iOS team_id,
                   the phones (alias → serial or UDID), what to dismiss at launch
    flows/         your shared steps (login, open a project…), imported as `from flows import …` next to the
                   engine's own (`phone`, `fields`, `state`, `pair`, `storagefiles`)
    scenarios/     one file per ticket or regression
    suites/        lists of scenarios to run together
    hooks.py       optional: what config cannot say (how a feature flag is stored, how to clean test data)
    runner.md      optional: your app's and phones' quirks, read by the runner after the engine's `runner.md`

With one pack the engine loads it; with several, name one in the project's `.claude/config/project.json`:

    { "device_test": { "pack": "myapp" } }

Any other key of that `device_test` block overrides the same key of `pack.json`. Without a pack the engine still
lists phones, holds leases, lints and scaffolds, and says that it has no app constants; a command that needs one
answers BLOCKED with the missing key (`android.package`, `ios.bundle_id`…). `examples/open-app.py` is a scenario
that needs only those ids: copy it to start.

The skill writes the plan and hands it to the plugin's agent `dev:device-tester` (Sonnet), or to the runner your
project playbook names; the runner calls `devtest` and returns only the verdict table, so the phone output never
fills your session.

Requirements: `adb` for Android; for an iPhone, Xcode and the WebDriverAgent sources. When they are missing,
`devtest wda` stops and prints the `npm install … appium-webdriveragent` line to run (they go into the state
folder, `~/.cache/devtest` by default).

## Customizing disk-cleanup

Without touching code:

- `--roots ~/work,~/oss` says where your checkouts live; `--days`, `--derived-days` and `--cache-hours` say how long
  things must sit unused. You can just ask: "scan ~/work too", "only checkouts idle for a month".
- A `.disk-cleanup-keep` file protects a folder for good: empty, the whole folder; with patterns (`*.mov`, one per
  line), only what matches below it.

The lists at the top of `skills/disk-cleanup/scripts/disk_cleanup.py`:

- `BUILD_DIRS`: the ignored build folders deleted in idle checkouts (add `.svelte-kit`, `.angular`, …);
- `DEFAULT_ROOTS`: the folders under home searched for checkouts when `--roots` is not given;
- `CACHE_SKIP`: `~/Library/Caches` entries never proposed;
- `LOOSE`: the folders searched for files of 1 GB or more;
- `TOOLS`: package managers emptied by their own command (command, folders measured, label).

A rule of your own (old screen recordings, a project's generated assets, …) takes three pieces: a function that
returns its items with `dir_item(...)`, like `look_items()`; its entry in `SECTIONS`; and a branch in `fenced()`
saying where that section may delete. `clean` refuses anything outside its section's fence, whatever the plan file
says, so a section without a fence deletes nothing. Add a case to `tests/test_disk_cleanup.py`.

To keep your version apart from plugin updates, copy `skills/disk-cleanup/` to `~/.claude/skills/disk-cleanup/` and
change it there: it becomes `/disk-cleanup`, next to `/dev:disk-cleanup`. Claude can make the change for you: "add
a disk-cleanup section for screen recordings older than 30 days in ~/Movies".

## How the skills are built

- **Scripts do the mechanical part, the model does the judgment.** Collecting every review comment across pages,
  refusing a merge on a dirty tree, or talking to the ClickUp API is deterministic: it lives in a script next to the
  skill (`skills/<name>/scripts/`). Deciding whether a comment is a real bug is not: that stays in the instructions.
- **Nothing is written without a clear ok.** Invoking `commit-push`, `merge` or `ticket` is the ok for their own
  writes. `release-notes` shows its draft and writes the changelog or a draft release only when you say so.
  `disk-cleanup` deletes only the numbers you pick, and re-checks each one right before. `memguard` installs its
  LaunchAgent only when asked; from then on it kills by itself, by design, but only agent-launched processes past
  its thresholds, and logs each kill. `pick` writes nothing
  until you approve its plan. `babysit` stops after its report and changes nothing until you answer. `ci-why`, `research`, `recap`, `my-prs`, `history`, `standup` and `crashes` are read-only.
- **Never force.** No `--force`, no `--no-verify`, no rebase of a shared branch.
- **Plain words.** Reports explain what happens to the user or the data, not just which line changes.

## Layout

```
.claude-plugin/{plugin,marketplace}.json   manifests
skills/<name>/SKILL.md                     the skills
skills/babysit/scripts/babysit.py          PR status and replies (Python, stdlib only)
skills/merge/scripts/merge.sh              the git part of /dev:merge (bash)
skills/pick/scripts/clickup.py             ClickUp API for pick and ticket (Python, stdlib only)
skills/my-prs/scripts/my_prs.py            your open PRs and their next step, one GraphQL call (Python, stdlib only)
skills/release-notes/scripts/release_prs.py  the PRs between two tags, grouped, batched GraphQL (Python, stdlib only)
skills/device-test/scripts/devtest*.py     the phone test runner: drivers, scenarios, leases (Python, stdlib only)
skills/device-test/packs/<name>/           your app's pack: config, flows, scenarios (local, gitignored)
skills/disk-cleanup/scripts/disk_cleanup.py  scan, numbered plan, re-checked deletion (Python, stdlib only)
skills/memguard/scripts/memguard.py        the memory guard (Python, stdlib only); install.sh runs it as a LaunchAgent
skills/pr-review/codex/*.md                the instructions of pr-review's Codex streams
skills/tested/scripts/tested.py            what of a PR the device-test ledger shows as tested (Python, stdlib only)
skills/sub-codex/scripts/sub-codex.sh      runs codex exec, or hands the task to a Claude agent (bash)
skills/sub-codex/scripts/quota-route.py    the Codex or Claude route and the tier, from both weekly quotas (Python, stdlib only)
skills/sub-codex/scripts/test-*            sub-codex's own tests
agents/device-tester.md                    the agent that runs device-test plans
agents/sub-claude.md                       the Claude worker for delegated sub-tasks
agents/artifact-maker.md                   the agent that builds claude.ai Artifacts
tests/                                     one test file per script
```

## Development

```bash
claude plugin validate .
python3 tests/test_babysit.py && python3 tests/test_clickup.py && python3 tests/test_my_prs.py && python3 tests/test_release_notes.py && python3 tests/test_disk_cleanup.py && python3 tests/test_memguard.py && bash tests/test_merge.sh
for t in tests/test_devtest_*.py; do python3 "$t"; done
python3 skills/sub-codex/scripts/test-route.py && python3 skills/sub-codex/scripts/test-astra.py && bash skills/sub-codex/scripts/test-docusketch-only.sh
claude plugin marketplace add ./      # try it locally without going through GitHub
```

## License

MIT
