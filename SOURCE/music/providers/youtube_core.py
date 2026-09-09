"""
Shared YouTube provider cache helpers and browser-only compatibility shims.

Product policy is browser-based YouTube search via youtube_iframe/browser_search.
The old API search core remains importable for compatibility, but any runtime
use fails closed before network access.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import requests

from config.settings import settings
from core.logging_config import get_logger

from .models import SearchResults, TrackSummary
from .youtube_music_cache import create_search_cache

if TYPE_CHECKING:
    from .youtube_music_cache import PersistentSearchCache, _SearchCache

logger = get_logger("viola.music.providers.youtube_core")

# Legacy quota guard configuration. Retained for import compatibility.
QUOTA_COOLDOWN_HOURS = 2

# Video ID pattern exported for browser-based YouTube paths.
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# HTTP status codes considered transient by remaining OAuth playlist modules.
_TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def _youtube_external_api_disabled_error(
    provider_id: str,
    operation: str,
    *,
    query: str | None = None,
) -> Exception:
    from .errors import MusicProviderUnavailableError

    details: dict[str, Any] = {
        "root_cause": "youtube_browser_provider_required",
        "kind": "PROVIDER_DISABLED",
        "provider": provider_id,
        "operation": operation,
        "required_path": "youtube_iframe.browser_search",
    }
    if query is not None:
        details["query"] = query[:50]
    return MusicProviderUnavailableError(
        "Legacy YouTube API search is disabled. Use the browser-based youtube_iframe/browser_search path.",
        technical_details=details,
    )


def _raise_youtube_external_api_disabled(provider_id: str, operation: str, *, query: str | None = None) -> None:
    raise _youtube_external_api_disabled_error(provider_id, operation, query=query)


def _is_transient_http_error(exc: BaseException) -> bool:
    """Return whether an HTTP exception is safe for callers to retry."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        if response is not None and response.status_code in _TRANSIENT_HTTP_CODES:
            return True
    return False


