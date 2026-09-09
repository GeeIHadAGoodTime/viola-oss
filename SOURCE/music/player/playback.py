"""
Music Player Playback Control.

This module contains playback control operations including:
- Play, pause, resume, stop
- Skip, previous track
- Volume control
- Seek operations
- Embedded/webview playback

Consolidated from:
- music_player_playback.py
- music_player_embedded.py
"""

from __future__ import annotations

import time
from typing import Any

from core.logging_config import get_logger
from models.player import QueueItem
from music.player.playback_state import PlaybackPhase

logger = get_logger(__name__)


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        logger.debug("State machine transition to %s failed (non-critical)", phase.name)


# ============================================================================
# Playback Controller
# ============================================================================


class MusicPlayerPlaybackController:
    """Handles playback control operations for the music player."""

    def __init__(self, player_instance):
        """
        Initialize playback controller.

        Args:
            player_instance: The MusicPlayer instance
        """
        self.player = player_instance

    def play_url(self, url: str) -> None:
        """
        Play a URL directly (bypassing queue).

        Args:
            url: URL to play
        """
        try:
            # Validate URL
            from music.providers.checker import is_youtube_url, require_provider_linked

            if is_youtube_url(url):
                require_provider_linked("youtube_music")

            # Stop current playback
            self.player._stop_locked()

            # Set up playback context
            self.player._current_url = url
            self.player._is_playing = True
            self.player._paused = False
            self.player._position_ms = 0
            _sm_transition(self.player, PlaybackPhase.PLAYING)

            # Load and start backend
            self.player._load_and_start_backend(url)

            # Emit state change
            self.player._emit()

            logger.info("Started direct playback of URL: %s", url)

        except Exception as e:
            logger.exception("Failed to play URL %s: %s", url, e)
            raise

    def play(self, item: Any | None = None) -> None:
        """
        Start or resume playback.

        Args:
            item: Specific item to play (None = current/next)
        """
        with self.player._lock:
            try:
                if item:
                    # Play specific item
                    self._play_specific_item(item)
                elif self.player._state.now_playing:
                    # Resume current item
                    self._resume_current_item()
                elif self.player._queue:
                    # Play next from queue
                    self._play_next_from_queue()
                else:
                    raise ValueError("Nothing to play")

            except Exception as e:
                logger.exception("Failed to start playback: %s", e)
                raise

    def pause(self) -> None:
        """Pause playback."""
        with self.player._lock:
            try:
                _sm = getattr(self.player, "_playback_sm", None)
                if _sm is not None:
                    if not _sm.is_playing:
                        return
                elif not self.player._is_playing or self.player._paused:
                    return

                # Pause backend
                if self.player._backend:
                    self.player._backend.pause()

                self.player._paused = True
                _sm_transition(self.player, PlaybackPhase.PAUSED)
                self.player._emit()

                logger.debug("Playback paused")

            except Exception:
                logger.exception("Failed to pause playback")
                raise

    def resume(self) -> None:
        """Resume playback with comprehensive state management.

        Uses timed lock acquisition to avoid deadlocks when _cv is contended.
        Falls back to direct backend resume if the lock cannot be acquired.

        IMPORTANT: Backend/engine resume is called OUTSIDE the _cv lock to
        prevent deadlock with the monitor loop and CDP poll threads.
        """
        acquired = self.player._cv.acquire(timeout=3.0)
        if not acquired:
            logger.warning("resume: _cv lock not acquired in 3s, best-effort resume")
            self._best_effort_resume()
            return

        need_engine_resume = False
        need_backend_resume = False
        try:
            # Check engine manager first if available
            if hasattr(self.player, "_engine_manager") and self.player._engine_manager is not None:
                try:
                    active_provider = self.player._engine_manager.active_provider()
                    if active_provider:
                        need_engine_resume = True
                except Exception as exc:
                    logger.warning("Engine manager active_provider check failed: %r", exc)

            if not need_engine_resume:
                # Fall back to backend manager
                self.player._ensure_playlist_coherent_locked()
                if self.player._backend_manager.backend:
                    need_backend_resume = True

            # Set resume flags INSIDE the lock (fast, no I/O)
            self.player._user_paused = False
            self.player._paused = False
            self.player._is_playing = True
            self.player._paused_current = None
            current_item = self.player._playlist.current()
            if current_item:
                self.player._set_now_playing_locked(current_item)
            self.player._state.is_playing = True
            _sm_transition(self.player, PlaybackPhase.PLAYING)
            self.player._cv.notify_all()
        finally:
            self.player._cv.release()

        # Perform backend/engine resume OUTSIDE the lock to avoid blocking
        # the monitor loop.  CDP engines may do network I/O here.
        if need_engine_resume:
            try:
                self.player._engine_manager.resume_active()
            except Exception as exc:
                logger.warning("Engine manager resume failed: %r", exc)
        elif need_backend_resume:
            try:
                self.player._backend_manager.resume_backend()
            except Exception as exc:
                logger.warning("Backend resume failed: %r", exc)

        logger.debug("Playback resumed")

    def _best_effort_resume(self) -> None:
        """Resume backend and set flags without holding _cv. Used as fallback."""
        backend = getattr(self.player._backend_manager, "backend", None)
        if backend and hasattr(backend, "resume"):
            try:
                backend.resume()
            except Exception as exc:
                logger.warning("Best-effort backend resume failed: %s", exc)
        self.player._user_paused = False
        self.player._paused = False
        self.player._is_playing = True
        if hasattr(self.player, "_state") and self.player._state is not None:
            self.player._state.is_playing = True
        _sm_transition(self.player, PlaybackPhase.PLAYING)

    def stop(self) -> None:
        """Stop playback."""
        with self.player._lock:
            self.player._stop_locked()

    def is_playing(self) -> bool:
        """Check if currently playing."""
        with self.player._lock:
            _sm = getattr(self.player, "_playback_sm", None)
            if _sm is not None:
                return _sm.is_playing
            return self.player._is_playing and not self.player._paused

    def set_volume(self, level: int) -> int:
        """
        Set playback volume.

        Args:
            level: Volume level (0-100)

        Returns:
            Actual volume level set
        """
        with self.player._lock:
            try:
                # Validate and clamp level
                level = max(0, min(100, level))

                # Set on backend
                if self.player._backend:
                    actual_level = self.player._backend.set_volume(level)
                else:
                    actual_level = level

                # Store volume
                self.player._volume = actual_level

                logger.debug("Volume set to %s", actual_level)
                return actual_level

            except Exception as e:
                logger.exception("Failed to set volume to %s: %s", level, e)
                raise

    def skip(self, count: int = 1) -> None:
        """
        Skip to next track(s).

        Args:
            count: Number of tracks to skip
        """
        # Notify spokes about track change
        try:
            app = getattr(self.player, "_app", None)
            stream_mgr = getattr(app.state, "audio_stream_manager", None) if app else None
            if stream_mgr is not None:
                stream_mgr.notify_track_change()
        except Exception:
            pass

        with self.player._lock:
            try:
                for _ in range(count):
                    if not self._skip_one_track():
                        break

                logger.debug("Skipped %s track(s)", count)

            except Exception as e:
                logger.exception("Failed to skip %s tracks: %s", count, e)
                raise

    def previous(self) -> None:
        """Go to previous track."""
        with self.player._lock:
            try:
                # Implementation would go back in queue
                # For now, just stop current track
                self.player._stop_locked()
                logger.debug("Previous track requested")

            except Exception:
                logger.exception("Failed to go to previous track")
                raise

    def seek(self, position_ms: int) -> None:
        """
        Seek to position in current track.

        Args:
            position_ms: Position in milliseconds
        """
        with self.player._lock:
            try:
                if not self.player._backend:
                    raise ValueError("No active backend")

                # Seek on backend
                self.player._backend.seek(position_seconds=position_ms / 1000.0)

                # Update position
                self.player._position_ms = position_ms

                logger.debug("Seeked to position %sms", position_ms)

            except Exception as e:
                logger.exception("Failed to seek to %sms: %s", position_ms, e)
                raise

    def skip_track(self) -> None:
        """Skip to the next track with comprehensive error handling and state management.

        Uses timed lock acquisition to avoid deadlocks when _cv is contended.
        """
        # Notify spokes BEFORE the skip so they can prepare for the transition
        try:
            player = self.player
            app = getattr(player, "_app", None)
            stream_mgr = getattr(app.state, "audio_stream_manager", None) if app else None
            if stream_mgr is not None:
                stream_mgr.notify_track_change()
        except Exception:
            pass

        try:
            acquired = self.player._cv.acquire(timeout=3.0)
            if not acquired:
                logger.warning("skip_track: _cv lock not acquired in 3s, best-effort skip")
                self._best_effort_skip()
                return

            try:
                if self._is_embedded_playback_mode():
                    self._skip_embedded_locked()
                    return

                self._ensure_coherent_playlist()
                should_fallback = self._process_skip_state()

                if should_fallback:
                    self._trigger_skip_fallback()

                logger.debug("Skip operation completed")
            finally:
                self.player._cv.release()

        except Exception as e:
            logger.exception("Critical error during skip: %s", e)
            self._emergency_stop()

    def _best_effort_skip(self) -> None:
        """Signal skip without holding _cv. Used as fallback."""
        try:
            self.player._pending_skip_tokens += 1
        except Exception as exc:
            logger.warning("Best-effort skip failed: %s", exc)

    def _is_embedded_playback_mode(self) -> bool:
        """Check if current playback is in embedded mode."""
        playback_mode = getattr(self.player._state, "playback_mode", None)
        now_playing = self.player._state.now_playing

        if not playback_mode and now_playing:
            playback_mode = getattr(now_playing, "playback_mode", None)

        if playback_mode in ("embedded_webview", "embedded_iframe_webview") and now_playing:
            logger.info("SKIP_EMBEDDED: Processing skip for embedded playback mode")
            return True
        return False

    def _ensure_coherent_playlist(self) -> None:
        """Ensure playlist is coherent before skip."""
        try:
            self.player._ensure_playlist_coherent_locked()
        except Exception as e:
            logger.exception("Failed to ensure playlist coherence: %s", e)

    def _process_skip_state(self) -> bool:
        """Process skip state update. Returns True if fallback needed."""
        try:
            return self._update_skip_state_locked()
        except Exception as e:
            logger.exception("Error during skip state update: %s", e)
            self._try_stop_recovery()
            return False

    def _update_skip_state_locked(self) -> bool:
        """Update state during skip. Returns True if fallback needed."""
        player = self.player
        should_trigger_fallback = False

        current = player._playlist.current()
        if current is None and not player._playlist.schedule_current():
            return self._handle_no_current_track()

        # Get current again in case schedule_current() changed it
        if current is None:
            current = player._playlist.current()

        if not player._state.is_playing:
            should_trigger_fallback = self._handle_not_playing_skip(current)
        else:
            should_trigger_fallback = self._handle_playing_skip()

        # Queue mutations auto-invalidate - no sync needed

        if player._playlist.queue_size() == 0:
            should_trigger_fallback = True

        return should_trigger_fallback

    def _handle_no_current_track(self) -> bool:
        """Handle skip when there's no current track."""
        player = self.player
        player._state.is_playing = False
        _sm_transition(player, PlaybackPhase.IDLE)
        player._cv.notify_all()
        return True  # Need fallback

    def _handle_not_playing_skip(self, current: Any) -> bool:
        """Handle skip when not currently playing."""
        player = self.player
        finished = player._playlist.complete_current(success=False)

        if finished is not None:
            player._state_pipeline.append_history(finished)

        # Queue mutations auto-invalidate - no sync needed
        player._set_now_playing_locked(player._playlist.current())
        player._state.is_playing = False
        _sm_transition(player, PlaybackPhase.IDLE)
        player._cv.notify_all()

        next_track = player._playlist.current()
        return next_track is None

    def _handle_playing_skip(self) -> bool:
        """Handle skip when currently playing."""
        player = self.player
        player._pending_skip_tokens += 1
        player._state.is_playing = False
        _sm_transition(player, PlaybackPhase.LOADING)
        player._cv.notify_all()
        return True  # Need fallback

    def _trigger_skip_fallback(self) -> None:
        """Trigger empty queue fallback."""
        try:
            self.player._trigger_empty_queue_fallback()
        except Exception as e:
            logger.error("Failed to trigger fallback: %s", e)

    def _try_stop_recovery(self) -> None:
        """Try to stop playback during error recovery."""
        try:
            self.player._stop_locked()
        except Exception as e:
            logger.exception("Failed to stop during skip error recovery: %s", e)

    def _emergency_stop(self) -> None:
        """Emergency stop on critical errors."""
        try:
            self.player._stop_locked()
        except Exception as e:
            logger.exception("Failed to stop during critical error: %s", e)

    def _skip_embedded_locked(self) -> None:
        """Handle skip for embedded playback modes. Must be called with _cv held."""
        player = self.player
        logger.info("SKIP_EMBEDDED: Processing skip for embedded playback mode")

        current = player._state.now_playing
        self._complete_current_embedded_track(current)

        # CRITICAL: After complete_current(), the PlaylistCursor is the authoritative source.
        # The next track to play is now playlist.current() (set by complete_current).
        # The remaining queue is playlist.upcoming().
        next_track = player._playlist.current()

        if next_track:
            self._play_next_embedded_track_from_cursor(next_track)
        else:
            self._handle_empty_embedded_queue()

        self._finalize_embedded_skip()

    def _complete_current_embedded_track(self, current: Any) -> None:
        """Complete the current track for embedded skip."""
        player = self.player

        if current:
            try:
                player._history.append(current)
            except Exception as exc:
                logger.warning("SKIP_EMBEDDED: Failed to append to history: %r", exc)
            logger.info("SKIP_EMBEDDED: Completed track %s", getattr(current, "id", "unknown"))

        try:
            if player._playlist.current():
                player._playlist.complete_current(success=True)
            else:
                logger.info("SKIP_EMBEDDED: No playlist current; scheduling first upcoming track")
                player._playlist.schedule_current()
        except Exception as exc:
            logger.warning("SKIP_EMBEDDED: Failed to advance playlist current: %r", exc)

    def _get_filtered_queue(self, current: Any) -> list[Any]:
        """Get queue filtered to exclude current track. DEPRECATED: Use playlist.upcoming() instead."""
        player = self.player
        queue = list(player._queue)
        current_id = getattr(current, "id", None) if current else None
        return [q for q in queue if getattr(q, "id", None) != current_id]

    def _play_next_embedded_track_from_cursor(self, next_track: Any) -> None:
        """Play the next track using PlaylistCursor as authoritative source."""
        player = self.player

        logger.info(
            "SKIP_EMBEDDED: Moving to next track %s: %s",
            getattr(next_track, "id", "unknown"),
            getattr(next_track, "title", "unknown"),
        )

        # Update player state to match PlaylistCursor
        player._set_now_playing_locked(next_track)
        player._state.is_playing = True
        _sm_transition(player, PlaybackPhase.PLAYING)

        # Queue mutations auto-invalidate - no sync needed

        # Re-assert embedded source flag before telling backend to play.
        # This is a safety measure — the flag should already be True from
        # the initial EMBEDDED_DEFER play, but re-asserting it guarantees
        # that is_embedded_source_active() returns True when backend.play()
        # checks whether to skip _dispatch_load (preventing doubled audio).
        try:
            from audio_core.streaming.pipeline_wiring import set_embedded_source

            set_embedded_source(True)
        except Exception:
            pass  # multiroom pipeline not active

        # Notify spokes about track change so they reset timing/buffers
        try:
            stream_mgr = getattr(player._app.state, "audio_stream_manager", None) if hasattr(player, "_app") else None
            if stream_mgr is not None:
                stream_mgr.notify_track_change()
        except Exception:
            pass  # multiroom not active or no app reference

        # Tell backend to play the track
        self._tell_backend_to_play(next_track)

        player._cv.notify_all()
        self._trigger_embedded_autoplay()

    def _play_next_embedded_track(self, actual_queue: list) -> None:
        """Play the next track in embedded mode. DEPRECATED: Use _play_next_embedded_track_from_cursor."""
        player = self.player
        next_track = actual_queue[0]
        # _state.queue is dead — cursor update below handles queue advance

        logger.info(
            "SKIP_EMBEDDED: Moving to next track %s: %s",
            getattr(next_track, "id", "unknown"),
            getattr(next_track, "title", "unknown"),
        )

        player._set_now_playing_locked(next_track)
        player._state.is_playing = True
        _sm_transition(player, PlaybackPhase.PLAYING)

        self._update_playlist_cursor(next_track)

        # Re-assert embedded source flag (same safety measure as primary path)
        try:
            from audio_core.streaming.pipeline_wiring import set_embedded_source

            set_embedded_source(True)
        except Exception:
            pass

        self._tell_backend_to_play(next_track)

        player._cv.notify_all()
        self._trigger_embedded_autoplay()

    def _update_playlist_cursor(self, track: Any) -> None:
        """Update playlist cursor for consistency."""
        try:
            self.player._playlist.force_current(track, pending=False)
        except Exception as exc:
            logger.warning("SKIP_EMBEDDED: Failed to update playlist cursor: %r", exc)

    def _tell_backend_to_play(self, track: Any) -> None:
        """Tell backend to play the new track."""
        backend = self.player._backend
        if backend and hasattr(backend, "play"):
            try:
                backend.play(track)
                logger.info(
                    "SKIP_EMBEDDED: Backend play() called for %s",
                    getattr(track, "id", "unknown"),
                )
            except Exception as exc:
                logger.warning("SKIP_EMBEDDED: Backend play() failed: %s", exc)

    def _handle_empty_embedded_queue(self) -> None:
        """Handle empty queue in embedded mode."""
        player = self.player
        logger.info("SKIP_EMBEDDED: Queue empty, stopping playback")
        player._set_now_playing_locked(None)
        player._state.is_playing = False
        _sm_transition(player, PlaybackPhase.IDLE)
        player._cv.notify_all()
        self._trigger_embedded_autoplay_empty()

    def _trigger_embedded_autoplay(self) -> None:
        """Trigger autoplay to refill queue if getting low."""
        player = self.player
        autoplay = getattr(player, "autoplay", None)
        if autoplay:
            remaining = len(player._queue)
            logger.info(
                "SKIP_EMBEDDED: Queue has %s songs remaining, checking autoplay",
                remaining,
            )
            try:
                autoplay.check_and_run_async()
            except Exception as exc:
                logger.debug("SKIP_EMBEDDED: Autoplay trigger failed: %s", exc)

    def _trigger_embedded_autoplay_empty(self) -> None:
        """Trigger autoplay to fill empty queue."""
        autoplay = getattr(self.player, "autoplay", None)
        if autoplay:
            try:
                autoplay.check_and_run_async()
            except Exception as exc:
                logger.debug("SKIP_EMBEDDED: Autoplay trigger failed: %s", exc)

    def _finalize_embedded_skip(self) -> None:
        """Finalize embedded skip with state emit and hub update."""
        player = self.player

        try:
            player._emit()
        except Exception as exc:
            logger.warning("SKIP_EMBEDDED: State emit failed: %r", exc)

        try:
            hub = getattr(player, "_hub_state_authority", None)
            if hub:
                logger.info("SKIP_EMBEDDED: Pushing authoritative state to hub")
                hub.update_canonical_now_playing(player._state)
        except Exception as exc:
            logger.debug("SKIP_EMBEDDED: Hub state push failed: %s", exc)

    def pause_playback(self) -> None:
        """Pause playback with comprehensive state management.

        Uses timed lock acquisition to avoid deadlocks when _cv is contended.
        Falls back to direct backend pause if the lock cannot be acquired.

        IMPORTANT: Backend pause is called OUTSIDE the _cv lock to prevent
        deadlock.  CDP engines may block on network I/O during pause, and
        holding _cv would starve the monitor loop (which acquires _cv each
        iteration to check skip tokens).
        """
        acquired = self.player._cv.acquire(timeout=3.0)
        if not acquired:
            logger.warning("pause_playback: _cv lock not acquired in 3s, best-effort pause")
            self._best_effort_pause()
            return

        need_engine_pause = False
        need_backend_pause = False
        try:
            if not self.player._backend_manager.backend:
                # Engine manager path
                need_engine_pause = hasattr(self.player, "_engine_manager") and self.player._engine_manager is not None
            else:
                self.player._ensure_playlist_coherent_locked()
                need_backend_pause = True

            # Set pause flags INSIDE the lock (fast, no I/O)
            self.player._user_paused = True
            self.player._paused = True
            self.player._is_playing = False
            self.player._paused_current = self.player._playlist.current()
            self.player._state.is_playing = False
            _sm_transition(self.player, PlaybackPhase.PAUSED, user_initiated=True)
            self.player._cv.notify_all()
        finally:
            self.player._cv.release()

        # Perform backend/engine pause OUTSIDE the lock to avoid blocking
        # the monitor loop.  CDP engines may do network I/O here.
        if need_engine_pause:
            try:
                self.player._engine_manager.pause_active()
            except Exception as exc:
                logger.warning("Engine manager pause failed: %r", exc)
        elif need_backend_pause:
            try:
                self.player._backend_manager.pause_backend()
            except Exception as exc:
                logger.warning("Backend pause failed: %r", exc)

        self.player._emit()

    def _best_effort_pause(self) -> None:
        """Pause backend and set flags without holding _cv. Used as fallback."""
        backend = getattr(self.player._backend_manager, "backend", None)
        if backend and hasattr(backend, "pause"):
            try:
                backend.pause()
            except Exception as exc:
                logger.warning("Best-effort backend pause failed: %s", exc)
        self.player._user_paused = True
        self.player._paused = True
        self.player._is_playing = False
        if hasattr(self.player, "_state") and self.player._state is not None:
            self.player._state.is_playing = False
        _sm_transition(self.player, PlaybackPhase.PAUSED, user_initiated=True)

    def stop_playback(self) -> None:
        """Stop playback with comprehensive state management.

        IMPORTANT: Backend stop is done OUTSIDE the lock to prevent
        AB-BA deadlock.  SpotifyCDPEngine.stop() joins its poll thread,
        which may be waiting to acquire player._lock via _emit().
        Holding _lock while joining would deadlock.
        """
        with self.player._lock:
            self.player._ensure_playlist_coherent_locked()
            # Update state flags FIRST (inside lock)
            self.player._is_playing = False
            self.player._paused = False
            self.player._user_paused = True
            self.player._paused_current = None
            self.player._state.is_playing = False
            _sm_transition(self.player, PlaybackPhase.STOPPED)
            self.player._set_now_playing_locked(None)
            self.player._playlist.reset(None)

        # Stop backend OUTSIDE the lock to avoid deadlock with poll threads
        # that call player._emit() -> acquires player._lock.
        try:
            self.player._stop_backend_locked()
        except Exception as exc:
            logger.warning("Backend stop during stop_playback failed: %r", exc)

        # Clear embedded source flag so hub-local can resume on next play
        try:
            from audio_core.streaming.pipeline_wiring import set_embedded_source

            set_embedded_source(False)
        except Exception:
            pass  # multiroom pipeline not active

        self.player._emit()

    def set_playback_volume(self, level: int) -> int:
        """Set playback volume with comprehensive backend management."""
        import threading

        logger.info(
            "SET_PLAYBACK_VOLUME: level=%s thread=%s",
            level,
            threading.current_thread().name,
        )
        level = max(0, min(100, level))

        with self.player._lock:
            # Set volume on engine manager if active
            if hasattr(self.player, "_engine_manager") and self.player._engine_manager is not None:
                try:
                    active_provider = self.player._engine_manager.active_provider()
                    if active_provider:
                        self.player._engine_manager.set_volume(level)
                except Exception as exc:
                    logger.warning("Engine manager set_volume failed: %r", exc)

            level = self.player._backend_manager.set_backend_volume(level)
            # CRITICAL: Update _volume (used by state() for broadcasts) not just _state.volume
            # state.py:100 reads from self.player._volume for PlayerState.volume
            self.player._volume = level
            self.player._state.volume = level

        # Update hub canonical state BEFORE emit to avoid conflict detection
        # This prevents the hub from overwriting intentional volume changes (e.g., ducking)
        hub = getattr(self.player, "_hub_state_authority", None)
        if hub is not None:
            try:
                hub.update_canonical_volume(level)
            except Exception as exc:
                logger.warning("Hub canonical volume update failed (non-critical): %s", exc)

        logger.info(
            "SET_PLAYBACK_VOLUME_EMIT: calling _emit() for volume=%s has_callback=%s",
            level,
            self.player.on_state_change is not None,
        )
        self.player._emit()
        return level

    def previous_track(self) -> None:
        """Skip to previous track with state management.

        Backend stop is done OUTSIDE the _cv lock to avoid deadlock with
        CDP poll threads.
        """
        with self.player._cv:
            self.player._ensure_playlist_coherent_locked()
            prev = self.player._playlist.rewind()
            if prev is None:
                from music.exceptions import InvalidOperation

                raise InvalidOperation("No previous track")

            self.player._pending_skip_tokens = 0
            self.player._user_paused = False
            self.player._paused_current = None
            self.player._state.is_playing = False
            _sm_transition(self.player, PlaybackPhase.LOADING)
            self.player._set_now_playing_locked(prev)
            # Queue mutations auto-invalidate - no sync needed
            self.player._cv.notify_all()

        # Stop backend OUTSIDE lock — CDP engines join poll threads
        try:
            self.player._stop_backend_locked()
        except Exception as exc:
            logger.warning("Backend stop during previous_track failed: %r", exc)

        self.player._emit()  # Best effort

    def _play_specific_item(self, item: Any) -> None:
        """Play a specific item."""
        # Set as current item
        self.player._set_now_playing_locked(item)
        self.player._current_url = getattr(item, "url", None) or getattr(item, "web_url", None)

        if not self.player._current_url:
            raise ValueError(f"Item has no playable URL: {item}")

        # Start playback
        self.player._is_playing = True
        self.player._paused = False
        self.player._position_ms = 0
        _sm_transition(self.player, PlaybackPhase.PLAYING)

        # Load backend
        self.player._load_and_start_backend(self.player._current_url)

        # Emit state change
        self.player._emit()

    def _resume_current_item(self) -> None:
        """Resume current item."""
        if self.player._backend:
            self.player._backend.resume()

        self.player._is_playing = True
        self.player._paused = False
        _sm_transition(self.player, PlaybackPhase.PLAYING)
        self.player._emit()

    def _play_next_from_queue(self) -> None:
        """Play next item from queue."""
        if not self.player._queue:
            raise ValueError("Queue is empty")

        # Get next item
        next_item = self.player._queue.pop(0)
        self.player._set_now_playing_locked(next_item)
        self.player._current_url = getattr(next_item, "url", None) or getattr(next_item, "web_url", None)

        if not self.player._current_url:
            raise ValueError(f"Queued item has no playable URL: {next_item}")

        # Start playback
        self.player._is_playing = True
        self.player._paused = False
        self.player._position_ms = 0
        _sm_transition(self.player, PlaybackPhase.PLAYING)

        # Load backend
        self.player._load_and_start_backend(self.player._current_url)

        # Emit state change
        self.player._emit()

    def _skip_one_track(self) -> bool:
        """
        Skip one track.

        Returns:
            True if successfully skipped, False if no more tracks
        """
        try:
            # Stop current playback
            self.player._stop_locked()

            # Check if there are more tracks in queue
            if not self.player._queue:
                logger.debug("No more tracks in queue to skip to")
                return False

            # Play next track
            self._play_next_from_queue()

            # Record skip metric
            try:
                from diagnostics.playback_metrics import PlaybackMetricsRecorder

                recorder = PlaybackMetricsRecorder()
                recorder.inc_counter("viola_tracks_skipped_total")
            except Exception as e:
                logger.exception("Failed to record skip metrics: %s", e)

            return True

        except Exception:
            logger.exception("Failed to skip track")
            return False


