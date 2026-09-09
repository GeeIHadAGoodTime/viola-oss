"""
Queue Manager Types.

This module contains type definitions for the queue manager
to avoid circular imports between modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class QueueOperationResult(Enum):
    """Result status for queue operations."""

    SUCCESS = "success"
    QUEUE_FULL = "queue_full"
    ITEM_NOT_FOUND = "item_not_found"
    INVALID_POSITION = "invalid_position"
    DUPLICATE_ITEM = "duplicate_item"
    OPERATION_TIMEOUT = "operation_timeout"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class QueueOperation:
    """Represents a queue operation with metadata."""

    operation_id: str
    operation_type: str
    timestamp: float
    item_id: str | None = None
    position: int | None = None
    result: QueueOperationResult = QueueOperationResult.SUCCESS
    error_message: str | None = None
