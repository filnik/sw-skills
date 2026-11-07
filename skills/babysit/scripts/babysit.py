#!/usr/bin/env python3
"""A pull request's state and every review comment on one screen, and the replies in one call.

    babysit.py status [<N|url>] [--full]         merge state, CI, Copilot, every comment to triage
    babysit.py answer <N|url> <answers.json> [--dry-run]
                                                  reply on threads and resolve them; review bodies and
                                                  PR comments get one PR comment between them

Nothing is dropped: every unresolved thread, every review with a body and every PR comment is listed or counted.
New and unanswered items print in full; items already printed are marked shown. After an answer, open threads take
one line, and reviews and PR comments are only counted (--full prints everything in full).
answers.json: [{"id": "c123", "body": "Fixed in abc1234: …", "resolve": true}, {"id": "ic456", "body": "…"}]
Needs gh, logged in. Stdlib only.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

STATE = Path.home() / ".cache" / "dev-babysit"
COMMAND_TIMEOUT = 60
PAGE = "pageInfo{hasNextPage endCursor}"
REVIEWS = "databaseId author{login} state body commit{oid}"
COMMENTS = "databaseId author{login} body"
THREADS = f"id isResolved isOutdated path line originalLine comments(first:100){{{PAGE} nodes{{{COMMENTS}}}}}"
META = """query($owner:String!,$name:String!,$n:Int!){viewer{login} repository(owner:$owner,name:$name){pullRequest(number:$n){
  title url state isDraft author{login} baseRefName headRefOid mergeable mergeStateStatus reviewDecision
  reviewRequests(first:20){nodes{requestedReviewer{... on Bot{login} ... on User{login}}}}
  commits(last:1){nodes{commit{statusCheckRollup{state contexts(first:100){nodes{__typename
    ... on CheckRun{name status conclusion detailsUrl isRequired(pullRequestNumber:$n)}
    ... on StatusContext{context state targetUrl isRequired(pullRequestNumber:$n)}}}}}}}}}}"""
FAILED = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")


def gh(*args, stdin=None):
    command = ["gh", *args]
    try:
        p = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    except FileNotFoundError:
        sys.exit("gh not found: install the GitHub CLI and run gh auth login")
    except subprocess.TimeoutExpired:
        sys.exit(f"{shlex.join(command[:3])}: timed out after {COMMAND_TIMEOUT} seconds")
    if p.returncode:
        sys.exit(f"gh {' '.join(args[:3])}: {p.stderr.strip()}")
    return p.stdout


def graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    return json.loads(gh(*args))["data"]


def target(ref):
    """(owner, name, number) from a PR URL, a number in the current repo, or the current branch's PR."""
    if not ref:
        ref = gh("pr", "view", "--json", "url", "-q", ".url").strip()
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", ref)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    owner, name = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip().split("/")
    return owner, name, int(ref.lstrip("#"))


def every(owner, name, n, connection, fields):
    """All nodes of one PR connection, page after page."""
    nodes, cursor = [], None
    query = (f"query($owner:String!,$name:String!,$n:Int!,$c:String){{repository(owner:$owner,name:$name)"
             f"{{pullRequest(number:$n){{{connection}(first:100,after:$c){{{PAGE} nodes{{{fields}}}}}}}}}}}")
    while True:
        page = graphql(query, owner=owner, name=name, n=n, c=cursor)["repository"]["pullRequest"][connection]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]


def review_threads(owner, name, n):
    threads = every(owner, name, n, "reviewThreads", THREADS)
    query = (f"query($t:ID!,$c:String){{node(id:$t){{... on PullRequestReviewThread{{"
             f"comments(first:100,after:$c){{{PAGE} nodes{{{COMMENTS}}}}}}}}}}}")
    for thread in threads:
        page = thread["comments"]
        while page["pageInfo"]["hasNextPage"]:
            page = graphql(query, t=thread["id"], c=page["pageInfo"]["endCursor"])["node"]["comments"]
            thread["comments"]["nodes"].extend(page["nodes"])
    return threads


def load_state(owner, name, n):
    """seen: ids answered by an `answer` run. shown: ids `status` has printed."""
    state_file = STATE / f"{owner}_{name}_{n}.json"
    ledger = json.loads(state_file.read_text()) if state_file.is_file() else {}
    return state_file, set(ledger.get("seen", [])), set(ledger.get("shown", []))


def save_state(state_file, seen, shown):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"seen": sorted(seen), "shown": sorted(shown)}))


