---
name: commit-push
description: Commits the current work with a clear message and pushes it. Stages only the files that belong to the change, writes the message in the repo's own style, and pushes to the branch's upstream (creating it on the first push). Never force-pushes, never skips hooks. Use for "/commit-push", "commit and push", "commit this", "push it", "committa e pusha", "fai commit e push", "salva e pusha". Not for opening the pull request afterwards (pr-create) or bringing the base branch in (merge).
---

# commit-push

Invoking this skill is the user's ok to commit and push the current work, once. It is not a standing permission:
the next commit needs a new request.

## 1. Look

```bash
git status --short --branch && git diff --cached --stat && git diff --stat && git log -8 --format='%s'
```

- Nothing to commit and nothing to push: say so and stop.
- A merge, rebase or cherry-pick in progress: say which and stop. Finishing it is not this skill's job.

## 2. Choose the files

- **Something is already staged**: commit exactly that, and mention the unstaged files you leave out.
- **Nothing staged**: stage the files of the work done in this conversation, by path (`git add <path>…`). Never
  `git add -A` or `git add .`.
- **Leave out and list**: files you did not touch and cannot tell belong to the change, untracked files you did not
  create, and anything that looks like a secret (`.env`, keys, tokens, credentials). Ask only when the change cannot
  be committed without one of them.

## 3. Write the message

Follow the style the log shows: Conventional Commits (`feat: …`), a ticket prefix, the language used. With no clear
style:

- subject: imperative, at most 72 characters, no final period (`Fix crash when the cart is empty`);
- a blank line, then 1–3 lines on why, only when the subject does not say it;
- the commit attribution lines your environment asks for, if any.

Write it to a temporary file and commit with `git commit -F <file>`, so quotes and backticks survive.

A pre-commit hook fails: fix what it reports, stage the fix and commit again.

## 4. Push

- **On the default branch** (`main`, `master`…): ask once before pushing, unless the user already said to push there.
- **The branch has an upstream**: `git push`.
- **No upstream**: `git push -u origin HEAD`.
- **Rejected** because the remote has commits this branch lacks: stop, explain it in one line and propose bringing
  them in first (the merge skill, or `git pull --rebase` if the user prefers).

## 5. Report

Two or three lines: the commit (`abc1234 Subject`), the files left out and why, where it was pushed, and the PR link
if `gh pr view --json url -q .url` finds one.

If a hook or a permission rule blocks the commit or the push, do not work around it: give the user the exact command
to run themselves, prefixed with `!`, for example `! git commit -F /tmp/msg.txt && git push`.

## Never

- `--force`, `--no-verify`, `--amend`.
- Commit a file that looks like a secret.
