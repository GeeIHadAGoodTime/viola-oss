"""
WebSocket command handlers for bidirectional playback control.

This module provides handlers for commands sent from WebSocket clients:
- seek: Seek to a specific position (from React UI → YouTube iframe)
- position_update: Update playback position (from YouTube iframe → React UI)
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from fastapi import WebSocket
from music.player.playback_state import PlaybackPhase
from music.runtime.worker_manager import record_finished_track_recents
from ui.core.player_state import to_player_state

# Map YouTube player states to PlaybackPhase for dual-write state machine migration
_YT_PHASE_MAP: dict[str, PlaybackPhase] = {
    "PLAYING": PlaybackPhase.PLAYING,
    "PAUSED": PlaybackPhase.PAUSED,
    "BUFFERING": PlaybackPhase.LOADING,
    "UNSTARTED": PlaybackPhase.LOADING,
    "CUED": PlaybackPhase.LOADING,
}


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        logger.debug("State machine transition failed for phase %s", phase)


from utils.api_helpers import broadcast_state

if TYPE_CHECKING:
    from ui.websocket.event_hub import EventHub

logger = get_logger(__name__)


def _extract_user_id_from_hub(hub: EventHub, ws: WebSocket) -> str | None:
    """Resolve the caller's user identity from websocket state or hub-wide fallback."""
    user_id = hub._client_user.get(ws)
    if isinstance(user_id, str) and user_id:
        return user_id

    user_clients = getattr(hub, "_user_clients", {})
    if isinstance(user_clients, dict) and len(user_clients) == 1:
        only_user = next(iter(user_clients))
        if isinstance(only_user, str) and only_user:
            return only_user

    mapped_users = {
        candidate for candidate in getattr(hub, "_client_user", {}).values() if isinstance(candidate, str) and candidate
    }
    if len(mapped_users) == 1:
        return next(iter(mapped_users))

    return None


