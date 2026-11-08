---
name: recap
description: Says where the work stands, in plain words - what is done (with the evidence), what is still open, and what the user has to decide, with a recommendation for each decision - plus the state of the branch, of the work not committed or not pushed, and of the pull request. Read-only. Use for "/recap", "recap", "where are we", "what's left", "summarize where we are", "fammi un recap", "dove siamo", "cosa resta aperto", "cosa devo decidere", "riassumi dove siamo". Not for release notes (the release-notes skill), yesterday's work (the standup skill), all open PRs (the my-prs skill), or summarizing a file, PR or page.
---

# recap

The user has lost the thread, or is coming back to it. Give it back to them in a minute of reading.

Read-only: no edits, no commands that change anything, no new work.

## 1. Gather

- **The conversation**: what the user asked, what was done, what was decided, what was tried and failed, the
  questions still unanswered.
- **The repo**, in one call (anything that fails, such as no `gh` or no upstream, is just left out):

  ```bash
  git status --short --branch; git log --oneline @{upstream}..HEAD; gh pr view --json number,url,state,mergeStateStatus
  ```

Do not re-read files or re-run tests to write the recap. If something was never verified, say so.

## 2. Write

In the user's language, short, in plain words: a reader who knows the project but not this session must follow it.
No jargon without a few words of explanation. No internal labels (`fix #3`, `phase B`) without saying what they are.

```
**Done**
- <outcome> — <evidence: file, commit, test result>

**Open**
- <what is left> — <what it is waiting for>

**To decide**
1. <the question> — I'd go with <option>, because <reason>.

**Repo**: branch <name> · <N> files not committed · <N> commits not pushed · PR #<N> <state>
```

- **Done** means verified: a test passed, a command ran, the user confirmed it. Anything else goes under **Open**,
  for example "written, not tested yet".
- Leave out an empty section. Never invent filler.
- About 15 lines at most, unless the user asks for more.
- End with the one next step you recommend.
