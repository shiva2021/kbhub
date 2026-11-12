import os

import pytest

from kbhub.embedder import Embedder, EmbedderUnavailable
from kbhub.format import format_hits
from kbhub.index import KBIndex

FILES_A = {
    "knowledge-base/decisions.md": (
        "# Decisions\n\n## D-009: Channel id format\nSite chats stay on <clientId>-<swId> channels. See PE-472.\n\n"
        "## D-002: Site channel migration (superseded 15 Oct)\nMigrate site chats to <siteId>-<swId> channels.\n"),
    "STATE.md": "# STATE\n\n## Status\nBuilding the wombat importer.\n",
    "old/plan.md": "## Old channel plan\nUse the channel migration runbook for channels.\n",
}
KB_A = '[[rules]]\nglob = "old/**"\nstatus = "superseded"\nnote = "Dropped in D-009"\n'
FILES_B = {"knowledge-base/lessons.md": "# Lessons\n\n## Channel imports are slow\nBatch the channel import in groups of 100.\n"}


@pytest.fixture
def idx(kb_home, scan_root, make_project):
    make_project(scan_root, "A", FILES_A, kb_toml=KB_A)
    make_project(scan_root, "B", FILES_B)
    index = KBIndex()
    index.sync()
    return index


def keys(hits):
    return [(h["project"], h["path"], h["heading"].split(" > ")[-1]) for h in hits]


def test_search_returns_relevant_hit_with_all_fields(idx):
    res = idx.search("channel id format")
    top = res["hits"][0]
    assert (top["project"], top["path"]) == ("A", "knowledge-base/decisions.md")
    assert top["heading"] == "Decisions > D-009: Channel id format"
    assert set(top) == {"project", "path", "heading", "start_line", "end_line", "status", "status_note",
                        "doc_type", "text", "tokens"}
    assert top["doc_type"] == "decisions" and res["keyword_only"] is False


def test_superseded_hidden_by_default(idx):
    res = idx.search("site channel migration")
    assert all(h["status"] != "superseded" for h in res["hits"])
    hidden = {(s["project"], s["path"]) for s in res["superseded_matches"]}
    assert ("A", "old/plan.md") in hidden and ("A", "knowledge-base/decisions.md") in hidden
    assert len(res["superseded_matches"]) <= 3
    assert set(res["superseded_matches"][0]) == {"project", "path", "heading", "status_note"}
    res = idx.search("site channel migration", include_superseded=True)
    assert ("A", "old/plan.md", "Old channel plan") in keys(res["hits"])
    assert res["superseded_matches"] == []


def test_project_and_doc_type_filters(idx):
    assert {h["project"] for h in idx.search("channel", project="B")["hits"]} == {"B"}
    assert {h["project"] for h in idx.search("channel", project="b")["hits"]} == {"B"}  # case-insensitive
    assert {h["doc_type"] for h in idx.search("channel", doc_type="lessons")["hits"]} == {"lessons"}
    assert idx.search("channel", project="B", doc_type="decisions")["hits"] == []
    with pytest.raises(ValueError, match=r"Unknown project 'nope'.*A, B"):
        idx.search("channel", project="nope")


def test_limit_clamped(idx):
    assert len(idx.search("channel", limit=1)["hits"]) == 1
    assert len(idx.search("channel", limit=0)["hits"]) == 1
    assert len(idx.search("channel", limit=99, include_superseded=True)["hits"]) <= 15


@pytest.mark.parametrize("keyword_only", [False, True])
def test_search_survives_punctuation(kb_home, idx, keyword_only):
    index = KBIndex(keyword_only=keyword_only)
    for q in ["<clientId>-<swId>", '"quoted"', "PE-472?", "a OR b", "it's (really) [odd]: 100% & more;", "?!", "AND", "*"]:
        res = index.search(q)
        assert isinstance(res["hits"], list), q
    assert ("A", "knowledge-base/decisions.md", "D-009: Channel id format") in keys(index.search("PE-472?")["hits"])
    for empty in ["", "   "]:
        with pytest.raises(ValueError, match="empty query"):
            index.search(empty)


def test_keyword_only_flag(kb_home, idx):
    ro = KBIndex(read_only=True, keyword_only=True)
    res = ro.search("wombat importer")
    assert res["keyword_only"] is True
    assert keys(res["hits"])[0] == ("A", "STATE.md", "Status")
    assert format_hits(res).startswith("(keyword-only search: Ollama unavailable)\n\n")


