"""
FocusManager - Centralized window focus coordination for Viola desktop app.

This module provides a singleton FocusManager that coordinates focus across
all windows in the application, preventing focus conflicts between:
- Main window (lowest priority)
- YouTube overlay (medium priority)
- OAuth popup windows (high priority)
- React modal dialogs (highest priority)

Key principles:
- Priority-based focus: Higher priority windows block lower priority windows
- Focus stack: Track window focus history for restoration
- Lock mechanism: Prevent focus changes during critical operations
- Signal-based: Emit focus_changed/focus_denied for observers

Usage:
    from ui.qt_native.focus_manager import get_focus_manager, FocusLevel

    mgr = get_focus_manager()

    # Request focus for a window
    if mgr.request_focus(main_window, FocusLevel.MAIN, "main_window"):
        main_window.show()
        main_window.activateWindow()

    # Release focus when window closes
    mgr.release_focus("main_window")

    # React modal opens - blocks lower windows
    mgr.push_react_modal("settings_modal")

    # React modal closes - restores previous focus
    mgr.pop_react_modal("settings_modal")
"""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from core.logging_config import get_logger

logger = get_logger(__name__)


class FocusLevel(IntEnum):
    """Priority levels for window focus.

    Higher values = higher priority.
    Higher priority windows block focus requests from lower priority windows.
    """

    MAIN = 1  # Main window - lowest priority
    OVERLAY = 2  # YouTube overlay - medium priority
    POPUP = 3  # OAuth popup windows - high priority
    DIALOG = 4  # React modal dialogs - highest priority