def clean(body):
    """A comment's text without the HTML and the boilerplate bots wrap it in."""
    b = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    # Copilot's overview: the per-file table restates the diff.
    b = re.sub(r"<details>\s*<summary><strong>What changed in this PR</strong></summary>.*?</details>", "", b, flags=re.S)
    b = re.sub(r"<sub>Review effort:.*?</sub>", "", b)
    b = re.sub(r"<picture>.*?alt=\"([^\"]*)\".*?</picture>", r"[\1]", b, flags=re.S)
    b = re.sub(r"\[([^\]]+)\]\(https://github\.com/[^)]*#discussion_r(\d+)\)", r"\1 (→ c\2)", b)
    b = re.sub(r"</?(details|summary|strong|sub|sup|b|i|p|br|picture|source|img)\b[^>]*>", "", b)
    b = b.replace("​", "")
    return re.sub(r"\n\s*\n+", "\n", b).strip()


def login(node):
    return (node.get("author") or {}).get("login", "ghost")


def indent(text):
    return "\n".join("    " + line for line in text.splitlines())


def one_line(text, width=110):
    t = " ".join(text.split())
    return t if len(t) <= width else t[:width] + " …"


def ci_lines(rollup):
    """A summary line, then one line per failed check with the command that reads its log."""
    if not rollup:
        return ["ci       no checks"]
    ok, bad, pending = 0, [], []
    for c in rollup["contexts"]["nodes"]:
        name = c.get("name") or c.get("context")
        state = (c.get("conclusion") or c.get("status") or c.get("state") or "").upper()
        if state in PASSED:
            ok += 1
        elif state in FAILED:
            job = re.search(r"/job/(\d+)", c.get("detailsUrl") or "")
            how = f"gh run view --job {job.group(1)} --log-failed | tail -80" if job else (c.get("detailsUrl") or c.get("targetUrl") or "")
            bad.append(f"         ✗ {'required' if c.get('isRequired') else 'optional'} {name} ({state.lower()}) → {how}")
        else:
            pending.append(name)
    head = f"ci       {ok} ok · {len(bad)} failed · {len(pending)} pending"
    head += f" ({', '.join(pending)})" if pending else ""
    return [head] + bad


def build_items(threads, reviews, comments, ours):
    """Every comment to triage as (id, who, where, body, replies), and the number of resolved threads."""
    items, resolved = [], 0
    for t in threads:
        if t["isResolved"]:
            resolved += 1
            continue
        cs = t["comments"]["nodes"]
        if not cs:
            continue
        where = f"{t['path']}:{t['line'] or t['originalLine'] or '?'}" + (" (outdated)" if t["isOutdated"] else "")
        if len(cs) > 1 and login(cs[-1]) in ours:
            where += " · answered, not resolved"
        replies = [(login(c), clean(c["body"])) for c in cs[1:]]
        items.append((f"c{cs[0]['databaseId']}", login(cs[0]), where, clean(cs[0]["body"]), replies))
    for r in reviews:
        body = clean(r["body"])
        if not body and r["state"] in ("COMMENTED", "PENDING"):
            continue  # an empty review only carries inline threads, listed above
        items.append((f"rv{r['databaseId']}", login(r), f"review {r['state']}", body or "(no text)", []))
    for c in comments:
        items.append((f"ic{c['databaseId']}", login(c), "PR comment", clean(c["body"]), []))
    return items, resolved


def copilot_line(reviews, requests, head):
    """Copilot's last review against the head commit, or None when Copilot is not involved in this PR."""
    copilot = [r for r in reviews if login(r).lower().startswith("copilot")]
    pending = any((q["requestedReviewer"] or {}).get("login", "").lower().startswith("copilot") for q in requests)
    if not copilot and not pending:
        return None
    last = (copilot[-1]["commit"] or {}).get("oid", "") if copilot else ""
    line = f"copilot  last review on {last[:10]}" if copilot else "copilot  no review yet"
    if last == head:
        return line + " = head"
    if copilot:
        line += f" (head is {head[:10]}: not reviewed since)"
    return line + (" · requested, pending" if pending else "")


def print_item(id_, mark, who, where, body, replies, ours):
    print(f"\n{id_}  {mark} · {who} · {where}")
    print(indent(body))
    for rwho, rbody in replies:
        print(f"    ↳ {rwho}: " + (one_line(rbody) if rwho in ours else "\n" + indent(rbody)))


