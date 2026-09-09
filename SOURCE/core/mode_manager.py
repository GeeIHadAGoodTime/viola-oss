"""
Operating Mode Manager for NOVVIOLA.

Manages application-wide operating modes (Normal, Dictation, Screen Look,
Conversation) with thread-safe transitions and listener notification.

The ModeManager is a thread-safe singleton accessed via ``get_instance()``.
Mode transitions fire registered callbacks so subsystems can react
(e.g., suppressing wake detection during dictation).

Usage:
    from core.mode_manager import ModeManager, ViolaMode

    mgr = ModeManager.get_instance()
    mgr.on_mode_change(lambda old, new: print(f"{old} -> {new}"))
    mgr.enter_mode(ViolaMode.DICTATION)
    mgr.exit_to_normal()
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class ViolaMode(StrEnum):
    """Operating modes for the Viola application.

    NORMAL:       Standard assistant behaviour — wake word, intent pipeline.
    DICTATION:    Live dictation — mic audio is transcribed and typed.
    SCREEN_LOOK:  Screen awareness — vision model analyses the display.
    CONVERSATION: Extended conversational mode — wake word is suppressed.
    """

    NORMAL = "normal"
    DICTATION = "dictation"
    SCREEN_LOOK = "screen_look"
    CONVERSATION = "conversation"


# Type alias for mode-change callback: (old_mode, new_mode) -> None
ModeChangeCallback = Callable[[ViolaMode, ViolaMode], None]


class ModeManager:
    """Thread-safe singleton that tracks the active operating mode.

    All mode transitions are guarded by a ``threading.Lock`` so concurrent
    callers (e.g., hotkey listener thread, async intent pipeline) never
    observe or cause an inconsistent state.

    Listeners registered via :meth:`on_mode_change` are invoked
    synchronously under the lock, so callbacks should be lightweight.
    Heavy work should be dispatched to another thread or event loop.
    """

    _instance: ModeManager | None = None
    _lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> ModeManager:
        """Return the singleton ModeManager, creating it on first call.

        Uses double-checked locking to avoid the overhead of acquiring
        the lock on every access after initialisation.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    logger.info("ModeManager singleton initialised")
        return cls._instance

    @classmethod
    def _reset_instance(cls) -> None:
        """Reset the singleton (testing only)."""
        with cls._lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Instance initialisation
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._mode: ViolaMode = ViolaMode.NORMAL
        self._listeners: list[ModeChangeCallback] = []
        self._instance_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_mode(self) -> ViolaMode:
        """Return the current operating mode (thread-safe read)."""
        with self._instance_lock:
            return self._mode

    def enter_mode(self, mode: ViolaMode) -> None:
        """Transition to *mode*, notifying all registered listeners.

        If the application is already in the requested mode, the call is
        a no-op (no callbacks are fired).

        Args:
            mode: The target operating mode.
        """
        with self._instance_lock:
            old = self._mode
            if old == mode:
                logger.debug("Already in mode %s — no-op", mode)
                return
            self._mode = mode
            logger.info("Mode transition: %s -> %s", old, mode)
            self._notify_listeners(old, mode)

    def exit_to_normal(self) -> None:
        """Convenience method: return to NORMAL mode."""
        self.enter_mode(ViolaMode.NORMAL)

    def on_mode_change(self, callback: ModeChangeCallback) -> None:
        """Register a listener that is called on every mode transition.

        The callback receives ``(old_mode, new_mode)`` and is invoked
        under the instance lock, so it must be fast and non-blocking.

        Args:
            callback: A callable ``(ViolaMode, ViolaMode) -> None``.
        """
        with self._instance_lock:
            self._listeners.append(callback)
            logger.debug(
                "Mode-change listener registered (total: %d)",
                len(self._listeners),
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _notify_listeners(self, old: ViolaMode, new: ViolaMode) -> None:
        """Fire all registered callbacks with the transition pair.

        Called while ``_instance_lock`` is held.  Exceptions in individual
        callbacks are logged but never propagated so one broken listener
        cannot block the mode transition.
        """
        for cb in self._listeners:
            try:
                cb(old, new)
            except Exception:
                logger.exception(
                    "Mode-change listener %s failed during %s -> %s",
                    cb,
                    old,
                    new,
                )


__all__ = [
    "ModeChangeCallback",
    "ModeManager",
    "ViolaMode",
]
