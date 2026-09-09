"""
Playback Scheduler - Buffer and schedule PCM chunks for synchronized playback.

The PlaybackScheduler maintains a priority queue of PCM chunks sorted by their
play_at timestamp. It provides a PCMSource-compatible interface for audio output
that reads data at the appropriate time for synchronized multi-room playback.

Design:
    - Priority queue ordered by play_at timestamp
    - TARGET_BUFFER_MS ensures adequate buffering for smooth playback
    - Late chunk policy determines whether to play or drop late chunks
    - Implements read() method for PCMSource protocol compatibility
"""

from __future__ import annotations

import heapq
import threading
import time
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

from .chunk_protocol import (
    PCMChunk,
    bytes_to_duration_ms,
)
from .exceptions import BufferOverrunError

if TYPE_CHECKING:
    from .late_chunk_policy import LateChunkPolicy

logger = get_logger(__name__)

# Target buffer level for smooth playback
TARGET_BUFFER_MS: int = 300  # 300ms buffer

# Maximum buffer to prevent unbounded growth
MAX_BUFFER_MS: int = 2000  # 2 seconds max


class PlaybackScheduler:
    """
    Buffers and schedules PCM chunks for synchronized playback.

    Chunks are stored in a priority queue ordered by play_at timestamp.
    The read() method returns data when it's due for playback, implementing
    the PCMSource protocol for integration with audio output.

    Usage:
        scheduler = PlaybackScheduler(late_policy)
        receiver = SpokeAudioReceiver(scheduler)

        # Audio output reads from scheduler
        pcm_data = scheduler.read(4096)
    """

    def __init__(
        self,
        late_policy: LateChunkPolicy | None = None,
        *,
        target_buffer_ms: int = TARGET_BUFFER_MS,
        max_buffer_ms: int = MAX_BUFFER_MS,
        hub_time_offset: float = 0.0,
    ) -> None:
        """
        Initialize the playback scheduler.

        Args:
            late_policy: Policy for handling late chunks (default: accept all)
            target_buffer_ms: Target buffer level in milliseconds
            max_buffer_ms: Maximum buffer level before dropping
            hub_time_offset: Offset from local to Hub monotonic time
        """
        self._late_policy = late_policy
        self._target_buffer_ms = target_buffer_ms
        self._max_buffer_ms = max_buffer_ms
        self._hub_time_offset = hub_time_offset

        # Priority queue: (play_at, sequence, chunk)
        # Sequence is tiebreaker for equal play_at times
        self._queue: list[tuple[float, int, PCMChunk]] = []
        self._lock = threading.Lock()

        # Current read position within the front chunk
        self._current_chunk: PCMChunk | None = None
        self._current_offset: int = 0

        # Metrics
        self._chunks_added: int = 0
        self._chunks_played: int = 0
        self._underruns: int = 0
        self._overruns: int = 0
        self._late_drops: int = 0

    def set_hub_time_offset(self, offset: float) -> None:
        """
        Set the offset from local monotonic to Hub monotonic time.

        Args:
            offset: hub_time = local_time + offset
        """
        with self._lock:
            self._hub_time_offset = offset

    def add_chunk(self, chunk: PCMChunk) -> None:
        """
        Add a chunk to the playback buffer.

        Args:
            chunk: PCMChunk to buffer

        Raises:
            BufferOverrunError: If buffer exceeds max level
        """
        with self._lock:
            # Check buffer level
            buffer_ms = self._buffer_level_ms_locked()
            if buffer_ms > self._max_buffer_ms:
                self._overruns += 1
                raise BufferOverrunError(
                    buffer_level_ms=buffer_ms,
                    max_buffer_ms=self._max_buffer_ms,
                )

            # Add to priority queue (play_at, sequence, chunk)
            heapq.heappush(
                self._queue,
                (chunk.header.play_at, chunk.header.sequence, chunk),
            )
            self._chunks_added += 1

            logger.debug(
                "Added chunk seq=%d, play_at=%.3f, buffer_ms=%.1f",
                chunk.header.sequence,
                chunk.header.play_at,
                buffer_ms + bytes_to_duration_ms(len(chunk.pcm_data)),
            )

    def get_next_chunk(self) -> PCMChunk | None:
        """
        Get the next chunk that is due for playback.

        Returns:
            PCMChunk if one is ready, None if buffer is empty or not time yet
        """
        with self._lock:
            return self._get_next_chunk_locked()

    def _get_next_chunk_locked(self) -> PCMChunk | None:
        """Get next chunk (must hold lock)."""
        if not self._queue:
            return None

        # Peek at front chunk
        play_at, seq, chunk = self._queue[0]

        # Convert play_at (Hub time) to local time for comparison
        local_time = time.monotonic()
        hub_time = local_time + self._hub_time_offset

        # Check if it's time to play
        if hub_time < play_at:
            # Not time yet
            return None

        # Pop the chunk
        heapq.heappop(self._queue)

        # Check late policy
        if self._late_policy is not None:
            if not self._late_policy.should_play(chunk, hub_time):
                self._late_drops += 1
                logger.debug(
                    "Dropped late chunk seq=%d, late_by=%.1fms",
                    seq,
                    (hub_time - play_at) * 1000,
                )
                # Try next chunk
                return self._get_next_chunk_locked()

        self._chunks_played += 1
        return chunk

    def read(self, num_bytes: int) -> bytes:
        """
        Read PCM data for audio output (PCMSource protocol).

        This method returns up to num_bytes of PCM data that is due for
        playback. If no data is available, it may return less than requested
        or an empty bytes object.

        Args:
            num_bytes: Maximum bytes to read

        Returns:
            PCM data ready for playback

        Raises:
            BufferUnderrunError: If buffer is critically low (optional)
        """
        result = bytearray()

        with self._lock:
            while len(result) < num_bytes:
                # Check current chunk
                if self._current_chunk is not None:
                    # Read from current chunk
                    remaining = len(self._current_chunk.pcm_data) - self._current_offset
                    to_read = min(num_bytes - len(result), remaining)

                    result.extend(self._current_chunk.pcm_data[self._current_offset : self._current_offset + to_read])
                    self._current_offset += to_read

                    # Check if chunk is exhausted
                    if self._current_offset >= len(self._current_chunk.pcm_data):
                        self._current_chunk = None
                        self._current_offset = 0

                else:
                    # Get next chunk
                    chunk = self._get_next_chunk_locked()
                    if chunk is None:
                        # No more chunks available
                        if len(result) == 0:
                            # Underrun - no data available
                            self._underruns += 1
                            buffer_ms = self._buffer_level_ms_locked()
                            logger.debug(
                                "Buffer underrun, buffer_ms=%.1f",
                                buffer_ms,
                            )
                        break

                    self._current_chunk = chunk
                    self._current_offset = 0

        return bytes(result)

    def _buffer_level_ms_locked(self) -> float:
        """Calculate current buffer level in milliseconds (must hold lock)."""
        total_bytes = sum(len(chunk.pcm_data) for _, _, chunk in self._queue)
        if self._current_chunk is not None:
            total_bytes += len(self._current_chunk.pcm_data) - self._current_offset
        return bytes_to_duration_ms(total_bytes)

    def buffer_level_ms(self) -> float:
        """Get current buffer level in milliseconds."""
        with self._lock:
            return self._buffer_level_ms_locked()

    def is_buffer_healthy(self) -> bool:
        """Check if buffer level is at or above target."""
        return self.buffer_level_ms() >= self._target_buffer_ms

    def clear(self) -> None:
        """Clear all buffered chunks."""
        with self._lock:
            self._queue.clear()
            self._current_chunk = None
            self._current_offset = 0
            logger.debug("Playback buffer cleared")

    def get_metrics(self) -> dict[str, Any]:
        """
        Get scheduler metrics for monitoring.

        Returns:
            Dictionary with buffer statistics
        """
        with self._lock:
            return {
                "buffer_level_ms": self._buffer_level_ms_locked(),
                "target_buffer_ms": self._target_buffer_ms,
                "max_buffer_ms": self._max_buffer_ms,
                "queue_length": len(self._queue),
                "chunks_added": self._chunks_added,
                "chunks_played": self._chunks_played,
                "underruns": self._underruns,
                "overruns": self._overruns,
                "late_drops": self._late_drops,
                "hub_time_offset": self._hub_time_offset,
            }

    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._chunks_added = 0
            self._chunks_played = 0
            self._underruns = 0
            self._overruns = 0
            self._late_drops = 0


__all__ = [
    "MAX_BUFFER_MS",
    "TARGET_BUFFER_MS",
    "PlaybackScheduler",
]
