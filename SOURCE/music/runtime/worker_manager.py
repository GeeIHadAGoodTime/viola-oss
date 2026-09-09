from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event, Timer, current_thread
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

from core.constants import TIMEOUT_SHORT
from core.user_context import get_current_or_device_user_id, get_current_user_id
from diagnostics.queue_history import (
    QueueEventType,
    get_queue_history,
    log_queue_event,
    new_correlation_id,
)
from models.player import QueueItem
from models.state_manager import RepeatMode
from music.controller.workers import IntegrityMonitorWorker, PlaybackWorker
from music.player.playback_state import PlaybackPhase
from music.recents_service import get_music_recents_service

if TYPE_CHECKING:
    from music.player import MusicPlayer


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        pass


def record_finished_track_recents(
    player: object,
    finished: object,
    *,
    user_id: str | None = None,
    logger: Any | None = None,
) -> None:
    """Best-effort durable recents write for a track that actually completed."""
    active_logger = logger or getattr(player, "_logger", None)
    try:
        track = _adapt_finished_to_recents_dict(finished)
        if track is None:
            if active_logger is not None:
                active_logger.debug("Music recents write skipped: completed track missing title and artist")
            return

        resolved_user_id = _resolve_music_recents_user_id(player, preferred_user_id=user_id)
        get_music_recents_service().record_play(resolved_user_id, track)
    except Exception as exc:
        if active_logger is not None:
            active_logger.warning("Music recents write failed after playback completion: %s", exc)


def _adapt_finished_to_recents_dict(finished: object) -> dict[str, str] | None:
    """Map queue item / track summary shapes to MusicRecentsService schema."""
    title = _track_text_field(finished, "title", "name")
    artist = _track_text_field(finished, "artist", "artists", "channel", "author")
    if not title and not artist:
        return None

    return {
        "provider": _track_text_field(finished, "provider", "source"),
        "track_uri": _track_text_field(
            finished,
            "track_uri",
            "uri",
            "provider_track_id",
            "stream_token",
            "video_id",
            "id",
            "url",
        ),
        "title": title,
        "artist": artist,
    }


