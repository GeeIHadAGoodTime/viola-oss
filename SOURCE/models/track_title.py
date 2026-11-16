"""The one test for "this text is an identifier, not a song title".

A queue item can briefly hold a Spotify URI or track URL where a title will
go. Everything that shows or says a title (state broadcast, queue list,
spoken reply) goes through this helper, so an id never reaches the user.
The React app has the same test in ``src/utils/trackTitle.js``.
"""

from __future__ import annotations

from typing import Any

# capabilities key set on an item whose title has not arrived yet
TITLE_PENDING_KEY = "title_pending"


def looks_like_track_id(value: Any) -> bool:
    """True for a Spotify URI or track link, which is never a song title."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip().lower()
    return cleaned.startswith("spotify:") or "open.spotify.com/" in cleaned


def display_title(value: Any) -> str:
    """The title to show or say: the text itself, or "" when it is an id."""
    if not isinstance(value, str) or looks_like_track_id(value):
        return ""
    return value
