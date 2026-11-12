"""Finding, registering and scaffolding knowledge-base projects. Standard library only (the
SessionStart hook imports this module).

A project is a folder containing `.kb.toml`. Folders inside a git work tree, or inside an
excluded folder, are never projects: nothing is ever read from or written to a code repo."""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import kbregistry
from .config import GlobalConfig, ProjectConfig

MARKER = ".kb.toml"
SKIP_DIRS = {"node_modules"}
# Direct children of the home folder that never hold knowledge bases (and that macOS guards).
HOME_SKIP = {"Library", "Applications", "Movies", "Music", "Pictures", "Public"}


def skip_child(folder: Path, name: str, skip: set[str] = SKIP_DIRS) -> bool:
    """True for a sub-folder that walks never enter: hidden, in `skip`, or a system folder of home."""
    return name.startswith(".") or name in skip or (name in HOME_SKIP and folder == Path.home())
TEMPLATES = Path(__file__).parent / "templates"
# template file -> path inside a new project
LAYOUT = {
    "kb.toml": MARKER,
    "CLAUDE.md": "CLAUDE.md",
    "STATE.md": "STATE.md",
    "decisions.md": "knowledge-base/decisions.md",
    "lessons.md": "knowledge-base/lessons.md",
    "open-items.md": "knowledge-base/open-items.md",
}
LAYOUT_DIRS = ("knowledge-base/runbooks", "knowledge-base/research", "plans", "handovers")


@dataclass
class Project:
    name: str
    path: Path
    config: ProjectConfig
    scope: str = "work"            # "work" or "personal"
    repo: Path | None = None         # main clone, for knowledge bases registered inside a git repo
    source: str = "marker"           # "marker" (.kb.toml) or "registry" (registry.json)
    fingerprint: str = ""            # changes when the project's rules or scope change


# ------------------------------------------------------------------ path helpers

