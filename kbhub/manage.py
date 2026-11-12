"""Creating, registering and finding knowledge bases wherever they live, including inside git
repos. Registered knowledge bases are recorded in the central registry (`kbregistry`); nothing is
ever written into a repo except the knowledge files themselves, and nothing is staged or committed."""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from . import kbregistry
from .config import SCOPES, STATUSES, GlobalConfig, ProjectConfig
from .registry import (MARKER, SKIP_DIRS, skip_child, Project, _key, discover_projects, is_within, new_project,
                       project_for_path, resolve_scope)

KB_DIR = "knowledge-base"
REPO_TEMPLATES = Path(__file__).parent / "templates" / "repo"
REPO_LAYOUT_DIRS = ("runbooks", "research", "plans", "handovers")


def _abs(path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _check_scope(scope: str | None) -> None:
    if scope is not None and scope not in SCOPES:
        raise ValueError(f"scope must be one of: {', '.join(SCOPES)}")


def _identity(folder: Path) -> dict:
    """How the registry names a folder: main clone + relative path inside a repo, else the path."""
    root = kbregistry.git_root(folder)
    if root is None:
        return {"path": str(folder)}
    return {"repo": str(kbregistry.main_clone(root)), "rel": folder.relative_to(root).as_posix()}


def _same_place(a: str, b: str) -> bool:
    """Same folder on disk, however it is spelled (`~/desktop` vs `~/Desktop`, symlinks)."""
    if a == b:
        return True
    key = _key(Path(a))
    return key is not None and key == _key(Path(b))


def _same(entry: dict, identity: dict) -> bool:
    if "path" in entry and "path" in identity:
        return _same_place(entry["path"], identity["path"])
    if "repo" in entry and "repo" in identity:
        return _same_place(entry["repo"], identity["repo"]) and entry["rel"].casefold() == identity["rel"].casefold()
    return False


def suggest_name(cfg: GlobalConfig, folder: Path) -> str:
    """Default project name: `<repo>/<topic>` for a topic folder, the owner's name for a folder
    that is itself a knowledge-base folder (`<repo>/docs/kb` -> `<repo>`)."""
    folder = _abs(folder)
    root = kbregistry.git_root(folder)
    main = kbregistry.main_clone(root) if root else None
    for kb_name in sorted(cfg.kb_folder_names, key=len, reverse=True):
        tail = tuple(kb_name.split("/"))
        if folder.parts[-len(tail):] == tail:
            owner = Path(*folder.parts[:-len(tail)])
            return main.name if root is not None and _key(owner) == _key(root) else owner.name
    return f"{main.name}/{folder.name}" if main else folder.name


def _check_registrable(cfg: GlobalConfig, folder: Path, must_exist: bool = True) -> dict:
    """Every guard of spec §14.3, before anything is written. Returns the folder's registry identity."""
    if must_exist and not folder.is_dir():
        raise ValueError(f"{folder} is not a folder")
    root = kbregistry.git_root(folder)
    # inside a repo only the path within the checkout matters: worktrees often live under hidden
    # folders (e.g. <repo>/.claude/worktrees/<name>)
    inner = folder.relative_to(root).parts if root is not None else folder.parts
    if any(part.startswith(".") for part in inner):
        raise ValueError(f"{folder} is inside a hidden folder; hidden folders are never indexed")
    if any(part in SKIP_DIRS for part in inner):
        raise ValueError(f"{folder} is inside node_modules; that is never indexed")
    key = _key(folder)
    if root is not None and (not inner or _key(root) == key):
        raise ValueError(f"{folder} is a repo root; register a knowledge-base folder inside it "
                         f"(e.g. {folder / KB_DIR / '<topic>'}), not the whole repo")
    roots = [*cfg.scan_roots, *cfg.kb_scan_roots, *cfg.personal_roots, *cfg.exclude_dirs,
             cfg.new_project_parent, Path.home()]
    if key is not None and any(_key(r) == key for r in roots):
        raise ValueError(f"{folder} is a configured root that holds many projects; "
                         "register one knowledge-base folder inside it")
    identity = _identity(folder)
    if "repo" in identity and not Path(identity["repo"]).is_dir():
        raise ValueError(f"the main clone {identity['repo']} of this worktree does not exist")
    for other in discover_projects(cfg)[0].values():
        if key is not None and _key(other.path) == key:
            continue  # the same folder: an update, not an overlap
        if is_within(folder, other.path) or is_within(other.path, folder):
            raise ValueError(f"{folder} overlaps the knowledge base {other.name!r} ({other.path}); "
                             "its files would be indexed twice")
    return identity


def _check_name_free(cfg: GlobalConfig, name: str, folder: Path, identity: dict, entries: list[dict]) -> None:
    clash = any(e.get("name") == name and not _same(e, identity) for e in entries)
    marker = next((p for n, p in discover_projects(cfg)[0].items() if n == name and p.source == "marker"), None)
    if clash or (marker is not None and _key(marker.path) != _key(folder)):
        raise ValueError(f"the name {name!r} is already used by another knowledge base; pass a different name")


def _project_named(cfg: GlobalConfig, name: str, prefer: Path | None = None) -> Project:
    project = discover_projects(cfg, session_dir=prefer)[0].get(name)
    if project is None:
        raise ValueError(f"{name!r} was registered but could not be loaded; check `kbhub projects`")
    return project


def register_kb(cfg: GlobalConfig, path, name: str | None = None, scope: str | None = None) -> Project:
    """Add an existing knowledge-base folder (inside a git repo or not) to the central registry.
    Registering the same folder again changes only what is passed: its name, scope and rules are
    otherwise kept."""
    folder = _abs(path)
    _check_scope(scope)
    identity = _check_registrable(cfg, folder)
    owner = Path(identity["repo"]) if "repo" in identity else folder

    def change(entries: list[dict]) -> str:
        mine = next((e for e in entries if _same(e, identity)), None)
        final_name = (name or (mine["name"] if mine else suggest_name(cfg, folder))).strip()
        _check_name_free(cfg, final_name, folder, identity, entries)
        if mine is None:
            entries.append({"name": final_name, **identity, "scope": scope or resolve_scope(cfg, owner)})
        else:  # never reset a stored scope or name that was not asked for
            if name:
                mine["name"] = final_name
            if scope:
                mine["scope"] = scope
        return final_name

    return _project_named(cfg, kbregistry.update_registry(cfg, change), prefer=folder)


def _check_topic(topic: str) -> None:
    if (not topic or topic != topic.strip() or topic.startswith(".") or "/" in topic or "\\" in topic
            or any(ch.isspace() and ch != " " for ch in topic)):
        raise ValueError(f"invalid topic {topic!r}: use a plain folder name such as 'billing'")


def create_kb(cfg: GlobalConfig, topic: str, location=None, name: str | None = None,
              scope: str | None = None) -> Project:
    """Create a new knowledge base. Inside a git repo: `<checkout root>/knowledge-base/<topic>/`
    (flat layout, no .kb.toml), registered centrally. Elsewhere: the standard project layout with
    a .kb.toml, as `new_project` does. Only writes files; never stages or commits. Everything is
    checked before the first file is written."""
    _check_topic(topic)
    _check_scope(scope)
    where = _abs(location) if location is not None else cfg.new_project_parent
    root = kbregistry.git_root(where)
    if root is None:
        project = new_project(cfg, topic, parent=where if location is not None else None)
        if not (name or scope):
            return project
        marker = project.path / MARKER
        text = marker.read_text(encoding="utf-8")
        if name:
            text = text.replace(f'name = "{topic}"', f"name = {json.dumps(name)}", 1)
        if scope:
            text += f'\nscope = "{scope}"\n'
        marker.write_text(text, encoding="utf-8")
        pc = ProjectConfig.load(marker)
        final_scope = resolve_scope(cfg, project.path, pc.scope)
        return Project(pc.name or project.name, project.path, pc, scope=final_scope,
                       fingerprint=f"{pc.fingerprint}:{final_scope}")

    folder = root / KB_DIR / topic
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise ValueError(f"{folder} already exists and is not empty; register it with register_kb instead")
    identity = _check_registrable(cfg, folder, must_exist=False)
    name = (name or suggest_name(cfg, folder)).strip()
    _check_name_free(cfg, name, folder, identity, kbregistry.load_entries(cfg)[0])
    values = {"{{name}}": name, "{{topic}}": topic, "{{date}}": date.today().isoformat()}
    for sub in REPO_LAYOUT_DIRS:
        (folder / sub).mkdir(parents=True, exist_ok=True)
    for template in sorted(REPO_TEMPLATES.iterdir()):
        text = template.read_text(encoding="utf-8")
        for token, value in values.items():
            text = text.replace(token, value)
        (folder / template.name).write_text(text, encoding="utf-8")
    return register_kb(cfg, folder, name=name, scope=scope)


def _find_project(cfg: GlobalConfig, name: str) -> Project:
    projects = discover_projects(cfg)[0]
    match = [p for n, p in projects.items() if n.lower() == name.lower()]
    if len(match) != 1:
        raise ValueError(f"Unknown project {name!r}. Known projects: {', '.join(sorted(projects)) or '(none)'}")
    return match[0]


def add_rule(cfg: GlobalConfig, project: str, glob: str, status: str | None = None,
             doc_type: str | None = None, note: str | None = None) -> None:
    """Add a status / doc-type rule to a registered knowledge base (first matching rule wins)."""
    if not status and not doc_type:
        raise ValueError("a rule needs a status or doc_type")
    if status and status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    target = _find_project(cfg, project)
    if target.source != "registry":
        raise ValueError(f"{target.name} is configured by {target.path / MARKER}; add the rule there as a [[rules]] entry")
    rule = {"glob": glob}
    rule.update({k: v for k, v in (("status", status), ("doc_type", doc_type), ("note", note)) if v})

    def change(entries: list[dict]) -> None:
        entry = next((e for e in entries if e.get("name") == target.name), None)
        if entry is None:
            raise ValueError(f"{target.name} is not in the registry under that name; check `kbhub projects`")
        entry.setdefault("rules", []).append(rule)

    kbregistry.update_registry(cfg, change)


def unregister(cfg: GlobalConfig, name: str) -> bool:
    """Remove a knowledge base from the registry. Its files are not touched."""
    def change(entries: list[dict]) -> bool:
        keep = [e for e in entries if e["name"] != name]
        removed = len(keep) != len(entries)
        entries[:] = keep
        return removed

    return kbregistry.update_registry(cfg, change)


# ------------------------------------------------------------------ scan

SCAN_SKIP = SKIP_DIRS | {"dist", "build", "vendor", "target", "__pycache__"}


def _subdirs(folder: Path) -> list[Path]:
    try:
        return sorted(folder / e.name for e in os.scandir(folder)
                      if e.is_dir(follow_symlinks=False) and not skip_child(folder, e.name, SCAN_SKIP))
    except OSError:
        return []


def _markdown_count(folder: Path) -> int:
    count = 0
    for _, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SCAN_SKIP]
        count += sum(1 for n in names if n.lower().endswith(".md") and not n.startswith("."))
    return count


