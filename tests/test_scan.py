import pytest

from kbhub.config import GlobalConfig
from kbhub.manage import register_kb, scan


@pytest.fixture
def cfg(kb_home):
    return GlobalConfig.load()


def paths(found, scan_root):
    return [str(__import__("pathlib").Path(c["path"]).relative_to(scan_root)) for c in found]


def test_finds_kb_folders_in_nested_repos(cfg, scan_root, make_repo):
    ng = make_repo(scan_root / "projects" / "apps" / "storefront", {"knowledge-base/onboarding/a.md": "x", "knowledge-base/onboarding/sub/b.md": "y"})
    garden = make_repo(scan_root / "personal" / "garden", {"docs/kb/x.md": "x", "docs/other.md": "no"})
    found = scan(cfg)
    assert paths(found, scan_root) == ["personal/garden/docs/kb", "projects/apps/storefront/knowledge-base/onboarding"]
    kb, onboarding = found
    assert kb == {"path": str(garden / "docs" / "kb"), "repo": str(garden), "name": "garden", "scope": "personal",
                  "files": 1, "registered": None}
    assert onboarding["name"] == "storefront/onboarding" and onboarding["repo"] == str(ng)
    assert onboarding["files"] == 2 and onboarding["scope"] == "work"


def test_topics_vs_single_folder(cfg, scan_root, make_repo):
    make_repo(scan_root / "single", {"knowledge-base/overview.md": "x", "knowledge-base/sub/more.md": "y"})
    make_repo(scan_root / "topics", {"knowledge-base/billing/a.md": "x", "knowledge-base/payroll/deep/b.md": "y",
                                     "knowledge-base/empty/readme.txt": "no markdown"})
    (scan_root / "hollow" / "knowledge-base").mkdir(parents=True)
    found = scan(cfg)
    assert paths(found, scan_root) == ["single/knowledge-base", "topics/knowledge-base/billing", "topics/knowledge-base/payroll"]
    assert found[0]["files"] == 2 and found[0]["name"] == "single"


def test_skips_noise(cfg, scan_root, tmp_path, make_repo, make_worktree):
    repo = make_repo(scan_root / "core", {
        "knowledge-base/real/a.md": "x", "node_modules/pkg/knowledge-base/a.md": "x", ".hidden/knowledge-base/a.md": "x",
        "dist/knowledge-base/a.md": "x", "build/knowledge-base/a.md": "x", "vendor/knowledge-base/a.md": "x",
        "knowledge-base/real/.private/secret.md": "x", "knowledge-base/real/node_modules/m.md": "x"})
    make_worktree(repo, scan_root / "core.worktrees" / "hi", {"knowledge-base/real/a.md": "x"})
    found = scan(cfg)
    assert paths(found, scan_root) == ["core/knowledge-base/real"] and found[0]["files"] == 1


def test_depth_limit(cfg, scan_root, make_repo):
    make_repo(scan_root / "a", {"knowledge-base/t/x.md": "x"})
    make_repo(scan_root / "b" / "c" / "d", {"knowledge-base/t/x.md": "x"})
    cfg.kb_scan_depth = 2
    assert paths(scan(cfg), scan_root) == ["a/knowledge-base/t"]


def test_registered_flag(cfg, scan_root, tmp_path, make_repo, make_worktree, make_project):
    core = make_repo(scan_root / "core", {"knowledge-base/billing/a.md": "x", "knowledge-base/payroll/b.md": "y"})
    make_project(scan_root, "Atlas", {"knowledge-base/decisions.md": "x"})
    wt = make_worktree(core, tmp_path / "wt", {"knowledge-base/billing/a.md": "newer in worktree"})
    __import__("os").utime(core / "knowledge-base/billing/a.md", (1000, 1000))
    register_kb(cfg, core / "knowledge-base" / "billing")   # resolves to the newer worktree copy
    found = {c["path"]: c for c in scan(cfg)}
    assert found[str(core / "knowledge-base" / "billing")]["registered"] == "core/billing"
    assert found[str(core / "knowledge-base" / "payroll")]["registered"] is None
    assert found[str(scan_root / "Atlas" / "knowledge-base")]["registered"] == "Atlas"
    assert list(found)[0] == str(core / "knowledge-base" / "payroll")   # unregistered first


def test_explicit_root_overrides_config_roots(cfg, scan_root, tmp_path, make_repo):
    make_repo(scan_root / "inside", {"knowledge-base/t/x.md": "x"})
    outside = make_repo(tmp_path / "outside", {"docs/kb/x.md": "x"})
    found = scan(cfg, root=tmp_path / "outside")
    assert [c["path"] for c in found] == [str(outside / "docs" / "kb")]
    assert scan(cfg, root=tmp_path / "nowhere") == []


def test_home_folder_system_dirs_are_never_walked(tmp_path, monkeypatch, kb_home):
    """With the home folder as a scan root, macOS media and system folders are skipped."""
    import json as _json
    from kbhub.config import GlobalConfig
    from kbhub.manage import scan
    from kbhub.registry import discover_projects

    home = tmp_path / "userhome"
    for sub in ("Library/notes", "Music/notes", "Documents/notes", "Library/x/knowledge-base", "code/knowledge-base"):
        (home / sub).mkdir(parents=True)
    for sub in ("Library/notes", "Music/notes", "Documents/notes"):
        (home / sub / ".kb.toml").write_text("")
        (home / sub / "a.md").write_text("# a\n")
    (home / "Library/x/knowledge-base/a.md").write_text("# a\n")
    (home / "code/knowledge-base/a.md").write_text("# a\n")
    monkeypatch.setenv("HOME", str(home))
    (kb_home / "config.toml").write_text(
        f"scan_roots = [{_json.dumps(str(home))}]\nkb_scan_roots = [{_json.dumps(str(home))}]\n"
        '[embedding]\nbackend = "wordllama"\n')
    cfg = GlobalConfig.load()
    projects, _ = discover_projects(cfg)
    assert [p.path.parent.name for p in projects.values()] == ["Documents"]
    assert [c["path"] for c in scan(cfg)] == [str(home / "code" / "knowledge-base")]
