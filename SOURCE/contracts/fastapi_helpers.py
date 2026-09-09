"""
FastAPI helpers for ResponseEnvelope adoption.

This module provides utilities to wrap FastAPI route responses in the
canonical ResponseEnvelope format without requiring large refactors.

Usage:
    from contracts.fastapi_helpers import envelope_response, envelope_error

    @router.post("/register")
    async def register(...):
        user = create_user(...)
        return envelope_response(user.model_dump())

    # For errors:
    raise envelope_error(422, "invalid_input", "Password too short")
"""

from __future__ import annotations

import json
import typing
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.json_types import JsonObject, JsonValue, to_json_value
from fastapi import HTTPException, status

if TYPE_CHECKING:
    from starlette.responses import Response


class SafeJSONResponse(JSONResponse):
    """JSONResponse subclass that uses ``ensure_ascii=True``.

    On Windows the default console codepage is cp1252.  When Starlette's
    ``JSONResponse`` serialises with ``ensure_ascii=False`` (the default),
    non-ASCII characters like ``°`` (U+00B0) or ``•`` (U+2022) appear as
    raw UTF-8 bytes in the response body.  If *any* intermediary in the
    chain (logging, proxy, terminal, file cache) interprets those bytes as
    cp1252 instead of UTF-8, the client sees mojibake (``Â°`` instead of
    ``°``, ``â€¢`` instead of ``•``).

    Using ``ensure_ascii=True`` guarantees the JSON text contains only
    ASCII code-points; non-ASCII characters are escaped as ``\\uXXXX``.
    ``JSON.parse()`` on the client correctly decodes these escapes, so the
    end result is identical — but the transport is immune to codepage
    misinterpretation.

    Use as the ``default_response_class`` when creating ``FastAPI()`` apps::

        from contracts.fastapi_helpers import SafeJSONResponse
        app = FastAPI(default_response_class=SafeJSONResponse)
    """

    def render(self, content: typing.Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=True,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


def envelope_response(
    data: Any = None,
    status_code: int = status.HTTP_200_OK,
    response: Response | None = None,
) -> JSONResponse:
    """
    Wrap data in a success ResponseEnvelope and return as JSONResponse.

    Args:
        data: The payload to include in the envelope's data field.
              Pydantic models should be converted with .model_dump() first.
        status_code: HTTP status code (default 200)
        response: Optional FastAPI injected Response to merge headers/cookies from.
                  When provided, headers (including Set-Cookie) from the injected
                  response will be copied to the returned JSONResponse.

    Returns:
        JSONResponse with ResponseEnvelope structure

    Example:
        @router.post("/login")
        async def login(response: Response, ...):
            set_session_cookie(response, token)  # Sets cookie on injected response
            return envelope_response(data, response=response)  # Cookie is preserved
    """
    json_data = to_json_value(data) if data is not None else None
    envelope = success_response(json_data)
    json_response = SafeJSONResponse(content=envelope, status_code=status_code)

    # Merge headers from injected response if provided
    # This is necessary because returning a Response subclass bypasses FastAPI's
    # automatic header merging from the injected Response dependency
    if response is not None:
        for key, value in response.headers.raw:
            json_response.headers.append(key.decode(), value.decode())

    return json_response


def envelope_error(
    status_code: int,
    code: str,
    message: str,
    details: JsonObject | None = None,
) -> HTTPException:
    """
    Create an HTTPException with ResponseEnvelope-formatted detail.

    Args:
        status_code: HTTP status code
        code: Error code string (e.g., "email_exists", "invalid_credentials")
        message: Human-readable error message
        details: Optional additional error details

    Returns:
        HTTPException with detail in ResponseEnvelope format

    Usage:
        raise envelope_error(401, "invalid_credentials", "Invalid email or password")
    """
    envelope = failure_response(code, message, details=details)
    return HTTPException(status_code=status_code, detail=envelope)


def model_to_envelope(
    model: Any,
    status_code: int = status.HTTP_200_OK,
) -> JSONResponse:
    """
    Convert a Pydantic model to a ResponseEnvelope JSONResponse.

    Args:
        model: Pydantic model instance with .model_dump() method
        status_code: HTTP status code

    Returns:
        JSONResponse with ResponseEnvelope wrapping the model data
    """
    if hasattr(model, "model_dump"):
        data = model.model_dump(mode="json")
    elif hasattr(model, "dict"):
        data = model.dict()
    else:
        data = to_json_value(model)
    return envelope_response(data, status_code)


class EnvelopeExceptionHandler:
    """
    Exception handler that converts HTTPExceptions to ResponseEnvelope format.

    This can be registered as a FastAPI exception handler to ensure all
    error responses follow the ResponseEnvelope contract.

    Usage:
        from fastapi import FastAPI
        from contracts.fastapi_helpers import EnvelopeExceptionHandler

        app = FastAPI()
        EnvelopeExceptionHandler.register(app)
    """

    @staticmethod
    def register(app: Any) -> None:
        """Register the envelope exception handler with a FastAPI app."""
        from fastapi.exceptions import RequestValidationError
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from fastapi import Request

        async def _http_exception_to_envelope(status_code: int, detail: Any) -> SafeJSONResponse:
            # If detail is already an envelope, use it directly
            if isinstance(detail, dict) and "ok" in detail and "data" in detail:
                return SafeJSONResponse(content=detail, status_code=status_code)

            # Convert legacy detail format to envelope
            if isinstance(detail, dict) and "error" in detail:
                code = str(detail.get("error", "error"))
                message = str(detail.get("message", code))
                details = detail.get("details")
                envelope = failure_response(
                    code,
                    message,
                    details=details if isinstance(details, dict) else None,
                )
            elif isinstance(detail, str):
                envelope = failure_response("error", detail)
            else:
                envelope = failure_response("error", str(detail))

            return SafeJSONResponse(content=envelope, status_code=status_code)

        @app.exception_handler(HTTPException)
        async def http_exception_handler(request: Request, exc: HTTPException) -> SafeJSONResponse:
            return await _http_exception_to_envelope(exc.status_code, exc.detail)

        @app.exception_handler(StarletteHTTPException)
        async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException) -> SafeJSONResponse:
            return await _http_exception_to_envelope(exc.status_code, exc.detail)

        @app.exception_handler(RequestValidationError)
        async def validation_exception_handler(request: Request, exc: RequestValidationError) -> SafeJSONResponse:
            errors = exc.errors()
            details: JsonObject = {"validation_errors": to_json_value(errors)}
            envelope = failure_response(
                "validation_error",
                "Request validation failed",
                details=details,
            )
            return SafeJSONResponse(content=envelope, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
