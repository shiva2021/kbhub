import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FILES = {
    "STATE.md": "# STATE\n\n## Status\nBuilding the wombat importer.\n",
    "knowledge-base/decisions.md": "# Decisions\n\n## D-001: Channel ids\nChannels use <clientId>-<swId>.\n",
}


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


async def _session(kb_home, body, cwd=None):
    env = {**os.environ, "KBHUB_HOME": str(kb_home)}
    env.pop("KBHUB_SESSION_DIR", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "kbhub.server"], env=env,
                                   cwd=str(cwd) if cwd else None)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()  # fails if stdout carried anything but MCP frames
            return await body(session, init)


def test_server_end_to_end(kb_home, scan_root, make_project):
    project = make_project(scan_root, "Alpha", FILES)

    async def body(session, init):
        assert init.serverInfo.name == "kbhub"
        assert "search_kb" in init.instructions and "superseded" in init.instructions
        tools = await session.list_tools()
        assert sorted(t.name for t in tools.tools) == [
            "add_rule", "create_kb", "get_section", "kb_setup", "list_projects", "list_sources", "record",
            "register_kb", "reindex", "scan_kbs", "search_kb"]

        found = text_of(await session.call_tool("search_kb", {"query": "channel id format"}))
        assert "[1] Alpha · knowledge-base/decisions.md:3-4 | decisions | current" in found

        section = text_of(await session.call_tool(
            "get_section", {"project": "Alpha", "path": "knowledge-base/decisions.md", "heading": "D-001"}))
        assert section.startswith("knowledge-base/decisions.md:3-4\n## D-001: Channel ids")

        bad = text_of(await session.call_tool("search_kb", {"query": "x", "project": "nope"}))
        assert bad.startswith("Error:") and "Alpha" in bad
        assert text_of(await session.call_tool("search_kb", {"query": "  "})).startswith("Error:")
        assert text_of(await session.call_tool("get_section", {"project": "nope", "path": "a.md"})).startswith("Error:")

        projects = text_of(await session.call_tool("list_projects", {}))
        assert "Alpha" in projects and str(project) in projects and "2 files" in projects
        sources = text_of(await session.call_tool("list_sources", {"project": "Alpha"}))
        assert "Alpha · STATE.md | state | current" in sources

        (project / "late.md").write_text("## Late\nplatypus note\n")
        stats = text_of(await session.call_tool("reindex", {}))
        assert '"changed": 1' in stats
        assert "late.md" in text_of(await session.call_tool("search_kb", {"query": "platypus", "limit": 99}))
        rebuilt = text_of(await session.call_tool("reindex", {"project": "Alpha"}))
        assert '"changed": 3' in rebuilt
        return True

    assert asyncio.run(_session(kb_home, body))


def test_server_records_entries(kb_home, scan_root, make_project):
    from datetime import date
    today = date.today().isoformat()
    project = make_project(scan_root, "Alpha", FILES)
    make_project(scan_root, "Alpha/sub", {"n.md": "x"}, kb_toml='name = "Alpha/sub"\n')

    async def body(session, init):
        assert "record(" in init.instructions
        call = lambda **args: session.call_tool("record", args)
        done = text_of(await call(project="alpha", kind="decision", title="Echidna ids", body="Channels use echidna ids.",
                                  why="The old shape clashed.", decided_by="Sam", supersedes="D-001"))
        assert done.startswith("Recorded D-002 in Alpha · knowledge-base/decisions.md:6-10.")
        assert "D-001 is now marked superseded" in done and "STATE.md" in done and "Searchable now" in done
        text = (project / "knowledge-base" / "decisions.md").read_text()
        assert f"## D-001: Channel ids (superseded {today}, see D-002)" in text
        assert f"- **Date:** {today} · **Status:** accepted · **Decided by:** Sam" in text
        found = text_of(await session.call_tool("search_kb", {"query": "echidna channel ids", "project": "Alpha"}))
        assert "knowledge-base/decisions.md:6-10 | decisions | current" in found
        assert "decisions.md:3-4" not in found          # the superseded decision is hidden at once

        lesson = text_of(await call(project="Alpha", kind="lesson", title="Numbats bite", body="Wear gloves."))
        assert lesson.startswith("Recorded the lesson in Alpha · knowledge-base/lessons.md:3-4.")
        handover = text_of(await call(project="Alpha", kind="handover", title="Importer", body="## Next\nWrite rows."))
        assert f"handovers/{today}-importer.md" in handover and "Link it from STATE.md" in handover
        assert "numbats" in text_of(await session.call_tool("search_kb", {"query": "numbats gloves"})).lower()

        for bad in ({"project": "nope"}, {"kind": "note"}, {"title": "Dropped it"}, {"why": None}):
            args = {"project": "Alpha", "kind": "decision", "title": "T", "body": "b", "why": "w", **bad}
            assert text_of(await call(**args)).startswith("Error:"), bad
        assert "D-003" not in (project / "knowledge-base" / "decisions.md").read_text()
        return True

    assert asyncio.run(_session(kb_home, body))


def test_server_reports_bad_config_as_text(kb_home, scan_root, make_project):
    make_project(scan_root, "Alpha", FILES)
    (kb_home / "config.toml").write_text("scan_depth = 3\nscan_roots = = 1\n")

    async def body(session, init):
        out = text_of(await session.call_tool("search_kb", {"query": "channel"}))
        assert out.startswith("Error:") and "config.toml:2:" in out
        return True

    assert asyncio.run(_session(kb_home, body))


