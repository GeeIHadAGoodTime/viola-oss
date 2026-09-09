"""
Authentication API Routes for Viola.

This module provides FastAPI routes for user authentication including
registration, login, OAuth, magic links, and session management.

Usage:
    >>> from fastapi import FastAPI
    >>> from auth.routes import auth_router
    >>> app = FastAPI()
    >>> app.include_router(auth_router, prefix="/auth")

Endpoints:
    POST /auth/register           - Email/password registration
    POST /auth/login              - Email/password login
    POST /auth/logout             - Logout (revoke session)
    POST /auth/magic-link/request - Request magic link email
    GET  /auth/magic-link/verify  - Verify magic link token
    GET  /auth/verify-email       - Verify email address with token
    POST /auth/resend-verification - Resend verification email
    GET  /auth/oauth/google       - Initiate Google OAuth
    GET  /auth/oauth/google/callback - Google OAuth callback
    GET  /auth/oauth/apple        - Initiate Apple OAuth
    GET  /auth/oauth/apple/callback - Apple OAuth callback
    POST /auth/password/change    - Change password for signed-in user
    GET  /auth/me                 - Get current user
    GET  /auth/sessions           - List active sessions
    DELETE /auth/sessions/{id}    - Revoke specific session
"""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from auth.csrf import clear_csrf_cookie, csrf_required
from auth.dependencies import (
    get_current_session,
    get_current_user,
    require_auth_token,
    require_real_user,
)
from auth.middleware import clear_refresh_cookie, clear_session_cookie
from auth.models import (
    Session,
    User,
    build_user_payload_with_entitlement,
)
from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)

logger = get_logger("viola.auth.routes")

log = logger  # Alias for consistency with validation helper


from auth.utils import mask_email as _mask_email


def _raise_messaging_channel_unavailable(channel: str) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "error": "messaging_channel_unavailable",
            "message": "%s account linking is not enabled for this launch." % channel,
        },
    )


_COMMON_PASSWORDS = {
    "password123456",
    "123456789012",
    "qwertyuiopas",
    "admin1234567",
    "letmein123456",
    "welcome12345",
    "monkey1234567",
    "password1234",
    "123456781234",
    "abc123456789",  # pragma: allowlist secret
    "password12345",
    "iloveyou12345",
}


def _retired_auth_route_response(replacement: str) -> JSONResponse:
    """Return the standard AUTH-SWAP-1 retired-route response.

    Must emit the canonical {ok: false, error: ...} envelope: the response
    contract middleware rewrites any body missing the 'ok' key to a 500
    `response_contract_violation`, which turned a clean 410 (route retired)
    into a confusing 500 + a hung client (desktop login on a stale bundle that
    still POSTs the retired /auth/login). failure_response() carries the
    replacement endpoint in error.details so callers can still discover it.
    """
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content=failure_response(
            "AUTH_ROUTE_RETIRED",
            "This endpoint has been retired. Use the GoTrue auth service.",
            details={"replacement": replacement},
        ),
    )


@asynccontextmanager
async def _db_transaction(db):
    """Bridge SQLite's sync transaction context and Postgres' async variant."""
    transaction = db.transaction()
    if hasattr(transaction, "__aenter__"):
        async with transaction:
            yield
        return

    with transaction:
        yield


def _record_funnel_step(step_name: str) -> None:
    try:
        from admin.instrumentation import record_funnel_step

        record_funnel_step(step_name)
    except Exception:
        logger.debug("Funnel metric skipped for %s", step_name)


# =============================================================================
# Nonce Infrastructure for System Browser OAuth
# =============================================================================
#
# In-memory dicts are the desktop fallback. When Redis is injected via
# set_nonce_redis(), Redis is the shared store for multi-instance SaaS
# OAuth callback state and the dicts act as a hot local cache only.

# nonce -> {created_at, status, user_data, oauth_state}
_login_nonces: dict[str, dict[str, Any]] = {}
# oauth_state -> nonce (preserves CSRF protection chain)
_state_to_nonce: dict[str, str] = {}

# Redis key prefixes for nonce storage.
_REDIS_NONCE_PREFIX = "vio:oauth:nonce:"
_REDIS_STATE_TO_NONCE_PREFIX = "vio:oauth:state:"
_NONCE_TTL_SECONDS = 300  # 5 minutes

# Lazy-init Redis handle (set once from app startup)
_nonce_redis = None


def set_nonce_redis(redis_backend: object) -> None:
    """Attach a Redis backend for cross-instance nonce sharing."""
    global _nonce_redis
    _nonce_redis = redis_backend
    logger.info("OAuth nonce store upgraded to Redis backend")


def _cloud_surface_enabled() -> bool:
    try:
        from config.settings import get_settings

        return str(getattr(get_settings(), "app_surface", "desktop")).lower() == "cloud"
    except Exception:
        return False


def _should_write_through_local_settings() -> bool:
    """Return False when this process shares ONE global SettingsManager across tenants.

    On the cloud surface a single SettingsManager instance serves every
    authenticated tenant in the same process, so any consent write-through there
    would land in the shared blob and leak one tenant's flag into the next
    tenant's unscoped read (#3320). Mirrors the identically-named gate in
    ui/api/routes/preferences.py.
    """
    from config.settings import get_settings

    settings = get_settings()
    surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    deployment = str(getattr(settings, "deployment_mode", surface) or surface).strip().lower()
    return surface != "cloud" and deployment != "cloud"


def _oauth_unavailable_detail() -> dict[str, str]:
    return {
        "error": "oauth_unavailable",
        "message": "Sign-in is temporarily unavailable. Please try again.",
    }


def _require_shared_nonce_store() -> None:
    """AUTH-01: fail-closed when cloud surface has no Redis-backed nonce store.

    The in-memory dicts are only safe in the single-process desktop case.
    When ``app_surface=cloud`` we must have a shared Redis backend wired via
    ``set_nonce_redis`` because different pod instances will otherwise race
    on state validation (state created on pod A, callback on pod B => 400).
    Silent in-memory fallback would also mean an attacker could swap nonces
    freely across pods.
    """
    if _cloud_surface_enabled() and _nonce_redis is None:
        logger.error(
            "OAuth nonce store requires a shared Redis backend in cloud mode "
            "(app_surface=cloud). Configure VIOLA_REDIS_URL and call "
            "auth.routes.set_nonce_redis() at startup."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_oauth_unavailable_detail(),
        )


def _redis_nonce_key(nonce: str) -> str:
    return f"{_REDIS_NONCE_PREFIX}{nonce}"


def _redis_state_key(state_value: str) -> str:
    return f"{_REDIS_STATE_TO_NONCE_PREFIX}{state_value}"


def _nonce_age_seconds(nonce_data: dict[str, Any]) -> float:
    try:
        created_at = float(nonce_data.get("created_at", 0))
    except (TypeError, ValueError):
        return _NONCE_TTL_SECONDS + 1
    return datetime.now(UTC).timestamp() - created_at


def _nonce_expired(nonce_data: dict[str, Any]) -> bool:
    return _nonce_age_seconds(nonce_data) > _NONCE_TTL_SECONDS


def _nonce_ttl_remaining(nonce_data: dict[str, Any]) -> int:
    remaining = _NONCE_TTL_SECONDS - _nonce_age_seconds(nonce_data)
    return max(1, int(remaining))


def _handle_nonce_redis_failure(exc: Exception) -> None:
    if _cloud_surface_enabled():
        logger.error("OAuth nonce Redis operation failed in cloud mode: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_oauth_unavailable_detail(),
        ) from exc
    logger.warning("OAuth nonce Redis operation failed; using in-memory fallback: %s", exc)


async def _redis_get_json(key: str) -> Any | None:
    if _nonce_redis is None:
        return None
    try:
        return await _nonce_redis.get_json(key)
    except Exception as exc:
        _handle_nonce_redis_failure(exc)
        return None


async def _redis_set_json(key: str, value: dict[str, Any], *, ttl: int) -> None:
    if _nonce_redis is None:
        return
    try:
        await _nonce_redis.set_json(key, value, ttl=ttl)
    except Exception as exc:
        _handle_nonce_redis_failure(exc)


