from __future__ import annotations

import asyncio

from contracts.api_response import success_response
from contracts.player_state import sanitize_player_state_payload
from core.logging_config import get_logger
from fastapi import Depends
from models.player import PlayerState, get_player_state_schema
from ui.api.routes.auth_dependencies import require_auth

log = get_logger(__name__)
from ui.api.context import ApiContext
from ui.api.routes.common import RouteToolbox
from ui.core.player_state import to_player_state as _to_player_state


def register_state_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    state = context.bindings.state
    music = context.bindings.music
    app = context.app

    async def _safe_get_state():
        """Non-blocking state fetch — runs in thread with timeout."""
        hub_authority = getattr(app.state, "hub_state_authority", None)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_to_player_state, music, state, hub_authority=hub_authority),
                timeout=3.0,
            )
        except (TimeoutError, Exception) as exc:
            log.warning("_to_player_state timed out or failed: %s", exc)
            return PlayerState()

    def _inject_multiroom_into_envelope(envelope: dict) -> dict:
        """Inject multiroom/CEF fields into a sanitized response envelope.

        ``sanitize_player_state_payload`` rebuilds a ``PlayerState`` which
        drops extra keys (``extra="ignore"``).  We inject the multiroom
        fields (``cef_active``, ``yt_hub_muted``, ``hub_local_playback_active``,
        ``hub_buffer_ms``) into the envelope's ``data`` dict *after*
        sanitization so they reach the REST client.
        """
        from utils.api_helpers import inject_multiroom_info

        data = envelope.get("data")
        if isinstance(data, dict):
            inject_multiroom_info(data)
        return envelope

    @router.get("/v1/state", dependencies=[Depends(require_auth)])
    async def get_state():
        async def _inner():
            ps = await _safe_get_state()
            payload = ps.model_dump()
            payload.update({"ok": True, "error": None})
            from utils.api_helpers import inject_preferences

            inject_preferences(payload)
            envelope = sanitize_player_state_payload(payload)
            return _inject_multiroom_into_envelope(envelope)

        return await toolbox.record_and_call(_inner, route="/v1/state", method="GET")

    @router.get("/v1/music/state", dependencies=[Depends(require_auth)])
    async def get_music_state():
        """Alias for /v1/state — backwards-compatible route for test suites and clients."""

        async def _inner():
            ps = await _safe_get_state()
            payload = ps.model_dump()
            payload.update({"ok": True, "error": None})
            from utils.api_helpers import inject_preferences

            inject_preferences(payload)
            envelope = sanitize_player_state_payload(payload)
            return _inject_multiroom_into_envelope(envelope)

        return await toolbox.record_and_call(_inner, route="/v1/music/state", method="GET")

    @router.get("/v1/schema/player_state", dependencies=[Depends(require_auth)])
    async def get_state_schema():
        async def _inner():
            return success_response({"schema": get_player_state_schema()})

        return await toolbox.record_and_call(_inner, route="/v1/schema/player_state", method="GET")

    @router.get("/v1/player/state", dependencies=[Depends(require_auth)])
    async def get_player_state():
        async def _inner():
            ps = await _safe_get_state()
            snapshot = ps.model_dump()
            snapshot.update({"ok": True, "error": None})
            from utils.api_helpers import inject_preferences

            inject_preferences(snapshot)
            try:
                queue_len = len(snapshot.get("queue") or [])
                title = None
                now_playing = snapshot.get("now_playing")
                if isinstance(now_playing, dict):
                    title = now_playing.get("title")
                log.debug(
                    "REST /v1/player/state queue=%s now_playing=%s is_playing=%s",
                    queue_len,
                    title,
                    snapshot.get("is_playing"),
                )
            except Exception as e:
                log.debug("Failed to log player state debug info: %s", e)
                pass
            envelope = sanitize_player_state_payload(snapshot)
            _inject_multiroom_into_envelope(envelope)
            data = envelope["data"]
            if isinstance(data, dict):
                data["source"] = "player_state"
            return envelope

        return await toolbox.record_and_call(_inner, route="/v1/player/state", method="GET")

    log.info("📊 State routes registered")
