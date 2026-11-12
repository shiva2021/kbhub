from pathlib import Path

import pytest

from kbhub.config import GlobalConfig
from kbhub.registry import (add_project, discover_projects, is_in_git_worktree, new_project,
                                project_files, project_for_path)


@pytest.fixture
def cfg(kb_home):
    return GlobalConfig.load()


def test_discovers_marked_folders_only(cfg, scan_root, make_project):
    make_project(scan_root, "A", {"STATE.md": "# s\nx"})
    (scan_root / "B").mkdir()
    projects, warnings = discover_projects(cfg)
    assert list(projects) == ["A"]
    assert projects["A"].path == scan_root / "A"
    assert warnings == []


def test_skips_git_repos_and_excluded(cfg, scan_root, make_project):
    (scan_root / "repo" / ".git").mkdir(parents=True)
    make_project(scan_root / "repo", "kb", {})
    make_project(scan_root / "excluded", "kb", {})
    make_project(scan_root, "ok", {})
    cfg.exclude_dirs = [scan_root / "excluded"]
    projects, _ = discover_projects(cfg)
    assert list(projects) == ["ok"]


def test_git_file_marker_counts_as_repo(scan_root):
    (scan_root / "wt" / "sub").mkdir(parents=True)
    (scan_root / "wt" / ".git").write_text("gitdir: /elsewhere\n")
    assert is_in_git_worktree(scan_root / "wt" / "sub")
    assert not is_in_git_worktree(scan_root)


def test_explicit_project_in_git_repo_rejected_with_warning(cfg, tmp_path, make_project):
    (tmp_path / "code" / ".git").mkdir(parents=True)
    kb = make_project(tmp_path / "code", "notes", {})
    cfg.explicit_projects = [kb, tmp_path / "gone"]
    projects, warnings = discover_projects(cfg)
    assert projects == {}
    assert any("git" in w and "notes" in w for w in warnings)
    assert any("gone" in w for w in warnings)


def test_depth_limit(cfg, scan_root, make_project):
    make_project(scan_root / "a" / "b", "three", {})
    make_project(scan_root / "a" / "b" / "c", "four", {})
    cfg.scan_depth = 3
    projects, _ = discover_projects(cfg)
    assert list(projects) == ["three"]


def test_name_collision(cfg, scan_root, make_project):
    make_project(scan_root / "x", "Atlas", {})
    make_project(scan_root / "y", "Atlas", {})
    projects, _ = discover_projects(cfg)
    assert sorted(projects) == ["Atlas", "y/Atlas"]
    assert projects["Atlas"].path == scan_root / "x" / "Atlas"


def test_kb_toml_name_wins(cfg, scan_root, make_project):
    make_project(scan_root, "atlas-notes", {}, kb_toml='name = "Atlas"\n')
    projects, _ = discover_projects(cfg)
    assert list(projects) == ["Atlas"]


def test_bad_kb_toml_still_discovered_with_warning(cfg, scan_root, make_project):
    make_project(scan_root, "broken", {}, kb_toml="name = = 1\n")
    projects, warnings = discover_projects(cfg)
    assert list(projects) == ["broken"]
    assert any(".kb.toml" in w for w in warnings)


def test_project_for_path_picks_deepest(cfg, scan_root, make_project):
    outer = make_project(scan_root, "outer", {"sub/x.md": "x"})
    make_project(outer, "inner", {"y.md": "y"})
    projects, _ = discover_projects(cfg)
    assert project_for_path(projects, outer / "sub").name == "outer"
    assert project_for_path(projects, outer / "inner" / "deep" / "er").name == "inner"
    assert project_for_path(projects, scan_root) is None


def test_add_project_refuses_git(cfg, tmp_path):
    notes = tmp_path / "repo" / "notes"
    notes.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    with pytest.raises(ValueError, match="inside a git repository"):
        add_project(cfg, notes)
    assert not (notes / ".kb.toml").exists()


def test_add_project_refuses_excluded_dir(cfg, scan_root):
    target = scan_root / "projects" / "kb"
    target.mkdir(parents=True)
    cfg.exclude_dirs = [scan_root / "projects"]
    with pytest.raises(ValueError, match="excluded"):
        add_project(cfg, target)
    assert not (target / ".kb.toml").exists()


def test_add_project_inside_scan_root_needs_no_config_entry(cfg, scan_root, kb_home):
    (scan_root / "mine").mkdir()
    before = (kb_home / "config.toml").read_text()
    project = add_project(cfg, scan_root / "mine", name="Mine")
    assert project.name == "Mine"
    assert (kb_home / "config.toml").read_text() == before
    assert "Mine" in discover_projects(GlobalConfig.load())[0]


