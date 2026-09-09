from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from diagnostics.operation_trace import OperationType, record_operation
from models.player import PlayerState, QueueItem
from music.backends.base import BaseBackend
from music.exceptions import ConfigurationError, InvalidOperation
from music.player.playback_state import PlaybackPhase
from music.providers.checker import (
    get_youtube_unavailable_reason,
    is_provider_linked,
    is_youtube_track_requiring_provider,
)
from music.resolution.provider_router import Source

from .autoplay_service import AutoplayService
from .backend_manager import BackendLifecycleManager
from .contracts import PlayerControlSurface, QueueEngine, StateService
from .play_command import PlayCommandService
from .queue_command import QueueCommandService


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return


class PlayerControlService(PlayerControlSurface):
    """
    Centralised control surface for MusicPlayer public APIs.

    This service owns high-level queue and playback entry points so callers can
    depend on a stable interface while the legacy MusicPlayer is decomposed.
    """

    on_state_change: Callable[[PlayerState], None] | None = None

    def __init__(
        self,
        *,
        owner: MusicPlayer,
        play_command: PlayCommandService,
        queue_command: QueueCommandService,
        autoplay_service: AutoplayService,
        backend_manager: BackendLifecycleManager,
        queue_engine: QueueEngine,
        state_service: StateService,
        condition,
        emit_callback: Callable[[], None],
        logger,
        test_mode: bool,
    ) -> None:
        self._player = owner
        self._play_command = play_command
        self._queue_command = queue_command
        self._autoplay_service = autoplay_service
        self._backend_manager = backend_manager
        self._queue_engine = queue_engine
        self._state_service = state_service
        self._cv = condition
        self._emit_callback = emit_callback
        self._logger = logger.getChild("control_service")
        self._test_mode = test_mode

    # ------------------------------------------------------------------ #
    # PlayerControlSurface compliance                                    #
    # ------------------------------------------------------------------ #

    def state(self) -> PlayerState:
        return self._player.state()

    def status(self) -> dict[str, Any]:
        return self._player.status()

    def emit_state_change(self) -> None:
        self._emit_callback()

    @staticmethod
    def _track_result_payload(item: QueueItem | None) -> dict[str, Any] | None:
        if item is None:
            return None
        capabilities = getattr(item, "capabilities", None) or {}
        return {
            "id": getattr(item, "id", None),
            "title": getattr(item, "title", None),
            "artist": getattr(item, "artist", None),
            "provider": getattr(item, "provider", None),
            "video_id": getattr(item, "video_id", None),
            "url": getattr(item, "url", None),
            "playback_mode": getattr(item, "playback_mode", None),
            # Provenance flag: True when `title` is an unverified placeholder
            # (e.g. the browser provider's query-echo before the real track is
            # known) rather than a confirmed track title (#2806). Verified
            # titles carry False, so consumers can trust an unflagged title.
            "title_unverified": bool(capabilities.get("title_unverified")),
        }

    def _play_backend_for_track(self, item: QueueItem | None, backend: object | None) -> None:
        if item is None or backend is None:
            return
        url = getattr(item, "url", None)
        if not isinstance(url, str) or not url:
            return
        play_method = getattr(backend, "play", None) or getattr(backend, "play_url", None)
        if not callable(play_method):
            return
        try:
            play_method(url)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self._logger.warning("Autoplay skip backend play failed: %s", exc)

    def _ensure_autoplay_buffer(
        self,
        anchor: QueueItem | None,
        *,
        start_if_idle: bool = False,
    ) -> dict[str, Any]:
        autoplay = getattr(self._player, "autoplay", None)
        if autoplay is None:
            return {"triggered": False, "added": 0, "available": False}

        added = 0
        try:
            set_anchor = getattr(autoplay, "set_anchor", None)
            if callable(set_anchor):
                set_anchor(anchor)
            ensure_blocking = getattr(autoplay, "ensure_buffer_blocking", None)
            if callable(ensure_blocking):
                added = int(ensure_blocking(current_track=anchor, timeout_seconds=3.5))
            else:
                check_and_run = getattr(autoplay, "check_and_run_async", None)
                if callable(check_and_run):
                    check_and_run()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self._logger.debug("Autoplay buffer refill failed after control command: %s", exc)

        backend_to_play: object | None = None
        track_to_play: QueueItem | None = None
        with self._cv:
            if self._queue_engine.current() is None and self._queue_engine.upcoming():
                self._queue_engine.schedule_current()
            current = self._queue_engine.current()
            state_now = getattr(self._player._state, "now_playing", None)
            if current is not None and state_now is None:
                self._player._set_now_playing_locked(current)
                state_now = current
            if start_if_idle and current is not None and not bool(getattr(self._player._state, "is_playing", False)):
                self._player._state.is_playing = True
                self._player._is_playing = True
                _sm_transition(self._player, PlaybackPhase.PLAYING, user_initiated=True)
                backend_to_play = getattr(self._player, "_backend", None)
                track_to_play = current
            queue_size = self._queue_engine.queue_size()
            now_payload = self._track_result_payload(state_now)
            self._cv.notify_all()

        self._play_backend_for_track(track_to_play, backend_to_play)
        return {
            "triggered": True,
            "available": True,
            "added": added,
            "queue_size": queue_size,
            "now_playing": now_payload,
        }

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def play(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        return self._play_command.play(query, source, emit=emit, metadata=metadata, interrupt=interrupt)

    async def play_async(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        return await asyncio.to_thread(
            self.play,
            query,
            source,
            emit=emit,
            metadata=metadata,
            interrupt=interrupt,
        )

    def enqueue(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> QueueItem | None:
        return self._queue_command.enqueue(
            query,
            source,
            emit=emit,
            metadata=metadata,
        )

    async def enqueue_async(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> QueueItem | None:
        return await self._queue_command.enqueue_async(
            query,
            source,
            emit=emit,
            metadata=metadata,
        )

    def play_next(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> QueueItem | None:
        return self._queue_command.play_next(
            query,
            source,
            emit=emit,
            metadata=metadata,
        )

    def enqueue_autoplay(self, query: str, *, metadata: dict[str, Any] | None = None) -> QueueItem | None:
        return self._autoplay_service.enqueue(query, metadata=metadata)

    def queue(self) -> list[QueueItem]:
        return list(self._queue_engine.upcoming())

    def queue_size(self) -> int:
        with self._cv:
            size = self._queue_engine.queue_size()
            if self._queue_engine.current():
                size += 1
            return size

    def allow_test_stream_playback(self, enabled: bool = True) -> None:
        if not self._test_mode:
            raise RuntimeError("allow_test_stream_playback is only available in test mode")
        self._backend_manager.allow_test_stream_playback(bool(enabled))
        self._logger.debug(
            "Test stream playback override %s",
            ("enabled" if self._backend_manager.test_stream_playback_allowed else "disabled"),
        )

    def use_test_backend(self, backend: BaseBackend) -> None:
        if not self._test_mode:
            raise RuntimeError("use_test_backend is only supported in test mode")
        self._backend_manager.use_test_backend(backend)
        self._logger.info("Test backend override applied: %s", type(backend).__name__)

    def pause(self) -> None:
        """Pause playback.

        Sets state flags inside _cv, then calls backend.pause() OUTSIDE
        the lock to avoid deadlock with CDP poll threads and the monitor loop.
        """
        backend = None
        with self._cv:
            self._player._user_paused = True
            self._player._paused = True
            self._player._is_playing = False
            if hasattr(self._player, "_state") and self._player._state is not None:
                self._player._state.is_playing = False
            _sm_transition(self._player, PlaybackPhase.PAUSED, user_initiated=True)
            backend = self._backend_manager.backend
            self._cv.notify_all()
        # Backend pause OUTSIDE lock — CDP engines may do network I/O
        if backend and hasattr(backend, "pause"):
            try:
                backend.pause()
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                from core.logging_config import get_logger

                get_logger(__name__).warning("Backend pause failed: %r", exc)
        self._emit_callback()
        record_operation(OperationType.PLAYBACK, "pause", success=True)

    def resume(self) -> None:
        """Resume playback.

        Sets state flags inside _cv, then calls backend.resume() OUTSIDE
        the lock to avoid deadlock with CDP poll threads and the monitor loop.
        """
        backend = None
        with self._cv:
            self._player._user_paused = False
            self._player._paused = False
            self._player._is_playing = True
            if hasattr(self._player, "_state") and self._player._state is not None:
                self._player._state.is_playing = True
            _sm_transition(self._player, PlaybackPhase.PLAYING)
            backend = self._backend_manager.backend
            self._cv.notify_all()
        # Backend resume OUTSIDE lock — CDP engines may do network I/O
        if backend and hasattr(backend, "resume"):
            try:
                backend.resume()
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                from core.logging_config import get_logger

                get_logger(__name__).warning("Backend resume failed: %r", exc)
        self._emit_callback()
        record_operation(OperationType.PLAYBACK, "resume", success=True)

    def stop(self) -> None:
        """Stop playback.

        Sets state flags inside _cv, then stops backend OUTSIDE the lock
        to avoid deadlock with CDP poll threads that call player._emit().
        """
        with self._cv:
            self._player._is_playing = False
            _sm_transition(self._player, PlaybackPhase.STOPPED)
            self._cv.notify_all()
        # Backend stop OUTSIDE lock — SpotifyCDPEngine.stop() joins its
        # poll thread, which may be waiting to acquire player._lock via _emit().
        try:
            self._player._stop_backend_locked()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            from core.logging_config import get_logger

            get_logger(__name__).warning("Backend stop failed: %r", exc)
        self._emit_callback()
        record_operation(OperationType.PLAYBACK, "stop", success=True)

    def _active_engine_provider(self) -> str | None:
        """Provider id of the engine manager's active playback handle, if any.

        Ground truth for "an engine-managed provider (e.g. Spotify CDP) is
        actively playing". Item typing on ``now_playing`` can be stale, wrong
        (mis-typed queue items), or missing entirely (``now_playing`` is None
        after a failed start) while the engine still owns live playback —
        transport routing must not depend on item typing alone (2026-07-02:
        conversational "skip" during Spotify raised "No next track in queue"
        because routing keyed only off ``now_playing``).

        Call WITHOUT ``self._cv`` held: the manager takes its own lock and its
        finish callback can re-enter queue machinery.
        """
        manager = getattr(self._player, "_playback_manager", None) or getattr(self._player, "_engine_manager", None)
        active = getattr(manager, "active_provider", None)
        if not callable(active):
            return None
        try:
            provider = active()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        return str(provider) if provider else None

    def skip(self) -> dict[str, Any]:
        """Skip to next track."""
        track_id = None
        backend_skip = None
        stop_backend = False
        next_track_id = None
        mode = "standard"
        queued_provider_backend: tuple[object | None, QueueItem, list[QueueItem]] | None = None
        autoplay_anchor: QueueItem | None = None
        run_autoplay_refill = False
        start_autoplay_if_idle = False
        engine_provider = self._active_engine_provider()

        with self._cv:
            # Check if we're in embedded playback mode
            # playback_mode can be on state directly or on the now_playing item
            state_playback_mode = getattr(self._player._state, "playback_mode", None)
            now_playing = self._player._state.now_playing
            item_playback_mode = getattr(now_playing, "playback_mode", None) if now_playing else None
            playback_mode = item_playback_mode or state_playback_mode
            provider = str(getattr(now_playing, "provider", "") or "").lower() if now_playing else ""
            video_id = getattr(now_playing, "video_id", None) if now_playing else None
            has_upcoming = bool(self._queue_engine.upcoming())

            track_id = getattr(now_playing, "id", None) if now_playing else None
            autoplay_anchor = now_playing
            mode = playback_mode or "standard"
            is_embedded = (
                playback_mode
                in (
                    "embedded_webview",
                    "embedded_iframe_webview",
                )
                or bool(video_id)
                or provider in {"youtube", "youtube_music", "youtube_iframe"}
            )
            # Provider-native transport is available when the item/state says
            # so OR when the engine manager actually has an active handle —
            # the latter is ground truth and survives stale/missing typing.
            provider_native = (
                playback_mode == "spotify_cdp" or provider in {"spotify", "spotify_cdp"} or engine_provider is not None
            )

            # For embedded mode, handle skip directly instead of via worker loop
            # Worker loop requires playlist.current() which may not be set for embedded
            # Exception: empty local queue + an active engine-managed provider
            # (Spotify CDP) — the provider has its own server-side queue/radio
            # continuation, so delegate to its native next-track transport
            # instead of stopping playback / refusing.
            if is_embedded and now_playing and (has_upcoming or not provider_native):
                play_args = self._skip_embedded_locked()
                if play_args is not None:
                    backend, url = play_args
                    next_track = self._queue_engine.current()
                    next_track_id = getattr(next_track, "id", None) if next_track is not None else None
                    autoplay_anchor = next_track or autoplay_anchor
                    try:
                        backend.play(url)
                        self._logger.info("SKIP_EMBEDDED: Backend play() called for next track")
                    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                        self._logger.warning("SKIP_EMBEDDED: Backend play() failed: %s", exc)
                else:
                    start_autoplay_if_idle = self._queue_engine.current() is None
                run_autoplay_refill = True
            elif provider_native and not has_upcoming:
                backend = self._backend_manager.backend
                for method_name in ("skip", "next", "next_track"):
                    method = getattr(backend, method_name, None) if backend is not None else None
                    if callable(method):
                        backend_skip = method
                        break
                if backend_skip is None:
                    self._logger.warning("Provider-native skip requested but active backend has no skip/next method")
                else:
                    self._logger.info(
                        "PROVIDER_NATIVE_SKIP: empty local queue, delegating to %s next-track transport",
                        engine_provider or provider or playback_mode,
                    )
            else:
                finished, next_track = self._skip_standard_locked()
                track_id = getattr(finished, "id", track_id)
                next_track_id = getattr(next_track, "id", None)
                autoplay_anchor = next_track or finished or autoplay_anchor
                run_autoplay_refill = True
                stop_backend = True
                if provider_native and next_track is not None:
                    backend = self._backend_manager.backend
                    queued_provider_backend = (backend, next_track, list(self._queue_engine.upcoming()))
                    mode = "spotify_cdp"
            self._cv.notify_all()

        if backend_skip is not None:
            try:
                backend_skip()
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self._logger.warning("Provider-native backend skip failed: %s", exc)
            else:
                mode = engine_provider or "spotify_cdp"

        if stop_backend:
            try:
                self._player._stop_backend_locked()
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self._logger.warning("Backend stop during skip failed: %r", exc)
            with self._cv:
                self._cv.notify_all()

        if queued_provider_backend is not None:
            backend, next_track, upcoming = queued_provider_backend
            self._play_queued_provider_backend(backend, next_track, upcoming)

        autoplay_result = {"triggered": False, "added": 0, "available": bool(getattr(self._player, "autoplay", None))}
        if run_autoplay_refill:
            autoplay_result = self._ensure_autoplay_buffer(
                autoplay_anchor,
                start_if_idle=start_autoplay_if_idle,
            )

        with self._cv:
            state_now_playing = getattr(self._player._state, "now_playing", None)
            if next_track_id is None:
                observed_id = getattr(state_now_playing, "id", None) if state_now_playing is not None else None
                if observed_id and observed_id != track_id:
                    next_track_id = observed_id
            now_payload = self._track_result_payload(state_now_playing)
            queue_size = self._queue_engine.queue_size()
            is_playing = bool(getattr(self._player._state, "is_playing", False))

        self._emit_callback()
        record_operation(
            OperationType.PLAYBACK,
            "skip",
            success=True,
            details={"track_id": track_id, "next_track_id": next_track_id, "mode": mode},
        )
        return {
            "ok": True,
            "track_id": track_id,
            "next_track_id": next_track_id,
            "mode": mode,
            "now_playing": now_payload,
            "is_playing": is_playing,
            "queue_size": queue_size,
            "autoplay": autoplay_result,
        }

    def _play_queued_provider_backend(
        self,
        backend: object | None,
        next_track: QueueItem,
        upcoming: list[QueueItem],
    ) -> None:
        url = getattr(next_track, "url", None)
        if not isinstance(url, str) or not url:
            self._logger.warning("Queued provider skip could not start track without URL: %s", next_track)
            return

        selected_backend = None
        engine_manager = getattr(self._player, "_engine_manager", None)
        attach_backend = getattr(engine_manager, "attach_backend", None) if engine_manager is not None else None
        if callable(attach_backend):
            try:
                selected_backend = attach_backend(next_track, upcoming=upcoming, default_backend=None)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self._logger.warning("Queued provider engine backend attach failed: %s", exc)
        if selected_backend is not None:
            try:
                from playback.engine_manager import _set_player_for_emit

                _set_player_for_emit(self._player)
            except (ImportError, AttributeError, RuntimeError, TypeError) as exc:
                self._logger.debug("Set player-for-emit on backend attach failed: %s", exc)
            backend = selected_backend
            with self._cv:
                self._player._backend = selected_backend
                self._player._backend_name = type(selected_backend).__name__
                self._player._state.playback_mode = getattr(next_track, "playback_mode", None) or "spotify_cdp"
                self._backend_manager.configure_backend_state(selected_backend)
                self._player._set_now_playing_locked(next_track)
                self._player._position_ms = 0
                self._player._duration_ms = 0
                self._player._state.position = 0
                if hasattr(self._player._state, "position_ms"):
                    self._player._state.position_ms = 0
                self._player._state.duration = 0
                self._player._state.position_percentage = 0.0
                self._cv.notify_all()

        if backend is None:
            self._logger.warning("Queued provider skip could not start track without backend")
            return

        set_context = getattr(backend, "set_context", None)
        if selected_backend is None and callable(set_context):
            artwork_callback = getattr(getattr(self._player, "_engine_manager", None), "_artwork_callback", None)
            try:
                set_context(next_track, upcoming, artwork_callback=artwork_callback)
            except TypeError:
                set_context(next_track, upcoming)

        play_method = None
        for method_name in ("play_url", "play"):
            method = getattr(backend, method_name, None)
            if callable(method):
                play_method = method
                break
        if play_method is None:
            self._logger.warning("Queued provider skip could not start backend without play/play_url")
            return

        try:
            play_method(url)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self._logger.warning("Queued provider backend play failed: %s", exc)
            return

        with self._cv:
            self._player._is_playing = True
            self._player._state.is_playing = True
            self._player._user_paused = False
            _sm_transition(self._player, PlaybackPhase.PLAYING, user_initiated=True)
            self._cv.notify_all()

    def _skip_standard_locked(self) -> tuple[QueueItem | None, QueueItem]:
        """Advance ordinary queue playback immediately for a user skip command."""
        player = self._player
        current = self._queue_engine.current()
        finished: QueueItem | None = current

        if current is None:
            if not self._queue_engine.upcoming():
                raise InvalidOperation("No next track in queue")
            finished = getattr(player._state, "now_playing", None)
            self._queue_engine.schedule_current()
        else:
            if not self._queue_engine.upcoming():
                raise InvalidOperation("No next track in queue")
            finished = self._queue_engine.complete_current(success=False)

        next_track = self._queue_engine.current()
        if next_track is None:
            raise InvalidOperation("No next track in queue")

        self._append_skip_history_locked(finished)
        player._pending_skip_tokens = 0
        player._user_paused = False
        if hasattr(player, "_paused_current"):
            player._paused_current = None
        player._is_playing = False
        player._state.is_playing = False
        _sm_transition(player, PlaybackPhase.LOADING, user_initiated=True)

        schedule_pending_start = getattr(self._backend_manager, "schedule_pending_start", None)
        if callable(schedule_pending_start):
            schedule_pending_start(getattr(next_track, "id", None))

        promote_current = getattr(player, "_promote_current_locked", None)
        if callable(promote_current):
            promote_current(set_is_playing=False)
        else:
            player._set_now_playing_locked(next_track)

        return finished, next_track

    def _append_skip_history_locked(self, item: QueueItem | None) -> None:
        if item is None:
            return
        state_pipeline = getattr(self._player, "_state_pipeline", None)
        append_history = getattr(state_pipeline, "append_history", None)
        if callable(append_history):
            append_history(item)
            return
        history = getattr(self._player, "_history", None)
        append = getattr(history, "append", None)
        if callable(append):
            append(item)

    def _skip_embedded_locked(self) -> tuple[object, str] | None:
        """Handle skip for embedded playback modes. Must be called with _cv held.

        Returns ``(backend, url)`` if the caller should call ``backend.play(url)``
        while still holding ``_cv``, or ``None`` when no backend call is needed
        (queue empty / autoplay triggered).
        """
        player = self._player
        logger = self._logger

        logger.info("SKIP_EMBEDDED: Processing skip for embedded playback mode")

        # Get current track info before completing
        current = player._state.now_playing
        if current:
            # Add to history
            try:
                player._history.append(current)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning("SKIP_EMBEDDED: Failed to append to history: %r", exc)
            logger.info("SKIP_EMBEDDED: Completed track %s", getattr(current, "id", "unknown"))

        # Complete current in playlist if set. Direct embedded URL playback can
        # have state.now_playing without a playlist cursor, so promote the first
        # upcoming item in that case instead of reporting a no-op skip.
        try:
            if player._playlist.current():
                player._playlist.complete_current(success=True)
            else:
                logger.info("SKIP_EMBEDDED: No playlist current; scheduling first upcoming track")
                player._playlist.schedule_current()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("SKIP_EMBEDDED: Failed to advance playlist current: %r", exc)

        # QUEUE ARCHITECTURE FIX (2026-01-13):
        # complete_current() already advances the queue. After calling it:
        # - playlist.current() is now the NEXT track (what was first in upcoming)
        # - playlist.upcoming() is the remaining queue
        # We just need to set now_playing to playlist.current() and start playback.
        next_track = player._playlist.current()

        if next_track:
            logger.info(
                "SKIP_EMBEDDED: Moving to next track %s",
                getattr(next_track, "id", "unknown"),
            )

            # Update now_playing to the next track from canonical source
            player._set_now_playing_locked(next_track)
            player._state.is_playing = True
            _sm_transition(player, PlaybackPhase.PLAYING)

            # Capture backend + URL for caller to invoke OUTSIDE the lock
            backend = player._backend
            track_url = getattr(next_track, "url", None) or ""
            if backend and hasattr(backend, "play"):
                return (backend, track_url)
        else:
            # No more tracks in queue
            logger.info("SKIP_EMBEDDED: Queue empty, stopping playback")
            player._set_now_playing_locked(None)
            player._state.is_playing = False
            _sm_transition(player, PlaybackPhase.IDLE)

        return None

    def previous(self) -> None:
        """Go to previous track (local history first, provider-native fallback)."""
        engine_provider = self._active_engine_provider()
        try:
            with self._cv:
                # Delegate to player's previous track logic if available
                if hasattr(self._player, "previous"):
                    self._player.previous()
                self._cv.notify_all()
        except InvalidOperation:
            # Local history has nothing better to say. When an engine-managed
            # provider is actively playing (Spotify CDP), delegate to its
            # native previous-track transport instead of refusing — Spotify
            # keeps its own history/queue server-side (2026-07-02 skip-routing
            # class, previous() half).
            backend = self._backend_manager.backend
            backend_previous = None
            if engine_provider is not None and backend is not None:
                for method_name in ("previous", "previous_track", "prev"):
                    method = getattr(backend, method_name, None)
                    if callable(method):
                        backend_previous = method
                        break
            if backend_previous is None:
                raise
            self._logger.info(
                "PROVIDER_NATIVE_PREVIOUS: no local history, delegating to %s previous-track transport",
                engine_provider,
            )
            backend_previous()
        self._emit_callback()

    def set_volume(self, value: int) -> int:
        """Set volume level (0-100)."""
        hub = None
        with self._cv:
            clamped = max(0, min(100, value))
            self._player._volume = clamped
            if hasattr(self._player, "_state") and self._player._state is not None:
                self._player._state.volume = clamped
            backend = self._backend_manager.backend
            if backend and hasattr(backend, "set_volume"):
                backend.set_volume(clamped)
            hub = getattr(self._player, "_hub_state_authority", None)
            self._cv.notify_all()
        if hub is not None:
            try:
                hub.update_canonical_volume(clamped)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self._logger.warning("Hub canonical volume update failed: %r", exc)
        self._emit_callback()
        return clamped

    def seek(self, position_ms: int) -> None:
        """Seek to a position in the current track."""
        position_ms = max(0, int(position_ms))
        with self._cv:
            self._player._position_ms = position_ms
            if hasattr(self._player, "_state") and self._player._state is not None:
                self._player._state.position = position_ms
                if hasattr(self._player._state, "position_ms"):
                    self._player._state.position_ms = position_ms
            backend = self._backend_manager.backend
            self._cv.notify_all()

        if backend and hasattr(backend, "seek"):
            try:
                backend.seek(position_seconds=position_ms / 1000.0)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self._logger.warning("Backend seek failed: %r", exc)
        self._emit_callback()
        record_operation(OperationType.PLAYBACK, "seek", success=True, details={"position_ms": position_ms})

    def clear_queue(self) -> None:
        with self._cv:
            queue_size = self._queue_engine.queue_size()
            self._player._controller.clear_queue()
            # No _update_state_queue_locked() - mutations auto-invalidate
            self._logger.info("Queue: cleared %d items", queue_size)
        self._emit_callback()

    def remove_from_queue(self, item_id: str) -> None:
        removed = False
        with self._cv:
            now_playing = self._player._state.now_playing
            if now_playing is not None and now_playing.id == item_id:
                raise InvalidOperation("Cannot remove currently playing track")
            removed = self._player._controller.remove_from_queue(item_id)
            # No _update_state_queue_locked() - mutations auto-invalidate
            if removed:
                self._logger.info("Queue: removed item %s", item_id)
            else:
                self._logger.warning("Queue: item %s not found for removal", item_id)
        if removed:
            self._emit_callback()

    def reorder_queue(self, from_index: int, to_index: int) -> None:
        with self._cv:
            self._player._controller.reorder_queue(from_index, to_index)
            # No _update_state_queue_locked() - mutations auto-invalidate
            self._logger.info("Queue: reordered item from index %d to %d", from_index, to_index)
        self._emit_callback()

    def play_item_now(self, item_id: str) -> None:
        with self._cv:
            target_item = self._locate_queue_item(item_id)
            if target_item is None:
                raise ValueError(f"Queue item {item_id} not found")

            if is_youtube_track_requiring_provider(target_item):
                if not is_provider_linked("youtube_music"):
                    reason = get_youtube_unavailable_reason()
                    raise ConfigurationError(reason)

            moved = self._player._controller.move_to_next(item_id)
            if not moved:
                raise ValueError(f"Failed to move item {item_id} to current")

            current = self._queue_engine.current()
            if current and current.id == item_id:
                self._queue_engine.schedule_current()
            else:
                self._queue_engine.schedule_current()
                self._player._set_now_playing_locked(self._queue_engine.current())
                # No _update_state_queue_locked() - mutations auto-invalidate

            self._cv.notify_all()
        self._emit_callback()

    def enqueue_resolved_item(
        self,
        item: QueueItem,
        *,
        play_immediately: bool = False,
        emit: bool = True,
    ) -> QueueItem:
        need_stop = False
        with self._cv:
            if play_immediately:
                need_stop = True
                self._player._pending_skip_tokens = 0
                self._player._user_paused = False
                self._player._paused_current = None
                self._queue_engine.reset(item)
                self._player._set_now_playing_locked(item)
                # No _update_state_queue_locked() - mutations auto-invalidate
            else:
                self._player._controller.append_to_queue(item)
                # No _update_state_queue_locked() - mutations auto-invalidate
            self._cv.notify_all()
        # Stop backend OUTSIDE lock — CDP engines join poll threads
        if need_stop:
            try:
                self._player._stop_backend_locked()
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                from core.logging_config import get_logger

                get_logger(__name__).warning("Backend stop during enqueue_resolved_item failed: %r", exc)
        if emit:
            self._emit_callback()
        return item

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _locate_queue_item(self, item_id: str) -> QueueItem | None:
        current = self._queue_engine.current()
        if current and current.id == item_id:
            return current
        for item in self._queue_engine.upcoming():
            if item.id == item_id:
                return item
        return None


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from music.player import MusicPlayer
