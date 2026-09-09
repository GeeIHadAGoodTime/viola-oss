from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return f'"{value!s}"'


def log_kv(logger: Any, level: str, event: str, **fields: Any) -> None:
    """
    Emit a structured key/value log line.

    Args:
        logger: Logger instance.
        level: Logging level name (e.g., \"info\", \"warning\").
        event: Event name for the log line.
        fields: Additional key/value fields to append.
    """
    log_method: Callable[[str], None] = getattr(logger, level, logger.info)
    parts = [f"event={event}"]
    for key, value in fields.items():
        parts.append(f"{key}={_format_value(value)}")
    log_method(" ".join(parts))
