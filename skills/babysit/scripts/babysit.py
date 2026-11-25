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
Config, the "babysit" block of .claude/config/project.json (found from the current folder upward), all optional:
    {"babysit": {"ignore_checks": ["PR Safeness"], "skip_comments": ["<!-- preview-builds-hint -->"],
                 "copilot": {"request": true, "auto_review_bases": ["main"]}}}
ignore_checks: checks whose name contains one of these (any case) are only counted, never failed or pending;
skip_comments: PR comments whose body contains one of these markers are only counted, as boilerplate;
copilot.request: say "request it" when Copilot has not reviewed the head and no request is pending, except on the
auto_review_bases, whose ruleset reviews every push.
Needs gh, logged in. Stdlib only.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))
import project_config  # noqa: E402

STATE = Path.home() / ".cache" / "dev-babysit"
COMMAND_TIMEOUT = 60
PAGE = "pageInfo{hasNextPage endCursor}"
REVIEWS = "databaseId author{login} state body commit{oid}"
COMMENTS = "databaseId author{login} body"
THREADS = f"id isResolved isOutdated path line originalLine comments(first:100){{{PAGE} nodes{{{COMMENTS}}}}}"
META = """query($owner:String!,$name:String!,$n:Int!){viewer{login} repository(owner:$owner,name:$name){pullRequest(number:$n){
  title url state isDraft author{login} baseRefName headRefName headRefOid mergeable mergeStateStatus reviewDecision
  assignees(first:5){nodes{login}}
  reviewRequests(first:20){nodes{requestedReviewer{... on Bot{login} ... on User{login}}}}
  commits(last:1){nodes{commit{statusCheckRollup{state contexts(first:100){nodes{__typename
    ... on CheckRun{name status conclusion detailsUrl isRequired(pullRequestNumber:$n)}
    ... on StatusContext{context state targetUrl isRequired(pullRequestNumber:$n)}}}}}}}}}}"""
OURS = """query($owner:String!,$name:String!,$n:Int!){viewer{login}
  repository(owner:$owner,name:$name){pullRequest(number:$n){author{login}}}}"""
RESOLVE = "mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}"
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
    if "shown" not in ledger:  # a legacy ledger's seen ids only prove that status printed them
        return state_file, set(), set(ledger.get("seen", []))
    return state_file, set(ledger.get("seen", [])), set(ledger["shown"])


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


def boilerplate(body, markers):
    """None to keep the comment, '' to drop it, or the one line worth keeping."""
    return "" if any(m in (body or "") for m in markers) else None


def without_boilerplate(comments, markers):
    """The PR comments to triage, and the authors of the boilerplate ones dropped."""
    kept, dropped = [], []
    for c in comments:
        short = boilerplate(c["body"], markers)
        if short == "":
            dropped.append(login(c))
        else:
            kept.append(c if short is None else {**c, "body": short})
    return kept, dropped


def indent(text, width=None):
    text = text if width is None or len(text) <= width else text[:width] + " …"
    return "\n".join("    " + line for line in text.splitlines())


def one_line(text, width=110):
    t = " ".join(text.split())
    return t if len(t) <= width else t[:width] + " …"


def ci_lines(rollup, ignore=()):
    """A summary line, then one line per failed check with the command that reads its log. Checks whose name
    contains one of `ignore` are only counted."""
    if not rollup:
        return ["ci       no checks"]
    ok, bad, pending, ignored = 0, [], [], 0
    for c in rollup["contexts"]["nodes"]:
        name = c.get("name") or c.get("context")
        state = (c.get("conclusion") or c.get("status") or c.get("state") or "").upper()
        if any(i.lower() in name.lower() for i in ignore):
            ignored += 1
        elif state in PASSED:
            ok += 1
        elif state in FAILED:
            job = re.search(r"/runs/(\d+)/job/(\d+)", c.get("detailsUrl") or "")
            how = (f"gh run view --job {job.group(2)} --log-failed | tail -80 · run {job.group(1)}" if job
                   else (c.get("detailsUrl") or c.get("targetUrl") or ""))
            bad.append(f"         ✗ {'required' if c.get('isRequired') else 'optional'} {name} ({state.lower()}) → {how}")
        else:
            pending.append(name)
    head = f"ci       {ok} ok · {len(bad)} failed · {len(pending)} pending"
    head += f" ({', '.join(pending)})" if pending else ""
    head += f" · {ignored} ignored by config" if ignored else ""
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


def handled(item, seen, ours):
    """Answered by an `answer` run and, for a thread, nobody has written in it since: a reply after ours reopens it."""
    id_, who, _, _, replies = item
    if id_ not in seen:
        return False
    return not id_.startswith("c") or (replies[-1][0] if replies else who) in ours


def already_answered(id_, thread_comments, seen, ours):
    """An `answer` run posted this reply before (the thread's last word is ours): a rerun must not post it twice."""
    if not id_.startswith("c"):
        return id_ in seen
    return id_ in seen and bool(thread_comments) and login(thread_comments[-1]) in ours


def copilot_line(reviews, requests, head, base=None, config=None):
    """Copilot's last review against the head commit, or None when Copilot is not involved in this PR.
    config, the babysit.copilot block: with "request", an unreviewed head off the auto_review_bases says
    "request it"."""
    config = config or {}
    copilot = [r for r in reviews if login(r).lower().startswith("copilot")]
    pending = any((q["requestedReviewer"] or {}).get("login", "").lower().startswith("copilot") for q in requests)
    if not copilot and not pending and not config.get("request"):
        return None
    last = (copilot[-1]["commit"] or {}).get("oid", "") if copilot else ""
    line = f"copilot  last review on {last[:10]}" if copilot else "copilot  no review yet"
    if last == head:
        return line + " = head"
    if copilot:
        line += f" (head is {head[:10]}: not reviewed since)"
    if pending:
        return line + " · requested, pending"
    if config.get("request") and base not in config.get("auto_review_bases", []):
        return line + " · request it"
    return line


