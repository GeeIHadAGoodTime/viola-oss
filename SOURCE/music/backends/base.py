"""
music/backends/base.py

Common backend interfaces, capability flags, and progress reporting
used by the streaming-aware music player.
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from core.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


@dataclasses.dataclass(frozen=True)
class BackendCapabilities:
    """Expose feature support so the UI can adapt at runtime."""

    streaming: bool = False
    pause: bool = False
    resume: bool = False
    seek: bool = False
    volume: bool = False
    position: bool = False
    duration: bool = False
    waveform: bool = False  # Placeholder for future visualizations


@dataclasses.dataclass
class BackendProgress:
    """Real-time progress snapshot emitted by streaming backends."""

    position_ms: int
    duration_ms: int | None
    buffered_ms: int | None = None


class ProgressListener(Protocol):
    """Protocol for progress callback functions."""

    def __call__(self, progress: BackendProgress) -> None: ...


class BaseBackend(abc.ABC):
    """
    Abstract interface that all playback backends must implement.

    Implementations should provide streaming controls and emit
    progress updates whenever possible to keep application state
    synchronized with the UI.
    """

    def __init__(self) -> None:
        self._progress_listener: ProgressListener | None = None

    # ---------- lifecycle ----------

    def set_progress_listener(self, listener: ProgressListener | None) -> None:
        """Register a callback that receives BackendProgress events."""
        self._progress_listener = listener

    @abc.abstractmethod
    def play(self, source: str) -> None:
        """Begin playback for the given media source."""

    # Backwards compatibility with legacy call sites
    def play_url(self, url: str) -> None:
        """Legacy alias for play(); retained for tests and older integrations."""
        self.play(url)

    @abc.abstractmethod
    def pause(self) -> None:
        """Pause playback without releasing the stream."""

    @abc.abstractmethod
    def resume(self) -> None:
        """Resume playback after a pause."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop playback and release any active resources."""

    @abc.abstractmethod
    def is_playing(self) -> bool:
        """Return True while audio is actively being rendered."""

    @abc.abstractmethod
    def set_volume(self, level: int) -> int:
        """Adjust output volume (0-100) and return the applied level."""

    @abc.abstractmethod
    def seek(self, position_seconds: float) -> None:
        """Seek to the requested playback position."""

    # ---------- information ----------

    @abc.abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Return the feature set supported by this backend."""

    def current_position_ms(self) -> int | None:
        """Return the current position in milliseconds when available."""
        return None

    def current_duration_ms(self) -> int | None:
        """Return the media duration in milliseconds when available."""
        return None

    # ---------- helper ----------

    def _emit_progress(self, progress: BackendProgress) -> None:
        """Utility for subclasses to send progress events."""
        if self._progress_listener:
            self._progress_listener(progress)

    # ---------- optional extension points (type stubs for duck typing) ----------

    def cleanup(self) -> None:
        """Optional cleanup hook for backends with external resources."""
        pass

    def get_position(self) -> int | None:
        """Return current position in milliseconds (alias for current_position_ms)."""
        return self.current_position_ms()

    def get_duration(self) -> int | None:
        """Return total duration in milliseconds (alias for current_duration_ms)."""
        return self.current_duration_ms()

    def get_position_percentage(self) -> float | None:
        """Return current position as percentage (0-100)."""
        pos = self.current_position_ms()
        dur = self.current_duration_ms()
        if pos is None or dur is None or dur == 0:
            return None
        return (pos / dur) * 100.0

    def update_expected_duration(self, duration_ms: int) -> None:
        """Update expected track duration (used by embedded backends)."""
        pass

    # ---------- gapless playback support ----------

    def preload_next_track(self, source: str) -> bool:
        """
        Preload the next track for gapless playback transition.

        This method prepares the next track for playback without starting it,
        reducing the gap between tracks when transitioning.

        Args:
            source: Media URL or path to preload

        Returns:
            True if preloading is supported and initiated successfully,
            False if preloading is not supported by this backend.

        Note:
            This is an optional capability. Backends that don't support
            preloading should return False (the default implementation).
        """
        return False

    def has_preloaded_track(self) -> bool:
        """
        Check if a track has been preloaded and is ready for playback.

        Returns:
            True if a preloaded track is ready, False otherwise.
        """
        return False

    def play_preloaded(self) -> bool:
        """
        Start playback of the preloaded track.

        This enables gapless transition from the current track to the
        preloaded one.

        Returns:
            True if the preloaded track was started successfully,
            False if no track was preloaded or playback failed.
        """
        return False

    def cancel_preload(self) -> None:
        """
        Cancel any pending preload operation and free resources.

        Call this when the queue changes and the preloaded track is
        no longer the next track to play.
        """
        pass

    # ---------- standardized helpers for subclasses ----------

    def _safe_call(self, operation: str, func: Callable[[], T], default: T | None = None) -> T | None:
        """
        Standardized error handling for backend operations.

        Wraps a callable with logging and exception handling.
        Subclasses should use this for operations that may fail.

        Args:
            operation: Name of the operation (for logging)
            func: Zero-argument callable to execute
            default: Value to return on failure (default: None)

        Returns:
            Result of func() on success, default on failure
        """
        try:
            return func()
        except Exception as exc:
            logger.warning("%s failed in %s: %s", operation, self.__class__.__name__, exc)
            return default

    def _update_state(self, **updates: Any) -> None:
        """
        Centralized state management for backend attributes.

        Updates internal state attributes with the given values.
        Only updates attributes that exist (prefixed with underscore).

        Args:
            **updates: Key-value pairs where key is the attribute name
                      (without underscore prefix) and value is the new value.

        Example:
            self._update_state(playing=True, position_ms=1000)
            # Sets self._playing = True, self._position_ms = 1000
        """
        for key, value in updates.items():
            attr_name = f"_{key}"
            if hasattr(self, attr_name):
                setattr(self, attr_name, value)
