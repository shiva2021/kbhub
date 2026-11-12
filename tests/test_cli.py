import json
import re

import pytest

from kbhub.cli import main

FILES = {
    "STATE.md": "# STATE\n\n## Status\nBuilding the wombat importer.\n",
    "knowledge-base/decisions.md": "# Decisions\n\n## D-001: Channel ids\nChannels use <clientId>-<swId>.\n",
}
KB = ('name = "Alpha"\n[[eval]]\nquestion = "What is the channel id format?"\nexpect = "decisions"\n'
      '[[eval]]\nquestion = "wombat importer status"\nexpect = "no-such-file"\n')


@pytest.fixture
def project(kb_home, scan_root, make_project):
    return make_project(scan_root, "alpha-folder", FILES, kb_toml=KB)


def snapshot(folder):
    return {str(p): p.stat().st_mtime_ns for p in sorted(folder.rglob("*"))}


def test_index_then_search(project, capsys):
    assert main(["index"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["changed"] == 2 and stats["rebuilt"] is True
    assert main(["search", "channel id format", "--project", "Alpha", "-k", "1"]) == 0
    out = capsys.readouterr().out
    assert "[1] Alpha · knowledge-base/decisions.md:3-4 | decisions | current" in out and "[2]" not in out
    assert main(["search", "channel", "--doc-type", "state", "--all"]) == 0
    assert "decisions.md" not in capsys.readouterr().out
    assert main(["index", "--rebuild"]) == 0
    assert json.loads(capsys.readouterr().out)["rebuilt"] is True


def test_search_syncs_first_by_default(project, capsys):
    assert main(["search", "wombat importer"]) == 0
    assert "Alpha · STATE.md" in capsys.readouterr().out


def test_section_sources_projects(project, capsys):
    assert main(["section", "Alpha", "knowledge-base/decisions.md", "D-001"]) == 0
    assert capsys.readouterr().out.startswith("knowledge-base/decisions.md:3-4\n## D-001: Channel ids")
    assert main(["sources", "--project", "Alpha"]) == 0
    out = capsys.readouterr().out
    assert "STATE.md" in out and "decisions" in out and "Alpha" in out
    assert main(["projects"]) == 0
    out = capsys.readouterr().out
    assert "Alpha" in out and str(project) in out and "2 files" in out


def test_new_project_then_indexed(kb_home, scan_root, capsys):
    assert main(["new-project", "billing"]) == 0
    assert capsys.readouterr().out.strip() == str(scan_root / "billing")
    assert main(["index"]) == 0
    capsys.readouterr()
    assert main(["search", "billing project created", "--project", "billing"]) == 0
    assert "billing · STATE.md" in capsys.readouterr().out
    assert main(["new-project", "billing"]) == 1
    assert "not empty" in capsys.readouterr().err


def test_new_project_with_parent(kb_home, tmp_path, capsys):
    assert main(["new-project", "side", "--parent", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "side")
    assert main(["projects"]) == 0
    assert "side" in capsys.readouterr().out


def test_add_project_git_refused_exit_1(kb_home, tmp_path, capsys):
    notes = tmp_path / "repo" / "notes"
    notes.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    assert main(["add-project", str(notes)]) == 1
    captured = capsys.readouterr()
    assert "inside a git repository" in captured.err and captured.out == ""
    assert not (notes / ".kb.toml").exists()


def test_add_project_with_name(kb_home, scan_root, capsys):
    (scan_root / "existing").mkdir()
    assert main(["add-project", str(scan_root / "existing"), "--name", "Named"]) == 0
    assert capsys.readouterr().out.strip() == str(scan_root / "existing")
    assert main(["projects"]) == 0
    assert "Named" in capsys.readouterr().out


def test_eval_output_format(project, capsys):
    assert main(["eval", "Alpha"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert any(re.fullmatch(r"rank 1\s+~\s*\d+ tok\s+What is the channel id format\?", l) for l in lines), lines
    assert any(re.fullmatch(r"MISS\s+~\s*\d+ tok\s+wombat importer status", l) for l in lines), lines
    assert lines[-1] == "hit@1 1/2  hit@3 1/2  hit@6 1/2"
    assert main(["eval"]) == 0
    assert "hit@1 1/2" in capsys.readouterr().out
    assert main(["eval", "nope"]) == 1
    assert "Unknown project" in capsys.readouterr().err


def test_no_sync_does_not_write(project, kb_home, capsys):
    assert main(["index"]) == 0
    capsys.readouterr()
    (project / "late.md").write_text("## Late\nplatypus note\n")
    before = snapshot(kb_home)
    assert main(["search", "channel", "--no-sync", "--keyword-only"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("(keyword-only search: Ollama unavailable)") and "decisions.md" in out
    assert main(["search", "platypus", "--no-sync"]) == 0
    assert "late.md" not in capsys.readouterr().out
    assert snapshot(kb_home) == before


def test_no_sync_without_an_index_reports_cleanly(project, kb_home, capsys):
    assert main(["search", "channel", "--no-sync", "--keyword-only"]) == 1
    captured = capsys.readouterr()
    assert "No index found" in captured.err and "KBHUB_HOME" in captured.err and str(kb_home) in captured.err
    assert "No matches" not in captured.out
    assert not (kb_home / "db").exists()


def test_bad_config_exits_1_naming_file_and_line(project, kb_home, capsys):
    (kb_home / "config.toml").write_text("scan_depth = 3\nscan_roots = = 1\n")
    assert main(["index"]) == 1
    err = capsys.readouterr().err
    assert "config.toml:2:" in err


# ---- v0.2

@pytest.fixture
def core(kb_home, scan_root, make_repo):
    return make_repo(scan_root / "projects" / "core", {"src/app.ts": "", "docs/kb/legacy.md": "## Legacy\nold platypus notes\n"})


def test_new_kb_in_repo_then_search(core, capsys, monkeypatch):
    monkeypatch.chdir(core / "src")
    assert main(["new-kb", "billing"]) == 0
    assert capsys.readouterr().out.strip() == str(core / "knowledge-base" / "billing")
    (core / "knowledge-base" / "billing" / "decisions.md").write_text("# Decisions\n\n## D-001: Ledger\nBilling uses the quokka ledger.\n")
    assert main(["search", "quokka ledger", "--project", "core"]) == 0
    assert "[1] core/billing · decisions.md:3-4 | decisions | current" in capsys.readouterr().out
    assert main(["new-kb", "payroll", "--in", str(core), "--name", "Payroll KB", "--scope", "personal"]) == 0
    capsys.readouterr()
    assert main(["projects"]) == 0
    out = capsys.readouterr().out
    assert "core/billing" in out and "Payroll KB" in out and "[personal]" in out and "[work]" in out
    assert f"repo: {core}" in out
    assert not list(core.rglob(".kb.toml"))


def test_scan_and_register(core, capsys):
    assert main(["scan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("NEW ") and str(core / "docs" / "kb") in out and "kbhub register" in out
    assert main(["register", str(core / "docs" / "kb"), "--name", "core/legacy"]) == 0
    assert capsys.readouterr().out.strip() == "core/legacy"
    assert main(["scan", str(core)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok  ") and "core/legacy" in out and "NEW" not in out
    assert main(["search", "platypus", "--project", "core/legacy"]) == 0
    assert "core/legacy · legacy.md" in capsys.readouterr().out


def test_search_scope_flags(kb_home, scan_root, make_project, capsys, monkeypatch):
    make_project(scan_root, "Work", {"n.md": "## Plan\nwork quokka plan\n"})
    diary = make_project(scan_root / "personal", "Diary", {"n.md": "## Secret\nprivate quokka diary\n"})
    monkeypatch.chdir(scan_root)
    assert main(["search", "quokka"]) == 0
    assert "Diary" not in capsys.readouterr().out
    assert main(["search", "quokka", "--scope", "personal"]) == 0
    out = capsys.readouterr().out
    assert "Diary · n.md" in out and "Work" not in out
    assert main(["search", "quokka", "--scope", "all"]) == 0
    out = capsys.readouterr().out
    assert "Diary" in out and "Work" in out
    assert main(["sources", "--scope", "personal"]) == 0
    assert "Work" not in capsys.readouterr().out
    monkeypatch.chdir(diary)      # a session inside a personal project defaults to personal
    assert main(["search", "quokka"]) == 0
    out = capsys.readouterr().out
    assert "Diary · n.md" in out and "Work" not in out
    assert main(["search", "quokka", "--scope", "bogus"]) == 1
    assert "Unknown scope" in capsys.readouterr().err


def test_rule_add_and_unregister(core, capsys):
    assert main(["register", str(core / "docs" / "kb"), "--name", "core/legacy"]) == 0
    assert main(["rule", "add", "core/legacy", "legacy.md", "--status", "superseded", "--note", "replaced"]) == 0
    capsys.readouterr()
    assert main(["search", "platypus"]) == 0
    out = capsys.readouterr().out
    assert "[1]" not in out and "do not act on it" in out
    assert main(["rule", "add", "core/legacy", "x/**"]) == 1
    assert "status or doc_type" in capsys.readouterr().err
    assert main(["unregister", "core/legacy"]) == 0
    assert main(["unregister", "core/legacy"]) == 1
    capsys.readouterr()
    assert main(["projects"]) == 0
    assert "core/legacy" not in capsys.readouterr().out
    assert (core / "docs" / "kb" / "legacy.md").is_file()


def test_register_repo_root_exit_1(core, capsys):
    assert main(["register", str(core)]) == 1
    assert "repo root" in capsys.readouterr().err
