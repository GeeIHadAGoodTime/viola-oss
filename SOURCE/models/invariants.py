"""
Invariant Validation Module
Validate state invariants to catch bugs early.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from models.player import QueueItem

logger = get_logger(__name__)


class InvariantViolation(Exception):
    """Raised when an invariant is violated"""

    pass


class InvariantValidator:
    """Validate state invariants"""

    def __init__(self, max_queue_length: int = 1000):
        """
        Initialize invariant validator.

        Args:
            max_queue_length: Maximum allowed queue length
        """
        self.max_queue_length = max_queue_length
        self._violations: list[str] = []

    def validate_queue_length(self, queue: list[Any]) -> bool:
        """
        Validate queue length invariant.

        Args:
            queue: Queue list

        Returns:
            True if valid

        Raises:
            InvariantViolation: If invariant violated
        """
        length = len(queue)

        if length < 0:
            violation = "Queue length cannot be negative"
            logger.error("Invariant violation: %s (length: %s)", violation, length)
            self._violations.append(violation)
            raise InvariantViolation(violation)

        if length > self.max_queue_length:
            violation = f"Queue length ({length}) exceeds maximum ({self.max_queue_length})"
            logger.error("Invariant violation: %s", violation)
            self._violations.append(violation)
            raise InvariantViolation(violation)

        return True

    def validate_currently_playing(self, queue: list[QueueItem], current_id: str | None) -> bool:
        """
        Validate currently playing uniqueness.

        Args:
            queue: Queue list
            current_id: ID of currently playing item

        Returns:
            True if valid

        Raises:
            InvariantViolation: If invariant violated
        """
        if current_id is None:
            return True  # Nothing playing is valid

        # Check that only one item with this ID exists
        playing_items = [item for item in queue if hasattr(item, "id") and item.id == current_id]

        if len(playing_items) > 1:
            violation = f"Multiple items with same ID in queue: {current_id}"
            logger.error("Invariant violation: %s", violation)
            self._violations.append(violation)
            raise InvariantViolation(violation)

        # Check that currently playing item is in queue (or was just removed)
        if len(playing_items) == 0:
            # Item might have been removed from queue (this is OK)
            logger.debug(
                "Currently playing item %s not in queue (may have been removed)",
                current_id,
            )

        return True

    def validate_monotonic_positions(self, queue: list[Any]) -> bool:
        """
        Validate monotonic position tracking.

        Args:
            queue: Queue list

        Returns:
            True if valid

        Raises:
            InvariantViolation: If invariant violated
        """
        positions = []
        for item in queue:
            if hasattr(item, "position"):
                pos = getattr(item, "position", None)
                if pos is not None:
                    positions.append(pos)

        if len(positions) > 1:
            # Check if positions are monotonic (ascending)
            if positions != sorted(positions):
                violation = "Positions must be monotonic (ascending)"
                logger.error("Invariant violation: %s (positions: %s)", violation, positions)
                self._violations.append(violation)
                raise InvariantViolation(violation)

        return True

    def validate_queue_consistency(self, queue: list[QueueItem]) -> bool:
        """
        Validate queue consistency (no duplicates, valid structure).

        Args:
            queue: Queue list

        Returns:
            True if valid

        Raises:
            InvariantViolation: If invariant violated
        """
        # Check for duplicate IDs
        ids: dict[str, int] = {}
        for i, item in enumerate(queue):
            if hasattr(item, "id"):
                item_id = item.id
                if item_id in ids:
                    violation = f"Duplicate item ID in queue: {item_id} at indices {ids[item_id]} and {i}"
                    logger.error("Invariant violation: %s", violation)
                    self._violations.append(violation)
                    raise InvariantViolation(violation)
                ids[item_id] = i

        # Check item structure
        for item in queue:
            if not hasattr(item, "id"):
                violation = "Queue item missing 'id' attribute"
                logger.error("Invariant violation: %s", violation)
                self._violations.append(violation)
                raise InvariantViolation(violation)

            if not hasattr(item, "title"):
                violation = "Queue item missing 'title' attribute"
                logger.error("Invariant violation: %s", violation)
                self._violations.append(violation)
                raise InvariantViolation(violation)

        return True

    def validate_state_consistency(self, state: dict[str, Any]) -> bool:
        """
        Validate overall state consistency.

        Args:
            state: State dictionary

        Returns:
            True if valid

        Raises:
            InvariantViolation: If invariant violated
        """
        # Validate queue exists
        if "queue" not in state:
            violation = "State missing 'queue' key"
            logger.error("Invariant violation: %s", violation)
            self._violations.append(violation)
            raise InvariantViolation(violation)

        queue = state.get("queue", [])

        # Validate queue length
        self.validate_queue_length(queue)

        # Validate queue consistency
        if queue:
            self.validate_queue_consistency(queue)

        # Validate currently playing
        current_id = state.get("now_playing", {}).get("id") if isinstance(state.get("now_playing"), dict) else None
        if current_id is None:
            current_id = state.get("now_playing") if hasattr(state.get("now_playing"), "id") else None

        self.validate_currently_playing(queue, current_id)

        # Validate monotonic positions
        self.validate_monotonic_positions(queue)

        return True

    def get_violations(self) -> list[str]:
        """
        Get list of recorded violations.

        Returns:
            List of violation messages
        """
        return self._violations.copy()

    def clear_violations(self) -> None:
        """Clear recorded violations"""
        self._violations.clear()
