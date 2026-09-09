"""Structured actions stored in scheduler rows.

Scheduler rows historically stored natural-language commands.  Tool-owned
scheduled work needs a durable representation that can dispatch without
re-entering LLM or instant-pattern routing.
"""

from __future__ import annotations

import json
from typing import Any

ACTION_PREFIX = "viola-tool-action:v1:"


def encode_tool_action(tool: str, args: dict[str, Any] | None = None) -> str:
    """Encode a tool action for storage in ``schedules.action``."""
    payload = {
        "tool": tool.strip(),
        "args": args or {},
    }
    return ACTION_PREFIX + json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def decode_tool_action(action: str) -> dict[str, Any] | None:
    """Decode a structured scheduler action, returning ``None`` for legacy text."""
    if not isinstance(action, str) or not action.startswith(ACTION_PREFIX):
        return None
    try:
        payload = json.loads(action[len(ACTION_PREFIX) :])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool")
    args = payload.get("args")
    if not isinstance(tool, str) or not tool.strip():
        return None
    if not isinstance(args, dict):
        args = {}
    return {"tool": tool.strip(), "args": args}


__all__ = ["ACTION_PREFIX", "decode_tool_action", "encode_tool_action"]
