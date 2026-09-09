"""Shared utilities for API response handling."""

from __future__ import annotations

import re
from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response


def _normalize_error_code(error_code: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", error_code.lower()).strip("_")
    return normalized or "request_failed"


def _default_error_message(error_code: str) -> str:
    message = error_code.replace("_", " ").strip()
    if not message:
        return "Request failed."
    message = message[0].upper() + message[1:]
    if message.endswith((".", "!", "?")):
        return message
    return f"{message}."


def error_response(
    error_code: str,
    status_code: int = 500,
    message: str | None = None,
) -> JSONResponse:
    """
    Create a standardized error JSONResponse.

    Args:
        error_code: Error code string (e.g., "play_failed", "not_supported")
        status_code: HTTP status code (default: 500)
        message: Optional error message (defaults to error_code if not provided)

    Returns:
        JSONResponse with standardized error format
    """
    error_msg = message if message is not None else _default_error_message(error_code)
    return JSONResponse(
        status_code=status_code,
        content=failure_response(_normalize_error_code(error_code), error_msg),
    )


def success_dict(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Create a standardized success response dict.

    Args:
        data: Optional data to merge into the response

    Returns:
        Dict with ok=True and optional data fields
    """
    response = {"ok": True, "error": None}
    if data:
        response.update(data)
    return response


def error_dict(error: str | Exception, **kwargs: Any) -> dict[str, Any]:
    """
    Create a standardized error response dict.

    Args:
        error: Error message string or Exception
        **kwargs: Optional additional fields to include

    Returns:
        Dict with ok=False and error message
    """
    error_msg = str(error)
    response = failure_response(
        _normalize_error_code(error_msg),
        _default_error_message(error_msg),
    )
    response.update(kwargs)
    return response


def inject_preferences(ps: dict[str, Any]) -> dict[str, Any]:
    """Inject repeat_mode and shuffle preferences into a state broadcast payload."""
    try:
        from core.state_selectors import select_repeat_mode, select_shuffle_enabled

        ps["repeat_mode"] = select_repeat_mode()
        ps["shuffle"] = select_shuffle_enabled()
    except Exception:
        pass
    return ps


def inject_multiroom_info(ps: dict[str, Any]) -> dict[str, Any]:
    """Inject source-local playback status into a state broadcast payload.

    Adds ``source_local_playback_active``, ``source_buffer_ms``, and ``cef_active``
    so the React UI can delay visual state updates to match the source's audio
    pipeline delay and skip rendering the YouTube iframe when CEF is
    capturing audio via Direct Injection (preventing double-play).

    Symbols renamed 2026-05-07 source/device pass: ``_hub_local_active`` →
    ``_source_local_active``, ``_spoke_count`` → ``_device_count``. The previous
    ``except ImportError`` swallowed the rename-induced import failure
    silently, leaving every state broadcast with zero/False for these fields.
    """
    try:
        from audio_core.streaming.pipeline_wiring import (
            _device_count,
            _source_local_active,
            is_direct_injection_active,
        )

        di_active = is_direct_injection_active()
        ps["source_local_playback_active"] = _source_local_active
        ps["yt_source_muted"] = di_active and _device_count > 0
        ps["cef_active"] = di_active
        if _source_local_active:
            from config.settings import settings

            ps["source_buffer_ms"] = getattr(settings, "source_buffer_ms", 140)
        else:
            ps["source_buffer_ms"] = 0
    except ImportError:
        ps["source_local_playback_active"] = False
        ps["yt_source_muted"] = False
        ps["cef_active"] = False
        ps["source_buffer_ms"] = 0
    return ps


async def broadcast_state(
    hub: Any,
    music: Any,
    state: Any,
    state_adapter: Any,
    force: bool = False,
    hub_authority: Any | None = None,
    user_id: str | None = None,
) -> None:
    """
    Helper to broadcast music player state via WebSocket hub.

    Args:
        hub: WebSocket hub instance
        music: Music player instance
        state: State object
        state_adapter: State adapter function/class
        force: Whether to force broadcast
        hub_authority: HubStateAuthority for state reconciliation (optional)
        user_id: Authenticated user scope for the broadcast (optional)
    """
    if user_id is None:
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except LookupError:
            user_id = None

    ps = state_adapter(music, state, hub_authority=hub_authority).model_dump()
    inject_preferences(ps)
    inject_multiroom_info(ps)
    await hub.broadcast("state", ps, user_id=user_id, force=force)
