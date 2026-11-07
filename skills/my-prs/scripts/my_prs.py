#!/usr/bin/env python3
"""Your open pull requests across every repository, one line each, the ones that need you first.

    my_prs.py      every open PR you authored: what it waits for and the next step

One GraphQL search, so it costs one API call whatever the number of repositories.
Needs gh, logged in. Stdlib only.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

QUERY = """query($q:String!){viewer{login} search(query:$q,type:ISSUE,first:100){issueCount nodes{... on PullRequest{
  number title url isDraft createdAt baseRefName repository{nameWithOwner} mergeable mergeStateStatus reviewDecision
  reviewRequests(first:10){nodes{requestedReviewer{... on User{login} ... on Team{name} ... on Bot{login}}}}
  reviewThreads(first:100){nodes{isResolved comments(last:1){nodes{author{login}}}}}
  commits(last:1){nodes{commit{statusCheckRollup{state}}}}}}}}"""
YOURS = 2  # steps up to this priority are yours to take


def gh(*args):
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        sys.exit("gh not found: install the GitHub CLI and run gh auth login")
    except subprocess.TimeoutExpired:
        sys.exit("gh api graphql: timed out after 90 seconds")
    if p.returncode:
        sys.exit(f"gh: {p.stderr.strip()}")
    return p.stdout


def ci_state(pr):
    nodes = (pr.get("commits") or {}).get("nodes") or [{}]
    return ((nodes[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state")


def steps(pr, me):
    """What the PR waits for, as (priority, text), most urgent first. 0-2 are yours to do, 3-4 are someone else's."""
    out, base, n = [], pr["baseRefName"], pr["number"]
    ci, merge, review = ci_state(pr), pr.get("mergeStateStatus"), pr.get("reviewDecision")
    to_answer = sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"]
                    and ((t["comments"]["nodes"] or [{}])[-1].get("author") or {}).get("login") != me)
    asked = [r["requestedReviewer"].get("login") or r["requestedReviewer"].get("name")
             for r in pr["reviewRequests"]["nodes"] if r.get("requestedReviewer")]

    if pr.get("mergeable") == "CONFLICTING" or merge == "DIRTY":
        out.append((0, f"conflicts with {base} → /dev:merge"))
    if ci in ("FAILURE", "ERROR"):
        out.append((0, "CI red → /dev:ci-why"))
    if to_answer or review == "CHANGES_REQUESTED":
        what = f"{to_answer} open thread{'s' if to_answer > 1 else ''} to answer" if to_answer else "changes requested"
        out.append((1, f"{what} → /dev:babysit"))
    if pr["isDraft"]:
        return sorted(out + [(4, "draft")])
    if merge == "BEHIND":
        out.append((2, f"behind {base} → gh pr update-branch {n}"))
    if ci in ("PENDING", "EXPECTED"):
        out.append((3, "CI running"))
    if review == "REVIEW_REQUIRED":
        out.append((3, f"waiting for {', '.join(asked)}") if asked
                   else (1, f"no reviewer asked → gh pr edit {n} --add-reviewer <login>"))
    if not out:
        if merge in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
            out.append((2, "ready to merge"))
        elif merge == "UNKNOWN" or not merge:
            out.append((3, "merge state not computed yet: run again in a minute"))
        else:
            out.append((3, f"blocked by a branch rule ({merge.lower()})"))
    return sorted(out)


def line(pr, todo, now):
    created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
    return (f"{pr['repository']['nameWithOwner']}#{pr['number']} · {pr['title']} · {(now - created).days}d · "
            + "; ".join(text for _, text in todo))


def main():
    if len(sys.argv) > 1:
        sys.exit(__doc__.strip())
    data = json.loads(gh("api", "graphql", "-f", f"query={QUERY}",
                         "-f", "q=is:pr is:open author:@me archived:false"))["data"]
    me, found = data["viewer"]["login"], data["search"]
    prs = [p for p in found["nodes"] if p]
    now = datetime.now(timezone.utc)
    rows = sorted(((steps(p, me), p) for p in prs), key=lambda r: (r[0][0][0], r[1]["createdAt"]))
    yours = sum(1 for todo, _ in rows if todo[0][0] <= YOURS)
    print(f"{me} · {len(prs)} open PRs · {yours} need you")
    if found["issueCount"] > len(prs):
        print(f"(only the first {len(prs)} of {found['issueCount']})")
    for todo, pr in rows:
        print(("→ " if todo[0][0] <= YOURS else "  ") + line(pr, todo, now))


if __name__ == "__main__":
    main()
