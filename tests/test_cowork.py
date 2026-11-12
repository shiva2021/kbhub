"""Cowork reads the index through a mounted engine home: nothing may be synced or written, and the
knowledge-base folders themselves are usually not there."""
import re
import zipfile
from pathlib import Path

import pytest

from kbhub.cli import main

REPO = Path(__file__).resolve().parent.parent
COWORK_SKILL = REPO / "skill" / "kbhub-cowork" / "SKILL.md"
MAIN_SKILL = REPO / "skill" / "kbhub" / "SKILL.md"

DECISIONS = ("# Decisions\n\n## D-001: Channel ids\nChannels use <clientId>-<swId>.\n### Rationale\nLegacy shape kept.\n\n"
             "## D-002: Old importer (superseded 2026-01-01, see D-003)\nUse the emu importer.\n\n"
             "## D-003: Importer\nUse the wombat importer.\n")


def snapshot(folder):
    return {str(p): p.stat().st_mtime_ns for p in sorted(folder.rglob("*"))}


@pytest.fixture
def mounted(kb_home, scan_root, make_project, tmp_path, capsys):
    """An index built on the Mac, read where the project folders do not exist (like the Cowork VM)."""
    make_project(scan_root, "alpha-folder", {"STATE.md": "# STATE\n\n## Status\nBuilding.\n",
                                             "knowledge-base/decisions.md": DECISIONS}, kb_toml='name = "Alpha"\n')
    make_project(scan_root / "personal", "Diary", {"notes.md": "## Secret\nprivate quokka entry\n"})
    assert main(["index"]) == 0
    capsys.readouterr()
    scan_root.rename(tmp_path / "moved-away")
    return kb_home


def test_projects_no_sync_lists_indexed_projects_in_scope(mounted, capsys):
    before = snapshot(mounted)
    assert main(["projects", "--no-sync"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^Alpha  \[work\]  2 files  ~\d+ tokens  .*alpha-folder$", out, re.M), out
    assert "Diary" not in out                       # personal stays out unless asked for
    assert main(["projects", "--no-sync", "--scope", "all"]) == 0
    out = capsys.readouterr().out
    assert "Alpha  [work]" in out and "Diary  [personal]" in out
    assert snapshot(mounted) == before


def test_sources_no_sync(mounted, capsys):
    before = snapshot(mounted)
    assert main(["sources", "--no-sync", "--project", "Alpha"]) == 0
    out = capsys.readouterr().out
    assert "Alpha · knowledge-base/decisions.md" in out and "Alpha · STATE.md" in out and "Diary" not in out
    assert "current/superseded" in out
    assert snapshot(mounted) == before


def test_section_no_sync_reads_the_indexed_text(mounted, capsys):
    before = snapshot(mounted)
    assert main(["section", "--no-sync", "Alpha", "knowledge-base/decisions.md", "D-001: Channel ids"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("(from the index, as of the Mac's last sync)\n")
    assert "knowledge-base/decisions.md:3-4 | current" in out and "Channels use <clientId>-<swId>." in out
    assert "Legacy shape kept." in out              # nested sub-sections come along
    assert "wombat" not in out and "emu" not in out
    assert snapshot(mounted) == before


def test_section_no_sync_marks_superseded_and_handles_misses(mounted, capsys):
    assert main(["section", "--no-sync", "alpha", "knowledge-base/decisions.md"]) == 0   # whole file
    out = capsys.readouterr().out
    assert "wombat importer" in out and "emu importer" in out
    assert re.search(r"decisions\.md:\d+-\d+ \| SUPERSEDED", out) and "do not act on it" in out
    assert main(["section", "--no-sync", "Alpha", "knowledge-base/decisions.md", "importer"]) == 0
    out = capsys.readouterr().out                   # substring fallback finds both importer sections
    assert "wombat importer" in out and "emu importer" in out and "Channels use" not in out
    assert main(["section", "--no-sync", "Alpha", "knowledge-base/decisions.md", "no such heading"]) == 0
    out = capsys.readouterr().out
    assert "No heading containing 'no such heading'" in out and "Decisions > D-003: Importer" in out
    assert main(["section", "--no-sync", "Alpha", "nope.md"]) == 0
    assert "Not indexed: nope.md in project Alpha" in capsys.readouterr().out
    assert main(["section", "--no-sync", "Nope", "x.md"]) == 1
    assert "Unknown project" in capsys.readouterr().err


def test_no_sync_listing_without_an_index_reports_cleanly(kb_home, capsys):
    for cmd in (["projects", "--no-sync"], ["sources", "--no-sync"], ["section", "--no-sync", "A", "x.md"]):
        assert main(cmd) == 1, cmd
        assert "No index found" in capsys.readouterr().err
    assert not (kb_home / "db").exists()


# ---- the skill itself

def _front_and_body(path: Path) -> tuple[dict, str]:
    m = re.match(r"---\n(.*?)\n---\n(.*)", path.read_text(encoding="utf-8"), re.S)
    assert m, f"{path} needs YAML frontmatter"
    return dict(line.split(": ", 1) for line in m.group(1).splitlines()), m.group(2)


def _command_prefix(body: str) -> str:
    line = next(l for l in body.splitlines() if "uv run" in l and "search --no-sync --keyword-only" in l)
    return line.strip().split(" search ")[0]


def test_cowork_skill_valid():
    front, body = _front_and_body(COWORK_SKILL)
    assert front["name"] == "kbhub-cowork"
    assert "Cowork" in front["description"] and len(front["description"]) <= 1024
    for needle in ("--no-sync", "--keyword-only", "--frozen", "PYTHONDONTWRITEBYTECODE=1", "UV_PROJECT_ENVIRONMENT",
                   'KBHUB_HOME="$HOME/mnt/.kbhub"', "projects --no-sync", "sources --no-sync",
                   "section --no-sync", "--project", "--doc-type", "--scope", "superseded", "last sync",
                   "manifest.json", "No index found", "STATE.md", "D-NNN", "handovers/", "never sync"):
        assert needle in body, needle
    text = COWORK_SKILL.read_text(encoding="utf-8")
    assert "/Users/" not in text and "@" not in text
    # every command in the skill is read-only: nothing that syncs, registers or creates
    for line in body.splitlines():
        if "uv run" in line:
            assert "--no-sync" in line, line
    for writer in ("kbhub index", "new-kb", "register ", "new-project", "add-project", "rule add"):
        assert writer not in body, writer


def test_both_skills_carry_the_same_cowork_command():
    assert _command_prefix(_front_and_body(COWORK_SKILL)[1]) == _command_prefix(_front_and_body(MAIN_SKILL)[1])
    assert "kbhub-cowork" in _front_and_body(MAIN_SKILL)[1]


def test_cowork_skill_packages_as_an_uploadable_zip(kb_home, tmp_path, capsys):
    out = tmp_path / "up" / "skill.zip"
    assert main(["cowork-skill", "--out", str(out)]) == 0
    assert str(out) in capsys.readouterr().out
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["kbhub-cowork/SKILL.md"]
        assert z.read("kbhub-cowork/SKILL.md") == COWORK_SKILL.read_bytes()
    assert main(["cowork-skill"]) == 0              # default: under the engine home, never in a project
    assert (kb_home / "dist" / "kbhub-cowork.zip").is_file()
