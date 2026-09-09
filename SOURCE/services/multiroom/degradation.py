"""
Multi-Room Sync Degradation Management

Manages graceful degradation levels for multi-room synchronization
when components fail or become unavailable.

Usage:
    from services.multiroom.degradation import DegradationManager, DegradationLevel

    manager = DegradationManager()
    manager.on_component_failure("cloud_relay")
    level = manager.current_level
    features = manager.get_available_features(level)
"""

from __future__ import annotations

import threading
from enum import StrEnum

from core.logging_config import get_logger

logger = get_logger(__name__)


class DegradationLevel(StrEnum):
    """
    Degradation levels for multi-room synchronization.

    Ordered from fully operational to most degraded:
    - FULL: All components healthy, full functionality
    - CLOUD_DEGRADED: Cloud relay unavailable, local P2P sync only
    - PARTIAL_SYNC: Some nodes unreachable, partial sync
    - SINGLE_DEVICE: Local-only mode, no sync
    """

    FULL = "full"
    CLOUD_DEGRADED = "cloud"
    PARTIAL_SYNC = "partial"
    SINGLE_DEVICE = "single"


# Feature availability by degradation level
_FEATURE_AVAILABILITY: dict[DegradationLevel, set[str]] = {
    DegradationLevel.FULL: {
        "cloud_sync",
        "p2p_sync",
        "device_discovery",
        "multi_room_playback",
        "room_groups",
        "remote_control",
        "conversation_sync",
    },
    DegradationLevel.CLOUD_DEGRADED: {
        "p2p_sync",
        "device_discovery",
        "multi_room_playback",
        "room_groups",
        "conversation_sync",
    },
    DegradationLevel.PARTIAL_SYNC: {
        "p2p_sync",
        "device_discovery",
        "multi_room_playback",
    },
    DegradationLevel.SINGLE_DEVICE: {
        "local_playback",
    },
}


class DegradationManager:
    """
    Manages graceful degradation for multi-room synchronization.

    Tracks component health and determines the current degradation level.
    Thread-safe for concurrent access.

    Components tracked:
    - cloud_relay: Cloud synchronization service
    - node_registry: Local node registry
    - device_discovery: mDNS/UDP device discovery
    - heartbeat: Node heartbeat monitoring
    - audio_streaming: Audio streaming between nodes
    """

    # Component to degradation impact mapping
    _COMPONENT_IMPACTS: dict[str, DegradationLevel] = {
        "cloud_relay": DegradationLevel.CLOUD_DEGRADED,
        "node_registry": DegradationLevel.SINGLE_DEVICE,
        "device_discovery": DegradationLevel.PARTIAL_SYNC,
        "heartbeat": DegradationLevel.PARTIAL_SYNC,
        "audio_streaming": DegradationLevel.PARTIAL_SYNC,
    }

    def __init__(self) -> None:
        """Initialize the degradation manager."""
        self._lock = threading.Lock()
        self._failed_components: set[str] = set()
        self._current_level = DegradationLevel.FULL
        logger.info("DegradationManager initialized")

    @property
    def current_level(self) -> DegradationLevel:
        """Get the current degradation level."""
        with self._lock:
            return self._current_level

    @property
    def failed_components(self) -> set[str]:
        """Get the set of currently failed components."""
        with self._lock:
            return self._failed_components.copy()

    def check_health(self) -> DegradationLevel:
        """
        Recalculate and return the current degradation level.

        Returns:
            Current degradation level based on failed components.
        """
        with self._lock:
            return self._calculate_level()

    def _calculate_level(self) -> DegradationLevel:
        """
        Calculate degradation level from failed components.

        Must be called with lock held.
        """
        if not self._failed_components:
            return DegradationLevel.FULL

        # Determine the most severe degradation from all failed components
        worst_level = DegradationLevel.FULL

        # Define severity ordering
        severity_order = [
            DegradationLevel.FULL,
            DegradationLevel.CLOUD_DEGRADED,
            DegradationLevel.PARTIAL_SYNC,
            DegradationLevel.SINGLE_DEVICE,
        ]

        for component in self._failed_components:
            impact = self._COMPONENT_IMPACTS.get(component, DegradationLevel.PARTIAL_SYNC)
            if severity_order.index(impact) > severity_order.index(worst_level):
                worst_level = impact

        return worst_level

    def on_component_failure(self, component: str) -> DegradationLevel:
        """
        Record a component failure and update degradation level.

        Args:
            component: Name of the failed component

        Returns:
            New degradation level after the failure
        """
        with self._lock:
            if component in self._failed_components:
                return self._current_level

            self._failed_components.add(component)
            new_level = self._calculate_level()

            if new_level != self._current_level:
                old_level = self._current_level
                self._current_level = new_level
                logger.warning(
                    "Degradation level changed: %s -> %s (component failed: %s)",
                    old_level.value,
                    new_level.value,
                    component,
                )
            else:
                logger.debug("Component failed but degradation level unchanged: %s", component)

            return self._current_level

    def on_component_recovery(self, component: str) -> DegradationLevel:
        """
        Record a component recovery and update degradation level.

        Args:
            component: Name of the recovered component

        Returns:
            New degradation level after the recovery
        """
        with self._lock:
            if component not in self._failed_components:
                return self._current_level

            self._failed_components.remove(component)
            new_level = self._calculate_level()

            if new_level != self._current_level:
                old_level = self._current_level
                self._current_level = new_level
                logger.info(
                    "Degradation level improved: %s -> %s (component recovered: %s)",
                    old_level.value,
                    new_level.value,
                    component,
                )
            else:
                logger.debug("Component recovered but degradation level unchanged: %s", component)

            return self._current_level

    def get_available_features(self, level: DegradationLevel | None = None) -> set[str]:
        """
        Get features available at a given degradation level.

        Args:
            level: Degradation level to check. If None, uses current level.

        Returns:
            Set of available feature names.
        """
        if level is None:
            level = self.current_level

        return _FEATURE_AVAILABILITY.get(level, set())

    def is_feature_available(self, feature: str, level: DegradationLevel | None = None) -> bool:
        """
        Check if a specific feature is available.

        Args:
            feature: Feature name to check
            level: Degradation level to check. If None, uses current level.

        Returns:
            True if the feature is available.
        """
        return feature in self.get_available_features(level)

    def reset(self) -> None:
        """Reset to full health state."""
        with self._lock:
            old_level = self._current_level
            self._failed_components.clear()
            self._current_level = DegradationLevel.FULL

        if old_level != DegradationLevel.FULL:
            logger.info("Degradation manager reset: %s -> full", old_level.value)

    def to_dict(self) -> dict[str, object]:
        """
        Serialize degradation state to a dictionary.

        Returns:
            Dictionary containing degradation state.
        """
        with self._lock:
            return {
                "level": self._current_level.value,
                "failed_components": list(self._failed_components),
                "available_features": list(self.get_available_features(self._current_level)),
            }


# Global degradation manager instance
_manager: DegradationManager | None = None
_manager_lock = threading.Lock()


def get_degradation_manager() -> DegradationManager:
    """Get the global degradation manager instance (singleton)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DegradationManager()
        return _manager


__all__ = [
    "DegradationLevel",
    "DegradationManager",
    "get_degradation_manager",
]
