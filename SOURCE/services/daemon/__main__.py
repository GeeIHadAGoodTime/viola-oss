"""Allow running the daemon as ``python -m services.daemon``."""

from __future__ import annotations

from services.daemon.viola_daemon import main

if __name__ == "__main__":
    main()
