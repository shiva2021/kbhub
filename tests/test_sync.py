import json
import multiprocessing
import os
import time

import pytest

from kbhub.embedder import Embedder, EmbedderUnavailable
from kbhub.index import KBIndex

DOC = "# Title\n\n## Alpha\nalpha body text\n\n## Beta\nbeta body text\n"


def rows(idx, **where):
    out = idx._table().to_arrow().to_pylist()
    return [r for r in out if all(r[k] == v for k, v in where.items())]


def manifest(kb_home):
    return json.loads((kb_home / "db" / "manifest.json").read_text())


@pytest.fixture
def two_projects(kb_home, scan_root, make_project):
    # both projects deliberately share file names: every filter must be project AND path
    a = make_project(scan_root, "A", {"STATE.md": DOC, "notes/x.md": "## Only A\nzebra\n"})
    b = make_project(scan_root, "B", {"STATE.md": DOC, "notes/x.md": "## Only B\nyak\n"})
    return a, b


def _touch_later(path, text):
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


def test_initial_sync_indexes_all_projects(kb_home, two_projects):
    idx = KBIndex()
    stats = idx.sync()
    assert stats["rebuilt"] is True and stats["changed"] == 4 and stats["pending_embed"] == 0
    assert {"changed", "removed", "rebuilt", "chunks_written", "pending_embed", "seconds", "warnings"} <= set(stats)
    assert {r["project"] for r in rows(idx)} == {"A", "B"}
    assert len(rows(idx, project="A", path="STATE.md")) == 2
    assert all(r["embedded"] and len(r["vector"]) == 256 for r in rows(idx))
    again = idx.sync()
    assert again["changed"] == 0 and again["rebuilt"] is False and again["chunks_written"] == 0
    m = manifest(kb_home)
    assert m["embedder"] == "wordllama:l2_supercat_256"
    assert set(m["projects"]) == {"A", "B"} and set(m["projects"]["A"]["files"]) == {"STATE.md", "notes/x.md"}
    assert KBIndex().sync()["changed"] == 0  # a fresh process sees the same state


def test_modify_and_delete_file(kb_home, two_projects):
    a, _ = two_projects
    idx = KBIndex()
    idx.sync()
    before_b = sorted(r["id"] for r in rows(idx, project="B"))
    _touch_later(a / "STATE.md", DOC + "\n## Gamma\ngamma body\n")
    stats = idx.sync()
    assert stats["changed"] == 1 and stats["rebuilt"] is False
    assert len(rows(idx, project="A", path="STATE.md")) == 3
    assert len(rows(idx, project="B", path="STATE.md")) == 2
    (a / "STATE.md").unlink()
    stats = idx.sync()
    assert stats["removed"] == 1
    assert rows(idx, project="A", path="STATE.md") == []
    assert sorted(r["id"] for r in rows(idx, project="B")) == before_b
    assert len(rows(idx, project="A")) == 1


def test_touch_without_change_is_not_rechunked(kb_home, two_projects):
    a, _ = two_projects
    idx = KBIndex()
    idx.sync()
    _touch_later(a / "STATE.md", DOC)
    assert idx.sync()["changed"] == 0


def test_project_removed(kb_home, two_projects):
    a, _ = two_projects
    idx = KBIndex()
    idx.sync()
    (a / ".kb.toml").unlink()
    idx.sync()
    assert {r["project"] for r in rows(idx)} == {"B"}
    assert set(manifest(kb_home)["projects"]) == {"B"}


def test_kb_toml_change_retags(kb_home, two_projects, monkeypatch):
    a, _ = two_projects
    idx = KBIndex()
    idx.sync()
    embedded = []
    real = Embedder.embed
    monkeypatch.setattr(Embedder, "embed", lambda self, texts, kind="document": embedded.extend(texts) or real(self, texts, kind))
    (a / ".kb.toml").write_text('[[rules]]\nglob = "notes/**"\nstatus = "superseded"\nnote = "old"\n')
    stats = idx.sync()
    assert stats["rebuilt"] is False
    assert {r["status"] for r in rows(idx, project="A", path="notes/x.md")} == {"superseded"}
    assert rows(idx, project="A", path="notes/x.md")[0]["status_note"] == "old"
    assert {r["status"] for r in rows(idx, project="A", path="STATE.md")} == {"current"}
    assert {r["status"] for r in rows(idx, project="B")} == {"current"}
    assert embedded == []  # unchanged text keeps its vectors; nothing is re-embedded
    assert all(r["embedded"] for r in rows(idx))


