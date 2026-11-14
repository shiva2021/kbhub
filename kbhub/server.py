"""MCP server exposing the knowledge-base index to Claude Code (stdio transport).

Registered once at user scope by `kbhub setup`:
    claude mcp add kbhub --scope user -- uv run --project ~/.kbhub kbhub-server
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import manage
from .format import format_hits
from .index import KBIndex, log

# The folder the Claude session works in. Claude Code starts the server there (it is registered
# with `uv run --project`, which does not change folder); it decides the default search scope and
# where create_kb puts a new knowledge base.
SESSION_DIR = Path(os.environ.get("KBHUB_SESSION_DIR") or os.getcwd())

INSTRUCTIONS = """\
Central index of every knowledge base on this machine: folders of markdown (decisions, lessons,
plans, runbooks, research, handovers) kept for code bases (outside them by default, or inside at
<repo>/knowledge-base/<topic>/) and for personal projects. Use it INSTEAD of opening knowledge files
to look for things.

Finding knowledge:
- Before opening knowledge files, call search_kb with a specific question or exact identifiers
  (ticket numbers, function names, id shapes). Each hit shows project · file:lines, doc type, status.
- Searches cover the scope of the session's folder: "work" or "personal". Pass a project name to
  reach any knowledge base (project="core" covers every core/<topic>), or scope="all" for everything.
- Read a knowledge base's README.md / CLAUDE.md and STATE.md in full; nothing else in full by default.
  Use get_section when a hit is relevant and you need the whole section.
- Never act on superseded results. They are hidden by default; include_superseded=true is for history.

Creating and updating knowledge:
- A code base with no knowledge base: when the session start says the user has not been asked yet,
  ask them once, then call kb_setup(enable=true|false). Yes finds the existing knowledge base or
  creates and indexes <knowledge-base folder>/<repo>/general, outside the repo. The answer is
  remembered; never ask again, and never offer one to a user who said no.
- Another topic for a code base that has one: create_kb(topic), one topic per functionality. It
  creates <knowledge-base folder>/<repo>/<topic>, outside the repo, so nothing is written into the
  code base. Only when the user asks for the knowledge to live in the repo, pass in_repo=true:
  that creates <repo>/knowledge-base/<topic>/ and stages or commits nothing.
- kb_setup and create_kb target the folder where the session started: if you have moved to another
  repo or into a git worktree since, pass location=<the folder you are working in>.
- Knowledge-base folders that exist but are not indexed (the session start mentions them, or the user
  asks you to look): scan_kbs lists them, register_kb adds them.
- Write knowledge as markdown files in the knowledge base (decisions, lessons, STATE.md, handovers),
  following the kbhub skill. There is no "save" tool: the index re-syncs by itself
  when files change, so new notes are searchable at once.
- Mark whole files or folders superseded or background with add_rule; a single section by putting
  "superseded" in its heading.
- Never create a .kb.toml inside a git repo.
"""

mcp = FastMCP("kbhub", instructions=INSTRUCTIONS)
_index: KBIndex | None = None


def index() -> KBIndex:
    """The shared index, synced at most every 5 seconds."""
    global _index
    if _index is None:
        _index = KBIndex(session_dir=SESSION_DIR)
    _index.maybe_sync(min_interval=5)
    return _index


def _tool(fn):
    """Register an MCP tool that keeps stdout clean (it is the MCP channel) and returns errors as
    text instead of raising."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return fn(*args, **kwargs)
        except Exception as exc:
            log(f"{fn.__name__} failed: {type(exc).__name__}: {exc}")
            return f"Error: {exc}"
    return mcp.tool()(wrapper)


def _stale_note(idx: KBIndex) -> str:
    if not idx.last_error:
        return ""
    return f"\n\n(Note: the last index sync failed, so results may be out of date: {idx.last_error})"


@_tool
def search_kb(query: str, project: str | None = None, doc_type: str | None = None,
              include_superseded: bool = False, limit: int = 6, scope: str | None = None) -> str:
    """Hybrid (semantic + keyword) search over the knowledge bases. Returns the most relevant
    sections with project, file:line references, doc type and status (current / background /
    superseded). Call this before opening knowledge files.

    Args:
        query: a natural-language question or exact identifiers, e.g. "Stream user id for a principal"
            or "PE-472 addPrincipalToUserChannels".
        project: optional project name (see list_projects), e.g. "Atlas" or "core/billing"; a repo name
            such as "core" covers all of its knowledge bases. A project is searched whatever its scope.
        doc_type: optional filter: decisions, lessons, open-items, plan, runbook, research, handover,
            state, entrypoint, notes (projects may define more).
        include_superseded: include dropped/historical material (default false).
        limit: number of sections to return (default 6, max 15).
        scope: "work", "personal" or "all". Default: the scope of the folder this session works in.
    """
    idx = index()
    res = idx.search(query, project, doc_type, include_superseded, min(max(int(limit), 1), 15), scope)
    return format_hits(res) + _stale_note(idx)


