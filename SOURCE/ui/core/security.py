from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import ParamSpec, TypeVar, cast
from urllib.parse import urlparse

from config import env
from config.settings import settings
from core.logging_config import get_logger
from fastapi import FastAPI, Request, Response, WebSocket
from ui.security import (
    AuthenticationPlugin,
    ErrorSanitizer,
    InputValidator,
    RateLimiterPlugin,
    ResourceLimits,
    SecurityConfig,
    SecurityManager,
    create_auth_plugin,
    create_rate_limiter,
    get_error_sanitizer,
    get_input_validator,
    get_resource_limits,
    get_security_config,
    get_security_manager,
)

log = get_logger(__name__)
from ui.security.bootstrap import register_bootstrap_routes
from ui.security.middleware import security_middleware

P = ParamSpec("P")
R = TypeVar("R")
RateLimitDecorator = Callable[[str], Callable[[Callable[P, R]], Callable[P, R]]]

_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::"})  # nosec B104 - explicit detection of wildcard bind hosts
_DEFAULT_CLOUD_WEBSOCKET_ORIGINS = (
    "https://api.useviola.com",
    "https://useviola.com",
    "https://www.useviola.com",
)


def is_loopback_bind_host(host: str | None) -> bool:
    """Return True when *host* binds only to the local machine."""
    return (host or "").strip().lower() in _LOOPBACK_BIND_HOSTS


def is_wildcard_bind_host(host: str | None) -> bool:
    """Return True for wildcard binds that expose the server on all interfaces."""
    return (host or "").strip().lower() in _WILDCARD_BIND_HOSTS


def is_lan_exposed_host(host: str | None) -> bool:
    """Return True when *host* is reachable beyond loopback."""
    normalized = (host or "").strip().lower()
    if not normalized:
        return False
    return not is_loopback_bind_host(normalized)


def is_loopback_client_host(client_host: str | None) -> bool:
    """Return True only for direct loopback clients."""
    return (client_host or "").strip().lower() in _LOOPBACK_CLIENT_HOSTS


def _allowed_websocket_origins() -> tuple[str, ...]:
    configured = list(settings.cors_origins or [])
    if str(getattr(settings, "app_surface", "desktop")).strip().lower() == "cloud":
        configured.extend(_DEFAULT_CLOUD_WEBSOCKET_ORIGINS)
        configured.extend(getattr(settings, "cloud_cors_origins", None) or [])

    normalized: list[str] = []
    for origin in configured:
        value = str(origin or "").strip().rstrip("/")
        if value and value != "*" and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def is_loopback_request(request: Request) -> bool:
    """Return True when the incoming request originated from loopback."""
    client_host = request.client.host if request.client is not None else None
    return is_loopback_client_host(client_host)


def is_desktop_surface() -> bool:
    """Return True when Viola is running as the desktop surface."""
    return str(getattr(settings, "app_surface", "desktop")).lower() == "desktop"


@dataclass(frozen=True)
class SecurityPostureSnapshot:
    """Structured snapshot of the current desktop/network security posture."""

    app_surface: str
    api_host: str
    api_port: int
    lan_exposed: bool
    wildcard_bind: bool
    auth_enabled: bool
    multiroom_enabled: bool
    auto_promoted_for_multiroom: bool
    mode: str
    summary: str
    warning: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "app_surface": self.app_surface,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "lan_exposed": self.lan_exposed,
            "wildcard_bind": self.wildcard_bind,
            "auth_enabled": self.auth_enabled,
            "multiroom_enabled": self.multiroom_enabled,
            "auto_promoted_for_multiroom": self.auto_promoted_for_multiroom,
            "mode": self.mode,
            "summary": self.summary,
            "warning": self.warning,
        }


