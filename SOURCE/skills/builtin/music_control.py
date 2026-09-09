"""
Music Control Skill

Handles music playback commands (play, pause, resume, skip, etc.)
"""

from __future__ import annotations

import asyncio
import inspect
import random
import re
from typing import Any

from core.logging_config import get_logger
from music.playlist_queue import queue_playlist_tracks

from ..base import Intent, Response, Skill

logger = get_logger(__name__)
_NO_MUSIC_TO_RESUME_MESSAGE = "I don't have any music to resume. What would you like to play?"


def _extract_playback_snapshot(music: object) -> tuple[bool | None, object | None]:
    """Return (is_playing, now_playing) from supported music backends."""
    is_playing: bool | None = None
    now_playing: object | None = None
    if hasattr(music, "state") and callable(music.state):
        try:
            state = music.state()
        except Exception:
            logger.debug("Failed to read music state for resume guard", exc_info=True)
            state = None
        if isinstance(state, dict):
            raw_is_playing = state.get("is_playing")
            if isinstance(raw_is_playing, bool):
                is_playing = raw_is_playing
            now_playing = state.get("now_playing") or state.get("current") or state.get("current_track")
        elif state is not None:
            raw_is_playing = getattr(state, "is_playing", None)
            if isinstance(raw_is_playing, bool):
                is_playing = raw_is_playing
            now_playing = (
                getattr(state, "now_playing", None)
                or getattr(state, "current", None)
                or getattr(state, "current_track", None)
            )
    if now_playing is None:
        now_playing = getattr(music, "now_playing", None) or getattr(music, "current_track", None)
    if is_playing is None:
        raw_attr = getattr(music, "is_playing", None)
        try:
            raw_is_playing = raw_attr() if callable(raw_attr) else raw_attr
        except Exception:
            raw_is_playing = None
        if isinstance(raw_is_playing, bool):
            is_playing = raw_is_playing
    return is_playing, now_playing


# CB-6: Strict stop pattern — only matches genuine music stop commands.
# Must NOT match conversational phrases like "stop worrying about it".
# Mirrors the instant command smart_stop pattern from instant_commands_patterns.py.
_STOP_RE = re.compile(
    r"^(?:(?:please|can\s+you|could\s+you)\s+)?"
    r"stop"
    r"(?:\s+(?:playing|music|playback|song|it|that|this|the\s+(?:music|song|track|audio)))?"
    r"[!.]*$",
    re.IGNORECASE,
)

# CB-11: Strict skip/next pattern — only matches genuine skip/next commands.
# Must NOT match seek-like commands such as "skip to 1:30" or "skip ahead 10 seconds".
_SKIP_RE = re.compile(
    r"^(?:(?:please|can\s+you|could\s+you)\s+)?" r"(?:skip|next|play\s+next)" r"(?:\s+(?:song|track))?" r"[!.]*$",
    re.IGNORECASE,
)

# CB-15: Strict pause pattern — only matches genuine music pause commands.
# Must NOT match conversational phrases like "pause this conversation" or "pause for a moment".
_PAUSE_RE = re.compile(
    r"^(?:pause|hold)" r"(?:\s+(?:the\s+)?(?:music|song|track|playback|audio|it|that|this))?" r"[!.]*$",
    re.IGNORECASE,
)

# CB-16: Strict resume pattern — only matches genuine music resume commands.
# Must NOT match conversational phrases like "resume this conversation".
_RESUME_RE = re.compile(
    r"^(?:resume|unpause|continue|go)"
    r"(?:\s+(?:the\s+)?(?:music|song|track|playback|audio|playing|it|that|this))?"
    r"[!.]*$",
    re.IGNORECASE,
)

# SP-BUG-11: Seek pattern — matches seek/skip-to/go-to/jump-to commands.
# Captures the target portion — either a time ("1:30", "90 seconds") OR a
# semantic keyword target ("the chorus", "the end"). Parseability is
# decided in _handle_seek so we can return a graceful "couldn't understand"
# response for recognized-but-unparseable targets instead of routing them
# to the fallback tool.
_SEEK_RE = re.compile(
    r"^(?:(?:please|can\s+you|could\s+you)\s+)?"
    r"(?:seek|skip|go|jump|fast\s*forward)\s+to\s+"
    r"(?P<time>.+?)"
    r"[!.]*$",
    re.IGNORECASE,
)


