from __future__ import annotations

import hmac
import threading
import time
from collections.abc import Iterable
from typing import Any

from fastapi.responses import JSONResponse

from config import env
from contracts.api_response import failure_response
from core.logging_config import get_logger
from diagnostics.queue_history import QueueEventType, get_queue_history, log_queue_event
from fastapi import Depends, HTTPException, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth

log = get_logger(__name__)

# =============================================================================
# DIAGNOSTIC EVENT STORAGE - stores recent iframe debug events
# =============================================================================

_yt_iframe_events: list[dict] = []
_yt_iframe_events_max = 200


def _store_yt_event(event_data: dict) -> None:
    """Store a YT iframe event for later retrieval."""
    global _yt_iframe_events
    _yt_iframe_events.append(event_data)
    if len(_yt_iframe_events) > _yt_iframe_events_max:
        _yt_iframe_events = _yt_iframe_events[-_yt_iframe_events_max:]


def _body_keys_for_log(body: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in body)[:20]


# =============================================================================
# SECURITY: Rate limiter for debug token authentication attempts
# =============================================================================

# Track failed auth attempts per IP: {ip: (count, first_failure_timestamp)}
_debug_auth_failures: dict[str, tuple[int, float]] = {}
_debug_auth_lock = threading.Lock()
_MAX_DEBUG_AUTH_FAILURES = 5  # Max failures before lockout
_DEBUG_AUTH_WINDOW_SECONDS = 300  # 5 minute window


def _check_debug_rate_limit(client_ip: str) -> None:
    """Check if client IP has exceeded debug auth rate limit."""
    if not client_ip:
        return

    now = time.time()
    with _debug_auth_lock:
        if client_ip in _debug_auth_failures:
            count, first_failure = _debug_auth_failures[client_ip]
            # Reset if window has passed
            if now - first_failure > _DEBUG_AUTH_WINDOW_SECONDS:
                del _debug_auth_failures[client_ip]
            elif count >= _MAX_DEBUG_AUTH_FAILURES:
                retry_after = int(_DEBUG_AUTH_WINDOW_SECONDS - (now - first_failure))
                log.warning(
                    "Debug auth rate limit exceeded for %s (%d attempts)",
                    client_ip,
                    count,
                )
                raise HTTPException(
                    status_code=429,
                    detail="Too many authentication attempts",
                    headers={"Retry-After": str(max(1, retry_after))},
                )


def _record_debug_auth_failure(client_ip: str) -> None:
    """Record a failed debug auth attempt."""
    if not client_ip:
        return

    now = time.time()
    with _debug_auth_lock:
        if client_ip in _debug_auth_failures:
            count, first_failure = _debug_auth_failures[client_ip]
            # Reset if window has passed
            if now - first_failure > _DEBUG_AUTH_WINDOW_SECONDS:
                _debug_auth_failures[client_ip] = (1, now)
            else:
                _debug_auth_failures[client_ip] = (count + 1, first_failure)
        else:
            _debug_auth_failures[client_ip] = (1, now)


class DebugRouteGuard:
    """Authorize access to debug endpoints even when global auth is disabled."""

    def __init__(self, context: ApiContext) -> None:
        from config.settings import settings

        security_context = context.security
        config = security_context.config

        # SECURITY: Fail-closed in production - debug routes disabled by default
        is_production = settings.env == "production" or env.get("VIOLA_ENV") == "production"
        if is_production:
            self._enabled = False
            log.info("Debug routes disabled in production environment")
        else:
            self._enabled = bool(getattr(config, "debug_routes_enabled", False) or config.debug_mode)

        allowlist_raw = getattr(config, "debug_route_allowlist", None) or []
        self._allowlist = self._normalize_allowlist(allowlist_raw)
        self._require_token = getattr(config, "debug_routes_require_token", True)

        auth_plugin = getattr(security_context, "auth_plugin", None)
        token_from_plugin: str | None = None
        if auth_plugin and hasattr(auth_plugin, "debug_auth_token"):
            token_from_plugin = auth_plugin.debug_auth_token

        self._debug_token = (
            token_from_plugin or env.get("VIOLA_DEBUG_AUTH_TOKEN") or env.get("NOVVIOLA_DEBUG_AUTH_TOKEN")
        )

    @staticmethod
    def _normalize_allowlist(raw: Iterable[str]) -> set[str]:
        normalized: set[str] = set()
        for entry in raw:
            trimmed = entry.strip()
            if not trimmed:
                continue
            normalized.add(trimmed.lower())
        return normalized

    def verify(self, request: Request) -> None:
        if not self._enabled:
            client_ip = (request.client.host if request.client else "") or ""
            if client_ip and client_ip.lower() in self._allowlist:
                log.debug("Debug allowlist granted for %s", client_ip)
                return
            raise HTTPException(status_code=404, detail="Not Found")

        # SECURITY: If neither token nor allowlist is configured, require token by default
        # This prevents misconfiguration from exposing debug routes publicly
        if not self._require_token and not self._allowlist:
            log.warning("Debug routes accessed without token or allowlist configuration")
            raise HTTPException(
                status_code=403,
                detail="Debug routes require authentication token",
            )

        client_ip = (request.client.host if request.client else "") or ""
        if self._allowlist and client_ip.lower() in self._allowlist:
            log.debug("Debug allowlist granted for %s", client_ip)
            return

        if not self._require_token:
            raise HTTPException(status_code=403, detail="Debug token required")

        # SECURITY: Check rate limit before processing token
        _check_debug_rate_limit(client_ip)

        provided_token = request.headers.get("X-Debug-Auth-Token")
        if provided_token and self._debug_token:
            if hmac.compare_digest(provided_token, self._debug_token):
                log.debug("Debug token validated for %s", request.url.path)
                return

        # SECURITY: Record failed attempt for rate limiting
        _record_debug_auth_failure(client_ip)
        raise HTTPException(status_code=403, detail="Debug access denied")


