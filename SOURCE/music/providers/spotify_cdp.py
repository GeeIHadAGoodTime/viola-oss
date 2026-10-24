"""
Spotify CDP Provider — MusicProvider implementation using Chrome DevTools Protocol.

Uses the SpotifyCDPController for all Spotify DOM interaction.
Audio plays through the user's own Chrome browser with full DRM/codec support.

This provider overrides the stub SpotifyProvider via @auto_register with override=True
(which is the default behavior of auto_register).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, NoReturn, cast

from pydantic import HttpUrl

from core.exceptions import ServiceUnavailableError
from core.logging_config import get_logger
from music.spotify.cdp_controller import SpotifyCDPError

from .base import MusicProvider
from .models import (
    AuthContext,
    AuthSession,
    PaginatedResult,
    PlaybackContext,
    PlaylistSummary,
    ProviderCapabilities,
    ProviderFeature,
    ProviderName,
    SearchResults,
    StreamInfo,
    TrackSummary,
)
from .registry import auto_register

if TYPE_CHECKING:
    from music.spotify.cdp_controller import SpotifyCDPController

logger = get_logger(__name__)

# Per-user controller instances shared between provider and engine
_cdp_controllers: dict[str, SpotifyCDPController] = {}


def _resolve_controller_user_id(user_id: str | None = None) -> str:
    if user_id:
        return user_id
    from core.user_context import get_current_or_device_user_id

    return get_current_or_device_user_id()


def get_cdp_controller(user_id: str | None = None) -> SpotifyCDPController:
    """Get or create the CDP controller for one authenticated user."""
    resolved_user_id = _resolve_controller_user_id(user_id)
    controller = _cdp_controllers.get(resolved_user_id)
    if controller is None:
        from music.spotify.cdp_controller import SpotifyCDPController

        controller = SpotifyCDPController(user_id=resolved_user_id)
        _cdp_controllers[resolved_user_id] = controller
    return controller


def set_cdp_controller(controller: SpotifyCDPController, *, user_id: str | None = None) -> None:
    """Set a user's CDP controller (for testing/DI)."""
    resolved_user_id = _resolve_controller_user_id(user_id or getattr(controller, "_user_id", None))
    _cdp_controllers[resolved_user_id] = controller


# ---------------------------------------------------------------------------
# Spotify Liked Songs (the user's real Spotify library, via the web player)
# ---------------------------------------------------------------------------
# Internal play query for the Liked Songs collection. The optional suffix
# carries an explicit shuffle choice; without it Spotify's own shuffle setting
# is left as the user had it.
SPOTIFY_LIKED_SONGS_URI = "spotify:collection:tracks"
_SPOTIFY_PROVIDER_IDS = frozenset({"spotify", "spotify_cdp"})


def spotify_liked_songs_query(shuffle: bool | None = None) -> str:
    if shuffle is None:
        return SPOTIFY_LIKED_SONGS_URI
    return "%s?shuffle=%s" % (SPOTIFY_LIKED_SONGS_URI, "on" if shuffle else "off")


def parse_spotify_liked_songs_query(query: object) -> tuple[bool, bool | None]:
    """Return ``(is_liked_songs, shuffle)`` for a play query."""
    if not isinstance(query, str):
        return False, None
    raw = query.strip()
    base, _, suffix = raw.partition("?")
    base = base.rstrip("/")
    if base.lower() not in (SPOTIFY_LIKED_SONGS_URI, "https://open.spotify.com/collection/tracks"):
        return False, None
    shuffle: bool | None = None
    if suffix.lower() == "shuffle=on":
        shuffle = True
    elif suffix.lower() == "shuffle=off":
        shuffle = False
    return True, shuffle