def status(args):
    owner, name, n = target(args.pr)
    data = graphql(META, owner=owner, name=name, n=n)
    me, pr = data["viewer"]["login"], data["repository"]["pullRequest"]
    ours = {me, login(pr)}
    threads = review_threads(owner, name, n)
    reviews = every(owner, name, n, "reviews", REVIEWS)
    comments = every(owner, name, n, "comments", COMMENTS)
    items, resolved = build_items(threads, reviews, comments, ours)

    state_file, seen, shown = load_state(owner, name, n)
    head = pr["headRefOid"]
    print(f"pr       #{n} {pr['state']}{' draft' if pr['isDraft'] else ''} · {pr['title']}")
    print(f"         {pr['url']} · base {pr['baseRefName']} · head {head[:10]}")
    print(f"merge    {pr['mergeable']} / {pr['mergeStateStatus']} · review {pr['reviewDecision'] or 'none'}")
    commit = pr["commits"]["nodes"][0]["commit"] if pr["commits"]["nodes"] else {}
    checks = ci_lines(commit.get("statusCheckRollup"))
    print(checks[0])
    copilot = copilot_line(reviews, pr["reviewRequests"]["nodes"], head)
    if copilot:
        print(copilot)

    # Only an answer marks an item handled; being shown leaves it to triage.
    earlier = [i for i in items if i[0] in seen]
    items = [i for i in items if i[0] not in seen]
    handled_threads = [i for i in earlier if i[0].startswith("c")]
    print(f"comments {len(items)} to triage · {sum(i[1] in ours for i in items)} of them yours to exclude"
          f" · {len(handled_threads)} threads answered but open"
          f" · {len(earlier) - len(handled_threads)} reviews and PR comments answered"
          f" · {resolved} threads resolved")
    print("## CI")
    for line in checks[1:]:
        print(line)
    print("\n## Comments")
    for id_, who, where, body, replies in items:
        if who in ours:
            tail = f" [{len(replies)} replies, last {replies[-1][0]}]" if replies else ""
            print(f"{id_}  yours · {who} · {where} — {one_line(body, 90)}{tail}")
        else:
            print_item(id_, "shown" if id_ in shown else "NEW", who, where, body, replies, ours)
        shown.add(id_)
    for id_, who, where, body, replies in (earlier if args.full else handled_threads):
        if args.full:
            print_item(id_, "earlier", who, where, body, replies, ours)
        else:
            tail = f" [{len(replies)} replies, last {replies[-1][0]}]" if replies else ""
            print(f"{id_}  seen · {who} · {where} — {one_line(body, 90)}{tail}")

    save_state(state_file, seen, shown)


def answer(args):
    owner, name, n = target(args.pr)
    answers = json.loads(Path(args.file).read_text())
    threads = review_threads(owner, name, n)
    state_file, seen, shown = load_state(owner, name, n)
    by_comment = {}
    for t in threads:
        ids = [c["databaseId"] for c in t["comments"]["nodes"]]
        for i in ids:
            by_comment[i] = (t["id"], ids[0])  # replies go to the thread's first comment
    grouped = []
    for a in answers:
        id_, body = a["id"], a["body"].strip()
        if id_.startswith("c") and id_[1:].isdigit():
            thread, top = by_comment.get(int(id_[1:]), (None, None))
            if not thread:
                print(f"{id_}  ✗ no such thread")
                continue
            if args.dry_run:
                print(f"{id_}  would reply{' and resolve' if a.get('resolve', True) else ''}: {one_line(body)}")
                continue
            gh("api", "-X", "POST", f"repos/{owner}/{name}/pulls/{n}/comments/{top}/replies", "-f", f"body={body}")
            seen.update((id_, f"c{top}"))
            save_state(state_file, seen, shown)
            done = "replied"
            if a.get("resolve", True):
                graphql("mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}", t=thread)
                done += " · resolved"
            print(f"{id_}  {done}")
        else:
            grouped.append((id_, body))
    if grouped:
        text = "\n\n".join(b for _, b in grouped)  # each body names what it answers
        if args.dry_run:
            print(f"pr       would post one PR comment:\n{indent(text)}")
        else:
            gh("pr", "comment", str(n), "--repo", f"{owner}/{name}", "--body-file", "-", stdin=text)
            seen.update(i for i, _ in grouped)
            save_state(state_file, seen, shown)
            print(f"pr       one PR comment for {', '.join(i for i, _ in grouped)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("pr", nargs="?")
    s.add_argument("--full", action="store_true")
    a = sub.add_parser("answer")
    a.add_argument("pr")
    a.add_argument("file")
    a.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    status(args) if args.cmd == "status" else answer(args)


if __name__ == "__main__":
    main()
