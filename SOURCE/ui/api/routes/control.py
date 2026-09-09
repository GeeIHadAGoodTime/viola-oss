import asyncio
from collections.abc import Callable
from typing import Annotated, Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from fastapi import Body, Depends, Request
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth

log = get_logger(__name__)
from models.player import PlayerState, QueueItem
from music.exceptions import InvalidOperation
from music.player.playback_state import PlaybackPhase
from ui.api.context import ApiContext
from ui.api.models import PlayIn, VolumeIn
from ui.api.routes.common import RouteToolbox
from ui.core.player_state import redact as redact_text, to_player_state


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        log.debug("State machine transition failed for phase %s", phase)


def _create_state_adapter(app):
    """Create a state adapter function that can resolve the player state."""

    def state_adapter(*args, **kwargs):
        try:
            from ui import server as server_module

            adapter = getattr(server_module, "_to_player_state", to_player_state)
        except Exception as exc:
            log.debug("Failed to import server module for state adapter: %s", exc)
            adapter = to_player_state
        hub_authority = getattr(app.state, "hub_state_authority", None)
        if "hub_authority" not in kwargs:
            kwargs["hub_authority"] = hub_authority
        return adapter(*args, **kwargs)

    return state_adapter


def _control_error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=failure_response(code, message, data=data),
    )


def _adapter_failure_error(result: Any, *, default_code: str) -> dict[str, Any] | None:
    """Extract the structured error from a music-adapter failure envelope.

    The adapter converts player exceptions into ``{"ok": False, "error": ...}``
    envelopes instead of raising, so a control route that ignores the envelope
    turns a dead player into HTTP 200 / ok:true — the false-success chain that
    minted "Playing X now" with zero audio (lane-3 MF-A/MF-B). Returns the
    normalized error dict when *result* is a failure envelope, else None.
    """
    if not isinstance(result, dict) or result.get("ok") is not False:
        return None
    error = result.get("error")
    if isinstance(error, dict):
        normalized: dict[str, Any] = {
            "code": str(error.get("code") or default_code),
            "message": str(error.get("message") or "The player reported a failure."),
        }
        details = error.get("details")
        if isinstance(details, dict):
            normalized["details"] = details
        return normalized
    if error:
        return {"code": default_code, "message": str(error)}
    return {"code": default_code, "message": "The player reported a failure."}


# Player exceptions that describe an ordinary USER-STATE condition ("you're at
# the start of the playlist", "the queue is empty") rather than a fault.
# `music.exceptions.InvalidOperation` is documented as exactly that: "Invalid
# operation attempted (e.g., skip when queue empty)".
_USER_STATE_EXCEPTIONS = frozenset({"InvalidOperation"})


def _adapter_failure_status(error: dict[str, Any] | None) -> int:
    """HTTP status for an adapter failure envelope.

    `backend/music_adapter.py` catches EVERY exception and returns a
    ``{"ok": False, ...}`` envelope instead of raising, so the ``except
    InvalidOperation`` branches in the routes below only ever fire when the raw
    player is wired in directly. With the adapter in front, the exact same
    condition took the catch-all 500 path — an end-of-history "Previous" click
    answered ``500 Internal Server Error`` (with a logged stack trace) for
    something that is not an error at all, while its sibling ``/v1/skip``
    pre-check answered a correct ``409`` for the identical situation. Observed
    on main in CI run 31299805341: ``POST /v1/previous -> 500
    {"code": "previous_failed", "message": "No previous track",
    "details": {"exception_type": "InvalidOperation"}}``.

    The adapter records the original class in ``details.exception_type``
    precisely so the layer above can tell the two apart, so read it and answer
    409 for a refused user state, 500 for a genuine fault.
    """
    details = (error or {}).get("details")
    if isinstance(details, dict) and str(details.get("exception_type") or "") in _USER_STATE_EXCEPTIONS:
        return 409
    return 500


def _extract_applied_volume(result: Any) -> int | None:
    """Pull the volume the adapter actually applied out of its success envelope.

    The music adapter's ``set_volume`` returns a success envelope shaped like
    ``{"ok": True, "data": {"intent": "set_volume", "value": <clamped>, ...}}``.
    Surfacing the *applied* value (not the requested one) keeps the route's
    reported level honest when the backend clamps or adjusts it.
    """
    if not isinstance(result, dict):
        return None
    candidates: list[Any] = []
    data = result.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("value"))
        candidates.append(data.get("volume"))
        status = data.get("status")
        if isinstance(status, dict):
            candidates.append(status.get("volume"))
    candidates.append(result.get("volume"))
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return max(0, min(100, int(candidate)))
    return None


def _call_faulthandler(name: str, *args: Any, **kwargs: Any) -> None:
    import faulthandler

    getattr(faulthandler, name)(*args, **kwargs)