def wants_spotify_liked_songs(provider: object = None, *, user_id: str | None = None) -> bool:
    """Whether a "play my liked songs" request means the user's Spotify Liked Songs.

    An explicit provider wins. With none given, Spotify Liked Songs play when
    Spotify is the active music service; otherwise Viola's own favorites do.
    """
    explicit = str(provider or "").strip().lower()
    if explicit:
        return explicit in _SPOTIFY_PROVIDER_IDS
    try:
        from music.providers.active_provider import get_active_music_provider_id

        return get_active_music_provider_id(user_id) in _SPOTIFY_PROVIDER_IDS
    except Exception:
        logger.debug("Active music provider lookup failed", exc_info=True)
        return False


def spotify_track_id_from_now_playing(now_playing: object) -> str | None:
    """Bare Spotify track id of a now-playing item, or None if it is not Spotify."""
    if now_playing is None:
        return None

    def _get(name: str) -> object:
        if isinstance(now_playing, dict):
            return now_playing.get(name)
        return getattr(now_playing, name, None)

    provider = str(_get("provider") or "").strip().lower()
    playback_mode = str(_get("playback_mode") or "").strip().lower()
    if provider not in _SPOTIFY_PROVIDER_IDS and playback_mode != "spotify_cdp":
        return None
    from music.spotify.cdp_controller import normalize_spotify_track_id

    # The web player is the truth for "this song": after a Liked Songs start
    # or Spotify's own auto-advance, the queue item still names the request.
    try:
        live = get_cdp_controller().get_now_playing()
    except Exception:
        live = None
    candidates: list[object] = [live.get("track_id") if isinstance(live, dict) else None]
    capabilities = _get("capabilities")
    if isinstance(capabilities, dict):
        candidates.append(capabilities.get("uri"))
    candidates.extend([_get("url"), _get("video_id"), _get("id")])
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            return normalize_spotify_track_id(candidate)
        except SpotifyCDPError:
            continue
    return None


def set_spotify_track_saved(track_id: str, saved: bool, *, user_id: str | None = None) -> dict[str, object]:
    """Add/remove a track in the user's Spotify Liked Songs through the web player.

    Blocking (runs on the controller's CDP thread). Raises SpotifyCDPError
    with a user-facing message on failure.
    """
    controller = get_cdp_controller(user_id)
    controller.ensure_running(wait_for_login=False)
    return controller.set_track_saved(track_id, saved)


