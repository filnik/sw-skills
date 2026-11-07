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
| `/dev:device-test` | Tests your app on real phones (Android via adb, iPhone via WebDriverAgent): a scenario runs whole in one call, one line per step, one verdict. Claude writes the plan, the `device-tester` agent runs it. | scenarios in `device-tests/` |

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

`device-test` needs a `device_test` block in `.claude/config/project.json`: the package or bundle id, the build
command, the path of the built app, the iOS `team_id`, and an alias for each phone (the full example is in the
skill). Scenarios live in your project under `device-tests/`, shared steps in `device-tests/flows/`. The skill hands
the plan to the plugin's agent `dev:device-tester` (Sonnet), which runs `devtest` and returns only the verdict
table, so the phone output never fills your session.

Requirements: `adb` for Android; for an iPhone, Xcode and the WebDriverAgent sources
(`npm install --prefix ~/.cache/devtest/ios/wda-src appium-webdriveragent`).

## How the skills are built

- **Scripts do the mechanical part, the model does the judgment.** Collecting every review comment across pages,
  refusing a merge on a dirty tree, or talking to the ClickUp API is deterministic: it lives in a script next to the
  skill (`skills/<name>/scripts/`). Deciding whether a comment is a real bug is not: that stays in the instructions.
- **Nothing is written without a clear ok.** Invoking `commit-push`, `merge` or `ticket` is the ok for their own
  writes. `release-notes` shows its draft and writes the changelog or a draft release only when you say so.
  `disk-cleanup` deletes only the numbers you pick, and re-checks each one right before. `pick` writes nothing
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
skills/disk-cleanup/scripts/disk_cleanup.py  scan, numbered plan, re-checked deletion (Python, stdlib only)
agents/device-tester.md                    the agent that runs device-test plans
tests/                                     one test file per script
```

## Development

```bash
claude plugin validate .
python3 tests/test_babysit.py && python3 tests/test_clickup.py && python3 tests/test_my_prs.py && python3 tests/test_release_notes.py && python3 tests/test_disk_cleanup.py && bash tests/test_merge.sh
for t in tests/test_devtest_*.py; do python3 "$t"; done
claude plugin marketplace add ./      # try it locally without going through GitHub
```

## License

MIT