async def _redis_get(key: str) -> str | None:
    if _nonce_redis is None:
        return None
    try:
        value = await _nonce_redis.get(key)
    except Exception as exc:
        _handle_nonce_redis_failure(exc)
        return None
    return value if isinstance(value, str) else None


async def _redis_set(key: str, value: str, *, ttl: int) -> None:
    if _nonce_redis is None:
        return
    try:
        await _nonce_redis.set(key, value, ttl=ttl)
    except Exception as exc:
        _handle_nonce_redis_failure(exc)


async def _redis_delete(*keys: str) -> None:
    if _nonce_redis is None:
        return
    try:
        for key in keys:
            if key:
                await _nonce_redis.delete(key)
    except Exception as exc:
        _handle_nonce_redis_failure(exc)


async def _store_login_nonce(nonce: str, nonce_data: dict[str, Any]) -> None:
    data = dict(nonce_data)
    _login_nonces[nonce] = data
    await _redis_set_json(_redis_nonce_key(nonce), data, ttl=_nonce_ttl_remaining(data))


async def _set_oauth_state_nonce(state_value: str, nonce: str, nonce_data: dict[str, Any]) -> None:
    _state_to_nonce[state_value] = nonce
    await _redis_set(
        _redis_state_key(state_value),
        nonce,
        ttl=_nonce_ttl_remaining(nonce_data),
    )


async def _delete_oauth_state_nonce(state_value: str | None) -> None:
    if not state_value:
        return
    _state_to_nonce.pop(state_value, None)
    await _redis_delete(_redis_state_key(state_value))


async def _delete_login_nonce(nonce: str, oauth_state: str | None = None) -> None:
    nonce_data = _login_nonces.pop(nonce, None)
    state_value = oauth_state
    if state_value is None and isinstance(nonce_data, dict):
        stored_state = nonce_data.get("oauth_state")
        state_value = stored_state if isinstance(stored_state, str) else None
    if state_value:
        _state_to_nonce.pop(state_value, None)

    keys = [_redis_nonce_key(nonce)]
    if state_value:
        keys.append(_redis_state_key(state_value))
    await _redis_delete(*keys)


async def _get_login_nonce(nonce: str) -> dict[str, Any] | None:
    nonce_data = _login_nonces.get(nonce)
    if nonce_data is not None:
        if _nonce_expired(nonce_data):
            await _delete_login_nonce(nonce, nonce_data.get("oauth_state"))
            return None
        return nonce_data

    redis_data = await _redis_get_json(_redis_nonce_key(nonce))
    if not isinstance(redis_data, dict):
        return None

    data = dict(redis_data)
    if _nonce_expired(data):
        await _delete_login_nonce(nonce, data.get("oauth_state"))
        return None

    _login_nonces[nonce] = data
    oauth_state = data.get("oauth_state")
    if isinstance(oauth_state, str):
        _state_to_nonce[oauth_state] = nonce
    return data


async def _get_nonce_for_oauth_state(state_value: str) -> str | None:
    nonce = _state_to_nonce.get(state_value)
    if nonce:
        nonce_data = await _get_login_nonce(nonce)
        if nonce_data is not None:
            return nonce
        _state_to_nonce.pop(state_value, None)

    redis_nonce = await _redis_get(_redis_state_key(state_value))
    if not redis_nonce:
        return None

    nonce_data = await _get_login_nonce(redis_nonce)
    if nonce_data is None:
        await _delete_oauth_state_nonce(state_value)
        return None

    _state_to_nonce[state_value] = redis_nonce
    return redis_nonce


def _purge_expired_nonces() -> None:
    """Remove expired in-memory nonce cache entries. Redis expires by TTL."""
    expired = [k for k, v in _login_nonces.items() if _nonce_expired(v)]
    for k in expired:
        state = _login_nonces[k].get("oauth_state")
        if state and state in _state_to_nonce:
            del _state_to_nonce[state]
        del _login_nonces[k]


def _oauth_local_base_url() -> str:
    """Desktop-local OAuth base URL using the HTTP-only callback port."""
    from config.settings import get_settings

    port = getattr(get_settings(), "oauth_callback_port", 8758)
    return f"http://localhost:{port}"


def _oauth_base_url() -> str:
    """OAuth base URL — cloud_url when deployed, localhost when desktop.

    Desktop: ALWAYS returns ``http://localhost:{oauth_callback_port}`` (default
    8758).  A dedicated HTTP-only listener on that port receives the callback
    because Google requires ``http://`` for localhost redirect URIs, while the
    main Viola server runs HTTPS.  Using localhost guarantees that state
    creation and callback validation happen in the **same process** — no
    cross-server state mismatch.

    Cloud (``VIOLA_CLOUD_BACKEND=1``): returns the public cloud_url so Google
    redirects to the same server that created the state.
    """

    from config.settings import get_settings

    s = get_settings()
    cloud_url = getattr(s, "cloud_url", None)
    if cloud_url:
        # On the cloud itself, always use the cloud URL.
        if os.environ.get("VIOLA_CLOUD_BACKEND") == "1":
            return cloud_url.rstrip("/")
    # Desktop: always use the local callback port.
    return _oauth_local_base_url()


def _check_oauth_port() -> None:
    """No-op: OAuth callbacks now go to a dedicated HTTP listener on
    ``oauth_callback_port`` (default 8758), so the main server port is
    irrelevant for OAuth.
    """


def _validate_return_url(return_url: str | None, request: Request) -> str | None:
    """Validate return_url is safe (same origin or relative path only).

    Delegates to the shared core.url_validation module which checks against
    the configured api_host, trusted domains, and cloud_url.
    """
    from core.url_validation import validate_return_url

    return validate_return_url(return_url)


# =============================================================================
# Registration Rate Limiting (per-IP, reuses brute_force.py pattern)
# =============================================================================


