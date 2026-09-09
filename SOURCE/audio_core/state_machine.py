"""
Playback State Machine

Explicit finite state machine (FSM) for audio playback state management.
All state transitions are validated against explicit rules, with comprehensive
logging and event emission.

State Diagram:
    IDLE --[play]--> LOADING
    LOADING --[loaded]--> PLAYING
    LOADING --[error]--> ERROR
    PLAYING --[pause]--> PAUSED
    PLAYING --[stop]--> IDLE
    PLAYING --[end]--> IDLE
    PLAYING --[error]--> ERROR
    PLAYING --[skip]--> LOADING
    PAUSED --[resume]--> PLAYING
    PAUSED --[stop]--> IDLE
    PAUSED --[error]--> ERROR
    PAUSED --[skip]--> LOADING
    ERROR --[retry]--> LOADING
    ERROR --[stop]--> IDLE
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.events.bus import EventBus, LocalEventBus
from core.logging_config import get_logger

from .events import PlaybackStateTransition


class PlaybackState(Enum):
    """Playback state enumeration."""

    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"


class StateTransitionError(Exception):
    """Exception raised for invalid state transitions."""

    def __init__(
        self,
        from_state: PlaybackState,
        to_state: PlaybackState,
        trigger: str,
        reason: str,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.trigger = trigger
        self.reason = reason
        super().__init__(f"Invalid transition: {from_state.value} --[{trigger}]--> {to_state.value}: {reason}")


# Valid state transitions (from_state -> {trigger: to_state})
VALID_TRANSITIONS: dict[PlaybackState, dict[str, PlaybackState]] = {
    PlaybackState.IDLE: {
        "play": PlaybackState.LOADING,
        "stop": PlaybackState.IDLE,  # No-op
    },
    PlaybackState.LOADING: {
        "loaded": PlaybackState.PLAYING,
        "error": PlaybackState.ERROR,
        "stop": PlaybackState.IDLE,
        "cancel": PlaybackState.IDLE,
    },
    PlaybackState.PLAYING: {
        "pause": PlaybackState.PAUSED,
        "stop": PlaybackState.IDLE,
        "end": PlaybackState.IDLE,
        "error": PlaybackState.ERROR,
        "skip": PlaybackState.LOADING,  # Skip to next track
    },
    PlaybackState.PAUSED: {
        "resume": PlaybackState.PLAYING,
        "stop": PlaybackState.IDLE,
        "error": PlaybackState.ERROR,
        "skip": PlaybackState.LOADING,  # Allow skip while paused
    },
    PlaybackState.ERROR: {
        "retry": PlaybackState.LOADING,
        "stop": PlaybackState.IDLE,
        "clear": PlaybackState.IDLE,
    },
}


@dataclass(frozen=True, slots=True)
class StateTransition:
    """Record of a state transition."""

    from_state: PlaybackState
    to_state: PlaybackState
    trigger: str
    timestamp: float
    now_playing_id: str | None = None
    error_message: str | None = None


class PlaybackStateMachine:
    """
    Explicit FSM for playback state management.

    Features:
    - Validated state transitions (explicit FSM rules)
    - Thread-safe state access
    - Comprehensive logging and event emission
    - Transition history for diagnostics
    - Error recovery support

    Threading Model:
    - Uses threading.RLock for thread-safe access
    - State queries are lock-protected
    - Transitions are atomic operations
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize state machine.

        Args:
            event_bus: Event bus for emitting events (default: LocalEventBus)
            logger: Logger instance (default: creates new logger)
        """
        self._state = PlaybackState.IDLE
        self._now_playing_id: str | None = None
        self._error_message: str | None = None
        self._error_code: str | None = None

        # Threading
        import threading

        self._lock = threading.RLock()

        # Event bus and logging
        self._event_bus = event_bus or LocalEventBus()
        self._logger = logger or get_logger("audio_core.state_machine")

        # Transition history
        self._transition_history: list[StateTransition] = []
        self._transition_count = 0

    def _validate_transition(self, to_state: PlaybackState, trigger: str) -> None:
        """
        Validate state transition against FSM rules.

        Args:
            to_state: Target state
            trigger: Trigger name

        Raises:
            StateTransitionError: If transition is invalid
        """
        valid_targets = VALID_TRANSITIONS.get(self._state, {})
        expected_state = valid_targets.get(trigger)

        if expected_state is None:
            raise StateTransitionError(
                self._state,
                to_state,
                trigger,
                f"Trigger '{trigger}' not allowed from state '{self._state.value}'",
            )

        if expected_state != to_state:
            raise StateTransitionError(
                self._state,
                to_state,
                trigger,
                f"Expected state '{expected_state.value}' for trigger '{trigger}', got '{to_state.value}'",
            )

    def _transition(
        self,
        to_state: PlaybackState,
        trigger: str,
        now_playing_id: str | None = None,
        error_message: str | None = None,
        error_code: str | None = None,
    ) -> StateTransition:
        """
        Perform state transition (internal, assumes lock held).

        Args:
            to_state: Target state
            trigger: Trigger name
            now_playing_id: Optional track ID
            error_message: Optional error message
            error_code: Optional error code

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        # Validate transition
        self._validate_transition(to_state, trigger)

        # Record transition
        from_state = self._state
        transition = StateTransition(
            from_state=from_state,
            to_state=to_state,
            trigger=trigger,
            timestamp=time.time(),
            now_playing_id=now_playing_id or self._now_playing_id,
            error_message=error_message,
        )

        # Update state
        self._state = to_state
        if now_playing_id is not None:
            self._now_playing_id = now_playing_id
        if error_message is not None:
            self._error_message = error_message
        if error_code is not None:
            self._error_code = error_code
        elif to_state != PlaybackState.ERROR:
            # Clear error state when leaving ERROR
            self._error_message = None
            self._error_code = None

        # Record history
        self._transition_history.append(transition)
        self._transition_count += 1
        if len(self._transition_history) > 1000:
            self._transition_history.pop(0)

        # Log transition
        log_data = {
            "from_state": from_state.value,
            "to_state": to_state.value,
            "trigger": trigger,
            "now_playing_id": self._now_playing_id,
        }
        if error_message:
            log_data["error"] = error_message

        self._logger.info("State transition: %s", log_data)

        # Emit event
        try:
            self._event_bus.publish(
                PlaybackStateTransition(
                    from_state=from_state.value,
                    to_state=to_state.value,
                    trigger=trigger,
                    now_playing_id=self._now_playing_id,
                    error=error_message,
                    source="audio_core.state_machine",
                )
            )
        except Exception as e:
            self._logger.warning("Failed to emit state transition event: %s", e)

        # Emit error event if applicable
        if to_state == PlaybackState.ERROR and error_message:
            try:
                from .events import PlaybackError

                self._event_bus.publish(
                    PlaybackError(
                        error_code=error_code or "unknown",
                        error_message=error_message,
                        track_id=self._now_playing_id,
                        recoverable=True,
                        source="audio_core.state_machine",
                    )
                )
            except Exception as e:
                self._logger.warning("Failed to emit error event: %s", e)

        return transition

    # ========== State Transition Methods ==========

    def play(self, track_id: str | None = None) -> StateTransition:
        """
        Transition to LOADING state (start playback).

        Args:
            track_id: Optional track ID

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.LOADING, "play", now_playing_id=track_id)

    def loaded(self) -> StateTransition:
        """
        Transition from LOADING to PLAYING (track loaded successfully).

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.PLAYING, "loaded")

    def pause(self) -> StateTransition:
        """
        Transition from PLAYING to PAUSED.

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.PAUSED, "pause")

    def resume(self) -> StateTransition:
        """
        Transition from PAUSED to PLAYING.

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.PLAYING, "resume")

    def stop(self) -> StateTransition:
        """
        Transition to IDLE (stop playback).

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.IDLE, "stop")

    def end(self) -> StateTransition:
        """
        Transition from PLAYING to IDLE (track ended naturally).

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.IDLE, "end")

    def skip(self) -> StateTransition:
        """
        Transition from PLAYING or PAUSED to LOADING (skip to next track).

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.LOADING, "skip")

    def error(self, error_message: str, error_code: str = "unknown", recoverable: bool = True) -> StateTransition:
        """
        Transition to ERROR state.

        Args:
            error_message: Error message
            error_code: Error code
            recoverable: Whether error is recoverable

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(
                PlaybackState.ERROR,
                "error",
                error_message=error_message,
                error_code=error_code,
            )

    def retry(self) -> StateTransition:
        """
        Transition from ERROR to LOADING (retry playback).

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.LOADING, "retry")

    def cancel(self) -> StateTransition:
        """
        Transition from LOADING to IDLE (cancel loading).

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.IDLE, "cancel")

    def clear_error(self) -> StateTransition:
        """
        Transition from ERROR to IDLE (clear error state).

        Returns:
            StateTransition record

        Raises:
            StateTransitionError: If transition is invalid
        """
        with self._lock:
            return self._transition(PlaybackState.IDLE, "clear")

    # ========== State Query Methods ==========

    def get_state(self) -> PlaybackState:
        """Get current state (thread-safe)."""
        with self._lock:
            return self._state

    def get_now_playing_id(self) -> str | None:
        """Get current track ID (thread-safe)."""
        with self._lock:
            return self._now_playing_id

    def get_error(self) -> tuple[str, str] | None:
        """
        Get error information (thread-safe).

        Returns:
            Tuple of (error_code, error_message) or None
        """
        with self._lock:
            if self._error_code and self._error_message:
                return (self._error_code, self._error_message)
            return None

    def is_idle(self) -> bool:
        """Check if in IDLE state (thread-safe)."""
        with self._lock:
            return self._state == PlaybackState.IDLE

    def is_loading(self) -> bool:
        """Check if in LOADING state (thread-safe)."""
        with self._lock:
            return self._state == PlaybackState.LOADING

    def is_playing(self) -> bool:
        """Check if in PLAYING state (thread-safe)."""
        with self._lock:
            return self._state == PlaybackState.PLAYING

    def is_paused(self) -> bool:
        """Check if in PAUSED state (thread-safe)."""
        with self._lock:
            return self._state == PlaybackState.PAUSED

    def is_error(self) -> bool:
        """Check if in ERROR state (thread-safe)."""
        with self._lock:
            return self._state == PlaybackState.ERROR

    def can_transition(self, trigger: str) -> bool:
        """
        Check if a transition is valid from current state (thread-safe).

        Args:
            trigger: Trigger name

        Returns:
            True if transition is valid
        """
        with self._lock:
            valid_targets = VALID_TRANSITIONS.get(self._state, {})
            return trigger in valid_targets

    def get_valid_transitions(self) -> set[str]:
        """
        Get set of valid triggers from current state (thread-safe).

        Returns:
            Set of valid trigger names
        """
        with self._lock:
            valid_targets = VALID_TRANSITIONS.get(self._state, {})
            return set(valid_targets.keys())

    # ========== Diagnostics ==========

    def get_transition_history(self, limit: int = 100) -> list[StateTransition]:
        """
        Get recent transition history (for diagnostics).

        Args:
            limit: Maximum number of transitions to return

        Returns:
            List of StateTransition records
        """
        with self._lock:
            return list(self._transition_history[-limit:])

    def get_state_info(self) -> dict[str, Any]:
        """
        Get current state information (for diagnostics).

        Returns:
            Dictionary with state information
        """
        with self._lock:
            return {
                "state": self._state.value,
                "now_playing_id": self._now_playing_id,
                "error_code": self._error_code,
                "error_message": self._error_message,
                "transition_count": self._transition_count,
                "valid_transitions": list(self.get_valid_transitions()),
            }
