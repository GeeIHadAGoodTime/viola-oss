"""
Thread Bridges - Safe cross-thread communication with StateHub.

AI Instructions
===============
Bridges allow components in different threads to interact with StateHub safely.
Each bridge adapts StateHub notifications to its thread's execution model.

Usage:
    # Qt thread bridge
    >>> from core.thread_bridges import QtStateBridge
    >>> bridge = QtStateBridge()
    >>> bridge.connect_to_hub()
    >>> bridge.on_voice_changed.connect(self.update_voice_indicator)

    # Voice thread bridge
    >>> from core.thread_bridges import VoiceThreadBridge
    >>> bridge = VoiceThreadBridge()
    >>> bridge.start()
    >>> bridge.on_playback_state_changed(self.handle_playback_change)

Architecture:
    - QtStateBridge: Uses Qt signals to deliver state changes on Qt main thread
    - VoiceThreadBridge: Filters state changes to voice-relevant updates
    - Both provide thread-safe dispatch() for sending commands to StateHub

Related Modules:
    - core/state_hub.py: Central state coordinator
    - core/unified_state.py: State dataclass definitions
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from core.logging_config import get_logger
from core.state_hub import StateCommand, get_state_hub
from core.unified_state import AppState, VoiceState

if TYPE_CHECKING:
    from models.player import PlayerState

logger = get_logger(__name__)


@runtime_checkable
class StateBridgeProtocol(Protocol):
    """Protocol for state bridges (Qt and non-Qt implementations)."""

    def connect_to_hub(self) -> None:
        """Start receiving state updates from the hub."""
        ...

    def disconnect_from_hub(self) -> None:
        """Stop receiving state updates."""
        ...

    def dispatch(self, command: StateCommand) -> None:
        """Dispatch command to hub (thread-safe)."""
        ...

    def get_state(self) -> AppState:
        """Get current state snapshot."""
        ...


# =============================================================================
# Qt Thread Bridge
# =============================================================================


class _FallbackQtStateBridge:
    """
    Fallback bridge when PyQt6 is not available.

    Uses simple callbacks instead of Qt signals for state change notifications.
    """

    def __init__(self, parent: Any = None, *, user_id: str | None = None) -> None:
        self._unsubscribe: Callable[[], None] | None = None
        self._hub = get_state_hub(user_id=user_id)
        self.user_id = self._hub.user_id
        self._callbacks: dict[str, list[Callable[..., None]]] = {
            "state_changed": [],
            "playback_changed": [],
            "voice_changed": [],
            "connection_changed": [],
            "room_changed": [],
            "ui_changed": [],
        }

    def connect_to_hub(self) -> None:
        """Start receiving state updates from the hub."""
        if self._unsubscribe is not None:
            logger.warning("QtStateBridge already connected")
            return
        self._unsubscribe = self._hub.subscribe(self._on_state_changed)
        logger.info("QtStateBridge connected to StateHub")

    def disconnect_from_hub(self) -> None:
        """Stop receiving state updates."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
            logger.info("QtStateBridge disconnected from StateHub")

    def dispatch(self, command: StateCommand) -> None:
        """Dispatch command to hub (thread-safe)."""
        self._hub.dispatch(command)

    def get_state(self) -> AppState:
        """Get current state snapshot."""
        return self._hub.get_state()

    def connect(self, signal_name: str, callback: Callable[..., None]) -> None:
        """Connect a callback to a signal (fallback API for non-Qt mode)."""
        if signal_name in self._callbacks:
            self._callbacks[signal_name].append(callback)

    def _on_state_changed(self, old_state: AppState, new_state: AppState) -> None:
        """Handle state changes."""
        for cb in self._callbacks["state_changed"]:
            try:
                cb(old_state, new_state)
            except Exception as e:
                logger.error("Callback error: %s", e)

        if self._playback_changed(old_state, new_state):
            for cb in self._callbacks["playback_changed"]:
                try:
                    cb(old_state, new_state)
                except Exception as e:
                    logger.error("Playback callback error: %s", e)

        if old_state.voice != new_state.voice:
            for cb in self._callbacks["voice_changed"]:
                try:
                    cb(old_state, new_state)
                except Exception as e:
                    logger.error("Voice callback error: %s", e)

        if old_state.connection != new_state.connection:
            for cb in self._callbacks["connection_changed"]:
                try:
                    cb(old_state, new_state)
                except Exception as e:
                    logger.error("Connection callback error: %s", e)

        if old_state.rooms != new_state.rooms or old_state.active_room_id != new_state.active_room_id:
            for cb in self._callbacks["room_changed"]:
                try:
                    cb(old_state, new_state)
                except Exception as e:
                    logger.error("Room callback error: %s", e)

        if old_state.ui != new_state.ui:
            for cb in self._callbacks["ui_changed"]:
                try:
                    cb(old_state, new_state)
                except Exception as e:
                    logger.error("UI callback error: %s", e)

    def _playback_changed(self, old: AppState, new: AppState) -> bool:
        """Check if playback state changed."""
        old_room = old.active_room
        new_room = new.active_room

        if old_room is None and new_room is None:
            return False
        if old_room is None or new_room is None:
            return True
        return bool(old_room.player != new_room.player)


