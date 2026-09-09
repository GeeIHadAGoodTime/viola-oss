"""Music and playback instant command handlers."""

from __future__ import annotations

from ._base import *

_NO_MUSIC_TO_RESUME_MESSAGE = "I don't have any music to resume. What would you like to play?"


def _extract_playback_snapshot(music: object) -> tuple[bool | None, object | None]:
    """Return (is_playing, now_playing) from supported music backends."""
    is_playing: bool | None = None
    now_playing: object | None = None

    state_getter = getattr(music, "state", None)
    if callable(state_getter):
        try:
            state = state_getter()
        except Exception:
            log.debug("Failed to read music state for resume guard", exc_info=True)
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


class MusicHandlersMixin:
    """Music and playback instant command handlers."""

    _PROVIDER_NAME_MAP: dict[str, str] = {
        "spotify": "spotify",
        "youtube": "youtube_iframe",
        "youtube music": "youtube_music",
        "local": "local",
        "my library": "local",
        "my local library": "local",
        "local files": "local",
        "local library": "local",
    }

    _PROVIDER_DISPLAY_NAMES: dict[str, str] = {
        "spotify": "Spotify",
        "youtube_iframe": "YouTube",
        "youtube_music": "YouTube Music",
        "local": "Local Library",
    }

    async def play_default_playlist(self, params: dict[str, object]) -> dict[str, object]:
        """Play the user's default playlist using PlaybackSessionController.

        When the active provider is "local", shuffles the local music library
        instead of routing to a YouTube-based playlist (GAP-2).
        """
        # -----------------------------------------------------------
        # GAP-2: If active provider is local, shuffle local library
        # -----------------------------------------------------------
        try:
            from music.providers.active_provider import get_active_music_provider_id

            active_provider = get_active_music_provider_id()
        except Exception:
            active_provider = None

        empty_local_result: dict[str, object] | None = None
        if active_provider == "local":
            local_result = await self._play_local_library_shuffled()
            if local_result.get("ok") or local_result.get("error") != "empty_library":
                return local_result
            empty_local_result = local_result
            log.info("Local library empty for play_default_playlist; falling back to saved playlists")

        try:
            from music.playback_session import get_playback_session_controller
            from music.playlist_manager import get_playlist_manager

            # Get playlist manager and find default playlist
            playlist_mgr = get_playlist_manager()
            _uid = str(params.get("_user_id", "")) or None
            default_playlist_name = playlist_mgr.get_default_playlist(user_id=_uid)

            if not default_playlist_name:
                # No default configured - use first available
                playlists = playlist_mgr.list_playlists(user_id=_uid)
                if playlists:
                    default_playlist_name = next(iter(playlists.keys()))
                    log.info(
                        "No default playlist, using first available: %s",
                        default_playlist_name,
                    )
                else:
                    if empty_local_result is not None:
                        return empty_local_result
                    return {
                        "ok": False,
                        "message": "No playlists saved. Add a playlist first.",
                        "data": {},
                        "error": "no_playlists",
                    }

            # Start playlist session via PlaybackSessionController
            session_controller = get_playback_session_controller()
            success = await session_controller.start_playlist(default_playlist_name, shuffle=True)

            if not success:
                return {
                    "ok": False,
                    "message": f"Couldn't load playlist '{default_playlist_name}'",
                    "data": {},
                    "error": "playlist_load_failed",
                }

            # Get first track and play it
            playlist_session = session_controller.get_playlist_session()
            if playlist_session and playlist_session.remaining_count() > 0:
                first_tracks = playlist_session.pop_next(1)
                if first_tracks:
                    first_track = first_tracks[0]
                    # CRITICAL: Use video_id for exact playback (avoids YouTube search)
                    video_id_value = first_track.get("video_id")
                    if isinstance(video_id_value, str) and video_id_value:
                        # Use embed URL for YouTubeWebBackend compliance
                        url = f"https://www.youtube.com/embed/{video_id_value}?autoplay=1"
                    else:
                        raw_url = first_track.get("url")
                        raw_title = first_track.get("title")
                        url = raw_url if isinstance(raw_url, str) and raw_url else None
                        if url is None and isinstance(raw_title, str) and raw_title:
                            url = raw_title

                    if url is not None and hasattr(self.controller.music, "play"):
                        # Build metadata so the player gets the real title/artist
                        metadata = _build_track_metadata(first_track, video_id_value, query_url=url)
                        play_fn = getattr(self.controller.music, "play", None)
                        if callable(play_fn):
                            result = play_fn(url, metadata=metadata)
                            if asyncio.iscoroutine(result):
                                await result

            total = playlist_session.total_count() if playlist_session else 0
            try:
                from core.activity_tracker import ACTIVITY_MUSIC, get_activity_tracker

                get_activity_tracker().record_start(ACTIVITY_MUSIC)
            except Exception:
                logger.debug("Activity tracker update failed silently", exc_info=True)
            return {
                "ok": True,
                "message": f"Playing {default_playlist_name} ({total} songs, shuffled)",
                "data": {
                    "playlist": default_playlist_name,
                    "total_songs": total,
                },
            }

        except Exception as e:
            log.exception("Failed to play default playlist: %s", e)
            return {
                "ok": False,
                "message": "Couldn't start the playlist. Check that you have music available.",
                "data": {},
                "error": "playlist_play_failed",
            }

    async def play_playlist(self, params: dict[str, object]) -> dict[str, object]:
        """Play a named local playlist — handles 'play my X playlist' / 'play the X playlist'."""
        try:
            import re

            original_text = str(params.get("_original_text", ""))
            match = re.search(
                r"^play\s+(?:my|the)\s+(?P<playlist_name>.+?)\s+playlist\.?$",
                original_text,
                re.I,
            )
            if not match:
                return {
                    "ok": False,
                    "message": "Which playlist?",
                    "data": {},
                    "error": "missing_playlist_name",
                }

            playlist_name = match.group("playlist_name").strip()

            from music.playback_session import get_playback_session_controller
            from music.playlist_manager import get_playlist_manager

            playlist_mgr = get_playlist_manager()
            _uid = str(params.get("_user_id", "")) or None
            playlist_info = playlist_mgr.get_playlist(playlist_name, user_id=_uid)
            if not playlist_info:
                return {
                    "ok": False,
                    "message": "Couldn't find a playlist named '%s'" % playlist_name,
                    "data": {},
                    "error": "playlist_not_found",
                }

            session_controller = get_playback_session_controller()
            success = await session_controller.start_playlist(playlist_name, shuffle=True)
            if not success:
                return {
                    "ok": False,
                    "message": "Couldn't load playlist '%s'" % playlist_name,
                    "data": {},
                    "error": "playlist_load_failed",
                }

            playlist_session = session_controller.get_playlist_session()
            if playlist_session and playlist_session.remaining_count() > 0:
                first_tracks = playlist_session.pop_next(1)
                if first_tracks:
                    first_track = first_tracks[0]
                    video_id_value = first_track.get("video_id")
                    if isinstance(video_id_value, str) and video_id_value:
                        url = "https://www.youtube.com/embed/%s?autoplay=1" % video_id_value
                    else:
                        raw_url = first_track.get("url")
                        raw_title = first_track.get("title")
                        url = raw_url if isinstance(raw_url, str) and raw_url else None
                        if url is None and isinstance(raw_title, str) and raw_title:
                            url = raw_title

                    if url is not None and hasattr(self.controller.music, "play"):
                        metadata = _build_track_metadata(first_track, video_id_value, query_url=url)
                        play_fn = getattr(self.controller.music, "play", None)
                        if callable(play_fn):
                            result = play_fn(url, metadata=metadata)
                            if asyncio.iscoroutine(result):
                                await result

            total = playlist_session.total_count() if playlist_session else 0
            try:
                from core.activity_tracker import ACTIVITY_MUSIC, get_activity_tracker

                get_activity_tracker().record_start(ACTIVITY_MUSIC)
            except Exception:
                logger.debug("Activity tracker update failed silently", exc_info=True)
            return {
                "ok": True,
                "message": "Playing %s (%d songs, shuffled)" % (playlist_name, total),
                "data": {
                    "playlist": playlist_name,
                    "total_songs": total,
                },
            }

        except Exception:
            log.exception("play_playlist failed")
            return {
                "ok": False,
                "message": "Couldn't play that playlist",
                "data": {},
                "error": "play_playlist_failed",
            }

    async def create_playlist(self, params: dict[str, object]) -> dict[str, object]:
        """Handle 'create a playlist called X' voice command.

        Extracts the playlist name from the utterance and prompts the user to
        provide a URL so the playlist can be saved via the MCP tool.
        """
        try:
            original_text = str(params.get("_original_text", ""))
            match = re.search(
                r"(?:create|make|save|add)\s+(?:a\s+)?playlist\s+(?:called|named|as)\s+(?P<playlist_name>.+?)\.?$",
                original_text,
                re.I,
            )
            if not match:
                return {
                    "ok": False,
                    "message": "What would you like to call the playlist?",
                    "data": {},
                    "error": "missing_playlist_name",
                }

            playlist_name = match.group("playlist_name").strip()

            # Actually create the playlist in the local store
            import asyncio as _asyncio

            from core.user_context import get_current_or_device_user_id, user_id_or_none
            from music.playlists.playlist_store import PlaylistStore

            _uid = user_id_or_none(params.get("_user_id")) or get_current_or_device_user_id()

            def _create() -> object:
                store = PlaylistStore()
                return store.create_playlist(_uid, playlist_name)

            try:
                record = await _asyncio.to_thread(_create)
                log.info("Created local playlist %r (id=%s)", playlist_name, record.id)
                return {
                    "ok": True,
                    "message": "Created playlist '%s'." % playlist_name,
                    "data": {"playlist_name": playlist_name, "playlist_id": record.id},
                }
            except ValueError as ve:
                return {
                    "ok": False,
                    "message": str(ve),
                    "data": {"playlist_name": playlist_name},
                    "error": "create_playlist_failed",
                }
        except Exception:
            log.exception("create_playlist handler failed")
            return {
                "ok": False,
                "message": "Couldn't create playlist",
                "data": {},
                "error": "create_playlist_failed",
            }

    async def delete_playlist(self, params: dict[str, object]) -> dict[str, object]:
        """Handle 'delete my X playlist' voice command."""
        try:
            original_text = str(params.get("_original_text", ""))
            match = re.search(
                r"(?:delete|remove)\s+(?:my\s+|the\s+)?(?P<playlist_name>.+?)\s+playlist\.?$",
                original_text,
                re.I,
            )
            if not match:
                return {
                    "ok": False,
                    "message": "Which playlist should I delete?",
                    "data": {},
                    "error": "missing_playlist_name",
                }

            playlist_name = match.group("playlist_name").strip()
            _uid = str(params.get("_user_id", "")) or None

            from music.playlist_manager import get_playlist_manager

            playlist_mgr = get_playlist_manager()
            if not playlist_mgr.get_playlist(playlist_name, user_id=_uid):
                all_names = list(playlist_mgr.list_playlists(user_id=_uid).keys())
                hint = "Available playlists: %s" % ", ".join(all_names) if all_names else "You have no saved playlists."
                return {
                    "ok": False,
                    "message": "Couldn't find a playlist named '%s'. %s" % (playlist_name, hint),
                    "data": {},
                    "error": "playlist_not_found",
                }

            success = playlist_mgr.remove_playlist(playlist_name, user_id=_uid)
            if success:
                return {
                    "ok": True,
                    "message": "Deleted playlist '%s'." % playlist_name,
                    "data": {"playlist": playlist_name},
                }
            return {
                "ok": False,
                "message": "Couldn't delete playlist '%s'" % playlist_name,
                "data": {},
                "error": "delete_failed",
            }
        except Exception:
            log.exception("delete_playlist handler failed")
            return {
                "ok": False,
                "message": "Couldn't delete playlist",
                "data": {},
                "error": "delete_playlist_failed",
            }

    async def list_playlists(self, params: dict[str, object]) -> dict[str, object]:
        """Handle 'list my playlists' voice command."""
        try:
            from music.playlist_manager import get_playlist_manager

            _uid = str(params.get("_user_id", "")) or None
            playlist_mgr = get_playlist_manager()
            playlists = playlist_mgr.list_playlists(user_id=_uid)

            if not playlists:
                return {
                    "ok": True,
                    "message": "You have no saved playlists yet.",
                    "data": {"playlists": []},
                }

            default_name = playlist_mgr.get_default_playlist(user_id=_uid)
            names = list(playlists.keys())
            if default_name and default_name in playlists:
                summary = "You have %d playlist%s: %s. Default is '%s'." % (
                    len(names),
                    "s" if len(names) != 1 else "",
                    ", ".join(names),
                    default_name,
                )
            else:
                summary = "You have %d playlist%s: %s." % (
                    len(names),
                    "s" if len(names) != 1 else "",
                    ", ".join(names),
                )

            return {
                "ok": True,
                "message": summary,
                "data": {"playlists": names, "default": default_name},
            }
        except Exception:
            log.exception("list_playlists handler failed")
            return {
                "ok": False,
                "message": "Couldn't list playlists",
                "data": {},
                "error": "list_playlists_failed",
            }

    async def add_to_playlist(self, params: dict[str, object]) -> dict[str, object]:
        """Add the currently playing track to a named playlist."""
        playlist_name_raw = params.get("playlist_name", "")
        playlist_name = playlist_name_raw.strip() if isinstance(playlist_name_raw, str) else ""
        if not playlist_name:
            return {
                "ok": False,
                "message": "Which playlist should I add this to?",
                "data": {},
                "error": "missing_playlist_name",
            }

        try:
            # Get current player state for track info
            player = self.controller.music
            current_track = None
            track_title = ""
            track_url = ""
            provider_name = ""

            # Try current_track attribute first, then queue
            if hasattr(player, "current_track") and player.current_track:
                ct = player.current_track
                track_title = str(getattr(ct, "title", "") or "")
                track_url = str(getattr(ct, "url", "") or "")
                provider_name = str(getattr(ct, "provider", "") or "")
                current_track = ct
            elif hasattr(player, "queue") and player.queue:
                q = player.queue
                item = getattr(q, "current_item", None) or (q[0] if hasattr(q, "__getitem__") else None)
                if item:
                    track_title = str(getattr(item, "title", "") or getattr(item, "name", "") or "")
                    track_url = str(getattr(item, "url", "") or getattr(item, "stream_url", "") or "")
                    provider_name = str(getattr(item, "provider", "") or "")
                    current_track = item

            if not current_track and not track_title:
                return {
                    "ok": False,
                    "message": "Nothing is playing right now",
                    "data": {},
                    "error": "no_current_track",
                }

            from music.playlist_manager import get_playlist_manager

            _uid = str(params.get("_user_id", "")) or None
            playlist_mgr = get_playlist_manager()
            if not playlist_mgr.get_playlist(playlist_name, user_id=_uid):
                return {
                    "ok": False,
                    "message": "No playlist named '%s' found" % playlist_name,
                    "data": {"playlist": playlist_name},
                    "error": "playlist_not_found",
                }

            track = {"title": track_title, "url": track_url, "provider": provider_name}
            added = playlist_mgr.add_track(playlist_name, track, user_id=_uid)
            if not added:
                return {
                    "ok": False,
                    "message": "Couldn't add track to '%s'" % playlist_name,
                    "data": {"playlist": playlist_name},
                    "error": "add_track_failed",
                }

            log.info("Added '%s' to playlist '%s'", track_title, playlist_name)
            return {
                "ok": True,
                "message": "Added '%s' to %s" % (track_title, playlist_name),
                "data": {"playlist": playlist_name, "track": track_title},
                "error": None,
            }
        except Exception:
            log.exception("add_to_playlist handler failed")
            return {
                "ok": False,
                "message": "Couldn't add track to playlist",
                "data": {},
                "error": "add_to_playlist_failed",
            }

    async def add_track_to_playlist(self, params: dict[str, object]) -> dict[str, object]:
        """Add a specific named track to a named playlist.

        Handles: "add Bohemian Rhapsody to my Chill Vibes playlist",
                 "put Bohemian Rhapsody on my Chill Vibes playlist".

        Unlike add_to_playlist (which adds the currently playing track),
        this handler accepts an explicit track_name captured from the utterance.
        The track is stored with the provided title; URL resolution is deferred
        to playback time so the instant command stays fast.
        """
        track_name_raw = params.get("track_name", "")
        track_name = track_name_raw.strip() if isinstance(track_name_raw, str) else ""
        playlist_name_raw = params.get("playlist_name", "")
        playlist_name = playlist_name_raw.strip() if isinstance(playlist_name_raw, str) else ""

        if not track_name:
            return {
                "ok": False,
                "message": "Which track should I add?",
                "data": {},
                "error": "missing_track_name",
            }
        if not playlist_name:
            return {
                "ok": False,
                "message": "Which playlist should I add it to?",
                "data": {},
                "error": "missing_playlist_name",
            }

        try:
            from music.playlist_manager import get_playlist_manager

            _uid = str(params.get("_user_id", "")) or None
            playlist_mgr = get_playlist_manager()
            if not playlist_mgr.get_playlist(playlist_name, user_id=_uid):
                return {
                    "ok": False,
                    "message": "No playlist named '%s' found. Create it first with 'create a playlist called %s'."
                    % (playlist_name, playlist_name),
                    "data": {"playlist": playlist_name, "track": track_name},
                    "error": "playlist_not_found",
                }

            # Store the track by title; URL/provider will be resolved at playback time.
            track = {"title": track_name, "url": "", "provider": ""}
            added = playlist_mgr.add_track(playlist_name, track, user_id=_uid)
            if not added:
                return {
                    "ok": False,
                    "message": "Couldn't add '%s' to '%s'" % (track_name, playlist_name),
                    "data": {"playlist": playlist_name, "track": track_name},
                    "error": "add_track_failed",
                }

            log.info("Added track '%s' to playlist '%s'", track_name, playlist_name)
            return {
                "ok": True,
                "message": "Added '%s' to %s" % (track_name, playlist_name),
                "data": {"playlist": playlist_name, "track": track_name},
                "error": None,
            }
        except Exception:
            log.exception("add_track_to_playlist handler failed")
            return {
                "ok": False,
                "message": "Couldn't add track to playlist",
                "data": {},
                "error": "add_track_to_playlist_failed",
            }

    async def _play_local_library_shuffled(self) -> dict[str, object]:
        """Shuffle the entire local music library and start playback (GAP-2).

        Fetches all audio tracks from the local library DB, shuffles them,
        plays the first track immediately, and enqueues up to 29 more.
        """
        try:
            from music.providers.local.db import get_local_library_repo

            repo = get_local_library_repo()
            repo.initialize()
            all_tracks = repo.get_all_tracks()

            # Filter to audio-only (skip video files if any)
            audio_tracks = [t for t in all_tracks if t.get("media_type", "audio") == "audio"]

            if not audio_tracks:
                return {
                    "ok": False,
                    "message": (
                        "Your local music library is empty. " "Tell me where your music files are and I'll set it up."
                    ),
                    "data": {},
                    "error": "empty_library",
                }

            random.shuffle(audio_tracks)

            # Cap at 30 tracks to keep the queue manageable
            batch = audio_tracks[:30]

            music = self.controller.music
            if music is None or not hasattr(music, "play"):
                return {
                    "ok": False,
                    "message": "Music player not available",
                    "data": {},
                    "error": "no_music_player",
                }

            # Play first track
            first = batch[0]
            file_path = first["file_path"]
            metadata = {
                "title": first.get("title") or first.get("file_name", "Unknown"),
                "artist": first.get("artist") or "Unknown Artist",
                "album": first.get("album") or "",
                "provider": "local",
            }
            artwork = first.get("artwork_data")
            if artwork:
                metadata["artwork_url"] = artwork

            play_fn = getattr(music, "play", None)
            if callable(play_fn):
                result = play_fn(file_path, "local", metadata=metadata, interrupt=True)
                if asyncio.iscoroutine(result):
                    await result

            # Enqueue remaining tracks
            enqueue_fn = getattr(music, "enqueue", None)
            if callable(enqueue_fn):
                for track in batch[1:]:
                    t_path = track["file_path"]
                    t_meta = {
                        "title": track.get("title") or track.get("file_name", "Unknown"),
                        "artist": track.get("artist") or "Unknown Artist",
                        "album": track.get("album") or "",
                        "provider": "local",
                    }
                    t_art = track.get("artwork_data")
                    if t_art:
                        t_meta["artwork_url"] = t_art
                    eq_result = enqueue_fn(t_path, "local", metadata=t_meta)
                    if asyncio.iscoroutine(eq_result):
                        await eq_result

            try:
                from core.activity_tracker import ACTIVITY_MUSIC, get_activity_tracker

                get_activity_tracker().record_start(ACTIVITY_MUSIC)
            except Exception:
                logger.debug("Activity tracker update failed silently", exc_info=True)

            log.info(
                "Local library shuffle: playing %d of %d tracks",
                len(batch),
                len(audio_tracks),
            )
            return {
                "ok": True,
                "message": "Shuffling your local library (%d songs)" % len(batch),
                "data": {
                    "provider": "local",
                    "total_songs": len(batch),
                    "library_size": len(audio_tracks),
                },
            }

        except Exception as exc:
            log.exception("Failed to play local library: %s", exc)
            return {
                "ok": False,
                "message": "Couldn't start local library playback",
                "data": {},
                "error": "local_library_failed",
            }

    async def stop(self, params: dict[str, object]) -> dict[str, object]:
        """Stop playback."""
        try:
            await _call_maybe_async(self.controller.music, "stop")
            try:
                from core.activity_tracker import ACTIVITY_MUSIC, get_activity_tracker

                get_activity_tracker().record_stop(ACTIVITY_MUSIC)
            except Exception:
                logger.debug("Activity tracker stop failed silently", exc_info=True)
            return {
                "ok": True,
                "message": _vary("stopped", "Playback stopped"),
                "data": {},
            }
        except Exception:
            log.exception("Command 'stop' failed")
            return {
                "ok": False,
                "message": "Couldn't stop playback right now. Try again?",
                "data": {},
                "error": "stop_failed",
            }

    async def pause(self, params: dict[str, object]) -> dict[str, object]:
        """Pause playback."""
        try:
            await _call_maybe_async(self.controller.music, "pause")
            return {
                "ok": True,
                "message": _vary("paused", "Playback paused"),
                "data": {},
            }
        except Exception:
            log.exception("Command 'pause' failed")
            return {
                "ok": False,
                "message": "Couldn't pause playback right now. Try again?",
                "data": {},
                "error": "pause_failed",
            }

    async def resume(self, params: dict[str, object]) -> dict[str, object]:
        """Resume playback."""
        try:
            pre_is_playing, pre_track = _extract_playback_snapshot(self.controller.music)
            if pre_track is None and pre_is_playing is not True:
                return {
                    "ok": False,
                    "message": _NO_MUSIC_TO_RESUME_MESSAGE,
                    "data": {},
                    "error": "nothing_to_resume",
                }
            if pre_is_playing is True and pre_track is not None:
                return {
                    "ok": True,
                    "message": "Music is already playing",
                    "data": {},
                }

            await _call_maybe_async(self.controller.music, "resume")
            post_is_playing, post_track = _extract_playback_snapshot(self.controller.music)
            if post_track is None:
                return {
                    "ok": False,
                    "message": _NO_MUSIC_TO_RESUME_MESSAGE,
                    "data": {},
                    "error": "nothing_to_resume",
                }
            if post_is_playing is False:
                return {
                    "ok": False,
                    "message": "Couldn't resume playback. Try saying 'play' instead?",
                    "data": {},
                    "error": "resume_failed",
                }
            return {
                "ok": True,
                "message": _vary("resumed", "Music resumed"),
                "data": {},
            }
        except Exception:
            log.exception("Command 'resume' failed")
            return {
                "ok": False,
                "message": "Couldn't resume playback. Try saying 'play' instead?",
                "data": {},
                "error": "resume_failed",
            }

    async def skip(self, params: dict[str, object]) -> dict[str, object]:
        """Skip to next track."""
        try:
            await _call_maybe_async(self.controller.music, "skip")
            return {
                "ok": True,
                "message": "Skipped to next track",
                "data": {},
            }
        except Exception:
            log.exception("Command 'skip' failed")
            return {
                "ok": False,
                "message": "Couldn't skip to the next track. Try again?",
                "data": {},
                "error": "skip_failed",
            }

    async def previous(self, params: dict[str, object]) -> dict[str, object]:
        """Go to previous track."""
        try:
            await _call_maybe_async(self.controller.music, "previous")
            return {
                "ok": True,
                "message": "Went to previous track",
                "data": {},
            }
        except Exception:
            log.exception("Command 'previous' failed")
            return {
                "ok": False,
                "message": "Couldn't go back to the previous track. Try again?",
                "data": {},
                "error": "previous_failed",
            }

    async def restart(self, params: dict[str, object]) -> dict[str, object]:
        """Restart current track by seeking to position 0."""
        try:
            music = self.controller.music
            backend_seek_ok = False
            try:
                if hasattr(music, "seek"):
                    result = music.seek(0)
                    if asyncio.iscoroutine(result):
                        await result
                    backend_seek_ok = True
                elif hasattr(music, "_playback_controller") and hasattr(music._playback_controller, "seek"):
                    music._playback_controller.seek(0)
                    backend_seek_ok = True
                elif hasattr(music, "_backend") and hasattr(music._backend, "seek"):
                    music._backend.seek(0)
                    backend_seek_ok = True
            except Exception as seek_exc:
                log.debug(
                    "Backend seek failed (will broadcast to iframe): %s",
                    seek_exc,
                )

            if not backend_seek_ok:
                # No backend handled the seek — update internal position
                player = getattr(music, "player", None)
                if player is not None:
                    player._position_ms = 0

            # Broadcast seek to WS clients (YouTube iframe, spokes)
            await self._broadcast_seek(0)

            return {
                "ok": True,
                "message": "Restarted current track",
                "data": {"position_seconds": 0},
            }
        except Exception:
            log.exception("Command 'restart' failed")
            return {
                "ok": False,
                "message": "Couldn't restart the track. Try saying 'play' again?",
                "data": {},
                "error": "restart_failed",
            }

    def _parse_time_to_seconds(text: str) -> int | None:
        """Parse a time expression from the original text and return seconds.

        Supported formats:
        - MM:SS or H:MM:SS  (e.g. "2:00", "1:30:00")
        - Plain integer seconds (e.g. "120")
        - Natural language: "1 minute 30 seconds", "2 hours 5 min 10 sec"

        Returns:
            Total seconds as an integer, or None if parsing fails.
        """
        text = text.strip()

        # Try MM:SS or H:MM:SS
        ts_match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if ts_match:
            parts = ts_match.groups()
            if parts[2] is not None:
                # H:MM:SS
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                # MM:SS
                return int(parts[0]) * 60 + int(parts[1])

        # Try natural language: "1 hour 30 minutes 10 seconds"
        hours = 0
        minutes = 0
        seconds = 0
        h_match = re.search(r"(\d+)\s*h(?:ours?)?", text)
        m_match = re.search(r"(\d+)\s*m(?:in(?:ute)?s?)?", text)
        s_match = re.search(r"(\d+)\s*s(?:ec(?:ond)?s?)?", text)
        if h_match:
            hours = int(h_match.group(1))
        if m_match:
            minutes = int(m_match.group(1))
        if s_match:
            seconds = int(s_match.group(1))

        if hours or minutes or seconds:
            return hours * 3600 + minutes * 60 + seconds

        # Try plain integer (seconds)
        plain_match = re.search(r"(\d+)", text)
        if plain_match:
            return int(plain_match.group(1))

        return None

    def _format_time(seconds: int) -> str:
        """Format seconds as a human-readable time string (M:SS or H:MM:SS)."""
        if seconds < 0:
            seconds = 0
        if seconds >= 3600:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            return "%d:%02d:%02d" % (h, m, s)
        m = seconds // 60
        s = seconds % 60
        return "%d:%02d" % (m, s)

    def _get_current_position_ms(self) -> int:
        """Get current playback position in milliseconds.

        Supports multiple music player types via state() or _position_ms.
        """
        music = self.controller.music
        # Try state() method first
        if hasattr(music, "state") and callable(music.state):
            try:
                state = music.state()
                # PlayerState objects have .position (in seconds)
                pos = getattr(state, "position", None)
                if isinstance(pos, (int, float)) and pos >= 0:
                    return int(pos * 1000)
                # Dict-based state (MusicControllerAdapter)
                if isinstance(state, dict):
                    pos_ms = state.get("position_ms")
                    if isinstance(pos_ms, (int, float)) and pos_ms >= 0:
                        return int(pos_ms)
                    pos = state.get("position")
                    if isinstance(pos, (int, float)) and pos >= 0:
                        return int(pos * 1000)
            except Exception:
                logger.debug("Failed to get position from state, using fallback", exc_info=True)
        # Fallback to _position_ms attribute
        pos_ms = getattr(music, "_position_ms", None)
        if isinstance(pos_ms, (int, float)):
            return int(pos_ms)
        return 0

    async def seek_absolute(self, params: dict[str, object]) -> dict[str, object]:
        """Seek to an absolute position in the current track."""
        try:
            position_seconds = None

            # Accept position from LLM params (seek command sends {"seconds": N} or {"position": N})
            for key in ("seconds", "position"):
                raw = params.get(key)
                if raw is not None:
                    try:
                        position_seconds = int(raw)
                    except (ValueError, TypeError) as exc:
                        logger.debug("seek %s param coerce skip on %r: %s", key, raw, exc)
                    break

            if position_seconds is None:
                # Fallback: extract from original text (instant command path)
                original_text = str(params.get("_original_text", ""))
                position_seconds = self._parse_time_to_seconds(original_text)

            if position_seconds is None:
                return {
                    "ok": False,
                    "message": "Couldn't understand the time position",
                    "data": {},
                    "error": "invalid_time",
                }

            if position_seconds < 0:
                position_seconds = 0

            # Call seek on the music player (position in milliseconds).
            # For YouTube iframe playback there is no backend, so
            # music.seek() raises "No active backend".  We catch this
            # and fall through to the WS broadcast which is the only
            # thing the iframe needs.
            music = self.controller.music
            backend_seek_ok = False
            try:
                if hasattr(music, "seek"):
                    result = music.seek(position_seconds * 1000)
                    if asyncio.iscoroutine(result):
                        await result
                    backend_seek_ok = True
                elif hasattr(music, "_playback_controller") and hasattr(music._playback_controller, "seek"):
                    music._playback_controller.seek(position_seconds * 1000)
                    backend_seek_ok = True
                elif hasattr(music, "_backend") and hasattr(music._backend, "seek"):
                    music._backend.seek(position_seconds)
                    backend_seek_ok = True
            except Exception as seek_exc:
                log.debug(
                    "Backend seek failed (will broadcast to iframe): %s",
                    seek_exc,
                )

            if not backend_seek_ok:
                # No backend handled the seek — update internal position
                # so state broadcasts reflect the new position.
                player = getattr(music, "player", None)
                if player is not None:
                    player._position_ms = int(position_seconds * 1000)

            # ALWAYS broadcast seek command to WS clients so the YouTube
            # iframe receives the seekTo postMessage (mirrors /v1/seek
            # REST handler).  This is the primary seek mechanism for
            # iframe playback.
            await self._broadcast_seek(position_seconds)

            time_str = self._format_time(position_seconds)
            return {
                "ok": True,
                "message": "Seeking to %s" % time_str,
                "data": {"position_seconds": position_seconds},
            }
        except Exception:
            log.exception("Command 'seek_absolute' failed")
            return {
                "ok": False,
                "message": "Couldn't jump to that position. The track may not support seeking.",
                "data": {},
                "error": "seek_failed",
            }

    async def seek_relative(self, params: dict[str, object]) -> dict[str, object]:
        """Seek forward or backward relative to the current position."""
        try:
            original_text = str(params.get("_original_text", ""))
            direction = str(params.get("direction", "forward"))

            # Parse the delta from the original text
            delta_seconds = self._parse_relative_delta(original_text)

            if delta_seconds is None or delta_seconds <= 0:
                return {
                    "ok": False,
                    "message": "Couldn't understand the time amount",
                    "data": {},
                    "error": "invalid_time",
                }

            # Get current position
            current_ms = self._get_current_position_ms()
            current_seconds = current_ms // 1000

            if direction == "backward":
                new_position = max(0, current_seconds - delta_seconds)
            else:
                new_position = current_seconds + delta_seconds

            # Call seek.  Same iframe fallback logic as seek_absolute:
            # if backend seek fails we still broadcast to WS clients.
            music = self.controller.music
            backend_seek_ok = False
            try:
                if hasattr(music, "seek"):
                    result = music.seek(new_position * 1000)
                    if asyncio.iscoroutine(result):
                        await result
                    backend_seek_ok = True
                elif hasattr(music, "_playback_controller") and hasattr(music._playback_controller, "seek"):
                    music._playback_controller.seek(new_position * 1000)
                    backend_seek_ok = True
                elif hasattr(music, "_backend") and hasattr(music._backend, "seek"):
                    music._backend.seek(new_position)
                    backend_seek_ok = True
            except Exception as seek_exc:
                log.debug(
                    "Backend seek failed (will broadcast to iframe): %s",
                    seek_exc,
                )

            if not backend_seek_ok:
                # No backend handled the seek — update internal position
                # so state broadcasts reflect the new position.
                player = getattr(music, "player", None)
                if player is not None:
                    player._position_ms = int(new_position * 1000)

            # ALWAYS broadcast seek command to WS clients so the YouTube
            # iframe receives the seekTo postMessage.
            await self._broadcast_seek(new_position)

            if direction == "backward":
                msg = "Rewound %d seconds" % delta_seconds
            else:
                msg = "Skipped ahead %d seconds" % delta_seconds

            return {
                "ok": True,
                "message": msg,
                "data": {
                    "delta_seconds": delta_seconds,
                    "direction": direction,
                    "new_position_seconds": new_position,
                },
            }
        except Exception:
            log.exception("Command 'seek_relative' failed")
            return {
                "ok": False,
                "message": "Couldn't skip ahead. The track may not support seeking.",
                "data": {},
                "error": "seek_relative_failed",
            }

    def _parse_relative_delta(text: str) -> int | None:
        """Parse a relative time delta from text like 'forward 30 seconds'.

        Returns the number of seconds as a positive integer, or None.
        """
        text = text.strip()

        # Check for minutes: "forward 2 minutes", "back 5 min"
        m_match = re.search(r"(\d+)\s*m(?:in(?:ute)?s?)?", text)
        if m_match:
            return int(m_match.group(1)) * 60

        # Check for seconds: "forward 30 seconds", "back 10 sec", "ahead 30"
        s_match = re.search(r"(\d+)\s*(?:s(?:ec(?:ond)?s?)?)?", text)
        if s_match:
            return int(s_match.group(1))

        return None

    def _get_current_volume(self) -> int:
        """Read current volume from the music player (canonical source).

        Supports multiple music player types:
        - MusicPlayer / ControlSurface: state() returns PlayerState with .volume
        - MusicControllerAdapter: state() returns a dict with "volume" key
        - Mocks / adapters: may expose a .volume or ._volume attribute directly
        """
        music = self.controller.music
        # Try state() method first (MusicPlayer / ControlSurface)
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
                logger.debug("Failed to get volume from state, using fallback", exc_info=True)
        # Fallback to _volume attribute (MusicPlayer internal)
        vol = getattr(music, "_volume", None)
        if isinstance(vol, (int, float)):
            return int(vol)
        # Fallback to volume attribute (adapters / mocks)
        vol = getattr(music, "volume", None)
        if isinstance(vol, (int, float)):
            return int(vol)
        return 50

    async def volume_up(self, params: dict[str, object]) -> dict[str, object]:
        """Increase volume."""
        try:
            current_vol = self._get_current_volume()
            new_vol = min(100, current_vol + 10)
            result = self.controller.music.set_volume(new_vol)
            if asyncio.iscoroutine(result):
                result = await result
            return {
                "ok": True,
                "message": f"Volume increased to {new_vol}%",
                "data": {"volume": new_vol},
            }
        except Exception as e:
            log.exception("Command 'volume_up' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't change the volume. Check your audio settings.",
                "data": {},
                "error": "volume_up_failed",
            }

    async def volume_down(self, params: dict[str, object]) -> dict[str, object]:
        """Decrease volume."""
        try:
            current_vol = self._get_current_volume()
            new_vol = max(0, current_vol - 10)
            result = self.controller.music.set_volume(new_vol)
            if asyncio.iscoroutine(result):
                result = await result
            return {
                "ok": True,
                "message": f"Volume decreased to {new_vol}%",
                "data": {"volume": new_vol},
            }
        except Exception as e:
            log.exception("Command 'volume_down' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't change the volume. Check your audio settings.",
                "data": {},
                "error": "volume_down_failed",
            }

    async def set_volume(self, params: dict[str, object]) -> dict[str, object]:
        """Set volume to a specific level."""
        try:
            # Accept level from LLM params (volume_set command sends {"level": N})
            level_param = params.get("level")
            if level_param is not None:
                try:
                    level = int(level_param)
                except (ValueError, TypeError):
                    level_param = None

            if level_param is None:
                # Fallback: extract from original text (instant command path)
                original_text = str(params.get("_original_text", ""))
                level_match = re.search(r"(\d{1,3})", original_text)
                if not level_match:
                    return {
                        "ok": False,
                        "message": "Please specify a volume level (0-100).",
                        "data": {},
                        "error": "missing_volume_level",
                    }
                level = int(level_match.group(1))
            level = max(0, min(100, level))
            result = self.controller.music.set_volume(level)
            if asyncio.iscoroutine(result):
                result = await result
            return {
                "ok": True,
                "message": "Volume set to %d%%" % level,
                "data": {"volume": level},
            }
        except Exception as e:
            log.exception("Command 'set_volume' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't set the volume. Check your audio settings.",
                "data": {},
                "error": "set_volume_failed",
            }

    async def mute(self, params: dict[str, object]) -> dict[str, object]:
        """Mute audio."""
        try:
            # Save current volume before muting so unmute can restore it
            self._pre_mute_volume = self._get_current_volume()
            # Prefer proper mute() method if available
            if hasattr(self.controller.music, "mute"):
                result = self.controller.music.mute()
                if asyncio.iscoroutine(result):
                    result = await result
                return {
                    "ok": True,
                    "message": "Audio muted",
                    "data": {"muted": True},
                }
            # Fallback to setting volume to 0
            elif hasattr(self.controller.music, "set_volume"):
                result = self.controller.music.set_volume(0)
                if asyncio.iscoroutine(result):
                    result = await result
                return {
                    "ok": True,
                    "message": "Audio muted",
                    "data": {"volume": 0},
                }
        except Exception as e:
            log.exception("Command 'mute' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't mute the audio. Check your audio settings.",
                "data": {},
                "error": "mute_failed",
            }
        return {
            "ok": False,
            "message": "This music provider doesn't support mute. Want me to set the volume to zero instead?",
            "data": {},
            "error": "mute_not_supported",
        }

    async def unmute(self, params: dict[str, object]) -> dict[str, object]:
        """Unmute audio."""
        try:
            # Prefer proper unmute() method if available
            if hasattr(self.controller.music, "unmute"):
                result = self.controller.music.unmute()
                if asyncio.iscoroutine(result):
                    result = await result
                self._pre_mute_volume = None
                return {
                    "ok": True,
                    "message": "Audio unmuted",
                    "data": {"muted": False},
                }
            # Fallback to restoring previous volume
            elif hasattr(self.controller.music, "set_volume"):
                prev_vol = getattr(self, "_pre_mute_volume", None)
                if prev_vol is None:
                    prev_vol = self._get_current_volume() or 50
                new_vol = max(10, prev_vol)  # Ensure at least 10%
                result = self.controller.music.set_volume(new_vol)
                if asyncio.iscoroutine(result):
                    result = await result
                self._pre_mute_volume = None
                return {
                    "ok": True,
                    "message": f"Audio unmuted (volume {new_vol}%)",
                    "data": {"volume": new_vol},
                }
        except Exception as e:
            log.exception("Command 'unmute' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't unmute the audio. Check your audio settings.",
                "data": {},
                "error": "unmute_failed",
            }
        return {
            "ok": False,
            "message": "This provider doesn't support unmute. Want me to turn the volume back up?",
            "data": {},
            "error": "unmute_not_supported",
        }

    async def what_playing(self, params: dict[str, object]) -> dict[str, object]:
        """Show what's currently playing."""
        try:
            if hasattr(self.controller.music, "state") and callable(self.controller.music.state):
                player_state = self.controller.music.state()
                # MusicControllerAdapter.state() returns a dict, while
                # MusicPlayer.state() returns a PlayerState object.  Handle both.
                if isinstance(player_state, dict):
                    now_playing = player_state.get("now_playing")
                else:
                    now_playing = getattr(player_state, "now_playing", None) if player_state else None
                if now_playing:
                    if isinstance(now_playing, dict):
                        title = now_playing.get("title", "Unknown")
                        artist = now_playing.get("artist", "")
                    else:
                        title = getattr(now_playing, "title", "Unknown")
                        artist = getattr(now_playing, "artist", "")
                    message = f"Now playing: {title}"
                    if artist:
                        message += f" by {artist}"

                    return {
                        "ok": True,
                        "message": message,
                        "data": {
                            "title": title,
                            "artist": artist,
                            "track": now_playing,
                        },
                    }
                else:
                    return {
                        "ok": True,
                        "message": "Nothing is currently playing",
                        "data": {},
                    }
            else:
                return {
                    "ok": False,
                    "message": "Music player not available",
                    "data": {},
                    "error": "no_music_player",
                }
        except Exception as e:
            log.exception("Command 'what_playing' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't check what's playing right now. Try again?",
                "data": {},
                "error": "what_playing_failed",
            }

    async def show_queue(self, params: dict[str, object]) -> dict[str, object]:
        """Show music queue."""
        try:
            if hasattr(self.controller.music, "state") and callable(self.controller.music.state):
                player_state = self.controller.music.state()
                # MusicControllerAdapter.state() returns a dict; handle both types.
                if isinstance(player_state, dict):
                    queue = player_state.get("queue", [])
                else:
                    queue = getattr(player_state, "queue", []) if player_state else []
                if queue:
                    tracks = []
                    for i, item in enumerate(queue[:10]):  # Show first 10
                        if isinstance(item, dict):
                            title = item.get("title", f"Track {i + 1}")
                            artist = item.get("artist", "")
                        else:
                            title = getattr(item, "title", f"Track {i + 1}")
                            artist = getattr(item, "artist", "")
                        track_info = title
                        if artist:
                            track_info += f" by {artist}"
                        tracks.append(f"{i + 1}. {track_info}")

                    message = f"Queue ({len(queue)} tracks):\n" + "\n".join(tracks)
                    if len(queue) > 10:
                        message += f"\n... and {len(queue) - 10} more"

                    return {
                        "ok": True,
                        "message": message,
                        "data": {"queue": queue, "count": len(queue)},
                    }
                else:
                    return {
                        "ok": True,
                        "message": "Queue is empty",
                        "data": {"queue": [], "count": 0},
                    }
            else:
                return {
                    "ok": False,
                    "message": "Music player not available",
                    "data": {},
                    "error": "no_music_player",
                }
        except Exception as e:
            log.exception("Command 'show_queue' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't load the play queue right now. Try again?",
                "data": {},
                "error": "show_queue_failed",
            }

    async def clear_queue(self, params: dict[str, object]) -> dict[str, object]:
        """Clear music queue."""
        try:
            if hasattr(self.controller.music, "clear_queue"):
                await _call_maybe_async(self.controller.music, "clear_queue")
                return {
                    "ok": True,
                    "message": "Queue cleared",
                    "data": {},
                }
            else:
                return {
                    "ok": False,
                    "message": "Queue clearing isn't available with this provider. I can skip tracks or start a new playlist instead.",
                    "data": {},
                    "error": "no_clear_queue",
                }
        except Exception as e:
            log.exception("Command 'clear_queue' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't clear the queue right now. Try again?",
                "data": {},
                "error": "clear_queue_failed",
            }

    def _get_now_playing(self) -> object | None:
        """Return the now_playing object from player state, or None."""
        music = self.controller.music
        if not hasattr(music, "state") or not callable(music.state):
            return None
        player_state = music.state()
        if isinstance(player_state, dict):
            return player_state.get("now_playing")
        return getattr(player_state, "now_playing", None) if player_state else None

    def _get_local_track_id(self, now_playing: object) -> int | None:
        """Extract the local library row ID from a now_playing item.

        Returns the integer library ID if the current track is a local
        provider track, otherwise None.
        """
        provider = (
            now_playing.get("provider") if isinstance(now_playing, dict) else getattr(now_playing, "provider", None)
        )
        if provider != "local":
            return None

        # The URL for local tracks IS the file path.
        url = now_playing.get("url") if isinstance(now_playing, dict) else getattr(now_playing, "url", None)
        if not url or not isinstance(url, str):
            return None

        try:
            from music.providers.local.db import get_local_library_repo

            repo = get_local_library_repo()
            repo.initialize()
            row = repo.get_track_by_path(url)
            if row:
                return int(row["id"])
        except Exception:
            log.exception("Failed to look up local track by path: %s", url)
        return None

    def _get_youtube_video_id(self, now_playing: object) -> str | None:
        """Extract the YouTube video ID from a now_playing item.

        Works for both dict and object representations.  Returns None if the
        current track is not a YouTube provider track or has no video_id.
        """
        if now_playing is None:
            return None
        provider = (
            now_playing.get("provider") if isinstance(now_playing, dict) else getattr(now_playing, "provider", None)
        )
        if provider not in ("youtube_music", "youtube"):
            return None
        # QueueItem serialises as dict with 'video_id' key; model instances have .video_id
        video_id = (
            now_playing.get("video_id") if isinstance(now_playing, dict) else getattr(now_playing, "video_id", None)
        )
        if video_id and isinstance(video_id, str):
            return video_id
        # Fallback: stream_token == video_id for YouTube provider
        token = (
            now_playing.get("stream_token")
            if isinstance(now_playing, dict)
            else getattr(now_playing, "stream_token", None)
        )
        if token and isinstance(token, str):
            return token
        return None

    def _get_spotify_track_id(self, now_playing: object) -> str | None:
        """Extract the Spotify track ID from a now_playing item.

        Returns the bare track ID (e.g. '4iV5W9uYEdYUVa79Axb7Rh') if the
        current track is a Spotify track, otherwise None.

        The URL for Spotify CDP tracks is 'https://open.spotify.com/track/{id}'.
        """
        if now_playing is None:
            return None
        provider = (
            now_playing.get("provider") if isinstance(now_playing, dict) else getattr(now_playing, "provider", None)
        )
        if provider != "spotify":
            return None

        url = now_playing.get("url") if isinstance(now_playing, dict) else getattr(now_playing, "url", None)
        if url and isinstance(url, str) and "open.spotify.com/track/" in url:
            return url.rstrip("/").split("/")[-1].split("?")[0]

        # Fallback: video_id field (sometimes populated with track ID)
        video_id = (
            now_playing.get("video_id") if isinstance(now_playing, dict) else getattr(now_playing, "video_id", None)
        )
        if video_id and isinstance(video_id, str):
            return video_id

        return None

    async def _spotify_save_track(self, track_id: str, *, save: bool) -> None:
        """Call the Spotify Web API to save (like) or unsave (unlike) a track.

        Args:
            track_id: Bare Spotify track ID.
            save: True to save (PUT /me/tracks), False to remove (DELETE /me/tracks).

        Raises:
            RuntimeError: If no access token is available or the API call fails.
        """
        import httpx

        from music.consent import get_consent_service

        service = get_consent_service()
        token_data = service.resolve_access_token("spotify")
        if not token_data or not token_data.get("access_token"):
            raise RuntimeError("Your Spotify session is not active. I can start the sign-in flow.")

        access_token = token_data["access_token"]
        url = "https://api.spotify.com/v1/me/tracks"
        headers = {"Authorization": "Bearer %s" % access_token, "Content-Type": "application/json"}
        params = {"ids": track_id}

        async with httpx.AsyncClient(timeout=10.0) as client:
            if save:
                response = await client.put(url, headers=headers, params=params)
            else:
                response = await client.delete(url, headers=headers, params=params)

        if response.status_code == 401:
            raise RuntimeError("Your Spotify session has expired. I can start the reconnection flow.")
        if response.status_code == 403:
            raise RuntimeError(
                "Spotify doesn't have permission to save tracks. " "I can reconnect Spotify with the right permissions."
            )
        if response.status_code not in (200, 201, 204):
            raise RuntimeError("Spotify API error %d: %s" % (response.status_code, response.text[:200]))

    def _rate_youtube_video(self, video_id: str, rating: str) -> dict[str, object]:
        """Record YouTube rating intent locally without calling YouTube APIs.

        The previous implementation used the external YouTube rating endpoint.
        Launch posture forbids that outbound path, so this keeps the user value
        by updating Viola's local preference store only.
        """
        log.info("YouTube rating kept local: video_id=%s rating=%s", video_id, rating)
        return {
            "ok": True,
            "message": "",
            "local_only": True,
        }

    def _get_track_metadata(self, now_playing: object) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract (title, artist, provider, url) from a now_playing object.

        Works for both dict and attribute-style representations.  All values
        may be None when the field is absent.
        """
        if now_playing is None:
            return None, None, None, None
        if isinstance(now_playing, dict):
            return (
                now_playing.get("title"),
                now_playing.get("artist"),
                now_playing.get("provider"),
                now_playing.get("url"),
            )
        return (
            getattr(now_playing, "title", None),
            getattr(now_playing, "artist", None),
            getattr(now_playing, "provider", None),
            getattr(now_playing, "url", None),
        )

    def _record_like_locally(
        self,
        now_playing: object,
        provider_track_id: str,
        user_id: str | None = None,
    ) -> None:
        """Persist a like in Viola's cross-provider LikesStore.

        Called after every successful thumbs-up, regardless of provider.
        Errors are logged and swallowed — the primary like action must not
        be blocked by a local-store failure.
        """
        try:
            from core.user_context import get_current_or_device_user_id, user_id_or_none
            from music.playlists.likes_store import LikesStore

            resolved_user_id = user_id_or_none(user_id) or get_current_or_device_user_id()
            title, artist, provider, url = self._get_track_metadata(now_playing)
            provider = provider or "unknown"
            store = LikesStore()
            store.add_like(
                user_id=resolved_user_id,
                title=title,
                artist=artist,
                provider=provider,
                provider_track_id=str(provider_track_id),
                url=url,
            )
        except Exception:
            log.exception("LikesStore.add_like failed — local record not saved")

    def _record_unlike_locally(
        self,
        now_playing: object,
        provider_track_id: str,
        user_id: str | None = None,
    ) -> None:
        """Mark a track as unliked in Viola's cross-provider LikesStore.

        Called after every thumbs-down, regardless of provider.  Errors are
        logged and swallowed so the primary dislike action is not blocked.
        """
        try:
            from core.user_context import get_current_or_device_user_id, user_id_or_none
            from music.playlists.likes_store import LikesStore

            resolved_user_id = user_id_or_none(user_id) or get_current_or_device_user_id()
            _, _, provider, _ = self._get_track_metadata(now_playing)
            provider = provider or "unknown"
            store = LikesStore()
            store.remove_like(user_id=resolved_user_id, provider=provider, provider_track_id=str(provider_track_id))
        except Exception:
            log.exception("LikesStore.remove_like failed — local record not updated")

    async def thumbs_up(self, params: dict[str, object]) -> dict[str, object]:
        """Thumbs up current song.

        - Local provider: persists the like in the local library database.
        - YouTube provider: saves the preference locally in Viola.
        - Other providers: honest error message.
        """
        from core.user_context import get_current_or_device_user_id, user_id_or_none

        user_id = user_id_or_none(params.get("_user_id")) or get_current_or_device_user_id()
        try:
            now_playing = self._get_now_playing()
            local_id = self._get_local_track_id(now_playing) if now_playing else None

            if local_id is not None:
                import asyncio

                from music.providers.local.db import get_local_library_repo

                def _like() -> None:
                    repo = get_local_library_repo()
                    repo.initialize()
                    repo.add_like(local_id)

                await asyncio.to_thread(_like)

                title = (
                    now_playing.get("title") if isinstance(now_playing, dict) else getattr(now_playing, "title", None)
                ) or "this song"
                log.info("Liked local track: %s (library_id=%d)", title, local_id)
                self._record_like_locally(now_playing, str(local_id), user_id)
                return {
                    "ok": True,
                    "message": "Liked %s" % title,
                    "data": {
                        "rating": "thumbs_up",
                        "provider": "local",
                        "track_id": local_id,
                    },
                }

            # YouTube provider: keep the preference local; no YouTube Data API call.
            youtube_video_id = self._get_youtube_video_id(now_playing)
            if youtube_video_id:
                import asyncio

                result = await asyncio.to_thread(self._rate_youtube_video, youtube_video_id, "like")
                title = (
                    (now_playing.get("title") if isinstance(now_playing, dict) else getattr(now_playing, "title", None))
                    if now_playing
                    else None
                )
                if result["ok"]:
                    log.info("Liked YouTube video locally: video_id=%s", youtube_video_id)
                    self._record_like_locally(now_playing, youtube_video_id, user_id)
                    return {
                        "ok": True,
                        "message": "Saved%s to Viola likes." % (' "%s"' % title if title else ""),
                        "data": {
                            "rating": "thumbs_up",
                            "provider": "youtube_music",
                            "video_id": youtube_video_id,
                            "persisted": "local",
                        },
                    }
                # Surface the specific error (auth, network, etc.)
                return {
                    "ok": False,
                    "message": result["message"],
                    "data": {"rating": "thumbs_up", "provider": "youtube_music", "video_id": youtube_video_id},
                    "error": result.get("error", "youtube_error"),
                }

            # Spotify provider — save track via Spotify Web API
            spotify_track_id = self._get_spotify_track_id(now_playing)
            if spotify_track_id:
                title = (
                    (now_playing.get("title") if isinstance(now_playing, dict) else getattr(now_playing, "title", None))
                    if now_playing
                    else None
                )
                try:
                    await self._spotify_save_track(spotify_track_id, save=True)
                    log.info("Liked Spotify track via Web API: %s (%s)", title, spotify_track_id)
                    self._record_like_locally(now_playing, spotify_track_id, user_id)
                    return {
                        "ok": True,
                        "message": "Liked%s on Spotify." % (' "%s"' % title if title else ""),
                        "data": {
                            "rating": "thumbs_up",
                            "provider": "spotify",
                            "track_id": spotify_track_id,
                        },
                    }
                except RuntimeError as api_err:
                    log.warning("Spotify like API failed: %s", api_err)
                    return {
                        "ok": False,
                        "message": str(api_err),
                        "data": {"rating": "thumbs_up", "provider": "spotify", "persisted": False},
                        "error": "spotify_api_error",
                    }

            # Non-local, non-YouTube, non-Spotify provider
            provider = (
                (
                    now_playing.get("provider")
                    if isinstance(now_playing, dict)
                    else getattr(now_playing, "provider", None)
                )
                if now_playing
                else None
            )
            provider_label = str(provider).capitalize() if provider else "this provider"
            return {
                "ok": False,
                "message": "Likes aren't supported on %s yet, but I've noted your preference and will remember it."
                % provider_label,
                "data": {"rating": "thumbs_up", "provider": provider, "persisted": False},
                "error": "like_not_supported",
            }
        except Exception as e:
            log.exception("Command 'thumbs_up' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't save your rating right now. Try again?",
                "data": {},
                "error": "rating_failed",
            }

    async def thumbs_down(self, params: dict[str, object]) -> dict[str, object]:
        """Thumbs down current song.

        user_id: extracted from params["_user_id"]
        - Local provider: removes the track from liked songs, then skips.
        - YouTube provider: saves the preference locally, then skips.
        - Spotify provider: removes the track from Spotify library via Web API,
          then skips.
        - Other providers: skips and gives an honest message.
        """
        from core.user_context import get_current_or_device_user_id, user_id_or_none

        user_id = user_id_or_none(params.get("_user_id")) or get_current_or_device_user_id()
        try:
            now_playing = self._get_now_playing()
            local_id = self._get_local_track_id(now_playing) if now_playing else None

            if local_id is not None:
                import asyncio

                from music.providers.local.db import get_local_library_repo

                def _unlike() -> None:
                    repo = get_local_library_repo()
                    repo.initialize()
                    repo.remove_like(local_id)

                await asyncio.to_thread(_unlike)
                log.info("Unliked local track: library_id=%d", local_id)
                self._record_unlike_locally(now_playing, str(local_id), user_id)

            # Determine provider for messaging and provider-specific rating
            now_playing_np = self._get_now_playing()
            provider_np = (
                (
                    now_playing_np.get("provider")
                    if isinstance(now_playing_np, dict)
                    else getattr(now_playing_np, "provider", None)
                )
                if now_playing_np
                else None
            )
            is_local = local_id is not None

            # YouTube provider: keep the preference local; no YouTube Data API call.
            youtube_video_id = self._get_youtube_video_id(now_playing_np) if not is_local else None
            youtube_rated = False
            youtube_error_msg: str | None = None
            if youtube_video_id:
                import asyncio

                yt_result = await asyncio.to_thread(self._rate_youtube_video, youtube_video_id, "dislike")
                if yt_result["ok"]:
                    youtube_rated = True
                    log.info("Disliked YouTube video locally: video_id=%s", youtube_video_id)
                    self._record_unlike_locally(now_playing_np, youtube_video_id, user_id)
                else:
                    youtube_error_msg = yt_result.get("message", "")
                    log.warning("YouTube dislike failed for video_id=%s: %s", youtube_video_id, youtube_error_msg)

            # Spotify provider — remove track from library via Spotify Web API
            spotify_track_id = self._get_spotify_track_id(now_playing_np) if not is_local else None
            spotify_removed = False
            spotify_error_msg: str | None = None
            if spotify_track_id:
                try:
                    await self._spotify_save_track(spotify_track_id, save=False)
                    spotify_removed = True
                    log.info("Removed Spotify track from library: %s", spotify_track_id)
                    self._record_unlike_locally(now_playing_np, spotify_track_id, user_id)
                except RuntimeError as api_err:
                    spotify_error_msg = str(api_err)
                    log.warning("Spotify unlike API failed for track_id=%s: %s", spotify_track_id, api_err)

            # Skip to next track as a form of negative feedback
            if hasattr(self.controller.music, "skip"):
                await _call_maybe_async(self.controller.music, "skip")
                data: dict[str, object] = {"rating": "thumbs_down", "action": "skipped"}
                if is_local:
                    data["provider"] = "local"
                    data["track_id"] = local_id
                    msg = "Skipped and removed from liked songs."
                elif youtube_video_id:
                    data["provider"] = provider_np
                    data["video_id"] = youtube_video_id
                    if youtube_rated:
                        data["persisted"] = "local"
                        msg = "Noted and skipped."
                    else:
                        msg = "Skipped. %s" % (youtube_error_msg or "YouTube dislike could not be sent.")
                elif spotify_track_id:
                    data["provider"] = "spotify"
                    data["track_id"] = spotify_track_id
                    if spotify_removed:
                        msg = "Removed from Spotify and skipped."
                    else:
                        msg = "Skipped. %s" % (spotify_error_msg or "Spotify dislike could not be sent.")
                else:
                    provider_label = str(provider_np).capitalize() if provider_np else "this provider"
                    data["provider"] = provider_np
                    msg = (
                        "Skipped. Dislikes aren't supported on %s yet, but I've noted your preference." % provider_label
                    )
                return {
                    "ok": True,
                    "message": msg,
                    "data": data,
                }
            else:
                data = {"rating": "thumbs_down"}
                if is_local:
                    data["provider"] = "local"
                    data["track_id"] = local_id
                    msg = "Removed from liked songs."
                elif youtube_video_id:
                    data["provider"] = provider_np
                    data["video_id"] = youtube_video_id
                    if youtube_rated:
                        data["persisted"] = "local"
                        msg = "Noted in Viola."
                    else:
                        msg = youtube_error_msg or "YouTube dislike could not be sent."
                elif spotify_track_id:
                    data["provider"] = "spotify"
                    data["track_id"] = spotify_track_id
                    if spotify_removed:
                        msg = "Removed from Spotify."
                    else:
                        msg = spotify_error_msg or "Spotify dislike could not be sent."
                else:
                    provider_label = str(provider_np).capitalize() if provider_np else "this provider"
                    data["provider"] = provider_np
                    msg = "Noted. Dislikes aren't supported on %s yet, but I've noted your preference." % provider_label
                return {
                    "ok": True,
                    "message": msg,
                    "data": data,
                }
        except Exception as e:
            log.exception("Command 'thumbs_down' failed: %s", e)
            return {
                "ok": False,
                "message": "Couldn't save your rating right now. Try again?",
                "data": {},
                "error": "rating_failed",
            }

    async def repeat_off(self, params: dict[str, object]) -> dict[str, object]:
        """Turn repeat off."""
        try:
            from models.state_manager import RepeatMode
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            controller.set_repeat_mode(RepeatMode.OFF)

            return {
                "ok": True,
                "message": "Repeat is now off",
                "data": {"repeat_mode": "off"},
            }
        except Exception as e:
            log.exception("Failed to set repeat off: %s", e)
            return {
                "ok": False,
                "message": "Couldn't change the repeat mode right now. Try again?",
                "data": {},
                "error": "repeat_mode_failed",
            }

    async def repeat_on(self, params: dict[str, object]) -> dict[str, object]:
        """Turn repeat all on (loop queue/playlist)."""
        try:
            from models.state_manager import RepeatMode
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            controller.set_repeat_mode(RepeatMode.ALL)

            return {
                "ok": True,
                "message": "Repeat all is now on",
                "data": {"repeat_mode": "all"},
            }
        except Exception as e:
            log.exception("Failed to set repeat on: %s", e)
            return {
                "ok": False,
                "message": "Couldn't change the repeat mode right now. Try again?",
                "data": {},
                "error": "repeat_mode_failed",
            }

    async def repeat_one(self, params: dict[str, object]) -> dict[str, object]:
        """Turn repeat one on (loop current song)."""
        try:
            from models.state_manager import RepeatMode
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            controller.set_repeat_mode(RepeatMode.ONE)

            return {
                "ok": True,
                "message": "Repeat one is now on - this song will loop",
                "data": {"repeat_mode": "one"},
            }
        except Exception as e:
            log.exception("Failed to set repeat one: %s", e)
            return {
                "ok": False,
                "message": "Couldn't change the repeat mode right now. Try again?",
                "data": {},
                "error": "repeat_mode_failed",
            }

    async def _set_shuffle(self, enabled: bool, params: dict[str, object]) -> dict[str, object]:
        """Turn shuffle on or off for real -- play order included.

        #4214: both of these used to write `preferences.shuffle` on the
        ConsolidatedState and answer "Shuffle is now on". Nothing read that
        preference for ordering, so the queue kept its order; and the state
        broadcast serves the *StateHub* preference (`select_shuffle_enabled`),
        which was never written here at all, so even the displayed toggle did
        not move. Both halves now go through the one authority.
        """
        from music.shuffle_control import apply_shuffle

        label = "on" if enabled else "off"
        try:
            user_id = params.get("_user_id") if isinstance(params, dict) else None
            result = await asyncio.to_thread(
                apply_shuffle,
                self.controller.music,
                enabled,
                user_id=str(user_id) if user_id else None,
            )
        except Exception as e:
            log.exception("Failed to set shuffle %s: %s", label, e)
            return {
                "ok": False,
                "message": "Couldn't change shuffle mode right now. Try again?",
                "data": {},
                "error": "shuffle_mode_failed",
            }

        log.info("Shuffle %s (reordered %d upcoming tracks)", label, result.reordered)
        return {
            "ok": True,
            "message": "Shuffle is now %s" % label,
            "data": {"shuffle": result.enabled, "reordered": result.reordered},
        }

    async def shuffle_on(self, params: dict[str, object]) -> dict[str, object]:
        """Turn shuffle on."""
        return await self._set_shuffle(True, params)

    async def shuffle_off(self, params: dict[str, object]) -> dict[str, object]:
        """Turn shuffle off."""
        return await self._set_shuffle(False, params)

    async def get_volume(self, params: dict[str, object]) -> dict[str, object]:
        """Report the current volume level."""
        vol = self._get_current_volume()
        return {
            "ok": True,
            "message": "Volume is at %d%%." % vol,
            "data": {"volume": vol},
            "intent": "get_volume",
        }

    async def switch_provider(self, params: dict[str, object]) -> dict[str, object]:
        """Switch the active music provider."""
        try:
            original_text = str(params.get("_original_text", ""))
            # Extract provider name from the regex match
            import re

            match = re.search(
                r"(?:switch\s+to|use|change\s+to|set\s+(?:provider\s+to\s+)?)"
                r"\s*(?P<provider>spotify|youtube|youtube\s+music|local(?:\s+files)?|my\s+library)",
                original_text,
                re.I,
            )
            if not match:
                return {
                    "ok": False,
                    "message": "Couldn't understand which provider you want.",
                    "data": {},
                    "error": "no_provider_match",
                }

            raw_provider = match.group("provider").lower().strip()
            provider_id = self._PROVIDER_NAME_MAP.get(raw_provider)
            if not provider_id:
                return {
                    "ok": False,
                    "message": "I don't recognize that music provider.",
                    "data": {},
                    "error": "unknown_provider",
                }

            return self._set_active_provider(provider_id)
        except Exception:
            log.exception("switch_provider failed")
            return {
                "ok": False,
                "message": "Couldn't switch provider",
                "data": {},
                "error": "switch_provider_failed",
            }

    async def play_on_provider(self, params: dict[str, object]) -> dict[str, object]:
        """Switch provider and play a query — handles 'play X on Spotify'."""
        try:
            original_text = str(params.get("_original_text", ""))
            import re

            match = re.search(
                r"^(?:now\s+)?(?:play|put on)\s+(?P<query>.+?)\s+(?:on|from)\s+"
                r"(?P<provider>spotify|youtube|youtube music|local(?:\s+files|\s+library)?|"
                r"my (?:local )?library)\.?$",
                original_text,
                re.I,
            )
            if not match:
                return {
                    "ok": False,
                    "message": "Couldn't understand what to play or which provider.",
                    "data": {},
                    "error": "no_match",
                }

            query = match.group("query").strip()
            raw_provider = match.group("provider").lower().strip()
            provider_id = self._PROVIDER_NAME_MAP.get(raw_provider)
            if not provider_id:
                return {
                    "ok": False,
                    "message": "I don't recognize that music provider.",
                    "data": {},
                    "error": "unknown_provider",
                }

            # Switch provider first
            switch_result = self._set_active_provider(provider_id)
            if not switch_result.get("ok"):
                return switch_result

            # Now play the query through the music player
            display_name = self._PROVIDER_DISPLAY_NAMES.get(provider_id, provider_id)
            music = self.controller.music
            if music is None:
                return {
                    "ok": False,
                    "message": "Music player not available",
                    "data": {},
                    "error": "no_music_player",
                }

            # Determine source for local provider
            source = "local" if provider_id == "local" else None

            try:
                if hasattr(music, "play_async"):
                    import inspect

                    play_call = music.play_async(query, source, interrupt=True)
                    if inspect.isawaitable(play_call):
                        await play_call
                    # play_call already awaited or was sync
                else:
                    music.play(query, source, interrupt=True)
            except Exception as play_exc:
                log.error("play_on_provider: play failed: %s", play_exc)
                return {
                    "ok": False,
                    "message": "Switched to %s but couldn't play: %s" % (display_name, play_exc),
                    "data": {"provider": provider_id},
                    "error": "play_failed",
                }

            return {
                "ok": True,
                "message": "Playing %s on %s" % (query, display_name),
                "data": {"provider": provider_id, "query": query},
            }
        except Exception:
            log.exception("play_on_provider failed")
            return {
                "ok": False,
                "message": "Couldn't play on that provider",
                "data": {},
                "error": "play_on_provider_failed",
            }

    async def rescan_library(self, params: dict[str, object]) -> dict[str, object]:
        """Trigger a rescan of the local music library."""
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            configured_folder = sm.get("local_music_folder")
            folder = str(configured_folder).strip() if configured_folder else None
            if not folder:
                return {
                    "ok": False,
                    "message": "No local music folder configured. Tell me where your music files are and I'll scan them.",
                    "data": {},
                    "error": "not_configured",
                }

            def _run_scan():
                from music.providers.local.db import get_local_library_repo
                from music.providers.local.scanner import scan_and_index

                repo = get_local_library_repo()
                repo.initialize()
                return scan_and_index(folder, repo)

            stats = await asyncio.to_thread(_run_scan)
            scanned = stats.get("scanned", 0)
            upserted = stats.get("upserted", 0)
            removed = stats.get("removed", 0)

            message = "Library rescan complete: %d files scanned, %d updated, %d removed." % (
                scanned,
                upserted,
                removed,
            )
            return {"ok": True, "message": message, "data": stats}

        except Exception:
            log.exception("Command 'rescan_library' failed")
            return {
                "ok": False,
                "message": "Couldn't rescan your music library. Check that your library folder is accessible.",
                "data": {},
                "error": "rescan_library_failed",
            }

    async def play_favorites(self, params: dict[str, object]) -> dict[str, object]:
        """Play the user's liked/favorited songs through the canonical music executor."""
        try:
            from intent.command_executor import CommandExecutor

            executor = CommandExecutor(self.controller.music, None)
            result = await executor.execute_command("play_favorites", dict(params))
            ok = bool(result.get("success"))
            return {
                "ok": ok,
                "message": str(result.get("message") or ""),
                "data": result.get("data", {}) or {},
                "error": result.get("error"),
            }
        except Exception:
            log.exception("play_favorites failed")
            return {
                "ok": False,
                "message": "Couldn't play favorites right now.",
                "data": {},
                "error": "play_failed",
            }

    async def what_was_playing(self, params: dict[str, object]) -> dict[str, object]:
        """Show the previously played track from playback history."""
        try:
            music = self.controller.music
            player = None
            for attr in ("_player", "player", "_music_player"):
                candidate = getattr(music, attr, None)
                if candidate is not None:
                    player = candidate
                    break
            if player is None:
                player = music

            history = getattr(player, "_history", None)
            if history and len(history) > 0:
                prev = history[-1]
                if isinstance(prev, dict):
                    title = prev.get("title", "Unknown")
                    artist = prev.get("artist", "")
                else:
                    title = getattr(prev, "title", "Unknown")
                    artist = getattr(prev, "artist", "")
                message = "Previously playing: %s" % title
                if artist:
                    message += " by %s" % artist
                return {
                    "ok": True,
                    "message": message,
                    "data": {"title": title, "artist": artist},
                }
            return {
                "ok": True,
                "message": "No playback history available yet.",
                "data": {},
            }
        except Exception as exc:
            log.exception("Command 'what_was_playing' failed: %s", exc)
            return {
                "ok": False,
                "message": "Couldn't check playback history right now. Try again?",
                "data": {},
                "error": "what_was_playing_failed",
            }

    async def whats_next(self, params: dict[str, object]) -> dict[str, object]:
        """Show the next track(s) coming up in the queue."""
        try:
            state_getter = getattr(self.controller.music, "state", None)
            if not callable(state_getter):
                return {
                    "ok": False,
                    "message": "Music player not available",
                    "data": {},
                    "error": "no_music_player",
                }

            player_state = state_getter()
            if isinstance(player_state, dict):
                queue = player_state.get("queue", [])
            else:
                queue = getattr(player_state, "queue", []) if player_state else []

            if not queue:
                return {
                    "ok": True,
                    "message": "Nothing queued up next.",
                    "data": {"next_tracks": [], "total_queued": 0},
                }

            upcoming = []
            for index, item in enumerate(queue[:3]):
                if isinstance(item, dict):
                    title = item.get("title", "Track %d" % (index + 1))
                    artist = item.get("artist", "")
                else:
                    title = getattr(item, "title", "Track %d" % (index + 1))
                    artist = getattr(item, "artist", "")
                info = str(title)
                if artist:
                    info += " by %s" % artist
                upcoming.append(info)

            if len(queue) == 1:
                message = "Up next: %s" % upcoming[0]
            else:
                message = "Coming up:\n" + "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(upcoming))
                if len(queue) > 3:
                    message += "\n... and %d more" % (len(queue) - 3)

            return {
                "ok": True,
                "message": message,
                "data": {"next_tracks": upcoming, "total_queued": len(queue)},
            }
        except Exception as exc:
            log.exception("Command 'whats_next' failed: %s", exc)
            return {
                "ok": False,
                "message": "Couldn't check the queue right now. Try again?",
                "data": {},
                "error": "whats_next_failed",
            }

    def _set_active_provider(self, provider_id: str) -> dict[str, object]:
        """Set the active music provider in settings manager.

        Returns a result dict (ok, message, data).
        """
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            current = sm.get("active_music_provider_id", None)

            if current == provider_id:
                display_name = self._PROVIDER_DISPLAY_NAMES.get(provider_id, provider_id)
                return {
                    "ok": True,
                    "message": "Already using %s." % display_name,
                    "data": {"provider": provider_id, "changed": False},
                }

            # Stop playback and clear queue before switching
            from music.providers.active_provider import handle_provider_switch

            music_svc = getattr(self.controller, "music", None)
            handle_provider_switch(current, provider_id, music_service=music_svc)

            sm.set("active_music_provider_id", provider_id)
            display_name = self._PROVIDER_DISPLAY_NAMES.get(provider_id, provider_id)
            log.info("Switched active music provider to %s", provider_id)

            return {
                "ok": True,
                "message": "Switched to %s." % display_name,
                "data": {"provider": provider_id, "changed": True},
            }
        except Exception:
            log.exception("_set_active_provider failed")
            return {
                "ok": False,
                "message": "Couldn't switch provider",
                "data": {},
                "error": "set_provider_failed",
            }
