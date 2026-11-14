import json

import pytest

from kbhub import prefs


@pytest.fixture
def repo(kb_home, tmp_path, make_repo):
    return make_repo(tmp_path / "code" / "core")


def test_home_is_outside_the_engine_and_overridable(kb_home, tmp_path, monkeypatch):
    assert prefs.prefs_path() == tmp_path / "kbconfig" / "projects.json"
    monkeypatch.delenv("KBHUB_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert prefs.prefs_path() == tmp_path / "home" / ".kbhub-config" / "projects.json"


def test_choice_round_trip(repo, tmp_path, make_repo):
    assert prefs.get_choice(repo) is None and prefs.linked(repo) == []
    assert not prefs.prefs_home().exists()                       # reading never creates the folder
    prefs.set_choice(repo, True)
    assert prefs.get_choice(repo) == "on"
    prefs.set_choice(repo, False)
    assert prefs.get_choice(repo) == "off"
    other = make_repo(tmp_path / "elsewhere" / "core")           # same folder name, another repo
    assert prefs.get_choice(other) is None
    data = json.loads(prefs.prefs_path().read_text())
    assert data["version"] == 1 and list(data["projects"]) == [str(repo)]
    assert data["projects"][str(repo)]["name"] == "core" and data["projects"][str(repo)]["decided"]
    assert sorted(p.name for p in prefs.prefs_home().iterdir()) == ["projects.json", "projects.lock"]


def test_links_are_kept_with_the_choice(repo, tmp_path):
    a, b = tmp_path / "kb" / "core" / "general", tmp_path / "kb" / "core" / "billing"
    a.mkdir(parents=True), b.mkdir(parents=True)
    prefs.link_kb(repo, a)
    prefs.link_kb(repo, a)
    prefs.link_kb(repo, b)
    assert prefs.linked(repo) == [a, b] and prefs.get_choice(repo) is None
    prefs.set_choice(repo, True)
    assert prefs.linked(repo) == [a, b] and prefs.get_choice(repo) == "on"


def test_same_repo_other_spelling(repo):
    shouted = repo.parent / repo.name.upper()
    if not shouted.exists():
        pytest.skip("case-sensitive filesystem")
    prefs.set_choice(repo, True)
    assert prefs.get_choice(shouted) == "on"
    prefs.set_choice(shouted, False)
    assert len(json.loads(prefs.prefs_path().read_text())["projects"]) == 1


def test_broken_file_reads_as_not_asked_and_is_never_overwritten(repo):
    prefs.prefs_home().mkdir(parents=True)
    for bad in ("{ not json", "[]", '{"projects": []}'):
        prefs.prefs_path().write_text(bad)
        assert prefs.get_choice(repo) is None and prefs.linked(repo) == []
        with pytest.raises(ValueError, match="unreadable"):
            prefs.set_choice(repo, True)
        assert prefs.prefs_path().read_text() == bad
    prefs.prefs_path().write_text(json.dumps({"version": 1, "projects": {str(repo): {"kb": "maybe", "kbs": "x"}}}))
    assert prefs.get_choice(repo) is None and prefs.linked(repo) == []
