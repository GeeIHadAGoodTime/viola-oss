"""
Rating system client for music track ratings.
Handles thumbs up/down and rating status queries.
"""

from __future__ import annotations

from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

from .api_client_mixin import APIClientMixin

logger = get_logger(__name__)


class RatingClient(APIClientMixin):
    """Client for music rating operations."""

    def __init__(self, session):
        self.session = session
        self.base_url = None  # Set by parent client

    def thumbs_up(self, video_id: str, title: str, artist: str | None = None) -> bool:
        """Rate song with thumbs up."""
        return self._rate_song("up", video_id, title, artist)

    def thumbs_down(self, video_id: str, title: str, artist: str | None = None) -> bool:
        """Rate song with thumbs down."""
        return self._rate_song("down", video_id, title, artist)

    def get_rating_status(self, video_id: str) -> str | None:
        """Get rating status for a song (liked/disliked/None)."""
        try:
            response = self.session.get(
                f"{self.base_url}/v1/rating/status",
                params={"video_id": video_id},
                timeout=TIMEOUT_SHUTDOWN,
            )
            response.raise_for_status()
            payload = response.json()
            envelope = self._normalise_envelope(payload)
            if envelope is None or not envelope.get("ok", False):
                logger.debug(
                    "Rating status returned error: %r",
                    envelope.get("error") if envelope else payload,
                )
                return None
            data = envelope.get("data")
            if isinstance(data, dict):
                rating_info = data.get("rating")
                if isinstance(rating_info, dict):
                    rating_value = rating_info.get("rating")
                    # Handle both integer (1, -1) and string ("liked", "disliked") formats
                    if isinstance(rating_value, int):
                        if rating_value == 1:
                            return "liked"
                        elif rating_value == -1:
                            return "disliked"
                        return None
                    elif isinstance(rating_value, str):
                        return rating_value
                elif rating_info is None:
                    # No rating exists for this song
                    return None
            return None
        except Exception as e:
            logger.debug("Get rating status failed: %s", e)
            return None

    def remove_rating(self, video_id: str) -> bool:
        """Remove rating from a song (un-like or un-dislike)."""
        try:
            response = self.session.delete(
                f"{self.base_url}/v1/rating/{video_id}",
                timeout=TIMEOUT_SHUTDOWN,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error("Remove rating failed: %s", e)
            return False

    def _rate_song(self, rating: str, video_id: str, title: str, artist: str | None = None) -> bool:
        """Rate a song with the specified rating."""
        try:
            response = self.session.post(
                f"{self.base_url}/v1/rating/thumbs-{rating}",
                json={"video_id": video_id, "title": title, "artist": artist},
                timeout=TIMEOUT_SHUTDOWN,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error("Thumbs %s failed: %s", rating, e)
            return False


__all__ = ["RatingClient"]
