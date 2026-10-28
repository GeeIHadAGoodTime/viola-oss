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


def _clean(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def describe_tool_action(action: str, label: str | None = None) -> str:
    """A short human title for a scheduled action, for task cards and logs.

    The encoded form (``viola-tool-action:v1:{...}``) is a storage format; it
    showed up verbatim on the stage's task card when a reminder fired (1.0.5
    build 5). Legacy plain-text actions are already readable and pass through.
    """
    decoded = decode_tool_action(action)
    fallback = _clean(label) or "Scheduled task"
    if decoded is None:
        if isinstance(action, str) and action.startswith(ACTION_PREFIX):
            return fallback
        return _clean(action, 200) or fallback
    tool = decoded["tool"]
    args = decoded["args"]
    if tool == "notify":
        message = _clean(args.get("message"))
        if not message:
            return "Reminder"
        return message if message.lower().startswith("reminder") else "Reminder: %s" % message
    if tool == "alarm":
        alarm_label = _clean(args.get("label"))
        return "Alarm: %s" % alarm_label if alarm_label and alarm_label.lower() != "alarm" else "Alarm"
    if tool == "playback" and args.get("reason") == "sleep_timer":
        return "Sleep timer"
    if tool == "playback":
        return "Music: %s" % (_clean(args.get("action")) or "playback")
    return fallback


__all__ = ["ACTION_PREFIX", "decode_tool_action", "describe_tool_action", "encode_tool_action"]
