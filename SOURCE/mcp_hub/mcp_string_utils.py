"""Pure string utilities for MCP tool/server name parsing.

Claude parity (S6-010): Claude canonicalizes MCP tool names as
``mcp__<server>__<tool>`` and exposes parse helpers in
``services/mcp/mcpStringUtils.ts``. We mirror the same shape so any
prompt/permission/cache lookup that round-trips through the wire format
is identical across providers.

Viola's pre-parity wire format was ``<server>__<tool>``. The
``viola_legacy_alias`` helper preserves it for backwards-compatible
permission/dispatch lookups even though the model only ever sees the
Claude-canonical form.
"""

from __future__ import annotations

import re

# Conservative normalizer: MCP tool/server names are restricted to
# ``[A-Za-z0-9_-]``. Anything else is squashed to ``_`` so we never
# emit a wire name that the JSON-RPC layer would reject.
_NORMALIZE_RE = re.compile(r"[^A-Za-z0-9_-]")
_CLAUDEAI_SERVER_PREFIX = "claude.ai "


def normalize_name_for_mcp(name: str) -> str:
    """Strip MCP-disallowed characters out of ``name``.

    Mirrors ``normalizeNameForMCP`` (``services/mcp/normalization.ts``)
    so identical inputs produce identical wire names.
    """

    if not name:
        return ""
    raw_name = str(name)
    normalized = _NORMALIZE_RE.sub("_", raw_name)
    if raw_name.startswith(_CLAUDEAI_SERVER_PREFIX):
        normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def mcp_info_from_string(tool_string: str) -> dict[str, str | None] | None:
    """Parse ``mcp__<server>__<tool>`` into a ``{serverName, toolName}`` dict.

    Returns ``None`` if the input is not an ``mcp__`` rule. The toolName
    is ``None`` when the rule is a wildcard for the whole server
    (``mcp__server``).

    Known limitation (mirrored from TS): if a server name itself
    contains ``__``, parsing is wrong (server gets the first segment,
    tool absorbs the rest). MCP server names rarely use ``__`` so this
    is accepted as a quirk.
    """

    if not tool_string:
        return None
    parts = str(tool_string).split("__")
    if len(parts) < 2:
        return None
    mcp_part = parts[0]
    server_name = parts[1] if len(parts) >= 2 else ""
    if mcp_part != "mcp" or not server_name:
        return None
    tool_parts = parts[2:]
    tool_name: str | None = "__".join(tool_parts) if tool_parts else None
    return {"serverName": server_name, "toolName": tool_name}


def get_mcp_prefix(server_name: str) -> str:
    """Return ``"mcp__<normalized-server>__"``.

    Mirrors ``getMcpPrefix`` (``services/mcp/mcpStringUtils.ts:39-41``).
    """

    return "mcp__%s__" % normalize_name_for_mcp(server_name)


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Build the canonical ``mcp__<server>__<tool>`` name.

    Inverse of :func:`mcp_info_from_string`. Mirrors ``buildMcpToolName``
    (``services/mcp/mcpStringUtils.ts:50-52``).
    """

    return "%s%s" % (get_mcp_prefix(server_name), normalize_name_for_mcp(tool_name))


def viola_legacy_alias(server_name: str, tool_name: str) -> str:
    """Return Viola's pre-parity ``<server>__<tool>`` alias.

    Used as a secondary key in the hub's tool routing tables so existing
    permissions / dispatch lookups keep working after we migrate to the
    Claude-canonical ``mcp__<server>__<tool>`` form. The legacy alias
    is never surfaced to the model.
    """

    return "%s__%s" % (normalize_name_for_mcp(server_name), normalize_name_for_mcp(tool_name))


def get_mcp_display_name(full_name: str, server_name: str) -> str:
    """Strip the ``mcp__<server>__`` prefix off ``full_name``.

    Mirrors ``getMcpDisplayName`` (``services/mcp/mcpStringUtils.ts:75-
    81``). Returns ``full_name`` unchanged when it doesn't carry the
    prefix.
    """

    prefix = get_mcp_prefix(server_name)
    if full_name.startswith(prefix):
        return full_name[len(prefix) :]
    return full_name


def extract_mcp_tool_display_name(user_facing_name: str) -> str:
    """Extract a display name from a ``"<server> - <tool> (MCP)"`` string.

    Mirrors ``extractMcpToolDisplayName``
    (``services/mcp/mcpStringUtils.ts:88-105``).
    """

    if not user_facing_name:
        return ""
    # Remove "(MCP)" suffix if present (with optional surrounding whitespace).
    without_suffix = re.sub(r"\s*\(MCP\)\s*$", "", str(user_facing_name)).strip()
    # Drop the "<server> - " prefix when present.
    dash_index = without_suffix.find(" - ")
    if dash_index != -1:
        return without_suffix[dash_index + 3 :].strip()
    return without_suffix


__all__ = [
    "build_mcp_tool_name",
    "extract_mcp_tool_display_name",
    "get_mcp_display_name",
    "get_mcp_prefix",
    "mcp_info_from_string",
    "normalize_name_for_mcp",
    "viola_legacy_alias",
]
