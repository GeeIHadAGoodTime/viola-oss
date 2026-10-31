from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, TypedDict, cast

from contracts.api_response import failure_response, success_response
from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger
from diagnostics.failure_envelope import emit_failure
from music.providers.errors import MusicTrackNotFoundError
from music.runtime import PlayerControlSurface

if TYPE_CHECKING:
    from models.player import PlayerState, QueueItem

logger = get_logger(__name__)


# Response extra value types used by adapter envelopes.
ExtraValue = str | int | bool | dict[str, object] | list[object] | None


# Type definitions for adapter responses
class QueueItemDict(TypedDict, total=False):
    """Queue item as dictionary (simplified structure)."""

    id: str
    title: str
    artist: str | None
    album: str | None
    duration: int | None
    url: str | None
    thumbnail: str | None
    provider: str | None
    playback_mode: str | None


class StatusPayload(TypedDict, total=False):
    """Player status snapshot payload."""

    queue: list[QueueItemDict]
    current: QueueItemDict | None
    is_playing: bool
    position: float | None
    volume: int | None


class ResponseData(TypedDict, total=False):
    """Generic response data payload."""

    reason: str
    query: str
    source: str | None
    value: int
    seconds: int
    delta: int


class PlayResponse(TypedDict):
    """Response payload for play operation."""

    ok: bool
    data: ResponseData


class ErrorResponse(TypedDict):
    """Error response payload."""

    ok: bool
    error: str
    data: ResponseData


class StateDict(TypedDict, total=False):
    """Player state as dictionary."""

    is_playing: bool
    now_playing: QueueItemDict | None
    queue: list[QueueItemDict]
    volume: int
    position: int
    duration: int
    backend: str
    playback_mode: str | None


