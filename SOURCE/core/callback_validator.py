"""
Wake callback validation utilities.

Shared validation logic for _wake_callback_immutable assignments.
Used by VoiceOrchestrator and VoicePipeline to ensure callback integrity.
"""

from __future__ import annotations

import traceback
from typing import Any, Protocol


class LoggerProtocol(Protocol):
    """Protocol for logger instances that support debug and critical methods."""

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log debug message."""
        ...

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log critical message."""
        ...


def validate_wake_callback_assignment(
    name: str,
    value: Any,
    logger: LoggerProtocol,
) -> bool:
    """
    Validate and log wake callback assignments.

    This function implements the callback validation protocol used by both
    VoiceOrchestrator and VoicePipeline. It:

    1. Logs callback assignments with their calling location
    2. Validates that callbacks are callable (or None)
    3. Raises TypeError for values that are neither callable nor None

    Args:
        name: Attribute name being assigned
        value: Value being assigned
        logger: Logger instance for attribution/error logging

    Returns:
        True if this was a _wake_callback_immutable assignment (handled),
        False if this was a different attribute (caller should proceed normally)

    Raises:
        TypeError: If value is not None and not callable
    """
    if name != "_wake_callback_immutable":
        return False  # Not a wake callback, let caller handle normally

    # Record the calling location for each callback assignment
    stack_summary = traceback.extract_stack()[-6:-2]  # Adjusted for helper function depth
    caller_info = " <- ".join(
        f"{frame.filename.split('/')[-1]}:{frame.lineno}:{frame.name}" for frame in reversed(stack_summary)
    )
    logger.debug(
        "CALLBACK_ATTRIBUTION: %s = %s (type=%s) | from: %s",
        name,
        "<callable>" if callable(value) else repr(value),
        type(value).__name__,
        caller_info,
    )

    # Validate: must be None or callable
    if value is not None and not callable(value):
        stack = "".join(traceback.format_stack())
        error_msg = (
            f"FATAL: _wake_callback_immutable must be callable or None!\n"
            f"Got type: {type(value).__name__}\n"
            f"Got value: {value!r}\n"
            f"This is a BUG - find and fix the code that passed this value.\n"
            f"Stack trace:\n{stack}"
        )
        logger.critical(error_msg)
        # Reject invalid callbacks before the caller changes its state
        raise TypeError(error_msg)

    return True  # Validation passed, caller should proceed with assignment
