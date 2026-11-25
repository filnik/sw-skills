# Stream: code — bugs, duplicates and missed reuse

Find what can go wrong in the changed code, duplicated logic and missed reuse. Requirements and coverage are
other streams: report them only when they cause a bug.

1. The brief's risks first: answer each one as a question, yes/no or a number, with its evidence.
2. Then the diff, hunk by hunk:
   - logic and edge cases: null, empty, first and last, boundaries, units;
   - coroutines and concurrency: scope, cancellation, dispatcher, shared mutable state, Flow collection lifecycle,
     races between UI and background work, shared mutable state with more than one writer;
   - errors: swallowed exceptions, wrong fallback, retry loops, guards (null check, try/catch, early return) that
     only hide a crash instead of fixing its cause;
   - idempotence: does running it twice, or after a crash halfway, end in the same state (sync, upload, migrations)?
   - persistence: Room migrations and schema exports, DataStore keys, existing installs with old data;
   - platform: Android lifecycle and process death, iOS background, permissions;
   - Compose: state, `remember` keys, side effects;
   - leftovers: user-visible text, generated files that no longer match their generator, debug code;
   - security: secrets, personal data or tokens in logs.
3. Callers and siblings: for each changed signature or behaviour, its callers and the sibling implementations (the
   other platform's `actual`, other implementations of the interface). Are they still right?
4. Free pass: anything else in the diff that looks wrong, with the same evidence bar.
5. Reuse: for new functions, classes, composables, helpers and mechanisms, look for existing equivalents by name,
   key calls and types (commonMain first, then platform modules and test utilities). Check repeated logic inside
   the PR too, including Android/iOS code that could be shared. Read both sides: identical behaviour is a
   duplicate, a small extension away is near reuse, different semantics is not a finding. Include parallel
   structures (cache, retry, mapper, observer, preference, navigation or logging) for a job the app already does.
   Each reuse finding names the existing `file:line`, the exact edit, why behaviour stays the same, and size
   `small` (up to ~5 lines) or `large`; severity usually `nit` or `risk`, `bug` only when divergence is wrong.

## Report sections

1. Findings first (common.md), including reuse findings.
2. Under Evidence: risks, one row per question → answer → evidence; the existing code and proposed edit for each
   reuse finding, under its id.
