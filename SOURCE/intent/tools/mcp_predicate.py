"""Canonical MCP-tool predicate.

F-046 (R3-C / R9-C): the production predicate ``_is_mcp_tool`` lives in
``intent/agent_loop.py:881-926`` and currently returns ``True`` for any
tool name containing ``__`` *before* it consults the MCP hub route
table. That makes the ``updated_mcp_tool_output`` rewrite hook
(``intent/agent_loop.py:3328-3340``) fire on non-MCP tools whose names
happen to use ``__`` — for example a future built-in ``foo__bar`` tool,
or a Viola-internal alias. Claude's predicate
(``services/mcp/utils.ts:245-247``, ``services/tools/toolExecution.ts:1494-1498``)
is strict: a tool is MCP iff its name starts with ``mcp__`` or it
carries ``isMcp === true``.

This module exposes the canonical strict predicate so callers can adopt
it incrementally. The existing predicate in ``agent_loop.py`` is
**not yet rewired** to this module — that change crosses into R9-A's
scope (the hot loop and agent executor) and is intentionally deferred
to keep R9-C edits disjoint from R9-A's parallel work. The new
predicate ships with its own test suite so the contract is pinned
before adoption.

Two predicate flavors:

- :func:`is_canonical_mcp_tool_name`: pure-name check (no hub access),
  the Claude-equivalent fast path. ``True`` iff the tool name starts
  with ``mcp__``.
- :func:`is_mcp_tool_strict`: hub-aware. Resolves the tool name
  against the hub's ``_tool_routes`` and ``_configs``. Returns ``True``
  for canonical ``mcp__...`` names, or for legacy ``server__tool``
  names whose resolved server config has ``namespace=True`` (Viola
  marks user-configured external servers that way). Built-in servers
  (``namespace=False``) never get the MCP rewrite even if their tool
  names happen to include ``__``.

The canonical form is ``mcp__{server}__{tool}`` (G33 gate). Legacy
``{server}__{tool}`` aliases remain accepted only because the hub
config proves they came from an external MCP server.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_MCP_PREFIX = "mcp__"


def is_canonical_mcp_tool_name(tool_name: str) -> bool:
    """Pure-name Claude predicate: ``True`` iff name starts with ``mcp__``.

    No hub lookup, no double-underscore heuristic. This is the strict
    Claude-equivalent contract from ``services/mcp/utils.ts:245-247``.
    """
    return bool(tool_name) and tool_name.startswith(_MCP_PREFIX)


def is_mcp_tool_strict(hub: Any, tool_name: str) -> bool:
    """Return ``True`` iff ``tool_name`` is routed through an external MCP server.

    Priority order:

    1. Canonical ``mcp__{server}__{tool}`` → True without hub lookup.
    2. Hub route table + config: legacy ``{server}__{tool}`` is accepted
       only when the resolved server config carries ``namespace=True``
       (Viola's marker for user-configured external servers).
    3. Tool name contains ``__`` but no hub route exists → False
       (conservative: a hook cannot mutate a built-in tool's result).
    4. Tool name has no ``__`` → False.

    Compare with Claude's TS at ``services/tools/toolExecution.ts:1494-1498``,
    which applies the ``updatedMCPToolOutput`` hook field only when
    ``isMcpTool(tool)`` returns true.
    """
    if not tool_name:
        return False
    if is_canonical_mcp_tool_name(tool_name):
        # Canonical Claude shape — accept without further hub lookup.
        return True
    if "__" not in tool_name:
        return False
    if hub is None:
        return False
    routes = getattr(hub, "_tool_routes", None)
    if not isinstance(routes, Mapping):
        return False
    server_name = routes.get(tool_name)
    if not server_name:
        return False
    configs = getattr(hub, "_configs", None)
    if not isinstance(configs, Mapping):
        # Unknown config shape — conservatively reject so a hook cannot
        # silently mutate a built-in tool's result.
        return False
    config = configs.get(server_name)
    if config is None:
        return False
    # Viola convention: external (user-configured) MCP servers register
    # tools under a namespace prefix; built-in servers do not.
    return bool(getattr(config, "namespace", False))


__all__ = [
    "is_canonical_mcp_tool_name",
    "is_mcp_tool_strict",
]
