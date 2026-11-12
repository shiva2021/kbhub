import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skill" / "kbhub" / "SKILL.md"
BLOCK = REPO / "claude_md_block.md"


def test_skill_and_block_valid():
    text = SKILL.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert m, "SKILL.md needs YAML frontmatter"
    front = dict(line.split(": ", 1) for line in m.group(1).splitlines())
    assert front["name"] == "kbhub"
    assert front["description"].startswith("Use for any project, coding or research task")
    assert len(front["description"]) <= 1024
    body = m.group(2)
    for needle in ("search_kb", "get_section", "new-project", "add-project", "STATE.md", "superseded",
                   "--no-sync", "--keyword-only", "D-NNN", "handovers/", "UV_PROJECT_ENVIRONMENT",
                   # v0.2: knowledge bases inside code bases, registration, scope
                   "create_kb", "scan_kbs", "register_kb", "add_rule", "knowledge-base/<topic>", "new-kb",
                   "scope", "personal", "never commits", ".kb.toml", "where the session started"):
        assert needle in body, needle
    lowered = " ".join(text.lower().split())
    for stale in ("never write knowledge-base files into a code repo", "never inside a code repo",
                  "never create a knowledge base inside a code repository"):
        assert stale not in lowered, stale
    # the Cowork command must point the engine at the mounted home, or it finds no index
    cowork = next(line for line in body.splitlines() if "--no-sync --keyword-only" in line and "uv run" in line)
    assert 'KBHUB_HOME="$HOME/mnt/.kbhub"' in cowork
    assert "--scope all" not in cowork   # Cowork searches stay in the default (work) scope

    block = BLOCK.read_text(encoding="utf-8")
    lines = block.strip().splitlines()
    assert lines[0] == "<!-- kbhub:start -->" and lines[-1] == "<!-- kbhub:end -->"
    assert len(lines) <= 25
    for needle in ("search_kb", "STATE.md", "superseded", "kbhub", "create_kb", "scope",
                   "knowledge-base/<topic>"):
        assert needle in block, needle
    assert "never write knowledge-base files into a code repo" not in " ".join(block.lower().split())


def test_skill_frontmatter_is_strict_yaml():
    """claude.ai rejects an upload whose frontmatter a strict YAML parser cannot read."""
    import yaml

    for skill in sorted((REPO / "skill").glob("*/SKILL.md")):
        m = re.match(r"---\n(.*?)\n---\n", skill.read_text(encoding="utf-8"), re.S)
        assert m, skill
        front = yaml.safe_load(m.group(1))
        assert front["name"] == skill.parent.name
        assert isinstance(front["description"], str) and 0 < len(front["description"]) <= 1024


def test_every_command_the_docs_name_exists():
    """The README and reference must not teach a command the program does not have."""
    from kbhub.cli import _parser
    sub = next(a for a in _parser()._actions if getattr(a, "choices", None))
    known = set(sub.choices)
    for doc in (REPO / "README.md", REPO / "docs" / "reference.md"):
        text = doc.read_text(encoding="utf-8")
        named = set(re.findall(r"`kbhub ([a-z][a-z-]+)", text))
        assert named and named <= known, (doc.name, sorted(named - known))
        assert "~/.kbhub/setup" in (REPO / "README.md").read_text(encoding="utf-8")
    assert (REPO / "setup").is_file()