@_tool
def get_section(project: str, path: str, heading: str | None = None) -> str:
    """Return a full section (heading plus everything nested under it) straight from the markdown
    file, so it is always current. Use the project, path and the § heading from a search hit. Omit
    heading to get the whole file (avoid for large files)."""
    return index().section(project, path, heading)


def _project_line(p: dict) -> str:
    line = f"{p['name']} [{p['scope']}] | {p['path']} | {p['files']} files | ~{p['tokens']} tokens"
    if p["repo"]:
        line += f" | repo: {p['repo']}"
    if p["missing"]:
        line += " | MISSING"
    if p["warning"]:
        line += f" | warning: {p['warning']}"
    return line


@_tool
def list_projects(scope: str | None = None) -> str:
    """List the knowledge bases: name, scope, folder, file count, size, and the repo they belong to.
    scope: "work", "personal" or omit for all."""
    idx = index()
    lines = [_project_line(p) for p in idx.projects(scope)]
    return ("\n".join(lines) or "No knowledge bases found.") + _stale_note(idx)


@_tool
def list_sources(project: str | None = None, scope: str | None = None) -> str:
    """List indexed files with project, doc type, status and approximate token size. Use it to see
    what knowledge exists, or to find a project's decisions file. Same project/scope rules as search_kb."""
    idx = index()
    rows = idx.sources(project, scope)
    lines = [f"{r['project']} · {r['path']} | {r['doc_type']} | {r['status']} | ~{r['tokens']} tokens" for r in rows]
    return ("\n".join(lines) or "No sources indexed.") + _stale_note(idx)


@_tool
def reindex(project: str | None = None, rebuild: bool = False) -> str:
    """Force a re-sync of the index (normally automatic). project re-chunks one project;
    rebuild=true rebuilds everything from scratch."""
    return json.dumps(_fresh().sync(rebuild=rebuild, project=project))


def _fresh() -> KBIndex:
    global _index
    if _index is None:
        _index = KBIndex(session_dir=SESSION_DIR)
    return _index


def _indexed_summary(idx: KBIndex, name: str) -> str:
    """Sync now and describe one project, so a new knowledge base is searchable straight away."""
    try:
        idx.sync()
    except Exception as exc:
        return f"It will be indexed on the next sync (sync failed just now: {exc})."
    finally:
        idx._last_sync = 0.0  # notes written right after this must be found by the very next search
    p = next((p for p in idx.projects() if p["name"] == name), None)
    if p is None:
        return "It is registered but its folder could not be found."
    return f"Indexed: {p['files']} file{'' if p['files'] == 1 else 's'}, ~{p['tokens']} tokens."


@_tool
def scan_kbs(root: str | None = None) -> str:
    """Look on disk (inside git repos too) for knowledge-base folders and report which are not
    registered with the index yet. Changes nothing. root: folder to scan (default: the configured
    configured scan roots)."""
    idx = _fresh()
    found = manage.scan(idx.cfg, root)
    if not found:
        return "No knowledge-base folders found."
    lines = [(f"ok   {c['registered']}" if c["registered"] else f"NEW  {c['name']}")
             + f" [{c['scope']}] | {c['path']} | {c['files']} files" for c in found]
    if any(not c["registered"] for c in found):
        lines.append("\nNEW folders are not indexed. Confirm with the user, then call register_kb(path) for each "
                     "(the suggested name and scope are shown; pass name/scope to change them).")
    return "\n".join(lines)


@_tool
def register_kb(path: str, name: str | None = None, scope: str | None = None) -> str:
    """Register an existing folder of knowledge markdown with the central index. Works for folders
    inside git repos; writes nothing into the repo. name defaults to "<repo>/<folder>"; scope
    ("work" or "personal") defaults by location."""
    idx = _fresh()
    project = manage.register_kb(idx.cfg, path, name, scope)
    return (f"Registered {project.name} [{project.scope}] at {project.path}. "
            + _indexed_summary(idx, project.name))