# Try to load Qt-enhanced version if PyQt6 is available.
# We treat the bridge class as a factory to keep constructor typing explicit.
_QtStateBridgeClass: Callable[..., StateBridgeProtocol] = _FallbackQtStateBridge

try:
    from PySide6.QtCore import QObject, Signal

    class _QtSignalBridge(QObject):
        """Qt-enhanced bridge with proper signals for thread-safe delivery."""

        # General state change signal
        state_changed = Signal(object, object)  # (old_state, new_state)

        # Filtered signals for specific state domains
        playback_changed = Signal(object, object)
        voice_changed = Signal(object, object)
        connection_changed = Signal(object, object)
        room_changed = Signal(object, object)
        ui_changed = Signal(object, object)

        def __init__(self, parent: Any = None, *, user_id: str | None = None) -> None:
            super().__init__(parent)
            self._unsubscribe: Callable[[], None] | None = None
            self._hub = get_state_hub(user_id=user_id)
            self.user_id = self._hub.user_id

        def connect_to_hub(self) -> None:
            """Start receiving state updates from the hub."""
            if self._unsubscribe is not None:
                logger.warning("QtStateBridge already connected")
                return

            self._unsubscribe = self._hub.subscribe(self._on_state_changed)
            logger.info("QtStateBridge connected to StateHub")

        def disconnect_from_hub(self) -> None:
            """Stop receiving state updates."""
            if self._unsubscribe:
                self._unsubscribe()
                self._unsubscribe = None
                logger.info("QtStateBridge disconnected from StateHub")

        def dispatch(self, command: StateCommand) -> None:
            """Dispatch command to hub (thread-safe)."""
            self._hub.dispatch(command)

        def get_state(self) -> AppState:
            """Get current state snapshot."""
            return self._hub.get_state()

        def _on_state_changed(self, old_state: AppState, new_state: AppState) -> None:
            """Called by StateHub worker thread - emit signals to move to Qt thread."""
            # Always emit general signal
            self.state_changed.emit(old_state, new_state)

            # Emit filtered signals based on what changed
            if self._playback_changed(old_state, new_state):
                # mt-ok: Qt signal emit is in-process desktop thread-marshalling,
                # not a cross-user network broadcast.
                self.playback_changed.emit(old_state, new_state)

            if old_state.voice != new_state.voice:
                self.voice_changed.emit(old_state, new_state)

            if old_state.connection != new_state.connection:
                self.connection_changed.emit(old_state, new_state)

            if old_state.rooms != new_state.rooms or old_state.active_room_id != new_state.active_room_id:
                self.room_changed.emit(old_state, new_state)

            if old_state.ui != new_state.ui:
                self.ui_changed.emit(old_state, new_state)

        def _playback_changed(self, old: AppState, new: AppState) -> bool:
            """Check if playback state changed."""
            old_room = old.active_room
            new_room = new.active_room

            if old_room is None and new_room is None:
                return False
            if old_room is None or new_room is None:
                return True
            return bool(old_room.player != new_room.player)

    # Use the Qt-enhanced version
    _QtStateBridgeClass = _QtSignalBridge
    logger.debug("PyQt6 available, using Qt signal-based bridge")

