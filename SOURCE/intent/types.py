from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.json_types import to_json_value

# ==============================
# Public types & result helpers
# ==============================

IntentArgScalar: TypeAlias = str | int | float | bool | None
IntentArgValue: TypeAlias = IntentArgScalar | Mapping[str, "IntentArgValue"] | Sequence["IntentArgValue"]
IntentArgs: TypeAlias = dict[str, IntentArgValue]


@dataclass(frozen=True)
class RoutedCommand:
    """
    Represents a command result passed through the intent pipeline.

    This dataclass formalizes command-shaped metadata already produced by
    deterministic pipeline code, preventing data structure inconsistencies
    between pipeline processors.

    Attributes:
        command: The command name (e.g., 'play_music', 'pause_music')
        params: Command parameters as a dict (e.g., {'query': 'drake'})
        source: Where the command originated.
    """

    command: str
    params: IntentArgs = field(default_factory=dict)
    source: str = "llm_router"

    def to_dict(self) -> IntentArgs:
        """Convert to dictionary for serialization."""
        return {
            "command": self.command,
            "params": self.params,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, IntentArgValue], source: str = "llm_router") -> RoutedCommand:
        """
        Create a RoutedCommand from a dictionary.

        Args:
            data: Dict with 'command' and optionally 'params' keys
            source: The source of the command

        Returns:
            RoutedCommand instance

        Raises:
            ValueError: If 'command' key is missing or not a string
        """
        command = data.get("command")
        if not command or not isinstance(command, str):
            raise ValueError(f"RoutedCommand requires 'command' as non-empty string, got: {command!r}")
        params = data.get("params", {})
        if not isinstance(params, dict):
            params = {}
        return cls(command=command, params=params, source=source)


IntentName = Literal[
    "play",
    "pause",
    "resume",
    "stop",
    "skip",
    "next",
    "previous",
    "seek",
    "volume.set",
    "volume.up",
    "volume.down",
    "say",
    "help",
    "status",
    "play_next",
    "add_to_queue",
    "get_calendar_today",
    "get_calendar_tomorrow",
    "get_calendar_week",
    "get_next_event",
    "create_calendar_event",
    "delete_calendar_event",
]


@dataclass(frozen=True)
class Intent:
    name: IntentName
    args: IntentArgs


@dataclass(frozen=True)
class IntentResult:
    ok: bool
    intent: Intent
    message: str
    data: IntentArgs | None = None
    requires_clarification: bool = False
    policy_flags: list[str] = field(default_factory=list)

    def to_http(self) -> ResponseEnvelope:
        """
        HTTP-friendly normalized dict shape:
        { ok, intent, data, error, requires_clarification, policy_flags }
        - Places human 'message' into data['message'] to avoid conflating with 'error'.
        - Includes requires_clarification and policy_flags per PRD.
        """
        payload = dict(self.data or {})
        payload.setdefault("message", self.message)
        payload_value = to_json_value(
            {
                **payload,
                "intent": self.intent.name,
                "requires_clarification": self.requires_clarification,
                "policy_flags": self.policy_flags,
            }
        )

        if self.ok:
            response = success_response(payload_value)
            # Ensure fields are at top level for consistency
            if isinstance(response, dict):
                data_obj = response.get("data")
                if isinstance(data_obj, dict):
                    data_obj["requires_clarification"] = self.requires_clarification
                    data_obj["policy_flags"] = to_json_value(self.policy_flags)
            return response
        return failure_response(
            f"{self.intent.name}_failed",
            self.message,
            data=payload_value,
        )
