"""Run the Playwright browser MCP server via stdio transport.

Launch as: python -m mcp_servers.browser
The MCP hub connects to this process over stdin/stdout.
"""

from __future__ import annotations

import atexit
import logging
import signal
import sys

logger = logging.getLogger(__name__)


def _cleanup() -> None:
    """Best-effort synchronous cleanup."""
    try:
        import asyncio

        from mcp_servers.browser.server import manager

        loop = asyncio.new_event_loop()
        loop.run_until_complete(manager.shutdown())
        loop.close()
    except Exception:
        logger.debug("Browser cleanup failed on exit")


def main() -> None:
    """Entry point for subprocess execution."""
    from services.sentry_init import init_sentry

    init_sentry("mcp_servers.browser")

    # Register cleanup handlers
    atexit.register(_cleanup)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    from mcp_servers.browser.server import server

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
