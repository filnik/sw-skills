---
name: ticket
description: Turns a problem or a request into a well-written issue on GitHub Issues or a task on ClickUp - understands it first (the conversation, the code, the work in progress), checks for duplicates, and writes it as Problem, Scope, Out of scope and Acceptance criteria, with steps to reproduce for a bug. Creates it and returns the link. Use for "/ticket", "open an issue for this", "file a bug", "create a ticket for…", "log this as a task", "apri un ticket", "crea una issue", "segnalo come bug". Not for picking up an existing issue (pick).
---

# ticket

If the user asked for an issue in their own words (`/dev:ticket`, "open an issue", "file a bug"), that is the ok to
create one. If you chose this skill yourself or another skill handed over (e.g. crashes), show the title and body
and wait for an ok. Ask first also when the request is open (one issue or two, which of two scopes) or a likely
duplicate turns up. A change after creation is an edit of the issue.

## 1. The tracker

ClickUp when the user says so or the project has a `clickup` block in `.claude/config/project.json`; otherwise
GitHub Issues, through `gh`. ClickUp goes through the pick skill's script, installed next to this skill:
`python3 "${CLAUDE_SKILL_DIR}/../pick/scripts/clickup.py"` (written `clickup.py` below).

When `gh` or `clickup.py` fails (not installed, not logged in, no token, 401/403/404), show its message and stop.
Never ask for a token in the chat.

## 2. Understand it

- From the conversation: the request, what was already looked at, what was decided.
- "A ticket for this work": read `git diff` and `git status` and describe the change in progress.
- When the scope depends on how the code works today, read the code. Never guess it.
- For a bug, find where it happens if that is quick. Write what is known as fact and what is only suspected as a
  hypothesis.

## 3. Duplicates

- GitHub: `gh issue list --state all --search "<2-3 distinctive words>" --limit 10`.
- ClickUp: `clickup.py list --closed --search "<2-3 distinctive words>"`, closed tasks included, the closest
  names first.

A likely duplicate: show it and ask whether to comment on it instead, or create the new one anyway.

## 4. Write it

- **Title**: what is wrong or what to do, under 80 characters (`Checkout fails when the cart is empty`). In the
  language the tracker already uses; English when unsure.
- **Labels or tags**: only ones that already exist (`gh label list`; on ClickUp, the tags seen in `clickup.py list`).
  `bug`, `enhancement` or the like when they exist.
- **Body**, in this order:

  ```
  ## Problem
  What the user cannot do today, or the defect. For a bug: steps to reproduce, what happens, what should happen,
  where it was seen (version, environment).

  ## Scope
  What changes, in the user's words first, then the code areas involved.

  ## Out of scope
  What was considered and left out.

  ## Acceptance criteria
  - [ ] checkable statements, one per line
  ```

  Add links (a design, a log, a related issue) where there are some. Leave out a section only when there is truly
  nothing to put in it. Never copy tokens, keys, personal data or internal hostnames from logs into the body.

## 5. Create it

Write the body to a temporary file, then:

- GitHub: `gh issue create --title "<title>" --body-file <file> [--label <label>]…`
- ClickUp: `clickup.py create --title "<title>" --body-file <file> [--tag <tag>]…`

## 6. Report

The link and the title, in two lines, then: "to start it, `/pick <number or id>`".
