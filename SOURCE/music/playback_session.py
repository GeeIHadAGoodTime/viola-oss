"""
Playback Session Controller for Viola.

Manages playlist sessions and playback modes for simulating native playlist functionality
on providers like YouTube where native playlist APIs aren't accessible.

This module provides:
- PlaylistSession: Cached playlist state with shuffle support
- PlaybackSessionController: Orchestrates playlist mode, repeat mode, and autoplay transitions

Design:
- One-time playlist fetch when user says "play my playlist"
- Cached songs are shuffled using OS entropy for true randomness each session
- When playlist exhausts, silent transition to AI-driven autoplay
- Respects repeat modes (OFF, ALL, ONE) like real music players
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger
from models.state_manager import ConsolidatedState, PlaybackMode, RepeatMode

if TYPE_CHECKING:
    from music.autoplay_controller import AutoplayController
    from music.playlist_manager import PlaylistManager

logger = get_logger(__name__)


@dataclass
class PlaylistSession:
    """
    Cached playlist state for simulated playlist playback.

    Attributes:
        playlist_name: Name of the playlist being played
        original_tracks: Full list of tracks from the playlist (immutable reference)
        remaining_tracks: Shuffled copy of tracks yet to be played
    """

    playlist_name: str
    original_tracks: list[JsonDict]
    remaining_tracks: list[JsonDict] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Shuffle remaining tracks on initialization."""
        self._reshuffle()

    def _reshuffle(self) -> None:
        """
        Shuffle remaining tracks using OS entropy.

        Uses SystemRandom for cryptographic randomness - no seeding needed,
        guarantees different order every time even in same second.
        """
        self.remaining_tracks = self.original_tracks.copy()
        random.SystemRandom().shuffle(self.remaining_tracks)
        logger.debug(
            "Shuffled playlist '%s': %d tracks",
            self.playlist_name,
            len(self.remaining_tracks),
        )

    def pop_next(self, count: int = 1) -> list[JsonDict]:
        """
        Pop next N tracks from remaining.

        Args:
            count: Number of tracks to pop

        Returns:
            List of track dicts (may be fewer than count if exhausted)
        """
        result = self.remaining_tracks[:count]
        self.remaining_tracks = self.remaining_tracks[count:]
        return result

    def peek_next(self, count: int = 1) -> list[JsonDict]:
        """
        Peek at next N tracks without removing them.

        Args:
            count: Number of tracks to peek

        Returns:
            List of track dicts
        """
        return self.remaining_tracks[:count]

    def is_exhausted(self) -> bool:
        """Check if all playlist tracks have been played."""
        return len(self.remaining_tracks) == 0

    def remaining_count(self) -> int:
        """Get number of remaining tracks."""
        return len(self.remaining_tracks)

    def total_count(self) -> int:
        """Get total number of tracks in playlist."""
        return len(self.original_tracks)

    def reset(self, reshuffle: bool = True) -> None:
        """
        Reset playlist to beginning.

        Args:
            reshuffle: If True, reshuffle tracks (default). If False, maintain original order.
        """
        if reshuffle:
            self._reshuffle()
        else:
            self.remaining_tracks = self.original_tracks.copy()
        logger.info(
            "Reset playlist '%s': %d tracks (reshuffle=%s)",
            self.playlist_name,
            len(self.remaining_tracks),
            reshuffle,
        )

    def get_ai_seed_context(self, sample_size: int = 10) -> list[JsonDict]:
        """
        Get sample of playlist tracks for AI recommendation context.

        Args:
            sample_size: Maximum number of tracks to sample

        Returns:
            Random sample of original tracks for AI seeding
        """
        actual_size = min(sample_size, len(self.original_tracks))
        if actual_size == 0:
            return []
        return random.SystemRandom().sample(self.original_tracks, actual_size)


