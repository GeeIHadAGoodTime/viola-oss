"""Name and cover of a Spotify track from Spotify's public oEmbed endpoint.

No token is needed and only the track id is sent. Used while the web player
page is still loading, so a bare ``spotify:track:<id>`` play shows a real title
and cover instead of an identifier. Every failure is silent: the caller keeps
the "title pending" state until the page answers.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_ENDPOINT = "https://open.spotify.com/oembed?url=https://open.spotify.com/track/"
_MAX_BYTES = 64 * 1024


def fetch_track_oembed(track_id: str, *, timeout: float = 3.0) -> dict[str, str]:
    """Return {"title", "artwork_url"} for a track id, or {} on any failure."""
    if not isinstance(track_id, str) or not _ID_RE.match(track_id):
        return {}
    try:
        request = urllib.request.Request(_ENDPOINT + track_id, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload: Any = json.loads(response.read(_MAX_BYTES).decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    found: dict[str, str] = {}
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        found["title"] = title.strip()
    thumbnail = payload.get("thumbnail_url")
    if isinstance(thumbnail, str) and thumbnail.startswith("https://"):
        found["artwork_url"] = thumbnail
    return found
