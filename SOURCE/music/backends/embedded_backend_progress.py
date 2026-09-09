"""
Embedded Backend Progress Management - Extracted from embedded_backend.py

Handles progress reporting and playback verification logic.
"""

from __future__ import annotations

import threading
import time

from core.constants import TIMEOUT_MEDIUM
from core.logging_config import get_logger

logger = get_logger(__name__)


class EmbeddedBackendProgressManager:
    """Manages progress reporting and playback verification for embedded backend."""

    def __init__(self, backend_instance):
        self.backend = backend_instance
        self._progress_stop = threading.Event()
        self._progress_thread: threading.Thread | None = None

    def start_progress_pump(self) -> None:
        """Start progress reporting thread."""
        self.stop_progress_pump()
        self._progress_stop.clear()
        self._progress_thread = threading.Thread(target=self._progress_loop, name="EmbeddedProgress", daemon=True)
        self._progress_thread.start()

    def stop_progress_pump(self) -> None:
        """Stop progress reporting thread."""
        self._progress_stop.set()
        if self._progress_thread and self._progress_thread.is_alive():
            self._progress_thread.join(timeout=TIMEOUT_MEDIUM)
        self._progress_thread = None

    def _progress_loop(self) -> None:
        """Progress reporting loop with playback verification."""
        max_retries = 5
        retry_count = 0
        backoff = 1.0

        while retry_count < max_retries and not self._progress_stop.is_set():
            try:
                self._progress_loop_inner()
                break  # Clean exit (stop event set)
            except Exception:
                retry_count += 1
                logger.exception(
                    "EmbeddedProgress thread crashed (attempt %d/%d)",
                    retry_count,
                    max_retries,
                )
                if retry_count >= max_retries:
                    logger.error(
                        "EmbeddedProgress thread exhausted %d retries, giving up",
                        max_retries,
                    )
                    break
                self._progress_stop.wait(backoff)
                backoff = min(backoff * 2, 8.0)

    def _progress_loop_inner(self) -> None:
        """Inner progress loop body, separated for retry wrapper."""
        interval = 0.25  # Update every 250ms
        consecutive_failures = 0
        max_failures = 12  # 12 * 0.25s = 3 seconds of no progress
        last_position_ms = None
        verification_started = False

        while not self._progress_stop.wait(interval):
            if self.backend._test_mode:
                self._handle_test_mode_progress()
            else:
                success = self._handle_real_mode_progress(
                    consecutive_failures,
                    max_failures,
                    last_position_ms,
                    verification_started,
                )
                if success:
                    consecutive_failures = 0
                    last_position_ms = self.backend.current_position_ms()
                else:
                    consecutive_failures += 1

                # Check for too many consecutive failures
                if consecutive_failures >= max_failures:
                    self._handle_playback_failure()

    def _handle_test_mode_progress(self) -> None:
        """Handle progress updates in test mode."""
        if self.backend._is_playing and not self.backend._is_paused:
            # Increment position by interval (250ms = 0.25s = 250ms)
            if self.backend._position_ms is None:
                self.backend._position_ms = 0
            self.backend._position_ms += 250

            # Set a default duration if not set (e.g., 3 minutes)
            if self.backend._duration_ms is None:
                self.backend._duration_ms = 180000  # 3 minutes

            # Cap position at duration
            if self.backend._position_ms > self.backend._duration_ms:
                self.backend._position_ms = self.backend._duration_ms
                self.backend._is_playing = False
                if self.backend._signals:
                    self.backend._signals.playback_finished.emit()
                return

    def _handle_real_mode_progress(
        self,
        consecutive_failures: int,
        max_failures: int,
        last_position_ms: int | None,
        verification_started: bool,
    ) -> bool:
        """Handle progress updates in real mode."""
        # Check if player is actually ready and playing
        position_ms = self.backend.current_position_ms()

        # Start verification after initial load delay
        if not verification_started and (self.backend._pending_start or self.backend._is_playing):
            if not hasattr(self.backend, "_verification_start_time"):
                self.backend._verification_start_time = time.time()
            elif time.time() - self.backend._verification_start_time >= 2.0:
                verification_started = True

        # Verify playback is actually happening
        should_verify = (
            verification_started
            and not self.backend._is_paused
            and (self.backend._pending_start or self.backend._is_playing)
        )

        if should_verify:
            player_ready = self._check_player_ready()
            if player_ready or (position_ms and position_ms > 0):
                self._mark_playback_verified()

            if position_ms is None or position_ms == 0:
                # Position stuck at 0 or unavailable
                return False

        return position_ms is not None and position_ms != last_position_ms

    def _check_player_ready(self) -> bool:
        """Check if the YouTube player is ready."""
        try:
            # Try to get player state via JavaScript
            if self.backend._webview:
                # This would be implemented with actual JavaScript calls
                return False  # Placeholder
        except Exception as e:  # Silent OK: JS execution may fail if webview not ready
            logger.exception("JS execution failed (webview not ready): %s", e)
            pass
        return False

    def _mark_playback_verified(self) -> None:
        """Mark that playback has been verified as actually working."""
        if self.backend._pending_start:
            self.backend._pending_start = False
            self.backend._is_playing = True
            if self.backend._signals:
                self.backend._signals.playback_started.emit()
            logger.info("EMBEDDED_BACKEND: Playback verified and started")

    def _handle_playback_failure(self) -> None:
        """Handle detected playback failure."""
        logger.warning("EMBEDDED_BACKEND: Playback verification failed - stopping")
        self.backend._is_playing = False
        self.backend._is_paused = False
        self.backend._pending_start = False

        if self.backend._signals:
            self.backend._signals.playback_error.emit("Playback failed to start")
            self.backend._signals.playback_finished.emit()