def test_sync_handles_awkward_paths(kb_home, scan_root, make_project):
    rel = "Messaging Requirements/ünïcode.md"
    p = make_project(scan_root, "O'Brien notes", {rel: "## It's here\nfirst\n", "plain.md": "## P\nkeep\n"})
    make_project(scan_root, "Other", {rel: "## Other\nuntouched\n"})
    idx = KBIndex()
    idx.sync()
    assert [r["text"] for r in rows(idx, project="O'Brien notes", path=rel)] == ["## It's here\nfirst"]
    _touch_later(p / rel, "## It's here\nsecond\n\n## More\nmore text\n")
    assert idx.sync()["changed"] == 1
    assert sorted(r["text"] for r in rows(idx, project="O'Brien notes", path=rel)) == ["## It's here\nsecond", "## More\nmore text"]
    (p / rel).unlink()
    idx.sync()
    assert rows(idx, project="O'Brien notes", path=rel) == []
    assert len(rows(idx, project="O'Brien notes")) == 1
    assert len(rows(idx, project="Other", path=rel)) == 1


def test_pending_embed_recovers(kb_home, two_projects, monkeypatch):
    a, _ = two_projects
    idx = KBIndex()
    idx.sync()
    real = Embedder.embed

    def down(self, texts, kind="document"):
        raise EmbedderUnavailable("ollama stopped")

    monkeypatch.setattr(Embedder, "embed", down)
    (a / "new-note.md").write_text("## Fresh note\nwombat details\n")
    stats = idx.sync()
    assert stats["pending_embed"] == 1 and stats["chunks_written"] == 1
    new = rows(idx, project="A", path="new-note.md")
    assert len(new) == 1 and new[0]["embedded"] is False and not any(new[0]["vector"])
    assert manifest(kb_home)["projects"]["A"]["files"]["new-note.md"]["pending_embed"] is True
    assert all(r["embedded"] for r in rows(idx) if r["path"] != "new-note.md")
    still = idx.sync()  # still down: nothing is rewritten, the file stays pending
    assert still["chunks_written"] == 0 and still["pending_embed"] == 1
    monkeypatch.setattr(Embedder, "embed", real)
    stats = idx.sync()  # no file change, yet the pending file is re-embedded
    assert stats["pending_embed"] == 0
    new = rows(idx, project="A", path="new-note.md")
    assert len(new) == 1 and new[0]["embedded"] is True and any(new[0]["vector"])
    assert manifest(kb_home)["projects"]["A"]["files"]["new-note.md"]["pending_embed"] is False
    assert idx.sync()["changed"] == 0


def _rebuild_worker(queue):
    try:
        idx = KBIndex()
        idx.sync(rebuild=True)
        queue.put(idx._table().count_rows())
    except Exception as exc:  # pragma: no cover - reported to the parent
        queue.put(repr(exc))


