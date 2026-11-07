---
name: history
description: Answers questions about the history of the code with git and gh - when and by which commit or PR a line, function or behaviour was introduced or changed, which release first shipped a fix, whether a change is already on a release branch or in a tag, what changed in an area between two versions, who knows a piece of code. Every answer comes with its evidence (commit, date, PR, tag). Read-only. Use for "/history", "when was this introduced", "which release has this fix", "was this already in 1.4?", "what changed between v2.0 and v2.1", "who wrote this", "quando è stato introdotto", "in che release è finita la fix", "c'era già nella 1.4?", "cosa è cambiato tra", "chi ha scritto questa parte". Not for whether a bug has an issue (search the tracker) or why the CI is red (ci-why).
---

# history

Read-only: `git fetch` is the only command that changes anything, and it touches only the remote-tracking refs.

## 1. Up to date and complete

```bash
git fetch --tags --quiet origin
git rev-parse --is-shallow-repository
```

A shallow clone (`true`) has no history before its cut: say so, and fetch the rest with `git fetch --unshallow`
before answering.

## 2. The question, the commands

**When was it introduced or changed.** Start from the code, not from the messages:

- a string or a symbol: `git log -S'<text>' --oneline --date=short --format='%h %ad %an %s' -- <path>`
  (the commits that add or remove it; `-G'<regex>'` for a pattern);
- a function: `git log -L :<function>:<file> --no-patch --format='%h %ad %an %s' --date=short`;
- some lines: `git blame -w -C -C -L <from>,<to> <file>`, then `git show <commit>` for the change around it;
  `-w -C -C` skips whitespace edits and code moves, so the blame lands on who really wrote it;
- a file that was renamed: `git log --follow --oneline -- <file>`.

**From a commit to its PR**: `gh pr list --state merged --search <sha> --json number,title,url,mergedAt`.
**From a PR to its commit**: `gh pr view <N> --json mergeCommit,mergedAt,baseRefName`.

**Which release has it.**

- the first tag that contains a commit: `git tag --contains <sha> --sort=creatordate | head -1`, or
  `git describe --contains <sha>`;
- a branch: `git merge-base --is-ancestor <sha> origin/<branch> && echo yes`;
- a fix that reached the release by cherry-pick or squash has another sha. Look for its subject or PR number
  (`git log origin/<branch> --oneline --grep '<PR number or subject>'`) and for the same patch
  (`git cherry -v origin/<branch> <sha>`: if the line of that commit starts with `-`, the same change is already
  on the branch).

**What changed between two versions.**

- the PRs: `git log --first-parent --oneline <from>..<to>` (merge commits) or
  `git log --oneline --no-merges <from>..<to> -- <path>` for one area;
- the size: `git diff --stat <from> <to> -- <path>`.

**Who knows this code**: `git shortlog -sne --no-merges --since=<1 year ago> -- <path>`, the people who changed it
lately; the oldest author is often no longer the right person to ask.

## 3. Answer

In the user's language:

1. **The answer** in one or two lines (`introduced on 2026-03-12 in #418 by ann, first shipped in v2.3.0`).
2. **The evidence**: the commits with their date and author, the PR link, the tag or branch, and the command that
   proves the key point, so the user can run it again.
3. **What is not certain**, said plainly: a squash or cherry-pick found by subject only, a shallow history, a rename
   the search may have crossed.

Never state a version or a date without the command output behind it.
