"""Small shared UI state helper used by legacy unit coverage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class UIState:
    """
    Common UI state management patterns.
    """

    def __init__(self):
        """Initialize empty state"""
        self._state: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable]] = {}

    def set(self, key: str, value: Any) -> None:
        """
        Set state value and notify listeners.

        Args:
            key: State key
            value: State value
        """
        old_value = self._state.get(key)
        self._state[key] = value

        # Notify listeners
        if key in self._listeners:
            for callback in self._listeners[key]:
                try:
                    callback(value, old_value)
                except Exception:
                    logger.exception("UIState listener raised an exception")

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get state value.

        Args:
            key: State key
            default: Default value if not set

        Returns:
            State value
        """
        return self._state.get(key, default)

    def subscribe(self, key: str, callback: Callable) -> Callable:
        """
        Subscribe to state changes.

        Args:
            key: State key to subscribe to
            callback: Callback function(value, old_value)

        Returns:
            Unsubscribe function
        """
        if key not in self._listeners:
            self._listeners[key] = []

        self._listeners[key].append(callback)

        # Return unsubscribe function
        def unsubscribe():
            if key in self._listeners and callback in self._listeners[key]:
                self._listeners[key].remove(callback)

        return unsubscribe


__all__ = [
    "UIState",
]
