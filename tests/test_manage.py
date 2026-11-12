import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kbhub import kbregistry as kr
from kbhub.config import GlobalConfig
from kbhub.manage import add_rule, create_kb, register_kb, unregister
from kbhub.registry import discover_projects

LAYOUT_FILES = ("README.md", "STATE.md", "decisions.md", "lessons.md", "open-items.md")
LAYOUT_DIRS = ("runbooks", "research", "plans", "handovers")


@pytest.fixture
def cfg(kb_home):
    return GlobalConfig.load()


@pytest.fixture
def core(scan_root, make_repo):
    return make_repo(scan_root / "projects" / "core", {"knowledge-base/billing/a.md": "# a\nx", "src/app.ts": ""})


def entries(cfg):
    return kr.load_entries(cfg)[0]


def test_register_in_repo_and_idempotent(cfg, core):
    project = register_kb(cfg, core / "knowledge-base" / "billing")
    assert project.name == "core/billing" and project.repo == core and project.scope == "work"
    assert entries(cfg) == [{"name": "core/billing", "repo": str(core), "rel": "knowledge-base/billing", "scope": "work"}]
    add_rule(cfg, "core/billing", "old/**", status="superseded")
    again = register_kb(cfg, core / "knowledge-base" / "billing", scope="personal")
    assert again.scope == "personal" and len(entries(cfg)) == 1
    assert entries(cfg)[0]["rules"] == [{"glob": "old/**", "status": "superseded"}]  # re-registering keeps rules
    assert not list(core.rglob(".kb.toml"))


def test_register_default_names(cfg, core, scan_root, make_repo, tmp_path):
    garden = make_repo(scan_root / "personal" / "garden", {"docs/kb/x.md": "x"})
    assert register_kb(cfg, garden / "docs" / "kb").name == "garden"
    plain = tmp_path / "loose-notes"
    plain.mkdir()
    (plain / "n.md").write_text("x")
    project = register_kb(cfg, plain)
    assert project.name == "loose-notes" and project.repo is None
    assert {"name": "loose-notes", "path": str(plain), "scope": "work"} in entries(cfg)
    assert register_kb(cfg, core / "knowledge-base" / "billing", name="Billing KB").name == "Billing KB"


def test_register_from_worktree_records_main_clone(cfg, core, tmp_path, make_worktree):
    wt = make_worktree(core, tmp_path / "core.worktrees" / "hi", {"knowledge-base/payroll/p.md": "x"})
    project = register_kb(cfg, wt / "knowledge-base" / "payroll")
    assert project.name == "core/payroll" and project.repo == core
    assert project.path == wt / "knowledge-base" / "payroll"
    assert entries(cfg)[0]["repo"] == str(core) and entries(cfg)[0]["rel"] == "knowledge-base/payroll"


def test_register_refuses_roots(cfg, core, scan_root):
    (scan_root / "personal").mkdir(exist_ok=True)
    (core / ".hidden" / "kb").mkdir(parents=True)
    (core / "node_modules" / "pkg" / "kb").mkdir(parents=True)
    for bad, word in [(core, "repo root"), (scan_root, "configured root"), (scan_root / "personal", "configured root"),
                      (Path.home(), "configured root"), (core / ".hidden" / "kb", "hidden"),
                      (core / "node_modules" / "pkg" / "kb", "node_modules"), (core / "nope", "not a folder")]:
        with pytest.raises(ValueError, match=word):
            register_kb(cfg, bad)
    assert entries(cfg) == []


def test_register_name_clash(cfg, core, scan_root, make_project):
    make_project(scan_root, "Atlas", {"a.md": "x"})
    (core / "knowledge-base" / "payroll").mkdir()
    register_kb(cfg, core / "knowledge-base" / "billing")
    with pytest.raises(ValueError, match="already used"):
        register_kb(cfg, core / "knowledge-base" / "payroll", name="core/billing")
    with pytest.raises(ValueError, match="already used"):
        register_kb(cfg, core / "knowledge-base" / "payroll", name="Atlas")
    with pytest.raises(ValueError, match="scope"):
        register_kb(cfg, core / "knowledge-base" / "payroll", scope="office")
    assert len(entries(cfg)) == 1


