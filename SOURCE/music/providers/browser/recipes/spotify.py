"""
Spotify Web Player search-and-play recipe.

Provides JavaScript recipes for automating Spotify via browser
injection: searching, clicking the first track result, detecting playback,
and checking login / session state.

Selectors target the Spotify Web Player SPA as of early 2026.  Each method
uses a tiered fallback strategy (primary -> secondary -> generic) so that
minor DOM changes do not immediately break automation.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from core.logging_config import get_logger

from .base import ProviderRecipe

logger = get_logger(__name__)

# Spotify content path: /track/ID, /album/ID, /playlist/ID
_SPOTIFY_CONTENT_RE = re.compile(r"^/(?:track|album|playlist|episode|show)/([a-zA-Z0-9]+)")


class SpotifyRecipe(ProviderRecipe):
    """Recipe for Spotify Web Player (open.spotify.com)."""

    # -- Provider identity ---------------------------------------------------

    @property
    def name(self) -> str:
        return "spotify"

    @property
    def display_name(self) -> str:
        return "Spotify"

    @property
    def search_url_template(self) -> str:
        return "https://open.spotify.com/search/{query}"

    @property
    def home_url(self) -> str:
        return "https://open.spotify.com"

    # -- JS recipes ----------------------------------------------------------

    def get_play_first_result_js(self) -> str:
        """JS to find and click the first track result on Spotify.

        Strategy tiers:
          1. Primary -- ``[data-testid="tracklist-row"]`` inside search
             results.  Click the play button within the row first, then
             fall back to clicking the row itself.
          2. Secondary -- ``[data-testid="track-row"]`` or rows within a
             ``[data-testid="search-tracks-result"]`` container.
          3. Tertiary -- any clickable element inside a
             ``[role="row"]`` or ``[role="listitem"]`` in the search
             result area.
          4. Last resort -- trigger play via the Media Session API or
             ``<audio>`` element.

        Returns:
            Self-contained JS IIFE that returns a JSON string.
        """
        return """
(function() {
    try {
        // --- helpers ---------------------------------------------------------
        function clickEl(el) {
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
            el.click();
            return true;
        }

        // --- Strategy 1: tracklist-row test ID rows --------------------------
        var rows = document.querySelectorAll(
            '[data-testid="tracklist-row"]'
        );
        if (rows.length > 0) {
            var row = rows[0];
            // Try the play button within the row first
            var playBtn = row.querySelector(
                '[data-testid="play-button"], ' +
                'button[aria-label="Play"], ' +
                'button[data-testid="play-button-icon"]'
            );
            if (clickEl(playBtn)) {
                return JSON.stringify({
                    success: true,
                    message: 'Clicked play button in tracklist row',
                    strategy: 'tracklist_row_play_button'
                });
            }
            // Fall back to clicking the row itself
            var link = row.querySelector('a') || row;
            if (clickEl(link)) {
                return JSON.stringify({
                    success: true,
                    message: 'Clicked first tracklist row',
                    strategy: 'tracklist_row_click'
                });
            }
        }

        // --- Strategy 2: track-row or search-tracks-result container ---------
        var trackRows = document.querySelectorAll(
            '[data-testid="track-row"], ' +
            '[data-testid="search-tracks-result"] [role="row"]'
        );
        if (trackRows.length > 0) {
            var trackRow = trackRows[0];
            var playBtn2 = trackRow.querySelector(
                'button[aria-label="Play"], ' +
                '[data-testid="play-button"]'
            );
            if (clickEl(playBtn2)) {
                return JSON.stringify({
                    success: true,
                    message: 'Clicked play button in track row',
                    strategy: 'track_row_play_button'
                });
            }
            var link2 = trackRow.querySelector('a') || trackRow;
            if (clickEl(link2)) {
                return JSON.stringify({
                    success: true,
                    message: 'Clicked first track row',
                    strategy: 'track_row_click'
                });
            }
        }

        // --- Strategy 3: role-based row in search results --------------------
        var searchSection = document.querySelector(
            '[data-testid="search-category-card-0"], ' +
            'section[aria-label*="Song"], ' +
            'section[aria-label*="Track"], ' +
            '[data-testid="component-shelf"]'
        );
        if (searchSection) {
            var rowEl = searchSection.querySelector(
                '[role="row"], [role="listitem"]'
            );
            if (rowEl) {
                var playBtn3 = rowEl.querySelector(
                    'button[aria-label="Play"]'
                );
                if (clickEl(playBtn3)) {
                    return JSON.stringify({
                        success: true,
                        message: 'Clicked play button in search section',
                        strategy: 'search_section_play_button'
                    });
                }
                var link3 = rowEl.querySelector('a') || rowEl;
                if (clickEl(link3)) {
                    return JSON.stringify({
                        success: true,
                        message: 'Clicked first item in search section',
                        strategy: 'search_section_item'
                    });
                }
            }
        }

        // --- Strategy 4: Media element / Media Session play ------------------
        var media = document.querySelector('audio') || document.querySelector('video');
        if (media) {
            media.play();
            return JSON.stringify({
                success: true,
                message: 'Called play() on media element',
                strategy: 'media_element_play'
            });
        }

        return JSON.stringify({
            success: false,
            message: 'No playable results found on page'
        });
    } catch(e) {
        return JSON.stringify({
            success: false,
            message: 'JS error: ' + e.message
        });
    }
})();
""".strip()

    def get_detect_playback_started_js(self) -> str:
        """JS to detect whether playback has started on Spotify.

        Checks (in order):
          1. ``navigator.mediaSession.playbackState``
          2. Any ``<audio>`` element that is not paused and has
             ``currentTime > 0``.
          3. Spotify's now-playing widget or playback bar active state.

        Returns:
            JS IIFE returning JSON: ``{playing: bool}``.
        """
        return """
