---
name: my-prs
description: All your open pull requests across every repository on one screen, the ones that need you first - conflicts, red CI, review comments to answer, no reviewer asked, behind the base, ready to merge - and the ones waiting for someone else, with the next step for each, and how far each one is behind its own base. Two API calls. Read-only. Use for "/my-prs", "my open PRs", "which PRs need me", "what's blocking my PRs", "le mie PR aperte", "quali PR mi aspettano", "cosa manca alle mie PR", "a che punto sono le mie PR". Not for working through the comments of one PR (babysit).
---

# my-prs

Read-only: it reports and suggests, it never changes a PR.

## 0. Project pieces

A project can add to or replace parts of this skill in its playbook. Print this skill's section, and nothing else
of the playbook:

```bash
python3 "${CLAUDE_SKILL_DIR}/../../shared/project_config.py" section my-prs
```

Nothing printed: the defaults below. This skill's piece is `base-lag` (§2). A `### base-lag` subsection adds to the
default, or replaces it when its first line reads `Replaces the default.`; other text in the section wins where it
and this skill both speak.

## 1. Collect

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/my_prs.py"
```

It needs `gh`, logged in, with network access. It prints one line per open PR you authored, `→` in front of the
ones that need you, most urgent first:

- **yours**: conflicts with the base, red CI, review threads where the last word is not yours, changes requested,
  no reviewer asked, behind the base, ready to merge;
- **someone else's**: CI running, waiting for the named reviewers, a branch rule, a draft.

Before the steps, a line may carry the lag, in commits: `N behind <base>`, `on #N` (its base is the head of your PR
#N), `<base> M behind <default>` (a base that is not the default branch, behind it).

## 2. Report

In the user's language, labels included, short:

- one line: how many PRs, how many need the user;
- **Your turn**: one line per PR, `repo#N` with its link, the title, and the step with the skill that
  does it (`/dev:merge`, `/dev:ci-why`, `/dev:babysit`);
- **Waiting**: one line per PR, who or what it waits for, and the PR's age (the `Nd` column, days since it was opened);
- **Draft**: only how many and which, unless the user asks more.

Piece `base-lag`. The lag counts against the PR's own base, never the default branch, so a stacked PR is measured
against the branch it will merge into. A PR `on #N` shows no lag of its base: #N's line has it. A base that is not
the default branch passes its lag behind the default to every PR on it: say it once per base, not on each line.
Updating is a step only when GitHub says the PR is behind (the base requires up-to-date branches); otherwise the lag
is context, worth a word when it is large or the PR is ready to merge. A conflict is only `CONFLICTING` or `DIRTY`:
`BLOCKED` or `UNSTABLE` alone is not one.

End with the one step to take first: the PR closest to merging usually wins over the newest one.

Asked about a single PR: run the step it names (for example `/dev:babysit <url>`), not this skill again.
