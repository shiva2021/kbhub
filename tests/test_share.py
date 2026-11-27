"""Shared spaces: two devices, each with its own engine home, config home and folder of knowledge
bases, sharing through a local bare git repository."""
import fcntl
import importlib.util
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from kbhub import share
from kbhub.config import ConfigError, GlobalConfig
from kbhub.recording import record
from kbhub.registry import discover_projects, new_project

DAY = date(2026, 11, 27)
HOOKS = Path(__file__).resolve().parent.parent / "hooks"


class Device:
    def __init__(self, root: Path, monkeypatch):
        self.home, self.config, self.kb, self.patch = root / "home", root / "config", root / "kb", monkeypatch
        for folder in (self.home, self.kb):
            folder.mkdir(parents=True)
        (self.home / "config.toml").write_text(
            f"scan_roots = [{json.dumps(str(self.kb))}]\nexclude_dirs = []\n"
            f"new_project_parent = {json.dumps(str(self.kb))}\nkb_scan_roots = [{json.dumps(str(self.kb))}]\n"
            '[embedding]\nbackend = "wordllama"\n', encoding="utf-8")

    def use(self) -> GlobalConfig:
        """Make this the current device and return its config."""
        self.patch.setenv("KBHUB_HOME", str(self.home))
        self.patch.setenv("KBHUB_CONFIG_HOME", str(self.config))
        return GlobalConfig.load()

    def project(self, name: str):
        return discover_projects(self.use())[0][name]

    def sync(self, **kw) -> dict:
        cfg = self.use()
        return share.sync_space(cfg.spaces[0], **kw)

    def record(self, name: str, kind: str, title: str, **kw) -> dict:
        cfg = self.use()
        return record(cfg, self.project(name), kind, title, kw.pop("body", "Body."), today=DAY, **kw)

    def git(self, *args: str) -> str:
        return share._must(self.use().spaces[0], *args)


