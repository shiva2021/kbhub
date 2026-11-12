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
            "add_rule", "create_kb", "get_section", "list_projects", "list_sources", "register_kb", "reindex",
            "scan_kbs", "search_kb"]

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
    make_project(scan_root / "personal", "Diary", {"n.md": "## Secret\nprivate quokka diary\n"})
    make_project(scan_root, "Work", {"n.md": "## Plan\nwork quokka plan\n"})

    async def body(session, init):
        assert "create_kb" in init.instructions and "scope" in init.instructions
        assert "where the session started" in init.instructions
        call = lambda tool, /, **args: session.call_tool(tool, args)

        created = text_of(await call("create_kb", topic="billing"))      # location defaults to the session folder
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