def get_security_posture_snapshot(
    *,
    settings_instance: object | None = None,
    security_config: SecurityConfig | None = None,
) -> SecurityPostureSnapshot:
    """Compute a machine-readable snapshot of runtime security posture."""
    app_settings = settings_instance or settings
    config = security_config or get_security_config()

    app_surface = str(getattr(app_settings, "app_surface", "desktop")).lower()
    api_host = str(getattr(app_settings, "api_host", "127.0.0.1") or "127.0.0.1")
    api_port = int(getattr(app_settings, "api_port", 0) or 0)
    multiroom_enabled = True
    wildcard_bind = is_wildcard_bind_host(api_host)
    lan_exposed = app_surface == "desktop" and is_lan_exposed_host(api_host)
    auto_promoted_for_multiroom = bool(
        lan_exposed
        and wildcard_bind
        and multiroom_enabled
        and "VIOLA_API_HOST" not in os.environ
        and not env.get("VIOLA_HOST")
    )

    if app_surface != "desktop":
        mode = "cloud"
        summary = "Cloud surface; desktop LAN posture rules do not apply."
        warning = None
    elif not lan_exposed:
        mode = "loopback"
        if config.auth_enabled:
            summary = "Loopback-only desktop API; authentication is enabled."
        else:
            summary = "Loopback-only desktop API; local relaxed auth is active."
        warning = None
    elif config.auth_enabled:
        mode = "lan_auth_required"
        if auto_promoted_for_multiroom:
            summary = "LAN-exposed desktop API for multiroom; authentication is required."
            warning = "api_host was auto-promoted to 0.0.0.0 for multiroom reachability."
        else:
            summary = "LAN-exposed desktop API; authentication is required."
            warning = "Desktop API is reachable from the LAN. Remote clients need the API key."
    else:
        mode = "lan_auth_disabled_blocked"
        summary = "LAN-exposed desktop API with authentication disabled is blocked at startup."
        warning = "Enable VIOLA_SECURITY_AUTH_ENABLED or bind VIOLA_API_HOST to loopback."

    return SecurityPostureSnapshot(
        app_surface=app_surface,
        api_host=api_host,
        api_port=api_port,
        lan_exposed=lan_exposed,
        wildcard_bind=wildcard_bind,
        auth_enabled=bool(config.auth_enabled),
        multiroom_enabled=multiroom_enabled,
        auto_promoted_for_multiroom=auto_promoted_for_multiroom,
        mode=mode,
        summary=summary,
        warning=warning,
    )


def check_websocket_origin(ws: WebSocket, client_host: str | None = None) -> bool:
    """Validate the WebSocket Origin header against the CORS allowlist.

    Returns True if the connection should be allowed, False otherwise.

    Rules:
      - No Origin header from localhost (Qt WebEngine, same-machine tools) -> allowed
      - No Origin header from a remote host -> rejected (prevents LAN bypass)
      - Origin matches an entry in ``settings.cors_origins`` -> allowed
      - Loopback-bound runtime with matching loopback origin + api_port -> allowed
      - Server binds 0.0.0.0 and origin port matches api_port -> allowed
        (LAN spoke trust model: if the page was served by us, allow WS)
      - Otherwise -> denied

    Args:
        ws: The incoming WebSocket connection.
        client_host: The connecting client's IP address (``ws.client.host``).
            Pass this explicitly so the guard can distinguish localhost from
            remote clients when the Origin header is absent.
    """
    origin = ws.headers.get("origin")
    if origin is None:
        # Non-browser clients (Qt WebEngine embedded on the same machine,
        # local CLI tools) don't send an Origin header.
        # Only allow no-Origin connections from localhost; remote clients
        # without an Origin header are rejected to prevent WS bypass attacks.
        is_local = client_host in ("127.0.0.1", "::1", "localhost")
        if not is_local:
            log.warning(
                "WebSocket no-Origin connection rejected from non-localhost: %s",
                client_host,
            )
            return False
        return True

    allowed = _allowed_websocket_origins()
    origin_stripped = origin.rstrip("/")
    # Exact match
    if origin_stripped in allowed:
        return True

    # Check scheme://host match (ignore trailing slashes)
    for allowed_origin in allowed:
        if allowed_origin == origin_stripped:
            return True

    try:
        parsed = urlparse(origin_stripped)
        if (
            parsed.port == settings.api_port
            and parsed.hostname in ("127.0.0.1", "::1", "localhost")
            and settings.api_host in ("127.0.0.1", "::1", "localhost")
        ):
            return True
    except Exception:
        log.debug("Origin URL parse failed for loopback WebSocket origin check")

    # When binding to all interfaces, accept origins from private IP ranges
    # (RFC 1918) on our API port.  The previous check accepted ANY hostname
    # on the matching port, enabling DNS-rebinding attacks.
    if settings.api_host in ("0.0.0.0", "::"):  # nosec B104
        try:
            import re

            parsed = urlparse(origin)
            if parsed.port == settings.api_port and parsed.hostname:
                hostname = parsed.hostname
                # Allow localhost and RFC 1918 private ranges only
                _private_pattern = re.compile(
                    r"^(?:localhost|127\.0\.0\.1|\[::1\]|::1"
                    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                    r"|192\.168\.\d{1,3}\.\d{1,3}"
                    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                    r")$"
                )
                if _private_pattern.match(hostname):
                    return True
        except Exception:
            log.debug("Origin URL parse failed for WebSocket origin check")

    log.warning("WebSocket origin rejected: %s", origin)
    return False


