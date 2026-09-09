from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse, Response

from contracts.api_response import (
    ResponseContractError,
    ensure_envelope,
    failure_response,
    reconcile_status,
    status_for_envelope,
    success_response,
)
from contracts.fastapi_helpers import SafeJSONResponse
from core.logging_config import get_logger
from fastapi import HTTPException, Request

log = get_logger(__name__)
from ui.api.context import ApiContext

AsyncCallable = Callable[[], Awaitable[Any]]


@dataclass
class RouteToolbox:
    """Utility helpers shared across modular route registrations."""

    context: ApiContext

    async def record_and_call(
        self,
        call: AsyncCallable,
        *,
        route: str,
        method: str,
    ) -> Any:
        http_requests_total = self.context.http_requests_total
        try:
            response = await call()
            if isinstance(response, JSONResponse):
                status_code = response.status_code
                http_requests_total.inc(route=route, code=str(status_code), method=method)
                # For JSONResponse, validate the content
                # JSONResponse stores content internally - try to validate it
                envelope = None
                try:
                    # Try to access the body for validation
                    # JSONResponse serializes content when body() is called
                    try:
                        body_bytes = response.body
                        if body_bytes:
                            # response.body may return memoryview or bytes
                            if isinstance(body_bytes, memoryview):
                                body_bytes = body_bytes.tobytes()
                            payload = json.loads(body_bytes.decode("utf-8"))
                            # Check if payload already matches envelope contract
                            try:
                                envelope = ensure_envelope(payload)
                                # A failure envelope must never ride on a 2xx status,
                                # even when the handler set the status explicitly on a
                                # JSONResponse. Reconcile before returning so the same
                                # false-success invariant holds at this laundering path.
                                reconciled = reconcile_status(status_code, envelope)
                                if reconciled != status_code:
                                    log.warning(
                                        "Failure envelope emitted with 2xx status %d in %s %s; coercing to %d",
                                        status_code,
                                        method,
                                        route,
                                        reconciled,
                                    )
                                    status_code = reconciled
                                # Only update if envelope differs from payload OR the
                                # status was reconciled away from the handler's 2xx.
                                if envelope != payload or reconciled != response.status_code:
                                    response = SafeJSONResponse(status_code=status_code, content=envelope)
                            except ResponseContractError:
                                # Payload doesn't match contract - wrap it
                                envelope = success_response(payload)
                                response = SafeJSONResponse(status_code=status_code, content=envelope)
                        else:
                            # Empty body - this is an error
                            log.warning("Empty body in JSONResponse for %s %s", method, route)
                            envelope = failure_response(
                                "empty_response",
                                "Handler returned an empty response body.",
                                details={"route": route},
                            )
                            response = SafeJSONResponse(status_code=500, content=envelope)
                    except (AttributeError, RuntimeError, TypeError):
                        # Body not accessible yet - JSONResponse will serialize correctly
                        # Skip validation and return as-is
                        log.debug(
                            "Could not access JSONResponse body for %s %s - trusting response",
                            method,
                            route,
                        )
                        return response
                    except json.JSONDecodeError:
                        log.exception("Invalid JSON payload in %s %s response", method, route)
                        envelope = failure_response(
                            "invalid_json",
                            "Handler returned malformed JSON.",
                        )
                        response = SafeJSONResponse(status_code=500, content=envelope)
                except Exception:
                    log.exception(
                        "Unexpected error validating JSONResponse in %s %s",
                        method,
                        route,
                    )
                    # Don't fail the request - trust the original response
                    # The middleware will validate it later
                    return response
                return response
            if isinstance(response, Response):
                status_code = getattr(response, "status_code", 200)
                http_requests_total.inc(route=route, code=str(status_code), method=method)
                return response
            try:
                envelope = ensure_envelope(response)
            except ResponseContractError:
                envelope = success_response(response)
            # A failure envelope (ok is False) must never be laundered into a 2xx
            # status: derive the HTTP status from the envelope itself. This is the
            # single seam that kills the false-success class (#2736/#2826/#3013) —
            # a handler that returns a bare {"ok": False, ...} dict can no longer
            # be reported as HTTP 200 by this layer.
            status_code = status_for_envelope(envelope)
            http_requests_total.inc(route=route, code=str(status_code), method=method)
            # Return JSONResponse to ensure body is available for middleware
            return SafeJSONResponse(status_code=status_code, content=envelope)
        except HTTPException:
            raise
        except Exception:
            http_requests_total.inc(route=route, code="500", method=method)
            log.exception("Unhandled error in %s %s", method, route)
            return SafeJSONResponse(
                status_code=500,
                content=failure_response(
                    "internal_error",
                    "An unexpected server error occurred.",
                    details={"route": route, "method": method},
                ),
            )


def request_client_ip(request: Request) -> str | None:
    """Expose request client IP as dependency for allowlist checks."""
    return request.client.host if request.client else None


__all__ = ["RouteToolbox", "request_client_ip"]
