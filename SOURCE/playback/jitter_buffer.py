"""
Jitter Buffer Module
Buffer for network streams to handle variable latency.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class JitterBuffer:
    """
    Jitter buffer for network audio streams.

    Handles variable network latency by buffering packets
    and playing them at consistent intervals.
    """

    def __init__(self, target_latency_ms: int = 100, max_latency_ms: int = 500):
        """
        Initialize jitter buffer.

        Args:
            target_latency_ms: Target buffer latency in milliseconds
            max_latency_ms: Maximum allowed latency before dropping packets
        """
        self.target_latency_ms = target_latency_ms
        self.max_latency_ms = max_latency_ms
        self._lock = threading.Lock()
        self._packets: deque = deque()
        self._packet_timestamps: deque = deque()
        self._total_packets = 0
        self._dropped_packets = 0
        self._start_time: float | None = None

        logger.info(
            "JitterBuffer initialized: target=%sms, max=%sms",
            target_latency_ms,
            max_latency_ms,
        )

    def add_packet(self, packet: Any, timestamp: float | None = None) -> bool:
        """
        Add packet to jitter buffer.

        Args:
            packet: Audio packet data
            timestamp: Packet timestamp (None = use current time)

        Returns:
            True if packet added, False if dropped
        """
        if timestamp is None:
            timestamp = time.time()

        with self._lock:
            # Check if buffer is too full
            if self._packets:
                oldest_timestamp = self._packet_timestamps[0]
                latency_ms = (timestamp - oldest_timestamp) * 1000

                if latency_ms > self.max_latency_ms:
                    # Drop oldest packet to prevent excessive latency
                    self._packets.popleft()
                    self._packet_timestamps.popleft()
                    self._dropped_packets += 1
                    logger.warning("Dropped packet due to excessive latency: %.1fms", latency_ms)

            self._packets.append(packet)
            self._packet_timestamps.append(timestamp)
            self._total_packets += 1

            if self._start_time is None:
                self._start_time = timestamp

            return True

    def get_packet(self, timeout: float | None = None) -> Any | None:
        """
        Get next packet from buffer.

        Waits until target latency is reached before returning packet.

        Args:
            timeout: Maximum time to wait for packet (None = wait indefinitely)

        Returns:
            Packet data or None if timeout/empty
        """
        start_wait = time.time()

        while True:
            with self._lock:
                if not self._packets:
                    if timeout and (time.time() - start_wait) > timeout:
                        return None
                    time.sleep(0.01)  # Small sleep to avoid busy-wait
                    continue

                # Check if we've reached target latency
                oldest_timestamp = self._packet_timestamps[0]
                current_time = time.time()
                latency_ms = (current_time - oldest_timestamp) * 1000

                if latency_ms >= self.target_latency_ms:
                    # Return oldest packet
                    packet = self._packets.popleft()
                    self._packet_timestamps.popleft()
                    return packet

            if timeout and (time.time() - start_wait) > timeout:
                return None

            time.sleep(0.01)

    def clear(self) -> None:
        """Clear all packets from buffer"""
        with self._lock:
            self._packets.clear()
            self._packet_timestamps.clear()
            self._start_time = None

    def get_stats(self) -> dict[str, Any]:
        """
        Get jitter buffer statistics.

        Returns:
            Dictionary with buffer statistics
        """
        with self._lock:
            current_latency_ms = 0
            if self._packets and self._packet_timestamps:
                oldest_timestamp = self._packet_timestamps[0]
                current_latency_ms = (time.time() - oldest_timestamp) * 1000

            return {
                "buffer_size": len(self._packets),
                "current_latency_ms": current_latency_ms,
                "total_packets": self._total_packets,
                "dropped_packets": self._dropped_packets,
                "drop_rate": self._dropped_packets / max(1, self._total_packets),
            }
