"""Append one decision, lesson or handover to a knowledge base, in the standard format.

The markdown files stay the source of truth and can still be edited by hand; this only saves the
caller reading a whole file to add an entry, and keeps numbering, dates and superseding
consistent. Everything is checked before the first write, and the lock lives in the engine home,
so nothing but the knowledge files themselves is ever written into a knowledge-base folder."""
from __future__ import annotations

import fcntl
import re
from datetime import date
from pathlib import Path

from .chunker import FENCE_RE, HEADING_RE
from .config import GlobalConfig
from .registry import Project

KINDS = ("decision", "lesson", "handover")
KB_DIR = "knowledge-base"
DECISION_RE = re.compile(r"^D-(\d+)\b(.*)$", re.I)
PLACEHOLDER = "<short title>"       # the format example in the decisions template
ID_RE = re.compile(r"^D-0*(\d+)$", re.I)
STATE_HEADING_RE = re.compile(r"^active decisions$", re.I)
STATE_NONE_RE = re.compile(r"^None yet\.( See .*)$")
STATE_IDS_RE = re.compile(r"^(D-\d+(?:, D-\d+)*)\.( See .*)$")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) of every heading outside code fences."""
    found, in_fence = [], False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            found.append((i, len(m.group(1)), m.group(2).strip()))
    return found


def _check_title(cfg: GlobalConfig, title: str) -> str:
    title = (title or "").strip()
    if not title or "\n" in title or "\r" in title:
        raise ValueError("title must be one non-empty line")
    hidden = cfg.superseded_re.search(title)
    if hidden:
        raise ValueError(f"title contains {hidden.group(0)!r}, which would hide the entry from search as "
                         "superseded; reword the title")
    if title == PLACEHOLDER:
        raise ValueError(f"title {PLACEHOLDER!r} is the template's placeholder; give the entry a real title")
    return title


def _check_text(cfg: GlobalConfig, name: str, text: str | None, top_headings: bool = False) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    lines = text.splitlines()
    if sum(1 for line in lines if FENCE_RE.match(line)) % 2:
        raise ValueError(f"{name} has a code fence that is never closed (it would swallow every later entry)")
    heads = _headings(lines)
    if not top_headings and any(level <= 2 for _, level, _ in heads):
        raise ValueError(f"{name} must not contain a level 1 or 2 heading (it would start a new entry); "
                         "use ### or lower")
    hidden = next((m for m in (cfg.superseded_re.search(title) for _, _, title in heads) if m), None)
    if hidden:
        raise ValueError(f"a heading in {name} contains {hidden.group(0)!r}, which would hide that section from "
                         "search as superseded; reword the heading")
    return text


def _file(project: Project, name: str, title: str) -> tuple[Path, str, str]:
    """(path, relative path, current text) of a knowledge file, wherever this layout keeps it.
    A missing file starts from just its title."""
    for rel in (f"{KB_DIR}/{name}", name):
        if (project.path / rel).is_file():
            return project.path / rel, rel, (project.path / rel).read_text(encoding="utf-8")
    rel = f"{KB_DIR}/{name}" if (project.path / KB_DIR).is_dir() else name
    return project.path / rel, rel, f"# {title}\n"


def _append(text: str, entry: str) -> tuple[str, str]:
    """`text` with `entry` added after one blank line, and the entry's line range."""
    head = text.rstrip("\n")
    new = (head + "\n\n" if head else "") + entry + "\n"
    start = len(head.splitlines()) + 2 if head else 1
    return new, f"{start}-{start + len(entry.splitlines()) - 1}"


def _bullet(label: str, text: str) -> str:
    first, *rest = text.splitlines()
    return "\n".join([f"- **{label}:** {first}", *(f"  {line}" if line.strip() else "" for line in rest)])


def _decisions(lines: list[str], cfg: GlobalConfig) -> dict[int, tuple[int, bool]]:
    """Recorded decisions as {number: (line index, superseded)}. The template's format example
    (`## D-001: <short title>`) is not a decision."""
    found = {}
    for i, _, title in _headings(lines):
        m = DECISION_RE.match(title)
        if m and m.group(2).lstrip(": ").strip() != PLACEHOLDER:
            found[int(m.group(1))] = (i, bool(cfg.superseded_re.search(title)))
    return found


