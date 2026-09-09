"""
CSRF protection for cookie-authenticated cloud requests.

This middleware implements the double-submit-cookie pattern for the hosted
browser surface:

- The server issues a readable ``viola_csrf`` cookie on GET requests and
  whenever a session cookie is issued (login / magic-link / oauth complete).
- Browser clients read that cookie via ``document.cookie``.
- State-changing requests must echo the same value in ``X-CSRF-Token``.
- Cookie-authenticated state-changing requests with ``Origin`` or ``Referer``
  must come from the configured Viola origins.

Enforcement is scoped to requests that present a ``viola_session`` cookie.
Pre-session flows (``/auth/login``, ``/auth/register``, magic-link request,
password reset) cannot be CSRF-forged because there is no ambient session to
abuse, so they are exempt by design. Signature-verified webhooks are also
exempt via the explicit prefix list.

Two browser clients read the cookie and forward ``X-CSRF-Token``: the marketing
site's wrapper (``ViolaWebsite/js/auth.js``) and the cloud SPA's GoTrue client
(``ui/react-app/src/auth/authClient.js`` via ``ui/react-app/src/lib/csrf.js``).
Any other caller that uses ``fetch()`` directly must add the header itself.

Note that "exempt because there is no ambient session" is a property of the
REQUEST, not of the endpoint: a returning visitor signing in again still has a
stale ``viola_session`` cookie in the jar, so their sign-in POST is enforced.
That is deliberate — ``/auth/v1/token`` accepts the session cookie as the sole
credential on the empty-body refresh grant (``auth/gotrue_proxy.py``), so
ambient-cookie authority is genuinely reachable there and the endpoint must not
be exempted. The client attaches the header instead (#362 / #3547).
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response

from auth.middleware import SESSION_COOKIE_NAME, _extract_bearer_token, _is_localhost_http
from config.settings import get_settings
from fastapi import HTTPException

CSRF_COOKIE_NAME = "viola_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_ALLOWED_ORIGINS = frozenset(
    {
        "https://api.useviola.com",
        "https://useviola.com",
        "https://www.useviola.com",
    }
)
# Webhook and infrastructure paths that authenticate via signature or have no
# user session to forge. ``/billing/webhook/`` covers Stripe and BTCPay —
# ``billing_router`` is mounted under ``/billing`` in cloud_app.py, so the
# concrete paths are ``/billing/webhook/stripe`` and ``/billing/webhook/btcpay``.
DEFAULT_EXEMPT_PREFIXES = (
    "/webhooks/",
    "/webhook/",
    "/billing/webhook/",
    "/telegram/webhook",
    "/api/public/",
    "/health",
    "/metrics",
    # SEC-027 (2026-06-09 sweep): no "/_" namespace wildcard. A wildcard prefix
    # exempts every future route registered under it; list explicit prefixes only.
)
DEFAULT_OAUTH_EXEMPT_PREFIX = "/auth/oauth/"
DEFAULT_OAUTH_EXEMPT_SUFFIXES = (
    "/callback",
    "/complete",
    "/start",
)


def _get_app_surface() -> str:
    settings = get_settings()
    return str(getattr(settings, "app_surface", "desktop")).lower()


def _normalize_origin(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    if port and not default_port:
        host = "%s:%s" % (host, port)
    return "%s://%s" % (scheme, host)


def _configured_allowed_origins() -> frozenset[str]:
    settings = get_settings()
    origins = set(DEFAULT_ALLOWED_ORIGINS)

    configured = getattr(settings, "cloud_cors_origins", None)
    if isinstance(configured, str):
        configured_values = [item.strip() for item in configured.split(",")]
    elif configured:
        configured_values = [str(item).strip() for item in configured]
    else:
        configured_values = []

    for origin in configured_values:
        normalized = _normalize_origin(origin)
        if normalized:
            origins.add(normalized)

    cloud_url = _normalize_origin(str(getattr(settings, "cloud_url", "") or ""))
    if cloud_url:
        origins.add(cloud_url)

    return frozenset(origins)


def _request_origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        normalized = _normalize_origin(origin)
        return bool(normalized and normalized in _configured_allowed_origins())

    referer = request.headers.get("referer")
    if referer:
        normalized = _normalize_origin(referer)
        return bool(normalized and normalized in _configured_allowed_origins())

    # Non-browser clients and same-origin form submissions may omit both.
    # They still need the double-submit token below when using cookies.
    return True


def _has_bearer_authorization(request: Request) -> bool:
    return _extract_bearer_token(request.headers.get("Authorization")) is not None


def generate_csrf_token() -> str:
    """Generate a random CSRF token suitable for the double-submit pattern."""
    return secrets.token_urlsafe(32)


def set_csrf_cookie(
    response: Response,
    value: str,
    max_age_days: int = 30,
    secure: bool | None = None,
    domain: str | None = None,
) -> None:
    """
    Set the CSRF cookie with the same transport flags as the session cookie.

    Unlike the session cookie, this cookie must be readable by browser
    JavaScript so the client can copy it into ``X-CSRF-Token``.
    """
    if secure is None:
        secure = not _is_localhost_http()

    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=value,
        max_age=max_age_days * 24 * 60 * 60,
        httponly=False,
        secure=secure,
        samesite="lax",
        domain=domain,
        path="/",
    )


def clear_csrf_cookie(response: Response, domain: str | None = None) -> None:
    """Clear the CSRF cookie alongside the session cookie on logout/GDPR delete."""
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        domain=domain,
        path="/",
    )


def _issues_session_cookie(headers: list[tuple[bytes, bytes]]) -> bool:
    """True when an outgoing ASGI header list plants a ``viola_session`` cookie.

    Matches the cookie NAME only, and only a set (never a delete): a logout
    response clears the session, so it needs no CSRF cookie minted beside it.
    """
    prefix = SESSION_COOKIE_NAME.encode("ascii") + b"="
    for name, value in headers:
        if name.lower() != b"set-cookie" or not value.startswith(prefix):
            continue
        # ``delete_cookie`` emits an empty value plus an expiry in the past.
        # Starlette quotes that empty value, so the raw bytes are ``""`` rather
        # than the empty string -- strip the quoting before testing emptiness,
        # or a logout would read as an issuance.
        cookie_value = value[len(prefix) :].split(b";", 1)[0].strip().strip(b'"')
        if cookie_value:
            return True
    return False


def _csrf_set_cookie_header(token: str, request: Request) -> bytes:
    """Build the raw ``Set-Cookie`` bytes for the readable double-submit cookie.

    Deliberately NOT ``httponly``: the browser client has to read this value to
    echo it back in ``X-CSRF-Token``. Transport flags mirror the session
    cookie's (see ``set_csrf_cookie``).
    """
    from auth.middleware import _is_localhost_http

    secure = b"" if _is_localhost_http(request) else b"; Secure"
    return (
        CSRF_COOKIE_NAME.encode("ascii")
        + b"="
        + token.encode("ascii")
        + b"; Path=/; Max-Age="
        + str(30 * 24 * 60 * 60).encode("ascii")
        + b"; SameSite=Lax"
        + secure
    )


class CSRFMiddleware:
    """Enforce CSRF protection for cookie-authenticated cloud requests.

    Pure-ASGI (not BaseHTTPMiddleware) — see ``auth/middleware.py`` for the
    asyncpg cross-loop reasoning.

    Default exemptions:
    - ``/auth/oauth/*/callback``
    - ``/auth/oauth/*/complete``
    - ``/auth/oauth/*/start``
    - ``/webhooks/*``
    - ``/webhook/*`` for billing webhooks such as Stripe and BTCPay
    - ``/telegram/webhook`` for shared bot callbacks
    - ``/health*`` and ``/metrics*``
    """

    def __init__(
        self,
        app,
        *,
        exempt_prefixes: Iterable[str] | None = None,
        oauth_exempt_prefix: str = DEFAULT_OAUTH_EXEMPT_PREFIX,
        oauth_exempt_suffixes: Iterable[str] | None = None,
    ) -> None:
        self.app = app
        self._exempt_prefixes = tuple(exempt_prefixes or DEFAULT_EXEMPT_PREFIXES)
        self._oauth_exempt_prefix = oauth_exempt_prefix
        self._oauth_exempt_suffixes = tuple(oauth_exempt_suffixes or DEFAULT_OAUTH_EXEMPT_SUFFIXES)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if _get_app_surface() != "cloud":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = scope.get("path", "") or ""
        method = (scope.get("method", "") or "").upper()

        if method in STATE_CHANGING_METHODS and not self._is_exempt_path(path):
            if request.cookies.get(SESSION_COOKIE_NAME) and not _has_bearer_authorization(request):
                if not _request_origin_allowed(request):
                    response = JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF origin validation failed"},
                    )
                    await response(scope, receive, send)
                    return
                cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
                header_token = request.headers.get(CSRF_HEADER_NAME)
                if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
                    response = JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF token validation failed"},
                    )
                    await response(scope, receive, send)
                    return

        # Mint the readable double-submit cookie when the caller doesn't have
        # one yet. Two occasions, and the second one is load-bearing:
        #
        #   1. Any GET — the ordinary page load that arms a browser client.
        #   2. Any response that ISSUES a ``viola_session`` cookie.
        #
        # (2) exists because the enforcement predicate above keys off the
        # session cookie: the instant a response plants ``viola_session``, every
        # later cookie-authenticated state-changing request from that browser
        # MUST present a matching CSRF pair. A session cookie issued without a
        # CSRF cookie beside it is therefore a latent, unrecoverable 403 —
        # the client cannot mint the token, and the failing request is refused
        # before any handler can hand one back. Minting both together makes
        # "a session cookie never exists without a readable CSRF cookie" true
        # by construction instead of by luck of relative cookie lifetimes
        # (#362 / #3547). The module docstring has always claimed this
        # behaviour; until now only (1) implemented it.
        has_csrf_cookie = bool(request.cookies.get(CSRF_COOKIE_NAME))
        new_csrf = generate_csrf_token() if not has_csrf_cookie else None

        async def send_wrapper(message):
            if new_csrf is not None and message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 0)
                headers = list(message.get("headers", []))
                if 200 <= status_code < 400 and (method == "GET" or _issues_session_cookie(headers)):
                    headers.append((b"set-cookie", _csrf_set_cookie_header(new_csrf, request)))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _is_exempt_path(self, path: str) -> bool:
        if any(path.startswith(prefix) for prefix in self._exempt_prefixes):
            return True
        return path.startswith(self._oauth_exempt_prefix) and path.endswith(self._oauth_exempt_suffixes)


async def csrf_required(request: Request) -> None:
    """
    FastAPI dependency that enforces the double-submit CSRF check.

    Enforces the cookie-and-header pairing only when the caller is
    cookie-authenticated. Bearer-token clients (Authorization header, no
    session cookie) and unauthenticated requests are exempt — the route's
    own auth dependency decides whether to admit them.

    This is intentionally independent of ``CSRFMiddleware`` so it can be
    attached to individual routes (for example, billing state changes) even
    when the middleware isn't active, e.g. on desktop builds where
    ``app_surface != "cloud"``.

    Raises ``HTTPException(403)`` with a structured ``csrf_mismatch`` detail
    on failure.
    """
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    # If there's no cookie-session OR the caller is authenticating via
    # Authorization: Bearer, there is nothing for CSRF to defend against —
    # Bearer credentials are never auto-attached by browsers.
    if not session_cookie or _has_bearer_authorization(request):
        return

    if not _request_origin_allowed(request):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "csrf_origin_mismatch",
                "message": "CSRF origin not allowed",
            },
        )

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "csrf_mismatch",
                "message": "CSRF token missing or invalid",
            },
        )
