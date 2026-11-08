---
name: my-prs
description: All your open pull requests across every repository on one screen, the ones that need you first - conflicts, red CI, review comments to answer, no reviewer asked, behind the base, ready to merge - and the ones waiting for someone else, with the next step for each. One API call. Read-only. Use for "/my-prs", "my open PRs", "which PRs need me", "what's blocking my PRs", "le mie PR aperte", "quali PR mi aspettano", "cosa manca alle mie PR", "a che punto sono le mie PR". Not for working through the comments of one PR (babysit).
---

# my-prs

Read-only: it reports and suggests, it never changes a PR.

## 1. Collect

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/my_prs.py"
```

It needs `gh`, logged in, with network access. It prints one line per open PR you authored, `→` in front of the
ones that need you, most urgent first:

- **yours**: conflicts with the base, red CI, review threads where the last word is not yours, changes requested,
  no reviewer asked, behind the base, ready to merge;
- **someone else's**: CI running, waiting for the named reviewers, a branch rule, a draft.

## 2. Report

In the user's language, short:

- one line: how many PRs, how many need the user;
- **Tocca a te / Your turn**: one line per PR, `repo#N` with its link, the title, and the step with the skill that
  does it (`/dev:merge`, `/dev:ci-why`, `/dev:babysit`);
- **In attesa / Waiting**: one line per PR, who or what it waits for, and the PR's age (the `Nd` column, days since it was opened);
- **Draft**: only how many and which, unless the user asks more.

End with the one step to take first: the PR closest to merging usually wins over the newest one.

Asked about a single PR: run the step it names (for example `/dev:babysit <url>`), not this skill again.
