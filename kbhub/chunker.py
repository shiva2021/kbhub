"""Split markdown into heading-scoped chunks and tag each with status and doc type.
Ported from the Atlas prototype (context-index/ctxindex.py)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .config import GlobalConfig, file_meta

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Section:
    path: list[str]          # heading breadcrumb
    level: int
    superseded: bool
    start: int               # 1-based line numbers
    end: int
    lines: list[str] = field(default_factory=list)


def parse_sections(text: str, superseded_re: re.Pattern, title_fallback: str) -> list[Section]:
    lines = text.splitlines()
    stack: list[tuple[int, str, bool]] = []
    sections: list[Section] = []
    cur = Section([title_fallback], 0, False, 1, 1)
    in_fence = False
    for i, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            cur.end = i - 1
            sections.append(cur)
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            sup = bool(superseded_re.search(title)) or any(s[2] for s in stack)
            stack.append((level, title, sup))
            cur = Section([s[1] for s in stack], level, sup, i, i, [line])
        else:
            cur.lines.append(line)
    cur.end = len(lines)
    sections.append(cur)
    return [s for s in sections if "\n".join(s.lines).strip() and not _heading_only(s)]


def _heading_only(s: Section) -> bool:
    body = [l for l in s.lines if l.strip()]
    return len(body) == 1 and HEADING_RE.match(body[0]) is not None


def split_section(s: Section, max_tokens: int) -> list[tuple[int, int, str]]:
    """Return (start_line, end_line, text) windows; splits on blank lines outside code fences."""
    text = "\n".join(s.lines).strip()
    if approx_tokens(text) <= max_tokens:
        return [(s.start, s.end, text)]
    blocks, buf, buf_start, in_fence = [], [], s.start, False
    for offset, line in enumerate(s.lines):
        ln = s.start + offset
        if FENCE_RE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence and buf:
            blocks.append((buf_start, ln - 1, "\n".join(buf)))
            buf = []
            continue
        if not buf:
            buf_start = ln
        buf.append(line)
    if buf:
        blocks.append((buf_start, s.start + len(s.lines) - 1, "\n".join(buf)))
    blocks = [piece for b in blocks for piece in _split_block(*b, max_tokens)]
    windows, cur, cur_start, cur_end = [], [], None, None
    for b_start, b_end, b_text in blocks:
        if cur and approx_tokens("\n\n".join(cur) + b_text) > max_tokens:
            windows.append((cur_start, cur_end, "\n\n".join(cur)))
            cur = []
        if not cur:
            cur_start = b_start
        cur.append(b_text)
        cur_end = b_end
    if cur:
        windows.append((cur_start, cur_end, "\n\n".join(cur)))
    return windows


def _split_block(start: int, end: int, text: str, max_tokens: int) -> list[tuple[int, int, str]]:
    """Split one oversized block (long table or list) by lines; tables keep their header row."""
    if approx_tokens(text) <= max_tokens:
        return [(start, end, text)]
    lines = text.split("\n")
    header: list[str] = []
    body_from = 0
    if len(lines) > 2 and lines[0].lstrip().startswith("|") and TABLE_RULE_RE.match(lines[1]):
        header, body_from = lines[:2], 2
    out, cur, cur_start = [], [], start + body_from
    for off in range(body_from, len(lines)):
        line = lines[off]
        if cur and approx_tokens("\n".join(header + cur + [line])) > max_tokens:
            out.append((cur_start, start + off - 1, "\n".join(header + cur)))
            cur, cur_start = [], start + off
        cur.append(line)
    if cur:
        out.append((cur_start, end, "\n".join(header + cur)))
    return out


def chunk_file(project: str, rel: str, text: str, rules: list[dict], cfg: GlobalConfig) -> list[dict]:
    """Chunks (without vectors) for one file of one project."""
    meta = file_meta(rules, rel)
    out = []
    for s in parse_sections(text, cfg.superseded_re, PurePosixPath(rel).stem):
        status = "superseded" if s.superseded else meta["status"]
        note = meta["status_note"] or ("Heading marks this section as superseded" if s.superseded else "")
        crumb = " > ".join(s.path)
        for start, end, body in split_section(s, cfg.max_chunk_tokens):
            out.append({
                "id": hashlib.sha1(f"{project}:{rel}:{start}:{end}".encode()).hexdigest()[:16],
                "project": project,
                "path": rel,
                "heading": crumb,
                "start_line": start,
                "end_line": end,
                "status": status,
                "status_note": note,
                "doc_type": meta["doc_type"],
                "text": body,
                "search_text": f"{crumb}\n{body}",
                "tokens": approx_tokens(body),
            })
    return out
