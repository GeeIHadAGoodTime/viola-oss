"""
Queue Events and Logging Handler.

This module contains event emission and logging logic
extracted from the main QueueManager class to comply with code constraints.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.logging_config import get_logger

from .events import QueueStateChanged

logger = get_logger(__name__)


class QueueEventsHandler:
    """Handles event emission and logging for the queue manager."""

    def __init__(self, manager_instance):
        """
        Initialize events handler.

        Args:
            manager_instance: The QueueManager instance
        """
        self.manager = manager_instance

    def emit_queue_state_event(self) -> None:
        """Emit a queue state changed event."""
        try:
            if not hasattr(self.manager, "_event_bus"):
                return

            # Get current state
            state_data = {
                "length": len(self.manager._queue),
                "is_empty": len(self.manager._queue) == 0,
                "max_size": self.manager._max_size,
                "duplicate_detection": self.manager._enable_duplicate_detection,
                "items": self._get_queue_summary(),
            }

            # Add current position if available
            if hasattr(self.manager, "_current_position"):
                state_data["current_position"] = self.manager._current_position

            # Publish event (use publish(), not emit() - emit() is for Qt signals only)
            event = QueueStateChanged(
                queue_length=int(state_data["length"]),
                max_size=int(state_data["max_size"]),
                is_full=bool(state_data["length"] >= state_data["max_size"]),
                is_empty=bool(state_data["is_empty"]),
            )
            self.manager._event_bus.publish(event)

        except Exception as e:
            logger.debug("Failed to emit queue state event: %s", e)

    def log_operation(
        self,
        operation: str,
        result: str,
        reason: str | None = None,
        item_id: str | None = None,
        **kwargs,
    ) -> None:
        """
        Log a queue operation.

        Args:
            operation: Operation name
            result: Operation result
            reason: Optional reason
            item_id: Optional item ID
            **kwargs: Additional logging data
        """
        try:
            log_data = {
                "operation": operation,
                "result": result,
                "queue_length": len(self.manager._queue),
                "timestamp": time.time(),
            }

            if reason:
                log_data["reason"] = reason
            if item_id:
                log_data["item_id"] = item_id

            # Add any additional data
            log_data.update(kwargs)

            # Update metrics
            try:
                metrics = self._get_runtime_metrics()
                if metrics:
                    # Record queue depth
                    metrics.record_command_queue_depth(len(self.manager._queue))

            except Exception as e:
                logger.debug("Failed to update metrics: %s", e)

            # Log the operation
            level = logging.INFO if result == "success" else logging.WARNING
            logger.log(level, "Queue operation: %s -> %s", operation, result, extra=log_data)

        except Exception as e:
            logger.exception("Failed to log queue operation: %s", e)

    def _get_queue_summary(self) -> list[dict[str, Any]]:
        """
        Get a summary of queue items for event emission.

        Returns:
            List of item summaries
        """
        summary = []
        try:
            for i, item in enumerate(self.manager._queue[:10]):  # Limit to first 10 items
                item_summary = {
                    "index": i,
                    "id": getattr(item, "id", f"item_{i}"),
                    "title": getattr(item, "title", "Unknown"),
                }

                # Add optional fields if available
                if hasattr(item, "artist") and item.artist:
                    item_summary["artist"] = item.artist
                if hasattr(item, "duration") and item.duration:
                    item_summary["duration"] = item.duration

                summary.append(item_summary)

            # Indicate if there are more items
            if len(self.manager._queue) > 10:
                summary.append(
                    {
                        "index": -1,
                        "note": f"... and {len(self.manager._queue) - 10} more items",
                    }
                )

        except Exception as e:
            logger.debug("Failed to create queue summary: %s", e)
            summary = [{"error": "Failed to summarize queue"}]

        return summary

    def _get_runtime_metrics(self):
        """Get runtime metrics instance."""
        try:
            from diagnostics.runtime_metrics import get_runtime_metrics

            return get_runtime_metrics()
        except Exception as exc:
            logger.debug("Failed to import runtime metrics: %s", exc)
            return None
