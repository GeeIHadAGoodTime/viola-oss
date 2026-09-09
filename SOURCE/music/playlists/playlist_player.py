"""
PlaylistPlayer — bridges PlaylistStore (SQLite per-track storage) with
the Viola playback system.

Reads tracks from PlaylistStore, converts them to the track-dict format
used by PlaylistSession, and ARMS a playlist session on
PlaybackSessionController.

What this module does NOT do is start audio. Arming the session caches the
track list and flips the playback mode so the autoplay refill can draw from
the playlist; the first track still has to be handed to a player by the
caller (``skills/builtin/music_control.py`` does exactly that: it arms the
session, pops the first batch, and calls ``queue_playlist_tracks`` on a real
music interface). Treating the armed session as "playing" is how
``play_playlist`` came to tell users "Playing 'workout' — 12 songs,
shuffled." off the back of an in-memory object having been constructed, with
no audio anywhere. ``arm`` returns what was actually established so a caller
cannot mistake one for the other.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.logging_config import get_logger
from music.playlists.playlist_store import PlaylistStore, TrackRecord

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


@dataclass(frozen=True)
class PlaylistArmOutcome:
    """What arming a playlist session established -- and what it did not.

    ``session_armed`` says the controller now holds the playlist and will
    refill the queue from it. It deliberately does NOT say anything about
    audio: ``first_track`` is the track the caller must still start, and
    until something plays it there is no playback to report.
    """

    session_armed: bool
    track_count: int = 0
    first_track: dict | None = None
    remaining_in_session: int = 0
    reason: str = ""
    playback_started: bool = field(default=False, init=False)


def _track_to_session_dict(track: TrackRecord) -> dict:
    """
    Convert a TrackRecord to the dict format expected by PlaylistSession.

    PlaylistSession stores dicts with keys: url, title, artist, provider, etc.
    """
    entry: dict = {
        "title": track.title or track.track_uri,
        "artist": track.artist,
        "provider": track.provider,
    }

    provider = track.provider.lower() if track.provider else ""

    if provider in ("local",):
        # Local files: use file path as URL
        entry["url"] = track.track_uri
        entry["source"] = "local"
    elif provider in ("youtube", "youtube_music", "ytmusic"):
        # YouTube: track_uri may be a URL or video ID
        uri = track.track_uri
        if uri.startswith("http"):
            entry["url"] = uri
            entry["source"] = "url"
        else:
            # Treat as search query
            entry["url"] = "ytsearch1:" + uri
            entry["source"] = "ytsearch1"
    elif provider in ("spotify",):
        # Spotify: track_uri is "spotify:track:xxx"
        entry["url"] = track.track_uri
        entry["source"] = "spotify"
        entry["stream_token"] = track.track_uri
    else:
        # Unknown provider: use track_uri as-is
        entry["url"] = track.track_uri
        entry["source"] = "url"

    return entry


class PlaylistPlayer:
    """
    Bridges PlaylistStore with the Viola playback system.

    Usage::

        player = PlaylistPlayer()
        success = await player.play("user-123", "My Mix", shuffle=True)
    """

    def __init__(self, store: PlaylistStore | None = None) -> None:
        self._store = store or PlaylistStore()

    async def arm(
        self,
        user_id: str,
        playlist_name: str,
        shuffle: bool = True,
    ) -> PlaylistArmOutcome:
        """
        Load a playlist into the playback session controller.

        Reads all tracks, converts them to session dicts, injects a
        ``PlaylistSession`` into the controller, and flips the playback mode to
        PLAYLIST so the autoplay refill draws from the playlist. It then pops
        the first track off that session and hands it back, because SOMETHING
        still has to play it -- this method starts no audio and reads no player
        state, so it is not in a position to claim any.

        Args:
            user_id: User ID for data isolation.
            playlist_name: Name of the playlist (case-insensitive).
            shuffle: Whether to shuffle the track order.

        Returns:
            A ``PlaylistArmOutcome`` describing exactly what was established.
        """
        # Async-native store reads: ``arm`` is awaited on the cloud FastAPI
        # serving loop (via the cloud-native ``playlist`` tool), where a sync
        # store call (Postgres ``_run`` bridge) would raise SyncBridgeLoopError
        # (CL-20260711-afd7).
        tracks = await self._store.get_tracks_async(user_id, playlist_name)
        if not tracks:
            playlist = await self._store.get_playlist_async(user_id, playlist_name)
            if playlist is None:
                logger.warning("PlaylistPlayer: playlist %r not found", playlist_name)
                return PlaylistArmOutcome(session_armed=False, reason="playlist_not_found")
            logger.warning("PlaylistPlayer: playlist %r is empty", playlist_name)
            return PlaylistArmOutcome(session_armed=False, reason="playlist_empty")

        return self._arm_session(playlist_name, tracks, shuffle)

    def _arm_session(
        self,
        playlist_name: str,
        tracks: list[TrackRecord],
        shuffle: bool,
    ) -> PlaylistArmOutcome:
        """Inject the playlist into the controller. Starts no audio."""
        track_dicts = [_track_to_session_dict(t) for t in tracks]

        if shuffle:
            random.SystemRandom().shuffle(track_dicts)

        logger.info(
            "PlaylistPlayer: arming %r with %d tracks (shuffle=%s)",
            playlist_name,
            len(track_dicts),
            shuffle,
        )

        try:
            from music.playback_session import (
                PlaylistSession,
                get_playback_session_controller,
            )

            controller = get_playback_session_controller()

            # Create a PlaylistSession and inject it directly
            session = PlaylistSession(
                playlist_name=playlist_name,
                original_tracks=track_dicts,
            )
            controller._playlist_session = session

            # Set playback mode to PLAYLIST
            from models.state_manager import PlaybackMode

            if controller._state_mgr is not None:
                controller._state_mgr.set_playback_mode(PlaybackMode.PLAYLIST)

            logger.info(
                "PlaylistPlayer: playlist session armed for %r (%d tracks)",
                playlist_name,
                len(track_dicts),
            )
        except Exception:
            logger.exception("PlaylistPlayer: failed to arm playlist %r", playlist_name)
            return PlaylistArmOutcome(session_armed=False, reason="playlist_session_error")

        first_track = self._pop_first_track(controller, track_dicts)
        remaining = max(0, len(track_dicts) - 1)
        return PlaylistArmOutcome(
            session_armed=True,
            track_count=len(track_dicts),
            first_track=first_track,
            remaining_in_session=remaining,
        )

    @staticmethod
    def _pop_first_track(controller: object, track_dicts: list[dict]) -> dict | None:
        """Take the track the caller must start off the armed session.

        Popping through the controller (the same call
        ``skills/builtin/music_control.py`` makes) keeps the session's own
        cursor consistent, so the autoplay refill continues from track two
        instead of replaying track one. If the controller cannot hand one back
        for any reason, fall back to the local list rather than losing the
        playlist start entirely.
        """
        fill = getattr(controller, "fill_queue_from_playlist", None)
        if callable(fill):
            try:
                popped = fill(1)
            except Exception:
                logger.exception("PlaylistPlayer: could not pop the first playlist track")
                popped = None
            if isinstance(popped, list) and popped and isinstance(popped[0], dict):
                return popped[0]
        return track_dicts[0] if track_dicts else None

    async def play(
        self,
        user_id: str,
        playlist_name: str,
        shuffle: bool = True,
    ) -> bool:
        """
        Arm a playlist session. **This does not start audio.**

        Kept for callers that only need to know whether the playlist could be
        loaded. ``True`` means the controller is now holding the playlist, NOT
        that anything is playing -- use ``arm`` when the answer is going to be
        reported to a user.
        """
        # Async-native store reads, same as ``arm``: this runs on the cloud
        # FastAPI serving loop, where a sync store call (Postgres ``_run``
        # bridge) would raise SyncBridgeLoopError (CL-20260711-afd7).
        tracks = await self._store.get_tracks_async(user_id, playlist_name)
        if not tracks:
            playlist = await self._store.get_playlist_async(user_id, playlist_name)
            if playlist is None:
                logger.warning("PlaylistPlayer: playlist %r not found", playlist_name)
            else:
                logger.warning("PlaylistPlayer: playlist %r is empty", playlist_name)
            return False
        return self._arm_session(playlist_name, tracks, shuffle).session_armed

    def get_track_count(self, user_id: str, playlist_name: str) -> int:
        """Return the number of tracks in a playlist (sync; desktop/worker-thread callers)."""
        return len(self._store.get_tracks(user_id, playlist_name))

    async def get_track_count_async(self, user_id: str, playlist_name: str) -> int:
        """Async-native track count for serving-loop callers (CL-20260711-afd7)."""
        return len(await self._store.get_tracks_async(user_id, playlist_name))
