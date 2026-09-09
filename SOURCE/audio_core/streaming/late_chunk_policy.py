"""
Late Chunk Policy - Decide whether to play or drop late-arriving chunks.

When audio chunks arrive after their scheduled play_at time, we need to decide
whether to play them (accepting minor latency) or drop them (maintaining sync
but losing audio). This policy encapsulates that decision.

Design:
    - Chunks less than LATE_THRESHOLD_MS late: Play (minor latency acceptable)
    - Chunks more than LATE_THRESHOLD_MS late: Drop (would cause audible desync)
    - Track metrics for monitoring late arrivals
"""

from __future__ import annotations

import threading
from typing import Any

from core.logging_config import get_logger

from .chunk_protocol import PCMChunk

logger = get_logger(__name__)

# Threshold for determining if a chunk is too late to play
LATE_THRESHOLD_MS: int = 50


class LateChunkPolicy:
    """
    Policy for handling late-arriving PCM chunks.

    Chunks arriving less than LATE_THRESHOLD_MS after their play_at time
    are still played (minor latency is acceptable). Chunks arriving later
    are dropped to maintain synchronization.

    Usage:
        policy = LateChunkPolicy()
        if policy.should_play(chunk, current_hub_time):
            scheduler.play(chunk)
    """

    def __init__(
        self,
        late_threshold_ms: int = LATE_THRESHOLD_MS,
    ) -> None:
        """
        Initialize the late chunk policy.

        Args:
            late_threshold_ms: Maximum acceptable lateness in milliseconds
        """
        self._late_threshold_ms = late_threshold_ms
        self._late_threshold_sec = late_threshold_ms / 1000.0

        # Metrics
        self._lock = threading.Lock()
        self._late_count: int = 0  # Chunks that were late but played
        self._dropped_count: int = 0  # Chunks that were too late and dropped
        self._on_time_count: int = 0  # Chunks that arrived on time

    def should_play(self, chunk: PCMChunk, current_hub_time: float) -> bool:
        """
        Determine if a chunk should be played based on its lateness.

        Args:
            chunk: PCMChunk to evaluate
            current_hub_time: Current Hub monotonic time

        Returns:
            True if chunk should be played, False if it should be dropped
        """
        play_at = chunk.header.play_at
        lateness = current_hub_time - play_at  # Positive = late

        with self._lock:
            if lateness <= 0:
                # On time or early
                self._on_time_count += 1
                return True
            elif lateness <= self._late_threshold_sec:
                # Late but within threshold - play it
                self._late_count += 1
                logger.debug(
                    "Playing late chunk seq=%d, late_by=%.1fms",
                    chunk.header.sequence,
                    lateness * 1000,
                )
                return True
            else:
                # Too late - drop it
                self._dropped_count += 1
                logger.debug(
                    "Dropping late chunk seq=%d, late_by=%.1fms (threshold=%.1fms)",
                    chunk.header.sequence,
                    lateness * 1000,
                    self._late_threshold_ms,
                )
                return False

    @property
    def late_threshold_ms(self) -> int:
        """Get the late threshold in milliseconds."""
        return self._late_threshold_ms

    @property
    def late_count(self) -> int:
        """Get count of late chunks that were played."""
        with self._lock:
            return self._late_count

    @property
    def dropped_count(self) -> int:
        """Get count of chunks that were too late and dropped."""
        with self._lock:
            return self._dropped_count

    @property
    def on_time_count(self) -> int:
        """Get count of chunks that arrived on time."""
        with self._lock:
            return self._on_time_count

    def get_metrics(self) -> dict[str, Any]:
        """
        Get policy metrics for monitoring.

        Returns:
            Dictionary with late/dropped statistics
        """
        with self._lock:
            total = self._on_time_count + self._late_count + self._dropped_count
            late_rate = (self._late_count / total * 100) if total > 0 else 0.0
            drop_rate = (self._dropped_count / total * 100) if total > 0 else 0.0

            return {
                "late_threshold_ms": self._late_threshold_ms,
                "on_time_count": self._on_time_count,
                "late_count": self._late_count,
                "dropped_count": self._dropped_count,
                "total_evaluated": total,
                "late_rate_percent": round(late_rate, 2),
                "drop_rate_percent": round(drop_rate, 2),
            }

    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._late_count = 0
            self._dropped_count = 0
            self._on_time_count = 0


__all__ = [
    "LATE_THRESHOLD_MS",
    "LateChunkPolicy",
]
