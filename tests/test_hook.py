import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kbhub.config import GlobalConfig

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "session_start.py"


@pytest.fixture
def hook():
    spec = importlib.util.spec_from_file_location("session_start", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def projects(kb_home, scan_root, make_project):
    a = make_project(scan_root, "Alpha", {"STATE.md": "# STATE: Alpha\n\n## Status\n" + "Alpha is half built. " * 20 + "\n"},
                     kb_toml='name = "Atlas"\n')
    b = make_project(scan_root, "Beta", {"STATE.md": "# STATE\n\nBeta is done.\n"})
    make_project(scan_root, "NoState", {})
    return a, b


def run_main(hook, monkeypatch, capsys, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    code = hook.main()
    return code, capsys.readouterr().out


def test_inside_project_includes_state(hook, projects, kb_home):
    a, _ = projects
    (a / "sub" / "deeper").mkdir(parents=True)
    ctx = hook.build_context(a / "sub" / "deeper", GlobalConfig.load())
    assert '"Atlas"' in ctx and str(a) in ctx and "Alpha is half built." in ctx
    assert "search_kb" in ctx and "Beta is done" not in ctx
    (a / "STATE.md").write_text("# STATE\n" + "x" * 10_000)
    ctx = hook.build_context(a, GlobalConfig.load())
    assert "(truncated" in ctx
    assert ctx.count("x") <= 6000 and ctx.count("x") > 5900


def test_inside_project_without_state_file(hook, projects, scan_root):
    ctx = hook.build_context(scan_root / "NoState", GlobalConfig.load())
    assert '"NoState"' in ctx and "search_kb" in ctx


def test_outside_lists_projects(hook, projects, tmp_path):
    repo = tmp_path / "code" / "core"
    (repo / ".git").mkdir(parents=True)
    ctx = hook.build_context(repo, GlobalConfig.load())
    lines = {l.split(" — ")[0]: l for l in ctx.splitlines() if l.startswith("- ")}
    assert set(lines) == {"- Atlas", "- Beta", "- NoState"}
    assert lines["- Beta"].endswith("Beta is done.")
    summary = lines["- Atlas"].split(" — ", 2)[2]
    assert summary.startswith("Alpha is half built.") and len(summary) <= 120
    assert "search_kb" in ctx and "# STATE" not in ctx


def test_main_prints_hook_json(hook, projects, monkeypatch, capsys):
    a, _ = projects
    code, out = run_main(hook, monkeypatch, capsys, json.dumps({"cwd": str(a), "hook_event_name": "SessionStart"}))
    payload = json.loads(out)
    assert code == 0 and payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert '"Atlas"' in payload["hookSpecificOutput"]["additionalContext"]
    monkeypatch.chdir(a)
    code, out = run_main(hook, monkeypatch, capsys, "")  # no stdin payload: falls back to os.getcwd()
    assert code == 0 and '"Atlas"' in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_hook_never_fails(hook, projects, kb_home, monkeypatch, capsys):
    (kb_home / "config.toml").write_text("scan_roots = = broken\n")
    code, out = run_main(hook, monkeypatch, capsys, json.dumps({"cwd": "/"}))
    assert code == 0 and out == ""
    code, out = run_main(hook, monkeypatch, capsys, "not json at all")
    assert code == 0


def test_hook_writes_nothing(hook, projects, kb_home, scan_root, monkeypatch, capsys):
    def snapshot():
        return {str(p): p.stat().st_mtime_ns for root in (kb_home, scan_root) for p in sorted(root.rglob("*"))}
    before = snapshot()
    run_main(hook, monkeypatch, capsys, json.dumps({"cwd": str(projects[0])}))
    run_main(hook, monkeypatch, capsys, json.dumps({"cwd": "/"}))
    assert snapshot() == before


def _run_hook(cwd, extra=()):
    return subprocess.run([sys.executable, *extra, str(HOOK)], input=json.dumps({"cwd": str(cwd)}),
                          capture_output=True, text=True, env=dict(os.environ), timeout=30)


def test_hook_imports_no_lancedb(projects):
    proc = _run_hook(projects[0], extra=("-X", "importtime"))
    assert proc.returncode == 0 and "hookSpecificOutput" in proc.stdout
    imported = proc.stderr
    assert "kbhub.registry" in imported
    for heavy in ("lancedb", "numpy", "pyarrow", "pandas", "requests", "wordllama", "mcp"):
        assert f" {heavy}\n" not in imported and f" {heavy}." not in imported, heavy


def test_hook_fast(projects):
    def timed():
        t0 = time.perf_counter()
        proc = _run_hook(projects[0])
        return time.perf_counter() - t0, proc
    elapsed, proc = timed()
    if elapsed >= 0.5:  # one retry for a cold cache or a busy machine
        elapsed, proc = timed()
    assert proc.returncode == 0 and "Atlas" in proc.stdout
    assert elapsed < 0.5


# ---- v0.2: repo-aware context and scope

@pytest.fixture
def world(kb_home, scan_root, tmp_path, make_project, make_repo, make_worktree, write_registry):
    core = make_repo(scan_root / "projects" / "core", {
        "src/app.ts": "", "knowledge-base/billing/STATE.md": "# STATE\n\n## Status\nBilling ledger half migrated.\n",
        "knowledge-base/payroll/README.md": "# payroll\n"})
    webapp = make_repo(scan_root / "projects" / "webapp", {"src/x.vue": ""})
    wt = make_worktree(core, tmp_path / "core.worktrees" / "hi", {"src/app.ts": ""})
    make_project(scan_root, "Atlas", {"STATE.md": "# STATE\n\nAtlas is live.\n"})
    diary = make_project(scan_root / "personal", "Diary", {"STATE.md": "# STATE\n\nPrivate diary state.\n"})
    write_registry(kb_home, [
        {"name": "core/billing", "repo": str(core), "rel": "knowledge-base/billing", "scope": "work"},
        {"name": "core/payroll", "repo": str(core), "rel": "knowledge-base/payroll", "scope": "work"}])
    return {"core": core, "webapp": webapp, "wt": wt, "diary": diary, "scan": scan_root}


def test_repo_session_lists_repo_kbs_first(hook, world):
    ctx = hook.build_context(world["core"] / "src", GlobalConfig.load())
    assert "Session scope: work" in ctx and "search_kb" in ctx
    mine, others = ctx.index("core/billing"), ctx.index("- Atlas")
    assert ctx.index("this code base (core)") < mine < others
    assert "Billing ledger half migrated." in ctx and "- core/payroll" in ctx
    assert "Diary" not in ctx and "Private diary" not in ctx


def test_worktree_session_maps_to_main_clone(hook, world):
    ctx = hook.build_context(world["wt"] / "src", GlobalConfig.load())
    assert "this code base (core)" in ctx and "- core/billing" in ctx and "- core/payroll" in ctx


def test_inside_repo_kb_shows_its_state(hook, world):
    ctx = hook.build_context(world["core"] / "knowledge-base" / "billing" / "runbooks", GlobalConfig.load())
    assert '"core/billing"' in ctx and "Billing ledger half migrated." in ctx and "Session scope: work" in ctx


def test_unregistered_folder_hint(hook, world, kb_home, write_registry):
    (world["webapp"] / "docs" / "kb").mkdir(parents=True)
    (world["webapp"] / "docs" / "kb" / "a.md").write_text("x")
    ctx = hook.build_context(world["webapp"], GlobalConfig.load())
    assert "not registered" in ctx and "register_kb" in ctx and str(world["webapp"] / "docs" / "kb") in ctx
    write_registry(kb_home, [{"name": "webapp", "repo": str(world["webapp"]), "rel": "docs/kb", "scope": "work"}])
    ctx = hook.build_context(world["webapp"], GlobalConfig.load())
    assert "not registered" not in ctx and "- webapp" in ctx


def test_repo_without_kb_mentions_create_kb(hook, world):
    ctx = hook.build_context(world["webapp"] / "src", GlobalConfig.load())
    assert "no knowledge base yet" in ctx and "create_kb" in ctx and "- Atlas" in ctx
    assert "offer" in ctx and "do not create one unasked" in ctx
    assert "not registered" not in ctx


def test_scope_line_and_personal_hidden_from_work_session(hook, world):
    work = hook.build_context(world["scan"], GlobalConfig.load())
    assert "Session scope: work" in work and "- Atlas" in work and "Diary" not in work
    personal = hook.build_context(world["scan"] / "personal", GlobalConfig.load())
    assert "Session scope: personal" in personal and "- Diary" in personal
    assert "Atlas" not in personal and "core/billing" not in personal
    inside = hook.build_context(world["diary"], GlobalConfig.load())
    assert '"Diary"' in inside and "Session scope: personal" in inside


def test_hook_with_registry_is_fast_and_light(world):
    proc = _run_hook(world["core"] / "src", extra=("-X", "importtime"))
    assert proc.returncode == 0 and "core/billing" in proc.stdout
    for heavy in ("lancedb", "numpy", "pyarrow", "pandas", "requests", "wordllama", "mcp"):
        assert f" {heavy}\n" not in proc.stderr and f" {heavy}." not in proc.stderr, heavy
    t0 = time.perf_counter()
    _run_hook(world["core"] / "src")
    assert time.perf_counter() - t0 < 0.5 or (time.perf_counter() - t0 < 1.0 and _run_hook(world["core"]).returncode == 0)


def test_hook_points_at_the_session_checkouts_own_copy(hook, world, tmp_path, make_worktree):
    wt = world["wt"]
    (wt / "knowledge-base" / "billing").mkdir(parents=True)
    (wt / "knowledge-base" / "billing" / "STATE.md").write_text("# STATE\n\nWorktree copy state.\n")
    os.utime(wt / "knowledge-base" / "billing" / "STATE.md", (1000, 1000))   # older than the main clone's
    ctx = hook.build_context(wt / "src", GlobalConfig.load())
    assert str(wt / "knowledge-base" / "billing") in ctx and "Worktree copy state." in ctx
