"""Run the core-tools MCP server via stdio transport.

Launch as: python -m mcp_servers.core_tools
The MCP hub (or MCPServerStdio from the Agents SDK) connects over stdin/stdout.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    """Entry point for subprocess execution."""
    from services.sentry_init import init_sentry

    init_sentry("mcp_servers.core_tools")

    from mcp_servers.core_tools.server import server

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
