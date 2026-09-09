from __future__ import annotations

import time
from typing import Any

from core.constants import TIMEOUT_SHORT
from models.player import QueueItem
from music.player.playback_state import PlaybackPhase
from music.resolution.provider_router import Source


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        pass


class PlayCommandService:
    """Handles the heavy-weight play() orchestration."""

    def __init__(
        self,
        player: MusicPlayer,
        logger,
        pending_start_timeout: float,
        resolver,
    ) -> None:
        self._player = player
        self._logger = logger.getChild("play_command")
        self._pending_start_timeout = pending_start_timeout
        self._resolver = resolver

    def _append_to_queue_locked(
        self,
        item: QueueItem,
        query: str,
    ) -> tuple[bool, QueueItem]:
        """Append item to queue. Must be called with player._cv held.

        Returns (appended, return_item) tuple.
        """
        player = self._player
        logger = self._logger
        queue_len = player._playlist.queue_size()

        if queue_len >= player._max_queue_size:
            player._playlist.set_backpressure(origin="user", reason="capacity")
            logger.warning(
                "Queue at maximum capacity (%s), not adding: %s",
                player._max_queue_size,
                query[:50],
            )
            current_item = player._playlist.current()
            return (False, current_item if current_item else item)

        player._controller.append_to_queue(item)
        # Mutations auto-invalidate - no sync needed
        player._playlist.clear_backpressure()
        player._user_paused = False
        player._paused_current = None
        current_item = player._playlist.current()
        return_item = current_item if current_item else item

        if player._test_mode and current_item is not None:
            if player._state.now_playing is None or player._state.now_playing != current_item:
                player._set_now_playing_locked(current_item)
                player._state.is_playing = True
                _sm_transition(player, PlaybackPhase.LOADING)
                logger.info(
                    "Test mode: Set now_playing to current track after queueing: %s",
                    current_item.id,
                )
        player._cv.notify_all()
        return (True, return_item)

    @staticmethod
    def _is_embedded_webview(item: QueueItem | None) -> bool:
        """Check if item uses embedded webview playback."""
        if item is None:
            return False
        playback_mode = getattr(item, "playback_mode", None)
        return playback_mode in ("embedded_webview", "embedded_iframe_webview")

    def _try_set_now_playing_locked(self, item: QueueItem) -> bool:
        """Set now_playing if needed. Must be called with player._cv held.

        Returns True if now_playing was set/updated.
        """
        player = self._player
        if player._state.now_playing is None or player._state.now_playing != item:
            player._set_now_playing_locked(item)
            player._state.is_playing = True
            _sm_transition(player, PlaybackPhase.LOADING)
            return True
        return False

    def _start_fresh_playback_locked(
        self,
        item: QueueItem,
        query: str,
        is_interrupt: bool,
    ) -> None:
        """Start fresh playback with item. Must be called with player._cv held.

        NOTE: This method sets a ``_deferred_backend_stop`` flag on the
        player so the caller can stop the backend OUTSIDE the lock.
        The actual backend stop is deferred to avoid deadlock with CDP
        poll threads that may be waiting for player._lock via _emit().
        """
        player = self._player
        logger = self._logger

        if is_interrupt:
            logger.info(
                "Interrupting current playback to play new track: %s",
                query[:50],
            )
        # Cancel any active embedded watchdog from the previous track so it
        # doesn't fire a stale force-skip after the new track loads.
        # Also cancel any deferred autoplay timer (CB-13 fix) so a
        # completion-triggered autoplay doesn't override this play request.
        worker_mgr = getattr(player, "_worker_manager", None)
        if worker_mgr is not None:
            worker_mgr._cancel_embedded_watchdog()
            worker_mgr._cancel_deferred_autoplay()
        # Mark that backend stop is needed — actual stop deferred to
        # outside the _cv lock to prevent deadlock with CDP poll threads.
        player._deferred_backend_stop = True
        player._pending_skip_tokens = 0
        player._is_playing = False
        player._user_paused = False
        player._paused_current = None
        _sm_transition(player, PlaybackPhase.LOADING)
        player._controller.reset_playlist(item)
        player._playlist.clear_upcoming()
        player._playlist.schedule_current()
        player._backend_manager.schedule_pending_start(
            getattr(item, "id", None),
            timeout=self._pending_start_timeout,
        )
        player._promote_current_locked(set_is_playing=False)
        player._playlist.reset_autoplay_counter()
        player._autoplay_anchor = item
        player._cv.notify_all()

    def _ensure_engine_finish_hook(self) -> None:
        """Attach engine finish shim if not already done."""
        player = self._player
        manager = getattr(player, "_playback_manager", None)
        if manager is None or getattr(player, "_engine_finish_hook_set", False):
            return

        class _QueueEngineShim:
            def __init__(self, owner: MusicPlayer) -> None:
                self._owner = owner

            def handle_transport_finished(self, *, source: str = "transport") -> None:
                self._owner._on_engine_finished(source)

        try:
            manager.set_queue_engine(_QueueEngineShim(player))
            player._engine_finish_hook_set = True
        except Exception as e:
            self._logger.exception("Failed to attach engine finish shim (non-critical): %s", e)

    def _trigger_test_playback_if_needed(self, item: QueueItem) -> None:
        """Trigger test-mode playback processing if applicable."""
        player = self._player
        if not player._test_mode:
            return

        playback_mode = getattr(item, "playback_mode", None)
        should_trigger = False
        if self._is_embedded_webview(item):
            manager = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
            should_trigger = manager is not None
        elif playback_mode == "vlc_stream":
            should_trigger = True

        if should_trigger:
            try:
                player._worker_manager.process_next_track_for_tests()
            except Exception as e:
                self._logger.exception("Test-mode playback failed (non-critical): %s", e)

    def _set_embedded_now_playing(self, item: QueueItem, emit: bool) -> None:
        """Set now_playing immediately for embedded playback modes."""
        player = self._player
        logger = self._logger

        if not self._is_embedded_webview(item):
            return

        with player._cv:
            logger.info(
                "play_command setting now_playing for embedded mode - item=%s",
                item.id,
            )
            player._set_now_playing_locked(item)
            player._is_playing = True
            player._user_paused = False
            if hasattr(player, "_state") and player._state is not None:
                player._state.is_playing = True
                player._state.now_playing = item
            _sm_transition(player, PlaybackPhase.PLAYING)
            player._cv.notify_all()

        if emit:
            player._emit()

    def play(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        """
        Play a track.

        Args:
            query: Search query or URL
            source: Media source hint
            emit: Whether to emit state update
            metadata: Optional metadata
            interrupt: If True (default), interrupt current playback and play immediately.
                      If False, append to queue without interrupting.
        """
        player = self._player
        logger = self._logger
        logger.warning("[QUEUE_TRACE] PlayCommandService.play() ENTER query=%s", query[:80])

        self._ensure_engine_finish_hook()

        engine_manager = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
        item = self._resolver.resolve(
            query,
            source,
            metadata,
            engine_manager=engine_manager,
        )

        appended_to_queue = False
        return_item: QueueItem = item

        with player._cv:
            has_active_track = player._playlist.current() is not None or player._playlist.queue_size() > 0
            player._deferred_backend_stop = False  # Reset before locked section

            if has_active_track and not interrupt:
                # Append to queue without interrupting (explicit queue request)
                appended_to_queue, return_item = self._append_to_queue_locked(item, query)
            else:
                # Start fresh: either no active track, or interrupt requested
                self._start_fresh_playback_locked(item, query, has_active_track)

        # Perform deferred backend stop OUTSIDE the _cv lock to prevent
        # deadlock with CDP poll threads that call player._emit().
        if getattr(player, "_deferred_backend_stop", False):
            player._deferred_backend_stop = False
            try:
                player._stop_backend_locked()
            except Exception as exc:
                self._logger.warning("Deferred backend stop failed: %r", exc)

        self._trigger_test_playback_if_needed(item)

        if player._telemetry is not None:
            player._telemetry.record_play_request()

        autoplay = getattr(player, "autoplay", None)
        logger.info(
            "🔄 PlayCommand: autoplay=%s appended_to_queue=%s",
            autoplay is not None,
            appended_to_queue,
        )
        if autoplay and not appended_to_queue:
            try:
                autoplay.cancel_pending()
                autoplay.set_anchor(item)
                ensure_blocking = getattr(autoplay, "ensure_buffer_blocking", None)
                if callable(ensure_blocking):
                    logger.warning("[QUEUE_TRACE] PlayCommandService.play() calling ensure_buffer_blocking")
                    added = int(ensure_blocking(current_track=item, timeout_seconds=6.0))
                    logger.warning(
                        "[QUEUE_TRACE] PlayCommandService.play() ensure_buffer_blocking returned added=%d",
                        added,
                    )
                else:
                    logger.warning("[QUEUE_TRACE] PlayCommandService.play() calling check_and_run_async")
                    autoplay.check_and_run_async()
                    logger.warning("[QUEUE_TRACE] PlayCommandService.play() check_and_run_async returned")
            except Exception as exc:
                logger.exception("Autoplay reseed failed after manual play: %s", exc)
        else:
            logger.info(
                "🔄 PlayCommand: Skipping autoplay trigger (autoplay=%s, appended=%s)",
                autoplay is not None,
                appended_to_queue,
            )

        if emit:
            player._emit()

        # For embedded playback modes, set now_playing immediately
        self._set_embedded_now_playing(item, emit)

        if player._test_mode:
            self._ensure_now_playing_state(item)

        return return_item

    def _ensure_now_playing_state(self, item: QueueItem) -> None:
        """Replicates the test-mode safety nets from legacy play()."""
        player = self._player
        logger = self._logger

        # Initial sync: set now_playing if current exists but now_playing is None
        with player._cv:
            current_item = player._playlist.current()
            if current_item is not None and player._state.now_playing is None:
                self._try_set_now_playing_locked(current_item)
                logger.info(
                    "Test mode: Re-set now_playing after play() for current track: %s",
                    current_item.id,
                )

        time.sleep(0.02)

        # Handle embedded webview mode
        self._ensure_embedded_now_playing()

        # Poll for now_playing (test mode only)
        if player._test_mode:
            self._poll_embedded_now_playing(timeout=TIMEOUT_SHORT)

        # Final fallback and keepalive
        self._final_now_playing_fallback()

        # Start finish polling (test mode only)
        if player._test_mode:
            self._start_finish_polling()

    def _ensure_embedded_now_playing(self) -> None:
        """Ensure now_playing is set for embedded webview modes."""
        player = self._player
        logger = self._logger

        with player._cv:
            current_item = player._playlist.current()
            if self._is_embedded_webview(current_item) and current_item is not None:
                logger.info("UI: Setting now_playing for embedded mode")
                self._try_set_now_playing_locked(current_item)
                if player._playlist.current() is None:
                    player._playlist.force_current(current_item, pending=False)
                logger.info(
                    "UI: Final ensure now_playing for embedded webview: %s",
                    current_item.id,
                )
            player._cv.notify_all()

    def _poll_embedded_now_playing(self, timeout: float) -> None:
        """Poll briefly to ensure now_playing stays set for embedded modes."""
        player = self._player
        deadline = time.time() + timeout
        while time.time() < deadline:
            with player._cv:
                current_item = player._playlist.current()
                if self._is_embedded_webview(current_item) and current_item is not None:
                    self._try_set_now_playing_locked(current_item)
            time.sleep(0.01)

    def _final_now_playing_fallback(self) -> None:
        """Final fallback to set now_playing from any available source."""
        player = self._player
        logger = self._logger

        with player._cv:
            current_item = player._playlist.current()

            # Try current embedded item
            if self._is_embedded_webview(current_item) and current_item is not None:
                if self._try_set_now_playing_locked(current_item):
                    logger.info("Final ensure now_playing: %s", current_item.id)

            # Fallback chain if still None
            if player._state.now_playing is None:
                logger.warning("now_playing is None after safeguards!")
                self._apply_now_playing_fallback_locked()
            else:
                logger.info(
                    "SUCCESS: now_playing is %s",
                    player._state.now_playing.id if player._state.now_playing else None,
                )

            # Start keepalive thread for embedded modes
            if player._state.now_playing is not None:
                current_item = player._playlist.current()
                if self._is_embedded_webview(current_item):
                    self._start_now_playing_keepalive(current_item)

    def _apply_now_playing_fallback_locked(self) -> None:
        """Try fallback sources to set now_playing. Must be called with lock."""
        player = self._player
        logger = self._logger

        current_item = player._playlist.current()
        if current_item is not None:
            self._try_set_now_playing_locked(current_item)
            logger.info("Fallback: Set to current track: %s", current_item.id)
        elif player._history:
            first_played = player._history[0]
            self._try_set_now_playing_locked(first_played)
            logger.info("Fallback: Set to history[0]: %s", first_played.id)
        elif player._queue:
            first_queued = player._queue[0]
            self._try_set_now_playing_locked(first_queued)
            logger.info("Fallback: Set to queue[0]: %s", first_queued.id)

    def _start_now_playing_keepalive(self, item: QueueItem) -> None:
        """Start background thread to keep now_playing alive for embedded modes."""
        import threading

        player = self._player
        logger = self._logger

        def _keep_alive() -> None:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                with player._cv:
                    if player._state.now_playing is None and item is not None:
                        self._try_set_now_playing_locked(item)
                        logger.debug("Re-set now_playing in background thread")
                time.sleep(0.05)

        threading.Thread(target=_keep_alive, daemon=True).start()

    def _start_finish_polling(self) -> None:
        """Start background polling to detect track finish (test mode)."""
        import threading

        player = self._player
        manager = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
        if manager is None:
            return

        def _poll_finish() -> None:
            deadline = time.time() + 2.0
            track_was_playing = False
            while time.time() < deadline:
                try:
                    with player._cv:
                        current = player._playlist.current()
                        active_provider = manager.active_provider()
                        if current is not None:
                            track_was_playing = True
                        if active_provider is None and track_was_playing and current is not None:
                            player._history.append(current)
                            player._playlist.force_current(None, pending=False)
                            player._state.is_playing = False
                            player._state.now_playing = None
                            player._cv.notify_all()
                            return
                except Exception as e:
                    self._logger.debug("Finish polling failed (non-critical): %s", e)
                    break
                time.sleep(0.05)

        threading.Thread(target=_poll_finish, daemon=True).start()


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from music.player import MusicPlayer
