"""
Audio Service API

Public async service interface for audio control operations.
Combines queue manager and state machine into a unified API.

This module provides:
- REST-like async methods for queue and playback control
- Error handling with structured error codes
- Timeout support for all operations
- Comprehensive logging and event emission
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from dataclasses import dataclass
from typing import Any

from core.constants import TIMEOUT_LONG
from core.events.bus import EventBus, LocalEventBus
from core.exceptions import (
    AudioQueueError as QueueError,
    AudioServiceError,
    AudioStateError as StateError,
    AudioTimeoutError as TimeoutError,
)
from core.logging_config import get_logger
from models.player import QueueItem

from .events import VolumeChanged
from .queue_controller import QueueManager
from .queue_types import QueueOperationResult
from .service_playback_operations import AudioServicePlaybackOperations
from .service_queue_operations import AudioServiceQueueOperations
from .state_machine import PlaybackState, PlaybackStateMachine, StateTransitionError


@dataclass(frozen=True, slots=True)
class PlaybackStatus:
    """Current playback status."""

    state: PlaybackState
    now_playing: QueueItem | None
    queue_length: int
    volume: int
    position_ms: int = 0
    duration_ms: int = 0


class AudioServiceAPI:
    """
    Public async service API for audio control.

    Features:
    - Unified interface for queue and playback operations
    - Async-safe operations (no blocking on event loop)
    - Timeout support for all operations
    - Structured error handling
    - Comprehensive logging and event emission

    Threading Model:
    - All operations are async-safe
    - Uses asyncio locks internally
    - No blocking operations on event loop
    """

    def __init__(
        self,
        queue_manager: QueueManager | None = None,
        state_machine: PlaybackStateMachine | None = None,
        event_bus: EventBus | None = None,
        logger: logging.Logger | None = None,
        default_timeout: float = TIMEOUT_LONG,
    ) -> None:
        """
        Initialize audio service API.

        Args:
            queue_manager: Queue manager instance (default: creates new)
            state_machine: State machine instance (default: creates new)
            event_bus: Event bus for events (default: LocalEventBus)
            logger: Logger instance (default: creates new logger)
            default_timeout: Default timeout for operations (seconds)
        """
        self._event_bus = event_bus or LocalEventBus()
        self._logger = logger or get_logger("audio_core.service_api")
        self._default_timeout = default_timeout

        # Initialize components
        self._queue_manager = queue_manager or QueueManager(event_bus=self._event_bus)
        self._state_machine = state_machine or PlaybackStateMachine(event_bus=self._event_bus)

        # Playback state
        self._volume = 80
        self._position_ms = 0
        self._duration_ms = 0
        self._now_playing: QueueItem | None = None

        # Async lock for service-level operations
        self._lock = asyncio.Lock()

        # Initialize extracted handlers
        self._queue_operations = AudioServiceQueueOperations(self)
        self._playback_operations = AudioServicePlaybackOperations(self)

    # ========== Queue Operations ==========

    async def add_to_queue(
        self,
        item: QueueItem,
        position: int | None = None,
        allow_duplicates: bool = False,
        timeout: float | None = None,
    ) -> None:
        """
        Add item to queue.

        Args:
            item: QueueItem to add
            position: Optional position (None = append)
            allow_duplicates: Allow duplicate items
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            QueueError: If operation fails
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            result, error_msg = await asyncio.wait_for(
                self._queue_manager.add_async(item, position, allow_duplicates),
                timeout=timeout,
            )

            if result != QueueOperationResult.SUCCESS:
                raise QueueError(
                    error_msg or f"Failed to add item: {result.value}",
                    operation="add",
                )

        except builtins.TimeoutError:
            raise TimeoutError(
                f"Add to queue timed out after {timeout}s",
                timeout=timeout,
            ) from None

    async def remove_from_queue(self, item_id: str, timeout: float | None = None) -> None:
        """
        Remove item from queue.

        Args:
            item_id: ID of item to remove
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            QueueError: If operation fails
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            result, error_msg = await asyncio.wait_for(self._queue_manager.remove_async(item_id), timeout=timeout)

            if result != QueueOperationResult.SUCCESS:
                raise QueueError(
                    error_msg or f"Failed to remove item: {result.value}",
                    operation="remove",
                )

        except builtins.TimeoutError:
            raise TimeoutError(
                f"Remove from queue timed out after {timeout}s",
                timeout=timeout,
            ) from None

    async def move_in_queue(self, item_id: str, to_position: int, timeout: float | None = None) -> None:
        """
        Move item to new position in queue.

        Args:
            item_id: ID of item to move
            to_position: Target position (0-based)
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            QueueError: If operation fails
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            result, error_msg = await asyncio.wait_for(
                self._queue_manager.move_async(item_id, to_position), timeout=timeout
            )

            if result != QueueOperationResult.SUCCESS:
                raise QueueError(
                    error_msg or f"Failed to move item: {result.value}",
                    operation="move",
                )

        except builtins.TimeoutError:
            raise TimeoutError(
                f"Move in queue timed out after {timeout}s",
                timeout=timeout,
            ) from None

    async def clear_queue(self, reason: str = "user_request", timeout: float | None = None) -> None:
        """
        Clear all items from queue.

        Args:
            reason: Reason for clearing
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            await asyncio.wait_for(self._queue_manager.clear_async(reason), timeout=timeout)
        except builtins.TimeoutError:
            raise TimeoutError(
                f"Clear queue timed out after {timeout}s",
                timeout=timeout,
            ) from None

    async def get_queue(self) -> list[QueueItem]:
        """
        Get current queue items.

        Returns:
            List of QueueItems
        """
        return await self._queue_manager.get_items_async()

    async def get_queue_length(self) -> int:
        """
        Get current queue length.

        Returns:
            Queue length
        """
        return await self._queue_manager.get_length_async()

    # ========== Playback Control Operations ==========

    async def play(self, track_id: str | None = None, timeout: float | None = None) -> None:
        """
        Start playback (transition to LOADING).

        Args:
            track_id: Optional track ID
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            StateError: If transition is invalid
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            async with self._lock:
                await asyncio.wait_for(
                    asyncio.to_thread(self._state_machine.play, track_id),
                    timeout=timeout,
                )
        except builtins.TimeoutError:
            raise TimeoutError(
                f"Play operation timed out after {timeout}s",
                timeout=timeout,
            ) from None
        except StateTransitionError as e:
            raise StateError(
                str(e),
                operation="play",
            ) from e

    async def pause(self, timeout: float | None = None) -> None:
        """
        Pause playback.

        Args:
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            StateError: If transition is invalid
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            async with self._lock:
                await asyncio.wait_for(asyncio.to_thread(self._state_machine.pause), timeout=timeout)
        except builtins.TimeoutError:
            raise TimeoutError(
                f"Pause operation timed out after {timeout}s",
                timeout=timeout,
            ) from None
        except StateTransitionError as e:
            raise StateError(
                str(e),
                operation="pause",
            ) from e

    async def resume(self, timeout: float | None = None) -> None:
        """
        Resume playback.

        Args:
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            StateError: If transition is invalid
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            async with self._lock:
                await asyncio.wait_for(asyncio.to_thread(self._state_machine.resume), timeout=timeout)
        except builtins.TimeoutError:
            raise TimeoutError(
                f"Resume operation timed out after {timeout}s",
                timeout=timeout,
            ) from None
        except StateTransitionError as e:
            raise StateError(
                str(e),
                operation="resume",
            ) from e

    async def stop(self, timeout: float | None = None) -> None:
        """
        Stop playback.

        Args:
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            StateError: If transition is invalid
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            async with self._lock:
                await asyncio.wait_for(asyncio.to_thread(self._state_machine.stop), timeout=timeout)
        except builtins.TimeoutError:
            raise TimeoutError(
                f"Stop operation timed out after {timeout}s",
                timeout=timeout,
            ) from None
        except StateTransitionError as e:
            raise StateError(
                str(e),
                operation="stop",
            ) from e

    async def skip(self, timeout: float | None = None) -> None:
        """
        Skip to next track.

        Args:
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            StateError: If transition is invalid
            TimeoutError: If operation times out
        """
        timeout = timeout or self._default_timeout

        try:
            async with self._lock:
                await asyncio.wait_for(asyncio.to_thread(self._state_machine.skip), timeout=timeout)
        except builtins.TimeoutError:
            raise TimeoutError(
                f"Skip operation timed out after {timeout}s",
                timeout=timeout,
            ) from None
        except StateTransitionError as e:
            raise StateError(
                str(e),
                operation="skip",
            ) from e

    async def seek(self, position_ms: int, timeout: float | None = None) -> None:
        """
        Seek to position (not implemented in state machine, placeholder).

        Args:
            position_ms: Target position in milliseconds
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            AudioServiceError: If operation fails
        """
        async with self._lock:
            # Update the reported position only; this does not seek the backend.
            self._position_ms = max(0, min(position_ms, self._duration_ms))
            self._logger.debug("Seek to %sms (placeholder)", position_ms)

    # ========== State Query Operations ==========

    async def get_status(self) -> PlaybackStatus:
        """
        Get current playback status.

        Returns:
            PlaybackStatus object
        """
        async with self._lock:
            state = self._state_machine.get_state()
            now_playing_id = self._state_machine.get_now_playing_id()
            queue_length = await self._queue_manager.get_length_async()

            # Find now_playing item if we have an ID
            now_playing = None
            if now_playing_id:
                items = await self._queue_manager.get_items_async()
                for item in items:
                    if item.id == now_playing_id:
                        now_playing = item
                        break

            return PlaybackStatus(
                state=state,
                now_playing=now_playing or self._now_playing,
                queue_length=queue_length,
                volume=self._volume,
                position_ms=self._position_ms,
                duration_ms=self._duration_ms,
            )

    async def get_state(self) -> PlaybackState:
        """
        Get current playback state.

        Returns:
            PlaybackState enum value
        """
        async with self._lock:
            return self._state_machine.get_state()

    # ========== Volume Control ==========

    async def set_volume(self, volume: int, timeout: float | None = None) -> None:
        """
        Set playback volume.

        Args:
            volume: Volume level (0-100)
            timeout: Operation timeout (default: self._default_timeout)

        Raises:
            AudioServiceError: If volume is invalid
        """
        if volume < 0 or volume > 100:
            raise AudioServiceError(
                f"Volume must be between 0 and 100, got {volume}",
                operation="set_volume",
            )

        async with self._lock:
            previous_volume = self._volume
            self._volume = volume

            # Emit event
            try:
                self._event_bus.publish(
                    VolumeChanged(
                        volume=volume,
                        previous_volume=previous_volume,
                        source="audio_core.service_api",
                    )
                )
            except Exception as e:
                self._logger.warning("Failed to emit volume change event: %s", e)

    async def get_volume(self) -> int:
        """
        Get current volume.

        Returns:
            Volume level (0-100)
        """
        async with self._lock:
            return self._volume

    # ========== Internal State Management ==========

    async def _notify_loaded(self) -> None:
        """Notify that track has loaded (internal, called by playback backend)."""
        async with self._lock:
            try:
                self._state_machine.loaded()
            except StateTransitionError as e:
                self._logger.warning("Failed to transition to PLAYING: %s", e)

    async def _notify_ended(self) -> None:
        """Notify that track has ended (internal, called by playback backend)."""
        async with self._lock:
            try:
                self._state_machine.end()
            except StateTransitionError as e:
                self._logger.warning("Failed to transition to IDLE: %s", e)

    async def _notify_error(self, error_message: str, error_code: str = "unknown") -> None:
        """Notify of playback error (internal, called by playback backend)."""
        async with self._lock:
            try:
                self._state_machine.error(error_message, error_code)
            except StateTransitionError as e:
                self._logger.warning("Failed to transition to ERROR: %s", e)

    async def _update_position(self, position_ms: int, duration_ms: int) -> None:
        """Update playback position (internal, called by playback backend)."""
        async with self._lock:
            self._position_ms = position_ms
            self._duration_ms = duration_ms

    # ========== Diagnostics ==========

    async def get_metrics(self) -> dict[str, Any]:
        """
        Get service metrics (for monitoring).

        Returns:
            Dictionary with metrics
        """
        async with self._lock:
            queue_metrics = self._queue_manager.get_metrics()
            state_info = self._state_machine.get_state_info()

            return {
                "queue": queue_metrics,
                "state": state_info,
                "volume": self._volume,
                "position_ms": self._position_ms,
                "duration_ms": self._duration_ms,
            }
