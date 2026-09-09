"""Browser session authentication manager.

Tracks login state for each music provider by checking the QWebEngineView's
page content via recipe-specific JavaScript.  Sessions persist via
QWebEngineProfile cookies across restarts.

This does NOT handle OAuth tokens or API keys -- users log in directly
through the real provider website in QWebEngineView.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from music.providers.browser.recipes import (
    PROVIDER_HOME_URLS,
    get_available_providers,
    get_recipe,
)

if TYPE_CHECKING:
    from music.providers.browser.provider import BrowserPlaybackController

logger = get_logger(__name__)


@dataclass
class ProviderAuthStatus:
    """Authentication status for a single music provider."""

    provider_name: str
    display_name: str
    logged_in: bool = False
    session_expired: bool = False
    last_checked: float | None = None
    login_url: str | None = None
    icon_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""
        return {
            "name": self.provider_name,
            "display_name": self.display_name,
            "logged_in": self.logged_in,
            "session_expired": self.session_expired,
            "last_checked": self.last_checked,
            "login_url": self.login_url,
            "icon_url": self.icon_url,
        }


# Display names for providers exposed through browser-native auth.
_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "youtube_music": "YouTube Music",
    "spotify": "Spotify",
}


class BrowserAuthManager:
    """Manages browser session authentication state for music providers.

    Responsibilities:

    1. Track which providers the user has logged into
    2. Detect login state by injecting recipe JS
    3. Detect session expiry via URL redirect or recipe JS
    4. Trigger login flows by navigating to provider login pages
    5. Report auth status for UI display
    """

    def __init__(self, user_id: str | None = None) -> None:
        self._user_id = user_id
        self._provider_status: dict[str, ProviderAuthStatus] = {}
        self._controller: BrowserPlaybackController | None = None
        self._overlay_controller: Any | None = None
        self._initialize_provider_statuses()

    def _initialize_provider_statuses(self) -> None:
        """Populate status entries for current product-supported providers."""
        for name in get_available_providers():
            display_name = _PROVIDER_DISPLAY_NAMES.get(name, name)
            login_url = PROVIDER_HOME_URLS.get(name)
            icon_url: str | None = None

            try:
                recipe = get_recipe(name)
                display_name = recipe.display_name
                icon_url = recipe.get_provider_icon_url()
            except ValueError:
                pass

            self._provider_status[name] = ProviderAuthStatus(
                provider_name=name,
                display_name=display_name,
                login_url=login_url,
                icon_url=icon_url,
            )

    def set_controller(self, controller: BrowserPlaybackController) -> None:
        """Attach the BrowserPlaybackController for JS injection.

        Args:
            controller: The browser playback controller that provides
                access to the QWebEngineView page for JS execution.
        """
        self._controller = controller
        for mgr in list(_auth_managers.values()):
            if mgr is not self and mgr._controller is None:
                mgr._controller = controller
        logger.info("BrowserAuthManager controller attached")

    def set_overlay_controller(self, overlay_controller: Any) -> None:
        """Attach the overlay controller for showing/hiding the login overlay.

        Args:
            overlay_controller: A BrowserOverlayController instance
                with ``show_login()`` and ``hide()`` methods.
        """
        self._overlay_controller = overlay_controller
        for mgr in list(_auth_managers.values()):
            if mgr is not self and mgr._overlay_controller is None:
                mgr._overlay_controller = overlay_controller
        logger.info("BrowserAuthManager overlay controller attached")

    def get_auth_status(self) -> dict[str, ProviderAuthStatus]:
        """Return auth status for launch-supported providers.

        Returns:
            Dictionary mapping provider name to its ProviderAuthStatus.
        """
        return dict(self._provider_status)

    def get_provider_status(self, provider_name: str) -> ProviderAuthStatus:
        """Return auth status for a specific provider.

        Args:
            provider_name: The provider identifier (e.g. ``youtube_music``).

        Returns:
            The ProviderAuthStatus for the requested provider.

        Raises:
            KeyError: If *provider_name* is not a known provider.
        """
        if provider_name not in self._provider_status:
            raise KeyError("Unknown provider: %s" % provider_name)
        return self._provider_status[provider_name]

    async def check_login_state(self, provider_name: str) -> bool:
        """Inject recipe JS to check if user is logged in.

        Only works for providers that have an implemented recipe.
        For providers without a recipe, returns the cached ``logged_in``
        value without performing any JS check.

        Args:
            provider_name: The provider identifier.

        Returns:
            ``True`` if the user is logged in to the provider.

        Raises:
            KeyError: If *provider_name* is not a known provider.
        """
        if provider_name not in self._provider_status:
            raise KeyError("Unknown provider: %s" % provider_name)

        status = self._provider_status[provider_name]

        if provider_name not in get_available_providers():
            logger.debug(
                "No recipe for %s, returning cached login state: %s",
                provider_name,
                status.logged_in,
            )
            return status.logged_in

        if self._controller is None:
            logger.debug(
                "No controller attached, returning cached login state for %s",
                provider_name,
            )
            return status.logged_in

        try:
            recipe = get_recipe(provider_name)
            js_code = recipe.get_login_detection_js()
            result = await self._execute_js(js_code)

            if result is not None:
                parsed = self._parse_js_result(result)
                logged_in = parsed.get("logged_in", False)
                status.logged_in = bool(logged_in)
                status.last_checked = time.time()

                if logged_in:
                    status.session_expired = False
                    if provider_name == "spotify":
                        self._cache_spotify_login_cookies()

                logger.debug(
                    "Login check for %s: logged_in=%s",
                    provider_name,
                    status.logged_in,
                )
            else:
                logger.debug("Login check for %s returned None", provider_name)

        except Exception:
            logger.exception("Failed to check login state for %s", provider_name)

        return status.logged_in

    def _cache_spotify_login_cookies(self) -> None:
        """Snapshot Spotify cookies from QWebEngine for later CDP injection."""
        if not self._user_id:
            logger.debug("Skipping Spotify cookie cache: no authenticated user_id")
            return
        if self._controller is None:
            logger.debug("Skipping Spotify cookie cache for %s: no controller", self._user_id)
            return

        webview_controller = getattr(self._controller, "_webview_controller", None)
        dispatch_to_main = getattr(self._controller, "_post_to_main", None)
        try:
            from music.spotify.cookie_bridge import extract_spotify_cookies_from_qweb

            cookies = extract_spotify_cookies_from_qweb(
                webview_controller,
                user_id=self._user_id,
                dispatch_to_main=dispatch_to_main if callable(dispatch_to_main) else None,
            )
            logger.info(
                "Spotify BrowserAuth cookie snapshot complete for user %s (cookies=%d)",
                self._user_id,
                len(cookies),
            )
        except Exception:
            logger.exception("Failed to cache Spotify BrowserAuth cookies for user %s", self._user_id)

    async def check_session_expired(self, provider_name: str) -> bool:
        """Inject recipe JS to check if session has expired.

        Only works for providers that have an implemented recipe.

        Args:
            provider_name: The provider identifier.

        Returns:
            ``True`` if the session has expired and re-login is needed.

        Raises:
            KeyError: If *provider_name* is not a known provider.
        """
        if provider_name not in self._provider_status:
            raise KeyError("Unknown provider: %s" % provider_name)

        status = self._provider_status[provider_name]

        if provider_name not in get_available_providers():
            logger.debug(
                "No recipe for %s, returning cached expiry state: %s",
                provider_name,
                status.session_expired,
            )
            return status.session_expired

        if self._controller is None:
            logger.debug(
                "No controller attached, returning cached expiry state for %s",
                provider_name,
            )
            return status.session_expired

        try:
            recipe = get_recipe(provider_name)
            js_code = recipe.get_session_expired_js()
            result = await self._execute_js(js_code)

            if result is not None:
                parsed = self._parse_js_result(result)
                expired = parsed.get("expired", False)
                status.session_expired = bool(expired)
                status.last_checked = time.time()

                if expired:
                    status.logged_in = False

                logger.debug(
                    "Session expiry check for %s: expired=%s",
                    provider_name,
                    status.session_expired,
                )
            else:
                logger.debug("Session expiry check for %s returned None", provider_name)

        except Exception:
            logger.exception("Failed to check session expiry for %s", provider_name)

        return status.session_expired

    def initiate_login(self, provider_name: str) -> str:
        """Navigate QWebEngineView to provider login page.

        Args:
            provider_name: The provider identifier.

        Returns:
            The login URL that the browser was navigated to.

        Raises:
            KeyError: If *provider_name* is not a known provider.
            RuntimeError: If no controller is attached or no login URL
                is available.
        """
        if provider_name not in self._provider_status:
            raise KeyError("Unknown provider: %s" % provider_name)

        status = self._provider_status[provider_name]
        login_url = status.login_url

        if not login_url:
            raise RuntimeError("No login URL available for provider: %s" % provider_name)

        if self._controller is not None:
            self._controller.navigate(login_url)
            logger.info("Navigated to login page for %s", provider_name)
            # Show the overlay so the user can interact with the login page
            if self._overlay_controller is not None:
                try:
                    self._overlay_controller.show_login(provider_name)
                except Exception:
                    logger.exception("Failed to show login overlay for %s", provider_name)
        else:
            logger.warning(
                "No controller attached; login URL returned but browser " "not navigated for %s",
                provider_name,
            )

        return login_url

    def mark_logged_in(self, provider_name: str) -> None:
        """Mark a provider as successfully logged in.

        Args:
            provider_name: The provider identifier.

        Raises:
            KeyError: If *provider_name* is not a known provider.
        """
        if provider_name not in self._provider_status:
            raise KeyError("Unknown provider: %s" % provider_name)

        status = self._provider_status[provider_name]
        status.logged_in = True
        status.session_expired = False
        status.last_checked = time.time()
        logger.info("Provider %s marked as logged in", provider_name)

    def mark_session_expired(self, provider_name: str) -> None:
        """Mark a provider session as expired.

        Args:
            provider_name: The provider identifier.

        Raises:
            KeyError: If *provider_name* is not a known provider.
        """
        if provider_name not in self._provider_status:
            raise KeyError("Unknown provider: %s" % provider_name)

        status = self._provider_status[provider_name]
        status.session_expired = True
        status.logged_in = False
        status.last_checked = time.time()
        logger.info("Provider %s session marked as expired", provider_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_js(self, code: str) -> Any:
        """Execute JavaScript via the attached controller and await the result.

        Uses an ``asyncio.Future`` to bridge the callback-based
        ``inject_js`` method to async/await.

        Args:
            code: JavaScript code to execute.

        Returns:
            The JS return value, or ``None`` on timeout / error.
        """
        if self._controller is None:
            return None

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def _on_result(result: Any) -> None:
            if not future.done():
                loop.call_soon_threadsafe(future.set_result, result)

        self._controller.inject_js(code, callback=_on_result)

        try:
            result = await asyncio.wait_for(future, timeout=TIMEOUT_LONG)
            return result
        except TimeoutError:
            logger.warning("JS execution timed out after %s seconds", TIMEOUT_LONG)
            return None

    @staticmethod
    def _parse_js_result(result: Any) -> dict[str, Any]:
        """Parse a JS result (string or dict) into a dictionary.

        Recipe JS functions return JSON strings.  The QWebEngineView
        callback may deliver these as raw strings or already-parsed dicts
        depending on the Qt version.

        Args:
            result: The raw JS return value.

        Returns:
            Parsed dictionary.  Returns empty dict on parse failure.
        """
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                logger.debug("Failed to parse JS result: %s", result[:100])
        return {}


# ---------------------------------------------------------------------------
# Per-user managers (with shared runtime controller)
# ---------------------------------------------------------------------------

_DEFAULT_MANAGER_KEY = "__default__"
_auth_managers: dict[str, BrowserAuthManager] = {}


def get_browser_auth_manager(user_id: str | None = None) -> BrowserAuthManager:
    """Return the BrowserAuthManager for ``user_id`` (per-user isolation).

    When ``user_id`` is omitted, returns the shared default manager used
    by runtime components (QWebEngineView controller binding, etc.).
    The runtime controller attached to the default manager is propagated
    to per-user managers so injected JS continues to work after login.
    """
    key = user_id or _DEFAULT_MANAGER_KEY
    mgr = _auth_managers.get(key)
    if mgr is None:
        mgr = BrowserAuthManager(user_id=user_id)
        # Inherit the controller from the default manager so per-user
        # managers can still drive the single QWebEngineView.
        if key != _DEFAULT_MANAGER_KEY:
            default = _auth_managers.get(_DEFAULT_MANAGER_KEY)
            if default is not None:
                if default._controller is not None:
                    mgr.set_controller(default._controller)
                if default._overlay_controller is not None:
                    mgr.set_overlay_controller(default._overlay_controller)
        _auth_managers[key] = mgr
    return mgr


__all__ = [
    "BrowserAuthManager",
    "ProviderAuthStatus",
    "get_browser_auth_manager",
]
