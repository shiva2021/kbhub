import hashlib

import pytest

from kbhub.chunker import approx_tokens, chunk_file
from kbhub.config import GlobalConfig


@pytest.fixture
def cfg(kb_home):
    return GlobalConfig.load()


def chunks(cfg, text, rel="notes/a.md", rules=()):
    return chunk_file("P", rel, text, list(rules), cfg)


def test_split_by_headings_with_breadcrumb(cfg):
    out = chunks(cfg, "# T\n## A\nx\n### B\ny")
    assert [c["heading"] for c in out] == ["T > A", "T > A > B"]
    assert [(c["start_line"], c["end_line"]) for c in out] == [(2, 3), (4, 5)]
    a = out[0]
    assert a["project"] == "P" and a["path"] == "notes/a.md"
    assert a["text"] == "## A\nx" and a["search_text"] == "T > A\n## A\nx"
    assert a["tokens"] == approx_tokens(a["text"])
    assert a["id"] == hashlib.sha1(b"P:notes/a.md:2:3").hexdigest()[:16]
    assert set(a) == {"id", "project", "path", "heading", "start_line", "end_line", "status",
                      "status_note", "doc_type", "text", "search_text", "tokens"}
    assert (a["status"], a["doc_type"]) == ("current", "notes")


def test_text_before_first_heading_uses_file_name(cfg):
    out = chunks(cfg, "intro line\n\n# T\nbody")
    assert out[0]["heading"] == "a" and out[0]["text"] == "intro line"


def test_headings_in_code_fence_ignored(cfg):
    out = chunks(cfg, "## A\n```\n# not a heading\n```")
    assert len(out) == 1
    assert "# not a heading" in out[0]["text"]


def test_superseded_heading_inherits(cfg):
    out = chunks(cfg, "## Runbook 02 (superseded 15 Oct)\nold\n### Detail\nz\n## Live\nok")
    assert [c["status"] for c in out] == ["superseded", "superseded", "current"]
    assert out[1]["status_note"]


def test_file_rule_status(cfg):
    rules = [{"glob": "notes/**", "status": "background", "note": "explainer"}]
    out = chunks(cfg, "## A\nx", rules=rules)
    assert out[0]["status"] == "background" and out[0]["status_note"] == "explainer"


def test_doc_type_from_defaults(cfg):
    assert chunks(cfg, "## A\nx", rel="knowledge-base/decisions.md")[0]["doc_type"] == "decisions"


def test_large_table_split_keeps_header(cfg):
    header = "| id | name | notes |\n| --- | --- | --- |"
    rows = "\n".join(f"| {i} | name number {i} | some notes about row {i} |" for i in range(200))
    out = chunks(cfg, f"## Big table\n\n{header}\n{rows}\n")
    assert len(out) > 1
    assert all(c["tokens"] <= 450 + 60 for c in out)
    assert all(c["text"].startswith("| id | name | notes |") for c in out[1:])
    assert "| id | name | notes |" in out[0]["text"]
    assert all(f"| {i} | name number {i} |" in "".join(c["text"] for c in out) for i in (0, 99, 199))
    assert len({c["id"] for c in out}) == len(out)


def test_empty_and_heading_only_sections_dropped(cfg):
    assert chunks(cfg, "") == []
    assert chunks(cfg, "\n\n  \n") == []
    out = chunks(cfg, "# Title\n\n## Empty\n\n## Real\ncontent")
    assert [c["heading"] for c in out] == ["Title > Real"]
