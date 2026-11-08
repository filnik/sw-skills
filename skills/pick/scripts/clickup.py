#!/usr/bin/env python3
"""ClickUp for the pick and ticket skills: read, list, take and create tasks. Stdlib only.

    clickup.py get <id|url> [--download DIR]    the task, its comments and attachments (downloaded to DIR)
    clickup.py list [--tag T] [--status S] [--mine] [--list ID] [--closed] [--search WORDS] [--limit N]
                                                the open tasks of the project's list, one line each; --closed
                                                adds the closed ones, --search keeps the tasks whose name has
                                                any of the words (most matches first), --limit (default 30)
    clickup.py start <id|url> [--status S]      assign it to you and move it to an in-progress status
    clickup.py create --title T --body-file F [--tag T]... [--status S] [--list ID]

Config, found from the current directory upward, in .claude/config/project.json:
    {"clickup": {"list_id": "901234567", "workspace_id": "9012345", "in_progress_status": "in progress"}}
list_id is the list tasks are read from and created in; workspace_id is needed only for custom task ids (DEV-123);
in_progress_status is optional (default: the list's first status that reads like "in progress").
Token: the CLICKUP_TOKEN environment variable, else the file at clickup.token_path
(default .claude/secrets/clickup-token, which must be gitignored).
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.clickup.com/api/v2"
TOKEN_HOSTS = ("clickup.com", "clickup-attachments.com")  # the only hosts the token is ever sent to
IN_PROGRESS = re.compile(r"progress|doing|develop|working|started", re.I)


def project_config():
    """(project root, clickup config) from the nearest .claude/config/project.json upward."""
    here = Path.cwd().resolve()
    for d in [here, *here.parents]:
        f = d / ".claude" / "config" / "project.json"
        if f.is_file():
            return d, json.loads(f.read_text()).get("clickup", {})
    return here, {}


def read_token(root, cfg):
    tok = os.environ.get("CLICKUP_TOKEN", "").strip()
    if tok:
        return tok
    path = root / cfg.get("token_path", ".claude/secrets/clickup-token")
    if path.is_file():
        return path.read_text().strip()
    sys.exit(f"no ClickUp token: set CLICKUP_TOKEN or write it to {path}")


def request(tok, method, path, payload=None, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"ClickUp {method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"ClickUp {method} {path}: {e.reason}")


def parse_ref(ref, workspace_id=None):
    """(task id, extra query params) from a task URL, a task id, or a custom id such as DEV-123."""
    m = re.search(r"app\.clickup\.com/t/(?:(\d+)/)?([\w-]+)", ref)
    team, task = (m.group(1), m.group(2)) if m else (None, ref.strip().lstrip("#"))
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-\d+", task):
        team = team or workspace_id
        if not team:
            raise ValueError(f"{task} is a custom task id: set clickup.workspace_id in .claude/config/project.json")
        return task, {"custom_task_ids": "true", "team_id": team}
    return task, {}


def in_progress_status(statuses):
    """The list's first open status whose name reads like 'in progress', or None."""
    for s in sorted(statuses, key=lambda s: int(s.get("orderindex") or 0)):
        if s.get("type") not in ("closed", "done") and IN_PROGRESS.search(s.get("status", "")):
            return s["status"]
    return None


def names(people):
    return ", ".join(p.get("username") or p.get("email") or "?" for p in people) or "nobody"


def task_line(t):
    tags = "".join(f" #{x['name']}" for x in t.get("tags", []))
    return f"{t['id']} · {t['status']['status']} · {t.get('name', '')}{tags} · {names(t.get('assignees', []))}"


def format_task(t, comments, files=None):
    files = files or {}
    lst = t.get("list") or {}
    custom = f" ({t['custom_id']})" if t.get("custom_id") else ""
    tags = ", ".join(x["name"] for x in t.get("tags", [])) or "none"
    lines = [
        f"task      {t['id']}{custom} · {t.get('name', '')}",
        f"status    {t['status']['status']} · priority {(t.get('priority') or {}).get('priority') or 'none'} · tags {tags}",
        f"assigned  {names(t.get('assignees', []))}",
        f"list      {lst.get('name', '?')} ({lst.get('id', '?')})",
        f"url       {t.get('url', '')}",
        "",
        "## Description",
        (t.get("markdown_description") or t.get("text_content") or t.get("description") or "(empty)").strip(),
    ]
    attachments = t.get("attachments", [])
    if attachments:
        lines += ["", f"## Attachments ({len(attachments)})"]
        for a in attachments:
            kind = a.get("mimetype") or a.get("extension") or "?"
            lines.append(f"- {a.get('title', 'attachment')} ({kind}) → {files.get(a.get('id'), a.get('url', ''))}")
    lines += ["", f"## Comments ({len(comments)})"]
    for c in sorted(comments, key=lambda c: int(c.get("date") or 0)):
        when = datetime.fromtimestamp(int(c.get("date") or 0) / 1000, timezone.utc).strftime("%Y-%m-%d")
        lines.append(f"- {(c.get('user') or {}).get('username', '?')}, {when}: {(c.get('comment_text') or '').strip()}")
    return "\n".join(lines)


def token_host(url):
    """True when url points at ClickUp itself, so the token may go with it."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in TOKEN_HOSTS)


class DropAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    """urllib copies every header onto a redirect, whatever the new host: the token must not follow it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        moved = urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname
        if new is not None and moved:
            new.remove_header("Authorization")
        return new


