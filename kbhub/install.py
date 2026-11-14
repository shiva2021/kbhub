"""Wire kbhub into Claude Code: config, skill link, SessionStart hook, CLAUDE.md block, MCP
server registration and a first index. Idempotent; `uninstall` reverses everything except the
config and the index.

    uv run python -m kbhub.install install
    uv run python -m kbhub.install uninstall
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import prefs

REPO = Path(__file__).resolve().parent.parent      # the engine repo (code, skill, hook)
SKILL_NAME = "kbhub"
MCP_NAME = "kbhub"
OLLAMA_MODEL = "nomic-embed-text"
BLOCK_START = "<!-- kbhub:start -->"
BLOCK_END = "<!-- kbhub:end -->"
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'


def _block_re(name: str) -> re.Pattern:
    """One complete start…end block. It never runs across a second start marker, so a stray,
    unclosed marker cannot make it swallow the user's own text."""
    start, end = re.escape(f"<!-- {name}:start -->"), re.escape(f"<!-- {name}:end -->")
    return re.compile(f"{start}(?:(?!{start}).)*?{end}", re.S)


_BLOCK_RE = _block_re("kbhub")


def _strip_blocks(text: str, pattern: re.Pattern) -> str:
    """`text` without any complete block, keeping one blank line where a block stood."""
    while (m := pattern.search(text)):
        before, after = text[:m.start()].rstrip("\n"), text[m.end():].lstrip("\n")
        text = f"{before}\n\n{after}" if before and after else (before + "\n" if before else after)
    return text


def _backup(path: Path) -> None:
    """Copy of a user's file as it was just before kbhub changed it (the newest change only)."""
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".kbhub-backup"))


def _without_hooks(entries: list, is_ours) -> list:
    """SessionStart entries minus the hook commands `is_ours` picks; other commands in the same
    entry, and entries of shapes we do not know, are kept as they are."""
    out = []
    for entry in entries:
        hooks = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(hooks, list):
            out.append(entry)
            continue
        kept = [h for h in hooks if not (isinstance(h, dict) and is_ours(h))]
        if len(kept) == len(hooks):
            out.append(entry)
        elif kept:
            out.append({**entry, "hooks": kept})
    return out


def _is_our_command(hook: dict) -> bool:
    return str(REPO / "hooks" / "session_start.py") in str(hook.get("command", ""))