def _has_direct_markdown(folder: Path) -> bool:
    try:
        return any(e.is_file() and e.name.lower().endswith(".md") and not e.name.startswith(".")
                   for e in os.scandir(folder))
    except OSError:
        return False


def scan(cfg: GlobalConfig, root=None) -> list[dict]:
    """Find knowledge-base folders on disk, including inside git repos, and say which are not
    registered yet. On demand only (it walks deep); it changes nothing."""
    roots = [_abs(root)] if root is not None else cfg.kb_scan_roots
    tails = [tuple(n.split("/")) for n in cfg.kb_folder_names]
    candidates: dict[tuple[int, int], Path] = {}

    def offer(folder: Path) -> None:
        key = _key(folder)
        if key is not None and _markdown_count(folder):
            candidates.setdefault(key, folder)

    def walk(folder: Path, depth: int) -> None:
        if any(folder.parts[-len(t):] == t for t in tails):
            if _has_direct_markdown(folder):
                offer(folder)          # one knowledge base
            else:
                for sub in _subdirs(folder):
                    offer(sub)         # one per topic folder
            return
        if depth >= cfg.kb_scan_depth:
            return
        for sub in _subdirs(folder):
            if not kbregistry.is_linked_worktree(sub):  # a worktree repeats its main clone
                walk(sub, depth + 1)

    for r in roots:
        if r.is_dir():
            walk(r, 0)

    projects = discover_projects(cfg)[0]
    entries = kbregistry.load_entries(cfg)[0]
    out = []
    for folder in candidates.values():
        identity = _identity(folder)
        entry = next((e for e in entries if _same(e, identity)), None)
        covering = project_for_path(projects, folder)
        inside = [] if entry or covering else [p.name for p in projects.values() if is_within(p.path, folder)]
        owner = Path(identity["repo"]) if "repo" in identity else folder
        out.append({
            "path": str(folder), "repo": identity.get("repo"), "name": suggest_name(cfg, folder),
            "scope": resolve_scope(cfg, owner), "files": _markdown_count(folder),
            "registered": (entry["name"] if entry else covering.name if covering
                           else ", ".join(sorted(inside)) or None),   # its topics are registered
        })
    return sorted(out, key=lambda c: (c["registered"] is not None, c["path"]))