def test_embedder_down_falls_back_to_keywords(idx, monkeypatch):
    def down(self, texts, kind="document"):
        raise EmbedderUnavailable("no ollama")

    monkeypatch.setattr(Embedder, "embed", down)
    res = idx.search("wombat importer")
    assert res["keyword_only"] is True and keys(res["hits"])[0] == ("A", "STATE.md", "Status")


def test_note_written_while_embedder_down_is_keyword_searchable(idx, scan_root, monkeypatch):
    real = Embedder.embed
    monkeypatch.setattr(Embedder, "embed", lambda self, texts, kind="document": (_ for _ in ()).throw(EmbedderUnavailable("down")))
    (scan_root / "B" / "fresh.md").write_text("## Quokka rollout\nThe quokka rollout starts Monday.\n")
    idx.sync()
    monkeypatch.setattr(Embedder, "embed", real)  # embedder back for queries, the note still has no vector
    res = idx.search("quokka rollout")
    # it can only score through the keyword list, so it may tie with the top vector hit
    assert ("B", "fresh.md", "Quokka rollout") in keys(res["hits"])[:2]
    unrelated = idx.search("channel id format", limit=2)
    assert ("B", "fresh.md", "Quokka rollout") not in keys(unrelated["hits"])


def test_search_before_any_sync_builds_index(kb_home, scan_root, make_project):
    make_project(scan_root, "A", FILES_A)
    assert KBIndex().search("wombat")["hits"]
    ro_home_missing = KBIndex(read_only=True, keyword_only=True)
    assert isinstance(ro_home_missing.search("wombat")["hits"], list)


def test_section_fresh_and_bounded(idx, scan_root):
    out = idx.section("A", "knowledge-base/decisions.md", "D-009: Channel id format")
    assert out.startswith("knowledge-base/decisions.md:3-5\n## D-009: Channel id format\nSite chats stay")
    assert "D-002" not in out
    path = scan_root / "A" / "knowledge-base" / "decisions.md"
    path.write_text(path.read_text().replace("Site chats stay", "EDITED site chats stay"))
    assert "EDITED" in idx.section("A", "knowledge-base/decisions.md", "decisions > d-009")  # breadcrumb, any case
    whole = idx.section("A", "STATE.md")
    assert whole.startswith("STATE.md:1-4\n# STATE") and "wombat" in whole
    refused = idx.section("A", "../x.md")
    assert "outside" in refused and "EDITED" not in refused
    (scan_root / "x.md").write_text("secret")
    assert "secret" not in idx.section("A", "../x.md")
    assert "secret" not in idx.section("A", str(scan_root / "x.md"))
    missing = idx.section("A", "STATE.md", "No such heading")
    assert "No heading" in missing and "## Status" in missing
    with pytest.raises(ValueError, match="Unknown project"):
        idx.section("nope", "STATE.md")


def test_sources_and_projects(idx, kb_home, tmp_path):
    src = idx.sources()
    assert [(s["project"], s["path"]) for s in src] == [
        ("A", "STATE.md"), ("A", "knowledge-base/decisions.md"), ("A", "old/plan.md"), ("B", "knowledge-base/lessons.md")]
    dec = src[1]
    assert dec["doc_type"] == "decisions" and dec["status"] == "current/superseded" and dec["chunks"] == 2
    assert dec["tokens"] > 0 and set(dec) == {"project", "path", "doc_type", "status", "chunks", "tokens"}
    assert {s["project"] for s in idx.sources("B")} == {"B"}
    cfg_file = kb_home / "config.toml"
    gone = tmp_path / "gone-kb"
    cfg_file.write_text(f'explicit_projects = ["{gone}"]\n' + cfg_file.read_text())
    idx.sync()
    projects = {p["name"]: p for p in idx.projects()}
    assert set(projects) == {"A", "B", "gone-kb"}
    assert projects["A"]["files"] == 3 and projects["A"]["tokens"] > 0 and projects["A"]["missing"] is False
    assert projects["gone-kb"]["missing"] is True and projects["gone-kb"]["files"] == 0
    assert set(projects["A"]) == {"name", "path", "files", "tokens", "missing", "warning", "scope", "repo", "source"}


