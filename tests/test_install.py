import json
from pathlib import Path

import pytest

from kbhub.install import BLOCK_END, BLOCK_START, install, uninstall

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def claude_home(tmp_path):
    home = tmp_path / "claude"
    home.mkdir()
    return home


def go(claude_home, kb_home, **kw):
    return install(claude_home, kb_home, run_claude=False, check_ollama=False, **{"run_index": False, **kw})


def our_hooks(claude_home):
    settings = json.loads((claude_home / "settings.json").read_text())
    return [h["command"] for entry in settings.get("hooks", {}).get("SessionStart", [])
            for h in entry["hooks"] if "session_start.py" in h["command"]]


def test_install_idempotent(claude_home, tmp_path):
    kb_home = tmp_path / "fresh-home"
    first = go(claude_home, kb_home)
    go(claude_home, kb_home)
    assert first and all(isinstance(line, str) for line in first)
    assert (kb_home / "config.toml").read_text() == (REPO / "config.example.toml").read_text()
    link = claude_home / "skills" / "kbhub"
    assert link.is_symlink() and (link / "SKILL.md").is_file()
    assert link.resolve() == (REPO / "skill" / "kbhub").resolve()
    hooks = our_hooks(claude_home)
    assert len(hooks) == 1
    assert hooks[0] == f"{REPO}/.venv/bin/python {REPO}/hooks/session_start.py"
    md = (claude_home / "CLAUDE.md").read_text()
    assert md.count(BLOCK_START) == 1 and md.count(BLOCK_END) == 1 and "search_kb" in md


def test_existing_config_is_kept(claude_home, kb_home):
    before = (kb_home / "config.toml").read_text()
    go(claude_home, kb_home)
    assert (kb_home / "config.toml").read_text() == before


