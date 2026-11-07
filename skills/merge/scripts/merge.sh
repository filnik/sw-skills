#!/bin/bash
# The git part of the /merge skill: merge the target (the PR's base, or a named branch) into the current branch.
# Claude resolves the conflicts between start and finish.
# Usage: merge.sh [-C DIR] start [TARGET] | finish [--trailer LINE]... | abort

set -uo pipefail

dir=$PWD
if [ "${1:-}" = "-C" ]; then dir=${2:-}; shift 2; fi
cmd=${1:-}; [ $# -gt 0 ] && shift

die() { echo "merge: $*" >&2; exit 1; }
cd "$dir" 2>/dev/null || die "directory not found: $dir"
git rev-parse --git-dir >/dev/null 2>&1 || die "$dir is not a git repository"
branch=$(git symbolic-ref --short -q HEAD) || die "detached HEAD: check out a branch first"
state=$(git rev-parse --git-path dev-merge)

in_merge() { git rev-parse -q --verify MERGE_HEAD >/dev/null; }
ours() { in_merge && [ -f "$state/target" ] || die "no merge started by merge.sh"; }

show_conflicts() {
  echo "STATUS: conflicts in $(wc -l < "$state/conflicts" | tr -d ' ') files"
  git status --short --untracked-files=no | grep -E '^(DD|AU|UD|UA|DU|AA|UU) '
}

start() {
  local target="" origin_of="named" pr
  [ $# -le 1 ] || die "usage: merge.sh start [TARGET]"
  target=${1:-}
  if in_merge; then
    [ -f "$state/target" ] && die "a merge of $(cat "$state/target") is already in progress: finish or abort"
    die "a merge not started by merge.sh is in progress: finish or abort it first"
  fi
  [ ! -f "$state/target" ] || die "a previous merge.sh run did not finish: run abort"
  if ! { git diff --quiet && git diff --cached --quiet; }; then
    git status --short --untracked-files=no >&2
    die "uncommitted changes: commit or stash them first"
  fi

  if [ -z "$target" ]; then
    pr=$(gh pr view --json number,baseRefName,state --jq 'select(.state=="OPEN") | "\(.number) \(.baseRefName)"' 2>/dev/null)
    if [ -n "$pr" ]; then
      target=${pr#* }; origin_of="base of PR #${pr%% *}"
    else
      target=$(git symbolic-ref --short -q refs/remotes/origin/HEAD); target=${target#origin/}
      target=${target:-main}; origin_of="default branch, no open PR for $branch"
    fi
  fi
  [ "$target" = "$branch" ] && die "the target is the current branch"

  git fetch -q origin "+refs/heads/$target:refs/remotes/origin/$target" || die "origin/$target not found"
  # Start from origin/<branch>: commits pushed from elsewhere (or a GitHub update-branch) come first.
  if git fetch -q origin "+refs/heads/$branch:refs/remotes/origin/$branch" 2>/dev/null; then
    if [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$branch")" ]; then
      if git merge-base --is-ancestor HEAD "origin/$branch"; then
        git merge -q --ff-only "origin/$branch" || die "fast-forward to origin/$branch failed"
        echo "aligned with origin/$branch"
      elif git merge-base --is-ancestor "origin/$branch" HEAD; then
        echo "$(git rev-list --count "origin/$branch..HEAD") local commits not on origin yet: they go with the push"
      else
        die "$branch and origin/$branch have diverged: align them first (git pull --rebase or git pull)"
      fi
    fi
  else
    echo "$branch is not on origin yet: the push creates it"
  fi

  echo "target: origin/$target ($origin_of)"
  if git merge-base --is-ancestor "origin/$target" HEAD; then
    echo "STATUS: up to date, $branch already contains origin/$target"
    return 0
  fi
  echo "$(git rev-list --count "HEAD..origin/$target") commits coming in"

  local out
  out=$(git merge --no-ff --no-commit "origin/$target" 2>&1)
  in_merge || { echo "$out" >&2; die "the merge did not start"; }
  mkdir -p "$state"
  echo "$target" > "$state/target"
  git diff --name-only --diff-filter=U > "$state/conflicts"
  if [ -s "$state/conflicts" ]; then
    show_conflicts
  else
    echo "STATUS: clean, ready for finish"
  fi
}

finish() {
  local trailers=() target left=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --trailer) [ $# -ge 2 ] || die "--trailer needs a value"; trailers+=(--trailer "$2"); shift 2 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  ours
  while IFS= read -r f; do
    [ -f "$f" ] && grep -qE '^(<<<<<<<|>>>>>>>)( |$)' "$f" && left="$left $f"
  done < "$state/conflicts"
  [ -z "$left" ] || die "conflict markers still in:$left"
  git add -u || die "git add failed"
  [ -z "$(git diff --name-only --diff-filter=U)" ] || die "some files are still unmerged"

  target=$(cat "$state/target")
  git commit -q -m "Merge $target into $branch" ${trailers[@]+"${trailers[@]}"} || die "commit failed"
  rm -rf "$state"
  # Push explicitly to origin/<branch>: the upstream might point at the base.
  git push -q origin "HEAD:refs/heads/$branch" || die "push failed: the merge commit is only local"
  echo "$(git log -1 --format='%h %s') pushed to origin/$branch"
}

abort() {
  if ! in_merge && [ ! -f "$state/target" ]; then die "no merge in progress"; fi
  if in_merge; then git merge --abort || die "git merge --abort failed"; fi
  rm -rf "$state"
  echo "merge aborted: $branch is as it was before start"
}

case "$cmd" in
  start)  start "$@" ;;
  finish) finish "$@" ;;
  abort)  abort ;;
  *)      sed -n '2,4p' "$0"; exit 2 ;;
esac
