"""
Queue Operations Handler.

This module contains the core queue operation logic
extracted from the main QueueManager class to comply with code constraints.
"""

from __future__ import annotations

from collections import deque

from core.logging_config import get_logger
from models.player import QueueItem

from .events import QueueItemAdded
from .queue_types import QueueOperationResult

logger = get_logger(__name__)


class QueueOperationsHandler:
    """Handles core queue operations for the queue manager."""

    def __init__(self, manager_instance):
        """
        Initialize operations handler.

        Args:
            manager_instance: The QueueManager instance
        """
        self.manager = manager_instance

    def add_item_impl(
        self, item: QueueItem, position: int | None, allow_duplicates: bool
    ) -> tuple[QueueOperationResult, str | None]:
        """Internal add implementation (assumes lock held)."""
        if not item.id:
            return QueueOperationResult.INTERNAL_ERROR, "Item missing ID"

        # Check queue size first (before other validations)
        if len(self.manager._queue) >= self.manager._max_size:
            self.manager._log_operation(
                "add",
                QueueOperationResult.QUEUE_FULL,
                item_id=item.id,
                error_message=f"Queue full ({self.manager._max_size})",
            )
            return QueueOperationResult.QUEUE_FULL, "Queue full"

        # Validate position before duplicate detection
        # (invalid position should be reported even if item is duplicate)
        if position is not None:
            if position < 0 or position > len(self.manager._queue):
                self.manager._log_operation(
                    "add",
                    QueueOperationResult.INVALID_POSITION,
                    item_id=item.id,
                    position=position,
                    error_message=f"Invalid position: {position}",
                )
                return QueueOperationResult.INVALID_POSITION, "Invalid position"

        # Duplicate detection (after position validation)
        if self.manager._enable_duplicate_detection and not allow_duplicates and item.id in self.manager._item_ids:
            self.manager._log_operation(
                "add",
                QueueOperationResult.DUPLICATE_ITEM,
                item_id=item.id,
                error_message=f"Duplicate item: {item.id}",
            )
            return QueueOperationResult.DUPLICATE_ITEM, "Duplicate item"

        # Atomic add operation
        try:
            if position is None:
                self.manager._queue.append(item)
                insert_position = len(self.manager._queue) - 1
            else:
                # Convert deque to list, insert, convert back
                queue_list = list(self.manager._queue)
                queue_list.insert(position, item)
                self.manager._queue = deque(queue_list)
                insert_position = position

            self.manager._item_ids.add(item.id)

            # Log and emit events
            self.manager._log_operation("add", QueueOperationResult.SUCCESS, item.id, insert_position)
            self.manager._event_bus.publish(
                QueueItemAdded(
                    item_id=item.id,
                    position=insert_position,
                    queue_length=len(self.manager._queue),
                    source="audio_core.queue_manager",
                )
            )
            self.manager._emit_queue_state_event()

            return QueueOperationResult.SUCCESS, None

        except Exception as e:
            error_msg = f"Internal error: {e}"
            self.manager._log_operation(
                "add",
                QueueOperationResult.INTERNAL_ERROR,
                item_id=item.id,
                error_message=error_msg,
            )
            return QueueOperationResult.INTERNAL_ERROR, error_msg
