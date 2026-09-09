"""
music/errors/envelope.py
Structured playback error envelopes shared across server and clients.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from music.error_handler import ErrorInfo, classify_error


@dataclass(slots=True)
class PlaybackErrorEnvelope:
    """
    Canonical representation of playback failures exposed to clients.

    Fields align with UI expectations (`code`, `message`, `severity`, `hint`)
    while also carrying structured context for telemetry and diagnostics.
    """

    code: str
    message: str
    severity: str
    retryable: bool
    origin: str = "resolver"
    context: dict[str, Any] = field(default_factory=dict)
    hint: str | None = None
    timestamp: float = field(default_factory=lambda: time.time())
    category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise envelope into a JSON-friendly dictionary."""
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "retryable": self.retryable,
            "origin": self.origin,
            "timestamp": self.timestamp,
            "context": dict(self.context),
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.category:
            payload["category"] = self.category
        return payload

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        *,
        code: str | None,
        origin: str,
        context: dict[str, Any] | None = None,
    ) -> PlaybackErrorEnvelope:
        """
        Build an envelope from an exception using the global classifier.
        """
        error_info: ErrorInfo = classify_error(exc)
        base_context: dict[str, Any] = {
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
        if context:
            base_context.update(context)
        technical_details = getattr(error_info, "technical_details", None)
        if technical_details:
            base_context.setdefault("technical_details", technical_details)

        hint = error_info.suggestion or None

        return cls(
            code=code or error_info.category.value.upper(),
            message=error_info.message,
            severity=error_info.severity.value,
            retryable=error_info.retryable,
            origin=origin,
            context=base_context,
            hint=hint,
            category=error_info.category.value,
        )


__all__ = ["PlaybackErrorEnvelope"]