@_tool
def create_kb(topic: str, location: str | None = None, name: str | None = None, scope: str | None = None,
              in_repo: bool = False) -> str:
    """Create a new knowledge base and index it. For a git repo it is created outside the repo, at
    <knowledge-base folder>/<repo>/<topic>/ (CLAUDE.md, STATE.md, knowledge-base/, plans/,
    handovers/), named "<repo>/<topic>"; use one topic per functionality. Nothing is written into
    the code base. Outside a repo it creates a standalone knowledge-base project in that folder.

    Args:
        topic: folder name for the knowledge base, e.g. "billing".
        location: folder it is for. Default: the folder where the session started; pass this when
            you are working somewhere else now (another repo, or a git worktree created later).
        name: project name to use instead of "<repo>/<topic>".
        scope: "work" or "personal" (default: by location).
        in_repo: true only when the user asks for the knowledge to live in the code base: creates
            <repo>/knowledge-base/<topic>/ (README.md, STATE.md, decisions.md, lessons.md,
            open-items.md, runbooks/, research/, plans/, handovers/), to be committed with the code.
    """
    idx = _fresh()
    project = manage.create_kb(idx.cfg, topic, location or SESSION_DIR, name, scope, in_repo)
    committed = ("The files are new in the working tree and not committed: commit them with the change they "
                 "describe, on a branch, following the repo's rules." if project.repo else "")
    return (f"Created {project.name} [{project.scope}] at {project.path}. {_indexed_summary(idx, project.name)} "
            f"{committed}").strip()


@_tool
def kb_setup(enable: bool, location: str | None = None, scope: str | None = None) -> str:
    """Record the user's answer to "should kbhub keep a knowledge base for this code base?" and act
    on it. Call it once, after asking the user, when the session start says they have not been
    asked yet; call it again only when they change their mind.

    enable=true: uses the knowledge base the code base already has; else registers knowledge-base
    folders found in the repo; else creates <knowledge-base folder>/<repo>/general outside the repo
    and indexes it. Nothing is written into the code base.
    enable=false: remembers the no, so no session asks or offers again.

    Args:
        enable: the user's answer.
        location: the code base it is for. Default: the folder where the session started.
        scope: "work" or "personal" for a new knowledge base (default: by the repo's location).
    """
    idx = _fresh()
    done = manage.setup_project_kb(idx.cfg, location or SESSION_DIR, enable, scope)
    repo = done["repo"].name
    if not enable:
        return (f"Recorded: no knowledge base for {repo}. No session will ask or offer again. "
                "kb_setup(enable=true) switches it on later.")
    lines = [f"- {p.name} [{p.scope}] at {p.path}" for p in done["projects"]]
    if done["created"]:
        head = f"Recorded: yes. Created a knowledge base for {repo}, outside the repo:"
        tail = (f"{_indexed_summary(idx, done['projects'][0].name)} Read its CLAUDE.md and STATE.md, fill in what "
                "this session learns, and keep STATE.md current. Another topic gets its own: create_kb(topic).")
    elif done["registered"]:
        _indexed_summary(idx, done["projects"][0].name)
        head = f"Recorded: yes. {repo} already has knowledge, now indexed:"
        tail = "Search it with search_kb. Tell the user which folders were picked up."
    else:
        head = f"Recorded: yes. {repo} already has knowledge bases:"
        tail = "Search them with search_kb; nothing was created."
    skipped = "".join(f"\nNot registered: {s}" for s in done["skipped"])
    return "\n".join([head, *lines, tail]) + skipped


@_tool
def add_rule(project: str, glob: str, status: str | None = None, doc_type: str | None = None,
             note: str | None = None) -> str:
    """Tag files of a registered knowledge base: status "superseded" (hidden from search),
    "background" or "current", and/or a doc_type. glob is relative to the knowledge-base folder,
    e.g. "research/old-approach/**". note explains why (shown with results)."""
    idx = _fresh()
    manage.add_rule(idx.cfg, project, glob, status, doc_type, note)
    idx.sync()
    idx._last_sync = 0.0
    return f"Rule added to {project}: {glob} -> " + ", ".join(x for x in (status, doc_type) if x) + ". Index updated."


def main() -> None:
    log("starting MCP server")
    mcp.run()


if __name__ == "__main__":
    main()
