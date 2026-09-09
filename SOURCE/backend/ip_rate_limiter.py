"""
Library-backed sliding-window rate limiter for FastAPI, with optional Redis backend.

When Redis-backed rate limiting is enabled, state is stored through the
``limits`` Redis storage backend for cross-instance enforcement and persistence
across restarts. Otherwise, the same ``limits`` moving-window strategy uses
in-process memory, which is sufficient for desktop and single-process mode.

Endpoints that are NOT rate limited:
  exact health/status probes only                 (health and monitoring)
  /auth/v1/* selected public GoTrue endpoints (proxied endpoints own auth throttles)

Per-path limits:
  /api/v1/health-deep -- 30 req/min  (live dependency checks)
  /v1/command        -- 30 req/min  (LLM cost protection)
  /v1/telemetry/send -- 5 req/min   (admin-triggered external telemetry send)
  /auth/oauth/poll   -- 120 req/min (OAuth status polling)
  /auth/oauth/*      -- 30 req/min  (OAuth start/callback/preflight)
  /auth/register     -- 20 req/min  (registration has deeper 5/hr/IP + 3/hr/email gates)
  /auth/login        -- 20 req/min  (login has deeper brute-force guards)
  /auth/*            -- 30 req/min  (authenticated auth settings/session routes)
  /api/v1/storage/upload-url            -- 20 req/min  (signed upload URL minting)
  /api/v1/storage/internal/upload/*     -- 10 req/min  (unauth signed-token upload path)
  /api/v1/storage/internal/download/*   -- 60 req/min  (unauth signed-token download path)
  everything else    -- 200 req/min (generous desktop default)
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from contracts.fastapi_helpers import SafeJSONResponse
from core.logging_config import get_logger
from fastapi import Request, Response

if TYPE_CHECKING:
    from services.cache.redis_backend import RedisBackend

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path prefixes that bypass rate limiting entirely.
_EXEMPT_PREFIXES: tuple[str, ...] = ()
_EXEMPT_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/live",
        "/health/ready",
        "/health/startup",
        "/health/details",
        "/status",
        "/v1/health",
        "/v1/health/startup",
        "/v1/health/details",
        "/api/v1/health",
        "/api/v1/health/startup",
        "/api/v1/health/details",
        "/api/v1/status",
        "/monitoring/healthz",
        "/monitoring/readyz",
        # SEC-025 (2026-06-09 sweep): "/metrics" and "/v1/monitoring/metrics"
        # are deliberately NOT exempt. They are internal observability surfaces,
        # not public health probes — unauthenticated probing of them must be
        # rate-limited like any other route. The internal Prometheus scrape
        # (docker network, 2 req/min) is far below any per-IP ceiling.
        # These are GoTrue-owned endpoints that Caddy routes through Viola only
        # for response shaping (codex-signup), auth abuse guards (codex-bruteforce),
        # refresh replay detection, and compatibility cookie clearing.
        "/auth/v1/signup",
        "/auth/v1/token",
        "/auth/v1/recover",
        "/auth/v1/otp",
        "/auth/v1/magiclink",
        "/auth/v1/resend",
        "/auth/v1/verify",
        "/auth/v1/logout",
    }
)

# Per-path limit overrides: (path_prefix, max_requests, window_seconds)
# Checked in order; first match wins.
_PATH_LIMITS: tuple[tuple[str, int, int], ...] = (
    ("/admin/api/", 5, 60),  # 5 req/min -- SA-23: admin dashboard protection
    ("/api/admin/", 5, 60),  # 5 req/min -- launch admin write aliases
    ("/api/v1/health-deep", 30, 60),  # 30 req/min -- live dependency checks
    ("/v1/command", 30, 60),  # 30 req/min -- protect LLM quota
    ("/v1/telemetry/send", 5, 60),  # 5 req/min -- manual admin external send trigger
    ("/v1/auth/oauth/poll", 120, 60),  # OAuth poll loops must not starve signup/login.
    ("/auth/oauth/poll", 120, 60),
    ("/v1/auth/oauth/start", 30, 60),
    ("/auth/oauth/start", 30, 60),
    ("/v1/auth/oauth/", 30, 60),
    ("/auth/oauth/", 30, 60),
    ("/v1/auth/register", 20, 60),
    ("/auth/register", 20, 60),
    ("/v1/auth/login", 20, 60),
    ("/auth/login", 20, 60),
    ("/v1/auth/magic-link/", 20, 60),
    ("/auth/magic-link/", 20, 60),
    ("/v1/auth/resend-verification", 10, 60),
    ("/auth/resend-verification", 10, 60),
    ("/v1/auth/", 30, 60),
    ("/auth/", 30, 60),
    ("/api/v1/storage/internal/upload/", 10, 60),
    ("/api/v1/storage/internal/download/", 60, 60),
    ("/api/v1/storage/upload-url", 20, 60),
)

# Fallback for all other paths
_DEFAULT_LIMIT = 200
_DEFAULT_WINDOW = 60

# Legacy prefix retained only so admin reset can clear old keys.
_LEGACY_REDIS_KEY_PREFIX = "viola:ratelimit:ip:"
_REDIS_CLEAR_BUCKET_SUFFIXES: tuple[str, ...] = (
    "v1:command",
    "auth",
    "auth:login",
    "auth:register",
    "auth:magic_link",
    "auth:oauth",
    "auth:oauth:start",
    "auth:oauth:poll",
    "auth:resend_verification",
    "auth:misc",
    "admin:api",
    "telemetry:send",
    "health:deep",
)


# ---------------------------------------------------------------------------
# Core sliding-window store
# ---------------------------------------------------------------------------


class _SlidingWindowStore:
    """Compatibility wrapper around the shared ``limits`` moving-window store."""

    def __init__(self) -> None:
        from services.cache.rate_limit import LimitsSlidingWindowStore

        self._memory = LimitsSlidingWindowStore(record_rejected=True)
        self._seen_limits: dict[str, tuple[int, int]] = {}
        self._redis: RedisBackend | None = None

    def set_redis(self, redis_backend: RedisBackend | None) -> None:
        """Attach a Redis backend for cross-instance rate limiting."""
        self._redis = redis_backend
        logger.info("IP rate limiter upgraded to Redis backend")

    async def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        """
        Check and record a request. Returns True if the request is allowed.

        Uses ``limits`` Redis storage when available, otherwise falls back to
        the same ``limits`` moving-window strategy in memory.
        """
        self._seen_limits[key] = (int(max_requests), int(window_seconds))
        if self._redis is not None:
            from services.cache.rate_limit import redis_rate_limit_enabled

            if not redis_rate_limit_enabled():
                return await self._is_allowed_memory(key, max_requests, window_seconds)
            allowed, redis_error = await self._is_allowed_redis(key, max_requests, window_seconds)
            if not redis_error:
                return allowed
            from services.cache.rate_limit import cloud_rate_limit_fail_closed

            if cloud_rate_limit_fail_closed():
                return False

        return await self._is_allowed_memory(key, max_requests, window_seconds)

    async def _is_allowed_redis(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, bool]:
        """Sliding window check via ``limits`` Redis storage."""
        from services.cache.rate_limit import check_redis_sliding_window

        decision = await check_redis_sliding_window(
            self._redis,
            scope="ip",
            identifier=key,
            limit=max_requests,
            window_seconds=window_seconds,
        )
        return decision.allowed, decision.redis_error

    async def _is_allowed_memory(self, key: str, max_requests: int, window_seconds: int) -> bool:
        """In-memory sliding window check via ``limits``."""
        decision = self._memory.check(
            scope="ip",
            identifier=key,
            limit=max_requests,
            window_seconds=window_seconds,
        )
        return decision.allowed

    async def _cleanup(self, now: float) -> None:
        """Compatibility no-op; ``limits`` storage owns expiry cleanup."""
        del now

    async def clear_prefixes(self, prefixes: list[str]) -> int:
        """Clear buckets whose keys start with one of the supplied prefixes."""
        removed = 0
        keys = [key for key in self._seen_limits if any(key.startswith(prefix) for prefix in prefixes)]
        for key in keys:
            limit, window = self._seen_limits.pop(key)
            removed += self._memory.clear(scope="ip", identifier=key, limit=limit, window_seconds=window)

        if self._redis is not None:
            from services.cache.rate_limit import clear_redis_sliding_window, rate_limit_key

            for prefix in prefixes:
                for suffix in _REDIS_CLEAR_BUCKET_SUFFIXES:
                    identifier = prefix + suffix
                    limit, window = _get_limit_for_bucket_suffix(suffix)
                    try:
                        await clear_redis_sliding_window(
                            self._redis,
                            scope="ip",
                            identifier=identifier,
                            limit=limit,
                            window_seconds=window,
                        )
                        await self._redis.delete(rate_limit_key("ip", identifier, window))
                        await self._redis.delete(_LEGACY_REDIS_KEY_PREFIX + identifier)
                    except Exception:
                        logger.debug("Redis IP rate-limit clear failed for prefix=%s suffix=%s", prefix, suffix)
        return removed


_store = _SlidingWindowStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str:
    """Return the client IP address using the shared trusted-proxy rules."""
    try:
        from auth.ip_utils import extract_client_ip

        return extract_client_ip(request) or "unknown"
    except Exception:
        return request.client.host if request.client else "unknown"


def _get_limit_for_path(path: str) -> tuple[int, int]:
    """Return (max_requests, window_seconds) for the given path."""
    for prefix, max_req, window in _PATH_LIMITS:
        if path.startswith(prefix):
            return max_req, window
    return _DEFAULT_LIMIT, _DEFAULT_WINDOW


def _auth_bucket_suffix_for_path(path: str) -> str | None:
    """Return a route-family auth bucket so OAuth polling cannot starve signup."""
    if path.startswith("/v1/auth/"):
        auth_path = path[3:]
    else:
        auth_path = path

    if not auth_path.startswith("/auth/"):
        return None
    if auth_path.startswith("/auth/oauth/poll"):
        return "auth:oauth:poll"
    if auth_path.startswith("/auth/oauth/start"):
        return "auth:oauth:start"
    if auth_path.startswith("/auth/oauth/"):
        return "auth:oauth"
    if auth_path.startswith("/auth/register"):
        return "auth:register"
    if auth_path.startswith("/auth/login"):
        return "auth:login"
    if auth_path.startswith("/auth/magic-link/"):
        return "auth:magic_link"
    if auth_path.startswith("/auth/resend-verification"):
        return "auth:resend_verification"
    return "auth:misc"


def _bucket_suffix_for_path(path: str) -> str:
    if path == "/v1/command":
        return "v1:command"
    if path.startswith("/admin/api/") or path.startswith("/api/admin/"):
        return "admin:api"
    if path == "/v1/telemetry/send":
        return "telemetry:send"
    if path == "/api/v1/health-deep":
        return "health:deep"
    if path.startswith("/api/v1/storage/internal/upload/"):
        return "storage:internal:upload"
    if path.startswith("/api/v1/storage/internal/download/"):
        return "storage:internal:download"
    if path == "/api/v1/storage/upload-url":
        return "storage:upload-url"
    auth_suffix = _auth_bucket_suffix_for_path(path)
    if auth_suffix is not None:
        return auth_suffix
    return path.split("/")[1] if "/" in path else path


def _get_limit_for_bucket_suffix(suffix: str) -> tuple[int, int]:
    """Best-effort reverse lookup for admin reset cleanup of known buckets."""
    for prefix, limit, window in _PATH_LIMITS:
        if _bucket_suffix_for_path(prefix) == suffix:
            return limit, window
    return _DEFAULT_LIMIT, _DEFAULT_WINDOW


def _is_exempt(path: str) -> bool:
    """Return True if the path should bypass rate limiting."""
    return path in _EXEMPT_EXACT_PATHS or any(path.startswith(p) for p in _EXEMPT_PREFIXES)


# ---------------------------------------------------------------------------
# FastAPI middleware
# ---------------------------------------------------------------------------


class IPRateLimitMiddleware:
    """Pure-ASGI sliding-window rate limiter (Redis-aware).

    Pure ASGI is required (vs FastAPI ``@app.middleware("http")`` which
    wraps the function as ``BaseHTTPMiddleware``) because the inner anyio
    TaskGroup BaseHTTPMiddleware spawns breaks asyncpg's loop-bound
    connection waiters with ``RuntimeError: got Future ... attached to a
    different loop``. Keeping this on the same loop fixes downstream DB
    access in auth + billing + memory paths.

    Behavior:
    - Exempt paths (/health, /v1/health) are never limited.
    - /v1/command: 30 req/min per IP
    - /auth/* and /v1/auth/*: 10 req/min per IP
    - Everything else: 200 req/min per IP
    - Returns HTTP 429 with Retry-After header when exceeded.
    - ``VIOLA_TEST_BYPASS_LIMITS=1`` disables (regression + local dev).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        import os as _os

        path = scope.get("path", "") or ""
        if _is_exempt(path):
            await self.app(scope, receive, send)
            return

        if _os.environ.get("VIOLA_TEST_BYPASS_LIMITS") == "1":
            if not (path.startswith("/auth/") or path.startswith("/v1/auth/")):
                await self.app(scope, receive, send)
                return

        # Build a Request just to read the client IP (header-aware).
        request = Request(scope, receive)
        client_ip = _get_client_ip(request)
        max_req, window = _get_limit_for_path(path)
        bucket_key = f"{client_ip}:{_bucket_suffix_for_path(path)}"

        allowed = await _store.is_allowed(bucket_key, max_req, window)

        if not allowed:
            logger.warning(
                "Rate limit exceeded: ip=%s path=%s limit=%d/%ds",
                client_ip,
                path,
                max_req,
                window,
            )

            is_auth_path = path.startswith("/v1/auth/") or path.startswith("/auth/")
            try:
                from admin.instrumentation import record_abuse_signal

                record_abuse_signal(
                    "ip_rate_limit",
                    severity="critical" if is_auth_path else "warning",
                    details={
                        "ip": client_ip,
                        "path": path,
                        "limit": "%d/%ds" % (max_req, window),
                    },
                )
            except Exception:
                pass

            retry_after = window
            response = SafeJSONResponse(
                status_code=HTTPStatus.TOO_MANY_REQUESTS,
                content={
                    "ok": False,
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Please slow down.",
                        "details": {"retry_after_seconds": retry_after},
                    },
                    "data": None,
                },
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def attach_ip_rate_limiter(app: Any) -> None:
    """
    Attach the IP rate limiter to a FastAPI app.

    Call this once during app construction. Idempotent -- safe to call
    multiple times (skips if already installed).
    """
    if getattr(app.state, "_ip_rate_limiter_installed", False):
        return

    app.add_middleware(IPRateLimitMiddleware)
    app.state._ip_rate_limiter_installed = True
    logger.info("IP rate limiter enabled " "(/v1/command: 30/min, auth: 10/min, default: 200/min)")


def get_ip_rate_limit_store() -> _SlidingWindowStore:
    """Return the module-level store (for Redis injection at startup)."""
    return _store


async def clear_ip_rate_limits_for_ips(ips: list[str]) -> int:
    """Clear known IP-scoped buckets for the supplied client IPs."""
    prefixes = [f"{ip}:" for ip in ips if ip]
    if not prefixes:
        return 0
    return await _store.clear_prefixes(prefixes)