async def handle_seek(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """
    Handle seek commands from WebSocket clients.

    Broadcasts the seek command to all clients so that the YouTube iframe
    can receive and execute the seek.

    Args:
        action: Command action ("seek")
        payload: {position: float, source: str, _message_id: str}
        ws: WebSocket that sent the command
        hub: EventHub for broadcasting
    """
    position = payload.get("position", 0)
    source = payload.get("source", "unknown")
    # Phase 5: Extract message_id for echo prevention
    origin_message_id = payload.get("_message_id")

    # Input validation - reject invalid seek positions
    try:
        position = float(position) if position else 0
        # Reject negative positions
        if position < 0:
            logger.warning("WS seek: Invalid negative position %ss from %s", position, source)
            position = 0
        # Reject impossibly large positions (12 hours max)
        max_position = 12 * 60 * 60  # 12 hours in seconds
        if position > max_position:
            logger.warning(
                "WS seek: Position %ss exceeds max %ss from %s",
                position,
                max_position,
                source,
            )
            position = max_position
    except (TypeError, ValueError):
        logger.warning(
            "WS seek: Invalid position value: %s from %s",
            payload.get("position"),
            source,
        )
        return  # Don't broadcast invalid seeks

    logger.info("WS seek command: %ss from %s", position, source)

    user_id = _extract_user_id_from_hub(hub, ws)
    if not user_id:
        logger.debug("Skipping seek broadcast: websocket user_id is missing")
        return

    # Broadcast seek command to all clients (including YouTube iframe)
    # Pass origin_message_id so clients can avoid processing their own commands
    await hub.broadcast_command(
        "seek",
        {"position": position, "source": source},
        user_id=user_id,
        origin_message_id=origin_message_id,
    )


async def handle_youtube_state(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
    music: Any,
    state: Any,
) -> None:
    """
    Handle YouTube player state updates from the iframe.

    Updates internal player state and broadcasts to all clients
    so React UI and other components stay in sync.

    CRITICAL: When state is ENDED, this handler MUST call complete_current()
    to advance the queue to the next track. Without this, the queue stalls forever.

    Args:
        action: Command action ("youtube_state")
        payload: {state: str, position: float, duration: float, video_id: str}
        ws: WebSocket that sent the command
        hub: EventHub for broadcasting
        music: Music player adapter
        state: App state
    """
    yt_state = payload.get("state", "UNKNOWN")
    position = payload.get("position", 0)
    duration = payload.get("duration", 0)
    video_id = payload.get("video_id")

    # Input validation - reject obviously invalid values
    valid_states = {
        "UNSTARTED",
        "ENDED",
        "PLAYING",
        "PAUSED",
        "BUFFERING",
        "CUED",
        "UNKNOWN",
    }
    if yt_state not in valid_states:
        logger.warning("YouTube state: Invalid state value received: %s", yt_state)
        return

    # Validate position/duration are reasonable (reject negative or impossibly large values)
    try:
        position = max(0, float(position)) if position else 0
        duration = max(0, float(duration)) if duration else 0
        # Reject positions beyond duration (with 5s grace for timing differences)
        if duration > 0 and position > duration + 5:
            logger.warning("YouTube state: Invalid position %ss > duration %ss", position, duration)
            position = duration
    except (TypeError, ValueError):
        logger.warning(
            "YouTube state: Invalid position/duration values: pos=%s, dur=%s",
            position,
            duration,
        )
        position = 0
        duration = 0

    logger.info(
        "YouTube state: %s, pos: %ss, dur: %ss, video_id: %s",
        yt_state,
        position,
        duration,
        video_id,
    )

    # CRITICAL FIX: Validate video_id matches current track to prevent stale events
    # from old videos (e.g., delayed ENDED events) affecting the current track
    try:
        player_for_validation = getattr(music, "player", music)
        current_track = None
        if hasattr(player_for_validation, "_state"):
            current_track = getattr(player_for_validation._state, "now_playing", None)
        current_video_id = getattr(current_track, "video_id", None) if current_track else None

        if video_id and current_video_id and video_id != current_video_id:
            logger.warning(
                "STALE_EVENT: Ignoring YouTube state from old video. " "event_video=%s, current_video=%s, state=%s",
                video_id,
                current_video_id,
                yt_state,
            )
            return  # Don't process stale events from old videos
    except Exception as exc:
        logger.debug("Video ID validation failed (non-critical): %s", exc)
        # Continue processing if validation fails - better to process than drop

    # Emit debug event so we can verify handler is being called
    try:
        from ui.qt_native.debug_events import emit_debug_event

        emit_debug_event(
            "youtube_state_handler",
            {
                "state": yt_state,
                "position": position,
                "duration": duration,
            },
        )
    except Exception as exc:
        logger.debug("Debug event emission failed (non-critical): %s", exc)

    try:
        # Unwrap MusicControllerAdapter to get the actual player with _position_ms etc.
        player = getattr(music, "player", music)

        logger.info(
            "youtube_state handler: player=%s, type=%s",
            player is not None,
            type(player).__name__ if player else "None",
        )
        if player is not None:
            logger.info(
                "  player attrs: _is_playing=%s, _paused=%s",
                hasattr(player, "_is_playing"),
                hasattr(player, "_paused"),
            )
            position_ms = int(position * 1000)
            duration_ms = int(duration * 1000) if duration and duration > 0 else 0

            # CRITICAL FIX: Use condition variable for thread-safe state mutations
            # This prevents race conditions with the worker thread reading state
            cv = getattr(player, "_cv", None)
            if cv is not None:
                with cv:
                    if hasattr(player, "_position_ms"):
                        player._position_ms = position_ms
                    if hasattr(player, "_duration_ms") and duration_ms > 0:
                        player._duration_ms = duration_ms
                    if hasattr(player, "_is_playing"):
                        old_is_playing = player._is_playing
                        player._is_playing = yt_state == "PLAYING"
                        logger.info(
                            "  Set _is_playing: %s -> %s",
                            old_is_playing,
                            player._is_playing,
                        )
                    # CRITICAL: Also update _paused flag so state formula works correctly
                    # State formula is: is_playing = _is_playing AND NOT _paused
                    if hasattr(player, "_paused"):
                        old_paused = player._paused
                        player._paused = yt_state == "PAUSED"
                        logger.info("  Set _paused: %s -> %s", old_paused, player._paused)

                    # State machine dual-write
                    _yt_phase = _YT_PHASE_MAP.get(yt_state)
                    if _yt_phase is not None:
                        _sm_transition(player, _yt_phase)

                    # CRITICAL FIX: Mark track as having reached PLAYING state.
                    # This enables silence detection for tracks that actually started.
                    # Without this, silence detection would incorrectly skip tracks
                    # that never started playing (stayed at UNSTARTED).
                    if yt_state == "PLAYING":
                        backend_mgr = getattr(player, "_backend_manager", None)
                        if backend_mgr is not None and hasattr(backend_mgr, "mark_track_started_playing"):
                            backend_mgr.mark_track_started_playing()
                            logger.info("  Marked track as started playing (silence detection now active)")

                        # Trigger ProcTap rescan on PLAYING state so it
                        # discovers any new audio renderer PID within ~200ms
                        # during YouTube auto-advance.
                        try:
                            from audio_core.streaming.pipeline_wiring import (
                                request_proctap_rescan,
                            )

                            request_proctap_rescan()
                        except Exception:
                            logger.debug("request_proctap_rescan unavailable, multiroom may not be active")

                    # NOTE: _state.is_playing is NOT updated here - it's ignored by state()
                    # which computes is_playing via formula: _is_playing AND NOT _paused
                    # Hub authority reconciliation happens in broadcast_state() via to_player_state()
                    # Wake up worker thread to process state change
                    cv.notify_all()
            else:
                # Fallback for players without condition variable (shouldn't happen)
                logger.warning("Player missing _cv - state mutations not thread-safe")
                if hasattr(player, "_position_ms"):
                    player._position_ms = position_ms
                if hasattr(player, "_duration_ms") and duration_ms > 0:
                    player._duration_ms = duration_ms
                if hasattr(player, "_is_playing"):
                    player._is_playing = yt_state == "PLAYING"
                if hasattr(player, "_paused"):
                    player._paused = yt_state == "PAUSED"
                # State machine dual-write (fallback path)
                _yt_phase = _YT_PHASE_MAP.get(yt_state)
                if _yt_phase is not None:
                    _sm_transition(player, _yt_phase)
                # NOTE: _state.is_playing NOT updated - state() computes via formula

            # Emit debug event with results
            try:
                from ui.qt_native.debug_events import emit_debug_event

                state_obj = getattr(player, "_state", None)
                state_mgr = getattr(player, "_state_manager", None)
                state_mgr_player = getattr(state_mgr, "player", None) if state_mgr else None
                hub_auth = getattr(player, "_hub_state_authority", None)
                hub_canonical = getattr(hub_auth, "_canonical_state", None) if hub_auth else None
                emit_debug_event(
                    "youtube_state_updated",
                    {
                        "state": yt_state,
                        "is_playing_now": getattr(player, "_is_playing", None),
                        "paused_now": getattr(player, "_paused", None),
                        "state_is_playing": (getattr(state_obj, "is_playing", None) if state_obj else None),
                        "hub_is_playing": (getattr(hub_canonical, "is_playing", None) if hub_canonical else "N/A"),
                        "player_type": type(player).__name__,
                        "player_id": id(player),
                        "state_mgr_player_id": (id(state_mgr_player) if state_mgr_player else None),
                        "same_player": (id(player) == id(state_mgr_player) if state_mgr_player else "N/A"),
                    },
                )
            except Exception as exc:
                logger.debug("Debug event emission failed (non-critical): %s", exc)

            # CRITICAL FIX: Handle track completion when YouTube reports ENDED
            # This is what advances the queue to the next track
            if yt_state == "ENDED":
                logger.info("YouTube track ENDED - advancing queue. video_id=%s", video_id)
                await _handle_track_ended(
                    player,
                    hub,
                    music,
                    state,
                    user_id=_extract_user_id_from_hub(hub, ws),
                    event_video_id=video_id,
                )
                return  # State already broadcast in _handle_track_ended

        # Broadcast state update — scope to user when possible
        # Pass hub_authority so reconciliation happens via to_player_state()
        hub_authority = getattr(player, "_hub_state_authority", None) if player else None
        user_id = _extract_user_id_from_hub(hub, ws)
        ps = to_player_state(music, state, hub_authority=hub_authority).model_dump()
        from utils.api_helpers import inject_multiroom_info, inject_preferences

        inject_preferences(ps)
        inject_multiroom_info(ps)
        if not user_id:
            logger.debug("Skipping state broadcast from handle_youtube_state: user_id missing")
        elif hasattr(hub, "broadcast_playback_state"):
            await hub.broadcast_playback_state("state", ps, user_id=user_id, force=True)
        else:
            await hub.broadcast("state", ps, user_id=user_id, force=True)

    except Exception as e:
        logger.exception("YouTube state update failed: %s", e)


async def _handle_track_ended(
    player: Any,
    hub: EventHub,
    music: Any,
    state: Any,
    *,
    user_id: str | None = None,
    event_video_id: str | None = None,
) -> None:
    """
    Handle the completion of a YouTube track.

    This is called when YouTube reports ENDED state. It:
    1. Completes the current track in the playlist (which auto-schedules next)
    2. Updates internal player state
    3. Triggers the worker to start the next track
    4. Broadcasts the state update

    Handles both standard playback (playlist.current() is set) and
    embedded playback mode (only _state.now_playing is set).

    Note: Repeat One mode is handled by the worker thread, not here.
    This keeps the logic centralized and avoids duplication.

    Args:
        player: MusicPlayer instance
        hub: EventHub for broadcasting
        music: Music player adapter
        state: App state
    """
    # Unwrap adapter to get the actual MusicPlayer with _cv, _playlist, _state.
    # The 'player' parameter may be MusicControllerAdapter (which wraps the real player).
    actual_player = player
    if hasattr(player, "player"):
        actual_player = player.player
    elif hasattr(player, "_player"):
        actual_player = player._player

    logger.info(
        "TRACK_ENDED_HANDLER: Entered. raw=%s, actual=%s",
        type(player).__name__,
        type(actual_player).__name__,
    )

    # Check repeat mode — Repeat One replays the current track
    repeat_mode = "off"
    try:
        from core.state_selectors import select_repeat_mode

        repeat_mode = select_repeat_mode()
    except Exception:
        logger.debug("select_repeat_mode unavailable, defaulting to off")

    try:
        finished_track = None
        next_track = None
        # Deferred backend call — captured inside lock, executed outside
        deferred_backend = None
        deferred_url = None

        # Use the player's condition variable for thread-safe access
        cv = getattr(actual_player, "_cv", None)
        playlist = getattr(actual_player, "_playlist", None)

        if cv is not None and playlist is not None:
            with cv:
                # ATOMIC STALE EVENT GUARD: Re-check video_id match inside the
                # lock to prevent TOCTOU race.  The pre-lock guard in
                # handle_youtube_state() is best-effort; this is authoritative.
                # Between the outer guard and this lock acquisition, another
                # thread may have changed now_playing (e.g. a new play command).
                if event_video_id:
                    _player_state = getattr(actual_player, "_state", None)
                    _np = getattr(_player_state, "now_playing", None) if _player_state else None
                    _cur_vid = getattr(_np, "video_id", None) if _np else None
                    if _cur_vid and event_video_id != _cur_vid:
                        logger.warning(
                            "STALE_EVENT_LOCKED: Dropping ENDED for old video. " "event_video=%s, current_video=%s",
                            event_video_id,
                            _cur_vid,
                        )
                        return

                # Get the current track before completing
                # For embedded mode, playlist.current() may be None but _state.now_playing is set
                current = playlist.current()
                player_state = getattr(actual_player, "_state", None)
                state_now_playing = getattr(player_state, "now_playing", None) if player_state else None

                # Use state.now_playing if playlist.current() is None (embedded playback mode)
                if current is None and state_now_playing is not None:
                    logger.info("TRACK_ENDED: Using embedded mode - current from _state.now_playing")
                    current = state_now_playing

                # Repeat One: replay the same track without advancing the queue
                if repeat_mode == "one" and current is not None:
                    logger.info(
                        "TRACK_ENDED: Repeat One — replaying %s",
                        getattr(current, "id", "unknown"),
                    )
                    next_track = current
                    if hasattr(actual_player, "_set_now_playing_locked"):
                        actual_player._set_now_playing_locked(next_track)
                    if player_state is not None:
                        player_state.is_playing = True
                        _sm_transition(actual_player, PlaybackPhase.PLAYING)
                        backend = getattr(actual_player, "_backend", None)
                        if backend and hasattr(backend, "play"):
                            deferred_backend = backend
                            deferred_url = getattr(next_track, "url", None) or ""
                    cv.notify_all()
                    # Jump to deferred play outside lock
                elif current is not None:
                    # For embedded mode, add to history manually since playlist.current() is None
                    if playlist.current() is None:
                        # Embedded mode: manually add to history
                        finished_track = current
                        if hasattr(actual_player, "_history"):
                            try:
                                actual_player._history.append(finished_track)
                            except Exception as exc:
                                logger.warning("TRACK_ENDED: Failed to append to history: %r", exc)
                        if hasattr(actual_player, "_last_played"):
                            actual_player._last_played = finished_track
                        logger.info(
                            "TRACK_ENDED: Embedded mode - added %s to history",
                            getattr(finished_track, "id", "unknown"),
                        )

                        # Get next from queue (first upcoming track). Routed
                        # through the engine's mutator (not raw attribute
                        # writes on the wrapper) so the cursor's real
                        # current/upcoming state - and therefore the
                        # broadcast snapshot - actually advances instead of
                        # silently desyncing (#2757).
                        next_track = playlist.advance_embedded_cursor()
                        if next_track is not None:
                            logger.info(
                                "TRACK_ENDED: Embedded mode - next track is %s",
                                getattr(next_track, "id", "unknown"),
                            )
                        else:
                            logger.info("TRACK_ENDED: Embedded mode - queue empty")
                    else:
                        # Standard mode: use playlist.complete_current()
                        finished_track = playlist.complete_current(success=True)
                        logger.info(
                            "Track completed: %s",
                            getattr(finished_track, "id", "unknown"),
                        )

                        # Add to history (complete_current already does this, but also update player._history)
                        if finished_track and hasattr(actual_player, "_history"):
                            # Only append if not already the last item (complete_current adds to playlist history)
                            if not actual_player._history or actual_player._history[-1] != finished_track:
                                actual_player._history.append(finished_track)
                            actual_player._last_played = finished_track

                        # Get next track (already scheduled by complete_current)
                        next_track = playlist.current()

                    if finished_track is not None:
                        record_finished_track_recents(
                            actual_player,
                            finished_track,
                            user_id=user_id,
                            logger=logger,
                        )

                    # Repeat All: when queue is exhausted, loop from history.
                    # Routed through the engine mutator (see
                    # advance_embedded_cursor above) rather than poking
                    # playlist._history/_upcoming/_current directly, which
                    # are not real attributes on the PlaylistQueueEngine
                    # wrapper and left the cursor's real state (and the
                    # broadcast snapshot) stale (#2757).
                    if next_track is None and repeat_mode == "all" and playlist.history_ref:
                        looped_count = len(playlist.history_ref)
                        next_track = playlist.loop_from_history()
                        if next_track is not None:
                            logger.info(
                                "TRACK_ENDED: Repeat All — looping %d tracks, starting with %s",
                                looped_count,
                                getattr(next_track, "id", "unknown"),
                            )

                    # Check if autoplay should add more tracks
                    autoplay = getattr(actual_player, "autoplay", None)
                    if autoplay is not None and finished_track is not None:
                        try:
                            autoplay.set_anchor(finished_track)
                            autoplay.check_and_run_async()
                        except Exception:
                            logger.exception("Autoplay trigger failed after track end")

                # Set now_playing to next track (or None if queue is empty)
                if hasattr(actual_player, "_set_now_playing_locked"):
                    actual_player._set_now_playing_locked(next_track)

                # For embedded mode, update is_playing and capture backend for
                # deferred play() call OUTSIDE the lock to prevent deadlock.
                if player_state is not None:
                    if next_track is not None:
                        player_state.is_playing = True
                        _sm_transition(actual_player, PlaybackPhase.PLAYING)
                        backend = getattr(actual_player, "_backend", None)
                        if backend and hasattr(backend, "play"):
                            deferred_backend = backend
                            deferred_url = getattr(next_track, "url", None) or ""
                    else:
                        player_state.is_playing = False
                        _sm_transition(actual_player, PlaybackPhase.IDLE)

                # Wake up the worker thread to process the next track
                # The worker will handle Repeat One mode if enabled
                cv.notify_all()

        # Call backend.play() OUTSIDE _cv to prevent deadlock — play() may
        # acquire WebSocket/Qt locks that could contend with _cv.
        #
        # And OFF the event loop: SimpleBackend.play() synchronously probes the
        # next source with ffprobe (_probe_duration_frames) and spawns ffmpeg —
        # run inline it froze the asyncio loop up to 4.9s at every track
        # transition, starving spoke audio WS sends (py-spy conviction in
        # _diag/2026-07-01/spoke_ios_foreground_stall_and_video_sync.md).
        # play() is thread-safe (internal lock) and normally runs from the
        # player worker thread anyway.
        if deferred_backend is not None:
            try:
                await asyncio.to_thread(deferred_backend.play, deferred_url)
                logger.info(
                    "TRACK_ENDED: Backend play() called for %s (outside lock)",
                    getattr(next_track, "id", "unknown"),
                )
            except Exception as exc:
                logger.warning("TRACK_ENDED: Backend play() failed: %s", exc)

        # Emit state change (also outside lock)
        if hasattr(actual_player, "_emit"):
            actual_player._emit()

        # Broadcast updated state to all WebSocket clients
        # Pass hub_authority so reconciliation happens via to_player_state()
        hub_authority = getattr(actual_player, "_hub_state_authority", None)
        await broadcast_state(
            hub,
            music,
            state,
            to_player_state,
            hub_authority=hub_authority,
            user_id=user_id,
        )

        logger.info(
            "Track end handled: finished=%s, next=%s",
            getattr(finished_track, "id", None),
            getattr(next_track, "id", None),
        )

    except Exception as e:
        logger.exception("Failed to handle track end: %s", e)


async def handle_video_bounds(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """
    Handle video bounds updates from React UI.

    The React SmartDisplay reports the bounds of the video container
    so the native Qt YouTube overlay can position itself correctly.

    Args:
        action: Command action ("video_bounds")
        payload: {x: int, y: int, width: int, height: int, source: str}
        ws: WebSocket that sent the command
        hub: EventHub for broadcasting
    """
    x = payload.get("x", 0)
    y = payload.get("y", 0)
    width = payload.get("width", 0)
    height = payload.get("height", 0)
    source = payload.get("source", "unknown")

    logger.debug("Video bounds from %s: (%s, %s) %sx%s", source, x, y, width, height)

    # Broadcast bounds to all clients (Qt overlay controller will receive this)
    user_id = _extract_user_id_from_hub(hub, ws)
    await hub.broadcast(
        "video_bounds",
        {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "source": source,
        },
        user_id=user_id,
        force=True,
    )  # Force broadcast - bounds updates are important


async def handle_position_update(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
    music: Any,
    state: Any,
) -> None:
    """
    Handle position updates from YouTube iframe.

    Updates the internal player state and broadcasts to all clients
    so the React UI progress bar updates.

    Args:
        action: Command action ("position_update")
        payload: {position: float, duration: float}
        ws: WebSocket that sent the command
        hub: EventHub for broadcasting
        music: Music player adapter
        state: App state
    """
    position = payload.get("position", 0)
    duration = payload.get("duration", 0)  # Default to 0 like position

    # Debug: Log that we received the position update
    logger.info(
        "Position update received: %.1fs / %ss from %s",
        position,
        duration,
        payload.get("source", "unknown"),
    )

    try:
        # Unwrap MusicControllerAdapter to get the actual player with _position_ms etc.
        player = getattr(music, "player", music)

        if player is not None:
            # Player stores position in milliseconds - safely convert to numeric
            try:
                position_ms = int(float(position) * 1000) if position else 0
                duration_ms = int(float(duration) * 1000) if duration and float(duration) > 0 else 0
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid position/duration values: position=%s, duration=%s",
                    position,
                    duration,
                )
                position_ms = 0
                duration_ms = 0

            # CRITICAL FIX: Use condition variable for thread-safe state mutations
            cv = getattr(player, "_cv", None)
            if cv is not None:
                with cv:
                    if hasattr(player, "_position_ms"):
                        player._position_ms = position_ms
                        logger.info("Set player._position_ms = %sms", position_ms)
                    else:
                        logger.warning("Player missing _position_ms attribute")
                    if hasattr(player, "_duration_ms") and duration_ms > 0:
                        player._duration_ms = duration_ms
                        logger.info("Set player._duration_ms = %sms", duration_ms)
                    elif duration_ms > 0:
                        logger.warning("Player missing _duration_ms attribute")
                    # Note: Don't notify_all() for position updates - not a state change
            else:
                # Fallback for players without condition variable
                if hasattr(player, "_position_ms"):
                    player._position_ms = position_ms
                if hasattr(player, "_duration_ms") and duration_ms > 0:
                    player._duration_ms = duration_ms

            logger.debug("Updated player position: %sms / %sms", position_ms, duration_ms)
        else:
            logger.warning("Could not get player from music adapter")

        # Broadcast state update — scope to user when possible
        # Pass hub_authority so reconciliation happens via to_player_state()
        hub_authority = getattr(player, "_hub_state_authority", None) if player else None
        user_id = _extract_user_id_from_hub(hub, ws)
        ps = to_player_state(music, state, hub_authority=hub_authority).model_dump()
        from utils.api_helpers import inject_multiroom_info, inject_preferences

        inject_preferences(ps)
        inject_multiroom_info(ps)
        if not user_id:
            logger.debug("Skipping state broadcast from handle_position_update: user_id missing")
        elif hasattr(hub, "broadcast_playback_state"):
            await hub.broadcast_playback_state("state", ps, user_id=user_id)
        else:
            await hub.broadcast("state", ps, user_id=user_id)
    except Exception as e:
        logger.warning("Position update failed: %s", e)


async def handle_modal_opened(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """
    Handle modal_opened event from React UI.

    When a React modal dialog opens, this notifies the Qt FocusManager
    so it can block focus requests from lower-priority windows (like OAuth popups).

    Args:
        action: Command action ("modal_opened")
        payload: {modal_id: str}
        ws: WebSocket that sent the command
        hub: EventHub for broadcasting
    """
    modal_id = payload.get("modal_id", "react_modal")
    logger.info("React modal opened: %s", modal_id)

    # Notify the desktop focus manager from the FastAPI event handler.
    try:
        from ui.qt_native.focus_manager import get_focus_manager

        focus_mgr = get_focus_manager()
        focus_mgr.push_react_modal(modal_id)

        # Broadcast to all clients so other parts of the UI can react
        user_id = _extract_user_id_from_hub(hub, ws)
        await hub.broadcast(
            "focus_event",
            {
                "event_type": "modal_opened",
                "modal_id": modal_id,
            },
            user_id=user_id,
            force=True,
        )

    except Exception as e:
        logger.exception("Failed to handle modal_opened: %s", e)


async def handle_modal_closed(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """
    Handle modal_closed event from React UI.

    When a React modal dialog closes, this notifies the Qt FocusManager
    to restore focus to the previous window in the focus stack.

    Args:
        action: Command action ("modal_closed")
        payload: {modal_id: str}
        ws: WebSocket that sent the command
        hub: EventHub for broadcasting
    """
    modal_id = payload.get("modal_id", "react_modal")
    logger.info("React modal closed: %s", modal_id)

    # Update the FocusManager
    try:
        from ui.qt_native.focus_manager import get_focus_manager

        focus_mgr = get_focus_manager()
        focus_mgr.pop_react_modal(modal_id)

        # Broadcast to all clients
        user_id = _extract_user_id_from_hub(hub, ws)
        await hub.broadcast(
            "focus_event",
            {
                "event_type": "modal_closed",
                "modal_id": modal_id,
            },
            user_id=user_id,
            force=True,
        )

    except Exception as e:
        logger.exception("Failed to handle modal_closed: %s", e)


async def handle_frontend_diagnostics(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """
    Handle frontend diagnostics received from React/iframe.

    This stores the diagnostics so they can be retrieved via the
    /v1/diagnostics/full-playback-state endpoint.

    Args:
        action: Command action ("frontend_diagnostics")
        payload: Diagnostic data from frontend
        ws: WebSocket that sent the command
        hub: EventHub for broadcasting
    """
    try:
        from diagnostics.playback_stack import store_frontend_diagnostics

        logger.info(
            "Received frontend diagnostics: source=%s, state=%s",
            payload.get("source", "unknown"),
            payload.get("player_state_name", "UNKNOWN"),
        )

        store_frontend_diagnostics(payload)

    except Exception as e:
        logger.exception("Failed to store frontend diagnostics: %s", e)


async def _send_late_join_state(ws: WebSocket, music: Any, state: Any) -> None:
    """Send current playback state to a newly subscribed spoke.

    Sends a snapshot payload with ``videoId``, ``isPlaying``, and
    ``hubPosSec`` so the spoke can reconcile late-join state once it is
    unlocked and embed-ready.
    """
    if music is None:
        return

    try:
        player_state = music.state()

        is_playing = False
        if hasattr(player_state, "is_playing"):
            is_playing = player_state.is_playing
        elif isinstance(player_state, dict):
            is_playing = player_state.get("is_playing", False)

        # Extract track URL (video_id preferred for YouTube spokes)
        track_url = None
        position = 0
        now_playing = None

        if hasattr(player_state, "now_playing"):
            now_playing = player_state.now_playing
        elif isinstance(player_state, dict):
            now_playing = player_state.get("now_playing")

        if now_playing is not None:
            if hasattr(now_playing, "video_id"):
                track_url = now_playing.video_id or getattr(now_playing, "url", None)
            elif isinstance(now_playing, dict):
                track_url = now_playing.get("video_id") or now_playing.get("url")

        if hasattr(player_state, "position"):
            position = player_state.position if hasattr(player_state, "position") else 0
        elif isinstance(player_state, dict):
            position = player_state.get("position", 0)
        try:
            position = float(position) if position is not None else 0.0
        except (TypeError, ValueError):
            position = 0.0
        if not math.isfinite(position) or position < 0:
            position = 0.0

        if not track_url:
            logger.debug("Late-join: hub is playing but no track_url available")
            return

        snapshot_payload: dict[str, Any] = {
            "videoId": track_url,
            "track_url": track_url,
            "isPlaying": bool(is_playing),
            "hubPosSec": position,
        }
        from ui.websocket.event_hub import _send_json_safe

        await _send_json_safe(ws, {"type": "room_playback_snapshot", "payload": snapshot_payload})

        payload: dict[str, Any] = {
            "command": "play",
            "args": {
                "track_url": track_url,
                "position": position,
            },
        }
        if is_playing:
            await _send_json_safe(ws, {"type": "room_playback_command", "payload": payload})
        logger.info(
            "Late-join: sent playback snapshot to spoke (track=%s, playing=%s, position=%s)",
            track_url,
            is_playing,
            position,
        )

    except Exception as exc:
        logger.warning("Late-join state sync failed: %s", exc)


async def handle_subscribe_room(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
    music: Any = None,
    state: Any = None,
) -> None:
    """
    Handle room subscription from spoke-mode browser clients.

    The spoke client sends this immediately after WebSocket connect to
    subscribe to room-scoped broadcasts (room_playback_command, etc.).
    After subscribing, if the hub is currently playing something, sends
    the current playback state so the spoke can catch up (late-join).

    Args:
        action: Command action ("subscribe_room")
        payload: Must contain "room_id"
        ws: WebSocket that sent the command
        hub: EventHub for room subscription
        music: Music player adapter (for late-join state)
        state: App state (for late-join state)
    """
    room_id = payload.get("room_id")
    if room_id:
        await hub.subscribe_to_room(ws, room_id)
        logger.info("WebSocket client subscribed to room: %s", room_id)

        # Late-join: send current playback state so spoke can catch up
        await _send_late_join_state(ws, music, state)


async def handle_display_priority(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """Handle display priority override from voice commands.

    Broadcasts a display_priority_override message so the React frontend
    can switch between ``now_playing`` and ``agentic_task`` display modes
    while both activities are running.

    Payload:
        mode: "now_playing" | "agentic_task" | "auto"
              "auto" clears the override and returns to auto-detection.
    """
    mode = payload.get("mode", "auto")
    if mode not in ("now_playing", "agentic_task", "auto"):
        logger.warning("Invalid display_priority mode: %s", mode)
        return

    logger.info("Display priority override: %s", mode)
    user_id = _extract_user_id_from_hub(hub, ws)
    await hub.broadcast(
        "display_priority_override",
        {"mode": mode},
        user_id=user_id,
        force=True,
    )


def _extract_session_id_from_payload(payload: dict[str, Any]) -> str | None:
    """Return a payload-supplied session/task id, if the client sent one."""
    raw = payload.get("session_id") or payload.get("task_id") or payload.get("agent_id")
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    return raw or None


async def handle_agent_cancel(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> bool:
    """Handle cancel request for the active agent task.

    Multi-tenant: cancellation MUST resolve the target executor through the
    authenticated websocket user — never through a process-wide
    last-active fallback.  Without that, one tenant's cancel could stop
    another tenant's task.
    """
    user_id = _extract_user_id_from_hub(hub, ws)
    if not user_id:
        logger.warning("WS agent_cancel: ignoring request from unauthenticated socket")
        return False
    session_id = _extract_session_id_from_payload(payload)
    try:
        from intent.agent_executor import get_active_executor

        executor = get_active_executor(session_id=session_id, user_id=user_id)
        if executor is not None:
            executor.cancel()
            logger.info("WS agent_cancel: cancellation requested for user=%s", user_id)
            return True
        else:
            logger.info("WS agent_cancel: no active agent executor for user=%s", user_id)
    except ImportError:
        logger.debug("WS agent_cancel: agent_executor not available")
    return False


async def handle_agent_takeover(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """Pause the active agent so the user can interact with its browser."""
    user_id = _extract_user_id_from_hub(hub, ws)
    if not user_id:
        logger.warning("WS agent_takeover: ignoring request from unauthenticated socket")
        return
    session_id = _extract_session_id_from_payload(payload)
    try:
        from intent.agent_executor import get_active_executor

        executor = get_active_executor(session_id=session_id, user_id=user_id)
        if executor is not None:
            executor.request_user_takeover()
            logger.info("WS agent_takeover: takeover requested for user=%s", user_id)
        else:
            logger.info("WS agent_takeover: no active agent executor for user=%s", user_id)
    except ImportError:
        logger.debug("WS agent_takeover: agent_executor not available")


async def handle_agent_continue(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """Resume the active agent after a browser takeover."""
    user_id = _extract_user_id_from_hub(hub, ws)
    if not user_id:
        logger.warning("WS agent_continue: ignoring request from unauthenticated socket")
        return
    session_id = _extract_session_id_from_payload(payload)
    try:
        from intent.agent_executor import get_active_executor

        executor = get_active_executor(session_id=session_id, user_id=user_id)
        if executor is not None:
            executor.continue_from_user_takeover()
            logger.info("WS agent_continue: takeover released for user=%s", user_id)
        else:
            logger.info("WS agent_continue: no active agent executor for user=%s", user_id)
    except ImportError:
        logger.debug("WS agent_continue: agent_executor not available")


async def handle_agent_browser_input(
    action: str,
    payload: dict[str, Any],
    ws: WebSocket,
    *,
    hub: EventHub,
) -> None:
    """Forward spoke/cloud browser input to the visible CDP browser."""
    try:
        from mcp_servers.browser_cdp.server import dispatch_input_event

        result = await dispatch_input_event(payload or {})
        if not result.get("ok"):
            logger.warning("WS agent_browser_input failed: %s", result.get("error", "unknown"))
    except Exception:
        logger.exception("WS agent_browser_input failed")


def register_command_handlers(
    hub: EventHub,
    music: Any,
    state: Any,
) -> None:
    """
    Register all command handlers with the EventHub.

    This wires up the handlers so they are called when commands arrive
    via WebSocket.

    Args:
        hub: EventHub to register handlers with
        music: Music player adapter
        state: App state
    """
    # Register seek handler
    hub.register_command_handler(
        "seek",
        lambda a, p, ws: handle_seek(a, p, ws, hub=hub),
    )

    # Register position update handler
    hub.register_command_handler(
        "position_update",
        lambda a, p, ws: handle_position_update(a, p, ws, hub=hub, music=music, state=state),
    )

    # Register video bounds handler (for Qt overlay positioning)
    hub.register_command_handler(
        "video_bounds",
        lambda a, p, ws: handle_video_bounds(a, p, ws, hub=hub),
    )

    # Register YouTube state handler (Phase 4: unified WebSocket communication)
    hub.register_command_handler(
        "youtube_state",
        lambda a, p, ws: handle_youtube_state(a, p, ws, hub=hub, music=music, state=state),
    )

    # Register focus coordination handlers (Phase 6: React ↔ Qt focus)
    hub.register_command_handler(
        "modal_opened",
        lambda a, p, ws: handle_modal_opened(a, p, ws, hub=hub),
    )

    hub.register_command_handler(
        "modal_closed",
        lambda a, p, ws: handle_modal_closed(a, p, ws, hub=hub),
    )

    # Register frontend diagnostics handler (for playback state debugging)
    hub.register_command_handler(
        "frontend_diagnostics",
        lambda a, p, ws: handle_frontend_diagnostics(a, p, ws, hub=hub),
    )

    # Register spoke-mode handlers (multi-room playback)
    hub.register_command_handler(
        "subscribe_room",
        lambda a, p, ws: handle_subscribe_room(a, p, ws, hub=hub, music=music, state=state),
    )
    # Register agent cancel handler
    hub.register_command_handler(
        "agent_cancel",
        lambda a, p, ws: handle_agent_cancel(a, p, ws, hub=hub),
    )

    hub.register_command_handler(
        "agent_takeover",
        lambda a, p, ws: handle_agent_takeover(a, p, ws, hub=hub),
    )

    hub.register_command_handler(
        "agent_continue",
        lambda a, p, ws: handle_agent_continue(a, p, ws, hub=hub),
    )

    hub.register_command_handler(
        "agent_browser_input",
        lambda a, p, ws: handle_agent_browser_input(a, p, ws, hub=hub),
    )

    # Register display priority override handler
    hub.register_command_handler(
        "display_priority",
        lambda a, p, ws: handle_display_priority(a, p, ws, hub=hub),
    )

    logger.info("WebSocket command handlers registered")
