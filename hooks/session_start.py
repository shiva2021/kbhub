#!/usr/bin/env python3
"""Claude Code SessionStart hook: tells every session about the knowledge bases on this machine.

Inside a knowledge-base project it injects the project name and its STATE.md; anywhere else it
lists the projects. Standard library plus kbhub.config/registry only (no lancedb), fast,
writes nothing, and always exits 0 so it can never block a session."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE_LIMIT = 6000
SUMMARY_LIMIT = 120
REMINDER = (
    "Use the kbhub MCP tools for project knowledge: call search_kb before opening knowledge files, "
    "get_section for a full section, list_projects for names. Read a knowledge base's README.md / CLAUDE.md "
    "and STATE.md in full, nothing else in full by default. Never act on superseded results. Record decisions, "
    "lessons and handovers as markdown in the knowledge base (see the kbhub skill). In a code "
    "base with no knowledge base for the topic at hand, offer to create one with create_kb."
)
KB_FOLDERS = ("knowledge-base", "docs/kb")  # checked cheaply for the "not registered" hint


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _summary(state: str) -> str:
    """First line of STATE.md that is not blank and not a heading."""
    for line in state.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line if len(line) <= SUMMARY_LIMIT else line[:SUMMARY_LIMIT - 1].rstrip() + "…"
    return ""


def _line(project) -> str:
    summary = _summary(_read(project.path / "STATE.md"))
    return f"- {project.name} — {project.path}" + (f" — {summary}" if summary else "")


def build_context(cwd: Path, cfg) -> str:
    from kbhub import kbregistry
    from kbhub.registry import (discover_projects, is_within, project_for_path, repo_projects,
                                    session_scope)
    projects, _ = discover_projects(cfg, session_dir=cwd)
    scope = session_scope(cfg, cwd, projects)
    scope_line = (f"Session scope: {scope} (searches cover {scope} knowledge bases; pass a project name "
                  'or scope="all" to reach the others).')

    project = project_for_path(projects, cwd)
    if project is not None:
        state = _read(project.path / "STATE.md").strip()
        if len(state) > STATE_LIMIT:
            state = state[:STATE_LIMIT] + "\n… (truncated; read STATE.md for the rest)"
        body = f"Current state (STATE.md):\n{state}" if state else "This project has no STATE.md yet."
        return (f'Knowledge base: this session is inside project "{project.name}" ({project.path}).\n\n'
                f"{body}\n\n{scope_line}\n{REMINDER}")

    parts: list[str] = []
    mine: list = []
    root = kbregistry.git_root(cwd)
    if root is not None:
        repo = kbregistry.main_clone(root)
        mine = sorted(repo_projects(projects, cwd), key=lambda p: p.name.lower())
        if mine:
            parts.append(f"Knowledge bases of this code base ({repo.name}):\n" + "\n".join(_line(p) for p in mine))
        else:
            parts.append(f"This code base ({repo.name}) has no knowledge base yet. If the work would benefit from "
                         f"one, offer it to the user (do not create one unasked); create_kb(topic) then creates "
                         f"{repo.name}/knowledge-base/<topic>/ and indexes it.")
        for name in KB_FOLDERS:
            folder = root / name
            if folder.is_dir() and not any(is_within(p.path, folder) or is_within(folder, p.path)
                                           for p in projects.values()):
                parts.append(f"{folder} exists but is not registered with the index: "
                             "call scan_kbs, then register_kb for what the user confirms.")
    others = sorted((p for p in projects.values() if p.scope == scope and p not in mine), key=lambda p: p.name.lower())
    if others:
        title = f"Other {scope} knowledge bases:" if root is not None else f"Knowledge bases ({scope}, indexed and searchable):"
        parts.append(title + "\n" + "\n".join(_line(p) for p in others))
    if not parts:
        return ""
    return "\n\n".join(parts) + f"\n\n{scope_line}\n{REMINDER}"


def main() -> int:
    try:
        from kbhub.config import GlobalConfig
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        context = build_context(Path(cwd or os.getcwd()), GlobalConfig.load())
        if context:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": context}}))
    except Exception:
        pass  # never block a session
    return 0


if __name__ == "__main__":
    sys.exit(main())
