"""
Multi-Room Sync Metrics Collection

Provides thread-safe metrics collection for multi-room synchronization
including sync latency, failovers, reconnections, and dropped chunks.

Also provides a ComponentRegistry for tracking active multiroom components
(heartbeat protocols, playback schedulers) for health monitoring.

Usage:
    from services.multiroom.metrics import MultiRoomMetrics

    metrics = MultiRoomMetrics()
    metrics.record_sync_attempt(success=True, latency_ms=12.5)
    percentiles = metrics.get_latency_percentiles()
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

from core.logging_config import get_logger

if TYPE_CHECKING:
    from audio_core.streaming.playback_scheduler import PlaybackScheduler
    from services.multiroom.failover.heartbeat_protocol import HubHeartbeatProtocol

logger = get_logger(__name__)

# Maximum number of latency samples to retain for percentile calculations
MAX_LATENCY_SAMPLES = 1000


@dataclass
class MultiRoomMetrics:
    """
    Thread-safe metrics collection for multi-room synchronization.

    Tracks:
    - Sync attempt success/failure counts
    - Sync latency distribution (p50, p95, p99)
    - Connected node count
    - Hub failover events
    - Reconnection events
    - Dropped audio chunks

    All methods are thread-safe.
    """

    total_sync_attempts: int = 0
    total_sync_failures: int = 0
    connected_nodes: int = 0
    sync_latency_samples: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_LATENCY_SAMPLES))
    hub_failovers: int = 0
    reconnections: int = 0
    dropped_chunks: int = 0

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_sync_attempt(self, success: bool, latency_ms: float) -> None:
        """
        Record a synchronization attempt.

        Args:
            success: Whether the sync was successful
            latency_ms: Latency of the sync operation in milliseconds
        """
        with self._lock:
            self.total_sync_attempts += 1
            if not success:
                self.total_sync_failures += 1
            self.sync_latency_samples.append(latency_ms)

        if not success:
            logger.debug(
                "Sync attempt failed, total failures: %d/%d",
                self.total_sync_failures,
                self.total_sync_attempts,
            )

    def record_failover(self) -> None:
        """Record a hub failover event."""
        with self._lock:
            self.hub_failovers += 1
        logger.info("Hub failover recorded, total failovers: %d", self.hub_failovers)

    def record_reconnection(self) -> None:
        """Record a node reconnection event."""
        with self._lock:
            self.reconnections += 1
        logger.debug("Reconnection recorded, total: %d", self.reconnections)

    def record_dropped_chunk(self) -> None:
        """Record a dropped audio chunk."""
        with self._lock:
            self.dropped_chunks += 1

    def set_connected_nodes(self, count: int) -> None:
        """
        Set the current connected node count.

        Args:
            count: Number of currently connected nodes
        """
        with self._lock:
            self.connected_nodes = count

    def increment_connected_nodes(self) -> None:
        """Increment the connected node count by 1."""
        with self._lock:
            self.connected_nodes += 1

    def decrement_connected_nodes(self) -> None:
        """Decrement the connected node count by 1 (minimum 0)."""
        with self._lock:
            self.connected_nodes = max(0, self.connected_nodes - 1)

    def get_latency_percentiles(self) -> dict[str, float | None]:
        """
        Calculate latency percentiles from collected samples.

        Returns:
            Dictionary with p50, p95, p99 percentiles in milliseconds.
            Values are None if no samples are available.
        """
        with self._lock:
            samples = list(self.sync_latency_samples)

        if not samples:
            return {"p50": None, "p95": None, "p99": None}

        sorted_samples = sorted(samples)
        n = len(sorted_samples)

        def percentile(p: float) -> float:
            """Calculate the p-th percentile."""
            idx = (p / 100) * (n - 1)
            lower = int(idx)
            upper = min(lower + 1, n - 1)
            frac = idx - lower
            return sorted_samples[lower] * (1 - frac) + sorted_samples[upper] * frac

        return {
            "p50": round(percentile(50), 2),
            "p95": round(percentile(95), 2),
            "p99": round(percentile(99), 2),
        }

    def get_success_rate(self) -> float:
        """
        Calculate the sync success rate.

        Returns:
            Success rate as a float between 0.0 and 1.0.
            Returns 1.0 if no attempts have been made.
        """
        with self._lock:
            if self.total_sync_attempts == 0:
                return 1.0
            return (self.total_sync_attempts - self.total_sync_failures) / self.total_sync_attempts

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize metrics to a dictionary for JSON responses.

        Returns:
            Dictionary containing all metrics data.
        """
        with self._lock:
            percentiles = self.get_latency_percentiles()
            return {
                "total_sync_attempts": self.total_sync_attempts,
                "total_sync_failures": self.total_sync_failures,
                "sync_success_rate": round(self.get_success_rate(), 4),
                "connected_nodes": self.connected_nodes,
                "sync_latency_p50_ms": percentiles["p50"],
                "sync_latency_p95_ms": percentiles["p95"],
                "sync_latency_p99_ms": percentiles["p99"],
                "hub_failovers": self.hub_failovers,
                "reconnections": self.reconnections,
                "dropped_chunks": self.dropped_chunks,
                "latency_sample_count": len(self.sync_latency_samples),
            }

    def reset(self) -> None:
        """Reset all metrics to initial values."""
        with self._lock:
            self.total_sync_attempts = 0
            self.total_sync_failures = 0
            self.connected_nodes = 0
            self.sync_latency_samples.clear()
            self.hub_failovers = 0
            self.reconnections = 0
            self.dropped_chunks = 0
        logger.info("MultiRoomMetrics reset")


