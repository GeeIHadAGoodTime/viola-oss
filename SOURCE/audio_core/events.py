"""
Audio Core Event Types

Event definitions for audio core operations, extending the BaseEvent contract
from events/types.py to provide structured event flow for queue and playback operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.events.types import BaseEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueItemAdded(BaseEvent):
    """Event emitted when an item is added to the queue."""

    item_id: str
    position: int
    queue_length: int
    source: str = "audio_core.queue_manager"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueItemRemoved(BaseEvent):
    """Event emitted when an item is removed from the queue."""

    item_id: str
    position: int
    queue_length: int
    reason: str = "user_request"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueItemMoved(BaseEvent):
    """Event emitted when an item is reordered in the queue."""

    item_id: str
    from_position: int
    to_position: int
    queue_length: int


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueCleared(BaseEvent):
    """Event emitted when the queue is cleared."""

    previous_length: int
    reason: str = "user_request"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueStateChanged(BaseEvent):
    """Event emitted when queue state changes (length, max_size, etc.)."""

    queue_length: int
    max_size: int
    is_full: bool
    is_empty: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybackStateTransition(BaseEvent):
    """Event emitted when playback state machine transitions."""

    from_state: str
    to_state: str
    trigger: str
    now_playing_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybackPositionChanged(BaseEvent):
    """Event emitted when playback position updates."""

    position_ms: int
    duration_ms: int
    position_percentage: float
    track_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybackErrorEvent(BaseEvent):
    """Event emitted when a playback error occurs.

    Note: This is an event dataclass, not an exception. For the exception class,
    use core.exceptions.PlaybackError instead.
    """

    error_code: str
    error_message: str
    track_id: str | None = None
    recoverable: bool = True
    context: dict[str, Any] = field(default_factory=dict)


# Backward compatibility alias - deprecated, use PlaybackErrorEvent
PlaybackError = PlaybackErrorEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class VolumeChanged(BaseEvent):
    """Event emitted when volume changes."""

    volume: int
    previous_volume: int
    source: str = "user_request"


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncPulse(BaseEvent):
    """Event emitted periodically by sync engine for multi-room synchronization."""

    hub_time: float  # Hub monotonic clock time
    local_time: float  # Local monotonic clock time
    drift_ms: float  # Drift in milliseconds (positive = local ahead, negative = local behind)
    mode: str  # Sync mode: "active", "degrade", "disabled"


__all__ = [
    "PlaybackError",
    "PlaybackPositionChanged",
    "PlaybackStateTransition",
    "QueueCleared",
    "QueueItemAdded",
    "QueueItemMoved",
    "QueueItemRemoved",
    "QueueStateChanged",
    "SyncPulse",
    "VolumeChanged",
]