@pytest.fixture(autouse=True)
def plain_git(monkeypatch, tmp_path):
    """No user or system git config: the tests must not depend on, or touch, the real one."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def remote(tmp_path) -> str:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(path)], check=True)
    return str(path)


@pytest.fixture
def a(tmp_path, monkeypatch) -> Device:
    return Device(tmp_path / "a", monkeypatch)


@pytest.fixture
def b(tmp_path, monkeypatch) -> Device:
    return Device(tmp_path / "b", monkeypatch)


@pytest.fixture
def pair(a, b, remote):
    """Device A shares the knowledge base `notes`; device B has joined."""
    cfg = a.use()
    new_project(cfg, "notes")
    cfg, _ = share.init(cfg, remote=remote)
    assert share.add(cfg, "notes")["pushed"]
    done = share.join(b.use(), remote)
    assert not done["differing"]
    return a, b


def decisions(dev: Device) -> str:
    return (dev.kb / "notes/knowledge-base/decisions.md").read_text(encoding="utf-8")


def state(dev: Device) -> str:
    return (dev.kb / "notes/STATE.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the allowlist

def test_ignore_text_reincludes_each_level():
    text = share.ignore_text(["kbhub/general", "grabber", "kbhub/usage"])
    lines = text.splitlines()
    assert lines[2:5] == ["# shared: grabber", "# shared: kbhub/general", "# shared: kbhub/usage"]
    body = lines[5:]
    assert body == ["/*", "!/.gitignore", "!/.gitattributes", "!/grabber/", "!/kbhub/", "/kbhub/*",
                    "!/kbhub/general/", "!/kbhub/usage/", ".kbhub-conflicts/", ".DS_Store"]


def test_only_the_allowlisted_knowledge_base_is_tracked(a, remote, make_project):
    cfg = a.use()
    new_project(cfg, "notes")
    make_project(a.kb, "team/general", {"STATE.md": "# STATE\n"}, kb_toml='name = "team/general"\n')
    make_project(a.kb, "team/secret", {"STATE.md": "# never shared\n"}, kb_toml='name = "team/secret"\n')
    make_project(a.kb, "work", {"STATE.md": "# stays local\n"})
    cfg, space = share.init(cfg, remote=remote)
    assert share.shared(space) == [] and a.git("ls-files").split() == [".gitattributes", ".gitignore"]

    share.add(cfg, "team/general")
    share.add(cfg, "notes")
    tracked = a.git("ls-files").split()
    assert "team/general/STATE.md" in tracked and "notes/STATE.md" in tracked
    assert not [f for f in tracked if f.startswith(("work/", "team/secret/"))]
    assert share.shared(space) == ["notes", "team/general"]
    assert not (a.kb / ".git").exists()                  # the git directory lives in the config home
    assert share.git_dir(space) == a.config / "spaces" / "personal.git"

    # nothing about discovery changed: every knowledge base is still found
    assert set(discover_projects(a.use())[0]) == {"notes", "team/general", "team/secret", "work"}


def test_add_pins_the_scope_and_refuses_what_cannot_be_shared(a, remote, make_project, make_repo, write_registry):
    cfg = a.use()
    make_project(a.kb, "plain", {"STATE.md": "# STATE\n"}, kb_toml='name = "plain"\n\n[[rules]]\nglob = "old/**"\nstatus = "superseded"\n')
    repo = make_repo(a.kb.parent / "code", {"knowledge-base/x/README.md": "# x\n"})
    write_registry(a.home, [{"name": "code/x", "repo": str(repo), "rel": "knowledge-base/x", "scope": "work"}])
    make_project(a.kb, "nested", {"STATE.md": "# STATE\n", "vendor/.git/HEAD": "ref: x\n"})
    cfg, _ = share.init(cfg)

    plan = share.preview_add(cfg, "plain")
    assert plan["rel"] == "plain" and plan["scope"] == "work" and "STATE.md" in plan["files"]
    share.add(cfg, "plain")
    assert (a.kb / "plain/.kb.toml").read_text().startswith('name = "plain"\n\nscope = "work"\n\n[[rules]]')
    assert a.project("plain").scope == "work" and len(a.project("plain").config.rules) == 1

    with pytest.raises(ValueError, match="already shared"):
        share.add(cfg, "plain")
    with pytest.raises(ValueError, match="inside a code repo"):
        share.add(cfg, "code/x")
    with pytest.raises(ValueError, match="git repository of its own"):
        share.add(cfg, "nested")
    with pytest.raises(ValueError, match="Unknown knowledge base"):
        share.add(cfg, "nope")


def test_init_refusals(a, remote, pair, tmp_path, make_repo):
    cfg = a.use()
    with pytest.raises(ValueError, match="already exists"):
        share.init(cfg)
    with pytest.raises(ValueError, match="overlaps"):
        share.init(cfg, name="second", path=a.kb / "notes")
    with pytest.raises(ValueError, match="inside a git repository"):
        share.init(cfg, name="second", path=make_repo(tmp_path / "code") / "kb")
    with pytest.raises(ValueError, match="already holds notes"):
        share.init(cfg, name="second", path=tmp_path / "other", remote=remote)
    with pytest.raises(ValueError, match="lower-case"):
        share.init(cfg, name="Bad Name", path=tmp_path / "other")


def test_spaces_in_config(a):
    cfg = a.use()
    before = cfg.index_fingerprint
    cfg, space = share.init(cfg, remote=str(a.kb.parent / "not-there-yet.git"))
    assert cfg.spaces == [space] and cfg.index_fingerprint == before      # no index rebuild
    text = (a.home / "config.toml").read_text()
    assert '[embedding]\nbackend = "wordllama"' in text and text.count("[[spaces]]") == 1
    (a.home / "config.toml").write_text(text + '\n[[spaces]]\nname = "personal"\npath = "/x"\n')
    with pytest.raises(ConfigError, match="two"):
        GlobalConfig.load()


# ------------------------------------------------------------------ join

def test_join_lands_the_notes_with_the_same_scope(pair):
    a, b = pair
    assert (b.kb / "notes/STATE.md").is_file() and not (b.kb / ".git").exists()
    assert b.project("notes").scope == a.project("notes").scope == "work"
    assert share.shared(b.use().spaces[0]) == ["notes"]
    res = b.sync()
    assert res["ok"] and not res["pending"] and not res["pulled"] and not res["pushed"]


def test_join_never_overwrites_a_differing_local_file(a, b, remote):
    cfg = a.use()
    new_project(cfg, "notes")
    cfg, _ = share.init(cfg, remote=remote)
    share.add(cfg, "notes")

    (b.kb / "notes").mkdir()
    (b.kb / "notes/STATE.md").write_text("mine, written before joining\n")
    done = share.join(b.use(), remote)
    assert done["differing"] == ["notes/STATE.md"]
    assert (b.kb / "notes/STATE.md").read_text() == "mine, written before joining\n"
    assert (b.kb / "notes/CLAUDE.md").is_file()          # what was missing did land

    res = b.sync()
    assert res["pending"] == "paused" and "notes/STATE.md" in res["notes"][0]
    assert share.status(b.use())[0]["paused"] == ["notes/STATE.md"]

    (b.kb / "notes/STATE.md").rename(b.kb / "notes-STATE-aside.md")
    res = b.sync()
    assert res["ok"] and not res["pending"]
    assert (b.kb / "notes/STATE.md").read_text() == (a.kb / "notes/STATE.md").read_text()


def test_join_keep_local_shares_the_local_version(a, b, remote):
    cfg = a.use()
    new_project(cfg, "notes")
    cfg, _ = share.init(cfg, remote=remote)
    share.add(cfg, "notes")
    (b.kb / "notes").mkdir()
    (b.kb / "notes/STATE.md").write_text("mine\n")
    share.join(b.use(), remote)
    assert b.sync(keep_local=True)["pushed"]
    a.sync()
    assert (a.kb / "notes/STATE.md").read_text() == "mine\n"


def test_join_refuses_an_empty_remote_and_cleans_up(b, remote):
    with pytest.raises(ValueError, match="no notes yet"):
        share.join(b.use(), remote)
    assert not (b.config / "spaces/personal.git").exists() and not b.use().spaces


# ------------------------------------------------------------------ sync

def test_a_recorded_entry_reaches_the_other_device_and_its_index(pair):
    from kbhub.index import KBIndex
    a, b = pair
    idx = KBIndex(cfg=b.use())
    idx.sync()
    a.record("notes", "decision", "Wombats burrow at dusk", why="They avoid the heat.")
    assert a.sync()["pushed"]

    res = b.sync()
    assert "notes/knowledge-base/decisions.md" in res["pulled"]
    stats = KBIndex(cfg=b.use()).sync()
    assert not stats["rebuilt"] and stats["changed"] >= 1
    hits = KBIndex(cfg=b.use()).search("when do wombats burrow", scope="all")["hits"]
    assert any("Wombats burrow at dusk" in h["text"] for h in hits)


def test_new_hand_written_files_travel(pair):
    a, b = pair
    (a.kb / "notes/plans").mkdir(exist_ok=True)
    (a.kb / "notes/plans/migration.md").write_text("# Migration plan\nStep one.\n")
    (a.kb / "notes/.DS_Store").write_text("junk")
    assert a.sync()["pushed"]
    b.sync()
    assert (b.kb / "notes/plans/migration.md").read_text() == "# Migration plan\nStep one.\n"
    assert not (b.kb / "notes/.DS_Store").exists()


def test_both_devices_record_a_decision_while_apart(pair):
    a, b = pair
    a.record("notes", "decision", "From device A", why="Because A.")
    b.record("notes", "decision", "From device B", why="Because B.")
    assert "## D-001: From device A" in decisions(a) and "## D-001: From device B" in decisions(b)
    assert a.sync()["pushed"]

    res = b.sync()
    assert res["ok"] and res["pushed"] and not res["conflicts"] and not res["pending"]
    text = decisions(b)
    assert "## D-001: From device A" in text and "## D-002: From device B" in text
    assert text.count("## D-00") == 3                     # the template's example plus the two
    assert "D-001, D-002. See `knowledge-base/decisions.md`." in state(b)

    a.sync()
    assert decisions(a) == text and state(a) == state(b)
    assert not (b.kb / "notes/.kbhub-conflicts").exists()


def test_both_devices_record_lessons_while_apart(pair):
    a, b = pair
    a.record("notes", "lesson", "Lesson from A")
    b.record("notes", "lesson", "Lesson from B")
    a.sync()
    assert b.sync()["pushed"]
    text = (b.kb / "notes/knowledge-base/lessons.md").read_text()
    assert "Lesson from A" in text and "Lesson from B" in text and not b.sync()["conflicts"]


def test_superseding_on_one_device_while_the_other_appends(pair):
    a, b = pair
    a.record("notes", "decision", "First", why="Why one.")
    a.sync(), b.sync()
    a.record("notes", "decision", "Second replaces first", why="Why two.", supersedes="D-001")
    b.record("notes", "decision", "Unrelated", why="Why three.")
    a.sync()
    assert b.sync()["ok"]
    text = decisions(b)
    heads = [line for line in text.splitlines() if line.startswith("## D-") and "<short title>" not in line]
    assert heads == ["## D-001: First (superseded 2026-11-27, see D-002)", "## D-002: Second replaces first",
                     "## D-003: Unrelated"]
    assert "D-002, D-003. See" in state(b)


def test_renumbering_follows_the_superseded_mark():
    remote = "# Decisions\n\n## D-001: Old\n- a\n\n## D-002: Theirs\n- b\n"
    merged = ("# Decisions\n\n## D-001: Old (superseded 2026-11-27, see D-002)\n- a\n\n## D-002: Theirs\n- b\n\n"
              "## D-002: Mine\n- c\n- **Supersedes:** D-001\n")
    fixed = share.repair_decisions(merged, remote)
    assert "## D-001: Old (superseded 2026-11-27, see D-003)" in fixed
    assert "## D-002: Theirs" in fixed and "## D-003: Mine" in fixed
    assert share.repair_decisions(fixed, remote) == fixed            # nothing left to mend


def test_a_heading_left_twice_keeps_its_superseded_form():
    merged = "# Decisions\n\n## D-001: Old\n## D-001: Old (superseded 2026-11-27, see D-002)\n- a\n\n## D-002: New\n"
    fixed = share.repair_decisions(merged, merged)
    assert fixed.count("## D-001") == 1 and "(superseded 2026-11-27, see D-002)" in fixed


def test_refresh_state_leaves_a_hand_written_list_alone():
    decisions_text = "# Decisions\n\n## D-001: A\n\n## D-002: B (superseded 2026-11-27, see D-003)\n\n## D-003: C\n"
    shaped = "# STATE\n\n## Active decisions\nD-001. See `d.md`.\n\n## Blockers\nNone.\n"
    assert "D-001, D-003. See `d.md`." in share.refresh_state(shaped, decisions_text)
    by_hand = "# STATE\n\n## Active decisions\nD-001, D-002 (proposed). See `d.md`.\n"
    assert share.refresh_state(by_hand, decisions_text) == by_hand


def test_conflicting_state_keeps_the_remote_and_saves_the_local_copy(pair, monkeypatch):
    a, b = pair
    monkeypatch.setattr(share, "_host", lambda: "laptop-b")
    (a.kb / "notes/STATE.md").write_text(state(a).replace("Nothing started yet.", "A says: importer half built."))
    (b.kb / "notes/STATE.md").write_text(state(b).replace("Nothing started yet.", "B says: exporter is done."))
    (b.kb / "notes/plans").mkdir(exist_ok=True)           # empty folders do not travel
    (b.kb / "notes/plans/b-only.md").write_text("# B's plan\n")
    a.sync()

    res = b.sync()
    assert res["ok"] and res["pushed"] and not res["pending"]
    assert "A says: importer half built." in state(b) and "B says" not in state(b)
    copy = b.kb / "notes/.kbhub-conflicts/STATE.laptop-b.md"
    assert res["saved"] == res["conflicts"] == ["notes/.kbhub-conflicts/STATE.laptop-b.md"]
    assert "B says: exporter is done." in copy.read_text()
    assert "conflict copies waiting" in share.describe(res, quiet=True)
    assert not share._rebasing(b.use().spaces[0])

    # the copy is neither shared nor indexed, and the rest of B's work went out
    assert not [f for f in b.git("ls-files").split() if ".kbhub-conflicts" in f]
    from kbhub.registry import project_files
    assert not [f for f in project_files(b.project("notes"), b.use())[0] if "conflicts" in f]
    a.sync()
    assert (a.kb / "notes/plans/b-only.md").is_file() and not (a.kb / "notes/.kbhub-conflicts").exists()


def test_state_edits_in_different_sections_merge_with_decisions_on_both_sides(pair):
    a, b = pair
    a.record("notes", "decision", "From A", why="A.")
    b.record("notes", "decision", "From B", why="B.")
    (b.kb / "notes/STATE.md").write_text(state(b).replace("## Blockers\nNone.", "## Blockers\nWaiting on the vendor."))
    a.sync()
    res = b.sync()
    assert res["ok"] and not res["conflicts"]
    assert "Waiting on the vendor." in state(b) and "D-001, D-002. See" in state(b)


def test_an_unreachable_remote_never_loses_a_note(pair, monkeypatch):
    a, _ = pair
    real = share._git

    def offline(space, *args, **kw):
        return (124, "", "timed out") if args[0] in ("fetch", "push") else real(space, *args, **kw)

    monkeypatch.setattr(share, "_git", offline)
    a.record("notes", "lesson", "Written offline")
    res = a.sync()
    assert res["ok"] and res["pending"] == "pull" and "did not answer in time" in res["notes"][0]
    assert share.status(a.use())[0]["ahead"] == 1 and not share._rebasing(a.use().spaces[0])

    monkeypatch.setattr(share, "_git", real)
    assert a.sync()["pushed"] and share.status(a.use())[0]["ahead"] == 0


def test_a_rebase_left_behind_is_cleared(pair):
    a, b = pair
    a.record("notes", "lesson", "From A")
    a.sync()
    space = b.use().spaces[0]
    (share.git_dir(space) / "rebase-merge").mkdir()        # as an interrupted run leaves it
    res = b.sync()
    assert res["ok"] and res["pulled"] and not share._rebasing(space)


def test_two_syncs_at_once_one_skips(pair):
    a, _ = pair
    space = a.use().spaces[0]
    with open(share.git_dir(space).parent / "personal.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        res = a.sync()
    assert res["skipped"] and res["ok"] and share.describe(res, quiet=True) == ""
    assert not a.sync()["skipped"]


def test_a_local_folder_that_is_not_shared_yet_is_not_overwritten(pair, make_project):
    a, b = pair
    make_project(a.kb, "extra", {"STATE.md": "# A's extra\n"}, kb_toml='name = "extra"\n')
    make_project(b.kb, "extra", {"STATE.md": "# B's own, different\n"}, kb_toml='name = "extra"\n')
    share.add(a.use(), "extra")
    res = b.sync()
    assert res["pending"] == "pull" and "extra/STATE.md" in res["notes"][0]
    assert (b.kb / "extra/STATE.md").read_text() == "# B's own, different\n"
    (b.kb / "extra").rename(b.kb / "extra-aside")
    assert b.sync()["ok"] and (b.kb / "extra/STATE.md").read_text() == "# A's extra\n"


def test_remove_stops_sharing_and_keeps_the_files_here(pair):
    a, b = pair
    res = share.remove(a.use(), "notes")
    assert res["pushed"] and (a.kb / "notes/STATE.md").is_file()
    assert share.shared(a.use().spaces[0]) == [] and "notes/STATE.md" not in a.git("ls-files").split()
    with pytest.raises(ValueError, match="not shared"):
        share.remove(a.use(), "notes")


def test_sync_without_a_space_set_up(a):
    cfg = a.use()
    from kbhub.config import Space
    res = share.sync_space(Space("ghost", a.kb))
    assert not res["ok"] and "not set up" in res["notes"][0]
    with pytest.raises(ValueError, match="no shared space yet"):
        share.find_space(cfg)


# ------------------------------------------------------------------ hooks, record tool, command line

def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_start_pulls_and_says_so(pair):
    a, b = pair
    hook = _load("session_start")
    a.record("notes", "lesson", "From A")
    a.sync()
    said = hook._sync_spaces(b.use())
    assert said == "Shared space personal: 1 file updated from another device."
    assert "From A" in (b.kb / "notes/knowledge-base/lessons.md").read_text()
    assert hook._sync_spaces(b.use()) == ""              # nothing new: say nothing


def test_session_end_pushes_hand_edits(pair, monkeypatch):
    a, b = pair
    a.use()
    (a.kb / "notes/knowledge-base/open-items.md").write_text("# Open items\n- ask about the wombats\n")
    assert _load("session_end").main() == 0
    b.sync()
    assert "wombats" in (b.kb / "notes/knowledge-base/open-items.md").read_text()


def test_record_tool_pulls_first_and_pushes_after(pair, monkeypatch):
    from kbhub import server
    from kbhub.index import KBIndex
    a, b = pair
    a.record("notes", "decision", "From A", why="A.")
    a.sync()

    b.use()
    monkeypatch.setattr(server, "_index", KBIndex(cfg=b.use(), session_dir=b.kb))
    out = server.record("notes", "decision", "From B", "Body.", why="B.")
    assert "Recorded D-002" in out and "Shared space personal: pushed." in out
    a.sync()
    assert "## D-002: From B" in decisions(a)


def test_command_line(a, b, remote, capsys, monkeypatch):
    from kbhub.cli import main
    cfg = a.use()
    new_project(cfg, "notes")
    assert main(["share", "status"]) == 0 and "No shared space" in capsys.readouterr().out
    assert main(["share", "init", "--remote", remote]) == 0
    assert main(["share", "add", "notes"]) == 1          # not a terminal and no --yes: nothing shared
    assert "nothing was shared" in capsys.readouterr().err
    assert main(["share", "add", "notes", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "notes [work] will be shared" in out and "STATE.md" in out and "pushed" in out
    assert main(["share", "sync"]) == 0 and "up to date" in capsys.readouterr().out
    assert main(["share", "status"]) == 0
    out = capsys.readouterr().out
    assert "shares  notes" in out and "0 to push" in out

    b.use()
    assert main(["share", "join", remote]) == 0
    assert "shares: notes" in capsys.readouterr().out
    assert main(["search", "nothing started", "--scope", "all"]) == 0
    assert "notes · STATE.md" in capsys.readouterr().out


def test_doctor_reports_each_space(pair):
    from kbhub import doctor
    from kbhub.system import Env
    a, _ = pair
    a.use()
    env = Env.real()
    checks = doctor._space_checks(env)
    assert [(c.name, c.ok, c.required) for c in checks] == [("shared space personal", True, False)]
    (a.kb / "notes/plans/new.md").write_text("# new\n")
    assert "not pushed yet" in doctor._space_checks(env)[0].detail


def test_a_rebase_that_stops_without_a_conflict_loses_nothing(pair, monkeypatch):
    """Whatever stops a rebase (no git identity, a hook, a full disk), the local commit stays."""
    a, b = pair
    a.record("notes", "lesson", "From A")
    a.sync()
    b.record("notes", "lesson", "From B, must survive")
    space, real = b.use().spaces[0], share._git

    def stopping(space_, *args, **kw):
        if args == ("rebase", share.UPSTREAM):
            args = ("rebase", "--exec", "false", share.UPSTREAM)
        return real(space_, *args, **kw)

    monkeypatch.setattr(share, "_git", stopping)
    res = b.sync()
    assert res["pending"] == "pull" and "nothing was changed" in res["notes"][-1]
    assert not share._rebasing(space) and share.status(b.use())[0]["ahead"] == 1
    assert "From B, must survive" in (b.kb / "notes/knowledge-base/lessons.md").read_text()

    monkeypatch.setattr(share, "_git", real)
    assert b.sync()["pushed"]
    text = (b.kb / "notes/knowledge-base/lessons.md").read_text()
    assert "From A" in text and "From B, must survive" in text


def test_a_device_without_a_git_identity_can_still_merge(pair):
    a, b = pair                         # the fixture hides every git config, so neither has one
    a.record("notes", "lesson", "From A")
    b.record("notes", "lesson", "From B")
    a.sync()
    assert b.sync()["pushed"]
    assert "kbhub@" in b.git("log", "-1", "--format=%ce")


# ------------------------------------------------------------------ guards

def test_unshared_folders_stay_home_whatever_happens_to_the_ignore_file(pair, make_project):
    a, _ = pair
    make_project(a.kb, "work", {"STATE.md": "# never shared\n"})
    (a.kb / "export.bundle").write_text("big private file")
    ignore = a.kb / ".gitignore"
    good = ignore.read_text()

    ignore.unlink()                                       # deleted: put back from the last commit
    assert a.sync()["ok"] and ignore.read_text() == good

    ignore.write_text("*.log\n")                          # replaced by someone else's: stop
    res = a.sync()
    assert not res["ok"] and "was not written by kbhub" in res["notes"][-1]

    ignore.write_text(good.replace("/*\n", "/*\n!/work/\n", 1))   # edited by hand to let a folder through
    res = a.sync()
    assert not res["ok"] and "refused to share files outside" in res["notes"][-1] and "work/" in res["notes"][-1]

    ignore.write_text(good)
    assert a.sync()["ok"]
    tracked = a.git("ls-files").split()
    assert not [f for f in tracked if f.startswith("work/") or f.endswith(".bundle")]
    listed = subprocess.run(["git", "--git-dir", a.use().spaces[0].remote, "ls-tree", "-r", "--name-only", "main"],
                            capture_output=True, text=True).stdout.split()
    assert sorted(set(f.split("/")[0] for f in listed)) == [".gitattributes", ".gitignore", "notes"]


def test_two_devices_share_different_knowledge_bases_while_apart(pair, make_project):
    a, b = pair
    make_project(a.kb, "alpha", {"STATE.md": "# alpha\n"}, kb_toml='name = "alpha"\n')
    make_project(b.kb, "beta", {"STATE.md": "# beta\n"}, kb_toml='name = "beta"\n')
    real = share._git
    try:                                                  # B adds while offline, A adds and pushes
        share._git = lambda space, *args, **kw: ((124, "", "timed out") if args[0] in ("fetch", "push")
                                                 else real(space, *args, **kw))
        share.add(b.use(), "beta")
    finally:
        share._git = real
    share.add(a.use(), "alpha")

    res = b.sync()
    assert res["ok"] and res["pushed"] and not res["conflicts"]
    assert share.shared(b.use().spaces[0]) == ["alpha", "beta", "notes"]
    assert (b.kb / ".gitignore").read_text() == share.ignore_text(["alpha", "beta", "notes"])
    a.sync()
    assert share.shared(a.use().spaces[0]) == ["alpha", "beta", "notes"]
    assert (a.kb / "beta/STATE.md").read_text() == "# beta\n" and (b.kb / "alpha/STATE.md").is_file()


def test_merge_lists_keeps_additions_and_removals():
    text = lambda *rels: share.ignore_text(list(rels))
    assert share._merge_lists(text("a", "b"), text("a", "b", "c"), text("b", "d")) == ["b", "c", "d"]


def test_a_stale_index_lock_is_cleared(pair):
    a, _ = pair
    (share.git_dir(a.use().spaces[0]) / "index.lock").write_text("")   # a hook killed mid-commit
    a.record("notes", "lesson", "After the crash")
    assert a.sync()["pushed"]


def test_a_conflict_keeps_the_remote_file_byte_for_byte(pair):
    a, b = pair
    (a.kb / "notes/plans").mkdir(exist_ok=True), (b.kb / "notes/plans").mkdir(exist_ok=True)
    (a.kb / "notes/plans/p.md").write_bytes(b"# From A\r\nwindows line ends\r\n")
    (b.kb / "notes/plans/p.md").write_bytes(b"# From B\n")
    a.sync()
    res = b.sync()
    assert res["ok"] and len(res["saved"]) == 1
    assert (b.kb / "notes/plans/p.md").read_bytes() == b"# From A\r\nwindows line ends\r\n"


# ------------------------------------------------------------------ review findings (pull request 6)

def remote_files(dev: Device) -> list[str]:
    out = subprocess.run(["git", "--git-dir", dev.use().spaces[0].remote, "ls-tree", "-r", "--name-only", "main"],
                         capture_output=True, text=True, check=True).stdout
    return out.split()


@pytest.fixture
def trio(pair, make_project):
    """`pair`, plus a second shared knowledge base `other`."""
    a, b = pair
    make_project(a.kb, "other", {"STATE.md": "# other\n"}, kb_toml='name = "other"\n')
    share.add(a.use(), "other")
    b.sync()
    return a, b


def test_remove_keeps_local_files_when_the_remote_is_ahead(trio):
    a, b = trio
    (b.kb / "other/STATE.md").write_text("# other, edited on B\n")
    (b.kb / "notes/STATE.md").write_text(state(b) + "B touched notes too.\n")
    b.sync()
    (a.kb / "notes/knowledge-base/open-items.md").write_text("# Open items\n- not synced yet\n")
    before = sorted(f.relative_to(a.kb).as_posix() for f in (a.kb / "notes").rglob("*") if f.is_file())

    res = share.remove(a.use(), "notes")
    assert res["ok"] and res["pushed"]
    after = sorted(f.relative_to(a.kb).as_posix() for f in (a.kb / "notes").rglob("*") if f.is_file())
    assert after == before
    assert "not synced yet" in (a.kb / "notes/knowledge-base/open-items.md").read_text()
    assert "B touched notes too." in state(a)             # pulled first, so nothing of B's was lost either
    assert share.shared(a.use().spaces[0]) == ["other"]
    assert not [f for f in remote_files(a) if f.startswith("notes/")]
    assert (a.kb / "other/STATE.md").read_text() == "# other, edited on B\n"


def test_a_file_written_in_a_knowledge_base_that_was_just_unshared_is_not_left_tracked(trio):
    a, b = trio
    (b.kb / "notes/plans").mkdir(exist_ok=True)
    (b.kb / "notes/plans/b-new.md").write_text("# written before B knew\n")
    share.remove(a.use(), "notes")
    assert b.sync()["ok"]
    assert not [f for f in remote_files(b) if f.startswith("notes/")]
    assert not [f for f in b.git("ls-files").split() if f.startswith("notes/")]
    (b.kb / "notes/plans/b-new.md").write_text("# edited later\n")     # still on B, no longer shared
    res = b.sync()
    assert res["ok"] and not res["pushed"]


def test_a_failed_remove_changes_nothing_and_can_be_repeated(trio, monkeypatch):
    a, _ = trio
    space = a.use().spaces[0]
    (share.git_dir(space) / "index.lock").write_text("")   # left by a killed hook: cleared, not fatal
    real = share._git

    def failing(space_, *args, **kw):
        return (1, "", "simulated failure") if args[:2] == ("rm", "-r") else real(space_, *args, **kw)

    monkeypatch.setattr(share, "_git", failing)
    with pytest.raises(ValueError, match="simulated failure"):
        share.remove(a.use(), "notes")
    assert share.shared(space) == ["notes", "other"] and "notes/STATE.md" in a.git("ls-files").split()
    monkeypatch.setattr(share, "_git", real)
    assert a.sync()["ok"]
    assert share.remove(a.use(), "notes")["pushed"] and share.shared(space) == ["other"]


def test_remove_refuses_while_the_space_is_out_of_step(trio, monkeypatch):
    a, _ = trio
    real = share._git
    monkeypatch.setattr(share, "_git", lambda space, *args, **kw: (
        (124, "", "timed out") if args[0] == "fetch" else real(space, *args, **kw)))
    with pytest.raises(ValueError, match="still shared"):
        share.remove(a.use(), "notes")
    assert share.shared(a.use().spaces[0]) == ["notes", "other"]


def test_local_files_in_a_folder_another_device_starts_sharing_are_not_sent_unasked(pair, make_project):
    a, b = pair
    make_project(a.kb, "extra", {"STATE.md": "# extra\n"}, kb_toml='name = "extra"\n')
    (b.kb / "extra").mkdir()
    (b.kb / "extra/private-diary.md").write_text("dear diary\n")
    share.add(a.use(), "extra")

    res = b.sync()
    assert res["pending"] == "pull" and "extra/private-diary.md" in res["notes"][0]
    assert "extra/private-diary.md" not in remote_files(b)
    assert not (b.kb / "extra/STATE.md").exists()

    assert b.sync(keep_local=True)["pushed"]              # the owner says so: now it is shared
    assert "extra/private-diary.md" in remote_files(b) and (b.kb / "extra/STATE.md").is_file()


def test_join_pauses_on_local_files_the_shared_notes_do_not_have(a, b, remote):
    cfg = a.use()
    new_project(cfg, "notes")
    cfg, _ = share.init(cfg, remote=remote)
    share.add(cfg, "notes")
    (b.kb / "notes").mkdir()
    (b.kb / "notes/own.md").write_text("only on B\n")
    done = share.join(b.use(), remote)
    assert done["extra"] == ["notes/own.md"] and not done["differing"]
    assert b.sync()["pending"] == "paused" and "notes/own.md" not in remote_files(b)
    (b.kb / "notes/own.md").rename(b.kb / "own-aside.md")
    res = b.sync()
    assert res["ok"] and not res["pending"] and "notes/own.md" not in remote_files(b)


def test_the_users_own_ssh_command_is_left_alone(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("[core]\n\tsshCommand = ssh -i ~/.ssh/id_personal\n")
    share._own_ssh.clear()
    env = {"GIT_CONFIG_GLOBAL": str(config), "HOME": str(tmp_path), "PATH": __import__("os").environ["PATH"]}
    share._quiet_ssh(env)
    assert "GIT_SSH_COMMAND" not in env
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "none"), "HOME": str(tmp_path / "x"),
           "PATH": __import__("os").environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"}
    share._quiet_ssh(env)
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    env = {"GIT_SSH_COMMAND": "ssh -v"}
    share._quiet_ssh(env)
    assert env["GIT_SSH_COMMAND"] == "ssh -v"


def test_a_sync_interrupted_after_the_merge_still_renumbers_next_time(pair, monkeypatch):
    a, b = pair
    a.record("notes", "decision", "From A", why="A.")
    b.record("notes", "decision", "From B", why="B.")
    a.sync()
    real = share.repair

    def killed(*args, **kw):
        raise RuntimeError("hook killed at its timeout")

    monkeypatch.setattr(share, "repair", killed)
    res = b.sync()
    assert not res["ok"] and not res["pushed"]
    assert decisions(b).count("## D-001: From") == 2       # merged, not mended yet, and not pushed

    monkeypatch.setattr(share, "repair", real)
    res = b.sync()
    assert res["ok"] and res["pushed"]
    assert "## D-001: From A" in decisions(b) and "## D-002: From B" in decisions(b)
    assert "D-001, D-002. See" in state(b)


def test_two_decisions_with_the_same_title_stay_two_entries(pair):
    a, b = pair
    a.record("notes", "decision", "Use Postgres", body="A body.", why="A reason.")
    b.record("notes", "decision", "Use Postgres", body="B body.", why="B reason.")
    a.sync()
    assert b.sync()["ok"]
    text = decisions(b)
    assert "## D-001: Use Postgres" in text and "## D-002: Use Postgres" in text
    first, second = text.split("## D-002: Use Postgres")
    assert first.count("- **Decision:**") == 2 and second.count("- **Decision:**") == 1   # template + one, then one
    assert second.count("- **Date:** 2026-11-27") == 1 and "D-001, D-002. See" in state(b)


def test_a_hand_kept_active_decisions_line_survives_an_unrelated_merge(trio):
    a, b = trio
    a.record("notes", "decision", "One", why="w")
    a.record("notes", "decision", "Two", why="w")
    (a.kb / "notes/STATE.md").write_text(state(a).replace("D-001, D-002. See", "D-002. See"))   # trimmed by hand
    a.sync(), b.sync()
    (a.kb / "other/STATE.md").write_text("# other, from A\n")
    (b.kb / "other/a-new-file.md").write_text("# from B\n")
    b.sync()
    assert a.sync()["ok"]
    assert "\nD-002. See" in state(a)


def test_a_ds_store_in_the_conflicts_folder_is_not_a_conflict(pair):
    a, _ = pair
    (a.kb / "notes/.kbhub-conflicts").mkdir()
    (a.kb / "notes/.kbhub-conflicts/.DS_Store").write_text("finder")
    res = a.sync()
    assert res["conflicts"] == [] and share.describe(res, quiet=True) == ""


def test_a_conflict_copy_is_the_local_file_byte_for_byte(pair):
    a, b = pair
    (a.kb / "notes/plans").mkdir(exist_ok=True), (b.kb / "notes/plans").mkdir(exist_ok=True)
    (a.kb / "notes/plans/p.md").write_bytes(b"# From A\n")
    (b.kb / "notes/plans/p.md").write_bytes(b"# From B\r\nwindows\r\n\xff\xfe")
    a.sync()
    res = b.sync()
    assert (b.kb / res["saved"][0]).read_bytes() == b"# From B\r\nwindows\r\n\xff\xfe"