def _key(path: Path) -> tuple[int, int] | None:
    """Identity of a folder on disk, so `~/desktop/x` and `~/Desktop/x` compare equal."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _spellings(path: Path) -> tuple[Path, ...]:
    """The path as written and with symlinks resolved, so a link cannot hide where it leads."""
    lexical, real = Path(os.path.abspath(path)), Path(os.path.realpath(path))
    return (lexical,) if lexical == real else (lexical, real)


def is_within(path: Path, root: Path) -> bool:
    """True when `path` is `root` or lies inside it, as written or through symlinks (`path`
    itself need not exist yet)."""
    root_key = _key(root)
    if root_key is None:
        return False
    return any(_key(p) == root_key for base in _spellings(path) for p in (base, *base.parents))


def is_in_git_worktree(path: Path) -> bool:
    """True when `path`, as written or through symlinks, is inside a git work tree."""
    return any((p / ".git").exists() for base in _spellings(path) for p in (base, *base.parents))


def is_indexable(project_root: Path, path: Path) -> bool:
    """The one rule for which files belong to a project's knowledge: a markdown file whose real
    location is inside the project, not under a hidden folder, node_modules, a nested git repo
    or a nested project."""
    root, real = Path(os.path.realpath(project_root)), Path(os.path.realpath(path))
    try:
        parts = real.relative_to(root).parts
    except ValueError:
        return False
    if not parts or not parts[-1].lower().endswith(".md"):
        return False
    if any(part.startswith(".") or part in SKIP_DIRS for part in parts):
        return False
    folder = root
    for part in parts[:-1]:
        folder = folder / part
        if (folder / ".git").exists() or (folder / MARKER).exists():
            return False
    return True


def _excluded(cfg: GlobalConfig, path: Path) -> bool:
    return any(is_within(path, ex) for ex in cfg.exclude_dirs)


def _refuse_if_off_limits(cfg: GlobalConfig, path: Path) -> None:
    if is_in_git_worktree(path):
        raise ValueError(f"{path} is inside a git repository, where no .kb.toml is ever written; "
                         f"register it with `kbhub register {path}` instead")
    if _excluded(cfg, path):
        raise ValueError(f"{path} is inside an excluded folder (exclude_dirs in {cfg.path})")


# ------------------------------------------------------------------ discovery

def discover_projects(cfg: GlobalConfig, session_dir: Path | None = None) -> tuple[dict[str, Project], list[str]]:
    """Return (projects by name, warnings). See spec §4 and §14. `session_dir` is the folder a
    session works in: a knowledge base present in several checkouts of a repo resolves to that
    session's own checkout."""
    warnings: list[str] = []
    excluded = {k for k in map(_key, cfg.exclude_dirs) if k}
    found: dict[tuple[int, int], Path] = {}

    def walk(folder: Path, depth: int) -> None:
        key = _key(folder)
        try:
            if key is None or key in excluded or (folder / ".git").exists():
                return
            if (folder / MARKER).is_file():
                found.setdefault(key, folder)
        except OSError:      # a folder we may not look into
            return
        if depth >= cfg.scan_depth:
            return
        try:
            children = sorted(e.name for e in os.scandir(folder)
                              if e.is_dir(follow_symlinks=False) and not skip_child(folder, e.name))
        except OSError:
            return
        for name in children:
            walk(folder / name, depth + 1)

    for root in cfg.scan_roots:
        if not root.is_dir():
            continue
        if is_in_git_worktree(root):
            warnings.append(f"scan root {root} is inside a git repository; skipped")
            continue
        walk(root, 0)

    for folder in cfg.explicit_projects:
        key = _key(folder)
        if key is None or not (folder / MARKER).is_file():
            warnings.append(f"explicit project {folder} is missing (no folder or no {MARKER})")
        elif is_in_git_worktree(folder):
            warnings.append(f"explicit project {folder} is inside a git repository; not indexed")
        elif _excluded(cfg, folder):
            warnings.append(f"explicit project {folder} is inside an excluded folder; not indexed")
        else:
            found.setdefault(key, folder)

    projects: dict[str, Project] = {}

    def unique(name: str) -> str:
        base, n = name, 2
        while name in projects:
            name, n = f"{base}-{n}", n + 1
        return name

    # registered knowledge bases first (they may live inside git repos); they keep their names
    entries, registry_warnings = kbregistry.load_entries(cfg)
    warnings.extend(registry_warnings)
    registered: set[tuple[int, int]] = set()
    for entry in entries:
        folder = kbregistry.resolve_entry(entry, prefer=session_dir)
        if folder is None:
            where = entry.get("path") or f"{entry['repo']}/{entry['rel']}"
            warnings.append(f"registered knowledge base {entry['name']!r} is not present on any checkout ({where})")
            continue
        key = _key(folder)
        if key in registered:
            continue
        registered.add(key)
        pc = ProjectConfig.from_registry(entry)
        name = unique(entry["name"])
        projects[name] = Project(name, folder, pc, scope=entry["scope"],
                                 repo=Path(entry["repo"]) if "repo" in entry else None, source="registry",
                                 fingerprint=f"{pc.fingerprint}:{entry['scope']}")

    for folder in sorted(found.values(), key=str):
        if _key(folder) in registered:
            continue  # the registry entry describes this folder
        pc = ProjectConfig.load(folder / MARKER)
        if pc.error:
            warnings.append(f"bad {MARKER}, project indexed with defaults: {pc.error}")
        name = pc.name or folder.name
        if name in projects:
            name = f"{folder.parent.name}/{name}"
        name = unique(name)
        scope = resolve_scope(cfg, folder, pc.scope)
        projects[name] = Project(name, folder, pc, scope=scope, fingerprint=f"{pc.fingerprint}:{scope}")
    return projects, warnings


def resolve_scope(cfg: GlobalConfig, path: Path, explicit: str | None = None) -> str:
    """Scope of a folder: the explicit one, else personal under a personal root, else the default."""
    if explicit:
        return explicit
    if any(is_within(path, root) for root in cfg.personal_roots):
        return "personal"
    return cfg.default_scope


def session_scope(cfg: GlobalConfig, cwd: Path, projects: dict[str, Project]) -> str:
    """Scope a session works in: that of the project it sits in, else by folder. A folder that
    cannot be placed is `default_scope`, so personal knowledge never leaks by accident."""
    project = project_for_path(projects, cwd)
    return project.scope if project is not None else resolve_scope(cfg, cwd)


def repo_projects(projects: dict[str, Project], cwd: Path) -> list[Project]:
    """Knowledge bases registered for the repo that contains `cwd` (a worktree maps to its main clone)."""
    root = kbregistry.git_root(cwd)
    if root is None:
        return []
    key = _key(kbregistry.main_clone(root))
    return [p for p in projects.values() if p.repo is not None and key is not None and _key(p.repo) == key]


def project_for_path(projects: dict[str, Project], path: Path) -> Project | None:
    """The deepest project containing `path`."""
    inside = [p for p in projects.values() if is_within(path, p.path)]
    return max(inside, key=lambda p: len(p.path.parts), default=None)


