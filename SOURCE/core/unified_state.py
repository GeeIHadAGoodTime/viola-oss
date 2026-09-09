"""
Unified State Definitions - immutable StateHub snapshots for one user.

AI Instructions
===============
This module defines immutable state dataclasses for one user's StateHub view.
All state mutations go through the owning StateHub; components read via selectors.

Usage:
    >>> from core.unified_state import AppState, VoiceMode, VoiceState
    >>> state = AppState(user_id="user-123")
    >>> state.voice.mode  # VoiceMode.IDLE
    >>> state.active_room  # Get currently active room

Architecture:
    - All state is immutable (frozen dataclasses)
    - Updates create new instances via dataclasses.replace()
    - StateHub maintains the canonical per-user version
    - Components subscribe to state changes

Related Modules:
    - core/state_hub.py: Central state coordinator
    - core/state_selectors.py: Read-only state accessors
    - core/thread_bridges.py: Thread-safe bridges for Qt/voice threads
    - models/player.py: PlayerState, QueueItem definitions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from models.player import PlayerState


class VoiceMode(Enum):
    """Voice system operational mode."""

    IDLE = "idle"  # Not actively listening
    LISTENING = "listening"  # STT active, capturing audio
    PROCESSING = "processing"  # Command being processed
    SPEAKING = "speaking"  # TTS active
    CONVERSING = "conversing"  # Waiting for follow-up speech (between turns)


class ConnectionStatus(Enum):
    """Backend connection status."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class PlaybackMode(Enum):
    """
    Playback mode for queue population.

    FREEFORM: AI-driven autoplay fills queue with recommendations
    PLAYLIST: Drawing from a cached playlist, transitions to FREEFORM when exhausted
    """

    FREEFORM = "freeform"
    PLAYLIST = "playlist"


@dataclass(frozen=True)
class VoiceState:
    """
    Immutable voice state snapshot.

    Attributes:
        mode: Current voice mode (idle, listening, processing, speaking)
        wake_enabled: Whether wake word detection is configured
        wake_active: Whether wake word detector is currently running
        last_transcript: Most recent STT transcription result
        is_ducked: Whether audio is currently ducked
        duck_level: Target volume level when ducked (0-100)
        confidence: Last STT confidence score (0.0-1.0)
    """

    mode: VoiceMode = VoiceMode.IDLE
    wake_enabled: bool = False
    wake_active: bool = False
    last_transcript: str = ""
    is_ducked: bool = False
    duck_level: int = 20
    confidence: float = 0.0


@dataclass(frozen=True)
class RoomState:
    """
    State for a single room/zone (supports multi-room synchronization).

    Attributes:
        room_id: Unique identifier for this room
        room_name: Human-readable display name
        player: PlayerState containing playback state
        is_primary: Whether this is the local device's room
        last_sync: Unix timestamp of last state synchronization
    """

    room_id: str
    room_name: str
    player: PlayerState = field(default_factory=PlayerState)
    is_primary: bool = False
    last_sync: float = 0.0


@dataclass(frozen=True)
class ConnectionState:
    """
    Backend/network connection state.

    Attributes:
        backend_status: Current connection status
        backend_url: URL of the connected backend
        last_health_check: Unix timestamp of last successful health check
        consecutive_failures: Number of consecutive connection failures
        websocket_connected: Whether WebSocket is connected
    """

    backend_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    backend_url: str = ""
    last_health_check: float = 0.0
    consecutive_failures: int = 0
    websocket_connected: bool = False


@dataclass(frozen=True)
class UIState:
    """
    UI-specific state (not persisted across restarts).

    Attributes:
        current_view: Current UI view/tab name
        settings_open: Whether settings dialog is open
        history_open: Whether history dialog is open
        command_inflight: Whether a voice command is being processed
        last_error: Most recent error message for display
        toast_message: Current toast notification message
    """

    current_view: str = "main"
    settings_open: bool = False
    history_open: bool = False
    command_inflight: bool = False
    last_error: str = ""
    toast_message: str = ""


@dataclass(frozen=True)
class PreferencesState:
    """
    User preferences state (persisted across restarts).

    Attributes:
        volume: Default volume level (0-100)
        shuffle: Whether shuffle is enabled
        repeat_mode: Repeat mode ("off", "all", "one")
        autoplay_enabled: Whether autoplay is enabled
        playback_mode: Queue population mode (freeform/playlist)
        wake_word: Configured wake word
        tts_voice: Selected TTS voice
    """

    volume: int = 80
    shuffle: bool = False
    repeat_mode: str = "off"
    autoplay_enabled: bool = True
    playback_mode: PlaybackMode = PlaybackMode.FREEFORM
    wake_word: str = "Viola"
    tts_voice: str = "default"


