"""
Ring buffer abstraction coordinating decoded PCM delivery to downstream sinks.

The BufferManager balances producer/consumer access, exposes configurable
watermarks, and integrates tightly with DecoderTelemetry so underruns and
overflows surface instantly. This module is intentionally thread-safe and
does not rely on async constructs so it can operate in worker threads that
interface with native audio sinks.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from core.logging_config import get_logger

from .telemetry import DecoderTelemetry


class BufferOverflowError(RuntimeError):
    """Raised when writes exceed capacity without room to accommodate data."""


class BufferUnderrunError(RuntimeError):
    """Raised when reads are requested against an empty buffer."""


WatermarkCallback = Callable[["BufferMetrics"], None]


@dataclass(slots=True)
class BufferConfiguration:
    """Tunable parameters governing buffer behaviour."""

    capacity_bytes: int = 2 * 1024 * 1024
    """Total buffer capacity expressed in bytes (default: 2 MiB)."""

    high_watermark_ratio: float = 0.75
    """Fraction (0-1] above which the high watermark callback fires."""

    low_watermark_ratio: float = 0.25
    """Fraction (0-1] below which the low watermark callback fires."""

    write_block_timeout_sec: float = 5.0
    """Maximum duration a producer waits for capacity when block=True."""

    read_block_timeout_sec: float = 5.0
    """Maximum duration a consumer waits on data when block=True."""

    min_chunk_bytes: int = 4096
    """
    Minimum chunk size recommended for producer writes. Lower values increase
    lock contention and should be avoided in production except for tail frames.
    """


@dataclass(slots=True)
class BufferMetrics:
    """Snapshot of buffer state suitable for telemetry/logging."""

    capacity_bytes: int
    occupied_bytes: int
    underrun_count: int
    overflow_count: int
    last_write_timestamp: float
    last_read_timestamp: float

    @property
    def level_ratio(self) -> float:
        """Fractional occupancy (0.0-1.0)."""
        if self.capacity_bytes <= 0:
            return 0.0
        return min(1.0, max(0.0, self.occupied_bytes / self.capacity_bytes))

    @property
    def level_percent(self) -> float:
        """Occupancy percentage (0-100)."""
        return self.level_ratio * 100.0


class BufferManager:
    """
    Lock-protected ring buffer for PCM data with watermark notifications.

    Threading Model:
        - Producers/consumers operate via condition variables.
        - Watermark callbacks execute while holding the buffer lock; keep handlers
          lightweight to avoid stalling the pipeline.
    """

    def __init__(
        self,
        config: BufferConfiguration | None = None,
        telemetry: DecoderTelemetry | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config or BufferConfiguration()
        self._telemetry = telemetry
        self._logger = logger or get_logger("audio_core.decoder.buffer")

        if self._config.capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        if not 0.0 < self._config.low_watermark_ratio < 1.0:
            raise ValueError("low_watermark_ratio must be between 0 and 1")
        if not 0.0 < self._config.high_watermark_ratio <= 1.0:
            raise ValueError("high_watermark_ratio must be between 0 and 1")
        if self._config.low_watermark_ratio >= self._config.high_watermark_ratio:
            raise ValueError("low_watermark_ratio must be lower than high_watermark_ratio")

        self._buffer = bytearray(self._config.capacity_bytes)
        self._capacity = self._config.capacity_bytes
        self._read_pos = 0
        self._write_pos = 0
        self._size = 0

        self._lock = threading.Condition()
        self._high_callback: WatermarkCallback | None = None
        self._low_callback: WatermarkCallback | None = None
        self._underrun_count = 0
        self._overflow_count = 0
        self._last_write_ts = 0.0
        self._last_read_ts = 0.0
        self._last_high_state = False
        self._last_low_state = True  # Buffer starts empty -> low watermark

        self._emit_metrics_locked()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_high_watermark_callback(self, callback: WatermarkCallback | None) -> None:
        """Register a callback invoked when occupancy crosses the high watermark."""
        with self._lock:
            self._high_callback = callback

    def set_low_watermark_callback(self, callback: WatermarkCallback | None) -> None:
        """Register a callback invoked when occupancy crosses the low watermark."""
        with self._lock:
            self._low_callback = callback

    def write(self, data: bytes, *, block: bool = True) -> int:
        """
        Write PCM bytes into the ring buffer.

        Args:
            data: PCM payload to append.
            block: Whether to block until all bytes are written.

        Returns:
            Number of bytes written (may be less than len(data) when block=False).

        Raises:
            BufferOverflowError: When block=False and the buffer is full,
                or when block=True but space does not become available within the timeout.
        """
        if not data:
            return 0

        total_written = 0
        deadline = time.monotonic() + self._config.write_block_timeout_sec

        with self._lock:
            while total_written < len(data):
                available = self._capacity - self._size
                if available <= 0:
                    if not block:
                        self._overflow_count += 1
                        self._emit_metrics_locked()
                        raise BufferOverflowError("Buffer full; cannot write without blocking")

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._overflow_count += 1
                        self._emit_metrics_locked()
                        raise BufferOverflowError("Timed out waiting for buffer space")

                    self._lock.wait(timeout=remaining)
                    continue

                chunk = min(len(data) - total_written, available)
                self._write_chunk_locked(data, total_written, chunk)
                total_written += chunk
                self._condition_notify_all()

            return total_written

    def read(self, num_bytes: int, *, block: bool = True) -> bytes:
        """
        Consume PCM bytes from the ring buffer.

        Args:
            num_bytes: Desired byte count. If more data is available, the method
                returns at most `num_bytes`.
            block: When True (default), waits for data until timeout.

        Returns:
            Bytes read. Empty bytes implies timeout or no data when block=False.

        Raises:
            BufferUnderrunError: When block=False and buffer is empty,
                or when block=True and no data arrives before timeout.
        """
        if num_bytes <= 0:
            return b""

        deadline = time.monotonic() + self._config.read_block_timeout_sec

        with self._lock:
            while self._size == 0:
                if not block:
                    self._underrun_count += 1
                    self._emit_metrics_locked()
                    raise BufferUnderrunError("Buffer empty; cannot read without blocking")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._underrun_count += 1
                    self._emit_metrics_locked()
                    raise BufferUnderrunError("Timed out waiting for buffered data")

                self._lock.wait(timeout=remaining)

            chunk = min(num_bytes, self._size)
            payload = self._read_chunk_locked(chunk)
            self._condition_notify_all()
            return payload

    def available_read(self) -> int:
        """Return the number of bytes currently buffered."""
        with self._lock:
            return self._size

    def available_write(self) -> int:
        """Return remaining capacity in bytes."""
        with self._lock:
            return self._capacity - self._size

    def metrics(self) -> BufferMetrics:
        """Return latest metrics snapshot."""
        with self._lock:
            return self._collect_metrics_locked()

    def reset(self) -> None:
        """Reset buffer state and statistics."""
        with self._lock:
            self._read_pos = 0
            self._write_pos = 0
            self._size = 0
            self._underrun_count = 0
            self._overflow_count = 0
            self._last_high_state = False
            self._last_low_state = True
            self._emit_metrics_locked()
            self._lock.notify_all()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _condition_notify_all(self) -> None:
        self._lock.notify_all()
        self._emit_metrics_locked()

    def _write_chunk_locked(self, data: bytes, offset: int, length: int) -> None:
        end_pos = (self._write_pos + length) % self._capacity
        first_span = min(length, self._capacity - self._write_pos)
        self._buffer[self._write_pos : self._write_pos + first_span] = data[offset : offset + first_span]
        remainder = length - first_span
        if remainder:
            self._buffer[0:remainder] = data[offset + first_span : offset + first_span + remainder]

        self._write_pos = end_pos
        self._size += length
        self._last_write_ts = time.time()
        self._check_watermarks_locked()

    def _read_chunk_locked(self, length: int) -> bytes:
        end_pos = (self._read_pos + length) % self._capacity
        first_span = min(length, self._capacity - self._read_pos)
        chunk = bytes(self._buffer[self._read_pos : self._read_pos + first_span])
        remainder = length - first_span
        if remainder:
            chunk += bytes(self._buffer[0:remainder])

        self._read_pos = end_pos
        self._size -= length
        self._last_read_ts = time.time()
        self._check_watermarks_locked()
        return chunk

    def _collect_metrics_locked(self) -> BufferMetrics:
        return BufferMetrics(
            capacity_bytes=self._capacity,
            occupied_bytes=self._size,
            underrun_count=self._underrun_count,
            overflow_count=self._overflow_count,
            last_write_timestamp=self._last_write_ts,
            last_read_timestamp=self._last_read_ts,
        )

    def _emit_metrics_locked(self) -> None:
        metrics = self._collect_metrics_locked()

        if self._telemetry:
            self._telemetry.emit_buffer_metrics(metrics)

        # Emit structured log for operators lacking telemetry sinks
        self._logger.debug(
            "buffer_metrics",
            extra={
                "buffer_metrics": {
                    "capacity_bytes": metrics.capacity_bytes,
                    "occupied_bytes": metrics.occupied_bytes,
                    "level_percent": round(metrics.level_percent, 2),
                    "underrun_count": metrics.underrun_count,
                    "overflow_count": metrics.overflow_count,
                }
            },
        )

    def _check_watermarks_locked(self) -> None:
        metrics = self._collect_metrics_locked()

        high_triggered = metrics.level_ratio >= self._config.high_watermark_ratio
        if high_triggered and not self._last_high_state and self._high_callback:
            try:
                self._high_callback(metrics)
            except Exception:  # pragma: no cover - defensive
                self._logger.exception("High watermark callback failed")
        self._last_high_state = high_triggered

        low_triggered = metrics.level_ratio <= self._config.low_watermark_ratio
        if low_triggered and not self._last_low_state and self._low_callback:
            try:
                self._low_callback(metrics)
            except Exception:  # pragma: no cover - defensive
                self._logger.exception("Low watermark callback failed")
        self._last_low_state = low_triggered

        if self._telemetry:
            self._telemetry.emit_buffer_level(metrics)

    # ------------------------------------------------------------------ #
    # Context manager helpers
    # ------------------------------------------------------------------ #

    def __enter__(self) -> BufferManager:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with self._lock:
            self._buffer = bytearray(self._capacity)
            self._size = 0
            self._lock.notify_all()


__all__ = [
    "BufferConfiguration",
    "BufferManager",
    "BufferMetrics",
    "BufferOverflowError",
    "BufferUnderrunError",
    "WatermarkCallback",
]