(function() {
    try {
        // Strategy 1: W3C Media Session API
        if (navigator.mediaSession && navigator.mediaSession.playbackState === 'playing') {
            return JSON.stringify({playing: true});
        }

        // Strategy 2: Active media elements
        var mediaEls = document.querySelectorAll('audio, video');
        for (var i = 0; i < mediaEls.length; i++) {
            var el = mediaEls[i];
            if (!el.paused && el.currentTime > 0 && !el.ended) {
                return JSON.stringify({playing: true});
            }
        }

        // Strategy 3: Spotify now-playing widget / playback bar
        var nowPlaying = document.querySelector(
            '[data-testid="now-playing-widget"], ' +
            '[data-testid="now-playing-bar"], ' +
            '[data-testid="player-controls"]'
        );
        if (nowPlaying) {
            // If we see a pause button, playback is active
            var pauseBtn = nowPlaying.querySelector(
                'button[aria-label="Pause"], ' +
                '[data-testid="control-button-pause"]'
            );
            if (pauseBtn) {
                return JSON.stringify({playing: true});
            }
        }

        // Strategy 4: Check the playback bar footer area
        var footer = document.querySelector(
            '.Root__now-playing-bar, footer[data-testid="now-playing-bar"]'
        );
        if (footer) {
            var pauseBtn2 = footer.querySelector(
                'button[aria-label="Pause"]'
            );
            if (pauseBtn2) {
                return JSON.stringify({playing: true});
            }
        }

        return JSON.stringify({playing: false});
    } catch(e) {
        return JSON.stringify({playing: false});
    }
})();
""".strip()

    def get_login_detection_js(self) -> str:
        """JS to detect whether the user is logged in to Spotify.

        Checks for:
          1. User widget or avatar in the top bar
             (``[data-testid="user-widget"]``).
          2. Presence of ``Root__top-bar`` with user-specific elements.
          3. Login/signup buttons indicating user is NOT logged in.

        Returns:
            JS IIFE returning JSON: ``{logged_in: bool}``.
        """
        return """
