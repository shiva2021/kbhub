#!/usr/bin/env python3
"""Claude Code SessionEnd hook: push what the session wrote to the shared spaces, so notes edited
by hand reach the other devices. Standard library plus kbhub.config/share only. Always exits 0 and
prints nothing; whatever could not be pushed goes out with the next sync."""
from __future__ import annotations

import sys

NET_TIMEOUT = 10         # seconds each network step may take; the hook entry allows 30 in all


def main() -> int:
    try:
        from kbhub import share
        from kbhub.config import GlobalConfig
        cfg = GlobalConfig.load()
        if cfg.spaces:
            share.sync_all(cfg, NET_TIMEOUT)
    except Exception:
        pass  # never get in the way of a session ending
    return 0


if __name__ == "__main__":
    sys.exit(main())
