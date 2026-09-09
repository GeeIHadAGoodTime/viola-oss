"""
Request Tracing Enhancement

Adds structured logging with request IDs for end-to-end traceability.

Features:
- Unique request IDs
- Context propagation across async boundaries
- FastAPI middleware integration
- JSON structured logging
- Correlation across components

ROI: 80/100 - Massive debugging improvement, moderate implementation
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, Protocol, cast

from core.logging_config import get_logger

logger = get_logger(__name__)


# Context variables for request tracing
request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
user_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
request_start_time_ctx: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "request_start_time", default=None
)


class RequestTracingEnhancer:
    """
    Adds request tracing and structured logging.

    Usage:
        # In FastAPI app
        from utils.enhancements import RequestTracingEnhancer

        enhancer = RequestTracingEnhancer()
        app.add_middleware(enhancer.middleware_class)

        # In any code
        from utils.enhancements.request_tracing import traced_logger
        traced_logger.info("Processing request", extra_field="value")
    """

    def __init__(
        self,
        generate_id: Callable[[], str] | None = None,
        header_name: str = "X-Request-ID",
    ):
        """
        Initialize request tracing enhancer.

        Args:
            generate_id: Function to generate request IDs
            header_name: HTTP header name for request ID
        """
        self.generate_id = generate_id or (lambda: str(uuid.uuid4())[:8])
        self.header_name = header_name
        self._configure_logger()

    def _configure_logger(self) -> None:
        """Configure loguru with request context."""

        def format_record(record: dict[str, object]) -> None:
            """Add request context to log records."""
            req_id = request_id_ctx.get()
            user_id = user_id_ctx.get()

            extra_obj = record.get("extra")
            if not isinstance(extra_obj, dict):
                extra_obj = {}
                record["extra"] = extra_obj

            if req_id:
                extra_obj["request_id"] = req_id
            if user_id:
                extra_obj["user_id"] = user_id

        # Add patcher to include context in logs
        try:
            logger.configure(patcher=format_record)
            logger.info("📊 Structured logging configured with request tracing")
        except Exception as e:
            logger.warning("Failed to configure structured logging: %s", e)

    @property
    def middleware_class(self) -> type | None:
        """Get FastAPI middleware class."""
        try:
            from starlette.middleware.base import (
                BaseHTTPMiddleware,
                RequestResponseEndpoint,
            )
            from starlette.requests import Request
            from starlette.responses import Response
        except ImportError:
            logger.error("FastAPI/Starlette not available for middleware")
            return None

        enhancer = self

        class RequestTracingMiddleware(BaseHTTPMiddleware):
            async def dispatch(
                self,
                request: Request,
                call_next: RequestResponseEndpoint,
            ) -> Response:
                # Generate or extract request ID
                req_id = request.headers.get(enhancer.header_name)
                if not req_id:
                    req_id = enhancer.generate_id()

                # Set context
                request_id_ctx.set(req_id)
                request_start_time_ctx.set(time.time())

                # Extract user ID if available
                user_id = request.headers.get("X-User-ID")
                if user_id:
                    user_id_ctx.set(user_id)

                # Log request start
                logger.info(
                    "→ %s %s",
                    request.method,
                    request.url.path,
                    extra={
                        "request_id": req_id,
                        "method": request.method,
                        "path": str(request.url.path),
                        "query": str(request.url.query) if request.url.query else None,
                        "client": request.client.host if request.client else None,
                    },
                )

                # Process request
                try:
                    response = await call_next(request)

                    # Calculate duration
                    start_time = request_start_time_ctx.get()
                    duration_ms = int((time.time() - start_time) * 1000) if start_time is not None else None

                    # Log request end
                    logger.info(
                        "← %s %s %s",
                        request.method,
                        request.url.path,
                        response.status_code,
                        extra={
                            "request_id": req_id,
                            "status": response.status_code,
                            "duration_ms": duration_ms,
                        },
                    )

                    # Add request ID to response headers
                    response.headers[enhancer.header_name] = req_id
                    if duration_ms:
                        response.headers["X-Response-Time"] = f"{duration_ms}ms"

                    return response

                except Exception as e:
                    start_time = request_start_time_ctx.get()
                    duration_ms = int((time.time() - start_time) * 1000) if start_time is not None else None

                    logger.error(
                        "✗ %s %s ERROR",
                        request.method,
                        request.url.path,
                        extra={
                            "request_id": req_id,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "duration_ms": duration_ms,
                        },
                    )
                    raise

        return RequestTracingMiddleware

    @staticmethod
    def get_current_request_id() -> str | None:
        """Get current request ID from context."""
        return request_id_ctx.get()

    @staticmethod
    def set_request_id(req_id: str) -> None:
        """Set request ID in context."""
        request_id_ctx.set(req_id)


P = ParamSpec("P")


def traced(func: Callable[P, object]) -> Callable[P, object]:
    """
    Decorator to add tracing to functions.

    Usage:
        @traced
        async def process_command(text: str):
            ...
    """

    @wraps(func)
    async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
        req_id = request_id_ctx.get() or "N/A"
        func_name = f"{func.__module__}.{func.__name__}"

        logger.debug("→ %s", func_name, extra={"request_id": req_id, "function": func_name})

        start_time = time.time()
        try:
            awaited = await cast(Callable[P, Awaitable[object]], func)(*args, **kwargs)
            duration_ms = int((time.time() - start_time) * 1000)

            logger.debug(
                "← %s (%sms)",
                func_name,
                duration_ms,
                extra={
                    "request_id": req_id,
                    "function": func_name,
                    "duration_ms": duration_ms,
                },
            )
            return awaited
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(
                "✗ %s ERROR (%sms)",
                func_name,
                duration_ms,
                extra={
                    "request_id": req_id,
                    "function": func_name,
                    "duration_ms": duration_ms,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            raise

    @wraps(func)
    def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
        req_id = request_id_ctx.get() or "N/A"
        func_name = f"{func.__module__}.{func.__name__}"

        logger.debug("→ %s", func_name, extra={"request_id": req_id, "function": func_name})

        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            duration_ms = int((time.time() - start_time) * 1000)

            logger.debug(
                "← %s (%sms)",
                func_name,
                duration_ms,
                extra={
                    "request_id": req_id,
                    "function": func_name,
                    "duration_ms": duration_ms,
                },
            )
            return result
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(
                "✗ %s ERROR (%sms)",
                func_name,
                duration_ms,
                extra={
                    "request_id": req_id,
                    "function": func_name,
                    "duration_ms": duration_ms,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            raise

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


# Convenience: traced logger that automatically includes context
class TracedLogger:
    """Logger that automatically includes request context."""

    def _log(
        self,
        level: str,
        message: str,
        *,
        extra: dict[str, object] | None = None,
        **fields: object,
    ) -> None:
        """Log with automatic context injection."""
        merged: dict[str, object] = {}
        if extra is not None:
            merged.update(extra)
        req_id = request_id_ctx.get()
        if req_id:
            merged["request_id"] = req_id

        user_id = user_id_ctx.get()
        if user_id:
            merged["user_id"] = user_id

        merged.update(fields)

        log_fn = getattr(logger, level)
        log_fn(message, extra=merged)

    def debug(self, message: str, *, extra: dict[str, object] | None = None, **fields: object) -> None:
        self._log("debug", message, extra=extra, **fields)

    def info(self, message: str, *, extra: dict[str, object] | None = None, **fields: object) -> None:
        self._log("info", message, extra=extra, **fields)

    def warning(self, message: str, *, extra: dict[str, object] | None = None, **fields: object) -> None:
        self._log("warning", message, extra=extra, **fields)

    def error(self, message: str, *, extra: dict[str, object] | None = None, **fields: object) -> None:
        self._log("error", message, extra=extra, **fields)

    def critical(self, message: str, *, extra: dict[str, object] | None = None, **fields: object) -> None:
        self._log("critical", message, extra=extra, **fields)


# Global traced logger instance
traced_logger = TracedLogger()


class _MiddlewareApp(Protocol):
    __dict__: dict[str, object]

    def add_middleware(self, middleware_class: type) -> None: ...


def enhance_with_tracing(app: _MiddlewareApp) -> _MiddlewareApp:
    """
    Enhance FastAPI app with request tracing.

    Args:
        app: FastAPI app to enhance

    Returns:
        Enhanced app

    Example:
        from utils.enhancements import enhance_with_tracing
        app = enhance_with_tracing(app)
    """
    enhancer = RequestTracingEnhancer()
    middleware_class = enhancer.middleware_class
    if middleware_class is None:
        logger.warning("FastAPI/Starlette not available, skipping tracing enhancement")
        return app

    if app.__dict__.get("_tracing_enhanced") is not True:
        app.add_middleware(middleware_class)
        app.__dict__["_tracing_enhanced"] = True
        logger.info("✅ Enhanced FastAPI app with request tracing")

    return app
