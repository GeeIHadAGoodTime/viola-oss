"""
Authentication Middleware for Viola FastAPI Backend.

This module provides Starlette/FastAPI middleware that extracts
authentication information from requests and attaches user context
to the request state.

Usage:
    >>> from fastapi import FastAPI
    >>> from auth.middleware import AuthMiddleware
    >>> app = FastAPI()
    >>> app.add_middleware(AuthMiddleware)

Architecture:
    - Extracts session token from Cookie or Authorization header
    - Validates session and loads user
    - Attaches user to request.state.user
    - Does NOT block requests - that's handled by dependencies

Integration with Existing Auth:
    - Works alongside ui/security/auth.py (API key auth for local)
    - Falls back to local auth only when no session token is present
    - GoTrue is the canonical cloud auth authority
"""

from __future__ import annotations

import hmac
import threading
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from core.logging_config import get_logger
from core.types import UserContext

if TYPE_CHECKING:
    from auth.models import Session, User

logger = get_logger("viola.auth.middleware")

# Cookie name for session token
SESSION_COOKIE_NAME = "viola_session"
REFRESH_COOKIE_NAME = "viola_refresh"
_GOTRUE_PROFILE_ENGINE = None
_GOTRUE_AUTH_ENGINE = None
_SESSION_USED_AUDIT_BUCKET_SECONDS = 60 * 60
_SESSION_USED_AUDIT_CACHE_MAX = 10000
_SESSION_USED_AUDIT_SEEN: dict[str, int] = {}
_SESSION_USED_AUDIT_LOCK = threading.Lock()


class _LocalUser:
    """Lightweight stand-in for ``auth.models.User`` in desktop/local mode.

    Only exposes the ``.id`` attribute so that ``get_current_user_id`` works
    without importing the full Pydantic model (avoids circular imports and
    the need for a real auth database in desktop mode).
    """

    __slots__ = ("id",)

    def __init__(self, user_id: str) -> None:
        self.id = user_id


# Header names for auth
AUTH_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
_LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_SPOKE_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SPOKE_TOKEN_COMPAT_COOKIE_NAME = "spoke_token"


def _is_loopback_client_host(client_host: str | None) -> bool:
    """Return True only for direct loopback clients.

    ``testclient`` is included so focused unit tests can exercise the
    desktop-local path without a real socket.
    """
    return bool(client_host) and client_host.lower() in _LOOPBACK_CLIENT_HOSTS


def _extract_bearer_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def extract_token_from_request(request: Request) -> str | None:
    """
    Extract session token from request.

    Checks in order:
    1. Authorization: Bearer <token> header
    2. viola_session cookie

    Args:
        request: Incoming HTTP request

    Returns:
        Session token if found, None otherwise
    """
    # Check Authorization header first
    bearer_token = _extract_bearer_token(request.headers.get(AUTH_HEADER))
    if bearer_token is not None:
        return bearer_token

    # Check cookie
    return request.cookies.get(SESSION_COOKIE_NAME)


def extract_refresh_token_from_request(request: Request) -> str | None:
    """Extract the rotating refresh token cookie from a request."""
    return request.cookies.get(REFRESH_COOKIE_NAME)


def _token_source_from_request(request: Request) -> str:
    if _extract_bearer_token(request.headers.get(AUTH_HEADER)) is not None:
        return "bearer"
    if request.cookies.get(SESSION_COOKIE_NAME):
        return "cookie"
    return "none"


def _audit_user_id_or_none(user_id: str | None) -> str | None:
    if not user_id:
        return None
    try:
        uuid.UUID(str(user_id))
    except (TypeError, ValueError):
        return None
    return str(user_id)


