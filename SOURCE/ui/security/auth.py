"""
Authentication Plugin

Provides API key and token-based authentication.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from threading import RLock
from types import SimpleNamespace

from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from config import env
from core.logging_config import get_logger
from fastapi import FastAPI, Request

from .config import SecurityConfig, get_security_config
from .core import SecurityPlugin

log = get_logger(__name__)

# Single-use nonce tracking for WS auth tokens.
# Keys are SHA-256 hashes of consumed tokens; values are the timestamp
# when the token was consumed.  Entries older than 60s are periodically
# purged (WS tokens expire in 30s, so 60s gives ample margin).
_consumed_ws_nonces: dict[str, float] = {}
_ws_identity_claims: dict[str, tuple[float, str, str]] = {}
_ws_token_lock = RLock()
_nonce_cleanup_time: float = 0.0
WS_AUTH_TOKEN_TTL_SECONDS = 30
STREAM_AUTH_TOKEN_TTL_SECONDS = 300


@dataclass(frozen=True)
class WebSocketAuthTokenClaims:
    """Verified short-lived WebSocket token claims."""

    user_id: str | None = None
    session_id: str | None = None


class AuthenticationPlugin(SecurityPlugin):
    """API key and token-based authentication plugin."""

    def __init__(self, config: SecurityConfig | None = None):
        super().__init__("authentication", enabled=False)
        self.config = config or get_security_config()
        self.enabled = self.config.auth_enabled

        # API key header
        self.api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

        # Token header (for WebSocket)
        self.token_header_name = "X-Auth-Token"  # nosec B105

        # Required endpoints (empty = all endpoints)
        self.required_endpoints: list[str] = self.config.auth_required_endpoints or []

        # Exempt paths (bypass auth even if they match a required prefix)
        self.exempt_paths: list[str] = self.config.auth_exempt_paths or []

        # Token expiration (default: 1 hour)
        self.token_expiration_seconds = int(env.get("VIOLA_SECURITY_TOKEN_EXPIRATION_SECONDS", "3600"))

        # Auth failure tracking — persisted to SQLite, LRU-cached, exponential backoff
        try:
            from pathlib import Path

            from config.settings import settings as _s

            _db_dir = Path(_s.data_dir) / "security"
            _db_path = _db_dir / "auth_failures.db"
        except Exception:
            _db_path = None
        from ui.security.auth_failure_store import AuthFailureStore

        self._failure_store = AuthFailureStore(db_path=_db_path, max_failures=5, base_window=300)

        # Debug auth token (separate from main auth)
        self.debug_auth_token: str | None = None
        if self.config.debug_mode:
            self.debug_auth_token = env.get("VIOLA_DEBUG_AUTH_TOKEN")

    def initialize(self, app: FastAPI) -> None:
        """Initialize authentication middleware."""
        if not self.enabled:
            log.info("🔐 Authentication disabled")
            return

        if not self.config.auth_api_key and not self.config.auth_token_secret:
            log.warning("🔐 Authentication enabled but no API key or token secret configured")
            self.enabled = False
            return

        # Add authentication dependency
        app.middleware("http")(self._auth_middleware)

        log.info("🔐 Authentication plugin initialized")
        log.info(
            "   - API Key: %s",
            "configured" if self.config.auth_api_key else "not configured",
        )
        log.info(
            "   - Token Secret: %s",
            "configured" if self.config.auth_token_secret else "not configured",
        )
        log.info("   - Required endpoints: %s", self.required_endpoints or "all")
        log.info("   - Exempt paths: %s", self.exempt_paths or "none")

    def cleanup(self) -> None:
        """Cleanup authentication plugin.

        No cleanup needed for authentication plugin (stateless).
        """
        pass  # No cleanup needed (stateless plugin)

    def _is_exempt_path(self, path: str) -> bool:
        """Check whether *path* is exempt from authentication.

        Each entry in ``self.exempt_paths`` is matched as:
        - **Exact match** when the entry does NOT end with ``/``
          (e.g. ``/v1/consent/callback`` matches only that exact path).
        - **Prefix match** when the entry ends with ``/``
          (e.g. ``/api/public/`` matches ``/api/public/waitlist``).
        """
        for exempt in self.exempt_paths:
            if exempt.endswith("/"):
                # Prefix match — covers the subtree
                if path.startswith(exempt):
                    return True
            else:
                # Exact match only
                if path == exempt:
                    return True
        return False

    def _stream_query_api_key_matches(self, request: Request) -> bool:
        """Allow EventSource auth for loopback desktop command streams.

        Browsers cannot attach custom headers to EventSource requests. Keep
        query-string API key support scoped to the local SSE stream endpoint,
        mirroring the WebSocket fallback without broadening general REST auth.
        """
        if not request.url.path.startswith("/api/stream/"):
            return False
        api_key = request.query_params.get("api_key")
        if not api_key or not self.config.auth_api_key:
            return False
        try:
            from ui.core.security import is_desktop_surface, is_loopback_request

            if not is_desktop_surface() or not is_loopback_request(request):
                return False
        except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
            return False
        return hmac.compare_digest(api_key, self.config.auth_api_key)

    async def _auth_middleware(self, request: Request, call_next):
        """Authentication middleware."""
        path = request.url.path
        direct_client_ip = request.client.host if request.client else "unknown"
        try:
            from auth.ip_utils import extract_client_ip

            client_ip = extract_client_ip(request) or direct_client_ip
        except Exception:
            client_ip = direct_client_ip

        # Health endpoint: configurable auth (default: no auth for basic health)
        if path == "/health":
            # Basic health check: no auth
            # Detailed health check: require auth if enabled
            health_detail = request.query_params.get("detail")
            if health_detail == "true" and self.enabled:
                # Detailed health check requires auth
                if not await self._check_auth(request):
                    return await self._rate_limit_auth_failure(client_ip, request)
                return await call_next(request)
            # Basic health check: always allowed
            return await call_next(request)

        # Debug endpoints: require separate debug auth token
        if path.startswith("/debug/"):
            if self.config.debug_mode:
                # Check for debug auth token
                debug_token = request.headers.get("X-Debug-Auth-Token")
                if debug_token and self.debug_auth_token:
                    if hmac.compare_digest(debug_token, self.debug_auth_token):
                        log.info("🔐 Debug endpoint access granted for %s", path)
                        return await call_next(request)
                # Debug mode but no valid token
                log.warning(
                    "🔐 Debug endpoint access denied for %s (missing/invalid debug token)",
                    path,
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Debug access requires X-Debug-Auth-Token header"},
                )
            else:
                # Debug mode disabled - deny all debug endpoints
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Debug endpoints disabled"},
                )

        # Check if path is explicitly exempt from auth (e.g. OAuth callbacks,
        # public website endpoints).  Exempt entries are matched as exact path
        # OR as a prefix when the entry ends with "/".
        if self._is_exempt_path(path):
            return await call_next(request)

        # Check if endpoint requires auth
        if self.required_endpoints:
            # Only check auth for required endpoints
            requires_auth = any(path.startswith(ep) for ep in self.required_endpoints)
            if not requires_auth:
                return await call_next(request)
        # else: all endpoints require auth

        # Check authentication
        if not await self._check_auth(request):
            return await self._rate_limit_auth_failure(client_ip, request)

        # Auth succeeded — clear any prior failure history for this IP.
        # This prevents the death spiral where initial 401s (before a spoke
        # browser fetches its bootstrap key) permanently lock out the IP.
        self._failure_store.clear_failures(client_ip)

        return await call_next(request)

    async def _check_auth(self, request: Request) -> bool:
        """Check if request is authenticated.

        Accepts any of:
          1. X-API-Key header matching the configured key
          2. X-Auth-Token header (legacy timestamp:nonce:signature scheme)
          3. Authorization: Bearer <session-token>
          4. viola_session cookie (Path-A login mints this)

        Sources 3 + 4 validate against GoTrue. Without (4)
        the cookie minted by /auth/login Path-A proxy can't authenticate
        downstream calls like /v1/command, even though the route-level
        require_auth dependency would accept it — the plugin middleware
        rejects first.
        """
        # Account session credentials are authoritative when present. A stale
        # or forged cookie must fail closed instead of falling through to the
        # local desktop API key/device identity.
        session_cookie = request.cookies.get("viola_session")
        if session_cookie:
            return await self._verify_session_token(session_cookie, request=request)

        auth_header = request.headers.get("Authorization") or ""
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:]
            return bool(bearer and await self._verify_session_token(bearer, request=request))

        # Get API key from header
        api_key = request.headers.get("X-API-Key")

        # Verify API key
        if api_key and self.config.auth_api_key:
            if hmac.compare_digest(api_key, self.config.auth_api_key):
                return True
        if self._stream_query_api_key_matches(request):
            return True

        # Try legacy timestamp:nonce:signature token
        token = request.headers.get(self.token_header_name)
        if token and self.config.auth_token_secret:
            if self._verify_token(token):
                return True

        # Accept a valid multiroom-spoke credential as full hub auth.
        #
        # A spoke is a browser pointed at THIS hub over the LAN, paired via the
        # 6-digit code shown on the hub screen. It is a window into the hub, so
        # it must be 1:1 with the desktop: the same SmartDisplay, loading the
        # same user-scoped data. The credential is an HMAC token (verified +
        # server-side-expiry-enforced in verify_spoke_credential) presented as
        # the X-Spoke-Token header or the HttpOnly viola_spoke cookie that
        # bootstrap/confirm set. It resolves to the owning desktop principal
        # (hub_user_id == device user), so one-user-per-install scoping holds.
        # Paid / account-destructive actions stay independently gated by the
        # account login requirement, so this does not widen the money/phone
        # blast radius. Fails closed: a missing/forged/expired token returns
        # None here and falls through to the 401 + auth-failure rate limiter.
        #
        # Runs on a worker thread: the verify chain reads the secret file and,
        # on first use in a process, hardens the secret dir with an icacls
        # subprocess — pre-fix that spawn ran on the asyncio loop on EVERY
        # spoke request, starving multiroom audio WS sends (py-spy conviction,
        # _diag/2026-07-01/spoke_ios_foreground_stall_and_video_sync.md).
        if await asyncio.to_thread(self._spoke_credential_valid, request):
            return True

        return False

    def _spoke_credential_valid(self, request: Request) -> bool:
        """True when the request carries a valid paired-spoke credential."""
        try:
            from ui.security.spoke_credentials import (
                SPOKE_TOKEN_COOKIE_NAME,
                SPOKE_TOKEN_HEADER_NAME,
                verify_spoke_credential,
            )
        except ImportError:  # pragma: no cover - import guard
            return False

        presented = (request.headers.get(SPOKE_TOKEN_HEADER_NAME) or "").strip()
        if not presented:
            presented = (request.cookies.get(SPOKE_TOKEN_COOKIE_NAME) or "").strip()
        if not presented:
            return False
        return verify_spoke_credential(presented) is not None

    async def _rate_limit_auth_failure(self, client_ip: str, request: Request):
        """Handle authentication failure with persistent rate limiting + exponential backoff."""
        path = request.url.path

        # Check if already rate-limited before recording new failure
        is_limited, retry_after = self._failure_store.check_rate_limited(client_ip)
        if is_limited:
            log.warning(
                "🔐 Authentication rate limit exceeded for %s on %s (retry in %ds)",
                client_ip,
                path,
                retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": f"Too many authentication failures. Please wait {retry_after} seconds."},
                headers={"Retry-After": str(retry_after)},
            )

        # Record the failure (persisted to SQLite, survives restarts)
        count, _ = self._failure_store.record_failure(client_ip)

        log.warning(
            "🔐 Authentication failed for %s from %s (%s failures)",
            path,
            client_ip,
            count,
        )
        response = JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": "ApiKey"},
        )
        if request.cookies.get("viola_session"):
            from auth.middleware import clear_refresh_cookie, clear_session_cookie

            clear_session_cookie(response)
            clear_refresh_cookie(response)
        return response

    def _verify_token(self, token: str) -> bool:
        """Verify authentication token with expiration checking."""
        if not self.config.auth_token_secret:
            return False

        try:
            # Parse token: format is "timestamp:nonce:signature"
            parts = token.split(":", 2)
            if len(parts) != 3:
                return False

            timestamp_str, nonce, signature = parts
            timestamp = int(timestamp_str)

            # Check expiration
            now = int(time.time())
            if now - timestamp > self.token_expiration_seconds:
                log.debug(
                    "Token expired: %ss old (max: %ss)",
                    now - timestamp,
                    self.token_expiration_seconds,
                )
                return False

            # Verify signature
            message = f"{timestamp}:{nonce}".encode()
            expected_signature = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()

            return hmac.compare_digest(signature, expected_signature)
        except (ValueError, AttributeError):
            return False

    def generate_token(self) -> str:
        """Generate authentication token (for WebSocket) with expiration."""
        if not self.config.auth_token_secret:
            return ""

        # Generate token with timestamp and nonce for uniqueness
        timestamp = int(time.time())
        nonce = secrets.token_hex(16)  # Random nonce

        # Create signature
        message = f"{timestamp}:{nonce}".encode()
        signature = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()

        # Token format: "timestamp:nonce:signature"
        token = f"{timestamp}:{nonce}:{signature}"
        return token

    async def verify_request(self, request: Request) -> bool:
        """Verify request authentication (for use in endpoints).

        Supports:
        - X-API-Key header (API key auth)
        - Authorization: Bearer <token> header (session auth)
        - Session cookie (browser auth)
        """
        if not self.enabled:
            return True

        # Account session credentials are authoritative when present.
        session_cookie = request.cookies.get("viola_session")
        if session_cookie:
            return await self._verify_session_token(session_cookie, request=request)

        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]  # Remove "Bearer " prefix
            return await self._verify_session_token(token, request=request)

        # Check API key after account credentials so stale cookies do not
        # downgrade to the desktop device principal.
        api_key = request.headers.get("X-API-Key")
        if api_key and self.config.auth_api_key:
            if hmac.compare_digest(api_key, self.config.auth_api_key):
                return True
        if self._stream_query_api_key_matches(request):
            return True

        # A paired multiroom spoke is a full hub window — accept its signed
        # credential here too, so route-level Depends(require_auth) gates (e.g.
        # /v1/state) authenticate the same as the request middleware. Without
        # this the middleware would pass a spoke but the route dependency would
        # still 401. Same fail-closed verification as _check_auth, and same
        # worker-thread hop (file I/O + first-use icacls stay off the loop).
        if await asyncio.to_thread(self._spoke_credential_valid, request):
            return True

        return False

    async def _verify_session_token(
        self,
        token: str,
        *,
        request: Request | None = None,
        websocket=None,
    ) -> bool:
        """Verify a session token through the active auth authority."""
        if self._desktop_local_sessions_enabled():
            return await self._verify_desktop_local_session_token(
                token,
                request=request,
                websocket=websocket,
            )
        return await self._verify_gotrue_session_token(
            token,
            request=request,
            websocket=websocket,
        )

    @staticmethod
    def _desktop_local_sessions_enabled() -> bool:
        try:
            from config.settings import settings

            return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() != "cloud"
        except (AttributeError, ImportError, TypeError, ValueError):
            return False

    async def _verify_desktop_local_session_token(
        self,
        token: str,
        *,
        request: Request | None = None,
        websocket=None,
    ) -> bool:
        """Verify a desktop-local opaque session and attach cloud user state."""
        try:
            from auth.desktop_session import validate_desktop_session_token

            validation = await validate_desktop_session_token(token)
            if not validation.authenticated or validation.user is None or validation.session is None:
                log.warning("Desktop local UI auth rejected token: %s", validation.reason)
                return False
        except (
            AttributeError,
            ImportError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            log.warning("Desktop local UI auth failed closed: %s", exc)
            return False

        self._attach_desktop_local_authenticated_session(
            request=request,
            websocket=websocket,
            user=validation.user,
            session=validation.session,
            access_token=validation.access_token,
        )
        return True

    def _attach_desktop_local_authenticated_session(
        self,
        *,
        request: Request | None,
        websocket,
        user,
        session,
        access_token: str | None,
    ) -> None:
        state = self._get_auth_state(request or websocket)
        if state is not None:
            state.session = session
            state.user = user
            state.user_id = user.id
            state.db_user_id = user.id
            state.gotrue_access_token = access_token
            state.auth_error = None
            if access_token and getattr(state, "current_cloud_access_token_context_token", None) is None:
                from core.user_context import set_current_cloud_access_token

                state.current_cloud_access_token_context_token = set_current_cloud_access_token(access_token)

            from auth.principals import UserPrincipal
            from core.types import UserContext

            state.auth_principal = UserPrincipal(
                user_id=user.id,
                session_id=session.id,
                role="authenticated",
            )
            state.user_context = UserContext(
                user_id=user.id,
                session_id=session.id,
            )
            state.request_context = self._build_desktop_cloud_request_context(user)

        from core.user_context import set_current_user_id

        # #1822: capture the reset Token the SAME way current_cloud_access_token
        # two blocks above does, instead of discarding it as a bare statement.
        # A caller with no ASGI middleware teardown of its own (a WebSocket
        # route -- AuthMiddleware.__call__ explicitly no-ops for non-http scope)
        # owns resetting this via ws.state.current_user_context_token in its own
        # disconnect handler; an HTTP request's AuthMiddleware teardown
        # (_reset_request_identity_context) already reads request.state under
        # this exact attribute name, so writing it here is enough to be picked
        # up automatically for every HTTP caller too. Guarded like the cloud
        # token above so a second verifier in the same call chain (a
        # defense-in-depth auth dependency re-verifying an already-authenticated
        # request) never clobbers the first captured Token.
        if state is not None and getattr(state, "current_user_context_token", None) is None:
            state.current_user_context_token = set_current_user_id(user.id)
        else:
            # #1907 (user-context-token-discard gate): this branch is only
            # reachable when a Token was ALREADY captured above for the SAME
            # user.id (a defense-in-depth second verifier re-authenticating
            # an already-authenticated request) -- _get_auth_state only
            # returns None for a non-Starlette carrier missing .state, which
            # every real request/websocket here always has. Re-binding the
            # identical value doesn't need its own Token: the first capture's
            # eventual reset already unwinds this re-set too.
            set_current_user_id(user.id)  # ctx-token-ok: idempotent re-set, first capture above owns the reset

    async def _verify_gotrue_session_token(
        self,
        token: str,
        *,
        request: Request | None = None,
        websocket=None,
    ) -> bool:
        """Verify a GoTrue JWT and attach facade-compatible auth state."""
        try:
            from auth import gotrue_facade

            claims = gotrue_facade.decode_gotrue_jwt(token)
            user_id = claims["sub"]
            if not isinstance(user_id, str) or not user_id.strip():
                raise ValueError("GoTrue JWT subject must be a non-empty string")

            session = gotrue_facade.gotrue_claims_to_session(claims)
            if not await self._gotrue_session_is_alive(request or websocket, user_id=user_id, session_id=session.id):
                raise ValueError("GoTrue session has been revoked")

            app_profile_row = await asyncio.to_thread(
                self._get_or_create_gotrue_app_profile,
                request or websocket,
                user_id,
            )
            user = gotrue_facade.gotrue_claims_to_user(claims, app_profile_row)
        except Exception as e:
            log.warning("GoTrue UI auth rejected token: %s", e)
            return False

        self._attach_gotrue_authenticated_session(
            request=request,
            websocket=websocket,
            user=user,
            session=session,
            access_token=token,
        )
        return True

    def _get_or_create_gotrue_app_profile(self, carrier, user_id: str) -> dict:
        from auth.gotrue_facade import get_or_create_app_profile
        from auth.middleware import (
            _get_gotrue_profile_engine,
            _set_sqlalchemy_app_user_id,
        )

        app_state = getattr(getattr(carrier, "app", None), "state", None) if carrier is not None else None
        connection = getattr(app_state, "gotrue_profile_connection", None) if app_state is not None else None
        if connection is not None:
            _set_sqlalchemy_app_user_id(connection, user_id)
            return get_or_create_app_profile(connection, user_id)

        connection_factory = (
            getattr(app_state, "gotrue_profile_connection_factory", None) if app_state is not None else None
        )
        if callable(connection_factory):
            conn_or_context = connection_factory()
            if hasattr(conn_or_context, "__enter__"):
                with conn_or_context as conn:
                    _set_sqlalchemy_app_user_id(conn, user_id)
                    return get_or_create_app_profile(conn, user_id)
            _set_sqlalchemy_app_user_id(conn_or_context, user_id)
            return get_or_create_app_profile(conn_or_context, user_id)

        engine = getattr(app_state, "gotrue_profile_engine", None) if app_state is not None else None
        if engine is None:
            engine = _get_gotrue_profile_engine()

        with engine.begin() as conn:
            _set_sqlalchemy_app_user_id(conn, user_id)
            return get_or_create_app_profile(conn, user_id)

    async def _gotrue_session_is_alive(self, carrier, *, user_id: str, session_id: str) -> bool:
        from auth import gotrue_facade
        from services.cache.redis_backend import get_redis

        app_state = getattr(getattr(carrier, "app", None), "state", None) if carrier is not None else None
        redis_backend = getattr(app_state, "gotrue_session_redis", None) if app_state is not None else None
        if redis_backend is None:
            redis_backend = await get_redis()

        cached = await gotrue_facade.cached_gotrue_session_is_valid(
            redis_backend,
            user_id=user_id,
            session_id=session_id,
        )
        if cached is not None:
            return cached

        alive = await asyncio.to_thread(
            self._gotrue_session_exists,
            carrier,
            user_id,
            session_id,
        )
        if alive:
            await gotrue_facade.cache_gotrue_session_validity(
                redis_backend,
                user_id=user_id,
                session_id=session_id,
            )
        return alive

    def _gotrue_session_exists(self, carrier, user_id: str, session_id: str) -> bool:
        from auth.gotrue_facade import gotrue_session_exists
        from auth.middleware import _get_gotrue_profile_engine

        app_state = getattr(getattr(carrier, "app", None), "state", None) if carrier is not None else None
        connection = getattr(app_state, "gotrue_profile_connection", None) if app_state is not None else None
        if connection is not None:
            return gotrue_session_exists(connection, user_id=user_id, session_id=session_id)

        connection_factory = (
            getattr(app_state, "gotrue_profile_connection_factory", None) if app_state is not None else None
        )
        if callable(connection_factory):
            conn_or_context = connection_factory()
            if hasattr(conn_or_context, "__enter__"):
                with conn_or_context as conn:
                    return gotrue_session_exists(conn, user_id=user_id, session_id=session_id)
            return gotrue_session_exists(conn_or_context, user_id=user_id, session_id=session_id)

        engine = getattr(app_state, "gotrue_profile_engine", None) if app_state is not None else None
        if engine is None:
            engine = _get_gotrue_profile_engine()

        with engine.begin() as conn:
            return gotrue_session_exists(conn, user_id=user_id, session_id=session_id)

    def _attach_gotrue_authenticated_session(
        self,
        *,
        request: Request | None,
        websocket,
        user,
        session,
        access_token: str | None,
    ) -> None:
        state = self._get_auth_state(request or websocket)
        if state is not None:
            state.session = session
            state.user = user
            state.gotrue_access_token = access_token
            state.auth_error = None
            if access_token and getattr(state, "current_cloud_access_token_context_token", None) is None:
                from core.user_context import set_current_cloud_access_token

                state.current_cloud_access_token_context_token = set_current_cloud_access_token(access_token)

            from core.types import UserContext

            state.user_context = UserContext(
                user_id=user.id,
                session_id=session.id,
            )
            state.request_context = self._build_request_context(user)

        from core.user_context import set_current_user_id

        # #1822: same capture-the-reset-Token fix as
        # _attach_desktop_local_authenticated_session above -- see that
        # method's comment for the full rationale.
        if state is not None and getattr(state, "current_user_context_token", None) is None:
            state.current_user_context_token = set_current_user_id(user.id)
        else:
            # #1907 (user-context-token-discard gate): same idempotent-re-set
            # rationale as _attach_desktop_local_authenticated_session above.
            set_current_user_id(user.id)  # ctx-token-ok: idempotent re-set, first capture above owns the reset

    @staticmethod
    def _get_auth_state(carrier):
        if carrier is None:
            return None
        state = getattr(carrier, "state", None)
        if state is not None:
            return state
        try:
            state = SimpleNamespace()
            carrier.state = state
            return state
        except Exception:
            return None

    @staticmethod
    def _build_request_context(user):
        from auth.models import User
        from config.settings import settings
        from core.product import AppSurface, PlanId, coerce_app_surface
        from core.request_context import RequestContext

        if not isinstance(user, User):
            return None
        surface = coerce_app_surface(getattr(settings, "app_surface", AppSurface.DESKTOP.value))
        trust_level = "local_owner" if surface is AppSurface.DESKTOP else "cloud_authenticated"
        plan_id = user.plan_id.value if getattr(user, "has_paid_access", False) else PlanId.FREE.value
        return RequestContext(
            user_id=user.id,
            plan_id=plan_id,
            surface=surface.value,
            trust_level=trust_level,
        )

    @staticmethod
    def _build_desktop_cloud_request_context(user):
        from auth.models import User
        from config.settings import settings
        from core.product import AppSurface, PlanId, coerce_app_surface
        from core.request_context import RequestContext

        if not isinstance(user, User):
            return None
        surface = coerce_app_surface(getattr(settings, "app_surface", AppSurface.DESKTOP.value))
        plan_id = user.plan_id.value if getattr(user, "has_paid_access", False) else PlanId.FREE.value
        return RequestContext(
            user_id=user.id,
            plan_id=plan_id,
            surface=surface.value,
            trust_level="cloud_authenticated",
        )

    @staticmethod
    def _b64url_encode(value: str) -> str:
        encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
        return encoded.rstrip("=")

    @staticmethod
    def _b64url_decode(value: str) -> str:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")

    def generate_ws_auth_token(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Generate a short-lived token for WebSocket authentication.

        Clients should call the ``POST /v1/ws/auth`` REST endpoint (which
        uses this method) to exchange their authenticated HTTP request for
        a single-use, short-lived token, then connect to the WebSocket with
        ``?token=<value>`` instead of passing the raw API key in query
        params.

        The token expires in 30 seconds -- enough to complete the WS
        handshake but short enough to limit the replay window.
        """
        if not self.config.auth_token_secret:
            return ""

        timestamp = int(time.time())
        nonce = secrets.token_hex(16)
        if user_id and session_id:
            claim_id = secrets.token_urlsafe(18)
            message = f"wsu:{timestamp}:{nonce}:{claim_id}".encode()
            signature = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()
            token = f"wsu:{timestamp}:{nonce}:{claim_id}:{signature}"
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with _ws_token_lock:
                _ws_identity_claims[token_hash] = (
                    timestamp + float(WS_AUTH_TOKEN_TTL_SECONDS),
                    user_id,
                    session_id,
                )
            return token

        # Prefix with "ws:" so we can distinguish legacy WS auth tokens
        # from general-purpose tokens and identity-bound WS tokens.
        message = f"ws:{timestamp}:{nonce}".encode()
        signature = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()
        return f"ws:{timestamp}:{nonce}:{signature}"

    def generate_stream_auth_token(
        self,
        *,
        stream_id: str,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Generate a reusable EventSource token bound to one stream id."""
        if not self.config.auth_token_secret or not stream_id:
            return ""

        timestamp = int(time.time())
        nonce = secrets.token_hex(16)
        encoded_stream = self._b64url_encode(stream_id)
        if user_id and session_id:
            encoded_user = self._b64url_encode(user_id)
            encoded_session = self._b64url_encode(session_id)
            message_text = f"sseu:{timestamp}:{nonce}:{encoded_stream}:{encoded_user}:{encoded_session}"
            message = message_text.encode()
            signature = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()
            return f"{message_text}:{signature}"

        message = f"sse:{timestamp}:{nonce}:{encoded_stream}".encode()
        signature = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()
        return f"sse:{timestamp}:{nonce}:{encoded_stream}:{signature}"

    def _consume_ws_token_once(self, token: str, now: float) -> bool:
        global _nonce_cleanup_time

        token_hash = hashlib.sha256(token.encode()).hexdigest()

        with _ws_token_lock:
            # Periodic cleanup: purge nonces older than 60s and expired
            # identity claims for tokens that were minted but never consumed.
            if now - _nonce_cleanup_time > 60:
                cutoff = now - 60
                stale = [key for key, timestamp in _consumed_ws_nonces.items() if timestamp < cutoff]
                for key in stale:
                    del _consumed_ws_nonces[key]
                stale_claims = [key for key, (expires_at, _, _) in _ws_identity_claims.items() if expires_at < now]
                for key in stale_claims:
                    del _ws_identity_claims[key]
                _nonce_cleanup_time = now

            if token_hash in _consumed_ws_nonces:
                log.warning("WS auth token replay rejected (already consumed)")
                return False

            _consumed_ws_nonces[token_hash] = now
            return True

    def consume_ws_auth_token(self, token: str) -> WebSocketAuthTokenClaims | None:
        """Verify and consume a short-lived WS auth token.

        Identity-bound tokens return the GoTrue user/session ids needed by
        browser WebSocket routes. Legacy API-key exchange tokens return empty
        claims and remain supported for desktop-local WebSockets.
        """
        if not self.config.auth_token_secret:
            return None

        try:
            parts = token.split(":")
            prefix = parts[0] if parts else ""
            if prefix not in {"ws", "wsu"}:
                return None
            if prefix == "ws" and len(parts) != 4:
                return None
            if prefix == "wsu" and len(parts) not in {5, 6}:
                return None

            timestamp = int(parts[1])
            now = time.time()
            if now - timestamp > WS_AUTH_TOKEN_TTL_SECONDS:
                log.debug(
                    "WS auth token expired: %ss old (max %ss)",
                    now - timestamp,
                    WS_AUTH_TOKEN_TTL_SECONDS,
                )
                return None

            if prefix == "ws":
                nonce, signature = parts[2], parts[3]
                message = f"ws:{timestamp}:{nonce}".encode()
                claims = WebSocketAuthTokenClaims()
            elif len(parts) == 6:
                nonce, encoded_user, encoded_session, signature = (
                    parts[2],
                    parts[3],
                    parts[4],
                    parts[5],
                )
                user_id = self._b64url_decode(encoded_user)
                session_id = self._b64url_decode(encoded_session)
                if not user_id.strip() or not session_id.strip():
                    return None
                message = f"wsu:{timestamp}:{nonce}:{encoded_user}:{encoded_session}".encode()
                claims = WebSocketAuthTokenClaims(user_id=user_id, session_id=session_id)
            else:
                nonce, claim_id, signature = parts[2], parts[3], parts[4]
                message = f"wsu:{timestamp}:{nonce}:{claim_id}".encode()
                claims = None

            expected = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None

            if not self._consume_ws_token_once(token, now):
                return None

            if prefix == "wsu" and claims is None:
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                with _ws_token_lock:
                    identity = _ws_identity_claims.pop(token_hash, None)
                if identity is None:
                    return None
                expires_at, user_id, session_id = identity
                if expires_at < now or not user_id.strip() or not session_id.strip():
                    return None
                claims = WebSocketAuthTokenClaims(user_id=user_id, session_id=session_id)

            return claims
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None

    def verify_stream_auth_token(
        self,
        token: str,
        *,
        stream_id: str,
    ) -> WebSocketAuthTokenClaims | None:
        """Verify a reusable, stream-id-bound EventSource auth token."""
        if not self.config.auth_token_secret or not stream_id:
            return None

        try:
            parts = token.split(":")
            prefix = parts[0] if parts else ""
            if prefix == "sse" and len(parts) != 5:
                return None
            if prefix == "sseu" and len(parts) != 7:
                return None
            if prefix not in {"sse", "sseu"}:
                return None

            timestamp = int(parts[1])
            now = time.time()
            if now - timestamp > STREAM_AUTH_TOKEN_TTL_SECONDS:
                log.debug(
                    "Stream auth token expired: %ss old (max %ss)",
                    now - timestamp,
                    STREAM_AUTH_TOKEN_TTL_SECONDS,
                )
                return None

            nonce = parts[2]
            encoded_stream = parts[3]
            token_stream_id = self._b64url_decode(encoded_stream)
            if not hmac.compare_digest(token_stream_id, stream_id):
                return None

            if prefix == "sse":
                signature = parts[4]
                message = f"sse:{timestamp}:{nonce}:{encoded_stream}".encode()
                claims = WebSocketAuthTokenClaims()
            else:
                encoded_user, encoded_session, signature = parts[4], parts[5], parts[6]
                user_id = self._b64url_decode(encoded_user)
                session_id = self._b64url_decode(encoded_session)
                if not user_id.strip() or not session_id.strip():
                    return None
                message_text = f"sseu:{timestamp}:{nonce}:{encoded_stream}:{encoded_user}:{encoded_session}"
                message = message_text.encode()
                claims = WebSocketAuthTokenClaims(user_id=user_id, session_id=session_id)

            expected = hmac.new(self.config.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            return claims
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None

    def _verify_ws_auth_token(self, token: str) -> bool:
        """Verify a short-lived WS auth token from ``generate_ws_auth_token``.

        Tokens are single-use: once verified, the token's hash is recorded
        in ``_consumed_ws_nonces`` and subsequent attempts are rejected.
        """
        return self.consume_ws_auth_token(token) is not None

    async def verify_websocket(self, websocket, token: str | None = None) -> bool:
        """Verify WebSocket authentication.

        Supports (in priority order):

        1. Short-lived WS auth token via ``?token=`` query param (preferred).
           Obtained by calling ``POST /v1/ws/auth`` with an API key.
        2. X-Auth-Token header (supported by non-browser WS clients).
        3. Session cookie (browser sessions).

        The deprecated ``?api_key=`` query param path was REMOVED (SEC-005,
        2026-06-09 sweep): raw API keys in query strings leak to server
        logs, proxy logs, and browser history. Clients exchange their key
        for a short-lived token via ``POST /v1/ws/auth`` instead.
        """
        if not self.enabled or not self.config.websocket_auth_enabled:
            return True

        # --- 1. Short-lived WS auth token (preferred for browser clients) ---
        ws_token = websocket.query_params.get("token")
        if ws_token:
            # requal-M2: tokens are single-use, but several verifiers can run
            # during ONE handshake (ui/api/routes/websocket_auth.py consumes
            # the ``?token=`` first while probing for a bound identity). If a
            # previous verifier on this same socket already consumed and
            # verified the token, its claims are cached on the connection
            # state — honor them instead of re-consuming, which would
            # misclassify the same handshake as a replay and 403 the UI.
            state = self._get_auth_state(websocket)
            if state is not None and getattr(state, "ws_auth_token_claims", None) is not None:
                return True
            claims = self.consume_ws_auth_token(ws_token)
            if claims is not None:
                if state is not None:
                    state.ws_auth_token_claims = claims
                return True

        # --- 2. X-Auth-Token header (non-browser WS clients, Qt native) ---
        if not token:
            token = websocket.headers.get(self.token_header_name)
        if token and self._verify_token(token):
            return True

        # --- 3. Session cookie (browser sessions with httpOnly cookie) ---
        session_cookie = getattr(websocket, "cookies", {}).get("viola_session")
        if session_cookie:
            if await self._verify_session_token(session_cookie, websocket=websocket):
                return True

        # SEC-005 (2026-06-09 sweep): the deprecated ``?api_key=`` query-param
        # path was removed on every surface. A raw API key in a query string
        # leaks to server logs, proxy logs, and browser history; the
        # short-lived token from POST /v1/ws/auth is the supported exchange.
        if websocket.query_params.get("api_key"):
            log.warning("Rejected removed ?api_key= WebSocket auth path; use POST /v1/ws/auth")

        return False


def create_auth_plugin(config: SecurityConfig | None = None) -> AuthenticationPlugin:
    """Create authentication plugin."""
    return AuthenticationPlugin(config)