@dataclass(frozen=True)
class AppState:
    """
    Complete immutable StateHub snapshot for one user.

    This is immutable. All updates create new instances.
    The owning StateHub maintains the canonical version and broadcasts changes.

    Attributes:
        rooms: Dict of room_id -> RoomState for multi-room support
        active_room_id: ID of the currently selected/active room
        voice: Voice system state
        connection: Backend connection state
        ui: UI-specific state
        preferences: User preferences
        version: Monotonically increasing version for change detection
        user_id: Owner of this per-user state snapshot
        session_id: Unique identifier for this app session

    Usage:
        >>> state = AppState(user_id="user-123")
        >>> state.active_room  # Property returns active RoomState or None
        >>> state.local_room   # Property returns primary/local RoomState
    """

    # Multi-room support: Dict[room_id, RoomState]
    rooms: dict[str, RoomState] = field(default_factory=dict)
    active_room_id: str = "local"

    # Voice state for this StateHub owner
    voice: VoiceState = field(default_factory=VoiceState)

    # Connection state
    connection: ConnectionState = field(default_factory=ConnectionState)

    # UI state
    ui: UIState = field(default_factory=UIState)

    # User preferences
    preferences: PreferencesState = field(default_factory=PreferencesState)

    # Monotonic version for change detection
    version: int = 0

    # User identifier for this per-user StateHub snapshot
    user_id: str = ""

    # Session identifier
    session_id: str = ""

    @property
    def active_room(self) -> RoomState | None:
        """Get the currently active room state."""
        return self.rooms.get(self.active_room_id)

    @property
    def local_room(self) -> RoomState | None:
        """Get the local (primary) room state."""
        for room in self.rooms.values():
            if room.is_primary:
                return room
        return None

    @property
    def is_playing(self) -> bool:
        """Check if music is currently playing in the active room."""
        room = self.active_room
        return room is not None and room.player.is_playing

    @property
    def current_volume(self) -> int:
        """Get volume for active room, falling back to preferences."""
        room = self.active_room
        if room:
            return room.player.volume
        return self.preferences.volume


def create_initial_state(
    session_id: str = "",
    user_id: str = "",
    room_name: str = "This Device",
) -> AppState:
    """
    Create initial application state with local room registered.

    Args:
        session_id: Unique identifier for this session
        user_id: Owner of this state snapshot
        room_name: Display name for the local room

    Returns:
        Initialized AppState with local room
    """
    local_room = RoomState(
        room_id="local",
        room_name=room_name,
        player=PlayerState(),
        is_primary=True,
    )

    return AppState(
        rooms={"local": local_room},
        active_room_id="local",
        user_id=user_id,
        session_id=session_id,
    )


def room_to_dict(room: RoomState) -> dict[str, Any]:
    """Convert RoomState to dictionary for serialization."""
    return {
        "room_id": room.room_id,
        "room_name": room.room_name,
        "player": room.player.model_dump(),
        "is_primary": room.is_primary,
        "last_sync": room.last_sync,
    }


def state_to_dict(state: AppState) -> dict[str, Any]:
    """
    Convert AppState to dictionary for debugging/logging.

    Note: This is for debugging only. Use selectors for component access.
    """
    return {
        "version": state.version,
        "user_id": state.user_id,
        "session_id": state.session_id,
        "active_room_id": state.active_room_id,
        "rooms": {rid: room_to_dict(r) for rid, r in state.rooms.items()},
        "voice": {
            "mode": state.voice.mode.value,
            "wake_enabled": state.voice.wake_enabled,
            "wake_active": state.voice.wake_active,
            "is_ducked": state.voice.is_ducked,
        },
        "connection": {
            "status": state.connection.backend_status.value,
            "url": state.connection.backend_url,
            "websocket_connected": state.connection.websocket_connected,
        },
        "ui": {
            "current_view": state.ui.current_view,
            "command_inflight": state.ui.command_inflight,
        },
    }


__all__ = [
    "AppState",
    "ConnectionState",
    "ConnectionStatus",
    "PlaybackMode",
    "PreferencesState",
    "RoomState",
    "UIState",
    "VoiceMode",
    "VoiceState",
    "create_initial_state",
    "room_to_dict",
    "state_to_dict",
]