class FocusManager(QObject):
    """Centralized window focus coordinator.

    Singleton that manages focus across all windows in the application.
    Implements priority-based focus with focus stack for restoration.

    Signals:
        focus_changed(str, object): Emitted when focus changes to new window.
            Args: (window_id, widget or None)
        focus_denied(str, str): Emitted when focus request is denied.
            Args: (requester_id, reason)
    """

    # Singleton instance
    _instance: ClassVar[FocusManager | None] = None

    # Signals
    focus_changed = Signal(str, object)  # (window_id, widget or None)
    focus_denied = Signal(str, str)  # (requester_id, reason)

    def __init__(self):
        """Initialize FocusManager.

        Do not instantiate directly - use get_focus_manager() or get_instance().
        """
        super().__init__()

        # Focus stack: List of (level, window_id, widget)
        # Top of stack = current focused window
        # Note: widget can be None for React modals which live inside a webview
        self._focus_stack: list[tuple[FocusLevel, str, QWidget | None]] = []

        # Lock state
        self._locked: bool = False
        self._lock_reason: str = ""

        logger.info("FocusManager initialized")

    @classmethod
    def get_instance(cls) -> FocusManager:
        """Get the singleton FocusManager instance.

        Returns:
            FocusManager singleton
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def request_focus(self, widget: QWidget, level: FocusLevel, window_id: str) -> bool:
        """Request focus for a window.

        Args:
            widget: The Qt widget requesting focus
            level: Priority level for this window
            window_id: Unique identifier for this window

        Returns:
            True if focus granted, False if denied
        """
        # Check if locked
        if self._locked:
            reason = f"Focus locked: {self._lock_reason}"
            logger.debug("Focus denied for '%s': %s", window_id, reason)
            self.focus_denied.emit(window_id, reason)
            return False

        # Get current top of stack
        current_level = self.current_level()

        # Check priority - higher level blocks lower level
        if current_level is not None and level < current_level:
            reason = f"Blocked by higher priority window (level {current_level.name})"
            logger.debug("Focus denied for '%s' (level %s): %s", window_id, level.name, reason)
            self.focus_denied.emit(window_id, reason)
            return False

        # Grant focus - push to stack
        self._focus_stack.append((level, window_id, widget))
        logger.info("Focus granted: '%s' (level %s)", window_id, level.name)

        # Emit focus changed
        self.focus_changed.emit(window_id, widget)

        return True

    def release_focus(self, window_id: str) -> None:
        """Release focus for a window.

        If this window is at the top of the stack, focus is restored to the
        previous window. Otherwise, the window is just removed from the stack.

        Args:
            window_id: Unique identifier of window releasing focus
        """
        # Find window in stack
        stack_index = None
        for i, (level, wid, widget) in enumerate(self._focus_stack):
            if wid == window_id:
                stack_index = i
                break

        if stack_index is None:
            logger.debug("release_focus: '%s' not in focus stack", window_id)
            return

        # Was this the top of the stack?
        was_focused = stack_index == len(self._focus_stack) - 1

        # Remove from stack
        removed_level, _, _ = self._focus_stack.pop(stack_index)
        logger.info("Focus released: '%s' (level %s)", window_id, removed_level.name)

        # If was focused, restore previous focus
        if was_focused:
            if self._focus_stack:
                # Emit focus changed to new top
                new_level, new_id, new_widget = self._focus_stack[-1]
                logger.info("Focus restored: '%s' (level %s)", new_id, new_level.name)
                self.focus_changed.emit(new_id, new_widget)
            else:
                # Stack empty - no focus
                logger.info("Focus stack empty - no focused window")
                self.focus_changed.emit("", None)

    def current_level(self) -> FocusLevel | None:
        """Get the current highest focus level.

        Returns:
            Current focus level, or None if no windows have focus
        """
        if not self._focus_stack:
            return None
        return self._focus_stack[-1][0]

    def current_window_id(self) -> str | None:
        """Get the current focused window ID.

        Returns:
            Window ID of current focused window, or None if no focus
        """
        if not self._focus_stack:
            return None
        return self._focus_stack[-1][1]

    def lock(self, reason: str) -> None:
        """Lock focus changes.

        While locked, all focus requests will be denied.
        Use this during critical operations like window transitions.

        Args:
            reason: Human-readable reason for the lock
        """
        self._locked = True
        self._lock_reason = reason
        logger.info("Focus locked: %s", reason)

    def unlock(self) -> None:
        """Unlock focus changes."""
        if self._locked:
            logger.info("Focus unlocked (was: %s)", self._lock_reason)
            self._locked = False
            self._lock_reason = ""

    def is_locked(self) -> bool:
        """Check if focus is currently locked.

        Returns:
            True if locked, False otherwise
        """
        return self._locked

    def clear(self) -> None:
        """Clear all focus state.

        This is primarily for testing - clears the focus stack and unlock state.
        """
        self._focus_stack.clear()
        self._locked = False
        self._lock_reason = ""
        logger.debug("Focus state cleared")

    def push_react_modal(self, modal_id: str) -> None:
        """Called when a React modal dialog opens.

        This is a convenience method for React modals that don't have a Qt widget.
        Creates a virtual focus entry at DIALOG level.

        Args:
            modal_id: Unique identifier for the modal (e.g., "settings_modal")
        """
        # Use None for widget since React modals are in the web view
        self._focus_stack.append((FocusLevel.DIALOG, modal_id, None))
        logger.info("React modal opened: '%s' (level DIALOG)", modal_id)

        # Emit focus changed
        self.focus_changed.emit(modal_id, None)

    def pop_react_modal(self, modal_id: str) -> None:
        """Called when a React modal dialog closes.

        Releases focus for the modal and restores previous focus.

        Args:
            modal_id: Unique identifier of the modal closing
        """
        # React modals should be at DIALOG level
        # Find and remove from stack
        stack_index = None
        for i, (level, wid, widget) in enumerate(self._focus_stack):
            if wid == modal_id and level == FocusLevel.DIALOG:
                stack_index = i
                break

        if stack_index is None:
            logger.warning("pop_react_modal: '%s' not found in focus stack", modal_id)
            return

        # Was this the top of the stack?
        was_focused = stack_index == len(self._focus_stack) - 1

        # Remove from stack
        self._focus_stack.pop(stack_index)
        logger.info("React modal closed: '%s'", modal_id)

        # If was focused, restore previous focus
        if was_focused:
            if self._focus_stack:
                # Emit focus changed to new top
                new_level, new_id, new_widget = self._focus_stack[-1]
                logger.info("Focus restored: '%s' (level %s)", new_id, new_level.name)
                self.focus_changed.emit(new_id, new_widget)
            else:
                # Stack empty - no focus
                logger.info("Focus stack empty after modal close")
                self.focus_changed.emit("", None)


# Module-level convenience function
def get_focus_manager() -> FocusManager:
    """Get the singleton FocusManager instance.

    This is the recommended way to access the FocusManager.

    Returns:
        FocusManager singleton
    """
    return FocusManager.get_instance()


__all__ = ["FocusLevel", "FocusManager", "get_focus_manager"]