def test_format_hits_shape(idx):
    out = format_hits(idx.search("site channel migration"))
    assert "[1] A · knowledge-base/decisions.md:" in out or "[1] B · knowledge-base/lessons.md:" in out
    assert "§ " in out and " | decisions | current" in out
    assert "do not act on it" in out and "- A · old/plan.md § Old channel plan" in out
    shown = format_hits(idx.search("old channel plan", include_superseded=True))
    assert "superseded (Dropped in D-009)" in shown
    assert format_hits({"hits": [], "superseded_matches": [], "keyword_only": False}) == "No matches."


# ---- review fixes

def test_section_only_reads_indexable_files(idx, scan_root):
    a = scan_root / "A"
    (a / "vendor" / "repo" / ".git").mkdir(parents=True)
    (a / "vendor" / "repo" / "README.md").write_text("# r\nnested repo text\n")
    (a / ".secrets").mkdir()
    (a / ".secrets" / "x.md").write_text("hidden text\n")
    (a / "notes.txt").write_text("plain text file\n")
    (a / ".env").write_text("TOKEN=abc\n")
    for path, leak in [("vendor/repo/README.md", "nested repo text"), (".secrets/x.md", "hidden text"),
                       ("notes.txt", "plain text file"), (".env", "TOKEN")]:
        out = idx.section("A", path)
        assert leak not in out and out.startswith("Refused"), path
    assert "wombat" in idx.section("A", "STATE.md")


def test_section_matches_full_breadcrumb_when_subheadings_repeat(idx, scan_root):
    (scan_root / "A" / "dup.md").write_text(
        "# Decisions\n\n## D-001: First\n### Rationale\nrationale ONE\n\n## D-002: Second\n### Rationale\nrationale TWO\n")
    out = idx.section("A", "dup.md", "Decisions > D-002: Second > Rationale")
    assert "rationale TWO" in out and "rationale ONE" not in out and out.startswith("dup.md:8-9\n")
    assert "rationale ONE" in idx.section("A", "dup.md", "D-001: First > Rationale")
    ambiguous = idx.section("A", "dup.md", "Rationale")
    assert "Ambiguous" in ambiguous and "line 4" in ambiguous and "line 8" in ambiguous
    assert "ONE" not in ambiguous and "TWO" not in ambiguous


def test_read_only_project_filter_without_folders_on_disk(kb_home, idx, scan_root, tmp_path):
    scan_root.rename(tmp_path / "moved-away")  # like Cowork: the index is there, the folders are not
    ro = KBIndex(read_only=True, keyword_only=True)
    res = ro.search("channel", project="B")
    assert {h["project"] for h in res["hits"]} == {"B"}
    with pytest.raises(ValueError, match="Unknown project"):
        ro.search("channel", project="nope")


# ---- v0.2: scope

@pytest.fixture
def scoped(kb_home, scan_root, make_project, make_repo, write_registry):
    make_project(scan_root, "Work", {"notes.md": "## Rollout\nThe quokka rollout plan for work.\n"})
    make_project(scan_root / "personal", "Diary", {
        "notes.md": "## Secret\nThe quokka rollout diary entry, private.\n",
        "old.md": "## Old diary (superseded)\nquokka rollout history, private.\n"})
    core = make_repo(scan_root / "core", {
        "knowledge-base/billing/decisions.md": "## D-001\nBilling uses the quokka ledger.\n",
        "knowledge-base/payroll/decisions.md": "## D-001\nPayroll uses the wombat ledger.\n"})
    make_project(scan_root, "core_x", {"notes.md": "## Other\ncore_x quokka notes.\n"})
    write_registry(kb_home, [
        {"name": "core/billing", "repo": str(core), "rel": "knowledge-base/billing", "scope": "work"},
        {"name": "core/payroll", "repo": str(core), "rel": "knowledge-base/payroll", "scope": "work"}])
    index = KBIndex()
    index.sync()
    return index


def test_personal_never_leaks_into_work_scope(scoped):
    res = scoped.search("quokka rollout diary entry private", limit=15)
    assert res["hits"] and all(h["project"] != "Diary" for h in res["hits"])
    assert all(s["project"] != "Diary" for s in res["superseded_matches"])
    assert all(s["project"] != "Diary" for s in scoped.sources())
    with_all = scoped.search("quokka rollout diary entry private", include_superseded=True, limit=15)
    assert all(h["project"] != "Diary" for h in with_all["hits"])


