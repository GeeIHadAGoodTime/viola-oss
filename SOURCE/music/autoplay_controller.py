"""
Intelligent autoplay queue management.
Uses YouTube Music Radio for related track suggestions.

This module contains the orchestration logic for autoplay, managing:
- When to trigger autoplay (playlist mode or YouTube Music Radio)
- Thread/async safety
- Integration with the music player
- Playlist session management for simulated playlist playback

Note: The previous AI/GPT-based autoplay was replaced with YouTube Music Radio
to eliminate LLM token costs while still providing relevant recommendations.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.logging_config import get_logger
from models.player import QueueItem

if TYPE_CHECKING:
    from music.playback_session import PlaybackSessionController

logger = get_logger(__name__)


_YOUTUBE_PROVIDER_IDS = {"youtube_music", "youtube", "youtube_iframe"}


def _track_provider_id(track: dict[str, object]) -> str | None:
    """Normalize the track's provider to a string id (handles ProviderName enums)."""
    raw = track.get("provider")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = getattr(raw, "value", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return None


def _build_track_metadata(
    track: dict[str, object],
    video_id_value: str | None = None,
    query_url: str | None = None,
) -> dict[str, object]:
    """Build metadata dict from a playlist track for the player.

    Ensures the player receives the real title/artist instead of defaulting
    to the YouTube URL, and carries the track's provider/playback typing so a
    re-queued Spotify track is never mis-typed as an embedded-webview item
    (2026-07-02: provider dropped here -> provider=unknown/embedded_webview
    -> EMBEDDED_DEFER self-PID capture hijack killed multiroom audio).
    """
    metadata: dict[str, object] = {}
    title = track.get("title")
    if isinstance(title, str) and title:
        metadata["title"] = title
    artist = track.get("artist") or track.get("uploader")
    if isinstance(artist, str) and artist:
        metadata["artist"] = artist
    provider_id = _track_provider_id(track)
    vid = video_id_value or track.get("video_id")
    if isinstance(vid, str) and vid and (provider_id is None or provider_id in _YOUTUBE_PROVIDER_IDS):
        # video_id implies YouTube ONLY when the provider agrees (or is
        # unknown) — Spotify playlist entries reuse the track id in this
        # field, and treating it as a YouTube id plays the wrong track.
        metadata["video_id"] = vid
        metadata["provider"] = "youtube_music"
        metadata["thumbnail_url"] = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    if provider_id and "provider" not in metadata:
        metadata["provider"] = provider_id
    playback_mode = track.get("playback_mode")
    if isinstance(playback_mode, str) and playback_mode:
        metadata["playback_mode"] = playback_mode
    url = track.get("url")
    if isinstance(url, str) and url:
        metadata["url"] = url
    # Ensure URL is always present (required by _metadata_to_resolution)
    if "url" not in metadata and query_url:
        metadata["url"] = query_url
    return metadata


@dataclass
class AutoplayConfig:
    """
    Canonical configuration container for autoplay behavior.

    The legacy `music.autoplay_config` module has been removed; this dataclass
    is the single source of truth for defaults and runtime overrides.

    Best practices from launch-supported music apps:
    - Keep 10-15 songs buffered ahead via autoplay
    - Refill when queue drops below 7 songs
    - Trigger refill on track end, skip, and periodic checks
    - No cap on user-added songs (users can curate freely)
    """

    min_queue_size: int = 7  # Internal heuristic — balances responsiveness vs API calls
    max_queue_size: int = 15  # Target 15 songs via autoplay (prevents API spam)
    ai_enabled: bool = True
    regular_enabled: bool = True


class AutoplayController:
    """
    Manages automatic queue population.

    Supports two modes:
    - PLAYLIST: Draws from cached playlist tracks (simulated native playlist)
    - FREEFORM: AI-driven recommendations based on listening history

    Decoupled from MusicPlayer for better testing and clarity.
    """

    def __init__(self, music_player, settings_manager):
        """
        Initialize autoplay controller.

        Args:
            music_player: MusicPlayer instance
            settings_manager: Settings manager for configuration
        """

        self.player = music_player
        self.settings = settings_manager
        self.queue_config = getattr(music_player, "_queue_config", None)
        self.is_generating = False
        self._generation_lock = threading.Lock()
        self._anchor_lock = threading.Lock()
        self._anchor: QueueItem | None = None
        self._autoplay_task: asyncio.Task | None = None  # Store task ref to prevent GC
        self._event_hub: object | None = None  # EventHub for WebSocket broadcasts
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._session_controller: PlaybackSessionController | None = None
        self.config = self._load_config()

    # ------------------------------------------------------------------ #
    # Session controller integration
    # ------------------------------------------------------------------ #

    def get_session_controller(self) -> PlaybackSessionController | None:
        """Get or create the playback session controller."""
        if self._session_controller is None:
            try:
                from music.playback_session import get_playback_session_controller

                self._session_controller = get_playback_session_controller()
            except Exception as exc:
                logger.debug("Failed to get session controller: %s", exc)
        return self._session_controller

    def set_session_controller(self, controller: PlaybackSessionController) -> None:
        """Set the playback session controller (for dependency injection)."""
        self._session_controller = controller

    def is_playlist_mode(self) -> bool:
        """Check if currently in playlist mode."""
        controller = self.get_session_controller()
        if controller is not None:
            return bool(controller.is_playlist_mode())
        return False

    # ------------------------------------------------------------------ #
    # Configuration helpers
    # ------------------------------------------------------------------ #

    def _load_config(self) -> AutoplayConfig:
        """Load autoplay configuration from settings.

        The user's settings are the source of truth for whether autoplay runs
        and for the queue length that triggers a refill. ``queue_config`` still
        supplies the upper bound on how many tracks one refill may add, which
        is an internal API-spend guard rather than a user preference.
        """
        try:
            settings_mgr = None
            if self.settings:
                if hasattr(self.settings, "get"):
                    settings_mgr = self.settings
                else:
                    from ui.settings_manager import get_settings_manager

                    settings_mgr = get_settings_manager()

            default_cfg = AutoplayConfig()
            if settings_mgr is None:
                return default_cfg

            return AutoplayConfig(
                min_queue_size=settings_mgr.get("autoplay_min_queue", default_cfg.min_queue_size),
                max_queue_size=settings_mgr.get("autoplay_max_queue", default_cfg.max_queue_size),
                ai_enabled=settings_mgr.get("ai_autoplay_enabled", default_cfg.ai_enabled),
                regular_enabled=settings_mgr.get("autoplay_enabled", default_cfg.regular_enabled),
            )
        except Exception as exc:
            logger.warning("Failed to load autoplay config: %s", exc)
            return AutoplayConfig()

    def _refresh_config(self) -> AutoplayConfig:
        """Refresh cached configuration from settings."""
        self.config = self._load_config()
        return self.config

    def _get_autoplay_enabled(self) -> bool:
        """Return whether autoplay is enabled."""
        config = self._refresh_config()
        return bool(config.ai_enabled and config.regular_enabled)

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #

    def _claim_generation(self) -> bool:
        """Best-effort claim to run autoplay generation exactly once."""
        with self._generation_lock:
            if self.is_generating:
                return False
            self.is_generating = True
            return True

    def _release_generation(self) -> None:
        """Release generation flag."""
        with self._generation_lock:
            self.is_generating = False

    def _current_queue_size(self) -> int:
        with self.player._lock:
            return self.player._playlist.queue_size()

    def _resolve_current_track(self) -> QueueItem | None:
        with self.player._lock:
            current_track = self.player._state.now_playing
            if not current_track and hasattr(self.player, "_last_played"):
                current_track = self.player._last_played
        if current_track:
            return current_track
        with self._anchor_lock:
            return self._anchor

    # ------------------------------------------------------------------ #
    # WebSocket broadcast bridge (sync → async)
    # ------------------------------------------------------------------ #

    def set_broadcast_context(
        self,
        event_hub: object,
        main_loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Store references for broadcasting state to WebSocket clients.

        Called once during app startup.  The *event_hub* is the
        ``EventHub`` instance and *main_loop* the FastAPI event loop.
        """
        self._event_hub = event_hub
        self._main_loop = main_loop

    def _broadcast_state_update(self) -> None:
        """Push updated player state to WebSocket clients from any thread.

        Uses ``call_soon_threadsafe`` following the same pattern as
        ``audio_core.streaming.hub_broadcaster._schedule_broadcast``.
        """
        hub = self._event_hub
        loop = self._main_loop
        logger.warning("[QUEUE_TRACE] _broadcast_state_update ENTER hub=%s loop=%s", hub, loop)
        if hub is None or loop is None:
            logger.warning("[QUEUE_TRACE] _broadcast_state_update ABORT hub=%s loop=%s", hub, loop)
            return

        try:
            from ui.core.player_state import to_player_state

            ps = to_player_state(self.player, None).model_dump()
            _q = ps.get("queue", []) if isinstance(ps, dict) else []
            _qlen = len(_q) if _q else 0

            async def _do_broadcast() -> None:
                try:
                    from core.user_context import get_current_user_id

                    user_id = str(get_current_user_id() or "").strip()
                except LookupError:
                    logger.error("Autoplay state broadcast skipped: missing user_id")
                    return
                except RuntimeError as exc:
                    logger.error("Autoplay state broadcast skipped: user_id resolution failed: %s", exc)
                    return
                if not user_id:
                    logger.error("Autoplay state broadcast skipped: blank user_id")
                    return
                logger.warning(
                    "[QUEUE_TRACE] _broadcast_state_update EXECUTING queue_len=%d",
                    _qlen,
                )
                await hub.broadcast("state", ps, user_id=user_id, force=True)

            def _schedule() -> None:
                asyncio.create_task(_do_broadcast())

            loop.call_soon_threadsafe(_schedule)
            logger.warning("[QUEUE_TRACE] _broadcast_state_update SCHEDULED queue_len=%d", _qlen)
        except Exception as exc:
            logger.warning("[QUEUE_TRACE] _broadcast_state_update FAILED reason=%s", exc)

    # ------------------------------------------------------------------ #
    # Trigger logic
    # ------------------------------------------------------------------ #

    def should_trigger(self, *, respect_generation_flag: bool = True) -> bool:
        """
        Check if autoplay should run based on current state.

        Args:
            respect_generation_flag: When False, skip the guard that prevents
                                     concurrent generation. Used when the caller
                                     already holds the generation lock.
        """
        logger.debug(
            "🟡 SHOULD_TRIGGER: Called with respect_generation_flag=%s",
            respect_generation_flag,
        )

        if respect_generation_flag and self.is_generating:
            logger.debug("🟡 SHOULD_TRIGGER: Already generating, returning False")
            return False

        user_paused = getattr(self.player, "_user_paused", False)
        logger.debug("🟡 SHOULD_TRIGGER: user_paused=%s", user_paused)
        if user_paused:
            logger.debug("🟡 SHOULD_TRIGGER: User paused, returning False")
            return False

        try:
            queue_size = self._current_queue_size()
            logger.debug("🟡 SHOULD_TRIGGER: queue_size=%s", queue_size)
        except Exception as exc:
            logger.warning("🟡 SHOULD_TRIGGER: Failed to inspect queue size: %s", exc)
            return False

        config = self._refresh_config()
        logger.debug(
            "🟡 SHOULD_TRIGGER: config.ai_enabled=%s, config.regular_enabled=%s",
            config.ai_enabled,
            config.regular_enabled,
        )

        if not config.ai_enabled or not config.regular_enabled:
            logger.debug("🟡 SHOULD_TRIGGER: AI autoplay disabled in settings, returning False")
            return False

        # ``min_queue_size`` comes from the user's ``autoplay_min_queue``
        # setting (_load_config) and stays authoritative here. It used to be
        # overwritten by ``queue_config.limits.min_size_for_autoplay``, and
        # because QueueConfig.from_settings() has no production caller, that
        # override was always the hardcoded default — so "refill when I drop
        # below 3" was saved and confirmed while autoplay kept triggering at
        # the built-in threshold. The built-in default and the setting's
        # default are the same value, so honouring the setting changes
        # nothing for a user who never touched it.
        min_queue_size = config.min_queue_size
        max_queue_size = config.max_queue_size
        limits = getattr(self.queue_config, "limits", None) if self.queue_config is not None else None
        if limits is not None:
            configured_max = getattr(limits, "max_autoplay_size", None)
            if isinstance(configured_max, int):
                max_queue_size = configured_max

        logger.debug(
            "🟡 SHOULD_TRIGGER: min_queue_size=%s, max_queue_size=%s",
            min_queue_size,
            max_queue_size,
        )

        if queue_size >= max_queue_size:
            logger.debug(
                "🟡 SHOULD_TRIGGER: Queue at max (%s >= %s), returning False",
                queue_size,
                max_queue_size,
            )
            return False

        if queue_size < min_queue_size:
            logger.debug(
                "🟡 SHOULD_TRIGGER: Queue low (%s < %s), returning True!",
                queue_size,
                min_queue_size,
            )
            return True

        logger.debug(
            "🟡 SHOULD_TRIGGER: Queue sufficient (%s >= %s), returning False",
            queue_size,
            min_queue_size,
        )
        return False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def ensure_buffer(self, current_track=None, *, takeover_generation: bool = False) -> int:
        """
        Ensure queue has minimum number of songs.

        In PLAYLIST mode, draws from cached playlist tracks.
        In FREEFORM mode, uses AI-driven recommendations.

        Args:
            current_track: Current track for context (optional)
            takeover_generation: Caller already claimed the generation lock.

        Returns:
            Number of songs added to queue
        """
        logger.warning(
            "[QUEUE_TRACE] ensure_buffer ENTER current_track=%s takeover=%s",
            current_track,
            takeover_generation,
        )
        logger.debug(
            "🟢 ENSURE_BUFFER: Called with current_track=%s, takeover_generation=%s",
            current_track,
            takeover_generation,
        )

        release_needed = False
        if not takeover_generation:
            if not self._claim_generation():
                logger.debug("🟢 ENSURE_BUFFER: Could not claim generation, returning 0")
                return 0
            release_needed = True

        try:
            if not self.should_trigger(respect_generation_flag=False):
                logger.warning("[QUEUE_TRACE] ensure_buffer EXIT_EARLY reason=should_trigger_false")
                logger.debug("🟢 ENSURE_BUFFER: should_trigger=False, returning 0")
                return 0

            config = self.config  # already refreshed during should_trigger
            queue_size = self._current_queue_size()
            needed = max(1, config.min_queue_size - queue_size)
            logger.warning(
                "[QUEUE_TRACE] ensure_buffer QUEUE_CHECK current=%d threshold=%d needed=%d",
                queue_size,
                config.min_queue_size,
                needed,
            )

            # Check if we're in PLAYLIST mode first
            session_controller = self.get_session_controller()
            is_playlist = session_controller is not None and session_controller.is_playlist_mode()
            logger.warning(
                "[QUEUE_TRACE] ensure_buffer PLAYLIST_CHECK session_ctrl=%s is_playlist=%s",
                session_controller is not None,
                is_playlist,
            )
            if is_playlist:
                added = await self._fill_from_playlist(session_controller, needed)
                if added > 0:
                    logger.info("Autoplay: Added %s songs from playlist", added)
                    self._broadcast_state_update()
                    return added
                # Playlist exhausted or failed, fall through to AI mode
                logger.info("🔄 Autoplay: Playlist exhausted, transitioning to AI mode")

            # LOCAL mode: Shuffle tracks from the local music library.
            # Check both the global active-provider setting AND the currently
            # playing track's provider.  When a user plays a local file via
            # ``POST /v1/play {"source":"local"}``, the resolver tags the
            # QueueItem with ``provider="local"`` but does NOT change the
            # ``active_music_provider_id`` setting.  Without checking the
            # track-level provider the autoplay falls through to YouTube
            # Radio even though the user is listening to local files.
            from music.providers.active_provider import get_active_music_provider_id

            active_provider = get_active_music_provider_id()

            # Resolve the current track early so we can inspect its provider.
            if not current_track:
                current_track = self._resolve_current_track()

            current_track_provider = getattr(current_track, "provider", None)
            is_local = active_provider == "local" or current_track_provider == "local"

            if is_local:
                added = await self._fill_from_local_library(needed)
                if added > 0:
                    logger.info("Autoplay: Added %d songs from local library", added)
                    self._broadcast_state_update()
                return added

            # FREEFORM mode: Use AI-driven recommendations
            if not current_track:
                current_track = self._resolve_current_track()
                logger.warning(
                    "[QUEUE_TRACE] ensure_buffer RESOLVE_TRACK result=%s",
                    current_track.title if current_track else None,
                )
                if current_track:
                    logger.info("Autoplay: Using anchor track: %s", current_track.title)

            if not current_track:
                # Try to get context from playlist session if available
                if session_controller is not None:
                    seed_context = session_controller.get_ai_seed_context()
                    if seed_context:
                        logger.info(
                            "Autoplay: Using %s playlist songs as AI seed",
                            len(seed_context),
                        )
                        # Use first song from seed as pseudo-current track
                        first_seed = seed_context[0]
                        raw_title = first_seed.get("title")
                        title = raw_title if isinstance(raw_title, str) else "Unknown"
                        raw_url = first_seed.get("url")
                        url = raw_url if isinstance(raw_url, str) else None
                        raw_video_id = first_seed.get("video_id")
                        video_id = raw_video_id if isinstance(raw_video_id, str) else None
                        current_track = QueueItem(
                            id="seed-context",
                            title=title,
                            url=url,
                            video_id=video_id,
                        )

            if not current_track:
                logger.warning("[QUEUE_TRACE] ensure_buffer EXIT_EARLY reason=no_current_track")
                logger.warning("🟢 ENSURE_BUFFER: No current track - waiting for playback to start")
                return 0

            # FREEFORM mode: Use YouTube Music Radio for related tracks
            # This replaced the AI/GPT-based autoplay to save API tokens
            logger.warning(
                "[QUEUE_TRACE] ensure_buffer STRATEGY=freeform track=%s video_id=%s url=%s",
                current_track.title,
                getattr(current_track, "video_id", None),
                current_track.url,
            )

            from music.autoplay import get_ytmusic_radio

            radio = get_ytmusic_radio()
            logger.warning(
                "[QUEUE_TRACE] ensure_buffer RADIO_INSTANCE is_fetching=%s client=%s",
                radio._is_fetching,
                radio._ytmusic is not None,
            )

            # Extract video_id from current track (required for ytmusicapi radio)
            seed_video_id = getattr(current_track, "video_id", None)
            if not seed_video_id and current_track.url:
                # Try to extract video_id from URL
                import re

                match = re.search(r"(?:v=|/embed/|youtu\.be/)([a-zA-Z0-9_-]{11})", current_track.url)
                if match:
                    seed_video_id = match.group(1)

            logger.warning("[QUEUE_TRACE] ensure_buffer SEED_VIDEO_ID=%s", seed_video_id)
            if not seed_video_id:
                logger.warning(
                    "[QUEUE_TRACE] ensure_buffer EXIT_EARLY reason=no_video_id track=%s",
                    current_track.title,
                )
                return 0

            logger.warning(
                "[QUEUE_TRACE] ensure_buffer FETCH_START strategy=radio seed=%s limit=%d",
                seed_video_id,
                needed,
            )

            # Get related tracks from YouTube Music Radio
            import time as _time

            _fetch_t0 = _time.monotonic()
            related = await radio.get_related_tracks(
                seed_video_id=seed_video_id,
                limit=needed,
            )
            _fetch_elapsed_ms = (_time.monotonic() - _fetch_t0) * 1000
            logger.warning(
                "[QUEUE_TRACE] ensure_buffer FETCH_RESULT count=%d elapsed_ms=%.1f",
                len(related) if related else 0,
                _fetch_elapsed_ms,
            )
            logger.debug(
                "🟢 ENSURE_BUFFER: radio.get_related_tracks returned %s tracks",
                len(related) if related else 0,
            )

            if not related:
                logger.warning("Autoplay: YouTube Music Radio returned no tracks")
                return 0

            # Add related tracks to queue
            added = 0
            for track_data in related:
                try:
                    video_id = track_data.get("video_id")
                    if not video_id:
                        continue

                    # Use standard YouTube watch URL (will be resolved properly)
                    query = f"https://www.youtube.com/watch?v={video_id}"

                    # Pass track metadata for proper display
                    metadata = {
                        "title": track_data.get("title", "Unknown"),
                        "artist": track_data.get("artist"),
                        "artwork_url": track_data.get("thumbnail"),
                        "video_id": video_id,
                        "source": "autoplay",
                    }

                    # Use the player's enqueue method with metadata
                    result = self.player.enqueue_autoplay(query, metadata=metadata)
                    if result is not None:
                        added += 1
                        logger.warning(
                            "[QUEUE_TRACE] ensure_buffer ENQUEUED track=%s total=%d",
                            track_data.get("title", "Unknown"),
                            added,
                        )
                        logger.debug(
                            "Autoplay: Queued '%s' (video_id=%s)",
                            track_data.get("title", "Unknown"),
                            video_id,
                        )
                except Exception as exc:
                    logger.warning("Autoplay: Failed to queue track: %s", exc)

            logger.info(
                "🔄 Autoplay: YouTube Music Radio added %d/%d tracks",
                added,
                len(related),
            )
            if added > 0:
                logger.warning(
                    "[QUEUE_TRACE] ensure_buffer BROADCASTING queue_len=%d",
                    self._current_queue_size(),
                )
                self._broadcast_state_update()
            else:
                logger.warning("[QUEUE_TRACE] ensure_buffer NO_TRACKS_ADDED, skipping broadcast")
            return added

        except Exception as exc:
            logger.warning(
                "[QUEUE_TRACE] ensure_buffer EXCEPTION type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
            logger.error("Autoplay: Failed to ensure buffer: %s", exc)
            import traceback

            logger.error(traceback.format_exc())
            return 0
        finally:
            if release_needed:
                self._release_generation()

    def ensure_buffer_blocking(self, current_track=None, *, timeout_seconds: float = 8.0) -> int:
        """Run the existing autoplay refill path and wait for its result.

        Command handlers need the post-command queue/now-playing state to be
        truthful in their typed tool result. This is a synchronous wrapper
        around ``ensure_buffer``; it does not implement a second queue filler.
        """

        async def _run_with_timeout() -> int:
            return await asyncio.wait_for(
                self.ensure_buffer(current_track=current_track),
                timeout=max(0.1, float(timeout_seconds)),
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return int(asyncio.run(_run_with_timeout()))
            except TimeoutError:
                logger.warning("Autoplay: blocking refill timed out after %.1fs", timeout_seconds)
                return 0
            except (RuntimeError, TypeError, ValueError) as exc:
                logger.warning("Autoplay: blocking refill failed: %s", exc)
                return 0

        result: dict[str, object] = {"added": 0}

        def _runner() -> None:
            try:
                result["added"] = asyncio.run(_run_with_timeout())
            except (TimeoutError, RuntimeError, TypeError, ValueError) as exc:
                result["error"] = exc

        worker = threading.Thread(target=_runner, daemon=True)
        worker.start()
        worker.join(timeout=max(0.1, float(timeout_seconds)) + 0.5)
        if worker.is_alive():
            logger.warning("Autoplay: blocking refill worker did not finish after %.1fs", timeout_seconds)
            return 0

        error = result.get("error")
        if error is not None:
            if isinstance(error, asyncio.TimeoutError):
                logger.warning("Autoplay: blocking refill timed out after %.1fs", timeout_seconds)
                return 0
            logger.warning("Autoplay: blocking refill failed: %s", error)
            return 0
        added = result.get("added", 0)
        return int(added) if isinstance(added, int) else 0

    async def _fill_from_playlist(
        self,
        session_controller: PlaybackSessionController,
        needed: int,
    ) -> int:
        """
        Fill queue from playlist cache.

        Args:
            session_controller: PlaybackSessionController instance
            needed: Number of tracks needed

        Returns:
            Number of tracks added
        """
        try:
            tracks = session_controller.fill_queue_from_playlist(needed)
            if not tracks:
                return 0

            added = 0
            for track in tracks:
                try:
                    # CRITICAL FIX: Prioritize video_id for exact playback
                    # Order matters: video_id (exact) > url (exact) > title (search fallback)
                    # video_id implies YouTube ONLY when the provider agrees (or is
                    # unknown) — Spotify playlist entries reuse the track id in this
                    # field and a YouTube embed URL would play the wrong track.
                    provider_id = _track_provider_id(track)
                    video_id_value = track.get("video_id")
                    if (
                        isinstance(video_id_value, str)
                        and video_id_value
                        and (provider_id is None or provider_id in _YOUTUBE_PROVIDER_IDS)
                    ):
                        # Use embed URL for YouTubeWebBackend compliance (PRD v5.3 section 7.3)
                        query = f"https://www.youtube.com/embed/{video_id_value}?autoplay=1"
                    else:
                        raw_url = track.get("url")
                        raw_title = track.get("title")
                        query = raw_url if isinstance(raw_url, str) and raw_url else None
                        if query is None and isinstance(raw_title, str) and raw_title:
                            query = raw_title

                    if not query:
                        logger.warning(
                            "Playlist: Skipping track with no playable identifier: %s",
                            track,
                        )
                        continue

                    # Build metadata so the player gets the real title/artist
                    metadata = _build_track_metadata(track, video_id_value, query_url=query)
                    # Use the player's enqueue method
                    result = self.player.enqueue_autoplay(query, metadata=metadata)
                    if result is not None:
                        added += 1
                        logger.debug(
                            "Playlist: Queued '%s' (video_id=%s)",
                            track.get("title", "Unknown"),
                            video_id_value if isinstance(video_id_value, str) else None,
                        )
                except Exception as exc:
                    logger.warning("Playlist: Failed to queue track: %s", exc)

            return added

        except Exception as exc:
            logger.error("Playlist: Failed to fill from playlist: %s", exc)
            return 0

    async def _fill_from_local_library(self, needed: int) -> int:
        """Fill queue by shuffling tracks from the local music library.

        Queries all tracks from the local SQLite library, shuffles them
        randomly, and enqueues up to ``needed`` tracks. Tracks that are
        already in the queue (by file_path) are skipped to avoid duplicates.

        Args:
            needed: Number of tracks to add.

        Returns:
            Number of tracks actually added.
        """
        import random

        try:
            from music.providers.local.db import get_local_library_repo

            repo = get_local_library_repo()
            repo.initialize()
            all_tracks = repo.get_all_tracks()

            if not all_tracks:
                logger.warning("Autoplay: Local library is empty, cannot fill queue")
                return 0

            logger.debug(
                "Autoplay: Local library has %d tracks, need %d",
                len(all_tracks),
                needed,
            )

            # Build a set of file_paths already in the queue to avoid duplicates
            existing_urls: set[str] = set()
            try:
                with self.player._lock:
                    for item in self.player._queue:
                        if item.url:
                            existing_urls.add(item.url)
                    now_playing = self.player._state.now_playing
                    if now_playing and now_playing.url:
                        existing_urls.add(now_playing.url)
            except Exception as exc:
                logger.debug("Autoplay: Could not inspect queue for dedup: %s", exc)

            # Shuffle and pick candidates
            candidates = list(all_tracks)
            random.shuffle(candidates)

            added = 0
            for track in candidates:
                if added >= needed:
                    break

                file_path = track.get("file_path")
                if not file_path:
                    continue

                # Skip if already queued
                if file_path in existing_urls:
                    continue

                title = track.get("title") or track.get("file_name", "Unknown")
                artist = track.get("artist") or "Unknown Artist"

                metadata = {
                    "title": title,
                    "artist": artist,
                    "album": track.get("album"),
                    "provider": "local",
                    "source": "autoplay",
                    "file_path": file_path,
                }

                result = self.player.enqueue_autoplay(file_path, metadata=metadata)
                if result is not None:
                    added += 1
                    existing_urls.add(file_path)
                    logger.debug(
                        "Autoplay: Queued local track '%s' by %s",
                        title,
                        artist,
                    )

            logger.info(
                "Autoplay: Local library added %d/%d tracks (library size=%d)",
                added,
                needed,
                len(all_tracks),
            )
            return added

        except Exception as exc:
            logger.error("Autoplay: Failed to fill from local library: %s", exc)
            return 0

    def check_and_run_async(self) -> None:
        """
        Check if autoplay should run and trigger it asynchronously.
        This is the non-blocking entry point called from the music player.
        """
        logger.warning("[QUEUE_TRACE] check_and_run_async ENTER")
        if not self._claim_generation():
            logger.warning("[QUEUE_TRACE] check_and_run_async SKIP reason=could not claim generation lock")
            return

        try:
            should = self.should_trigger(respect_generation_flag=False)
            logger.warning("[QUEUE_TRACE] check_and_run_async should_trigger=%s", should)
            if not should:
                logger.warning("[QUEUE_TRACE] check_and_run_async SKIP reason=should_trigger returned False")
                self._release_generation()
                return

            current_track = self._resolve_current_track()

            # Don't return early if no current track - ensure_buffer has fallback logic
            # to get seed context from playlist session controller
            if not current_track:
                logger.info("🔄 Autoplay: No current track, will try playlist session fallback")

            config = self.config

            try:
                loop = asyncio.get_running_loop()
                logger.debug("🔄 Autoplay: Using running event loop")
                # Store task reference to prevent garbage collection
                self._autoplay_task = loop.create_task(self._run_async_wrapper(config.min_queue_size, current_track))
                return
            except RuntimeError:  # No running event loop in this thread, use thread fallback
                pass

            logger.debug("🔄 Autoplay: Starting background thread")

            def run_in_thread():
                """Run autoplay in a separate thread with its own event loop."""
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(
                            self.ensure_buffer(
                                current_track=current_track,
                                takeover_generation=True,
                            )
                        )
                        logger.info("🔄 Autoplay: Background thread completed")
                    except Exception as exc_inner:
                        logger.error("Autoplay: Background thread failed: %s", exc_inner)
                        import traceback

                        logger.error(traceback.format_exc())
                    finally:
                        loop.close()
                        self._release_generation()
                except Exception as exc_outer:
                    logger.error("Autoplay: Thread exception: %s", exc_outer)
                    self._release_generation()

            thread = threading.Thread(target=run_in_thread, daemon=True)
            thread.start()
            logger.debug("🔄 Autoplay: Background thread started")

        except Exception as exc:
            logger.error("Autoplay: Exception in check_and_run_async: %s", exc)
            import traceback

            logger.error(traceback.format_exc())
            self._release_generation()

    async def _run_async_wrapper(self, min_queue, current_track) -> None:
        """Async wrapper for running in existing event loop."""
        try:
            logger.debug("🔄 Autoplay: Running in async wrapper")
            await self.ensure_buffer(current_track=current_track, takeover_generation=True)
        except Exception as exc:
            logger.error("Autoplay: Async wrapper failed: %s", exc)
            import traceback

            logger.error(traceback.format_exc())
        finally:
            self._release_generation()

    def cancel_pending(self) -> None:
        """Cancel any in-flight autoplay generation and reset state.

        Called on manual play to ensure user intent takes priority over
        autoplay suggestions (CB-13 fix).
        """
        # Cancel running async task if present
        task = self._autoplay_task
        if task is not None and not task.done():
            task.cancel()
            logger.info("Autoplay: Cancelled in-flight autoplay task (manual play override)")
        self._autoplay_task = None
        self._release_generation()

    def set_anchor(self, track: QueueItem | None) -> None:
        """Store anchor track used when manual play resets context."""
        with self._anchor_lock:
            self._anchor = track

    def reset_context(self) -> None:
        """
        Reset all autoplay context (Product Decision 2026-01-11).

        Called on app startup and after idle timeout to prevent stale
        context from affecting AI recommendations.
        """
        logger.info("Autoplay: Resetting context (preventing stale feedback loops)")

        # Clear anchor track
        with self._anchor_lock:
            self._anchor = None

        # Reset generation state
        self._release_generation()

        # Clear playlist session if exists
        session_controller = self.get_session_controller()
        if session_controller is not None:
            session_controller.exit_playlist_mode()

    def trigger_startup_fill(self) -> None:
        """
        Trigger autoplay on application startup.

        This is called once when the app is ready to populate the initial queue
        from the default playlist or AI recommendations.
        """
        logger.info("🔄 Autoplay: Startup fill triggered")

        # Only trigger if queue is empty and nothing is playing
        try:
            queue_size = self._current_queue_size()
            if queue_size > 0:
                logger.info(
                    "Autoplay: Queue already has %s items, skipping startup fill",
                    queue_size,
                )
                return

            current = self._resolve_current_track()
            if current is not None:
                logger.info("🔄 Autoplay: Track already playing, skipping startup fill")
                return
        except Exception as exc:
            logger.debug("Autoplay: Could not check state for startup fill: %s", exc)
            return

        # Trigger autoplay - will use playlist session fallback for context
        self.check_and_run_async()
