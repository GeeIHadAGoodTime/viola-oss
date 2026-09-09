"""Agent tools package.

Tool implementations live here. The MCP server in mcp_servers/core_tools/
wraps these functions as MCP tools for the agent loop.
"""

from __future__ import annotations

import importlib

from intent.tools import (
    delegation,
    desktop,
    filesystem,
    memory,
    scheduling,
    self_management,
    shell,
    system_state,
    web_read,
    web_search,
)

try:
    phone_transmit = importlib.import_module("intent.tools.phone_transmit")
except ModuleNotFoundError as exc:
    if exc.name not in {"intent.tools.phone_transmit", "services.payments"}:
        raise
    phone_transmit = None

__all__ = [
    "delegation",
    "desktop",
    "filesystem",
    "memory",
    "scheduling",
    "self_management",
    "shell",
    "system_state",
    "web_read",
    "web_search",
]

if phone_transmit is not None:
    __all__.append("phone_transmit")