def test_server_creates_registers_and_scopes(kb_home, scan_root, make_project, make_repo):
    core = make_repo(scan_root / "projects" / "core", {"src/app.ts": "", "docs/kb/legacy.md": "## Legacy\nold platypus notes\n"})
    webapp = make_repo(scan_root / "projects" / "webapp", {"src/x.vue": ""})
    make_project(scan_root / "personal", "Diary", {"n.md": "## Secret\nprivate quokka diary\n"})
    make_project(scan_root, "Work", {"n.md": "## Plan\nwork quokka plan\n"})

    async def body(session, init):
        assert "create_kb" in init.instructions and "scope" in init.instructions
        assert "where the session started" in init.instructions
        call = lambda tool, /, **args: session.call_tool(tool, args)

        assert "kb_setup" in init.instructions and "in_repo" in init.instructions
        asked = text_of(await call("kb_setup", enable=True, location=str(webapp)))   # the user said yes
        general = scan_root / "webapp" / "general"
        assert "Created" in asked and "webapp/general" in asked and str(general) in asked and "Indexed" in asked
        assert (general / "STATE.md").is_file() and sorted(p.name for p in webapp.iterdir()) == [".git", "src"]
        again = text_of(await call("kb_setup", enable=True, location=str(webapp)))
        assert "already has" in again and "webapp/general" in again and "Created" not in again
        assert "no knowledge base for webapp" in text_of(await call("kb_setup", enable=False, location=str(webapp)))
        outside = text_of(await call("create_kb", topic="payroll"))      # a topic for the session's repo: outside it
        assert "core/payroll" in outside and str(scan_root / "core" / "payroll") in outside
        assert "not committed" not in outside and not (core / "knowledge-base").exists()

        created = text_of(await call("create_kb", topic="billing", in_repo=True))   # only when asked for
        folder = core / "knowledge-base" / "billing"
        assert "core/billing" in created and str(folder) in created and "not committed" in created
        assert (folder / "STATE.md").is_file() and not list(core.rglob(".kb.toml"))
        (folder / "decisions.md").write_text("# Decisions\n\n## D-001: Ledger\nBilling uses the wombat ledger.\n")
        found = text_of(await call("search_kb", query="wombat ledger", project="core/billing", limit=10))
        assert "core/billing · decisions.md:3-4 | decisions | current" in found

        default = text_of(await call("search_kb", query="quokka"))
        assert "Work · n.md" in default and "Diary" not in default
        personal = text_of(await call("search_kb", query="quokka", scope="personal"))
        assert "Diary · n.md" in personal and "Work" not in personal
        assert "Diary" in text_of(await call("search_kb", query="quokka", project="Diary"))
        assert text_of(await call("search_kb", query="quokka", scope="office")).startswith("Error:")

        listing = text_of(await call("list_projects"))
        assert "core/billing" in listing and "[work]" in listing and "Diary" in listing and "[personal]" in listing
        assert "Diary" not in text_of(await call("list_sources"))

        scanned = text_of(await call("scan_kbs"))
        assert "NEW" in scanned and str(core / "docs" / "kb") in scanned and "register_kb" in scanned
        assert text_of(await call("register_kb", path=str(core))).startswith("Error:")
        registered = text_of(await call("register_kb", path=str(core / "docs" / "kb"), name="core/legacy"))
        assert "core/legacy" in registered and "1 file" in registered
        assert "core/legacy · legacy.md" in text_of(await call("search_kb", query="platypus"))

        ruled = text_of(await call("add_rule", project="core/legacy", glob="legacy.md", status="superseded", note="replaced"))
        assert "core/legacy" in ruled
        after = text_of(await call("search_kb", query="platypus", project="core/legacy"))
        assert "[1]" not in after and "do not act on it" in after
        return True

    assert asyncio.run(_session(kb_home, body, cwd=core / "src"))


def test_server_default_scope_follows_personal_session_folder(kb_home, scan_root, make_project):
    diary = make_project(scan_root / "personal", "Diary", {"n.md": "## Secret\nprivate quokka diary\n"})
    make_project(scan_root, "Work", {"n.md": "## Plan\nwork quokka plan\n"})

    async def body(session, init):
        out = text_of(await session.call_tool("search_kb", {"query": "quokka"}))
        assert "Diary · n.md" in out and "Work" not in out
        return True

    assert asyncio.run(_session(kb_home, body, cwd=diary))


def test_record_reports_success_when_the_sync_fails(kb_home, scan_root, make_project, monkeypatch):
    """The entry is on disk by then: an error would make the caller record it a second time."""
    from kbhub import server
    from kbhub.index import KBIndex
    project = make_project(scan_root, "Alpha", FILES)
    idx = KBIndex(session_dir=scan_root)
    monkeypatch.setattr(server, "_index", idx)
    monkeypatch.setattr(idx, "maybe_sync", lambda **kw: None)

    def broken(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(idx, "sync", broken)
    out = server.record("Alpha", "lesson", "Numbats bite", "Wear gloves.")
    assert out.startswith("Recorded the lesson in Alpha · knowledge-base/lessons.md:")
    assert "after the next sync" in out and "disk full" in out and idx._last_sync == 0.0
    assert (project / "knowledge-base" / "lessons.md").read_text().count("Numbats bite") == 1
