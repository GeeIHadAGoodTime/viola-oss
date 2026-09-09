"""
Audio Core Contracts and Protocols

This module defines the contracts and protocols used by the audio core
for type-safe interactions between components.
"""

from __future__ import annotations

from typing import Protocol

from core.constants import TIMEOUT_MEDIUM
from core.json_types import JsonDict
from models.player import QueueItem


class PlaybackService(Protocol):
    """
    Protocol defining the interface for playback services.

    This protocol ensures type safety when interacting with playback services
    throughout the audio core and integration layers. It defines the synchronous
    interface that matches the legacy MusicPlayer API and includes all methods
    used by the AudioCoreServiceAdapter.
    """

    # Playback control methods
    def play(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonDict | None = None,
    ) -> QueueItem | None:
        """Start playback with query."""
        ...

    def play_async(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonDict | None = None,
    ) -> QueueItem | None:
        """Start playback asynchronously."""
        ...

    def enqueue(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonDict | None = None,
    ) -> QueueItem | None:
        """Add to queue."""
        ...

    def play_next(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonDict | None = None,
    ) -> QueueItem | None:
        """Skip to next track."""
        ...

    def pause(self) -> None:
        """Pause playback."""
        ...

    def resume(self) -> None:
        """Resume playback from paused state."""
        ...

    def stop(self) -> None:
        """Stop playback."""
        ...

    def skip(self) -> None:
        """Skip to next track (alias for next)."""
        ...

    def next(self) -> None:
        """Skip to next track."""
        ...

    def seek(self, seconds: int) -> None:
        """Seek to position in seconds."""
        ...

    def set_volume(self, level: int) -> int:
        """Set volume level (0-100)."""
        ...

    # Extended volume control
    def change_volume(self, delta: int) -> JsonDict:
        """Change volume by delta and return result."""
        ...

    # Queue management
    def clear_queue(self, reason: str = "user") -> None:
        """Clear the playback queue."""
        ...

    def remove_from_queue(self, item_id: str) -> None:
        """Remove item from queue by ID."""
        ...

    def reorder_queue(self, from_index: int, to_index: int) -> None:
        """Reorder queue item from one position to another."""
        ...

    def play_item_now(self, item_id: str) -> None:
        """Play specific item immediately."""
        ...

    def get_queue(self) -> list[JsonDict]:
        """Get current queue as dicts."""
        ...

    # Status and state
    def state(self) -> JsonDict:
        """Get current playback state."""
        ...

    def status(self) -> JsonDict:
        """Get current playback status."""
        ...

    def queue(self) -> list[JsonDict]:
        """Get current queue."""
        ...

    def queue_size(self) -> int:
        """Get queue size."""
        ...

    # Volume
    @property
    def volume(self) -> int:
        """Get current volume level."""
        ...

    def get_volume(self) -> int:
        """Get current volume level."""
        ...

    # Worker management
    def worker_heartbeat(self) -> int:
        """Get worker heartbeat."""
        ...

    def wait_for_worker_heartbeat(self, last_seen: int, timeout: float = TIMEOUT_MEDIUM) -> bool:
        """Wait for worker heartbeat."""
        ...

    # Position and duration
    def position(self) -> int | None:
        """Get current position in seconds."""
        ...

    def duration(self) -> int | None:
        """Get track duration in seconds."""
        ...

    # Queue position
    def current_queue_position(self) -> int | None:
        """Get current queue position."""
        ...

    # Backend info
    def backend_name(self) -> str | None:
        """Get backend name."""
        ...

    # Lifecycle
    def emit_state_change(self) -> None:
        """Emit state change event."""
        ...

    def shutdown(self) -> None:
        """Shutdown the service."""
        ...
