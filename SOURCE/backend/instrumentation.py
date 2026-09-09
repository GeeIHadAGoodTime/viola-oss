"""
FastAPI app instrumentation utilities.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger
from fastapi import FastAPI, Request, Response

logger = get_logger(__name__)

# Header name for correlation ID
CORRELATION_ID_HEADER = "X-Correlation-ID"


def add_correlation_id_middleware(app: FastAPI) -> None:
    """
    Add correlation ID middleware to track requests through the system.

    The middleware:
    - Extracts existing correlation ID from request headers
    - Generates a new correlation ID if none exists
    - Attaches correlation ID to request.state for use in handlers
    - Adds correlation ID to response headers

    Args:
        app: FastAPI application instance
    """
    if getattr(app.state, "correlation_id_middleware_added", False):
        return

    @app.middleware("http")
    async def _correlation_id_middleware(request: Request, call_next: Callable) -> Response:
        # Extract or generate correlation ID
        correlation_id = request.headers.get(CORRELATION_ID_HEADER)

        if not correlation_id:
            # Generate new UUID-based correlation ID
            correlation_id = f"req-{uuid.uuid4().hex[:12]}"

        # Attach to request state for use in route handlers
        request.state.correlation_id = correlation_id

        # Process request
        response = await call_next(request)

        # Add correlation ID to response headers
        response.headers[CORRELATION_ID_HEADER] = correlation_id

        return response

    app.state.correlation_id_middleware_added = True
    logger.debug("Correlation ID middleware added")


def instrument_app(app: FastAPI, runtime_metrics: Any) -> Any:
    """
    Instrument FastAPI app with runtime metrics and request logging.

    Args:
        app: FastAPI application instance
        runtime_metrics: Runtime metrics collector

    Returns:
        The runtime metrics instance
    """
    if getattr(app.state, "runtime_metrics_instrumented", False):
        return runtime_metrics

    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next: Callable) -> Response:
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time

        # Log slow requests
        if duration > 1.0:  # Log requests taking more than 1 second
            logger.info(
                "Slow request: %s %s took %.2fs",
                request.method,
                request.url.path,
                duration,
            )

        return response

    app.state.runtime_metrics_instrumented = True
    return runtime_metrics
