from __future__ import annotations

"""
Typed event contracts shared across the application.

These dataclasses provide a structured schema for the signals emitted by
services and consumed by UI layers, enabling strongly-typed event flow
while remaining trivially serialisable for diagnostics and testing.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BaseEvent:
    """Base event structure with metadata common to all events."""

    user_id: str | None = None
    correlation_id: str | None = None
    emitted_at: datetime = field(default_factory=_utc_now)
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectivityChanged(BaseEvent):
    """Connectivity status transition emitted by connectivity services/widgets."""

    state: str = ""
    previous_state: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CommandIssued(BaseEvent):
    """User issued a command intent."""

    text: str = ""


@dataclass(frozen=True, slots=True)
class CommandQueued(BaseEvent):
    """Command queued waiting for backend readiness."""

    text: str = ""
    pending_count: int = 0


@dataclass(frozen=True, slots=True)
class CommandCompleted(BaseEvent):
    """Command execution finished (success or failure)."""

    text: str = ""
    status: Literal["success", "error"] = "success"
    message: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationQueued(BaseEvent):
    """Notification manager is about to display a message to the user."""

    level: Literal["success", "warning", "error", "info"] = "info"
    message: str = ""
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class FeatureToggleUpdated(BaseEvent):
    """Feature flag or experiment toggle changed state."""

    toggle_name: str = ""
    enabled: bool = False
    scope: Literal["user", "session", "system"] = "session"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ServiceLifecycleEvent(BaseEvent):
    """Lifecycle events emitted by managed services."""

    service_name: str = ""
    status: Literal["starting", "started", "stopping", "stopped"] = "starting"
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TelemetryEvent(BaseEvent):
    """Structured telemetry emission for analytics/diagnostics streams."""

    name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CallLifecycleEvent(BaseEvent):
    """Lifecycle transition emitted for a phone call."""

    call_id: str = ""
    status: str = ""
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CallTranscriptDelta(BaseEvent):
    """Live transcript delta for a call listener WebSocket.

    Published by TranscriptFrameCollector during a live phone-call pipeline.
    Word-level streaming for Viola's outbound speech (LLMTextFrame, partial=True)
    and utterance-level for the recipient (TranscriptionFrame, partial=False).
    """

    call_id: str = ""
    role: Literal["them", "viola", "system"] = "system"
    text: str = ""
    partial: bool = False
    ts: str = ""


@dataclass(frozen=True, slots=True)
class CallCostUpdate(BaseEvent):
    """Periodic live cost estimate for an active phone call."""

    call_id: str = ""
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    recipient_state: Literal["voicemail", "ivr", "human", "hold"] = "human"
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class PhoneLoginRequired(BaseEvent):
    """Paid phone/SMS action needs an authenticated Viola account."""

    tool_name: str = ""
    action: str = ""
    error_code: str = "login_required_for_paid_action"
    message: str = ""


@dataclass(frozen=True, slots=True)
class PhoneToSRequired(BaseEvent):
    """Phone calling requires Terms of Service acceptance before retry."""

    tool_name: str = ""
    action: str = "phone_call"
    error_code: str = "phone_tos_required"
    message: str = ""


@dataclass(frozen=True, slots=True)
class CallListenerJoined(BaseEvent):
    """A listen-in client joined a phone call."""

    call_id: str = ""


@dataclass(frozen=True, slots=True)
class CallTakeoverStarted(BaseEvent):
    """A listen-in client started human takeover for a phone call."""

    call_id: str = ""


@dataclass(frozen=True, slots=True)
class CallTakeoverReleased(BaseEvent):
    """A listen-in client released human takeover for a phone call."""

    call_id: str = ""


@dataclass(frozen=True, slots=True)
class DiagnosticsEmitted(BaseEvent):
    """Diagnostics bus produced a structured record."""

    name: str = ""
    severity: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    message: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthRefreshRequired(BaseEvent):
    """Emitted when an OAuth token refresh fails and the user must re-authenticate."""

    provider_id: str = ""
    user_id: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Agent lifecycle events (C3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentStarted(BaseEvent):
    """Emitted when an agent task begins execution."""

    task_id: str = ""
    user_text: str = ""
    native_mode: bool = False


@dataclass(frozen=True, slots=True)
class AgentToolStart(BaseEvent):
    """Emitted just before a tool call is executed."""

    task_id: str = ""
    tool_name: str = ""
    iteration: int = 0


@dataclass(frozen=True, slots=True)
class AgentToolEnd(BaseEvent):
    """Emitted after a tool call completes."""

    task_id: str = ""
    tool_name: str = ""
    iteration: int = 0
    duration_ms: int = 0
    ok: bool = True
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AgentThinking(BaseEvent):
    """Emitted when the agent sends a request to the LLM."""

    task_id: str = ""
    iteration: int = 0
    message_count: int = 0


@dataclass(frozen=True, slots=True)
class AgentAnswer(BaseEvent):
    """Emitted when the agent produces a final answer."""

    task_id: str = ""
    iterations_used: int = 0
    answer_length: int = 0
    has_command: bool = False


@dataclass(frozen=True, slots=True)
class AgentError(BaseEvent):
    """Emitted when the agent encounters an error."""

    task_id: str = ""
    iteration: int = 0
    error: str = ""
    stage: str = ""


@dataclass(frozen=True, slots=True)
class AgentEnded(BaseEvent):
    """Emitted when an agent task finishes (success, timeout, or error)."""

    task_id: str = ""
    outcome: str = ""
    iterations_used: int = 0
    duration_s: float = 0.0
    tools_called: list[str] = field(default_factory=list)


__all__ = [
    "AgentAnswer",
    "AgentEnded",
    "AgentError",
    "AgentStarted",
    "AgentThinking",
    "AgentToolEnd",
    "AgentToolStart",
    "AuthRefreshRequired",
    "BaseEvent",
    "CallCostUpdate",
    "CallLifecycleEvent",
    "CallListenerJoined",
    "CallTakeoverReleased",
    "CallTakeoverStarted",
    "CallTranscriptDelta",
    "CommandCompleted",
    "CommandIssued",
    "CommandQueued",
    "ConnectivityChanged",
    "DiagnosticsEmitted",
    "FeatureToggleUpdated",
    "NotificationQueued",
    "PhoneLoginRequired",
    "PhoneToSRequired",
    "ServiceLifecycleEvent",
    "TelemetryEvent",
]
