"""Last known good title, artist and cover for Spotify tracks.

A direct-URI play (spotify:track:<id>) seeds the queue item with the raw URI
as its title, and the real name arrives some seconds later from the Spotify
page. Until then the stage would show the id with no cover. This remembers
what the page said for each track id so a repeat play starts with the real
name and cover, and gives one test for "this text is an id, not a title".
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

_MAX_ENTRIES = 256
_lock = threading.Lock()
_known: OrderedDict[str, dict[str, str]] = OrderedDict()


def looks_like_track_id(value: Any) -> bool:
    """True for a Spotify URI or track URL, which is never a song title."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip().lower()
    return cleaned.startswith("spotify:") or "open.spotify.com/" in cleaned


def track_id_from_item(item: Any) -> str | None:
    """The Spotify track id a queue item plays, from its uri or url."""
    caps = getattr(item, "capabilities", None)
    candidates: list[Any] = []
    if isinstance(caps, dict):
        candidates.extend([caps.get("uri"), caps.get("track_uri")])
    candidates.extend([getattr(item, "url", None), getattr(item, "title", None)])
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if text.startswith("spotify:track:"):
            return text[len("spotify:track:") :].strip() or None
        if "open.spotify.com/track/" in text:
            return text.split("open.spotify.com/track/", 1)[1].split("?", 1)[0].split("/", 1)[0].strip() or None
    return None


def remember(track_id: str | None, *, title: Any = None, artist: Any = None, artwork_url: Any = None) -> None:
    """Keep the good values; an id-like title or an empty value never replaces one."""
    if not track_id:
        return
    with _lock:
        entry = _known.pop(track_id, {})
        if isinstance(title, str) and title.strip() and not looks_like_track_id(title):
            entry["title"] = title.strip()
        if isinstance(artist, str) and artist.strip() and not looks_like_track_id(artist):
            entry["artist"] = artist.strip()
        if isinstance(artwork_url, str) and artwork_url.strip():
            entry["artwork_url"] = artwork_url.strip()
        if entry:
            _known[track_id] = entry
            while len(_known) > _MAX_ENTRIES:
                _known.popitem(last=False)


def lookup(track_id: str | None) -> dict[str, str]:
    if not track_id:
        return {}
    with _lock:
        return dict(_known.get(track_id, {}))


def clear() -> None:
    with _lock:
        _known.clear()