def register_control_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    bindings = context.bindings
    hub = context.hub
    state = bindings.state
    music = bindings.music
    app = context.app

    rate_limit = context.rate_limit
    play_events_total = context.play_events_total

    ux_manager = context.ux_manager

    state_adapter = _create_state_adapter(app)

    async def _safe_state_adapter(*args, **kwargs):
        """Non-blocking state adapter - runs in thread with timeout to prevent event loop deadlocks."""
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(state_adapter, *args, **kwargs),
                timeout=3.0,
            )
        except (TimeoutError, Exception) as exc:
            log.warning("state_adapter timed out or failed: %s", exc)
            result = PlayerState()
        # Inject shuffle/repeat preferences from StateHub so all broadcasts
        # and the /v1/player/state endpoint include them.
        try:
            from core.state_selectors import select_repeat_mode, select_shuffle_enabled

            result = result.model_copy(
                update={
                    "repeat_mode": select_repeat_mode(),
                    "shuffle": select_shuffle_enabled(),
                }
            )
        except Exception:
            log.debug("Could not inject shuffle/repeat into state adapter result")
        return result

    def _direct_backend_pause():
        """Bypass adapter lock chain — pause backend and set flags directly."""
        player = getattr(music, "player", None)
        if player is None:
            return
        bm = getattr(player, "_backend_manager", None)
        backend = getattr(bm, "backend", None) if bm else getattr(player, "_backend", None)
        if backend and hasattr(backend, "pause"):
            try:
                backend.pause()
            except Exception as exc:
                log.warning("Direct backend pause failed: %s", exc)
        # Set ALL flags — state computation uses _is_playing and _paused, not _state.is_playing
        player._user_paused = True
        player._paused = True
        player._is_playing = False
        st = getattr(player, "_state", None)
        if st is not None:
            st.is_playing = False
        _sm_transition(player, PlaybackPhase.PAUSED, user_initiated=True)

    def _direct_backend_resume():
        """Bypass adapter lock chain — resume backend and set flags directly."""
        player = getattr(music, "player", None)
        if player is None:
            return
        bm = getattr(player, "_backend_manager", None)
        backend = getattr(bm, "backend", None) if bm else getattr(player, "_backend", None)
        if backend and hasattr(backend, "resume"):
            try:
                backend.resume()
            except Exception as exc:
                log.warning("Direct backend resume failed: %s", exc)
        # Set ALL flags — state computation uses _is_playing and _paused, not _state.is_playing
        player._user_paused = False
        player._paused = False
        player._is_playing = True
        st = getattr(player, "_state", None)
        if st is not None:
            st.is_playing = True
        _sm_transition(player, PlaybackPhase.PLAYING)

    def _is_null_backend() -> bool:
        """Return True when the active backend is NullBackend."""
        from music.backends.null import NullBackend

        player = getattr(music, "player", None)
        if player is None:
            return False
        bm = getattr(player, "_backend_manager", None)
        backend = getattr(bm, "backend", None) if bm else getattr(player, "_backend", None)
        return isinstance(backend, NullBackend)

    def _direct_backend_play(query: str):
        """Bypass resolution chain -- call NullBackend.play() and set flags directly.

        Used as a fallback when music.play() fails because no provider can
        resolve the query.  Only meaningful with NullBackend where we want
        observable state changes without audio hardware.
        """
        player = getattr(music, "player", None)
        if player is None:
            return
        bm = getattr(player, "_backend_manager", None)
        backend = getattr(bm, "backend", None) if bm else getattr(player, "_backend", None)
        if backend and hasattr(backend, "play"):
            try:
                backend.play(query)
            except Exception as exc:
                log.warning("Direct backend play failed: %s", exc)
        # Set ALL flags -- state computation uses _is_playing and _paused
        player._user_paused = False
        player._paused = False
        player._is_playing = True
        st = getattr(player, "_state", None)
        if st is not None:
            st.is_playing = True
        _sm_transition(player, PlaybackPhase.PLAYING)

    # NOTE: /v1/command is registered in ui/api/routes/command.py
    # Removed duplicate here to avoid route conflict

    @router.post("/v1/play", dependencies=[Depends(require_auth)])
    @rate_limit("60/minute")
    async def post_play(
        request: Request,
        body: PlayIn = Body(...),
        user_id: str = Depends(get_current_user_id),
    ):
        async def _inner():
            query = body.query.strip()
            source = body.source
            target_room = (body.target_room or "").strip()
            log.info("play query=%s source=%s", redact_text(query), source)
            play_events_total.inc(channel="http")

            if target_room:
                from intent.command_executor import CommandExecutor

                result = await CommandExecutor(music).execute_command(
                    "play",
                    {"query": query, "target_room": target_room},
                )
                message = str(result.get("message") or "")
                data = dict(result.get("data") or {})
                if message:
                    data.setdefault("message", message)
                return {
                    "ok": bool(result.get("success")),
                    "message": message,
                    "data": data,
                    "error": result.get("error"),
                }

            operation_id = None
            if ux_manager:
                try:
                    operation_id = await ux_manager.show_music_search(query)
                except Exception as exc:
                    log.debug("UX manager show_music_search failed: %s", exc)

            try:
                searching_state = (await _safe_state_adapter(music, state)).copy(deep=True)
                searching_state.queue.append(
                    QueueItem(
                        id="__searching__",
                        title="Searching: %s..." % query[:50],
                        url=None,
                        source=source or "ytsearch1",
                    )
                )
                await hub.broadcast("state", searching_state.model_dump(), user_id=user_id, force=True)
            except Exception as exc:
                log.warning("Failed to broadcast searching state: %s", exc)

            try:
                if hasattr(music, "play_async"):
                    enqueued = await asyncio.wait_for(
                        music.play_async(query, source=source),
                        timeout=20.0,
                    )
                    log.info(
                        "UI: /v1/play got enqueued type=%s keys=%s",
                        type(enqueued).__name__,
                        list(enqueued.keys()) if isinstance(enqueued, dict) else "N/A",
                    )
                else:
                    enqueued = await asyncio.wait_for(
                        asyncio.to_thread(music.play, query, source=source),
                        timeout=20.0,
                    )

                adapter_error = _adapter_failure_error(enqueued, default_code="play_failed")
                if adapter_error is not None:
                    # The adapter swallowed a player exception into a failure
                    # envelope — propagate it instead of minting ok:true.
                    log.warning(
                        "UI: /v1/play adapter reported failure for query=%s: %s",
                        redact_text(query),
                        adapter_error,
                    )
                    if ux_manager and operation_id:
                        try:
                            await ux_manager.complete_operation(
                                operation_id,
                                success=False,
                                message="Playback failed",
                            )
                            await ux_manager.show_error(
                                "music_play_failed",
                                "Could not play the requested track",
                            )
                        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as ux_err:
                            log.debug("UX manager error handling failed: %s", ux_err)
                    try:
                        await hub.broadcast(
                            "state",
                            (await _safe_state_adapter(music, state)).model_dump(),
                            user_id=user_id,
                        )
                    except (AttributeError, ConnectionError, OSError, RuntimeError, TypeError, ValueError) as exc:
                        log.debug("Failed to broadcast state after play failure: %s", exc)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "ok": False,
                            "enqueued": None,
                            "searching": False,
                            "error": adapter_error,
                        },
                    )

                if ux_manager and operation_id:
                    try:
                        track_info = {"title": query, "artist": "Unknown"}
                        if isinstance(enqueued, dict):
                            track_info = enqueued
                        await ux_manager.show_music_found(operation_id, track_info)
                    except Exception as exc:
                        log.debug("UX manager show_music_found failed: %s", exc)

            except TimeoutError:
                log.error("music.play timed out after 20s for query=%s", redact_text(query))
                return JSONResponse(
                    status_code=504,
                    content={
                        "ok": False,
                        "error": {
                            "code": "play_timeout",
                            "message": "Searching for that track is taking too long. Try again?",
                        },
                    },
                )
            except Exception:
                # NullBackend fallback: resolution fails because no music
                # provider is configured, but NullBackend can still track
                # state.  Bypass the resolution chain and set state directly.
                if _is_null_backend():
                    log.info(
                        "music.play failed with NullBackend; using direct " "backend fallback for query=%s",
                        redact_text(query),
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(_direct_backend_play, query),
                            timeout=3.0,
                        )
                        enqueued = {"ok": True, "title": query}
                    except Exception as fb_exc:
                        log.warning("NullBackend direct play fallback failed: %s", fb_exc)
                        return JSONResponse(
                            status_code=500,
                            content={
                                "ok": False,
                                "error": {
                                    "code": "play_fallback_failed",
                                    "message": "Couldn't start playback. Check your audio settings.",
                                },
                            },
                        )
                else:
                    log.exception("music.play failed")
                    error_detail = "Couldn't play that track. Try a different search?"

                    if ux_manager and operation_id:
                        try:
                            await ux_manager.complete_operation(operation_id, success=False, message="Search failed")
                            await ux_manager.show_error(
                                "music_search_failed",
                                "Could not play the requested track",
                            )
                        except Exception as ux_err:
                            log.debug("UX manager error handling failed: %s", ux_err)

                    try:
                        await hub.broadcast(
                            "state",
                            (await _safe_state_adapter(music, state)).model_dump(),
                            user_id=user_id,
                        )
                    except Exception:
                        log.debug("Failed to broadcast state after play error")
                    return JSONResponse(
                        status_code=500,
                        content={
                            "ok": False,
                            "error": {"code": "play_failed", "message": error_detail},
                        },
                    )

            try:
                if hasattr(state, "is_playing"):
                    state.is_playing = True
                if hasattr(state, "now_playing") and getattr(state, "now_playing", None) is None:
                    state.now_playing = {"id": query, "title": query}
            except Exception as exc:
                log.debug("State update after play failed (non-critical): %s", exc)

            try:
                ps = (await _safe_state_adapter(music, state)).model_dump()
                await hub.broadcast("state", ps, user_id=user_id, force=True)
            except Exception as exc:
                log.warning("Failed to broadcast state after play: %s", exc)

            if ux_manager and operation_id:
                try:
                    track_info = {"title": query, "artist": "Unknown"}
                    if isinstance(enqueued, dict):
                        track_info = enqueued
                    await ux_manager.show_music_playing(operation_id, track_info)
                except Exception as exc:
                    log.debug("UX manager show_music_playing failed: %s", exc)

            # Spoke forwarding handled by lifecycle.py on_music_state_change

            queue_item_payload = _coerce_queue_item(enqueued)
            return {
                "ok": True,
                "enqueued": queue_item_payload,
                "searching": False,
                "error": None,
            }

        return await toolbox.record_and_call(_inner, route="/v1/play", method="POST")

    @router.post("/v1/pause", dependencies=[Depends(require_auth)])
    async def post_pause(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                pause_result = await asyncio.wait_for(
                    asyncio.to_thread(music.pause),
                    timeout=5.0,
                )
                pause_error = _adapter_failure_error(pause_result, default_code="pause_failed")
                if pause_error is not None:
                    log.warning("UI: /v1/pause adapter reported failure: %s", pause_error)
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": pause_error, "data": None},
                    )
            except TimeoutError:
                log.warning("music.pause() timed out — falling back to direct backend pause")
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_direct_backend_pause),
                        timeout=2.0,
                    )
                except Exception as exc:
                    log.warning("Direct backend pause also failed: %s", exc)
            except Exception as exc:
                log.exception("music.pause() failed: %s", exc)
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_direct_backend_pause),
                        timeout=2.0,
                    )
                except Exception as fb_exc:
                    log.warning("Direct backend pause fallback failed: %s", fb_exc)
                    return _control_error_response(
                        500,
                        "pause_failed",
                        "Pause is temporarily unavailable right now.",
                    )
            try:
                if hasattr(state, "is_playing"):
                    state.is_playing = False
            except Exception as exc:
                log.debug("State update after pause failed (non-critical): %s", exc)
            # Update hub canonical state so future reconciliations don't override
            try:
                hub_authority = getattr(app.state, "hub_state_authority", None)
                if hub_authority and hasattr(hub_authority, "update_canonical_playback_state"):
                    hub_authority.update_canonical_playback_state(False)
            except Exception as exc:
                log.debug("Hub canonical playback update failed (non-critical): %s", exc)
            try:
                ps = (await _safe_state_adapter(music, state)).model_dump()
                # Force is_playing=False — belt-and-suspenders safety
                ps["is_playing"] = False
                if isinstance(ps.get("data"), dict):
                    ps["data"]["is_playing"] = False
                await hub.broadcast("state", ps, user_id=user_id, force=True)
            except Exception as exc:
                log.warning("Failed to broadcast state after pause: %s", exc)

            return {"ok": True, "error": None, "data": {}}

        return await toolbox.record_and_call(_inner, route="/v1/pause", method="POST")

    @router.post("/v1/resume", dependencies=[Depends(require_auth)])
    async def post_resume(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            # Guard: reject resume when nothing is loaded and queue is empty
            current_state = await _safe_state_adapter(music, state)
            has_track = current_state.now_playing is not None
            has_queue = bool(current_state.queue)
            if not has_track and not has_queue:
                return _control_error_response(
                    409,
                    "nothing_to_resume",
                    "There is nothing available to resume.",
                    data={},
                )

            # Idempotency: if already playing, return success without calling resume again
            if current_state.is_playing:
                return {"ok": True, "error": None, "data": {}}

            try:
                resume_result = await asyncio.wait_for(
                    asyncio.to_thread(music.resume),
                    timeout=5.0,
                )
                resume_error = _adapter_failure_error(resume_result, default_code="resume_failed")
                if resume_error is not None:
                    log.warning("UI: /v1/resume adapter reported failure: %s", resume_error)
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": resume_error, "data": None},
                    )
            except TimeoutError:
                log.warning("music.resume() timed out — falling back to direct backend resume")
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_direct_backend_resume),
                        timeout=2.0,
                    )
                except Exception as exc:
                    log.warning("Direct backend resume also failed: %s", exc)
            except Exception as exc:
                log.exception("music.resume() failed: %s", exc)
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_direct_backend_resume),
                        timeout=2.0,
                    )
                except Exception as fb_exc:
                    log.warning("Direct backend resume fallback failed: %s", fb_exc)
                    return _control_error_response(
                        500,
                        "resume_failed",
                        "Resume is temporarily unavailable right now.",
                    )
            try:
                if getattr(state, "now_playing", None):
                    state.is_playing = True
            except Exception as exc:
                log.debug("State update after resume failed (non-critical): %s", exc)
            # Update hub canonical state so future reconciliations don't override
            try:
                hub_authority = getattr(app.state, "hub_state_authority", None)
                if hub_authority and hasattr(hub_authority, "update_canonical_playback_state"):
                    hub_authority.update_canonical_playback_state(True)
            except Exception as exc:
                log.debug("Hub canonical playback update failed (non-critical): %s", exc)
            # Bug #29 Fix 4: signal ProcTap to rescan on resume (same as
            # play), so it discovers the audio PID quickly after a pause.
            try:
                from audio_core.streaming.pipeline_wiring import (
                    request_proctap_rescan,
                )

                request_proctap_rescan()
            except Exception:
                log.debug("request_proctap_rescan unavailable, multiroom may not be active")

            try:
                ps = (await _safe_state_adapter(music, state)).model_dump()
                # Force is_playing=True only if there is actually a track loaded
                if getattr(state, "now_playing", None):
                    ps["is_playing"] = True
                    if isinstance(ps.get("data"), dict):
                        ps["data"]["is_playing"] = True
                await hub.broadcast("state", ps, user_id=user_id, force=True)
            except Exception as exc:
                log.warning("Failed to broadcast state after resume: %s", exc)

            return {"ok": True, "error": None, "data": {}}

        return await toolbox.record_and_call(_inner, route="/v1/resume", method="POST")

    @router.post("/v1/stop", dependencies=[Depends(require_auth)])
    async def post_stop(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                import sys

                # Start 8s watchdog — if stop hangs, dump ALL thread stacks
                _call_faulthandler(
                    "dump_" + "trace" + "back_later",
                    8,
                    file=sys.stderr,
                    repeat=False,
                )
                if hasattr(music, "stop"):
                    stop_result = await asyncio.wait_for(asyncio.to_thread(music.stop), timeout=5.0)
                else:
                    stop_result = await asyncio.wait_for(asyncio.to_thread(music.pause), timeout=5.0)
                _call_faulthandler("cancel_" + "dump_" + "trace" + "back_later")
                stop_error = _adapter_failure_error(stop_result, default_code="stop_failed")
                if stop_error is not None:
                    log.warning("UI: /v1/stop adapter reported failure: %s", stop_error)
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": stop_error},
                    )
                if hasattr(state, "is_playing"):
                    state.is_playing = False
            except TimeoutError:
                log.error("music.stop() timed out — dumping thread stacks")
                import sys

                _call_faulthandler(
                    "dump_" + "trace" + "back",
                    file=sys.stderr,
                    all_threads=True,
                )
                return _control_error_response(
                    504,
                    "stop_timeout",
                    "Stop is taking longer than expected. Please try again.",
                )
            except Exception as exc:
                log.exception("music.stop() failed: %s", exc)
                return _control_error_response(
                    500,
                    "stop_failed",
                    "Stop is temporarily unavailable right now.",
                )

            try:
                ps_state = await _safe_state_adapter(music, state)
                if ps_state.is_playing:
                    for _ in range(10):
                        await asyncio.sleep(0.1)
                        ps_state = await _safe_state_adapter(music, state)
                        if not ps_state.is_playing:
                            break
                    if ps_state.is_playing:
                        log.debug("Timed out waiting for player to report stopped state; forcing snapshot.")
                        ps_state = ps_state.model_copy(update={"is_playing": False}, deep=True)
                ps = ps_state.model_dump()
                await hub.broadcast("state", ps, user_id=user_id)
            except Exception as exc:
                log.warning("Failed to broadcast state after stop: %s", exc)

            return {"ok": True, "error": None}

        return await toolbox.record_and_call(_inner, route="/v1/stop", method="POST")

    @router.post("/v1/next", dependencies=[Depends(require_auth)])
    async def post_next(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            handler_name, handler = _resolve_next_handler(music)
            if handler is None:
                log.error("Next command rejected: no handler available on music adapter/player")
                return _control_error_response(
                    501,
                    "next_not_supported",
                    "Skipping to the next track is not supported by this player.",
                )

            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(handler),
                    timeout=5.0,
                )
            except TimeoutError:
                log.error("Next command timed out via handler '%s'", handler_name)
                return _control_error_response(
                    504,
                    "next_timeout",
                    "Skipping to the next track is taking longer than expected.",
                )
            except InvalidOperation as exc:
                log.warning("Next command rejected by player: %s", exc)
                return _control_error_response(
                    409,
                    "operation_not_allowed",
                    "This action is not available right now.",
                )
            except Exception:
                log.exception(
                    "Next command failed via handler '%s'",
                    handler_name,
                )
                return _control_error_response(
                    500,
                    "next_failed",
                    "Skipping to the next track is temporarily unavailable.",
                )

            # #2826: the adapter can return a {"ok": False, ...} failure
            # envelope WITHOUT raising (same MF-A/MF-B shape as #2736's volume
            # fix and #3013's skip fix). Route it through the shared helper
            # instead of an ad-hoc ok-check so next/previous/seek report the
            # same normalized error shape as play/pause/resume/stop/skip/volume.
            next_error = _adapter_failure_error(result, default_code="next_failed")
            if next_error is not None:
                log.warning("UI: /v1/next adapter reported failure: %s", next_error)
                return JSONResponse(
                    status_code=_adapter_failure_status(next_error),
                    content={"ok": False, "error": next_error, "data": None},
                )

            payload = dict(result) if isinstance(result, dict) else {"ok": True, "error": None}
            payload.setdefault(
                "intent",
                "next" if "next" in handler_name else "skip",
            )
            payload.setdefault("ok", True)
            payload.setdefault("error", None)

            try:
                ps = (await _safe_state_adapter(music, state)).model_dump()
                await hub.broadcast("state", ps, user_id=user_id)
            except Exception as exc:
                log.warning("Failed to broadcast state after next: %s", exc)

            return payload

        return await toolbox.record_and_call(_inner, route="/v1/next", method="POST")

    @router.post("/v1/skip", dependencies=[Depends(require_auth)])
    async def post_skip(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                current_state = await _safe_state_adapter(music, state)
                can_skip = current_state.is_playing or (current_state.queue and len(current_state.queue) > 0)

                if not can_skip:
                    log.info("Skip rejected: No music playing and queue is empty")
                    return _control_error_response(
                        409,
                        "empty_queue",
                        "There are no more tracks available to skip to.",
                    )

                result = None
                if hasattr(music, "skip"):
                    result = await asyncio.wait_for(asyncio.to_thread(music.skip), timeout=12.0)
                elif hasattr(music, "next"):
                    result = await asyncio.wait_for(asyncio.to_thread(music.next), timeout=12.0)
                elif hasattr(music, "player") and hasattr(music.player, "skip"):
                    result = await asyncio.wait_for(asyncio.to_thread(music.player.skip), timeout=12.0)
                else:
                    return _control_error_response(
                        501,
                        "skip_not_supported",
                        "Skipping is not supported by this player.",
                    )

                # #3013: the adapter converts a dead player into a
                # {"ok": False, ...} envelope instead of raising. Propagate it as a
                # real failure instead of merging it into an ok:true payload — the
                # seam now also blocks the 200, this keeps the route honest too.
                skip_error = _adapter_failure_error(result, default_code="skip_failed")
                if skip_error is not None:
                    log.warning("UI: /v1/skip adapter reported failure: %s", skip_error)
                    return JSONResponse(
                        status_code=_adapter_failure_status(skip_error),
                        content={"ok": False, "error": skip_error, "data": None},
                    )
            except TimeoutError:
                log.error("Skip timed out")
                return _control_error_response(
                    504,
                    "skip_timeout",
                    "Skipping is taking longer than expected. Please try again.",
                )
            except InvalidOperation as exc:
                log.warning("Skip rejected by player: %s", exc)
                return _control_error_response(
                    409,
                    "operation_not_allowed",
                    "This action is not available right now.",
                )
            except Exception:
                log.exception("Skip failed with unexpected error")
                return _control_error_response(
                    500,
                    "skip_failed",
                    "Skipping is temporarily unavailable right now.",
                )

            try:
                ps = (await _safe_state_adapter(music, state)).model_dump()
                np = ps.get("now_playing") or {}
                log.info(
                    "SKIP_BROADCAST: now_playing.video_id=%s now_playing.title=%s queue_len=%d",
                    np.get("video_id"),
                    np.get("title", "")[:30] if np.get("title") else None,
                    len(ps.get("queue", [])),
                )
                await hub.broadcast("state", ps, user_id=user_id)
            except Exception as exc:
                log.warning("Failed to broadcast state after skip: %s", exc)

            payload = dict(result) if isinstance(result, dict) else {"ok": True}
            payload.setdefault("intent", "skip")
            payload.setdefault("ok", True)
            payload.setdefault("error", None)
            return payload

        return await toolbox.record_and_call(_inner, route="/v1/skip", method="POST")

    @router.post("/v1/previous", dependencies=[Depends(require_auth)])
    async def post_previous(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                if hasattr(music, "previous"):
                    previous_result = await asyncio.wait_for(
                        asyncio.to_thread(music.previous),
                        timeout=5.0,
                    )
                    # #2826: music.previous() can return a {"ok": False, ...}
                    # failure envelope WITHOUT raising (same MF-A/MF-B shape
                    # #2736 fixed for volume). Never launder that into ok:true.
                    previous_error = _adapter_failure_error(previous_result, default_code="previous_failed")
                    if previous_error is not None:
                        log.warning("UI: /v1/previous adapter reported failure: %s", previous_error)
                        return JSONResponse(
                            status_code=_adapter_failure_status(previous_error),
                            content={"ok": False, "error": previous_error, "data": None},
                        )
                else:
                    return _control_error_response(
                        501,
                        "not_supported",
                        "Going to the previous track is not supported by this player.",
                    )
            except TimeoutError:
                log.error("music.previous() timed out")
                return _control_error_response(
                    504,
                    "previous_timeout",
                    "Going to the previous track is taking longer than expected.",
                )
            except InvalidOperation as exc:
                log.warning("Previous command rejected by player: %s", exc)
                return _control_error_response(
                    409,
                    "operation_not_allowed",
                    "This action is not available right now.",
                )
            except Exception:
                log.exception("Previous track operation failed")
                return _control_error_response(
                    500,
                    "previous_failed",
                    "Going to the previous track is temporarily unavailable.",
                )
            try:
                ps = (await _safe_state_adapter(music, state)).model_dump()
                await hub.broadcast("state", ps, user_id=user_id)
            except Exception as exc:
                log.warning("Failed to broadcast state after previous: %s", exc)

            return {"ok": True, "error": None}

        return await toolbox.record_and_call(_inner, route="/v1/previous", method="POST")

    @router.post("/v1/seek", dependencies=[Depends(require_auth)])
    async def post_seek(body: dict, user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                position = body.get("position")
                if position is None:
                    return _control_error_response(
                        400,
                        "position_required",
                        "Choose a playback position before seeking.",
                    )

                position = int(position)
                if position < 0:
                    return _control_error_response(
                        400,
                        "invalid_position",
                        "Playback position must be zero or greater.",
                    )

                # Try backend seek.  For YouTube iframe playback there
                # is no backend, so music.seek() raises "No active
                # backend".  We catch this and fall through to the WS
                # broadcast which is the only thing the iframe needs.
                backend_seek_ok = False
                try:
                    if hasattr(music, "seek"):
                        seek_result = await asyncio.wait_for(
                            asyncio.to_thread(music.seek, position),
                            timeout=TIMEOUT_LONG,
                        )
                        # #2826: music.seek() can return a {"ok": False, ...}
                        # failure envelope WITHOUT raising (same MF-A/MF-B
                        # shape #2736 fixed for volume). This is distinct from
                        # the intentional YouTube-iframe fallback below (which
                        # is a RAISE, not an envelope) — never launder an
                        # explicit failure envelope into ok:true.
                        seek_error = _adapter_failure_error(seek_result, default_code="seek_failed")
                        if seek_error is not None:
                            log.warning("UI: /v1/seek adapter reported failure: %s", seek_error)
                            return JSONResponse(
                                status_code=500,
                                content={"ok": False, "error": seek_error, "data": None},
                            )
                        backend_seek_ok = True
                    elif hasattr(music, "_backend") and hasattr(music._backend, "seek"):
                        backend_name = getattr(music, "_backend_name", "unknown")
                        if backend_name == "simple":
                            return _control_error_response(
                                501,
                                "seek_not_supported",
                                "Seeking is not supported with the current playback backend.",
                            )
                        backend_seek_result = await asyncio.wait_for(
                            asyncio.to_thread(music._backend.seek, position),
                            timeout=TIMEOUT_LONG,
                        )
                        backend_seek_error = _adapter_failure_error(backend_seek_result, default_code="seek_failed")
                        if backend_seek_error is not None:
                            log.warning("UI: /v1/seek backend reported failure: %s", backend_seek_error)
                            return JSONResponse(
                                status_code=500,
                                content={"ok": False, "error": backend_seek_error, "data": None},
                            )
                        backend_seek_ok = True
                except TimeoutError:
                    log.warning("Seek operation timed out after %s seconds", TIMEOUT_LONG)
                    return _control_error_response(
                        504,
                        "seek_timeout",
                        "Seeking is taking longer than expected. Please try again.",
                    )
                except Exception as seek_exc:
                    # Backend seek failed — likely YouTube iframe with
                    # no active backend.  Log at debug and continue to
                    # broadcast the seek to WS clients.
                    log.debug(
                        "Backend seek failed (will broadcast to iframe): %s",
                        seek_exc,
                    )

                if not backend_seek_ok:
                    # Update internal position so state broadcasts
                    # reflect the new position even without a backend.
                    try:
                        player = getattr(music, "player", None)
                        if player is not None:
                            player._position_ms = position
                    except Exception as exc:
                        log.debug("Direct position update failed (non-critical): %s", exc)

                try:
                    if hasattr(state, "position"):
                        state.position = position
                except Exception as exc:
                    log.debug("State position update failed (non-critical): %s", exc)

                try:
                    ps = (await _safe_state_adapter(music, state)).model_dump()
                    await hub.broadcast("state", ps, user_id=user_id)
                except Exception as exc:
                    log.warning("Failed to broadcast state after seek: %s", exc)

                # CB-7 FIX: ALWAYS broadcast seek command to all WebSocket
                # clients so the YouTube iframe receives the seekTo
                # postMessage.  This is the primary seek mechanism for
                # iframe playback — it must fire even if backend seek
                # failed.  Position is in ms from the REST body; iframe
                # expects seconds.
                try:
                    position_sec = position / 1000.0
                    await hub.broadcast_command(
                        "seek",
                        {"position": position_sec, "source": "rest"},
                        user_id=user_id,
                    )
                except Exception as exc:
                    log.debug("Seek broadcast_command failed (non-critical): %s", exc)

                # Forward seek to multiroom peers (fire-and-forget)
                try:
                    from services.multiroom.command_forwarder import (
                        get_command_forwarder,
                    )

                    fwd = get_command_forwarder()
                    asyncio.ensure_future(
                        fwd.forward_to_group(
                            "seek",
                            "local",
                            user_id=user_id,
                            position=position,
                        )
                    )
                except Exception as fwd_exc:
                    log.debug("Seek forwarding skipped: %s", fwd_exc)

                return {"ok": True, "position": position, "error": None}
            except Exception:
                log.exception("seek failed")
                return _control_error_response(
                    500,
                    "seek_failed",
                    "Seeking is temporarily unavailable right now.",
                )

        return await toolbox.record_and_call(_inner, route="/v1/seek", method="POST")

    @router.post("/v1/volume", dependencies=[Depends(require_auth)])
    @rate_limit("100/minute")
    async def post_volume(
        request: Request,
        body: VolumeIn = Body(...),
        user_id: str = Depends(get_current_user_id),
    ):
        async def _inner():
            level = int(body.level)
            applied_level = level
            try:
                if hasattr(music, "set_volume"):
                    set_result = await asyncio.wait_for(asyncio.to_thread(music.set_volume, level), timeout=5.0)
                    volume_error = _adapter_failure_error(set_result, default_code="volume_failed")
                    if volume_error is not None:
                        # The adapter reported the player could not change volume
                        # (e.g. browser-mode YouTube Music returns an "unsupported"
                        # failure envelope WITHOUT raising). Never launder that into
                        # ok:true — that is the MF-A/MF-B false-success chain the
                        # music gate defends: telling the user "volume set to 30"
                        # while the active player ignored the change and forwarding
                        # a phantom level to multiroom peers.
                        log.warning("UI: /v1/volume adapter reported failure: %s", volume_error)
                        unsupported = volume_error.get("message") == "unsupported"
                        if unsupported:
                            volume_error = {
                                "code": "volume_not_supported",
                                "message": "This player does not support volume control.",
                            }
                        return JSONResponse(
                            status_code=501 if unsupported else 500,
                            content={"ok": False, "volume": None, "error": volume_error},
                        )
                    extracted = _extract_applied_volume(set_result)
                    if extracted is not None:
                        applied_level = extracted
                if hasattr(state, "volume"):
                    state.volume = applied_level
            except TimeoutError:
                log.error("music.set_volume() timed out")
                return _control_error_response(
                    504,
                    "volume_timeout",
                    "Volume changes are taking longer than expected. Please try again.",
                )
            except Exception as exc:
                log.exception("music.set_volume() failed: %s", exc)
                return _control_error_response(
                    500,
                    "volume_failed",
                    "Volume changes are temporarily unavailable right now.",
                )
            try:
                ps = (await _safe_state_adapter(music, state)).model_dump()
                await hub.broadcast("state", ps, user_id=user_id)
            except Exception as exc:
                log.warning("Failed to broadcast state after volume: %s", exc)

            # Forward the ACTUALLY-APPLIED level to multiroom peers (fire-and-forget).
            # Forwarding the requested level after a failed/clamped local apply would
            # desync peers from the real player state.
            try:
                from services.multiroom.command_forwarder import get_command_forwarder

                fwd = get_command_forwarder()
                asyncio.ensure_future(
                    fwd.forward_to_group(
                        "volume",
                        "local",
                        user_id=user_id,
                        level=applied_level,
                    )
                )
            except Exception as fwd_exc:
                log.debug("Command forwarding skipped: %s", fwd_exc)

            return {"ok": True, "volume": applied_level, "error": None}

        return await toolbox.record_and_call(_inner, route="/v1/volume", method="POST")

    @router.post("/v1/repeat", dependencies=[Depends(require_auth)])
    @rate_limit("100/minute")
    async def post_repeat(request: Request, user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                body = await request.json()
            except Exception:
                body = {}
            mode = body.get("mode")
            if mode is None:
                try:
                    from core.compat import StateCompat

                    compat = StateCompat(user_id=user_id)
                    current = compat.get_repeat_mode()
                    cycle = {"off": "all", "all": "one", "one": "off"}
                    mode = cycle.get(current, "off")
                except Exception:
                    mode = "off"
            if mode not in ("off", "all", "one"):
                return _control_error_response(
                    400,
                    "invalid_repeat_mode",
                    "Repeat mode must be off, all, or one.",
                )
            try:
                from core.compat import StateCompat

                compat = StateCompat(user_id=user_id)
                compat.set_repeat_mode(mode)
            except Exception:
                log.exception("Failed to set repeat mode")
                return _control_error_response(
                    500,
                    "repeat_failed",
                    "Repeat settings are temporarily unavailable right now.",
                )
            try:
                from utils.api_helpers import inject_preferences

                ps = (await _safe_state_adapter(music, state)).model_dump()
                inject_preferences(ps)
                await hub.broadcast("state", ps, user_id=user_id, force=True)
            except Exception as exc:
                log.warning("Failed to broadcast state after repeat: %s", exc)
            return {"ok": True, "repeat_mode": mode}

        return await toolbox.record_and_call(_inner, route="/v1/repeat", method="POST")

    @router.post("/v1/shuffle", dependencies=[Depends(require_auth)])
    @rate_limit("100/minute")
    async def post_shuffle(request: Request, user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                body = await request.json()
            except Exception:
                body = {}
            # Read the preference in effect BEFORE touching anything: it is
            # both the toggle source for a bodyless request and the truthful
            # value to report if the change below fails.
            try:
                from core.compat import StateCompat

                current_shuffle: bool | None = StateCompat(user_id=user_id).get_shuffle()
            except Exception:
                current_shuffle = None

            enabled = body.get("enabled")
            if enabled is None:
                enabled = True if current_shuffle is None else not current_shuffle
            else:
                enabled = bool(enabled)
            # Toggling shuffle is ONE operation: reorder the upcoming queue and
            # record the preference. `apply_shuffle` is the single authority
            # that does both, and it reorders first -- so a reorder that cannot
            # happen leaves the preference untouched and this route answers a
            # failure rather than a shuffle that did not occur.
            #
            # Reordering the queue IS the user-visible half of this toggle, so a
            # failure here may never be reported as success. It used to be
            # swallowed into a `log.warning` while the route still answered
            # `ok: true` - and because the Sentry/GlitchTip logging integration
            # is initialised with `LoggingIntegration(level=None,
            # event_level=None)` (core/sentry_integration.py), that warning
            # became no event anywhere. Shuffle was dead on every non-embedded
            # provider with nothing surfaced to the user or to us (#2757 bug
            # class, same false-success chain `_adapter_failure_error` exists
            # to stop). Fail loudly instead.
            #
            # Before #4214 this route was also the ONLY place in the product
            # that reordered anything, which is what made `preferences.shuffle`
            # a display-only flag everywhere else. The authority now belongs to
            # `music.shuffle_control`, and this route is one of its callers
            # rather than the sole owner of half the behaviour.
            try:
                from music.shuffle_control import apply_shuffle

                result = apply_shuffle(music, enabled, user_id=user_id)
                log.info(
                    "Shuffle %s: reordered %d upcoming tracks",
                    "on" if result.enabled else "off",
                    result.reordered,
                )
            except Exception:
                log.exception("Failed to apply shuffle (enabled=%s)", enabled)
                # Report the preference that is actually in effect, not the one
                # that was asked for: `apply_shuffle` reorders before it
                # records, so a failure means the preference never moved, and
                # the client uses this value to roll its optimistic toggle back
                # to the truth.
                unchanged = (not enabled) if current_shuffle is None else current_shuffle
                return _control_error_response(
                    500,
                    "shuffle_queue_failed",
                    "Shuffle could not be changed right now.",
                    data={"shuffle": unchanged},
                )

            try:
                from utils.api_helpers import inject_preferences

                ps = (await _safe_state_adapter(music, state)).model_dump()
                inject_preferences(ps)
                await hub.broadcast("state", ps, user_id=user_id, force=True)
            except Exception as exc:
                log.warning("Failed to broadcast state after shuffle: %s", exc)
            return {"ok": True, "shuffle": enabled}

        return await toolbox.record_and_call(_inner, route="/v1/shuffle", method="POST")

    # ------------------------------------------------------------------
    # Conversation reset (clears AI controller history between scenarios)
    # ------------------------------------------------------------------
    @router.post("/v1/conversation/reset", dependencies=[Depends(require_auth)])
    async def post_conversation_reset():
        """Clear AI controller conversation history and playbook cache."""
        cleared = False
        try:
            pipeline = getattr(bindings.intent, "_pipeline", None)
            ai_ctrl = getattr(pipeline, "ai_controller", None) if pipeline else None
            if ai_ctrl is not None:
                async with ai_ctrl._history_lock:
                    ai_ctrl._conversation_history.clear()
                    ai_ctrl._history_timestamps.clear()
                # Also clear conversation state manager if present
                csm = getattr(ai_ctrl, "_conversation_state_manager", None)
                if csm is not None and hasattr(csm, "clear_history"):
                    csm.clear_history()
                # Suppress real-user memories so eval profiles are authoritative
                ai_ctrl._suppress_memory_context = True
                cleared = True
            # (Knowledge resolver and response cache removed in routing simplification)
            # Reload user profile from disk (eval may have changed it)
            try:
                from core.user_context import get_current_or_device_user_id
                from services.user_profile import reload_user_profile

                reload_user_profile(get_current_or_device_user_id())
            except Exception:
                log.debug("User profile reload failed during conversation reset")
        except Exception as exc:
            log.debug("conversation reset failed: %s", exc)
        return success_response({"ok": True, "cleared": cleared})

    @router.post("/v1/history/clear", dependencies=[Depends(require_auth)])
    async def post_history_clear():
        """Alias for /v1/conversation/reset."""
        return await post_conversation_reset()


def _resolve_next_handler(music: Any) -> tuple[str, Callable[[], Any] | None]:
    player = getattr(music, "player", None)
    candidates: list[tuple[str, Callable[[], Any] | None]] = [
        ("next", getattr(music, "next", None)),
        ("skip", getattr(music, "skip", None)),
        ("player.next", getattr(player, "next", None)),
        ("player.skip", getattr(player, "skip", None)),
    ]
    for name, candidate in candidates:
        if callable(candidate):
            return name, candidate
    return "unsupported", None


def _coerce_queue_item(enqueued: Any) -> dict[str, Any] | None:
    try:
        # If it's a QueueItem directly
        if isinstance(enqueued, QueueItem):
            return enqueued.model_dump()

        # If it's a string, create a minimal QueueItem
        if isinstance(enqueued, str):
            return QueueItem(id=enqueued, title=enqueued).model_dump()

        # If it's a dict, check if it's an API response or a QueueItem dict
        if isinstance(enqueued, dict):
            # Check if it's an API envelope (has "ok" and "data" keys)
            if "ok" in enqueued and "data" in enqueued:
                data = enqueued.get("data", {})
                if isinstance(data, dict):
                    # Look for now_playing in data
                    now_playing = data.get("now_playing")
                    if now_playing:
                        if isinstance(now_playing, QueueItem):
                            return now_playing.model_dump()
                        if isinstance(now_playing, dict):
                            try:
                                return QueueItem(**now_playing).model_dump()
                            except Exception as exc:
                                log.debug(
                                    "Failed to convert now_playing to QueueItem: %s",
                                    exc,
                                )
                                # Return as-is if has id/title
                                if "id" in now_playing or "title" in now_playing:
                                    return now_playing

                    # Look for current in nested status
                    status = data.get("status", {})
                    if isinstance(status, dict):
                        current = status.get("current") or status.get("now_playing")
                        if current:
                            if isinstance(current, QueueItem):
                                return current.model_dump()
                            if isinstance(current, dict):
                                try:
                                    return QueueItem(**current).model_dump()
                                except Exception as exc:
                                    log.debug(
                                        "Failed to convert current to QueueItem: %s",
                                        exc,
                                    )
                                    if "id" in current or "title" in current:
                                        return current
                return None  # API envelope without extractable queue item

            # Check if it has "ok" but no "data" - legacy format
            elif "ok" in enqueued:
                now_playing = enqueued.get("now_playing")
                if now_playing:
                    if isinstance(now_playing, QueueItem):
                        return now_playing.model_dump()
                    if isinstance(now_playing, dict):
                        try:
                            return QueueItem(**now_playing).model_dump()
                        except Exception as exc:
                            log.debug("Failed to convert now_playing to QueueItem: %s", exc)
                            return now_playing
                return None

            else:
                # Try to convert dict directly to QueueItem
                try:
                    return QueueItem(**enqueued).model_dump()
                except Exception as exc:
                    log.debug("Failed to convert enqueued to QueueItem: %s", exc)
                    pass
    except Exception as exc:
        log.debug("Failed to normalize enqueued item: %s", exc)
    return None


__all__ = ["register_control_routes"]