def _run(cmd: list[str], env: dict | None = None, timeout: int = 900) -> tuple[int, str]:
    """Run a command; returns (exit code, combined output). A missing program is exit 127."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)}: timed out"


def _hook_command() -> str:
    return f"{shlex.quote(str(REPO / '.venv' / 'bin' / 'python'))} {shlex.quote(str(REPO / 'hooks' / 'session_start.py'))}"


def _is_our_hook(entry: dict) -> bool:
    marker = str(REPO / "hooks" / "session_start.py")
    return any(marker in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix it and run the installer again") from None
    if not isinstance(settings, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = settings.get("hooks")
    if "hooks" in settings and not isinstance(hooks, dict):
        raise ValueError(f'{path}: "hooks" is not an object; fix it by hand and run this again')
    if isinstance(hooks, dict) and "SessionStart" in hooks and not isinstance(hooks["SessionStart"], list):
        raise ValueError(f'{path}: "hooks.SessionStart" is not a list; fix it by hand and run this again')
    return settings


def _save_settings(path: Path, settings: dict, original: dict) -> bool:
    if settings == original and path.exists():
        return False
    _backup(path)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def _session_start_entries(settings: dict) -> list:
    hooks = settings.get("hooks")
    entries = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


# ------------------------------------------------------------------ install steps

def _step_config(kb_home: Path, check_ollama: bool, log: list[str]) -> None:
    config = kb_home / "config.toml"
    created = not config.exists()
    if created:
        kb_home.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "config.example.toml", config)
        log.append(f"config: wrote {config}")
    else:
        log.append(f"config: kept existing {config}")
    if not check_ollama:
        return
    code, out = _run(["ollama", "list"])
    if code == 0:
        if OLLAMA_MODEL in out:
            log.append(f"ollama: model {OLLAMA_MODEL} is ready")
            return
        code, out = _run(["ollama", "pull", OLLAMA_MODEL])
        if code == 0:
            log.append(f"ollama: model {OLLAMA_MODEL} is ready")
            return
        log.append(f"ollama: could not pull {OLLAMA_MODEL} ({out.splitlines()[-1] if out else 'no output'})")
    if not created:  # never change an embedding backend the user already has
        log.append("ollama: not reachable right now; existing config left unchanged "
                   "(searches fall back to keywords until Ollama answers)")
        return
    text = config.read_text(encoding="utf-8")
    new = re.sub(r'(?m)^(\s*backend\s*=\s*)"ollama"', r'\1"wordllama"', text)
    if new != text:
        config.write_text(new, encoding="utf-8")
    log.append('ollama: not available, so config uses backend = "wordllama" (offline model). '
               'Install Ollama and set backend = "ollama" for better search quality.')


def _step_skill(claude_home: Path, log: list[str]) -> None:
    link = claude_home / "skills" / SKILL_NAME
    target = REPO / "skill" / SKILL_NAME
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        log.append(f"skill: {link} exists and is not a symlink; left alone (remove it to use the kbhub skill)")
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    log.append(f"skill: linked {link} -> {target}")


def _step_hook(claude_home: Path, log: list[str]) -> None:
    path = claude_home / "settings.json"
    original = _load_settings(path)
    settings = json.loads(json.dumps(original))
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    entries = _without_hooks(hooks.get("SessionStart", []), _is_our_command)
    entries.append({"hooks": [{"type": "command", "command": _hook_command()}]})
    hooks["SessionStart"] = entries
    changed = _save_settings(path, settings, original)
    log.append(f"hook: SessionStart hook {'installed in' if changed else 'already in'} {path}")


def _step_claude_md(claude_home: Path, log: list[str]) -> None:
    path = claude_home / "CLAUDE.md"
    block = (REPO / "claude_md_block.md").read_text(encoding="utf-8").strip()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    first = _BLOCK_RE.search(text)
    if first:   # replace the first copy in place and drop any later ones
        slot = "\x00kbhub-block\x00"
        new = _strip_blocks(text[:first.start()] + slot + text[first.end():], _BLOCK_RE).replace(slot, block)
    elif text.strip():
        new = text.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new = block + "\n"
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup(path)
        path.write_text(new, encoding="utf-8")
    log.append(f"rule: kbhub block {'written to' if new != text else 'already in'} {path}")


def _step_mcp(log: list[str], run=None, uv: str | None = None, claude: str = "claude") -> bool:
    """Register the MCP server. `uv` and `claude` should be absolute paths when they were only just
    installed: Claude Code starts the server without the shell's PATH."""
    run = run or _run
    run([claude, "mcp", "remove", MCP_NAME, "--scope", "user"])  # fine if it was not registered
    uv = uv or shutil.which("uv") or "uv"
    code, out = run([claude, "mcp", "add", MCP_NAME, "--scope", "user", "--",
                     uv, "run", "--project", str(REPO), "kbhub-server"])  # --project keeps the session's folder
    log.append(f"mcp: registered server {MCP_NAME} at user scope" if code == 0
               else f"mcp: FAILED to register {MCP_NAME}: {out}")
    return code == 0


def _step_index(kb_home: Path, log: list[str], run=None) -> None:
    run = run or _run
    env = {**os.environ, "KBHUB_HOME": str(kb_home)}
    code, out = run([sys.executable, "-m", "kbhub.cli", "index"], env=env)
    if code != 0:
        log.append(f"index: FAILED ({out.splitlines()[-1] if out else 'no output'}); run `kbhub index` to retry")
        return
    code, out = run([sys.executable, "-m", "kbhub.cli", "projects"], env=env)
    listing = [l for l in out.splitlines() if not l.startswith("[")] if code == 0 else []
    log.append("index: built. Projects:" if listing else "index: built. No knowledge-base projects found yet.")
    log.extend(f"  {l}" for l in listing)


LAUNCHER_MARK = "# kbhub launcher (written by `kbhub setup`; safe to delete)"