# ============================================================================
# Embedded Playback
# ============================================================================


class MusicPlayerEmbeddedPlayback:
    """Handles embedded YouTube/webview playback for the music player."""

    def __init__(self, player_instance):
        """
        Initialize embedded playback handler.

        Args:
            player_instance: The MusicPlayer instance
        """
        self.player = player_instance

    def get_browser_backend_display_name(self, item: QueueItem) -> str:
        """
        Get display name for browser backend.

        Args:
            item: Queue item

        Returns:
            Display name string
        """
        if hasattr(item, "provider") and item.provider:
            return f"{item.provider} Web Player"
        elif hasattr(item, "source") and item.source == "url":
            return "Web Player"
        else:
            return "Browser Player"

    def play_embedded_webview(self, item: QueueItem) -> bool:
        """
        Play item using embedded webview.

        Args:
            item: Queue item to play

        Returns:
            True if playback started successfully, False otherwise
        """
        try:
            if not self.player._backend:
                logger.error("No backend available for embedded webview playback")
                return False

            # Check if backend supports embedded playback
            if not getattr(self.player._backend_capabilities, "is_embedded", False):
                logger.debug("Backend does not support embedded playback")
                return False

            # Prepare backend for the track
            self.player._backend_mgr.prepare_backend_for_track(item)

            # Start playback
            url = getattr(item, "url", "") or getattr(item, "web_url", "")
            if not url:
                logger.error("No URL available for embedded playback")
                return False

            logger.info("Starting embedded webview playback: %s", item.title)
            self.player._backend.play_url(url)

            # Update state
            with self.player._state_lock:
                self.player._set_now_playing_locked(item)
                self.player._last_backend_activity = time.time()

            self.player._emit()
            return True

        except Exception as e:
            logger.exception("Embedded webview playback failed: %s", e)
            return False

    def play_embedded_iframe_webview(self, item: QueueItem) -> bool:
        """
        Play item using embedded iframe webview.

        Args:
            item: Queue item to play

        Returns:
            True if playback started successfully, False otherwise
        """
        try:
            if not self.player._backend:
                logger.error("No backend available for iframe webview playback")
                return False

            # Check if backend supports iframe playback
            if not getattr(self.player._backend_capabilities, "supports_youtube", False):
                logger.debug("Backend does not support YouTube iframe playback")
                return False

            # For YouTube iframe, we need to ensure compliance
            if hasattr(self.player, "_compliance_context"):
                compliance_check = self.player._compliance_context.check_item_compliance(item)
                if not compliance_check.compliant:
                    logger.warning(
                        "Item not compliant with YouTube ToS: %s",
                        compliance_check.reason,
                    )
                    # Still allow playback but log the issue
                    self.player._record_compliance_violation(item, compliance_check)

            # Prepare backend for the track
            self.player._backend_mgr.prepare_backend_for_track(item)

            # Start iframe playback
            video_id = getattr(item, "video_id", None) or getattr(item, "youtube_id", None)
            if not video_id:
                logger.error("No video ID available for iframe playback")
                return False

            logger.info("Starting YouTube iframe playback: %s (ID: %s)", item.title, video_id)

            # Use iframe-specific play method if available
            if hasattr(self.player._backend, "play_youtube_iframe"):
                self.player._backend.play_youtube_iframe(video_id)
            else:
                # Fallback to URL-based playback
                youtube_url = f"https://www.youtube.com/watch?v={video_id}"
                self.player._backend.play_url(youtube_url)

            # Update state
            with self.player._state_lock:
                self.player._set_now_playing_locked(item)
                self.player._last_backend_activity = time.time()

            self.player._emit()
            return True

        except Exception as e:
            logger.exception("Embedded iframe webview playback failed: %s", e)
            return False

    def play_external_browser(self, item: QueueItem) -> bool:
        """
        Play item using external browser.

        Args:
            item: Queue item to play

        Returns:
            True if playback started successfully, False otherwise
        """
        try:
            url = getattr(item, "url", "") or getattr(item, "web_url", "")
            if not url:
                logger.error("No URL available for external browser playback")
                return False

            logger.info("Starting external browser playback: %s", item.title)

            # Launch external browser - this is a fallback for when embedded fails
            import webbrowser

            webbrowser.open(url)

            # Update state to indicate external playback
            with self.player._state_lock:
                self.player._set_now_playing_locked(item)
                self.player._external_playback = True
                self.player._last_backend_activity = time.time()

            self.player._emit()
            return True

        except Exception as e:
            logger.exception("External browser playback failed: %s", e)
            return False

    def start_and_monitor_playback(self, item: QueueItem) -> bool:
        """
        Start playback and begin monitoring.

        Args:
            item: Queue item to play

        Returns:
            True if playback started successfully, False otherwise
        """
        # Try embedded iframe first (YouTube)
        if self.play_embedded_iframe_webview(item):
            return True

        # Try embedded webview
        if self.play_embedded_webview(item):
            return True

        # Fallback to external browser
        if self.play_external_browser(item):
            return True

        logger.error("All playback methods failed for: %s", item.title)
        return False

    def record_compliance_violation(self, item: QueueItem, compliance_check: Any) -> None:
        """
        Record a compliance violation.

        Args:
            item: Queue item that violated compliance
            compliance_check: Compliance check result
        """
        try:
            violation = {
                "timestamp": time.time(),
                "item_id": item.id,
                "title": item.title,
                "reason": compliance_check.reason,
                "severity": getattr(compliance_check, "severity", "warning"),
                "provider": getattr(item, "provider", "unknown"),
                "url": getattr(item, "url", None),
            }

            if not hasattr(self.player, "_compliance_violations"):
                self.player._compliance_violations = []

            self.player._compliance_violations.append(violation)

            # Keep only last 100 violations
            if len(self.player._compliance_violations) > 100:
                self.player._compliance_violations = self.player._compliance_violations[-100:]

            logger.warning("Compliance violation recorded: %s", compliance_check.reason)

        except Exception as e:
            logger.debug("Failed to record compliance violation: %s", e)
