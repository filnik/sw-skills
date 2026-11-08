---
name: merge
description: Brings the base branch into the current branch and resolves the conflicts - by default the base of the branch's pull request (or the repo's default branch when there is none), or the branch named. A script does the git part (fetch, align with origin, merge, commit, push) and refuses anything unsafe; Claude resolves each conflict keeping both sides' intent, runs the tests that cover it, and pushes. Use for "/merge", "/merge develop", "merge main", "fix the conflicts", "bring main in", "the PR has conflicts", "mergia main", "fixa i conflitti", "porta dentro main". Not for merging the pull request itself into its base (gh pr merge), nor for a rebase.
---

# merge

If the user asked for this in their own words (`/dev:merge`, or "merge main" in this turn), that is the ok, once,
for its git writes: the merge, its commit and the push. If you chose this skill yourself or another skill handed
over, say what you would merge into what and wait for an ok. The writes go only through `scripts/merge.sh`, next to
this file. Run it from the repository, or pass `-C <dir>` right after the script path.

## 1. Start

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/merge.sh" start [TARGET]
```

TARGET only when the user names one. The script:

- refuses uncommitted changes, a detached HEAD and a merge already in progress;
- picks the target: the named branch, else the base of the branch's PR (via `gh`), else origin's default branch
  when `gh` says there is no PR. When `gh` fails (missing, logged out, offline) it stops: ask the user for the
  target, never assume the default branch;
- fast-forwards the local branch to `origin/<branch>` when the remote is ahead, and stops when they diverged;
- merges `origin/<target>` without committing.

Its last line says what comes next:

- `STATUS: up to date`: say so and stop.
- `STATUS: clean, ready for finish`: go to 3.
- `STATUS: conflicts in N files`, with the files listed: go to 2.

## 2. Resolve

For each conflicted file:

1. Read what each side changed and why: `git diff MERGE_HEAD...HEAD -- <file>` is this branch,
   `git diff HEAD...MERGE_HEAD -- <file>` is the target, `git log --merge --oneline -- <file>` lists the commits.
2. Keep both intents. Never drop a change from either side just to make the conflict go away. Where they truly
   contradict, keep this branch's behaviour and say why.
3. Lockfiles: take the target's version (`git checkout --theirs <path>`), then regenerate it with the project's tool
   (`npm install`, `poetry lock`…) when this branch changed dependencies.
4. Binary files: take one side whole, `git checkout --ours <path>` for this branch or `--theirs` for the target, and
   say which.

Then look past the markers. If one side renamed, removed or changed the signature of something the other side's new
code uses, the result breaks even where git saw no conflict: fix it. `git diff HEAD...MERGE_HEAD` is everything the
target brings.

Run the narrowest tests that cover the resolved files, as the repo's README or CLAUDE.md says, and the build or type
check if the project has one. Fix only what the merge broke.

A choice you are not sure about, such as both sides changing the same behaviour in incompatible ways: explain it to
the user in plain words and wait before finishing.

## 3. Finish

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/merge.sh" finish [--trailer "<line>"]...
```

One `--trailer` per commit attribution line your environment asks for, if any. The script refuses leftover conflict
markers, stages the resolution, commits `Merge <target> into <branch>` and pushes to `origin/<branch>`. It never
force-pushes.

If finish says the push failed, stop: the merge commit exists only locally. Rejected because `origin/<branch>` got
new commits: with the user's ok, `git pull --no-rebase`, then `git push origin HEAD:<branch>`; never `--rebase`,
which would flatten the merge commit. Denied by a hook or a permission rule: give the user
`! git push origin HEAD:<branch>`.

`bash "${CLAUDE_SKILL_DIR}/scripts/merge.sh" abort` drops the merge in progress: the branch goes back to where it was
before the merge. A fast-forward to `origin/<branch>` that `start` made stays.

## Report

A few lines: the target and where it came from, the commits it brought in, each resolved file with the choice made,
the tests run and their result, the pushed commit. The push restarts the CI.
