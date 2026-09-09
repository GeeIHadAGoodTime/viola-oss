"""
Playback engine manager coordinating provider-specific adapters.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, cast

from core.logging_config import StructuredLogger, get_logger
from models.player import QueueItem
from playback.feature_flags import PlaybackFeatureFlags

from .engines import (
    PlaybackHandle,
    ProviderPlaybackEngine,
    QueueContext,
    SpotifyCDPEngine,
    SpotifyWebPlaybackEngine,
    YouTubeEmbeddedEngine,
)
from .engines.base import PlaybackError

if TYPE_CHECKING:  # pragma: no cover - typing helpers
    from playback.engines.youtube import YouTubeEmbedController
    from playback.queue_engine import PlaybackQueueEngine


# Module-level per-user weak references to the MusicPlayer so async
# provider callbacks can emit state updates back to the correct session.
_player_refs_lock = threading.RLock()
_player_refs_by_user: dict[str, weakref.ref[Any]] = {}


def _player_emit_user_ids(user_id: str | None = None) -> tuple[str, ...]:
    """Return explicit/context/desktop-principal user IDs for player emit lookups.

    Both sides of the emit-ref store run OUTSIDE any HTTP request: the
    playback worker thread stores the ref (playback_executor calls
    _set_player_for_emit) and provider poll threads read it (e.g. the
    Spotify CDP poller's _broadcast_state). With only the request
    ContextVar consulted, both resolved to zero candidates there — the ref
    was silently never stored, lookups returned None, and engine state
    pushes (live position/duration) never reached the player, leaving the
    UI progress bar at zero. On the desktop surface the install's active
    principal IS the user (same pattern as the /ws/events fix, b35c6418),
    so include it. On cloud surfaces get_desktop_active_user_id raises and
    callers must still carry an explicit or request-scoped principal —
    the multi-tenant no-silent-global rule is preserved.
    """
    candidates: list[str] = []
    if user_id:
        candidates.append(user_id)

    try:
        from core.user_context import get_current_user_id

        current_user_id = get_current_user_id()
        if current_user_id not in candidates:
            candidates.append(current_user_id)
    except Exception:
        pass

    # LookupError = cloud surface (request principal required there);
    # ImportError/RuntimeError = early-boot import edge. Either way the
    # remaining candidates decide, matching the pre-existing lookup shape.
    with contextlib.suppress(ImportError, LookupError, RuntimeError):
        from core.user_context import get_desktop_active_user_id

        desktop_user_id = get_desktop_active_user_id()
        if desktop_user_id not in candidates:
            candidates.append(desktop_user_id)

    return tuple(candidates)


def _set_player_for_emit(player: Any, user_id: str | None = None) -> None:
    """Store per-user weak references to the player for async state emit."""
    ref = weakref.ref(player)
    with _player_refs_lock:
        for candidate_user_id in _player_emit_user_ids(user_id):
            _player_refs_by_user[candidate_user_id] = ref


def _get_player_for_emit(user_id: str | None = None) -> Any | None:
    """Retrieve the player for state emit, or None if unavailable."""
    with _player_refs_lock:
        for candidate_user_id in _player_emit_user_ids(user_id):
            ref = _player_refs_by_user.get(candidate_user_id)
            if ref is None:
                continue
            player = ref()
            if player is None:
                _player_refs_by_user.pop(candidate_user_id, None)
                continue
            return player
    return None


ArtworkCallback = Callable[[QueueItem, str | None], None]


@dataclass
class _UserPlaybackSession:
    """Per-user playback state for multi-user isolation."""

    active_handle: Any = None
    active_backend: Any = None  # EngineBackedBackend | None
    last_accessed: float = field(default_factory=lambda: __import__("time").monotonic())


# Bounded session cache constants
_MAX_USER_SESSIONS = 1000  # Hard cap on concurrent user sessions
_SESSION_IDLE_TIMEOUT = 3600.0  # 1 hour idle timeout


class EngineBackedBackend:
    """
    Adapter that makes a provider playback engine look like the legacy backend.
    """

    def __init__(
        self,
        engine: ProviderPlaybackEngine,
        manager: PlaybackEngineManager,
        feature_flags: PlaybackFeatureFlags,
        logger: logging.Logger,
    ) -> None:
        self._engine = engine
        self._manager = manager
        self._feature_flags = feature_flags
        self._logger = logger
        self._item: QueueItem | None = None
        self._upcoming: list[QueueItem] = []
        self._handle: PlaybackHandle | None = None
        self._artwork_callback: ArtworkCallback | None = None
        self._paused: bool = False  # Track pause state for monitor loop compatibility

    # ---------- legacy backend surface ----------
    def set_context(
        self,
        item: QueueItem,
        upcoming: Iterable[QueueItem],
        *,
        artwork_callback: ArtworkCallback | None,
    ) -> None:
        self._item = item
        self._upcoming = list(upcoming)
        self._artwork_callback = artwork_callback
        self._paused = False  # Track pause state for monitor loop compatibility
        if self._feature_flags.hot_buffer_enabled and hasattr(self._engine, "prefetch"):
            try:
                queue_context = QueueContext(
                    upcoming_items=self._upcoming,
                    feature_flags=self._feature_flags.to_dict(),
                )
                self._engine.prefetch(self._upcoming, queue_context=queue_context)
            except Exception as exc:  # pragma: no cover - provider optional
                self._logger.debug(
                    "Provider %s prefetch failed: %s",
                    self._engine.provider_id,
                    exc,
                )

    def play_url(self, url: str) -> None:
        if not self._item:
            raise PlaybackError("Engine backend missing active queue item")

        queue_context = QueueContext(
            upcoming_items=self._upcoming,
            feature_flags=self._feature_flags.to_dict(),
        )
        handle = self._engine.play(
            self._item,
            queue_context=queue_context,
            on_artwork=self._artwork_callback,
        )
        handle.set_on_finished(self._manager._on_handle_finished)
        self._handle = handle
        self._manager._set_active_handle(handle, backend=self)

    def play(self, url: str) -> None:
        self.play_url(url)

    def pause(self) -> None:
        if self._handle:
            self._paused = True  # Set BEFORE engine call so monitor loop sees it immediately
            self._engine.pause(self._handle)

    def resume(self) -> None:
        if self._handle:
            self._paused = False  # Clear BEFORE engine call
            self._engine.resume(self._handle)

    def stop(self) -> None:
        if self._handle:
            self._paused = False  # Clear pause on stop
            try:
                self._engine.stop(self._handle)
            finally:
                self._handle = None

    def set_volume(self, level: int) -> int:
        if self._handle:
            self._engine.set_volume(self._handle, level)
        return level

    def is_playing(self) -> bool:
        return bool(self._handle and self._handle.is_active())

    def get_position(self) -> int:
        if not self._handle:
            return 0
        return int(self._engine.current_position(self._handle))

    def get_duration(self) -> int:
        if not self._handle:
            return 0
        return int(self._engine.duration(self._handle))

    def current_position_ms(self) -> int | None:
        """Return playback position in milliseconds (legacy backend interface)."""
        if not self._handle:
            return None
        try:
            pos_s = self._engine.current_position(self._handle)
            return int(pos_s * 1000)
        except Exception:
            return None

    def current_duration_ms(self) -> int | None:
        """Return track duration in milliseconds (legacy backend interface)."""
        if not self._handle:
            return None
        try:
            dur_s = self._engine.duration(self._handle)
            return int(dur_s * 1000) if dur_s > 0 else None
        except Exception:
            return None

    def get_position_percentage(self) -> float:
        duration = self.get_duration()
        if duration <= 0:
            return 0.0
        return min(1.0, max(0.0, self.get_position() / max(duration, 1)))

    def seek(self, position_seconds: float | int) -> None:
        if self._handle:
            seek = getattr(self._engine, "seek", None)
            if callable(seek):
                seek(self._handle, float(position_seconds))
                return
        raise PlaybackError(f"Provider '{self._engine.provider_id}' does not yet support seeking")

    def next(self) -> None:
        if self._handle:
            next_track = getattr(self._engine, "next_track", None)
            if callable(next_track):
                next_track(self._handle)
                return
        raise PlaybackError(f"Provider '{self._engine.provider_id}' does not yet support next track")

    def skip(self) -> None:
        self.next()

    def previous(self) -> None:
        if self._handle:
            previous_track = getattr(self._engine, "previous_track", None)
            if callable(previous_track):
                previous_track(self._handle)
                return
        raise PlaybackError(f"Provider '{self._engine.provider_id}' does not yet support previous track")

    def set_video_output(self, *_args, **_kwargs) -> None:
        # Provider SDKs manage their own rendering surfaces.
        return


class PlaybackEngineManager:
    """
    Coordinates provider playback engines and exposes a backend-compatible API.
    """

    _URL_HINTS: ClassVar[dict[str, str]] = {
        "open.spotify.com": "spotify",
        "spotify:track": "spotify",
        "youtu": "youtube_music",
    }

    def __init__(
        self,
        *,
        feature_flags: PlaybackFeatureFlags,
        logger: logging.Logger | StructuredLogger | None = None,
        controllers: Mapping[str, YouTubeEmbedController] | None = None,
        token_resolver: Callable[[str], Mapping[str, object] | None] | None = None,
        queue_engine: PlaybackQueueEngine | None = None,
    ) -> None:
        self._feature_flags = feature_flags
        if logger is None:
            self._logger = get_logger("viola.playback.manager")._logger
        elif isinstance(logger, StructuredLogger):
            self._logger = logger._logger
        else:
            self._logger = logger
        self._lock = threading.RLock()
        self._engines: dict[str, ProviderPlaybackEngine] = {}
        # Per-user playback sessions for multi-user isolation
        self._user_sessions: dict[str, _UserPlaybackSession] = {}
        self._artwork_callback: ArtworkCallback | None = None
        self._token_resolver = token_resolver
        self._queue_engine = queue_engine

        self._register_defaults(controllers or {})

    def set_queue_engine(self, queue_engine: PlaybackQueueEngine) -> None:
        """Attach a queue engine to receive transport lifecycle notifications."""
        self._queue_engine = queue_engine

    def _resolve_user_id(self, user_id: str | None = None) -> str:
        """Resolve user_id from explicit param or ambient context."""
        if user_id:
            return user_id
        try:
            from core.user_context import get_current_user_id

            return get_current_user_id()
        except Exception:
            from core.user_context import get_device_user_id

            return get_device_user_id()

    def _get_session(self, user_id: str) -> _UserPlaybackSession:
        """Return the playback session for *user_id*, creating on first access.

        Enforces bounded growth:
        - Lazy eviction of sessions idle for >1 hour on every access.
        - Hard cap of _MAX_USER_SESSIONS; when exceeded, the oldest idle
          session (with no active playback) is evicted.
        """
        import time as _time

        session = self._user_sessions.get(user_id)
        if session is not None:
            session.last_accessed = _time.monotonic()
            return session

        # Before creating a new session, evict stale ones
        self._evict_idle_sessions()

        session = _UserPlaybackSession()
        self._user_sessions[user_id] = session
        return session

    def _evict_idle_sessions(self) -> None:
        """Remove sessions that are idle and have no active playback."""
        import time as _time

        now = _time.monotonic()
        stale_ids: list[str] = []
        for uid, sess in self._user_sessions.items():
            if sess.active_handle is not None:
                continue  # Never evict sessions with active playback
            idle_seconds = now - sess.last_accessed
            if idle_seconds > _SESSION_IDLE_TIMEOUT and sess.active_handle is None:
                stale_ids.append(uid)

        for uid in stale_ids:
            del self._user_sessions[uid]

        # Hard cap: if still over limit, evict oldest idle sessions
        if len(self._user_sessions) > _MAX_USER_SESSIONS:
            candidates = [(uid, sess) for uid, sess in self._user_sessions.items() if sess.active_handle is None]
            candidates.sort(key=lambda x: x[1].last_accessed)
            excess = len(self._user_sessions) - _MAX_USER_SESSIONS
            for uid, _sess in candidates[:excess]:
                del self._user_sessions[uid]

    # ------------------------------------------------------------------#
    # Engine registration
    # ------------------------------------------------------------------#
    def _register_defaults(
        self,
        controllers: Mapping[str, YouTubeEmbedController],
    ) -> None:
        """
        Register first-party playback engines.

        Args:
            controllers: Mapping of provider_id -> UI bridge controllers.
                - "youtube_music": YouTubeEmbedController
        """
        spotify_engine = SpotifyWebPlaybackEngine(
            token_resolver=self._token_resolver,
            logger=self._logger.getChild("spotify"),
        )
        self._engines[spotify_engine.provider_id] = spotify_engine

        if controllers:
            # YouTube engine - only pass YouTube controllers
            yt_controller = controllers.get("youtube_music")
            if yt_controller is not None:
                from playback.engines.youtube import YouTubeEmbedController

                # Type narrowing: dict key guarantees this is YouTubeEmbedController
                youtube_engine = YouTubeEmbeddedEngine(
                    controller=cast(YouTubeEmbedController, yt_controller),
                    logger=self._logger.getChild("youtube"),
                )
                self._engines[youtube_engine.provider_id] = youtube_engine

        else:
            # Create engines without controllers if none provided
            youtube_engine = YouTubeEmbeddedEngine(
                controller=None,
                logger=self._logger.getChild("youtube"),
            )
            self._engines[youtube_engine.provider_id] = youtube_engine

        # Spotify CDP engine — controls Spotify via Chrome DevTools Protocol
        spotify_cdp_engine = SpotifyCDPEngine(
            logger=self._logger.getChild("spotify_cdp"),
        )
        self._engines[spotify_cdp_engine.provider_id] = spotify_cdp_engine

    # ------------------------------------------------------------------#
    # Public API
    # ------------------------------------------------------------------#
    def set_artwork_callback(self, callback: ArtworkCallback) -> None:
        self._artwork_callback = callback

    def capability_matrix(self) -> list[dict[str, object]]:
        matrix: list[dict[str, object]] = []
        for engine in self._engines.values():
            matrix.append(
                {
                    "provider_id": engine.provider_id,
                    "display_name": engine.display_name,
                    "capabilities": engine.capabilities.to_dict(),
                    "available": engine.is_available(),
                    "delegates_to_legacy_backend": engine.delegates_to_legacy_backend(),
                    "availability_error": engine.availability_error(),
                }
            )
        return matrix

    def get_engine(self, provider_id: str) -> ProviderPlaybackEngine | None:
        engine = self._engines.get(provider_id)
        if engine is None and provider_id == "browser":
            engine = self._lazy_register_browser_engine()
        return engine

    def _lazy_register_browser_engine(self) -> ProviderPlaybackEngine | None:
        """Lazy-register BrowserPlaybackEngine on first use."""
        from config.settings import settings

        if not settings.browser_provider_enabled:
            return None
        try:
            from playback.engines.browser import BrowserPlaybackEngine

            engine = BrowserPlaybackEngine(
                logger_override=self._logger.getChild("browser"),
            )
            self._engines["browser"] = engine
            self._logger.info("Lazy-registered BrowserPlaybackEngine")
            return engine
        except Exception as exc:
            self._logger.warning("Failed to lazy-register BrowserPlaybackEngine: %r", exc)
            return None

    def identify_provider(self, item: QueueItem) -> str | None:
        # Check playback_mode first — CDP items override default provider routing
        if item.playback_mode == "spotify_cdp":
            return "spotify_cdp"
        if item.provider:
            return str(item.provider)
        if item.source:
            normalized = str(item.source).lower()
            if normalized in self._engines:
                return normalized
            if normalized == "ytsearch1":
                return "youtube_music"
        if item.url:
            url_lower = item.url.lower()
            for hint, provider in self._URL_HINTS.items():
                if hint in url_lower:
                    return provider
        return None

    def resolve_track(self, provider_id: str, query: str) -> QueueItem:
        engine = self.get_engine(provider_id)
        if not engine:
            raise PlaybackError(f"Provider '{provider_id}' not registered")
        if not engine.is_available():
            raise PlaybackError(engine.availability_error() or f"Provider '{provider_id}' unavailable")
        return engine.resolve_track(query)

    def attach_backend(
        self,
        item: QueueItem,
        *,
        upcoming: Iterable[QueueItem],
        default_backend,
    ):
        if self._feature_flags.fallback_to_legacy_backend:
            return default_backend

        # Check if item requires embedded player - must NOT use VLC
        requires_embedded = False
        embedded = False

        # Check capabilities dict for embedded flags
        requires_embedded_value = item.capabilities.get("requires_embedded_player")
        if isinstance(requires_embedded_value, bool):
            requires_embedded = requires_embedded_value

        embedded_value = item.capabilities.get("embedded")
        if isinstance(embedded_value, bool):
            embedded = embedded_value

        # If item requires embedded player, we MUST use embedded engine, not VLC
        if requires_embedded or embedded:
            provider_id = self.identify_provider(item)
            if not provider_id:
                self._logger.warning(
                    "Item requires embedded player but provider could not be identified: %s",
                    item.id,
                )
                # Still reject VLC - embedded requirement is strict
                return None

            engine = self.get_engine(provider_id)
            if engine is None:
                self._logger.error(
                    "EMBED_ROUTING_ERROR: Item requires embedded player but no engine found for provider=%s item_id=%s",
                    provider_id,
                    item.id,
                )
                return None

            if not engine.is_available():
                self._logger.error(
                    "EMBED_ROUTING_ERROR: Embedded engine unavailable for provider=%s item_id=%s error=%s",
                    provider_id,
                    item.id,
                    engine.availability_error(),
                )
                return None

            # Log embedded player selection
            self._logger.info(
                "EMBED_PLAYER_SELECT provider=%s backend=%s video_id=%s title=%s url=%s",
                provider_id,
                engine.__class__.__name__,
                item.video_id or "none",
                item.title or "Unknown",
                item.url[:80] if item.url else "none",
            )

            adapter = EngineBackedBackend(
                engine,
                self,
                self._feature_flags,
                self._logger,
            )
            adapter.set_context(item, upcoming, artwork_callback=self._artwork_callback)
            return adapter

        # Normal provider-based routing (non-embedded)
        provider_id = self.identify_provider(item)
        if not provider_id:
            return default_backend

        engine = self.get_engine(provider_id)
        if engine is None:
            self._logger.debug("No engine registered for provider %s", provider_id)
            return default_backend
        if not engine.is_available():
            self._logger.debug(
                "Provider %s unavailable (%s); using legacy backend",
                provider_id,
                engine.availability_error(),
            )
            return default_backend

        if engine.delegates_to_legacy_backend():
            try:
                queue_context = QueueContext(
                    upcoming_items=list(upcoming),
                    feature_flags=self._feature_flags.to_dict(),
                )
                engine.prefetch(list(upcoming), queue_context=queue_context)
            except Exception as exc:  # pragma: no cover
                self._logger.debug("Prefetch delegation failed: %s", exc)
            return default_backend

        # Log with distinctive marker for YouTube Music pipeline tracing
        if provider_id == "youtube_music":
            self._logger.info(
                "YTM_ENGINE_SELECT provider=youtube_music backend=YouTubeEmbeddedEngine video_id=%s",
                item.video_id or "none",
            )

        adapter = EngineBackedBackend(
            engine,
            self,
            self._feature_flags,
            self._logger,
        )
        adapter.set_context(item, upcoming, artwork_callback=self._artwork_callback)
        return adapter

    def pause_active(self, user_id: str | None = None) -> None:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            backend = session.active_backend
            handle = session.active_handle
            if backend:
                backend.pause()
            elif handle:
                handle.mark_paused()

    def resume_active(self, user_id: str | None = None) -> None:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            backend = session.active_backend
            handle = session.active_handle
            if backend:
                backend.resume()
            elif handle and handle.is_paused():
                handle.mark_resumed()

    def stop_active(self, user_id: str | None = None) -> None:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            backend = session.active_backend
            handle = session.active_handle
            if backend:
                backend.stop()
            if handle:
                handle.request_stop()
            session.active_backend = None
            session.active_handle = None

    def set_volume(self, level: int, user_id: str | None = None) -> int:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            backend = session.active_backend
            if backend:
                return backend.set_volume(level)
        return level

    def active_provider(self, user_id: str | None = None) -> str | None:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            handle = session.active_handle
            if handle:
                return str(handle.provider_id)
        return None

    def current_position(self, user_id: str | None = None) -> float | None:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            backend = session.active_backend
            if backend:
                try:
                    return float(backend.get_position())
                except Exception:  # pragma: no cover - defensive
                    return None
        return None

    def current_duration(self, user_id: str | None = None) -> float | None:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            backend = session.active_backend
            if backend:
                try:
                    return float(backend.get_duration())
                except Exception:  # pragma: no cover - defensive
                    return None
        return None

    # ------------------------------------------------------------------#
    # Internal callbacks
    # ------------------------------------------------------------------#
    def _set_active_handle(
        self,
        handle: PlaybackHandle,
        *,
        backend: EngineBackedBackend,
        user_id: str | None = None,
    ) -> None:
        uid = self._resolve_user_id(user_id)
        with self._lock:
            session = self._get_session(uid)
            session.active_handle = handle
            session.active_backend = backend

    def _on_handle_finished(self, handle: PlaybackHandle) -> None:
        with self._lock:
            for session in self._user_sessions.values():
                if session.active_handle is handle:
                    session.active_handle = None
                    session.active_backend = None
        if handle.stop_requested():
            self._logger.debug(
                "Ignoring stop-requested provider finish for item=%s provider=%s",
                getattr(handle.item, "id", "unknown"),
                getattr(handle, "provider_id", "unknown"),
            )
            return
        if self._queue_engine:
            provider = getattr(handle, "provider_id", None)
            self._queue_engine.handle_transport_finished(source=provider or "provider")