class PlaybackSessionController:
    """
    Manages playback mode, repeat mode, and bridges playlist/autoplay logic.

    This controller provides the "illusion" of native playlist support for providers
    like YouTube by:
    1. Caching playlist tracks locally when user starts a playlist
    2. Drawing from the cache to fill queue
    3. Transitioning silently to AI autoplay when playlist exhausts

    Thread Safety:
        Methods are generally thread-safe as they operate on immutable state or
        use the underlying state manager's locking.

    Multi-tenant note:
        Playback session state is desktop-only — there is one audio output
        device per machine, so a single ``PlaybackSessionController`` serves
        one active listener at a time.  Cloud playlist data is per-user-keyed
        in ``PlaylistManager`` and the cloud playlist routes; this controller
        intentionally holds the *current* session for the locally signed-in
        user and is not multi-tenant by design.
    """  # mt-ok: desktop single-listener controller

    def __init__(
        self,
        autoplay_controller: AutoplayController | None = None,
        playlist_manager: PlaylistManager | None = None,
        state_manager: ConsolidatedState | None = None,
    ) -> None:
        """
        Initialize PlaybackSessionController.

        Args:
            autoplay_controller: AutoplayController for AI-driven queue filling
            playlist_manager: PlaylistManager for fetching playlist tracks
            state_manager: ConsolidatedState for repeat/playback mode state
        """
        self._autoplay = autoplay_controller
        self._playlist_mgr = playlist_manager
        self._state_mgr = state_manager
        self._playlist_session: PlaylistSession | None = None
        self._logger = get_logger(__name__ + ".session")

    # ------------------------------------------------------------------ #
    # Playlist Session Management
    # ------------------------------------------------------------------ #

    async def start_playlist(self, playlist_name: str, shuffle: bool = True) -> bool:
        """
        Start a playlist session.

        Fetches all tracks from the playlist, caches them, and sets mode to PLAYLIST.

        Args:
            playlist_name: Name of the playlist to play
            shuffle: Whether to shuffle the playlist (default True)

        Returns:
            True if playlist started successfully, False otherwise
        """
        if self._playlist_mgr is None:
            self._logger.error("Cannot start playlist: PlaylistManager not available")
            return False

        self._logger.info("Starting playlist session: '%s' (shuffle=%s)", playlist_name, shuffle)

        try:
            # Fetch ALL tracks from playlist (not progressive - we want full list)
            videos = await self._playlist_mgr.get_playlist_videos(
                playlist_name,
                limit=500,  # Fetch full playlist
                shuffle=shuffle,
                progressive=False,
            )

            videos_value = to_json_value(videos)
            videos_json: list[JsonDict] = []
            if isinstance(videos_value, list):
                for entry in videos_value:
                    if isinstance(entry, dict):
                        videos_json.append(entry)

            if not videos_json:
                self._logger.warning("Playlist '%s' is empty or not found", playlist_name)
                return False

            # Create playlist session with shuffled tracks
            self._playlist_session = PlaylistSession(
                playlist_name=playlist_name,
                original_tracks=videos_json,
            )

            # Set playback mode to PLAYLIST
            if self._state_mgr is not None:
                self._state_mgr.set_playback_mode(PlaybackMode.PLAYLIST)

            self._logger.info(
                "Playlist session started: '%s' with %d tracks",
                playlist_name,
                len(videos_json),
            )
            return True

        except Exception as exc:
            self._logger.exception(
                "Failed to start playlist '%s': %s",
                playlist_name,
                exc,
            )
            return False

    def exit_playlist_mode(self) -> None:
        """Exit playlist mode and return to freeform autoplay."""
        self._playlist_session = None
        if self._state_mgr is not None:
            self._state_mgr.set_playback_mode(PlaybackMode.FREEFORM)
        self._logger.info("Exited playlist mode, now in FREEFORM mode")

    def get_playlist_session(self) -> PlaylistSession | None:
        """Get current playlist session if any."""
        return self._playlist_session

    def is_playlist_mode(self) -> bool:
        """Check if currently in playlist mode."""
        if self._state_mgr is not None:
            return self._state_mgr.is_playlist_mode()
        return self._playlist_session is not None

    def get_playlist_status(self) -> JsonDict:
        """
        Get current playlist status for UI display.

        Returns:
            Dict with playlist info or empty dict if not in playlist mode
        """
        if self._playlist_session is None:
            return {}

        return {
            "playlist_name": self._playlist_session.playlist_name,
            "remaining_tracks": self._playlist_session.remaining_count(),
            "total_tracks": self._playlist_session.total_count(),
            "is_exhausted": self._playlist_session.is_exhausted(),
        }

    # ------------------------------------------------------------------ #
    # Repeat Mode Management
    # ------------------------------------------------------------------ #

    def get_repeat_mode(self) -> RepeatMode:
        """Get current repeat mode."""
        if self._state_mgr is not None:
            return self._state_mgr.get_repeat_mode()
        return RepeatMode.OFF

    def set_repeat_mode(self, mode: RepeatMode) -> RepeatMode:
        """Set repeat mode."""
        if self._state_mgr is not None:
            return self._state_mgr.set_repeat_mode(mode)
        return mode

    def cycle_repeat_mode(self) -> RepeatMode:
        """Cycle through repeat modes: OFF → ALL → ONE → OFF."""
        if self._state_mgr is not None:
            new_mode = self._state_mgr.cycle_repeat_mode()
            self._logger.info("Cycled repeat mode to: %s", new_mode.value)
            return new_mode
        return RepeatMode.OFF

    def should_repeat_current(self) -> bool:
        """Check if current track should repeat (Repeat One mode)."""
        if self._state_mgr is not None:
            return self._state_mgr.should_repeat_current()
        return False

    def should_loop_queue(self) -> bool:
        """Check if queue should loop (Repeat All mode)."""
        if self._state_mgr is not None:
            return self._state_mgr.should_loop_queue()
        return False

    # ------------------------------------------------------------------ #
    # Queue Fill Logic
    # ------------------------------------------------------------------ #

    def fill_queue_from_playlist(self, needed: int) -> list[JsonDict]:
        """
        Fill queue from playlist cache.

        Args:
            needed: Number of tracks needed

        Returns:
            List of track dicts to add to queue
        """
        if self._playlist_session is None:
            return []

        tracks = self._playlist_session.pop_next(needed)

        if self._playlist_session.is_exhausted():
            if self.should_loop_queue():
                # Repeat All: Reset playlist and continue
                self._playlist_session.reset(reshuffle=True)
                self._logger.info(
                    "Playlist '%s' looping (Repeat All mode)",
                    self._playlist_session.playlist_name,
                )
                # Get more tracks if we didn't get enough
                if len(tracks) < needed:
                    tracks.extend(self._playlist_session.pop_next(needed - len(tracks)))
            else:
                # Playlist exhausted, transition to freeform
                self._transition_to_freeform()

        return tracks

    def _transition_to_freeform(self) -> None:
        """
        Silent transition from PLAYLIST to FREEFORM mode.

        The playlist session is kept for AI context seeding.
        """
        self._logger.info(
            "Playlist '%s' exhausted, transitioning to FREEFORM mode",
            (self._playlist_session.playlist_name if self._playlist_session else "unknown"),
        )
        if self._state_mgr is not None:
            self._state_mgr.set_playback_mode(PlaybackMode.FREEFORM)

    def get_ai_seed_context(self) -> list[JsonDict]:
        """
        Get seed songs for AI recommendations.

        First tries active playlist session, then falls back to default playlist
        for cold-start autoplay scenarios.

        Returns:
            Sample of playlist tracks for AI seeding
        """
        # First, try active playlist session
        if self._playlist_session is not None:
            return self._playlist_session.get_ai_seed_context(sample_size=10)

        # Fallback: try to get tracks from default playlist for cold-start
        if self._playlist_mgr is not None:
            try:
                default_name = self._playlist_mgr.get_default_playlist()
                if not default_name:
                    # No default, try first available
                    playlists = self._playlist_mgr.list_playlists()
                    if playlists:
                        default_name = next(iter(playlists.keys()))

                if default_name:
                    # Get cached tracks from playlist manager (private method)
                    cached = self._playlist_mgr._get_cached_tracks(default_name)
                    cached_value = to_json_value(cached) if cached is not None else []
                    cached_json: list[JsonDict] = []
                    if isinstance(cached_value, list):
                        for entry in cached_value:
                            if isinstance(entry, dict):
                                cached_json.append(entry)

                    if cached_json:
                        sample_size = min(10, len(cached_json))
                        sample = random.SystemRandom().sample(cached_json, sample_size)
                        self._logger.info(
                            "Cold-start: Using %d tracks from '%s' as AI seed",
                            len(sample),
                            default_name,
                        )
                        return sample
            except Exception as exc:
                self._logger.debug("Failed to get default playlist for cold-start: %s", exc)

        return []


# ------------------------------------------------------------------ #
# Singleton accessor
# ------------------------------------------------------------------ #

_playback_session_controller: PlaybackSessionController | None = None


def get_playback_session_controller() -> PlaybackSessionController:
    """
    Get or create the global PlaybackSessionController instance.

    Returns:
        PlaybackSessionController singleton
    """
    global _playback_session_controller
    if _playback_session_controller is None:
        _playback_session_controller = PlaybackSessionController()
    return _playback_session_controller


def initialize_playback_session_controller(
    autoplay_controller: AutoplayController | None = None,
    playlist_manager: PlaylistManager | None = None,
    state_manager: ConsolidatedState | None = None,
) -> PlaybackSessionController:
    """
    Initialize the global PlaybackSessionController with dependencies.

    Args:
        autoplay_controller: AutoplayController instance
        playlist_manager: PlaylistManager instance
        state_manager: ConsolidatedState instance

    Returns:
        Initialized PlaybackSessionController
    """
    global _playback_session_controller
    _playback_session_controller = PlaybackSessionController(
        autoplay_controller=autoplay_controller,
        playlist_manager=playlist_manager,
        state_manager=state_manager,
    )
    return _playback_session_controller
