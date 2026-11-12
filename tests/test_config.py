from pathlib import Path

import pytest

from kbhub.config import (ConfigError, GlobalConfig, ProjectConfig, file_meta, glob_match)


def test_glob_case_insensitive_and_root():
    assert glob_match("x/DECISIONS.md", "**/*decisions*.md")
    assert glob_match("CLAUDE.md", "**/CLAUDE.md")
    assert not glob_match("a/b.md", "*.txt")


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KBHUB_HOME", str(tmp_path))
    cfg = GlobalConfig.load(tmp_path / "none.toml")
    assert cfg.scan_depth == 3
    assert cfg.max_chunk_tokens == 450
    assert cfg.fingerprint == "defaults"
    assert cfg.db_dir == tmp_path / "db"
    assert cfg.exclude_dirs == [] and cfg.scan_roots == [Path.home()] and cfg.new_project_parent == Path.home()
    assert cfg.embedding["ollama_model"] == "nomic-embed-text"


def test_load_uses_engine_home_env(kb_home, scan_root):
    cfg = GlobalConfig.load()
    assert cfg.path == kb_home / "config.toml"
    assert cfg.scan_roots == [scan_root]
    assert cfg.embedding["backend"] == "wordllama"
    assert cfg.embedding["ollama_url"] == "http://localhost:11434"
    assert cfg.fingerprint != "defaults"


def test_tilde_expanded(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('scan_roots = ["~/Desktop"]\n')
    assert GlobalConfig.load(p).scan_roots == [Path.home() / "Desktop"]


def test_bad_global_toml_raises_with_line(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("scan_depth = 3\nmax_chunk_tokens = 450\nscan_roots = = 1\n")
    with pytest.raises(ConfigError) as exc:
        GlobalConfig.load(p)
    assert ":3:" in str(exc.value)
    assert str(p) in str(exc.value)


def test_wrong_type_in_global_config_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('scan_depth = "deep"\n')
    with pytest.raises(ConfigError, match="scan_depth"):
        GlobalConfig.load(p)


def test_example_config_documents_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("KBHUB_HOME", str(tmp_path))
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    a, b = GlobalConfig.load(example), GlobalConfig.load(tmp_path / "none.toml")
    for key in ("scan_roots", "scan_depth", "exclude_dirs", "explicit_projects", "new_project_parent",
                "max_chunk_tokens", "max_file_bytes", "embedding", "personal_roots", "default_scope",
                "kb_scan_roots", "kb_scan_depth", "kb_folder_names"):
        assert getattr(a, key) == getattr(b, key), key


def test_bad_kb_toml_is_soft(tmp_path):
    p = tmp_path / ".kb.toml"
    p.write_text("name = \n[[rules]\n")
    pc = ProjectConfig.load(p)
    assert pc.error is not None
    assert pc.rules == []
    assert pc.name is None


def test_kb_toml_rules_and_eval_loaded(tmp_path):
    p = tmp_path / ".kb.toml"
    p.write_text('name = "Atlas"\n[[rules]]\nglob = "old/**"\nstatus = "superseded"\nnote = "n"\n'
                 '[[eval]]\nquestion = "q?"\nexpect = "decisions"\n')
    pc = ProjectConfig.load(p)
    assert pc.error is None and pc.name == "Atlas"
    assert pc.rules == [{"glob": "old/**", "status": "superseded", "note": "n"}]
    assert pc.eval_cases == [("q?", "decisions")]


def test_kb_toml_invalid_status_is_soft(tmp_path):
    p = tmp_path / ".kb.toml"
    p.write_text('[[rules]]\nglob = "old/**"\nstatus = "gone"\n')
    pc = ProjectConfig.load(p)
    assert pc.error is not None and pc.rules == []


def test_file_meta_rules_then_defaults():
    rules = [{"glob": "old/**", "status": "superseded", "note": "n"}]
    meta = file_meta(rules, "old/a.md")
    assert meta["status"] == "superseded" and meta["status_note"] == "n"
    assert file_meta([], "knowledge-base/decisions.md")["doc_type"] == "decisions"
    assert file_meta([], "handovers/2026-01-01-x.md")["doc_type"] == "handover"
    assert file_meta([], "STATE.md")["doc_type"] == "state"
    assert file_meta([], "misc/x.md") == {"status": "current", "status_note": "", "doc_type": "notes"}
    assert file_meta([{"glob": "handovers/**", "doc_type": "pr"}], "handovers/a.md")["doc_type"] == "pr"


# ---- v0.2

def test_new_keys_default(tmp_path, monkeypatch):
    monkeypatch.setenv("KBHUB_HOME", str(tmp_path))
    cfg = GlobalConfig.load(tmp_path / "none.toml")
    assert cfg.personal_roots == []
    assert cfg.default_scope == "work"
    assert cfg.kb_scan_roots == [Path.home()] and cfg.kb_scan_depth == 7
    assert cfg.kb_folder_names == ["knowledge-base", "knowledge_base", "docs/kb"]


def test_bad_default_scope_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('default_scope = "office"\n')
    with pytest.raises(ConfigError, match="default_scope"):
        GlobalConfig.load(p)


def test_index_fingerprint_ignores_unrelated_keys(tmp_path):
    a, b, c = tmp_path / "a.toml", tmp_path / "b.toml", tmp_path / "c.toml"
    a.write_text("scan_depth = 3\n")
    b.write_text("# a comment\nscan_depth = 5\n")
    c.write_text("max_chunk_tokens = 300\n")
    ca, cb, cc = GlobalConfig.load(a), GlobalConfig.load(b), GlobalConfig.load(c)
    assert ca.index_fingerprint == cb.index_fingerprint and ca.fingerprint != cb.fingerprint
    assert cc.index_fingerprint != ca.index_fingerprint
    assert GlobalConfig.load(tmp_path / "none.toml").index_fingerprint == ca.index_fingerprint


def test_kb_toml_scope(tmp_path):
    p = tmp_path / ".kb.toml"
    p.write_text('scope = "personal"\n')
    assert ProjectConfig.load(p).scope == "personal"
    p.write_text('scope = "x"\n')
    pc = ProjectConfig.load(p)
    assert pc.error is not None and pc.scope == "personal"   # an unknown scope fails closed
    p.write_text("")
    assert ProjectConfig.load(p).scope is None


def test_project_config_from_registry():
    entry = {"name": "core/billing", "scope": "work",
             "rules": [{"glob": "old/**", "status": "superseded"}], "eval": [{"question": "q", "expect": "e"}]}
    pc = ProjectConfig.from_registry(entry)
    assert pc.name == "core/billing" and pc.rules == entry["rules"] and pc.eval_cases == [("q", "e")]
    assert pc.scope == "work" and pc.error is None and pc.fingerprint
    assert ProjectConfig.from_registry({**entry, "rules": []}).fingerprint != pc.fingerprint
    bad = ProjectConfig.from_registry({**entry, "rules": [{"status": "superseded"}]})
    assert bad.error is not None and bad.rules == []


def test_flat_doc_type_defaults():
    assert file_meta([], "decisions.md")["doc_type"] == "decisions"
    assert file_meta([], "README.md")["doc_type"] == "entrypoint"
    assert file_meta([], "runbooks/a.md")["doc_type"] == "runbook"
    assert file_meta([], "research/x/y.md")["doc_type"] == "research"
    assert file_meta([], "lessons.md")["doc_type"] == "lessons"
    assert file_meta([], "open-items.md")["doc_type"] == "open-items"
    assert file_meta([], "knowledge-base/decisions.md")["doc_type"] == "decisions"
    assert file_meta([], "sub/README.md")["doc_type"] == "notes"
