"""The machine as the setup wizard and `kbhub doctor` see it: where things live and how programs
are run. Tests swap in a temp home and a fake runner, so nothing is ever really installed there."""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .install import _run


@dataclass
class Env:
    home: Path
    claude_home: Path
    kb_home: Path
    run: Callable = _run
    which: Callable = shutil.which
    sleep: Callable = time.sleep
    path: str = ""                                  # the shell's PATH before setup added to it
    apps_dir: Path = Path("/Applications")
    bin_dirs: list[Path] | None = None              # where freshly installed programs land
    shell_rc: Path | None = None                    # the shell start-up file PATH lines go in

    def __post_init__(self):
        if self.bin_dirs is None:
            self.bin_dirs = [self.home / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
        if self.shell_rc is None:
            shell = os.path.basename(os.environ.get("SHELL", "zsh"))
            self.shell_rc = self.home / {"bash": ".bash_profile"}.get(shell, ".zshrc")

    @classmethod
    def real(cls) -> "Env":
        from .config import engine_home
        home = Path.home()
        extra = os.environ.get("KBHUB_EXTRA_PATH")     # same override the `setup` script honours
        bin_dirs = None if extra is None else [home / ".local" / "bin", *(Path(d) for d in extra.split(os.pathsep) if d)]
        return cls(home=home, claude_home=Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
                   kb_home=engine_home(), path=os.environ.get("KBHUB_ORIG_PATH") or os.environ.get("PATH", ""),
                   apps_dir=Path(os.environ.get("KBHUB_APPS_DIR") or "/Applications"), bin_dirs=bin_dirs)

    @property
    def user_bin(self) -> Path:
        return self.home / ".local" / "bin"

    def find(self, name: str) -> str | None:
        """Absolute path of a program, also when it was installed a moment ago and is not on PATH."""
        hit = self.which(name)
        if hit:
            return str(hit)
        for folder in self.bin_dirs:
            candidate = folder / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    @property
    def ollama_app(self) -> Path:
        return self.apps_dir / "Ollama.app"

    def ollama(self) -> str | None:
        exe = self.find("ollama")
        if exe:
            return exe
        inside = self.ollama_app / "Contents" / "Resources" / "ollama"
        return str(inside) if inside.is_file() else None
