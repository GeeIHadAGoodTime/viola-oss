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

# While the in-app sign-in view is open, check for a finished sign-in this
# often, for at most this long (password managers, 2-step codes and "forgot
# password" detours take minutes, not seconds).
LOGIN_WATCH_INTERVAL_SECONDS = 2.0
LOGIN_WATCH_TIMEOUT_SECONDS = 600.0

# Providers whose sign-in Viola can detect on its own (a sign-in cookie the
# in-app browser receives). Others still rely on an explicit refresh.
_WATCHED_LOGIN_PROVIDERS = frozenset({"spotify"})

_SPOTIFY_WEB_PLAYER_HOST = "open.spotify.com"
_PAGE_HOST_JS = "(function() { try { return location.hostname || ''; } catch (e) { return ''; } })();"


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
        self._login_watch_tasks: dict[str, asyncio.Task[None]] = {}
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
        self._watch_spotify_cookies()

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

        if provider_name == "spotify":
            # The sign-in cookie is the authority: it does not depend on which
            # page the shared in-app browser happens to show (the agent
            # browser and YouTube use the same view), and it survives a
            # restart. The page check below is only a fallback for when the
            # cookie store cannot be read, and only on Spotify's own page.
            signed_in = await asyncio.to_thread(self.refresh_spotify_sign_in)
            if signed_in is not None:
                return status.logged_in
            if not await self._spotify_page_is_open():
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
                        await asyncio.to_thread(self._cache_spotify_login_cookies)

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

    def _webview_and_dispatcher(self) -> tuple[Any, Any]:
        controller = self._controller
        webview_controller = getattr(controller, "_webview_controller", None)
        dispatch_to_main = getattr(controller, "_post_to_main", None)
        return webview_controller, dispatch_to_main if callable(dispatch_to_main) else None

    def _watch_spotify_cookies(self) -> None:
        try:
            from music.spotify.cookie_bridge import ensure_spotify_cookie_tracker

            webview_controller, dispatch_to_main = self._webview_and_dispatcher()
            ensure_spotify_cookie_tracker(webview_controller, dispatch_to_main=dispatch_to_main, wait_seconds=0)
        except Exception:
            logger.exception("Could not start watching the in-app browser's Spotify cookies")

    def refresh_spotify_sign_in(self) -> bool | None:
        """Read Spotify's sign-in cookie from the in-app browser and update status.

        May block briefly (the first call attaches the cookie tracker on the Qt
        main thread, and the profile's cookie database is read from disk);
        async callers run it with ``asyncio.to_thread``.

        Returns:
            ``True`` when the in-app browser is signed in to Spotify, ``False``
            when it is not, or ``None`` when the cookie store could not be read
            (status is then left unchanged).
        """
        status = self._provider_status.get("spotify")
        if status is None or self._controller is None:
            return None

        from music.spotify.cookie_bridge import (
            cache_spotify_cookies,
            clear_cached_spotify_cookies,
            has_spotify_auth_cookie,
            read_spotify_cookies_from_qweb,
        )

        webview_controller, dispatch_to_main = self._webview_and_dispatcher()
        try:
            cookies = read_spotify_cookies_from_qweb(webview_controller, dispatch_to_main=dispatch_to_main)
        except Exception:
            logger.exception("Could not read Spotify cookies from the in-app browser")
            return None
        if cookies is None:
            return None

        signed_in = has_spotify_auth_cookie(cookies)
        status.logged_in = signed_in
        status.last_checked = time.time()
        if signed_in:
            status.session_expired = False
            if self._user_id:
                cache_spotify_cookies(cookies, user_id=self._user_id)
        elif self._user_id:
            clear_cached_spotify_cookies(user_id=self._user_id)
        return signed_in

    def forget_spotify_sign_in(self) -> None:
        """Disconnect: sign the in-app browser out of Spotify and drop cached state.

        Without this, the next sign-in check would find the in-app browser's
        Spotify cookie and report Spotify connected again right after the user
        pressed Disconnect. Blocking (runs on a worker thread); the caller
        cancels any running sign-in watch on its event loop first.
        """
        from music.spotify.cookie_bridge import clear_cached_spotify_cookies, delete_spotify_cookies_from_qweb

        if self._controller is not None:
            webview_controller, dispatch_to_main = self._webview_and_dispatcher()
            try:
                deleted = delete_spotify_cookies_from_qweb(webview_controller, dispatch_to_main=dispatch_to_main)
                logger.info("Spotify disconnect: in-app browser cookies deleted=%s", deleted)
            except Exception:
                logger.exception("Spotify disconnect: could not clear the in-app browser's Spotify cookies")
        if self._user_id:
            clear_cached_spotify_cookies(user_id=self._user_id)
        status = self._provider_status.get("spotify")
        if status is not None:
            status.logged_in = False
            status.session_expired = False
            status.last_checked = time.time()

    async def _spotify_page_is_open(self) -> bool:
        host = await self._execute_js(_PAGE_HOST_JS)
        return isinstance(host, str) and host.strip().lower() == _SPOTIFY_WEB_PLAYER_HOST

    # ------------------------------------------------------------------
    # Sign-in watch: notice a finished sign-in without the user telling us
    # ------------------------------------------------------------------

    def is_login_watch_active(self, provider_name: str) -> bool:
        """Return True while Viola is waiting for a sign-in to *provider_name*."""
        task = self._login_watch_tasks.get(provider_name)
        return task is not None and not task.done()

    def cancel_login_watch(self, provider_name: str) -> bool:
        """Stop waiting for a sign-in. Returns True when a watch was running."""
        task = self._login_watch_tasks.pop(provider_name, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def cancel_login(self, provider_name: str) -> bool:
        """The user cancelled sign-in: stop waiting and close the sign-in view."""
        if provider_name not in self._provider_status:
            raise KeyError("Unknown provider: %s" % provider_name)
        was_watching = self.cancel_login_watch(provider_name)
        self._close_login_view()
        logger.info("%s sign-in cancelled by the user", self._display_name(provider_name))
        return was_watching

    def _display_name(self, provider_name: str) -> str:
        status = self._provider_status.get(provider_name)
        return status.display_name if status is not None else provider_name

    def _login_view_open(self) -> bool:
        overlay = self._overlay_controller
        if overlay is None:
            return True
        return str(getattr(overlay, "state", "login") or "") == "login"

    def _close_login_view(self) -> None:
        overlay = self._overlay_controller
        if overlay is None or not self._login_view_open():
            return
        try:
            overlay.hide()
        except Exception:
            logger.exception("Failed to close the sign-in view")

    def _start_login_watch(self, provider_name: str, *, select_as_music_source: bool) -> bool:
        if provider_name not in _WATCHED_LOGIN_PROVIDERS:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info(
                "No event loop for the %s sign-in watch; sign-in is picked up on the next status check",
                self._display_name(provider_name),
            )
            return False
        self.cancel_login_watch(provider_name)
        task = loop.create_task(
            self._watch_login(provider_name, select_as_music_source=select_as_music_source),
            name="browser-login-watch-%s" % provider_name,
        )
        self._login_watch_tasks[provider_name] = task
        return True

    async def _probe_sign_in(self, provider_name: str) -> str | None:
        """Return what proved the sign-in (``auth_cookie`` / ``page``), or None."""
        if provider_name != "spotify":
            return None
        signed_in = await asyncio.to_thread(self.refresh_spotify_sign_in)
        if signed_in:
            return "auth_cookie"
        if signed_in is None and await self._spotify_page_is_open():
            recipe = get_recipe(provider_name)
            parsed = self._parse_js_result(await self._execute_js(recipe.get_login_detection_js()))
            if parsed.get("logged_in"):
                return "page"
        return None

    async def _watch_login(self, provider_name: str, *, select_as_music_source: bool) -> None:
        display_name = self._display_name(provider_name)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOGIN_WATCH_TIMEOUT_SECONDS
        logger.info("Waiting for %s sign-in in the in-app browser", display_name)
        try:
            while loop.time() < deadline:
                await asyncio.sleep(LOGIN_WATCH_INTERVAL_SECONDS)
                # Read before probing: if the user closed the view, this probe
                # is the last one (they may have signed in and then closed it).
                view_open = self._login_view_open()
                signal = await self._probe_sign_in(provider_name)
                if signal:
                    await self._finish_login(
                        provider_name,
                        signal=signal,
                        select_as_music_source=select_as_music_source,
                    )
                    return
                if not view_open:
                    logger.info("%s sign-in view closed before a sign-in was detected", display_name)
                    return
            logger.info(
                "Stopped waiting for %s sign-in after %d minutes",
                display_name,
                int(LOGIN_WATCH_TIMEOUT_SECONDS // 60),
            )
            self._close_login_view()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s sign-in watch failed", display_name)
        finally:
            current = asyncio.current_task()
            if self._login_watch_tasks.get(provider_name) is current:
                self._login_watch_tasks.pop(provider_name, None)

    async def _finish_login(self, provider_name: str, *, signal: str, select_as_music_source: bool) -> None:
        self.mark_logged_in(provider_name)
        logger.info(
            "%s sign-in detected (%s); closing the sign-in view",
            self._display_name(provider_name),
            signal,
        )
        if provider_name == "spotify" and signal != "auth_cookie":
            await asyncio.to_thread(self._cache_spotify_login_cookies)
        self._close_login_view()
        if select_as_music_source:
            await self._select_music_source(provider_name)

    async def _select_music_source(self, provider_name: str) -> bool:
        """Make *provider_name* the music source the user chose in Settings.

        Settings asks for this when the user picked Spotify as the Music
        Source and had to sign in first. Saving it here, when the sign-in is
        detected, means the choice sticks even if the Settings window was
        closed meanwhile or never saved.
        """
        if not self._user_id:
            logger.warning("Not changing the music source after sign-in: no signed-in Viola user")
            return False
        try:
            from ui.settings_effects import apply_setting_effect, broadcast_settings_changed
            from ui.settings_manager import get_settings_manager

            settings_mgr = get_settings_manager()
            previous = settings_mgr.get("active_music_provider_id", None, user_id=self._user_id)
            if not settings_mgr.set_user_setting(
                self._user_id,
                "active_music_provider_id",
                provider_name,
                save_immediately=True,
            ):
                logger.warning("Settings refused the music source change to %s", provider_name)
                return False
            apply_setting_effect(
                "active_music_provider_id",
                provider_name,
                settings_mgr=settings_mgr,
                previous=previous,
                user_id=self._user_id,
            )
            await broadcast_settings_changed(settings_mgr, user_id=self._user_id)
            logger.info(
                "Music source set to %s after sign-in (was %s)",
                provider_name,
                previous or "not set",
            )
            return True
        except Exception:
            logger.exception("Could not save %s as the music source after sign-in", provider_name)
            return False

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

    def initiate_login(self, provider_name: str, *, select_as_music_source: bool = False) -> str:
        """Navigate QWebEngineView to provider login page.

        Shows the in-app sign-in view and, for providers Viola can watch,
        starts waiting for the sign-in to finish: once it does, the provider
        is marked signed in and the view closes on its own.

        Args:
            provider_name: The provider identifier.
            select_as_music_source: Also make this provider the music source
                once the sign-in is detected (Settings > Music Source).

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
            if provider_name == "spotify":
                # Watch the in-app browser's cookies before Spotify can set the
                # sign-in cookie, so the sign-in is seen when it happens.
                self._watch_spotify_cookies()
            self._controller.navigate(login_url)
            logger.info("Navigated to login page for %s", provider_name)
            # Show the overlay so the user can interact with the login page
            if self._overlay_controller is not None:
                try:
                    self._overlay_controller.show_login(provider_name)
                except Exception:
                    logger.exception("Failed to show login overlay for %s", provider_name)
            self._start_login_watch(provider_name, select_as_music_source=select_as_music_source)
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


# ---------------------------------------------------------------------------
# Is Spotify usable right now? (music source fallback)
# ---------------------------------------------------------------------------

SPOTIFY_NOT_CONNECTED_NOTICE = (
    "Spotify isn't connected yet, so this is playing on YouTube. Connect Spotify in Settings > Music."
)
_SPOTIFY_RECHECK_SECONDS = 30.0
_NOTICE_REPEAT_SECONDS = 20.0
_last_notice_at = 0.0


def _resolve_sign_in_user_id(user_id: str | None) -> str | None:
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_or_device_user_id

        return get_current_or_device_user_id() or None
    except Exception:
        return None


def is_spotify_signed_in(user_id: str | None = None) -> bool:
    """Return True when Spotify can play for *user_id* right now.

    Signed in means any of: the in-app sign-in was detected, its cookies are
    cached for the playback browser, or the playback browser's own profile
    holds Spotify's sign-in cookies. When none of those is known, the in-app
    browser's cookie store is read (about a second, at most every 30 s), which
    finds a sign-in from before a restart.

    Blocking; async callers use ``asyncio.to_thread``.
    """
    resolved = _resolve_sign_in_user_id(user_id)
    if not resolved:
        return False

    manager = get_browser_auth_manager(resolved)
    status = manager.get_auth_status().get("spotify")
    if status is not None and status.logged_in:
        return True

    try:
        from music.spotify.cookie_bridge import get_cached_spotify_cookies, has_spotify_auth_cookie

        if has_spotify_auth_cookie(get_cached_spotify_cookies(user_id=resolved)):
            return True
    except Exception:
        logger.debug("Spotify cookie cache check failed", exc_info=True)

    try:
        from music.spotify.cdp_controller import has_cdp_session_cookies

        if has_cdp_session_cookies(user_id=resolved):
            return True
    except Exception:
        logger.debug("Spotify playback-profile cookie check failed", exc_info=True)

    if status is None or manager.is_login_watch_active("spotify"):
        return False
    if time.time() - (status.last_checked or 0.0) < _SPOTIFY_RECHECK_SECONDS:
        return False
    return bool(manager.refresh_spotify_sign_in())


def notify_spotify_not_connected() -> bool:
    """Tell the user on screen that Spotify was skipped because it isn't connected.

    The music source is Spotify but Spotify cannot play, so Viola plays on
    YouTube instead. Saying so on screen (as well as in the tool result the
    reply is written from) means the switch is never silent. Returns True when
    the notice went out.
    """
    global _last_notice_at
    now = time.monotonic()
    if _last_notice_at and now - _last_notice_at < _NOTICE_REPEAT_SECONDS:
        return False
    try:
        from core.state_hub import SetToastMessage, get_state_hub

        get_state_hub().dispatch(SetToastMessage(message=SPOTIFY_NOT_CONNECTED_NOTICE))
    except Exception:
        logger.debug("Spotify fallback notice skipped (state hub not initialized)")
        return False
    _last_notice_at = now
    return True


__all__ = [
    "SPOTIFY_NOT_CONNECTED_NOTICE",
    "BrowserAuthManager",
    "ProviderAuthStatus",
    "get_browser_auth_manager",
    "is_spotify_signed_in",
    "notify_spotify_not_connected",
]
