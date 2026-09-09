"""
Spoke Audio Receiver - Receive and process PCM chunks from Hub.

The SpokeAudioReceiver sits on Spoke (playback device) side and receives
PCM chunks from the Hub via WebSocket. It deserializes chunks and forwards
them to the PlaybackScheduler for buffered, synchronized playback.

Design:
    - Receives base64-encoded chunk data from WebSocket messages
    - Deserializes to PCMChunk
    - Handles out-of-order and duplicate chunks
    - Forwards valid chunks to PlaybackScheduler
"""

from __future__ import annotations

import base64
import threading
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

from .chunk_protocol import deserialize
from .exceptions import ChunkDeserializationError

if TYPE_CHECKING:
    from .playback_scheduler import PlaybackScheduler

logger = get_logger(__name__)


class SpokeAudioReceiver:
    """
    Receives PCM chunks from Hub and forwards to playback scheduler.

    Handles:
    - Chunk deserialization from wire format
    - Out-of-order chunk detection
    - Duplicate chunk filtering
    - Gap detection (missing chunks)

    Usage:
        receiver = SpokeAudioReceiver(playback_scheduler)
        # In WebSocket message handler:
        receiver.on_chunk_received(message_data)
    """

    def __init__(
        self,
        scheduler: PlaybackScheduler,
        *,
        max_sequence_gap: int = 100,
    ) -> None:
        """
        Initialize the spoke receiver.

        Args:
            scheduler: PlaybackScheduler to receive decoded chunks
            max_sequence_gap: Maximum sequence gap before resync
        """
        self._scheduler = scheduler
        self._max_sequence_gap = max_sequence_gap

        # State
        self._lock = threading.Lock()
        self._last_sequence: int = -1
        self._running = False

        # Metrics
        self._chunks_received: int = 0
        self._chunks_out_of_order: int = 0
        self._chunks_duplicates: int = 0
        self._chunks_gaps: int = 0
        self._deserialization_errors: int = 0

    def start(self) -> None:
        """Start the receiver."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._last_sequence = -1
            logger.info("Spoke receiver started")

    def stop(self) -> None:
        """Stop the receiver."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            logger.info(
                "Spoke receiver stopped, chunks_received=%d, out_of_order=%d",
                self._chunks_received,
                self._chunks_out_of_order,
            )

    def on_chunk_received(self, data: bytes | str) -> bool:
        """
        Handle received chunk data from WebSocket.

        Args:
            data: Raw bytes or base64-encoded string

        Returns:
            True if chunk was successfully processed
        """
        with self._lock:
            if not self._running:
                return False

        # Decode base64 if string
        if isinstance(data, str):
            try:
                data = base64.b64decode(data)
            except Exception:
                logger.warning("Failed to decode base64 chunk data")
                with self._lock:
                    self._deserialization_errors += 1
                return False

        # Deserialize chunk
        try:
            chunk = deserialize(data)
        except ChunkDeserializationError as e:
            logger.warning(
                "Chunk deserialization failed: %s",
                e.reason,
            )
            with self._lock:
                self._deserialization_errors += 1
            return False

        # Check sequence
        seq = chunk.header.sequence
        with self._lock:
            self._chunks_received += 1

            # Detect duplicates
            if seq == self._last_sequence:
                self._chunks_duplicates += 1
                logger.debug("Duplicate chunk seq=%d", seq)
                return False

            # Detect out-of-order
            if seq < self._last_sequence:
                self._chunks_out_of_order += 1
                # Still accept out-of-order chunks for the scheduler
                # The scheduler will handle timing
                logger.debug(
                    "Out-of-order chunk: seq=%d, last=%d",
                    seq,
                    self._last_sequence,
                )

            # Detect gaps
            if self._last_sequence >= 0 and seq > self._last_sequence + 1:
                gap = seq - self._last_sequence - 1
                self._chunks_gaps += gap
                logger.debug(
                    "Chunk gap detected: %d chunks missing (seq %d to %d)",
                    gap,
                    self._last_sequence + 1,
                    seq - 1,
                )

                # Check for large gaps (possible resync needed)
                if gap > self._max_sequence_gap:
                    logger.warning(
                        "Large sequence gap (%d), may need resync",
                        gap,
                    )

            self._last_sequence = max(self._last_sequence, seq)

        # Forward to scheduler
        try:
            self._scheduler.add_chunk(chunk)
            return True
        except Exception:
            logger.exception(
                "Failed to add chunk seq=%d to scheduler",
                seq,
            )
            return False

    def on_websocket_message(self, payload: dict[str, Any]) -> bool:
        """
        Handle WebSocket message payload containing a chunk.

        Expected payload format:
            {
                "chunk": "<base64 encoded chunk>",
                "sequence": <int>,
                "play_at": <float>
            }

        Args:
            payload: Decoded JSON payload from WebSocket

        Returns:
            True if chunk was successfully processed
        """
        chunk_data = payload.get("chunk")
        if not chunk_data:
            logger.debug("WebSocket message missing chunk data")
            return False

        return self.on_chunk_received(chunk_data)

    def get_metrics(self) -> dict[str, Any]:
        """
        Get receiver metrics for monitoring.

        Returns:
            Dictionary with reception statistics
        """
        with self._lock:
            return {
                "running": self._running,
                "last_sequence": self._last_sequence,
                "chunks_received": self._chunks_received,
                "chunks_out_of_order": self._chunks_out_of_order,
                "chunks_duplicates": self._chunks_duplicates,
                "chunks_gaps": self._chunks_gaps,
                "deserialization_errors": self._deserialization_errors,
            }

    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._chunks_received = 0
            self._chunks_out_of_order = 0
            self._chunks_duplicates = 0
            self._chunks_gaps = 0
            self._deserialization_errors = 0


__all__ = [
    "SpokeAudioReceiver",
]
