"""
diagnostics/error_classification.py
===================================

Smart exception categorization system for AI-friendly logging.

Exceptions are classified as either:
- EXPECTED: Normal operational conditions (log at DEBUG level)
- UNEXPECTED: Bugs, misconfigurations, or system problems (log at WARNING/ERROR)

This enables AI debugging sessions to quickly identify real problems vs normal noise.

Usage:
    from diagnostics.error_classification import (
        ErrorCategory,
        categorize_exception,
        log_categorized_exception,
    )

    try:
        some_operation()
    except Exception as e:
        log_categorized_exception(
            logger, e,
            message="Operation failed",
            component="my_component",
            operation="some_operation",
        )
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.logging_config import StructuredLogger


class ErrorCategory(Enum):
    """
    Classification of exceptions for logging level decisions.

    EXPECTED categories: Log at DEBUG level - these are normal operational conditions
    UNEXPECTED categories: Log at WARNING/ERROR level - these indicate real problems
    """

    # === EXPECTED errors - Log at DEBUG level ===
    # These are normal operational conditions that users/admins shouldn't worry about

    EXPECTED_USER_INPUT = auto()
    """Invalid user input, empty commands, malformed user requests."""

    EXPECTED_TIMEOUT = auto()
    """Service timeouts that are retryable (network latency, slow responses)."""

    EXPECTED_RATE_LIMIT = auto()
    """API rate limits - expected when using external services heavily."""

    EXPECTED_NETWORK = auto()
    """Transient network issues (connection reset, temporary DNS failures)."""

    EXPECTED_RESOURCE_BUSY = auto()
    """Device busy, lock contention, resource temporarily unavailable."""

    EXPECTED_GRACEFUL_SHUTDOWN = auto()
    """Task cancellation, shutdown signals, clean termination."""

    EXPECTED_CACHE_MISS = auto()
    """Cache miss, stale cache, corrupt cache file (recoverable)."""

    EXPECTED_CLIENT_DISCONNECT = auto()
    """WebSocket/HTTP client disconnected (normal for browsers)."""

    # === UNEXPECTED errors - Log at WARNING or ERROR level ===
    # These indicate bugs, misconfigurations, or system problems

    UNEXPECTED_STATE = auto()
    """Invalid state transitions, assertion failures about state."""

    UNEXPECTED_DATA = auto()
    """Malformed data from internal sources, schema violations."""

    UNEXPECTED_CONFIG = auto()
    """Missing or invalid configuration that should exist."""

    UNEXPECTED_DEPENDENCY = auto()
    """Missing imports, broken dependencies, version mismatches."""

    UNEXPECTED_SYSTEM = auto()
    """OS errors, resource exhaustion, permission denied."""

    UNEXPECTED_BUG = auto()
    """Type errors, attribute errors, likely code bugs."""


# Exception type -> Category mapping
# More specific exceptions should be listed first
# Note: Uses BaseException to include KeyboardInterrupt, SystemExit, CancelledError
EXCEPTION_CATEGORY_MAP: dict[type[BaseException], ErrorCategory] = {
    # === Expected exceptions ===
    # Timeout/Cancellation
    asyncio.TimeoutError: ErrorCategory.EXPECTED_TIMEOUT,
    TimeoutError: ErrorCategory.EXPECTED_TIMEOUT,
    asyncio.CancelledError: ErrorCategory.EXPECTED_GRACEFUL_SHUTDOWN,
    KeyboardInterrupt: ErrorCategory.EXPECTED_GRACEFUL_SHUTDOWN,
    SystemExit: ErrorCategory.EXPECTED_GRACEFUL_SHUTDOWN,
    # Network
    ConnectionResetError: ErrorCategory.EXPECTED_NETWORK,
    ConnectionRefusedError: ErrorCategory.EXPECTED_NETWORK,
    ConnectionAbortedError: ErrorCategory.EXPECTED_NETWORK,
    BrokenPipeError: ErrorCategory.EXPECTED_NETWORK,
    socket.timeout: ErrorCategory.EXPECTED_TIMEOUT,
    # Resource
    BlockingIOError: ErrorCategory.EXPECTED_RESOURCE_BUSY,
    # Data
    json.JSONDecodeError: ErrorCategory.EXPECTED_CACHE_MISS,
    # === Unexpected exceptions ===
    # Bugs
    TypeError: ErrorCategory.UNEXPECTED_BUG,
    AttributeError: ErrorCategory.UNEXPECTED_BUG,
    AssertionError: ErrorCategory.UNEXPECTED_BUG,
    NotImplementedError: ErrorCategory.UNEXPECTED_BUG,
    RecursionError: ErrorCategory.UNEXPECTED_BUG,
    # Dependencies
    ImportError: ErrorCategory.UNEXPECTED_DEPENDENCY,
    ModuleNotFoundError: ErrorCategory.UNEXPECTED_DEPENDENCY,
    # System
    PermissionError: ErrorCategory.UNEXPECTED_SYSTEM,
    MemoryError: ErrorCategory.UNEXPECTED_SYSTEM,
    OSError: ErrorCategory.UNEXPECTED_SYSTEM,
    # Data
    ValueError: ErrorCategory.UNEXPECTED_DATA,
    KeyError: ErrorCategory.UNEXPECTED_DATA,
    IndexError: ErrorCategory.UNEXPECTED_DATA,
}

# Message patterns for heuristic categorization
_EXPECTED_MESSAGE_PATTERNS = [
    ("timeout", ErrorCategory.EXPECTED_TIMEOUT),
    ("timed out", ErrorCategory.EXPECTED_TIMEOUT),
    ("rate limit", ErrorCategory.EXPECTED_RATE_LIMIT),
    ("too many requests", ErrorCategory.EXPECTED_RATE_LIMIT),
    ("429", ErrorCategory.EXPECTED_RATE_LIMIT),
    ("connection reset", ErrorCategory.EXPECTED_NETWORK),
    ("connection refused", ErrorCategory.EXPECTED_NETWORK),
    ("resource busy", ErrorCategory.EXPECTED_RESOURCE_BUSY),
    ("device busy", ErrorCategory.EXPECTED_RESOURCE_BUSY),
    ("cancelled", ErrorCategory.EXPECTED_GRACEFUL_SHUTDOWN),
    ("shutting down", ErrorCategory.EXPECTED_GRACEFUL_SHUTDOWN),
    ("client disconnected", ErrorCategory.EXPECTED_CLIENT_DISCONNECT),
    ("websocket closed", ErrorCategory.EXPECTED_CLIENT_DISCONNECT),
]

_UNEXPECTED_MESSAGE_PATTERNS = [
    ("assertion failed", ErrorCategory.UNEXPECTED_STATE),
    ("invalid state", ErrorCategory.UNEXPECTED_STATE),
    ("unexpected state", ErrorCategory.UNEXPECTED_STATE),
    ("missing key", ErrorCategory.UNEXPECTED_CONFIG),
    ("not configured", ErrorCategory.UNEXPECTED_CONFIG),
    ("import error", ErrorCategory.UNEXPECTED_DEPENDENCY),
    ("module not found", ErrorCategory.UNEXPECTED_DEPENDENCY),
]


def categorize_exception(
    exc: BaseException,
    context: str = "",
    *,
    default: ErrorCategory = ErrorCategory.UNEXPECTED_BUG,
) -> ErrorCategory:
    """
    Determine the category for an exception.

    Uses the exception type mapping first, then falls back to heuristics
    based on exception message and context.

    Args:
        exc: The exception to categorize.
        context: Optional context string (e.g., "playback.vlc.cleanup").
        default: Default category if no match found.

    Returns:
        The determined ErrorCategory.
    """
    # Check if exception has a category attribute (ViolaError hierarchy)
    if hasattr(exc, "category"):
        category_attr = getattr(exc, "category", None)
        if category_attr is not None:
            return category_attr

    # Check type mapping (most specific first via isinstance)
    for exc_type, category in EXCEPTION_CATEGORY_MAP.items():
        if isinstance(exc, exc_type):
            return category

    # Heuristics based on message
    msg = str(exc).lower()
    full_context = f"{context} {msg}".lower()

    # Check expected patterns first (more lenient)
    for pattern, category in _EXPECTED_MESSAGE_PATTERNS:
        if pattern in full_context:
            return category

    # Check unexpected patterns
    for pattern, category in _UNEXPECTED_MESSAGE_PATTERNS:
        if pattern in full_context:
            return category

    # Special case: OSError with specific errno values
    if isinstance(exc, OSError) and exc.errno is not None:
        import errno

        # These are generally expected/recoverable
        expected_errnos = {
            errno.EBUSY,  # Device or resource busy
            errno.EAGAIN,  # Resource temporarily unavailable
            errno.EWOULDBLOCK,  # Operation would block
            errno.EINTR,  # Interrupted system call
            errno.ECONNRESET,  # Connection reset by peer
            errno.EPIPE,  # Broken pipe
        }
        if exc.errno in expected_errnos:
            return ErrorCategory.EXPECTED_RESOURCE_BUSY

    return default


def is_expected_exception(exc: BaseException, context: str = "") -> bool:
    """
    Check if an exception is categorized as expected.

    Convenience function for quick checks.
    """
    category = categorize_exception(exc, context)
    return category.name.startswith("EXPECTED_")


def log_categorized_exception(
    logger: StructuredLogger,
    exc: BaseException,
    message: str,
    component: str,
    operation: str,
    *,
    context: str = "",
    include_traceback: bool | None = None,
    **extra_context: Any,
) -> ErrorCategory:
    """
    Log an exception at the appropriate level based on its category.

    Expected exceptions are logged at DEBUG level (low noise).
    Unexpected exceptions are logged at WARNING or ERROR level.

    Args:
        logger: The logger to use.
        exc: The exception to log.
        message: Human-readable message describing what failed.
        component: Component name (e.g., "playback.vlc").
        operation: Operation that failed (e.g., "cleanup").
        context: Additional context for categorization.
        include_traceback: Whether to include traceback. If None, auto-decides:
            - True for unexpected bugs/system errors
            - False for expected errors
        **extra_context: Additional context fields for structured logging.

    Returns:
        The determined ErrorCategory for caller use.
    """
    category = categorize_exception(exc, f"{component}.{operation} {context}")

    log_context = {
        "component": component,
        "operation": operation,
        "error_category": category.name,
        "exception_type": type(exc).__name__,
        **extra_context,
    }

    # Auto-decide traceback inclusion
    if include_traceback is None:
        include_traceback = category in (
            ErrorCategory.UNEXPECTED_BUG,
            ErrorCategory.UNEXPECTED_SYSTEM,
            ErrorCategory.UNEXPECTED_STATE,
        )

    formatted_message = f"{message}: {exc}"

    if category.name.startswith("EXPECTED_"):
        # Expected errors: DEBUG level, minimal noise
        logger.debug(formatted_message, **log_context)
    elif category in (ErrorCategory.UNEXPECTED_BUG, ErrorCategory.UNEXPECTED_SYSTEM):
        # Critical unexpected: ERROR with full stack
        if include_traceback:
            logger.exception(message, **log_context)
        else:
            logger.error(formatted_message, **log_context)
    else:
        # Other unexpected: WARNING
        if include_traceback:
            logger.warning(formatted_message, **log_context)
            logger.debug("Stack trace:", exc_info=True)
        else:
            logger.warning(formatted_message, **log_context)

    return category


def compute_error_context_hash(
    component: str,
    code: str,
    context: dict[str, Any] | None = None,
) -> str:
    """
    Compute a hash for error deduplication.

    The hash is based on stable context fields only, excluding transient
    data like timestamps and correlation IDs.

    Args:
        component: Component name.
        code: Error code.
        context: Optional context dict.

    Returns:
        A short hash string for deduplication.
    """
    # Extract stable fields only
    stable_context = {}
    if context:
        for key, value in context.items():
            # Skip transient fields
            if key in ("timestamp", "correlation_id", "request_id", "trace_id"):
                continue
            # Include fields explicitly marked as stable
            if (
                key.startswith("key_")
                or key.startswith("stable_")
                or key
                in (
                    "provider_name",
                    "error_type",
                    "exception_type",
                    "tier",
                    "backend",
                    "operation",
                )
            ):
                stable_context[key] = value

    # Build hash input
    hash_input = f"{component}:{code}:{json.dumps(stable_context, sort_keys=True)}"
    return hashlib.md5(hash_input.encode(), usedforsecurity=False).hexdigest()[:12]


__all__ = [
    "ErrorCategory",
    "categorize_exception",
    "compute_error_context_hash",
    "is_expected_exception",
    "log_categorized_exception",
]
