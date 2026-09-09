"""Shared utilities and base mixin for instant command handlers."""

from __future__ import annotations

import ast
import asyncio
import datetime
import inspect
import math
import os
import random
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)
log = get_logger("viola.intent.instant")

_RESPONSE_VARIANTS: dict[str, tuple[str, ...]] = {
    "stopped": ("Stopped.", "Done.", "Music stopped."),
    "paused": ("Paused.", "Got it, paused.", "Music paused."),
    "resumed": ("Resuming.", "Playing again.", "Continuing playback."),
    "skipped": ("Skipped.", "Next track.", "Skipping ahead."),
    "previous": ("Going back.", "Previous track.", "Rewinding."),
}


def _vary(key: str, fallback: str = "") -> str:
    """Return a random response variant for the given key."""
    variants = _RESPONSE_VARIANTS.get(key)
    if variants:
        return random.choice(variants)
    return fallback


_MAX_EXPONENT_OPERAND: float = 1e15

_SAFE_MATH_FUNCS: dict[str, object] = {
    "sqrt": math.sqrt,
    "abs": abs,
}

_FORECAST_REQUEST_TERMS = ("forecast", "tomorrow", "week", "days", "upcoming")


def _is_forecast_request(text: str) -> bool:
    normalized = text.strip().lower()
    return any(term in normalized for term in _FORECAST_REQUEST_TERMS)


def _forecast_day_label(index: int, day: dict[str, object]) -> str:
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"

    raw_label = day.get("day")
    if isinstance(raw_label, str) and raw_label.strip():
        return raw_label.strip()

    raw_date = day.get("date")
    if isinstance(raw_date, str) and raw_date.strip():
        try:
            return datetime.date.fromisoformat(raw_date[:10]).strftime("%A")
        except ValueError:
            return raw_date.strip()

    return f"Day {index + 1}"


def _format_forecast_message(
    weather_data: dict[str, object],
    original_text: str,
) -> str | None:
    if not _is_forecast_request(original_text):
        return None

    forecast = weather_data.get("forecast")
    if not isinstance(forecast, list) or not forecast:
        return None

    location = weather_data.get("location", "your area")
    forecast_parts: list[str] = []
    for index, raw_day in enumerate(forecast[:3]):
        if not isinstance(raw_day, dict):
            continue

        label = _forecast_day_label(index, raw_day)
        condition_obj = raw_day.get("condition") or raw_day.get("summary")
        condition = condition_obj.strip() if isinstance(condition_obj, str) else ""
        high = raw_day.get("high")
        low = raw_day.get("low")
        temperature = raw_day.get("temperature")

        details: list[str] = []
        if isinstance(high, (int, float)) and isinstance(low, (int, float)):
            details.append(f"high {int(high)}°F and low {int(low)}°F")
        elif isinstance(temperature, (int, float)):
            details.append(f"around {int(temperature)}°F")
        if condition:
            details.append(condition.lower())
        if details:
            forecast_parts.append(f"{label} looks {' with '.join(details)}")

    if not forecast_parts:
        return None

    return f"In {location}, " + ". ".join(forecast_parts) + "."