def _step_launcher(bin_dir: Path, uv: str, log: list[str]) -> None:
    """Put a `kbhub` command in `bin_dir` so it works from any folder."""
    path = Path(bin_dir) / "kbhub"
    if path.exists() and LAUNCHER_MARK not in path.read_text(encoding="utf-8", errors="replace"):
        log.append(f"command: {path} exists and is not ours; left alone")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\n{LAUNCHER_MARK}\n'
                    f'exec {shlex.quote(uv)} run --project {shlex.quote(str(REPO))} kbhub "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    log.append(f"command: `kbhub` installed at {path}")


def _remove_launcher(bin_dir: Path, log: list[str]) -> None:
    path = Path(bin_dir) / "kbhub"
    if path.is_file() and LAUNCHER_MARK in path.read_text(encoding="utf-8", errors="replace"):
        path.unlink()
        log.append(f"command: removed {path}")


def install(claude_home: Path, kb_home: Path, run_claude: bool = True, check_ollama: bool = True,
            run_index: bool = True) -> list[str]:
    """Install into `claude_home` (normally ~/.claude). Returns a human-readable step log."""
    claude_home, kb_home = Path(claude_home), Path(kb_home)
    log: list[str] = []
    _load_settings(claude_home / "settings.json")  # fail before changing anything if it is broken
    _step_config(kb_home, check_ollama, log)
    _step_skill(claude_home, log)
    _step_hook(claude_home, log)
    _step_claude_md(claude_home, log)
    if run_claude:
        _step_mcp(log)
    if run_index:
        _step_index(kb_home, log)
    return log


def uninstall(claude_home: Path, kb_home: Path, run_claude: bool = True, bin_dir: Path | None = None,
              run=None, claude: str = "claude") -> list[str]:
    """Remove the skill link, hook, CLAUDE.md block, MCP registration and the `kbhub` command.
    Keeps config.toml, the registry and db/."""
    claude_home, kb_home = Path(claude_home), Path(kb_home)
    log: list[str] = []
    path = claude_home / "settings.json"
    original = _load_settings(path)          # a broken settings.json stops us before anything is removed
    if bin_dir is not None:
        _remove_launcher(bin_dir, log)

    link = claude_home / "skills" / SKILL_NAME
    if link.is_symlink():
        link.unlink()
        log.append(f"skill: removed link {link}")

    settings = json.loads(json.dumps(original))
    entries = _session_start_entries(settings)
    if any(_is_our_hook(e) for e in entries):
        hooks = settings["hooks"]
        hooks["SessionStart"] = _without_hooks(hooks["SessionStart"], _is_our_command)
        if not hooks["SessionStart"]:
            del hooks["SessionStart"]
        if not hooks:
            del settings["hooks"]
        _save_settings(path, settings, original)
        log.append(f"hook: removed SessionStart hook from {path}")

    md = claude_home / "CLAUDE.md"
    text = md.read_text(encoding="utf-8") if md.exists() else ""
    if _BLOCK_RE.search(text):
        _backup(md)
        new = _strip_blocks(text, _BLOCK_RE)
        if new.strip():
            md.write_text(new, encoding="utf-8")
        else:
            md.unlink()
        log.append(f"rule: removed kbhub block from {md}")

    if run_claude:
        code, out = (run or _run)([claude, "mcp", "remove", MCP_NAME, "--scope", "user"])
        log.append(f"mcp: removed server {MCP_NAME}" if code == 0 else f"mcp: nothing removed ({out})")
    log.append(f"kept: your settings, registry and index in {kb_home} (delete that folder to remove everything)")
    log.append(f"kept: your per-project answers in {prefs.prefs_home()}")
    return log


def main(argv: list[str] | None = None) -> int:
    from .config import engine_home
    args = sys.argv[1:] if argv is None else argv
    if args not in (["install"], ["uninstall"]):
        print("usage: python -m kbhub.install {install|uninstall}", file=sys.stderr)
        return 2
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        steps = (install(claude_home, engine_home()) if args == ["install"]
                 else uninstall(claude_home, engine_home(), bin_dir=Path.home() / ".local" / "bin"))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
