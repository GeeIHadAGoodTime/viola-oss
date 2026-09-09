"""
FastAPI Dependencies for Viola Authentication.

This module provides dependency injection functions for route-level
authentication and authorization in FastAPI routes.

Architecture:
    - Dependencies use request.state set by AuthMiddleware
    - Raise HTTPException for auth failures (consistent error responses)
    - Support both required and optional authentication
"""

from __future__ import annotations

from typing import Annotated

from auth.middleware import (
    extract_token_from_request,
    get_auth_error_from_request,
    get_session_from_request,
    get_user_from_request,
)
from auth.models import Session, User
from core.logging_config import get_logger
from fastapi import Depends, HTTPException, Request, status
from ui.core.security import is_desktop_surface, is_loopback_request

logger = get_logger("viola.auth.dependencies")


# =============================================================================
# Authentication Dependencies
# =============================================================================


async def get_current_user_optional(request: Request) -> User | None:
    """
    Get current user if authenticated, None otherwise.

    Use this when authentication is optional but you want to
    customize behavior for authenticated users.

    Args:
        request: FastAPI request

    Returns:
        User if authenticated, None otherwise
    """
    return get_user_from_request(request)


async def get_current_user(request: Request) -> User:
    """
    Get current authenticated user.

    Raises HTTPException 401 if not authenticated.

    Args:
        request: FastAPI request

    Returns:
        Authenticated user

    Raises:
        HTTPException: 401 if not authenticated
    """
    user = get_user_from_request(request)
    if user is None:
        auth_error = get_auth_error_from_request(request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "not_authenticated",
                "message": auth_error or "Authentication required",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_current_session(request: Request) -> Session:
    """
    Get current session.

    Raises HTTPException 401 if no valid session.

    Args:
        request: FastAPI request

    Returns:
        Current session

    Raises:
        HTTPException: 401 if no session
    """
    session = get_session_from_request(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "no_session",
                "message": "Valid session required",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return session


async def get_verified_user(user: User = Depends(get_current_user)) -> User:
    """
    Get current user with verified email.

    Raises HTTPException 403 if email not verified.

    Args:
        user: Authenticated user (from get_current_user)

    Returns:
        User with verified email

    Raises:
        HTTPException: 403 if email not verified
    """
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "email_not_verified",
                "message": "Please verify your email address",
            },
        )
    return user


# =============================================================================
# Authorization Dependencies
# =============================================================================


async def require_session_auth(user: User = Depends(get_current_user)) -> User:
    """
    Require session-based authentication (cloud auth).

    Returns the authenticated User object.  This is used by cloud/session-based
    routes that need the User model.  For API-key + session auth (used by most
    routes), see ``ui.api.routes.auth_dependencies.require_auth`` instead.

    Args:
        user: Authenticated user

    Returns:
        Authenticated user

    Raises:
        HTTPException: 401 if not authenticated
    """
    return user