def _eval_node(node: ast.AST) -> float | int:
    """Recursively evaluate an AST node for safe math operations only.

    Supports: numeric constants, +, -, *, /, //, %, **, unary +/-,
    and whitelisted single-arg functions (sqrt, abs).
    For **, both operands are converted to float and clamped to
    ±_MAX_EXPONENT_OPERAND so oversized inputs return ±inf quickly
    instead of allocating a huge integer.
    Raises ValueError for anything else.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        msg = "Unsupported unary operator"
        raise ValueError(msg)

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            # Convert both operands to float so Python uses IEEE-754 arithmetic
            # (avoiding arbitrarily-large integer allocation).  Clamp each to
            # ±_MAX_EXPONENT_OPERAND so extreme values produce ±inf instantly.
            fbase = float(left)
            fexp = float(right)
            if abs(fbase) > _MAX_EXPONENT_OPERAND:
                fbase = math.copysign(_MAX_EXPONENT_OPERAND, fbase)
            if abs(fexp) > _MAX_EXPONENT_OPERAND:
                fexp = math.copysign(_MAX_EXPONENT_OPERAND, fexp)
            try:
                return fbase**fexp
            except OverflowError:
                # Result exceeds float range → return infinity
                return math.inf
        msg = "Unsupported binary operator"
        raise ValueError(msg)

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _SAFE_MATH_FUNCS:
            if len(node.args) != 1 or node.keywords:
                msg = "Function %s takes exactly 1 argument" % node.func.id
                raise ValueError(msg)
            arg = _eval_node(node.args[0])
            return _SAFE_MATH_FUNCS[node.func.id](arg)
        msg = "Unsupported function"
        raise ValueError(msg)

    msg = "Unsupported expression"
    raise ValueError(msg)


def _safe_eval_math(expr: str) -> float | int:
    """Evaluate a math expression via AST walking — no eval() used."""
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree.body)


def _build_track_metadata(
    track: dict[str, object],
    video_id_value: str | None = None,
    query_url: str | None = None,
) -> dict[str, object]:
    """Build metadata dict from a playlist track for the player.

    Ensures the player receives the real title/artist instead of defaulting
    to the YouTube URL.
    """
    metadata: dict[str, object] = {}
    title = track.get("title")
    if isinstance(title, str) and title:
        metadata["title"] = title
    artist = track.get("artist") or track.get("uploader")
    if isinstance(artist, str) and artist:
        metadata["artist"] = artist
    vid = video_id_value or track.get("video_id")
    if isinstance(vid, str) and vid:
        metadata["video_id"] = vid
        metadata["provider"] = "youtube_music"
        metadata["thumbnail_url"] = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    url = track.get("url")
    if isinstance(url, str) and url:
        metadata["url"] = url
    # Ensure URL is always present (required by _metadata_to_resolution)
    if "url" not in metadata and query_url:
        metadata["url"] = query_url
    return metadata


def _broadcast_display_priority(mode: str) -> None:
    """Broadcast a display priority override via the overlay controller's broadcast_fn.

    Args:
        mode: "now_playing", "agentic_task", or "auto" (clears override)
    """
    try:
        from services.browser_overlay_controller import get_overlay_controller

        overlay = get_overlay_controller()
        if overlay is not None and overlay._broadcast_fn is not None:
            overlay._broadcast_fn(
                {
                    "type": "display_priority_override",
                    "payload": {"mode": mode},
                }
            )
    except Exception as exc:
        logger.debug("Display priority broadcast failed: %s", exc)


async def _call_maybe_async(target: object, method_name: str, *args: object) -> None:
    method = getattr(target, method_name, None)
    if not callable(method):
        return
    result = method(*args)
    if asyncio.iscoroutine(result):
        await result


__all__ = [
    "BaseInstantCommandHandlersMixin",
    "Path",
    "_broadcast_display_priority",
    "_build_track_metadata",
    "_call_maybe_async",
    "_format_forecast_message",
    "_safe_eval_math",
    "_vary",
    "ast",
    "asyncio",
    "datetime",
    "inspect",
    "log",
    "logger",
    "math",
    "os",
    "random",
    "re",
    "subprocess",
    "sys",
    "threading",
    "webbrowser",
]


class BaseInstantCommandHandlersMixin:
    """Common plumbing shared by instant command handler mixins."""

    @classmethod
    def build_handler_map(cls, instance: object) -> dict[str, object]:
        """Bind all public async command handlers across the full MRO."""
        return {
            name: getattr(instance, name)
            for name, member in inspect.getmembers(instance.__class__, predicate=inspect.iscoroutinefunction)
            if not name.startswith("_")
        }

    def __init__(self, controller_instance):
        """
        Initialize handlers.

        Args:
            controller_instance: The InstantCommandHandler instance
        """
        self.controller = controller_instance
        self._event_hub = None

    def set_event_hub(self, hub) -> None:
        """Inject the EventHub so seek commands can broadcast to WS clients."""
        self._event_hub = hub

    async def _broadcast_seek(self, position_seconds: float) -> None:
        """Broadcast a seek command to WebSocket clients (e.g. YouTube iframe).

        Without this broadcast the iframe player never receives the seekTo
        postMessage, so voice-initiated seeks update backend state but the
        video keeps playing from its old position.
        """
        hub = self._event_hub
        if hub is None:
            return
        try:
            from core.user_context import get_current_user_id

            user_id: str | None = get_current_user_id()
        except LookupError:
            user_id = None
        try:
            await hub.broadcast_command(
                "seek",
                {"position": position_seconds, "source": "voice"},
                user_id=user_id,
            )
        except Exception as exc:
            log.debug("Seek broadcast_command failed (non-critical): %s", exc)

    async def _broadcast_calendar_updated(self, action: str, payload: dict[str, object] | None = None) -> None:
        """Broadcast a calendar cache invalidation event to WS clients."""
        hub = self._event_hub
        if hub is None:
            return
        try:
            from core.user_context import get_current_user_id

            user_id: str | None = get_current_user_id()
        except LookupError:
            user_id = None
        try:
            body: dict[str, object] = {"action": action}
            if isinstance(payload, dict):
                body.update(payload)
            await hub.broadcast("calendar_updated", body, user_id=user_id, force=True)
        except Exception as exc:
            log.debug("Calendar update broadcast failed (non-critical): %s", exc)
