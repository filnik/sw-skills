#!/usr/bin/env python3
"""The pull requests and commits between two refs, grouped, ready to write release notes from.

    release_prs.py [FROM] [TO]     default TO: HEAD; default FROM: the latest tag before TO

Reads the first-parent history of FROM..TO, finds the PR of each commit ("Merge pull request #N", "Title (#N)"),
fetches every PR in batched GraphQL calls (title, author, labels, the start of the body) and groups them:
breaking, feature, fix, other, internal. Commits with no PR are listed apart. Without gh it lists the commits only.
Read-only. Stdlib only.
"""
import json
import re
import subprocess
import sys

BATCH = 40
EXCERPT = 400
PR_IN_SUBJECT = re.compile(r"^Merge pull request #(\d+)|\(#(\d+)\)\s*$")
CONVENTIONAL = re.compile(r"^(\w+)(\([^)]*\))?(!)?:")
INTERNAL = {"chore", "ci", "build", "test", "tests", "refactor", "style", "docs", "deps"}
BOTS = ("dependabot", "renovate")
ORDER = ("breaking", "feature", "fix", "other", "internal")


def run(*cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {p.stderr.strip()}")
    return p.stdout.strip()


def previous_tag(to):
    head = run("git", "rev-parse", f"{to}^{{commit}}")
    for tag in run("git", "tag", "--merged", to, "--sort=-creatordate").splitlines():
        if run("git", "rev-parse", f"{tag}^{{commit}}") != head:
            return tag
    sys.exit(f"no tag before {to}: pass FROM explicitly")


def pr_number(subject):
    m = PR_IN_SUBJECT.search(subject)
    return int(m.group(1) or m.group(2)) if m else None


def kind(title, labels, body, author):
    """breaking, feature, fix, internal or other, from the labels, a Conventional Commits title and the author."""
    labels = [x.lower() for x in labels]
    m = CONVENTIONAL.match(title.strip().lower())
    if any("breaking" in x for x in labels) or (m and m.group(3)) or "BREAKING CHANGE" in (body or ""):
        return "breaking"
    if any(x in ("bug", "fix", "bugfix") or x.startswith("type: bug") for x in labels) or (m and m.group(1) == "fix"):
        return "fix"
    if any(x in ("feature", "enhancement") or x.startswith("type: feat") for x in labels) or \
            (m and m.group(1) in ("feat", "feature", "perf")):
        return "feature"
    if (author or "").lower().startswith(BOTS) or any(x in INTERNAL or x == "dependencies" for x in labels) or \
            (m and m.group(1) in INTERNAL):
        return "internal"
    return "other"


def excerpt(body):
    """The start of a PR body without HTML comments, headings, images, empty checkboxes and blank lines."""
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln and not re.match(r"^(#{1,6}\s|!\[|<img|- \[ \])", ln)]
    text = " ".join(lines)
    return text if len(text) <= EXCERPT else text[:EXCERPT].rsplit(" ", 1)[0] + " …"


def fetch_prs(numbers):
    repo = json.loads(run("gh", "repo", "view", "--json", "owner,name"))
    found = {}
    for i in range(0, len(numbers), BATCH):
        fields = " ".join(f"p{n}: pullRequest(number:{n}){{number title url author{{login}} "
                          f"labels(first:10){{nodes{{name}}}} body}}" for n in numbers[i:i + BATCH])
        query = f'query{{repository(owner:"{repo["owner"]["login"]}",name:"{repo["name"]}"){{{fields}}}}}'
        data = json.loads(run("gh", "api", "graphql", "-f", f"query={query}"))["data"]["repository"]
        found.update({pr["number"]: pr for pr in data.values() if pr})
    return found


def main():
    args = sys.argv[1:]
    if len(args) > 2 or any(a.startswith("-") for a in args):
        sys.exit(__doc__.strip())
    to = args[1] if len(args) == 2 else "HEAD"
    frm = args[0] if args else previous_tag(to)
    log = run("git", "log", "--first-parent", "--format=%h%x1f%s%x1f%an", f"{frm}..{to}")
    commits = [line.split("\x1f") for line in log.splitlines() if line]
    numbers = sorted({n for _, s, _ in commits for n in [pr_number(s)] if n})
    try:
        prs = fetch_prs(numbers) if numbers else {}
    except RuntimeError as e:
        prs = {}
        print(f"(PR details unavailable: {e})")

    print(f"{frm}..{to} · {len(commits)} commits · {len(prs)} PRs")
    groups = {k: [] for k in ORDER}
    for n in numbers:
        pr = prs.get(n)
        if pr:
            labels = [x["name"] for x in pr["labels"]["nodes"]]
            author = (pr.get("author") or {}).get("login", "ghost")
            groups[kind(pr["title"], labels, pr["body"], author)].append((pr, labels, author))
    for k in ORDER:
        if groups[k]:
            print(f"\n## {k} ({len(groups[k])})")
        for pr, labels, author in groups[k]:
            tags = f" · [{', '.join(labels)}]" if labels else ""
            print(f"#{pr['number']} · {pr['title']} · @{author}{tags} · {pr['url']}")
            if k != "internal" and excerpt(pr["body"]):
                print(f"    {excerpt(pr['body'])}")
    direct = [(h, s, a) for h, s, a in commits if not pr_number(s) or pr_number(s) not in prs]
    if direct:
        print(f"\n## commits without a PR ({len(direct)})")
        for h, s, a in direct:
            print(f"{h} · {s} · {a}")


if __name__ == "__main__":
    main()
