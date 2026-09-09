"""
Unified Route Error Handler
===========================

Standardized error handling for all API routes.

This module provides a consistent error handling pattern to replace the
3 different error handling styles currently used across route files.

Usage:
    from ui.api.routes.error_handler import handle_route_error, route_handler

    @router.post("/v1/action")
    async def my_action(body: MyRequest) -> ResponseEnvelope:
        try:
            result = await do_something(body)
            return success_response(result)
        except Exception as exc:
            logger.debug("Operation failed: %s", exc)
            return handle_route_error(exc, "my_action")

    # Or use the decorator:
    @router.post("/v1/action")
    @route_handler("my_action")
    async def my_action(body: MyRequest) -> ResponseEnvelope:
        result = await do_something(body)
        return success_response(result)
"""

from __future__ import annotations

import functools
import importlib
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, cast

from contracts.api_response import ResponseEnvelope, failure_response
from contracts.fastapi_helpers import SafeJSONResponse
from core.logging_config import get_logger

try:
    from utils.api_helpers import error_response
except ImportError:
    # Fallback if api_helpers not available
    def error_response(
        error_code: str,
        status_code: int = 500,
        message: str | None = None,
    ) -> SafeJSONResponse:
        """Fallback error response."""
        return SafeJSONResponse(
            status_code=status_code,
            content=failure_response(
                error_code,
                message or "Try again in a moment",
            ),
        )


logger = get_logger(__name__)


def _optional_exception_type(module_name: str, exception_name: str) -> type[Exception] | None:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    exc_type = getattr(module, exception_name, None)
    if isinstance(exc_type, type) and issubclass(exc_type, Exception):
        return exc_type
    return None


_QUEUE_SERVICE_ERROR = _optional_exception_type("ui.api.services.queue_service", "QueueServiceError")
_INVALID_OPERATION = _optional_exception_type("music.exceptions", "InvalidOperation")


def handle_route_error(
    exc: Exception,
    operation: str,
    log_level: int = logging.ERROR,
) -> ResponseEnvelope | SafeJSONResponse:
    """
    Handle an exception in a route and return appropriate response.

    This provides consistent error handling across all routes:
    1. Known service errors -> proper status code and payload
    2. Validation errors -> 400 with message
    3. Operation errors -> 409 with message
    4. Unknown errors -> 500 with logged details

    Args:
        exc: The exception that occurred
        operation: Name of the operation (for logging)
        log_level: Logging level for unknown errors

    Returns:
        ResponseEnvelope or JSONResponse with error details
    """
    # Known service errors with their own status codes
    if _QUEUE_SERVICE_ERROR is not None and isinstance(exc, _QUEUE_SERVICE_ERROR):
        status_code_obj = getattr(exc, "status_code", 500)
        status_code = int(status_code_obj) if isinstance(status_code_obj, int) else 500
        payload_func = getattr(exc, "to_payload", None)
        payload = (
            payload_func()
            if callable(payload_func)
            else failure_response(
                "queue_service_error",
                "Try again in a moment",
            )
        )
        return SafeJSONResponse(
            status_code=status_code,
            content=payload,
        )

    # Invalid operation (conflict)
    # SECURITY: Log full exception server-side, return generic message to client
    if _INVALID_OPERATION is not None and isinstance(exc, _INVALID_OPERATION):
        logger.warning("%s invalid: %s", operation, exc)
        return error_response(
            "operation_not_allowed",
            status_code=409,
            message="This action is not available right now.",
        )

    # Value errors (bad input)
    # SECURITY: Don't expose validation details that might contain secrets
    if isinstance(exc, ValueError):
        logger.warning("%s validation error: %s", operation, exc)
        return error_response("invalid_input", status_code=400, message="Invalid input")

    # Key errors (missing required field)
    # SECURITY: Don't expose field names that might leak internal structure
    if isinstance(exc, KeyError):
        logger.warning("%s missing field: %s", operation, exc)
        return error_response("missing_field", status_code=400, message="Missing required field")

    # Type errors (wrong type)
    # SECURITY: Don't expose type details
    if isinstance(exc, TypeError):
        logger.warning("%s type error: %s", operation, exc)
        return error_response("invalid_type", status_code=400, message="Invalid input type")

    # All other errors
    # SECURITY: Never expose exception details to client - may contain secrets
    if log_level == logging.ERROR:
        logger.exception("%s failed: %s", operation, exc)
    else:
        logger.log(log_level, "%s failed: %s", operation, exc)
    return error_response(
        "request_failed",
        status_code=500,
        message="Try again in a moment",
    )


P = ParamSpec("P")


def route_handler(
    operation: str, log_level: int = logging.ERROR
) -> Callable[[Callable[P, object]], Callable[P, object]]:
    """
    Decorator to wrap a route handler with standardized error handling.

    Usage:
        @router.post("/v1/action")
        @route_handler("my_action")
        async def my_action(body: MyRequest) -> ResponseEnvelope:
            result = await do_something(body)
            return success_response(result)
            # Exceptions are automatically caught and handled

    Args:
        operation: Name of the operation (for logging)
        log_level: Logging level for unknown errors

    Returns:
        Decorated function with error handling
    """

    def decorator(func: Callable[P, object]) -> Callable[P, object]:
        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
            async_func = cast(Callable[P, Awaitable[object]], func)
            try:
                return await async_func(*args, **kwargs)
            except Exception as exc:
                logger.debug("Operation failed: %s", exc)
                return handle_route_error(exc, operation, log_level)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                logger.debug("Operation failed: %s", exc)
                return handle_route_error(exc, operation, log_level)

        # Return appropriate wrapper based on function type
        import asyncio

        if asyncio.iscoroutinefunction(func):
            return cast(Callable[P, object], async_wrapper)
        return sync_wrapper

    return decorator
