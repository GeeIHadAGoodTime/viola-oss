"""
Hub Failover System - Fencing Token Manager

Manages monotonic fencing tokens for leader election and split-brain prevention.

Fencing tokens ensure that stale leaders cannot make authoritative decisions
after a failover has occurred. Each new token is strictly greater than the
previous, providing a total ordering of leadership epochs.

Usage:
    from services.multiroom.failover.fencing import FencingTokenManager

    manager = FencingTokenManager()
    token = manager.generate_token()
    if manager.validate_token(incoming_token):
        # Accept the operation
        pass
"""

from __future__ import annotations

import threading
import time

from core.logging_config import get_logger

logger = get_logger(__name__)


class FencingTokenManager:
    """
    Manages monotonic fencing tokens for leader election.

    Fencing tokens are used to:
    - Determine which hub should be primary during split-brain
    - Validate that incoming operations are from the current leader
    - Ensure stale leaders are rejected after failover

    Thread-safe implementation using threading.Lock.
    """

    def __init__(self, initial_token: int = 0) -> None:
        """
        Initialize the fencing token manager.

        Args:
            initial_token: Starting token value (default 0)
        """
        self._current_token = initial_token
        self._lock = threading.Lock()
        self._last_generation_time = 0.0
        logger.debug("FencingTokenManager initialized with token %d", initial_token)

    def generate_token(self) -> int:
        """
        Generate a new monotonic fencing token.

        The token is strictly greater than the previous token.
        Uses time-based increment to ensure monotonicity across restarts.

        Returns:
            New fencing token
        """
        with self._lock:
            # Use time-based component to ensure uniqueness across restarts
            # Token = (unix_millis << 16) + counter
            now_millis = int(time.time() * 1000)
            time_component = now_millis << 16

            # Ensure strict monotonicity
            new_token = max(self._current_token + 1, time_component)
            self._current_token = new_token
            self._last_generation_time = time.time()

            logger.debug("Generated new fencing token: %d", new_token)
            return new_token

    def validate_token(self, token: int) -> bool:
        """
        Check if a token is newer than or equal to the current stored token.

        This is used to validate incoming operations from a leader.
        A token is valid if it is >= the current token.

        Args:
            token: Token to validate

        Returns:
            True if token is valid (newer or equal), False otherwise
        """
        with self._lock:
            is_valid = token >= self._current_token
            if not is_valid:
                logger.warning(
                    "Rejected stale fencing token: %d < current %d",
                    token,
                    self._current_token,
                )
            return is_valid

    def accept_token(self, token: int) -> bool:
        """
        Accept and store a new token if it is newer than the current.

        Used when receiving a token from another hub that should become
        the new authoritative token.

        Args:
            token: Token to potentially accept

        Returns:
            True if token was accepted (newer), False otherwise
        """
        with self._lock:
            if token > self._current_token:
                logger.info(
                    "Accepted new fencing token: %d (was %d)",
                    token,
                    self._current_token,
                )
                self._current_token = token
                return True
            return False

    def get_current_token(self) -> int:
        """
        Return the current fencing token.

        Returns:
            Current token value
        """
        with self._lock:
            return self._current_token

    def compare_tokens(self, token_a: int, token_b: int) -> int:
        """
        Compare two fencing tokens.

        Args:
            token_a: First token
            token_b: Second token

        Returns:
            -1 if token_a < token_b
             0 if token_a == token_b
             1 if token_a > token_b
        """
        if token_a < token_b:
            return -1
        if token_a > token_b:
            return 1
        return 0


__all__ = [
    "FencingTokenManager",
]
