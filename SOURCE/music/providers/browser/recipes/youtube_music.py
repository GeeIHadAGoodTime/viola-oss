"""
YouTube search-and-extract recipe.

Provides JavaScript recipes for searching youtube.com and extracting
video IDs from search results.  The hidden browser is a SEARCH ENGINE —
it never plays audio.  Playback happens via the YouTubeEmbed iframe in
SmartDisplay.

Selectors target youtube.com search results as of early 2026.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from core.logging_config import get_logger

from .base import ProviderRecipe

logger = get_logger(__name__)

# 11-char YouTube video ID pattern
_YT_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


class YouTubeMusicRecipe(ProviderRecipe):
    """Recipe for YouTube (youtube.com) — search-only, extract video IDs.

    The hidden browser navigates to youtube.com search results and extracts
    video IDs from the page.  It never plays audio.  Playback is handled
    by the YouTubeEmbed iframe in SmartDisplay.
    """

    # -- Provider identity ---------------------------------------------------

    @property
    def name(self) -> str:
        return "youtube_music"

    @property
    def display_name(self) -> str:
        return "YouTube"

    @property
    def search_url_template(self) -> str:
        return "https://www.youtube.com/results?search_query={query}"

    @property
    def home_url(self) -> str:
        return "https://www.youtube.com"

    # -- JS recipes ----------------------------------------------------------

    def get_extract_video_ids_js(self) -> str:
        """JS to extract video IDs from youtube.com search results.

        Finds all ``<a>`` elements with ``href`` containing ``/watch?v=``,
        excludes shorts/playlists/channels, deduplicates, and returns up
        to 10 unique 11-character video IDs.

        Returns:
            Self-contained JS IIFE returning JSON:
            ``{video_ids: [...], count: N}`` or
            ``{video_ids: [], count: 0, error: "..."}``
        """
        return """
(function() {
    try {
        var links = document.querySelectorAll('a[href*="/watch?v="]');
        var seen = {};
        var ids = [];
        for (var i = 0; i < links.length && ids.length < 10; i++) {
            var href = links[i].getAttribute('href') || '';
            if (href.indexOf('/shorts/') !== -1) continue;
            var match = href.match(/[?&]v=([\\w-]{11})/);
            if (match && !seen[match[1]]) {
                seen[match[1]] = true;
                ids.push(match[1]);
            }
        }
        return JSON.stringify({video_ids: ids, count: ids.length});
    } catch(e) {
        return JSON.stringify({video_ids: [], count: 0, error: e.message});
    }
})();
""".strip()

    def get_play_first_result_js(self) -> str:
        """No-op: the hidden browser never plays audio.

        Search-only mode — video_id extraction is handled by
        :meth:`get_extract_video_ids_js`.  Playback happens in the
        YouTubeEmbed iframe in SmartDisplay.

        Returns:
            JSON indicating search-only mode.
        """
        return '(function(){return JSON.stringify({success:false,message:"Search-only mode"});})();'

    def get_detect_playback_started_js(self) -> str:
        """No-op: the hidden browser never plays audio.

        Returns:
            JSON indicating not playing (search-only mode).
        """
        return "(function(){return JSON.stringify({playing:false});})();"

    def get_login_detection_js(self) -> str:
        """JS to detect whether the user is logged in to YouTube.

        Checks for:
          1. Avatar button (``#avatar-btn`` or ``img`` inside the
             account/avatar container).
          2. ``ytcfg`` global containing sign-in state.
          3. Sign-in button presence (means NOT logged in).

        Returns:
            JS IIFE returning JSON: ``{logged_in: bool}``.
        """
        return """
(function() {
    try {
        // Strategy 1: Avatar button / image in the top-right account area
        var avatar = document.querySelector(
            '#avatar-btn img, ' +
            'button#avatar-btn img, ' +
            'button[aria-label="Account"] img, ' +
            'img.yt-spec-avatar-shape__avatar'
        );
        if (avatar) {
            return JSON.stringify({logged_in: true});
        }

        // Strategy 2: Check ytcfg for sign-in data (YouTube internal config)
        if (typeof ytcfg !== 'undefined' && ytcfg.get) {
            var sessionIndex = ytcfg.get('SESSION_INDEX');
            if (sessionIndex !== undefined && sessionIndex !== null && sessionIndex !== '') {
                return JSON.stringify({logged_in: true});
            }
            var loggedIn = ytcfg.get('LOGGED_IN');
            if (loggedIn === true) {
                return JSON.stringify({logged_in: true});
            }
        }

        // Strategy 3: Sign-in button present means NOT logged in
        var signInBtn = document.querySelector(
            'a[href*="accounts.google.com/ServiceLogin"], ' +
            'a[aria-label="Sign in"], ' +
            'ytd-button-renderer a[href*="accounts.google.com"]'
        );
        if (signInBtn) {
            return JSON.stringify({logged_in: false});
        }

        return JSON.stringify({logged_in: false});
    } catch(e) {
        return JSON.stringify({logged_in: false});
    }
})();
""".strip()

    def get_session_expired_js(self) -> str:
        """JS to detect session expiry on YouTube.

        Checks for:
          1. Redirect to ``accounts.google.com`` (current URL changed).
          2. Login prompt overlay or consent screen.

        Returns:
            JS IIFE returning JSON: ``{expired: bool}``.
        """
        return """
(function() {
    try {
        var url = window.location.href;
        if (url.indexOf('accounts.google.com') !== -1) {
            return JSON.stringify({expired: true});
        }
        if (url.indexOf('consent.google.com') !== -1) {
            return JSON.stringify({expired: true});
        }

        var loginOverlay = document.querySelector(
            'yt-upsell-dialog-renderer, ' +
            '#consent-bump, ' +
            '[data-testid="upsell-dialog"]'
        );
        if (loginOverlay) {
            return JSON.stringify({expired: true});
        }

        return JSON.stringify({expired: false});
    } catch(e) {
        return JSON.stringify({expired: false});
    }
})();
""".strip()

    def get_provider_icon_url(self) -> str | None:
        """Return YouTube icon URL."""
        return "https://www.youtube.com/s/desktop/favicon_144x144.png"

    # -- Embed support -------------------------------------------------------

    def has_embed_support(self) -> bool:
        return True

    def get_content_id_from_url(self, url: str) -> str | None:
        """Extract YouTube video ID from a watch page URL.

        Returns ``None`` for search pages, browse pages, etc.
        """
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        video_ids = params.get("v", [])
        if video_ids and _YT_VIDEO_ID_RE.match(video_ids[0]):
            return video_ids[0]
        return None

    def get_watch_url_pattern(self) -> str:
        return r"youtube\.com/watch\?v="
