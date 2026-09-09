"""Provider-centric resolution logic extracted from MusicPlayer."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from models.player import QueueItem
from music.exceptions import ResolutionError
from music.providers.checker import is_youtube_url
from music.providers.errors import (
    MusicProviderUnavailableError,
    MusicTrackNotFoundError,
    NoActiveMusicProviderError,
)
from music.resolution.error_handler import ResolutionErrorHandler
from music.resolution.helpers import ResolutionMetadata, queue_item_from_resolution
from music.resolution.provider_router_metadata import ProviderRouterMetadataResolver
from music.resolution.youtube_resolver import YouTubeMusicResolver

Source = Literal["ytsearch1", "url", "local", "browser", "spotify_cdp"]


def _spotify_track_id_from_query(value: str | None) -> str | None:
    """Extract a Spotify track id from a spotify: URI or open.spotify.com URL."""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("spotify:track:"):
        return raw[len("spotify:track:") :].strip() or None
    if "open.spotify.com/track/" in raw:
        return raw.split("open.spotify.com/track/", 1)[1].split("?", 1)[0].split("/", 1)[0].strip() or None
    return None


def _is_spotify_identifier(value: str | None) -> bool:
    """True for any Spotify URI/URL (track, album, playlist, artist)."""
    raw = (value or "").strip().lower()
    return raw.startswith("spotify:") or "open.spotify.com/" in raw


class CustomResolver(Protocol):
    def resolve(self, query: str, source: Source) -> ResolutionMetadata: ...


class BackgroundResolutionResult(Protocol):
    is_success: bool

    def to_metadata(self, *, default_source: Source, resolver_label: str) -> ResolutionMetadata: ...


class AsyncBackgroundResolver(Protocol):
    async def resolve(self, query: str, source: Source) -> BackgroundResolutionResult: ...


class BackgroundWorker(Protocol):
    def resolve(self, query: str, source: Source) -> ResolutionMetadata: ...


class ProviderEngineManager(Protocol):
    def get_engine(self, provider_id: str) -> object | None: ...

    def resolve_track(self, provider_id: str, identifier: str) -> QueueItem: ...


class ProviderRouter:
    """Handles provider-aware query resolution into QueueItems."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        resolver: CustomResolver | None,
        background_resolver: AsyncBackgroundResolver | None,
        background_available: bool,
        error_handler: ResolutionErrorHandler,
        record_resolution_failure: Callable[[str, Source, Exception], None],
        background_worker: BackgroundWorker | None = None,
    ) -> None:
        self._logger: logging.Logger = logger
        self._resolver: CustomResolver | None = resolver
        self._background_resolver: AsyncBackgroundResolver | None = background_resolver
        self._background_available: bool = background_available
        self._error_handler: ResolutionErrorHandler = error_handler
        self._record_resolution_failure: Callable[[str, Source, Exception], None] = record_resolution_failure
        self._background_worker: BackgroundWorker | None = background_worker
        self._metadata_resolver: ProviderRouterMetadataResolver = ProviderRouterMetadataResolver(logger)

    def set_background_resolver(
        self,
        resolver: AsyncBackgroundResolver | None,
        available: bool,
    ) -> None:
        self._background_resolver = resolver
        self._background_available = available

    def set_custom_resolver(self, resolver: CustomResolver | None) -> None:
        self._resolver = resolver

    def resolve_to_queue_item(
        self,
        query: str,
        source: Source | None,
        metadata: Mapping[str, object] | None = None,
        *,
        engine_manager: ProviderEngineManager | None = None,
    ) -> QueueItem:
        if metadata:
            # Spotify track identifiers must NEVER take the generic
            # inline-metadata path: that path types items
            # provider=None/playback_mode="embedded_webview", which routes
            # playback down the EMBEDDED_DEFER self-PID capture branch and
            # kills multiroom capture (2026-07-02 field incident: a queued
            # spotify:track: item notified ProcTap of Viola's own PID and
            # spokes went permanently silent). Build the item through the
            # canonical Spotify constructor instead, then keep the caller's
            # richer display metadata.
            metadata_url = metadata.get("url")
            spotify_target: str | None = None
            for candidate in (
                metadata_url if isinstance(metadata_url, str) else None,
                query,
            ):
                if _spotify_track_id_from_query(candidate):
                    spotify_target = candidate
                    break
            if spotify_target:
                item = self._resolve_via_spotify_cdp(spotify_target)
                meta_title = metadata.get("title")
                if isinstance(meta_title, str) and meta_title.strip():
                    item.title = meta_title.strip()
                meta_artist = metadata.get("artist")
                if isinstance(meta_artist, str) and meta_artist.strip():
                    item.artist = meta_artist.strip()
                return item
            try:
                metadata_resolution = self._metadata_to_resolution(query, source, metadata)
            except Exception as exc:
                self._logger.exception("Inline metadata invalid for %s: %s", query[:50], exc)
            else:
                if metadata_resolution is not None:
                    queue_item = queue_item_from_resolution(
                        metadata_resolution,
                        force_source=source or metadata_resolution.source,
                        logger=self._logger,
                    )
                    if not getattr(queue_item, "id", None):
                        queue_item.id = str(uuid.uuid4())
                    return queue_item
        manager = engine_manager
        if manager is not None and ":" in (query or ""):
            try:
                provider_id, rest = query.split(":", 1)
                engine = manager.get_engine(provider_id)
                if engine is not None:
                    item = self._ensure_queue_item(
                        manager.resolve_track(provider_id, rest),
                        context="engine_manager.resolve_track",
                    )
                    if not item.provider:
                        item.provider = provider_id
                    item.capabilities.setdefault("requires_embedded_player", True)
                    item.capabilities.setdefault("embedded", True)
                    if not item.playback_mode:
                        item.playback_mode = "embedded_webview"
                    return item
            except Exception as e:
                self._logger.exception(
                    "Engine-scheme resolution failed (non-critical); falling back: %s",
                    e,
                )

        if source == "local" or (
            not source
            and (
                query.startswith(("/", "./", "../"))
                or (":" in query and query.split(":")[0] in ("file", "C", "D", "E", "F", "G", "H"))
            )
        ):
            actual_source: Source = source or "local"
            try:
                metadata_obj = self._resolve_metadata(query, actual_source)
            except Exception as exc:
                self._record_resolution_failure(query, actual_source, exc)
                raise
            return self._to_queue_item(metadata_obj, actual_source)

        if source == "url" or query.startswith(("http://", "https://")):
            actual_source = source or "url"
            try:
                metadata_obj = self._resolve_metadata(query, actual_source)
            except Exception as exc:
                self._record_resolution_failure(query, actual_source, exc)
                raise
            return self._to_queue_item(metadata_obj, actual_source)

        actual_source = self._determine_provider_source(query)

        if actual_source == "browser":
            return self._resolve_via_browser_provider(query)

        if actual_source == "spotify_cdp":
            return self._resolve_via_spotify_cdp(query)

        try:
            metadata_obj = self._resolve_metadata(query, actual_source)
        except Exception as exc:
            self._record_resolution_failure(query, actual_source, exc)
            raise
        return self._to_queue_item(metadata_obj, actual_source)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _to_queue_item(self, metadata: ResolutionMetadata, source: Source) -> QueueItem:
        queue_item = queue_item_from_resolution(metadata, force_source=source, logger=self._logger)
        if not getattr(queue_item, "id", None):
            queue_item.id = str(uuid.uuid4())
        return queue_item

    def _ensure_queue_item(self, candidate: object, *, context: str) -> QueueItem:
        if isinstance(candidate, QueueItem):
            return candidate
        try:
            return QueueItem.model_validate(candidate)
        except ValidationError as exc:  # pragma: no cover - defensive
            raise TypeError(f"{context} returned incompatible queue payload: {type(candidate)!r}") from exc

    def _metadata_to_resolution(
        self, query: str, source: Source | None, metadata: Mapping[str, object]
    ) -> ResolutionMetadata | None:
        raw_url = metadata.get("url")
        if not isinstance(raw_url, str):
            return None
        normalized_url = raw_url.strip()
        if not normalized_url:
            return None

        source_hint = metadata.get("source")
        effective_source: Source = source or cast(Source, source_hint) if isinstance(source_hint, str) else "url"
        raw_title = metadata.get("title")
        name_fallback = metadata.get("name")
        title = (
            raw_title
            if isinstance(raw_title, str) and raw_title.strip()
            else (
                name_fallback if isinstance(name_fallback, str) and name_fallback.strip() else query or normalized_url
            )
        )
        artist_value = metadata.get("artist")
        artist = artist_value if isinstance(artist_value, str) else None
        video_id_value = metadata.get("video_id")
        video_id = video_id_value if isinstance(video_id_value, str) else None
        provider_value = metadata.get("provider")
        provider = provider_value if isinstance(provider_value, str) else None
        resolver_path = "metadata.inline"
        extras_value = metadata.get("extras")
        extras: dict[str, object]
        if isinstance(extras_value, Mapping):
            extras_mapping = cast(Mapping[str, object], extras_value)
            extras = {str(key): value for key, value in extras_mapping.items()}
        else:
            extras = {}
        capabilities_value = metadata.get("capabilities")
        capabilities: dict[str, object]
        if isinstance(capabilities_value, Mapping):
            capabilities_mapping = cast(Mapping[str, object], capabilities_value)
            capabilities = {str(key): value for key, value in capabilities_mapping.items()}
        else:
            capabilities = {}
        # Gate direct stream URLs (googlevideo.com) behind the unsafe flag.
        # YouTube stream extraction may violate YouTube Terms of Service.
        from config.settings import settings as _cfg

        _streaming_flag = _cfg.unsafe_allow_youtube_streaming
        allow_stream_urls = "googlevideo.com" in normalized_url and _streaming_flag
        if "googlevideo.com" in normalized_url and not _streaming_flag:
            self._logger.warning(
                "Blocked googlevideo.com stream URL — unsafe_allow_youtube_streaming is disabled: %s",
                normalized_url[:80],
            )
        resolved_url = normalized_url
        playback_mode = "vlc_stream" if allow_stream_urls else "embedded_webview"

        explicit_mode = metadata.get("playback_mode")
        if _is_spotify_identifier(normalized_url):
            # Spotify content is engine-managed (spotify_cdp). Typing it
            # embedded_webview sends playback down the EMBEDDED_DEFER
            # self-PID capture branch and kills multiroom capture
            # (2026-07-02). This wins even over an explicit embedded mode —
            # that combination IS the bug shape.
            provider = provider or "spotify"
            playback_mode = "spotify_cdp"
        elif isinstance(explicit_mode, str) and explicit_mode:
            playback_mode = explicit_mode

        if is_youtube_url(normalized_url):
            provider = provider or "youtube_music"
            (
                resolved_url,
                resolved_title,
                resolved_video_id,
                resolved_artist,
                allow_stream_urls,
            ) = self._resolve_metadata_youtube_url(
                normalized_url,
                effective_source,
                title,
                video_id,
                artist,
            )
            title = resolved_title or title
            video_id = resolved_video_id or video_id
            artist = resolved_artist or artist
            resolver_path = "metadata.youtube_watch"
            playback_mode = "vlc_stream" if allow_stream_urls else "embedded_webview"

        validated_url = self._validate_metadata_url(resolved_url, allow_stream_urls=allow_stream_urls)
        if "playback_mode" not in extras:
            extras["playback_mode"] = playback_mode

        return ResolutionMetadata(
            url=validated_url,
            title=title,
            source=effective_source,
            video_id=video_id,
            artist=artist,
            provider=provider,
            resolver_path=resolver_path,
            capabilities=capabilities,
            extras=extras,
        )

    def _resolve_metadata_youtube_url(
        self,
        url: str,
        _source: Source,
        fallback_title: str | None,
        fallback_video_id: str | None,
        fallback_artist: str | None,
    ) -> tuple[str, str | None, str | None, str | None, bool]:
        self._logger.debug(
            "Embedded playback will handle YouTube URL %s; no direct stream extraction",
            url[:80],
        )
        return (url, fallback_title, fallback_video_id, fallback_artist, False)

    def validate_metadata_url(self, url: str, *, allow_stream_urls: bool = False) -> str:
        """Public hook for callers that need to validate metadata overrides."""
        return self._validate_metadata_url(url, allow_stream_urls=allow_stream_urls)

    def _validate_metadata_url(self, url: str, *, allow_stream_urls: bool = False) -> str:
        """
        Enforce PRD §7.7 embedded-player rules for metadata-derived URLs.

        Reject direct stream URLs (googlevideo) unless explicitly allowed and ensure
        that YouTube playback only flows through watch/short URLs that the embedded
        backend can handle without scraping.
        """
        if not url:
            return url

        if "googlevideo.com" in url and not allow_stream_urls:
            self._logger.error(
                "Rejected direct stream URL (googlevideo.com) - violates control-layer-only policy: %s",
                url[:80],
            )
            raise NotImplementedError(
                "Direct stream URLs (googlevideo.com) are not allowed. "
                + "YouTube playback must use the embedded player with video IDs only. "
                + "See docs/LEGACY_YOUTUBE_SCRAPING_REMOVAL.md"
            )

        is_youtube_watch_url = (
            "youtube.com/watch" in url
            or "youtu.be/" in url
            or (url.startswith("http") and "youtube.com" in url and "/watch" in url)
        )

        if not is_youtube_watch_url:
            return url

        return url

    def _resolve_url_source_metadata(self, query: str, source: Source) -> ResolutionMetadata:
        normalized_url = query.strip()
        if is_youtube_url(normalized_url):
            (
                resolved_url,
                resolved_title,
                resolved_video_id,
                resolved_artist,
                allow_stream_urls,
            ) = self._resolve_metadata_youtube_url(normalized_url, source, normalized_url, None, None)
            extras = {"playback_mode": "embedded_webview"}
            validated_url = self._validate_metadata_url(resolved_url, allow_stream_urls=allow_stream_urls)
            return ResolutionMetadata(
                url=validated_url,
                title=resolved_title or normalized_url,
                source=source,
                video_id=resolved_video_id,
                artist=resolved_artist,
                provider="youtube_music",
                resolver_path="url.youtube",
                extras=extras,
            )

        # Gate googlevideo.com stream URLs behind the unsafe flag
        from config.settings import settings as _cfg

        _allow_gv = "googlevideo.com" in normalized_url and _cfg.unsafe_allow_youtube_streaming
        validated_url = self._validate_metadata_url(normalized_url, allow_stream_urls=_allow_gv)
        return ResolutionMetadata(
            url=validated_url,
            title=normalized_url,
            source=source,
            provider=None,
            resolver_path="url.direct",
        )

    def _resolve_metadata(self, query: str, source: Source) -> ResolutionMetadata:
        return self._metadata_resolver.resolve_metadata(query, source, self._resolver)

    @staticmethod
    def _current_user_id() -> str:
        from core.user_context import get_current_user_id

        return get_current_user_id()

    def _resolve_via_provider(self, query: str, source: Source) -> ResolutionMetadata | None:
        try:
            from music.providers.active_provider import get_active_music_provider_id

            active_provider_id = get_active_music_provider_id()
        except Exception as e:
            self._logger.exception("Failed to get active music provider ID: %s", e)
            active_provider_id = None

        self._logger.info(
            "provider_router._resolve_via_provider: ENTRY query=%r source=%s active_provider_id=%s",
            query,
            source,
            active_provider_id,
        )
        user_id = self._current_user_id()

        if source == "url" or query.startswith(("http://", "https://")):
            early_exit_msg = (
                "provider_router._resolve_via_provider: EARLY_EXIT reason=url_source "
                "query=%r source=%s active_provider_id=%s"
            )
            self._logger.warning(early_exit_msg, query, source, active_provider_id)
            return None
        if source == "local":
            early_exit_msg = (
                "provider_router._resolve_via_provider: EARLY_EXIT reason=local_source "
                "query=%r source=%s active_provider_id=%s"
            )
            self._logger.warning(early_exit_msg, query, source, active_provider_id)
            return None

        # Spotify CDP — handled by dedicated resolver, not YouTubeMusicResolver
        if source == "spotify_cdp" or active_provider_id in ("spotify", "spotify_cdp"):
            self._logger.info(
                "provider_router._resolve_via_provider: routing to Spotify CDP for query=%r",
                query[:50],
            )
            return None  # Spotify is resolved in _resolve_via_spotify_cdp, not here

        try:
            resolver = YouTubeMusicResolver(logger=self._logger)
            metadata = resolver.resolve_query(
                query=query,
                source=source,
                active_provider_id=active_provider_id,
                user_id=user_id,
            )
            return metadata
        except ValueError as e:
            self._logger.exception("Invalid metadata from provider resolution: %s", e)
            return None
        except ResolutionError:
            raise
        except MusicProviderUnavailableError:
            raise
        except Exception as exc:
            raise self._error_handler.wrap_provider_error(
                original_error=exc,
                query=query,
                user_id=user_id,
                stage="resolve_via_provider",
                root_cause="unexpected_exception_in_resolve_via_provider",
            ) from exc

    def _resolve_via_background(self, query: str, source: Source) -> ResolutionMetadata | None:
        resolver = self._background_resolver
        if resolver is None or not self._background_available:
            return None

        if self._background_worker is not None:
            try:
                return self._background_worker.resolve(query, source)
            except Exception as exc:
                self._logger.debug("Background worker error: %s", exc)
                return None

        async def _resolve_async() -> BackgroundResolutionResult:
            return await resolver.resolve(query, source)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_resolve_async())
        except Exception as exc:
            self._logger.debug("Background resolver error: %s", exc)
            return None
        finally:
            with suppress(Exception):
                loop.run_until_complete(asyncio.sleep(0))
            loop.close()

        if not result.is_success:
            return None

        try:
            return result.to_metadata(
                default_source=source,
                resolver_label="background",
            )
        except Exception as exc:
            self._logger.debug("Background resolver produced invalid metadata: %s", exc)
            return None

    def _resolve_via_browser_provider(self, query: str) -> QueueItem:
        """Resolve a search query through the browser-native provider.

        The browser provider creates a synthetic QueueItem whose URL is a
        search page.  When this item is played, the QWebEngineView navigates
        to the search URL and the recipe JS clicks the first result.
        """
        from music.providers.browser.music_provider import BrowserMusicProvider

        user_id = self._current_user_id()

        provider = BrowserMusicProvider()
        results = provider.search_tracks(user_id, query, limit=1)
        if not results.items:
            raise MusicTrackNotFoundError(f"Browser provider returned no results for '{query}'")

        track = results.items[0]
        from music.providers.models import PlaybackContext

        stream = provider.resolve_stream(user_id, track, playback=PlaybackContext())

        search_url = str(stream.url)
        # Source the title from the resolved TrackSummary rather than the raw
        # query, and carry the provider's honesty flag: the browser provider's
        # title is a query-echo placeholder (blind first-result click), not a
        # confirmed match. Propagating title_unverified lets now_playing consumers
        # (e.g. _classify_query_match) avoid reporting a spurious "exact" match
        # before the embedded player reports the real track (#2806).
        title_unverified = bool(track.extras.get("title_unverified"))
        item = QueueItem(
            id=str(uuid.uuid4()),
            title=track.title,
            url=search_url,
            source="browser",
            provider="browser",
            playback_mode="embedded_webview",
            capabilities={
                "requires_embedded_player": True,
                "embedded": True,
                "search_url": search_url,
                "recipe": track.extras.get("recipe", "youtube_music"),
                "resolver_path": "provider.browser",
                "resolver_extras": {"playback_mode": "embedded_webview"},
                "title_unverified": title_unverified,
            },
        )
        self._logger.info(
            "Browser provider resolved query=%s → url=%s",
            query[:50],
            search_url,
        )
        return item

    def _resolve_via_spotify_cdp(self, query: str) -> QueueItem:
        """Resolve a search query through the Spotify CDP provider.

        Uses SpotifyCDPProvider to search Spotify's web player via Chrome
        DevTools Protocol and returns a QueueItem configured for CDP playback.

        Short-circuit: when the query is already a Spotify URI / track URL,
        skip the search and build a QueueItem with the URI in capabilities
        so the playback engine plays exact via play_track_by_uri (bypassing
        Spotify's own search ranking, which can return a different track).
        """
        from music.providers.models import TrackSummary
        from music.providers.spotify_cdp import SpotifyCDPProvider

        # Direct URI path — extract the track id and skip search.
        # No user context is needed here: this branch is pure item
        # construction and must work on worker threads (playlist refill)
        # outside HTTP request context.
        track_id_for_uri = _spotify_track_id_from_query(query)

        if track_id_for_uri:
            spotify_uri = "spotify:track:%s" % track_id_for_uri
            placeholder_url = "https://open.spotify.com/track/%s" % track_id_for_uri
            item = QueueItem(
                id=str(uuid.uuid4()),
                title=query,  # caller may have richer metadata; runtime can update on poll
                url=placeholder_url,
                source="spotify_cdp",
                artist="",
                provider="spotify",
                playback_mode="spotify_cdp",
                capabilities={
                    "requires_embedded_player": False,
                    "uri": spotify_uri,
                    "resolver_path": "provider.spotify_cdp.direct_uri",
                    "resolver_extras": {"playback_mode": "spotify_cdp"},
                },
            )
            self._logger.info(
                "Spotify CDP direct-URI resolution: track_id=%s",
                track_id_for_uri,
            )
            return item

        user_id = self._current_user_id()
        provider = SpotifyCDPProvider()
        results = provider.search_tracks(user_id, query, limit=1)
        if not results.items:
            raise MusicTrackNotFoundError(f"Spotify returned no results for '{query}'")

        track = results.items[0]
        from music.providers.models import PlaybackContext

        stream = provider.resolve_stream(user_id, track, playback=PlaybackContext())

        # Build QueueItem with Spotify CDP metadata
        search_index = track.extras.get("search_index", "0")
        spotify_uri = track.extras.get("uri", "")

        item = QueueItem(
            id=str(uuid.uuid4()),
            title=track.title or query,
            url=str(stream.url),
            source="spotify_cdp",
            artist=track.artist_name,
            provider="spotify",
            playback_mode="spotify_cdp",
            capabilities={
                "requires_embedded_player": False,
                "search_index": search_index,
                "uri": spotify_uri,
                "resolver_path": "provider.spotify_cdp",
                "resolver_extras": {"playback_mode": "spotify_cdp"},
            },
        )
        self._logger.info(
            "Spotify CDP resolved query=%s → title=%s artist=%s",
            query[:50],
            track.title,
            track.artist_name,
        )
        return item

    def _determine_provider_source(self, query: str) -> Source:
        try:
            from music.providers.selection import select_first_run_music_provider

            selection = select_first_run_music_provider(query)
            self._logger.info(
                "Selected music provider=%s source=%s reason=%s query=%s",
                selection.provider_id,
                selection.source,
                selection.reason,
                query[:50],
            )
            return selection.source
        except MusicProviderUnavailableError:
            raise
        except Exception as exc:
            self._logger.exception(
                "First-run provider selection failed (query=%s): %s",
                query[:50],
                exc,
            )

        try:
            from music.providers.active_provider import get_active_music_provider_id

            active_provider_id = get_active_music_provider_id()
            if not active_provider_id:
                active_provider_id = "youtube_iframe"
                self._logger.info(
                    "No active provider set; defaulting to youtube_iframe for query=%s",
                    query[:50],
                )

            if active_provider_id == "local":
                return "local"

            if active_provider_id in ("spotify", "spotify_cdp"):
                return "spotify_cdp"

            if active_provider_id == "browser":
                from config.settings import settings

                if settings.browser_provider_enabled:
                    return "browser"
                self._logger.warning(
                    "Browser provider selected but not enabled (set BROWSER_PROVIDER_ENABLED=true)",
                )

            if active_provider_id not in ("youtube_music", "youtube_iframe", "browser"):
                self._logger.warning(
                    "Rejected play intent: active provider '%s' not supported by this player (query=%s)",
                    active_provider_id,
                    query[:50],
                )
                raise MusicProviderUnavailableError(
                    f"Active provider '{active_provider_id}' is not supported by this player. "
                    + "Say 'connect Spotify' or 'connect YouTube Music' and I'll set it up."
                )
        except (
            NoActiveMusicProviderError,
            MusicProviderUnavailableError,
            MusicTrackNotFoundError,
        ):
            raise
        except Exception as exc:
            self._logger.exception(
                "Error checking active provider (query=%s): %s",
                query[:50],
                exc,
            )
            raise NoActiveMusicProviderError(
                "Music provider check failed. Say 'connect Spotify' or 'connect YouTube Music' to set it up."
            ) from exc

        return "ytsearch1"