except ImportError:
    logger.debug("PyQt6 not available, QtStateBridge will use basic callbacks")


def QtStateBridge(parent: Any = None, *, user_id: str | None = None) -> StateBridgeProtocol:
    """
    Create a QtStateBridge instance.

    When PyQt6 is available, returns a Qt signal-based bridge.
    When PyQt6 is not available, returns a callback-based bridge.

    Args:
        parent: Optional Qt parent object (only used when Qt is available)

    Returns:
        A StateBridgeProtocol-compatible bridge instance
    """
    return _QtStateBridgeClass(parent, user_id=user_id)


# =============================================================================
# Voice Thread Bridge
# =============================================================================


class VoiceThreadBridge:
    """
    Bridge between StateHub and voice orchestrator thread.

    Filters state changes to voice-relevant updates only.
    Voice thread reads state via selectors and dispatches commands.

    Usage:
        >>> bridge = VoiceThreadBridge()
        >>> bridge.start()
        >>> bridge.on_voice_state_changed(self.handle_voice_update)
        >>> bridge.on_playback_state_changed(self.handle_playback_update)
        >>> # Use duck/unduck state:
        >>> bridge.on_ducking_changed(self.handle_ducking)
        >>> # Clean up:
        >>> bridge.stop()
    """

    def __init__(self, *, user_id: str | None = None) -> None:
        self._hub = get_state_hub(user_id=user_id)
        self.user_id = self._hub.user_id
        self._unsubscribe: Callable[[], None] | None = None
        self._voice_callbacks: list[Callable[[VoiceState], None]] = []
        self._playback_callbacks: list[Callable[[bool, Any], None]] = []
        self._ducking_callbacks: list[Callable[[bool], None]] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start listening for relevant state changes."""
        if self._unsubscribe is not None:
            logger.warning("VoiceThreadBridge already started")
            return

        self._unsubscribe = self._hub.subscribe(self._on_state_changed)
        logger.info("VoiceThreadBridge started")

    def stop(self) -> None:
        """Stop listening for state changes."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
            logger.info("VoiceThreadBridge stopped")

    def dispatch(self, command: StateCommand) -> None:
        """Dispatch command to hub (thread-safe)."""
        self._hub.dispatch(command)

    def get_state(self) -> AppState:
        """Get current state snapshot."""
        return self._hub.get_state()

    def on_voice_state_changed(self, callback: Callable[[VoiceState], None]) -> Callable[[], None]:
        """
        Register callback for voice state changes.

        Args:
            callback: Function called with new VoiceState

        Returns:
            Unsubscribe function
        """
        with self._lock:
            self._voice_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._voice_callbacks:
                    self._voice_callbacks.remove(callback)

        return unsubscribe

    def on_playback_state_changed(
        self,
        callback: Callable[[bool, Any], None],
    ) -> Callable[[], None]:
        """
        Register callback for playback state changes.

        Callback receives (is_playing, now_playing).

        Args:
            callback: Function called with (is_playing, now_playing_item)

        Returns:
            Unsubscribe function
        """
        with self._lock:
            self._playback_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._playback_callbacks:
                    self._playback_callbacks.remove(callback)

        return unsubscribe

    def on_ducking_changed(self, callback: Callable[[bool], None]) -> Callable[[], None]:
        """
        Register callback for ducking state changes.

        Args:
            callback: Function called with is_ducked boolean

        Returns:
            Unsubscribe function
        """
        with self._lock:
            self._ducking_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._ducking_callbacks:
                    self._ducking_callbacks.remove(callback)

        return unsubscribe

    def _on_state_changed(self, old: AppState, new: AppState) -> None:
        """Filter to voice-relevant changes and notify callbacks."""
        # Voice state changed
        if old.voice != new.voice:
            with self._lock:
                voice_cbs = list(self._voice_callbacks)
            for cb in voice_cbs:
                try:
                    cb(new.voice)
                except Exception as e:
                    logger.error("Voice callback error: %s", e)

        # Ducking state changed
        if old.voice.is_ducked != new.voice.is_ducked:
            with self._lock:
                ducking_cbs = list(self._ducking_callbacks)
            for ducking_cb in ducking_cbs:
                try:
                    ducking_cb(new.voice.is_ducked)
                except Exception as e:
                    logger.error("Ducking callback error: %s", e)

        # Playback state changed
        old_room = old.active_room
        new_room = new.active_room
        playback_changed = False

        if old_room is None and new_room is None:
            pass
        elif (
            old_room is None
            or new_room is None
            or (
                old_room.player.is_playing != new_room.player.is_playing
                or old_room.player.now_playing != new_room.player.now_playing
            )
        ):
            playback_changed = True

        if playback_changed:
            with self._lock:
                playback_callbacks = list(self._playback_callbacks)
            is_playing = new_room.player.is_playing if new_room else False
            now_playing = new_room.player.now_playing if new_room else None
            for playback_cb in playback_callbacks:
                try:
                    playback_cb(is_playing, now_playing)
                except Exception as e:
                    logger.error("Playback callback error: %s", e)


