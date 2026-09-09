"""
Audio ducking - automatically reduce music volume during speech recognition and TTS.

This helps Viola hear you better by reducing background music volume when:
1. Listening for a command (STT active)
2. Speaking a response (TTS active)
"""

from __future__ import annotations

import threading
import time
from typing import Any, Literal

from core.constants import TIMEOUT_MEDIUM
from core.logging_config import get_logger

logger = get_logger(__name__)


class AudioDucker:
    """
    Manages automatic audio ducking (volume reduction) during voice interaction.

    Features:
    - Smooth volume transitions (fade in/out)
    - Nested ducking support (multiple concurrent duck requests)
    - Automatic restoration when all requests complete
    - Thread-safe operation
    - Timeout safety net (auto-unduck after max duration)
    """

    # Maximum time audio can stay ducked before auto-restoring (safety net)
    MAX_DUCK_DURATION = 60.0  # 60 seconds

    def __init__(self, music_player: Any, duck_level: int = 20, fade_duration: float = 0.3):
        """
        Initialize audio ducker.

        Args:
            music_player: MusicPlayer instance with volume control
            duck_level: Target volume level when ducked (0-100)
            fade_duration: Time in seconds for fade transitions
        """
        self.music_player = music_player
        self.duck_level = max(0, min(100, duck_level))
        self.fade_duration = max(0.1, fade_duration)

        self._lock = threading.Lock()
        self._original_volume: int | None = None
        self._duck_count = 0  # Number of active duck requests
        self._fade_thread: threading.Thread | None = None
        self._fade_cancel = threading.Event()
        self._timeout_timer: threading.Timer | None = None

        logger.info(
            "AudioDucker initialized: duck_level=%s, fade_duration=%ss",
            self.duck_level,
            self.fade_duration,
        )

    def duck(self) -> None:
        """
        Duck (lower) the audio volume.
        Can be called multiple times (nested) - volume restored only when all ducks end.
        """
        import threading as th

        with self._lock:
            self._duck_count += 1

            if self._duck_count == 1:
                # First duck request - save current volume and fade down
                try:
                    state = self.music_player.state()
                    if hasattr(state, "volume"):
                        self._original_volume = state.volume
                    elif isinstance(state, dict):
                        self._original_volume = state.get("volume", 50)
                    else:
                        self._original_volume = 50

                    # Detailed logging for debugging
                    player_type = type(self.music_player).__name__
                    has_set_volume = hasattr(self.music_player, "set_volume")
                    is_playing = getattr(state, "is_playing", None) or (
                        state.get("is_playing") if isinstance(state, dict) else None
                    )

                    logger.info(
                        "🔉 DUCK_START: %d → %d (player=%s, has_set_volume=%s, " "is_playing=%s, thread=%s)",
                        self._original_volume,
                        self.duck_level,
                        player_type,
                        has_set_volume,
                        is_playing,
                        th.current_thread().name,
                    )

                    # Cancel any ongoing fade
                    if self._fade_thread and self._fade_thread.is_alive():
                        self._fade_cancel.set()
                        self._fade_thread.join(timeout=TIMEOUT_MEDIUM)

                    # Start fade down
                    self._fade_cancel.clear()
                    self._fade_thread = threading.Thread(
                        target=self._fade_to_volume,
                        args=(self._original_volume, self.duck_level),
                        daemon=True,
                        name="audio-duck-down",
                    )
                    self._fade_thread.start()

                    # Start timeout safety net
                    self._start_timeout_timer()

                except Exception as e:
                    logger.warning("Failed to duck audio: %s", e)
            else:
                logger.debug("🔉 Nested duck request (count: %s)", self._duck_count)

    def unduck(self) -> None:
        """
        Un-duck (restore) the audio volume.
        Volume only restored when all duck requests have been un-ducked.
        """
        with self._lock:
            if self._duck_count <= 0:
                logger.warning("Un-duck called without matching duck")
                return

            self._duck_count -= 1

            if self._duck_count == 0:
                # Cancel timeout timer since we're properly undocking
                self._cancel_timeout_timer()

                # Last duck request ended - restore original volume
                if self._original_volume is not None:
                    logger.info(
                        "🔊 Un-ducking audio: %s → %s",
                        self.duck_level,
                        self._original_volume,
                    )

                    # Cancel any ongoing fade
                    if self._fade_thread and self._fade_thread.is_alive():
                        self._fade_cancel.set()
                        self._fade_thread.join(timeout=TIMEOUT_MEDIUM)

                    # Start fade up
                    self._fade_cancel.clear()
                    self._fade_thread = threading.Thread(
                        target=self._fade_to_volume,
                        args=(self.duck_level, self._original_volume),
                        daemon=True,
                        name="audio-duck-up",
                    )
                    self._fade_thread.start()

                    self._original_volume = None
            else:
                logger.debug("🔊 Nested un-duck (remaining: %s)", self._duck_count)

    def _fade_to_volume(self, start_vol: int, end_vol: int) -> None:
        """
        Smoothly fade volume from start_vol to end_vol over fade_duration.
        Can be cancelled by setting _fade_cancel event.
        """
        if start_vol == end_vol:
            logger.debug("Fade skipped - same volume")
            return

        steps = max(5, int(self.fade_duration * 20))  # 20 steps per second
        step_duration = self.fade_duration / steps
        start = float(start_vol)
        end = float(end_vol)
        volume_step = (end - start) / steps
        current_vol = start

        # Log player type information for debugging
        player_type = type(self.music_player).__name__
        inner_player = getattr(self.music_player, "player", None)
        inner_type = type(inner_player).__name__ if inner_player else "None"
        backend = getattr(inner_player, "_backend", None) if inner_player else None
        backend_type = type(backend).__name__ if backend else "None"

        logger.debug(
            "FADE_START: %d → %d (steps=%d, step_duration=%.3fs) " "player=%s, inner=%s, backend=%s",
            start_vol,
            end_vol,
            steps,
            step_duration,
            player_type,
            inner_type,
            backend_type,
        )

        for i in range(steps):
            if self._fade_cancel.is_set():
                logger.debug("Fade cancelled at step %d", i)
                return

            current_vol += volume_step
            try:
                target_vol = round(current_vol)
                self.music_player.set_volume(target_vol)

                # Log every few steps to track progress
                if i == 0 or i == steps - 1 or i % 5 == 0:
                    logger.debug("FADE_STEP: %d/%d → volume=%d", i + 1, steps, target_vol)
            except Exception as e:
                logger.warning("Failed to set volume during fade step %s: %s", i, e)
                break

            time.sleep(step_duration)

        # Ensure we reach exact target volume
        try:
            self.music_player.set_volume(end_vol)

            # Verify the volume was actually set
            try:
                state = self.music_player.state()
                actual_vol = (
                    state.volume
                    if hasattr(state, "volume")
                    else state.get("volume") if isinstance(state, dict) else None
                )
                # Check if playback is active
                is_playing = (
                    state.is_playing
                    if hasattr(state, "is_playing")
                    else (state.get("is_playing", False) if isinstance(state, dict) else False)
                )
                if actual_vol is not None and actual_vol != end_vol:
                    # Only warn if playback is active - when not playing, volume state may not update
                    if is_playing:
                        logger.warning(
                            "DUCK_VERIFY_MISMATCH: Requested volume=%d but player reports volume=%d",
                            end_vol,
                            actual_vol,
                        )
                    else:
                        logger.debug(
                            "DUCK_VERIFY_SKIPPED: Volume=%d (player reports %d, but not playing)",
                            end_vol,
                            actual_vol,
                        )
                        logger.info("DUCK_COMPLETE: Volume set to %d (not playing)", end_vol)
                else:
                    logger.info("DUCK_COMPLETE: Volume set to %d (verified)", end_vol)
            except Exception as e:
                logger.debug("Failed to verify final volume: %s", e, exc_info=True)
                logger.info("DUCK_COMPLETE: Volume set to %d (unverified)", end_vol)

        except Exception as e:
            logger.warning("Failed to set final volume: %s", e)

    def _start_timeout_timer(self) -> None:
        """Start a safety timeout that auto-unducks if ducking persists too long."""
        self._cancel_timeout_timer()
        self._timeout_timer = threading.Timer(
            self.MAX_DUCK_DURATION,
            self._on_timeout,
        )
        self._timeout_timer.daemon = True
        self._timeout_timer.start()
        logger.debug("Duck timeout safety net started (%ss)", self.MAX_DUCK_DURATION)

    def _cancel_timeout_timer(self) -> None:
        """Cancel the safety timeout timer."""
        if self._timeout_timer is not None:
            self._timeout_timer.cancel()
            self._timeout_timer = None

    def _on_timeout(self) -> None:
        """Handle ducking timeout - force restore audio."""
        logger.warning(
            "⚠️ Audio ducking timeout (%ss) - forcing audio restore. "
            "This may indicate a bug in voice command processing.",
            self.MAX_DUCK_DURATION,
        )
        # Force reset duck count and restore volume
        with self._lock:
            if self._duck_count > 0:
                self._duck_count = 0
                if self._original_volume is not None:
                    try:
                        self.music_player.set_volume(self._original_volume)
                        logger.info("🔊 Audio force-restored to %s", self._original_volume)
                    except Exception as e:
                        logger.warning("Failed to force-restore volume: %s", e)
                    self._original_volume = None

    def is_ducked(self) -> bool:
        """Check if audio is currently ducked."""
        with self._lock:
            return self._duck_count > 0


class DuckingContext:
    """
    Context manager for automatic audio ducking.

    Usage:
        with DuckingContext(audio_ducker):
            # Audio is ducked here
            transcribe_audio()
        # Audio restored automatically
    """

    def __init__(self, ducker: AudioDucker | None):
        self.ducker = ducker

    def __enter__(self) -> DuckingContext:
        if self.ducker:
            self.ducker.duck()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> Literal[False]:
        if self.ducker:
            self.ducker.unduck()
        return False  # Don't suppress exceptions


# Global singleton instance (initialized by backend runtime)
_global_ducker: AudioDucker | None = None


def set_global_ducker(ducker: AudioDucker) -> None:
    """Set the global audio ducker instance."""
    global _global_ducker
    _global_ducker = ducker
    logger.info("Global audio ducker configured")


def get_global_ducker() -> AudioDucker | None:
    """Get the global audio ducker instance."""
    return _global_ducker


def duck_context() -> DuckingContext:
    """
    Get a ducking context manager using the global ducker.

    Usage:
        with duck_context():
            # Audio is automatically ducked
            do_voice_stuff()
        # Audio automatically restored
    """
    return DuckingContext(_global_ducker)
