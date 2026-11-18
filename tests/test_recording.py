from datetime import date
from pathlib import Path

import pytest

from kbhub.config import GlobalConfig
from kbhub.manage import create_kb
from kbhub.recording import record
from kbhub.registry import discover_projects

TODAY = date(2026, 11, 18)
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(kb_home):
    return GlobalConfig.load()


@pytest.fixture
def kb(cfg, scan_root):
    """A standalone knowledge base made from the real templates (placeholder heading included)."""
    return create_kb(cfg, "atlas", location=scan_root)


def decision(cfg, kb, title="Use Postgres", body="Store ledgers in Postgres.", why="Mongo lost writes.", **kw):
    return record(cfg, kb, "decision", title, body, why=why, today=TODAY, **kw)


def test_first_decision_skips_the_template_placeholder(cfg, kb):
    done = decision(cfg, kb, decided_by="Sam")
    path = kb.path / "knowledge-base" / "decisions.md"
    text = path.read_text()
    assert done["id"] == "D-001" and done["path"] == "knowledge-base/decisions.md"
    assert text.endswith(
        "\n\n## D-001: Use Postgres\n"
        "- **Date:** 2026-11-18 · **Status:** accepted · **Decided by:** Sam\n"
        "- **Decision:** Store ledgers in Postgres.\n"
        "- **Why:** Mongo lost writes.\n")
    assert "## D-001: <short title>" in text            # the template's format block is left alone
    lines = text.splitlines()
    start, end = (int(n) for n in done["lines"].split("-"))
    assert lines[start - 1] == "## D-001: Use Postgres" and end == len(lines)


def test_decisions_number_on_and_state_follows(cfg, kb):
    decision(cfg, kb)
    second = decision(cfg, kb, title="Second")
    assert second["id"] == "D-002" and second["state"] == "updated"
    state = (kb.path / "STATE.md").read_text()
    assert "## Active decisions\nD-001, D-002. See `knowledge-base/decisions.md`.\n" in state
    assert "## Blockers\nNone." in state                # the rest of STATE.md is untouched


def test_decision_without_decided_by_omits_it(cfg, kb):
    decision(cfg, kb)
    assert "- **Date:** 2026-11-18 · **Status:** accepted\n" in (kb.path / "knowledge-base/decisions.md").read_text()


def test_supersede_marks_the_old_heading_and_drops_it_from_state(cfg, kb):
    decision(cfg, kb)
    decision(cfg, kb, title="Second")
    done = decision(cfg, kb, title="Use SQLite", supersedes="d-1")
    text = (kb.path / "knowledge-base/decisions.md").read_text()
    assert done["id"] == "D-003" and done["superseded"] == "D-001"
    assert "## D-001: Use Postgres (superseded 2026-11-18, see D-003)\n" in text
    assert text.endswith("- **Why:** Mongo lost writes.\n- **Supersedes:** D-001\n")
    assert "D-002, D-003. See" in (kb.path / "STATE.md").read_text()
    with pytest.raises(ValueError, match="already superseded"):
        decision(cfg, kb, title="Again", supersedes="D-001")
    with pytest.raises(ValueError, match="D-002, D-003"):
        decision(cfg, kb, title="Again", supersedes="D-009")
    assert "D-004" not in (kb.path / "knowledge-base/decisions.md").read_text()   # a refusal writes nothing


def test_numbering_ignores_code_fences_and_counts_superseded(cfg, kb):
    path = kb.path / "knowledge-base" / "decisions.md"
    path.write_text("# Decisions\n\n## D-004: Old (superseded 2026-01-01, see D-005)\nx\n\n"
                    "```\n## D-090: in a fence\n```\n\n## D-005: New\ny")
    assert decision(cfg, kb)["id"] == "D-006"
    assert "y\n\n## D-006: Use Postgres\n" in path.read_text()


def test_state_left_alone_when_its_shape_is_not_known(cfg, kb):
    state = kb.path / "STATE.md"
    custom = "# STATE\n\n## Active decisions\n- D-001 (pending review)\n- see the plan\n"
    state.write_text(custom)
    done = decision(cfg, kb)
    assert state.read_text() == custom and done["state"].startswith("not changed")
    state.unlink()
    assert decision(cfg, kb, title="Second")["state"].startswith("not changed")


def test_in_repo_layout_and_missing_file(cfg, scan_root, make_repo):
    repo = make_repo(scan_root / "projects" / "core")
    kb = create_kb(cfg, "billing", location=repo, in_repo=True)
    assert decision(cfg, kb)["path"] == "decisions.md"
    assert "D-001. See" in (kb.path / "STATE.md").read_text()
    (kb.path / "lessons.md").unlink()
    done = record(cfg, kb, "lesson", "Ledger locks", "Lock the ledger row first.", today=TODAY)
    assert done["path"] == "lessons.md"
    assert (kb.path / "lessons.md").read_text() == "# Lessons\n\n## 2026-11-18: Ledger locks\nLock the ledger row first.\n"
    assert sorted(p.name for p in kb.path.iterdir() if p.is_file()) == [
        "README.md", "STATE.md", "decisions.md", "lessons.md", "open-items.md"]   # no lock or temp files


