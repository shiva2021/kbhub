"""`kbhub doctor`, `update` and `uninstall`."""
from kbhub import doctor, setup as su
from kbhub.cli import main
from conftest import FakeRun


def by_name(results):
    return {c.name: c for c in results}


def test_doctor_on_an_empty_machine_names_every_gap_with_a_fix(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=()))
    results = doctor.checks(env)
    lines, ok = doctor.report(results)
    assert ok is False
    failed = [c for c in results if not c.ok]
    assert {"uv (runs kbhub)", "Claude Code", "session-start hook", "skill", "rule in CLAUDE.md", "index"} <= {c.name for c in failed}
    assert all(c.fix for c in failed)
    assert any(line.strip().startswith("fix:") for line in lines)


def test_doctor_after_setup_is_green(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "brew")), yes=True)
    su.run_wizard(env, ui)
    (home / ".kbhub" / "db").mkdir(exist_ok=True)
    (home / ".kbhub" / "db" / "manifest.json").write_text("{}")
    results = doctor.checks(env)
    assert [c.name for c in results if not c.ok] == []
    assert doctor.report(results)[1] is True


def test_doctor_optional_gaps_do_not_fail_it(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "brew")), yes=True, path="/usr/bin")
    su.run_wizard(env, ui)
    (home / ".kbhub" / "db").mkdir(exist_ok=True)
    (home / ".kbhub" / "db" / "manifest.json").write_text("{}")
    (home / ".zshrc").unlink()
    run.ollama_up = False                      # Ollama stopped, and ~/.local/bin is not on PATH
    results = by_name(doctor.checks(env))
    assert not results["search quality"].ok and not results["`kbhub` command"].ok
    lines, ok = doctor.report(list(results.values()))
    assert ok is True and any(line.startswith("  ! search quality") for line in lines)


def test_doctor_reports_a_broken_config(machine):
    home, build = machine
    (home / ".kbhub").mkdir()
    (home / ".kbhub" / "config.toml").write_text("scan_roots = [\n")
    env, ui, run, said = build()
    c = by_name(doctor.checks(env))["settings (config.toml)"]
    assert not c.ok and "config.toml" in c.detail


def test_cli_doctor_exit_code(machine, monkeypatch, capsys):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=()))
    monkeypatch.setattr("kbhub.system.Env.real", classmethod(lambda cls: env))
    assert main(["doctor"]) == 1
    assert "✗ Claude Code" in capsys.readouterr().out


def test_cli_uninstall_removes_wiring_and_launcher_but_keeps_data(machine, monkeypatch, capsys):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "brew")), yes=True)
    su.run_wizard(env, ui)
    monkeypatch.setattr("kbhub.system.Env.real", classmethod(lambda cls: env))
    assert main(["uninstall"]) == 0
    assert not (home / ".claude" / "skills" / "kbhub").exists() and not (home / ".local" / "bin" / "kbhub").exists()
    assert "kbhub" not in (home / ".claude" / "CLAUDE.md").read_text() if (home / ".claude" / "CLAUDE.md").exists() else True
    assert (home / ".kbhub" / "config.toml").is_file()
    assert run.ran("claude mcp remove kbhub --scope user")


def test_cli_update_pulls_syncs_and_rewires(machine, monkeypatch, capsys):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "git")))
    monkeypatch.setattr("kbhub.system.Env.real", classmethod(lambda cls: env))
    assert main(["update"]) == 0
    flat = run.calls_flat()
    assert "pull --ff-only" in flat and "/fake/bin/uv sync" in flat
    assert flat.index("pull --ff-only") < flat.index("uv sync") < flat.index("kbhub.setup --wire-only")


def test_cli_update_stops_when_the_pull_fails(machine, monkeypatch, capsys):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "git"), fail=("pull --ff-only",)))
    monkeypatch.setattr("kbhub.system.Env.real", classmethod(lambda cls: env))
    assert main(["update"]) == 1
    assert not run.ran("uv sync") and "simulated failure" in capsys.readouterr().err


def test_doctor_counts_every_knowledge_base_whatever_folder_it_runs_from(machine):
    home, build = machine
    env, ui, run, said = build(run=FakeRun(present=("uv", "claude", "brew")), yes=True)
    su.run_wizard(env, ui)
    (home / ".kbhub" / "db").mkdir(exist_ok=True)
    (home / ".kbhub" / "db" / "manifest.json").write_text("{}")
    doctor.checks(env)
    assert run.ran("kbhub.cli projects --no-sync --scope all")
