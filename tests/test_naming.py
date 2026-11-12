"""The product is generic: a former, company-specific name must not appear anywhere."""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWED = {"tests/test_naming.py"}


def test_former_name_appears_nowhere():
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO, capture_output=True,
                           text=True, check=True).stdout.split("\n")
    hits = []
    for rel in filter(None, files):
        path = REPO / rel
        if rel in ALLOWED or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "hireup" in text.lower() or "hire up" in text.lower():
            hits.append(rel)
    assert hits == []
