#!/bin/bash
# End-to-end test for skills/merge/scripts/merge.sh on throwaway repos. Run: bash tests/test_merge.sh (exit 0 = green).
set -euo pipefail

merge="$(cd "$(dirname "$0")/.." && pwd)/skills/merge/scripts/merge.sh"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/dev-merge-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# Isolated git config, and a gh that finds no PR, so the target falls back to origin's default branch.
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$tmp/gitconfig"
git config --global user.name test
git config --global user.email test@example.com
git config --global init.defaultBranch main
mkdir "$tmp/fakebin"
printf '#!/bin/sh\nexit 1\n' > "$tmp/fakebin/gh"
chmod +x "$tmp/fakebin/gh"
export PATH="$tmp/fakebin:$PATH"

fail() { echo "FAIL: $*" >&2; exit 1; }
expect() { grep -q -- "$1" <<< "$2" || fail "expected '$1' in: $2"; }

git init -q --bare "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/seed" 2>/dev/null
printf 'one\ntwo\nthree\n' > "$tmp/seed/file.txt"
git -C "$tmp/seed" add file.txt && git -C "$tmp/seed" commit -qm base && git -C "$tmp/seed" push -q origin main
git clone -q "$tmp/origin.git" "$tmp/me"
git clone -q "$tmp/origin.git" "$tmp/other"

# The branch and main change the same line.
git -C "$tmp/me" switch -qc feature
sed -i.bak 's/two/two-feature/' "$tmp/me/file.txt" && rm "$tmp/me/file.txt.bak"
git -C "$tmp/me" commit -qam feature && git -C "$tmp/me" push -q origin feature
sed -i.bak 's/two/two-main/' "$tmp/other/file.txt" && rm "$tmp/other/file.txt.bak"
git -C "$tmp/other" commit -qam main-change && git -C "$tmp/other" push -q origin main

# Uncommitted changes are refused.
echo dirty >> "$tmp/me/file.txt"
out=$("$merge" -C "$tmp/me" start 2>&1) && fail "start accepted uncommitted changes"
expect "uncommitted changes" "$out"
git -C "$tmp/me" checkout -q file.txt

# A conflict stops at start; finish refuses the markers.
out=$("$merge" -C "$tmp/me" start 2>&1)
expect "default branch" "$out"
expect "STATUS: conflicts in 1 files" "$out"
out=$("$merge" -C "$tmp/me" finish 2>&1) && fail "finish accepted conflict markers"
expect "conflict markers still in: file.txt" "$out"

# Resolved, finish commits a two-parent merge and pushes it.
printf 'one\ntwo-feature-and-main\nthree\n' > "$tmp/me/file.txt"
out=$("$merge" -C "$tmp/me" finish --trailer "Co-Authored-By: Test <t@example.com>" 2>&1)
expect "pushed to origin/feature" "$out"
[ "$(git -C "$tmp/origin.git" rev-list --parents -n 1 feature | wc -w | tr -d ' ')" = 3 ] || fail "not a merge commit"
expect "Merge main into feature" "$(git -C "$tmp/origin.git" log -1 --format=%B feature)"
expect "Co-Authored-By: Test" "$(git -C "$tmp/origin.git" log -1 --format=%B feature)"

# Nothing new on main: up to date.
out=$("$merge" -C "$tmp/me" start 2>&1)
expect "STATUS: up to date" "$out"

# Abort puts the branch back.
sed -i.bak 's/three/three-main/' "$tmp/other/file.txt" && rm "$tmp/other/file.txt.bak"
git -C "$tmp/other" commit -qam again && git -C "$tmp/other" push -q origin main
sed -i.bak 's/three/three-feature/' "$tmp/me/file.txt" && rm "$tmp/me/file.txt.bak"
git -C "$tmp/me" commit -qam again-feature
before=$(git -C "$tmp/me" rev-parse HEAD)
"$merge" -C "$tmp/me" start >/dev/null 2>&1
out=$("$merge" -C "$tmp/me" abort 2>&1)
expect "merge aborted" "$out"
[ "$(git -C "$tmp/me" rev-parse HEAD)" = "$before" ] || fail "abort moved HEAD"
[ -z "$(git -C "$tmp/me" status --porcelain)" ] || fail "abort left changes"

echo ok
