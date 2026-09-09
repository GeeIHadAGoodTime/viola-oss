from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from core.constants import TIMEOUT_GRACE
from core.logging_config import get_logger
from music.providers.youtube_core import YOUTUBE_VIDEO_ID_RE

logger = get_logger(__name__)

YOUTUBE_OEMBED_ENDPOINT = "https://www.youtube.com/oembed"
YOUTUBE_WATCH_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_AVAILABILITY_SOURCE = "youtube_oembed"

_UNPLAYABLE_OEMBED_STATUS_CODES = {400, 401, 403, 404, 410, 451}


@dataclass(frozen=True, slots=True)
class YouTubeVideoAvailability:
    video_id: str
    playable: bool
    embeddable: bool
    reason: str
    status_code: int | None = None
    title: str | None = None

    def as_track_extras(self) -> dict[str, str]:
        extras = {
            "availability_checked": "true",
            "availability_source": YOUTUBE_AVAILABILITY_SOURCE,
            "availability_playable": str(self.playable).lower(),
            "availability_embeddable": str(self.embeddable).lower(),
            "availability_reason": self.reason,
        }
        if self.status_code is not None:
            extras["availability_status_code"] = str(self.status_code)
        if self.title:
            extras["availability_title"] = self.title
        return extras


class YouTubeAvailabilityChecker(Protocol):
    def __call__(self, video_id: str) -> YouTubeVideoAvailability:
        """Return whether a YouTube video is safe to hand to the embedded player."""


def _unplayable(video_id: str, reason: str, *, status_code: int | None = None) -> YouTubeVideoAvailability:
    return YouTubeVideoAvailability(
        video_id=video_id,
        playable=False,
        embeddable=False,
        reason=reason,
        status_code=status_code,
    )


def check_youtube_video_playable(
    video_id: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = TIMEOUT_GRACE,
) -> YouTubeVideoAvailability:
    """
    Verify that a YouTube video can be embedded before resolving it for playback.

    The endpoint returns embed metadata only for videos that YouTube is willing
    to expose through its embed surface. Non-200 responses are treated as
    unplayable so deleted, private, restricted, or non-embeddable candidates are
    dropped before the player sees them.
    """
    if not YOUTUBE_VIDEO_ID_RE.match(video_id):
        return _unplayable(video_id, "invalid_video_id")

    params = {
        "url": YOUTUBE_WATCH_URL_TEMPLATE.format(video_id=video_id),
        "format": "json",
    }

    close_client = client is None
    active_client = client or httpx.Client(timeout=timeout)
    try:
        try:
            response = active_client.get(YOUTUBE_OEMBED_ENDPOINT, params=params)
        except httpx.TimeoutException:
            return _unplayable(video_id, "oembed_timeout")
        except httpx.RequestError as exc:
            logger.debug("YouTube availability check request failed for %s: %s", video_id, exc)
            return _unplayable(video_id, "oembed_request_failed")

        status_code = response.status_code
        if status_code != 200:
            reason = (
                "oembed_unplayable_status"
                if status_code in _UNPLAYABLE_OEMBED_STATUS_CODES
                else "oembed_unverified_status"
            )
            return _unplayable(video_id, reason, status_code=status_code)

        try:
            payload = response.json()
        except ValueError:
            return _unplayable(video_id, "oembed_invalid_json", status_code=status_code)

        html = payload.get("html") if isinstance(payload, dict) else None
        if not isinstance(html, str) or "<iframe" not in html:
            return _unplayable(video_id, "oembed_missing_embed_html", status_code=status_code)

        title = payload.get("title") if isinstance(payload, dict) else None
        return YouTubeVideoAvailability(
            video_id=video_id,
            playable=True,
            embeddable=True,
            reason="oembed_ok",
            status_code=status_code,
            title=title if isinstance(title, str) else None,
        )
    finally:
        if close_client:
            active_client.close()
