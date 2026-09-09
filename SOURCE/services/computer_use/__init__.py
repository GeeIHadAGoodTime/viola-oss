"""Desktop computer-use service package.

This package backs the MCP `computer` tool. It is desktop-only and intentionally
keeps OS control behind service functions so the agent loop never calls
pywinauto directly.
"""

from __future__ import annotations

LOGICAL_HEIGHT = 768
LOGICAL_WIDTH = 1024

__all__ = ["LOGICAL_HEIGHT", "LOGICAL_WIDTH"]