class _RegistrationRateLimiter:
    """Per-IP and per-email sliding-window rate limiter for registration.

    In single-process (desktop) mode, uses an in-memory dict. In cloud mode,
    a Redis-backed sliding-window counter is wired via ``set_redis``; when
    Redis is present, the in-memory dict is only a hot cache, and persisted
    state is authoritative across pods.
    """

    def __init__(
        self,
        *,
        max_registrations: int = 5,
        max_email_registrations: int | None = None,
        window_seconds: int = 3600,
    ) -> None:
        self._max = max_registrations
        self._email_max = max_email_registrations if max_email_registrations is not None else max_registrations
        self._window = window_seconds
        self._records: dict[str, list[float]] = {}  # key -> list of timestamps
        import threading

        self._lock = threading.Lock()
        self._redis = None

    def set_redis(self, redis_backend: object) -> None:
        """Attach a Redis backend for cross-instance sliding windows."""
        self._redis = redis_backend

    def _redis_enabled(self) -> bool:
        if self._redis is None:
            return False
        try:
            from services.cache.rate_limit import redis_rate_limit_enabled

            return redis_rate_limit_enabled()
        except Exception:
            return False

    # ---- Internal helpers ----

    def _limit_for_bucket(self, bucket: str) -> int:
        return self._email_max if bucket == "email" else self._max

    def _redis_zkey(self, bucket: str, key: str) -> str:
        from services.cache.rate_limit import rate_limit_key

        return rate_limit_key("registration.%s" % bucket, key, self._window)

    def _legacy_redis_zkey(self, bucket: str, key: str) -> str:
        return f"viola:reg_limit:{bucket}:{key}"

    @staticmethod
    def _cloud_non_dev() -> bool:
        try:
            from config.settings import get_settings

            s = get_settings()
            return (
                str(getattr(s, "app_surface", "desktop")).lower() == "cloud"
                and str(getattr(s, "env", "dev")).lower() != "dev"
            )
        except Exception:
            return False

    def _count_redis(self, bucket: str, key: str) -> int:
        """Best-effort Redis sliding-window count. Returns -1 on failure."""
        if not self._redis_enabled():
            return -1
        try:
            import time as _time

            zkey = self._redis_zkey(bucket, key)
            now = _time.time()
            cutoff = now - self._window
            # We model attempts as scored set entries (score = timestamp).
            client = getattr(self._redis, "_client", None) or getattr(self._redis, "client", None)
            if client is None:
                return -1
            # Drop expired entries
            client.zremrangebyscore(zkey, 0, cutoff)
            return int(client.zcard(zkey) or 0)
        except Exception as exc:
            logger.warning("Registration limiter Redis count failed: %s", exc)
            return -1

    async def _count_redis_async(self, bucket: str, key: str) -> int:
        """Redis sliding-window count for async request handlers."""
        if not self._redis_enabled():
            return -1
        try:
            import time as _time

            zkey = self._redis_zkey(bucket, key)
            now = _time.time()
            cutoff = now - self._window
            await self._redis.zremrangebyscore(zkey, 0, cutoff)
            return int(await self._redis.zcard(zkey))
        except Exception as exc:
            logger.warning("Registration limiter Redis count failed: %s", exc)
            return -2 if self._cloud_non_dev() else -1

    def _record_redis(self, bucket: str, key: str) -> None:
        if not self._redis_enabled():
            return
        try:
            import time as _time

            zkey = self._redis_zkey(bucket, key)
            now = _time.time()
            client = getattr(self._redis, "_client", None) or getattr(self._redis, "client", None)
            if client is None:
                return
            client.zadd(zkey, {str(now): now})
            client.expire(zkey, self._window + 60)
        except Exception as exc:
            logger.warning("Registration limiter Redis record failed: %s", exc)

    async def _record_redis_async(self, bucket: str, key: str) -> None:
        if not self._redis_enabled():
            return
        try:
            import time as _time
            import uuid

            zkey = self._redis_zkey(bucket, key)
            now = _time.time()
            await self._redis.zadd(zkey, now, "%s:%s" % (now, uuid.uuid4().hex))
            await self._redis.expire(zkey, self._window + 60)
        except Exception as exc:
            logger.warning("Registration limiter Redis record failed: %s", exc)

    def _check_key(self, bucket: str, key: str) -> tuple[bool, str | None, int | None]:
        """Check whether a bucket/key combo is under the sliding-window cap."""
        import time

        now = time.time()

        # Redis path (cloud) — authoritative when available
        redis_count = self._count_redis(bucket, key)
        limit = self._limit_for_bucket(bucket)
        if redis_count >= limit:
            # Retry-after uses the window as a conservative hint; the exact
            # oldest-timestamp is accessible but one-extra call per reject
            # isn't worth the latency.
            return (
                False,
                "Too many registration attempts. Wait a moment and try again.",
                self._window,
            )

        with self._lock:
            attempts = self._records.get(f"{bucket}:{key}")
            if attempts is None:
                return True, None, None

            cutoff = now - self._window
            while attempts and attempts[0] < cutoff:
                attempts.pop(0)

            if len(attempts) >= limit:
                retry = int(attempts[0] + self._window - now) + 1 if attempts else self._window
                return (
                    False,
                    "Too many registration attempts. Wait a moment and try again.",
                    retry,
                )

        return True, None, None

    async def _check_key_async(self, bucket: str, key: str) -> tuple[bool, str | None, int | None]:
        """Async check that uses Redis when wired, memory otherwise."""
        import time

        now = time.time()

        if self._redis is not None:
            from services.cache.rate_limit import (
                check_redis_sliding_window,
                redis_rate_limit_enabled,
            )

            if redis_rate_limit_enabled():
                decision = await check_redis_sliding_window(
                    self._redis,
                    scope="registration.%s" % bucket,
                    identifier=key,
                    limit=self._limit_for_bucket(bucket),
                    window_seconds=self._window,
                )
                if not decision.allowed:
                    return (
                        False,
                        (
                            "Registration is temporarily unavailable. Please try again shortly."
                            if decision.redis_error
                            else "Too many registration attempts. Wait a moment and try again."
                        ),
                        decision.retry_after or self._window,
                    )
                if not decision.redis_error:
                    return True, None, None

        redis_count = await self._count_redis_async(bucket, key)
        if redis_count == -2:
            return (
                False,
                "Registration is temporarily unavailable. Please try again shortly.",
                self._window,
            )
        limit = self._limit_for_bucket(bucket)
        if redis_count >= limit:
            return (
                False,
                "Too many registration attempts. Wait a moment and try again.",
                self._window,
            )
        if redis_count >= 0:
            return True, None, None

        with self._lock:
            attempts = self._records.get(f"{bucket}:{key}")
            if attempts is None:
                return True, None, None

            cutoff = now - self._window
            while attempts and attempts[0] < cutoff:
                attempts.pop(0)

            if len(attempts) >= limit:
                retry = int(attempts[0] + self._window - now) + 1 if attempts else self._window
                return (
                    False,
                    "Too many registration attempts. Wait a moment and try again.",
                    retry,
                )

        return True, None, None

    def check_ip(self, ip: str) -> tuple[bool, str | None, int | None]:
        """Check whether an IP is allowed to register."""
        return self._check_key("ip", ip)

    async def check_ip_async(self, ip: str) -> tuple[bool, str | None, int | None]:
        """Check whether an IP is allowed to register."""
        return await self._check_key_async("ip", ip)

    def check_email(self, email: str) -> tuple[bool, str | None, int | None]:
        """AUTH-14: per-email sliding-window check.

        Prevents a single email address from being targeted to burn N/day
        registration slots before the per-IP cap trips.
        """
        return self._check_key("email", (email or "").strip().lower())

    async def check_email_async(self, email: str) -> tuple[bool, str | None, int | None]:
        """AUTH-14: async per-email sliding-window check."""
        return await self._check_key_async("email", (email or "").strip().lower())

    async def count_recent_async(self, ip: str) -> int:
        """Async read-only count of recent attempts from an IP."""
        return await self._count_recent_key_async("ip", ip)

    async def count_recent_email_async(self, email: str) -> int:
        """Async read-only count of recent attempts for an email."""
        return await self._count_recent_key_async("email", (email or "").strip().lower())

    async def _count_recent_key_async(self, bucket: str, key: str) -> int:
        import time

        redis_count = await self._count_redis_async(bucket, key)
        if redis_count == -2:
            return self._limit_for_bucket(bucket)
        if redis_count >= 0:
            return redis_count

        now = time.time()
        with self._lock:
            attempts = self._records.get(f"{bucket}:{key}")
            if not attempts:
                return 0
            cutoff = now - self._window
            while attempts and attempts[0] < cutoff:
                attempts.pop(0)
            return len(attempts)

    def record(self, ip: str | None, email: str | None = None) -> None:
        """Record a registration attempt from this IP (and optionally email)."""
        import time

        now = time.time()
        # Redis records (best-effort)
        if ip:
            self._record_redis("ip", ip)
        if email:
            self._record_redis("email", email.strip().lower())

        with self._lock:
            for bucket, key in (
                ("ip", ip if ip else None),
                ("email", (email or "").strip().lower() if email else None),
            ):
                if key is None or key == "":
                    continue
                full_key = f"{bucket}:{key}"
                if full_key not in self._records:
                    self._records[full_key] = []
                attempts = self._records[full_key]
                cutoff = now - self._window
                while attempts and attempts[0] < cutoff:
                    attempts.pop(0)
                attempts.append(now)

    async def record_async(self, ip: str | None, email: str | None = None) -> None:
        """Record a registration attempt from async request handlers."""
        import time

        now = time.time()
        # Async Redis checks use an atomic check-and-record script, so this
        # method only maintains the memory fallback to avoid double-counting.

        with self._lock:
            for bucket, key in (
                ("ip", ip if ip else None),
                ("email", (email or "").strip().lower() if email else None),
            ):
                if key is None or key == "":
                    continue
                full_key = f"{bucket}:{key}"
                if full_key not in self._records:
                    self._records[full_key] = []
                attempts = self._records[full_key]
                cutoff = now - self._window
                while attempts and attempts[0] < cutoff:
                    attempts.pop(0)
                attempts.append(now)


_registration_limiter: _RegistrationRateLimiter | None = None


def _get_registration_limiter() -> _RegistrationRateLimiter:
    """Get or create the global registration rate limiter."""
    global _registration_limiter
    if _registration_limiter is None:
        _registration_limiter = _RegistrationRateLimiter(
            max_registrations=5,
            max_email_registrations=3,
            window_seconds=3600,
        )
    return _registration_limiter


