# Stream: tests — coverage of the change

Is every behaviour the PR adds or changes covered by a test that would fail if it broke?

1. List the behaviours the PR adds or changes, one line each: branches, error paths, edge cases, platform-specific
   code, migrations, and the brief's checks.
2. For each, find the tests that exercise it: added or changed in the PR, and existing ones (grep the symbols in
   `commonTest`, `androidUnitTest`, `iosTest`, `*Test.kt`, screenshot tests, Swift tests). Mark it `covered`,
   `partial` or `uncovered`.
3. For each covering test, read its assertions: would it fail if the change were reverted or the branch broke? Flag
   tests that assert nothing meaningful or only that a mock was called, would still pass if the code under test
   returned nothing (null, empty, Unit), mock the unit under test, copy their
   expected values from the implementation, cover the happy path where the change is about an edge case, depend on
   real time, order or shared state, or whose name does not match what they check. Test names use "and", never
   commas or dashes.
4. Tests the PR deletes or weakens (assertions removed, `@Ignore`, looser matchers): acceptable or not, and why.
5. CI: `gh pr checks <n>` and the test jobs at the head SHA. A test job skipped by the test selection while the PR
   touches its module is a finding.
6. When the brief allows it, run the unit tests of the touched modules once and report the result.

Per gap: the test to add, with the file it belongs in (next to the existing tests of that class), its name, the
arrange/act/assert in 3–5 lines, and size `small` or `large`. A missing test is a `risk`, never a `bug`: `bug` is a
test that asserts the wrong behaviour. `nit` for naming and structure.

## Report sections

1. Coverage: one row per behaviour → tests (`file:line`) → covered / partial / uncovered → quality note.
2. Deleted or weakened tests.
3. CI test jobs at the head SHA.
