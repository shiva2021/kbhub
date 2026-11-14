#!/usr/bin/env python3
"""Claude Code SessionStart hook: tells every session about the knowledge bases on this machine.

Inside a knowledge-base project it injects the project name and its STATE.md; anywhere else it
lists the projects. In a code base with no knowledge base it tells the session to ask the user,
once, whether they want one (the answer is kept by kbhub.prefs). Standard library plus
kbhub.config/registry/prefs only (no lancedb), fast, writes nothing, and always exits 0 so it can
never block a session."""
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
    "lessons and handovers as markdown in the knowledge base (see the kbhub skill)."
)
KB_FOLDERS = ("knowledge-base", "knowledge_base", "docs/kb")  # checked cheaply for the "not registered" hint


def _no_kb_note(repo: Path, choice: str | None) -> str:
    """What to tell a session in a code base that has no knowledge base."""
    name = repo.name
    if choice == "off":
        return (f"The user chose not to keep a knowledge base for this code base ({name}). Do not offer or create "
                "one unless they ask; if they do, kb_setup(enable=true) switches it on.")
    if choice == "on":
        return (f"The user wants a knowledge base for this code base ({name}) but none is indexed. Call "
                "kb_setup(enable=true) now: it finds the existing one or creates and indexes a new one outside the repo.")
    return (
        f"ACTION NEEDED (kbhub): this code base ({name}) has no knowledge base and the user has not been asked yet. "
        f"Before anything else in your first reply, ask the user once: \"Should kbhub keep a knowledge base for "
        f"{name}?\" with the options Yes and No (use the AskUserQuestion tool if you have it). Then call "
        "kb_setup(enable=true) for Yes or kb_setup(enable=false) for No, and carry on with what they asked for. "
        "Yes finds an existing knowledge base or creates and indexes a new one outside the repo; nothing is written "
        "into the code base. Either answer is remembered, so they are never asked again for this code base. Do not "
        "skip this because the task looks small. If you cannot ask the user (a non-interactive run), or they do "
        "not answer, skip it and do not call kb_setup.")


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
    from kbhub import kbregistry, prefs
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
        mine = sorted(repo_projects(projects, cwd, cfg), key=lambda p: p.name.lower())
        if mine:
            parts.append(f"Knowledge bases of this code base ({repo.name}):\n" + "\n".join(_line(p) for p in mine)
                         + "\nA new topic gets its own: create_kb(topic) creates it outside the repo.")
        else:
            parts.append(_no_kb_note(repo, prefs.get_choice(repo)))
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
