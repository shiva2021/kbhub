"""`kbhub doctor`: check that every part of the install is in place and say how to fix what is not."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from .install import (BLOCK_START, LAUNCHER_MARK, MCP_NAME, OLLAMA_MODEL, PATH_LINE, REPO, SKILL_NAME, _is_our_hook,
                      _session_start_entries)
from .system import Env


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    required: bool = True


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def checks(env: Env) -> list[Check]:
    from .config import ConfigError, GlobalConfig
    out: list[Check] = []
    rerun = "run `kbhub setup`"

    uv = env.find("uv")
    out.append(Check("uv (runs kbhub)", bool(uv), uv or "not found", rerun))
    claude = env.find("claude")
    out.append(Check("Claude Code", bool(claude), claude or "not found",
                     "install Claude Code (https://claude.com/claude-code), then " + rerun))
    if claude:
        code, text = env.run([claude, "mcp", "get", MCP_NAME])
        out.append(Check("search tools connected to Claude Code", code == 0,
                         "MCP server registered" if code == 0 else "MCP server not registered", rerun))

    try:
        settings = json.loads(_read(env.claude_home / "settings.json") or "{}")
    except ValueError:
        settings = {}
    hooked = isinstance(settings, dict) and any(_is_our_hook(e) for e in _session_start_entries(settings))
    out.append(Check("session-start hook", hooked, "" if hooked else "not in settings.json", rerun))
    link = env.claude_home / "skills" / SKILL_NAME
    linked = link.is_symlink() and os.path.realpath(link) == os.path.realpath(REPO / "skill" / SKILL_NAME)
    out.append(Check("skill", linked, "" if linked else f"{link} is missing or points elsewhere", rerun))
    ruled = BLOCK_START in _read(env.claude_home / "CLAUDE.md")
    out.append(Check("rule in CLAUDE.md", ruled, "" if ruled else "block not found", rerun))
    launcher = env.user_bin / "kbhub"
    on_path = str(env.user_bin) in env.path.split(os.pathsep)
    in_rc = PATH_LINE in _read(env.shell_rc)
    has_launcher = launcher.is_file() and LAUNCHER_MARK in _read(launcher)
    out.append(Check("`kbhub` command", has_launcher and (on_path or in_rc),
                     ("works in new Terminal windows" if has_launcher and in_rc and not on_path else "")
                     if has_launcher and (on_path or in_rc) else
                     (f"{env.user_bin} is not on your PATH" if has_launcher else f"{launcher} not found"),
                     f"add `{PATH_LINE}` to {env.shell_rc} and open a new terminal"
                     if has_launcher else rerun, required=False))

    backend = None
    try:
        cfg = GlobalConfig.load(env.kb_home / "config.toml")
        backend = cfg.embedding.get("backend")
        exists = (env.kb_home / "config.toml").is_file()
        out.append(Check("settings (config.toml)", exists, str(cfg.path) if exists else "no config.toml yet", rerun))
    except ConfigError as exc:
        out.append(Check("settings (config.toml)", False, str(exc), "fix that line in config.toml"))

    if backend == "ollama":
        exe = env.ollama()
        code, text = env.run([exe, "list"]) if exe else (127, "")
        ready = code == 0 and OLLAMA_MODEL in text
        detail = ("Ollama with " + OLLAMA_MODEL if ready else "Ollama is not installed" if not exe
                  else "Ollama is not running" if code != 0 else f"model {OLLAMA_MODEL} is not downloaded")
        out.append(Check("search quality", ready, detail + ("" if ready else "; searches match words only until fixed"),
                         "open the Ollama app" if exe and code != 0 else rerun, required=False))
    elif backend:
        out.append(Check("search quality", True, f"built-in offline model ({backend})", required=False))

    manifest = env.kb_home / "db" / "manifest.json"
    if manifest.is_file():
        code, text = env.run([sys.executable, "-m", "kbhub.cli", "projects", "--no-sync", "--scope", "all"],
                             env={**os.environ, "KBHUB_HOME": str(env.kb_home)})
        names = [l for l in text.splitlines() if l.strip() and not l.startswith("[")] if code == 0 else []
        out.append(Check("index", code == 0, f"{len(names)} knowledge base(s) indexed" if code == 0
                         else (text.splitlines()[-1] if text else "cannot be read"), "run `kbhub index --rebuild`"))
    else:
        out.append(Check("index", False, "not built yet", "run `kbhub index`"))
    return out


def report(results: list[Check]) -> tuple[list[str], bool]:
    """Lines to print, and whether every required check passed."""
    lines = []
    for c in results:
        mark = "✓" if c.ok else ("✗" if c.required else "!")
        lines.append(f"  {mark} {c.name}" + (f": {c.detail}" if c.detail else ""))
        if not c.ok and c.fix:
            lines.append(f"      fix: {c.fix}")
    ok = all(c.ok for c in results if c.required)
    lines.append("Everything needed is in place." if ok else "Some things need attention (see the fixes above).")
    return lines, ok