def test_add_project_outside_roots_persists(cfg, tmp_path):
    outside = tmp_path / "else where" / "O'Brien notes"
    outside.mkdir(parents=True)
    add_project(cfg, outside)
    reloaded = GlobalConfig.load()
    assert reloaded.explicit_projects == [outside]
    assert "O'Brien notes" in discover_projects(reloaded)[0]
    add_project(reloaded, outside)  # idempotent
    assert GlobalConfig.load().explicit_projects == [outside]


def test_add_project_creates_config_when_absent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KBHUB_HOME", str(home))
    cfg = GlobalConfig.load()
    cfg.scan_roots = [tmp_path / "nowhere"]  # keep the real Desktop out of the test
    outside = tmp_path / "kb"
    outside.mkdir()
    add_project(cfg, outside)
    reloaded = GlobalConfig.load()
    assert reloaded.explicit_projects == [outside]
    assert reloaded.scan_depth == 3


def test_new_project_layout(cfg, tmp_path):
    project = new_project(cfg, "billing", parent=tmp_path)
    root = tmp_path / "billing"
    assert project.path == root and project.name == "billing"
    for rel in (".kb.toml", "CLAUDE.md", "STATE.md", "knowledge-base/decisions.md",
                "knowledge-base/lessons.md", "knowledge-base/open-items.md"):
        assert (root / rel).is_file(), rel
    for rel in ("knowledge-base/runbooks", "knowledge-base/research", "plans", "handovers"):
        assert (root / rel).is_dir(), rel
    assert "billing" in (root / "STATE.md").read_text()
    assert "{{" not in (root / "STATE.md").read_text() + (root / "CLAUDE.md").read_text()
    assert "billing" in discover_projects(GlobalConfig.load())[0]


def test_new_project_defaults_to_configured_parent(cfg, scan_root):
    assert new_project(cfg, "fresh").path == scan_root / "fresh"


def test_new_project_refuses_existing_and_git(cfg, tmp_path, scan_root):
    (scan_root / "taken").mkdir()
    (scan_root / "taken" / "file.txt").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        new_project(cfg, "taken")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="inside a git repository"):
        new_project(cfg, "kb", parent=tmp_path / "repo")
    assert not (tmp_path / "repo" / "kb").exists()
    with pytest.raises(ValueError):
        new_project(cfg, "../escape")


def test_project_files_skips_big_and_hidden(cfg, scan_root, make_project):
    root = make_project(scan_root, "P", {
        "a.md": "x", "sub dir/B.MD": "y", ".claude/x.md": "hidden", "node_modules/m/readme.md": "n",
        "notes.txt": "t", "big.md": "x" * 3_000_000,
    })
    make_project(root, "nested", {"inner.md": "belongs to the nested project"})
    projects, _ = discover_projects(cfg)
    files, warnings = project_files(projects["P"], cfg)
    assert sorted(files) == ["a.md", "sub dir/B.MD"]
    assert files["a.md"] == root / "a.md"
    assert len(warnings) == 1 and "big.md" in warnings[0]


# ---- review fixes: symlinks must not defeat the git / exclude guards

def _repo(tmp_path):
    docs = tmp_path / "code" / "core" / "docs"
    docs.mkdir(parents=True)
    (tmp_path / "code" / "core" / ".git").mkdir()
    (docs / "README.md").write_text("# repo readme\nsecret repo text\n")
    return docs


def test_add_project_refuses_symlink_into_git_repo(cfg, scan_root, tmp_path):
    docs = _repo(tmp_path)
    (scan_root / "kb-link").symlink_to(docs, target_is_directory=True)
    with pytest.raises(ValueError, match="inside a git repository"):
        add_project(cfg, scan_root / "kb-link")
    assert not (docs / ".kb.toml").exists()


def test_new_project_refuses_symlinked_parent_into_git_repo(cfg, scan_root, tmp_path):
    docs = _repo(tmp_path)
    (scan_root / "link").symlink_to(docs, target_is_directory=True)
    with pytest.raises(ValueError, match="inside a git repository"):
        new_project(cfg, "kb", parent=scan_root / "link")
    assert not (docs / "kb").exists()


def test_symlink_into_excluded_dir_refused(cfg, scan_root, tmp_path):
    excluded = tmp_path / "projects"
    (excluded / "thing").mkdir(parents=True)
    cfg.exclude_dirs = [excluded]
    (scan_root / "link").symlink_to(excluded / "thing", target_is_directory=True)
    with pytest.raises(ValueError, match="excluded"):
        add_project(cfg, scan_root / "link")
    assert not (excluded / "thing" / ".kb.toml").exists()


def test_explicit_symlink_into_git_repo_not_discovered(cfg, scan_root, tmp_path):
    docs = _repo(tmp_path)
    (docs / ".kb.toml").write_text("")
    link = tmp_path / "elsewhere-link"
    link.symlink_to(docs, target_is_directory=True)
    cfg.explicit_projects = [link]
    projects, warnings = discover_projects(cfg)
    assert projects == {} and any("git" in w for w in warnings)


