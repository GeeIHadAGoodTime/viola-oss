"""
Spotify Web Playback engine leveraging the official Web Playback SDK (via the
Spotify Web API control surface).
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from types import ModuleType
from typing import Protocol

from config.settings import get_runtime_base_url, settings
from core.logging_config import get_logger
from models.player import QueueItem

from .base import (
    PlaybackCapabilities,
    PlaybackError,
    PlaybackHandle,
    ProviderPlaybackEngine,
    QueueContext,
)

_TRACK_URI_RE = re.compile(r"(spotify:track:|https?://open\.spotify\.com/track/)([A-Za-z0-9]+)")


class _TokenResolver(Protocol):
    """Protocol for token resolution callable."""

    def __call__(self, provider: str) -> Mapping[str, object] | None: ...


class SpotifyWebPlaybackEngine(ProviderPlaybackEngine):
    """
    Adapter for Spotify's official playback stack.

    The implementation controls a Web Playback SDK session through the Spotify
    Web API.  Audio output happens inside the user's authorised Spotify
    session/device; Viola orchestrates queueing, gapless hand-offs, and artwork
    updates.
    """

    _SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing streaming"

    def __init__(
        self,
        *,
        device_id: str | None = None,
        logger: logging.Logger | None = None,
        token_resolver: _TokenResolver | Callable[[str], Mapping[str, object] | None] | None = None,
    ) -> None:
        capabilities = PlaybackCapabilities(
            gapless=True,
            hot_buffer=True,
            artwork_sync=True,
            max_bitrate_kbps=320,
            supports_offline=False,
            supports_lyrics=True,
        )
        super().__init__("spotify", "Spotify Web Playback", capabilities)
        self._logger = logger or get_logger("viola.playback.spotify")
        self._logger.debug("Initialising SpotifyWebPlaybackEngine")
        self._device_id = device_id or settings.spotify_device_id
        self._token_resolver = token_resolver
        self._token_info: dict[str, object] | None = None
        self._client: object | None = None
        self._handles: dict[str, PlaybackHandle] = {}
        self._lock = threading.Lock()
        self._spotipy: ModuleType | None = None
        self._oauth_cls: type | None = None

        try:
            _spotipy_module = importlib.import_module("spotipy")
            oauth2_module = importlib.import_module("spotipy.oauth2")
            oauth_cls_name = "SpotifyOAuth"
            _SpotifyOAuth = getattr(oauth2_module, oauth_cls_name)
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            self._mark_unavailable(f"spotipy not installed: {exc}")
            return

        self._spotipy = _spotipy_module
        self._oauth_cls = _SpotifyOAuth

    # ------------------------------------------------------------------#
    # Private helpers
    # ------------------------------------------------------------------#
    def _ensure_client(self):
        if not self.is_available():
            raise PlaybackError(self.availability_error() or "Spotify unavailable")

        with self._lock:
            if self._client is not None and self._token_info:
                expires_at = self._token_info.get("expires_at")
                if expires_at and expires_at - int(time.time()) > 60:
                    return self._client

            token_info = self._resolve_token()
            if not token_info:
                raise PlaybackError(
                    "Spotify credentials missing. Configure Batch B token vault or environment variables."
                )

            self._token_info = token_info
            if self._spotipy is not None:
                self._client = self._spotipy.Spotify(auth=token_info["access_token"])
                return self._client
            else:
                raise PlaybackError("Spotify client not available")

    def _resolve_token(self) -> dict[str, object] | None:
        if self._token_resolver:
            try:
                resolved = self._token_resolver("spotify")
                if resolved:
                    return dict(resolved)
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning("Credential resolver failed: %s", exc)

        client_id = settings.spotify_client_id
        client_secret = settings.spotify_client_secret
        refresh_token = settings.spotify_refresh_token
        redirect_uri = settings.spotify_redirect_uri or f"{get_runtime_base_url()}/callback"

        if not all([client_id, client_secret, refresh_token]) or self._oauth_cls is None:
            return None

        oauth = self._oauth_cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=self._SCOPES,
        )
        refreshed: dict[str, object] = oauth.refresh_access_token(refresh_token)
        refreshed["refresh_token"] = refresh_token
        return refreshed

    def _extract_track_id(self, query: str) -> str | None:
        if not query:
            return None
        match = _TRACK_URI_RE.search(query)
        if match:
            return match.group(2)
        if query.startswith("spotify:track:"):
            return query.split(":")[-1]
        return None

    def _poll_until_complete(self, handle: PlaybackHandle) -> None:
        try:
            client = self._ensure_client()
        except Exception as exc:
            handle.mark_finished(exc)
            return

        handle.mark_started()
        artwork_url = handle.item.artwork_url
        if artwork_url:
            handle.mark_artwork(artwork_url)

        last_progress = 0
        while not handle.stop_requested():
            try:
                playback = client.current_playback()
            except Exception as exc:  # pragma: no cover - network error
                self._logger.warning("Spotify current_playback failed: %s", exc)
                time.sleep(1.5)
                continue

            if not playback or playback.get("item") is None:
                break

            is_playing = playback.get("is_playing", False)
            progress_ms = playback.get("progress_ms", 0) or 0
            duration_ms = playback.get("item", {}).get("duration_ms", 0) or 0

            if duration_ms and progress_ms >= duration_ms:
                break

            if not is_playing and progress_ms == last_progress:
                # Track paused or stopped externally
                break

            last_progress = progress_ms
            time.sleep(0.8)

        handle.mark_finished()

    def _start_thread(self, target: Callable[[PlaybackHandle], None], handle: PlaybackHandle) -> None:
        thread = threading.Thread(
            target=target,
            args=(handle,),
            name=f"SpotifyPlayback-{handle.item.id}",
            daemon=True,
        )
        thread.start()

    # ------------------------------------------------------------------#
    # ProviderPlaybackEngine interface
    # ------------------------------------------------------------------#
    def resolve_track(self, query: str) -> QueueItem:
        track_id = self._extract_track_id(query)
        if track_id is None:
            raise PlaybackError(f"Unable to parse Spotify track identifier from '{query}'")

        client = self._ensure_client()
        track = client.track(track_id)
        if not track:
            raise PlaybackError(f"Spotify track not found: {track_id}")

        artwork_url = None
        images = track.get("album", {}).get("images") or []
        if images:
            # Select the largest image by width (Spotify provides 64, 300, 640)
            best = max(images, key=lambda img: img.get("width", 0) or img.get("height", 0) or 0)
            artwork_url = best.get("url") or images[0].get("url")

        artists = ", ".join(artist.get("name", "") for artist in track.get("artists", []))

        item = QueueItem(
            id=f"spotify-{track_id}",
            title=track.get("name", "Unknown Track"),
            url=f"spotify:track:{track_id}",
            source="spotify",
            video_id=None,
            artist=artists or "Unknown Artist",
            provider="spotify",
            stream_token=track_id,
            resolved_at=time.time(),
            artwork_url=artwork_url,
        )
        if artwork_url:
            item.capabilities["thumbnail_url"] = artwork_url
        item.set_capability("bitrate_kbps", 320)
        item.set_capability("lyrics", bool(track.get("available_markets")))
        return item

    def prefetch(self, upcoming: Iterable[QueueItem], *, queue_context: QueueContext) -> None:
        if not self.capabilities.hot_buffer:
            return
        try:
            client = self._ensure_client()
        except Exception as exc:
            self._logger.debug("Skipping Spotify prefetch: %s", exc)
            return

        for item in upcoming:
            track_id = item.stream_token or self._extract_track_id(item.url or "")
            if not track_id:
                continue
            try:
                client.add_to_queue(uri=f"spotify:track:{track_id}", device_id=self._device_id)
                self._logger.debug("Prefetched Spotify track %s", track_id)
            except Exception as exc:  # pragma: no cover - network / device issues
                self._logger.debug("Spotify add_to_queue failed: %s", exc)

    def play(
        self,
        item: QueueItem,
        *,
        queue_context: QueueContext,
        on_artwork=None,
    ) -> PlaybackHandle:
        client = self._ensure_client()
        track_id = item.stream_token or self._extract_track_id(item.url or "")
        if track_id is None:
            raise PlaybackError("Spotify QueueItem missing track identifier")

        handle = PlaybackHandle(item, self.provider_id)

        try:
            client.start_playback(
                device_id=self._device_id,
                uris=[f"spotify:track:{track_id}"],
            )
            self._logger.info("Spotify start_playback sent for %s", track_id)
        except Exception as exc:
            handle.mark_finished(exc)
            raise

        if item.artwork_url and on_artwork:
            on_artwork(item, item.artwork_url)

        with self._lock:
            self._handles[item.id] = handle

        self._start_thread(self._poll_until_complete, handle)
        return handle

    def pause(self, handle: PlaybackHandle) -> None:
        try:
            client = self._ensure_client()
            client.pause_playback(device_id=self._device_id)
            handle.mark_paused()
        except Exception as exc:  # pragma: no cover - network
            self._logger.warning("Spotify pause failed: %s", exc)

    def resume(self, handle: PlaybackHandle) -> None:
        try:
            client = self._ensure_client()
            client.start_playback(device_id=self._device_id)
            handle.mark_resumed()
        except Exception as exc:  # pragma: no cover - network
            self._logger.warning("Spotify resume failed: %s", exc)

    def stop(self, handle: PlaybackHandle) -> None:
        handle.request_stop()
        try:
            client = self._ensure_client()
            client.pause_playback(device_id=self._device_id)
        except Exception as exc:  # pragma: no cover
            self._logger.debug("Spotify stop failed: %s", exc)
        finally:
            with self._lock:
                self._handles.pop(handle.item.id, None)

    def set_volume(self, handle: PlaybackHandle, level: int) -> None:
        try:
            client = self._ensure_client()
            client.volume(level, device_id=self._device_id)
        except Exception as exc:  # pragma: no cover - network
            self._logger.debug("Spotify volume adjustment failed: %s", exc)

    def current_position(self, handle: PlaybackHandle) -> float:
        try:
            client = self._ensure_client()
            playback = client.current_playback()
            if playback and playback.get("progress_ms") is not None:
                return playback["progress_ms"] / 1000
        except Exception as exc:
            self._logger.debug("Spotify: current_position query failed: %r", exc)
            return 0.0
        return 0.0

    def duration(self, handle: PlaybackHandle) -> float:
        try:
            client = self._ensure_client()
            playback = client.current_playback()
            if playback and playback.get("item", {}).get("duration_ms") is not None:
                return playback["item"]["duration_ms"] / 1000
        except Exception as exc:
            self._logger.debug("Spotify: duration query failed: %r", exc)
            return 0.0
        return 0.0