def project_files(project: Project, cfg: GlobalConfig) -> tuple[dict[str, Path], list[str]]:
    """Markdown files of one project as {relative posix path: path}, plus warnings. Skips hidden
    folders, node_modules, nested projects, nested git repos and oversized files."""
    files: dict[str, Path] = {}
    warnings: list[str] = []
    for folder, dirs, names in os.walk(project.path):
        here = Path(folder)
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in SKIP_DIRS
                         and not (here / d / MARKER).exists() and not (here / d / ".git").exists())
        for name in sorted(names):
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            p = here / name
            if p.is_symlink() and not is_indexable(project.path, p):
                continue  # a link out of the project (e.g. into a code repo) is never indexed
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = p.relative_to(project.path).as_posix()
            if size > cfg.max_file_bytes:
                warnings.append(f"{project.name}: skipped {rel} ({size} bytes is over max_file_bytes)")
                continue
            files[rel] = p
    return files, warnings


# ------------------------------------------------------------------ registration

def _find(cfg: GlobalConfig, folder: Path) -> Project | None:
    key = _key(folder)
    return next((p for p in discover_projects(cfg)[0].values() if _key(p.path) == key), None)


def _persist_explicit(cfg: GlobalConfig, folder: Path) -> GlobalConfig:
    """Add `folder` to explicit_projects in config.toml (created from the defaults if absent)."""
    if cfg.path.is_file():
        text = cfg.path.read_text(encoding="utf-8")
    else:
        example = Path(__file__).parent.parent / "config.example.toml"
        text = example.read_text(encoding="utf-8") if example.is_file() else ""
    current = tomllib.loads(text).get("explicit_projects", [])
    line = "explicit_projects = [" + ", ".join(json.dumps(v) for v in [*current, str(folder)]) + "]"
    pattern = re.compile(r"^explicit_projects\s*=\s*\[.*?\]", re.M | re.S)
    if pattern.search(text):
        new = pattern.sub(lambda _: line, text, count=1)
    else:  # top-level keys must come before the first [table]
        table = re.search(r"^\[", text, re.M)
        at = table.start() if table else len(text)
        head = text[:at]
        new = head + ("" if not head or head.endswith("\n") else "\n") + line + "\n" + ("\n" if table else "") + text[at:]
    if tomllib.loads(new).get("explicit_projects") != [*current, str(folder)]:
        raise ValueError(f"could not update explicit_projects in {cfg.path}; add {folder} by hand")
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.path.write_text(new, encoding="utf-8")
    reloaded = GlobalConfig.load(cfg.path)
    # keep any in-memory overrides of where to scan (the file is the source for the rest)
    reloaded.scan_roots, reloaded.scan_depth, reloaded.exclude_dirs = cfg.scan_roots, cfg.scan_depth, cfg.exclude_dirs
    return reloaded


def _register(cfg: GlobalConfig, folder: Path) -> Project:
    project = _find(cfg, folder)
    if project is None:  # outside the scan roots, or deeper than scan_depth
        project = _find(_persist_explicit(cfg, folder), folder)
    if project is None:
        raise ValueError(f"{folder} could not be registered as a knowledge-base project")
    return project


def add_project(cfg: GlobalConfig, path: Path, name: str | None = None) -> Project:
    """Register an existing folder as a KB project. Refuses git work trees and excluded folders."""
    folder = Path(os.path.abspath(os.path.expanduser(str(path))))
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder")
    _refuse_if_off_limits(cfg, folder)
    marker = folder / MARKER
    if not marker.exists():
        marker.write_text(f"name = {json.dumps(name or folder.name)}\n", encoding="utf-8")
    return _register(cfg, folder)


def new_project(cfg: GlobalConfig, name: str, parent: Path | None = None) -> Project:
    """Create the standard KB layout (spec §9) in <parent>/<name> and register it."""
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise ValueError(f"invalid project name {name!r}: use a plain folder name")
    base = Path(os.path.abspath(os.path.expanduser(str(parent)))) if parent else cfg.new_project_parent
    folder = base / name
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise ValueError(f"{folder} already exists and is not empty")
    _refuse_if_off_limits(cfg, folder)
    values = {"{{name}}": name, "{{date}}": date.today().isoformat()}
    for sub in LAYOUT_DIRS:
        (folder / sub).mkdir(parents=True, exist_ok=True)
    for template, rel in LAYOUT.items():
        text = (TEMPLATES / template).read_text(encoding="utf-8")
        for token, value in values.items():
            text = text.replace(token, value)
        (folder / rel).write_text(text, encoding="utf-8")
    return _register(cfg, folder)