def _resolve_music_recents_user_id(
    player: object,
    *,
    preferred_user_id: str | None = None,
) -> str:
    try:
        user_id = get_current_user_id()
        if user_id.strip():
            return user_id.strip()
    except LookupError:
        pass

    if preferred_user_id and preferred_user_id.strip():
        return preferred_user_id.strip()

    for owner in (
        getattr(player, "_state_manager", None),
        getattr(player, "_state_service", None),
        player,
    ):
        if owner is None:
            continue
        for field_name in (
            "user_id",
            "_user_id",
            "session_user_id",
            "_session_user_id",
        ):
            value = getattr(owner, field_name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return get_current_or_device_user_id()


def _track_text_field(track: object, *field_names: str) -> str:
    for field_name in field_names:
        value = _track_field_value(track, field_name)
        text = _coerce_track_field_to_text(value)
        if text:
            return text
    return ""


def _track_field_value(track: object, field_name: str) -> object:
    if isinstance(track, Mapping):
        return track.get(field_name)

    value = getattr(track, field_name, None)
    if value is not None:
        return value

    model_extra = getattr(track, "model_extra", None)
    if isinstance(model_extra, Mapping):
        return model_extra.get(field_name)

    return None


def _coerce_track_field_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return _track_text_field(value, "name", "title", "artist")
    if isinstance(value, (list, tuple, set)):
        parts = [_coerce_track_field_to_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    return str(value).strip()


class _BackendManagerProtocol(Protocol):
    def schedule_pending_start(self, item_id: str | None) -> None: ...

    def perform_integrity_pass(self) -> None: ...


class _WorkerTakeMeta(TypedDict, total=False):
    reason: str
    pending_before: bool
    is_playing_before: bool


# Idle-poll debug logs (#4863) repeat their liveness keepalive at most this
# often, in poll ticks. _wait_for_work_item polls every TIMEOUT_SHORT (0.1s),
# so 100 ticks is roughly once every 10s of continuous idle — a state change
# still logs immediately regardless of this floor.
_IDLE_DEBUG_KEEPALIVE_TICKS = 100


@dataclass
class PlaybackResult:
    """Result of a playback attempt.

    Attributes:
        success: Whether playback succeeded
        elapsed: Elapsed playback time in seconds
        is_embedded_webview: Whether this was embedded webview playback
        correlation_id: Unique ID for this playback attempt (propagates to completion)
    """

    success: bool
    elapsed: float | None
    is_embedded_webview: bool
    correlation_id: str | None = None


class RuntimeWorkerManager:
    """
    Owns playback/integrity workers and their control loops.

    The workers still operate on the legacy MusicPlayer instance, but keeping
    their lifecycle and loops here isolates the threading concerns from the
    rest of the runtime wiring.
    """

    def __init__(
        self,
        *,
        player: MusicPlayer,
        logger,
        queue_failure_ttl: int,
        start_playback: Callable[[QueueItem, tuple[int, str | None]], float | bool] | None = None,
        stop_backend: Callable[[], None] | None = None,
    ) -> None:
        self._player = player
        self._logger = logger.getChild("workers")
        self._queue_failure_ttl = queue_failure_ttl
        self._playback_worker: PlaybackWorker | None = None
        self._integrity_worker: IntegrityMonitorWorker | None = None
        self._integrity_stop = Event()
        self._stop_flag = False
        self._worker_heartbeat = 0
        # Dedupe state for the idle-poll debug logs (#4863): keyed by log
        # site, value is (last-logged signature, heartbeat it was logged at).
        # Without this, _wait_for_work_item's every-100ms poll logs the same
        # "nothing to do" lines at 10Hz forever while idle.
        self._last_idle_debug_signatures: dict[str, tuple[tuple, int]] = {}
        self._start_playback_cb = start_playback or player._start_and_monitor_playback
        self._stop_backend_cb = stop_backend or player._stop_backend_locked
        self._embedded_watchdog: Timer | None = None
        self._deferred_autoplay_timer: Timer | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def start(
        self,
        *,
        playback_enabled: bool,
        integrity_enabled: bool,
    ) -> None:
        self._stop_flag = False
        self._integrity_stop.clear()

        if playback_enabled:
            self._playback_worker = PlaybackWorker(
                target=self._playback_loop,
                enabled=True,
            )
            self._playback_worker.start()
        else:
            self._playback_worker = None

        if integrity_enabled:
            self._integrity_worker = IntegrityMonitorWorker(
                target=self._integrity_loop,
                stop_callback=self._integrity_stop.set,
                enabled=True,
            )
            self._integrity_worker.start()
        else:
            self._integrity_worker = None

    def shutdown(self, *, join_timeout: float) -> None:
        self._stop_flag = True
        self._integrity_stop.set()
        self._cancel_embedded_watchdog()
        self._cancel_deferred_autoplay()

        if self._playback_worker:
            self._playback_worker.join(timeout=join_timeout)
            self._playback_worker = None

        if self._integrity_worker:
            self._integrity_worker.stop()
            self._integrity_worker.join(timeout=join_timeout)
            self._integrity_worker = None
        if self._stop_backend_cb:
            try:
                self._stop_backend_cb()
            except Exception as e:
                self._logger.exception("stop_backend callback failed (non-critical): %s", e)

    # ------------------------------------------------------------------ #
    # Test helpers                                                       #
    # ------------------------------------------------------------------ #

    def process_next_track_for_tests(self) -> bool:
        player = self._player
        if not player._test_mode:
            raise RuntimeError("_process_next_track_for_tests() is only available in test mode")

        with player._cv:
            current = player._playlist.current()
            if current is not None and not player._playlist.pending:
                return False
            if current is None and not player._playlist.schedule_current():
                return False
            current = player._playlist.current()
            if current is None:
                return False
            token = player._playlist.snapshot()
            player._playlist.mark_playing()
            meta: _WorkerTakeMeta = {"reason": "test_mode"}
            self._log_worker_take_current(current, meta)

        result = self._start_playback_cb(current, token)
        playback_result = self._interpret_playback_result(current, result)
        with player._cv:
            if current is not None and player._playlist.current() is None:
                player._playlist.force_current(current, pending=False)
            if not playback_result.success or playback_result.elapsed is not None:
                player._playlist.complete_current(success=playback_result.success)
                player._set_now_playing_locked(None)
                player._state.is_playing = False
                _sm_transition(player, PlaybackPhase.IDLE)
            player._cv.notify_all()
        return playback_result.success

    # ------------------------------------------------------------------ #
    # Playback coordination helpers                                      #
    # ------------------------------------------------------------------ #

    def _log_worker_take_current(
        self,
        item: QueueItem,
        meta: _WorkerTakeMeta | None,
    ) -> None:
        """Emit structured diagnostics whenever the worker claims an item."""
        player = self._player
        meta_dict: _WorkerTakeMeta = meta or {}
        player._logger.info(
            "ENGINE_WORKER_TAKE_CURRENT item_id=%s reason=%s pending_before=%s is_playing_before=%s playback_mode=%s provider=%s",
            getattr(item, "id", "unknown"),
            meta_dict.get("reason", "unknown"),
            meta_dict.get("pending_before"),
            meta_dict.get("is_playing_before"),
            getattr(item, "playback_mode", None) or "unknown",
            getattr(item, "provider", None) or "unknown",
        )

    @staticmethod
    def _item_debug_fields(item: QueueItem | None) -> tuple[str | None, str | None]:
        if item is None:
            return None, None
        return getattr(item, "id", None), getattr(item, "title", None)

    def _pending_start_debug_fields(self) -> tuple[float | None, str | None]:
        backend_manager = getattr(self._player, "_backend_manager", None)
        if backend_manager is None:
            return getattr(self._player, "_pending_start_deadline", None), None
        return (
            getattr(backend_manager, "pending_start_deadline", None),
            getattr(backend_manager, "pending_start_item_id", None),
        )

    def _should_log_idle_debug(self, key: str, signature: tuple) -> bool:
        """Dedupe the idle-poll debug logs in ``_wait_for_work_item`` (#4863).

        ``_wait_for_work_item`` re-checks its state every ``TIMEOUT_SHORT``
        (0.1s) even when nothing is happening, and previously logged the same
        "nothing to do" line on every single tick — a 10Hz debug-log flood
        while idle. Log once per distinct ``signature`` (i.e. when the
        observed state actually changes) plus an occasional keepalive so a
        genuinely stuck state is still visible, instead of every poll tick.

        ``key`` separates independent call sites (e.g. WORKER_WAIT_LOOP vs
        WORKER_MAYBE_TAKE) so they dedupe against their own last-logged state,
        not each other's.
        """
        last_signature, last_heartbeat = self._last_idle_debug_signatures.get(key, (None, -_IDLE_DEBUG_KEEPALIVE_TICKS))
        heartbeat = self._worker_heartbeat
        if signature != last_signature or heartbeat - last_heartbeat >= _IDLE_DEBUG_KEEPALIVE_TICKS:
            self._last_idle_debug_signatures[key] = (signature, heartbeat)
            return True
        return False

    def _maybe_take_current_locked(
        self,
    ) -> tuple[QueueItem | None, tuple[int, str | None] | None, _WorkerTakeMeta | None]:
        """
        Determine whether there is a current item that needs backend start.
        Must be called with ``player._cv`` held.
        """
        player = self._player
        current_check = player._playlist.current()
        current_id, current_title = self._item_debug_fields(current_check)
        pending_deadline, pending_item_id = self._pending_start_debug_fields()
        entry_signature = (current_id, current_title, player._playlist.pending, pending_deadline, pending_item_id)
        if self._should_log_idle_debug("WORKER_MAYBE_TAKE", entry_signature):
            player._logger.debug(
                "WORKER_MAYBE_TAKE current_id=%s current_title=%s playlist_pending=%s pending_start_deadline=%s pending_start_item_id=%s",
                current_id,
                current_title,
                player._playlist.pending,
                pending_deadline,
                pending_item_id,
            )
        if current_check is not None and player._playlist.pending:
            meta: _WorkerTakeMeta = {
                "reason": "pending_current",
                "pending_before": True,
                "is_playing_before": player._state.is_playing,
            }
            token = player._promote_current_locked(set_is_playing=False, mark_playing=True)
            if token is not None:
                player._clear_pending_start_locked()
                return current_check, token, meta

        if current_check is None and player._playlist.schedule_current():
            current_scheduled = player._playlist.current()
            meta_scheduled: _WorkerTakeMeta = {
                "reason": "scheduled_next",
                "pending_before": player._playlist.pending,
                "is_playing_before": player._state.is_playing,
            }
            token = player._promote_current_locked(set_is_playing=False, mark_playing=True)
            if current_scheduled is not None and token is not None:
                player._clear_pending_start_locked()
                return current_scheduled, token, meta_scheduled

        current_id, current_title = self._item_debug_fields(player._playlist.current())
        pending_deadline, pending_item_id = self._pending_start_debug_fields()
        none_signature = (current_id, current_title, player._playlist.pending, pending_deadline, pending_item_id)
        if self._should_log_idle_debug("WORKER_MAYBE_TAKE_NONE", none_signature):
            player._logger.debug(
                "WORKER_MAYBE_TAKE_NONE current_id=%s current_title=%s playlist_pending=%s pending_start_deadline=%s pending_start_item_id=%s",
                current_id,
                current_title,
                player._playlist.pending,
                pending_deadline,
                pending_item_id,
            )
        return None, None, None

    def _force_pending_start_locked(
        self,
    ) -> tuple[QueueItem | None, tuple[int, str | None] | None, _WorkerTakeMeta | None]:
        """
        Force the worker to claim the current track if pending playback stalls.
        """
        player = self._player
        current = player._playlist.current()
        if current is None:
            player._clear_pending_start_locked()
            return None, None, None

        player._logger.warning(
            "Pending playback for item %s exceeded pending-start deadline; forcing worker start.",
            getattr(current, "id", "unknown"),
        )
        meta: _WorkerTakeMeta = {
            "reason": "pending_timeout",
            "pending_before": player._playlist.pending,
            "is_playing_before": player._state.is_playing,
        }
        token = player._promote_current_locked(set_is_playing=False, mark_playing=True)
        if token is not None:
            player._clear_pending_start_locked()
            return current, token, meta

        # Could not promote; extend deadline to avoid tight loops
        backend_manager = cast(_BackendManagerProtocol, player._backend_manager)
        backend_manager.schedule_pending_start(getattr(current, "id", None))
        return None, None, None

    # ------------------------------------------------------------------ #
    # Heartbeat helpers                                                  #
    # ------------------------------------------------------------------ #

    def heartbeat(self) -> int:
        return self._worker_heartbeat

    def should_stop(self) -> bool:
        return self._stop_flag

    def wait_for_heartbeat(self, last_seen: int, timeout: float = 0.5) -> bool:
        deadline = time.time() + max(0.0, timeout)
        player = self._player
        while True:
            if self.heartbeat() > last_seen:
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return self.heartbeat() > last_seen
            wait_duration = min(0.05, remaining)
            with player._cv:
                player._cv.wait(timeout=wait_duration)

    # ------------------------------------------------------------------ #
    # Repeat mode helpers                                                #
    # ------------------------------------------------------------------ #

    def _get_repeat_mode(self) -> RepeatMode:
        """Get current repeat mode from playback session controller."""
        try:
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            return controller.get_repeat_mode()
        except Exception as e:
            self._logger.exception("Failed to get repeat mode: %s", e)
            return RepeatMode.OFF

    def _should_repeat_current(self) -> bool:
        """Check if current track should repeat (Repeat One mode)."""
        return self._get_repeat_mode() == RepeatMode.ONE

    def _handle_repeat_one(self, finished_track: QueueItem) -> bool:
        """Handle Repeat One mode by re-queuing the finished track."""
        player = self._player

        try:
            backend = getattr(player, "_backend", None)
            if backend is not None and hasattr(backend, "seek"):
                backend.seek(0)
                self._logger.info(
                    "Repeat One: Seeking to beginning of track %s",
                    getattr(finished_track, "id", "unknown"),
                )
                return True

            player._playlist.force_current(finished_track, pending=True)
            self._logger.info(
                "Repeat One: Re-queued track %s",
                getattr(finished_track, "id", "unknown"),
            )
            return True

        except Exception:
            # Loud, not silent. Returning False degrades to "advance to the
            # next track", which is the right behaviour for playback (never
            # hard-stop the music), but it means the user asked for Repeat One
            # and got something else. At `warning` that trade was invisible;
            # `exception` records the traceback at ERROR so the swallow is
            # inspectable instead of a mystery.
            self._logger.exception(
                "Repeat One: Failed to repeat track %s - advancing instead",
                getattr(finished_track, "id", "unknown"),
            )
            return False

    # ------------------------------------------------------------------ #
    # Embedded watchdog timer                                            #
    # ------------------------------------------------------------------ #

    def _start_embedded_watchdog(self, item: QueueItem) -> None:
        """Start safety watchdog for embedded playback.

        If no YT_TRACK_ENDED arrives within timeout, force-skip the track.
        Timeout = track duration + 60s, or 10 minutes if duration unknown.
        """
        self._cancel_embedded_watchdog()
        duration_ms = getattr(item, "duration_ms", None) or getattr(item, "duration", None)
        if duration_ms and isinstance(duration_ms, (int, float)) and duration_ms > 0:
            # duration_ms > 1000 ⇒ assume milliseconds, else seconds
            duration_s = duration_ms / 1000 if duration_ms > 1000 else duration_ms
            timeout = duration_s + 60
        else:
            timeout = 600  # 10 minutes
        self._embedded_watchdog = Timer(timeout, self._embedded_watchdog_fire)
        self._embedded_watchdog.daemon = True
        self._embedded_watchdog.start()
        self._logger.info(
            "EMBEDDED_WATCHDOG: Set %ds timeout for track %s",
            int(timeout),
            getattr(item, "id", "unknown"),
        )

    def _cancel_embedded_watchdog(self) -> None:
        """Cancel the embedded watchdog timer if running."""
        if self._embedded_watchdog is not None:
            self._embedded_watchdog.cancel()
            self._embedded_watchdog = None

    def _embedded_watchdog_fire(self) -> None:
        """Safety timeout fired — force-advance past stuck embedded track."""
        player = self._player
        self._logger.warning(
            "EMBEDDED_WATCHDOG: No YT_TRACK_ENDED received — forcing skip",
        )
        with player._cv:
            player._pending_skip_tokens += 1
            player._cv.notify_all()

    # ------------------------------------------------------------------ #
    # Playback loop - Main entry point                                   #
    # ------------------------------------------------------------------ #

    def _playback_loop(self) -> None:
        """Main playback worker loop."""
        player = self._player

        while True:
            try:
                # Wait for work item
                current, token, _meta = self._wait_for_work_item()
                if self._stop_flag:
                    with player._cv:
                        player._cv.notify_all()
                    return

                if current is None or token is None:
                    continue

                # Execute playback
                result = self._execute_and_process_playback(current, token)

                # Handle completion
                self._handle_playback_completion(current, result)
            except Exception:
                self._logger.exception("Unhandled error in playback loop iteration — continuing")

    # ------------------------------------------------------------------ #
    # Playback loop - Wait for work                                      #
    # ------------------------------------------------------------------ #

    def _wait_for_work_item(
        self,
    ) -> tuple[QueueItem | None, tuple[int, str | None] | None, _WorkerTakeMeta | None]:
        """Wait until there's work to do. Returns (current, token, meta)."""
        player = self._player

        with player._cv:
            while not self._stop_flag:
                # Update heartbeat
                self._update_heartbeat()
                current_id, current_title = self._item_debug_fields(player._playlist.current())
                pending_deadline, pending_item_id = self._pending_start_debug_fields()
                wait_loop_signature = (
                    current_id,
                    current_title,
                    player._playlist.pending,
                    pending_deadline,
                    pending_item_id,
                )
                if self._should_log_idle_debug("WORKER_WAIT_LOOP", wait_loop_signature):
                    player._logger.debug(
                        "WORKER_WAIT_LOOP heartbeat=%s current_id=%s current_title=%s playlist_pending=%s pending_start_deadline=%s pending_start_item_id=%s",
                        self._worker_heartbeat,
                        current_id,
                        current_title,
                        player._playlist.pending,
                        pending_deadline,
                        pending_item_id,
                    )

                # Expire old failures
                player._playlist.expire_failures(self._queue_failure_ttl)

                # Handle pending skip
                if self._handle_pending_skip_locked():
                    continue

                # Try to get current item
                current, token, meta = self._maybe_take_current_locked()
                if current is not None and token is not None:
                    self._log_current_check(current, token)
                    self._log_worker_take_current(current, meta)
                    return current, token, meta

                # Check for forced pending start
                result = self._check_pending_start_deadline_locked()
                if result[0] is not None:
                    return result

                # Wait for signal
                player._cv.wait(timeout=TIMEOUT_SHORT)

        return None, None, None

    def _update_heartbeat(self) -> None:
        """Update heartbeat counter and emit telemetry."""
        player = self._player
        self._worker_heartbeat += 1

        player._telemetry.heartbeat(
            "music.player.worker",
            status="ok",
            heartbeat=self._worker_heartbeat,
        )

        if self._worker_heartbeat % 10 == 0:
            player._telemetry.emit_debug_event(
                "music_engine_heartbeat",
                {"status": "worker_loop", "heartbeat": self._worker_heartbeat},
                source="music_worker",
            )

        player._cv.notify_all()

    def _handle_pending_skip_locked(self) -> bool:
        """Handle pending skip if present. Returns True if skip was handled.

        P0-2 Hardening: Uses skip dedup to prevent UI+backend double-advance.
        """
        player = self._player
        current = player._playlist.current()

        if not player._pending_skip_tokens or not current:
            return False

        from_id = current.id if current else None
        _upcoming_list = player._playlist.upcoming()
        next_track = _upcoming_list[0] if _upcoming_list else None
        to_id = next_track.id if next_track else None

        # Get correlation_id from current playback (may be None for legacy code paths)
        corr_id = getattr(current, "_correlation_id", None)

        # P0-2: Check skip dedup - prevent double-advance from UI+backend
        history = get_queue_history()
        if corr_id and history.check_skip_dedup(from_id, corr_id):
            player._logger.debug(
                "Skip dedup triggered for track %s correlation_id=%s",
                from_id,
                corr_id,
            )
            player._pending_skip_tokens -= 1
            return True  # Skip was already processed

        # Log the skip event BEFORE processing
        log_queue_event(
            QueueEventType.PLAY_SKIP_CALLED,
            track_id=from_id,
            title=getattr(current, "title", None) if current else None,
            from_id=from_id,
            to_id=to_id,
            correlation_id=corr_id,
            reason="user_skip",
            outcome="skip",
            queue_length=player._playlist.queue_size(),
        )

        player._logger.debug("Skip pending detected in worker loop")
        player._pending_skip_tokens -= 1
        finished = player._playlist.complete_current(success=False)

        # Clear active playback tracking after skip completion
        if finished and corr_id:
            history.clear_active_playback(finished.id, corr_id, history.get_current_seq())

        if finished is not None:
            player._history.append(finished)

        # Queue mutations auto-invalidate - no sync needed
        player._set_now_playing_locked(player._playlist.current())

        if finished:
            player._last_played = finished

        if not player._playlist.current():
            player._state.is_playing = False
            _sm_transition(player, PlaybackPhase.IDLE)

        if player.autoplay:
            try:
                player.autoplay.check_and_run_async()
            except Exception as e:
                player._logger.exception("Autoplay trigger failed after skip (non-critical): %s", e)

        return True

    def _log_current_check(
        self,
        current: QueueItem,
        token: tuple[int, str | None],
    ) -> None:
        """Log current item check details."""
        player = self._player
        player._logger.info(
            "ENGINE_WORKER_CURRENT_CHECK item_id=%s token_version=%s playlist_pending=%s state_is_playing=%s",
            getattr(current, "id", "unknown"),
            token[0] if isinstance(token, tuple) else "unknown",
            player._playlist.pending,
            player._state.is_playing,
        )

    def _check_pending_start_deadline_locked(
        self,
    ) -> tuple[QueueItem | None, tuple[int, str | None] | None, _WorkerTakeMeta | None]:
        """Check and handle pending start deadline."""
        player = self._player
        backend_manager = getattr(player, "_backend_manager", None)

        deadline = (
            backend_manager.pending_start_deadline
            if backend_manager is not None
            else getattr(player, "_pending_start_deadline", None)
        )

        if deadline is None or time.time() < deadline:
            return None, None, None

        forced_current, forced_token, forced_meta = self._force_pending_start_locked()
        if forced_current is not None and forced_token is not None:
            self._log_worker_take_current(forced_current, forced_meta)
            return forced_current, forced_token, forced_meta

        return None, None, None

    # ------------------------------------------------------------------ #
    # Playback loop - Execute playback                                   #
    # ------------------------------------------------------------------ #

    def _execute_and_process_playback(
        self,
        current: QueueItem,
        token: tuple[int, str | None],
    ) -> PlaybackResult:
        """Execute playback and process the result.

        P0-3 / P1-1 Hardening:
        - Generates unique correlation_id per playback attempt
        - Registers playback start for repeated-start detection
        - Propagates correlation_id through to completion
        """
        # P1-1: Generate correlation_id for this playback attempt
        corr_id = new_correlation_id()

        # Store correlation_id on track for skip dedup (temporary attribute)
        try:
            object.__setattr__(current, "_correlation_id", corr_id)
        except (AttributeError, TypeError):
            # Frozen dataclass or slots - use dict fallback
            pass

        # P0-3: Register playback start and check for repeated start
        history = get_queue_history()
        is_repeated = history.register_playback_start(current.id, corr_id)

        if is_repeated:
            self._logger.warning(
                "Repeated start detected for track %s (corr_id=%s)",
                current.id,
                corr_id,
            )

        # Log start request with correlation_id and video_id
        video_id = getattr(current, "video_id", None)
        url = getattr(current, "url", None)
        # Fallback: extract video_id from YouTube URL if not set
        if not video_id and url and "youtube.com" in url:
            import re

            match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url)
            if match:
                video_id = match.group(1)
        log_queue_event(
            QueueEventType.PLAY_START_REQUEST,
            track_id=current.id,
            title=getattr(current, "title", None),
            current_id=current.id,
            correlation_id=corr_id,
            video_id=video_id,
            url=url,
            playback_mode=getattr(current, "playback_mode", None),
            provider=getattr(current, "provider", None),
            token_version=token[0] if token else None,
            is_repeated_start=is_repeated,
        )

        success = self._start_playback_cb(current, token)
        result = self._interpret_playback_result(current, success, corr_id)

        # Log result with correlation_id and video_id
        if result.success:
            log_queue_event(
                QueueEventType.PLAY_STARTED,
                track_id=current.id,
                title=getattr(current, "title", None),
                current_id=current.id,
                correlation_id=corr_id,
                outcome="success",
                video_id=video_id,
                elapsed=result.elapsed,
                is_embedded_webview=result.is_embedded_webview,
            )
        else:
            log_queue_event(
                QueueEventType.PLAY_ERROR,
                track_id=current.id,
                title=getattr(current, "title", None),
                current_id=current.id,
                correlation_id=corr_id,
                outcome="fail",
                reason="playback_failed",
                video_id=video_id,
                elapsed=result.elapsed,
                is_embedded_webview=result.is_embedded_webview,
            )

        return result

    def _interpret_playback_result(
        self,
        current: QueueItem,
        success: float | bool,
        correlation_id: str | None = None,
    ) -> PlaybackResult:
        """Interpret the raw playback result into a structured result.

        Returns:
            PlaybackResult with:
            - success=True if playback started successfully
            - elapsed=None for embedded webview (async), or seconds for stream playback
            - is_embedded_webview=True for iframe/webview modes

        Note: Check bool BEFORE (int, float) because bool is a subclass of int in Python!
        isinstance(True, int) == True, so we must explicitly check for bool first.
        """
        is_embedded_webview = getattr(current, "playback_mode", None) in (
            "embedded_webview",
            "embedded_iframe_webview",
        )
        # Browser-native provider handles track-end detection via its own
        # BrowserPlaybackController polling — NOT via YT_TRACK_ENDED.
        # Treating it as embedded_webview traps the worker in a 600s
        # watchdog wait that the browser provider never resolves.
        if is_embedded_webview and getattr(current, "provider", None) == "browser":
            is_embedded_webview = False

        # CRITICAL: Check bool first - bool is a subclass of int in Python!
        # Without this, isinstance(True, (int, float)) returns True
        if isinstance(success, bool):
            # Boolean return: True means success, False means failure
            # Embedded playback functions return bool directly
            success_flag = success
            elapsed = None
        elif isinstance(success, (int, float)):
            # Numeric return: elapsed time in seconds
            # For stream playback, this is the playback duration
            elapsed = float(success)
            if is_embedded_webview and elapsed == 0.0:
                # Special case: embedded webview with 0.0 elapsed is failure
                # (This shouldn't happen anymore since embedded returns bool)
                success_flag = False
            else:
                success_flag = True
        else:
            # Unknown type - treat as failure
            elapsed = None
            success_flag = False

        return PlaybackResult(
            success=success_flag,
            elapsed=elapsed,
            is_embedded_webview=is_embedded_webview,
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------ #
    # Playback loop - Handle completion                                  #
    # ------------------------------------------------------------------ #

    def _handle_playback_completion(
        self,
        current: QueueItem,
        result: PlaybackResult,
    ) -> None:
        """Handle track completion after playback ends.

        IMPORTANT: ``player._emit()`` is called OUTSIDE ``with player._cv``
        to prevent deadlock — _emit may acquire other locks (WebSocket hub,
        Qt signal dispatch) that could contend with _cv.
        """
        player = self._player
        current_id, current_title = self._item_debug_fields(current)
        player._logger.info(
            "WORKER_COMPLETION_ENTER current_id=%s current_title=%s result_success=%s thread=%s",
            current_id,
            current_title,
            result.success,
            current_thread().name,
        )

        with player._cv:
            self._worker_heartbeat += 1
            player._cv.notify_all()

            # Guard: If a new play request reset the playlist while this
            # track was being monitored, the playlist's current track is
            # now a DIFFERENT item.  Completing it would incorrectly
            # advance past the new track.  Skip completion entirely and
            # let the worker loop pick up the new pending track.
            playlist_current = player._playlist.current()
            playlist_current_id, playlist_current_title = self._item_debug_fields(playlist_current)
            player._logger.info(
                "WORKER_COMPLETION_LOCKED current_id=%s current_title=%s playlist_current_id=%s playlist_current_title=%s playlist_pending=%s result_success=%s thread=%s",
                current_id,
                current_title,
                playlist_current_id,
                playlist_current_title,
                player._playlist.pending,
                result.success,
                current_thread().name,
            )
            if playlist_current is not None and playlist_current.id != current.id:
                self._logger.info(
                    "Skipping completion for interrupted track %s (playlist current is now %s)",
                    current.id,
                    playlist_current.id,
                )
                # Don't call _complete_track_locked or _update_now_playing —
                # the new play request already set up the playlist correctly.
                pass
            # Check Repeat One mode BEFORE completing track
            elif result.success and self._try_handle_repeat_one():
                player._cv.notify_all()
                # _emit outside lock below

            # DEFER: For successful embedded playback, don't complete here.
            # Wait for YT_TRACK_ENDED → _handle_track_ended() in command_handlers.
            # For FAILED embedded playback, fall through to complete the track.
            elif result.is_embedded_webview and result.success:
                self._logger.info(
                    "EMBEDDED_DEFER_COMPLETION track_id=%s - waiting for YouTube ENDED signal",
                    current.id,
                )
                self._start_embedded_watchdog(current)
                player._cv.notify_all()
                # _emit outside lock below

            else:
                # Complete the track
                finished = self._complete_track_locked(result)

                # Repeat All: when queue is exhausted, loop from history.
                # Routed through the engine's own mutator, which owns the real
                # PlaylistCursor and invalidates the broadcast snapshot. The
                # previous code poked `_history`/`_upcoming`/`_current`/
                # `_sync_state` on `player._playlist` - but that is a
                # PlaylistQueueEngine, and those live on its `_cursor`, not on
                # the wrapper. So the first read raised AttributeError *inside*
                # the completion lock, which skipped both
                # `_update_now_playing_after_completion()` and `player._emit()`:
                # Repeat All never looped AND no state broadcast went out, so
                # clients kept rendering the finished track as still playing
                # long after audio had stopped (#2757).
                if (
                    result.success
                    and player._playlist.current() is None
                    and self._get_repeat_mode() == RepeatMode.ALL
                    and player._playlist.history_ref
                ):
                    looped_count = len(player._playlist.history_ref)
                    looped_first = player._playlist.loop_from_history()
                    if looped_first is not None:
                        self._logger.info(
                            "Repeat All (local): looping %d tracks, starting with %s",
                            looped_count,
                            getattr(looped_first, "id", "unknown"),
                        )

                # Record telemetry and trigger autoplay
                if finished:
                    self._record_completion_telemetry(result, finished)

                # Update state - queue mutations auto-invalidate, no sync needed
                self._update_now_playing_after_completion(result, finished)

        player._emit()
        player._logger.info(
            "WORKER_COMPLETION_RETURNING current_id=%s current_title=%s result_success=%s thread=%s",
            current_id,
            current_title,
            result.success,
            current_thread().name,
        )

    def _try_handle_repeat_one(self) -> bool:
        """Try to handle repeat one mode. Returns True if handled."""
        player = self._player

        if not self._should_repeat_current():
            return False

        current_for_repeat = player._playlist.current()
        if current_for_repeat is None:
            return False

        return self._handle_repeat_one(current_for_repeat)

    def _complete_track_locked(self, result: PlaybackResult) -> QueueItem | None:
        """Complete the current track. Returns finished track or None.

        P0-1 Hardening:
        - Uses completion guard keyed by (track_id, correlation_id)
        - Logs QUEUE_COMPLETE with correlation_id
        - Clears active playback tracking
        - Prevents double-advance via guard check
        """
        player = self._player
        current = player._playlist.current()

        if current is None:
            return None

        track_id = current.id
        corr_id = result.correlation_id

        # P0-1: Check completion guard - prevents double-advance
        history = get_queue_history()
        if history.check_completion_guard(track_id, corr_id):
            self._logger.warning(
                "Completion guard triggered for track %s correlation_id=%s - not advancing",
                track_id,
                corr_id,
            )
            return current  # Return track but don't advance again

        # Determine outcome string
        outcome = "success" if result.success else "fail"

        # Get next track info before completion
        _upcoming_list = player._playlist.upcoming()
        next_track = _upcoming_list[0] if _upcoming_list else None
        next_id = next_track.id if next_track else None

        # BUG FIX: Previously, embedded webview failures returned None without
        # advancing the queue, causing the failed track to stay as _current.
        # This created a retry loop where the same failed video would play
        # repeatedly. Now we always call complete_current() to advance the queue.
        finished = player._playlist.complete_current(success=result.success)

        # Log QUEUE_COMPLETE with correlation_id
        completion_seq = log_queue_event(
            QueueEventType.COMPLETE,
            track_id=track_id,
            title=getattr(finished, "title", None) if finished else None,
            from_id=track_id,
            to_id=next_id,
            correlation_id=corr_id,
            outcome=outcome,
            queue_length=(len(player._playlist.upcoming()) if player._playlist.upcoming() else 0),
        )

        # Clear active playback tracking
        if corr_id:
            history.clear_active_playback(track_id, corr_id, completion_seq)

        if finished:
            player._last_played = finished
            if result.success:
                record_finished_track_recents(player, finished)

        return finished

    def _record_completion_telemetry(
        self,
        result: PlaybackResult,
        finished: QueueItem,
    ) -> None:
        """Record telemetry for track completion.

        CB-13 fix: Autoplay trigger is debounced by 500ms after *natural*
        track completion to give the user time to enqueue a new track via
        ``/v1/play``.  If a manual play arrives during the window,
        ``cancel_pending()`` will cancel the deferred trigger.

        For *failed* completions (skips), autoplay is triggered immediately
        because: (a) the user is actively skipping and not about to issue a
        new play command, and (b) the skip handler in
        ``_handle_pending_skip_locked`` already calls
        ``check_and_run_async()`` directly.  Using the deferred timer for
        skips caused the timer to keep resetting on each rapid skip,
        preventing autoplay from ever firing until the queue was fully
        depleted.
        """
        player = self._player
        min_duration = getattr(player._playback_cfg, "MIN_PLAYBACK_DURATION_SEC", 0)

        metrics_elapsed = result.elapsed if result.elapsed is not None and result.elapsed >= min_duration else None

        player._telemetry.record_track_completion(metrics_elapsed)

        if player.autoplay:
            try:
                player.autoplay.set_anchor(finished)
                if result.success:
                    # CB-13: Debounce autoplay trigger after NATURAL track
                    # completion.  A 500ms delay gives the user time to send
                    # /v1/play before autoplay fills the queue with YouTube
                    # Radio tracks.
                    self._schedule_deferred_autoplay()
                else:
                    # Skip / failure: trigger autoplay immediately.  The
                    # skip handler already called check_and_run_async()
                    # directly, but re-triggering here is a safety net in
                    # case the earlier call was blocked by the generation
                    # lock.  Using the deferred timer here would keep
                    # resetting on rapid skips, starving autoplay.
                    player.autoplay.check_and_run_async()
            except Exception as e:
                player._logger.exception("Autoplay reseed/check failed (non-critical): %s", e)

    # ------------------------------------------------------------------ #
    # Deferred autoplay (CB-13 race condition fix)                       #
    # ------------------------------------------------------------------ #

    def _schedule_deferred_autoplay(self) -> None:
        """Schedule autoplay trigger after a short debounce window.

        This prevents the race where autoplay fills the queue with YouTube
        Radio tracks before the user's ``/v1/play`` request arrives.  The
        timer is cancelled by ``cancel_pending()`` if a manual play comes
        in during the window.
        """
        self._cancel_deferred_autoplay()
        timer = Timer(0.5, self._deferred_autoplay_fire)
        timer.daemon = True
        timer.start()
        self._deferred_autoplay_timer = timer
        self._logger.debug("Deferred autoplay scheduled (500ms debounce)")

    def _cancel_deferred_autoplay(self) -> None:
        """Cancel the deferred autoplay timer if running."""
        timer = getattr(self, "_deferred_autoplay_timer", None)
        if timer is not None:
            timer.cancel()
            self._deferred_autoplay_timer = None

    def _deferred_autoplay_fire(self) -> None:
        """Fire deferred autoplay after debounce window expires.

        Re-checks queue state before triggering to avoid overriding a
        user play request that arrived during the debounce window.
        """
        self._deferred_autoplay_timer = None
        player = self._player
        autoplay = getattr(player, "autoplay", None)
        if autoplay is None:
            return

        # Re-check: if user enqueued a track during the debounce window,
        # the queue is no longer empty — autoplay.should_trigger() will
        # return False and skip the fetch.
        try:
            autoplay.check_and_run_async()
        except Exception as e:
            self._logger.exception("Deferred autoplay trigger failed (non-critical): %s", e)

    def _update_now_playing_after_completion(
        self,
        result: PlaybackResult,
        finished: QueueItem | None,
    ) -> None:
        """Update now_playing state after track completion."""
        player = self._player
        current_track = player._playlist.current()

        # Handle test mode preservation
        if self._should_preserve_test_mode_now_playing(finished, current_track):
            return

        # Determine now_playing based on playback mode
        now_playing_mode = (
            getattr(player._state.now_playing, "playback_mode", None) if player._state.now_playing is not None else None
        )
        is_now_playing_embedded = now_playing_mode in (
            "embedded_webview",
            "embedded_iframe_webview",
        )

        if result.is_embedded_webview or is_now_playing_embedded:
            self._update_now_playing_embedded(current_track)
        else:
            self._update_now_playing_standard(current_track, finished)

        # Update is_playing
        if player._user_paused:
            self._logger.debug("_user_paused unexpectedly True during completion")
        player._state.is_playing = (
            current_track is not None or player._state.now_playing is not None
        ) and not player._user_paused
        _sm_transition(
            player,
            PlaybackPhase.PLAYING if player._state.is_playing else PlaybackPhase.IDLE,
        )

    def _should_preserve_test_mode_now_playing(
        self,
        finished: QueueItem | None,
        current_track: QueueItem | None,
    ) -> bool:
        """Check if test mode now_playing should be preserved."""
        player = self._player

        if not player._test_mode:
            return False

        if finished is None or len(player._queue) == 0:
            return False

        if player._state.now_playing is None or player._state.now_playing == finished:
            player._set_now_playing_locked(finished)
            player._state.is_playing = True
            _sm_transition(player, PlaybackPhase.PLAYING)
            player._logger.info(
                "Test mode: Preserved now_playing to finished track %s (queue has %d items)",
                finished.id,
                len(player._queue),
            )
            return True

        return False

    def _update_now_playing_embedded(self, current_track: QueueItem | None) -> None:
        """Update now_playing for embedded webview modes."""
        player = self._player

        if player._state.now_playing is not None:
            if current_track is not None and current_track != player._state.now_playing:
                player._set_now_playing_locked(current_track)
        elif current_track is not None:
            player._set_now_playing_locked(current_track)

    def _update_now_playing_standard(
        self,
        current_track: QueueItem | None,
        finished: QueueItem | None,
    ) -> None:
        """Update now_playing for standard playback modes."""
        player = self._player

        if (
            player._test_mode
            and current_track is None
            and player._state.now_playing is not None
            and len(player._queue) == 0
        ):
            player._set_now_playing_locked(None)
        else:
            player._set_now_playing_locked(current_track)

    # ------------------------------------------------------------------ #
    # Integrity loop                                                     #
    # ------------------------------------------------------------------ #

    def _integrity_loop(self) -> None:
        player = self._player
        backend_manager = cast(_BackendManagerProtocol, player._backend_lifecycle_manager)
        interval = getattr(player._playback_cfg, "INTEGRITY_INTERVAL_SECONDS", 15)
        while not self._integrity_stop.wait(interval):
            try:
                backend_manager.perform_integrity_pass()
            except Exception as e:
                player._logger.exception("Queue integrity monitor iteration failed (non-critical): %s", e)

    def run_integrity_check(self) -> None:
        """Expose a single integrity pass for tests."""
        player = self._player
        backend_manager = cast(_BackendManagerProtocol, player._backend_lifecycle_manager)
        backend_manager.perform_integrity_pass()
