"""
music.providers.browser.error_handler
--------------------------------------

Centralized error handler for browser-native playback issues.

Handles ad detection, playback stalls, navigation failures, auth expiry,
content blocking, and recipe injection failures.  Each error type has a
recovery strategy that returns a :class:`RecoveryAction` describing what
the caller should do next.

Usage::

    handler = BrowserErrorHandler(controller, auth_manager)
    if handler.detect_ad():
        handler.handle_error(BrowserErrorHandler.AD_DETECTED, {})
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from core.constants import TIMEOUT_LONG, TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

if TYPE_CHECKING:
    from music.providers.browser.auth_manager import BrowserAuthManager
    from music.providers.browser.provider import BrowserPlaybackController

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ErrorType(StrEnum):
    """Categorized error types for browser-native playback."""

    NAVIGATION_ERROR = "navigation_error"
    PLAYBACK_STALL = "playback_stall"
    AD_DETECTED = "ad_detected"
    AUTH_EXPIRED = "auth_expired"
    RECIPE_FAILURE = "recipe_failure"
    CONTENT_BLOCKED = "content_blocked"


class RecoveryStrategy(StrEnum):
    """Actions the caller should take after error handling."""

    RETRY = "retry"
    SKIP = "skip"
    REFRESH = "refresh"
    LOGIN = "login"
    WAIT = "wait"
    ABORT = "abort"
    NONE = "none"


@dataclass
class RecoveryAction:
    """Describes the recovery action to take after an error.

    Attributes:
        strategy: What the caller should do.
        recovered: ``True`` if the handler already resolved the issue.
        message: Human-readable description for logging or UI.
        delay_seconds: How long to wait before executing the strategy.
        context: Additional context dict for the caller.
    """

    strategy: RecoveryStrategy = RecoveryStrategy.NONE
    recovered: bool = False
    message: str = ""
    delay_seconds: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ad detection JavaScript
# ---------------------------------------------------------------------------

# CSS selectors indicating an ad is playing on common music services.
# Each tuple is (site_hint, selector).
_AD_DETECTION_SELECTORS: list[tuple[str, str]] = [
    # YouTube / YouTube Music
    ("youtube", ".ad-showing"),
    ("youtube", ".ytp-ad-player-overlay"),
    ("youtube", ".ytp-ad-player-overlay-instream-info"),
    ("youtube", ".ytp-ad-text"),
    # Spotify web player
    ("spotify", ".ad-break"),
    ("spotify", '[data-testid="ad-content"]'),
]

# CSS selectors for skip-ad buttons.
_AD_SKIP_SELECTORS: list[str] = [
    # YouTube skip ad buttons (multiple variants over time)
    ".ytp-ad-skip-button",
    ".ytp-ad-skip-button-modern",
    ".ytp-skip-ad-button",
    'button[class*="skip-ad"]',
    # Generic "skip" aria label
    '[aria-label*="Skip" i]',
]


def _js_detect_ad() -> str:
    """Return JS that checks whether an ad is currently playing.

    Returns a JSON object:
    ``{ "ad_detected": bool, "source": str, "skippable": bool }``
    """
    selectors_js = ", ".join("'%s'" % sel for (_, sel) in _AD_DETECTION_SELECTORS)
    skip_selectors_js = ", ".join("'%s'" % sel for sel in _AD_SKIP_SELECTORS)
    return """
(function() {
    try {
        var adSelectors = [%s];
        var skipSelectors = [%s];
        var adFound = false;
        var source = '';

        for (var i = 0; i < adSelectors.length; i++) {
            if (document.querySelector(adSelectors[i])) {
                adFound = true;
                source = adSelectors[i];
                break;
            }
        }

        // Fallback: check MediaSession title for "Advertisement"
        if (!adFound && navigator.mediaSession && navigator.mediaSession.metadata) {
            var title = (navigator.mediaSession.metadata.title || '').toLowerCase();
            if (title.indexOf('advertisement') !== -1 || title.indexOf('ad break') !== -1) {
                adFound = true;
                source = 'mediasession_title';
            }
        }

        var skippable = false;
        if (adFound) {
            for (var j = 0; j < skipSelectors.length; j++) {
                if (document.querySelector(skipSelectors[j])) {
                    skippable = true;
                    break;
                }
            }
        }

        return {ad_detected: adFound, source: source, skippable: skippable};
    } catch (e) {
        return {ad_detected: false, source: 'error', skippable: false};
    }
})();
""".strip() % (
        selectors_js,
        skip_selectors_js,
    )


def _js_skip_ad() -> str:
    """Return JS that attempts to skip or mute a currently playing ad.

    Tries skip buttons first.  If no skip button is found, mutes all
    media elements so the user does not hear the ad audio.

    Returns ``{ "skipped": bool, "muted": bool }``.
    """
    skip_selectors_js = ", ".join("'%s'" % sel for sel in _AD_SKIP_SELECTORS)
    return """
