"""The setup wizard: `~/.kbhub/setup` (or `kbhub setup`). Installs what is missing, asks where
your projects live, connects kbhub to Claude Code and builds the first index. Every question has
a default, so pressing Enter all the way through works; `--yes` does that for you.

    kbhub setup [--yes] [--wire-only]
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from . import doctor, install
from .config import ConfigError, GlobalConfig, engine_home
from .install import OLLAMA_MODEL, PATH_LINE, REPO
from .system import Env

STEPS = 6
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"       # Anthropic's native installer
OLLAMA_ZIP = "https://ollama.com/download/Ollama-darwin.zip"
WORK_FOLDERS = ("Desktop/projects", "Developer", "code", "Code", "Projects", "projects", "src", "dev",
                "repos", "workspace")


class Cancelled(Exception):
    """The person closed the input (Ctrl-D) or it ran out: stop, never guess a yes."""


class UI:
    """Prints progress and asks questions. With `yes` every default is taken without asking."""

    def __init__(self, yes: bool = False, ask=input, say=print):
        self.yes, self.ask, self.say = yes, ask, say

    def step(self, n: int, title: str) -> None:
        self.say(f"\n[{n}/{STEPS}] {title}")

    def ok(self, text: str) -> None:
        self.say(f"  ✓ {text}")

    def note(self, text: str) -> None:
        self.say(f"  • {text}")

    def warn(self, text: str) -> None:
        self.say(f"  ! {text}")

    def question(self, text: str, default: str = "") -> str:
        if self.yes:
            return default
        try:
            answer = self.ask(f"  ? {text}{f' [{default}]' if default else ''}: ").strip()
        except (EOFError, StopIteration):
            raise Cancelled from None
        return answer or default

    def confirm(self, text: str, default: bool = True) -> bool:
        answer = self.question(f"{text} ({'Y/n' if default else 'y/N'})").lower()
        return default if not answer else answer.startswith("y")


def _last(out: str) -> str:
    return out.splitlines()[-1] if out else "no output"


# ------------------------------------------------------------------ steps

def _claude(env: Env, ui: UI, allow_install: bool) -> str | None:
    exe = env.find("claude")
    if exe:
        ui.ok(f"Claude Code is installed ({exe})")
        return exe
    if not allow_install:
        return None
    ui.note("Claude Code is not installed. kbhub works through it.")
    if ui.confirm("Install Claude Code now with Anthropic's installer?"):
        code, out = env.run(["bash", "-c", CLAUDE_INSTALL], timeout=1800)
        exe = env.find("claude")
        if code == 0 and exe:
            ui.ok(f"Claude Code installed ({exe})")
            return exe
        ui.warn(f"Claude Code could not be installed ({_last(out)}).")
    ui.warn("Carrying on without Claude Code. Install it, then run `kbhub setup` again to connect it.")
    return None


def _ollama_ready(env: Env, exe: str) -> bool:
    for _ in range(30):
        if env.run([exe, "list"])[0] == 0:
            return True
        env.sleep(1)
    return False


def _ollama(env: Env, ui: UI, allow_install: bool) -> bool:
    """Make Ollama and its model ready for searching by meaning. False means: use the offline model."""
    exe = env.ollama()
    if exe is None:
        if not allow_install:
            return False
        ui.note("Best search (by meaning, not only by words) uses Ollama, a free app that runs on this Mac.")
        ui.note("It needs about 1.5 GB. Without it kbhub uses a smaller built-in model: no install, weaker search.")
        if not ui.confirm("Install Ollama?"):
            return False
        brew = env.find("brew")
        if brew:
            code, out = env.run([brew, "install", "ollama"], timeout=1800)
            if code == 0:
                env.run([brew, "services", "start", "ollama"])
        else:
            with tempfile.TemporaryDirectory() as tmp:
                archive = str(Path(tmp) / "Ollama.zip")
                code, out = env.run(["curl", "-fL", "--silent", "--show-error", "-o", archive, OLLAMA_ZIP], timeout=1800)
                if code == 0:
                    env.apps_dir.mkdir(parents=True, exist_ok=True)
                    code, out = env.run(["ditto", "-xk", archive, str(env.apps_dir)])
            if code == 0:
                env.run(["open", "-g", str(env.ollama_app)])
        if code != 0:
            ui.warn(f"Ollama could not be installed ({_last(out)}).")
            return False
        exe = env.ollama() or str(env.ollama_app / "Contents" / "Resources" / "ollama")
    if env.run([exe, "list"])[0] != 0 and allow_install:      # installed but not running: start it
        if env.ollama_app.is_dir():
            env.run(["open", "-g", str(env.ollama_app)])
        elif env.find("brew"):
            env.run([env.find("brew"), "services", "start", "ollama"])
    if not (_ollama_ready(env, exe) if allow_install else env.run([exe, "list"])[0] == 0):
        ui.warn("Ollama is installed but not answering.")
        return False
    code, out = env.run([exe, "list"])
    if OLLAMA_MODEL not in out:
        ui.note(f"Downloading the search model ({OLLAMA_MODEL}, about 270 MB)…")
        code, out = env.run([exe, "pull", OLLAMA_MODEL], timeout=3600)
        if code != 0:
            ui.warn(f"The search model could not be downloaded ({_last(out)}).")
            return False
    ui.ok("Ollama and the search model are ready")
    return True


def _clean_path(env: Env, answer: str) -> Path:
    """A folder as people type or drag it in: quoted, with `\\ ` for spaces, `~`, or relative to home."""
    s = answer.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1].strip()
    s = s.replace("\\ ", " ")
    if s == "~" or s.startswith("~/"):
        s = str(env.home) + s[1:]
    path = Path(s)
    return Path(os.path.normpath(path if path.is_absolute() else env.home / path))


def _folder(env: Env, ui: UI, text: str, default: Path | None, must_exist: bool = True) -> Path | None:
    """Ask for a folder, up to three times. Enter takes `default` (None = no folder)."""
    for _ in range(3):
        answer = ui.question(text, _short(env, default) if default else "")
        if not answer:
            return None
        path = _clean_path(env, answer)
        if path.is_dir() or (not must_exist and not path.exists()) or path == default:
            return path
        ui.warn(f"{_short(env, path)} is not a folder." if path.exists() else f"{_short(env, path)} does not exist.")
    ui.note(f"Using {_short(env, default)}." if default else "Leaving it empty.")
    return default


def _short(env: Env, path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(env.home)) if path != env.home else "~"
    except ValueError:
        return str(path)


def _write_config(env: Env, ui: UI, backend: str, ask: bool) -> None:
    default_work = next((env.home / f for f in WORK_FOLDERS if (env.home / f).is_dir()), env.home)
    notes_default = env.home / "knowledge-bases"
    work, personal, notes = default_work, None, notes_default
    if ask:
        work = _folder(env, ui, "Where do your projects (code, research) live?", default_work) or default_work
        personal = _folder(env, ui, "Folder of personal projects, kept apart from work (Enter for none)?", None)
        notes = _folder(env, ui, "Where should your knowledge bases go (your notes are kept here, outside your projects)?",
                        notes_default, must_exist=False) or notes_default
    roots = [str(p) for p in dict.fromkeys(p for p in (work, personal) if p)]
    lines = [
        "# kbhub settings, written by `kbhub setup`. Every possible key is explained in",
        f"# {REPO / 'config.example.toml'}",
        "",
        "# Where kbhub looks for knowledge-base folders inside your projects (`kbhub scan`).",
        f"kb_scan_roots = {json.dumps(roots)}",
        "",
        "# Where it looks for standalone knowledge bases (folders with a .kb.toml).",
        f"scan_roots = {json.dumps(list(dict.fromkeys(roots + [str(notes)])))}",
        "",
        "# Where new knowledge bases are created: <project>/<topic> for a code project.",
        f"new_project_parent = {json.dumps(str(notes))}",
        "",
        "# Knowledge bases under these folders are personal; everything else is work.",
        f"personal_roots = {json.dumps([str(personal)] if personal else [])}",
        'default_scope = "work"',
        "",
        "[embedding]",
        '# "ollama" = best search, needs the Ollama app. "wordllama" = built-in offline model.',
        f'backend = "{backend}"',
        f'ollama_model = "{OLLAMA_MODEL}"',
        'ollama_url = "http://localhost:11434"',
        "",
    ]
    env.kb_home.mkdir(parents=True, exist_ok=True)
    (env.kb_home / "config.toml").write_text("\n".join(lines), encoding="utf-8")
    ui.ok(f"Settings saved to {env.kb_home / 'config.toml'}")


def _existing_backend(env: Env) -> str | None:
    path = env.kb_home / "config.toml"
    if not path.is_file():
        return None
    return GlobalConfig.load(path).embedding.get("backend")


def _connect(env: Env, ui: UI, claude: str | None, offer_path: bool = True) -> None:
    log: list[str] = []
    install._step_skill(env.claude_home, log)
    install._step_hook(env.claude_home, log)
    install._step_claude_md(env.claude_home, log)
    uv = env.find("uv") or "uv"
    if claude:
        install._step_mcp(log, run=env.run, uv=uv, claude=claude)
    install._step_launcher(env.user_bin, uv, log)
    for line in log:
        (ui.warn if "FAILED" in line or "left alone" in line else ui.ok)(line.split(": ", 1)[-1])
    if offer_path and str(env.user_bin) not in env.path.split(os.pathsep):
        _put_on_path(env, ui)


def _put_on_path(env: Env, ui: UI) -> None:
    rc = env.shell_rc
    current = rc.read_text(encoding="utf-8", errors="replace") if rc.is_file() else ""
    if PATH_LINE in current:
        ui.note(f"The `kbhub` command works in new Terminal windows (PATH is set in {_short(env, rc)}).")
        return
    ui.note(f"{_short(env, env.user_bin)} is not on your PATH, so typing `kbhub` will not work yet.")
    if ui.confirm(f"Add it to {_short(env, rc)} for you?"):
        with rc.open("a", encoding="utf-8") as f:
            f.write(("" if not current or current.endswith("\n") else "\n") + f"\n# added by kbhub setup\n{PATH_LINE}\n")
        ui.ok(f"Added to {_short(env, rc)}; the `kbhub` command works in new Terminal windows (PATH updated)")
    else:
        ui.warn(f"To use the `kbhub` command, add this line to {_short(env, rc)}:  {PATH_LINE}")


def _register_found(env: Env, ui: UI) -> None:
    from . import manage
    cfg = GlobalConfig.load(env.kb_home / "config.toml")
    new = [c for c in manage.scan(cfg) if not c["registered"]]
    if not new:
        ui.note("No existing knowledge-base folders found. That is fine: you will create your first one below.")
        return
    ui.note(f"Found {len(new)} folder(s) of notes that look like knowledge bases:")
    for c in new:
        label = f"{c['name']} ({c['scope']}, {c['files']} file(s)) in {_short(env, Path(c['path']))}"
        if ui.confirm(f"Add {label} to the index?"):
            try:
                manage.register_kb(cfg, c["path"], name=c["name"], scope=c["scope"])
                ui.ok(f"Added {c['name']}")
            except ValueError as exc:
                ui.warn(f"Skipped {c['name']}: {exc}")


def run_wizard(env: Env, ui: UI, wire_only: bool = False) -> int:
    """`wire_only` (used by `kbhub update`) asks nothing and installs nothing: it only reconnects."""
    if wire_only:
        ui.yes = True
    plain_say, home = ui.say, str(env.home)
    ui.say = lambda text: plain_say(str(text).replace(home, "~"))      # short, readable paths
    install._load_settings(env.claude_home / "settings.json")   # fail before changing anything if it is broken

    ui.step(1, "Claude Code")
    claude = _claude(env, ui, allow_install=not wire_only)

    ui.step(2, "Search quality")
    existing = _existing_backend(env)
    if wire_only and existing == "wordllama":
        ui.ok("Using the built-in offline model, as your settings say")
        backend = "wordllama"
    else:
        ready = _ollama(env, ui, allow_install=not wire_only)
        backend = existing or ("ollama" if ready else "wordllama")
        if existing == "wordllama" and ready:
            path = env.kb_home / "config.toml"
            path.write_text(re.sub(r'(?m)^(\s*backend\s*=\s*)"wordllama"', r'\1"ollama"',
                                   path.read_text(encoding="utf-8")), encoding="utf-8")
            backend = "ollama"
            ui.ok("Search now uses Ollama (the index rebuilds once)")
        elif backend == "wordllama":
            ui.note("Using the built-in offline model. Search works; it matches meaning less well. "
                    "Run `kbhub setup` again any time to add Ollama.")
        elif not ready:
            ui.warn("Your settings say Ollama, but it is not available right now, so searches match words only "
                    "until it is running. Open the Ollama app, or run `kbhub setup` again.")

    ui.step(3, "Your folders")
    if existing:
        ui.ok(f"Keeping your settings in {env.kb_home / 'config.toml'} (edit that file to change folders)")
    else:
        _write_config(env, ui, backend, ask=not wire_only)

    ui.step(4, "Connect to Claude Code")
    _connect(env, ui, claude, offer_path=not wire_only)

    ui.step(5, "Existing knowledge bases")
    if wire_only:
        ui.note("Skipped (run `kbhub scan` to look for new folders).")
    else:
        _register_found(env, ui)

    ui.step(6, "Build the index and check everything")
    log: list[str] = []
    install._step_index(env.kb_home, log, run=env.run)
    for line in log:
        (ui.warn if "FAILED" in line else ui.note)(line.split(": ", 1)[-1] if line.startswith("index:") else line)
    lines, ok = doctor.report(doctor.checks(env))
    for line in lines:
        ui.say(line)

    ui.say("\nYou're set." if ok else "\nSetup finished, with the items above still to fix.")
    ui.say("  Next: open Claude Code in one of your projects and say:")
    ui.say('        "create a knowledge base for <topic>"')
    ui.say("  Later: `kbhub doctor` checks the install, `kbhub update` gets the newest version,")
    ui.say("         `kbhub setup` runs this again, `kbhub uninstall` removes it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    unknown = [a for a in args if a not in ("--yes", "-y", "--wire-only")]
    if unknown:
        print(f"usage: kbhub setup [--yes] [--wire-only]   (unknown: {' '.join(unknown)})", file=sys.stderr)
        return 2
    if sys.platform != "darwin" and not os.environ.get("KBHUB_ANY_PLATFORM"):
        print("kbhub supports macOS only for now.", file=sys.stderr)
        return 1
    if os.path.realpath(REPO) != os.path.realpath(engine_home()):
        print(f"kbhub must live in {engine_home()} (this copy is in {REPO}).\n"
              f"Move it there and run setup again:  mv {REPO} {engine_home()}", file=sys.stderr)
        return 1
    yes = "--yes" in args or "-y" in args
    ui = UI(yes=yes)
    ui.say("kbhub setup. Press Enter to accept the suggestion in [brackets].")
    try:
        return run_wizard(Env.real(), ui, wire_only="--wire-only" in args)
    except (ValueError, ConfigError) as exc:
        print(f"\nSetup stopped: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, Cancelled):
        print("\nSetup cancelled part-way. Run it again to finish; it picks up where it stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