def set_registration_limiter_redis(redis_backend: object) -> None:
    """AUTH-02: wire a Redis backend into the registration limiter.

    Cloud mode MUST call this at startup so per-pod in-memory state is not
    the authoritative view of the registration rate limit. Falls back to
    in-memory when called with ``None``.
    """
    _get_registration_limiter().set_redis(redis_backend)
    if redis_backend is not None:
        logger.info("Registration rate limiter upgraded to Redis backend")


def _reset_registration_limiter() -> None:
    """Reset the global registration limiter. Test-only hook (BILL-12)."""
    global _registration_limiter
    _registration_limiter = None


# Create router
auth_router = APIRouter(tags=["auth"])


# =============================================================================
# Request/Response Models
# =============================================================================


class RegisterRequest(BaseModel):
    """Registration request body."""

    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    coppa_age_confirmed: bool = False
    tos_accepted: bool = False


class RegisterResponse(BaseModel):
    """Registration response."""

    user: User
    message: str = "Check your email to verify your account. If you already have one, sign in instead."


class AuthUserPayload(BaseModel):
    """Canonical JSON-ready auth user payload."""

    id: str
    email: str
    email_verified: bool
    subscription_status: str
    plan_id: str
    plan_family: str
    has_paid_access: bool
    display_name: str
    created_at: datetime
    updated_at: datetime
    current_period_end: datetime | None = None
    canceled_at: datetime | None = None
    payment_provider: str | None = None


class RefreshSessionRequest(BaseModel):
    """Refresh-token rotation request body."""

    refresh_token: str | None = None


class RefreshSessionPayload(BaseModel):
    """Payload emitted after refresh-token rotation."""

    session: Session
    token: str
    refresh_token: str


class MagicLinkResponse(BaseModel):
    """Magic link request response."""

    message: str = "If an account exists with that email, a magic link has been sent."


class SessionListResponse(BaseModel):
    """Session list response."""

    sessions: list[Session]
    current_session_id: str | None = None


class AuthMePayload(BaseModel):
    """Canonical /auth/me payload."""

    user: AuthUserPayload
    oauth_providers: list[str] = []


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    message: str
    details: dict[str, Any] | None = None


class OAuthCompleteRequest(BaseModel):
    """OAuth completion request body (code + state from callback)."""

    code: str
    state: str


class EmailVerificationResponse(BaseModel):
    """Email verification response."""

    user: User
    message: str = "Email verified successfully. You can now log in."


# =============================================================================
# Password Reset Models
# =============================================================================


class PasswordResetRequestModel(BaseModel):
    """Password reset request."""

    email: EmailStr


class PasswordResetVerifyResponse(BaseModel):
    """Password reset token verification response."""

    valid: bool
    masked_email: str | None = None
    message: str = "Token is valid."


class PasswordResetCompleteModel(BaseModel):
    """Password reset completion request."""

    token: str
    new_password: str = Field(min_length=12, max_length=128)


class PasswordChangeModel(BaseModel):
    """Signed-in password change request."""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class DesktopPasswordChangeProxyModel(PasswordChangeModel):
    """Cloud-side password change request from a desktop Path A session."""

    email: EmailStr


class PasswordResetResponse(BaseModel):
    """Password reset response (always success to prevent enumeration)."""

    message: str = "If an account exists with that email, a password reset link has been sent."


def _presentation_user_id_or_500(user: Any, *, context: str) -> str:
    raw_user_id = getattr(user, "id", None)
    if isinstance(raw_user_id, str) and raw_user_id.strip():
        return raw_user_id
    if raw_user_id is not None:
        coerced_user_id = str(raw_user_id).strip()
        if coerced_user_id:
            return coerced_user_id

    try:
        raise ValueError("Authenticated user context is missing a user id")
    except ValueError as exc:
        logger.exception("%s missing authenticated user id", context)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=failure_response(
                "invalid_user_context",
                "Authenticated user context is missing a user id.",
            ),
        ) from exc


async def _build_user_payload(db, user: User | Any) -> AuthUserPayload | dict[str, Any]:
    """Return a JSON-ready user payload with canonical entitlement truth."""
    if not isinstance(user, User):
        return {"id": _presentation_user_id_or_500(user, context="auth user payload")}

    subscription = await db.subscriptions.get_subscription(user.id)
    return AuthUserPayload.model_validate(build_user_payload_with_entitlement(user, subscription))


# =============================================================================
# MFA Models
# =============================================================================


class MFASetupResponse(BaseModel):
    """MFA TOTP setup response."""

    secret: str
    provisioning_uri: str
    message: str = "Scan the QR code with your authenticator app, then verify with a code."


class MFATOTPVerifyModel(BaseModel):
    """MFA TOTP verification request."""

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class MFAStatusResponse(BaseModel):
    """MFA status response."""

    mfa_enabled: bool
    totp_enabled: bool = False
    backup_codes_remaining: int = 0


class MFABackupCodesResponse(BaseModel):
    """MFA backup codes response."""

    codes: list[str]
    message: str = "Save these backup codes securely. Each can only be used once."


class MFADisableModel(BaseModel):
    """MFA disable request (requires current code for verification)."""

    code: str = Field(description="Current TOTP code or backup code")


class MFABackupCodeVerifyModel(BaseModel):
    """MFA backup code verification request."""

    code: str = Field(min_length=8, max_length=8)


class MFAChallengeResponse(BaseModel):
    """Returned by the login endpoint when the user has MFA enabled.

    The client must call POST /auth/mfa/totp/authenticate with this
    mfa_token plus the TOTP (or backup) code to receive a full session.
    """

    requires_mfa: bool = True
    mfa_token: str


class MFATOTPAuthenticateRequest(BaseModel):
    """Request body for POST /auth/mfa/totp/authenticate."""

    mfa_token: str
    code: str = Field(
        min_length=6,
        max_length=8,
        description="6-digit TOTP code or 8-character backup code",
    )


# =============================================================================
# Dependency Helpers
# =============================================================================

# Track initialization state to avoid re-initializing on every request
_db_initialized = False


async def _get_auth_db_dep():
    """Get auth database dependency.

    Database is initialized on first access and reused for subsequent requests.
    """
    global _db_initialized
    from auth.database import get_auth_db

    db = get_auth_db()
    if not _db_initialized:
        await db.initialize()
        _db_initialized = True
    return db


async def _get_session_service_dep():
    """
    Legacy test seam for pre-GoTrue session services.

    Runtime auth now uses request.state.session from AuthMiddleware. Routes
    must read sessions from the auth DB or GoTrue facade state instead of
    constructing a custom SessionService.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=failure_response(
            "custom_session_service_retired",
            "Custom auth sessions were retired. Use GoTrue facade request state.",
        ),
    )


async def _promote_pending_subscription_after_email_verification(user_id: str, email: str) -> None:
    """Promote a paid guest-checkout subscription after the email is verified."""
    try:
        from billing.service import get_billing_service

        billing = get_billing_service()
        promoted = await billing.promote_pending_to_active(user_id)
        if promoted:
            logger.info(
                "Email verification promoted pending subscription for user %s",
                _mask_email(email),
            )
    except Exception:
        # Do NOT fail the email verification because of a billing hiccup.
        # The webhook path will retry on the next provider event, and admins
        # can run the promotion manually if needed.
        logger.exception("Failed to promote pending subscription after email verification")


def _signup_email_delivery_required() -> bool:
    """Require provider-confirmed verification email handoff outside test runs."""
    try:
        from config.settings import get_settings

        cfg = get_settings()
        if getattr(cfg, "pytest_in_progress", False):
            return False
        return bool(getattr(cfg, "signup_email_delivery_required", True))
    except Exception:
        logger.exception("Could not read signup email delivery requirement; failing closed")
        return True


def _verification_email_backend_configured(email_verification: Any) -> bool:
    email_service = getattr(email_verification, "email_service", None)
    if email_service is None:
        return False
    configured = getattr(email_service, "is_configured", False)
    if callable(configured):
        configured = configured()
    return bool(configured)


def _verification_email_unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=failure_response(
            "verification_email_unavailable",
            "We could not send the verification email. Please try again shortly.",
        ),
    )


async def _send_verification_email_for_signup(
    email_verification: Any,
    email: str,
    *,
    context: str,
    send_email: bool = True,
) -> bool:
    try:
        delivery = await email_verification.create_verification_token_with_delivery(
            email=email,
            send_email=send_email,
        )
    except Exception:
        logger.exception(
            "Verification email delivery raised during %s for %s",
            context,
            _mask_email(email),
        )
        return False

    if not send_email:
        return True

    if delivery.sent:
        logger.info("Verification email delivery confirmed for: %s", _mask_email(email))
        return True

    logger.error(
        "Verification email delivery failed for %s during %s: attempted=%s error=%s",
        _mask_email(email),
        context,
        delivery.attempted,
        delivery.error,
    )
    return False


# =============================================================================
# Registration & Login Routes
# =============================================================================


@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid registration data"},
    },
)
async def register(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/signup."""
    return _retired_auth_route_response("POST /auth/v1/signup")


