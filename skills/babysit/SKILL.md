---
name: babysit
description: Brings an open pull request to merge. One script call reads the merge state, the CI and every review comment - inline threads, review bodies, PR comments, from people and from bots like Copilot or Gemini - without losing any. A report explains each comment and proposes fix, decline, follow-up or rerun, and nothing changes until the user answers. Then it makes the approved fixes, commits and pushes them, and replies to and resolves the threads in one call. Use for "/babysit", "/babysit 123", "babysit the PR", "address the review comments", "what do the reviewers say", "get the PR to merge", "guarda i commenti della PR", "cosa dicono i reviewer", "porta la PR al merge". Not for opening a PR (pr-create) or only diagnosing a red CI (ci-why).
---

# babysit

**The goal is to get this PR merged, not to make it bigger.**

```
status → report → the user answers → fixes, commit, push → replies → status again
```

`scripts/babysit.py` sits next to this file. It needs `gh`, logged in, with network access.

## 1. Status

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/babysit.py" status [<N|url>] [--full]
```

Without a number it uses the current branch's PR. It prints:

- the PR, its base and head, the merge state and the review decision;
- the CI: the counts, and each failed check with the command that reads its log;
- Copilot's last review and whether it saw the head commit, when Copilot reviews this PR;
- every comment to triage, each with an id: `c…` is an unresolved inline thread, with `path:line` and its replies;
  `rv…` is a review with a body; `ic…` is a PR comment.

The markers:

- `NEW`: never shown before, printed in full.
- `shown`: printed before but not answered yet, printed in full again. It still needs triage.
- `seen`: a thread already answered that is still open. One line, not to triage.
- `yours`: written by you or by the PR author. One line, not to triage.
- `--full` also prints in full the reviews and comments already answered, marked `earlier`. Use it after the
  conversation was compacted.

Use nothing else to find comments (no `gh pr view --comments`): the script pages through all of them. Read code only
for the item you are triaging.

## 2. The report

One numbered item per comment to triage, except `yours`, `seen`, `earlier` and the comments that ask for nothing: an
"LGTM", a bot's overview whose findings are already inline threads, a deploy preview link. No comment may get lost:
each one in the "to triage" count is either an item or one of those. With nothing left, say so in one line, then the
merge state and the CI in one line each.

First a table:

| # | Id | Who · where | Likelihood | Verdict |
|---|---|---|---|---|

Then one section per item, in the same order, for a reader who has not opened the comment. Two to four lines, in
plain words; code names support the explanation, they do not replace it:

```
**1 · Fix · c4353576750 · copilot · `src/cart.ts:130`**
- **Comment:** what the reviewer says, in one sentence.
- **Likelihood:** High (normal use hits it) · Medium (a common but specific condition) · Low (a rare sequence:
  name it) · None in production (tests or mocks only); then the condition that triggers it.
- **Problem:** whether it holds in the code and why, naming the function or line, and what the user or the data
  would suffer.
- **Solution:** the concrete change and the test that covers it, or the reason to decline, which becomes the reply.
```

Pick the verdict against shipping this PR, not a better one:

- **Fix**: a real bug, regression or broken contract in this diff; a required check the diff broke; a small
  improvement inside the lines the PR touches.
- **Follow-up**: valid and worth doing, but too big for this PR or outside it. The solution is the issue title.
  Small things are never follow-ups: fix them or decline them.
- **Decline**: style or preference, already handled, a misreading of the diff, or against what the PR is meant to do.
- **Rerun**: a check failed for infrastructure or flakiness: `gh run rerun <run-id> --failed`.
- **Merge state**: `BEHIND` → `gh pr update-branch <N>`; `CONFLICTING` → the merge skill.

Order: Fix (bugs first), Follow-up, Rerun and merge state, Decline. A person's "changes requested" outranks any bot.

## 3. Stop and wait

End the turn with the report and one line saying the user can answer item by item, for example
"1 fix, 2 decline, 3 follow-up". Until the answer arrives, change nothing: no edit, reply, resolve, rerun or branch
update. Silence is not a yes.

## 4. After the answer

1. **Fixes**: the smallest edit that works, only for the approved items. Run the narrowest tests that cover each fix.
   Then commit and push them in one commit, as the commit-push skill does.
2. **Follow-ups**: open the issue only if the user said so (`gh issue create`, or the user's tracker), and put its
   link in the reply.
3. **Replies**, after the push, so the commit hash exists. Write a JSON file and run
   `python3 "${CLAUDE_SKILL_DIR}/scripts/babysit.py" answer <N|url> <answers.json>` (`--dry-run` first shows what would be posted):

   ```json
   [{"id": "c123", "body": "Fixed in abc1234: an empty cart no longer reaches the total."},
    {"id": "c456", "body": "Declined: the value is validated by the caller.", "resolve": false},
    {"id": "rv789", "body": "Copilot, missing null check in cart: fixed in abc1234."}]
   ```

   - A thread (`c…`) gets the reply and is resolved; `"resolve": false` leaves it open.
   - Everything else (`rv…`, `ic…`) goes into ONE PR comment, so each body says what it answers.
   - One or two sentences each: `Fixed in <sha>: …`, `Declined: …` or `Tracked in <link>: …`.
4. **Status again** (step 1): new comments and new CI results show up as `NEW`.

## Never

- Merge the PR, rebase or force-push.
- Edit lockfiles, CI workflows or tooling config just to satisfy a comment.
- Widen the PR: no refactor, rename or "while we're here" change that no approved item asked for.
