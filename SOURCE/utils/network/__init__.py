"""
Network resilience utilities for Viola.

Provides:
- Resilient HTTP client with auto-retry
- Network quality detection
- Graceful degradation when offline
"""

from __future__ import annotations

from utils.network.quality_detector import NetworkQuality, NetworkQualityDetector
from utils.network.resilient_client import ResilientClient, resilient_fetch

__all__ = [
    "NetworkQuality",
    "NetworkQualityDetector",
    "ResilientClient",
    "resilient_fetch",
]
