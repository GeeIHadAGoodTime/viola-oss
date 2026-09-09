"""
Core application event infrastructure.

This package centralises the typed event contracts used across the
application, providing a shared backbone for both the Qt client and
headless services.
"""

from .bus import EventBus, LocalEventBus, SignalBus, Subscription, TypedSignal, get_event_bus, set_event_bus
from .types import (
    AgentAnswer,
    AgentEnded,
    AgentError,
    AgentStarted,
    AgentThinking,
    AgentToolEnd,
    AgentToolStart,
    AuthRefreshRequired,
    BaseEvent,
    CallLifecycleEvent,
    CallListenerJoined,
    CallTakeoverReleased,
    CallTakeoverStarted,
    CallTranscriptDelta,
    CommandCompleted,
    CommandIssued,
    CommandQueued,
    ConnectivityChanged,
    DiagnosticsEmitted,
    FeatureToggleUpdated,
    NotificationQueued,
    ServiceLifecycleEvent,
    TelemetryEvent,
)

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
    "EventBus",
    "FeatureToggleUpdated",
    "LocalEventBus",
    "NotificationQueued",
    "ServiceLifecycleEvent",
    "SignalBus",
    "Subscription",
    "TelemetryEvent",
    "TypedSignal",
    "get_event_bus",
    "set_event_bus",
]