def test_project_files_skips_symlinks_leaving_the_project(cfg, scan_root, tmp_path, make_project):
    docs = _repo(tmp_path)
    root = make_project(scan_root, "P", {"real.md": "x", "sub/inner.md": "y"})
    (root / "linked.md").symlink_to(docs / "README.md")
    (root / "alias.md").symlink_to(root / "sub" / "inner.md")
    projects, _ = discover_projects(cfg)
    files, _ = project_files(projects["P"], cfg)
    assert sorted(files) == ["alias.md", "real.md", "sub/inner.md"]


# ---- v0.2: registry projects, scope, repo association

import os

from kbhub.registry import repo_projects, resolve_scope, session_scope


def _entry(repo, rel, name, scope="work", **extra):
    return {"name": name, "repo": str(repo), "rel": rel, "scope": scope, **extra}


def test_registry_project_inside_git_repo_discovered(kb_home, scan_root, make_repo, write_registry):
    repo = make_repo(scan_root / "code" / "core", {"knowledge-base/billing/a.md": "# a\nx", "README.md": "repo readme"})
    (repo / "knowledge-base" / "billing" / ".kb.toml").write_text('name = "marker"\n')  # must stay ignored
    write_registry(kb_home, [_entry(repo, "knowledge-base/billing", "core/billing")])
    projects, warnings = discover_projects(GlobalConfig.load())
    assert list(projects) == ["core/billing"] and warnings == []
    p = projects["core/billing"]
    assert p.path == repo / "knowledge-base" / "billing" and p.repo == repo
    assert p.source == "registry" and p.scope == "work" and p.fingerprint.endswith(":work")
    files, _ = project_files(p, GlobalConfig.load())
    assert sorted(files) == ["a.md"]


def test_nested_repo_entry(kb_home, scan_root, make_repo, write_registry):
    repo = make_repo(scan_root / "projects" / "apps" / "storefront", {"knowledge-base/onboarding/n.md": "x"})
    write_registry(kb_home, [_entry(repo, "knowledge-base/onboarding", "storefront/onboarding")])
    projects, _ = discover_projects(GlobalConfig.load())
    assert projects["storefront/onboarding"].path == repo / "knowledge-base" / "onboarding"


def test_worktree_copy_resolves_to_newest_and_indexes_once(kb_home, scan_root, tmp_path, make_repo, make_worktree, write_registry):
    repo = make_repo(scan_root / "core", {"knowledge-base/billing/a.md": "main"})
    wt = make_worktree(repo, tmp_path / "core.worktrees" / "hi", {"knowledge-base/billing/a.md": "worktree"})
    os.utime(repo / "knowledge-base/billing/a.md", (1000, 1000))
    os.utime(wt / "knowledge-base/billing/a.md", (2000, 2000))
    write_registry(kb_home, [_entry(repo, "knowledge-base/billing", "core/billing")])
    projects, _ = discover_projects(GlobalConfig.load())
    assert list(projects) == ["core/billing"]
    assert projects["core/billing"].path == wt / "knowledge-base" / "billing" and projects["core/billing"].repo == repo
    os.utime(repo / "knowledge-base/billing/a.md", (3000, 3000))
    assert discover_projects(GlobalConfig.load())[0]["core/billing"].path == repo / "knowledge-base" / "billing"


def test_unresolved_entry_warns_and_is_not_a_project(kb_home, scan_root, make_repo, write_registry):
    repo = make_repo(scan_root / "core")
    write_registry(kb_home, [_entry(repo, "knowledge-base/gone", "core/gone")])
    projects, warnings = discover_projects(GlobalConfig.load())
    assert projects == {} and any("core/gone" in w and "not present" in w for w in warnings)


def test_registry_entry_replaces_marker_for_same_folder(kb_home, scan_root, make_project, write_registry):
    folder = make_project(scan_root, "notes", {"a.md": "x"}, kb_toml='name = "marker-name"\n')
    write_registry(kb_home, [{"name": "registry-name", "path": str(folder), "scope": "personal"}])
    projects, _ = discover_projects(GlobalConfig.load())
    assert list(projects) == ["registry-name"] and projects["registry-name"].scope == "personal"


def test_scope_resolution(kb_home, scan_root, make_project, make_repo, write_registry):
    make_project(scan_root / "personal", "diary", {"a.md": "x"})
    make_project(scan_root / "personal", "workish", {"a.md": "x"}, kb_toml='scope = "work"\n')
    make_project(scan_root, "Atlas", {"a.md": "x"})
    repo = make_repo(scan_root / "personal" / "garden", {"docs/kb/a.md": "x"})
    write_registry(kb_home, [_entry(repo, "docs/kb", "garden", scope="personal")])
    cfg = GlobalConfig.load()
    projects, _ = discover_projects(cfg)
    assert {n: p.scope for n, p in projects.items()} == {
        "diary": "personal", "workish": "work", "Atlas": "work", "garden": "personal"}
    assert projects["diary"].source == "marker" and projects["diary"].repo is None
    assert resolve_scope(cfg, scan_root / "personal" / "x") == "personal"
    assert resolve_scope(cfg, scan_root / "elsewhere") == "work"
    assert resolve_scope(cfg, scan_root / "personal" / "x", explicit="work") == "work"


