---
name: standup
description: Writes your standup update from what really happened - your commits in local repos (pushed or not), the PRs you opened, merged and reviewed, the issues you opened, and this conversation - since the previous working day or any date you name. Groups them by outcome, not by commit, and adds what is in progress and what is blocked or waiting. Ready to paste; it never posts it. Read-only. Use for "/standup", "what did I do yesterday", "write my standup", "daily update", "what did I do this week", "cosa ho fatto ieri", "scrivimi lo standup", "il daily", "riassunto della settimana". Not for where the current task stands (recap).
---

# standup

Read-only: it gathers and writes; posting is up to the user.

## 1. Since when

The previous working day from 00:00 (on Monday: Friday), unless the user names a date ("this week", "since
Tuesday"). Compute the date with `date` and write it as `YYYY-MM-DD`; below it is `<since>`.

## 2. Gather

All in one go:

**Commits**, pushed or not. In the current repo, or in every repo under the folder the user names
(`find <dir> -maxdepth 3 -name .git -prune` lists them, worktrees included). In each one:

```bash
git -C <repo> log --all --no-merges --since=<since> --author="$(git -C <repo> config user.email)" \
  --format='%h %ad %s' --date=format:'%a %H:%M'
```

**GitHub** (needs `gh`, logged in):

```bash
gh search prs --author @me --created ">=<since>" --json repository,number,title,url,state,isDraft
gh search prs --author @me --merged-at ">=<since>" --json repository,number,title,url
gh search issues --author @me --created ">=<since>" --json repository,number,title,url,state
gh api graphql -f from=<since>T00:00:00Z -f query='query($from:DateTime!){viewer{contributionsCollection(from:$from){
  pullRequestReviewContributions(first:100){nodes{occurredAt pullRequestReview{state}
  pullRequest{title url author{login}}}}}}}'
```

The last one gives the reviews you gave, each with its time.

**Open work**: `python3 "${CLAUDE_SKILL_DIR}/../my-prs/scripts/my_prs.py"` when the my-prs skill is installed next
to this one (the open PRs and what they wait for), otherwise `gh search prs --author @me --state open --json
repository,number,title,url,isDraft`.

**This conversation**: what was done and not committed yet, and what was decided.

## 3. Write

In the user's language, ready to paste:

```
**Done**
- <an outcome in plain words> (<PR or repo>)
**In progress**
- <what, and how far it is>
**Blocked / waiting**
- <what, waiting for whom, since when>
```

- **One line per outcome, not per commit**: ten commits of one feature are one line. Write what changed for
  users or the team, not the commit subjects.
- Reviews done go in Done, grouped ("reviewed 3 PRs: …").
- A section with nothing in it is left out.
- Links only when the user wants them; PR numbers are enough.
- If the user keeps their plans for today in a section of their own (**Today**), propose it from the open work and
  say it is a proposal.

Then, outside the block, one line with what was left out and why (a repo with no commits, noisy chores).
