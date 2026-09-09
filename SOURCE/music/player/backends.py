"""
Music Player Backend Management.

This module contains backend management operations including:
- Backend selection and loading
- Backend lifecycle management
- Backend health evaluation
- Backend capability extraction

Consolidated from: music_player_backend_management.py
"""

from __future__ import annotations

import time
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class MusicPlayerBackendManager:
    """Handles backend selection, loading, and management for the music player."""

    def __init__(self, player_instance):
        """
        Initialize backend manager.

        Args:
            player_instance: The MusicPlayer instance
        """
        self.player = player_instance
        self._pending_start_deadline: float | None = None
        self._pending_start_item_id: str | None = None
        self._pending_start_timeout: float = 10.0

    @property
    def pending_start_deadline(self) -> float | None:
        """Public accessor for pending start deadline (matches BackendLifecycleManager API)."""
        return self._pending_start_deadline

    def load_and_start_backend(self, url: str) -> None:
        """
        Load appropriate backend and start playback.

        Args:
            url: URL to play
        """
        try:
            # Select backend strategy
            strategy = self._select_backend_strategy(url)

            # Load backend
            backend = self._load_backend(strategy)

            # Configure backend
            self._configure_backend(backend, url)

            # Start playback
            backend.play_url(url)

            # Store backend reference
            self.player._backend = backend

            logger.info("Loaded and started backend %s for URL: %s", type(backend).__name__, url)

        except Exception:
            logger.exception("Failed to load and start backend for %s", url)
            raise

    def configure_backend_state(self, backend: Any = None) -> None:
        """Configure backend with current player state."""
        try:
            target_backend = backend if backend is not None else getattr(self.player, "_backend", None)
            if target_backend is None:
                return

            # Set volume
            if hasattr(self.player, "_volume") and hasattr(target_backend, "set_volume"):
                target_backend.set_volume(self.player._volume)

            # Set any other state
            # (Additional configuration can be added here)

            logger.debug("Backend state configured")

        except Exception:
            logger.exception("Failed to configure backend state")
            # Don't raise - configuration failures shouldn't stop playback

    def use_test_backend(self, backend_type: str = "simple") -> None:
        """
        Switch to test backend for testing.

        Args:
            backend_type: Type of test backend to use
        """
        try:
            # This would load a test-specific backend
            # Implementation depends on test setup
            logger.info("Switched to test backend: %s", backend_type)

        except Exception:
            logger.exception("Failed to switch to test backend %s", backend_type)
            raise

    def _select_backend_strategy(self, url: str) -> str:
        """
        Select appropriate backend strategy for URL.

        Args:
            url: URL to analyze

        Returns:
            Backend strategy name
        """
        try:
            # Check if URL is YouTube
            if "youtube.com" in url or "youtu.be" in url:
                return "youtube_iframe"
            # Default to simple
            return "simple"

        except Exception as e:
            # Fallback to simple strategy
            logger.exception("Failed to select backend strategy: %s", e)
            return "simple"

    def _load_backend(self, strategy: str) -> Any:
        """
        Load backend instance for strategy.

        Args:
            strategy: Backend strategy name

        Returns:
            Backend instance
        """
        try:
            from music.backends.loader import BackendLoadRequest, BackendStrategyLoader

            loader = BackendStrategyLoader(logger=logger)
            load_result = loader.load(
                BackendLoadRequest(
                    backend_name=strategy,
                    backend_factory=None,
                    embedded_only=False,
                )
            )

            return load_result.backend

        except Exception:
            logger.exception("Failed to load backend for strategy %s", strategy)
            raise

    def _configure_backend(self, backend: Any, url: str) -> None:
        """
        Configure backend for playback.

        Args:
            backend: Backend instance
            url: URL being played
        """
        try:
            # Set volume
            if hasattr(self.player, "_volume"):
                backend.set_volume(self.player._volume)

            # Set progress callback
            if hasattr(self.player, "_emit"):
                # Create a callback that emits state changes
                def progress_callback(progress):
                    try:
                        # Update position
                        if hasattr(progress, "position_ms"):
                            self.player._position_ms = progress.position_ms

                        # Emit state change
                        self.player._emit()
                    except Exception:
                        logger.exception("Progress callback error")

                backend.set_progress_listener(progress_callback)

            # Additional configuration based on URL type
            if "youtube.com" in url or "youtu.be" in url:
                # YouTube-specific configuration
                pass

            logger.debug("Backend configured for URL: %s", url)

        except Exception:
            logger.exception("Failed to configure backend")
            # Don't raise - continue with default configuration

    def restart_backend(self, reason: str = "") -> None:
        """
        Restart the backend with rate limiting and proper cleanup.

        Args:
            reason: Reason for the restart
        """
        try:
            # Rate limiting - don't restart too frequently
            now_ts = time.time()
            if hasattr(self.player, "_backend_restart_lock") and hasattr(self.player, "_last_backend_restart_at"):
                with self.player._backend_restart_lock:
                    if now_ts - self.player._last_backend_restart_at < 2.0:
                        return
                    self.player._last_backend_restart_at = now_ts
            elif hasattr(self.player, "_last_backend_restart_at"):
                if now_ts - self.player._last_backend_restart_at < 2.0:
                    return
                self.player._last_backend_restart_at = now_ts

            # Cleanup old backend
            backend_to_cleanup = getattr(self.player, "_backend", None)
            if backend_to_cleanup and hasattr(backend_to_cleanup, "cleanup"):
                try:
                    backend_to_cleanup.cleanup()
                except Exception:
                    logger.exception("Backend cleanup during restart failed")

            # Clear current backend
            self.player._backend = None

            # Initialize new backend
            try:
                self.initialize_backend()
            except Exception as exc:
                logger.error("Backend restart failed: %r", exc)

                # Record telemetry
                try:
                    if hasattr(self.player, "_telemetry"):
                        self.player._telemetry.record_backend_restart(reason=f"{reason}_init_failed")
                except Exception as e:
                    logger.exception("Failed to record telemetry (non-critical): %s", e)

                raise

            # Record successful restart
            try:
                if hasattr(self.player, "_telemetry"):
                    self.player._telemetry.record_backend_restart(reason=reason)
            except Exception as e:
                logger.exception("Failed to record telemetry (non-critical): %s", e)

            logger.info("Backend restarted successfully, reason: %s", reason)

        except Exception:
            logger.exception("Backend restart failed")
            raise

    def evaluate_backend_health(self, backend) -> dict[str, Any] | None:
        """
        Return health error metadata if backend reports an unhealthy state.

        Args:
            backend: The backend to evaluate

        Returns:
            Health error metadata dict if unhealthy, None if healthy
        """
        health_check = getattr(backend, "health_check", None)
        if not callable(health_check):
            return None
        try:
            health = health_check()
        except Exception as exc:
            logger.exception("Backend health check failed: %s", exc)
            return {
                "status": "error",
                "reason": "health_check_exception",
                "exception_type": exc.__class__.__name__,
                "error": repr(exc),
            }

        if isinstance(health, dict):
            status = str(health.get("status", "")).lower()
            if status in ("error", "unhealthy", "fail", "failed"):
                return health
            elif status in ("ok", "healthy", "good", "success"):
                return None
            else:
                # Unknown status - assume healthy
                return None
        elif health is True:
            return None
        elif health is False:
            return {"status": "error", "reason": "health_check_false"}
        else:
            # Unknown health type - assume healthy
            return None

    def attach_backend_listeners(self, backend) -> None:
        """
        Attach backend listeners and refresh backend capability state.

        Args:
            backend: The backend to attach listeners to
        """
        # Update backend identifiers and capabilities on state
        with self.player._cv:
            try:
                self.player._state.backend = getattr(self.player, "_backend_name", type(backend).__name__)
                self.player._state.backend_display_name = type(backend).__name__
            except Exception as e:
                logger.exception("Failed to update backend state (non-critical): %s", e)

            # Extract and update capabilities
            caps = self.extract_backend_capabilities(backend)
            if caps:
                self.player._state.backend_capabilities = caps

        # Attach progress listener if supported
        if hasattr(backend, "set_progress_listener"):
            try:
                backend.set_progress_listener(self.player._on_backend_progress)
            except Exception as exc:
                logger.debug("Failed to attach progress listener: %r", exc)

        # Attach state change listener if supported
        if hasattr(backend, "set_state_change_listener"):
            try:
                backend.set_state_change_listener(self.player._emit)
            except Exception as exc:
                logger.debug("Failed to attach state change listener: %r", exc)

        # Attach error listener if supported
        if hasattr(backend, "set_error_listener"):
            try:
                backend.set_error_listener(self.player._on_backend_error)
            except Exception as exc:
                logger.debug("Failed to attach error listener: %r", exc)

    def extract_backend_capabilities(self, backend) -> dict[str, Any]:
        """
        Extract capabilities from backend.

        Args:
            backend: The backend to extract capabilities from

        Returns:
            Dict of backend capabilities
        """
        caps = {}

        # Volume control
        caps["volume_control"] = hasattr(backend, "set_volume") and callable(getattr(backend, "set_volume", None))

        # Seek support
        caps["seek"] = hasattr(backend, "seek") and callable(getattr(backend, "seek", None))

        # Pause/Resume
        caps["pause_resume"] = (hasattr(backend, "pause") and callable(getattr(backend, "pause", None))) and (
            hasattr(backend, "resume") and callable(getattr(backend, "resume", None))
        )

        # Stop
        caps["stop"] = hasattr(backend, "stop") and callable(getattr(backend, "stop", None))

        # Position/duration reporting
        caps["position_duration"] = hasattr(backend, "current_position_ms") and callable(
            getattr(backend, "current_position_ms", None)
        )

        # Gapless playback
        caps["gapless"] = getattr(backend, "supports_gapless", False)

        # Streaming
        caps["streaming"] = getattr(backend, "supports_streaming", True)

        return caps

    # ------------------------------------------------------------------ #
    # Backend lifecycle methods (required by MusicPlayer)                #
    # ------------------------------------------------------------------ #

    def stop_backend_locked(self) -> None:
        """Stop the current backend.

        Despite the ``_locked`` name this must NOT be called while holding
        ``player._lock``. Backend ``stop()`` implementations join their own
        background threads (SimpleBackend joins its playback thread,
        SpotifyCDPEngine joins its poll thread) and those threads call back into
        the player -- SimpleBackend's playback thread reaches
        ``MusicPlayerStateManager.handle_backend_progress``, which takes
        ``player._lock``. Holding that lock across the join is an AB-BA
        deadlock, broken only when the join times out, so every stop pays the
        full timeout on a perfectly healthy device.

        ``MusicRuntimeControlSurface.stop`` already established the correct
        shape: mutate player state under the lock, then stop the backend
        outside it.
        """
        # Stop engine manager if present
        engine_manager = getattr(self.player, "_engine_manager", None)
        if engine_manager is not None:
            try:
                engine_manager.stop_active()
            except Exception as exc:
                logger.debug("Engine manager stop failed: %r", exc)

        # Stop the backend itself
        backend = getattr(self.player, "_backend", None)
        if backend is None:
            return
        try:
            backend.stop()
        except Exception as exc:
            logger.debug("Backend stop failed: %r", exc)

    def stop_backend(self) -> None:
        """Stop the current backend.

        Deliberately does NOT hold ``player._lock`` across the stop -- see
        :meth:`stop_backend_locked` for why that is a deadlock. Nothing on the
        player is mutated here, so there is no state for the lock to protect.
        """
        self.stop_backend_locked()

    def pause_backend(self) -> None:
        """Pause the current backend."""
        backend = getattr(self.player, "_backend", None)
        if backend is None:
            return
        try:
            backend.pause()
        except Exception as exc:
            logger.debug("Backend pause failed: %r", exc)

    def resume_backend(self) -> None:
        """Resume the current backend."""
        backend = getattr(self.player, "_backend", None)
        if backend is None:
            return
        try:
            backend.resume()
        except Exception as exc:
            logger.debug("Backend resume failed: %r", exc)

    def clear_pending_start(self) -> None:
        """Clear any pending start deadline."""
        self._pending_start_deadline = None
        self._pending_start_item_id = None

    def schedule_pending_start(self, item_id: str | None, *, timeout: float | None = None) -> None:
        """Schedule a pending start with timeout."""
        effective_timeout = timeout or self._pending_start_timeout
        self._pending_start_deadline = time.time() + max(0.0, effective_timeout)
        self._pending_start_item_id = item_id

    def init_backend(self) -> None:
        """Initialize the backend using backend loader."""
        try:
            from music.backends.loader import BackendLoadRequest, BackendStrategyLoader

            loader = BackendStrategyLoader(logger=logger)
            backend_name = getattr(self.player, "_backend_name", "simple")
            backend_factory = getattr(self.player, "_backend_factory", None)

            load_result = loader.load(
                BackendLoadRequest(
                    backend_name=backend_name,
                    backend_factory=backend_factory,
                    embedded_only=False,
                )
            )
            backend = load_result.backend
            if backend is not None:
                self.player._backend = backend
                self.player._backend_name = load_result.backend_name
                self.configure_backend_state(backend)
                logger.info("Backend initialized: %s", load_result.backend_name)
            else:
                logger.warning("Backend loader returned no backend instance")
        except Exception:
            logger.exception("Failed to initialize backend")

    def prepare_backend_for_track(self, item: Any) -> None:
        """Prepare backend for playing a specific track."""
        # Set pending start deadline
        self._pending_start_deadline = time.time() + self._pending_start_timeout
        self._pending_start_item_id = getattr(item, "id", None)

    def set_backend_volume(self, level: int) -> int:
        """Set backend volume level."""
        backend = getattr(self.player, "_backend", None)
        if backend is None:
            return level
        try:
            return backend.set_volume(level)
        except Exception as exc:
            logger.warning("Failed to set backend volume: %r", exc)
            return level

    def initialize_backend(self) -> None:
        """Alias for init_backend (used by restart_backend)."""
        self.init_backend()

    @property
    def backend(self) -> Any:
        """Get current backend instance."""
        return getattr(self.player, "_backend", None)

    def handle_backend_assignment(self, backend: Any) -> None:
        """Handle backend assignment from player."""
        # Track backend assignments for debugging
        assignments = getattr(self.player, "_backend_assignments", {})
        if backend is not None:
            assignments[type(backend).__name__] = time.time()
        logger.debug("Backend assigned: %s", type(backend).__name__ if backend else None)
