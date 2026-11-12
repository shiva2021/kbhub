"""Findings of the v0.3 branch review, each pinned by a test."""
import json
import os

import pytest

from conftest import FakeRun
from kbhub import install as inst
from kbhub import setup as su
from kbhub.config import GlobalConfig


# 1. Ctrl-D must cancel, never accept installs --------------------------------------------------

def test_end_of_input_cancels_an_interactive_run_and_installs_nothing(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv",)), answers=[])      # EOF at "Install Claude Code?"
    with pytest.raises(su.Cancelled):
        su.run_wizard(env, ui)
    assert not run.ran("install.sh") and not run.ran("Ollama-darwin") and not (home / ".zshrc").exists()


# 2. Re-running setup offers Ollama again -------------------------------------------------------

def test_rerun_can_switch_from_the_offline_model_to_ollama(machine):
    home, build = machine
    env, ui, run, said = build(answers=["n", "", "", ""])                     # first run: decline Ollama
    su.run_wizard(env, ui)
    assert 'backend = "wordllama"' in (home / ".kbhub" / "config.toml").read_text()
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "brew")), answers=["y"])
    su.run_wizard(env, ui)
    text = (home / ".kbhub" / "config.toml").read_text()
    assert 'backend = "ollama"' in text and "wordllama\"\n" not in text.split("[embedding]")[1].split("ollama_model")[0]
    assert run.ran("brew install ollama")


def test_existing_ollama_config_with_ollama_down_says_so_honestly(machine):
    home, build = machine
    (home / ".kbhub").mkdir()
    (home / ".kbhub" / "config.toml").write_text('[embedding]\nbackend = "ollama"\n')
    env, ui, run, said = build(answers=["n"])                                  # no Ollama, decline install
    su.run_wizard(env, ui)
    assert 'backend = "ollama"' in (home / ".kbhub" / "config.toml").read_text()
    text = "\n".join(said)
    assert "match words only" in text and "Using the built-in offline model" not in text


# 3. Folder answers ------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["~/My\\ Projects", '"~/My Projects"', "'~/My Projects'", "My Projects", "  ~/My Projects/  "])
def test_folder_answers_as_people_type_them(machine, answer):
    home, build = machine
    (home / "My Projects").mkdir()
    env, ui, run, said = build(answers=["n", answer, "", ""])
    su.run_wizard(env, ui)
    assert GlobalConfig.load(home / ".kbhub" / "config.toml").kb_scan_roots == [home / "My Projects"]


def test_three_bad_folder_answers_fall_back_out_loud(machine):
    home, build = machine
    env, ui, run, said = build(answers=["n", "~/nope", "~/nope", "~/nope", "", ""])
    su.run_wizard(env, ui)
    assert GlobalConfig.load(home / ".kbhub" / "config.toml").kb_scan_roots == [home / "Developer"]
    assert any("Using ~/Developer" in line for line in said)


def test_notes_folder_answer_is_cleaned_and_may_not_be_a_file(machine):
    home, build = machine
    (home / "afile").write_text("x")
    env, ui, run, said = build(answers=["n", "", "", "~/afile", "'~/My Notes'"])
    su.run_wizard(env, ui)
    assert GlobalConfig.load(home / ".kbhub" / "config.toml").new_project_parent == home / "My Notes"


# 4. Unreadable folders ---------------------------------------------------------------------------

def test_unreadable_folders_are_skipped_not_fatal(kb_home, scan_root, make_project, make_repo):
    from kbhub.manage import scan
    from kbhub.registry import discover_projects
    make_project(scan_root, "Alpha", {"a.md": "x"})
    make_repo(scan_root / "projects" / "shop", {"knowledge-base/billing/a.md": "x"})
    locked = scan_root / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / ".kb.toml").write_text("")
    os.chmod(locked, 0o000)
    try:
        cfg = GlobalConfig.load()
        projects, _ = discover_projects(cfg)
        assert "Alpha" in projects
        assert [c["name"] for c in scan(cfg)] == ["shop/billing"]
    finally:
        os.chmod(locked, 0o755)


