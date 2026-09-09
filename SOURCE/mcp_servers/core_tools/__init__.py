"""Core Tools MCP Server package.

Exports the configured FastMCP server instance and factory/utility functions
for use by the MCP client hub.
"""

from __future__ import annotations

from mcp_servers.core_tools.server import (
    create_core_tools_server,
    get_tool_count,
    server,
)

__all__ = [
    "create_core_tools_server",
    "get_tool_count",
    "server",
]