def print_item(id_, mark, who, where, body, replies, ours):
    print(f"\n{id_}  {mark} · {who} · {where}")
    print(indent(body))
    for rwho, rbody in replies:
        print(f"    ↳ {rwho}: " + (one_line(rbody) if rwho in ours else "\n" + indent(rbody)))


def config_line(path, cfg, book):
    """Where the config came from, the locale replies are written in, and the playbook to follow."""
    if not path:
        return None
    line = f"config   {path} · locale {cfg.get('locale') or 'en'}"
    return line + (f" · playbook {book}" if book else "")


def status(args):
    path, cfg = project_config.find()
    settings = cfg.get("babysit", {})
    owner, name, n = target(args.pr)
    data = graphql(META, owner=owner, name=name, n=n)
    me, pr = data["viewer"]["login"], data["repository"]["pullRequest"]
    ours = {me, login(pr)}
    threads = review_threads(owner, name, n)
    reviews = every(owner, name, n, "reviews", REVIEWS)
    comments, dropped = without_boilerplate(every(owner, name, n, "comments", COMMENTS),
                                            settings.get("skip_comments", []))
    items, resolved = build_items(threads, reviews, comments, ours)

    state_file, seen, shown = load_state(owner, name, n)
    head = pr["headRefOid"]
    print(f"pr       #{n} {pr['state']}{' draft' if pr['isDraft'] else ''} · {pr['title']}")
    print(f"         {pr['url']} · base {pr['baseRefName']} · head {pr['headRefName']} {head[:10]}")
    print(f"merge    {pr['mergeable']} / {pr['mergeStateStatus']} · review {pr['reviewDecision'] or 'none'}"
          f" · assignees {', '.join(a['login'] for a in pr['assignees']['nodes']) or 'NONE'}")
    commit = pr["commits"]["nodes"][0]["commit"] if pr["commits"]["nodes"] else {}
    checks = ci_lines(commit.get("statusCheckRollup"), settings.get("ignore_checks", []))
    print(checks[0])
    copilot = copilot_line(reviews, pr["reviewRequests"]["nodes"], head, pr["baseRefName"], settings.get("copilot"))
    if copilot:
        print(copilot)
    where = config_line(path, cfg, project_config.playbook())
    if where:
        print(where)

    # Only an answer marks an item handled; being shown leaves it to triage.
    earlier = [i for i in items if handled(i, seen, ours)]
    items = [i for i in items if not handled(i, seen, ours)]
    handled_threads = [i for i in earlier if i[0].startswith("c")]
    print(f"comments {len(items)} to triage · {sum(i[1] in ours for i in items)} of them yours to exclude"
          f" · {len(handled_threads)} threads answered but open"
          f" · {len(earlier) - len(handled_threads)} reviews and PR comments answered"
          f" · {resolved} threads resolved"
          + (f" · {len(dropped)} boilerplate skipped ({', '.join(sorted(set(dropped)))})" if dropped else ""))
    print("## CI")
    for line in checks[1:]:
        print(line)
    print("\n## Comments")
    for id_, who, where, body, replies in items:
        if who in ours:
            tail = f" [{len(replies)} replies, last {replies[-1][0]}]" if replies else ""
            print(f"{id_}  yours · {who} · {where} — {one_line(body, 90)}{tail}")
        else:
            mark = "NEW REPLY" if id_ in seen else "shown" if id_ in shown else "NEW"
            print_item(id_, mark, who, where, body, replies, ours)
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
    who = graphql(OURS, owner=owner, name=name, n=n)
    ours = {who["viewer"]["login"], login(who["repository"]["pullRequest"])}
    threads = review_threads(owner, name, n)
    state_file, seen, shown = load_state(owner, name, n)
    by_comment = {}
    for t in threads:
        ids = [c["databaseId"] for c in t["comments"]["nodes"]]
        for i in ids:
            by_comment[i] = (t, ids[0])  # replies go to the thread's first comment
    grouped = []
    for a in answers:
        id_, body = a["id"], a["body"].strip()
        if id_.startswith("c") and id_[1:].isdigit():
            thread, top = by_comment.get(int(id_[1:]), (None, None))
            if not thread:
                print(f"{id_}  ✗ no such thread")
                continue
            resolve = a.get("resolve", True) and not thread["isResolved"]
            if already_answered(f"c{top}", thread["comments"]["nodes"], seen, ours):
                if resolve and not args.dry_run:
                    graphql(RESOLVE, t=thread["id"])
                print(f"{id_}  already answered, skipped{' · resolved now' if resolve else ''}")
                continue
            if args.dry_run:
                print(f"{id_}  would reply{' and resolve' if resolve else ''}: {one_line(body)}")
                continue
            gh("api", "-X", "POST", f"repos/{owner}/{name}/pulls/{n}/comments/{top}/replies", "-f", f"body={body}")
            seen.update((id_, f"c{top}"))
            save_state(state_file, seen, shown)
            done = "replied"
            if resolve:
                graphql(RESOLVE, t=thread["id"])
                done += " · resolved"
            print(f"{id_}  {done}")
        elif already_answered(id_, [], seen, ours):
            print(f"{id_}  already answered, skipped")
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