(function() {
    try {
        var skipSelectors = [%s];
        var skipped = false;

        // Try clicking a skip button
        for (var i = 0; i < skipSelectors.length; i++) {
            var btn = document.querySelector(skipSelectors[i]);
            if (btn) {
                btn.click();
                skipped = true;
                break;
            }
        }

        // If we could not skip, mute all media elements
        var muted = false;
        if (!skipped) {
            var elements = document.querySelectorAll('audio, video');
            for (var j = 0; j < elements.length; j++) {
                elements[j].muted = true;
                muted = true;
            }
        }

        return {skipped: skipped, muted: muted};
    } catch (e) {
        return {skipped: false, muted: false};
    }
})();
""".strip() % skip_selectors_js


def _js_unmute_all() -> str:
    """Return JS that unmutes all media elements on the page."""
    return """
(function() {
    try {
        var elements = document.querySelectorAll('audio, video');
        for (var i = 0; i < elements.length; i++) {
            elements[i].muted = false;
        }
        return true;
    } catch (e) {
        return false;
    }
})();
""".strip()


def _js_detect_content_blocked() -> str:
    """Return JS that checks for common geo-restriction or DRM block indicators."""
    return """
(function() {
    try {
        var body = document.body ? document.body.innerText : '';
        var lc = body.toLowerCase();
        var blocked = false;
        var reason = '';

        var patterns = [
            'not available in your country',
            'content is not available',
            'video is not available',
            'this content is unavailable',
            'geo-restricted',
            'blocked in your region',
            'age-restricted',
            'sign in to confirm your age'
        ];

        for (var i = 0; i < patterns.length; i++) {
            if (lc.indexOf(patterns[i]) !== -1) {
                blocked = true;
                reason = patterns[i];
                break;
            }
        }

        return {blocked: blocked, reason: reason};
    } catch (e) {
        return {blocked: false, reason: 'check_error'};
    }
})();
""".strip()


def _js_detect_auth_redirect() -> str:
    """Return JS that checks if the page redirected to a login/sign-in page."""
    return """
(function() {
    try {
        var url = window.location.href.toLowerCase();
        var title = (document.title || '').toLowerCase();

        var loginPatterns = [
            'accounts.google.com/signin',
            'accounts.google.com/servicelogin',
            'login.spotify.com',
            'accounts.spotify.com',
            '/login',
            '/signin'
        ];

        var titlePatterns = ['sign in', 'log in', 'login'];

        for (var i = 0; i < loginPatterns.length; i++) {
            if (url.indexOf(loginPatterns[i]) !== -1) {
                return {auth_redirect: true, source: 'url'};
            }
        }

        for (var j = 0; j < titlePatterns.length; j++) {
            if (title.indexOf(titlePatterns[j]) !== -1) {
                return {auth_redirect: true, source: 'title'};
            }
        }

        return {auth_redirect: false, source: ''};
    } catch (e) {
        return {auth_redirect: false, source: 'error'};
    }
})();
""".strip()


def _js_click_play() -> str:
    """Return JS that tries to click the play button if paused."""
    return """
