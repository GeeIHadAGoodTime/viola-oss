"""Null audio output driver.

Discards all PCM data.  Useful for testing and headless environments
where no sound hardware is available.  Records write statistics so
tests can verify that data flowed through the pipeline.
"""

from __future__ import annotations

import threading

from core.logging_config import get_logger

from .base import AudioOutputDriver

logger = get_logger(__name__)

__all__ = [
    "NullAudioOutput",
]


class NullAudioOutput(AudioOutputDriver):
    """Audio output driver that silently discards all PCM data.

    Metrics are tracked so tests can assert that chunks were delivered:

    * ``bytes_written`` -- total bytes passed to :meth:`write`.
    * ``chunk_count`` -- number of :meth:`write` calls.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self.bytes_written: int = 0
        self.chunk_count: int = 0

    # -- AudioOutputDriver interface ----------------------------------------

    def start(self, sample_rate: int, channels: int, sample_width: int) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self.bytes_written = 0
            self.chunk_count = 0
        logger.info(
            "NullAudioOutput started: rate=%d channels=%d width=%d",
            sample_rate,
            channels,
            sample_width,
        )

    def write(self, data: bytes) -> None:
        with self._lock:
            if not self._running:
                return
            self.bytes_written += len(data)
            self.chunk_count += 1

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        logger.info(
            "NullAudioOutput stopped: bytes_written=%d chunk_count=%d",
            self.bytes_written,
            self.chunk_count,
        )
