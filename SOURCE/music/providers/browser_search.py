"""HTTP-based YouTube search and playlist resolution.

Fetches YouTube search results and playlist pages directly via HTTP and
extracts video metadata from the ``ytInitialData`` JSON blob embedded in
static HTML. No browser rendering required.

Per MROOM-16 (2026-05-15), browser search is the sole YouTube text-search and
playlist-resolution path; the YouTube Data API/OAuth dependency was removed.
This module is the implementation of that decision. It does NOT use a
QWebEngineView -- an earlier 2026-03-04 design did, which created a parentless
top-level Qt widget (offscreen "ghost" window) and an in-process Chromium
target advertised on the shared CDP devtools port. Both were eliminated when
HTTP+ytInitialData scraping replaced the rendered fallback (HTTP path itself
landed 2026-04-27 as the primary; rendered fallback retired here).

YouTube playlist pages embed up to ~100-200 entries in the initial HTML's
``ytInitialData``. Longer playlists use continuation tokens that are NOT
followed -- this matches the prior Qt implementation's behavior (it didn't
follow continuations either) and keeps scraping bounded to one HTTP round trip.

Threading model
---------------
``search()`` and ``resolve_playlist()`` are synchronous and thread-safe; both
issue one ``urllib.request.urlopen`` call (no shared mutable state).
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from collections import deque
from typing import Any
from urllib.parse import quote_plus

from core.logging_config import get_logger

logger = get_logger("viola.music.providers.browser_search")

# Regex for 11-character YouTube video IDs
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_YT_INITIAL_DATA_MARKERS = (
    "var ytInitialData = ",
    "window.ytInitialData = ",
    "ytInitialData = ",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0 Safari/537.36"
)
_REQUEST_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

# Cap the HTML body we read so a hostile/oversized response can't OOM us.
_MAX_HTML_BYTES = 5_000_000


def _text_from_runs(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    simple_text = value.get("simpleText")
    if isinstance(simple_text, str):
        return simple_text.strip()
    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    parts = [str(run.get("text") or "") for run in runs if isinstance(run, dict)]
    return "".join(parts).strip()


def _last_thumbnail_url(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    thumbs = value.get("thumbnails")
    if not isinstance(thumbs, list) or not thumbs:
        return ""
    last = thumbs[-1]
    if isinstance(last, dict):
        url = last.get("url")
        if isinstance(url, str):
            return url
    return ""


def _decode_yt_initial_data(html_text: str) -> Any:
    """Find and JSON-decode the first ``ytInitialData`` blob in the HTML."""
    decoder = json.JSONDecoder()
    for marker in _YT_INITIAL_DATA_MARKERS:
        idx = html_text.find(marker)
        if idx == -1:
            continue
        start = idx + len(marker)
        try:
            data, _ = decoder.raw_decode(html_text[start:])
            return data
        except json.JSONDecodeError:
            continue
    return None


def _walk_for_renderer(data: Any, renderer_key: str, limit: int) -> list[dict[str, Any]]:
    """Walk a ytInitialData tree collecting all values under ``renderer_key``.

    Bounded breadth-first walk so results are returned in document order; bails
    out at ``limit`` matches or after visiting ``max_visited`` nodes to prevent
    pathological pages from spinning.
    """
    found: list[dict[str, Any]] = []
    queue: deque[Any] = deque([data])
    visited = 0
    max_visited = 100_000

    while queue and len(found) < limit and visited < max_visited:
        visited += 1
        obj = queue.popleft()
        if isinstance(obj, dict):
            renderer = obj.get(renderer_key)
            if isinstance(renderer, dict):
                found.append(renderer)
                if len(found) >= limit:
                    break
            queue.extend(obj.values())
        elif isinstance(obj, list):
            queue.extend(obj)

    return found


def _renderer_to_entry(renderer: dict[str, Any]) -> dict[str, str] | None:
    """Convert a videoRenderer / playlistVideoRenderer dict to our entry shape."""
    video_id = renderer.get("videoId")
    if not (isinstance(video_id, str) and _VIDEO_ID_RE.match(video_id)):
        return None
    channel = _text_from_runs(renderer.get("ownerText")) or _text_from_runs(renderer.get("shortBylineText"))
    return {
        "video_id": video_id,
        "title": _text_from_runs(renderer.get("title")),
        "channel": channel,
        "duration_text": _text_from_runs(renderer.get("lengthText")),
        "thumbnail_url": _last_thumbnail_url(renderer.get("thumbnail")),
    }


def _extract_search_results_from_html(html_text: str, limit: int) -> list[dict[str, str]]:
    data = _decode_yt_initial_data(html_text)
    if data is None:
        return []

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for renderer_key in ("videoRenderer", "compactVideoRenderer"):
        for renderer in _walk_for_renderer(data, renderer_key, limit * 2):
            entry = _renderer_to_entry(renderer)
            if entry is None:
                continue
            vid = entry["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            results.append(entry)
            if len(results) >= limit:
                return results
    return results


def _extract_playlist_entries_from_html(html_text: str, limit: int) -> list[dict[str, str]]:
    data = _decode_yt_initial_data(html_text)
    if data is None:
        return []

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for renderer in _walk_for_renderer(data, "playlistVideoRenderer", limit * 2):
        entry = _renderer_to_entry(renderer)
        if entry is None:
            continue
        vid = entry["video_id"]
        if vid in seen:
            continue
        seen.add(vid)
        results.append(entry)
        if len(results) >= limit:
            break
    return results


def _fetch_html(url: str, *, timeout: float) -> str | None:
    """One bounded HTTP GET returning decoded HTML, or None on failure."""
    request = urllib.request.Request(url, headers=_REQUEST_HEADERS)
    http_timeout = max(2.0, min(timeout, 6.0))
    try:
        with urllib.request.urlopen(request, timeout=http_timeout) as response:  # nosec B310 - youtube.com only
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read(_MAX_HTML_BYTES).decode(charset, errors="replace")
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        logger.warning("BrowserSearch: HTTP fetch failed url=%s error=%s", url, exc)
        return None


def _get_settings() -> Any:
    """Lazy import to avoid circular deps at module level."""
    try:
        from config.settings import get_settings

        return get_settings()
    except (ImportError, AttributeError, RuntimeError) as exc:
        logger.debug("BrowserSearch: settings unavailable: %s", exc)
        return None


def _get_timeout() -> float:
    cfg = _get_settings()
    if cfg is None:
        return 10.0
    return float(getattr(cfg, "browser_search_timeout_seconds", 10.0))


class BrowserSearchEngine:
    """Singleton engine for HTTP-based YouTube search and playlist resolution.

    Usage::

        engine = BrowserSearchEngine.get_instance()
        results = engine.search("Drake Nokia", limit=10)
        playlist = engine.resolve_playlist("PLxxxxxx", limit=200)

    Both methods issue a single HTTP GET to youtube.com and parse the
    ``ytInitialData`` JSON blob from the response. No browser rendering;
    no Qt webview; no top-level Windows HWND; no in-process Chromium target.
    """

    _instance: BrowserSearchEngine | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> BrowserSearchEngine:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 25) -> list[dict[str, str]]:
        """Search YouTube for ``query`` and return video metadata.

        Returns a list of dicts with keys ``video_id``, ``title``, ``channel``,
        ``duration_text``, ``thumbnail_url``. Empty list if YouTube returns no
        parseable ``ytInitialData`` (rare; users see "no results" and can
        retry or fall through to another provider per the selection cascade
        in ``music/providers/selection.py``).
        """
        if not query or not query.strip():
            return []

        encoded_query = quote_plus(query)
        url = f"https://www.youtube.com/results?search_query={encoded_query}"
        timeout = _get_timeout()

        html_text = _fetch_html(url, timeout=timeout)
        if html_text is None:
            return []

        results = _extract_search_results_from_html(html_text, limit)
        if results:
            logger.info(
                "BrowserSearch: query=%r results=%d first_id=%s",
                query,
                len(results),
                results[0].get("video_id"),
            )
        else:
            logger.warning("BrowserSearch: query=%r returned no parseable results", query)
        return results

    def resolve_playlist(self, playlist_id: str, limit: int = 200) -> list[dict[str, str]]:
        """Resolve a YouTube playlist and return up to ``limit`` video entries.

        Pagination note: YouTube embeds ~100-200 entries in the initial
        ``ytInitialData`` blob. Longer playlists use continuation tokens that
        are NOT followed. The first batch is returned, matching the prior
        Qt-rendered implementation's behavior.
        """
        if not playlist_id or not playlist_id.strip():
            return []

        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        timeout = _get_timeout()

        html_text = _fetch_html(url, timeout=timeout)
        if html_text is None:
            return []

        entries = _extract_playlist_entries_from_html(html_text, limit)
        if entries:
            logger.info(
                "BrowserSearch: playlist_id=%s entries=%d first_id=%s",
                playlist_id,
                len(entries),
                entries[0].get("video_id"),
            )
        else:
            logger.warning(
                "BrowserSearch: playlist_id=%s returned no parseable entries",
                playlist_id,
            )
        return entries