class MusicControllerAdapter:
    """
    Thin, defensive wrapper around ``MusicPlayer`` to provide a stable dict API.

    This adapter is the single canonical implementation shared by both backend
    services and UI clients. It normalises method names, return payloads, and
    error handling so higher-level code can rely on a consistent surface.
    """

    def __init__(self, player: PlayerControlSurface) -> None:
        self.player = player
        self._control = getattr(player, "control_surface", None)

    @property
    def autoplay(self) -> object | None:
        """Expose the autoplay controller from the underlying player."""
        return getattr(self.player, "autoplay", None)

    def set_hub_state_authority(self, hub_authority: object) -> None:
        """
        Forward hub state authority to the underlying player.

        This allows the music player to push authoritative state changes
        (like interrupt-based play) directly to the hub, bypassing conflict detection.

        Args:
            hub_authority: Hub state authority object (type varies by implementation)
        """
        if hasattr(self.player, "set_hub_state_authority"):
            self.player.set_hub_state_authority(hub_authority)
            logger.debug("Hub state authority forwarded to player")

    # --------------------------------------------------------------------- #
    # Internal helpers                                                      #
    # --------------------------------------------------------------------- #
    def _targets(self) -> list[object]:
        """Return control-surface target list (control first)."""

        candidates: list[object] = []
        if self._control is not None:
            candidates.append(self._control)
        candidates.append(self.player)

        deduped: list[object] = []
        seen: set[int] = set()
        for target in candidates:
            if target is None:
                continue
            ident = id(target)
            if ident in seen:
                continue
            deduped.append(target)
            seen.add(ident)
        return deduped

    def _call(self, names: list[str], *args: object, **kwargs: object) -> object:
        """
        Call the first available method from ``names`` on the wrapped player.

        Security note: ``names`` MUST be hard-coded. Never pass user input.
        """
        for target in self._targets():
            for name in names:
                attr = getattr(target, name, None)
                if callable(attr):
                    return attr(*args, **kwargs)
        raise AttributeError(f"Music player missing expected methods: {names}")

    @staticmethod
    def _success(
        intent: str,
        **extra: ExtraValue,
    ) -> dict[str, object]:
        """Create success response with intent and extra data."""
        payload: dict[str, object] = {"intent": intent}
        payload.update(extra)
        envelope = success_response(to_json_value(payload))
        # ResponseEnvelope is a TypedDict; return a plain dict for legacy callers.
        return dict(envelope)

    @staticmethod
    def _failure(
        intent: str,
        exc: Exception,
        **extra: ExtraValue,
    ) -> dict[str, object]:
        """Create failure response with intent, exception, and extra context."""
        payload: dict[str, object] = {"intent": intent}
        payload.update(extra)
        message = str(exc) or f"{intent} failed"

        provider_name = extra.get("provider_name") or getattr(exc, "provider_name", None)
        error_code = getattr(exc, "error_code", None)

        # Emit structured failure envelope for diagnostics/observability.
        # Avoid `**extra` expansion here; mypy must type-check kwargs against
        # the fixed signature before they reach `**context`.
        emit_failure(
            f"{intent}_failed",
            "backend.music_adapter",
            message=message,
            exc=exc,
            exception_type=type(exc).__name__,
            tier="backend",
            provider_name=str(provider_name) if provider_name else None,
            error_code=str(error_code) if error_code else None,
        )

        envelope = failure_response(
            f"{intent}_failed",
            message,
            data=to_json_value(payload),
            details={"exception_type": type(exc).__name__},
        )
        # ResponseEnvelope is a TypedDict; return a plain dict for legacy callers.
        return dict(envelope)

    @staticmethod
    def _expected_failure(
        intent: str,
        exc: Exception,
        **extra: ExtraValue,
    ) -> dict[str, object]:
        """The same failure envelope as ``_failure``, without a diagnostics event.

        For an ordinary outcome the caller handles (a search hit YouTube will
        not embed), not a fault: ``emit_failure`` logs at ERROR with the
        traceback, which made each unplayable candidate look like a crash in
        the 1.0.5 build-7 log.
        """
        payload: dict[str, object] = {"intent": intent}
        payload.update(extra)
        envelope = failure_response(
            f"{intent}_failed",
            str(exc) or f"{intent} failed",
            data=to_json_value(payload),
            details={"exception_type": type(exc).__name__},
        )
        return dict(envelope)

    def _status_payload(self) -> dict[str, ExtraValue]:
        """Get status payload for response inclusion."""
        status = self.status()
        if not status:
            return {}
        status_dict = cast(dict[str, object], dict(status))
        return {"status": status_dict}

    def _check_browser_mode_unsupported(self, operation: str) -> dict[str, ExtraValue] | None:
        """
        Check if browser mode is active and return unsupported error if so.

        Args:
            operation: Operation name (e.g., "pause", "next", "seek", "volume control")

        Returns:
            Error response dict if browser mode is active, None otherwise
        """
        state = self.state()
        playback_mode = state.get("playback_mode")
        if not playback_mode:
            now_playing = state.get("now_playing")
            if now_playing and isinstance(now_playing, dict):
                playback_mode = now_playing.get("playback_mode")

        if playback_mode == "external_browser":
            # Return legacy-style error shape expected by certain tests
            return {
                "ok": False,
                "error": "unsupported",
                "data": {"reason": "Browser-mode YTM does not support pause/next/seek yet."},
            }
        return None

    # --------------------------------------------------------------------- #
    # Playback controls                                                     #
    # --------------------------------------------------------------------- #
    def play(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        interrupt: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """
        Play a track by query string.

        Args:
            query: Search query, URL, or track identifier
            source: Optional source type (e.g., "ytsearch1", "url", "local").
                   If None, the player will infer from the query.
            emit: Whether to emit state change events (default: True)
            interrupt: If True (default), interrupt current playback and play immediately.
                      If False, append to queue without interrupting.

        Returns:
            Dict with success status and player state

        Note:
            This method signature matches the underlying MusicPlayer.play() to allow
            direct positional argument passing from intent interpreters.
        """
        kwargs: dict[str, object] = {"emit": emit, "interrupt": interrupt}
        if metadata is not None:
            kwargs["metadata"] = metadata
        # source is passed positionally to match MusicPlayer.play() signature

        try:
            try:
                result = self._call(
                    ["play", "play_query", "enqueue_and_play"],
                    query,
                    source,
                    **kwargs,
                )
            except TypeError as te:
                # Fallback for legacy players without emit/source/interrupt support.
                logger.debug(
                    "play() method signature mismatch, retrying without emit/interrupt: %s",
                    te,
                )
                kwargs.pop("emit", None)
                kwargs.pop("interrupt", None)
                result = self._call(
                    ["play", "play_query", "enqueue_and_play"],
                    query,
                    source,
                    **kwargs,
                )
            # Inspect the underlying result instead of discarding it - mirrors
            # play_async's handling below. The sync player normally raises on
            # failure rather than returning an ok:false dict, but when it
            # does hand back a failure envelope, this must not be re-wrapped
            # as success: the defense-in-depth `result.get("ok") is False`
            # gate in backend/intent_bridge/dispatch.py's _handle_play was
            # dead for this sync path otherwise (#2757).
            if isinstance(result, dict) and result.get("ok") is False:
                logger.warning(
                    "Play operation returned failure envelope: %s",
                    result.get("error"),
                )
                return result
            payload_extra: dict[str, ExtraValue] = {"query": query}
            if source:
                payload_extra["source"] = source
            if isinstance(result, dict):
                payload_extra["now_playing"] = result
            elif hasattr(result, "model_dump"):
                payload_extra["now_playing"] = result.model_dump()
            payload_extra.update(self._status_payload())
            return self._success("play", **payload_extra)
        except Exception as exc:
            logger.exception("Play operation failed: %s", exc)
            return self._failure("play", exc, query=query)

    async def play_async(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        interrupt: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """
        Async version of play() for non-blocking operation.

        Args:
            query: Search query, URL, or track identifier
            source: Optional source type (e.g., "ytsearch1", "url", "local")
            emit: Whether to emit state change events (default: True)
            interrupt: If True (default), interrupt current playback and play immediately.
                      If False, append to queue without interrupting.

        Returns:
            Dict with success status and player state
        """
        kwargs: dict[str, object] = {"emit": emit, "interrupt": interrupt}
        if metadata is not None:
            kwargs["metadata"] = metadata
        logger.info(
            "UI: play_async called - query=%s, source=%s, interrupt=%s",
            query[:50] if query else None,
            source,
            interrupt,
        )

        queue_item = None
        requested_at = time.monotonic()
        try:
            if hasattr(self.player, "play_async"):
                logger.info("UI: Calling player.play_async")
                try:
                    result = await self.player.play_async(query, source, **kwargs)
                    logger.info("UI: player.play_async returned: %s", type(result).__name__)
                    # Capture the QueueItem if returned
                    if hasattr(result, "model_dump"):
                        queue_item = result.model_dump()
                    elif isinstance(result, dict):
                        queue_item = result
                except TypeError as te:
                    logger.warning(
                        "UI: play_async got TypeError, retrying without emit/interrupt: %s",
                        te,
                    )
                    kwargs.pop("emit", None)
                    kwargs.pop("interrupt", None)
                    result = await self.player.play_async(query, source, **kwargs)
                    logger.info(
                        "UI: player.play_async (retry) returned: %s",
                        type(result).__name__,
                    )
                    if hasattr(result, "model_dump"):
                        queue_item = result.model_dump()
                    elif isinstance(result, dict):
                        queue_item = result
            else:
                loop_result = await asyncio.to_thread(
                    self.play,
                    query,
                    source,
                    emit=emit,
                    interrupt=interrupt,
                    metadata=metadata,
                )
                if not loop_result.get("ok", False):
                    return loop_result
            if isinstance(queue_item, dict) and queue_item.get("ok") is False:
                # The player handed back a failure envelope — never re-wrap
                # it as success (lane-3 MF-A false-success chain).
                logger.warning(
                    "UI: play_async player returned failure envelope: %s",
                    queue_item.get("error"),
                )
                return queue_item
            playback_confirmed = await self._confirm_iframe_playback(
                requested_at,
                queue_item,
                interrupt=interrupt,
            )
            payload_extra: dict[str, ExtraValue] = {"query": query}
            if source:
                payload_extra["source"] = source
            if queue_item:
                payload_extra["now_playing"] = queue_item
            if playback_confirmed is not None:
                payload_extra["playback_confirmed"] = playback_confirmed
            payload_extra.update(self._status_payload())
            logger.info(
                "UI: play_async success, returning payload with now_playing=%s playback_confirmed=%s",
                bool(queue_item),
                playback_confirmed,
            )
            return self._success("play", **payload_extra)
        except MusicTrackNotFoundError as exc:
            # An ordinary "this candidate cannot be played" answer (deleted,
            # private or non-embeddable video). The caller falls through to
            # the next search result; a traceback at ERROR here made one
            # unplayable search hit look like a crash (1.0.5 build 7).
            logger.warning("UI: play_async found nothing playable for query=%s: %s", query[:50] if query else None, exc)
            return self._expected_failure("play", exc, query=query)
        except Exception as exc:
            logger.exception("UI: play_async failed with exception: %s", exc)
            return self._failure("play", exc, query=query)

    async def _confirm_iframe_playback(
        self,
        requested_at: float,
        queue_item: object,
        *,
        interrupt: bool,
    ) -> bool | None:
        """Wait for the React iframe to confirm a play request actually plays.

        Returns True when the iframe reported PLAYING for the requested video,
        False when it did not within the confirmation window, and None when
        there is nothing to confirm (a queued item, a non-iframe track, test
        mode, or a newer request took over).

        1.0.5 build 7, session 4: "play Drake" picked the video that was
        already loaded and paused. The backend marked it playing and Viola
        said so, but the iframe never got a command and stayed paused for 10+
        minutes. A play request for an iframe track now waits for the
        iframe's own PLAYING report, re-sends ``play`` once if it is slow,
        and otherwise puts the player back to what the iframe last reported,
        so /v1/player/state (which the media tool reads to verify playback)
        no longer says playing.
        """
        if not interrupt or not isinstance(queue_item, dict):
            return None
        video_id = queue_item.get("video_id")
        if not isinstance(video_id, str) or not video_id:
            return None
        player = self.player
        if player is None or getattr(player, "_test_mode", False):
            return None
        try:
            from music.runtime.embedded_playback import react_iframe_owns_playback
            from music.runtime.iframe_playback_report import (
                CONFIRMED,
                UNCONFIRMED,
                await_iframe_playback,
            )
        except ImportError:
            return None
        try:
            now_playing = getattr(getattr(player, "_state", None), "now_playing", None)
            if getattr(now_playing, "video_id", None) != video_id or not react_iframe_owns_playback(player):
                return None
        except Exception:
            logger.debug("PLAY_CONFIRM: ownership check failed", exc_info=True)
            return None

        try:
            from core.user_context import get_current_user_id

            user_id: str | None = get_current_user_id()
        except (ImportError, LookupError):
            user_id = None

        async def _resend_play() -> None:
            from ui.websocket.event_hub import broadcast_transport_command

            logger.warning("PLAY_CONFIRM: player has not reported PLAYING for %s yet; re-sending play", video_id)
            await broadcast_transport_command("play", {"source": "play_confirm"}, user_id=user_id)

        verdict = await await_iframe_playback(
            player,
            since=requested_at,
            video_id=video_id,
            nudge=_resend_play,
        )
        if verdict == CONFIRMED:
            logger.info("PLAY_CONFIRM: player reported PLAYING for %s", video_id)
            return True
        if verdict != UNCONFIRMED:
            return None
        self._mark_iframe_not_playing(player, video_id)
        return False

    @staticmethod
    def _mark_iframe_not_playing(player: object, video_id: str) -> None:
        """Make the reported state match an iframe that never started."""
        from music.player.playback_state import PlaybackPhase

        cv = getattr(player, "_cv", None)
        if cv is None:
            return
        with cv:
            now_playing = getattr(getattr(player, "_state", None), "now_playing", None)
            sm = getattr(player, "_playback_sm", None)
            if getattr(now_playing, "video_id", None) != video_id or sm is None:
                return
            if sm.phase != PlaybackPhase.PLAYING:
                return
            reported = getattr(player, "_iframe_state", None)
            reported_video = getattr(player, "_iframe_state_video_id", None)
            target = (
                PlaybackPhase.PAUSED
                if reported == "PAUSED" and reported_video in (None, video_id)
                else PlaybackPhase.LOADING
            )
            sm.transition(target, force=True)
            state = getattr(player, "_state", None)
            if state is not None:
                state.is_playing = False
            cv.notify_all()
        logger.warning(
            "PLAY_CONFIRM: player never reported PLAYING for %s; reporting %s instead of playing",
            video_id,
            target.name,
        )
        emit = getattr(player, "_emit", None)
        if callable(emit):
            try:
                emit()
            except Exception:
                logger.debug("PLAY_CONFIRM: state emit failed", exc_info=True)

    def pause(self) -> dict[str, object]:
        try:
            browser_error = self._check_browser_mode_unsupported("pause")
            if browser_error:
                return cast(dict[str, object], browser_error)
            self._call(["pause"])
            return self._success("pause", **self._status_payload())
        except Exception as exc:
            logger.exception("Pause operation failed: %s", exc)
            return self._failure("pause", exc)

    def resume(self) -> dict[str, object]:
        try:
            self._call(["resume"])
            return self._success("resume", **self._status_payload())
        except Exception as exc:
            logger.exception("Resume operation failed: %s", exc)
            return self._failure("resume", exc)

    def stop(self) -> dict[str, object]:
        try:
            self._call(["stop"])
            return self._success("stop", **self._status_payload())
        except Exception as exc:
            logger.exception("Stop operation failed: %s", exc)
            return self._failure("stop", exc)

    def next(self) -> dict[str, object]:
        try:
            browser_error = self._check_browser_mode_unsupported("next")
            if browser_error:
                return cast(dict[str, object], browser_error)
            self._call(["next", "skip", "next_track"])
            return self._success("next", **self._status_payload())
        except Exception as exc:
            logger.exception("Next operation failed: %s", exc)
            return self._failure("next", exc)

    def skip(self) -> dict[str, object]:
        try:
            self._call(["skip", "next", "next_track"])
            return self._success("skip", **self._status_payload())
        except Exception as exc:
            logger.exception("Skip operation failed: %s", exc)
            return self._failure("skip", exc)

    def previous(self) -> dict[str, object]:
        try:
            self._call(["previous", "prev", "previous_track"])
            return self._success("previous", **self._status_payload())
        except Exception as exc:
            logger.exception("Previous operation failed: %s", exc)
            return self._failure("previous", exc)

    def set_volume(self, value: int) -> dict[str, object]:
        try:
            logger.info(
                "ADAPTER_SET_VOLUME: value=%s player_type=%s has_set_volume=%s",
                value,
                type(self.player).__name__,
                hasattr(self.player, "set_volume"),
            )
            browser_error = self._check_browser_mode_unsupported("volume control")
            if browser_error:
                logger.warning("ADAPTER_SET_VOLUME: blocked by browser mode")
                return cast(dict[str, object], browser_error)
            clamped = max(0, min(100, int(value)))
            if hasattr(self.player, "set_volume"):
                logger.info("ADAPTER_SET_VOLUME: calling player.set_volume(%s)", clamped)
                self.player.set_volume(clamped)
                logger.info("ADAPTER_SET_VOLUME: player.set_volume returned")
            elif hasattr(self.player, "volume"):
                self.player.volume = clamped
            else:
                raise AttributeError("No set_volume capability")
            payload: dict[str, ExtraValue] = {"value": clamped}
            payload.update(self._status_payload())
            logger.info("ADAPTER_SET_VOLUME: success, fields=%s", sorted(payload.keys()))
            return self._success("set_volume", **payload)
        except Exception as exc:
            logger.exception("Set volume operation failed: %s", exc)
            return self._failure("set_volume", exc, value=value)

    def change_volume(self, delta: int) -> dict[str, object]:
        try:
            current = getattr(self.player, "volume", 50)
            return self.set_volume(int(current) + int(delta))
        except Exception as exc:
            logger.exception("Change volume operation failed: %s", exc)
            return self._failure("change_volume", exc, delta=delta)

    def seek(self, seconds: int) -> dict[str, object]:
        try:
            browser_error = self._check_browser_mode_unsupported("seek")
            if browser_error:
                return cast(dict[str, object], browser_error)
            value = int(seconds)
            self._call(["seek", "seek_seconds", "jump"], value)
            payload: dict[str, ExtraValue] = {"seconds": value}
            payload.update(self._status_payload())
            return self._success("seek", **payload)
        except Exception as exc:
            logger.exception("Seek operation failed: %s", exc)
            return self._failure("seek", exc, seconds=seconds)

    def emit_state_change(self) -> None:
        """
        Manually trigger a state change broadcast on the underlying player.

        This is best-effort; errors are logged but not raised to callers.
        """
        try:
            for target in self._targets():
                emitter = getattr(target, "emit_state_change", None)
                if callable(emitter):
                    emitter()
                    return
            for target in self._targets():
                fallback = getattr(target, "_emit", None)
                if callable(fallback):
                    fallback()
                    return
        except Exception as exc:
            logger.warning("Failed to emit state change: %s", exc)

    # --------------------------------------------------------------------- #
    # Queue management                                                      #
    # --------------------------------------------------------------------- #
    def clear_queue(self) -> None:
        self._call(["clear_queue"])

    def remove_from_queue(self, item_id: str) -> None:
        self._call(["remove_from_queue"], item_id)

    def reorder_queue(self, from_index: int, to_index: int) -> None:
        self._call(["reorder_queue"], from_index, to_index)

    def play_item_now(self, item_id: str) -> None:
        self._call(["play_item_now"], item_id)

    def enqueue(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Enqueue a track without interrupting current playback."""
        kwargs: dict[str, object] = {"emit": emit}
        if metadata is not None:
            kwargs["metadata"] = metadata
        try:
            result = self._call(["enqueue"], query, source, **kwargs)
            payload_extra: dict[str, ExtraValue] = {"query": query}
            if source:
                payload_extra["source"] = source
            return self._success("enqueue", **payload_extra)
        except Exception as exc:
            logger.exception("Enqueue operation failed: %s", exc)
            return self._failure("enqueue", exc, query=query)

    async def enqueue_async(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Async version of enqueue() for non-blocking operation."""
        kwargs: dict[str, object] = {"emit": emit}
        if metadata is not None:
            kwargs["metadata"] = metadata
        try:
            enqueue_fn = getattr(self.player, "enqueue_async", None)
            if callable(enqueue_fn):
                result = await enqueue_fn(query, source, **kwargs)
            else:
                result = await asyncio.to_thread(
                    self.enqueue,
                    query,
                    source,
                    emit=emit,
                    metadata=metadata,
                )
                return result  # already wrapped by sync enqueue
            payload_extra: dict[str, ExtraValue] = {"query": query}
            if source:
                payload_extra["source"] = source
            return self._success("enqueue", **payload_extra)
        except Exception as exc:
            logger.exception("Enqueue async operation failed: %s", exc)
            return self._failure("enqueue", exc, query=query)

    # --------------------------------------------------------------------- #
    # Status helpers                                                        #
    # --------------------------------------------------------------------- #
    def status(self) -> StatusPayload:
        """Return a status snapshot compatible with existing API responses."""

        def _get(name: str, default: object | None = None) -> object | None:
            """Get attribute from first available target."""
            for target in self._targets():
                value = getattr(target, name, None)
                if value is not None:
                    return value
            return default

        queue_raw = _get("queue", [])
        current_raw = _get("current", None) or _get("current_track", None)
        is_playing = bool(_get("is_playing", False))
        position = _get("position", None)
        volume = _get("volume", None)

        # Ensure queue is a list of dicts
        queue: list[QueueItemDict] = []
        if isinstance(queue_raw, list):
            # Filter and cast items to QueueItemDict (they're already dicts)
            queue = [cast(QueueItemDict, item) for item in queue_raw if isinstance(item, dict)]

        # Ensure current is a dict or None
        current: QueueItemDict | None = None
        if isinstance(current_raw, dict):
            current = cast(QueueItemDict, current_raw)

        position_value: float | None = None
        if isinstance(position, (int, float)) and not isinstance(position, bool):
            position_value = float(position)

        return StatusPayload(
            queue=queue,
            current=current,
            is_playing=is_playing,
            position=position_value,
            volume=volume if isinstance(volume, int) else None,
        )

    def state(self) -> JsonDict:
        """
        Return the unified player state.

        If the wrapped player exposes a ``state()`` method, use that. Otherwise
        fall back to the status snapshot.

        Returns:
            State dictionary (usually StateDict-compatible, but may have extra keys)
        """
        for target in self._targets():
            state_callable = getattr(target, "state", None)
            if not callable(state_callable):
                continue
            player_state = state_callable()

            if hasattr(player_state, "model_dump"):
                dumped = player_state.model_dump()
                payload_value = to_json_value(dumped)
                if isinstance(payload_value, dict):
                    return payload_value
                return {"value": payload_value}

            if isinstance(player_state, dict):
                payload_value = to_json_value(player_state)
                if isinstance(payload_value, dict):
                    return payload_value
                return {"value": payload_value}

            if hasattr(player_state, "__dict__"):
                payload_value = to_json_value(dict(vars(player_state)))
                if isinstance(payload_value, dict):
                    return payload_value
                return {"value": payload_value}

            # Edge case: non-dict state, wrap in dict
            payload_value = to_json_value(player_state)
            return {"value": payload_value}

        # Fallback to status snapshot (converted to StateDict-like structure)
        status = self.status()
        state_dict: dict[str, object] = {
            "is_playing": status.get("is_playing", False),
            "now_playing": status.get("current"),
            "queue": status.get("queue", []),
            "volume": status.get("volume", 50),
            "position": status.get("position", 0),
        }
        payload_value = to_json_value(state_dict)
        return payload_value if isinstance(payload_value, dict) else {"value": payload_value}


__all__ = ["MusicControllerAdapter"]
