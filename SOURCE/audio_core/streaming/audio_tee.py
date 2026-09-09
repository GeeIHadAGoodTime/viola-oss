"""
Audio Tee - Split PCM stream to multiple outputs.

The AudioTee receives PCM data and distributes it to multiple registered
output callbacks. This enables sending audio to both local playback and
network broadcast simultaneously for multi-room sync.

Threading Model:
    - Thread-safe via Lock for output registration/removal
    - write() can be called from any thread (typically audio decode thread)
    - Callbacks execute in the calling thread context
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from core.logging_config import get_logger

logger = get_logger(__name__)


class AudioTee:
    """
    Splits a PCM audio stream to multiple output callbacks.

    Usage:
        tee = AudioTee()
        tee.add_output(local_speaker_callback)
        tee.add_output(network_broadcast_callback)

        # In audio decode loop:
        tee.write(pcm_data)
    """

    def __init__(self) -> None:
        """Initialize the audio tee with no outputs."""
        self._outputs: list[Callable[[bytes], None]] = []
        self._lock = threading.Lock()
        self._bytes_written: int = 0
        self._write_count: int = 0

    def add_output(self, callback: Callable[[bytes], None]) -> None:
        """
        Register an output callback to receive PCM data.

        Args:
            callback: Function that receives bytes on each write
        """
        with self._lock:
            if callback not in self._outputs:
                self._outputs.append(callback)
                logger.debug(
                    "Added output callback, total outputs: %d",
                    len(self._outputs),
                )

    def remove_output(self, callback: Callable[[bytes], None]) -> None:
        """
        Unregister an output callback.

        Args:
            callback: Function to remove from outputs
        """
        with self._lock:
            if callback in self._outputs:
                self._outputs.remove(callback)
                logger.debug(
                    "Removed output callback, total outputs: %d",
                    len(self._outputs),
                )

    def write(self, pcm_data: bytes) -> None:
        """
        Write PCM data to all registered outputs.

        This method is thread-safe and will call each registered callback
        with a copy of the PCM data. Exceptions in callbacks are logged
        but do not prevent delivery to other outputs.

        Args:
            pcm_data: Raw PCM audio data to distribute
        """
        if not pcm_data:
            return

        # Take snapshot of outputs under lock
        with self._lock:
            outputs_snapshot = list(self._outputs)
            self._bytes_written += len(pcm_data)
            self._write_count += 1

        if not outputs_snapshot:
            return

        # Deliver to each output outside the lock
        for callback in outputs_snapshot:
            try:
                callback(pcm_data)
            except Exception:
                logger.exception("Output callback failed, continuing with other outputs")

    def clear_outputs(self) -> None:
        """Remove all registered output callbacks."""
        with self._lock:
            count = len(self._outputs)
            self._outputs.clear()
            logger.debug("Cleared %d output callbacks", count)

    @property
    def output_count(self) -> int:
        """Return the number of registered outputs."""
        with self._lock:
            return len(self._outputs)

    def get_metrics(self) -> dict[str, int]:
        """
        Get tee metrics for monitoring.

        Returns:
            Dictionary with bytes_written, write_count, and output_count
        """
        with self._lock:
            return {
                "bytes_written": self._bytes_written,
                "write_count": self._write_count,
                "output_count": len(self._outputs),
            }

    def reset_metrics(self) -> None:
        """Reset write counters."""
        with self._lock:
            self._bytes_written = 0
            self._write_count = 0


__all__ = [
    "AudioTee",
]
