"""
Audio Service Queue Operations Handler.

This module contains queue operation logic
extracted from the main AudioServiceAPI class to comply with code constraints.
"""

from __future__ import annotations

import asyncio

from core.exceptions import AudioQueueError as QueueError
from core.logging_config import get_logger
from models.player import QueueItem

from .queue_types import QueueOperationResult

logger = get_logger(__name__)


class AudioServiceQueueOperations:
    """Handles queue operations for the audio service API."""

    def __init__(self, api_instance):
        """
        Initialize queue operations handler.

        Args:
            api_instance: The AudioServiceAPI instance
        """
        self.api = api_instance

    async def add_to_queue(
        self,
        item: QueueItem,
        position: int | None = None,
        allow_duplicates: bool = False,
        timeout: float | None = None,
    ) -> str:
        """
        Add item to queue.

        Args:
            item: Item to add
            position: Position to insert at (None = append)
            allow_duplicates: Allow duplicate items
            timeout: Operation timeout

        Returns:
            Item ID

        Raises:
            QueueError: If operation fails
        """
        try:
            # Use default timeout if not specified
            timeout = timeout or self.api._default_timeout

            # Execute in thread pool to avoid blocking
            result, item_id = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.api._queue_manager.add(item, position, allow_duplicates),
            )

            if result == QueueOperationResult.SUCCESS:
                if item_id:
                    return item_id
                else:
                    raise QueueError(
                        "Add operation succeeded but no item ID returned",
                        operation="add",
                    )
            elif result == QueueOperationResult.QUEUE_FULL:
                raise QueueError("Queue is at maximum capacity", operation="add")
            elif result == QueueOperationResult.DUPLICATE_ITEM:
                raise QueueError(f"Item {item_id} already exists in queue", operation="add")
            elif result == QueueOperationResult.INVALID_POSITION:
                raise QueueError(f"Invalid position: {position}", operation="add")
            else:
                raise QueueError(f"Unknown error: {result}", operation="add")

        except TimeoutError:
            raise QueueError("Queue add operation timed out", operation="add") from None
        except Exception as e:
            logger.exception("Failed to add item to queue: %s", e)
            raise QueueError(f"Internal error: {e!s}", operation="add") from e

    async def remove_from_queue(
        self,
        item_id: str,
        timeout: float | None = None,
    ) -> None:
        """
        Remove item from queue.

        Args:
            item_id: ID of item to remove
            timeout: Operation timeout

        Raises:
            QueueError: If operation fails
        """
        try:
            timeout = timeout or self.api._default_timeout

            result, _removed_id = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.api._queue_manager.remove(item_id)
            )

            if result == QueueOperationResult.SUCCESS:
                return
            elif result == QueueOperationResult.ITEM_NOT_FOUND:
                raise QueueError(f"Item {item_id} not found in queue", operation="remove")
            else:
                raise QueueError(f"Remove operation failed: {result}", operation="remove")

        except TimeoutError:
            raise QueueError("Queue remove operation timed out", operation="remove") from None
        except Exception as e:
            logger.exception("Failed to remove item from queue: %s", e)
            raise QueueError(f"Internal error: {e!s}", operation="remove") from e

    async def move_in_queue(
        self,
        item_id: str,
        new_position: int,
        timeout: float | None = None,
    ) -> None:
        """
        Move item to new position in queue.

        Args:
            item_id: ID of item to move
            new_position: New position (0-based)
            timeout: Operation timeout

        Raises:
            QueueError: If operation fails
        """
        try:
            timeout = timeout or self.api._default_timeout

            result, _moved_id = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.api._queue_manager.move(item_id, new_position)
            )

            if result == QueueOperationResult.SUCCESS:
                return
            elif result == QueueOperationResult.ITEM_NOT_FOUND:
                raise QueueError(f"Item {item_id} not found in queue", operation="move")
            elif result == QueueOperationResult.INVALID_POSITION:
                raise QueueError(f"Invalid position: {new_position}", operation="move")
            else:
                raise QueueError(f"Move operation failed: {result}", operation="move")

        except TimeoutError:
            raise QueueError("Queue move operation timed out", operation="move") from None
        except Exception as e:
            logger.exception("Failed to move item in queue: %s", e)
            raise QueueError(f"Internal error: {e!s}", operation="move") from e

    async def clear_queue(
        self,
        reason: str = "user_request",
        timeout: float | None = None,
    ) -> None:
        """
        Clear all items from queue.

        Args:
            reason: Reason for clearing
            timeout: Operation timeout

        Raises:
            QueueError: If operation fails
        """
        try:
            timeout = timeout or self.api._default_timeout

            await asyncio.get_event_loop().run_in_executor(None, lambda: self.api._queue_manager.clear(reason))

        except TimeoutError:
            raise QueueError("Queue clear operation timed out", operation="clear") from None
        except Exception as e:
            logger.exception("Failed to clear queue: %s", e)
            raise QueueError(f"Internal error: {e!s}", operation="clear") from e

    async def get_queue(self) -> list[QueueItem]:
        """
        Get current queue items.

        Returns:
            List of queue items
        """
        try:
            return await asyncio.get_event_loop().run_in_executor(None, lambda: self.api._queue_manager.get_items())
        except Exception as e:
            logger.exception("Failed to get queue: %s", e)
            raise QueueError(f"Internal error: {e!s}", operation="get") from e

    async def get_queue_length(self) -> int:
        """
        Get current queue length.

        Returns:
            Number of items in queue
        """
        try:
            return await asyncio.get_event_loop().run_in_executor(None, lambda: self.api._queue_manager.get_length())
        except Exception as e:
            logger.exception("Failed to get queue length: %s", e)
            raise QueueError(f"Internal error: {e!s}", operation="get_length") from e
