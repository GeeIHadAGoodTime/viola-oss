from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.json_types import JsonDict, JsonValue, to_json_value


def new_command_id() -> str:
    """Generate a new unique command ID for idempotency."""
    return f"cmd-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class IntentPayload:
    """Normalized representation of an interpreted intent.

    Attributes:
        type: The intent type (e.g., "play", "skip", "pause")
        params: Parameters for the intent
        command_id: Unique ID for idempotency - prevents duplicate execution on retry
    """

    type: str
    params: JsonDict = field(default_factory=dict)
    command_id: str | None = None

    def to_legacy(self) -> JsonDict:
        d: JsonDict = {"type": self.type, "params": dict(self.params)}
        if self.command_id:
            d["command_id"] = self.command_id
        return d

    def with_command_id(self, command_id: str | None = None) -> IntentPayload:
        """Return a new IntentPayload with a command_id set."""
        return IntentPayload(
            type=self.type,
            params=self.params,
            command_id=command_id or new_command_id(),
        )


@dataclass(frozen=True)
class AnswerPayload:
    """Represents a conversational answer surfaced by GPT or other interpreters."""

    message: str
    spoken: bool = False
    original_intent: str | None = None
    continue_listening: bool = False
    card: dict | None = None

    def to_legacy(self) -> JsonDict:
        payload: JsonDict = {
            "type": "answer",
            "params": {"message": self.message, "spoken": self.spoken},
        }
        if self.original_intent:
            params = payload["params"]
            if isinstance(params, dict):
                params["original_intent"] = self.original_intent
        if self.continue_listening:
            payload["continue_listening"] = True
            params = payload["params"]
            if isinstance(params, dict):
                params["continue_listening"] = True
        if self.card and isinstance(self.card, dict):
            payload["card"] = self.card
        return payload


@dataclass(frozen=True)
class InterpreterResult:
    """Structured output from interpreter strategies."""

    payload: IntentPayload | None = None
    answer: AnswerPayload | None = None
    error: str | None = None
    raw: JsonValue | None = None

    @property
    def success(self) -> bool:
        return self.payload is not None or self.answer is not None


@dataclass(frozen=True)
class SkillResult:
    """Normalized skill response for downstream adapters."""

    message: str
    spoken: bool
    data: JsonDict | None = None

    def to_legacy(self) -> JsonDict:
        return {
            "type": "skill",
            "params": {
                "message": self.message,
                "spoken": self.spoken,
                "data": self.data,
            },
        }


@dataclass(frozen=True)
class DispatchResult:
    """Normalized command dispatch result."""

    ok: bool
    intent: str
    payload: JsonDict = field(default_factory=dict)
    error: str | None = None

    def to_envelope(self) -> ResponseEnvelope:
        data_payload: JsonDict = {"intent": self.intent, **self.payload}
        if self.ok:
            return success_response(data_payload)
        error_code = self.error or "dispatch_failed"
        message = str(to_json_value(self.payload.get("message")) or error_code)
        return failure_response(error_code, message, data=data_payload)