def _parse_seek_time(time_str: str) -> int | None:
    """Parse a human time string into milliseconds.

    Supports formats:
    - "1:30" or "01:30" (MM:SS)
    - "1:02:30" (H:MM:SS)
    - "1 minute 30 seconds"
    - "3 minutes"
    - "90 seconds"
    - "1 hour 2 minutes 30 seconds"
    - Abbreviations: "min", "mins", "sec", "secs", "hr", "hrs"

    Returns milliseconds, or None if parsing fails.
    """
    text = time_str.strip().lower()

    # Try colon-separated format first: H:MM:SS or MM:SS or M:SS
    colon_match = re.match(
        r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$",
        text,
    )
    if colon_match:
        parts = colon_match.groups()
        if parts[2] is not None:
            # H:MM:SS
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
        else:
            # MM:SS
            hours = 0
            minutes = int(parts[0])
            seconds = int(parts[1])
        total_seconds = hours * 3600 + minutes * 60 + seconds
        return total_seconds * 1000

    # Try natural language: "X hour(s) Y minute(s) Z second(s)"
    total_seconds = 0
    found_any = False

    hour_match = re.search(r"(\d+)\s*(?:hours?|hrs?|h)\b", text)
    if hour_match:
        total_seconds += int(hour_match.group(1)) * 3600
        found_any = True

    minute_match = re.search(r"(\d+)\s*(?:minutes?|mins?|m)\b", text)
    if minute_match:
        total_seconds += int(minute_match.group(1)) * 60
        found_any = True

    second_match = re.search(r"(\d+)\s*(?:seconds?|secs?|s)\b", text)
    if second_match:
        total_seconds += int(second_match.group(1))
        found_any = True

    if found_any:
        return total_seconds * 1000

    # Try bare number (interpret as seconds)
    bare_match = re.match(r"^(\d+)$", text)
    if bare_match:
        return int(bare_match.group(1)) * 1000

    return None


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


