"""
State Selectors - Read-only accessors for StateHub state.

AI Instructions
===============
Selectors provide derived/computed state from the StateHub.
Components should use selectors rather than accessing state directly.
This enables memoization and prevents components from depending on state structure.

Usage:
    >>> from core.state_selectors import select_is_playing, select_now_playing
    >>>
    >>> if select_is_playing():
    ...     track = select_now_playing()
    ...     logger.info("Playing: %s", track.title)

Benefits:
    - Encapsulates state structure (components don't need to know internals)
    - Enables future memoization/caching
    - Provides type-safe access
    - Simplifies testing (mock selectors instead of entire state)

Related Modules:
    - core/state_hub.py: Central state coordinator
    - core/unified_state.py: State dataclass definitions
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.state_hub import get_state_hub
from core.unified_state import (
    AppState,
    ConnectionStatus,
    PlaybackMode,
    RoomState,
    VoiceMode,
    VoiceState,
)

if TYPE_CHECKING:
    from models.player import PlayerState, QueueItem


# =============================================================================
# Raw State Access
# =============================================================================


def select_state() -> AppState:
    """
    Get complete state snapshot.

    Use specific selectors when possible - this is mainly for debugging.
    """
    return get_state_hub().get_state()


def select_version() -> int:
    """Get current state version number."""
    return get_state_hub().get_state().version


def select_session_id() -> str:
    """Get current session identifier."""
    return get_state_hub().get_state().session_id


# =============================================================================
# Playback Selectors
# =============================================================================


def select_is_playing() -> bool:
    """Is music currently playing in the active room?"""
    state = get_state_hub().get_state()
    room = state.active_room
    return room is not None and room.player.is_playing


def select_now_playing() -> QueueItem | None:
    """Get currently playing track in active room."""
    state = get_state_hub().get_state()
    room = state.active_room
    return room.player.now_playing if room else None


def select_queue() -> list[QueueItem]:
    """Get queue for active room."""
    state = get_state_hub().get_state()
    room = state.active_room
    return list(room.player.queue) if room else []


def select_queue_length() -> int:
    """Get number of items in active room queue."""
    state = get_state_hub().get_state()
    room = state.active_room
    return len(room.player.queue) if room else 0


def select_volume() -> int:
    """Get volume for active room."""
    state = get_state_hub().get_state()
    room = state.active_room
    return room.player.volume if room else state.preferences.volume


def select_position() -> int:
    """Get current playback position in seconds."""
    state = get_state_hub().get_state()
    room = state.active_room
    return room.player.position if room else 0


def select_duration() -> int:
    """Get current track duration in seconds."""
    state = get_state_hub().get_state()
    room = state.active_room
    return room.player.duration if room else 0


def select_position_percentage() -> float:
    """Get playback position as percentage (0.0-1.0)."""
    state = get_state_hub().get_state()
    room = state.active_room
    return room.player.position_percentage if room else 0.0


def select_player_state() -> PlayerState | None:
    """Get full player state for active room."""
    state = get_state_hub().get_state()
    room = state.active_room
    return room.player if room else None


def select_playback_backend() -> str | None:
    """Get the active playback backend identifier."""
    state = get_state_hub().get_state()
    room = state.active_room
    return room.player.backend if room else None


# =============================================================================
# Voice Selectors
# =============================================================================


def select_voice_state() -> VoiceState:
    """Get complete voice state."""
    return get_state_hub().get_state().voice


def select_voice_mode() -> VoiceMode:
    """Get current voice mode."""
    return get_state_hub().get_state().voice.mode


def select_is_listening() -> bool:
    """Is voice system listening for commands?"""
    return get_state_hub().get_state().voice.mode == VoiceMode.LISTENING


def select_is_processing() -> bool:
    """Is voice system processing a command?"""
    return get_state_hub().get_state().voice.mode == VoiceMode.PROCESSING


def select_is_speaking() -> bool:
    """Is TTS currently speaking?"""
    return get_state_hub().get_state().voice.mode == VoiceMode.SPEAKING


def select_voice_idle() -> bool:
    """Is voice system idle?"""
    return get_state_hub().get_state().voice.mode == VoiceMode.IDLE


def select_is_ducked() -> bool:
    """Is audio currently ducked?"""
    return get_state_hub().get_state().voice.is_ducked


def select_wake_enabled() -> bool:
    """Is wake word detection configured/enabled?"""
    return get_state_hub().get_state().voice.wake_enabled


def select_wake_active() -> bool:
    """Is wake word detector actively running?"""
    return get_state_hub().get_state().voice.wake_active


def select_last_transcript() -> str:
    """Get the most recent STT transcription."""
    return get_state_hub().get_state().voice.last_transcript


def select_last_confidence() -> float:
    """Get the confidence of the last transcription."""
    return get_state_hub().get_state().voice.confidence


# =============================================================================
# Connection Selectors
# =============================================================================


def select_backend_connected() -> bool:
    """Is backend connected?"""
    return get_state_hub().get_state().connection.backend_status == ConnectionStatus.CONNECTED


def select_backend_status() -> ConnectionStatus:
    """Get backend connection status."""
    return get_state_hub().get_state().connection.backend_status


def select_backend_url() -> str:
    """Get backend URL."""
    return get_state_hub().get_state().connection.backend_url


def select_websocket_connected() -> bool:
    """Is WebSocket connected?"""
    return get_state_hub().get_state().connection.websocket_connected


def select_connection_failures() -> int:
    """Get number of consecutive connection failures."""
    return get_state_hub().get_state().connection.consecutive_failures


def select_last_health_check() -> float:
    """Get timestamp of last successful health check."""
    return get_state_hub().get_state().connection.last_health_check


# =============================================================================
# UI Selectors
# =============================================================================


def select_command_inflight() -> bool:
    """Is a command currently being processed?"""
    return get_state_hub().get_state().ui.command_inflight


def select_current_view() -> str:
    """Get current UI view name."""
    return get_state_hub().get_state().ui.current_view


def select_settings_open() -> bool:
    """Is settings dialog open?"""
    return get_state_hub().get_state().ui.settings_open


def select_history_open() -> bool:
    """Is history dialog open?"""
    return get_state_hub().get_state().ui.history_open


def select_last_error() -> str:
    """Get last error message."""
    return get_state_hub().get_state().ui.last_error


def select_toast_message() -> str:
    """Get current toast notification message."""
    return get_state_hub().get_state().ui.toast_message


# =============================================================================
# Preferences Selectors
# =============================================================================


def select_default_volume() -> int:
    """Get default volume preference."""
    return get_state_hub().get_state().preferences.volume


def select_shuffle_enabled() -> bool:
    """Is shuffle enabled?"""
    return get_state_hub().get_state().preferences.shuffle


def select_repeat_mode() -> str:
    """Get repeat mode (off, all, one)."""
    return get_state_hub().get_state().preferences.repeat_mode


def select_autoplay_enabled() -> bool:
    """Is autoplay enabled?"""
    return get_state_hub().get_state().preferences.autoplay_enabled


def select_playback_mode() -> PlaybackMode:
    """Get playback mode (freeform/playlist)."""
    return get_state_hub().get_state().preferences.playback_mode


def select_is_playlist_mode() -> bool:
    """Is in playlist mode?"""
    return get_state_hub().get_state().preferences.playback_mode == PlaybackMode.PLAYLIST


def select_wake_word() -> str:
    """Get configured wake word."""
    return get_state_hub().get_state().preferences.wake_word


def select_tts_voice() -> str:
    """Get selected TTS voice."""
    return get_state_hub().get_state().preferences.tts_voice


# =============================================================================
# Multi-Room Selectors
# =============================================================================


def select_room_ids() -> list[str]:
    """Get all room IDs."""
    return list(get_state_hub().get_state().rooms.keys())


def select_room_count() -> int:
    """Get number of registered rooms."""
    return len(get_state_hub().get_state().rooms)


def select_active_room_id() -> str:
    """Get active room ID."""
    return get_state_hub().get_state().active_room_id


def select_active_room() -> RoomState | None:
    """Get active room state."""
    return get_state_hub().get_state().active_room


def select_local_room() -> RoomState | None:
    """Get local (primary) room state."""
    return get_state_hub().get_state().local_room


def select_room_state(room_id: str) -> RoomState | None:
    """Get state for specific room."""
    return get_state_hub().get_state().rooms.get(room_id)


def select_room_name(room_id: str) -> str | None:
    """Get display name for a room."""
    room = get_state_hub().get_state().rooms.get(room_id)
    return room.room_name if room else None


def select_room_is_playing(room_id: str) -> bool:
    """Is a specific room playing?"""
    room = get_state_hub().get_state().rooms.get(room_id)
    return room is not None and room.player.is_playing


def select_all_rooms() -> list[RoomState]:
    """Get all room states."""
    return list(get_state_hub().get_state().rooms.values())


def select_remote_rooms() -> list[RoomState]:
    """Get all non-primary (remote) rooms."""
    return [r for r in get_state_hub().get_state().rooms.values() if not r.is_primary]


# =============================================================================
# Derived/Computed Selectors
# =============================================================================


def select_any_room_playing() -> bool:
    """Is any room currently playing music?"""
    return any(r.player.is_playing for r in get_state_hub().get_state().rooms.values())


def select_total_queue_items() -> int:
    """Get total queue items across all rooms."""
    return sum(len(r.player.queue) for r in get_state_hub().get_state().rooms.values())


def select_voice_system_busy() -> bool:
    """Is the voice system busy (not idle)?"""
    mode = get_state_hub().get_state().voice.mode
    return mode != VoiceMode.IDLE


def select_can_accept_voice_command() -> bool:
    """Can the system accept a new voice command?"""
    state = get_state_hub().get_state()
    return (
        state.voice.mode == VoiceMode.IDLE
        and not state.ui.command_inflight
        and state.connection.backend_status == ConnectionStatus.CONNECTED
    )


def select_playback_summary() -> dict[str, object]:
    """Get a summary of current playback state for UI display."""
    state = get_state_hub().get_state()
    room = state.active_room

    if not room:
        return {
            "playing": False,
            "track": None,
            "position": 0,
            "duration": 0,
            "volume": state.preferences.volume,
            "queue_length": 0,
        }

    track = room.player.now_playing
    return {
        "playing": room.player.is_playing,
        "track": track.title if track else None,
        "artist": track.artist if track else None,
        "artwork_url": track.artwork_url if track else None,
        "position": room.player.position,
        "duration": room.player.duration,
        "position_percentage": room.player.position_percentage,
        "volume": room.player.volume,
        "queue_length": len(room.player.queue),
        "backend": room.player.backend,
    }


def select_voice_summary() -> dict[str, object]:
    """Get a summary of voice state for UI display."""
    state = get_state_hub().get_state()

    return {
        "mode": state.voice.mode.value,
        "wake_enabled": state.voice.wake_enabled,
        "wake_active": state.voice.wake_active,
        "is_ducked": state.voice.is_ducked,
        "last_transcript": state.voice.last_transcript,
        "command_inflight": state.ui.command_inflight,
    }


def select_connection_summary() -> dict[str, object]:
    """Get a summary of connection state for UI display."""
    state = get_state_hub().get_state()

    return {
        "backend_status": state.connection.backend_status.value,
        "backend_url": state.connection.backend_url,
        "websocket_connected": state.connection.websocket_connected,
        "consecutive_failures": state.connection.consecutive_failures,
        "healthy": state.connection.backend_status == ConnectionStatus.CONNECTED,
    }


def get_active_timers() -> list[dict[str, object]]:
    """Return all active (non-expired) timers as a list of dicts.

    Each dict includes at least ``name`` and ``remaining_seconds`` keys.
    Returns an empty list if the timer service is unavailable or no timers
    are running.
    """
    try:
        from services.timer_service import get_timer_service

        service = get_timer_service()
        return [t.to_dict() for t in service.get_all_timers() if not t.is_expired]
    except Exception:
        return []


__all__ = [
    "get_active_timers",
    "select_active_room",
    "select_active_room_id",
    "select_all_rooms",
    # Derived
    "select_any_room_playing",
    "select_autoplay_enabled",
    # Connection
    "select_backend_connected",
    "select_backend_status",
    "select_backend_url",
    "select_can_accept_voice_command",
    # UI
    "select_command_inflight",
    "select_connection_failures",
    "select_connection_summary",
    "select_current_view",
    # Preferences
    "select_default_volume",
    "select_duration",
    "select_history_open",
    "select_is_ducked",
    "select_is_listening",
    # Playback
    "select_is_playing",
    "select_is_playlist_mode",
    "select_is_processing",
    "select_is_speaking",
    "select_last_confidence",
    "select_last_error",
    "select_last_health_check",
    "select_last_transcript",
    "select_local_room",
    "select_now_playing",
    "select_playback_backend",
    "select_playback_mode",
    "select_playback_summary",
    "select_player_state",
    "select_position",
    "select_position_percentage",
    "select_queue",
    "select_queue_length",
    "select_remote_rooms",
    "select_repeat_mode",
    "select_room_count",
    # Multi-room
    "select_room_ids",
    "select_room_is_playing",
    "select_room_name",
    "select_room_state",
    "select_session_id",
    "select_settings_open",
    "select_shuffle_enabled",
    # Raw access
    "select_state",
    "select_toast_message",
    "select_total_queue_items",
    "select_tts_voice",
    "select_version",
    "select_voice_idle",
    "select_voice_mode",
    # Voice
    "select_voice_state",
    "select_voice_summary",
    "select_voice_system_busy",
    "select_volume",
    "select_wake_active",
    "select_wake_enabled",
    "select_wake_word",
    "select_websocket_connected",
]