from ui.api.routes._guards import require_dev_mode as _require_dev_mode


def register_debug_routes(context: ApiContext) -> None:
    app = context.app
    guard = DebugRouteGuard(context)
    rate_limit = context.rate_limit

    def require_debug(request: Request) -> None:
        guard.verify(request)

    @app.get("/debug/routes")
    async def debug_routes(_: None = Depends(require_debug)):
        routes = []
        for route in app.routes:
            route_path = getattr(route, "path", None)
            route_methods = getattr(route, "methods", None)
            if route_path is not None:
                routes.append(
                    {
                        "path": route_path,
                        "methods": sorted(route_methods) if route_methods else [],
                        "name": getattr(route, "name", "unknown"),
                    }
                )
        return {"routes": routes, "total": len(routes)}

    @app.post("/debug/tools/call")
    async def debug_tools_call(
        request: Request,
        _: None = Depends(require_debug),
    ):
        """Call an MCP tool directly. Body: {"tool": "browser_snapshot", "args": {}}"""
        body = await request.json()
        tool_name = body.get("tool", "")
        tool_args = body.get("args", {})
        if not tool_name:
            return {"ok": False, "error": "Missing 'tool' field"}
        try:
            from intent.tools.self_management import _mcp_hub as hub

            if hub is None:
                return {"ok": False, "error": "MCP hub not initialized"}
            result = await hub.call_tool(tool_name, tool_args)
            # MCP result → dict
            if hasattr(result, "content"):
                texts = [c.text for c in result.content if hasattr(c, "text")]
                return {"ok": True, "data": texts[0] if len(texts) == 1 else texts}
            return {"ok": True, "data": str(result)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/debug/test-upload")
    async def debug_test_upload(
        _: None = Depends(require_debug),
        audio=Depends(_read_upload),
    ):
        filename, content, content_type = audio
        return {
            "ok": True,
            "filename": filename,
            "size": len(content),
            "content_type": content_type,
            "message": "File upload test successful",
        }

    # =========================================================================
    # DIAGNOSTIC ENDPOINTS for YouTube playback debugging
    # These endpoints receive events from React and iframe components
    # No auth required - they only log, no sensitive data returned
    # =========================================================================

    @app.post("/v1/debug/react-loaded", dependencies=[Depends(require_debug), Depends(_require_dev_mode)])
    @rate_limit("10/minute")
    async def debug_react_loaded(request: Request):
        """Called when React SmartDisplay component mounts."""
        try:
            body = (
                await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
            )
        except Exception:
            body = {}
        log.info("DIAG_REACT_LOADED: React SmartDisplay component mounted, fields=%s", _body_keys_for_log(body))
        return {"ok": True}

    @app.post("/v1/debug/state-received", dependencies=[Depends(require_debug), Depends(_require_dev_mode)])
    @rate_limit("10/minute")
    async def debug_state_received(request: Request):
        """Called when React receives playerState update."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        video_id = body.get("video_id")
        is_playing = body.get("is_playing")
        log.info(
            "DIAG_STATE_RECEIVED: React received state update, video_id=%s, is_playing=%s",
            video_id,
            is_playing,
        )
        return {"ok": True}

    @app.post("/v1/debug/youtube-embed-render", dependencies=[Depends(require_debug), Depends(_require_dev_mode)])
    @rate_limit("10/minute")
    async def debug_youtube_embed_render(request: Request):
        """Called when YouTubeEmbed component renders."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        video_id = body.get("video_id") or body.get("videoId")
        log.info(
            "DIAG_YOUTUBE_EMBED_RENDER: YouTubeEmbed rendering with videoId=%s",
            video_id,
        )
        return {"ok": True}

    # =========================================================================
    # CAPTURE WAV DUMP — answers "does capture already contain echo?"
    # =========================================================================

    @app.get(
        "/v1/debug/capture-wav",
        dependencies=[Depends(require_debug), Depends(_require_dev_mode)],
    )
    async def debug_capture_wav(request: Request, seconds: float = 5.0):
        """Dump the stamper PCM ring buffer to a WAV file on disk and return path.

        This captures the EXACT audio that gets sent to spokes (post-gain,
        post-stamper).  Listen to the WAV: if echo exists here, the problem
        is at capture (ProcTap/WASAPI). If clean, echo is in delivery.
        """
        import io
        import struct as st
        import wave
        from pathlib import Path as _P

        from fastapi.responses import FileResponse

        stamper = getattr(request.app.state, "chunk_stamper", None)
        if stamper is None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "ChunkStamper not active"},
            )

        seconds = min(seconds, 10.0)
        raw_pcm = stamper.get_pcm_capture(seconds)
        if not raw_pcm:
            return JSONResponse(
                status_code=204,
                content={"ok": False, "error": "No audio in PCM ring (silence?)"},
            )

        bit_depth = getattr(stamper, "_bit_depth", 16)
        sample_rate = 48000
        channels = 2

        # Convert int24 packed → int16 for universal WAV playback
        if bit_depth == 24:
            import numpy as np

            # Unpack int24 LE → int32 via zero-padded bytes, then shift to int16
            raw = np.frombuffer(raw_pcm, dtype=np.uint8)
            n_samples = len(raw) // 3
            raw = raw[: n_samples * 3].reshape(-1, 3)
            padded = np.zeros((n_samples, 4), dtype=np.uint8)
            padded[:, :3] = raw
            int32_vals = padded.view(np.int32).reshape(-1)
            # Sign-extend: shift left 8 then arithmetic right 8
            int32_vals = (int32_vals << 8).astype(np.int32) >> 8
            # Take top 16 bits (>> 8) and clamp to int16
            int16_vals = np.clip(int32_vals >> 8, -32768, 32767).astype(np.int16)
            pcm_bytes = int16_vals.tobytes()
            wav_bits = 16
        else:
            pcm_bytes = raw_pcm
            wav_bits = 16

        wav_path = _P("logs") / "capture_debug.wav"
        wav_path.parent.mkdir(parents=True, exist_ok=True)

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(wav_bits // 8)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)

        duration_sec = len(pcm_bytes) / (sample_rate * channels * (wav_bits // 8))
        log.info(
            "CAPTURE_WAV: saved %.1fs of audio to %s (%d bytes, %d-bit)",
            duration_sec,
            wav_path,
            len(pcm_bytes),
            wav_bits,
        )

        return FileResponse(
            str(wav_path),
            media_type="audio/wav",
            filename="capture_debug.wav",
            headers={
                "X-Duration-Sec": f"{duration_sec:.2f}",
                "X-Bit-Depth-Source": str(bit_depth),
                "X-Samples": str(len(pcm_bytes) // (channels * (wav_bits // 8))),
            },
        )

    @app.post("/v1/debug/yt-iframe-event", dependencies=[Depends(require_debug), Depends(_require_dev_mode)])
    @rate_limit("60/minute")
    async def debug_yt_iframe_event(request: Request):
        """Receive events from youtube_iframe.html for server-side logging."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        event = body.get("event", "unknown")
        body["received_at"] = time.time()
        _store_yt_event(body)
        log.info("DIAG_YT_IFRAME: event=%s, fields=%s", event, _body_keys_for_log(body))
        return {"ok": True}

    @app.get(
        "/v1/debug/yt-events",
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def get_yt_events(request: Request):
        """Get recent YT iframe debug events."""
        return {
            "ok": True,
            "events": _yt_iframe_events,
            "count": len(_yt_iframe_events),
        }

    @app.post(
        "/v1/debug/yt-events/clear",
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def clear_yt_events(request: Request):
        """Clear stored YT iframe debug events."""
        global _yt_iframe_events
        _yt_iframe_events = []
        return {"ok": True, "message": "Events cleared"}

    @app.post("/v1/debug/yt-track-ended", dependencies=[Depends(require_debug), Depends(_require_dev_mode)])
    @rate_limit("10/minute")
    async def debug_yt_track_ended(request: Request):
        """Called when React detects video ended and triggers skip."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        video_id = body.get("video_id")
        log.info(
            "DIAG_YT_TRACK_ENDED: React detected track ended, video_id=%s, triggering skip",
            video_id,
        )
        return {"ok": True}

    @app.post(
        "/v1/debug/simulate-yt-ended",
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    @rate_limit("10/minute")
    async def simulate_yt_ended(request: Request):
        """Simulate YouTube ENDED event for testing auto-advance.

        This endpoint triggers the same completion flow that would occur
        when YouTube reports state=0 (ENDED) via WebSocket.
        """
        from ui.websocket.command_handlers import _handle_track_ended

        try:
            body = await request.json()
        except Exception:
            body = {}
        video_id = body.get("video_id")

        log.info("SIMULATE_YT_ENDED: Simulating track end for video_id=%s", video_id)

        # Get the music player from context bindings
        try:
            music = context.bindings.music
            if music is None:
                log.error("SIMULATE_YT_ENDED: Music player not available in bindings")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "music_player_unavailable",
                        "Music playback is not available right now.",
                    ),
                )

            hub = context.hub
            state = context.bindings.state

            # The music binding might be an adapter - get the actual player
            actual_player = getattr(music, "_player", music) if hasattr(music, "_player") else music
            if hasattr(music, "player"):
                actual_player = music.player

            # Get state before
            playlist = getattr(actual_player, "_playlist", None)
            current_before = playlist.current() if playlist else None
            current_id_before = getattr(current_before, "id", None) if current_before else None

            # Also check _state.now_playing for embedded mode
            player_state = getattr(actual_player, "_state", None)
            state_now_playing = getattr(player_state, "now_playing", None) if player_state else None
            state_now_playing_id = getattr(state_now_playing, "id", None) if state_now_playing else None

            log.info(
                "SIMULATE_YT_ENDED: Got music=%s, actual_player=%s, hub=%s, state=%s",
                type(music).__name__,
                type(actual_player).__name__,
                type(hub).__name__,
                type(state).__name__,
            )
            log.info(
                "SIMULATE_YT_ENDED: Before - playlist.current()=%s, _state.now_playing=%s",
                current_id_before,
                state_now_playing_id,
            )

            # Call the same handler that WebSocket would trigger
            # Pass actual_player so the handler can find _playlist and _state
            await _handle_track_ended(actual_player, hub, music, state)

            # Get state after
            current_after = playlist.current() if playlist else None
            current_id_after = getattr(current_after, "id", None) if current_after else None
            state_now_playing_after = getattr(player_state, "now_playing", None) if player_state else None
            state_now_playing_id_after = (
                getattr(state_now_playing_after, "id", None) if state_now_playing_after else None
            )

            log.info(
                "SIMULATE_YT_ENDED: After - playlist.current()=%s, _state.now_playing=%s",
                current_id_after,
                state_now_playing_id_after,
            )
            log.info(
                "SIMULATE_YT_ENDED: Changed? now_playing %s -> %s",
                state_now_playing_id,
                state_now_playing_id_after,
            )

            log.info("SIMULATE_YT_ENDED: Track end simulated successfully")
            return {
                "ok": True,
                "simulated": video_id,
                "music_type": type(music).__name__,
                "player_type": type(actual_player).__name__,
                "before_playlist_current": current_id_before,
                "before_now_playing": state_now_playing_id,
                "after_playlist_current": current_id_after,
                "after_now_playing": state_now_playing_id_after,
                "now_playing_changed": state_now_playing_id != state_now_playing_id_after,
            }
        except Exception:
            log.exception("SIMULATE_YT_ENDED: Failed to simulate track end")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "simulate_track_end_failed",
                    "Try again in a moment",
                ),
            )

    # =========================================================================
    # QUEUE HISTORY ENDPOINTS - mandatory for debugging oscillation loops
    # Returns ring buffer of most recent queue operations (last 200)
    # =========================================================================

    @app.get(
        "/v1/debug/queue-history",
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def get_queue_history_endpoint(
        request: Request,
        limit: int = 200,
        since_seq: int | None = None,
        track_id: str | None = None,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ):
        """Get recent queue operation history for debugging.

        Returns the last N queue/playback events with all structured fields
        including track_id, command_id, correlation_id, reason, from_id, to_id, etc.

        Query params:
            limit: Max events to return (capped at 200)
            since_seq: Only return events with seq > this value
            track_id: Filter by track ID
            command_id: Filter by command ID (user-action idempotency key)
            correlation_id: Filter by correlation ID (playback attempt ID)

        This endpoint is mandatory for debugging "why did we play X again?" issues.
        """
        history = get_queue_history()
        events = history.get_recent(
            limit=min(limit, 200),
            since_seq=since_seq,
            track_id=track_id,
            command_id=command_id,
            correlation_id=correlation_id,
        )
        state = history.get_state_snapshot()

        # Get queue snapshot for current state
        queue_snapshot = None
        try:
            music = context.bindings.music
            if music is None:
                raise ValueError("Music player not initialized")
            player = getattr(music, "player", music)
            playlist = player._playlist

            current = playlist.current()
            upcoming = playlist.upcoming()

            queue_snapshot = {
                "current_id": current.id if current else None,
                "upcoming_first_5": [item.id for item in upcoming[:5]],
                "queue_length": len(upcoming),
                "pending": playlist.pending,
                "version": playlist.version,
            }
        except Exception as e:
            log.debug("Could not get queue snapshot: %s", e)
            queue_snapshot = {"error": "snapshot_unavailable"}

        return {
            "ok": True,
            "server_time": time.time(),
            "events": events,
            "count": len(events),
            "state": state,
            "queue_snapshot": queue_snapshot,
        }

    @app.post(
        "/v1/debug/queue-history/clear",
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def clear_queue_history_endpoint(request: Request):
        """Clear queue operation history."""
        get_queue_history().clear()
        return {"ok": True, "message": "Queue history cleared"}

    @app.get(
        "/v1/debug/queue-state",
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def get_queue_state_endpoint(request: Request):
        """Get current queue state snapshot for debugging.

        Returns:
        - current track id/title
        - next track id/title
        - queue length
        - list of upcoming track ids
        - any retry state for current track
        """
        try:
            music = context.bindings.music
            if music is None:
                return {"ok": False, "error": "Music player not initialized yet."}
            player = getattr(music, "player", music)
            playlist = player._playlist

            current = playlist.current()
            upcoming = playlist.upcoming()

            return {
                "ok": True,
                "current": (
                    {
                        "id": current.id if current else None,
                        "title": getattr(current, "title", None) if current else None,
                        "video_id": (getattr(current, "video_id", None) if current else None),
                    }
                    if current
                    else None
                ),
                "next": (
                    {
                        "id": upcoming[0].id if upcoming else None,
                        "title": (getattr(upcoming[0], "title", None) if upcoming else None),
                        "video_id": (getattr(upcoming[0], "video_id", None) if upcoming else None),
                    }
                    if upcoming
                    else None
                ),
                "queue_length": len(upcoming),
                "upcoming_ids": [item.id for item in upcoming[:10]],
                "playlist_version": playlist.version,
                "playlist_pending": playlist.pending,
                "history_snapshot": get_queue_history().get_state_snapshot(),
            }
        except Exception:
            log.exception("Failed to get queue state")
            return {
                "ok": False,
                "error": "Couldn't load queue state. Please try again.",
            }

    @app.post(
        "/v1/debug/log-queue-event",
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    @rate_limit("60/minute")
    async def log_queue_event_endpoint(request: Request):
        """Manually log a queue event for debugging purposes.

        Useful for UI components to report events that should be correlated
        with backend queue operations.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}

        event_type_str = body.get("event_type", "QUEUE_ADD")
        try:
            event_type = QueueEventType(event_type_str)
        except ValueError:
            event_type = QueueEventType.ADD

        log_queue_event(
            event_type,
            track_id=body.get("track_id"),
            title=body.get("title"),
            queue_pos=body.get("queue_pos"),
            current_id=body.get("current_id"),
            next_id=body.get("next_id"),
            reason=body.get("reason"),
            outcome=body.get("outcome"),
            command_id=body.get("command_id"),
            queue_length=body.get("queue_length"),
        )
        return {"ok": True}

    log.info("Debug routes registered with guarded access (including YouTube diagnostics and queue history)")


async def _read_upload(request: Request):
    try:
        form = await request.form()
        upload_file: Any = None
        for value in form.values():
            if hasattr(value, "read") and callable(getattr(value, "read", None)):
                upload_file = value
                break
        if upload_file is None:
            raise HTTPException(status_code=400, detail="No file provided")
        content = await upload_file.read()
        filename = getattr(upload_file, "filename", "unknown")
        content_type = getattr(upload_file, "content_type", "application/octet-stream")
        if hasattr(upload_file, "close") and callable(getattr(upload_file, "close", None)):
            await upload_file.close()
        return filename, content, content_type
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Failed to read debug upload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid upload payload") from exc
