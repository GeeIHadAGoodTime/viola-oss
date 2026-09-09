"""
Deterministic Queue Manager

Thread-safe and async-safe queue manager for audio playback operations.
All operations are atomic and deterministic, with comprehensive logging and
event emission for observability.

Design Principles:
- Atomic operations: All queue mutations are single atomic transactions
- Thread-safe: Uses asyncio locks for async contexts, threading locks for sync
- Async-safe: No blocking operations on event loop
- Deterministic: Same inputs always produce same outputs
- Observable: Structured logging and event emission for all operations
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any

from core.events.bus import EventBus, LocalEventBus
from core.logging_config import get_logger
from diagnostics.runtime_metrics import get_runtime_metrics
from models.player import QueueItem

from .events import QueueCleared, QueueItemMoved, QueueItemRemoved
from .queue_events import QueueEventsHandler
from .queue_operations import QueueOperationsHandler
from .queue_types import QueueOperation, QueueOperationResult

# Queue types are now imported from queue_types.py


class QueueManager:
    """
    Deterministic, thread-safe, async-safe queue manager.

    Features:
    - Atomic queue operations (add, remove, reorder, clear)
    - Thread-safe for concurrent access
    - Async-safe (no blocking on event loop)
    - Deterministic behavior
    - Comprehensive logging and event emission
    - Extensible for plugin sources and advanced features

    Threading Model:
    - Uses asyncio.Lock for async contexts
    - Uses threading.Lock for sync contexts
    - Supports both sync and async operations
    """

    def __init__(
        self,
        max_size: int = 100,
        event_bus: EventBus | None = None,
        logger: logging.Logger | None = None,
        enable_duplicate_detection: bool = True,
    ) -> None:
        """
        Initialize queue manager.

        Args:
            max_size: Maximum queue size (default: 100)
            event_bus: Event bus for emitting events (default: LocalEventBus)
            logger: Logger instance (default: creates new logger)
            enable_duplicate_detection: Enable duplicate item detection (default: True)
        """
        if max_size < 1:
            raise ValueError("max_size must be >= 1")

        self._max_size = max_size
        self._queue: deque[QueueItem] = deque()
        self._item_ids: set[str] = set()  # For O(1) duplicate detection
        self._enable_duplicate_detection = enable_duplicate_detection

        # Threading primitives
        self._thread_lock = threading.RLock()
        self._async_lock: asyncio.Lock | None = None
        self._operation_history: deque[QueueOperation] = deque(maxlen=1000)

        # Event bus and logging
        self._event_bus = event_bus or LocalEventBus()
        self._logger = logger or get_logger("audio_core.queue_controller")
        self._metrics = get_runtime_metrics()

        # Metrics
        self._operation_count = 0
        self._last_operation_time: float | None = None
        self._active_user_id: str | None = None

        # Initialize extracted handlers
        self._operations_handler: QueueOperationsHandler = QueueOperationsHandler(self)
        self._events_handler: QueueEventsHandler = QueueEventsHandler(self)

    def set_user(self, user_id: str) -> None:
        """Switch the active queue owner and clear state on user changes."""
        resolved_user_id = (user_id or "").strip()
        if not resolved_user_id:
            raise ValueError("user_id is required")

        with self._thread_lock:
            previous_user_id = self._active_user_id
            if previous_user_id == resolved_user_id:
                return

            self._active_user_id = resolved_user_id
            if previous_user_id is not None:
                self._logger.info(
                    "Queue user switched: %s -> %s, clearing queue state",
                    previous_user_id,
                    resolved_user_id,
                )
                self.clear(reason="user_switch")

    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create async lock (lazy initialization)."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _emit_queue_state_event(self) -> None:
        """Emit queue state event and update metrics."""
        from .events import QueueStateChanged

        self._event_bus.publish(
            QueueStateChanged(
                queue_length=len(self._queue),
                max_size=self._max_size,
                is_full=len(self._queue) >= self._max_size,
                is_empty=len(self._queue) == 0,
                user_id=self._active_user_id,
            )
        )

        # Record metrics separately (not part of event emission)
        try:
            queue_length = len(self._queue)
            self._metrics.record_command_queue_depth(queue_length)
            self._metrics.record_queue_drift(
                expected_queue_len=queue_length,
                actual_queue_len=queue_length,
                now_playing_track_id=None,
            )
            self._metrics.heartbeat(
                "music.queue",
                status="ok",
                queue_length=queue_length,
                max_size=self._max_size,
                is_full=queue_length >= self._max_size,
            )
        except Exception as e:
            self._logger.warning("Failed to record queue metrics: %s", e)

    def _log_operation(
        self,
        operation_type: str,
        result: QueueOperationResult,
        item_id: str | None = None,
        position: int | None = None,
        error_message: str | None = None,
    ) -> QueueOperation:
        """Log operation and return operation record."""
        operation = QueueOperation(
            operation_id=str(uuid.uuid4()),
            operation_type=operation_type,
            timestamp=time.time(),
            item_id=item_id,
            position=position,
            result=result,
            error_message=error_message,
        )
        self._operation_history.append(operation)
        self._operation_count += 1
        self._last_operation_time = operation.timestamp

        # Structured logging
        log_data = {
            "operation": operation_type,
            "result": result.value,
            "queue_length": len(self._queue),
            "item_id": item_id,
            "position": position,
        }
        if error_message:
            log_data["error"] = error_message

        if result == QueueOperationResult.SUCCESS:
            self._logger.debug("Queue operation: %s", log_data)
        else:
            self._logger.warning("Queue operation failed: %s", log_data)

        return operation

    # ========== Synchronous Operations ==========

    def add(
        self,
        item: QueueItem,
        position: int | None = None,
        allow_duplicates: bool = False,
    ) -> tuple[QueueOperationResult, str | None]:
        """
        Add item to queue (synchronous, thread-safe).

        Args:
            item: QueueItem to add
            position: Optional position (None = append to end)
            allow_duplicates: Allow duplicate items (default: False)

        Returns:
            Tuple of (result, error_message)
        """
        with self._thread_lock:
            return self._add_impl(item, position, allow_duplicates)

    def _add_impl(
        self,
        item: QueueItem,
        position: int | None,
        allow_duplicates: bool,
    ) -> tuple[QueueOperationResult, str | None]:
        """Internal add implementation (assumes lock held)."""
        # Delegate to operations handler
        return self._operations_handler.add_item_impl(item, position, allow_duplicates)

    def remove(self, item_id: str) -> tuple[QueueOperationResult, str | None]:
        """
        Remove item from queue by ID (synchronous, thread-safe).

        Args:
            item_id: ID of item to remove

        Returns:
            Tuple of (result, error_message)
        """
        with self._thread_lock:
            return self._remove_impl(item_id)

    def _remove_impl(self, item_id: str) -> tuple[QueueOperationResult, str | None]:
        """Internal remove implementation (assumes lock held)."""
        if item_id not in self._item_ids:
            self._log_operation(
                "remove",
                QueueOperationResult.ITEM_NOT_FOUND,
                item_id=item_id,
                error_message=f"Item not found: {item_id}",
            )
            return QueueOperationResult.ITEM_NOT_FOUND, "Item not found"

        try:
            # Find and remove item
            queue_list = list(self._queue)
            position = None
            for i, item in enumerate(queue_list):
                if item.id == item_id:
                    position = i
                    queue_list.pop(i)
                    break

            if position is None:
                return QueueOperationResult.ITEM_NOT_FOUND, "Item not found"

            self._queue = deque(queue_list)
            self._item_ids.discard(item_id)

            # Log and emit events
            self._log_operation("remove", QueueOperationResult.SUCCESS, item_id, position)
            self._event_bus.publish(
                QueueItemRemoved(
                    item_id=item_id,
                    position=position,
                    queue_length=len(self._queue),
                    source="audio_core.queue_controller",
                    user_id=self._active_user_id,
                )
            )
            self._emit_queue_state_event()

            return QueueOperationResult.SUCCESS, None

        except Exception as e:
            error_msg = f"Internal error: {e}"
            self._log_operation(
                "remove",
                QueueOperationResult.INTERNAL_ERROR,
                item_id=item_id,
                error_message=error_msg,
            )
            return QueueOperationResult.INTERNAL_ERROR, error_msg

    def move(self, item_id: str, to_position: int) -> tuple[QueueOperationResult, str | None]:
        """
        Move item to new position (synchronous, thread-safe).

        Args:
            item_id: ID of item to move
            to_position: Target position (0-based)

        Returns:
            Tuple of (result, error_message)
        """
        with self._thread_lock:
            return self._move_impl(item_id, to_position)

    def _move_impl(self, item_id: str, to_position: int) -> tuple[QueueOperationResult, str | None]:
        """Internal move implementation (assumes lock held)."""
        if item_id not in self._item_ids:
            return QueueOperationResult.ITEM_NOT_FOUND, "Item not found"

        # Allow to_position == len(queue) - 1 for moving to end
        # After removing item, valid positions are 0 to len-1
        queue_len = len(self._queue)
        if to_position < 0 or to_position >= queue_len:
            return QueueOperationResult.INVALID_POSITION, "Invalid position"

        try:
            queue_list = list(self._queue)
            from_position = None
            item = None

            for i, q_item in enumerate(queue_list):
                if q_item.id == item_id:
                    from_position = i
                    item = queue_list.pop(i)
                    break

            if from_position is None or item is None:
                return QueueOperationResult.ITEM_NOT_FOUND, "Item not found"

            queue_list.insert(to_position, item)
            self._queue = deque(queue_list)

            # Log and emit events
            self._log_operation(
                "move",
                QueueOperationResult.SUCCESS,
                item_id=item_id,
                position=to_position,
            )
            self._event_bus.publish(
                QueueItemMoved(
                    item_id=item_id,
                    from_position=from_position,
                    to_position=to_position,
                    queue_length=len(self._queue),
                    source="audio_core.queue_controller",
                    user_id=self._active_user_id,
                )
            )

            return QueueOperationResult.SUCCESS, None

        except Exception as e:
            error_msg = f"Internal error: {e}"
            self._log_operation(
                "move",
                QueueOperationResult.INTERNAL_ERROR,
                item_id=item_id,
                error_message=error_msg,
            )
            return QueueOperationResult.INTERNAL_ERROR, error_msg

    def clear(self, reason: str = "user_request") -> None:
        """
        Clear all items from queue (synchronous, thread-safe).

        Args:
            reason: Reason for clearing (for logging/events)
        """
        with self._thread_lock:
            previous_length = len(self._queue)
            self._queue.clear()
            self._item_ids.clear()

            self._log_operation("clear", QueueOperationResult.SUCCESS)
            self._event_bus.publish(
                QueueCleared(
                    previous_length=previous_length,
                    reason=reason,
                    source="audio_core.queue_controller",
                    user_id=self._active_user_id,
                )
            )
            self._emit_queue_state_event()

    def get_items(self) -> list[QueueItem]:
        """
        Get copy of all queue items (synchronous, thread-safe).

        Returns:
            List of QueueItems (copy)
        """
        with self._thread_lock:
            return list(self._queue)

    def get_length(self) -> int:
        """Get current queue length (synchronous, thread-safe)."""
        with self._thread_lock:
            return len(self._queue)

    def is_empty(self) -> bool:
        """Check if queue is empty (synchronous, thread-safe)."""
        with self._thread_lock:
            return len(self._queue) == 0

    def is_full(self) -> bool:
        """Check if queue is full (synchronous, thread-safe)."""
        with self._thread_lock:
            return len(self._queue) >= self._max_size

    def peek(self, position: int = 0) -> QueueItem | None:
        """
        Peek at item at position without removing (synchronous, thread-safe).

        Args:
            position: Position to peek (default: 0)

        Returns:
            QueueItem or None if position invalid
        """
        with self._thread_lock:
            if position < 0 or position >= len(self._queue):
                return None
            queue_list = list(self._queue)
            return queue_list[position] if queue_list else None

    def pop_next(self) -> QueueItem | None:
        """
        Pop next item from queue (synchronous, thread-safe).

        Returns:
            QueueItem or None if queue empty
        """
        with self._thread_lock:
            if not self._queue:
                return None

            item = self._queue.popleft()
            self._item_ids.discard(item.id)

            self._log_operation("pop_next", QueueOperationResult.SUCCESS, item.id)
            self._emit_queue_state_event()

            return item

    # ========== Async Operations ==========
    #
    # These methods use both async lock (for async callers) AND thread lock
    # (for thread safety with sync callers). This prevents races when async
    # and sync code access the queue concurrently from different contexts.

    async def add_async(
        self,
        item: QueueItem,
        position: int | None = None,
        allow_duplicates: bool = False,
    ) -> tuple[QueueOperationResult, str | None]:
        """
        Add item to queue (async, thread-safe).

        Args:
            item: QueueItem to add
            position: Optional position (None = append to end)
            allow_duplicates: Allow duplicate items (default: False)

        Returns:
            Tuple of (result, error_message)
        """
        async with self._get_async_lock():
            with self._thread_lock:
                return self._add_impl(item, position, allow_duplicates)

    async def remove_async(self, item_id: str) -> tuple[QueueOperationResult, str | None]:
        """
        Remove item from queue (async, thread-safe).

        Args:
            item_id: ID of item to remove

        Returns:
            Tuple of (result, error_message)
        """
        async with self._get_async_lock():
            with self._thread_lock:
                return self._remove_impl(item_id)

    async def move_async(self, item_id: str, to_position: int) -> tuple[QueueOperationResult, str | None]:
        """
        Move item to new position (async, thread-safe).

        Args:
            item_id: ID of item to move
            to_position: Target position (0-based)

        Returns:
            Tuple of (result, error_message)
        """
        async with self._get_async_lock():
            with self._thread_lock:
                return self._move_impl(item_id, to_position)

    async def clear_async(self, reason: str = "user_request") -> None:
        """
        Clear all items from queue (async, thread-safe).

        Args:
            reason: Reason for clearing (for logging/events)
        """
        async with self._get_async_lock():
            with self._thread_lock:
                previous_length = len(self._queue)
                self._queue.clear()
                self._item_ids.clear()

                self._log_operation("clear", QueueOperationResult.SUCCESS)
                self._event_bus.publish(
                    QueueCleared(
                        previous_length=previous_length,
                        reason=reason,
                        source="audio_core.queue_controller",
                        user_id=self._active_user_id,
                    )
                )
                self._emit_queue_state_event()

    async def get_items_async(self) -> list[QueueItem]:
        """Get copy of all queue items (async, thread-safe)."""
        async with self._get_async_lock():
            with self._thread_lock:
                return list(self._queue)

    async def get_length_async(self) -> int:
        """Get current queue length (async, thread-safe)."""
        async with self._get_async_lock():
            with self._thread_lock:
                return len(self._queue)

    # ========== Metrics and Diagnostics ==========

    def get_operation_history(self, limit: int = 100) -> list[QueueOperation]:
        """
        Get recent operation history (for diagnostics).

        Args:
            limit: Maximum number of operations to return

        Returns:
            List of QueueOperation records
        """
        with self._thread_lock:
            return list(self._operation_history)[-limit:]

    def get_metrics(self) -> dict[str, Any]:
        """
        Get queue metrics (for monitoring).

        Returns:
            Dictionary with metrics
        """
        with self._thread_lock:
            return {
                "queue_length": len(self._queue),
                "max_size": self._max_size,
                "is_full": len(self._queue) >= self._max_size,
                "is_empty": len(self._queue) == 0,
                "operation_count": self._operation_count,
                "last_operation_time": self._last_operation_time,
                "unique_items": len(self._item_ids),
            }