# Global metrics instance
_metrics: MultiRoomMetrics | None = None
_metrics_lock = threading.Lock()


def get_multiroom_metrics() -> MultiRoomMetrics:
    """Get the global multi-room metrics instance (singleton)."""
    global _metrics
    with _metrics_lock:
        if _metrics is None:
            _metrics = MultiRoomMetrics()
        return _metrics


class ComponentRegistry:
    """
    Registry for tracking active multiroom components.

    Provides weak references to active heartbeat protocols and playback
    schedulers so the health endpoint can access their metrics without
    creating strong circular references.

    All methods are thread-safe.
    """

    def __init__(self) -> None:
        """Initialize the component registry."""
        self._lock = threading.Lock()
        self._heartbeat_protocols: WeakSet[HubHeartbeatProtocol] = WeakSet()
        self._playback_schedulers: WeakSet[PlaybackScheduler] = WeakSet()
        self._last_heartbeat_sent_time: float = 0.0
        self._last_heartbeat_received_time: float = 0.0

    def register_heartbeat_protocol(self, protocol: HubHeartbeatProtocol) -> None:
        """
        Register a heartbeat protocol instance.

        Args:
            protocol: HubHeartbeatProtocol instance to track
        """
        with self._lock:
            self._heartbeat_protocols.add(protocol)
        logger.debug("Registered heartbeat protocol for hub %s", protocol.hub_id)

    def unregister_heartbeat_protocol(self, protocol: HubHeartbeatProtocol) -> None:
        """
        Unregister a heartbeat protocol instance.

        Args:
            protocol: HubHeartbeatProtocol instance to remove
        """
        with self._lock:
            self._heartbeat_protocols.discard(protocol)
        logger.debug("Unregistered heartbeat protocol for hub %s", protocol.hub_id)

    def register_playback_scheduler(self, scheduler: PlaybackScheduler) -> None:
        """
        Register a playback scheduler instance.

        Args:
            scheduler: PlaybackScheduler instance to track
        """
        with self._lock:
            self._playback_schedulers.add(scheduler)
        logger.debug("Registered playback scheduler")

    def unregister_playback_scheduler(self, scheduler: PlaybackScheduler) -> None:
        """
        Unregister a playback scheduler instance.

        Args:
            scheduler: PlaybackScheduler instance to remove
        """
        with self._lock:
            self._playback_schedulers.discard(scheduler)
        logger.debug("Unregistered playback scheduler")

    def record_heartbeat_sent(self) -> None:
        """Record that a heartbeat was sent."""
        with self._lock:
            self._last_heartbeat_sent_time = time.time()

    def record_heartbeat_received(self) -> None:
        """Record that a heartbeat was received."""
        with self._lock:
            self._last_heartbeat_received_time = time.time()

    def get_last_heartbeat_ms(self) -> float | None:
        """
        Get time since last heartbeat activity in milliseconds.

        This returns the most recent of:
        - Time since last heartbeat received from any registered protocol
        - Time since last recorded heartbeat received event

        Returns:
            Milliseconds since last heartbeat, or None if no heartbeats recorded
        """
        with self._lock:
            # Check registered heartbeat protocols
            min_time_since = float("inf")

            for protocol in list(self._heartbeat_protocols):
                try:
                    time_since = protocol.get_time_since_last_heartbeat()
                    if time_since < min_time_since:
                        min_time_since = time_since
                except Exception:
                    # Protocol may have been destroyed
                    logger.debug("Failed to get heartbeat time from protocol")

            # Also check manually recorded heartbeat time
            if self._last_heartbeat_received_time > 0:
                manual_time_since = time.time() - self._last_heartbeat_received_time
                if manual_time_since < min_time_since:
                    min_time_since = manual_time_since

            if min_time_since == float("inf"):
                return None

            return round(min_time_since * 1000, 1)  # Convert to milliseconds

    def get_buffer_level_ms(self) -> float | None:
        """
        Get current audio buffer level in milliseconds.

        If multiple schedulers are registered, returns the average buffer level.

        Returns:
            Buffer level in milliseconds, or None if no schedulers active
        """
        with self._lock:
            if not self._playback_schedulers:
                return None

            total_buffer = 0.0
            count = 0

            for scheduler in list(self._playback_schedulers):
                try:
                    buffer_ms = scheduler.buffer_level_ms()
                    total_buffer += buffer_ms
                    count += 1
                except Exception:
                    # Scheduler may have been destroyed
                    logger.debug("Failed to get buffer level from scheduler")

            if count == 0:
                return None

            return round(total_buffer / count, 1)

    def get_heartbeat_status(self) -> dict[str, Any]:
        """
        Get detailed heartbeat status.

        Returns:
            Dictionary with heartbeat metrics
        """
        with self._lock:
            protocol_count = len(self._heartbeat_protocols)
            last_beat_ms = None

            # Get most recent heartbeat time
            for protocol in list(self._heartbeat_protocols):
                try:
                    time_since = protocol.get_time_since_last_heartbeat()
                    time_ms = round(time_since * 1000, 1)
                    if last_beat_ms is None or time_ms < last_beat_ms:
                        last_beat_ms = time_ms
                except Exception:
                    logger.debug("Heartbeat protocol metric read failed, skipping")

            # Fall back to manual recording
            if last_beat_ms is None and self._last_heartbeat_received_time > 0:
                last_beat_ms = round((time.time() - self._last_heartbeat_received_time) * 1000, 1)

            return {
                "active_protocols": protocol_count,
                "last_beat_ms": last_beat_ms,
                "status": "active" if protocol_count > 0 else "inactive",
            }

    def get_streaming_status(self) -> dict[str, Any]:
        """
        Get detailed audio streaming status.

        Returns:
            Dictionary with streaming metrics
        """
        with self._lock:
            scheduler_count = len(self._playback_schedulers)
            buffer_levels: list[float] = []
            total_underruns = 0
            total_overruns = 0

            for scheduler in list(self._playback_schedulers):
                try:
                    metrics = scheduler.get_metrics()
                    buffer_levels.append(metrics.get("buffer_level_ms", 0.0))
                    total_underruns += metrics.get("underruns", 0)
                    total_overruns += metrics.get("overruns", 0)
                except Exception:
                    logger.debug("Playback scheduler metric read failed, skipping")

            avg_buffer = round(sum(buffer_levels) / len(buffer_levels), 1) if buffer_levels else None

            return {
                "active_schedulers": scheduler_count,
                "buffer_level_ms": avg_buffer,
                "underruns": total_underruns,
                "overruns": total_overruns,
                "status": "streaming" if scheduler_count > 0 else "idle",
            }


# Global component registry instance
_registry: ComponentRegistry | None = None
_registry_lock = threading.Lock()


def get_component_registry() -> ComponentRegistry:
    """Get the global component registry instance (singleton)."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ComponentRegistry()
        return _registry


__all__ = [
    "MAX_LATENCY_SAMPLES",
    "ComponentRegistry",
    "MultiRoomMetrics",
    "get_component_registry",
    "get_multiroom_metrics",
]