@auto_register(ProviderName.SPOTIFY)
class SpotifyCDPProvider(MusicProvider[None]):
    """Provider for Spotify via Chrome DevTools Protocol.

    Searches Spotify's web player by navigating Chrome to the search page
    and extracting results from the DOM. Playback happens through the CDP
    engine, not through stream URLs.
    """

    display_name = "Spotify"
    provider_name = ProviderName.SPOTIFY

    def __init__(self) -> None:
        super().__init__()
        self._controllers_by_user: dict[str, SpotifyCDPController] = {}
        self._controller: SpotifyCDPController | None = None

    def _get_controller(self, user_id: str | None = None) -> SpotifyCDPController:
        """Lazy-init: get the shared controller and ensure it's running."""
        if self._controller is not None:
            return self._controller
        resolved_user_id = _resolve_controller_user_id(user_id)
        controller = self._controllers_by_user.get(resolved_user_id)
        if controller is None:
            controller = get_cdp_controller(user_id=resolved_user_id)
            self._controllers_by_user[resolved_user_id] = controller
        return controller

    def _raise_search_unavailable(
        self,
        query: str,
        *,
        operation: str,
        exc: Exception,
    ) -> NoReturn:
        logger.exception("Spotify CDP %s failed for query=%r", operation, query)
        raise ServiceUnavailableError(
            "Spotify",
            "CDP %s failed: %s" % (operation, exc),
        ) from exc

    # ------------------------------------------------------------------
    # MusicProvider ABC
    # ------------------------------------------------------------------
    def authenticate_user(self, user_id: str, context: AuthContext) -> AuthSession:
        """CDP auth uses browser cookies — no OAuth flow needed."""
        controller = self._get_controller(user_id)
        try:
            controller.ensure_running()
            is_linked = controller.is_logged_in()
        except Exception as exc:
            logger.warning("Spotify CDP auth check failed: %s", exc)
            is_linked = False

        return AuthSession(
            is_linked=is_linked,
            requires_redirect=not is_linked,
            scopes=["playback", "search"],
        )

    def list_playlists(
        self,
        user_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> PaginatedResult[PlaylistSummary]:
        """Not implemented for CDP — would require navigating to library."""
        return PaginatedResult(items=[], next_cursor=None, total=0)

    def search_tracks(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchResults:
        """Search Spotify via CDP browser navigation.

        Navigates Chrome to open.spotify.com/search/{query} and extracts
        track results from the DOM.
        """
        controller = self._get_controller(user_id)

        try:
            controller.ensure_running(wait_for_login=False)
        except (SpotifyCDPError, OSError, RuntimeError, TimeoutError) as exc:
            self._raise_search_unavailable(
                query,
                operation="search initialization",
                exc=exc,
            )

        if not controller.is_logged_in():
            logger.warning("Spotify CDP: not logged in, search may return limited results")

        try:
            raw_results = controller.search(query)
        except (SpotifyCDPError, OSError, RuntimeError, TimeoutError) as exc:
            self._raise_search_unavailable(
                query,
                operation="track search",
                exc=exc,
            )

        items: list[TrackSummary] = []
        for i, result in enumerate(raw_results):
            if i >= limit:
                break
            if not result.get("title") and not result.get("uri"):
                continue

            track_id = str(uuid.uuid4())[:8]
            # Extract Spotify track ID from URI if available
            uri = result.get("uri")
            provider_track_id = None
            if uri and "spotify:track:" in uri:
                provider_track_id = uri.split(":")[-1]

            items.append(
                TrackSummary(
                    id="spotify-cdp-%s" % track_id,
                    title=result.get("title") or "Unknown Track",
                    artist_name=result.get("artist") or "Unknown Artist",
                    album_name=None,
                    duration_ms=None,
                    is_explicit=False,
                    artwork_url=None,  # Artwork resolved on play
                    provider_track_id=provider_track_id,
                    extras={
                        "search_index": str(result.get("index", i)),
                        "uri": uri or "",
                    },
                )
            )

        logger.info(
            "Spotify CDP search: query=%r results=%d",
            query,
            len(items),
        )

        return SearchResults(
            items=items,
            next_cursor=None,
            total=len(items),
            query=query,
        )

    def resolve_stream(
        self,
        user_id: str,
        track: TrackSummary,
        *,
        playback: PlaybackContext,
    ) -> StreamInfo:
        """Resolve stream for CDP playback.

        Spotify CDP doesn't provide a stream URL — playback happens
        via the browser. We return a placeholder URL with metadata
        that the CDP engine uses to control playback.
        """
        # Use spotify: pseudo-URL scheme
        track_id = track.provider_track_id or track.id
        placeholder_url = "https://open.spotify.com/track/%s" % track_id

        return StreamInfo(
            url=cast(HttpUrl, placeholder_url),
            expires_at=None,
            drm=None,
            content_type="audio/spotify-cdp",
            bitrate_kbps=256,
            requires_embedded_player=False,
            metadata={
                "provider": "spotify",
                "playback_mode": "spotify_cdp",
                "search_index": track.extras.get("search_index", "0"),
                "uri": track.extras.get("uri", ""),
            },
        )

    def fetch_artwork(
        self,
        track: TrackSummary,
        *,
        width: int = 512,
        height: int = 512,
    ) -> str | None:
        """Fetch artwork URL from the current now-playing state."""
        controller = self._get_controller()
        try:
            np = controller.get_now_playing()
            if np and np.get("artwork_url"):
                return str(np["artwork_url"])
        except SpotifyCDPError as exc:
            logger.debug("Spotify CDP artwork fetch failed: %s", exc)
        return None

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.provider_name,
            features=[ProviderFeature.GAPLESS, ProviderFeature.LYRICS],
            max_bitrate_kbps=256,
            supports_explicit_filter=False,
            supports_offline_downloads=False,
            notes="CDP-based: audio plays through Chrome, no stream URL extraction.",
        )
