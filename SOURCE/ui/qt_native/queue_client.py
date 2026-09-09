"""
Queue management client for music queue operations.
Handles queue retrieval, clearing, reordering, and track removal.
"""

from __future__ import annotations

from typing import Any

from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

from .api_client_mixin import APIClientMixin

logger = get_logger(__name__)


class QueueClient(APIClientMixin):
    """Client for music queue management operations."""

    def __init__(self, session):
        self.session = session
        self.base_url = None  # Set by parent client

    def get_queue(self) -> dict[str, Any] | None:
        """Get current music queue."""
        try:
            response = self.session.get(f"{self.base_url}/v1/queue", timeout=TIMEOUT_SHUTDOWN)
            response.raise_for_status()
            payload = response.json()
            envelope = self._normalise_envelope(payload)
            if envelope is None:
                logger.error("Queue endpoint returned invalid envelope with type=%s", type(payload).__name__)
                return None
            if not envelope.get("ok", False):
                logger.warning("Queue endpoint reported error: %r", envelope.get("error"))
                return None
            data_section = envelope.get("data")
            if isinstance(data_section, dict):
                return data_section
            logger.error("Unexpected queue payload: %r", data_section)
            return None
        except Exception as e:
            logger.error("Get queue failed: %s", e)
            return None

    def clear_queue(self) -> bool:
        """Clear the music queue."""
        try:
            response = self.session.post(f"{self.base_url}/v1/queue/clear", timeout=TIMEOUT_SHUTDOWN)
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error("Clear queue failed: %s", e)
            return False

    def reorder_queue(self, from_index: int, to_index: int) -> bool:
        """Reorder queue by moving track from one position to another."""
        try:
            response = self.session.post(
                f"{self.base_url}/v1/queue/reorder",
                json={"from_index": from_index, "to_index": to_index},
                timeout=TIMEOUT_SHUTDOWN,
            )
            response.raise_for_status()
            logger.info("Reordered queue: %s -> %s", from_index, to_index)
            return True
        except Exception as e:
            logger.error("Reorder queue failed: %s", e)
            return False

    def remove_from_queue(self, item_id: str) -> bool:
        """Remove track from queue by item ID."""
        try:
            response = self.session.delete(f"{self.base_url}/v1/queue/item/{item_id}", timeout=TIMEOUT_SHUTDOWN)
            response.raise_for_status()
            logger.info("Removed track %s from queue", item_id)
            return True
        except Exception as e:
            logger.error("Remove from queue failed: %s", e)
            return False


__all__ = ["QueueClient"]