class MusicControlSkill(Skill):
    """Skill for controlling music playback."""

    name = "music_control"
    description = "Control music playback (play, pause, skip, volume)"
    priority = 100  # High priority - music is core functionality

    def __init__(self, context=None):
        super().__init__(context)
        self._logger = logger.bind(skill=self.name)

    def patterns(self) -> list[str]:
        return [
            # Play
            r"^play\s+(?P<query>.+)",
            r"^(?:can you )?play (?P<query>.+)",
            # Pause
            r"^pause\s*(?:music|playback|song)?",
            r"^(?:(?:please|can\s+you|could\s+you)\s+)?stop\s*(?:music|playback|song|playing|it|that|this|the\s+(?:music|song|track|audio))?[!.]*$",
            r"^hold\s+(?:on|it)",
            # Resume
            r"^resume\s*(?:music|playback)?",
            r"^continue\s*(?:playing)?",
            r"^unpause",
            # Skip (CB-11: $ anchor prevents matching "skip to 1:30")
            r"^(?:(?:please|can\s+you|could\s+you)\s+)?(?:skip|next)\s*(?:song|track)?[!.]*$",
            r"^(?:(?:please|can\s+you|could\s+you)\s+)?play\s+next[!.]*$",
            # Seek to position (SP-BUG-11: must come before skip/restart/previous).
            # Matches both numeric targets ("skip to 1:30") and semantic keyword
            # targets ("skip to the chorus"); parseability is decided in
            # _handle_seek so unparseable targets get a graceful error instead
            # of routing to fallback.
            r"^(?:(?:please|can\s+you|could\s+you)\s+)?(?:seek|skip|go|jump|fast\s*forward)\s+to\s+(?P<time>.+?)[!.]*$",
            # Restart / seek-to-zero (R2-2/R2-3: must come before Previous)
            r"^(?:restart|start\s+over)",
            r"^(?:play\s+)?(?:from\s+)?the\s+beginning",
            r"^go\s+back\s+to\s+the\s+(?:beginning|start)",
            # Previous
            r"^(?:previous|back)\s*(?:song|track)?",
            r"^go back(?!\s+to\s+the\s+(?:beginning|start))",
            # Volume
            r"^(?:(?:please|can\s+you|could\s+you|go\s+ahead\s+and|hey\s+viola)\s+)?(?:set\s+(?:the\s+)?|change\s+|turn\s+)?volume\s+(?:to\s+)?(?P<level>\d{1,3})%?",
            r"^volume up",
            r"^volume down",
            r"^(?:turn it )?(?P<direction>up|down)",
            # Mute / Unmute
            r"^(?:mute|silence|quiet)$",
            r"^(?:unmute|un silence|sound on)$",
            # Shuffle
            r"^(?:shuffle|shuffle on|mix it up)$",
            r"^(?:shuffle off|no shuffle|stop shuffling)$",
            # Repeat
            r"^(?:repeat off|no repeat|stop repeating|don'?t repeat)$",
            r"^(?:repeat|repeat on|repeat all|loop playlist|loop queue)$",
            r"^(?:repeat one|loop song|loop this|loop this song|loop this track|repeat this song|repeat this track|repeat current song|replay this)$",
        ]

    async def execute(self, intent: Intent) -> Response:
        """Execute music control command."""
        self._logger.info(
            "DEBUG: MusicControlSkill.execute called with intent.text=%s intent.params=%s",
            intent.text,
            intent.params,
        )
        if not self.context.music:
            self._logger.warning("DEBUG: context.music is None!")
            return Response(message="Music player not available", success=False)

        text = intent.text.lower()

        # Play
        if "play" in text and intent.params.get("query"):
            query = intent.params["query"]

            # ── "play X on <provider>" — switch provider then play ──
            import re as _re

            _PROVIDER_MAP = {
                "spotify": "spotify",
                "youtube": "youtube_iframe",
                "youtube music": "youtube_music",
                "local": "local",
                "my library": "local",
            }
            _on_provider_match = _re.match(
                r"^(.+?)\s+on\s+(spotify|youtube|youtube music|local|my library)$",
                query,
                _re.I,
            )
            if _on_provider_match:
                query = _on_provider_match.group(1).strip()
                target_raw = _on_provider_match.group(2).strip().lower()
                target_id = _PROVIDER_MAP.get(target_raw, target_raw)
                self._logger.info(
                    "play_on_provider detected: query=%s provider=%s",
                    query,
                    target_id,
                )
                try:
                    from ui.settings_manager import get_settings_manager

                    sm = get_settings_manager()
                    sm.set("active_music_provider_id", target_id)
                except Exception as exc:
                    self._logger.warning("Provider switch to %s failed: %s", target_id, exc)

            # ── YouTube playlist URL detection (BUG-3 fix) ──
            # If the query is a YouTube playlist URL, resolve its tracks and
            # queue them instead of treating the URL as a single video play.
            if "list=" in query and ("youtube.com/" in query or "youtu.be/" in query):
                try:
                    from music.providers.youtube_playlist_core import (
                        extract_playlist_id,
                    )

                    playlist_id = extract_playlist_id(query)
                    if playlist_id:
                        self._logger.info(
                            "YouTube playlist URL detected: id=%s",
                            playlist_id,
                        )
                        tracks = await self._resolve_youtube_playlist_url(
                            playlist_id,
                        )
                        if tracks:
                            successfully_queued, failed_count = await queue_playlist_tracks(
                                self.context.music,
                                tracks,
                                log=self._logger,
                            )
                            if successfully_queued > 0:
                                return Response(
                                    message="Playing YouTube playlist (%d tracks queued)" % successfully_queued,
                                    spoken=True,
                                )
                            return Response(
                                message="Couldn't play any tracks from that playlist",
                                success=False,
                                spoken=True,
                            )
                        # If resolution returned empty, fall through to
                        # normal play path (treated as single URL).
                except Exception as exc:
                    self._logger.warning(
                        "YouTube playlist URL resolution failed: %s",
                        exc,
                    )
                    # Fall through to normal play path

            # Check if this is a playlist command
            # Pattern: "play [name] playlist" or "play [name]" where playerlist name matches
            playlist_name = None

            # Try to extract playlist name
            query_lower = query.lower().strip()

            # Check for "my playlist", "my music" or "default playlist" - use default playlist
            if query_lower in (
                "my playlist",
                "my music",
                "my",
                "default playlist",
                "default",
            ):
                from music.playlist_manager import get_playlist_manager

                playlist_mgr = get_playlist_manager()
                default_playlist = playlist_mgr.get_default_playlist()
                if default_playlist:
                    playlist_name = default_playlist
                else:
                    # No default playlist set
                    if self.context.tts:

                        async def speak_async():
                            try:
                                if self.context.tts is not None:
                                    await self.context.tts.speak(
                                        "You don't have a default playlist set. Just tell me which playlist you'd like as your default and I'll set it."
                                    )
                            except Exception as e:
                                logger.debug("TTS speak failed (non-critical): %s", e)

                        # mt-ok: TTS output is per-machine, no user data crosses boundary
                        _task = asyncio.create_task(speak_async())
                        _task.add_done_callback(_log_task_exception)
                    return Response(
                        message="No default playlist set. Just tell me which playlist you'd like as your default and I'll set it.",
                        success=False,
                    )
            # Check if query ends with "playlist"
            elif query_lower.endswith(" playlist"):
                playlist_name = query_lower[:-9].strip()  # Remove " playlist"
            else:
                # Check if the query itself is a playlist name
                from music.playlist_manager import get_playlist_manager

                playlist_mgr = get_playlist_manager()
                if playlist_mgr.get_playlist(query_lower):
                    playlist_name = query_lower

            # If we found a playlist, play it instead of searching
            if playlist_name:
                # ── LOCAL provider: resolve against local library playlists ──
                local_result = await self._try_local_playlist(playlist_name)
                if local_result is not None:
                    return local_result

                try:
                    from music.playlist_manager import get_playlist_manager

                    playlist_mgr = get_playlist_manager()
                    playlist_info = playlist_mgr.get_playlist(playlist_name)

                    if not playlist_info:
                        # Playlist not found, fall through to search
                        pass
                    else:
                        # Get shuffle setting
                        shuffle_value = playlist_info.get("shuffle", True) if isinstance(playlist_info, dict) else True
                        should_shuffle: bool = bool(shuffle_value)

                        # Voice feedback
                        if self.context.tts:

                            async def speak_async():
                                try:
                                    if self.context.tts is not None:
                                        await self.context.tts.speak(f"Playing your {playlist_name} playlist")
                                except Exception as e:
                                    logger.debug("TTS speak failed (non-critical): %s", e)

                            # mt-ok: TTS output is per-machine, no user data crosses boundary
                            _task = asyncio.create_task(speak_async())
                            _task.add_done_callback(_log_task_exception)

                        # Use PlaybackSessionController to cache ALL playlist tracks
                        # and set PLAYLIST mode so autoplay can refill the queue.
                        from music.playback_session import (
                            get_playback_session_controller,
                        )

                        session = get_playback_session_controller()
                        started = await session.start_playlist(playlist_name, shuffle=should_shuffle)

                        if not started:
                            return Response(
                                message=f"The {playlist_name} playlist is empty",
                                success=False,
                                spoken=True,
                            )

                        # Pop initial batch from session for immediate playback
                        initial_tracks = session.fill_queue_from_playlist(10)
                        if not initial_tracks:
                            return Response(
                                message=f"The {playlist_name} playlist is empty",
                                success=False,
                                spoken=True,
                            )

                        first_track = initial_tracks[0] if initial_tracks else None
                        successfully_queued, failed_count = await queue_playlist_tracks(
                            self.context.music, initial_tracks, log=self._logger
                        )

                        if successfully_queued == 0:
                            return Response(
                                message="None of the tracks in %s could be loaded. The provider may be unavailable -- try a different playlist or play a single track to test."
                                % playlist_name,
                                success=False,
                                spoken=True,
                            )

                        # Use the full playlist count for the response message
                        playlist_session = session.get_playlist_session()
                        total_tracks = playlist_session.total_count() if playlist_session else len(initial_tracks)

                        playlist_message = self._build_playlist_response(
                            playlist_name=playlist_name,
                            queued=successfully_queued,
                            total=total_tracks,
                            failed=failed_count,
                            first_track=self._format_track_reference(first_track),
                        )
                        return Response(
                            message=playlist_message,
                            success=True,
                            spoken=True,
                        )

                except Exception as e:
                    # If playlist playback fails, fall through to search
                    self._logger.error(
                        "Playlist playback pipeline failed | playlist={playlist} error={error}",
                        playlist=playlist_name,
                        error=str(e),
                    )

            # Not a playlist or playlist handling failed - treat as search query
            # Detect local library keywords and extract source before play
            from intent.interpreter_helpers import _detect_source

            source, cleaned_query = _detect_source(query)
            # _detect_source returns "ytsearch1" as default; normalize to None
            # so the player uses its own active-provider logic for non-local.
            play_source = source if source == "local" else None
            play_query = cleaned_query if source == "local" else query

            # ── Fire-and-forget for "play X on <provider>" ──────────────
            # Spotify CDP launch can exceed the 6s skill timeout.  If we
            # await the full play chain the bridge times out, falls through
            # to the interpreter, and instant-command `play_on_provider`
            # fires a duplicate.  Return immediately to prevent that.
            if _on_provider_match:

                async def _bg_play() -> None:
                    try:
                        if hasattr(self.context.music, "play_async"):
                            coro = self.context.music.play_async(
                                play_query,
                                play_source,
                                interrupt=True,
                            )
                            if inspect.isawaitable(coro):
                                await coro
                        else:
                            # SP-BUG-6: Run off event loop to prevent blocking
                            await asyncio.to_thread(
                                self.context.music.play,
                                play_query,
                                play_source,
                                interrupt=True,
                            )
                    except Exception as exc:
                        self._logger.error(
                            "Background play-on-provider failed: %s",
                            exc,
                        )

                _task = asyncio.create_task(_bg_play())
                _task.add_done_callback(_log_task_exception)
                return Response(
                    message="Playing %s on %s" % (query, target_raw.title()),
                    spoken=True,
                )

            try:
                # Voice feedback: Tell user we're searching (fire and forget - don't wait)
                if self.context.tts:
                    # Speak while searching (truly non-blocking - fire and forget)
                    # Random friendly search phrases
                    search_phrases = [
                        f"Looking for {play_query}",
                        f"Searching for {play_query}",
                        f"Finding {play_query} for you",
                        f"Let me find {play_query}",
                        "One moment, searching",
                        "Searching for that now",
                    ]

                    # Fire and forget - don't wait for TTS to complete
                    async def speak_async():
                        try:
                            if self.context.tts is not None:
                                await self.context.tts.speak(random.choice(search_phrases))
                        except Exception as e:
                            # Silently fail if TTS has issues
                            logger.debug("TTS speak failed (non-critical): %s", e)

                    _task = asyncio.create_task(speak_async())
                    _task.add_done_callback(_log_task_exception)

                # SP-BUG-6 fix: Fire-and-forget pattern. Spotify CDP search can
                # take 30+ seconds which exceeds the 6s skill timeout, causing
                # the command to fall through to the interpreter and create
                # double-play. Return immediately to prevent that.
                async def _bg_play() -> None:
                    try:
                        if hasattr(self.context.music, "play_async"):
                            coro = self.context.music.play_async(
                                play_query,
                                play_source,
                                interrupt=True,
                            )
                            if inspect.isawaitable(coro):
                                await coro
                        else:
                            await asyncio.to_thread(
                                self.context.music.play,
                                play_query,
                                play_source,
                                interrupt=True,
                            )
                    except Exception as exc:
                        self._logger.error(
                            "Background play failed: %s",
                            exc,
                        )

                _task = asyncio.create_task(_bg_play())
                _task.add_done_callback(_log_task_exception)
                return Response(
                    message="Playing %s" % play_query,
                    spoken=True,
                )
            except Exception as e:
                self._logger.error(
                    "Play command setup failed: %s",
                    e,
                )
                return Response(
                    message="Playback failed (%s). Try a different track or check your music provider settings." % e,
                    success=False,
                )

        # Mute (before pause/stop to avoid "mute" matching "stop" handler)
        elif (
            any(word in text for word in ["mute", "silence", "quiet"])
            and "unmute" not in text
            and "un silence" not in text
            and "sound on" not in text
        ):
            return await self._handle_mute()

        # Unmute
        elif any(phrase in text for phrase in ["unmute", "un silence", "sound on"]):
            return await self._handle_unmute()

        # Shuffle (before pause/stop to avoid "stop shuffling" matching "stop" handler)
        elif "shuffle" in text or "shuffling" in text or "mix it up" in text:
            if "off" in text or "no shuffle" in text or "stop shuffling" in text:
                return self._handle_shuffle(enabled=False)
            return self._handle_shuffle(enabled=True)

        # Repeat (before pause/stop to avoid "stop repeating" matching "stop" handler)
        elif "repeat" in text or "repeating" in text or "loop" in text or "don't repeat" in text:
            if "off" in text or "no repeat" in text or "stop repeating" in text or "don't repeat" in text:
                return self._handle_repeat(mode="off")
            elif (
                "one" in text
                or "this song" in text
                or "this track" in text
                or "current song" in text
                or "loop song" in text
                or "loop this" in text
                or "replay this" in text
            ):
                return self._handle_repeat(mode="one")
            return self._handle_repeat(mode="all")

        # Stop (before pause so "stop" doesn't fall into the pause handler)
        # CB-6: Use strict regex — substring "stop" in text would false-positive
        # on conversational phrases like "stop worrying about it".
        elif _STOP_RE.match(text):
            try:
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.stop)
                return Response(message="Stopped", spoken=False)
            except Exception as e:
                return Response(message=f"Couldn't stop: {e!s}", success=False)

        # Pause (CB-15: strict regex — substring "pause" in text would false-positive
        # on conversational phrases like "pause this conversation")
        elif _PAUSE_RE.match(text):
            try:
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.pause)
                return Response(message="Paused", spoken=False)
            except Exception as e:
                return Response(message=f"Couldn't pause: {e!s}", success=False)

        # Resume (CB-16: strict regex — substring "resume" in text would false-positive
        # on conversational phrases like "resume this conversation")
        elif _RESUME_RE.match(text):
            try:
                pre_is_playing, pre_track = _extract_playback_snapshot(self.context.music)
                if pre_track is None and pre_is_playing is not True:
                    return Response(message=_NO_MUSIC_TO_RESUME_MESSAGE, success=False)
                if pre_is_playing is True and pre_track is not None:
                    return Response(message="Music is already playing", spoken=False)
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.resume)
                post_is_playing, post_track = _extract_playback_snapshot(self.context.music)
                if post_track is None:
                    return Response(message=_NO_MUSIC_TO_RESUME_MESSAGE, success=False)
                if post_is_playing is False:
                    return Response(message="Couldn't resume playback. Try saying 'play' instead?", success=False)
                return Response(message="Music resumed", spoken=False)
            except Exception as e:
                return Response(message=f"Couldn't resume: {e!s}", success=False)

        # Seek to position (SP-BUG-11: must come before skip so "skip to 1:30"
        # is handled as a seek, not rejected by skip's strict regex)
        elif _SEEK_RE.match(text):
            return await self._handle_seek(text)

        # Skip (CB-11: strict regex — substring "skip" in text would false-positive
        # on seek commands like "skip to 1:30")
        elif _SKIP_RE.match(text):
            try:
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.next)
                return Response(message="Skipped to next track", spoken=False)
            except Exception as e:
                return Response(message=f"Couldn't skip: {e!s}", success=False)

        # Restart / seek-to-zero: "restart", "start over", "go back to the beginning"
        # Must come BEFORE the "previous" check so "go back to the beginning"
        # is not consumed by the "back" substring test (R2-2/R2-3).
        elif re.match(
            r"(?:restart|start\s+over|(?:play\s+)?(?:from\s+)?the\s+beginning"
            r"|go\s+back\s+to\s+the\s+(?:beginning|start))",
            text,
        ):
            try:
                # SP-BUG-6: Run off event loop to prevent blocking
                if hasattr(self.context.music, "seek"):
                    await asyncio.to_thread(self.context.music.seek, 0)
                return Response(message="Restarted current track", spoken=False)
            except Exception as e:
                return Response(message=f"Couldn't restart: {e!s}", success=False)

        # Previous
        elif any(word in text for word in ["previous", "back"]):
            try:
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.previous)
                return Response(message="Back to previous track", spoken=False)
            except Exception as e:
                return Response(message=f"Couldn't go back: {e!s}", success=False)

        # Volume
        elif "volume" in text or intent.params.get("direction"):
            try:
                if intent.params.get("level"):
                    # Set absolute volume (CB-3: clamp before formatting message)
                    level = min(max(int(intent.params["level"]), 0), 100)
                    # SP-BUG-6: Run off event loop to prevent blocking
                    await asyncio.to_thread(self.context.music.set_volume, level)
                    return Response(message=f"Volume set to {level}%", spoken=False)

                elif "up" in text or intent.params.get("direction") == "up":
                    # Increase volume
                    current_vol = self._get_current_volume()
                    new_vol = min(100, current_vol + 10)
                    # SP-BUG-6: Run off event loop to prevent blocking
                    await asyncio.to_thread(self.context.music.set_volume, new_vol)
                    return Response(message="Volume up", spoken=False)

                elif "down" in text or intent.params.get("direction") == "down":
                    # Decrease volume
                    current_vol = self._get_current_volume()
                    new_vol = max(0, current_vol - 10)
                    # SP-BUG-6: Run off event loop to prevent blocking
                    await asyncio.to_thread(self.context.music.set_volume, new_vol)
                    return Response(message="Volume down", spoken=False)

            except Exception as e:
                return Response(message=f"Couldn't change volume: {e!s}", success=False)

        # No internal handler matched — return None so the pipeline falls
        # through to the LLM interpreter for natural-language music commands
        # like "hold on for a sec" or "make it louder".
        return None

    # ------------------------------------------------------------------ #
    # YouTube playlist URL resolution (BUG-3)
    # ------------------------------------------------------------------ #

    async def _resolve_youtube_playlist_url(self, playlist_id: str) -> list[dict[str, object]]:
        """Resolve a YouTube playlist ID into a list of track dicts.

        Uses BrowserSearchEngine.resolve_playlist() (primary, zero API quota)
        with PlaylistManager API fallback.

        Returns:
            List of dicts with keys: video_id, title, url.  Empty on failure.
        """
        # Primary: browser-based resolution (zero API quota)
        try:
            from music.providers.browser_search import BrowserSearchEngine

            engine = BrowserSearchEngine()
            raw = await asyncio.to_thread(
                engine.resolve_playlist,
                playlist_id,
                200,
            )
            if raw:
                tracks: list[dict[str, object]] = []
                for entry in raw:
                    vid = entry.get("video_id")
                    if not vid:
                        continue
                    tracks.append(
                        {
                            "video_id": vid,
                            "title": entry.get("title", ""),
                            "url": "https://www.youtube.com/watch?v=%s" % vid,
                        }
                    )
                if tracks:
                    self._logger.info(
                        "Resolved %d tracks from YouTube playlist %s",
                        len(tracks),
                        playlist_id,
                    )
                    return tracks
        except Exception as exc:
            self._logger.warning(
                "Browser playlist resolution failed: %s",
                exc,
            )

        # Fallback: PlaylistManager (uses API)
        try:
            from music.playlist_manager import get_playlist_manager

            mgr = get_playlist_manager()
            # Temporarily add the playlist so get_playlist_videos can find it
            temp_url = "https://www.youtube.com/playlist?list=%s" % playlist_id
            mgr.add_playlist("__temp_url_playlist__", temp_url)
            try:
                videos = await asyncio.to_thread(
                    mgr.get_playlist_videos,
                    "__temp_url_playlist__",
                    limit=200,
                )
                return list(videos) if videos else []
            finally:
                mgr.remove_playlist("__temp_url_playlist__")
        except Exception as exc:
            self._logger.warning(
                "PlaylistManager fallback resolution failed: %s",
                exc,
            )
        return []

    # ------------------------------------------------------------------ #
    # Local playlist support (GAP-3)
    # ------------------------------------------------------------------ #

    async def _try_local_playlist(self, playlist_name: str) -> Response | None:
        """Attempt to play a local library playlist when the active provider is 'local'.

        Returns a Response if a local playlist was found and queued, or None to
        fall through to the YouTube playlist path.
        """
        from music.providers.active_provider import get_active_music_provider_id

        if get_active_music_provider_id() != "local":
            return None

        try:
            from music.providers.local.db import get_local_library_repo

            repo = get_local_library_repo()
            repo.initialize()

            # Exact (case-insensitive) match first
            playlist_row = repo.get_playlist_by_name(playlist_name)

            # Fuzzy match: scan all playlists for a close name
            if not playlist_row:
                playlist_row = self._fuzzy_match_local_playlist(
                    repo,
                    playlist_name,
                )

            if not playlist_row:
                self._logger.debug(
                    "No local playlist matches '%s'",
                    playlist_name,
                )
                return None

            playlist_id = playlist_row["id"]
            matched_name = playlist_row["name"]
            tracks = repo.get_playlist_songs(playlist_id)

            if not tracks:
                return Response(
                    message="The %s playlist is empty" % matched_name,
                    success=False,
                    spoken=True,
                )

            # Voice feedback
            if self.context.tts:

                async def _speak_local_playlist() -> None:
                    try:
                        if self.context.tts is not None:
                            await self.context.tts.speak("Playing your %s playlist" % matched_name)
                    except Exception as e:
                        logger.debug("TTS speak failed (non-critical): %s", e)

                # mt-ok: TTS output is per-machine, no user data crosses boundary
                _task = asyncio.create_task(_speak_local_playlist())
                _task.add_done_callback(_log_task_exception)

            # Build track list in the format queue_playlist_tracks expects.
            # For local tracks: url = file_path, no video_id.
            video_list: list[dict[str, object]] = []
            for t in tracks:
                video_list.append(
                    {
                        "url": t["file_path"],
                        "title": t.get("title") or t.get("file_name", "Unknown"),
                        "artist": t.get("artist"),
                        "duration": t.get("duration_seconds"),
                    }
                )

            # Shuffle by default for local playlists
            random.shuffle(video_list)

            successfully_queued, failed_count = await queue_playlist_tracks(
                self.context.music,
                video_list,
                log=self._logger,
            )

            if successfully_queued == 0:
                return Response(
                    message="None of the tracks in %s could be loaded. The provider may be unavailable -- try a different playlist or play a single track to test."
                    % matched_name,
                    success=False,
                    spoken=True,
                )

            playlist_message = self._build_playlist_response(
                playlist_name=matched_name,
                queued=successfully_queued,
                total=len(tracks),
                failed=failed_count,
                first_track=self._format_track_reference(video_list[0]),
            )
            return Response(
                message=playlist_message,
                success=True,
                spoken=True,
            )

        except Exception as exc:
            self._logger.error(
                "Local playlist playback failed: playlist=%s error=%s",
                playlist_name,
                exc,
            )
            return None

    @staticmethod
    def _fuzzy_match_local_playlist(
        repo: object,
        name: str,
        threshold: int = 70,
    ) -> dict | None:
        """Find the best fuzzy-matching local playlist.

        Uses simple substring and starts-with heuristics. If rapidfuzz is
        available it uses token_sort_ratio for better matching quality.

        Returns the playlist row dict, or None if no match exceeds *threshold*.
        """
        from music.providers.local.db import LocalLibraryRepo

        if not isinstance(repo, LocalLibraryRepo):
            return None

        all_playlists = repo.list_playlists()
        if not all_playlists:
            return None

        name_lower = name.lower().strip()

        # Pass 1: substring / starts-with (cheap, no deps)
        for p in all_playlists:
            p_name_lower = p["name"].lower()
            if p_name_lower == name_lower:
                return repo.get_playlist_by_name(p["name"])
            if p_name_lower.startswith(name_lower) or name_lower.startswith(p_name_lower):
                return repo.get_playlist_by_name(p["name"])

        # Pass 2: rapidfuzz (if available)
        try:
            from rapidfuzz import fuzz

            best_score = 0
            best_playlist = None
            for p in all_playlists:
                score = fuzz.token_sort_ratio(name_lower, p["name"].lower())
                if score > best_score:
                    best_score = score
                    best_playlist = p

            if best_playlist and best_score >= threshold:
                return repo.get_playlist_by_name(best_playlist["name"])
        except ImportError:
            pass

        return None

    async def _handle_seek(self, text: str) -> Response:
        """Handle seek-to-position commands (SP-BUG-11)."""
        match = _SEEK_RE.match(text)
        if not match:
            return Response(message="Couldn't understand the seek position", success=False)

        time_str = match.group("time")
        position_ms = _parse_seek_time(time_str)

        if position_ms is None:
            self._logger.warning("Could not parse seek time: %s", time_str)
            return Response(
                message="Couldn't understand the time '%s'" % time_str,
                success=False,
            )

        try:
            if not hasattr(self.context.music, "seek"):
                return Response(message="Seek is not supported", success=False)
            # SP-BUG-6: Run off event loop to prevent blocking
            await asyncio.to_thread(self.context.music.seek, position_ms)
            # Format a human-readable position for the response
            total_secs = position_ms // 1000
            mins = total_secs // 60
            secs = total_secs % 60
            if mins > 0:
                pos_label = "%d:%02d" % (mins, secs)
            else:
                pos_label = "%d seconds" % secs
            self._logger.info("Seeked to %s (%d ms)", pos_label, position_ms)
            return Response(
                message="Seeked to %s" % pos_label,
                spoken=False,
            )
        except Exception as e:
            return Response(message="Couldn't seek: %s" % e, success=False)

    async def _handle_mute(self) -> Response:
        """Mute audio via music player."""
        try:
            # Save current volume before muting so unmute can restore it
            self._pre_mute_volume = self._get_current_volume()
            if hasattr(self.context.music, "mute"):
                # SP-BUG-6: Run off event loop to prevent blocking
                result = await asyncio.to_thread(self.context.music.mute)
                return Response(
                    message="Audio muted",
                    data={"muted": True},
                )
            elif hasattr(self.context.music, "set_volume"):
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.set_volume, 0)
                return Response(
                    message="Audio muted",
                    data={"volume": 0},
                )
        except Exception as e:
            return Response(message=f"Couldn't mute: {e!s}", success=False)
        return Response(message="Mute not supported", success=False)

    async def _handle_unmute(self) -> Response:
        """Unmute audio via music player."""
        try:
            if hasattr(self.context.music, "unmute"):
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.unmute)
                return Response(
                    message="Audio unmuted",
                    data={"muted": False},
                )
            elif hasattr(self.context.music, "set_volume"):
                prev_vol = getattr(self, "_pre_mute_volume", None)
                if prev_vol is None:
                    prev_vol = self._get_current_volume() or 50
                new_vol = max(10, prev_vol)
                # SP-BUG-6: Run off event loop to prevent blocking
                await asyncio.to_thread(self.context.music.set_volume, new_vol)
                self._pre_mute_volume = None
                return Response(
                    message=f"Audio unmuted (volume {new_vol}%)",
                    data={"volume": new_vol},
                )
        except Exception as e:
            return Response(message=f"Couldn't unmute: {e!s}", success=False)
        return Response(message="Unmute not supported", success=False)

    def _handle_shuffle(self, *, enabled: bool) -> Response:
        """Turn shuffle on or off for real -- play order included.

        #4214: this used to write `preferences.shuffle` and dispatch
        `SetShuffle`, then answer "Shuffle is now on". Neither store carries
        ordering semantics -- every reader of that preference only reports it
        back out -- so the queue played in exactly the same order and the
        answer was a false success. Both halves now go through the one
        authority, which reorders before it records, so a reorder that cannot
        happen is reported as a failure instead of announced as a shuffle.
        """
        from music.shuffle_control import apply_shuffle

        try:
            apply_shuffle(self.context.music, enabled)
        except Exception as e:
            return Response(message=f"Couldn't change shuffle: {e!s}", success=False)

        label = "on" if enabled else "off"
        return Response(
            message=f"Shuffle is now {label}",
            data={"shuffle": enabled},
        )

    def _handle_repeat(self, *, mode: str) -> Response:
        """Set repeat mode via PlaybackSessionController and StateHub."""
        try:
            from models.state_manager import RepeatMode
            from music.playback_session import get_playback_session_controller

            mode_map = {
                "off": RepeatMode.OFF,
                "all": RepeatMode.ALL,
                "one": RepeatMode.ONE,
            }
            controller = get_playback_session_controller()
            controller.set_repeat_mode(mode_map[mode])
            # Update the StateHub so API endpoints reflect the new value
            from core.state_hub import SetRepeatMode, get_state_hub

            get_state_hub().dispatch(SetRepeatMode(mode=mode))

            messages = {
                "off": "Repeat is now off",
                "all": "Repeat all is now on",
                "one": "Repeat one is now on - this song will loop",
            }
            return Response(
                message=messages[mode],
                data={"repeat_mode": mode},
            )
        except Exception as e:
            return Response(message=f"Couldn't change repeat mode: {e!s}", success=False)

    def _get_current_volume(self) -> int:
        """Read the current volume from the music player.

        Supports multiple music player types:
        - MusicPlayer / ControlSurface: state() returns PlayerState with .volume attribute
        - MusicControllerAdapter: state() returns a dict with "volume" key
        - Mocks / adapters: may expose a .volume or ._volume attribute directly
        """
        music = self.context.music
        # Try state() method first
        if hasattr(music, "state") and callable(music.state):
            try:
                state = music.state()
                # PlayerState / objects with .volume attribute
                vol = getattr(state, "volume", None)
                if isinstance(vol, (int, float)):
                    return int(vol)
                # MusicControllerAdapter returns a dict from state()
                if isinstance(state, dict):
                    vol = state.get("volume")
                    if isinstance(vol, (int, float)):
                        return int(vol)
            except Exception:
                pass
        # Fallback to _volume attribute (MusicPlayer internal)
        vol = getattr(music, "_volume", None)
        if isinstance(vol, (int, float)):
            return int(vol)
        # Fallback to volume attribute (adapters / mocks)
        vol = getattr(music, "volume", None)
        if isinstance(vol, (int, float)):
            return int(vol)
        return 50

    def _format_track_reference(self, track: Any) -> str | None:
        """Return a user-friendly description of the given track metadata."""
        if track is None:
            return None
        if hasattr(track, "model_dump"):
            try:
                track = track.model_dump()
            except Exception as e:
                logger.debug("Track model_dump failed (non-critical): %s", e)
                track = getattr(track, "__dict__", {})
        if not isinstance(track, dict):
            return None

        title = track.get("title") or track.get("name")
        artist = track.get("artist") or track.get("uploader") or track.get("author")

        if title and artist:
            return f"{title} by {artist}"
        return title or artist

    def _build_playlist_response(
        self,
        *,
        playlist_name: str,
        queued: int,
        total: int,
        failed: int,
        first_track: str | None,
    ) -> str:
        """Construct a descriptive response when starting playlist playback."""
        playlist_label = playlist_name or "playlist"
        songs_word = "song" if queued == 1 else "songs"

        if total and queued != total:
            base = f"Playing your {playlist_label} playlist ({queued} of {total} {songs_word} ready)"
        elif total:
            base = f"Playing your {playlist_label} playlist with {total} {songs_word}"
        else:
            base = f"Playing your {playlist_label} playlist with {queued} {songs_word}"

        if failed:
            skipped_word = "track was" if failed == 1 else "tracks were"
            base += f"; {failed} {skipped_word} skipped"

        if first_track:
            base += f". Starting with {first_track}"

        if not base.endswith("."):
            base += "."
        return base

    def _extract_playback_error(self, result: Any) -> str | None:
        """Detect structured playback errors returned by backend helpers."""
        if result is None:
            return None

        if isinstance(result, dict):
            if result.get("ok") is False:
                return str(result.get("error") or result.get("message") or "unknown error")
            error_value = result.get("error")
            if error_value:
                return str(error_value)

        if hasattr(result, "ok") and getattr(result, "ok", True) is False:
            error_attr = getattr(result, "error", None) or getattr(result, "message", None)
            return str(error_attr or "unknown error")

        error_val = getattr(result, "error", None) if hasattr(result, "error") else None
        if error_val:
            return str(error_val)

        if isinstance(result, str) and result.lower().startswith("error"):
            return result

        return None

    async def validate(self) -> bool:
        """Validate music player is available."""
        return self.context.has("music")