async def require_real_user(user: User = Depends(get_current_user)) -> User:
    """Require a real ``auth.models.User`` instance — reject ``_LocalUser``.

    Use this for cloud-only endpoints that need the full Pydantic ``User``
    with attributes like ``email``, ``plan_id``, and ``subscription_status``.
    Routes intentionally shared between desktop and cloud (and thus handle
    ``_LocalUser`` gracefully, like ``/auth/me``) should NOT depend on this
    — use ``get_current_user`` there.

    Raises 401 when the middleware attached a ``_LocalUser`` (desktop mode
    with auth disabled) or when the session is otherwise not backed by a
    real cloud User.
    """
    if not isinstance(user, User):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "not_authenticated",
                "message": "Cloud authentication required for this endpoint.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_auth_or_api_key(request: Request) -> User | None:
    """
    Require desktop-operator auth: loopback session or explicit API key.

    On desktop, this is intentionally stricter than generic auth:
    - loopback requests may use session auth or the API key
    - non-loopback requests must present a valid X-API-Key
    - when security auth is disabled, only loopback requests are allowed

    In cloud mode, remote session auth remains valid because the route
    surface is user-scoped rather than machine-operator scoped.

    Returns the User when session auth succeeds, or None when API-key
    auth succeeds (no user object available).

    Raises:
        HTTPException: 401 if neither JWT nor API key is valid
    """
    import hmac

    from ui.security.config import get_security_config

    config = get_security_config()
    client_is_loopback = is_loopback_request(request)
    desktop_surface = is_desktop_surface()
    remote_operator_message = "Remote desktop settings access requires a valid X-API-Key."

    if not config.auth_enabled:
        if desktop_surface and client_is_loopback:
            return get_user_from_request(request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "desktop_loopback_required",
                "message": "Desktop settings access is limited to loopback while security auth is disabled.",
            },
        )

    api_key = request.headers.get("X-API-Key")
    api_key_valid = bool(api_key and config.auth_api_key and hmac.compare_digest(api_key, config.auth_api_key))
    if api_key_valid:
        return None

    user = get_user_from_request(request)
    if user is not None and (not desktop_surface or client_is_loopback):
        return user

    # A PIN-paired LAN spoke is a trusted read-only window into the hub. It
    # reaches this guard only after the auth middleware has already (a) verified
    # its HMAC spoke credential — which exists only because the bootstrap PIN
    # pairing flow minted it — and (b) authorized this exact path+method against
    # the trusted-spoke scope allowlist (request.state.spoke_trusted). For SAFE
    # (read) methods we honor that decision so the spoke can render hub parity
    # (weather/theme/layout) without the operator API key. Operator MUTATIONS
    # stay gated: a remote spoke still cannot WRITE settings without X-API-Key,
    # so the PIN-pairing gate and the read-only boundary are both preserved.
    if (
        request.method in ("GET", "HEAD", "OPTIONS")
        and getattr(request.state, "spoke_trusted", False)
        and user is not None
    ):
        return user

    if desktop_surface and user is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "explicit_api_key_required",
                "message": remote_operator_message,
            },
        )

    auth_error = get_auth_error_from_request(request)
    if client_is_loopback or not desktop_surface:
        error_code = "not_authenticated"
        message = auth_error or "Authentication required. Provide a valid session or API key."
        error_status = status.HTTP_401_UNAUTHORIZED
    else:
        error_code = "explicit_api_key_required"
        message = remote_operator_message
        error_status = status.HTTP_403_FORBIDDEN

    raise HTTPException(
        status_code=error_status,
        detail={
            "error": error_code,
            "message": message,
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_verified_email(user: User = Depends(get_verified_user)) -> User:
    """
    Require authenticated user with verified email.

    Args:
        user: Verified user

    Returns:
        User with verified email
    """
    return user


# =============================================================================
# Service Dependencies (for injection into routes)
# =============================================================================


async def get_auth_db(request: Request):
    """
    Get auth database from app state or global singleton.

    Args:
        request: FastAPI request

    Returns:
        Auth database instance
    """
    # Try app.state first (set during startup)
    db = getattr(request.app.state, "auth_db", None)
    if db is not None:
        return db

    # Fall back to global singleton
    from auth.database import get_auth_db as _get_global_db

    return _get_global_db()


# =============================================================================
# Token Extraction (for routes that need raw token)
# =============================================================================


async def get_auth_token(request: Request) -> str | None:
    """
    Get raw auth token from request.

    Use this when you need the token itself, not the user.

    Args:
        request: FastAPI request

    Returns:
        Auth token if present, None otherwise
    """
    return extract_token_from_request(request)


async def require_auth_token(token: str | None = Depends(get_auth_token)) -> str:
    """
    Require auth token in request.

    Args:
        token: Auth token from request

    Returns:
        Auth token

    Raises:
        HTTPException: 401 if no token
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "no_token", "message": "Authorization token required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


# =============================================================================
# Type Aliases for Dependency Injection
# =============================================================================

# Use these for cleaner type annotations in route functions
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user_optional)]
VerifiedUser = Annotated[User, Depends(require_verified_email)]
CurrentSession = Annotated[Session, Depends(get_current_session)]
