"""The central registry of knowledge bases that `.kb.toml` discovery cannot see: anything inside a
git repo (no marker file is ever written into a repo). Standard library only, imports only
`config`: the SessionStart hook loads this module.

`$KBHUB_HOME/registry.json`:

    {"version": 1, "kbs": [
      {"name": "core/billing", "repo": "/…/core", "rel": "knowledge-base/billing", "scope": "work"},
      {"name": "notes", "path": "/…/notes", "scope": "personal", "rules": […], "eval": […]}
    ]}

A `repo` + `rel` entry names the main clone and a folder relative to the repo root; it resolves to
whichever checkout (main clone or linked worktree) has that folder, newest edits first.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path, PurePosixPath
from typing import Callable, TypeVar

from .config import SCOPES, GlobalConfig, ProjectConfig

T = TypeVar("T")
VERSION = 1


def registry_path(cfg: GlobalConfig) -> Path:
    return cfg.home / "registry.json"


# ------------------------------------------------------------------ entries

def entry_problem(entry) -> str | None:
    """Why an entry is invalid, or None when it is fine."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return "entry needs a non-empty name"
    if entry.get("scope") not in SCOPES:
        return f"{name!r}: scope must be one of {', '.join(SCOPES)}"
    has_path = "path" in entry
    has_repo = "repo" in entry or "rel" in entry
    if has_path == has_repo:
        return f"{name!r}: needs either path, or repo and rel"
    if has_path:
        if not isinstance(entry["path"], str) or not os.path.isabs(entry["path"]):
            return f"{name!r}: path must be an absolute path"
    else:
        repo, rel = entry.get("repo"), entry.get("rel")
        if not isinstance(repo, str) or not os.path.isabs(repo):
            return f"{name!r}: repo must be an absolute path"
        if not isinstance(rel, str) or not rel or os.path.isabs(rel) or ".." in PurePosixPath(rel).parts:
            return f"{name!r}: rel must be a path inside the repo"
    return ProjectConfig.from_registry(entry).error


def _read(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    kbs = data.get("kbs") if isinstance(data, dict) else None
    if not isinstance(kbs, list):
        raise ValueError('expected {"version": 1, "kbs": [...]}')
    return kbs


def load_entries(cfg: GlobalConfig) -> tuple[list[dict], list[str]]:
    """Valid entries plus warnings. Never raises: a broken registry must not break search."""
    path = registry_path(cfg)
    if not path.is_file():
        return [], []
    try:
        raw = _read(path)
    except (OSError, ValueError) as exc:
        return [], [f"{path} is unreadable, registered knowledge bases are ignored: {exc}"]
    entries, warnings = [], []
    for entry in raw:
        problem = entry_problem(entry)
        if problem:
            warnings.append(f"{path}: skipped an entry: {problem}")
        else:
            entries.append(entry)
    return entries, warnings


def update_registry(cfg: GlobalConfig, fn: Callable[[list[dict]], T]) -> T:
    """Change the registry under a lock: `fn` mutates the list of entries in place and its return
    value is passed back. Refuses to touch a registry it cannot parse, or to save invalid entries."""
    path = registry_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name("registry.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                entries = _read(path) if path.is_file() else []
            except (OSError, ValueError) as exc:
                raise ValueError(f"{path} is unreadable ({exc}); fix or delete it first") from None
            result = fn(entries)
            for entry in entries:
                problem = entry_problem(entry)
                if problem:
                    raise ValueError(f"invalid registry entry: {problem}")
            tmp = path.with_name("registry.json.tmp")
            tmp.write_text(json.dumps({"version": VERSION, "kbs": entries}, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            tmp.replace(path)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# ------------------------------------------------------------------ git layout (no git binary needed)

def _is(test) -> bool:
    """A path test that treats "not allowed to look" as "not there"."""
    try:
        return bool(test())
    except OSError:
        return False


def git_root(path: Path) -> Path | None:
    """Root of the checkout containing `path`: the nearest folder with a `.git` (file or folder)."""
    path = Path(os.path.abspath(path))
    return next((p for p in (path, *path.parents) if _is((p / ".git").exists)), None)


def is_linked_worktree(checkout_root: Path) -> bool:
    return _is((Path(checkout_root) / ".git").is_file)


def main_clone(checkout_root: Path) -> Path:
    """The main clone a checkout belongs to (itself, unless it is a linked worktree)."""
    checkout_root = Path(checkout_root)
    dot_git = checkout_root / ".git"
    if not dot_git.is_file():
        return checkout_root
    try:
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return checkout_root
    if not text.startswith("gitdir:"):
        return checkout_root
    admin = Path(text[len("gitdir:"):].strip())
    if not admin.is_absolute():
        admin = (checkout_root / admin).resolve()
    # <main>/.git/worktrees/<name>
    if admin.parent.name == "worktrees" and admin.parent.parent.name == ".git":
        return admin.parent.parent.parent
    return checkout_root  # a submodule or something else: treat it as its own repo


def checkouts(main: Path) -> list[Path]:
    """The main clone followed by its linked worktrees that still exist."""
    main = Path(main)
    found = [main]
    try:
        records = sorted((main / ".git" / "worktrees").iterdir())
    except OSError:
        return found
    for record in records:
        try:
            dot_git = Path((record / "gitdir").read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            continue
        if dot_git.name == ".git" and dot_git.parent.is_dir() and dot_git.exists():
            found.append(dot_git.parent)
    return found


def _newest_markdown(folder: Path) -> float:
    newest = 0.0
    for here, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        for name in names:
            if name.lower().endswith(".md"):
                try:
                    newest = max(newest, os.stat(os.path.join(here, name)).st_mtime)
                except OSError:
                    pass
    return newest


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def resolve_entry(entry: dict, prefer: Path | None = None) -> Path | None:
    """The folder an entry stands for right now, or None when it is not on disk anywhere.
    For a repo entry with copies in several checkouts: the checkout containing `prefer` (the
    folder a session works in) wins when it has the folder, so a session always reads and writes
    its own branch; otherwise the copy whose markdown was edited most recently."""
    if "path" in entry:
        folder = Path(entry["path"])
        return folder if folder.is_dir() else None
    copies = [c for c in checkouts(Path(entry["repo"])) if (c / entry["rel"]).is_dir()]
    if not copies:
        return None
    if len(copies) > 1 and prefer is not None:
        here = git_root(prefer)
        mine = next((c for c in copies if here is not None and _same_folder(c, here)), None)
        if mine is not None:
            return mine / entry["rel"]
    best = copies[0] if len(copies) == 1 else max(copies, key=lambda c: _newest_markdown(c / entry["rel"]))
    return best / entry["rel"]  # ties keep the first, i.e. the main clone
