"""
Network quality detection for adaptive behavior.

Detects network quality and adjusts behavior accordingly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import httpx

from core.logging_config import get_logger

logger = get_logger(__name__)


class NetworkQuality(Enum):
    """Network quality levels"""

    OFFLINE = "offline"
    SLOW = "slow"  # < 1 Mbps
    MODERATE = "moderate"  # 1-10 Mbps
    FAST = "fast"  # 10-50 Mbps
    FASTEST = "fastest"  # > 50 Mbps


@dataclass
class NetworkStats:
    """Network statistics"""

    quality: NetworkQuality
    latency_ms: float
    bandwidth_mbps: float | None = None
    reliability: float = 1.0  # 0.0-1.0 success rate
    last_check: float = 0.0


class NetworkQualityDetector:
    """
    Detects network quality and adapts behavior.

    Features:
    - Latency measurement
    - Bandwidth estimation
    - Reliability tracking
    - Quality classification
    """

    def __init__(
        self,
        test_url: str = "https://www.google.com",
        cache_ttl: float = 60.0,  # Cache results for 60 seconds
    ):
        """
        Initialize network quality detector.

        Args:
            test_url: URL to use for network tests
            cache_ttl: Cache TTL in seconds
        """
        self.test_url = test_url
        self.cache_ttl = cache_ttl
        self._stats: NetworkStats | None = None
        self._last_check_time = 0.0

    async def check_quality(self, force: bool = False) -> NetworkQuality:
        """
        Check current network quality.

        Args:
            force: Force check even if cache is valid

        Returns:
            Network quality level
        """
        current_time = time.time()

        # Use cached result if available
        if not force and self._stats and (current_time - self._last_check_time) < self.cache_ttl:
            return self._stats.quality

        # Perform network test
        try:
            latency = await self._measure_latency()
            bandwidth = await self._estimate_bandwidth()

            quality = self._classify_quality(latency, bandwidth)

            self._stats = NetworkStats(
                quality=quality,
                latency_ms=latency * 1000,  # Convert to ms
                bandwidth_mbps=bandwidth,
                reliability=1.0,
                last_check=current_time,
            )
            self._last_check_time = current_time

            logger.debug(
                "Network quality: %s (latency: %.1fms, bandwidth: %.2fMbps)",
                quality.value,
                latency * 1000,
                bandwidth,
            )

            return quality

        except Exception as e:
            logger.warning("Network check failed: %s", e)
            self._stats = NetworkStats(
                quality=NetworkQuality.OFFLINE,
                latency_ms=float("inf"),
                reliability=0.0,
                last_check=current_time,
            )
            return NetworkQuality.OFFLINE

    async def _measure_latency(self) -> float:
        """
        Measure network latency.

        Returns:
            Latency in seconds
        """
        timeouts = [1.0, 2.0, 5.0]  # Try progressively longer timeouts

        for timeout in timeouts:
            try:
                start = time.time()
                async with httpx.AsyncClient(timeout=timeout) as client:
                    await client.get(self.test_url)
                elapsed = time.time() - start
                return elapsed
            except (httpx.TimeoutException, httpx.ConnectError):
                continue

        # All attempts failed
        raise ConnectionError("Could not measure latency")

    async def _estimate_bandwidth(self) -> float:
        """
        Estimate network bandwidth.

        Returns:
            Estimated bandwidth in Mbps
        """
        # Simple estimation: measure download speed of a small test file
        # For now, use latency as a proxy
        latency = await self._measure_latency()

        # Rough estimation: lower latency = higher bandwidth
        if latency < 0.05:  # < 50ms
            return 50.0  # Assume > 50 Mbps
        elif latency < 0.1:  # < 100ms
            return 10.0  # Assume 10-50 Mbps
        elif latency < 0.5:  # < 500ms
            return 1.0  # Assume 1-10 Mbps
        else:
            return 0.1  # Assume < 1 Mbps

    def _classify_quality(self, latency: float, bandwidth: float | None) -> NetworkQuality:
        """
        Classify network quality based on metrics.

        Args:
            latency: Latency in seconds
            bandwidth: Bandwidth in Mbps (optional)

        Returns:
            Network quality level
        """
        latency_ms = latency * 1000

        # Use bandwidth if available
        if bandwidth is not None:
            if bandwidth > 50:
                return NetworkQuality.FASTEST
            elif bandwidth > 10:
                return NetworkQuality.FAST
            elif bandwidth > 1:
                return NetworkQuality.MODERATE
            else:
                return NetworkQuality.SLOW

        # Fall back to latency-based classification
        if latency_ms < 50:
            return NetworkQuality.FASTEST
        elif latency_ms < 100:
            return NetworkQuality.FAST
        elif latency_ms < 500:
            return NetworkQuality.MODERATE
        elif latency_ms < 2000:
            return NetworkQuality.SLOW
        else:
            return NetworkQuality.OFFLINE

    def get_stats(self) -> NetworkStats | None:
        """
        Get current network statistics.

        Returns:
            Network stats or None if not checked yet
        """
        return self._stats

    def is_offline(self) -> bool:
        """Check if network is offline"""
        if not self._stats:
            return False  # Unknown, assume online
        return self._stats.quality == NetworkQuality.OFFLINE

    def should_use_cache(self) -> bool:
        """
        Determine if cache should be used based on network quality.

        Returns:
            True if cache should be preferred
        """
        if not self._stats:
            return False

        return self._stats.quality in [NetworkQuality.SLOW, NetworkQuality.OFFLINE]
