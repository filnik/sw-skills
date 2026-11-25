# Luna workers: instructions for Codex

You run on gpt-6.2-sol. The mechanical part of your task goes to sub-agents on `gpt-6-luna`, which cost about a
twentieth of you per token; the thinking stays with you. Your skill's instructions list which part is which.

## Spawn

`spawn_agent` with `model: "gpt-6-luna"`, `reasoning_effort: "high"`, `fork_turns: "none"` and a message that
stands on its own:

- "You are a worker: do only this step, do not spawn agents, and draw no conclusions beyond what you are asked.";
- the instruction file and section it must follow (it reads them itself), the repo or clone, the ids, the window
  with the timezone, the filters, the read-only rules of your brief and the git and search rules at the top of
  your prompt (never commit, push, rebase, merge, reset or switch branches; `rg` on the working tree, `LC_ALL=C`
  before any git grep that is not plain `-F` without `-i`), copied word for word;
- the exact commands, queries or tool calls, or what each one must count when the worker writes them;
- where to write the raw output (under your scratch dir) and what to return: a compact table, counts with their
  definition, the query text, the file paths. Never raw logs or whole files.

Independent pieces go to parallel workers (one per source, per failed job, per item), then `wait_agent`, unless
your skill says a source takes one query at a time. Searches over the same repo: at most two workers at a time.

## Keep for yourself

Planning (which queries, keys, windows and searches answer the question), every judgement (match or not,
regression or not, cause, class, new or grown), the comparisons, the checks below, the report and the final
message.

## Check what comes back

Before you use a result: the window, filters and version strings are the ones you asked for; a part is not larger
than its total; each number says what it counts; a quoted line is in the file it cites. Something off or missing:
a `followup_task` to that worker, or that one step yourself, never the whole gathering again.

If `spawn_agent` fails, or a worker cannot do its step (a connector or tool it does not have, an error it cannot
fix), do that step yourself and say so in the final message.
