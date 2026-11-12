"""The `setup` shell script: the only part that runs before Python exists on the machine."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

FAKE_UV = '#!/bin/sh\necho "uv $*" >> "$LOG"\necho "ORIG=$KBHUB_ORIG_PATH" >> "$LOG"\n'


@pytest.fixture
def box(tmp_path):
    """A copy of the script in an empty world: fake home, and a PATH holding only fakes plus /usr/bin."""
    repo, bin_dir, home = tmp_path / "repo", tmp_path / "bin", tmp_path / "home"
    for d in (repo, bin_dir, home):
        d.mkdir()
    shutil.copy(REPO / "setup", repo / "setup")
    log = tmp_path / "log.txt"

    def fake(name, body):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    def run(*args, uname="Darwin", chip="arm64"):
        fake("uname", f'#!/bin/sh\n[ "$1" = "-m" ] && echo {chip} || echo {uname}\n')
        env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "LOG": str(log), "KBHUB_EXTRA_PATH": ""}
        p = subprocess.run([str(repo / "setup"), *args], env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60)
        return p, (log.read_text() if log.exists() else "")

    return fake, run, home, bin_dir


def test_refuses_other_systems_before_doing_anything(box):
    fake, run, home, bin_dir = box
    fake("uv", FAKE_UV)
    p, log = run(uname="Linux")
    assert p.returncode == 1 and "macOS" in p.stderr and log == ""


def test_refuses_intel_macs_with_the_reason(box):
    fake, run, home, bin_dir = box
    fake("uv", FAKE_UV)
    p, log = run(chip="x86_64")
    assert p.returncode == 1 and "Apple silicon" in p.stderr and log == ""


def test_with_uv_present_it_syncs_then_hands_over_with_the_arguments(box):
    fake, run, home, bin_dir = box
    fake("uv", FAKE_UV)
    p, log = run("--yes")
    assert p.returncode == 0, p.stderr
    lines = [l for l in log.splitlines() if l.startswith("uv ")]
    assert lines[0].startswith("uv sync") and lines[-1] == "uv run python -m kbhub.setup --yes"
    assert f"ORIG={bin_dir}:/usr/bin:/bin" in log          # the wizard learns the shell's own PATH


def test_without_uv_it_installs_uv_first(box):
    fake, run, home, bin_dir = box
    fake("uv-to-install", FAKE_UV)       # what the "download" puts in ~/.local/bin
    fake("curl", "#!/bin/sh\necho 'mkdir -p \"$HOME/.local/bin\" && cp \"" + str(bin_dir / "uv-to-install")
         + "\" \"$HOME/.local/bin/uv\"'\n")
    p, log = run()                # no keyboard: installs without asking
    assert p.returncode == 0, p.stderr + p.stdout
    assert os.access(home / ".local" / "bin" / "uv", os.X_OK)
    assert "uv run python -m kbhub.setup" in log and "uv" in p.stdout


def test_without_uv_and_a_failed_download_it_stops_with_a_clear_message(box):
    fake, run, home, bin_dir = box
    fake("curl", "#!/bin/sh\nexit 22\n")
    p, log = run("--yes")
    assert p.returncode == 1 and "uv" in p.stderr and "astral.sh" in p.stderr and log == ""
