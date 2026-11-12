"""Package the Cowork skill for upload to claude.ai. Cowork cannot reach the MCP server, so it gets
its own skill, which only ever runs read-only commands against the mounted engine home."""
from __future__ import annotations

import zipfile
from pathlib import Path

from .config import engine_home

SKILL_NAME = "kbhub-cowork"
SKILL_DIR = Path(__file__).resolve().parent.parent / "skill" / SKILL_NAME


def package(out: str | Path | None = None) -> Path:
    """Write the skill folder as a zip (`<skill name>/...`, the layout claude.ai expects)."""
    target = Path(out).expanduser() if out else engine_home() / "dist" / f"{SKILL_NAME}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in SKILL_DIR.rglob("*") if p.is_file() and not p.name.startswith(".")):
            z.write(f, f"{SKILL_NAME}/{f.relative_to(SKILL_DIR).as_posix()}")
    return target