(function() {
    try {
        var media = document.querySelector('video') || document.querySelector('audio');
        if (media && media.paused) {
            media.play();
            return true;
        }
        return false;
    } catch (e) {
        return false;
    }
})();
""".strip()


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


# Stall detection threshold: if position has not changed for this many
# seconds while supposedly playing, we consider it a stall.
_STALL_THRESHOLD_SECONDS = 10.0

# Navigation retry delay in seconds
_NAV_RETRY_DELAY = 2.0

# Maximum consecutive stall recovery attempts before giving up
_MAX_STALL_RECOVERIES = 3


class BrowserErrorHandler:
    """Handles browser-native playback errors with recovery strategies.

    Each error type maps to a handler method that returns a
    :class:`RecoveryAction`.  The caller is responsible for acting on the
    action (e.g. skipping to next track, refreshing the page).

    The handler is stateless except for stall-detection counters that
    reset when the error type changes.
    """

    # Re-export error types as class-level constants for convenience
    NAVIGATION_ERROR = ErrorType.NAVIGATION_ERROR
    PLAYBACK_STALL = ErrorType.PLAYBACK_STALL
    AD_DETECTED = ErrorType.AD_DETECTED
    AUTH_EXPIRED = ErrorType.AUTH_EXPIRED
    RECIPE_FAILURE = ErrorType.RECIPE_FAILURE
    CONTENT_BLOCKED = ErrorType.CONTENT_BLOCKED

    def __init__(
        self,
        controller: BrowserPlaybackController | None = None,
        auth_manager: BrowserAuthManager | None = None,
    ) -> None:
        self._controller = controller
        self._auth_manager = auth_manager
        self._stall_recovery_count: int = 0
        self._ad_muted: bool = False

        logger.info("BrowserErrorHandler initialized")

    def set_controller(self, controller: BrowserPlaybackController) -> None:
        """Attach or replace the playback controller.

        Args:
            controller: The BrowserPlaybackController instance.
        """
        self._controller = controller

    def set_auth_manager(self, auth_manager: BrowserAuthManager) -> None:
        """Attach or replace the auth manager.

        Args:
            auth_manager: The BrowserAuthManager instance.
        """
        self._auth_manager = auth_manager

    # ------------------------------------------------------------------
    # Main dispatch
    # ------------------------------------------------------------------

    def handle_error(
        self,
        error_type: str,
        context: dict[str, Any] | None = None,
    ) -> RecoveryAction:
        """Handle a browser playback error and return a recovery action.

        Args:
            error_type: One of the :class:`ErrorType` values.
            context: Optional context dict with error-specific details
                (e.g. ``{"url": "...", "status_code": 404}``).

        Returns:
            A :class:`RecoveryAction` describing what the caller should do.
        """
        ctx = context or {}
        handler_map = {
            ErrorType.NAVIGATION_ERROR: self._handle_navigation_error,
            ErrorType.PLAYBACK_STALL: self._handle_playback_stall,
            ErrorType.AD_DETECTED: self._handle_ad_detected,
            ErrorType.AUTH_EXPIRED: self._handle_auth_expired,
            ErrorType.RECIPE_FAILURE: self._handle_recipe_failure,
            ErrorType.CONTENT_BLOCKED: self._handle_content_blocked,
        }

        handler = handler_map.get(error_type)
        if handler is None:
            logger.warning("BrowserErrorHandler unknown error type: %s", error_type)
            return RecoveryAction(
                strategy=RecoveryStrategy.ABORT,
                message="Unknown error type: %s" % error_type,
            )

        logger.info("BrowserErrorHandler handling %s", error_type)
        try:
            return handler(ctx)
        except Exception:
            logger.exception("BrowserErrorHandler failed to handle %s", error_type)
            return RecoveryAction(
                strategy=RecoveryStrategy.ABORT,
                message="Error handler itself failed for %s" % error_type,
            )

    # ------------------------------------------------------------------
    # Ad detection
    # ------------------------------------------------------------------

    def detect_ad(self) -> bool:
        """Check if an advertisement is currently playing.

        Injects JavaScript to look for ad markers in the DOM (YouTube,
        Spotify) and checks MediaSession title for "Advertisement".

        Returns:
            ``True`` if an ad is currently detected.
        """
        if self._controller is None:
            return False

        # Fire-and-forget detection -- we cannot get a synchronous result
        # from inject_js unless we are on the Qt main thread with a callback.
        # For synchronous callers, we inject the detection JS and rely on
        # the health monitor to act on the result.
        # This method is best used from the Qt main thread poll callback.
        self._controller.inject_js(_js_detect_ad())
        return False

    def detect_ad_sync(self, js_result: dict[str, Any] | None) -> bool:
        """Evaluate a previously-collected ad detection JS result.

        This is the synchronous path used by the health monitor when it
        already has the JS result from a callback.

        Args:
            js_result: The return value from ``_js_detect_ad()`` JS.

        Returns:
            ``True`` if an ad was detected.
        """
        if not js_result or not isinstance(js_result, dict):
            return False
        return bool(js_result.get("ad_detected", False))

    def get_ad_skip_js(self) -> str:
        """Return JavaScript code that skips or mutes the current ad.

        Returns:
            Self-contained JavaScript string.
        """
        return _js_skip_ad()

    def get_ad_detect_js(self) -> str:
        """Return JavaScript code that detects ads.

        Returns:
            Self-contained JavaScript string.
        """
        return _js_detect_ad()

    # ------------------------------------------------------------------
    # Error-specific handlers
    # ------------------------------------------------------------------

    def _handle_navigation_error(self, ctx: dict[str, Any]) -> RecoveryAction:
        """Handle a page navigation failure.

        Strategy: retry once after a 2-second delay.  If the context
        indicates this is already a retry, abort.
        """
        is_retry = ctx.get("retry_attempt", 0) > 0
        url = ctx.get("url", "unknown")

        if is_retry:
            logger.warning("BrowserErrorHandler navigation retry failed for %s", url)
            return RecoveryAction(
                strategy=RecoveryStrategy.SKIP,
                message="Navigation failed after retry for %s" % url,
            )

        logger.info("BrowserErrorHandler scheduling navigation retry for %s", url)
        return RecoveryAction(
            strategy=RecoveryStrategy.RETRY,
            delay_seconds=_NAV_RETRY_DELAY,
            message="Retrying navigation to %s" % url,
            context={"url": url, "retry_attempt": 1},
        )

    def _handle_playback_stall(self, ctx: dict[str, Any]) -> RecoveryAction:
        """Handle a playback stall (audio stopped unexpectedly).

        Recovery escalation:
        1. Click play button
        2. Refresh the page
        3. Re-navigate to the URL
        """
        self._stall_recovery_count += 1
        attempt = self._stall_recovery_count

        if attempt > _MAX_STALL_RECOVERIES:
            logger.warning(
                "BrowserErrorHandler stall recovery exhausted after %d attempts",
                _MAX_STALL_RECOVERIES,
            )
            self._stall_recovery_count = 0
            return RecoveryAction(
                strategy=RecoveryStrategy.SKIP,
                message="Playback stall unrecoverable after %d attempts" % _MAX_STALL_RECOVERIES,
            )

        if attempt == 1:
            # Step 1: Try clicking play
            logger.info("BrowserErrorHandler stall recovery step 1: click play")
            if self._controller is not None:
                self._controller.inject_js(_js_click_play())
            return RecoveryAction(
                strategy=RecoveryStrategy.WAIT,
                recovered=True,
                delay_seconds=TIMEOUT_SHUTDOWN,
                message="Attempted play click to recover stall",
            )

        if attempt == 2:
            # Step 2: Refresh the page
            logger.info("BrowserErrorHandler stall recovery step 2: refresh page")
            return RecoveryAction(
                strategy=RecoveryStrategy.REFRESH,
                delay_seconds=TIMEOUT_SHUTDOWN,
                message="Refreshing page to recover stall",
            )

        # Step 3: Re-navigate
        url = ctx.get("url", "")
        logger.info(
            "BrowserErrorHandler stall recovery step 3: re-navigate to %s",
            url[:120] if url else "current URL",
        )
        return RecoveryAction(
            strategy=RecoveryStrategy.RETRY,
            delay_seconds=TIMEOUT_SHUTDOWN,
            message="Re-navigating to recover stall",
            context={"url": url},
        )

    def _handle_ad_detected(self, ctx: dict[str, Any]) -> RecoveryAction:
        """Handle an ad being detected during playback.

        Tries to skip the ad.  If unskippable, mutes media elements
        and returns a WAIT action so the caller knows to check again.
        """
        if self._controller is None:
            return RecoveryAction(
                strategy=RecoveryStrategy.WAIT,
                message="Ad detected but no controller to skip",
            )

        skippable = ctx.get("skippable", False)

        if skippable:
            logger.info("BrowserErrorHandler skipping ad")
            self._controller.inject_js(_js_skip_ad())
            return RecoveryAction(
                strategy=RecoveryStrategy.NONE,
                recovered=True,
                message="Ad skipped",
            )

        # Unskippable ad -- mute and wait
        if not self._ad_muted:
            logger.info("BrowserErrorHandler muting unskippable ad")
            self._controller.inject_js(_js_skip_ad())
            self._ad_muted = True

        return RecoveryAction(
            strategy=RecoveryStrategy.WAIT,
            delay_seconds=TIMEOUT_LONG,
            message="Unskippable ad detected, muted and waiting",
        )

    def on_ad_ended(self) -> None:
        """Call when an ad finishes to restore volume.

        Unmutes media elements if they were muted during an unskippable ad.
        """
        if self._ad_muted and self._controller is not None:
            logger.info("BrowserErrorHandler ad ended, unmuting")
            self._controller.inject_js(_js_unmute_all())
            self._ad_muted = False

    def _handle_auth_expired(self, ctx: dict[str, Any]) -> RecoveryAction:
        """Handle session expiry or login redirect.

        Notifies the auth manager and returns a LOGIN action so the
        caller can prompt the user.
        """
        provider_name = ctx.get("provider_name", "unknown")

        if self._auth_manager is not None:
            try:
                self._auth_manager.mark_session_expired(provider_name)
            except KeyError:
                logger.warning(
                    "BrowserErrorHandler unknown provider for auth expiry: %s",
                    provider_name,
                )

        logger.warning("BrowserErrorHandler auth expired for provider %s", provider_name)
        return RecoveryAction(
            strategy=RecoveryStrategy.LOGIN,
            message="Session expired for %s, re-login required" % provider_name,
            context={"provider_name": provider_name},
        )

    def _handle_recipe_failure(self, ctx: dict[str, Any]) -> RecoveryAction:
        """Handle a JS recipe injection failure.

        This typically means the page structure changed and the recipe
        selectors no longer match.  Not much we can do automatically.
        """
        recipe_name = ctx.get("recipe_name", "unknown")
        logger.warning("BrowserErrorHandler recipe failure for %s", recipe_name)
        return RecoveryAction(
            strategy=RecoveryStrategy.SKIP,
            message="Recipe %s failed, skipping track" % recipe_name,
        )

    def _handle_content_blocked(self, ctx: dict[str, Any]) -> RecoveryAction:
        """Handle geo-restricted or DRM-blocked content.

        There is no automatic recovery -- skip to the next track.
        """
        reason = ctx.get("reason", "unknown restriction")
        logger.warning("BrowserErrorHandler content blocked: %s", reason)
        return RecoveryAction(
            strategy=RecoveryStrategy.SKIP,
            message="Content blocked: %s" % reason,
        )

    # ------------------------------------------------------------------
    # Stall detection helpers
    # ------------------------------------------------------------------

    def reset_stall_counter(self) -> None:
        """Reset the stall recovery attempt counter.

        Call this when playback resumes successfully after a stall.
        """
        if self._stall_recovery_count > 0:
            logger.debug("BrowserErrorHandler stall counter reset")
            self._stall_recovery_count = 0

    # ------------------------------------------------------------------
    # JS accessors for health monitor
    # ------------------------------------------------------------------

    def get_content_blocked_js(self) -> str:
        """Return JavaScript that detects geo/DRM content blocking."""
        return _js_detect_content_blocked()

    def get_auth_redirect_js(self) -> str:
        """Return JavaScript that detects login-page redirects."""
        return _js_detect_auth_redirect()

    def get_click_play_js(self) -> str:
        """Return JavaScript that clicks the play button if paused."""
        return _js_click_play()


__all__ = [
    "BrowserErrorHandler",
    "ErrorType",
    "RecoveryAction",
    "RecoveryStrategy",
]
