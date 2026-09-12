"""Shared tool-name matching for request and child execution allowlists."""

from __future__ import annotations

import re
from collections.abc import Iterable


def normalize_tool_permission_name(value: object) -> str:
    """Normalize a tool name for permission comparisons."""

    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def tool_name_allowed_by_allowlist(allowed_tools: Iterable[str] | None, tool_name: str) -> bool:
    """Match exact and namespaced tool aliases against an allowlist."""

    allowed = {str(name).strip() for name in (allowed_tools or ()) if str(name).strip()}
    if not allowed:
        return True
    normalized_allowed = {normalize_tool_permission_name(name) for name in allowed}
    if "*" in normalized_allowed:
        return True
    raw_tool = str(tool_name or "").strip().lower()
    normalized_tool = normalize_tool_permission_name(raw_tool)
    candidates = {
        normalized_tool,
        normalize_tool_permission_name(raw_tool.split("__")[-1]),
        normalize_tool_permission_name(raw_tool.rsplit(".", 1)[-1]),
    }
    return bool(normalized_allowed.intersection(candidates))


__all__ = ["normalize_tool_permission_name", "tool_name_allowed_by_allowlist"]
