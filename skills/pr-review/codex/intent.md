# Stream: intent — the ticket's requirements and the fit with the app

Two questions: does the PR do what the ticket asks, and nothing it should not; and does the change make sense in the
app's flow.

## 1. Requirements

For each check `AC1…` in the brief:

- status: `met` | `partial` | `missing` | `not verifiable from code`;
- where it is done: the code path from the user action or trigger (screen, button, background job, sync) to the
  effect (state, request, DB row, UI), with `file:line` at each hop;
- for `partial` or `missing`, the case left out: a platform, offline, an existing install, an error path, another
  entry point to the same feature.

Then: what the PR changes that no check and no scope line asks for (behaviour changed silently), and out-of-scope
lines it crosses. Behaviour the ticket leaves open is looked up in the knowledge base the brief names, not
guessed. When the ticket or the knowledge base puts the feature behind a flag, check that every new path is gated and
the flag-off path is unchanged.

## 2. Fit with the app

Judge the approach, not the lines:

- Altitude: the layer the change sits in (UI, ViewModel, use case, repository, sync, platform) and whether the cause
  or the responsibility lives there; a fix patched at one caller while the other callers keep the problem.
- Flows: grep the entry points that reach the changed code, up to the screens and background jobs, and say for each
  whether it still behaves right: navigation and back stack, restart and process death, offline and sync,
  background work, existing installs and migrations, both platforms.
- Consistency: does it do things the way the app already does them elsewhere (state handling, errors, logging,
  analytics, strings). Line-level duplication is the reuse stream's job; mention only a second mechanism for
  something the app already has a mechanism for.
- Proposed verdict, for Claude to decide: `fits` | `fits with changes` | `wrong approach`, with the reason in 2–4
  lines and the counterexamples behind it; for `wrong approach`, the approach that would fit and the existing code it
  would build on.

## Report sections

1. Requirements: one row per check → status → `file:line` → gap.
2. Unrequested changes.
3. Flow map, one line per entry point (entry → changed code → effect → still right?), then the verdict and reason.
