import json
import multiprocessing
import os

import pytest

from kbhub import kbregistry as kr
from kbhub.config import GlobalConfig


@pytest.fixture
def cfg(kb_home):
    return GlobalConfig.load()


def test_missing_file_is_empty(cfg, kb_home):
    assert kr.registry_path(cfg) == kb_home / "registry.json"
    assert kr.load_entries(cfg) == ([], [])


def test_round_trip(cfg, tmp_path):
    a = {"name": "O'Brien notes", "path": str(tmp_path / "O'Brien notes" / "ünï"), "scope": "personal"}
    b = {"name": "core/billing", "repo": str(tmp_path / "core"), "rel": "knowledge-base/billing", "scope": "work",
         "rules": [{"glob": "old/**", "status": "superseded", "note": "n"}], "eval": [{"question": "q", "expect": "e"}]}
    assert kr.update_registry(cfg, lambda entries: entries.extend([a, b]) or len(entries)) == 2
    entries, warnings = kr.load_entries(cfg)
    assert entries == [a, b] and warnings == []
    assert json.loads(kr.registry_path(cfg).read_text())["version"] == 1
    kr.update_registry(cfg, lambda entries: entries.pop(0))
    assert kr.load_entries(cfg)[0] == [b]


def test_bad_registry_is_soft(cfg, tmp_path):
    kr.registry_path(cfg).write_text("{ not json")
    entries, warnings = kr.load_entries(cfg)
    assert entries == [] and len(warnings) == 1 and "registry.json" in warnings[0]
    good = {"name": "ok", "path": str(tmp_path), "scope": "work"}
    bad = [{"name": "", "path": "/x", "scope": "work"}, {"name": "s", "path": "/x", "scope": "office"},
           {"name": "both", "path": "/x", "repo": "/r", "rel": "a", "scope": "work"},
           {"name": "neither", "scope": "work"}, {"name": "dots", "repo": "/r", "rel": "../x", "scope": "work"},
           {"name": "abs", "repo": "/r", "rel": "/etc", "scope": "work"}, "not a dict",
           {"name": "rules", "path": "/x", "scope": "work", "rules": [{"status": "superseded"}]}]
    kr.registry_path(cfg).write_text(json.dumps({"version": 1, "kbs": [good, *bad]}))
    entries, warnings = kr.load_entries(cfg)
    assert entries == [good] and len(warnings) == len(bad)


def test_update_on_bad_json_refuses(cfg):
    kr.registry_path(cfg).write_text("{ not json")
    with pytest.raises(ValueError, match="registry.json"):
        kr.update_registry(cfg, lambda entries: entries.append({"name": "x", "path": "/x", "scope": "work"}))
    assert kr.registry_path(cfg).read_text() == "{ not json"


def test_update_rejects_invalid_entries(cfg):
    with pytest.raises(ValueError):
        kr.update_registry(cfg, lambda entries: entries.append({"name": "x", "scope": "work"}))
    assert kr.load_entries(cfg) == ([], [])


def _append_worker(n):
    cfg = GlobalConfig.load()
    for i in range(20):
        kr.update_registry(cfg, lambda entries: entries.append({"name": f"w{n}-{i}", "path": f"/kb/{n}/{i}", "scope": "work"}))


def test_concurrent_writers(cfg, kb_home):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_worker, args=(n,)) for n in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    entries, warnings = kr.load_entries(cfg)
    assert len(entries) == 40 and warnings == [] and len({e["name"] for e in entries}) == 40


def test_git_helpers(tmp_path, make_repo, make_worktree):
    main = make_repo(tmp_path / "code" / "core")
    wt = make_worktree(main, tmp_path / "code" / "core.worktrees" / "hi")
    (wt / "a" / "b").mkdir(parents=True)
    assert kr.git_root(main / "x" / "y") == main and kr.git_root(wt / "a" / "b") == wt
    assert kr.git_root(tmp_path) is None
    assert kr.main_clone(main) == main and kr.main_clone(wt) == main
    assert kr.checkouts(main) == [main, wt]
    gone = make_worktree(main, tmp_path / "gone")
    (gone / ".git").unlink()
    gone.rmdir()
    assert kr.checkouts(main) == [main, wt]
    assert kr.is_linked_worktree(wt) and not kr.is_linked_worktree(main)


def _touch(path, text, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (when, when))


def test_resolve_entry_newest_copy(tmp_path, make_repo, make_worktree):
    main = make_repo(tmp_path / "core")
    wt = make_worktree(main, tmp_path / "core-wt")
    entry = {"name": "core/billing", "repo": str(main), "rel": "knowledge-base/billing", "scope": "work"}
    assert kr.resolve_entry(entry) is None
    _touch(wt / "knowledge-base" / "billing" / "a.md", "wt", 1_000)
    assert kr.resolve_entry(entry) == wt / "knowledge-base" / "billing"
    _touch(main / "knowledge-base" / "billing" / "a.md", "main", 2_000)
    assert kr.resolve_entry(entry) == main / "knowledge-base" / "billing"
    _touch(wt / "knowledge-base" / "billing" / "sub" / "b.md", "wt newer", 3_000)
    assert kr.resolve_entry(entry) == wt / "knowledge-base" / "billing"
    plain = {"name": "p", "path": str(tmp_path / "plain"), "scope": "work"}
    assert kr.resolve_entry(plain) is None
    (tmp_path / "plain").mkdir()
    assert kr.resolve_entry(plain) == tmp_path / "plain"


def test_resolve_entry_prefers_the_session_checkout(tmp_path, make_repo, make_worktree):
    main = make_repo(tmp_path / "core")
    mine = make_worktree(main, tmp_path / "wt-mine")
    theirs = make_worktree(main, tmp_path / "wt-theirs")
    entry = {"name": "core/billing", "repo": str(main), "rel": "knowledge-base/billing", "scope": "work"}
    _touch(mine / "knowledge-base" / "billing" / "a.md", "mine", 1_000)
    _touch(theirs / "knowledge-base" / "billing" / "a.md", "theirs", 9_000)
    assert kr.resolve_entry(entry) == theirs / "knowledge-base" / "billing"            # no session: newest
    assert kr.resolve_entry(entry, prefer=mine / "src" / "deep") == mine / "knowledge-base" / "billing"
    assert kr.resolve_entry(entry, prefer=main) == theirs / "knowledge-base" / "billing"   # main has no copy
    assert kr.resolve_entry(entry, prefer=tmp_path / "unrelated") == theirs / "knowledge-base" / "billing"
