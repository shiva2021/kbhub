"""Plain-text rendering of search results for agents (spec §8)."""
from __future__ import annotations

KEYWORD_ONLY_NOTICE = "(keyword-only search: Ollama unavailable)\n\n"


def format_hits(result: dict) -> str:
    if not result["hits"]:
        out = "No matches."
    else:
        parts = []
        for i, h in enumerate(result["hits"], 1):
            flag = h["status"] + (f" ({h['status_note']})" if h["status"] != "current" and h["status_note"] else "")
            parts.append(f"[{i}] {h['project']} · {h['path']}:{h['start_line']}-{h['end_line']} | {h['doc_type']} | {flag}\n"
                         f"    § {h['heading']}\n{h['text']}")
        out = "\n\n---\n\n".join(parts)
    if result.get("superseded_matches"):
        seen, lines = set(), []
        for s in result["superseded_matches"]:
            key = (s["project"], s["path"], s["heading"])
            if key not in seen:
                seen.add(key)
                lines.append(f"- {s['project']} · {s['path']} § {s['heading']}")
        out += ("\n\n(Also matched in SUPERSEDED material, hidden by default — do not act on it; "
                "pass include_superseded=true only if you need history:\n" + "\n".join(lines) + ")")
    if result.get("keyword_only"):
        out = KEYWORD_ONLY_NOTICE + out
    return out