async def reject_websocket(ws: WebSocket, *, code: int, reason: str) -> None:
    """Reject a WebSocket connection while delivering the real close code/reason.

    Root cause (issue #1166, class found via #1061): sending ``websocket.close``
    BEFORE ``websocket.accept`` never reaches the wire as an actual close frame.
    Uvicorn's ASGI websockets implementation collapses ANY pre-accept close into
    a blanket HTTP 403 Forbidden with an empty body, discarding ``code`` and
    ``reason`` outright --
    ``uvicorn/protocols/websockets/websockets_sansio_impl.py``, the ``elif
    message_type == "websocket.close":`` branch taken while ``not
    self.handshake_complete`` -- it never even reads ``message["code"]`` or
    ``message["reason"]`` before calling ``self.conn.reject(HTTPStatus.FORBIDDEN,
    "")``. The browser's WebSocket therefore never fires ``onopen``, and its
    ``onclose`` reports the generic abnormal-closure code 1006 with an empty
    reason no matter what the app meant to say (auth expired, wrong tenant,
    rate-limited, plan required, ...) -- a real client-side failure becomes
    indistinguishable from a bare "forbidden".

    Per RFC 6455 there is no way to convey an app-chosen close code/reason
    without first completing the opening handshake -- a rejected HTTP upgrade
    has no room for WebSocket control-frame semantics. So this helper always
    accepts first, then immediately sends the real close frame: the socket
    technically reaches OPEN for a moment, but the client's ``onclose`` handler
    now receives the ``code``/``reason`` the app actually intended, which it can
    branch on (see uvicorn's post-handshake ``elif message_type ==
    "websocket.close" and not self.transport.is_closing():`` branch, which DOES
    read and forward ``code``/``reason`` via ``self.conn.send_close(code,
    reason)``).

    Every cloud WebSocket route that rejects a connection before doing its own
    ``ws.accept()`` MUST reject through this helper, never a bare
    ``ws.close(...)`` -- enforced by ``scripts/check_ws_pre_accept_close.py``
    (gate ``ws-pre-accept-close-delivers-code``).
    """
    try:
        await ws.accept()
    except Exception:  # noqa: BLE001, RUF100 - reject-path cleanup; a vanished client must never raise here
        log.debug("reject_websocket: accept() failed while rejecting (client likely gone)")
        return
    try:
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001, RUF100 - reject-path cleanup; a vanished client must never raise here
        log.debug("reject_websocket: close() failed while rejecting (client likely gone)")


@dataclass
class SecurityContext:
    manager: SecurityManager
    config: SecurityConfig
    auth_plugin: AuthenticationPlugin
    rate_limiter_plugin: RateLimiterPlugin
    rate_limiter_enabled: bool
    error_sanitizer: ErrorSanitizer
    resource_limits: ResourceLimits
    input_validator: InputValidator
    rate_limit: RateLimitDecorator
    limiter: object | None
    default_rate_limit: str | None