def test_scope_all_and_personal(scoped):
    personal = scoped.search("quokka rollout", scope="personal", limit=15)
    assert {h["project"] for h in personal["hits"]} == {"Diary"}
    assert {s["project"] for s in personal["superseded_matches"]} == {"Diary"}
    everything = scoped.search("quokka rollout", scope="all", limit=15)
    assert {"Diary", "Work"} <= {h["project"] for h in everything["hits"]}
    assert {s["project"] for s in scoped.sources(scope="personal")} == {"Diary"}
    assert {s["project"] for s in scoped.sources(scope="all")} >= {"Diary", "Work", "core/billing"}


def test_explicit_project_overrides_scope(scoped):
    res = scoped.search("quokka rollout", project="Diary")
    assert {h["project"] for h in res["hits"]} == {"Diary"}
    assert {s["project"] for s in scoped.sources("Diary")} == {"Diary"}


def test_project_prefix_matches_topics(scoped):
    assert {h["project"] for h in scoped.search("ledger", project="core")["hits"]} == {"core/billing", "core/payroll"}
    assert {h["project"] for h in scoped.search("ledger", project="core/billing")["hits"]} == {"core/billing"}
    assert {h["project"] for h in scoped.search("quokka", project="CORE")["hits"]} <= {"core/billing", "core/payroll"}
    assert {h["project"] for h in scoped.search("quokka", project="core_x")["hits"]} == {"core_x"}
    assert "D-001" in scoped.section("core/billing", "decisions.md", "D-001")
    with pytest.raises(ValueError, match="Unknown project"):
        scoped.section("core", "decisions.md")


def test_session_dir_sets_default_scope(scoped, scan_root):
    here = KBIndex(session_dir=scan_root / "personal" / "Diary")
    assert here.session_scope == "personal"
    assert {h["project"] for h in here.search("quokka rollout", limit=15)["hits"]} == {"Diary"}
    work = KBIndex(session_dir=scan_root / "core" / "src")
    assert work.session_scope == "work"
    assert all(h["project"] != "Diary" for h in work.search("quokka rollout", limit=15)["hits"])
    assert KBIndex().session_scope == "work"


def test_unknown_scope_rejected(scoped):
    with pytest.raises(ValueError, match="Unknown scope"):
        scoped.search("quokka", scope="office")


def test_projects_listing_has_scope_repo_source(scoped, kb_home, scan_root, write_registry):
    core = scan_root / "core"
    write_registry(kb_home, [
        {"name": "core/billing", "repo": str(core), "rel": "knowledge-base/billing", "scope": "work"},
        {"name": "core/ghost", "repo": str(core), "rel": "knowledge-base/ghost", "scope": "work"}])
    scoped.sync()
    listing = {p["name"]: p for p in scoped.projects()}
    assert listing["core/billing"]["repo"] == str(core) and listing["core/billing"]["source"] == "registry"
    assert listing["Diary"]["scope"] == "personal" and listing["Diary"]["source"] == "marker" and listing["Diary"]["repo"] is None
    assert listing["core/ghost"]["missing"] is True and listing["core/ghost"]["files"] == 0
    assert "core/payroll" not in listing
    assert {p["name"] for p in scoped.projects(scope="personal")} == {"Diary"}


def test_session_in_older_worktree_searches_its_own_copy(kb_home, scan_root, tmp_path, make_repo, make_worktree, write_registry):
    repo = make_repo(scan_root / "core", {"knowledge-base/billing/n.md": "## Main\nmain clone wombat text\n"})
    wt = make_worktree(repo, tmp_path / "wt", {"knowledge-base/billing/n.md": "## Mine\nmy worktree quokka note\n"})
    os.utime(wt / "knowledge-base/billing/n.md", (1000, 1000))
    write_registry(kb_home, [{"name": "core/billing", "repo": str(repo), "rel": "knowledge-base/billing", "scope": "work"}])
    mine = KBIndex(session_dir=wt)
    mine.sync()
    assert "quokka" in mine.search("note", project="core/billing")["hits"][0]["text"]
    assert "quokka" in mine.section("core/billing", "n.md")
    other = KBIndex(session_dir=repo)
    other.sync()
    assert "wombat" in other.search("text", project="core/billing")["hits"][0]["text"]