def search(tasks, words):
    """The tasks whose name has any of the words (case-insensitive), the ones matching most words first."""
    words = [w.lower() for w in words.split() if w]
    scored = [(sum(w in t.get("name", "").lower() for w in words), i, t) for i, t in enumerate(tasks)]
    return [t for n, i, t in sorted(scored, key=lambda x: (-x[0], x[1])) if n]


def download(tok, attachments, outdir):
    """Save each attachment in outdir; {attachment id: local path, or the failure and the URL}.
    The token goes only to ClickUp's own hosts, and is dropped on a redirect to another host."""
    outdir.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(DropAuthOnHostChange)
    saved = {}
    for i, a in enumerate(attachments, 1):
        if not a.get("url"):
            continue
        safe = re.sub(r"[^\w.-]", "_", a.get("title") or "attachment")
        path = outdir / f"{i:02d}-{safe}"
        try:
            req = urllib.request.Request(a["url"], headers={"Authorization": tok} if token_host(a["url"]) else {})
            with opener.open(req, timeout=60) as r:
                path.write_bytes(r.read())
            saved[a.get("id")] = str(path)
        except (urllib.error.URLError, OSError) as e:
            saved[a.get("id")] = f"DOWNLOAD FAILED ({e}): {a['url']}"
    return saved


def me(tok):
    return request(tok, "GET", "/user")["user"]["id"]


def list_id_of(cfg, given):
    lid = given or cfg.get("list_id") or cfg.get("team_board_id")
    if not lid:
        sys.exit("no list: set clickup.list_id in .claude/config/project.json, or pass --list")
    return lid


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("ref")
    g.add_argument("--download", metavar="DIR")
    ls = sub.add_parser("list")
    ls.add_argument("--tag")
    ls.add_argument("--status")
    ls.add_argument("--mine", action="store_true")
    ls.add_argument("--list")
    ls.add_argument("--closed", action="store_true")
    ls.add_argument("--search", metavar="WORDS")
    ls.add_argument("--limit", type=int, default=30)
    st = sub.add_parser("start")
    st.add_argument("ref")
    st.add_argument("--status")
    cr = sub.add_parser("create")
    cr.add_argument("--title", required=True)
    cr.add_argument("--body-file", required=True)
    cr.add_argument("--tag", action="append", default=[])
    cr.add_argument("--status")
    cr.add_argument("--list")
    args = ap.parse_args()

    root, cfg = project_config()
    tok = read_token(root, cfg)
    try:
        ref = parse_ref(args.ref, cfg.get("workspace_id")) if getattr(args, "ref", None) else None
    except ValueError as e:
        sys.exit(str(e))

    if args.cmd == "get":
        t = request(tok, "GET", f"/task/{ref[0]}", params={**ref[1], "include_markdown_description": "true"})
        comments = request(tok, "GET", f"/task/{t['id']}/comment").get("comments", [])
        files = download(tok, t.get("attachments", []), Path(args.download)) if args.download else {}
        print(format_task(t, comments, files))

    elif args.cmd == "list":
        lid = list_id_of(cfg, args.list)
        params = {"include_closed": "true" if args.closed else "false", "subtasks": "true"}
        if args.tag:
            params["tags[]"] = [args.tag]
        if args.status:
            params["statuses[]"] = [args.status]
        if args.mine:
            params["assignees[]"] = [me(tok)]
        tasks, page = [], 0
        while True:
            data = request(tok, "GET", f"/list/{lid}/task", params={**params, "page": page})
            tasks += data.get("tasks", [])
            if data.get("last_page", True) or not data.get("tasks"):
                break
            page += 1
        if args.search:
            tasks = search(tasks, args.search)
        kind = "tasks" if args.closed else "open tasks"
        what = f' matching "{args.search}"' if args.search else ""
        shown = f" (the first {args.limit})" if len(tasks) > args.limit else ""
        print(f"{len(tasks)} {kind}{what} in list {lid}{shown}")
        for t in tasks[:args.limit]:
            print(task_line(t))

    elif args.cmd == "start":
        t = request(tok, "GET", f"/task/{ref[0]}", params=ref[1])
        status = args.status or cfg.get("in_progress_status") or \
            in_progress_status(request(tok, "GET", f"/list/{t['list']['id']}").get("statuses", []))
        payload = {"assignees": {"add": [me(tok)]}}
        if status and status.lower() != t["status"]["status"].lower():
            payload["status"] = status
        request(tok, "PUT", f"/task/{t['id']}", payload)
        print(f"{t['id']} assigned to you · status {payload.get('status', t['status']['status'])}"
              + ("" if status else " (no in-progress status found: pass --status to move it)"))

    else:
        payload = {"name": args.title, "markdown_description": Path(args.body_file).read_text(),
                   "custom_item_id": 0}  # the default task type: custom types hit a limit on the Free plan
        if args.tag:
            payload["tags"] = args.tag
        if args.status:
            payload["status"] = args.status
        t = request(tok, "POST", f"/list/{list_id_of(cfg, args.list)}/task", payload)
        print(f"created {t['id']} · {t.get('name', '')}\n{t.get('url', '')}")


if __name__ == "__main__":
    main()