def _is_uuid_shaped(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        uuid.UUID(value.strip())
    except (TypeError, ValueError):
        return False
    return True


def _is_cloud_surface() -> bool:
    from config.settings import settings

    return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud"


def _claim_marks_spoke(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in {
        "spoke",
        "paired_spoke",
        "cloud_spoke",
    }


def _claims_mapping_marks_spoke(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(
        _claim_marks_spoke(value.get(key))
        for key in (
            "role",
            "actor_role",
            "viola_role",
            "viola_principal",
            "device_role",
            "credential_type",
        )
    )


def _gotrue_claims_indicate_spoke(claims: object) -> bool:
    """Decide whether a GoTrue JWT identifies a spoke principal.

    Security: ``user_metadata`` is partly attacker-controlled because
    GoTrue's ``POST /auth/v1/signup`` accepts a ``data:{...}`` envelope from
    the unauthenticated client and stores it verbatim in
    ``raw_user_meta_data`` (which is what populates the JWT's
    ``user_metadata`` claim). Only the reserved booleans ``email_verified``
    and ``phone_verified`` get normalized; arbitrary keys like ``role``,
    ``actor_role``, ``viola_role``, ``device_role``, ``credential_type``,
    and ``device_id`` are written through untouched.

    Reading any of those keys from ``user_metadata`` lets a regular signed-up
    user mark their own session as a spoke principal, which in turn pulls
    them through the spoke principal builder
    (``_spoke_principal_from_context``) and rewrites their ``UserContext``
    with an attacker-chosen ``device_id`` for audit / multiroom routing.
    The privilege impact is bounded today (spoke scope is read-only
    multiroom/audio paths and explicitly denies billing/admin/auth), but it
    still poisons audit trails and downgrades the user's own session into a
    confusing self-DOS — and it's exactly the trust boundary the wider
    multi-tenant model relies on staying clean.

    Only top-level role-shaped claims and ``app_metadata`` (server-side
    only — GoTrue refuses signup writes to ``app_metadata``) are honoured
    here.
    """
    if not isinstance(claims, Mapping):
        return False
    return (
        _claim_marks_spoke(claims.get("role"))
        or _claim_marks_spoke(claims.get("actor_role"))
        or _claim_marks_spoke(claims.get("credential_type"))
        or _claims_mapping_marks_spoke(claims.get("app_metadata"))
    )


def _spoke_device_id_from_claims(claims: object) -> str | None:
    """Return the spoke device_id from a trusted JWT location only.

    See ``_gotrue_claims_indicate_spoke`` — ``user_metadata`` is partly
    attacker-controlled, so a signup-time ``data:{device_id:"victim"}``
    would silently impersonate another spoke device in audit trails / room
    routing. Only the top-level claim and ``app_metadata`` are honoured.
    """
    if not isinstance(claims, Mapping):
        return None
    for key in ("device_id", "spoke_device_id"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = claims.get("app_metadata")
    if isinstance(nested, Mapping):
        for key in ("device_id", "spoke_device_id"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _response_sets_session_cookie(response: Response) -> bool:
    set_cookie_values = response.headers.getlist("set-cookie")
    return any(value.startswith(f"{SESSION_COOKIE_NAME}=") for value in set_cookie_values)


def _spoke_auth_error_response(status_code: int, code: str, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
            "data": None,
        },
        headers=headers,
    )


def _host_without_port(value: str | None) -> str | None:
    raw = (value or "").split(",", 1)[0].strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://%s" % raw
    host = urlsplit(raw).hostname
    return host.lower() if host else None


def _is_useviola_host(host: str) -> bool:
    return host == "useviola.com" or host.endswith(".useviola.com")


def _spoke_cookie_origin_allowed(request: Request) -> bool:
    origin_host = _host_without_port(request.headers.get("Origin"))
    if origin_host is None:
        return False
    if _is_useviola_host(origin_host):
        return True
    request_hosts = {
        host
        for host in (
            _host_without_port(request.headers.get("X-Forwarded-Host")),
            _host_without_port(request.headers.get("Host")),
        )
        if host
    }
    return origin_host in request_hosts


def _spoke_cookie_csrf_blocked(request: Request, *, token_source: str) -> bool:
    if token_source != "cookie":
        return False
    if request.method.upper() not in _SPOKE_CSRF_METHODS:
        return False
    return not _spoke_cookie_origin_allowed(request)


def _postgres_sqlalchemy_url(database_url: str) -> str:
    from auth import cloud_session

    return cloud_session._postgres_sqlalchemy_url(database_url)


def _get_gotrue_profile_engine():
    from auth import cloud_session

    return cloud_session._get_gotrue_profile_engine()


def _get_gotrue_auth_engine():
    from auth import cloud_session

    return cloud_session._get_gotrue_auth_engine()


def _set_sqlalchemy_app_user_id(conn: object, user_id: str) -> None:
    from auth import cloud_session

    return cloud_session._set_sqlalchemy_app_user_id(conn, user_id)


def _should_emit_session_used_audit(user_id: str, session_id: str, *, now: float | None = None) -> bool:
    """Return True once per user/session/hour bucket."""
    user_key = str(user_id or "").strip()
    session_key = str(session_id or "").strip()
    if not user_key or not session_key:
        return False

    current_bucket = int((time.time() if now is None else now) // _SESSION_USED_AUDIT_BUCKET_SECONDS)
    key = "%s:%s" % (user_key, session_key)
    with _SESSION_USED_AUDIT_LOCK:
        if _SESSION_USED_AUDIT_SEEN.get(key) == current_bucket:
            return False
        if len(_SESSION_USED_AUDIT_SEEN) >= _SESSION_USED_AUDIT_CACHE_MAX:
            stale_keys = [
                seen_key for seen_key, seen_bucket in _SESSION_USED_AUDIT_SEEN.items() if seen_bucket < current_bucket
            ]
            for seen_key in stale_keys:
                _SESSION_USED_AUDIT_SEEN.pop(seen_key, None)
            if len(_SESSION_USED_AUDIT_SEEN) >= _SESSION_USED_AUDIT_CACHE_MAX:
                _SESSION_USED_AUDIT_SEEN.clear()
        _SESSION_USED_AUDIT_SEEN[key] = current_bucket
        return True


def reset_session_used_audit_cache_for_tests() -> None:
    """Clear the hourly session-used sampling cache."""
    with _SESSION_USED_AUDIT_LOCK:
        _SESSION_USED_AUDIT_SEEN.clear()


class AuthMiddleware:
    """
    Authentication middleware that attaches user context to requests.

    Does not block unauthenticated requests - that's handled by
    route-level dependencies. This middleware simply extracts and
    validates any provided credentials.

    Implementation note: pure-ASGI middleware (NOT BaseHTTPMiddleware).
    BaseHTTPMiddleware spawns inner sub-tasks via anyio TaskGroup which
    breaks asyncpg's loop-bound connection waiters with
    ``RuntimeError: got Future ... attached to a different loop``.
    Pure ASGI keeps everything on the same event loop, so asyncpg pool
    acquires from session/user lookups stay safe.

    Attributes:
        session_service: Session verification service
        user_service: User lookup service
    """

    def __init__(
        self,
        app,
        session_service=None,
        user_service=None,
    ) -> None:
        """
        Initialize auth middleware.

        Args:
            app: Starlette/FastAPI ASGI application (callable)
            session_service: Optional session service (uses global if None)
            user_service: Optional user service (uses global if None)
        """
        self.app = app
        self._session_service = session_service
        self._user_service = user_service

    @property
    def user_service(self):
        """Return the injected legacy-compatible user service, if any."""
        return self._user_service

    async def __call__(self, scope, receive, send) -> None:
        """Pure-ASGI entry point.

        Extracts session token, validates, and attaches user to request state
        on the same event loop the downstream app runs on. No sub-task spawning,
        so asyncpg connection waiters stay loop-consistent.
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Bind the client's own IANA timezone for the life of this request, and
        # do it before any early return so even auth-exempt routes carry it.
        # Purely a ContextVar set from a header -- no I/O, no storage, no
        # consent surface -- which is what lets every downstream layer parse
        # and render wall-clock times in the user's zone instead of the
        # container's UTC (#3557: "2pm" from a Chicago user was stored 14:00Z).
        from services.user_timezone import client_timezone_from_headers, client_timezone_scope

        raw_headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in (scope.get("headers") or [])}
        with client_timezone_scope(client_timezone_from_headers(raw_headers)):
            await self._handle_http(scope, receive, send)

    async def _handle_http(self, scope, receive, send) -> None:
        """Authenticate one HTTP request and hand it to the downstream app."""
        # Build a Request view over the scope so we can read cookies/headers
        # and mutate request.state. Starlette's request.state writes through
        # to scope["state"], so downstream handlers see the same state dict.
        scope.setdefault("state", {})
        request = Request(scope, receive)

        request.state.user = None
        request.state.session = None
        request.state.auth_principal = None
        request.state.spoke_trusted = False
        request.state.user_id = None
        request.state.user_context = None
        request.state.request_context = None
        request.state.db_user_id = None
        request.state.db_user_context_token = None
        request.state.current_user_context_token = None
        request.state.current_cloud_access_token_context_token = None
        request.state.gotrue_claims = None
        request.state.auth_error = None
        request.state.clear_invalid_session_cookie = False
        request.state.clear_invalid_refresh_cookie = False
        request.state.rotated_access_token = None
        request.state.rotated_refresh_token = None
        request.state.rotated_access_max_age = None
        request.state.rotated_refresh_max_age = None
        request.state.gotrue_access_token = None

        path = scope.get("path", "")
        if self._should_skip_auth(path):
            await self.app(scope, receive, send)
            return

        token = extract_token_from_request(request)
        token_source = _token_source_from_request(request)

        if path.startswith("/auth/"):
            cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
            logger.debug(
                "[AUTH_DIAG] path=%s cookie_present=%s token_source=%s",
                path,
                cookie_value is not None,
                token_source,
            )

        gotrue_authenticated = False
        if token:
            if _is_cloud_surface():
                await self._attach_gotrue_authenticated_session(
                    request,
                    token=token,
                    token_source=token_source,
                )
            else:
                await self._attach_desktop_local_authenticated_session(
                    request,
                    token=token,
                    token_source=token_source,
                )
            gotrue_authenticated = getattr(request.state, "user", None) is not None

        if gotrue_authenticated:
            spoke_jwt_authenticated = _gotrue_claims_indicate_spoke(getattr(request.state, "gotrue_claims", None))
            if _is_cloud_surface() or spoke_jwt_authenticated:
                spoke_attempted, spoke_response = await self._resolve_spoke_request(
                    request,
                    authenticated_user_id=getattr(request.state, "user_id", None),
                    allow_spoke_jwt=spoke_jwt_authenticated,
                )
                if spoke_response is not None:
                    await spoke_response(scope, receive, send)
                    self._reset_request_identity_context(request)
                    return
        else:
            spoke_attempted, spoke_response = await self._resolve_spoke_request(request)
            if spoke_response is not None:
                await spoke_response(scope, receive, send)
                self._reset_request_identity_context(request)
                return
            # Fail closed: only bootstrap the local device principal when NO account
            # token was presented. A presented-but-invalid account cookie must remain
            # terminal-unauthenticated (it still gets cleared via clear_invalid_session_cookie
            # below), never silently downgraded to the shared install device principal.
            if not spoke_attempted and not token:
                self._maybe_inject_local_user(request)

        # Set request_context contextvar so downstream code sees it.
        token_ctx = None
        if request.state.request_context is not None:
            from core.request_context import set_request_context

            token_ctx = set_request_context(request.state.request_context)

        # Wrap send so we can clear an invalid session cookie on the response,
        # but only if the downstream app didn't already set its own viola_session.
        clear_invalid_cookie = bool(request.state.clear_invalid_session_cookie)
        clear_invalid_refresh_cookie = bool(request.state.clear_invalid_refresh_cookie)

        async def send_wrapper(message):
            if (clear_invalid_cookie or clear_invalid_refresh_cookie) and message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                already_setting_session = any(
                    name.lower() == b"set-cookie" and b"viola_session=" in value and b"Max-Age=0" not in value
                    for name, value in headers
                )
                if clear_invalid_cookie and not already_setting_session:
                    headers.append(
                        (
                            b"set-cookie",
                            b"viola_session=; Path=/; Max-Age=0; HttpOnly; SameSite=lax",
                        )
                    )
                if clear_invalid_refresh_cookie:
                    already_setting_refresh = any(
                        name.lower() == b"set-cookie" and b"viola_refresh=" in value and b"Max-Age=0" not in value
                        for name, value in headers
                    )
                    if not already_setting_refresh:
                        headers.append(
                            (
                                b"set-cookie",
                                b"viola_refresh=; Path=/; Max-Age=0; HttpOnly; SameSite=lax",
                            )
                        )
                message = {**message, "headers": headers}
            elif message.get("type") == "http.response.start":
                access_token = getattr(request.state, "rotated_access_token", None)
                refresh_token = getattr(request.state, "rotated_refresh_token", None)
                if access_token and refresh_token:
                    headers = list(message.get("headers", []))
                    secure = b"" if _is_localhost_http(request) else b"; Secure"
                    access_max_age = int(getattr(request.state, "rotated_access_max_age", None) or 15 * 60)
                    refresh_max_age = int(getattr(request.state, "rotated_refresh_max_age", None) or 30 * 24 * 60 * 60)
                    already_setting_session = any(
                        name.lower() == b"set-cookie" and b"viola_session=" in value and b"Max-Age=0" not in value
                        for name, value in headers
                    )
                    already_setting_refresh = any(
                        name.lower() == b"set-cookie" and b"viola_refresh=" in value and b"Max-Age=0" not in value
                        for name, value in headers
                    )
                    if not already_setting_session:
                        headers.append(
                            (
                                b"set-cookie",
                                b"viola_session="
                                + access_token.encode("ascii")
                                + b"; Path=/; Max-Age="
                                + str(access_max_age).encode("ascii")
                                + b"; HttpOnly; SameSite=lax"
                                + secure,
                            )
                        )
                    if not already_setting_refresh:
                        headers.append(
                            (
                                b"set-cookie",
                                b"viola_refresh="
                                + refresh_token.encode("ascii")
                                + b"; Path=/; Max-Age="
                                + str(refresh_max_age).encode("ascii")
                                + b"; HttpOnly; SameSite=lax"
                                + secure,
                            )
                        )
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self._reset_request_identity_context(request)
            if token_ctx is not None:
                from core.request_context import reset_request_context

                reset_request_context(token_ctx)

    @staticmethod
    def _reset_request_identity_context(request: Request) -> None:
        # NOTE: each reset tolerates ValueError("Token was created in a
        # different Context"). Route-level auth (ui/security/auth.py
        # require_auth) may set these ContextVars inside the ENDPOINT's task
        # context and stash the Token on request.state; this middleware
        # cleanup runs in its own context, where that Token cannot be reset.
        # That is harmless — the endpoint task's context (and the value set in
        # it) died with the task, so nothing leaks — but before this guard the
        # raise turned every succeeded handler into a 500 for signed-in
        # desktop users (found live 2026-07-04: /v1/onboarding/save,
        # /v1/onboarding/check-mic-permission, /v1/transcribe all 500ing right
        # after GoTrue sign-in).
        current_user_token = getattr(request.state, "current_user_context_token", None)
        if current_user_token is not None:
            request.state.current_user_context_token = None
            from core.user_context import reset_current_user_id

            try:
                reset_current_user_id(current_user_token)
            except ValueError:
                logger.debug("current_user_id token from another context; skipping reset")

        db_user_token = getattr(request.state, "db_user_context_token", None)
        if db_user_token is not None:
            request.state.db_user_context_token = None
            from core.db_backend import reset_db_user_id

            try:
                reset_db_user_id(db_user_token)
            except ValueError:
                logger.debug("db_user_id token from another context; skipping reset")

        cloud_token = getattr(request.state, "current_cloud_access_token_context_token", None)
        if cloud_token is not None:
            request.state.current_cloud_access_token_context_token = None
            from core.user_context import reset_current_cloud_access_token

            try:
                reset_current_cloud_access_token(cloud_token)
            except ValueError:
                logger.debug("cloud_access_token token from another context; skipping reset")

    async def _attach_gotrue_authenticated_session(
        self,
        request: Request,
        *,
        token: str,
        token_source: str,
    ) -> None:
        try:
            from auth import cloud_session
        except ImportError:
            logger.warning("Hosted authentication is unavailable on this installation")
            request.state.auth_error = "Invalid or expired session"
            request.state.clear_invalid_session_cookie = token_source == "cookie"
            return

        return await cloud_session._attach_gotrue_authenticated_session(
            self, request, token=token, token_source=token_source
        )

    async def _attach_desktop_local_authenticated_session(
        self,
        request: Request,
        *,
        token: str,
        token_source: str,
    ) -> None:
        db_user_token = None
        try:
            from auth.desktop_session import (
                validate_desktop_session_token,
            )
            from auth.principals import UserPrincipal
            from config.settings import settings
            from core.db_backend import set_db_user_id
            from core.product import AppSurface, PlanId, coerce_app_surface
            from core.request_context import RequestContext
            from core.user_context import set_current_cloud_access_token, set_current_user_id

            validation = await validate_desktop_session_token(token)
            if not validation.authenticated or validation.user is None or validation.session is None:
                logger.warning(
                    "Desktop local auth middleware rejected token: %s",
                    validation.reason,
                )
                request.state.auth_error = "Invalid or expired session"
                # A TRANSIENT refresh failure (cloud 5xx / network blip mid-refresh)
                # PRESERVES the server-side session row (auth/desktop_session.py's
                # DesktopSessionRefreshTransientError) — only THIS request should
                # fail unauthenticated. Clearing the cookie here would still sign
                # the browser out even though nothing was revoked (issue #340:
                # a >30-min-old desktop session showed "signed-in: none" after a
                # transient hiccup hit the scheduled access-token refresh).
                is_transient_refresh_failure = validation.reason == "gotrue_refresh_transient"
                should_clear_cookie = token_source == "cookie" and not is_transient_refresh_failure
                request.state.clear_invalid_session_cookie = should_clear_cookie
                request.state.clear_invalid_refresh_cookie = should_clear_cookie
                return

            user = validation.user
            session = validation.session
            db_user_token = set_db_user_id(user.id)

            request.state.session = session
            request.state.user = user
            request.state.user_id = user.id
            request.state.db_user_id = user.id
            request.state.db_user_context_token = db_user_token
            request.state.gotrue_claims = None
            request.state.gotrue_access_token = validation.access_token
            request.state.auth_principal = UserPrincipal(
                user_id=user.id,
                session_id=session.id,
                role="authenticated",
            )
            request.state.auth_error = None

            request.state.current_user_context_token = set_current_user_id(user.id)
            if validation.access_token:
                request.state.current_cloud_access_token_context_token = set_current_cloud_access_token(
                    validation.access_token
                )
            request.state.user_context = UserContext(
                user_id=user.id,
                session_id=session.id,
            )
            surface = coerce_app_surface(getattr(settings, "app_surface", AppSurface.DESKTOP.value))
            plan_id = user.plan_id.value if getattr(user, "has_paid_access", False) else PlanId.FREE.value
            request.state.request_context = RequestContext(
                user_id=user.id,
                plan_id=plan_id,
                surface=surface.value,
                trust_level="cloud_authenticated",
            )
            await self._emit_session_used_audit(
                request,
                user=user,
                session=session,
                token_source="desktop_local_%s" % token_source,
            )
        except (
            AttributeError,
            ImportError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            if db_user_token is not None:
                from core.db_backend import reset_db_user_id

                reset_db_user_id(db_user_token)
            from auth.desktop_session import (
                delete_desktop_session_token,
                is_desktop_local_session_token,
            )

            if is_desktop_local_session_token(token):
                delete_desktop_session_token(token)
            logger.warning("Desktop local auth middleware failed closed: %s", exc)
            request.state.auth_error = "Invalid or expired session"
            request.state.clear_invalid_session_cookie = token_source == "cookie"
            request.state.clear_invalid_refresh_cookie = token_source == "cookie"
            return

    async def _resolve_spoke_request(
        self,
        request: Request,
        *,
        authenticated_user_id: str | None = None,
        allow_spoke_jwt: bool = False,
    ) -> tuple[bool, JSONResponse | None]:
        from auth.spoke_scopes import (
            is_spoke_allowed,
            is_spoke_denied,
            is_spoke_path_allowed,
        )
        from ui.security.spoke_credentials import (
            SPOKE_TOKEN_COOKIE_NAME,
            SPOKE_TOKEN_HEADER_NAME,
            validate_spoke_token,
        )

        header_token = (request.headers.get(SPOKE_TOKEN_HEADER_NAME) or "").strip()
        cookie_token = (
            request.cookies.get(SPOKE_TOKEN_COOKIE_NAME) or request.cookies.get(_SPOKE_TOKEN_COMPAT_COOKIE_NAME) or ""
        ).strip()
        if not header_token and not cookie_token and not allow_spoke_jwt:
            return False, None

        credential = None
        token_source = "none"
        if header_token:
            credential = validate_spoke_token(header_token)
            if credential is not None:
                token_source = "header"
        if credential is None and cookie_token:
            credential = validate_spoke_token(cookie_token)
            if credential is not None:
                token_source = "cookie"

        if credential is None and (header_token or cookie_token):
            return True, _spoke_auth_error_response(
                401,
                "not_authenticated",
                "Authentication required",
            )
        if credential is None and not allow_spoke_jwt:
            return False, None

        if _spoke_cookie_csrf_blocked(request, token_source=token_source):
            return True, _spoke_auth_error_response(
                403,
                "csrf_blocked",
                "Cookie-based spoke requests require same-origin Origin on state-changing methods.",
            )

        principal = self._spoke_principal_from_context(
            credential=credential,
            authenticated_user_id=authenticated_user_id,
            claims=getattr(request.state, "gotrue_claims", None),
        )
        if principal.hub_user_id is None or (_is_cloud_surface() and not _is_uuid_shaped(principal.hub_user_id)):
            return True, _spoke_auth_error_response(
                401,
                "not_authenticated",
                "Cloud spoke authentication requires a valid user UUID.",
            )

        # A paired LAN/desktop spoke (validated HMAC credential, not the cloud
        # surface) is a trusted window into the hub and gets full hub parity
        # minus the sensitive denylist. A cloud spoke (GoTrue-JWT-marked, no
        # local credential) stays on the narrow read-only allowlist.
        trusted_spoke = (
            credential is not None
            and getattr(credential, "source", None) in ("paired", "legacy_shared_secret")
            and not _is_cloud_surface()
        )

        path = request.url.path
        if is_spoke_denied(path, trusted=trusted_spoke):
            await self._emit_spoke_scope_denied_audit(request, principal=principal)
            return True, _spoke_auth_error_response(
                403,
                "scope_exceeded",
                "Spoke credentials cannot access this resource.",
            )

        if not is_spoke_path_allowed(path, trusted=trusted_spoke):
            return True, _spoke_auth_error_response(
                401,
                "not_authenticated",
                "Authentication required",
            )

        if not is_spoke_allowed(path, request.method, trusted=trusted_spoke):
            await self._emit_spoke_scope_denied_audit(request, principal=principal)
            return True, _spoke_auth_error_response(
                403,
                "method_not_allowed_for_spoke",
                "Spoke credentials cannot use this HTTP method for this resource.",
            )

        self._attach_spoke_authenticated_session(request, principal=principal)
        # Record the trust decision so route-level operator guards
        # (auth.dependencies.require_auth_or_api_key) can align with the scope
        # decision already made here. A PIN-paired LAN spoke is trusted for the
        # reads the scope allowlist permits; a cloud-narrow spoke is not.
        request.state.spoke_trusted = bool(trusted_spoke)
        return True, None

    @staticmethod
    def _spoke_principal_from_context(*, credential, authenticated_user_id: str | None, claims: object):
        from auth.principals import SpokePrincipal

        hub_user_id = authenticated_user_id
        if hub_user_id is None and credential is not None:
            hub_user_id = credential.hub_user_id

        device_id = None
        token_id = None
        if credential is not None:
            device_id = credential.device_id or "legacy_shared_secret"
            token_id = credential.token_id
        else:
            device_id = _spoke_device_id_from_claims(claims) or "gotrue_spoke"
            session_id = None
            if isinstance(claims, Mapping):
                raw_session_id = claims.get("session_id")
                if isinstance(raw_session_id, str) and raw_session_id.strip():
                    session_id = raw_session_id.strip()
            token_id = "gotrue:%s" % (session_id or "spoke_jwt")

        return SpokePrincipal(
            device_id=device_id,
            token_id=token_id,
            hub_user_id=hub_user_id,
        )

    def _attach_spoke_authenticated_session(self, request: Request, *, principal) -> None:
        from config.settings import settings
        from core.db_backend import set_db_user_id
        from core.product import AppSurface, PlanId, coerce_app_surface
        from core.request_context import RequestContext
        from core.user_context import set_current_user_id

        if principal.hub_user_id is None or (_is_cloud_surface() and not _is_uuid_shaped(principal.hub_user_id)):
            raise ValueError("Cloud spoke authentication requires a valid user UUID")

        self._reset_request_identity_context(request)
        db_user_token = set_db_user_id(principal.hub_user_id)
        request.state.auth_principal = principal
        request.state.user_id = principal.hub_user_id
        request.state.user = _LocalUser(principal.hub_user_id)
        request.state.db_user_id = principal.hub_user_id
        request.state.db_user_context_token = db_user_token
        request.state.auth_error = None

        request.state.current_user_context_token = set_current_user_id(principal.hub_user_id)
        request.state.user_context = UserContext(
            user_id=principal.hub_user_id,
            device_id=principal.device_id,
        )

        surface = coerce_app_surface(getattr(settings, "app_surface", AppSurface.DESKTOP.value))
        trust_level = "local_owner" if surface is AppSurface.DESKTOP else "cloud_authenticated"
        request.state.request_context = RequestContext(
            user_id=principal.hub_user_id,
            plan_id=PlanId.FREE.value,
            surface=surface.value,
            trust_level=trust_level,
        )

    async def _emit_spoke_scope_denied_audit(self, request: Request, *, principal) -> None:
        try:
            from auth.audit import AuthEventType, get_auth_audit_logger
            from auth.ip_utils import extract_client_info

            ip_address, user_agent = extract_client_info(request)
            await get_auth_audit_logger().alog_event(
                AuthEventType.SPOKE_SCOPE_DENIED,
                user_id=_audit_user_id_or_none(principal.hub_user_id),
                ip_address=ip_address,
                user_agent=user_agent,
                outcome="blocked",
                details={
                    "device_id": principal.device_id,
                    "token_id": principal.token_id,
                    "hub_user_id": principal.hub_user_id,
                    "path": request.url.path,
                    "method": request.method,
                },
            )
        except Exception:
            logger.exception("Failed to emit spoke scope-denied auth audit event")

    async def _emit_session_used_audit(
        self,
        request: Request,
        *,
        user: User,
        session: Session,
        token_source: str,
    ) -> None:
        if not _should_emit_session_used_audit(user.id, session.id):
            return
        try:
            from auth.audit import AuthEventType, get_auth_audit_logger
            from auth.ip_utils import extract_client_info

            ip_address, user_agent = extract_client_info(request)
            await get_auth_audit_logger().alog_event(
                AuthEventType.SESSION_USED,
                user_id=user.id,
                email=str(user.email) if getattr(user, "email", None) else None,
                ip_address=ip_address,
                user_agent=user_agent,
                details={
                    "session_id": session.id,
                    "path": request.url.path,
                    "method": request.method,
                    "token_source": token_source,
                    "sample": "hourly_per_session",
                },
            )
        except Exception:
            logger.exception("Failed to emit GoTrue session-used auth audit event")

    def _get_or_create_gotrue_app_profile(self, request: Request, user_id: str) -> dict:
        from auth import cloud_session

        return cloud_session._get_or_create_gotrue_app_profile(self, request, user_id)

    def _sync_gotrue_profile_verification(self, conn, user_id: str, row: dict) -> dict:
        from auth import cloud_session

        return cloud_session._sync_gotrue_profile_verification(self, conn, user_id, row)

    async def _gotrue_session_is_alive(self, request: Request, *, user_id: str, session_id: str) -> bool:
        from auth import cloud_session

        return await cloud_session._gotrue_session_is_alive(self, request, user_id=user_id, session_id=session_id)

    def _gotrue_session_exists(self, request: Request, user_id: str, session_id: str) -> bool:
        from auth import cloud_session

        return cloud_session._gotrue_session_exists(self, request, user_id, session_id)

    @staticmethod
    def _build_request_context(user: User):
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

    def _maybe_inject_local_user(self, request: Request) -> None:
        """Attach the trusted desktop *active* principal for local API-key requests.

        The desktop API-key path (the CLAUDE.md-canonical viola-runner
        ``/v1/command`` path) may be used before a GoTrue session exists, but the
        principal it represents is the install's *active* user, never a
        caller-supplied header. When a valid desktop GoTrue session is signed in,
        that active user IS the signed-in account; only when no account is signed
        in does it fall back to the bootstrap device binding.

        #2646 / M-BILL-1 (#337): this previously bound the bare device-id helper
        unconditionally, so a GUI-signed-in install still resolved the anonymous
        ``device-*`` principal on every cookieless API-key turn. The managed-AI
        account gate (``core/account_gate.requires_account_for_command`` via
        ``ui/api/routes/command.py``) then read that device principal as "no
        account" and fired ``account_required`` even though the desktop was signed
        in (live repro on the signed v17 bundle, #2619). ``get_desktop_active_user_id``
        is the same account-preferring resolver PR #2031 wired into the managed-LLM
        provider router/factory — it returns the signed-in GoTrue account when a
        valid desktop session row exists and falls back to the device id otherwise,
        so signed-out installs still gate exactly as before.
        """
        from config.settings import settings
        from ui.security.config import get_security_config

        config = get_security_config()
        if str(getattr(settings, "app_surface", "desktop")).lower() == "cloud":
            return

        client_host = request.client.host if request.client is not None else None
        if not _is_loopback_client_host(client_host):
            return

        api_key = request.headers.get("X-API-Key")
        api_key_matches = bool(api_key and config.auth_api_key and hmac.compare_digest(api_key, config.auth_api_key))

        if not api_key_matches:
            return

        if request.state.user is not None:
            # Already authenticated via another mechanism
            return

        from core.user_context import get_desktop_active_user_id, set_current_user_id

        # mt-ok: desktop API-key auth injects the install's active principal —
        # the signed-in GoTrue account when one exists, else the bootstrap device
        # id (never a caller-supplied header). See #2646 in the docstring above.
        local_user_id = get_desktop_active_user_id()
        request.state.current_user_context_token = set_current_user_id(local_user_id)
        request.state.user = _LocalUser(local_user_id)
        request.state.user_id = local_user_id
        request.state.user_context = UserContext(
            user_id=local_user_id,
        )
        request.state.auth_error = None

    def _should_skip_auth(self, path: str) -> bool:
        """
        Check if auth should be skipped for this path.

        Some paths don't need auth processing (static files, health checks).
        """
        # SEC-025/SEC-027 (2026-06-09 sweep): EXPLICIT prefixes only.
        # "/metrics" must NOT be here — the cloud Prometheus scrape route
        # enforces its own scrape auth (admin/cloud_metrics.py) and the desktop
        # variant is loopback-gated; skipping auth processing here hid the
        # public exposure. Wildcard namespace prefixes like "/_" are forbidden:
        # they silently unauthenticate every future route registered underneath
        # them. Add the exact prefix a route actually needs, never a namespace.
        skip_prefixes = (
            "/static/",
            "/health",
            "/status",
            "/api/v1/health",
            "/api/v1/status",
        )
        return any(path.startswith(prefix) for prefix in skip_prefixes)


# =============================================================================
# Session Cookie Utilities
# =============================================================================


def _is_localhost_http(request_or_response: object = None) -> bool:
    """Detect if running on localhost HTTP (not HTTPS).

    Desktop app always runs on localhost HTTP, so secure=False is correct.
    This prevents cookie rejection by browsers that enforce Secure flag on HTTP.
    """
    from core.constants import LOCALHOST, LOCALHOST_NAME

    localhost_hosts = {LOCALHOST, LOCALHOST_NAME, "127.0.0.1", "::1"}

    if request_or_response is not None:
        try:
            url = getattr(request_or_response, "url", None)
            scheme = str(getattr(url, "scheme", "") or "").lower()
            hostname = str(getattr(url, "hostname", "") or "").lower()
            if scheme == "http" and hostname in localhost_hosts:
                return True
        except Exception:
            pass

    # Check settings-based detection (most reliable for desktop app)
    try:
        from config.settings import settings

        surface = getattr(settings, "app_surface", "desktop") or "desktop"
        surface_value = getattr(surface, "value", surface)
        if str(surface_value).strip().lower() == "cloud":
            return False
        host = getattr(settings, "api_host", "") or ""
        if host in (LOCALHOST, LOCALHOST_NAME, "0.0.0.0", ""):  # nosec B104
            return True
    except Exception:
        pass

    return False


def set_session_cookie(
    response: Response,
    token: str,
    max_age_days: int = 30,
    max_age_seconds: int | None = None,
    secure: bool | None = None,
    domain: str | None = None,
) -> None:
    """
    Set session cookie on response.

    Args:
        response: FastAPI/Starlette response
        token: Session token
        max_age_days: Cookie lifetime in days
        max_age_seconds: Explicit cookie lifetime in seconds. When provided,
            takes precedence over max_age_days.
        secure: Whether to set Secure flag (HTTPS only).
                None = auto-detect (False on localhost HTTP, True otherwise).
        domain: Optional cookie domain
    """
    if secure is None:
        secure = not _is_localhost_http()

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=(max_age_seconds if max_age_seconds is not None else max_age_days * 24 * 60 * 60),
        httponly=True,
        secure=secure,
        samesite="lax",  # "strict" breaks OAuth redirects from Google
        domain=domain,
        path="/",
    )


def set_refresh_cookie(
    response: Response,
    token: str,
    max_age_days: int = 30,
    secure: bool | None = None,
    domain: str | None = None,
) -> None:
    """Set the rotating refresh token cookie on response."""
    if secure is None:
        secure = not _is_localhost_http()

    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age_days * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        domain=domain,
        path="/",
    )


def clear_session_cookie(response: Response, domain: str | None = None) -> None:
    """
    Clear session cookie from response.

    Args:
        response: FastAPI/Starlette response
        domain: Optional cookie domain (must match set_session_cookie)
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        domain=domain,
        path="/",
    )


def clear_refresh_cookie(response: Response, domain: str | None = None) -> None:
    """Clear the refresh token cookie from response."""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        domain=domain,
        path="/",
    )


# =============================================================================
# Request Context Helpers
# =============================================================================


def get_user_from_request(request: Request) -> User | None:
    """Get authenticated user from request state."""
    return getattr(request.state, "user", None)


def get_session_from_request(request: Request) -> Session | None:
    """Get session from request state."""
    return getattr(request.state, "session", None)


def get_auth_error_from_request(request: Request) -> str | None:
    """Get auth error message from request state."""
    return getattr(request.state, "auth_error", None)