def test_concurrent_sync(kb_home, two_projects):
    idx = KBIndex()
    idx.sync()
    expected = len(rows(idx))
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_rebuild_worker, args=(queue,)) for _ in range(2)]
    for p in procs:
        p.start()
    results = [queue.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    assert results == [expected, expected]
    assert KBIndex()._table().count_rows() == expected


@pytest.mark.parametrize("pattern", ["data/*.lance", "_versions/*.manifest"])
def test_corrupt_table_rebuilds(kb_home, two_projects, pattern):
    idx = KBIndex()
    idx.sync()
    expected = len(rows(idx))
    victims = list((kb_home / "db" / "lance" / "chunks.lance").glob(pattern))
    assert victims
    for f in victims:
        f.write_bytes(b"")
    fresh = KBIndex()
    stats = fresh.sync()
    assert stats["rebuilt"] is True
    assert len(rows(fresh)) == expected


def test_read_only_refuses_sync(kb_home, two_projects):
    KBIndex().sync()
    ro = KBIndex(read_only=True)
    with pytest.raises(RuntimeError, match="read-only"):
        ro.sync()
    assert ro.maybe_sync() is None


def test_read_only_creates_nothing(kb_home, two_projects):
    ro = KBIndex(read_only=True, keyword_only=True)
    assert ro.maybe_sync() is None
    assert not (kb_home / "db").exists()


def test_maybe_sync_throttles_and_never_raises(kb_home, two_projects, monkeypatch):
    idx = KBIndex()
    assert idx.maybe_sync()["changed"] == 4
    assert idx.maybe_sync() is None  # within 5 s
    assert idx.maybe_sync(min_interval=0)["changed"] == 0
    (kb_home / "config.toml").write_text("scan_roots = = broken\n")
    assert idx.maybe_sync(min_interval=0) is None
    assert "config.toml" in idx.last_error


def test_global_config_change_rebuilds(kb_home, two_projects):
    idx = KBIndex()
    idx.sync()
    cfg_file = kb_home / "config.toml"
    cfg_file.write_text(cfg_file.read_text().replace("exclude_dirs = []", "exclude_dirs = []\nmax_chunk_tokens = 300"))
    stats = idx.sync()
    assert stats["rebuilt"] is True and idx.cfg.max_chunk_tokens == 300


def test_no_projects_gives_empty_index(kb_home):
    idx = KBIndex()
    stats = idx.sync()
    assert stats["chunks_written"] == 0
    assert idx.sync()["rebuilt"] is False


# ---- review fixes

def test_transient_error_during_sync_keeps_the_index(kb_home, two_projects, monkeypatch):
    a, _ = two_projects
    idx = KBIndex()
    idx.sync()
    before = len(rows(idx))
    (a / "new.md").write_text("## New\nnew text\n")

    def disk_full(self, tbl):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(KBIndex, "_finish_write", disk_full)
    with pytest.raises(OSError):
        idx.sync()
    assert (kb_home / "db" / "manifest.json").is_file()
    assert len(rows(idx)) >= before          # nothing was wiped
    monkeypatch.undo()
    monkeypatch.setenv("KBHUB_HOME", str(kb_home))
    stats = idx.sync()
    assert stats["rebuilt"] is False
    assert len(rows(idx)) == before + 1 and len(rows(idx, project="A", path="new.md")) == 1


def test_maybe_sync_skips_when_another_session_holds_the_lock(kb_home, two_projects):
    import fcntl
    idx = KBIndex()
    idx.sync()
    with open(kb_home / "db" / "sync.lock", "w") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        t0 = time.time()
        assert idx.maybe_sync(min_interval=0) is None
        assert time.time() - t0 < 2 and idx.last_error is None
        fcntl.flock(other, fcntl.LOCK_UN)
    assert idx.maybe_sync(min_interval=0)["changed"] == 0


def test_vector_size_change_rebuilds_instead_of_failing_forever(kb_home, two_projects, monkeypatch):
    a, _ = two_projects
    idx = KBIndex()
    idx.sync()
    real = Embedder.embed
    monkeypatch.setattr(Embedder, "embed", lambda self, texts, kind="document": [v[:128] for v in real(self, texts, kind)])
    (a / "new.md").write_text("## New\nnew text\n")
    stats = idx.sync()
    assert stats["rebuilt"] is True
    assert {len(r["vector"]) for r in rows(idx)} == {128}


# ---- v0.2: registry projects, scope, fingerprints

def _entry(repo, rel, name, scope="work", **extra):
    return {"name": name, "repo": str(repo), "rel": rel, "scope": scope, **extra}


@pytest.fixture
def repo_kb(kb_home, scan_root, make_repo, make_project, write_registry):
    repo = make_repo(scan_root / "core", {"knowledge-base/billing/decisions.md": DOC, "knowledge-base/billing/old/x.md": "## Old\nlegacy\n",
                                          "README.md": "# repo readme\nnot knowledge\n"})
    make_project(scan_root, "Marker", {"STATE.md": DOC})
    write_registry(kb_home, [_entry(repo, "knowledge-base/billing", "core/billing")])
    return repo


def test_in_repo_registry_kb_indexed(kb_home, repo_kb):
    idx = KBIndex()
    idx.sync()
    assert {r["project"] for r in rows(idx)} == {"core/billing", "Marker"}
    assert {r["path"] for r in rows(idx, project="core/billing")} == {"decisions.md", "old/x.md"}
    assert {r["scope"] for r in rows(idx)} == {"work"}
    assert rows(idx, project="core/billing", path="decisions.md")[0]["doc_type"] == "decisions"
    assert manifest(kb_home)["schema"] == 3
    _touch_later(repo_kb / "knowledge-base/billing/decisions.md", DOC + "\n## Gamma\ng\n")
    assert idx.sync()["changed"] == 1
    (repo_kb / "knowledge-base/billing/old/x.md").unlink()
    assert idx.sync()["removed"] == 1 and len(rows(idx, project="core/billing")) == 3


def test_rule_change_in_registry_retags_only_that_kb(kb_home, repo_kb, write_registry, monkeypatch):
    idx = KBIndex()
    idx.sync()
    embedded = []
    real = Embedder.embed
    monkeypatch.setattr(Embedder, "embed", lambda self, texts, kind="document": embedded.extend(texts) or real(self, texts, kind))
    write_registry(kb_home, [_entry(repo_kb, "knowledge-base/billing", "core/billing",
                                    rules=[{"glob": "old/**", "status": "superseded", "note": "gone"}])])
    stats = idx.sync()
    assert stats["rebuilt"] is False and embedded == []
    assert {r["status"] for r in rows(idx, project="core/billing", path="old/x.md")} == {"superseded"}
    assert {r["status"] for r in rows(idx, project="Marker")} == {"current"}


def test_scope_change_retags(kb_home, repo_kb, write_registry):
    idx = KBIndex()
    idx.sync()
    write_registry(kb_home, [_entry(repo_kb, "knowledge-base/billing", "core/billing", scope="personal")])
    stats = idx.sync()
    assert stats["rebuilt"] is False
    assert {r["scope"] for r in rows(idx, project="core/billing")} == {"personal"}
    assert {r["scope"] for r in rows(idx, project="Marker")} == {"work"}


def test_config_comment_edit_does_not_rebuild(kb_home, repo_kb):
    idx = KBIndex()
    idx.sync()
    cfg_file = kb_home / "config.toml"
    cfg_file.write_text("# just a comment\nscan_depth = 2\n" + cfg_file.read_text())
    stats = idx.sync()
    assert stats["rebuilt"] is False and stats["chunks_written"] == 0
    assert idx.cfg.scan_depth == 2


def test_old_schema_manifest_rebuilds_once(kb_home, repo_kb):
    idx = KBIndex()
    idx.sync()
    m = manifest(kb_home)
    del m["schema"]
    (kb_home / "db" / "manifest.json").write_text(json.dumps(m))
    assert KBIndex().sync()["rebuilt"] is True
    assert KBIndex().sync()["rebuilt"] is False


def test_worktree_copy_indexes_once_from_newest(kb_home, repo_kb, tmp_path, make_worktree):
    wt = make_worktree(repo_kb, tmp_path / "core.worktrees" / "hi",
                       {"knowledge-base/billing/decisions.md": "## Worktree only\nplatypus\n"})
    os.utime(repo_kb / "knowledge-base/billing/decisions.md", (1000, 1000))
    os.utime(repo_kb / "knowledge-base/billing/old/x.md", (1000, 1000))
    os.utime(wt / "knowledge-base/billing/decisions.md", (2000, 2000))
    idx = KBIndex()
    idx.sync()
    got = rows(idx, project="core/billing")
    assert [r["text"] for r in got] == ["## Worktree only\nplatypus"]      # one copy only, the newest
    _touch_later(repo_kb / "knowledge-base/billing/decisions.md", "## Main again\nwombat\n")
    idx.sync()
    texts = sorted(r["text"] for r in rows(idx, project="core/billing"))
    assert texts == ["## Main again\nwombat", "## Old\nlegacy"]


def test_bad_registry_keeps_marker_projects_searchable(kb_home, repo_kb):
    idx = KBIndex()
    idx.sync()
    (kb_home / "registry.json").write_text("{ not json")
    stats = idx.sync()
    assert any("registry.json" in w for w in stats["warnings"])
    assert {r["project"] for r in rows(idx)} == {"Marker"}
    assert idx.search("alpha body")["hits"]


def test_index_from_an_older_schema_rebuilds_once(kb_home, scan_root, make_project):
    from kbhub.config import GlobalConfig
    make_project(scan_root, "Alpha", {"a.md": "# A\nquokka\n"})
    KBIndex(GlobalConfig.load()).sync(wait=True)
    man_path = kb_home / "db" / "manifest.json"
    man = json.loads(man_path.read_text())
    man["schema"] = 2
    man_path.write_text(json.dumps(man))
    assert KBIndex(GlobalConfig.load()).sync(wait=True)["rebuilt"] is True
    assert KBIndex(GlobalConfig.load()).sync(wait=True)["rebuilt"] is False
