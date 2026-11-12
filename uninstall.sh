#!/usr/bin/env bash
# Remove kbhub from Claude Code. Keeps config.toml and the index (db/).
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec uv run kbhub uninstall
