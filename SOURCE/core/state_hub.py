"""
StateHub - Per-user state coordinator using actor model.

AI Instructions
===============
StateHub is the coordinator for one user's immutable application snapshot.
All state mutations MUST go through dispatch(). Never modify state directly.

Usage:
    >>> from core.state_hub import get_state_hub, UpdateVoiceMode
    >>> from core.unified_state import VoiceMode
    >>>
    >>> hub = get_state_hub(user_id="user-123")
    >>> hub.dispatch(UpdateVoiceMode(mode=VoiceMode.LISTENING, user_id="user-123"))
    >>> state = hub.get_state()
    >>> state.voice.mode  # VoiceMode.LISTENING

Threading Model:
    - dispatch() is thread-safe, can be called from any thread
    - get_state() returns immutable snapshot, safe to read from any thread
    - State mutations happen on single worker thread (no races)
    - Subscribers are called from worker thread (bridge to Qt thread if needed)

Related Modules:
    - core/unified_state.py: State dataclass definitions
    - core/state_selectors.py: Read-only state accessors
    - core/thread_bridges.py: Qt and voice thread bridges
"""

from __future__ import annotations

import importlib
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, cast

from core.constants import TIMEOUT_DEFAULT, TIMEOUT_SHORT
from core.unified_state import (
    AppState,
    ConnectionStatus,
    PlaybackMode,
    RoomState,
    VoiceMode,
    create_initial_state,
)
from models.player import PlayerState


