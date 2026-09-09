"""
Playback State Machine.

Single source of truth for playback phase. Every state change goes through
PlaybackStateMachine.transition(). Derived properties replace the legacy
boolean flags (_is_playing, _paused, _user_paused).

Created as part of P0-2 fix (5-flag state divergence elimination).
"""

from __future__ import annotations

from enum import Enum, auto

from core.logging_config import get_logger

logger = get_logger(__name__)


class PlaybackPhase(Enum):
    """Authoritative playback phase enum."""

    IDLE = auto()  # Nothing loaded, nothing playing
    LOADING = auto()  # Track resolved, backend starting
    PLAYING = auto()  # Audio actively playing
    PAUSED = auto()  # Audio paused, can resume
    STOPPED = auto()  # Explicitly stopped by user (track may still be in memory)
    ERROR = auto()  # Playback failed


class InvalidTransitionError(Exception):
    """Raised when a state transition is not valid."""

    def __init__(self, current: PlaybackPhase, target: PlaybackPhase) -> None:
        self.current = current
        self.target = target
        super().__init__("Invalid transition: %s -> %s" % (current.name, target.name))


# Valid transitions: from_phase -> set of allowed target phases
VALID_TRANSITIONS: dict[PlaybackPhase, frozenset[PlaybackPhase]] = {
    PlaybackPhase.IDLE: frozenset({PlaybackPhase.LOADING, PlaybackPhase.ERROR}),
    PlaybackPhase.LOADING: frozenset(
        {
            PlaybackPhase.PLAYING,
            PlaybackPhase.ERROR,
            PlaybackPhase.IDLE,  # cancelled
            PlaybackPhase.STOPPED,  # user stopped during load
        }
    ),
    PlaybackPhase.PLAYING: frozenset(
        {
            PlaybackPhase.PAUSED,
            PlaybackPhase.STOPPED,
            PlaybackPhase.LOADING,  # new track
            PlaybackPhase.IDLE,  # track ended naturally
            PlaybackPhase.ERROR,
        }
    ),
    PlaybackPhase.PAUSED: frozenset(
        {
            PlaybackPhase.PLAYING,  # resume
            PlaybackPhase.STOPPED,
            PlaybackPhase.LOADING,  # new track
            PlaybackPhase.IDLE,
            PlaybackPhase.ERROR,
        }
    ),
    PlaybackPhase.STOPPED: frozenset(
        {
            PlaybackPhase.LOADING,  # new play
            PlaybackPhase.IDLE,
        }
    ),
    PlaybackPhase.ERROR: frozenset(
        {
            PlaybackPhase.LOADING,  # retry
            PlaybackPhase.IDLE,  # reset
        }
    ),
}


class PlaybackStateMachine:
    """Single source of truth for playback phase.

    All state changes go through :meth:`transition`.  The old boolean
    flags are replaced by derived read-only properties.
    """

    __slots__ = ("_phase", "_user_initiated_pause")

    def __init__(self) -> None:
        self._phase: PlaybackPhase = PlaybackPhase.IDLE
        self._user_initiated_pause: bool = False

    # -- Transition -----------------------------------------------------------

    def transition(
        self,
        new_phase: PlaybackPhase,
        *,
        user_initiated: bool = False,
        force: bool = False,
    ) -> PlaybackPhase:
        """Move to *new_phase*.

        Parameters
        ----------
        new_phase:
            Target phase.
        user_initiated:
            Only meaningful when transitioning to PAUSED — records that the
            pause was requested by the user (as opposed to a system pause).
        force:
            If ``True``, allow the transition even if it is not listed in
            ``VALID_TRANSITIONS``.  A warning is logged.  This is an escape
            hatch for legacy code paths that will be cleaned up later.

        Returns
        -------
        The new phase after the transition.

        Raises
        ------
        InvalidTransitionError
            If the transition is not valid and *force* is ``False``.
        """
        old = self._phase

        if old == new_phase:
            # Same-state "transition" is always allowed (idempotent).
            if new_phase == PlaybackPhase.PAUSED:
                self._user_initiated_pause = user_initiated
            return self._phase

        allowed = VALID_TRANSITIONS.get(old, frozenset())
        if new_phase not in allowed:
            if force:
                logger.warning(
                    "Forced transition %s -> %s (not in VALID_TRANSITIONS)",
                    old.name,
                    new_phase.name,
                )
            else:
                raise InvalidTransitionError(old, new_phase)

        self._phase = new_phase

        # Track user-initiated pause
        if new_phase == PlaybackPhase.PAUSED:
            self._user_initiated_pause = user_initiated
        elif new_phase != PlaybackPhase.PAUSED:
            # Leaving PAUSED clears the flag
            if old == PlaybackPhase.PAUSED:
                pass  # keep _user_initiated_pause for inspection during transition
            if new_phase in (
                PlaybackPhase.PLAYING,
                PlaybackPhase.LOADING,
                PlaybackPhase.IDLE,
                PlaybackPhase.STOPPED,
            ):
                self._user_initiated_pause = False

        logger.debug("Phase %s -> %s", old.name, new_phase.name)
        return self._phase

    # -- Derived properties ---------------------------------------------------

    @property
    def phase(self) -> PlaybackPhase:
        """Current phase."""
        return self._phase

    @property
    def is_playing(self) -> bool:
        """``True`` when audio is actively playing."""
        return self._phase == PlaybackPhase.PLAYING

    @property
    def is_paused(self) -> bool:
        """``True`` when audio is paused."""
        return self._phase == PlaybackPhase.PAUSED

    @property
    def is_idle(self) -> bool:
        """``True`` when nothing is loaded."""
        return self._phase == PlaybackPhase.IDLE

    @property
    def is_loading(self) -> bool:
        """``True`` when a track is being loaded."""
        return self._phase == PlaybackPhase.LOADING

    @property
    def is_stopped(self) -> bool:
        """``True`` when playback has been explicitly stopped."""
        return self._phase == PlaybackPhase.STOPPED

    @property
    def is_error(self) -> bool:
        """``True`` when playback is in error state."""
        return self._phase == PlaybackPhase.ERROR

    @property
    def user_initiated_pause(self) -> bool:
        """``True`` when the last pause was user-initiated."""
        return self._user_initiated_pause
