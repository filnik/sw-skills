#!/bin/bash
# Checks that sub-codex and the quota hook keep Codex to the folders of the sub_codex config, on a throwaway home.
# Never starts Codex: in an allowed folder the script stops earlier, on the missing prompt (exit 2).
here=$(cd "$(dirname "$0")" && pwd); fail=0
tmp=$(mktemp -d "${TMPDIR:-/tmp}/sub-codex-test.XXXXXX") || exit 1
trap 'rm -rf "$tmp"' EXIT
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected $3, got $2"; fail=1; fi; }
mkdir -p "$tmp/bare" "$tmp/broken/.claude/config" "$tmp/home/.claude/config" "$tmp/home/work/app" \
  "$tmp/home/work-fake-prefix" "$tmp/personal" "$tmp/out"
export TMPDIR="$tmp/out"
printf '{"sub_codex": {"folders": ["~/work"], "label": "Work", "language": "Testish"}}' > "$tmp/home/.claude/config/project.json"
printf '{"sub_codex": ' > "$tmp/broken/.claude/config/project.json"

HOME="$tmp/bare" bash "$here/sub-codex.sh" --cd "$tmp/personal" 2>/dev/null; check "no config: any folder allowed" $? 2
HOME="$tmp/broken" bash "$here/sub-codex.sh" --cd "$tmp/personal" 2>/dev/null; check "broken config: refused" $? 4
export HOME="$tmp/home"
bash "$here/sub-codex.sh" --cd "$tmp/personal" 2>/dev/null; check "outside the folders: refused" $? 4
bash "$here/sub-codex.sh" --cd "$tmp/home/work-fake-prefix" 2>/dev/null; check "similar prefix but outside: refused" $? 4
bash "$here/sub-codex.sh" --cd "$tmp/home/missing" 2>/dev/null; check "missing folder: refused" $? 4
bash "$here/sub-codex.sh" --cd "$tmp/home/work" 2>/dev/null; check "folder root: allowed" $? 2
bash "$here/sub-codex.sh" --cd "$tmp/home/work/app" 2>/dev/null; check "subfolder: allowed" $? 2

out=$(SUB_CODEX_ROUTE="claude reserve" bash "$here/sub-codex.sh" --cd "$tmp/home/work" --name test-handoff "try" 2>/dev/null)
check "Codex in reserve: exit 5" $? 5
check "Codex in reserve: sub-claude call" \
  "$(printf '%s\n' "$out" | grep -cE 'subagent_type: sub-claude, model: (sonnet|opus|fable), effort: (medium|high|xhigh)')" 1
check "Codex in reserve: language from the config" "$(printf '%s\n' "$out" | grep -c 'Final message in Testish\.')" 1
SUB_CODEX_ROUTE="claude usedup" bash "$here/sub-codex.sh" --cd "$tmp/home/work" --needs-codex "try" >/dev/null 2>&1
check "Codex used up and --needs-codex: exit 5" $? 5
SUB_CODEX_ROUTE="claude reserve" bash "$here/sub-codex.sh" --cd "$tmp/home/work" >/dev/null 2>&1
check "reserve without a prompt: exit 2" $? 2

hook() { printf '{"cwd": "%s"}' "$1" | python3 "$here/quota-route.py" | grep -c "$2"; }
check "hook outside the folders: Claude" "$(hook "$tmp/personal" '^quota route: claude (not a Work folder')" 1
check "hook inside the folders: normal routing" "$(hook "$tmp/home/work/app" 'not a Work folder')" 0
check "language from the config" "$(python3 "$here/quota-route.py" --language)" Testish
export HOME="$tmp/bare"
check "hook with no config: normal routing" "$(hook "$tmp/personal" 'not a ')" 0
check "language without a config: English" "$(python3 "$here/quota-route.py" --language)" English
exit $fail
