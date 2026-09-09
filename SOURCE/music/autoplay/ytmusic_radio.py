"""
YouTube Music Radio Autoplay using ytmusicapi.

Uses YouTube Music's actual radio feature via get_watch_playlist().
This returns curated recommendations, not search results.
"""

from __future__ import annotations

import asyncio

from core.logging_config import get_logger

logger = get_logger(__name__)


class YouTubeMusicRadio:
    """
    Autoplay using YouTube Music's radio feature.

    Uses ytmusicapi.get_watch_playlist() which returns the same
    curated mix you get when clicking "Start Radio" in YouTube Music.
    """

    def __init__(self, min_queue_size: int = 5):
        self._min_queue_size = min_queue_size
        self._is_fetching = False
        self._ytmusic = None
        self._recent_video_ids: set[str] = set()

    def _get_client(self):
        """Lazy-load ytmusicapi client."""
        logger.warning(
            "[QUEUE_TRACE] _get_client ENTER _ytmusic=%s _init_attempted=%s",
            self._ytmusic,
            getattr(self, "_init_attempted", "N/A"),
        )
        if self._ytmusic is None:
            try:
                from ytmusicapi import YTMusic

                # No auth needed for get_watch_playlist
                logger.warning("[QUEUE_TRACE] _get_client CREATING YTMusic")
                self._ytmusic = YTMusic()
                logger.warning("[QUEUE_TRACE] _get_client SUCCESS client=%s", self._ytmusic)
                logger.info("YouTubeMusicRadio: ytmusicapi client initialized")
            except ImportError:
                logger.warning("[QUEUE_TRACE] _get_client FAILED ImportError — ytmusicapi not installed")
                logger.error("YouTubeMusicRadio: ytmusicapi not installed. Run: pip install ytmusicapi")
            except Exception as e:
                logger.warning("[QUEUE_TRACE] _get_client FAILED exception=%s", e, exc_info=True)
                logger.exception("YouTubeMusicRadio: Failed to init ytmusicapi: %s", e)
        logger.warning("[QUEUE_TRACE] _get_client RETURN client_is_none=%s", self._ytmusic is None)
        return self._ytmusic

    @property
    def is_available(self) -> bool:
        """Check if ytmusicapi is available."""
        return self._get_client() is not None

    def should_refill(self, queue_size: int) -> bool:
        """Check if queue needs refilling."""
        return queue_size < self._min_queue_size and not self._is_fetching

    async def get_related_tracks(
        self,
        seed_video_id: str,
        limit: int = 10,
    ) -> list[dict]:
        """
        Get radio tracks using YouTube Music's get_watch_playlist.

        This returns the same curated mix you get when clicking
        "Start Radio" on a song in YouTube Music.

        Args:
            seed_video_id: Video ID to base radio on
            limit: Max tracks to return

        Returns:
            List of track dicts with video_id, title, artist
        """
        logger.warning(
            "[QUEUE_TRACE] radio.get_related_tracks ENTER seed=%s limit=%d",
            seed_video_id,
            limit,
        )
        client = self._get_client()
        if not client:
            logger.warning(
                "[QUEUE_TRACE] radio.get_related_tracks EXIT reason=no_client (_ytmusic is %s)",
                type(self._ytmusic).__name__ if self._ytmusic else "None",
            )
            logger.warning("YouTubeMusicRadio: Client not available")
            return []

        if self._is_fetching:
            logger.warning("[QUEUE_TRACE] radio.get_related_tracks EXIT reason=already_fetching")
            logger.debug("YouTubeMusicRadio: Already fetching, skipping")
            return []

        self._is_fetching = True
        try:
            logger.warning(
                "[QUEUE_TRACE] radio.get_related_tracks NETWORK_START seed=%s",
                seed_video_id,
            )

            # get_watch_playlist returns YouTube Music's actual radio
            # This is curated, not search results
            import time as _time

            _radio_t0 = _time.monotonic()
            playlist = await asyncio.to_thread(
                client.get_watch_playlist,
                seed_video_id,
                limit=limit + 10,  # Fetch extra to filter
            )
            _radio_elapsed = (_time.monotonic() - _radio_t0) * 1000
            logger.warning(
                "[QUEUE_TRACE] radio.get_related_tracks NETWORK_DONE elapsed_ms=%.1f playlist_keys=%s",
                _radio_elapsed,
                (list(playlist.keys()) if isinstance(playlist, dict) else type(playlist).__name__),
            )

            if not playlist:
                logger.warning("[QUEUE_TRACE] radio.get_related_tracks EXIT reason=playlist_none")
                logger.warning("YouTubeMusicRadio: get_watch_playlist returned None")
                return []

            tracks = playlist.get("tracks", [])
            if not tracks:
                logger.warning(
                    "[QUEUE_TRACE] radio.get_related_tracks EXIT reason=no_tracks_in_playlist keys=%s",
                    list(playlist.keys()),
                )
                logger.warning("YouTubeMusicRadio: No tracks in playlist")
                return []

            logger.warning(
                "[QUEUE_TRACE] radio.get_related_tracks RAW_TRACKS=%d recent_ids=%d",
                len(tracks),
                len(self._recent_video_ids),
            )
            result = []
            for track in tracks:
                video_id = track.get("videoId")

                # Skip seed track and recent tracks
                if not video_id:
                    continue
                if video_id == seed_video_id:
                    continue
                if video_id in self._recent_video_ids:
                    continue

                # Extract artist
                artists = track.get("artists", [])
                artist = artists[0].get("name", "Unknown") if artists else "Unknown"

                # Extract thumbnail
                thumbnails = track.get("thumbnail", [])
                thumbnail = thumbnails[-1].get("url") if thumbnails else None

                result.append(
                    {
                        "video_id": video_id,
                        "title": track.get("title", "Unknown"),
                        "artist": artist,
                        "thumbnail": thumbnail,
                        "duration_seconds": self._parse_duration(track.get("length")),
                    }
                )

                self._recent_video_ids.add(video_id)

                if len(result) >= limit:
                    break

            # Keep recent IDs bounded
            if len(self._recent_video_ids) > 100:
                self._recent_video_ids = set(list(self._recent_video_ids)[-50:])

            logger.info(
                "YouTubeMusicRadio: Got %d tracks from radio for %s",
                len(result),
                seed_video_id,
            )
            return result

        except Exception as e:
            logger.warning(
                "[QUEUE_TRACE] radio.get_related_tracks EXCEPTION type=%s msg=%s",
                type(e).__name__,
                e,
            )
            logger.exception("YouTubeMusicRadio: get_watch_playlist failed: %s", e)
            return []
        finally:
            self._is_fetching = False

    def _parse_duration(self, length_str: str | None) -> int:
        """Parse duration string like '3:45' to seconds."""
        if not length_str:
            return 0
        try:
            parts = length_str.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            pass
        return 0

    def clear_history(self) -> None:
        """Clear recent video IDs."""
        self._recent_video_ids.clear()


# Singleton
_radio_instance: YouTubeMusicRadio | None = None


def get_ytmusic_radio() -> YouTubeMusicRadio:
    """Get YouTube Music radio singleton."""
    global _radio_instance
    if _radio_instance is None:
        _radio_instance = YouTubeMusicRadio()
    return _radio_instance