(function() {
    try {
        // Strategy 1: User widget / avatar in the top bar
        var userWidget = document.querySelector(
            '[data-testid="user-widget"], ' +
            '[data-testid="user-widget-link"], ' +
            'button[data-testid="user-widget-link"]'
        );
        if (userWidget) {
            return JSON.stringify({logged_in: true});
        }

        // Strategy 2: Top bar with user-specific content
        var topBar = document.querySelector('.Root__top-bar');
        if (topBar) {
            var avatar = topBar.querySelector(
                'img[alt*="avatar" i], ' +
                'figure img, ' +
                '[data-testid="user-widget"] img'
            );
            if (avatar) {
                return JSON.stringify({logged_in: true});
            }
        }

        // Strategy 3: Check for profile menu button
        var profileBtn = document.querySelector(
            'button[aria-label*="Profile" i], ' +
            'button[aria-label*="Account" i]'
        );
        if (profileBtn) {
            return JSON.stringify({logged_in: true});
        }

        // Strategy 4: Login / signup buttons mean NOT logged in
        var loginBtn = document.querySelector(
            '[data-testid="login-button"], ' +
            '[data-testid="signup-button"], ' +
            'button[data-testid="login-button"], ' +
            'a[href*="login"], ' +
            'a[href*="signup"]'
        );
        if (loginBtn) {
            return JSON.stringify({logged_in: false});
        }

        // Unable to determine -- assume not logged in
        return JSON.stringify({logged_in: false});
    } catch(e) {
        return JSON.stringify({logged_in: false});
    }
})();
""".strip()

    def get_session_expired_js(self) -> str:
        """JS to detect session expiry on Spotify.

        Checks for:
          1. Redirect to Spotify login page (``accounts.spotify.com``).
          2. "Your session has expired" modal or overlay.
          3. Text-based detection of expiry messages on the page.

        Returns:
            JS IIFE returning JSON: ``{expired: bool}``.
        """
        return """
(function() {
    try {
        // Strategy 1: URL-based redirect detection
        var url = window.location.href;
        if (url.indexOf('accounts.spotify.com') !== -1) {
            return JSON.stringify({expired: true});
        }
        if (url.indexOf('/login') !== -1 || url.indexOf('/signin') !== -1) {
            return JSON.stringify({expired: true});
        }

        // Strategy 2: Session expired modal / overlay
        var expiredModal = document.querySelector(
            '[data-testid="session-expired-modal"], ' +
            '[data-testid="error-dialog"], ' +
            '.ReactModal__Content'
        );
        if (expiredModal) {
            var modalText = expiredModal.innerText || '';
            if (modalText.toLowerCase().indexOf('session') !== -1 ||
                modalText.toLowerCase().indexOf('expired') !== -1 ||
                modalText.toLowerCase().indexOf('log in') !== -1) {
                return JSON.stringify({expired: true});
            }
        }

        // Strategy 3: Text-based detection of session expiry
        var body = document.body ? document.body.innerText : '';
        var expiryPhrases = [
            'your session has expired',
            'session expired',
            'please log in again',
            'you have been logged out'
        ];
        var bodyLower = body.toLowerCase();
        for (var i = 0; i < expiryPhrases.length; i++) {
            if (bodyLower.indexOf(expiryPhrases[i]) !== -1) {
                // Only flag if page also lacks the normal player bar
                var playerBar = document.querySelector(
                    '[data-testid="now-playing-bar"], .Root__now-playing-bar'
                );
                if (!playerBar) {
                    return JSON.stringify({expired: true});
                }
            }
        }

        return JSON.stringify({expired: false});
    } catch(e) {
        // On error, conservatively assume not expired
        return JSON.stringify({expired: false});
    }
})();
""".strip()

    def get_provider_icon_url(self) -> str | None:
        """Return Spotify icon URL."""
        return "https://open.spotify.com/favicon.ico"

    # -- Embed support -------------------------------------------------------

    def has_embed_support(self) -> bool:
        return True

    def get_content_id_from_url(self, url: str) -> str | None:
        """Extract Spotify track/album/playlist ID from a content page URL."""
        parsed = urlparse(url)
        match = _SPOTIFY_CONTENT_RE.match(parsed.path)
        if match:
            return match.group(1)
        return None

    def get_watch_url_pattern(self) -> str:
        return r"open\.spotify\.com/(track|album|playlist)/"