def test_session_scope(kb_home, scan_root, tmp_path, make_project, make_repo):
    diary = make_project(scan_root / "personal", "diary", {"a.md": "x"})
    workish = make_project(scan_root / "personal", "workish", {"a.md": "x"}, kb_toml='scope = "work"\n')
    repo = make_repo(scan_root / "projects" / "core")
    cfg = GlobalConfig.load()
    projects, _ = discover_projects(cfg)
    assert session_scope(cfg, diary / "sub", projects) == "personal"
    assert session_scope(cfg, workish, projects) == "work"
    assert session_scope(cfg, scan_root / "personal" / "no-project", projects) == "personal"
    assert session_scope(cfg, repo, projects) == "work"
    assert session_scope(cfg, Path("/"), projects) == "work"
    assert session_scope(cfg, tmp_path / "does-not-exist", projects) == "work"


def test_repo_projects_from_main_and_worktree(kb_home, scan_root, tmp_path, make_repo, make_worktree, write_registry):
    core = make_repo(scan_root / "core", {"knowledge-base/billing/a.md": "x", "knowledge-base/payroll/b.md": "y", "src/x.ts": ""})
    other = make_repo(scan_root / "webapp", {"knowledge-base/ui/a.md": "x"})
    wt = make_worktree(core, tmp_path / "core.worktrees" / "hi")
    write_registry(kb_home, [_entry(core, "knowledge-base/billing", "core/billing"),
                             _entry(core, "knowledge-base/payroll", "core/payroll"),
                             _entry(other, "knowledge-base/ui", "webapp/ui")])
    projects, _ = discover_projects(GlobalConfig.load())
    assert sorted(p.name for p in repo_projects(projects, core / "src")) == ["core/billing", "core/payroll"]
    assert sorted(p.name for p in repo_projects(projects, wt)) == ["core/billing", "core/payroll"]
    assert repo_projects(projects, scan_root) == []


def test_bad_registry_keeps_marker_projects(kb_home, scan_root, make_project):
    make_project(scan_root, "Atlas", {"a.md": "x"})
    (kb_home / "registry.json").write_text("{ not json")
    projects, warnings = discover_projects(GlobalConfig.load())
    assert list(projects) == ["Atlas"] and any("registry.json" in w for w in warnings)


def test_add_project_git_error_points_at_register(cfg, tmp_path):
    notes = tmp_path / "repo" / "notes"
    notes.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    with pytest.raises(ValueError, match="inside a git repository.*register"):
        add_project(cfg, notes)


# ---- review fixes (v0.2)

def test_bad_kb_toml_scope_fails_closed(kb_home, scan_root, make_project):
    make_project(scan_root, "typo", {"a.md": "x"}, kb_toml='scope = "private"\n')
    make_project(scan_root, "badrule", {"a.md": "x"}, kb_toml='scope = "personal"\n[[rules]]\nglob = 3\n')
    make_project(scan_root, "broken", {"a.md": "x"}, kb_toml='scope = "personal"\nname = = 1\n')
    make_project(scan_root, "fine", {"a.md": "x"}, kb_toml='scope = "work"\n[[rules]]\nglob = 3\n')
    projects, warnings = discover_projects(GlobalConfig.load())
    assert {n: p.scope for n, p in projects.items()} == {
        "typo": "personal", "badrule": "personal", "broken": "personal", "fine": "work"}
    assert len(warnings) == 4


def test_discovery_prefers_session_checkout(kb_home, scan_root, tmp_path, make_repo, make_worktree, write_registry):
    repo = make_repo(scan_root / "core", {"knowledge-base/billing/a.md": "main"})
    wt = make_worktree(repo, tmp_path / "wt", {"knowledge-base/billing/a.md": "worktree"})
    os.utime(wt / "knowledge-base/billing/a.md", (1000, 1000))
    os.utime(repo / "knowledge-base/billing/a.md", (9000, 9000))
    write_registry(kb_home, [_entry(repo, "knowledge-base/billing", "core/billing")])
    cfg = GlobalConfig.load()
    assert discover_projects(cfg)[0]["core/billing"].path == repo / "knowledge-base" / "billing"
    assert discover_projects(cfg, session_dir=wt / "src")[0]["core/billing"].path == wt / "knowledge-base" / "billing"
