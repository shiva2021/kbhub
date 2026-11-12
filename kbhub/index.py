"""The central index: one LanceDB table for every knowledge-base project, kept in sync with the
markdown files incrementally. The markdown stays the source of truth; `db/` can be deleted at
any time and rebuilds itself on the next sync."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

from .chunker import FENCE_RE, HEADING_RE, chunk_file
from . import kbregistry
from .config import SCOPES, GlobalConfig
from .embedder import Embedder, EmbedderUnavailable
from .registry import MARKER, Project, discover_projects, is_indexable, is_within, project_files, session_scope

TABLE = "chunks"
SCHEMA = 3          # bump when the table columns change; a different schema rebuilds once
RRF_K = 60
HIT_KEYS = ("project", "path", "heading", "start_line", "end_line", "status", "status_note",
            "doc_type", "text", "tokens")


class IndexCorrupt(Exception):
    """The table on disk cannot be read or no longer fits the embedder; it must be rebuilt."""


def log(*a) -> None:
    print("[kbhub]", *a, file=sys.stderr, flush=True)


def sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def embed_input(row: dict) -> str:
    """What is embedded for a chunk: project and file path give the vector some context."""
    return f"{row['project']} | {row['path']} | {row['search_text']}"


class KBIndex:
    def __init__(self, cfg: GlobalConfig | None = None, read_only: bool = False, keyword_only: bool = False,
                 session_dir: Path | None = None):
        self.cfg = cfg or GlobalConfig.load()
        self.read_only = read_only
        self.keyword_only = keyword_only
        self.session_dir = Path(session_dir) if session_dir else None  # folder the session works in
        self.last_error: str | None = None
        self._embedder: Embedder | None = None
        self._db = None
        self._last_sync = 0.0
        self._projects: dict[str, Project] | None = None
        self._warnings: list[str] = []

    # ------------------------------------------------------------------ plumbing

    @property
    def lance_dir(self) -> Path:
        return self.cfg.db_dir / "lance"

    @property
    def manifest_path(self) -> Path:
        return self.cfg.db_dir / "manifest.json"

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(self.cfg)
        return self._embedder

    def _connect(self):
        if self._db is None:
            if self.read_only and not self.lance_dir.is_dir():
                return None  # read-only never creates anything
            import lancedb
            self._db = lancedb.connect(str(self.lance_dir))
        return self._db

    def _table(self):
        try:
            db = self._connect()
            return db.open_table(TABLE) if db is not None else None
        except Exception:
            return None

    def _manifest(self) -> dict:
        try:
            man = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return man if isinstance(man, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_manifest(self, man: dict) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(man, indent=1), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def _wipe(self) -> None:
        shutil.rmtree(self.lance_dir, ignore_errors=True)
        self.manifest_path.unlink(missing_ok=True)
        self._db = None

    def projects_map(self) -> dict[str, Project]:
        """Projects by name; discovered fresh on every sync and cached in between."""
        if self._projects is None:
            self._projects, self._warnings = discover_projects(self.cfg, self.session_dir)
        return self._projects

    @property
    def session_scope(self) -> str:
        """Scope searches default to: that of the session's folder, else the configured default."""
        if self.session_dir is None:
            return self.cfg.default_scope
        return session_scope(self.cfg, self.session_dir, self.projects_map())

    # ------------------------------------------------------------------ sync

    def maybe_sync(self, min_interval: float = 5.0) -> dict | None:
        """Sync unless one ran in the last `min_interval` seconds. Never raises: a failed sync
        is logged (and kept in `last_error`) and searches use the existing index."""
        if self.read_only or time.time() - self._last_sync < min_interval:
            return None
        try:
            stats = self.sync(wait=False)
            self.last_error = None
            return stats
        except BlockingIOError:  # another session is syncing; its result is as good as ours
            self._last_sync = time.time()
            self.last_error = None
            log("another session is syncing; using the index as it is")
            return None
        except Exception as exc:
            self.last_error = str(exc) or type(exc).__name__
            log(f"sync failed, using the existing index: {self.last_error}")
            return None

    def sync(self, rebuild: bool = False, project: str | None = None, wait: bool = True) -> dict:
        """Bring the index up to date. `rebuild` re-indexes everything; `project` re-chunks one
        project. Serialised with a file lock, because every Claude Code session runs its own
        server process against the same db/. With `wait=False` a busy lock raises
        BlockingIOError instead of waiting for the other session to finish.

        Only a table that cannot be read (or no longer fits the embedder) is wiped and rebuilt.
        Any other failure (disk full, embedder down, bad config) is raised and the existing
        index is left as it is."""
        if self.read_only:
            raise RuntimeError("this index is read-only; sync is not allowed")
        self.cfg.db_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.db_dir / "sync.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                try:
                    return self._sync(rebuild, project)
                except IndexCorrupt as exc:
                    log(f"index unusable ({exc}); rebuilding from the markdown")
                    self._wipe()
                    return self._sync(True, None)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _reload_config(self) -> None:
        new = GlobalConfig.load(self.cfg.path)  # it may have been edited since we started
        if new.fingerprint != self.cfg.fingerprint:
            self.cfg, self._embedder = new, None

    def _known_dim(self, man: dict, tbl) -> int | None:
        if man.get("dim"):
            return int(man["dim"])
        if tbl is not None:
            try:
                return tbl.schema.field("vector").type.list_size
            except Exception:
                pass
        return self.embedder._dim

    def _schema(self, dim: int):
        import pyarrow as pa
        text = pa.string()
        return pa.schema([
            ("id", text), ("project", text), ("path", text), ("heading", text),
            ("start_line", pa.int32()), ("end_line", pa.int32()),
            ("scope", text), ("status", text), ("status_note", text), ("doc_type", text),
            ("text", text), ("search_text", text), ("tokens", pa.int32()),
            ("vector", pa.list_(pa.float32(), dim)), ("embedded", pa.bool_()),
        ])

    def _sync(self, rebuild: bool, only_project: str | None) -> dict:
        t0 = time.time()
        self._last_sync = t0
        self._reload_config()
        projects, warnings = discover_projects(self.cfg, self.session_dir)
        self._projects, self._warnings = projects, warnings
        if only_project is not None and only_project not in projects:
            raise ValueError(f"Unknown project {only_project!r}. Known projects: {', '.join(sorted(projects)) or '(none)'}")

        man = self._manifest()
        tbl = self._table()
        if tbl is None and (self.lance_dir / f"{TABLE}.lance").exists():
            raise IndexCorrupt("the table exists but cannot be opened")
        if tbl is not None and not rebuild:
            try:  # cheap health probe
                tbl.count_rows()
                tbl.head(1)
            except Exception as exc:
                raise IndexCorrupt(f"{type(exc).__name__}: {exc}") from exc
        same_embedder = man.get("embedder") == self.embedder.id
        if (rebuild or (tbl is None and not man.get("empty")) or not same_embedder
                or man.get("schema") != SCHEMA or man.get("config") != self.cfg.index_fingerprint):
            rebuild = True
            man = {"schema": SCHEMA, "embedder": self.embedder.id, "config": self.cfg.index_fingerprint,
                   "dim": man.get("dim") if same_embedder else None, "projects": {}}
        old = man.get("projects", {})

        # ---- what changed on disk
        work: list[dict] = []                      # files to (re)chunk
        removed: list[tuple[str, str]] = []        # (project, path) no longer on disk
        drop_projects = [n for n in old if n not in projects]
        new_projects: dict[str, dict] = {}
        for name, proj in projects.items():
            pm = old.get(name)
            rechunk = pm is not None and (pm.get("path") != str(proj.path)
                                          or pm.get("config") != proj.fingerprint
                                          or name == only_project)
            if rechunk:
                drop_projects.append(name)
            known = {} if pm is None or rechunk else pm.get("files", {})
            files, file_warnings = project_files(proj, self.cfg)
            warnings.extend(file_warnings)
            for rel, p in files.items():
                try:
                    mtime = p.stat().st_mtime
                    k = known.get(rel)
                    pending = bool(k and k.get("pending_embed"))
                    if k and k["mtime"] == mtime and not pending:
                        continue
                    data = p.read_bytes()
                except OSError:
                    continue
                sha = hashlib.sha1(data).hexdigest()
                same = bool(k and k["sha"] == sha)
                if same and not pending:
                    k["mtime"] = mtime
                    continue
                work.append({"project": name, "rel": rel, "mtime": mtime, "sha": sha,
                             "retry_only": same,  # unchanged, only waiting for its vectors
                             "rows": [dict(r, scope=proj.scope) for r in
                                      chunk_file(name, rel, data.decode("utf-8", errors="replace"),
                                                 proj.config.rules, self.cfg)]})
            removed.extend((name, rel) for rel in known if rel not in files)
            new_projects[name] = {"path": str(proj.path), "config": proj.fingerprint,
                                  "files": {rel: k for rel, k in known.items() if rel in files}}

        # ---- vectors: reuse those of unchanged text, embed the rest
        cache: dict[str, list[float]] = {}
        if tbl is not None and not rebuild:
            for name in {w["project"] for w in work}:
                where = f"project = {sql_str(name)} AND embedded = true"
                n = tbl.count_rows(where)
                if n:
                    for r in (tbl.search().where(where).select(["project", "path", "search_text", "vector"])
                              .limit(n).to_list()):
                        cache[embed_input(r)] = r["vector"]
        missing = list(dict.fromkeys(embed_input(r) for w in work for r in w["rows"] if embed_input(r) not in cache))
        dim = self._known_dim(man, tbl)
        try:
            if missing:
                cache.update(zip(missing, self.embedder.embed(missing)))
                new_dim = len(cache[missing[0]])
                if tbl is not None and not rebuild and dim and new_dim != dim:
                    raise IndexCorrupt(f"embedder now returns {new_dim}-d vectors, the table holds {dim}-d")
                dim = new_dim
        except EmbedderUnavailable as exc:
            if dim is None:
                raise EmbedderUnavailable(f"{exc} (the first index build needs the embedder)") from exc
            log(f"embedder unavailable, new text is keyword-searchable only for now: {exc}")
            work = [w for w in work if not w["retry_only"]]  # leave already-pending rows as they are
        rows = []
        for w in work:
            w["pending"] = False
            for r in w["rows"]:
                vec = cache.get(embed_input(r))
                r["vector"], r["embedded"] = (vec, True) if vec is not None else ([0.0] * dim, False)
                w["pending"] = w["pending"] or vec is None
                rows.append(r)

        # ---- write
        wrote = bool(rebuild or rows or removed or drop_projects)
        if wrote:
            import pyarrow as pa
            data = pa.Table.from_pylist(rows, schema=self._schema(dim)) if rows else None
            db = self._connect()
            if rebuild or tbl is None:
                if data is not None:
                    tbl = db.create_table(TABLE, data=data, mode="overwrite")
                else:
                    db.drop_table(TABLE, ignore_missing=True)
                    tbl = None
            else:
                for name in drop_projects:
                    tbl.delete(f"project = {sql_str(name)}")
                by_project: dict[str, list[str]] = {}
                for name, rel in removed + [(w["project"], w["rel"]) for w in work]:
                    if name not in drop_projects:
                        by_project.setdefault(name, []).append(rel)
                for name, rels in by_project.items():
                    for i in range(0, len(rels), 200):
                        paths = ", ".join(sql_str(r) for r in rels[i:i + 200])
                        tbl.delete(f"project = {sql_str(name)} AND path IN ({paths})")
                if data is not None:
                    tbl.add(data)
            if tbl is not None:
                self._finish_write(tbl)

        # ---- manifest
        for w in work:
            new_projects[w["project"]]["files"][w["rel"]] = {
                "mtime": w["mtime"], "sha": w["sha"], "pending_embed": w["pending"]}
        man.update({"projects": new_projects, "dim": dim, "empty": tbl is None})
        self._save_manifest(man)
        stats = {
            "changed": len(work), "removed": len(removed), "rebuilt": rebuild,
            "chunks_written": len(rows),
            "pending_embed": sum(1 for p in new_projects.values() for f in p["files"].values() if f.get("pending_embed")),
            "seconds": round(time.time() - t0, 2), "warnings": warnings,
        }
        if wrote:
            log(f"sync {stats}")
        return stats

    def _finish_write(self, tbl) -> None:
        try:
            if tbl.count_rows():
                from lancedb.index import FTS
                tbl.create_index("search_text", config=FTS(stem=True, remove_stop_words=True, with_position=True),
                                 replace=True)
        except (ImportError, TypeError):
            tbl.create_fts_index("search_text", replace=True)
        try:  # compact and drop old table versions so db/ doesn't grow with every edit
            tbl.optimize(cleanup_older_than=timedelta(hours=1))
        except Exception as exc:
            log(f"optimize skipped: {exc}")

    # ------------------------------------------------------------------ queries

    def _known_names(self) -> set[str]:
        """Project names. A read-only index also accepts the names it was built with, since the
        project folders may not exist where it is being read (Cowork)."""
        known = set(self.projects_map())
        if self.read_only:
            known |= set(self._manifest().get("projects", {}))
        return known

    def _unknown(self, name: str, known: set[str]) -> ValueError:
        return ValueError(f"Unknown project {name!r}. Known projects: {', '.join(sorted(known)) or '(none)'}")

    def _project_name(self, name: str) -> str:
        """The exact name of the one project called `name` (case-insensitive)."""
        known = self._known_names()
        if name in known:
            return name
        loose = [n for n in known if n.lower() == name.lower()]
        if len(loose) != 1:
            raise self._unknown(name, known)
        return loose[0]

    def _project_names(self, name: str) -> list[str]:
        """Names a `project` filter stands for: the project itself plus every "<name>/<topic>",
        so "core" covers all knowledge bases of the core repo."""
        known = self._known_names()
        want = name.lower().rstrip("/")
        names = sorted(n for n in known if n.lower() == want or n.lower().startswith(want + "/"))
        if not names:
            raise self._unknown(name, known)
        return names

    def _scope_filter(self, scope: str | None) -> str | None:
        """SQL filter for a scope argument: None means the session's scope, "all" means no filter."""
        if scope == "all":
            return None
        scope = scope or self.session_scope
        if scope not in SCOPES:
            raise ValueError(f"Unknown scope {scope!r}. Use one of: {', '.join(SCOPES)}, all")
        return f"scope = {sql_str(scope)}"

    def _where(self, project: str | None, scope: str | None) -> list[str]:
        """An explicit project wins over scope: asking for a project by name always reaches it."""
        if project:
            return ["project IN (" + ", ".join(sql_str(n) for n in self._project_names(project)) + ")"]
        scope_filter = self._scope_filter(scope)
        return [scope_filter] if scope_filter else []

    def _resolve_project(self, name: str) -> Project:
        projects = self.projects_map()
        exact = self._project_name(name)
        if exact not in projects:
            raise ValueError(f"Project {exact!r} is indexed but its folder is not available here")
        return projects[exact]

    def _ready_table(self):
        tbl = self._table()
        if tbl is None and not self.read_only:
            self.sync()
            tbl = self._table()
        return tbl

    def _require_index(self):
        """The table, or None when the index is empty. A read-only index that is not there at all is
        an error: the caller is pointed at the wrong folder, and "no matches" would hide that."""
        tbl = self._ready_table()
        if tbl is None and self.read_only and not self._manifest():
            raise ValueError(f"No index found at {self.cfg.db_dir}. Point KBHUB_HOME at the kbhub "
                             "folder that holds db/, or build the index with `kbhub index`.")
        return tbl

    def search(self, query: str, project: str | None = None, doc_type: str | None = None,
               include_superseded: bool = False, limit: int = 6, scope: str | None = None) -> dict:
        """Hybrid search: vector and keyword results fused with reciprocal rank fusion. Keyword
        only when asked for or when the embedder is unavailable. Without `project`, results stay
        inside `scope` (default: the session's scope; "all" for everything)."""
        if not query or not query.strip():
            raise ValueError("empty query")
        limit = min(max(int(limit), 1), 15)
        base = self._where(project, scope)
        if doc_type:
            base.append(f"doc_type = {sql_str(doc_type)}")
        vec = None
        if not self.keyword_only:
            try:
                vec = self.embedder.embed([query], kind="query")[0]
            except EmbedderUnavailable as exc:
                log(f"embedder unavailable, keyword search only: {exc}")
        result = {"hits": [], "superseded_matches": [], "keyword_only": vec is None}
        tbl = self._require_index()
        if tbl is None:
            return result
        fts_text = re.sub(r"[^\w\s\-./<>]", " ", query).strip()

        def run(extra: list[str], k: int) -> list[dict]:
            where = " AND ".join(base + extra)
            fetch = max(k * 4, 20)
            ranked: list[list[dict]] = []
            if vec is not None:
                try:  # rows still waiting for their vectors must not take part in vector ranking
                    ranked.append(tbl.search(vec, vector_column_name="vector")
                                  .where(" AND ".join(base + extra + ["embedded = true"]), prefilter=True)
                                  .limit(fetch).to_list())
                except Exception as exc:
                    log(f"vector search failed ({exc}); using keyword results only")
            if fts_text:
                try:
                    q = tbl.search(fts_text, query_type="fts", fts_columns="search_text")
                    if where:
                        q = q.where(where, prefilter=True)
                    ranked.append(q.limit(fetch).to_list())
                except Exception as exc:  # e.g. odd punctuation the FTS parser rejects
                    log(f"keyword search failed ({exc}); using vector results only")
            scores: dict[str, float] = {}
            rows: dict[str, dict] = {}
            for hits in ranked:
                for rank, h in enumerate(hits, 1):
                    scores[h["id"]] = scores.get(h["id"], 0.0) + 1.0 / (RRF_K + rank)
                    rows[h["id"]] = h
            return [rows[i] for i in sorted(scores, key=scores.get, reverse=True)[:k]]

        hits = run([] if include_superseded else ["status != 'superseded'"], limit)
        hidden = [] if include_superseded else run(["status = 'superseded'"], 3)
        result["hits"] = [{k: h[k] for k in HIT_KEYS} for h in hits]
        result["superseded_matches"] = [{k: h[k] for k in ("project", "path", "heading", "status_note")} for h in hidden]
        return result

    def section(self, project: str, path: str, heading: str | None = None) -> str:
        """A full section (heading plus everything nested under it), read fresh from disk."""
        proj = self._resolve_project(project)
        p = (proj.path / path).resolve()
        if not is_within(p, proj.path) or self._resolve_owner(p) is not proj:
            return f"Refused: {path} is outside project {proj.name}"
        if not is_indexable(proj.path, p):
            return (f"Refused: {path} is not a knowledge file of project {proj.name} "
                    "(only its markdown files are readable; hidden folders and nested repos are not)")
        if not p.is_file():
            return f"Not found: {path} in project {proj.name}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if not heading:
            return f"{path}:1-{len(lines)}\n" + "\n".join(lines)
        want = [part.strip().lower() for part in heading.split(" > ") if part.strip()]
        found = []  # (line index, level, breadcrumb of lower-cased titles)
        stack: list[tuple[int, str]] = []
        in_fence = False
        for i, line in enumerate(lines):
            if FENCE_RE.match(line):
                in_fence = not in_fence
            m = None if in_fence else HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, m.group(2).strip().lower()))
                found.append((i, level, [t for _, t in stack]))
        # most specific first: the breadcrumb as given, then its last part, then a substring
        matches = ([h for h in found if h[2][-len(want):] == want]
                   or [h for h in found if want and h[2][-1] == want[-1]]
                   or [h for h in found if want and want[-1] in h[2][-1]])
        if not matches:
            return (f"No heading containing {heading!r} in {path}. Headings:\n"
                    + "\n".join(lines[i] for i, _, _ in found))
        if len(matches) > 1:
            return (f"Ambiguous heading {heading!r} in {path}: {len(matches)} sections match. "
                    "Ask again with more of the § breadcrumb. Candidates:\n"
                    + "\n".join(f"line {i + 1}: {' > '.join(crumb)}" for i, _, crumb in matches))
        start, level, _ = matches[0]
        end = next((i for i, lv, _ in found if i > start and lv <= level), len(lines))
        return f"{path}:{start + 1}-{end}\n" + "\n".join(lines[start:end]).strip()

    def _resolve_owner(self, path: Path) -> Project | None:
        """The deepest project containing `path` (a nested project owns its own files)."""
        inside = [p for p in self.projects_map().values() if is_within(path, p.path)]
        return max(inside, key=lambda p: len(p.path.parts), default=None)

    def _file_rows(self, project: str | None = None, scope: str | None = None) -> list[dict]:
        tbl = self._require_index()
        if tbl is None:
            return []
        where = " AND ".join(self._where(project, scope)) or None
        n = tbl.count_rows(where)
        if not n:
            return []
        q = tbl.search()
        if where:
            q = q.where(where)
        return q.select(["project", "path", "scope", "status", "doc_type", "tokens"]).limit(n).to_list()

    def sources(self, project: str | None = None, scope: str | None = None) -> list[dict]:
        """Indexed files with doc type, status and size (same project/scope rule as search)."""
        files: dict[tuple[str, str], dict] = {}
        for r in self._file_rows(project, scope):
            f = files.setdefault((r["project"], r["path"]), {
                "project": r["project"], "path": r["path"], "doc_type": r["doc_type"],
                "status": set(), "chunks": 0, "tokens": 0})
            f["status"].add(r["status"])
            f["chunks"] += 1
            f["tokens"] += int(r["tokens"])
        out = [files[k] for k in sorted(files)]
        for f in out:
            f["status"] = "/".join(sorted(f["status"]))
        return out

    def indexed_projects(self, scope: str | None = None) -> list[dict]:
        """Projects as the index holds them, without discovering or syncing anything: for readers
        (Cowork) where the knowledge-base folders are not on disk. Same scope rule as search; `path`
        is the folder on the machine that built the index."""
        paths = {name: p.get("path", "") for name, p in self._manifest().get("projects", {}).items()}
        out: dict[str, dict] = {}
        for r in self._file_rows(scope=scope):
            p = out.setdefault(r["project"], {"name": r["project"], "scope": r["scope"], "files": set(),
                                              "tokens": 0, "path": paths.get(r["project"], "")})
            p["files"].add(r["path"])
            p["tokens"] += int(r["tokens"])
        for p in out.values():
            p["files"] = len(p["files"])
        return [out[name] for name in sorted(out)]

    def indexed_section(self, project: str, path: str, heading: str | None = None) -> str:
        """A section (and everything nested under it) as the index holds it, for readers that cannot
        open the file. `heading` is a § breadcrumb or any part of one; without it, the whole file."""
        tbl = self._require_index()
        name = self._project_name(project)
        rows = []
        if tbl is not None:
            where = f"project = {sql_str(name)} AND path = {sql_str(path)}"
            n = tbl.count_rows(where)
            if n:
                rows = (tbl.search().where(where).limit(n)
                        .select(["heading", "start_line", "end_line", "status", "status_note", "text"]).to_list())
        if not rows:
            return f"Not indexed: {path} in project {name}"
        rows.sort(key=lambda r: r["start_line"])
        if heading:
            want = [part.strip().lower() for part in heading.split(" > ") if part.strip()]
            crumbs = [[t.strip().lower() for t in r["heading"].split(" > ")] for r in rows]
            within = lambda c: any(c[i:i + len(want)] == want for i in range(len(c) - len(want) + 1))
            keep = [r for r, c in zip(rows, crumbs) if want and within(c)]
            if not keep:  # a substring of one heading, e.g. just the decision number
                keep = [r for r, c in zip(rows, crumbs) if want and any(want[-1] in t for t in c)]
            if not keep:
                return (f"No heading containing {heading!r} in {path}. Headings:\n"
                        + "\n".join(dict.fromkeys(r["heading"] for r in rows)))
            rows = keep
        parts = ["(from the index, as of the Mac's last sync)"]
        for r in rows:
            status = r["status"]
            if status == "superseded":
                status = "SUPERSEDED" + (f" ({r['status_note']})" if r["status_note"] else "") + ": do not act on it"
            elif r["status_note"]:
                status += f" ({r['status_note']})"
            parts.append(f"{path}:{r['start_line']}-{r['end_line']} | {status}\n§ {r['heading']}\n{r['text'].strip()}\n")
        return "\n".join(parts).rstrip()

    def projects(self, scope: str | None = None) -> list[dict]:
        """Every known project (optionally one scope), including registered or configured ones
        whose folder is missing."""
        if scope not in (None, "all", *SCOPES):
            raise ValueError(f"Unknown scope {scope!r}. Use one of: {', '.join(SCOPES)}, all")
        known = self.projects_map()
        totals: dict[str, list[int]] = {}
        for s in self.sources(scope="all"):
            t = totals.setdefault(s["project"], [0, 0])
            t[0] += 1
            t[1] += s["tokens"]
        out = [{"name": name, "path": str(p.path), "files": totals.get(name, [0, 0])[0],
                "tokens": totals.get(name, [0, 0])[1], "missing": False,
                "warning": f"bad project config, indexed with defaults: {p.config.error}" if p.config.error else None,
                "scope": p.scope, "repo": str(p.repo) if p.repo else None, "source": p.source}
               for name, p in sorted(known.items())]
        for entry in kbregistry.load_entries(self.cfg)[0]:
            if kbregistry.resolve_entry(entry) is None:
                where = entry.get("path") or f"{entry['repo']}/{entry['rel']}"
                out.append({"name": entry["name"], "path": where, "files": 0, "tokens": 0, "missing": True,
                            "warning": "registered, but the folder is not present on any checkout",
                            "scope": entry["scope"], "repo": entry.get("repo"), "source": "registry"})
        for folder in self.cfg.explicit_projects:
            if not (folder / MARKER).is_file():
                out.append({"name": folder.name, "path": str(folder), "files": 0, "tokens": 0, "missing": True,
                            "warning": f"configured in explicit_projects but the folder or its {MARKER} is missing",
                            "scope": self.cfg.default_scope, "repo": None, "source": "marker"})
        if scope in SCOPES:
            out = [p for p in out if p["scope"] == scope]
        return out
