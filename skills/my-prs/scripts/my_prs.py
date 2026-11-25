#!/usr/bin/env python3
"""Your open pull requests across every repository, one line each, the ones that need you first.

    my_prs.py      every open PR you authored: what it waits for and the next step

Two GraphQL calls, whatever the number of repositories: the search, then how far each PR is behind its own base
and how far a base that is not the default branch is behind it. Needs gh, logged in. Stdlib only.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

QUERY = """query($q:String!){viewer{login} search(query:$q,type:ISSUE,first:100){issueCount nodes{... on PullRequest{
  id number title url isDraft createdAt baseRefName headRefName headRefOid baseRef{target{oid}}
  repository{nameWithOwner defaultBranchRef{name}} mergeable mergeStateStatus reviewDecision
  reviewRequests(first:10){nodes{requestedReviewer{... on User{login} ... on Team{name} ... on Bot{login}}}}
  reviewThreads(first:100){nodes{isResolved comments(last:1){nodes{author{login}}}}}
  commits(last:1){nodes{commit{statusCheckRollup{state}}}}}}}}"""
YOURS = 2  # steps up to this priority are yours to take


def run_gh(*args):
    """(gh's stdout, the error or None)."""
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        sys.exit("gh not found: install the GitHub CLI and run gh auth login")
    except subprocess.TimeoutExpired:
        return None, "timed out after 90 seconds"
    return p.stdout, (p.stderr.strip() or f"exit {p.returncode}") if p.returncode else None


def gh(*args):
    out, err = run_gh(*args)
    if err:
        sys.exit(f"gh: {err}")
    return out


def ci_state(pr):
    nodes = (pr.get("commits") or {}).get("nodes") or [{}]
    return ((nodes[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state")


def default_branch(pr):
    return ((pr.get("repository") or {}).get("defaultBranchRef") or {}).get("name")


def stacked_on(prs):
    """{PR id: the number of your PR whose head branch is its base}, in the same repository."""
    heads = {(p["repository"]["nameWithOwner"], p.get("headRefName")): p["number"] for p in prs}
    stacks = {}
    for p in prs:
        below = heads.get((p["repository"]["nameWithOwner"], p["baseRefName"]))
        if below and below != p["number"]:
            stacks[p["id"]] = below
    return stacks


def lag_query(prs, stacks):
    """One query: each PR's head against its base, and a base that is neither the default branch nor another of your
    PRs against the default branch. None when there is nothing to compare."""
    parts = []
    for i, p in enumerate(prs):
        tip = ((p.get("baseRef") or {}).get("target") or {}).get("oid")
        if not tip or not p.get("headRefOid"):
            continue
        inherited = ""
        if default_branch(p) and p["baseRefName"] != default_branch(p) and p["id"] not in stacks:
            inherited = " repository{defaultBranchRef{compare(headRef:%s){behindBy}}}" % json.dumps(tip)
        parts.append("p%d:node(id:%s){... on PullRequest{baseRef{compare(headRef:%s){behindBy}}%s}}"
                     % (i, json.dumps(p["id"]), json.dumps(p["headRefOid"]), inherited))
    return "query{%s}" % " ".join(parts) if parts else None


def apply_lag(prs, data):
    """Store the counts from the lag query's data on each PR: behind (its base) and baseBehind (the default)."""
    for i, p in enumerate(prs):
        node = (data or {}).get(f"p{i}") or {}
        p["behind"] = (((node.get("baseRef") or {}).get("compare")) or {}).get("behindBy")
        p["baseBehind"] = ((((node.get("repository") or {}).get("defaultBranchRef") or {}).get("compare"))
                           or {}).get("behindBy")


def measure_lag(prs, stacks):
    """Fill behind and baseBehind on each PR. Returns an error text when the counts could not be read."""
    query = lag_query(prs, stacks)
    if not query:
        return None
    out, err = run_gh("api", "graphql", "-f", f"query={query}")
    try:
        data = json.loads(out or "{}").get("data")
    except ValueError:
        data = None
    apply_lag(prs, data)
    return err if data is None else None


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
        count = f"{pr['behind']} " if pr.get("behind") else ""
        out.append((2, f"{count}behind {base} → gh pr update-branch {n}"))
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


def lag(pr):
    """Commits behind its own base, the PR of yours it sits on, and the lag the base itself carries; "" when none."""
    base, out = pr["baseRefName"], []
    if pr.get("stackedOn"):
        out.append(f"on #{pr['stackedOn']}")
    if pr.get("behind") and pr.get("mergeStateStatus") != "BEHIND":
        out.append(f"{pr['behind']} behind {base}")
    if pr.get("baseBehind"):
        out.append(f"{base} {pr['baseBehind']} behind {default_branch(pr)}")
    return ", ".join(out)


def line(pr, todo, now):
    created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
    behind = lag(pr)
    return (f"{pr['repository']['nameWithOwner']}#{pr['number']} · {pr['title']} · {(now - created).days}d · "
            + (f"{behind} · " if behind else "") + "; ".join(text for _, text in todo))


def main():
    if len(sys.argv) > 1:
        sys.exit(__doc__.strip())
    data = json.loads(gh("api", "graphql", "-f", f"query={QUERY}",
                         "-f", "q=is:pr is:open author:@me archived:false"))["data"]
    me, found = data["viewer"]["login"], data["search"]
    prs = [p for p in found["nodes"] if p]
    stacks = stacked_on(prs)
    for p in prs:
        p["stackedOn"] = stacks.get(p["id"])
    lag_error = measure_lag(prs, stacks)
    now = datetime.now(timezone.utc)
    rows = sorted(((steps(p, me), p) for p in prs), key=lambda r: (r[0][0][0], r[1]["createdAt"]))
    yours = sum(1 for todo, _ in rows if todo[0][0] <= YOURS)
    print(f"{me} · {len(prs)} open PRs · {yours} need you")
    if found["issueCount"] > len(prs):
        print(f"(only the first {len(prs)} of {found['issueCount']})")
    if lag_error:
        print(f"(lag behind the bases not measured: {lag_error[:200]})")
    for todo, pr in rows:
        print(("→ " if todo[0][0] <= YOURS else "  ") + line(pr, todo, now))


if __name__ == "__main__":
    main()