# =============================================================================
# Backend Thread Bridge
# =============================================================================


class BackendThreadBridge:
    """
    Bridge for backend/API thread to StateHub.

    Provides easy access to common state operations for the backend.

    Usage:
        >>> bridge = BackendThreadBridge()
        >>> bridge.start()
        >>> # Update player state from backend
        >>> bridge.update_player_state(player_state)
        >>> bridge.stop()
    """

    def __init__(self, *, user_id: str | None = None) -> None:
        self._hub = get_state_hub(user_id=user_id)
        self.user_id = self._hub.user_id
        self._unsubscribe: Callable[[], None] | None = None
        self._player_callbacks: list[Callable[[], PlayerState]] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the bridge."""
        if self._unsubscribe is not None:
            return
        self._unsubscribe = self._hub.subscribe(self._on_state_changed)
        logger.info("BackendThreadBridge started")

    def stop(self) -> None:
        """Stop the bridge."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
            logger.info("BackendThreadBridge stopped")

    def dispatch(self, command: StateCommand) -> None:
        """Dispatch command to hub."""
        self._hub.dispatch(command)

    def get_state(self) -> AppState:
        """Get current state."""
        return self._hub.get_state()

    def update_player_state(self, player_state: PlayerState, room_id: str = "local") -> None:
        """
        Convenience method to update player state.

        Args:
            player_state: PlayerState instance
            room_id: Room to update (default: local)
        """
        from core.state_hub import UpdatePlayerState

        self._hub.dispatch(UpdatePlayerState(room_id=room_id, player_state=player_state))

    def set_backend_connected(self, connected: bool, url: str = "") -> None:
        """
        Convenience method to update backend connection status.

        Args:
            connected: Whether backend is connected
            url: Backend URL
        """
        from core.state_hub import SetBackendStatus
        from core.unified_state import ConnectionStatus

        status = ConnectionStatus.CONNECTED if connected else ConnectionStatus.DISCONNECTED
        self._hub.dispatch(SetBackendStatus(status=status, url=url))

    def on_player_state_requested(self, callback: Callable[[], PlayerState]) -> Callable[[], None]:
        """
        Register callback for when player state is needed.

        This allows the backend to push state updates on demand.

        Args:
            callback: Function that returns current PlayerState

        Returns:
            Unsubscribe function
        """
        with self._lock:
            self._player_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._player_callbacks:
                    self._player_callbacks.remove(callback)

        return unsubscribe

    def _on_state_changed(self, old: AppState, new: AppState) -> None:
        """Handle state changes (mostly for monitoring)."""
        pass  # Backend bridge is primarily for pushing state, not reacting


__all__ = [
    "BackendThreadBridge",
    "QtStateBridge",
    "VoiceThreadBridge",
]