def test_register_scope_defaults(cfg, scan_root, make_repo):
    garden = make_repo(scan_root / "personal" / "garden", {"docs/kb/x.md": "x"})
    assert register_kb(cfg, garden / "docs" / "kb").scope == "personal"


def test_create_kb_in_repo_layout(cfg, core):
    project = create_kb(cfg, "payroll", location=core)
    folder = core / "knowledge-base" / "payroll"
    assert project.path == folder and project.name == "core/payroll" and project.source == "registry"
    for rel in LAYOUT_FILES:
        assert (folder / rel).is_file(), rel
    for rel in LAYOUT_DIRS:
        assert (folder / rel).is_dir(), rel
    text = "".join((folder / rel).read_text() for rel in LAYOUT_FILES)
    assert "{{" not in text and "core/payroll" in text and "search_kb" in (folder / "README.md").read_text()
    assert not list(core.rglob(".kb.toml")) and not (folder / "CLAUDE.md").exists()
    assert "core/payroll" in discover_projects(GlobalConfig.load())[0]


def test_second_topic_same_repo(cfg, core):
    create_kb(cfg, "payroll", location=core)
    create_kb(cfg, "rostering", location=core)
    register_kb(cfg, core / "knowledge-base" / "billing")
    assert sorted(discover_projects(GlobalConfig.load())[0]) == ["core/billing", "core/payroll", "core/rostering"]


def test_create_kb_from_subfolder_and_worktree(cfg, core, tmp_path, make_worktree):
    (core / "src" / "deep").mkdir(parents=True)
    assert create_kb(cfg, "one", location=core / "src" / "deep").path == core / "knowledge-base" / "one"
    wt = make_worktree(core, tmp_path / "core.worktrees" / "hi")
    project = create_kb(cfg, "two", location=wt)
    assert project.path == wt / "knowledge-base" / "two" and project.repo == core and project.name == "core/two"
    assert not (core / "knowledge-base" / "two").exists()


def test_create_kb_outside_repo_uses_marker_layout(cfg, scan_root):
    project = create_kb(cfg, "side-project")
    assert project.path == scan_root / "side-project" and project.source == "marker"
    assert (project.path / ".kb.toml").is_file() and (project.path / "knowledge-base" / "decisions.md").is_file()
    assert entries(cfg) == []
    personal = create_kb(cfg, "journal", scope="personal")
    assert personal.scope == "personal"


def test_create_kb_refuses_existing_and_bad_topic(cfg, core):
    with pytest.raises(ValueError, match="not empty"):
        create_kb(cfg, "billing", location=core)
    for bad in ("", ".hidden", "a/b", "..", "a\\b"):
        with pytest.raises(ValueError, match="topic"):
            create_kb(cfg, bad, location=core)
    assert entries(cfg) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_create_kb_in_real_git_repo_stages_nothing(cfg, tmp_path):
    repo = tmp_path / "realrepo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    (repo / "app.txt").write_text("code\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    head = git("rev-parse", "HEAD")
    create_kb(cfg, "billing", location=repo)
    register_kb(cfg, repo / "knowledge-base" / "billing")
    assert git("status", "--porcelain").split() == ["??", "knowledge-base/"]
    assert git("diff", "--cached", "--name-only") == "" and git("rev-parse", "HEAD") == head
    assert not list(repo.rglob(".kb.toml"))


def test_add_rule_and_unregister(cfg, core, scan_root, make_project):
    make_project(scan_root, "Atlas", {"a.md": "x"})
    register_kb(cfg, core / "knowledge-base" / "billing")
    before = discover_projects(cfg)[0]["core/billing"].fingerprint
    add_rule(cfg, "core/billing", "old/**", status="superseded", note="gone")
    add_rule(cfg, "CORE/billing", "specs/**", doc_type="spec")
    assert entries(cfg)[0]["rules"] == [{"glob": "old/**", "status": "superseded", "note": "gone"},
                                        {"glob": "specs/**", "doc_type": "spec"}]
    assert discover_projects(cfg)[0]["core/billing"].fingerprint != before
    with pytest.raises(ValueError, match=r"\.kb\.toml"):
        add_rule(cfg, "Atlas", "old/**", status="superseded")
    with pytest.raises(ValueError, match="status or doc_type"):
        add_rule(cfg, "core/billing", "x/**")
    with pytest.raises(ValueError, match="status"):
        add_rule(cfg, "core/billing", "x/**", status="gone")
    with pytest.raises(ValueError, match="Unknown project"):
        add_rule(cfg, "nope", "x/**", status="superseded")
    assert unregister(cfg, "core/billing") is True and unregister(cfg, "core/billing") is False
    assert (core / "knowledge-base" / "billing" / "a.md").is_file() and entries(cfg) == []


