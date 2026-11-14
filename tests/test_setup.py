"""The setup wizard, driven with scripted answers and a fake command runner (nothing is installed)."""
import json
import tomllib
from pathlib import Path

import pytest

from conftest import FakeRun
from kbhub import setup as su
from kbhub.config import GlobalConfig


def config(home):
    return tomllib.loads((home / ".kbhub" / "config.toml").read_text())


def test_all_defaults_installs_everything_and_wires_claude(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "brew")), yes=True)
    assert su.run_wizard(env, ui) == 0
    cfg = config(home)
    assert cfg["kb_scan_roots"] == [str(home / "Developer")] and cfg["personal_roots"] == []
    assert cfg["embedding"]["backend"] == "ollama" and cfg["default_scope"] == "work"
    assert run.ran("brew install ollama") and run.ran("ollama pull nomic-embed-text")
    assert run.ran("claude mcp add kbhub --scope user -- /fake/bin/uv run --project")
    assert (home / ".claude" / "skills" / "kbhub").is_symlink()
    assert "session_start.py" in (home / ".claude" / "settings.json").read_text()
    assert "<!-- kbhub:start -->" in (home / ".claude" / "CLAUDE.md").read_text()
    launcher = home / ".local" / "bin" / "kbhub"
    assert launcher.is_file() and launcher.stat().st_mode & 0o111 and "/fake/bin/uv" in launcher.read_text()
    text = "\n".join(said)
    assert "[1/6]" in text and "[6/6]" in text and "keep a knowledge base" in text
    GlobalConfig.load(home / ".kbhub" / "config.toml")      # what was written is a valid config


def test_answers_choose_folders_and_decline_ollama(machine):
    home, build = machine
    (home / "work").mkdir()
    (home / "mine").mkdir()
    # Ollama? n · work folder · personal folder · notes folder
    env, ui, run, said = build(answers=["n", "~/work", "~/mine", ""])
    assert su.run_wizard(env, ui) == 0
    cfg = config(home)
    assert cfg["embedding"]["backend"] == "wordllama"
    assert cfg["kb_scan_roots"] == [str(home / "work"), str(home / "mine")]
    assert cfg["personal_roots"] == [str(home / "mine")]
    assert cfg["new_project_parent"] == str(home / "knowledge-bases")
    assert str(home / "knowledge-bases") in cfg["scan_roots"]
    assert not run.ran("install ollama") and not run.ran("Ollama-darwin")


def test_ollama_without_homebrew_uses_the_app_download(machine):
    home, build = machine
    env, ui, run, said = build(yes=True)
    assert su.run_wizard(env, ui) == 0
    assert run.ran("https://ollama.com/download/Ollama-darwin.zip") and run.ran("ditto -xk")
    assert run.ran(f"{home}/Applications/Ollama.app/Contents/Resources/ollama pull nomic-embed-text")
    assert config(home)["embedding"]["backend"] == "ollama"


def test_ollama_install_failure_falls_back_to_offline_model(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "brew"), fail=("brew install ollama",)), yes=True)
    assert su.run_wizard(env, ui) == 0
    assert config(home)["embedding"]["backend"] == "wordllama"
    assert any("offline" in line for line in said)


def test_ollama_already_running_only_pulls_the_model(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "ollama"), ollama_up=True), yes=True)
    assert su.run_wizard(env, ui) == 0
    assert run.ran("/fake/bin/ollama pull nomic-embed-text") and not run.ran("brew")
    assert config(home)["embedding"]["backend"] == "ollama"


def test_missing_claude_is_installed_or_skipped_with_a_clear_message(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv",), ollama_up=True), yes=True)
    assert su.run_wizard(env, ui) == 0
    assert run.ran("https://claude.ai/install.sh") and run.ran("claude mcp add kbhub")

    home2 = home.parent / "home2"
    env, ui, run, said = build(run=FakeRun(present=("uv",), ollama_up=True),
                               answers=["n"] + [""] * 6)   # Claude Code? n, then defaults
    assert su.run_wizard(env, ui) == 0
    assert not run.ran("install.sh") and not run.ran("mcp add")
    assert any("kbhub setup" in line and "Claude Code" in line for line in said)


def test_existing_config_is_kept(machine):
    home, build = machine
    (home / ".kbhub").mkdir()
    (home / ".kbhub" / "config.toml").write_text('max_chunk_tokens = 111\n[embedding]\nbackend = "wordllama"\n')
    env, ui, run, said = build(answers=["n"])     # Ollama is offered again; declined
    assert su.run_wizard(env, ui) == 0
    assert (home / ".kbhub" / "config.toml").read_text() == 'max_chunk_tokens = 111\n[embedding]\nbackend = "wordllama"\n'


def test_found_knowledge_bases_are_offered_and_registered(machine):
    home, build = machine
    repo = home / "Developer" / "shop"
    (repo / ".git").mkdir(parents=True)
    for topic in ("billing", "search"):
        (repo / "knowledge-base" / topic).mkdir(parents=True)
        (repo / "knowledge-base" / topic / "README.md").write_text(f"# {topic}\nquokka\n")
    # Ollama? n · three folder defaults · billing? y · search? n
    env, ui, run, said = build(answers=["n", "", "", "", "y", "n"])
    assert su.run_wizard(env, ui) == 0
    kbs = json.loads((home / ".kbhub" / "registry.json").read_text())["kbs"]
    assert [k["name"] for k in kbs] == ["shop/billing"]
    assert not list(repo.rglob(".kb.toml"))


def test_launcher_warns_when_bin_dir_is_not_on_path(machine):
    home, build = machine
    env, ui, run, said = build(yes=True, path="/usr/bin:/bin")
    assert su.run_wizard(env, ui) == 0
    assert any(".local/bin" in line and "PATH" in line for line in said)


def test_wire_only_asks_nothing_and_installs_nothing(machine):
    home, build = machine
    env, ui, run, said = build(answers=[])
    assert su.run_wizard(env, ui, wire_only=True) == 0
    assert run.ran("claude mcp add kbhub") and not run.ran("Ollama-darwin") and not run.ran("install.sh")
    assert (home / ".kbhub" / "config.toml").is_file()


def test_offers_to_put_the_command_on_path_and_never_twice(machine):
    home, build = machine
    env, ui, run, said = build(yes=True, path="/usr/bin:/bin")
    assert su.run_wizard(env, ui) == 0
    rc = (home / ".zshrc").read_text()
    assert rc.count('export PATH="$HOME/.local/bin:$PATH"') == 1
    env, ui, run, said = build(yes=True, path="/usr/bin:/bin")       # run again in the same old shell
    su.run_wizard(env, ui)
    assert (home / ".zshrc").read_text() == rc


def test_declining_the_path_change_leaves_the_shell_file_alone(machine):
    home, build = machine
    (home / ".zshrc").write_text("# mine\n")
    # Ollama? n · three folder defaults · add to PATH? n
    env, ui, run, said = build(answers=["n", "", "", "", "n"], path="/usr/bin:/bin")
    assert su.run_wizard(env, ui) == 0
    assert (home / ".zshrc").read_text() == "# mine\n"
    assert any("export PATH" in line for line in said)


def test_messages_show_the_home_folder_as_a_tilde(machine):
    home, build = machine
    env, ui, run, said = build(yes=True)
    su.run_wizard(env, ui)
    assert not [line for line in said if str(home) in line]
    assert any("~/.kbhub/config.toml" in line for line in said)