def configure_security(app: FastAPI) -> SecurityContext:
    """
    Configure the unified security system and return the resources required by
    the UI API layer.
    """
    security_mgr = get_security_manager()
    security_config = get_security_config()

    if settings.pytest_in_progress and env.get("VIOLA_SECURITY_TEST_AUTH", "0") != "1":
        security_config.auth_enabled = False
        security_config.rate_limiting_enabled = False

    auth_plugin = create_auth_plugin(security_config)
    rate_limiter_plugin = create_rate_limiter(security_config)

    security_mgr.register_plugin(auth_plugin)
    security_mgr.register_plugin(rate_limiter_plugin, dependencies=[])

    security_mgr.initialize(app)
    app.middleware("http")(security_middleware)
    app.state.rate_limiter_plugin = rate_limiter_plugin
    app.state.auth_plugin = auth_plugin  # Store for debug endpoints
    register_bootstrap_routes(app.router)

    error_sanitizer = get_error_sanitizer()
    resource_limits = get_resource_limits()
    input_validator = get_input_validator()

    log.info("🔐 Unified security system initialized")
    log.info(
        "   - Authentication: %s",
        "enabled" if auth_plugin.is_enabled() else "disabled",
    )
    log.info(
        "   - Rate Limiting: %s",
        "enabled" if rate_limiter_plugin.is_enabled() else "disabled",
    )
    log.info(
        "   - Error Sanitization: %s",
        "enabled" if security_config.error_sanitization_enabled else "disabled",
    )
    log.info(
        "   - Resource Limits: File=%sMB, Request=%sMB, WS=%s",
        security_config.max_file_size_mb,
        security_config.max_request_size_mb,
        security_config.max_websocket_connections,
    )

    rate_limit: RateLimitDecorator
    limiter: object | None = None
    default_rate_limit: str | None = None
    rate_limiter_enabled = rate_limiter_plugin.is_enabled()

    def _with_marker(factory: RateLimitDecorator) -> RateLimitDecorator:
        def decorator(limit: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
            original = factory(limit)

            def _apply(func: Callable[P, R]) -> Callable[P, R]:
                wrapped = original(func)
                # Mark the function as having rate limiting applied
                if hasattr(wrapped, "__dict__"):
                    wrapped.__dict__["_viola_rate_limit_applied"] = True
                return wrapped

            return _apply

        return decorator

    if rate_limiter_enabled:
        rate_limit = rate_limiter_plugin.limit
        limiter = getattr(rate_limiter_plugin, "limiter", None)
        default_rate_limit = rate_limiter_plugin.get_default_limit()
    else:
        rate_limit, limiter, default_rate_limit = _configure_legacy_rate_limiter(
            app, rate_limiter_plugin.config.rate_limiting_default
        )
        rate_limiter_enabled = limiter is not None

    app.state.rate_limiter_enabled = rate_limiter_enabled
    app.state.default_rate_limit = default_rate_limit
    if limiter is not None:
        app.state.limiter = limiter

    if default_rate_limit:
        rate_limit = _with_marker(rate_limit)

    return SecurityContext(
        manager=security_mgr,
        config=security_config,
        auth_plugin=auth_plugin,
        rate_limiter_plugin=rate_limiter_plugin,
        rate_limiter_enabled=rate_limiter_enabled,
        error_sanitizer=error_sanitizer,
        resource_limits=resource_limits,
        input_validator=input_validator,
        rate_limit=rate_limit,
        limiter=limiter,
        default_rate_limit=default_rate_limit,
    )


def _configure_legacy_rate_limiter(
    app: FastAPI, default_limit: str
) -> tuple[RateLimitDecorator, object | None, str | None]:
    """Fallback to SlowAPI-based limiter when unified rate limiting is disabled."""
    try:
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.util import get_remote_address
    except ImportError:
        log.debug("slowapi not installed (using unified security system)")

        def _noop(limit: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(func: Callable[P, R]) -> Callable[P, R]:
                return func

            return decorator

        return _noop, None, None

    limiter = Limiter(key_func=get_remote_address, default_limits=[default_limit])

    def rate_limit_exception_handler(request: Request, exc: Exception) -> Response:
        """Adapt slowapi exception handler to FastAPI signature."""
        # FastAPI accepts Exception; slowapi requires RateLimitExceeded.
        if isinstance(exc, RateLimitExceeded):
            return _rate_limit_exceeded_handler(request, exc)
        else:
            # Fallback for other exceptions
            return Response(
                content='{"detail": "Rate limit exceeded"}',
                status_code=429,
                media_type="application/json",
            )

    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)

    log.info("🚦 Legacy rate limiting enabled (fallback)")

    return cast(RateLimitDecorator, limiter.limit), limiter, default_limit


# =============================================================================
__all__ = [
    "SecurityContext",
    "check_websocket_origin",
    "configure_security",
    "reject_websocket",
]