class _PerUserSearchRateLimiter:
    """
    Legacy in-memory sliding window limiter retained for imports/tests.

    The legacy API search runtime path is disabled before this limiter is reached.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = defaultdict(deque)

    def check_or_raise(self, user_id: str) -> None:
        """
        Check if the user is within rate limits. Raise if exceeded.

        Kept for compatibility with tests and older imports.
        """
        max_requests = settings.youtube_search_rate_limit_per_user
        window_seconds = settings.youtube_search_rate_limit_window_seconds

        redis_decision = self._check_redis(user_id, max_requests=max_requests, window_seconds=window_seconds)
        if redis_decision is not None:
            return

        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            window = self._windows[user_id]

            while window and window[0] <= cutoff:
                window.popleft()

            if len(window) >= max_requests:
                from .errors import MusicProviderUnavailableError

                logger.warning(
                    "User %s exceeded YouTube search rate limit: %s/%s per %ss",
                    user_id,
                    len(window),
                    max_requests,
                    window_seconds,
                )
                raise MusicProviderUnavailableError(
                    "Too many searches. Please wait a moment before searching again.",
                    technical_details={
                        "root_cause": "per_user_rate_limit",
                        "kind": "RATE_LIMIT",
                        "provider": "youtube",
                        "user_id": user_id,
                        "limit": max_requests,
                        "window_seconds": window_seconds,
                    },
                )

            window.append(now)

    def _check_redis(self, user_id: str, *, max_requests: int, window_seconds: int) -> bool | None:
        from services.cache.rate_limit import (
            check_redis_sliding_window_sync,
            cloud_rate_limit_fail_closed,
            redis_rate_limit_enabled,
        )

        if not redis_rate_limit_enabled():
            return None

        decision = check_redis_sliding_window_sync(
            scope="youtube.search",
            identifier=user_id,
            limit=max_requests,
            window_seconds=window_seconds,
        )
        if decision.redis_error and not cloud_rate_limit_fail_closed():
            return None
        if not decision.allowed:
            from .errors import MusicProviderUnavailableError

            logger.warning(
                "User %s exceeded YouTube search rate limit: %s/%s per %ss",
                user_id,
                decision.count,
                max_requests,
                window_seconds,
            )
            raise MusicProviderUnavailableError(
                "Too many searches. Please wait a moment before searching again.",
                technical_details={
                    "root_cause": "per_user_rate_limit",
                    "kind": "RATE_LIMIT",
                    "provider": "youtube",
                    "user_id": user_id,
                    "limit": max_requests,
                    "window_seconds": window_seconds,
                },
            )
        return True


_per_user_rate_limiter = _PerUserSearchRateLimiter()

_shared_search_cache: PersistentSearchCache | _SearchCache | None = None
_cache_lock = threading.Lock()


def get_shared_search_cache() -> PersistentSearchCache | _SearchCache:
    """
    Get the singleton search cache instance.

    Browser search uses this cache; the legacy API search writer is disabled.
    """
    global _shared_search_cache
    with _cache_lock:
        if _shared_search_cache is None:
            _shared_search_cache = create_search_cache(
                persistent=True,
                ttl_days=30,
                max_entries=500,
            )
            logger.info("YouTube shared search cache initialized")
        return _shared_search_cache


def clear_shared_search_cache() -> int:
    """Clear the shared search cache. Returns count of entries cleared."""
    cache = get_shared_search_cache()
    return cache.clear()


def get_shared_cache_stats() -> dict[str, Any]:
    """Get statistics for the shared search cache."""
    cache = get_shared_search_cache()
    return cache.stats()


class SharedQuotaGuard:
    """
    Legacy quota cooldown guard retained for import compatibility.

    The browser search path does not consume external API quota.
    """

    _instance: SharedQuotaGuard | None = None
    _lock = threading.Lock()

    def __new__(cls) -> SharedQuotaGuard:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self._exhausted_until: datetime | None = None
        self._state_lock = threading.RLock()

    def check_or_raise(self) -> None:
        """Raise if a legacy quota cooldown is active."""
        from .errors import MusicProviderUnavailableError

        with self._state_lock:
            if self._exhausted_until is not None:
                now = datetime.now(UTC)
                if now < self._exhausted_until:
                    raise MusicProviderUnavailableError(
                        "YouTube is busy right now. Try again in a moment.",
                        technical_details={
                            "root_cause": "youtube_legacy_quota_cooldown",
                            "kind": "QUOTA",
                            "provider": "youtube",
                            "exhausted_until": self._exhausted_until.isoformat(),
                        },
                    )
                self._exhausted_until = None

    def mark_exhausted(self) -> None:
        """Mark the legacy quota as exhausted for the cooldown period."""
        with self._state_lock:
            self._exhausted_until = datetime.now(UTC) + timedelta(hours=QUOTA_COOLDOWN_HOURS)
            logger.warning("YouTube legacy quota cooldown until %s", self._exhausted_until.isoformat())

    def is_exhausted(self) -> bool:
        """Return True if quota cooldown is currently active."""
        with self._state_lock:
            if self._exhausted_until is None:
                return False
            return datetime.now(UTC) < self._exhausted_until


def get_shared_quota_guard() -> SharedQuotaGuard:
    """Get the singleton quota guard."""
    return SharedQuotaGuard()


class YouTubeSearchCore:
    """
    Legacy YouTube API search shim.

    Browser search is the only supported YouTube search path. This class is
    retained so old imports fail closed with an actionable error instead of
    silently falling back to API-key/OAuth API calls.
    """

    def __init__(
        self,
        auth_mode: Literal["oauth", "api_key"],
        *,
        get_oauth_token: Callable[[str], str | None] | None = None,
        get_api_key: Callable[[], str | None] | None = None,
        provider_id: str = "youtube",
        use_music_category: bool = False,
    ):
        """
        Initialize the legacy shim.

        Args are retained for source compatibility only. They no longer enable
        legacy API search, including the old API-key fallback.
        """
        self.auth_mode = auth_mode
        self._get_oauth_token = get_oauth_token
        self._get_api_key = get_api_key
        self.provider_id = provider_id
        self.use_music_category = use_music_category
        self._cache = get_shared_search_cache()
        self._quota_guard = get_shared_quota_guard()
        self._api_call_count = 0
        self._api_call_lock = threading.RLock()

        if auth_mode == "api_key":
            logger.warning(
                "youtube_core: API-key YouTube fallback disabled for provider=%s; use youtube_iframe/browser_search",
                provider_id,
            )

    def search_tracks(
        self,
        query: str,
        limit: int = 25,
        user_id: str | None = None,
        cursor: str | None = None,
    ) -> SearchResults:
        """Fail closed for the retired YouTube API search path."""
        if not user_id:
            raise ValueError("user_id is required")

        _ = (limit, cursor)
        _raise_youtube_external_api_disabled(self.provider_id, "retired_search", query=query)
        return SearchResults(items=[], next_cursor=None, total=0, query=query)

    def _make_api_request(
        self,
        query: str,
        limit: int,
        user_id: str,
        cursor: str | None,
    ) -> dict:
        """Fail closed for the retired API request path."""
        _ = (limit, user_id, cursor)
        _raise_youtube_external_api_disabled(self.provider_id, "retired_search", query=query)
        return {}

    def _do_search_request(self, params: dict, headers: dict) -> dict:
        """Fail closed for the retired HTTP request path."""
        _ = (params, headers)
        _raise_youtube_external_api_disabled(self.provider_id, "retired_search")
        return {}

    def _build_auth(self, user_id: str) -> tuple[dict, dict]:
        """Fail closed for retired API auth construction."""
        _ = user_id
        _raise_youtube_external_api_disabled(self.provider_id, "retired_auth")
        return {}, {}

    def _validate_video_availability(
        self,
        tracks: list[TrackSummary],
        user_id: str,
    ) -> list[TrackSummary]:
        """Fail closed for retired video availability validation."""
        _ = (tracks, user_id)
        _raise_youtube_external_api_disabled(self.provider_id, "retired_video_validation")
        return []

    def _handle_http_error(self, exc: requests.exceptions.HTTPError, query: str) -> SearchResults:
        """Fail closed for retired API HTTP error handling."""
        raise _youtube_external_api_disabled_error(self.provider_id, "retired_search", query=query) from exc

    def _parse_results(self, data: dict, query: str) -> SearchResults:
        """Fail closed for retired API response parsing."""
        _ = data
        _raise_youtube_external_api_disabled(self.provider_id, "retired_search", query=query)
        return SearchResults(items=[], next_cursor=None, total=0, query=query)

    def _handle_test_mode(self, query: str) -> SearchResults | None:
        """Test mode no longer bypasses the retired API path."""
        _ = query
        return None

    def evict_cache_entry(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 1,
    ) -> bool:
        """Evict a specific shared browser-search cache entry."""
        if not user_id:
            raise ValueError("user_id is required")
        cache_key = f"{user_id}:{query}:{limit}"
        return self._cache.evict(cache_key)
