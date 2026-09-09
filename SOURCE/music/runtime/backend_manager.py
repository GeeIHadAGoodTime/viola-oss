from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from config.settings import settings
from models.player import QueueItem
from music.backends.base import BaseBackend
from music.backends.loader import BackendLoadRequest, BackendStrategyLoader

if TYPE_CHECKING:
    from diagnostics.runtime_metrics import RuntimeMetrics


class BackendLifecycleManager:
    """Manages backend lifecycle, health monitoring, and restart logic."""

    def __init__(
        self,
        *,
        player: Any,  # MusicPlayer - typed as Any to access private attrs
        backend_loader: BackendStrategyLoader,
        telemetry: RuntimeMetrics | None,
        logger: logging.Logger,
        pending_start_timeout: float,
    ) -> None:
        self._player = player
        self._backend_loader = backend_loader
        self._telemetry = telemetry
        self._logger = logger.getChild("backend_lifecycle")
        self._restart_lock = threading.Lock()
        self._last_restart_at = 0.0
        self._pending_start_timeout = pending_start_timeout
        self._pending_start_deadline: float | None = None
        self._pending_start_item_id: str | None = None
        self._allow_test_stream_playback = False
        self._manual_backend_override_ref: BaseBackend | None = None
        self._manual_backend_override_active = False
        self._default_backend_cls: type | None = None
        self._backend_silence_since: float | None = None
        # Track whether the current track has actually started playing.
        # This prevents silence detection from skipping tracks that never started.
        self._track_has_reached_playing: bool = False

    # Properties

    @property
    def backend(self) -> BaseBackend | None:
        return self._player._backend

    @property
    def backend_name(self) -> str | None:
        return getattr(self._player, "_backend_name", None)

    @property
    def manual_override_active(self) -> bool:
        return self._manual_backend_override_active

    @property
    def manual_override_backend(self) -> BaseBackend | None:
        return self._manual_backend_override_ref

    @property
    def test_stream_playback_allowed(self) -> bool:
        return self._allow_test_stream_playback

    @property
    def pending_start_deadline(self) -> float | None:
        return self._pending_start_deadline

    @property
    def pending_start_item_id(self) -> str | None:
        return self._pending_start_item_id

    # Public API

    def init_backend(self) -> None:
        with self._player._lock:
            # Use centralized settings for embedded_only flag
            embedded_only = settings.embedded_only

            load_result = self._backend_loader.load(
                BackendLoadRequest(
                    backend_name=self._player._backend_name,
                    backend_factory=self._player._backend_factory,
                    embedded_only=embedded_only,
                )
            )
            backend = load_result.backend
            self._player._backend_name = load_result.backend_name
            self._adopt_backend(backend, backend_name=load_result.backend_name)
            if backend is None:
                self._logger.error("Backend loader returned no backend instance")
                return
            try:
                self._player._state.volume = backend.set_volume(self._player._state.volume)
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning("Failed to set initial volume on backend: %r", exc)
            self._configure_backend_state(backend)

    def apply_test_backend_override(self, backend: BaseBackend) -> None:
        """Inject test backend for deterministic playback."""

        if backend is None:
            raise ValueError("Test backend override requires a backend instance")

        with self._player._lock:
            self._player._backend = backend
            self._player._backend_name = type(backend).__name__
            self._player._configure_backend_state(backend)

    def restart_backend(self, *, reason: str) -> None:
        with self._restart_lock:
            now_ts = time.time()
            if now_ts - self._last_restart_at < 2.0:
                return
            self._last_restart_at = now_ts
            backend_to_cleanup = self.backend
            if backend_to_cleanup and hasattr(backend_to_cleanup, "cleanup"):
                try:
                    backend_to_cleanup.cleanup()
                except Exception as e:
                    self._logger.exception("Backend cleanup during restart failed (non-critical): %s", e)
            self._adopt_backend(None)
            try:
                self.init_backend()
            except Exception as exc:
                self._logger.error("Backend restart failed: %r", exc)
                if self._telemetry is not None:
                    self._telemetry.record_music_backend_restart(reason=f"{reason}_init_failed")
                    self._telemetry.record_failure(
                        "music_backend_restart_failed",
                        "music.backend",
                        message="Backend restart failed during initialization",
                    )
                return
            self._player._backend_restart_count += 1
            if self._telemetry is not None:
                self._telemetry.record_music_backend_restart(reason=reason)

        resume_position = 0
        with self._player._lock:
            resume_position = self._player._state.position
        with self._player._cv:
            current_item = self._player._playlist.current()
            backend = self.backend
        if backend is None or current_item is None:
            return
        if not current_item.url:
            self._logger.warning(
                "Unable to restart backend for %s: queue item missing resolved URL",
                current_item.id,
            )
            if self._telemetry is not None:
                self._telemetry.record_failure(
                    "music_backend_restart_missing_url",
                    "music.backend",
                    message="Backend restart aborted due to missing URL",
                )
            return
        try:
            self._prepare_backend_for_track(current_item)
            backend.play_url(current_item.url)
            if resume_position > 0 and hasattr(backend, "seek"):
                try:
                    backend.seek(resume_position)
                except Exception as e:
                    self._logger.exception(
                        "Failed to restore playback position after restart (non-critical): %s",
                        e,
                    )
            playing_now = False
            try:
                playing_now = backend.is_playing()
            except Exception as e:
                self._logger.debug("is_playing check after restart failed (non-critical): %s", e)
                playing_now = False
            with self._player._lock:
                self._player._state.is_playing = playing_now
        except Exception as exc:
            self._logger.error("Backend restart playback failed for %s: %r", current_item.id, exc)
            self._player._record_queue_failure(
                current_item,
                stage="backend_restart_failure",
                metadata={"reason": reason, "error": repr(exc)},
            )
            with self._player._cv:
                self._player._pending_skip_tokens += 1
                self._player._cv.notify_all()

    def evaluate_backend_health(self, backend: BaseBackend) -> dict[str, Any] | None:
        health_check = getattr(backend, "health_check", None)
        if not callable(health_check):
            return None
        try:
            health = health_check()
        except Exception as exc:
            self._logger.exception("Backend health check failed: %s", exc)
            return {
                "status": "error",
                "reason": "health_check_exception",
                "exception_type": exc.__class__.__name__,
                "error": repr(exc),
            }

        if isinstance(health, dict):
            status = str(health.get("status", "")).lower()
            if status in {"error", "failed", "fatal", "unhealthy"}:
                return health
            return None

        if isinstance(health, str):
            status = health.lower()
            if status not in {"ok", "healthy", "ready"}:
                return {"status": "error", "reason": health}
            return None

        if health is False:
            return {"status": "error", "reason": "health_check_false"}

        return None

    def perform_integrity_pass(self) -> None:
        snapshot = self._player.state()
        now_ts = time.time()
        with self._player._lock:
            playlist_current = self._player._playlist.current()
            playlist_current_id = self._player._playlist.current_id()
            upcoming_count = self._player._playlist.upcoming_count()
            backend = self.backend
            user_paused = self._player._user_paused
            last_emit_at = self._player._last_emitted_at
        expected_queue_len = len(snapshot.queue) + (1 if snapshot.now_playing is not None else 0)
        actual_queue_len = upcoming_count + (1 if playlist_current_id else 0)
        drift = actual_queue_len - expected_queue_len
        now_playing_id = getattr(snapshot.now_playing, "id", None) if snapshot.now_playing is not None else None
        backend_healthy = True
        health_error = None
        if backend is not None:
            health_error = self.evaluate_backend_health(backend)
            backend_healthy = health_error is None

        need_emit = False
        if drift != 0:
            self._logger.warning(
                "Queue drift detected (expected=%s actual=%s drift=%s)",
                expected_queue_len,
                actual_queue_len,
                drift,
            )
            if self._telemetry is not None:
                self._telemetry.record_queue_drift(
                    expected_queue_len=expected_queue_len,
                    actual_queue_len=actual_queue_len,
                    now_playing_track_id=now_playing_id,
                )
            need_emit = True

        if playlist_current is not None and not user_paused:
            backend_playing = False
            try:
                backend_playing = backend.is_playing() if backend else False
            except Exception as e:
                self._logger.debug(
                    "is_playing check during integrity pass failed (non-critical): %s",
                    e,
                )
                backend_playing = False
            if not backend_playing:
                # Check if we're still within the pending start window
                # (video is loading/buffering, not actually silent)
                within_pending_start = (
                    self._pending_start_deadline is not None and now_ts < self._pending_start_deadline
                )
                if within_pending_start:
                    # Reset silence timer - we're still waiting for track to start
                    self._backend_silence_since = None
                    self._logger.debug(
                        "Backend not playing but within pending start window (%.1fs remaining)",
                        self._pending_start_deadline - now_ts,
                    )
                elif not self._track_has_reached_playing:
                    # CRITICAL FIX: Track has NEVER started playing.
                    # Do NOT trigger silence detection for tracks that never started.
                    # Wait for YouTube's onError callback or a much longer timeout instead.
                    # This fixes the bug where music videos were being skipped after 8s
                    # because they never transitioned out of UNSTARTED state.
                    self._logger.debug(
                        "Backend not playing but track never reached PLAYING state - "
                        "waiting for YouTube onError or explicit error, not triggering silence skip"
                    )
                    self._backend_silence_since = None  # Don't accumulate silence time
                elif self._backend_silence_since is None:
                    self._backend_silence_since = now_ts
                elif now_ts - self._backend_silence_since > getattr(
                    self._player._playback_cfg, "BACKEND_SILENCE_MAX_SEC", 8
                ):
                    # Track DID start playing but has now gone silent - this is a valid skip trigger
                    self._player._logger.error(
                        "Backend silent for %.2fs after track started playing; skipping track",
                        now_ts - self._backend_silence_since,
                    )
                    if self._telemetry is not None:
                        self._telemetry.record_music_buffer_underrun()
                    with self._player._cv:
                        self._player._pending_skip_tokens += 1
                        self._player._cv.notify_all()
                    need_emit = True
                    self._backend_silence_since = None
            else:
                self._backend_silence_since = None
        else:
            self._backend_silence_since = None

        if not backend_healthy and playlist_current is not None:
            source = "backend"
            if health_error is not None:
                source = f"{source}_health"
                self._logger.warning("Backend health degraded: %s", health_error)
            if self._telemetry is not None:
                self._telemetry.record_music_heartbeat_miss(source=source)
            self.restart_backend(reason="backend_health_check")
            need_emit = True
        if need_emit or now_ts - last_emit_at > 5.0:
            self._player._emit()

    # Backend state helpers

    def handle_backend_assignment(self, backend: BaseBackend | None) -> None:
        if backend is None:
            self._manual_backend_override_ref = None
            self._manual_backend_override_active = False
            return
        if self._default_backend_cls is None:
            self._default_backend_cls = type(backend)
            self._manual_backend_override_ref = None
            self._manual_backend_override_active = False
            return
        if not isinstance(backend, self._default_backend_cls):
            self._manual_backend_override_ref = backend
            self._manual_backend_override_active = True
        else:
            self._manual_backend_override_ref = None
            self._manual_backend_override_active = False

    def allow_test_stream_playback(self, enabled: bool) -> None:
        if not getattr(self._player, "_test_mode", False):
            raise RuntimeError("allow_test_stream_playback is only available in test mode")
        self._allow_test_stream_playback = bool(enabled)

    def use_test_backend(self, backend: BaseBackend) -> None:
        if not getattr(self._player, "_test_mode", False):
            raise RuntimeError("use_test_backend is only supported in test mode")
        self._manual_backend_override_active = True
        self._manual_backend_override_ref = backend
        self._adopt_backend(backend, backend_name=type(backend).__name__)

    def schedule_pending_start(self, item_id: str | None, *, timeout: float | None = None) -> None:
        effective_timeout = timeout or self._pending_start_timeout
        self._pending_start_deadline = time.time() + max(0.0, effective_timeout)
        self._pending_start_item_id = item_id
        # Reset the "track has reached playing" flag when a new track starts.
        # This ensures silence detection won't trigger for tracks that never started.
        self._track_has_reached_playing = False
        self._backend_silence_since = None

    def clear_pending_start(self) -> None:
        self._pending_start_deadline = None
        self._pending_start_item_id = None

    def mark_track_started_playing(self) -> None:
        """Mark that the current track has successfully reached PLAYING state.

        This is called when YouTube reports player_state == PLAYING.
        Once set, silence detection becomes active for this track.
        This prevents skipping tracks that never started playing.
        """
        if not self._track_has_reached_playing:
            self._logger.info("Track reached PLAYING state - silence detection now active")
        self._track_has_reached_playing = True
        # Clear the pending start deadline since we're now playing
        self.clear_pending_start()

    @property
    def track_has_reached_playing(self) -> bool:
        """Whether the current track has reached PLAYING state."""
        return self._track_has_reached_playing

    def prepare_backend_for_track(self, item: QueueItem) -> None:
        self._prepare_backend_for_track(item)

    def configure_backend_state(self, backend: BaseBackend) -> None:
        self._configure_backend_state(backend)

    def extract_backend_capabilities(self, backend: BaseBackend) -> dict[str, Any]:
        return self._extract_backend_capabilities(backend)

    def stop_backend_locked(self) -> None:
        engine_manager = getattr(self._player, "_engine_manager", None)
        if engine_manager is not None:
            try:
                engine_manager.stop_active()
            except Exception as exc:
                self._logger.debug("Engine manager stop failed: %r", exc)
        backend = self.backend
        if backend is None:
            return
        try:
            backend.stop()
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("Backend stop failed: %r", exc)

    def stop_backend(self) -> None:
        with self._player._lock:
            self.stop_backend_locked()

    def set_backend_volume(self, level: int) -> int:
        backend = self.backend
        if backend is None:
            return level
        try:
            return backend.set_volume(level)
        except Exception as exc:
            self._logger.warning("Failed to set backend volume: %r", exc)
            return level

    def pause_backend(self) -> bool:
        backend = self.backend
        if backend is None:
            return False
        try:
            backend.pause()
            return True
        except Exception as exc:
            self._logger.warning("Backend pause failed: %r", exc)
            return False

    def resume_backend(self) -> bool:
        backend = self.backend
        if backend is None:
            return False
        try:
            backend.resume()
            return True
        except Exception as exc:
            self._logger.warning("Backend resume failed: %r", exc)
            return False

    def backend_is_playing(self) -> bool:
        backend = self.backend
        if backend is None:
            return False
        try:
            return bool(backend.is_playing())
        except Exception as e:
            self._logger.exception("Backend is_playing check failed: %s", e)
            return False

    def snapshot_backend_position(self) -> tuple[int, int, float]:
        backend = self.backend
        if backend is None:
            return (0, 0, 0.0)
        position = 0
        duration = 0
        percentage = 0.0
        try:
            if hasattr(backend, "get_position"):
                position = backend.get_position() or 0
            if hasattr(backend, "get_duration"):
                duration = backend.get_duration() or 0
            if hasattr(backend, "get_position_percentage"):
                percentage = backend.get_position_percentage() or 0.0
            elif duration:
                percentage = position / duration if duration else 0.0
        except Exception as e:
            self._logger.exception("Backend position query failed: %s", e)
            pass  # Silent OK: progress query failure returns defaults
        return int(position), int(duration), float(percentage)

    def _adopt_backend(
        self,
        backend: BaseBackend | None,
        *,
        backend_name: str | None = None,
    ) -> None:
        player = self._player
        self._player._backend = backend
        if backend is None:
            return
        self._player._backend_name = backend_name or type(backend).__name__
        self.handle_backend_assignment(backend)

        # Re-wire video output if previously injected
        video_widget = getattr(player, "_video_widget_ref", None)
        if video_widget is not None and hasattr(backend, "set_video_output"):
            backend.set_video_output(video_widget)

    def _prepare_backend_for_track(self, item: QueueItem) -> None:
        backend = self.backend
        if backend is None:
            return
        duration_hint: float | None = None
        capabilities = getattr(item, "capabilities", None)
        if isinstance(capabilities, dict):
            raw_duration = capabilities.get("duration_seconds") or capabilities.get("duration")
            if raw_duration is not None:
                try:
                    duration_hint = max(0.0, float(raw_duration))
                except (TypeError, ValueError):
                    duration_hint = None
        if hasattr(backend, "update_expected_duration") and duration_hint is not None:
            try:
                backend.update_expected_duration(int(duration_hint))
            except Exception:  # pragma: no cover - defensive
                self._logger.exception("Backend update_expected_duration failed")

    def _configure_backend_state(self, backend: BaseBackend) -> None:
        state = self._player._state
        if state.playback_mode == "external_browser":
            self._logger.debug(
                "Skipping backend state configuration - external_browser mode is active (backend=%s)",
                type(backend).__name__,
            )
            return

        from music.backends.embedded_backend import EmbeddedPlayerBackend
        from music.backends.youtube_web_backend import YouTubeWebBackend

        if isinstance(backend, YouTubeWebBackend):
            state.backend = "youtube_web"
            state.backend_display_name = "YouTube Music (Embedded)"
            self._logger.info(
                "Backend state configured: youtube_web (playback_mode=%s)",
                state.playback_mode,
            )
        elif isinstance(backend, EmbeddedPlayerBackend):
            state.backend = "embedded"
            state.backend_display_name = "Embedded Player"
            self._logger.info(
                "Backend state configured: embedded (playback_mode=%s)",
                state.playback_mode,
            )
        elif hasattr(backend, "_engine") and hasattr(backend, "_manager"):
            state.backend = "embedded"
            engine = getattr(backend, "_engine", None)
            if engine:
                state.backend_display_name = getattr(engine, "display_name", type(backend).__name__)
            else:
                state.backend_display_name = type(backend).__name__
        else:
            state.backend = self._player._backend_name
            state.backend_display_name = type(backend).__name__

        capabilities = self._extract_backend_capabilities(backend)
        if capabilities:
            state.backend_capabilities = capabilities
            merged = dict(state.playback_capabilities)
            merged.update(capabilities)
            state.playback_capabilities = merged

        if hasattr(backend, "set_progress_listener"):
            try:
                backend.set_progress_listener(self._player._on_backend_progress)
            except Exception:  # pragma: no cover - defensive
                self._logger.exception("Failed to attach backend progress listener")

    def _extract_backend_capabilities(self, backend: BaseBackend) -> dict[str, Any]:
        if not hasattr(backend, "capabilities"):
            return {}
        try:
            caps = backend.capabilities()
        except Exception:  # pragma: no cover - defensive
            self._logger.exception("Backend capabilities() raised")
            return {}
        if caps is None:
            return {}
        if dataclasses.is_dataclass(caps) and not isinstance(caps, type):
            return dataclasses.asdict(caps)
        if isinstance(caps, dict):
            return dict(caps)
        return {}


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass
