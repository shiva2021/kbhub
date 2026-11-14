"""The per-code-base choice: does the user want a knowledge base for this repo? Standard library
only (the SessionStart hook loads this module).

Kept outside every repo and outside the engine checkout, in `~/.kbhub-config/projects.json`
(`$KBHUB_CONFIG_HOME` moves it):

    {"version": 1, "projects": {
      "/…/core": {"name": "core", "kb": "on", "decided": "2026-11-14", "kbs": ["/…/kb/core/general"]},
      "/…/scratch": {"name": "scratch", "kb": "off", "decided": "2026-11-14"}
    }}

The key is the main clone of the repo, so every worktree shares one answer. `kbs` lists the
knowledge bases created or linked for that repo.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import date
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")
VERSION = 1
ON, OFF = "on", "off"


def prefs_home() -> Path:
    return Path(os.environ.get("KBHUB_CONFIG_HOME") or Path.home() / ".kbhub-config")


def prefs_path() -> Path:
    return prefs_home() / "projects.json"


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise ValueError('expected {"version": 1, "projects": {...}}')
    return projects


def _same(a: str, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _find(projects: dict, repo: Path) -> str | None:
    """The key that stands for `repo`, however it is spelled (`~/desktop` vs `~/Desktop`)."""
    key = os.path.abspath(repo)
    if key in projects:
        return key
    return next((k for k in projects if _same(k, repo)), None)


def entry(repo: Path) -> dict:
    """What is recorded for a repo (its main clone); {} when nothing is, or the file is unreadable."""
    path = prefs_path()
    try:
        projects = _read(path) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    found = projects.get(_find(projects, Path(repo)) or "")
    return found if isinstance(found, dict) else {}


def get_choice(repo: Path) -> str | None:
    """"on", "off", or None when the user has not been asked yet."""
    choice = entry(repo).get("kb")
    return choice if choice in (ON, OFF) else None


def linked(repo: Path) -> list[Path]:
    """Knowledge-base folders recorded for a repo."""
    kbs = entry(repo).get("kbs")
    return [Path(k) for k in kbs if isinstance(k, str)] if isinstance(kbs, list) else []


def _update(repo: Path, fn: Callable[[dict], T]) -> T:
    """Change one repo's record under a lock. Refuses to touch a file it cannot parse."""
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name("projects.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                projects = _read(path) if path.is_file() else {}
            except (OSError, ValueError) as exc:
                raise ValueError(f"{path} is unreadable ({exc}); fix or delete it first") from None
            key = _find(projects, Path(repo)) or os.path.abspath(repo)
            record = projects.get(key)
            if not isinstance(record, dict):
                record = projects[key] = {"name": Path(repo).name}
            result = fn(record)
            tmp = path.with_name("projects.json.tmp")
            tmp.write_text(json.dumps({"version": VERSION, "projects": projects}, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            tmp.replace(path)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def set_choice(repo: Path, on: bool) -> None:
    def change(record: dict) -> None:
        record["kb"] = ON if on else OFF
        record["decided"] = date.today().isoformat()

    _update(repo, change)


def link_kb(repo: Path, folder: Path) -> None:
    """Record that the knowledge base at `folder` belongs to `repo`."""
    def change(record: dict) -> None:
        kbs = record.get("kbs") if isinstance(record.get("kbs"), list) else []
        if not any(_same(k, folder) for k in kbs if isinstance(k, str)):
            kbs.append(os.path.abspath(folder))
        record["kbs"] = kbs

    _update(repo, change)
