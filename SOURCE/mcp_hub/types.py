"""Type definitions for the MCP Client Hub."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ServerConfig:
    """Configuration for an MCP server connection."""

    name: str
    transport: str  # "inprocess", "stdio", "sse"
    command: str = ""  # For stdio: command to launch
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""  # For SSE: server URL
    module: str = ""  # For inprocess: Python module path
    enabled: bool = True
    namespace: bool = False  # Prefix tool names with server name for external servers