@auth_router.post(
    "/login",
    responses={
        401: {"model": ErrorResponse, "description": "Invalid credentials"},
    },
)
async def login(
    http_request: Request,
    response: Response,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/token?grant_type=password."""
    return _retired_auth_route_response("POST /auth/v1/token?grant_type=password")


@auth_router.post("/logout")
async def logout(
    http_request: Request,
    response: Response,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/logout (or just delete client-side session)."""
    return _retired_auth_route_response("POST /auth/v1/logout (or just delete client-side session)")


@auth_router.post("/refresh")
async def refresh_session_tokens(
    http_request: Request,
    body: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/token?grant_type=refresh_token."""
    return _retired_auth_route_response("POST /auth/v1/token?grant_type=refresh_token")


# =============================================================================
# Magic Link Routes
# =============================================================================


@auth_router.post(
    "/magic-link/request",
    response_model=MagicLinkResponse,
)
async def request_magic_link(
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/otp."""
    return _retired_auth_route_response("POST /auth/v1/otp")


@auth_router.get("/magic-link/verify")
async def verify_magic_link(
    http_request: Request,
    response: Response,
    token: str | None = Query(None, description="Magic link token"),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/verify."""
    return _retired_auth_route_response("POST /auth/v1/verify")


@auth_router.post("/magic-link/verify-code")
async def verify_magic_link_by_code(
    http_request: Request,
    response: Response,
    body: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/verify."""
    return _retired_auth_route_response("POST /auth/v1/verify")


# =============================================================================
# Email Verification Routes
# =============================================================================


def _wants_html_response(http_request: Request) -> bool:
    """True when the caller looks like a browser (Accept includes text/html).

    Default httpx/programmatic clients send ``Accept: */*``; only browsers
    explicitly list ``text/html``. Used to decide between HTML landing pages
    (for users clicking email links) and JSON (for API consumers + tests).
    """
    accept = (http_request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _verify_email_html_page(
    *,
    title: str,
    heading: str,
    body: str,
    cta_text: str | None,
    cta_href: str | None,
    status_code: int,
) -> HTMLResponse:
    cta_block = ""
    if cta_text and cta_href:
        cta_block = (
            f'<p style="margin:32px 0 0;"><a href="{cta_href}" '
            'style="background-color:#6366f1;color:#fff;padding:12px 24px;'
            "text-decoration:none;border-radius:6px;display:inline-block;"
            f'font-weight:500;">{cta_text}</a></p>'
        )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Viola</title>
<style>
  html,body{{margin:0;padding:0;background:#0b0b0c;color:#f5f5f5;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    min-height:100%;}}
  .wrap{{max-width:560px;margin:0 auto;padding:80px 24px;text-align:center;}}
  h1{{font-size:1.8rem;font-weight:600;margin:0 0 16px;letter-spacing:-0.02em;}}
  p{{font-size:1rem;line-height:1.55;color:#c8c8cc;margin:0 0 12px;}}
</style>
</head>
<body>
  <div class="wrap">
    <h1>{heading}</h1>
    <p>{body}</p>
    {cta_block}
  </div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=status_code)


@auth_router.get(
    "/verify-email",
    response_model=EmailVerificationResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": "Invalid or expired verification link",
        },
    },
)
async def verify_email(
    http_request: Request,
    token: str | None = Query(None, description="Email verification token"),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue handles email confirmation via confirmation_token URL."""
    return _retired_auth_route_response("GoTrue handles email confirmation via confirmation_token URL")


@auth_router.post(
    "/resend-verification",
    response_model=MagicLinkResponse,
)
async def resend_verification_email(
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/resend."""
    return _retired_auth_route_response("POST /auth/v1/resend")


# =============================================================================
# OAuth Routes
# =============================================================================


@auth_router.post("/oauth/start")
async def oauth_start(request: Request) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GET /auth/v1/authorize?provider={provider}."""
    return _retired_auth_route_response("GET /auth/v1/authorize?provider={provider}")


@auth_router.get("/oauth/poll")
async def oauth_poll(
    http_request: Request,
    _login_nonce: str | None = Query(None, alias="login_nonce", description="Login nonce"),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue handles OAuth callbacks itself."""
    return _retired_auth_route_response("GoTrue handles OAuth callbacks itself")


@auth_router.get("/oauth/google")
async def oauth_google_redirect(
    request: Request,
    return_url: str | None = Query(None, description="URL to redirect after auth"),
    _login_nonce: str | None = Query(None, alias="login_nonce", description="Nonce for system browser flow"),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GET /auth/v1/authorize?provider=google."""
    return _retired_auth_route_response("GET /auth/v1/authorize?provider=google")


@auth_router.get("/oauth/google/callback")
async def oauth_google_callback(
    request: Request,
    response: Response,
    state: str | None = Query(None),
    code: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue handles OAuth callbacks itself."""
    return _retired_auth_route_response("GoTrue handles OAuth callbacks itself")


@auth_router.post(
    "/internal/google/refreshToken",
    dependencies=[Depends(csrf_required)],
)
async def google_refresh_proxy(
    request: Request,
    _user: User = Depends(get_current_user),
):
    """Token refresh proxy for the Google Workspace MCP server.

    The MCP server calls this instead of Google's cloud function so we
    can use Viola's client_secret to refresh the token.
    Requires an authenticated user session.
    """
    from services.oauth.google import is_google_restricted_features_enabled

    if not is_google_restricted_features_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=failure_response(
                "google_workspace_restricted_disabled",
                "Google Workspace is not available in this build.",
            ),
        )

    body = await request.json()
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail=failure_response(
                "refresh_token_required",
                "Refresh token is required.",
            ),
        )

    try:
        from services.oauth.workspace_bridge import handle_refresh_token

        result = await handle_refresh_token(refresh_token)

        # Successful refresh — reset dead-token counter
        from services.oauth.dead_token_detector import get_dead_token_detector

        get_dead_token_detector().record_success(_user.id, "google")

        return JSONResponse(content=result)
    except Exception as exc:
        logger.exception("Google token refresh proxy failed")

        # Track consecutive refresh failures for dead token detection
        from services.oauth.dead_token_detector import get_dead_token_detector

        get_dead_token_detector().record_failure(_user.id, "google", exc)

        return JSONResponse(
            status_code=502,
            content={
                "error_code": "google_refresh_failed",
                "message": "Google token refresh failed",
            },
        )


@auth_router.get("/oauth/apple")
async def oauth_apple_redirect(
    request: Request,
    return_url: str | None = Query(None),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GET /auth/v1/authorize?provider=apple."""
    return _retired_auth_route_response("GET /auth/v1/authorize?provider=apple")


@auth_router.post("/oauth/apple/callback")
async def oauth_apple_callback(
    request: Request,
    response: Response,
    code: str | None = Form(None),
    state: str | None = Form(None),
    user: str | None = Form(None),
    error: str | None = Form(None),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue handles OAuth callbacks itself."""
    return _retired_auth_route_response("GoTrue handles OAuth callbacks itself")


# =============================================================================
# User & Session Management Routes
# =============================================================================


@auth_router.get("/me")
async def get_me(
    user: User = Depends(get_current_user),
    db=Depends(_get_auth_db_dep),
):
    """
    Get current authenticated user.
    """
    # SEC-03: in monetized (cloud) builds, reject ``_LocalUser`` stand-ins
    # with 401 instead of returning the legacy minimal payload.  The minimal
    # payload bypassed the pydantic ``User`` contract and silently exposed a
    # reduced-guarantee response to cloud clients.  Desktop builds preserve
    # the legacy behavior so local-only flows keep working.
    if not isinstance(user, User):
        try:
            from config.settings import settings as _settings

            _monetized = bool(getattr(_settings, "is_monetized_build", False))
        except Exception:
            _monetized = False
        if _monetized:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=failure_response(
                    "not_authenticated",
                    "Cloud authentication required.",
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )
        local_user_id = _presentation_user_id_or_500(user, context="auth me local payload")
        return JSONResponse(
            content=success_response(
                {
                    "user": {"id": local_user_id},
                    "oauth_providers": [],
                }
            ),
        )

    # GoTrue owns OAuth identities after AUTH-SWAP-1. The legacy
    # public.oauth_identities table is intentionally retired.
    providers: list[str] = []
    user_payload = await _build_user_payload(db, user)
    if isinstance(user_payload, dict):
        return JSONResponse(
            content=success_response(
                {
                    "user": user_payload,
                    "oauth_providers": providers,
                }
            ),
        )

    return JSONResponse(
        content=success_response(AuthMePayload(user=user_payload, oauth_providers=providers).model_dump(mode="json")),
    )


@auth_router.get("/sessions")
async def list_sessions(
    current_session: Session = Depends(get_current_session),
    db=Depends(_get_auth_db_dep),
):
    """
    List all active sessions for current user.

    Returns the standard ``{ok, data}`` envelope. Returning a bare
    ``SessionListResponse`` tripped the response-contract middleware
    ("Missing 'ok' key") and 500'd the route — see /auth/me for the pattern.
    """
    return JSONResponse(
        content=success_response(
            SessionListResponse(
                sessions=[current_session],
                current_session_id=current_session.id,
            ).model_dump(mode="json")
        ),
    )


@auth_router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str,
    http_request: Request,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/logout."""
    return _retired_auth_route_response("POST /auth/v1/logout")


# =============================================================================
# Health & Info Routes
# =============================================================================


@auth_router.get("/providers")
async def get_auth_providers():
    """
    Get available authentication providers.
    """
    from services.oauth.apple import is_apple_configured
    from services.oauth.google import is_google_configured

    return JSONResponse(
        content=success_response(
            {
                "email_password": True,
                "magic_link": True,
                "google": is_google_configured(),
                "apple": is_apple_configured(),
            }
        ),
    )


# =============================================================================
# OAuth Complete & Preflight Routes
# =============================================================================


@auth_router.post("/oauth/{provider}/complete")
async def oauth_complete(
    provider: str,
    http_request: Request,
    response: Response,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue handles OAuth callbacks itself."""
    return _retired_auth_route_response("GoTrue handles OAuth callbacks itself")


@auth_router.get("/oauth/preflight")
async def oauth_preflight(provider: str = Query(default="google")):
    """
    Check if an OAuth provider is configured and ready.

    Used by clients before initiating OAuth flow.
    If no provider is specified, defaults to "google".
    """
    supported_providers = {"google", "apple"}

    if provider not in supported_providers:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=failure_response(
                "invalid_provider",
                "OAuth provider is not supported.",
                details={"provider": provider},
            ),
        )

    if provider == "google":
        from services.oauth.google import is_google_configured

        configured = is_google_configured()
    elif provider == "apple":
        from services.oauth.apple import is_apple_configured

        configured = is_apple_configured()
    else:
        configured = False

    if not configured:
        return JSONResponse(
            content=success_response(
                {
                    "ready": False,
                    "provider": provider,
                    "configured": False,
                }
            ),
        )

    return JSONResponse(
        content=success_response(
            {
                "ready": True,
                "provider": provider,
                "configured": True,
            }
        )
    )


# =============================================================================
# Password Reset Routes
# =============================================================================


@auth_router.post(
    "/password-reset/request",
    response_model=PasswordResetResponse,
)
async def request_password_reset(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/recover."""
    return _retired_auth_route_response("POST /auth/v1/recover")


@auth_router.get("/reset-password", include_in_schema=False)
async def reset_password_landing(
    http_request: Request,
    token: str | None = Query(None, description="Password reset token"),
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue's recovery confirmation flow."""
    return _retired_auth_route_response("GoTrue's recovery confirmation flow")


@auth_router.get(
    "/password-reset/verify/{token}",
    response_model=PasswordResetVerifyResponse,
)
async def verify_password_reset_token(token: str) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue's recovery confirmation flow."""
    return _retired_auth_route_response("GoTrue's recovery confirmation flow")


@auth_router.post(
    "/password-reset/complete",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or expired token"},
    },
)
async def complete_password_reset(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GoTrue's recovery confirmation flow."""
    return _retired_auth_route_response("GoTrue's recovery confirmation flow")


@auth_router.post("/password/change")
async def change_password(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: PUT /auth/v1/user with password field."""
    return _retired_auth_route_response("PUT /auth/v1/user with password field")


@auth_router.post("/password/change/desktop-proxy")
async def desktop_proxy_change_password(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: PUT /auth/v1/user with password field."""
    return _retired_auth_route_response("PUT /auth/v1/user with password field")


# =============================================================================
# MFA Routes
# =============================================================================


@auth_router.post(
    "/mfa/setup",
    response_model=MFASetupResponse,
)
async def setup_mfa(http_request: Request) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/factors."""
    return _retired_auth_route_response("POST /auth/v1/factors")


@auth_router.post(
    "/mfa/verify-setup",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid verification code"},
    },
)
async def verify_mfa_setup(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/factors/{factor_id}/verify."""
    return _retired_auth_route_response("POST /auth/v1/factors/{factor_id}/verify")


@auth_router.get(
    "/mfa/status",
    response_model=MFAStatusResponse,
)
async def get_mfa_status() -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GET /auth/v1/factors."""
    return _retired_auth_route_response("GET /auth/v1/factors")


@auth_router.post(
    "/mfa/verify",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid TOTP code"},
    },
)
async def verify_mfa(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/factors/{factor_id}/verify."""
    return _retired_auth_route_response("POST /auth/v1/factors/{factor_id}/verify")


@auth_router.delete(
    "/mfa",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid verification code"},
    },
)
async def disable_mfa(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: DELETE /auth/v1/factors/{factor_id}."""
    return _retired_auth_route_response("DELETE /auth/v1/factors/{factor_id}")


@auth_router.post(
    "/mfa/backup-codes",
    response_model=MFABackupCodesResponse,
)
async def generate_backup_codes(http_request: Request) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: GET /auth/v1/factors."""
    return _retired_auth_route_response("GET /auth/v1/factors")


@auth_router.post(
    "/mfa/backup-codes/verify",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid backup code"},
    },
)
async def verify_backup_code(
    http_request: Request,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/factors/{factor_id}/verify."""
    return _retired_auth_route_response("POST /auth/v1/factors/{factor_id}/verify")


@auth_router.post(
    "/mfa/totp/authenticate",
    responses={
        401: {"model": ErrorResponse, "description": "Invalid MFA token or code"},
    },
)
async def mfa_totp_authenticate(
    http_request: Request,
    response: Response,
    request: Any = None,
) -> JSONResponse:
    """Retired under AUTH-SWAP-1. Replacement: POST /auth/v1/factors/{factor_id}/verify."""
    return _retired_auth_route_response("POST /auth/v1/factors/{factor_id}/verify")


# =============================================================================
# GDPR Routes
# =============================================================================


def _cloud_gdpr_service_or_503(db: Any):
    from auth.cloud_gdpr import CloudGDPRService, CloudGDPRUnavailableError

    try:
        return CloudGDPRService(db)
    except CloudGDPRUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=failure_response(
                "cloud_gdpr_unavailable",
                "Account export and deletion require the cloud PostgreSQL backend.",
            ),
        ) from exc


@auth_router.get("/gdpr/export")
@auth_router.post("/gdpr/export", dependencies=[Depends(csrf_required)])
async def gdpr_export(
    http_request: Request,
    _body: dict[str, Any] | None = None,
    user: User = Depends(get_current_user),
    raw_token: str = Depends(require_auth_token),
    db=Depends(_get_auth_db_dep),
):
    """Compatibility alias for GoTrue-native account export."""
    password = None
    if isinstance(_body, dict):
        password = _body.get("current_password") or _body.get("password")
    data = await _cloud_gdpr_service_or_503(db).export_user_data(
        user,
        http_request,
        password=password if isinstance(password, str) else None,
        raw_token=raw_token,
        require_step_up=True,
    )
    return JSONResponse(content=success_response(data))


@auth_router.get("/gdpr/deletion-preview")
async def gdpr_deletion_preview(
    http_request: Request,
    user: User = Depends(get_current_user),
    db=Depends(_get_auth_db_dep),
):
    """Compatibility preview for GoTrue-native account deletion."""
    preview = await _cloud_gdpr_service_or_503(db).preview_account_deletion(user, http_request)
    return JSONResponse(content=success_response(preview))


def _build_deletion_receipt(
    *,
    deletion_id: str,
    user_id: str,
    deleted_at: datetime,
    categories_erased: list[str],
) -> dict[str, Any]:
    """Build a tamper-evident deletion receipt (WEB-R4).

    ``receipt_token`` is an HMAC-SHA256 of ``deletion_id|user_id|timestamp``
    signed with the server's jwt_secret.  Users can later present the
    receipt to support; operators can recompute the HMAC to confirm it
    was issued by this deployment.

    The user id in the receipt is scrubbed via SHA-256 so the receipt
    can be handed back to the user (or a support tool) without re-
    exposing the raw user id after deletion.
    """
    import hashlib
    import hmac

    from config.settings import settings as _settings

    jwt_secret = _settings.jwt_secret or ""
    timestamp = deleted_at.isoformat()
    scrubbed_user_id = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    scheduled_purge_at = (deleted_at + timedelta(hours=72)).isoformat()

    token_material = f"{deletion_id}|{user_id}|{timestamp}".encode()
    receipt_token = hmac.new(
        jwt_secret.encode("utf-8") if jwt_secret else b"viola-dev-delete-receipt",
        token_material,
        hashlib.sha256,
    ).hexdigest()

    return {
        "deletion_id": deletion_id,
        "user_id_scrubbed": scrubbed_user_id,
        "scheduled_purge_at": scheduled_purge_at,
        "categories_erased": categories_erased,
        "receipt_token": receipt_token,
    }


@auth_router.post("/gdpr/delete", dependencies=[Depends(csrf_required)])
async def gdpr_delete(
    http_request: Request,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    raw_token: str = Depends(require_auth_token),
    db=Depends(_get_auth_db_dep),
):
    """Compatibility alias for GoTrue-native account deletion."""
    # Require explicit confirmation
    confirmation = body.get("confirmation")
    if confirmation != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response(
                "confirmation_required",
                "You must pass confirmation='DELETE' to delete your account",
            ),
        )

    result = await _cloud_gdpr_service_or_503(db).delete_account(
        user,
        http_request,
        password=body.get("current_password") or body.get("password"),
        raw_token=raw_token,
    )
    payload = {
        "message": "Account and all associated cloud data have been deleted.",
        **result,
    }
    json_response = JSONResponse(content=success_response(payload))

    # Clear session cookie on the actual response being returned
    clear_session_cookie(json_response)
    clear_refresh_cookie(json_response)
    clear_csrf_cookie(json_response)

    return json_response


# =============================================================================
# User Settings Routes
# =============================================================================


def _settings_default_plan_id(user: User) -> str:
    from core.product import PlanId

    return user.plan_id.value if getattr(user, "has_paid_access", False) else PlanId.FREE.value


async def get_user_settings(
    http_request: Request,
    user: User = Depends(get_current_user),
    db=Depends(_get_auth_db_dep),
):
    """Get user settings (creates defaults if none exist)."""
    import json as _json

    from auth.settings_schema import (
        migrate_settings,
        normalize_settings,
        settings_changed,
    )

    if not isinstance(user, User):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=failure_response(
                "not_authenticated",
                "Cloud user authentication required",
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.user_settings.get_settings(user.id)
    default_plan_id = _settings_default_plan_id(user)

    if result is None:
        # Create default settings
        settings = normalize_settings({}, plan_id=default_plan_id)
        settings_str = _json.dumps(settings)
        version = await db.user_settings.save_settings(user.id, settings_str, 1)
        return JSONResponse(content=success_response({"settings": settings, "version": version}))

    settings, version = result

    # Check if migration is needed
    migrated = migrate_settings(settings, plan_id=default_plan_id)
    if settings_changed(settings, migrated):
        # Persist migrated settings
        settings_str = _json.dumps(migrated)
        version = await db.user_settings.save_settings(user.id, settings_str, version)
        settings = migrated

    return JSONResponse(content=success_response({"settings": settings, "version": version}))


async def patch_user_settings(
    http_request: Request,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    db=Depends(_get_auth_db_dep),
):
    """Update user settings with optimistic locking."""
    import json as _json

    from auth.settings_schema import normalize_settings, validate_settings_patch

    if not isinstance(user, User):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=failure_response(
                "not_authenticated",
                "Cloud user authentication required",
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

    patch_settings = body.get("settings", {})
    client_version = body.get("version")

    if not isinstance(patch_settings, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("invalid_request", "'settings' must be an object"),
        )

    # Validate patch payload
    errors = validate_settings_patch(patch_settings)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response(
                "invalid_settings",
                "Settings validation failed",
                details=errors,
            ),
        )

    # Get current settings
    result = await db.user_settings.get_settings(user.id)
    default_plan_id = _settings_default_plan_id(user)
    if result is None:
        current_settings = normalize_settings({}, plan_id=default_plan_id)
        current_version = 1
    else:
        current_settings, current_version = result

    # Optimistic locking: check version
    if client_version is not None and client_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=failure_response(
                "version_conflict",
                "Settings have been modified by another device",
                details={"current_version": current_version},
            ),
        )

    # Shallow merge
    merged = {**current_settings, **patch_settings}
    merged = normalize_settings(merged, plan_id=default_plan_id)
    new_version = current_version + 1

    settings_str = _json.dumps(merged)
    await db.user_settings.save_settings(user.id, settings_str, new_version)

    # Write-through: propagate consent flags to local SettingsManager so that
    # privacy_consent gate functions (which read from SettingsManager) see the
    # change immediately.  This is NOT a sync layer — it's a single write at
    # the API boundary that keeps the local consent store authoritative.
    #
    # Gated off on cloud (#3320): the cloud process shares ONE global
    # SettingsManager across every authenticated tenant, so any write-through
    # there leaks one tenant's consent flag into the shared blob for the next
    # tenant's unscoped read. On desktop, set() alone routes to the correct
    # per-user store; the former direct `_sm.settings[k] = v` dict write bypassed
    # that scoping unconditionally (it only tripped _GuardedSettingsDict's SYSTEM-
    # key guard, never the user-scoped consent_* keys) and is removed. Mirrors
    # ui/api/routes/preferences.py's gated twin.
    _consent_keys = {k: v for k, v in patch_settings.items() if k.startswith("consent_")}
    if _consent_keys and _should_write_through_local_settings():
        try:
            from ui.settings_manager import get_settings_manager

            _sm = get_settings_manager()
            for _ck, _cv in _consent_keys.items():
                _sm.set(_ck, _cv, save_immediately=False)
            _sm.save()
        except Exception as exc:
            logger.warning("Failed to write-through consent to SettingsManager: %s", exc)

    # Broadcast to EventHub if available
    event_hub = getattr(http_request.app.state, "event_hub", None)
    if event_hub is not None:
        try:
            await event_hub.broadcast_to_user(
                user_id=user.id,
                event_type="settings_updated",
                payload={"settings": merged, "version": new_version},
            )
        except Exception as exc:
            logger.warning("Failed to broadcast settings update: %s", exc)

    return JSONResponse(content=success_response({"settings": merged, "version": new_version}))


# =============================================================================
# Telegram Account Linking
# =============================================================================


@auth_router.get("/telegram/status")
async def telegram_link_status(
    user: User = Depends(get_current_user),
):
    """Check if the authenticated user has a linked Telegram account."""
    from auth.database import get_auth_db

    db = get_auth_db()
    pool = getattr(db, "_pool", None)
    if pool is None:
        return JSONResponse(
            content=success_response({"linked": False, "message": "Telegram linking requires setup first"}),
        )

    from auth.telegram_linking import TelegramLinkingService

    service = TelegramLinkingService(pool)
    link = await service.get_link_by_user_id(user.id)

    if link is None:
        return JSONResponse(content=success_response({"linked": False}))

    return JSONResponse(
        content=success_response(
            {
                "linked": True,
                "telegram_username": link.telegram_username,
                "telegram_first_name": link.telegram_first_name,
                "linked_at": link.linked_at.isoformat() if link.linked_at else None,
            }
        )
    )


@auth_router.post("/telegram/link-token", dependencies=[Depends(csrf_required)])
async def create_telegram_link_token(
    user: User = Depends(get_current_user),
):
    """Generate a one-time link token for connecting Telegram.

    Returns a deep link URL (t.me/BOT?start=TOKEN) that the user
    clicks to initiate the linking flow in Telegram.
    """

    from auth.database import get_auth_db

    db = get_auth_db()
    pool = getattr(db, "_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "unavailable",
                "message": "Telegram linking requires setup first",
            },
        )

    from auth.telegram_linking import TelegramLinkingService

    service = TelegramLinkingService(pool)

    try:
        token = await service.create_link_token(user.id)
    except Exception:
        logger.exception("Failed to create Telegram link token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "token_generation_failed",
                "message": "Failed to generate link. Please try again.",
            },
        )

    # Build the deep link URL
    bot_username = os.environ.get("VIOLA_TELEGRAM_BOT_USERNAME", "")
    if not bot_username:
        # Try to get it from the webhook module
        try:
            from backend.telegram_webhook import _BOT_USERNAME

            bot_username = _BOT_USERNAME
        except (ImportError, AttributeError) as exc:
            logger.debug("Telegram bot username import fallback skipped: %s", exc)

    if not bot_username:
        bot_username = "violavoice_bot"  # production bot

    deep_link = f"https://t.me/{bot_username}?start={token}"

    return JSONResponse(
        content=success_response(
            {
                "token": token,
                "deep_link": deep_link,
                "bot_username": bot_username,
                "expires_in_minutes": 15,
            }
        )
    )


@auth_router.post("/telegram/unlink", dependencies=[Depends(csrf_required)])
async def unlink_telegram(
    user: User = Depends(get_current_user),
):
    """Unlink the user's Telegram account."""
    from auth.database import get_auth_db

    db = get_auth_db()
    pool = getattr(db, "_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "unavailable",
                "message": "Telegram linking requires setup first",
            },
        )

    from auth.telegram_linking import TelegramLinkingService

    service = TelegramLinkingService(pool)
    success = await service.unlink_by_user_id(user.id)

    if success:
        return JSONResponse(content=success_response({"unlinked": True, "message": "Telegram account disconnected"}))
    else:
        return JSONResponse(content=success_response({"unlinked": False, "message": "No Telegram account linked"}))


# ────────────────────────────────────────────────────────────────────────────
# Discord linking endpoints (mirrors Telegram pattern)
# ────────────────────────────────────────────────────────────────────────────


@auth_router.get("/discord/status")
async def discord_link_status(
    user: User = Depends(get_current_user),
):
    """Check if the authenticated user has a linked Discord account."""
    from auth.database import get_auth_db

    db = get_auth_db()
    pool = getattr(db, "_pool", None)
    if pool is None:
        return JSONResponse(
            content=success_response({"linked": False, "message": "Discord linking requires setup first"}),
        )

    from auth.discord_linking import DiscordLinkingService

    service = DiscordLinkingService(pool)
    link = await service.get_link_by_user_id(user.id)

    if link is None:
        return JSONResponse(content=success_response({"linked": False}))

    return JSONResponse(
        content=success_response(
            {
                "linked": True,
                "discord_username": link.discord_username,
                "discord_display_name": link.discord_display_name,
                "linked_at": link.linked_at.isoformat() if link.linked_at else None,
            }
        )
    )


@auth_router.post("/discord/link-token", dependencies=[Depends(csrf_required)])
async def create_discord_link_token(
    user: User = Depends(get_current_user),
):
    """Reject Discord link-token creation until a hosted bot completes consent."""
    _raise_messaging_channel_unavailable("Discord")


@auth_router.post("/discord/unlink", dependencies=[Depends(csrf_required)])
async def unlink_discord(
    user: User = Depends(get_current_user),
):
    """Unlink the user's Discord account."""
    from auth.database import get_auth_db

    db = get_auth_db()
    pool = getattr(db, "_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "unavailable",
                "message": "Discord linking requires setup first",
            },
        )

    from auth.discord_linking import DiscordLinkingService

    service = DiscordLinkingService(pool)
    success = await service.unlink_by_user_id(user.id)

    if success:
        return JSONResponse(content=success_response({"unlinked": True, "message": "Discord account disconnected"}))
    else:
        return JSONResponse(content=success_response({"unlinked": False, "message": "No Discord account linked"}))


@auth_router.get("/matrix/status")
async def matrix_link_status(
    user: User = Depends(get_current_user),
):
    """Check if the authenticated user has a linked Matrix account."""
    from auth.database import get_auth_db

    db = get_auth_db()
    pool = getattr(db, "_pool", None)
    if pool is None:
        return JSONResponse(
            content=success_response({"linked": False, "message": "Matrix linking requires setup first"}),
        )

    from auth.matrix_linking import MatrixLinkingService

    service = MatrixLinkingService(pool)
    link = await service.get_link_by_user_id(user.id)

    if link is None:
        return JSONResponse(content=success_response({"linked": False}))

    return JSONResponse(
        content=success_response(
            {
                "linked": True,
                "matrix_username": link.matrix_username,
                "matrix_display_name": link.matrix_display_name,
                "linked_at": link.linked_at.isoformat() if link.linked_at else None,
            }
        )
    )


@auth_router.post("/matrix/link-token", dependencies=[Depends(csrf_required)])
async def create_matrix_link_token(
    user: User = Depends(get_current_user),
):
    """Reject Matrix link-token creation until a hosted bot completes consent."""
    _raise_messaging_channel_unavailable("Matrix")


@auth_router.post("/matrix/unlink", dependencies=[Depends(csrf_required)])
async def unlink_matrix(
    user: User = Depends(get_current_user),
):
    """Unlink the user's Matrix account."""
    from auth.database import get_auth_db

    db = get_auth_db()
    pool = getattr(db, "_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "unavailable",
                "message": "Matrix linking requires setup first",
            },
        )

    from auth.matrix_linking import MatrixLinkingService

    service = MatrixLinkingService(pool)
    success = await service.unlink_by_user_id(user.id)

    if success:
        return JSONResponse(content=success_response({"unlinked": True, "message": "Matrix account disconnected"}))
    else:
        return JSONResponse(content=success_response({"unlinked": False, "message": "No Matrix account linked"}))


def _get_cloud_base_url() -> str:
    """Return the base URL for the Viola cloud auth backend.

    Overridable via ``VIOLA_CLOUD_BASE_URL`` for tests + self-hosted.
    Trailing slashes are stripped for consistent URL composition.
    """

    return os.environ.get("VIOLA_CLOUD_BASE_URL", "https://api.useviola.com").rstrip("/")


def _desktop_should_proxy_to_cloud() -> bool:
    """True when this hub should forward auth requests to the Viola cloud.

    Triggers when ``VIOLA_APP_SURFACE`` is not ``"cloud"`` AND the cloud
    base URL is non-self. The cloud surface itself never proxies (would
    loop). Local-only test hubs (``VIOLA_CLOUD_BASE_URL`` pointing at
    127.* / localhost) keep their existing local-DB auth path.
    """

    surface = (os.environ.get("VIOLA_APP_SURFACE") or "desktop").strip().lower()
    if surface == "cloud":
        return False
    cloud = _get_cloud_base_url()
    if not cloud:
        return False
    if any(
        cloud.startswith(p)
        for p in (
            "http://localhost",
            "http://127.",
            "https://localhost",
            "https://127.",
        )
    ):
        return False
    return True
