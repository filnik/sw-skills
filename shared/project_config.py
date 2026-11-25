#!/usr/bin/env python3
"""The project's .claude/config/project.json, found from a folder upward. Stdlib only.

    python3 project_config.py [<key>]   print the config, or one top-level block, as JSON ({} when there is none);
                                        "playbook" is printed as an absolute path, and only when the file exists

Every ancestor is checked, not only the first one with a .claude folder: a repo can have its own .claude/ while the
config sits in a folder above it. All the configs found are merged, the nearest winning key by key. Scripts import this file through their own resolved path, which works through a
symlinked skill folder and from the plugin cache:

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))
    from project_config import project_config
"""
import json
import sys
from pathlib import Path

CONFIG = Path(".claude") / "config" / "project.json"


def configs(start=None):
    """Every .claude/config/project.json at or above start, nearest first, as (path, content)."""
    here = Path(start or Path.cwd()).resolve()
    found = []
    for d in [here, *here.parents]:
        f = d / CONFIG
        if f.is_file():
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError) as e:
                sys.exit(f"{f}: {e}")
            found.append((f, data if isinstance(data, dict) else {}))
    return found


def find(start=None):
    """(the nearest config file, all configs above start merged), or (None, {}). A nearer file wins key by key, so a
    repo's own config adds to the one of the folder above it instead of hiding it."""
    found = configs(start)
    merged = {}
    for _, data in reversed(found):
        merged.update(data)
    return (found[0][0] if found else None), merged


def project_config(start=None):
    """The parsed config, or {}."""
    return find(start)[1]


def project_root(path):
    """The folder holding .claude/config/project.json."""
    return Path(path).parents[2]


def playbook(start=None):
    """The playbook file the nearest config naming one points to (relative to that project root, or absolute), when
    it exists."""
    path, name = next(((f, c["playbook"]) for f, c in configs(start) if c.get("playbook")), (None, None))
    if not path:
        return None
    p = Path(name).expanduser()
    p = p if p.is_absolute() else project_root(path) / p
    return p if p.is_file() else None


if __name__ == "__main__":
    cfg = dict(project_config())
    book = playbook()
    cfg.pop("playbook", None)
    if book:
        cfg["playbook"] = str(book)
    key = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps(cfg.get(key, {}) if key else cfg, indent=1, ensure_ascii=False))
