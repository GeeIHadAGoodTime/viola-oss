"""Per-tool result-to-content mapping.

Claude parity (S6-011): Claude maps each tool's raw result onto
provider-shaped ``ContentBlockParam`` content (text / image / resource
blocks) inside the per-tool ``renderResultForAssistant`` adapters
(``src/tools/.../tool.tsx``). Viola's pre-parity path was to wrap every
result in its own ``{ok, data}`` JSON envelope — fine for legacy tools
but lossy for:

- MCP tools, whose result already includes structured ``mcp_content``
  blocks (text + image + resource) that the model should see directly,
- ToolSearch, which returns ``tool_reference`` blocks the model
  consumes natively without the JSON envelope.

The mapper preserves the legacy envelope for unknown tools so we never
regress non-parity surfaces. ``None`` means "fall back to the legacy
envelope" and the agent loop keeps using the pre-mapper code path.
"""

from __future__ import annotations

import json
from typing import Any


def map_tool_result_to_content(
    *,
    tool_name: str,
    tool_result: Any,
    executor: Any = None,
) -> list[dict[str, Any]] | str | None:
    """Map ``tool_result`` to provider-shaped content for the assistant.

        Returns:
            - ``list[dict]`` of Claude-shaped content blocks (text/image/etc.)
            - ``str`` for plain-text content
            - ``None`` to signal "use the legacy ``{ok, data}`` envelope".

        Tools handled explicitly:

        - ``ToolSearch`` — match names mapped to ``tool_reference`` blocks.
    - MCP tools with ``mcp_content`` — surface the structured blocks
      preserving any text/image content the server returned.

        Anything else returns ``None`` and the caller keeps the legacy
        envelope.
    """

    del executor  # currently unused; reserved for tool-instance lookup parity
    if tool_result is None:
        return None

    data = getattr(tool_result, "data", None)

    # ── ToolSearch: emit Claude-shaped ``tool_reference`` blocks. ──
    if tool_name in {"ToolSearch", "tool_search"} and isinstance(data, dict):
        matches = data.get("matches")
        if isinstance(matches, list):
            blocks = []
            for item in matches:
                if isinstance(item, str) and item.strip():
                    blocks.append({"type": "tool_reference", "tool_name": item.strip()})
            if blocks:
                return blocks

        references = data.get("tool_references")
        if isinstance(references, list):
            blocks: list[dict[str, Any]] = []
            for item in references:
                if isinstance(item, dict) and item.get("type") == "tool_reference":
                    blocks.append(dict(item))
            if blocks:
                return blocks

    # ── MCP tools: surface ``mcp_content`` blocks if present. ──
    # R13 keeps MCP content on ToolResult.mcp_content instead of injecting it
    # into data. The data-dict fallback stays for replay/old-test fixtures.
    mcp_blocks = _extract_mcp_content_blocks(getattr(tool_result, "mcp_content", None))
    if mcp_blocks is None:
        mcp_blocks = _extract_mcp_content_blocks(data)
    if mcp_blocks is not None:
        return mcp_blocks

    return None


def _extract_mcp_content_blocks(data: Any) -> list[dict[str, Any]] | None:
    """Pull a Claude-shaped content block list out of an MCP result.

    The hub returns an ``mcp_content`` list where each entry is already shaped
    as a Claude ``ContentBlockParam`` (text / image / etc.). We pass the list
    through unchanged, dropping unknown block types defensively.
    """

    raw_blocks = data
    if isinstance(data, dict):
        raw_blocks = data.get("mcp_content")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return None
    safe_blocks: list[dict[str, Any]] = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in {"text", "image", "audio", "resource", "resource_link"}:
            safe_blocks.append(dict(block))
    if not safe_blocks:
        return None
    return safe_blocks


__all__ = ["map_tool_result_to_content"]
