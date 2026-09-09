"""
Command Service Type Definitions.

This module contains the core type definitions for the command service,
extracted to avoid circular import dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.json_types import to_json_value
from utils.text_encoding import repair_mojibake, repair_mojibake_deep


@dataclass(slots=True)
class CommandRequest:
    """Request object for command execution."""

    text: Any
    history: list[Any] | None = None
    channel: Any = "http"
    tracer: Any | None = None
    trace_id: str | None = None
    request_id: str | None = None
    origin: Literal["typed", "voice_wake", "voice_ptt"] | None = None
    user_id: str = ""


@dataclass(slots=True)
class CommandResult:
    """Result object from command execution."""

    ok: bool
    intent: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    status_code: int = 200
    requires_clarification: bool = False
    policy_flags: list[str] = field(default_factory=list)

    def to_envelope(self) -> ResponseEnvelope:
        """Convert result to API response envelope.

        Applies ``repair_mojibake_deep`` to the full payload so that any
        UTF-8-decoded-as-cp1252 artefacts (e.g. ``Â°`` instead of ``°``,
        ``â€¢`` instead of ``•``) are repaired before the response reaches
        the client.  This is a defence-in-depth measure — the ideal fix is
        to prevent double-encoding at source, but on Windows the cp1252
        default codepage can introduce mojibake at hard-to-trace I/O
        boundaries.
        """
        payload_value = to_json_value(
            repair_mojibake_deep(
                {
                    "intent": self.intent,
                    **self.data,
                    "requires_clarification": self.requires_clarification,
                    "policy_flags": self.policy_flags,
                }
            )
        )
        if self.ok:
            response = success_response(payload_value)
            # Ensure fields are in data payload
            if isinstance(response, dict):
                data_obj = response.get("data")
                if isinstance(data_obj, dict):
                    data_obj["requires_clarification"] = self.requires_clarification
                    data_obj["policy_flags"] = to_json_value(self.policy_flags)
            return response
        error_code = self.error or "command_failed"
        raw_message = str(self.data.get("message") or error_code)
        message = repair_mojibake(raw_message)
        return failure_response(error_code, message, data=payload_value)

    def to_payload(self) -> ResponseEnvelope:
        """Alias for backward compatibility with legacy callers."""
        return self.to_envelope()

    def serialize(self) -> dict[str, Any]:
        """Return a JSON-friendly representation suitable for persistence."""
        return {
            "ok": self.ok,
            "intent": self.intent,
            "data": self.data,
            "error": self.error,
            "status_code": self.status_code,
            "requires_clarification": self.requires_clarification,
            "policy_flags": self.policy_flags,
        }

    @classmethod
    def from_serialized(cls, payload: dict[str, Any]) -> CommandResult:
        """Reconstruct a result from :meth:`serialize` output."""
        return cls(
            ok=bool(payload.get("ok", False)),
            intent=str(payload.get("intent", "unknown")),
            data=dict(payload.get("data", {})),
            error=payload.get("error"),
            status_code=int(payload.get("status_code", 200)),
            requires_clarification=bool(payload.get("requires_clarification", False)),
            policy_flags=list(payload.get("policy_flags", [])),
        )