def test_existing_settings_preserved(claude_home, kb_home):
    original = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo other"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
        },
    }
    (claude_home / "settings.json").write_text(json.dumps(original, indent=2))
    go(claude_home, kb_home)
    go(claude_home, kb_home)
    settings = json.loads((claude_home / "settings.json").read_text())
    assert settings["model"] == "opus" and settings["permissions"] == original["permissions"]
    assert settings["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert settings["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    assert len(settings["hooks"]["SessionStart"]) == 2 and len(our_hooks(claude_home)) == 1
    assert json.loads((claude_home / "settings.json.kbhub-backup").read_text()) == original  # the file as it was before the last change


def test_invalid_settings_json_is_not_touched(claude_home, kb_home):
    (claude_home / "settings.json").write_text("{ not json")
    with pytest.raises(ValueError, match="settings.json"):
        go(claude_home, kb_home)
    assert (claude_home / "settings.json").read_text() == "{ not json"


def test_claude_md_outside_text_kept(claude_home, kb_home):
    (claude_home / "CLAUDE.md").write_text("# Mine\n\nKeep this.\n")
    go(claude_home, kb_home)
    md = (claude_home / "CLAUDE.md").read_text()
    assert md.startswith("# Mine\n\nKeep this.\n") and BLOCK_START in md
    stale = md.replace("search_kb", "OLD_TEXT") + "\nAfter the block.\n"
    (claude_home / "CLAUDE.md").write_text(stale)
    go(claude_home, kb_home)
    md = (claude_home / "CLAUDE.md").read_text()
    assert "OLD_TEXT" not in md and "search_kb" in md and md.count(BLOCK_START) == 1
    assert md.startswith("# Mine\n\nKeep this.\n") and md.endswith("\nAfter the block.\n")


def test_existing_real_skill_folder_not_clobbered(claude_home, kb_home):
    real = claude_home / "skills" / "kbhub"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("mine")
    log = go(claude_home, kb_home)
    assert (real / "SKILL.md").read_text() == "mine" and not real.is_symlink()
    assert any("not a symlink" in line for line in log)


def test_uninstall_reverses(claude_home, kb_home):
    (claude_home / "settings.json").write_text(json.dumps(
        {"model": "opus", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo other"}]}]}}))
    (claude_home / "CLAUDE.md").write_text("# Mine\n\nKeep this.\n")
    go(claude_home, kb_home)
    (kb_home / "db").mkdir()
    log = uninstall(claude_home, kb_home, run_claude=False)
    assert log
    settings = json.loads((claude_home / "settings.json").read_text())
    assert settings == {"model": "opus", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo other"}]}]}}
    assert (claude_home / "CLAUDE.md").read_text() == "# Mine\n\nKeep this.\n"
    assert not (claude_home / "skills" / "kbhub").exists()
    assert not (claude_home / "skills" / "kbhub").is_symlink()
    assert (kb_home / "db").is_dir() and (kb_home / "config.toml").is_file()
    uninstall(claude_home, kb_home, run_claude=False)  # idempotent


def test_uninstall_on_clean_home_leaves_no_litter(claude_home, kb_home):
    go(claude_home, kb_home)
    uninstall(claude_home, kb_home, run_claude=False)
    assert json.loads((claude_home / "settings.json").read_text()) == {}
    assert not (claude_home / "CLAUDE.md").exists()


def test_install_indexes_and_lists_projects(claude_home, kb_home, scan_root, make_project):
    make_project(scan_root, "Alpha", {"STATE.md": "# STATE\n\n## Status\nwombat\n"})
    log = go(claude_home, kb_home, run_index=True)
    assert any("Alpha" in line for line in log)
    assert (kb_home / "db" / "manifest.json").is_file()


def test_ollama_missing_switches_to_wordllama(claude_home, tmp_path, monkeypatch):
    import kbhub.install as inst
    monkeypatch.setattr(inst, "_run", lambda cmd, **kw: (1, "ollama: command not found"))
    kb_home = tmp_path / "fresh-home"
    log = install(claude_home, kb_home, run_claude=False, check_ollama=True, run_index=False)
    assert 'backend = "wordllama"' in (kb_home / "config.toml").read_text()
    assert any("wordllama" in line for line in log)


def test_ollama_present_pulls_model(claude_home, tmp_path, monkeypatch):
    import kbhub.install as inst
    calls = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **kw: (calls.append(cmd), (0, "ok"))[1])
    kb_home = tmp_path / "fresh-home"
    install(claude_home, kb_home, run_claude=False, check_ollama=True, run_index=False)
    assert ["ollama", "list"] in calls and ["ollama", "pull", "nomic-embed-text"] in calls
    assert 'backend = "ollama"' in (kb_home / "config.toml").read_text()


def test_run_claude_registers_mcp_server(claude_home, kb_home, monkeypatch):
    import kbhub.install as inst
    calls = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **kw: (calls.append(cmd), (0, "ok"))[1])
    install(claude_home, kb_home, run_claude=True, check_ollama=False, run_index=False)
    assert calls[0] == ["claude", "mcp", "remove", "kbhub", "--scope", "user"]
    add = calls[1]
    assert add[:6] == ["claude", "mcp", "add", "kbhub", "--scope", "user"]
    assert add[6] == "--" and add[7].endswith("uv") and add[8:] == ["run", "--project", str(REPO), "kbhub-server"]
    calls.clear()
    uninstall(claude_home, kb_home, run_claude=True)
    assert calls == [["claude", "mcp", "remove", "kbhub", "--scope", "user"]]


def test_existing_config_never_flipped_when_ollama_is_unavailable(claude_home, kb_home, monkeypatch):
    import kbhub.install as inst
    config = kb_home / "config.toml"
    config.write_text(config.read_text().replace('backend = "wordllama"', 'backend = "ollama"'))
    before = config.read_text()
    monkeypatch.setattr(inst, "_run", lambda cmd, **kw: (1, "could not connect to ollama app"))
    log = install(claude_home, kb_home, run_claude=False, check_ollama=True, run_index=False)
    assert config.read_text() == before
    assert any("left unchanged" in line for line in log)


def test_ollama_pull_skipped_when_model_already_present(claude_home, tmp_path, monkeypatch):
    import kbhub.install as inst
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return (0, "NAME  ID\nnomic-embed-text:latest  abc") if cmd == ["ollama", "list"] else (1, "offline")

    monkeypatch.setattr(inst, "_run", fake)
    kb_home = tmp_path / "fresh-home"
    install(claude_home, kb_home, run_claude=False, check_ollama=True, run_index=False)
    assert ["ollama", "pull", "nomic-embed-text"] not in calls
    assert 'backend = "ollama"' in (kb_home / "config.toml").read_text()
