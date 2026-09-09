"""Null (headless) backend for testing without native audio.

Accepts all playback commands and tracks state internally, but never
initialises audio devices, spawns subprocesses, or touches hardware.
Designed for integration tests (e.g. dual-instance multiroom) where
the player must update ``PlayerState`` without producing sound.

Activate via ``VIOLA_PLAYER_BACKEND=null``.
"""

from __future__ import annotations

import threading

from core.logging_config import get_logger
from music.backends.base import BackendCapabilities, BackendProgress, BaseBackend

logger = get_logger(__name__)


class NullBackend(BaseBackend):
    """Backend that updates state but never touches native audio."""

    def __init__(self) -> None:
        super().__init__()
        self._playing = False
        self._paused = False
        self._volume = 50
        self._position_ms = 0
        self._duration_ms = 0
        self._source: str | None = None
        self._lock = threading.Lock()

    # -- lifecycle --

    def play(self, source: str) -> None:
        with self._lock:
            self._source = source
            self._playing = True
            self._paused = False
            self._position_ms = 0
            self._duration_ms = 180_000  # default 3 min
        logger.info("NullBackend: play(%s)", source)
        self._emit_progress(
            BackendProgress(
                position_ms=0,
                duration_ms=self._duration_ms,
            )
        )

    def pause(self) -> None:
        with self._lock:
            if self._playing:
                self._paused = True
        logger.debug("NullBackend: pause")

    def resume(self) -> None:
        with self._lock:
            if self._paused:
                self._paused = False
        logger.debug("NullBackend: resume")

    def stop(self) -> None:
        with self._lock:
            self._playing = False
            self._paused = False
            self._position_ms = 0
        logger.debug("NullBackend: stop")

    def is_playing(self) -> bool:
        with self._lock:
            return self._playing and not self._paused

    def set_volume(self, level: int) -> int:
        clamped = max(0, min(100, level))
        with self._lock:
            self._volume = clamped
        logger.debug("NullBackend: volume=%d", clamped)
        return clamped

    def seek(self, position_seconds: float) -> None:
        with self._lock:
            self._position_ms = int(position_seconds * 1000)
        logger.debug("NullBackend: seek(%.1fs)", position_seconds)

    # -- information --

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=True,
            pause=True,
            resume=True,
            seek=True,
            volume=True,
            position=True,
            duration=True,
        )

    def current_position_ms(self) -> int | None:
        with self._lock:
            return self._position_ms

    def current_duration_ms(self) -> int | None:
        with self._lock:
            return self._duration_ms