def test_lesson_appends_with_the_date(cfg, kb):
    done = record(cfg, kb, "lesson", "Retries hide bugs", "Fail loudly.\n\n### Detail\nMore.", today=TODAY)
    text = (kb.path / "knowledge-base" / "lessons.md").read_text()
    assert done["path"] == "knowledge-base/lessons.md" and done["id"] is None
    assert text.endswith("\n\n## 2026-11-18: Retries hide bugs\nFail loudly.\n\n### Detail\nMore.\n")


def test_handover_writes_a_new_file_and_never_overwrites(cfg, kb):
    done = record(cfg, kb, "handover", "Importer: half done!", "## Done\nParsing.\n\n## Next\nWrite rows.", today=TODAY)
    assert done["path"] == "handovers/2026-11-18-importer-half-done.md"
    assert (kb.path / done["path"]).read_text() == (
        "# Handover: Importer: half done!\n\n- **Date:** 2026-11-18\n\n## Done\nParsing.\n\n## Next\nWrite rows.\n")
    with pytest.raises(ValueError, match="already exists"):
        record(cfg, kb, "handover", "Importer half done", "other", today=TODAY)


@pytest.mark.parametrize("kind", ["decision", "lesson", "handover"])
@pytest.mark.parametrize("title", ["Dropped the cache", "Not needed any more", "", "two\nlines", "Deprecated API"])
def test_titles_that_would_be_hidden_or_broken_are_refused(cfg, kb, kind, title):
    with pytest.raises(ValueError, match="title"):
        record(cfg, kb, kind, title, "body", why="because", today=TODAY)


def test_bad_input_is_refused_and_writes_nothing(cfg, kb):
    before = {p: p.read_text() for p in kb.path.rglob("*.md")}
    with pytest.raises(ValueError, match="kind"):
        record(cfg, kb, "note", "T", "b", today=TODAY)
    with pytest.raises(ValueError, match="why"):
        record(cfg, kb, "decision", "T", "b", today=TODAY)
    with pytest.raises(ValueError, match="body"):
        record(cfg, kb, "lesson", "T", "  ", today=TODAY)
    with pytest.raises(ValueError, match="heading"):
        record(cfg, kb, "lesson", "T", "fine\n## Another section\nx", today=TODAY)
    with pytest.raises(ValueError, match="heading"):
        record(cfg, kb, "decision", "T", "fine", why="# Big\nx", today=TODAY)
    with pytest.raises(ValueError, match="supersedes"):
        record(cfg, kb, "lesson", "T", "b", supersedes="D-001", today=TODAY)
    with pytest.raises(ValueError, match="supersedes"):
        decision(cfg, kb, supersedes="the old one")
    assert {p: p.read_text() for p in kb.path.rglob("*.md")} == before


def test_multiline_decision_stays_inside_its_bullet(cfg, kb):
    decision(cfg, kb, body="Line one.\nLine two.\n```\n## not a heading\n```")
    text = (kb.path / "knowledge-base/decisions.md").read_text()
    assert "- **Decision:** Line one.\n  Line two.\n  ```\n  ## not a heading\n  ```\n- **Why:**" in text
    assert decision(cfg, kb, title="Next")["id"] == "D-002"


def test_recorded_entries_are_indexed_as_current(cfg, kb):
    from kbhub.index import KBIndex
    decision(cfg, kb, title="Wombat ledger")
    decision(cfg, kb, title="Quokka ledger", supersedes="D-001")
    idx = KBIndex(cfg)
    idx.sync()
    hits = idx.search("ledger", "atlas", None, True, 15, None)["hits"]
    status = {h["heading"].split(" > ")[-1].split(":")[0]: h["status"] for h in hits if "D-00" in h["heading"] and "<short title>" not in h["heading"]}
    assert status["D-002"] == "current" and status["D-001"] == "superseded"
    assert discover_projects(cfg)[0]["atlas"].path == kb.path


def test_lock_file_is_ignored_by_git():
    assert "record.lock" in (REPO / ".gitignore").read_text().split()


def test_a_title_in_angle_brackets_still_counts(cfg, kb):
    assert decision(cfg, kb, title="<clientId>-<swId> channel ids")["id"] == "D-001"
    assert decision(cfg, kb, title="Next")["id"] == "D-002"
    with pytest.raises(ValueError, match="placeholder"):
        decision(cfg, kb, title="<short title>")


def test_text_that_would_hide_later_entries_is_refused(cfg, kb):
    with pytest.raises(ValueError, match="code fence"):
        decision(cfg, kb, body="Run this:\n```\nmake")
    with pytest.raises(ValueError, match="code fence"):
        decision(cfg, kb, why="because\n~~~\nx")
    with pytest.raises(ValueError, match="hide that section"):
        record(cfg, kb, "handover", "Importer", "## Dropped approaches\nPolling.", today=TODAY)
    with pytest.raises(ValueError, match="hide that section"):
        record(cfg, kb, "lesson", "Importer", "Fine.\n\n### Not needed\nPolling.", today=TODAY)
    assert not list((kb.path / "handovers").iterdir())
    assert decision(cfg, kb)["id"] == "D-001"


def test_new_knowledge_bases_and_sessions_are_told_about_record(cfg, kb, scan_root, make_repo):
    assert "`record` tool" in (kb.path / "CLAUDE.md").read_text()
    repo_kb = create_kb(cfg, "billing", location=make_repo(scan_root / "projects" / "core"), in_repo=True)
    assert "`record` tool" in (repo_kb.path / "README.md").read_text()
    assert "record tool" in (REPO / "hooks" / "session_start.py").read_text()
