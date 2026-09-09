"""
Security Middleware

Unified middleware that applies all security plugins.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from fastapi.responses import JSONResponse

from core.logging_config import get_logger
from fastapi import HTTPException, Request, Response

from .config import get_security_config
from .errors import get_error_sanitizer, sanitize_error_response
from .limits import get_resource_limits

log = get_logger(__name__)

_RateLimitExceededType: type[Exception] | None

try:  # pragma: no cover - optional dependency guard
    from slowapi.errors import RateLimitExceeded as _RateLimitExceededType
except ImportError:  # pragma: no cover - slowapi optional
    _RateLimitExceededType = None

RateLimitExceeded = _RateLimitExceededType


async def security_middleware(request: Request, call_next: Callable) -> Response:
    """
    Unified security middleware.

    Applies:
    - Resource limits
    - Error sanitization
    - Request size validation
    """
    config = get_security_config()
    limits = get_resource_limits()
    sanitizer = get_error_sanitizer()

    try:
        # Validate request size
        limits.validate_request_size(request)

        response: Response | None = None

        limiter = getattr(request.app.state, "limiter", None)
        limiter_enabled = getattr(request.app.state, "rate_limiter_enabled", False)
        default_limit = getattr(request.app.state, "default_rate_limit", None)

        if limiter_enabled and limiter is not None:
            route = request.scope.get("route")
            endpoint = getattr(route, "endpoint", None) if route else None
            explicit_limiter = bool(endpoint and getattr(endpoint, "_viola_rate_limit_applied", False))
            route_limit = getattr(route, "_rate_limit_value", default_limit) if route else default_limit

            # Exempt consent endpoints from rate limiting (PRD requirement: users should not
            # see rate limits during normal linking flows, even with repeated button clicks)
            path = request.url.path
            is_consent_endpoint = path.startswith("/v1/consent/")

            if is_consent_endpoint:
                # Skip rate limiting for consent endpoints
                log.debug(
                    "Skipping rate limit for consent endpoint: path=%s method=%s",
                    path,
                    request.method,
                )
            elif route_limit and not explicit_limiter:
                try:
                    limiter._check_request_limit(request, endpoint, in_middleware=True)
                except Exception as exc:
                    if RateLimitExceeded is not None and isinstance(exc, RateLimitExceeded):
                        # Diagnostic logging for rate limit violations
                        path = request.url.path
                        method = request.method
                        # Get bucket key (IP address) for logging
                        try:
                            key_func = getattr(limiter, "key_func", None)
                            if key_func:
                                bucket_key = key_func(request)
                            else:
                                bucket_key = "unknown"
                        except Exception as e:
                            log.exception("Failed to get rate limit bucket key: %s", e)
                            bucket_key = "unknown"

                        log.warning(
                            "Rate limit exceeded: path=%s method=%s bucket_key=%s limit=%s",
                            path,
                            method,
                            bucket_key,
                            route_limit,
                        )

                        # Record as security abuse signal for audit trail
                        is_auth_path = path.startswith("/v1/auth/") or path.startswith("/auth/")
                        try:
                            from admin.instrumentation import record_abuse_signal

                            record_abuse_signal(
                                "slowapi_rate_limit",
                                severity="critical" if is_auth_path else "warning",
                                details={
                                    "ip": bucket_key,
                                    "path": path,
                                    "method": method,
                                    "limit": str(route_limit),
                                },
                            )
                        except (ImportError, RuntimeError, TypeError, ValueError) as exc:
                            log.debug("Failed to record slowapi abuse signal: %s", exc)

                        handler = request.app.exception_handlers.get(RateLimitExceeded)
                        if handler is None:
                            raise
                        result = handler(request, exc)
                        response = await result if inspect.isawaitable(result) else result
                    else:
                        raise

        if response is None:
            response = await call_next(request)

        # Process request
        if response is not None and config.security_headers_enabled:
            headers = config.security_headers
            if headers is not None:
                path = request.url.path
                is_frameable_path = path.startswith("/static/webviews/")
                for header, value in headers.items():
                    if is_frameable_path:
                        # Allow framing for embedded webview content (YouTube iframe)
                        if header.lower() == "x-frame-options":
                            response.headers[header] = "SAMEORIGIN"
                            continue
                        if header.lower() == "content-security-policy" and "frame-ancestors" in value:
                            value = value.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
                    response.headers[header] = value

        return response or JSONResponse(status_code=500, content={"ok": False, "error": "Internal error"})

    except HTTPException as e:
        # HTTP exceptions (e.g., 413, 401) - sanitize error
        sanitized_error = sanitizer.sanitize_dict(
            {
                "ok": False,
                "error": e.detail,
            }
        )
        return JSONResponse(
            status_code=e.status_code,
            content=sanitized_error,
        )

    except Exception as e:
        if RateLimitExceeded is not None and isinstance(e, RateLimitExceeded):
            raise
        # Unexpected errors - sanitize
        log.exception("Security middleware error: %s", e)
        sanitized = sanitize_error_response(e, {"endpoint": request.url.path})

        return JSONResponse(
            status_code=500,
            content=sanitized,
        )
