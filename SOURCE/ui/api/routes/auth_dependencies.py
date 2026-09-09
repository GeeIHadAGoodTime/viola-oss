"""
Authentication Dependencies for API Routes.

Provides FastAPI dependencies for requiring authentication on routes.
"""

from __future__ import annotations

from dataclasses import dataclass

from auth.dependencies import require_auth_or_api_key
from core.logging_config import get_logger
from fastapi import HTTPException, Request, status
from ui.core.security import is_desktop_surface, is_loopback_request
from ui.security.config import get_security_config

logger = get_logger(__name__)


@dataclass(frozen=True)
class PublicRouteRule:
    methods: frozenset[str]
    path: str
    prefix: bool = False

    def matches(self, method: str, path: str) -> bool:
        if method.upper() not in self.methods:
            return False
        if self.prefix:
            return path.startswith(self.path)
        return path == self.path


_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})
_LEGACY_CONFIRM_METHODS = frozenset({"GET", "HEAD", "POST"})

# Explicitly public routes that never require auth.
# These rules are method-aware so a public GET on a shared path does not
# accidentally make POST/PUT/DELETE public too.
PUBLIC_ROUTE_RULES: tuple[PublicRouteRule, ...] = (
    PublicRouteRule(_READ_ONLY_METHODS, "/health"),
    PublicRouteRule(_READ_ONLY_METHODS, "/status"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/health"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/health"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/health/details"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/status"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/health-deep"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/incidents"),
    PublicRouteRule(_READ_ONLY_METHODS, "/monitoring/healthz"),
    PublicRouteRule(_READ_ONLY_METHODS, "/monitoring/readyz"),
    PublicRouteRule(_READ_ONLY_METHODS, "/auth/providers"),
    PublicRouteRule(_READ_ONLY_METHODS, "/auth/oauth/preflight"),
    PublicRouteRule(_READ_ONLY_METHODS, "/"),
    PublicRouteRule(_LEGACY_CONFIRM_METHODS, "/confirm-test-session"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/telemetry/status"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/version/latest"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/version"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/consent/status"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/consent/capabilities"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/consent/providers/status"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/browser/auth/status"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/spotify/cdp/status"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/rooms"),
    PublicRouteRule(_READ_ONLY_METHODS, "/v1/rooms/groups"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/clock"),
    PublicRouteRule(_READ_ONLY_METHODS, "/api/v1/multiroom/sync-diag"),
)


def _is_public_route(method: str, path: str) -> bool:
    return any(rule.matches(method, path) for rule in PUBLIC_ROUTE_RULES)


async def require_auth(request: Request) -> None:
    """
    FastAPI dependency that requires authentication for API endpoints.

    Usage:
        @router.get("/v1/something", dependencies=[Depends(require_auth)])
        async def my_endpoint(): ...
    """
    method = request.method.upper()
    path = request.url.path

    if _is_public_route(method, path):
        return

    config = get_security_config()
    if not config.auth_enabled:
        if is_desktop_surface() and is_loopback_request(request):
            return
        logger.warning(
            "Blocked unauthenticated remote access to %s %s while desktop auth is disabled",
            method,
            path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Loopback access required while desktop authentication is disabled.",
        )

    if getattr(request.state, "user", None) is not None:
        return

    from ui.security.auth import AuthenticationPlugin

    auth_plugin = AuthenticationPlugin(config)
    if not await auth_plugin.verify_request(request):
        logger.warning("Authentication required for %s %s", method, path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "ApiKey"},
        )


async def require_operator_auth(request: Request) -> None:
    """Require desktop-operator access for sensitive mutable routes."""
    await require_auth_or_api_key(request)


async def get_current_user_id(request: Request) -> str:
    """Extract authenticated user_id from request state."""
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "id", None):
        return user.id

    user_context = getattr(request.state, "user_context", None)
    if user_context is not None and getattr(user_context, "user_id", None):
        return user_context.user_id

    config = get_security_config()
    if not config.auth_enabled and is_desktop_surface() and is_loopback_request(request):
        from core.user_context import get_device_user_id

        # mt-ok: auth-disabled desktop loopback uses the single-install device principal.
        return get_device_user_id()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )
