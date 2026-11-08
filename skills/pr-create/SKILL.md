---
name: pr-create
description: Opens the pull request of the current branch with gh, or updates the one already open - title and body from the repo's PR template, base from the branch you name, else the open PR, else the repo's default branch (asking when the branch looks stacked on another feature branch), and only the tests that really ran. Pushes the branch first when needed; stops on uncommitted work. Use for "/pr-create", "open the PR", "create a pull request", "make a PR", "apri la PR", "crea la PR". Not for committing (commit-push), following the review afterwards (babysit) or a red CI (ci-why).
---

# pr-create

Needs `gh`, logged in, with network access.

## 1. Context

```bash
git status --short --branch; gh pr view --json number,url,state,baseRefName,title,body; gh repo view --json defaultBranchRef -q .defaultBranchRef.name; git fetch -q origin
```

- **Uncommitted changes**: list them and stop, proposing the commit-push skill. A PR opened now would miss them.
- **An open PR for this branch**: update it (step 4), never open a second one. A merged or closed PR does not count.
- **The base**: the branch the user named; else the open PR's base; else the default branch. When the branch seems
  to start from another feature branch, ask. This prints the nearest origin branches in its history:

  ```bash
  git log --first-parent -n 200 --format=%D --decorate-refs=refs/remotes/origin HEAD | grep -v '^$' | head -3
  ```

  The first one that is neither this branch nor the default branch is its likely parent.

Then what the PR contains:

```bash
git log --oneline origin/<base>..HEAD; git diff --stat origin/<base>...HEAD
```

Nothing ahead of the base: say so and stop. Read the diff only for the files whose purpose this conversation does not
already explain.

## 2. Title and body

Write the body to a temporary file.

- **Title**: imperative, at most 72 characters. Put the ticket key first when the branch name or the commits carry
  one and the repo's PRs do that.
- **Body**: the repo's template when it has one (`.github/pull_request_template.md`,
  `.github/PULL_REQUEST_TEMPLATE.md`, `docs/pull_request_template.md`, `pull_request_template.md`): its headings, in
  its order, none left empty, none added. Without one:

  ```
  ## Why
  The problem, in 2-3 sentences. Link the issue if there is one.

  ## What changed
  Per area, or per commit when they are meant to be read in order.
  Intentional behaviour changes, called out so the reviewer does not take them for bugs.

  ## How to test
  Steps a reviewer can follow, each with what should happen.

  ## Tests
  What actually ran and its result; what was only compiled; what was not tested.
  ```

- Never claim a test, a check or a manual try that did not happen in this session or that the user did not report.
  Leave a template checkbox unticked when you cannot know.
- **Updating an open PR**: start from its current title and body (step 1), not from scratch. Keep what a person
  added on GitHub: ticked boxes, notes, screenshots, links. Change only what the new commits make stale.
- End with the PR attribution lines your environment asks for, if any.

## 3. Push

The branch is not on origin, or has commits not pushed: `git push -u origin HEAD`. Rejected: stop and explain. Never
force.

## 4. Create or update

```bash
gh pr create --base <base> --head <branch> --title '<title>' --body-file <file>
```

Put the title in single quotes, writing a `'` inside it as `'\''`: in double quotes, backticks and `$` in a title
run as commands. Add `--draft` when the user asks for a draft. With a PR already open, edit it only when its title
or body should change:

```bash
gh pr edit <N> --title '<title>' --body-file <file>
```

Add `--base <branch>` to the edit only when the user named a base that differs from the PR's.

## 5. Report

The PR link and its base, in two lines.
