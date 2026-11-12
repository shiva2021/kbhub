"""Global and per-project configuration. Standard library only: the SessionStart hook imports
this module and must stay fast, so never import lancedb/numpy here."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUPERSEDED_RE = re.compile(r"(?i)\b(superseded|historical|dropped|not needed|obsolete|deprecated)\b")
STATUSES = ("current", "background", "superseded")
SCOPES = ("work", "personal")

DEFAULTS: dict = {
    # Machine-neutral: the setup wizard writes the person's real folders into config.toml.
    "scan_roots": ["~"],
    "scan_depth": 3,
    "exclude_dirs": [],
    "explicit_projects": [],
    "new_project_parent": "~",
    "max_chunk_tokens": 450,
    "max_file_bytes": 2_000_000,
    "personal_roots": [],
    "default_scope": "work",
    "kb_scan_roots": ["~"],
    "kb_scan_depth": 7,
    "kb_folder_names": ["knowledge-base", "knowledge_base", "docs/kb"],
}
DEFAULT_EMBEDDING: dict = {
    "backend": "ollama",
    "ollama_model": "nomic-embed-text",
    "ollama_url": "http://localhost:11434",
}

# Built-in doc types (spec §5). A project's own rules are checked first.
DEFAULT_DOC_TYPE_RULES: list[dict] = [
    {"glob": "**/CLAUDE.md", "doc_type": "entrypoint"},
    {"glob": "**/STATE.md", "doc_type": "state"},
    {"glob": "handovers/**", "doc_type": "handover"},
    {"glob": "plans/**", "doc_type": "plan"},
    {"glob": "knowledge-base/decisions*", "doc_type": "decisions"},
    {"glob": "knowledge-base/lessons*", "doc_type": "lessons"},
    {"glob": "knowledge-base/open-items*", "doc_type": "open-items"},
    {"glob": "knowledge-base/runbooks/**", "doc_type": "runbook"},
    {"glob": "knowledge-base/research/**", "doc_type": "research"},
    # flat layout of knowledge bases that live inside a code repo (<repo>/knowledge-base/<topic>/)
    {"glob": "README.md", "doc_type": "entrypoint"},
    {"glob": "decisions*", "doc_type": "decisions"},
    {"glob": "lessons*", "doc_type": "lessons"},
    {"glob": "open-items*", "doc_type": "open-items"},
    {"glob": "runbooks/**", "doc_type": "runbook"},
    {"glob": "research/**", "doc_type": "research"},
]


class ConfigError(Exception):
    """Message is "<file>:<line>: <reason>" (the line is omitted when unknown)."""


def engine_home() -> Path:
    return expand(os.environ.get("KBHUB_HOME") or "~/.kbhub")


def expand(p: str | os.PathLike) -> Path:
    return Path(os.path.expanduser(str(p)))


def glob_match(rel: str, pattern: str) -> bool:
    """Case-insensitive glob; `*` also crosses folders, and "**/x" matches "x" at the root."""
    rel, pattern = rel.lower(), pattern.lower()
    if fnmatch.fnmatchcase(rel, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatchcase(rel, pattern[3:])


def _toml_error(path: Path, exc: tomllib.TOMLDecodeError) -> str:
    line = getattr(exc, "lineno", None)
    if not line:
        m = re.search(r"at line (\d+)", str(exc))
        line = int(m.group(1)) if m else None
    reason = re.sub(r"\s*\(at line \d+, column \d+\)", "", str(exc))
    return f"{path}:{line}: {reason}" if line else f"{path}: {reason}"


def _typed(path: Path, raw: dict, key: str, kind, default):
    value = raw.get(key, default)
    ok = isinstance(value, kind) and not isinstance(value, bool)
    if ok and kind is list:
        ok = all(isinstance(v, str) for v in value)
    if not ok:
        want = "a list of strings" if kind is list else f"a {kind.__name__}"
        raise ConfigError(f"{path}: {key} must be {want}")
    return value


@dataclass
class GlobalConfig:
    path: Path
    home: Path
    scan_roots: list[Path]
    scan_depth: int
    exclude_dirs: list[Path]
    explicit_projects: list[Path]
    new_project_parent: Path
    db_dir: Path
    max_chunk_tokens: int
    max_file_bytes: int
    superseded_re: re.Pattern
    embedding: dict
    fingerprint: str                # hash of the whole file: has it been edited?
    personal_roots: list[Path] = field(default_factory=list)
    default_scope: str = "work"
    kb_scan_roots: list[Path] = field(default_factory=list)
    kb_scan_depth: int = 7
    kb_folder_names: list[str] = field(default_factory=list)
    index_fingerprint: str = ""     # hash of the keys that change what gets indexed: rebuild?

    @classmethod
    def load(cls, path: Path | None = None) -> "GlobalConfig":
        home = engine_home()
        p = Path(path) if path else home / "config.toml"
        raw, fingerprint = {}, "defaults"
        if p.is_file():
            data = p.read_bytes()
            fingerprint = hashlib.sha1(data).hexdigest()
            try:
                raw = tomllib.loads(data.decode("utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(_toml_error(p, exc)) from None
            except UnicodeDecodeError as exc:
                raise ConfigError(f"{p}: not valid UTF-8 ({exc.reason})") from None
        embedding = raw.get("embedding", {})
        if not isinstance(embedding, dict):
            raise ConfigError(f"{p}: [embedding] must be a table")
        paths = lambda key: [expand(v) for v in _typed(p, raw, key, list, DEFAULTS[key])]
        number = lambda key: _typed(p, raw, key, int, DEFAULTS[key])
        default_scope = _typed(p, raw, "default_scope", str, DEFAULTS["default_scope"])
        if default_scope not in SCOPES:
            raise ConfigError(f"{p}: default_scope must be one of {', '.join(SCOPES)}")
        merged_embedding = {**DEFAULT_EMBEDDING, **embedding}
        index_keys = {"max_chunk_tokens": number("max_chunk_tokens"), "max_file_bytes": number("max_file_bytes"),
                      "embedding": merged_embedding}
        return cls(
            path=p,
            home=home,
            scan_roots=paths("scan_roots"),
            scan_depth=number("scan_depth"),
            exclude_dirs=paths("exclude_dirs"),
            explicit_projects=paths("explicit_projects"),
            new_project_parent=expand(_typed(p, raw, "new_project_parent", str, DEFAULTS["new_project_parent"])),
            db_dir=home / "db",
            max_chunk_tokens=number("max_chunk_tokens"),
            max_file_bytes=number("max_file_bytes"),
            superseded_re=SUPERSEDED_RE,
            embedding=merged_embedding,
            fingerprint=fingerprint,
            personal_roots=paths("personal_roots"),
            default_scope=default_scope,
            kb_scan_roots=paths("kb_scan_roots"),
            kb_scan_depth=number("kb_scan_depth"),
            kb_folder_names=list(_typed(p, raw, "kb_folder_names", list, DEFAULTS["kb_folder_names"])),
            index_fingerprint=hashlib.sha1(json.dumps(index_keys, sort_keys=True).encode()).hexdigest(),
        )


@dataclass
class ProjectConfig:
    name: str | None = None
    rules: list[dict] = field(default_factory=list)
    eval_cases: list[tuple[str, str]] = field(default_factory=list)
    fingerprint: str = ""
    error: str | None = None
    scope: str | None = None

    @classmethod
    def from_registry(cls, entry: dict) -> "ProjectConfig":
        """Config of a registry entry (in-repo knowledge bases have no .kb.toml). Never raises."""
        shape = {"rules": entry.get("rules", []), "eval": entry.get("eval", [])}
        fingerprint = hashlib.sha1(json.dumps(shape, sort_keys=True, default=str).encode()).hexdigest()
        name, scope = entry.get("name"), entry.get("scope")
        try:
            return cls(name=name, rules=_rules(shape), eval_cases=_eval_cases(shape), fingerprint=fingerprint,
                       scope=scope)
        except ValueError as exc:
            return cls(name=name, fingerprint=fingerprint, scope=scope, error=f"registry entry {name!r}: {exc}")

    @classmethod
    def load(cls, kb_toml: Path) -> "ProjectConfig":
        """Never raises: a broken .kb.toml gives defaults with `error` set."""
        try:
            data = Path(kb_toml).read_bytes()
        except OSError as exc:
            return cls(error=f"{kb_toml}: {exc.strerror or exc}")
        fingerprint = hashlib.sha1(data).hexdigest()
        text = data.decode("utf-8", errors="replace")
        try:
            raw = tomllib.loads(data.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            # Scope fails closed: a file that says personal stays personal even when it is broken.
            says_personal = re.search(r"""(?m)^\s*scope\s*=\s*["']personal["']""", text) is not None
            error = _toml_error(Path(kb_toml), exc) if isinstance(exc, tomllib.TOMLDecodeError) else f"{kb_toml}: {exc}"
            return cls(fingerprint=fingerprint, error=error, scope="personal" if says_personal else None)
        scope = raw.get("scope")
        if scope is not None and scope not in SCOPES:
            return cls(fingerprint=fingerprint, scope="personal",   # unknown scope: never leak into work
                       error=f"{kb_toml}: scope must be one of {', '.join(SCOPES)}; treated as personal")
        try:
            return cls(name=_project_name(raw), rules=_rules(raw), eval_cases=_eval_cases(raw),
                       fingerprint=fingerprint, scope=scope)
        except ValueError as exc:
            return cls(fingerprint=fingerprint, error=f"{kb_toml}: {exc}", scope=scope)


def _project_name(raw: dict) -> str | None:
    name = raw.get("name")
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    return name.strip()


def _rules(raw: dict) -> list[dict]:
    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules must be [[rules]] tables")
    for n, r in enumerate(rules, 1):
        if not isinstance(r, dict) or not isinstance(r.get("glob"), str):
            raise ValueError(f"rule {n} needs a glob string")
        for key in ("status", "doc_type", "note"):
            if key in r and not isinstance(r[key], str):
                raise ValueError(f"rule {n}: {key} must be a string")
        if "status" in r and r["status"] not in STATUSES:
            raise ValueError(f"rule {n}: status must be one of {', '.join(STATUSES)}")
    return rules


def _eval_cases(raw: dict) -> list[tuple[str, str]]:
    cases = raw.get("eval", [])
    if not isinstance(cases, list):
        raise ValueError("eval must be [[eval]] tables")
    out = []
    for n, c in enumerate(cases, 1):
        if not (isinstance(c, dict) and isinstance(c.get("question"), str) and isinstance(c.get("expect"), str)):
            raise ValueError(f"eval case {n} needs question and expect strings")
        out.append((c["question"], c["expect"]))
    return out


def file_meta(rules: list[dict], rel: str) -> dict:
    """Status and doc type for one file: the first matching rule wins per key; project rules are
    checked before the built-in doc-type defaults."""
    meta = {"status": "current", "status_note": "", "doc_type": "notes"}
    got_status = got_type = False
    for r in [*rules, *DEFAULT_DOC_TYPE_RULES]:
        if not glob_match(rel, r["glob"]):
            continue
        if "status" in r and not got_status:
            meta["status"], meta["status_note"], got_status = r["status"], r.get("note", ""), True
        if "doc_type" in r and not got_type:
            meta["doc_type"], got_type = r["doc_type"], True
    return meta
