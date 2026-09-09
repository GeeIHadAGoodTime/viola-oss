"""
Latency Provider for Multi-Room Sync.

Provides device-specific latency information for accurate audio synchronization.
Integrates with DeviceProfile to read calibrated AEC delay values.

Usage:
    from audio_core.latency_provider import DeviceProfileLatencyProvider

    provider = DeviceProfileLatencyProvider()
    latency_ms = provider.get_latency_ms("device-abc123")
"""

from __future__ import annotations

import threading
from typing import Protocol

from core.logging_config import get_logger

logger = get_logger(__name__)

# Default latency values for different device types (in milliseconds)
WIRED_DEFAULT_MS: float = 20.0
BLUETOOTH_DEFAULT_MS: float = 150.0

# Latency validation range
LATENCY_MIN_MS: float = 0.0
LATENCY_MAX_MS: float = 500.0


class LatencyProvider(Protocol):
    """Protocol for latency providers used by SyncEngine."""

    def get_latency_ms(self, device_id: str) -> float:
        """
        Get latency in milliseconds for the specified device.

        Args:
            device_id: Device identifier (fingerprint)

        Returns:
            Latency in milliseconds
        """
        ...

    def set_latency_ms(self, device_id: str, latency: float) -> None:
        """
        Set manual latency override for the specified device.

        Args:
            device_id: Device identifier (fingerprint)
            latency: Latency in milliseconds
        """
        ...

    def reset_to_default(self, device_id: str) -> None:
        """
        Reset device latency to default (remove manual override).

        Args:
            device_id: Device identifier (fingerprint)
        """
        ...


class DeviceProfileLatencyProvider:
    """
    Latency provider that reads from DeviceProfile calibration data.

    Integrates with the wake detector's device profile manager to use
    calibrated AEC delay values for accurate sync timing.

    Thread-safe with internal caching for performance.
    """

    # Maximum number of device entries to cache. In multi-room setups a
    # household typically has fewer than 50 devices; 200 provides headroom
    # while preventing unbounded growth from device-id churn.
    _MAX_CACHE_SIZE: int = 200

    def __init__(self) -> None:
        """Initialize the latency provider."""
        self._lock = threading.RLock()
        # Cache: device_id -> latency_ms
        self._cache: dict[str, float] = {}
        # Manual overrides: device_id -> latency_ms
        self._overrides: dict[str, float] = {}
        logger.info("DeviceProfileLatencyProvider initialized")

    def get_latency_ms(self, device_id: str) -> float:
        """
        Get latency in milliseconds for the specified device.

        Priority:
        1. Manual override (if set)
        2. Cached value
        3. DeviceProfile aec_delay_ms
        4. Default based on device type (wired vs Bluetooth)

        Args:
            device_id: Device identifier (fingerprint)

        Returns:
            Latency in milliseconds
        """
        with self._lock:
            # Check for manual override first
            if device_id in self._overrides:
                return self._overrides[device_id]

            # Check cache
            if device_id in self._cache:
                return self._cache[device_id]

            # Load from DeviceProfile
            latency_ms = self._load_from_profile(device_id)

            # Enforce max cache size before inserting
            if len(self._cache) >= self._MAX_CACHE_SIZE:
                # Evict an arbitrary entry (dict iteration order is insertion order)
                try:
                    oldest_key = next(iter(self._cache))
                    del self._cache[oldest_key]
                except StopIteration:
                    pass

            self._cache[device_id] = latency_ms
            return latency_ms

    def set_latency_ms(self, device_id: str, latency: float) -> None:
        """
        Set manual latency override for the specified device.

        Args:
            device_id: Device identifier (fingerprint)
            latency: Latency in milliseconds (0-500ms range)

        Raises:
            ValueError: If latency is outside valid range
        """
        if not LATENCY_MIN_MS <= latency <= LATENCY_MAX_MS:
            raise ValueError(
                "Latency must be between %s and %s ms, got %s",
                LATENCY_MIN_MS,
                LATENCY_MAX_MS,
                latency,
            )

        with self._lock:
            self._overrides[device_id] = latency
            # Update cache as well
            self._cache[device_id] = latency
            logger.info(
                "Set manual latency override for device %s: %s ms",
                device_id,
                format(latency, ".1f"),
            )

    def reset_to_default(self, device_id: str) -> None:
        """
        Reset device latency to default (remove manual override).

        Args:
            device_id: Device identifier (fingerprint)
        """
        with self._lock:
            # Remove override
            if device_id in self._overrides:
                del self._overrides[device_id]
                logger.info("Removed manual latency override for device %s", device_id)

            # Clear cache to force re-read from profile
            if device_id in self._cache:
                del self._cache[device_id]

    def invalidate_cache(self, device_id: str | None = None) -> None:
        """
        Invalidate cache for a device or all devices.

        Args:
            device_id: Specific device to invalidate, or None for all
        """
        with self._lock:
            if device_id is None:
                self._cache.clear()
                logger.debug("Invalidated all latency cache entries")
            elif device_id in self._cache:
                del self._cache[device_id]
                logger.debug("Invalidated latency cache for device %s", device_id)

    def _load_from_profile(self, device_id: str) -> float:
        """
        Load latency from DeviceProfile.

        Args:
            device_id: Device identifier (fingerprint)

        Returns:
            Latency in milliseconds
        """
        try:
            from voice.wake_detector.device_profile_manager import load_profile

            profile = load_profile(device_id)
            if profile is not None and profile.aec_delay_ms is not None:
                latency_ms = profile.aec_delay_ms
                logger.debug(
                    "Loaded latency from profile for device %s: %s ms",
                    device_id,
                    format(latency_ms, ".1f"),
                )
                return latency_ms

            # No calibrated delay, use default based on device type
            if profile is not None and profile.output_is_bluetooth:
                logger.debug(
                    "Using Bluetooth default latency for device %s: %s ms",
                    device_id,
                    BLUETOOTH_DEFAULT_MS,
                )
                return BLUETOOTH_DEFAULT_MS

        except Exception as e:
            logger.debug("Failed to load profile for device %s: %s", device_id, e)

        # Fallback to wired default
        logger.debug(
            "Using wired default latency for device %s: %s ms",
            device_id,
            WIRED_DEFAULT_MS,
        )
        return WIRED_DEFAULT_MS


__all__ = [
    "BLUETOOTH_DEFAULT_MS",
    "LATENCY_MAX_MS",
    "LATENCY_MIN_MS",
    "WIRED_DEFAULT_MS",
    "DeviceProfileLatencyProvider",
    "LatencyProvider",
]