# ---- review fixes (v0.2)

def test_reregister_keeps_scope_and_name(cfg, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("x")
    register_kb(cfg, notes, name="mynotes", scope="personal")
    again = register_kb(cfg, notes)                      # no name, no scope: nothing may change
    assert (again.name, again.scope) == ("mynotes", "personal")
    renamed = register_kb(cfg, notes, name="journal")    # rename only: scope must survive
    assert (renamed.name, renamed.scope) == ("journal", "personal")
    assert entries(cfg) == [{"name": "journal", "path": str(notes), "scope": "personal"}]
    assert register_kb(cfg, notes, scope="work").scope == "work"   # an explicit change still works


def test_create_kb_from_worktree_under_hidden_folder(cfg, core, make_worktree):
    wt = make_worktree(core, core / ".claude" / "worktrees" / "feat")
    project = create_kb(cfg, "rostering", location=wt)
    assert project.name == "core/rostering" and project.path == wt / "knowledge-base" / "rostering"
    assert entries(cfg)[0]["rel"] == "knowledge-base/rostering" and entries(cfg)[0]["repo"] == str(core)
    assert register_kb(cfg, wt / "knowledge-base" / "rostering").name == "core/rostering"


def test_create_kb_outside_scan_roots_non_repo(cfg, tmp_path):
    where = tmp_path / "elsewhere"
    where.mkdir()
    project = create_kb(cfg, "side", location=where, name="Side KB", scope="personal")
    assert project.path == where / "side" and (project.name, project.scope) == ("Side KB", "personal")
    assert "Side KB" in discover_projects(GlobalConfig.load())[0]
    plain = create_kb(cfg, "plain", location=where)
    assert plain.name == "plain" and plain.scope == "work"


def test_register_refuses_overlap(cfg, core, scan_root, make_project):
    register_kb(cfg, core / "knowledge-base" / "billing")
    (core / "knowledge-base" / "README.md").write_text("# index")
    with pytest.raises(ValueError, match="overlaps"):
        register_kb(cfg, core / "knowledge-base")                       # parent of a registered folder
    (core / "knowledge-base" / "billing" / "deep").mkdir()
    with pytest.raises(ValueError, match="overlaps"):
        register_kb(cfg, core / "knowledge-base" / "billing" / "deep")  # inside a registered folder
    atlas = make_project(scan_root, "Atlas", {"knowledge-base/d.md": "x"})
    with pytest.raises(ValueError, match="overlaps"):
        register_kb(cfg, atlas / "knowledge-base")                        # inside a marker project
    assert len(entries(cfg)) == 1


def test_scan_reports_parent_of_registered_topics_as_covered(cfg, core):
    from kbhub.manage import scan
    register_kb(cfg, core / "knowledge-base" / "billing")
    (core / "knowledge-base" / "README.md").write_text("# index")
    found = {c["path"]: c for c in scan(cfg)}
    assert found[str(core / "knowledge-base")]["registered"] is not None


def test_create_kb_name_clash_writes_nothing(cfg, core, scan_root, make_repo):
    create_kb(cfg, "billing2", location=core, name="taken")
    other = make_repo(scan_root / "other" / "core")
    with pytest.raises(ValueError, match="already used"):
        create_kb(cfg, "x", location=other, name="taken")
    assert not (other / "knowledge-base").exists()
    for bad in (" ", "a\nb", "billing "):
        with pytest.raises(ValueError, match="topic"):
            create_kb(cfg, bad, location=other)


def test_register_same_folder_other_spelling_is_idempotent(cfg, core):
    folder = core / "knowledge-base" / "billing"
    shouted = core.parent / core.name.upper() / "knowledge-base" / "billing"
    if not shouted.exists():
        pytest.skip("case-sensitive filesystem")
    register_kb(cfg, folder)
    again = register_kb(cfg, shouted)
    assert again.name == "core/billing" and len(entries(cfg)) == 1