# 5. CLAUDE.md is never eaten ---------------------------------------------------------------------

def test_stray_start_marker_never_costs_the_user_their_rules(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    md = claude / "CLAUDE.md"
    md.write_text("<!-- kbhub:start -->\n\n# My rules\nalways be kind\n")
    for _ in range(3):
        inst._step_claude_md(claude, [])
    text = md.read_text()
    assert "always be kind" in text and text.count("<!-- kbhub:end -->") == 1
    inst.uninstall(claude, tmp_path / "kb", run_claude=False)
    assert "always be kind" in md.read_text() and "<!-- kbhub:end -->" not in md.read_text()


def test_duplicate_blocks_collapse_to_one_and_a_backup_is_taken(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    md = claude / "CLAUDE.md"
    old = "<!-- kbhub:start -->\nold\n<!-- kbhub:end -->"
    md.write_text(f"# Top\n\n{old}\n\nmiddle\n\n{old}\n\nbottom\n")
    inst._step_claude_md(claude, [])
    text = md.read_text()
    assert text.count("<!-- kbhub:start -->") == 1 and "middle" in text and "bottom" in text and "\nold\n" not in text
    assert "\nold\n" in (claude / "CLAUDE.md.kbhub-backup").read_text()


# 6. settings.json shapes -------------------------------------------------------------------------

@pytest.mark.parametrize("hooks", [{"SessionStart": None}, {"SessionStart": {"x": 1}}, [], "x"])
def test_unexpected_settings_shapes_stop_before_anything_changes(tmp_path, hooks):
    claude, kb = tmp_path / ".claude", tmp_path / "kb"
    claude.mkdir()
    original = json.dumps({"hooks": hooks, "keep": True})
    (claude / "settings.json").write_text(original)
    with pytest.raises(ValueError, match="settings.json"):
        inst.install(claude, kb, run_claude=False, check_ollama=False, run_index=False)
    with pytest.raises(ValueError, match="settings.json"):
        inst.uninstall(claude, kb, run_claude=False)
    assert (claude / "settings.json").read_text() == original
    assert sorted(p.name for p in claude.iterdir()) == ["settings.json"] and not kb.exists()


def test_a_hook_entry_shared_with_another_command_keeps_the_other_command(tmp_path):
    claude, kb = tmp_path / ".claude", tmp_path / "kb"
    claude.mkdir()
    other = {"type": "command", "command": "echo mine"}
    ours = {"type": "command", "command": inst._hook_command()}
    (claude / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [other, ours]}]}}))
    inst._step_hook(claude, [])
    entries = json.loads((claude / "settings.json").read_text())["hooks"]["SessionStart"]
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert commands.count("echo mine") == 1 and commands.count(inst._hook_command()) == 1
    inst.uninstall(claude, kb, run_claude=False)
    entries = json.loads((claude / "settings.json").read_text())["hooks"]["SessionStart"]
    assert [h["command"] for e in entries for h in e["hooks"]] == ["echo mine"]


def test_every_settings_change_leaves_a_fresh_backup(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text('{"model": "a"}')
    inst._step_hook(claude, [])
    assert json.loads((claude / "settings.json.kbhub-backup").read_text()) == {"model": "a"}
    inst.uninstall(claude, tmp_path / "kb", run_claude=False)
    assert "session_start.py" in (claude / "settings.json.kbhub-backup").read_text()


def test_cli_admin_errors_are_messages_not_tracebacks(machine, monkeypatch, capsys):
    from kbhub.cli import main
    home, build = machine
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{not json")
    env, ui, run, said = build()
    monkeypatch.setattr("kbhub.system.Env.real", classmethod(lambda cls: env))
    assert main(["uninstall"]) == 1
    assert "settings.json" in capsys.readouterr().err