def _state_update(project: Project, new_id: str, gone: str | None) -> tuple[Path, str] | str:
    """The new text of STATE.md with its "Active decisions" list brought up to date, or the reason
    it is left alone. Only the two shapes kbhub itself writes are edited."""
    path = project.path / "STATE.md"
    if not path.is_file():
        return "not changed: there is no STATE.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    heads = _headings(lines)
    at = next((n for n, (_, _, title) in enumerate(heads) if STATE_HEADING_RE.match(title)), None)
    if at is None:
        return 'not changed: STATE.md has no "Active decisions" section; add the id by hand'
    start = heads[at][0] + 1
    end = heads[at + 1][0] if at + 1 < len(heads) else len(lines)
    body = [i for i in range(start, end) if lines[i].strip()]
    none = STATE_NONE_RE.match(lines[body[0]]) if len(body) == 1 else None
    ids = STATE_IDS_RE.match(lines[body[0]]) if len(body) == 1 else None
    if not none and not ids:
        return 'not changed: the "Active decisions" section of STATE.md is hand-written; update it by hand'
    keep = [d for d in (ids.group(1).split(", ") if ids else []) if d not in (gone, new_id)]
    lines[body[0]] = ", ".join([*keep, new_id]) + "." + (none.group(1) if none else ids.group(2))
    return path, "\n".join(lines) + "\n"


def record(cfg: GlobalConfig, project: Project, kind: str, title: str, body: str, why: str | None = None,
           decided_by: str | None = None, supersedes: str | None = None, today: date | None = None) -> dict:
    """Add one entry to `project` and return what was written: `path` (relative to the knowledge
    base), `lines`, `id` (decisions only), `superseded` and `state` (what happened to STATE.md)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    title = _check_title(cfg, title)
    body = _check_text(cfg, "body", body, top_headings=kind == "handover")
    if supersedes and kind != "decision":
        raise ValueError("supersedes only applies to a decision")
    day = (today or date.today()).isoformat()
    cfg.home.mkdir(parents=True, exist_ok=True)
    with open(cfg.home / "record.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if kind == "decision":
                return _decision(cfg, project, title, body, why, decided_by, supersedes, day)
            if kind == "lesson":
                return _lesson(project, title, body, day)
            return _handover(project, title, body, day)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _decision(cfg: GlobalConfig, project: Project, title: str, body: str, why: str | None,
              decided_by: str | None, supersedes: str | None, day: str) -> dict:
    why = _check_text(cfg, "why (a decision needs its reason)", why)
    decided_by = " ".join((decided_by or "").split())
    path, rel, text = _file(project, "decisions.md", "Decisions")
    lines = text.splitlines()
    known = _decisions(lines, cfg)
    new_id = f"D-{max(known, default=0) + 1:03d}"
    old_id = None
    if supersedes:
        m = ID_RE.match(supersedes.strip())
        if not m:
            raise ValueError(f"supersedes must be a decision id such as D-002, not {supersedes!r}")
        number = int(m.group(1))
        active = ", ".join(f"D-{n:03d}" for n, (_, dead) in sorted(known.items()) if not dead) or "(none)"
        if number not in known:
            raise ValueError(f"there is no D-{number:03d} in {rel}. Decisions in force: {active}")
        at, dead = known[number]
        if dead:
            raise ValueError(f"D-{number:03d} is already superseded. Decisions in force: {active}")
        old_id = f"D-{number:03d}"
        lines[at] = f"{lines[at].rstrip()} (superseded {day}, see {new_id})"
    entry = "\n".join(filter(None, [
        f"## {new_id}: {title}",
        f"- **Date:** {day} · **Status:** accepted" + (f" · **Decided by:** {decided_by}" if decided_by else ""),
        _bullet("Decision", body),
        _bullet("Why", why),
        f"- **Supersedes:** {old_id}" if old_id else "",
    ]))
    new_text, span = _append("\n".join(lines), entry)
    state = _state_update(project, new_id, old_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")      # the old heading's mark and the new entry: one write
    if not isinstance(state, str):
        try:
            state[0].write_text(state[1], encoding="utf-8")
            state = "updated"
        except OSError as exc:
            state = f"not changed: {exc}"
    return {"path": rel, "lines": span, "id": new_id, "superseded": old_id, "state": state}


def _lesson(project: Project, title: str, body: str, day: str) -> dict:
    path, rel, text = _file(project, "lessons.md", "Lessons")
    new_text, span = _append(text, f"## {day}: {title}\n{body}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return {"path": rel, "lines": span, "id": None, "superseded": None, "state": None}


def _handover(project: Project, title: str, body: str, day: str) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")
    if not slug:
        raise ValueError("title needs at least one letter or digit to name the handover file")
    rel = f"handovers/{day}-{slug}.md"
    path = project.path / rel
    if path.exists():
        raise ValueError(f"{rel} already exists; use another title, or edit that file")
    text = f"# Handover: {title}\n\n- **Date:** {day}\n\n{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"path": rel, "lines": f"1-{len(text.splitlines())}", "id": None, "superseded": None, "state": None}