class _LoggerLike(Protocol):
    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def info(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def warning(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def error(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def exception(self, msg: str, *args: object, **kwargs: object) -> None: ...


def _get_logger() -> _LoggerLike:
    """Import the structured logger lazily to keep mypy focused on this module."""
    logging_config = importlib.import_module("core.logging_config")
    get_logger = cast(Callable[[str | None], _LoggerLike], logging_config.get_logger)
    return get_logger(__name__)


logger = _get_logger()


def _is_cloud_surface() -> bool:
    try:
        from config.settings import settings
    except (ImportError, RuntimeError, AttributeError, ValueError):
        return False
    surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    deployment = str(getattr(settings, "deployment_mode", "") or "").strip().lower()
    return surface == "cloud" or deployment == "cloud"


def _normalize_user_id(value: str | None, *, source: str) -> str:
    user_id = str(value or "").strip()
    if not user_id:
        raise ValueError("StateHub requires a non-empty user_id from %s" % source)
    return user_id


def _resolve_user_id(user_id: str | None = None) -> str:
    """Resolve the StateHub owner from explicit or ambient identity."""

    if user_id is not None:
        return _normalize_user_id(user_id, source="explicit user_id")
    try:
        from core.user_context import get_current_user_id

        return _normalize_user_id(get_current_user_id(), source="current user context")
    except (LookupError, ValueError) as exc:
        if _is_cloud_surface():
            raise RuntimeError("StateHub requires authenticated user_id on cloud surface") from exc
    try:
        from core.user_context import get_device_user_id

        return _normalize_user_id(get_device_user_id(), source="device user_id")
    except Exception as exc:
        raise RuntimeError("StateHub requires user_id or desktop device user_id") from exc


# =============================================================================
# State Commands (Mutations)
# =============================================================================


@dataclass(frozen=True, kw_only=True)
class StateCommand:
    """Base class for state mutations. All commands are immutable."""

    user_id: str | None = None


# --- Voice Commands ---


@dataclass(frozen=True)
class UpdateVoiceMode(StateCommand):
    """Update voice mode (idle, listening, processing, speaking)."""

    mode: VoiceMode


@dataclass(frozen=True)
class SetWakeEnabled(StateCommand):
    """Enable or disable wake word detection."""

    enabled: bool


@dataclass(frozen=True)
class SetWakeActive(StateCommand):
    """Set whether wake word detector is actively running."""

    active: bool


@dataclass(frozen=True)
class SetDucked(StateCommand):
    """Set audio ducking state."""

    is_ducked: bool


@dataclass(frozen=True)
class SetLastTranscript(StateCommand):
    """Update the last STT transcription."""

    transcript: str
    confidence: float = 0.0


# --- Room Commands ---


@dataclass(frozen=True)
class AddRoom(StateCommand):
    """Add a new room to the state."""

    room_id: str
    room_name: str
    is_primary: bool = False


@dataclass(frozen=True)
class RemoveRoom(StateCommand):
    """Remove a room from the state."""

    room_id: str


@dataclass(frozen=True)
class SetActiveRoom(StateCommand):
    """Set the active room."""

    room_id: str


@dataclass(frozen=True)
class RenameRoom(StateCommand):
    """Rename an existing room (atomic, preserves all transient state)."""

    room_id: str
    new_name: str


@dataclass(frozen=True)
class UpdatePlayerState(StateCommand):
    """Update player state for a specific room."""

    room_id: str
    player_state: PlayerState


@dataclass(frozen=True)
class SyncRoomState(StateCommand):
    """Sync complete room state (from remote room)."""

    room_id: str
    player_state: PlayerState
    sync_time: float | None = None


# --- Connection Commands ---


@dataclass(frozen=True)
class SetBackendStatus(StateCommand):
    """Update backend connection status."""

    status: ConnectionStatus
    url: str = ""


@dataclass(frozen=True)
class SetWebSocketConnected(StateCommand):
    """Update WebSocket connection status."""

    connected: bool


@dataclass(frozen=True)
class RecordHealthCheck(StateCommand):
    """Record a successful health check."""

    timestamp: float | None = None


@dataclass(frozen=True)
class RecordConnectionFailure(StateCommand):
    """Record a connection failure."""

    pass


@dataclass(frozen=True)
class ResetConnectionFailures(StateCommand):
    """Reset connection failure counter."""

    pass


# --- UI Commands ---


@dataclass(frozen=True)
class SetCommandInflight(StateCommand):
    """Set whether a command is currently being processed."""

    inflight: bool


@dataclass(frozen=True)
class SetCurrentView(StateCommand):
    """Set the current UI view."""

    view: str


@dataclass(frozen=True)
class SetSettingsOpen(StateCommand):
    """Set whether settings dialog is open."""

    open: bool


@dataclass(frozen=True)
class SetHistoryOpen(StateCommand):
    """Set whether history dialog is open."""

    open: bool


@dataclass(frozen=True)
class SetLastError(StateCommand):
    """Set the last error message."""

    error: str


@dataclass(frozen=True)
class SetToastMessage(StateCommand):
    """Set toast notification message."""

    message: str


# --- Preferences Commands ---


@dataclass(frozen=True)
class SetVolume(StateCommand):
    """Set the default volume preference."""

    volume: int


@dataclass(frozen=True)
class SetShuffle(StateCommand):
    """Set shuffle preference."""

    shuffle: bool


@dataclass(frozen=True)
class SetRepeatMode(StateCommand):
    """Set repeat mode preference."""

    mode: str  # "off", "all", "one"


@dataclass(frozen=True)
class SetAutoplay(StateCommand):
    """Set autoplay preference."""

    enabled: bool


@dataclass(frozen=True)
class SetPlaybackMode(StateCommand):
    """Set playback mode (freeform/playlist)."""

    mode: PlaybackMode


# --- Batch Commands ---


@dataclass(frozen=True)
class BatchCommands(StateCommand):
    """Execute multiple commands atomically."""

    commands: tuple[StateCommand, ...]


# =============================================================================
# Command Handler Functions (for type registry pattern)
# =============================================================================


def _apply_update_voice_mode(state: AppState, cmd: UpdateVoiceMode) -> AppState:
    """Handler for UpdateVoiceMode command."""
    new_voice = replace(state.voice, mode=cmd.mode)
    return replace(state, voice=new_voice)


def _apply_set_wake_enabled(state: AppState, cmd: SetWakeEnabled) -> AppState:
    """Handler for SetWakeEnabled command."""
    new_voice = replace(state.voice, wake_enabled=cmd.enabled)
    return replace(state, voice=new_voice)


def _apply_set_wake_active(state: AppState, cmd: SetWakeActive) -> AppState:
    """Handler for SetWakeActive command."""
    new_voice = replace(state.voice, wake_active=cmd.active)
    return replace(state, voice=new_voice)


def _apply_set_ducked(state: AppState, cmd: SetDucked) -> AppState:
    """Handler for SetDucked command."""
    if state.voice.is_ducked == cmd.is_ducked:
        return state  # No change - caller will not bump version
    new_voice = replace(state.voice, is_ducked=cmd.is_ducked)
    return replace(state, voice=new_voice)


def _apply_set_last_transcript(state: AppState, cmd: SetLastTranscript) -> AppState:
    """Handler for SetLastTranscript command."""
    new_voice = replace(state.voice, last_transcript=cmd.transcript, confidence=cmd.confidence)
    return replace(state, voice=new_voice)


def _apply_add_room(state: AppState, cmd: AddRoom) -> AppState:
    """Handler for AddRoom command."""
    if cmd.room_id in state.rooms:
        logger.warning("Room already exists", room_id=cmd.room_id)
        return state
    new_room = RoomState(room_id=cmd.room_id, room_name=cmd.room_name, is_primary=cmd.is_primary)
    rooms = dict(state.rooms)
    rooms[cmd.room_id] = new_room
    return replace(state, rooms=rooms)


def _apply_remove_room(state: AppState, cmd: RemoveRoom) -> AppState:
    """Handler for RemoveRoom command."""
    if cmd.room_id not in state.rooms:
        return state
    if state.rooms[cmd.room_id].is_primary:
        logger.warning("Cannot remove primary room", room_id=cmd.room_id)
        return state
    rooms = dict(state.rooms)
    del rooms[cmd.room_id]
    active = state.active_room_id if state.active_room_id != cmd.room_id else "local"
    return replace(state, rooms=rooms, active_room_id=active)


def _apply_set_active_room(state: AppState, cmd: SetActiveRoom) -> AppState:
    """Handler for SetActiveRoom command."""
    if cmd.room_id not in state.rooms:
        logger.warning("Cannot set active room - not found", room_id=cmd.room_id)
        return state
    if state.active_room_id == cmd.room_id:
        return state  # No change
    return replace(state, active_room_id=cmd.room_id)


def _apply_rename_room(state: AppState, cmd: RenameRoom) -> AppState:
    """Handler for RenameRoom command."""
    if cmd.room_id not in state.rooms:
        logger.warning("Cannot rename room - not found", room_id=cmd.room_id)
        return state
    old_room = state.rooms[cmd.room_id]
    if old_room.room_name == cmd.new_name:
        return state  # No change
    rooms = dict(state.rooms)
    rooms[cmd.room_id] = replace(old_room, room_name=cmd.new_name)
    return replace(state, rooms=rooms)


def _apply_update_player_state(state: AppState, cmd: UpdatePlayerState) -> AppState:
    """Handler for UpdatePlayerState command."""
    if cmd.room_id not in state.rooms:
        logger.warning("Room not found for player update", room_id=cmd.room_id)
        return state
    rooms = dict(state.rooms)
    old_room = rooms[cmd.room_id]
    rooms[cmd.room_id] = replace(old_room, player=cmd.player_state)
    return replace(state, rooms=rooms)


def _apply_sync_room_state(state: AppState, cmd: SyncRoomState) -> AppState:
    """Handler for SyncRoomState command."""
    sync_time = cmd.sync_time if cmd.sync_time else time.time()
    rooms = dict(state.rooms)
    if cmd.room_id not in state.rooms:
        new_room = RoomState(
            room_id=cmd.room_id,
            room_name=f"Room {cmd.room_id}",
            player=cmd.player_state,
            is_primary=False,
            last_sync=sync_time,
        )
        rooms[cmd.room_id] = new_room
    else:
        old_room = rooms[cmd.room_id]
        rooms[cmd.room_id] = replace(old_room, player=cmd.player_state, last_sync=sync_time)
    return replace(state, rooms=rooms)


def _apply_set_backend_status(state: AppState, cmd: SetBackendStatus) -> AppState:
    """Handler for SetBackendStatus command."""
    new_conn = replace(
        state.connection,
        backend_status=cmd.status,
        backend_url=cmd.url if cmd.url else state.connection.backend_url,
    )
    return replace(state, connection=new_conn)


def _apply_set_websocket_connected(state: AppState, cmd: SetWebSocketConnected) -> AppState:
    """Handler for SetWebSocketConnected command."""
    new_conn = replace(state.connection, websocket_connected=cmd.connected)
    return replace(state, connection=new_conn)


def _apply_record_health_check(state: AppState, cmd: RecordHealthCheck) -> AppState:
    """Handler for RecordHealthCheck command."""
    ts = cmd.timestamp if cmd.timestamp else time.time()
    new_conn = replace(state.connection, last_health_check=ts, consecutive_failures=0)
    return replace(state, connection=new_conn)


def _apply_record_connection_failure(state: AppState, cmd: RecordConnectionFailure) -> AppState:
    """Handler for RecordConnectionFailure command."""
    new_conn = replace(state.connection, consecutive_failures=state.connection.consecutive_failures + 1)
    return replace(state, connection=new_conn)


def _apply_reset_connection_failures(state: AppState, cmd: ResetConnectionFailures) -> AppState:
    """Handler for ResetConnectionFailures command."""
    new_conn = replace(state.connection, consecutive_failures=0)
    return replace(state, connection=new_conn)


def _apply_set_command_inflight(state: AppState, cmd: SetCommandInflight) -> AppState:
    """Handler for SetCommandInflight command."""
    new_ui = replace(state.ui, command_inflight=cmd.inflight)
    return replace(state, ui=new_ui)


def _apply_set_current_view(state: AppState, cmd: SetCurrentView) -> AppState:
    """Handler for SetCurrentView command."""
    new_ui = replace(state.ui, current_view=cmd.view)
    return replace(state, ui=new_ui)


def _apply_set_settings_open(state: AppState, cmd: SetSettingsOpen) -> AppState:
    """Handler for SetSettingsOpen command."""
    new_ui = replace(state.ui, settings_open=cmd.open)
    return replace(state, ui=new_ui)


def _apply_set_history_open(state: AppState, cmd: SetHistoryOpen) -> AppState:
    """Handler for SetHistoryOpen command."""
    new_ui = replace(state.ui, history_open=cmd.open)
    return replace(state, ui=new_ui)


def _apply_set_last_error(state: AppState, cmd: SetLastError) -> AppState:
    """Handler for SetLastError command."""
    new_ui = replace(state.ui, last_error=cmd.error)
    return replace(state, ui=new_ui)


def _apply_set_toast_message(state: AppState, cmd: SetToastMessage) -> AppState:
    """Handler for SetToastMessage command."""
    new_ui = replace(state.ui, toast_message=cmd.message)
    return replace(state, ui=new_ui)


def _apply_set_volume(state: AppState, cmd: SetVolume) -> AppState:
    """Handler for SetVolume command."""
    clamped = max(0, min(100, cmd.volume))
    new_prefs = replace(state.preferences, volume=clamped)
    return replace(state, preferences=new_prefs)


def _apply_set_shuffle(state: AppState, cmd: SetShuffle) -> AppState:
    """Handler for SetShuffle command."""
    new_prefs = replace(state.preferences, shuffle=cmd.shuffle)
    return replace(state, preferences=new_prefs)


def _apply_set_repeat_mode(state: AppState, cmd: SetRepeatMode) -> AppState:
    """Handler for SetRepeatMode command."""
    if cmd.mode not in ("off", "all", "one"):
        logger.warning("Invalid repeat mode", mode=cmd.mode)
        return state
    new_prefs = replace(state.preferences, repeat_mode=cmd.mode)
    return replace(state, preferences=new_prefs)


def _apply_set_autoplay(state: AppState, cmd: SetAutoplay) -> AppState:
    """Handler for SetAutoplay command."""
    new_prefs = replace(state.preferences, autoplay_enabled=cmd.enabled)
    return replace(state, preferences=new_prefs)


def _apply_set_playback_mode(state: AppState, cmd: SetPlaybackMode) -> AppState:
    """Handler for SetPlaybackMode command."""
    new_prefs = replace(state.preferences, playback_mode=cmd.mode)
    return replace(state, preferences=new_prefs)


# Command handler type registry - maps command types to handler functions
# Note: Each handler is typed for its specific command subclass but we store them generically
_COMMAND_HANDLERS: dict[type[StateCommand], Callable[..., AppState]] = {
    UpdateVoiceMode: _apply_update_voice_mode,
    SetWakeEnabled: _apply_set_wake_enabled,
    SetWakeActive: _apply_set_wake_active,
    SetDucked: _apply_set_ducked,
    SetLastTranscript: _apply_set_last_transcript,
    AddRoom: _apply_add_room,
    RemoveRoom: _apply_remove_room,
    SetActiveRoom: _apply_set_active_room,
    RenameRoom: _apply_rename_room,
    UpdatePlayerState: _apply_update_player_state,
    SyncRoomState: _apply_sync_room_state,
    SetBackendStatus: _apply_set_backend_status,
    SetWebSocketConnected: _apply_set_websocket_connected,
    RecordHealthCheck: _apply_record_health_check,
    RecordConnectionFailure: _apply_record_connection_failure,
    ResetConnectionFailures: _apply_reset_connection_failures,
    SetCommandInflight: _apply_set_command_inflight,
    SetCurrentView: _apply_set_current_view,
    SetSettingsOpen: _apply_set_settings_open,
    SetHistoryOpen: _apply_set_history_open,
    SetLastError: _apply_set_last_error,
    SetToastMessage: _apply_set_toast_message,
    SetVolume: _apply_set_volume,
    SetShuffle: _apply_set_shuffle,
    SetRepeatMode: _apply_set_repeat_mode,
    SetAutoplay: _apply_set_autoplay,
    SetPlaybackMode: _apply_set_playback_mode,
}


# =============================================================================
# State Subscriber Types
# =============================================================================

StateCallback = Callable[[AppState, AppState], None]
"""Callback signature: (old_state, new_state) -> None"""


# =============================================================================
# StateHub Implementation
# =============================================================================


class StateHub:
    """
    Central state coordinator using actor model.

    Features:
    - Single worker thread processes all state mutations (no races)
    - Immutable state snapshots for thread-safe reads
    - Command queue for ordered state updates
    - Subscriber notification for reactive components

    Thread Safety:
    - dispatch() is thread-safe (uses queue)
    - get_state() is thread-safe (returns immutable copy)
    - Subscribers called from worker thread (use bridges for Qt)
    """

    def __init__(
        self,
        initial_state: AppState | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """
        Initialize the state hub.

        Args:
            initial_state: Optional initial state (creates default if None)
            session_id: Optional session ID (generates UUID if None)
            user_id: Owner of this StateHub
        """
        if session_id is None:
            session_id = str(uuid.uuid4())[:8]
        initial_user_id = getattr(initial_state, "user_id", None) if initial_state is not None else None
        resolved_user_id = _resolve_user_id(user_id if user_id is not None else initial_user_id or None)
        self.user_id = resolved_user_id

        if initial_state is None:
            self._state = create_initial_state(session_id=session_id, user_id=resolved_user_id)
        else:
            self._state = replace(initial_state, session_id=session_id, user_id=resolved_user_id)

        self._lock = threading.Lock()
        self._command_queue: queue.Queue[StateCommand | None] = queue.Queue()
        self._subscribers: list[StateCallback] = []
        self._running = False
        self._worker_thread: threading.Thread | None = None
        self._started_event = threading.Event()

    @property
    def is_running(self) -> bool:
        """Whether the StateHub worker thread is accepting commands."""
        return self._running

    def start(self) -> None:
        """Start the state hub worker thread."""
        if self._running:
            logger.debug("StateHub already running")
            return

        self._running = True
        self._worker_thread = threading.Thread(
            target=self._process_commands,
            daemon=True,
            name="StateHub-Worker-%s" % self.user_id,
        )
        self._worker_thread.start()
        self._started_event.wait(timeout=TIMEOUT_DEFAULT)  # Wait for worker to initialize
        logger.info("StateHub started", session_id=self._state.session_id, user_id=self.user_id)

    def stop(self, timeout: float = 2.0) -> None:
        """
        Stop the state hub.

        Args:
            timeout: Maximum time to wait for worker thread to stop
        """
        if not self._running:
            return

        self._running = False
        self._command_queue.put(None)  # Sentinel to unblock queue.get()

        if self._worker_thread:
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning("StateHub worker thread did not stop in time")
            else:
                logger.info("StateHub stopped")

    def dispatch(self, command: StateCommand) -> bool:
        """
        Dispatch a state mutation command.

        Thread-safe - can be called from any thread.
        Commands are processed in order by the worker thread.

        Args:
            command: The state command to execute

        Returns:
            True when the command was accepted by the worker queue.
        """
        if not self._running:
            logger.error("StateHub not running, refusing command", command=type(command).__name__)
            raise RuntimeError("StateHub not running")
        self._command_queue.put(self._bind_command_user(command))
        return True

    def dispatch_sync(self, command: StateCommand, timeout: float = 1.0) -> AppState:
        """
        Dispatch a command and wait for it to be processed.

        Useful for testing or when you need to ensure the command completed.

        Args:
            command: The state command to execute
            timeout: Maximum time to wait for processing

        Returns:
            The new state after the command was processed
        """
        if not self._running:
            raise RuntimeError("StateHub not running")

        # Use an event to signal completion
        completed = threading.Event()
        result_state: list[AppState] = []

        def on_complete(old: AppState, new: AppState) -> None:
            result_state.append(new)
            completed.set()

        # Temporarily subscribe
        with self._lock:
            self._subscribers.append(on_complete)

        try:
            self._command_queue.put(self._bind_command_user(command))
            if completed.wait(timeout=timeout):
                return result_state[0] if result_state else self._state
            logger.warning("dispatch_sync timed out")
            return self._state
        finally:
            with self._lock:
                if on_complete in self._subscribers:
                    self._subscribers.remove(on_complete)

    def get_state(self) -> AppState:
        """
        Get current state snapshot.

        Thread-safe - returns immutable snapshot that's safe to
        read from any thread without synchronization.

        Returns:
            Current AppState (immutable)
        """
        with self._lock:
            return self._state

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        """
        Subscribe to state changes.

        Callback receives (old_state, new_state) on each change.
        Callbacks are invoked from the worker thread.

        Args:
            callback: Function called with (old_state, new_state)

        Returns:
            Unsubscribe function - call it to stop receiving updates
        """
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _process_commands(self) -> None:
        """Worker thread that processes state mutations."""
        self._started_event.set()
        logger.debug("StateHub worker started")

        while self._running:
            try:
                command = self._command_queue.get(timeout=TIMEOUT_SHORT)
                if command is None:  # Sentinel for shutdown
                    continue

                old_state = self._state
                new_state = self._apply_command(old_state, command)

                if new_state is not old_state:
                    with self._lock:
                        self._state = new_state
                        subscribers = list(self._subscribers)

                    # Notify subscribers outside lock
                    self._notify_subscribers(subscribers, old_state, new_state)

            except queue.Empty:
                continue
            except Exception as e:
                logger.exception("StateHub error processing command", error=str(e))

        logger.debug("StateHub worker stopped")

    def _notify_subscribers(
        self,
        subscribers: list[StateCallback],
        old_state: AppState,
        new_state: AppState,
    ) -> None:
        """Notify all subscribers of state change."""
        for callback in subscribers:
            try:
                callback(old_state, new_state)
            except Exception as e:
                logger.error(
                    "State subscriber error",
                    error=str(e),
                    callback=(callback.__name__ if hasattr(callback, "__name__") else str(callback)),
                )

    def _bind_command_user(self, command: StateCommand) -> StateCommand:
        command_user_id = self.user_id
        if command.user_id is not None:
            command_user_id = _normalize_user_id(command.user_id, source="%s.user_id" % type(command).__name__)
        if command_user_id != self.user_id:
            raise ValueError(
                "StateHub command user_id %s does not match hub user_id %s" % (command_user_id, self.user_id)
            )
        if isinstance(command, BatchCommands):
            bound_commands = tuple(self._bind_command_user(cmd) for cmd in command.commands)
            if command.user_id == self.user_id and bound_commands == command.commands:
                return command
            return replace(command, commands=bound_commands, user_id=self.user_id)
        if command.user_id == self.user_id:
            return command
        return replace(command, user_id=self.user_id)

    def _apply_command(self, state: AppState, command: StateCommand) -> AppState:
        """
        Apply a command and return new state (or same if no change).

        This is the reducer - all state mutations happen here.
        Uses a type registry pattern for clean dispatch.
        """
        command = self._bind_command_user(command)

        # Handle BatchCommands specially (recursive)
        if isinstance(command, BatchCommands):
            current = state
            for cmd in command.commands:
                current = self._apply_command(current, cmd)
            # Batch increments version only once (from original version)
            if current is not state:
                return replace(current, version=state.version + 1)
            return current

        # Look up handler in type registry
        handler = _COMMAND_HANDLERS.get(type(command))
        if handler is None:
            logger.warning("Unknown command type", command_type=type(command).__name__)
            return state

        # Apply handler and bump version if state changed
        new_state = handler(state, command)
        if new_state is state:
            return state  # No change, don't bump version
        return replace(new_state, version=state.version + 1)


# =============================================================================
# User-Partitioned Hub Registry
# =============================================================================

_HUBS_BY_USER: dict[str, StateHub] = {}
_HUBS_LOCK = threading.Lock()


def get_state_hub(*, user_id: str | None = None) -> StateHub:
    """
    Get or create the current user's state hub.

    The hub is lazily initialized on first access and automatically started.
    Cloud surfaces require an authenticated user_id; desktop startup may fall
    back to the stable device user id.

    Returns:
        The current user's StateHub instance
    """
    resolved_user_id = _resolve_user_id(user_id)
    with _HUBS_LOCK:
        hub = _HUBS_BY_USER.get(resolved_user_id)
        if hub is None:
            hub = StateHub(user_id=resolved_user_id)
            hub.start()
            _HUBS_BY_USER[resolved_user_id] = hub
        return hub


def reset_state_hub(*, user_id: str | None = None) -> None:
    """
    Reset one user's state hub, or all hubs when no user_id is supplied.

    This is primarily for tests and controlled teardown.
    """
    hubs: list[StateHub]
    with _HUBS_LOCK:
        if user_id is None:
            hubs = list(_HUBS_BY_USER.values())
            _HUBS_BY_USER.clear()
        else:
            hub = _HUBS_BY_USER.pop(_resolve_user_id(user_id), None)
            hubs = [hub] if hub is not None else []
    for hub in hubs:
        hub.stop()


def init_state_hub(session_id: str | None = None, *, user_id: str | None = None) -> StateHub:
    """
    Initialize the current user's state hub with custom settings.

    Call this during application startup before any components access state.

    Args:
        session_id: Optional session identifier

    Returns:
        The initialized StateHub
    """
    resolved_user_id = _resolve_user_id(user_id)
    old_hub: StateHub | None = None
    with _HUBS_LOCK:
        old_hub = _HUBS_BY_USER.get(resolved_user_id)
        hub = StateHub(session_id=session_id, user_id=resolved_user_id)
        hub.start()
        _HUBS_BY_USER[resolved_user_id] = hub
    if old_hub is not None:
        old_hub.stop()
    return hub


__all__ = [
    # Room commands
    "AddRoom",
    # Batch
    "BatchCommands",
    "RecordConnectionFailure",
    "RecordHealthCheck",
    "RemoveRoom",
    "RenameRoom",
    "ResetConnectionFailures",
    "SetActiveRoom",
    "SetAutoplay",
    # Connection commands
    "SetBackendStatus",
    # UI commands
    "SetCommandInflight",
    "SetCurrentView",
    "SetDucked",
    "SetHistoryOpen",
    "SetLastError",
    "SetLastTranscript",
    "SetPlaybackMode",
    "SetRepeatMode",
    "SetSettingsOpen",
    "SetShuffle",
    "SetToastMessage",
    # Preferences commands
    "SetVolume",
    "SetWakeActive",
    "SetWakeEnabled",
    "SetWebSocketConnected",
    # Base command
    "StateCommand",
    # Hub
    "StateHub",
    "SyncRoomState",
    "UpdatePlayerState",
    # Voice commands
    "UpdateVoiceMode",
    "get_state_hub",
    "init_state_hub",
    "reset_state_hub",
]
