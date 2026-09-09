"""YouTube embed health monitoring.

Probes YouTube embed availability to detect issues before users report them.
This catches:
- YouTube API quota issues
- Embed blocking (region, age-restricted, etc.)
- Network connectivity problems
- Rate limiting
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import httpx

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class YouTubeHealthStatus(Enum):
    """YouTube embed health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Slow but working
    BLOCKED = "blocked"  # Embed blocked (403, region, etc.)
    QUOTA_EXCEEDED = "quota_exceeded"  # YouTube API quota hit
    NETWORK_ERROR = "network_error"  # Can't reach YouTube
    UNKNOWN = "unknown"  # Unknown error


@dataclass
class YouTubeHealthResult:
    """Result of a YouTube health check."""

    status: YouTubeHealthStatus
    latency_ms: float | None
    error: str | None
    last_check: float  # Unix timestamp
    details: dict | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "last_check": self.last_check,
            "last_check_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_check)),
            "details": self.details,
        }


# Well-known video ID that should always be embeddable
# "Me at the zoo" - first YouTube video, public domain
TEST_VIDEO_ID = "jNQXAC9IVRw"

# Latency thresholds
LATENCY_DEGRADED_MS = 2000  # >2s is degraded


async def check_youtube_embed_health(
    video_id: str = TEST_VIDEO_ID,
    timeout: float = TIMEOUT_LONG,
) -> YouTubeHealthResult:
    """Check if YouTube embeds are working.

    Uses a HEAD request to YouTube's embed endpoint to verify:
    1. Network connectivity to YouTube
    2. Embed endpoint responds
    3. No blocking or quota issues

    Args:
        video_id: Video ID to test (default: "Me at the zoo")
        timeout: Request timeout in seconds

    Returns:
        YouTubeHealthResult with status, latency, and any errors
    """
    embed_url = f"https://www.youtube.com/embed/{video_id}"
    start = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.head(embed_url, follow_redirects=True)
            latency = (time.perf_counter() - start) * 1000

            if response.status_code == 200:
                status = YouTubeHealthStatus.HEALTHY
                if latency > LATENCY_DEGRADED_MS:
                    status = YouTubeHealthStatus.DEGRADED

                return YouTubeHealthResult(
                    status=status,
                    latency_ms=round(latency, 2),
                    error=None,
                    last_check=time.time(),
                    details={
                        "video_id": video_id,
                        "response_headers": dict(response.headers),
                    },
                )

            elif response.status_code == 403:
                # Could be blocked, region-restricted, or quota
                return YouTubeHealthResult(
                    status=YouTubeHealthStatus.BLOCKED,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error="YouTube returned 403 - embeds may be blocked",
                    last_check=time.time(),
                    details={"status_code": 403, "video_id": video_id},
                )

            elif response.status_code == 429:
                return YouTubeHealthResult(
                    status=YouTubeHealthStatus.QUOTA_EXCEEDED,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error="YouTube returned 429 - rate limited",
                    last_check=time.time(),
                    details={"status_code": 429, "video_id": video_id},
                )

            else:
                return YouTubeHealthResult(
                    status=YouTubeHealthStatus.DEGRADED,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=f"Unexpected status code: {response.status_code}",
                    last_check=time.time(),
                    details={
                        "status_code": response.status_code,
                        "video_id": video_id,
                    },
                )

    except httpx.TimeoutException:
        return YouTubeHealthResult(
            status=YouTubeHealthStatus.NETWORK_ERROR,
            latency_ms=timeout * 1000,
            error=f"Request timed out after {timeout}s",
            last_check=time.time(),
            details={"video_id": video_id, "timeout": timeout},
        )

    except httpx.ConnectError as e:
        return YouTubeHealthResult(
            status=YouTubeHealthStatus.NETWORK_ERROR,
            latency_ms=None,
            error=f"Connection error: {e}",
            last_check=time.time(),
            details={"video_id": video_id},
        )

    except Exception as e:
        logger.exception("YouTube health check failed unexpectedly")
        return YouTubeHealthResult(
            status=YouTubeHealthStatus.UNKNOWN,
            latency_ms=None,
            error=str(e),
            last_check=time.time(),
            details={"video_id": video_id, "exception_type": type(e).__name__},
        )


def check_youtube_embed_health_sync(
    video_id: str = TEST_VIDEO_ID,
    timeout: float = TIMEOUT_LONG,
) -> YouTubeHealthResult:
    """Synchronous version of check_youtube_embed_health.

    Use this in contexts where async is not available.
    """
    embed_url = f"https://www.youtube.com/embed/{video_id}"
    start = time.perf_counter()

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.head(embed_url, follow_redirects=True)
            latency = (time.perf_counter() - start) * 1000

            if response.status_code == 200:
                status = YouTubeHealthStatus.HEALTHY
                if latency > LATENCY_DEGRADED_MS:
                    status = YouTubeHealthStatus.DEGRADED

                return YouTubeHealthResult(
                    status=status,
                    latency_ms=round(latency, 2),
                    error=None,
                    last_check=time.time(),
                )

            elif response.status_code == 403:
                return YouTubeHealthResult(
                    status=YouTubeHealthStatus.BLOCKED,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error="YouTube returned 403",
                    last_check=time.time(),
                )

            elif response.status_code == 429:
                return YouTubeHealthResult(
                    status=YouTubeHealthStatus.QUOTA_EXCEEDED,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error="YouTube returned 429",
                    last_check=time.time(),
                )

            else:
                return YouTubeHealthResult(
                    status=YouTubeHealthStatus.DEGRADED,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=f"Status: {response.status_code}",
                    last_check=time.time(),
                )

    except Exception as e:
        return YouTubeHealthResult(
            status=YouTubeHealthStatus.UNKNOWN,
            latency_ms=None,
            error=str(e),
            last_check=time.time(),
        )
