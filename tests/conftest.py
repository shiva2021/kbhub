import json
from pathlib import Path

import pytest


@pytest.fixture
def scan_root(tmp_path) -> Path:
    root = tmp_path / "scan"
    root.mkdir()
    return root


@pytest.fixture
def kb_home(tmp_path, monkeypatch, scan_root) -> Path:
    """Engine home in a tmp dir, with a config whose scan roots never touch the real Desktop."""
    home = tmp_path / "kbhome"
    home.mkdir()
    monkeypatch.setenv("KBHUB_HOME", str(home))
    (home / "config.toml").write_text(
        f"scan_roots = [{json.dumps(str(scan_root))}]\n"
        "exclude_dirs = []\n"
        f"new_project_parent = {json.dumps(str(scan_root))}\n"
        f"personal_roots = [{json.dumps(str(scan_root / 'personal'))}]\n"
        f"kb_scan_roots = [{json.dumps(str(scan_root))}]\n"
        '[embedding]\nbackend = "wordllama"\n',
        encoding="utf-8",
    )
    return home


def _make_project(root: Path, name: str, files: dict[str, str], kb_toml: str = "") -> Path:
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".kb.toml").write_text(kb_toml, encoding="utf-8")
    for rel, text in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def make_project():
    return _make_project


def _make_repo(folder: Path, files: dict[str, str] | None = None) -> Path:
    """A fake main clone: a folder with a .git directory."""
    folder = Path(folder)
    (folder / ".git").mkdir(parents=True, exist_ok=True)
    for rel, text in (files or {}).items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return folder


def _make_worktree(main: Path, folder: Path, files: dict[str, str] | None = None) -> Path:
    """A fake linked worktree of `main`, wired the way git does it."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    admin = Path(main) / ".git" / "worktrees" / folder.name
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "gitdir").write_text(f"{folder / '.git'}\n", encoding="utf-8")
    (folder / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return folder


def _write_registry(kb_home: Path, entries: list[dict]) -> Path:
    path = Path(kb_home) / "registry.json"
    path.write_text(json.dumps({"version": 1, "kbs": entries}, indent=1), encoding="utf-8")
    return path


@pytest.fixture
def make_repo():
    return _make_repo


@pytest.fixture
def make_worktree():
    return _make_worktree


@pytest.fixture
def write_registry():
    return _write_registry


# ------------------------------------------------------------------ setup wizard / doctor

class FakeRun:
    """Stands in for running programs. `present` are the programs that exist; `ollama_up` says
    whether the Ollama server answers; installing something makes it present."""

    def __init__(self, present=("uv", "claude"), ollama_up=False, fail=()):
        self.present, self.ollama_up, self.fail, self.calls = set(present), ollama_up, set(fail), []

    def which(self, name):
        return f"/fake/bin/{name}" if name in self.present else None

    def __call__(self, cmd, env=None, timeout=900):
        self.calls.append(cmd)
        line = " ".join(cmd)
        prog = Path(cmd[0]).name
        if any(f in line for f in self.fail):
            return 1, "simulated failure"
        if "claude.ai/install.sh" in line:
            self.present.add("claude")
            return 0, ""
        if prog == "brew" and cmd[1:3] == ["install", "ollama"]:
            self.present.add("ollama")
            return 0, ""
        if (prog == "brew" and "services" in cmd) or prog == "open":
            self.ollama_up = True
            return 0, ""
        if prog == "ditto":
            self.present.add("ollama-app")
            return 0, ""
        if prog == "ollama":
            if not self.ollama_up:
                return 1, "could not connect to ollama server"
            return 0, "NAME\nnomic-embed-text:latest" if "pull" in self.calls_flat() else "NAME\n"
        if prog not in self.present and not prog.startswith("python") and prog not in ("bash", "curl", "ditto", "open"):
            return 127, f"{prog}: command not found"
        return 0, ""

    def calls_flat(self):
        return " | ".join(" ".join(c) for c in self.calls)

    def ran(self, text):
        return text in self.calls_flat()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fresh machine: empty home, no Claude wiring, no kbhub data."""
    home = tmp_path / "home"
    (home / "Developer").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KBHUB_HOME", str(home / ".kbhub"))

    from kbhub import setup as _su

    def build(run=None, answers=None, yes=False, **kw):
        run = run or FakeRun()
        said = []
        feed = iter(answers or [])
        ui = _su.UI(yes=yes, ask=lambda prompt: next(feed), say=said.append)
        env = _su.Env(home=home, claude_home=home / ".claude", kb_home=home / ".kbhub", run=run, which=run.which,
                     sleep=lambda s: None, path=kw.pop("path", f"{home}/.local/bin:/usr/bin"),
                     apps_dir=home / "Applications", bin_dirs=[], **kw)
        return env, ui, run, said

    return home, build
