from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from core.exceptions import ErrorContext, ServiceError, ServiceUnavailableError
from core.logging_config import get_logger
from services.cdp_target import get_preferred_target_id, is_local_target_url, target_id_matches

logger = get_logger(__name__)

try:
    from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - exercised by connect() error handling
    PlaywrightError = RuntimeError
    PlaywrightTimeoutError = TimeoutError

_PLAYWRIGHT_OPERATION_ERRORS = (
    PlaywrightError,
    PlaywrightTimeoutError,
    RuntimeError,
    TimeoutError,
    TypeError,
    ValueError,
)

_STAGE_MARKER_TITLE = "__viola_stage_overlay__"
_STAGE_MARKER_URL_FRAGMENT = "__viola_stage_overlay__"


class PlaywrightCDPConnectionError(ServiceUnavailableError):
    """Failed to connect Playwright to the desktop CDP endpoint."""

    def __init__(self, message: str) -> None:
        ServiceError.__init__(
            self,
            message,
            context=ErrorContext(
                component="playwright_cdp_client",
                operation="connect",
                params={"service": "playwright_cdp_client"},
                user_message=message,
                recovery_hint=(
                    "This is a fault in Viola's own browser on this device. No website was "
                    "contacted, so nothing is known about whether the site itself is reachable."
                ),
            ),
        )
        self.service = "playwright_cdp_client"


class PlaywrightCDPCommandError(ServiceError):
    """A Playwright-driven browser action failed."""

    def __init__(self, method: str, error: BaseException | str) -> None:
        self.method = method
        self.error = str(error)
        self.is_timeout = "timeout" in self.error.lower() or "timed out" in self.error.lower()
        message = "The browser could not complete that action. Please try again."
        super().__init__(
            message,
            context=ErrorContext(
                component="playwright_cdp_client",
                operation=method,
                params={"error": self.error},
                user_message=message,
                recovery_hint="Wait for the page to finish loading and try again.",
            ),
        )


class PlaywrightCDPCommandTimeoutError(PlaywrightCDPCommandError):
    """A Playwright browser action exceeded the local command budget."""


class _DriverLoop:
    """A private event loop that can actually spawn Playwright's driver.

    Viola's API server installs ``WindowsSelectorEventLoopPolicy`` process-wide
    (``core/server_factory.py``), and on Windows a ``SelectorEventLoop`` has no
    subprocess transport at all -- ``loop.subprocess_exec`` raises
    ``NotImplementedError``. Playwright starts by launching its Node driver as a
    subprocess, so ``async_playwright().start()`` cannot run on the app's own
    loop no matter what the CDP endpoint does. That is the second half of the
    desktop browser outage: the raw-CDP client this replaced spoke straight over
    a websocket and never needed a child process.

    ``music/spotify/cdp_controller.py`` already pins its Playwright work to a
    thread with a Proactor loop for exactly this reason. This is the same remedy
    for the desktop browser, kept inside the client so no caller has to know and
    the app-wide loop policy is left alone.

    Playwright's async objects are bound to the loop that created them, so every
    call that touches them is marshalled back here. Calls already running on this
    loop are awaited directly, so nested use is safe.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._loop is not None

    def start(self) -> None:
        if self._loop is not None:
            return
        ready = threading.Event()
        holder: dict[str, Any] = {}

        def _serve() -> None:
            loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            holder["loop"] = loop
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                with contextlib.suppress(Exception):
                    loop.close()

        thread = threading.Thread(target=_serve, name="viola-cdp-driver", daemon=True)
        thread.start()
        if not ready.wait(10.0):
            raise PlaywrightCDPConnectionError(
                "Viola's own built-in browser could not be started on this computer, so no page "
                "was opened and no request was sent to the website."
            )
        self._loop = holder["loop"]
        self._thread = thread

    async def run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Await *factory()* on the driver loop, from anywhere."""
        loop = self._loop
        if loop is None or asyncio.get_running_loop() is loop:
            return await factory()

        async def _invoke() -> Any:
            return await factory()

        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_invoke(), loop))

    async def stop(self) -> None:
        loop, self._loop = self._loop, None
        thread, self._thread = self._thread, None
        if loop is not None:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(None, thread.join, 10.0)


class PlaywrightCDPClient:
    """Playwright-backed controller for the visible Qt WebEngine CDP page."""

    def __init__(self, port: int = 9222, command_timeout: float = 30.0) -> None:
        self._port = port
        self._command_timeout = command_timeout
        self._driver = _DriverLoop()
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._bridge: Any | None = None
        self._page: Any | None = None
        self._cdp_session: Any | None = None
        self._target_id = ""
        self._connected = False
        self._network_requests: dict[int, dict[str, Any]] = {}
        self._network_log_by_user: dict[str, list[dict[str, Any]]] = {}
        self._active_user_id: str | None = None

    async def connect(self, target_url_filter: str | None = None) -> None:
        if self.connected:
            logger.debug("Playwright CDP client already connected")
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightCDPConnectionError("Playwright is not installed. Please reinstall Viola.") from exc

        try:
            # Playwright's CDP attach always configures browser-level download
            # behavior, which Qt WebEngine has no concept of and rejects. Route
            # the attach through a loopback relay that answers that one command
            # locally; see services/browser/qt_cdp_bridge for the full rationale.
            # The relay starts first so a dead DevTools endpoint fails here
            # instead of after spawning Playwright's driver process. It stays on
            # the caller's loop: Playwright reaches it over TCP from the Node
            # driver, so the two never share a loop.
            from services.browser.qt_cdp_bridge import QtWebEngineCDPBridge

            self._bridge = QtWebEngineCDPBridge(self._port, connect_timeout=self._command_timeout)
            endpoint = await self._bridge.start()

            self._driver.start()

            async def _attach() -> None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint,
                    timeout=int(self._command_timeout * 1000),
                )
                self._page = await self._select_page(target_url_filter=target_url_filter)
                self._target_id = await self._target_id_for_page(self._page)
                self._cdp_session = await self._page.context.new_cdp_session(self._page)

            await self._driver.run(_attach)
            self._attach_network_capture(self._page)
            self._connected = True
            logger.info("Playwright CDP client connected (target=%s url=%s)", self._target_id, self._page.url)
        except PlaywrightCDPConnectionError:
            await self.close()
            raise
        except Exception as exc:
            logger.exception("Playwright CDP connection failed on port %s", self._port)
            await self.close()
            raise PlaywrightCDPConnectionError(
                "Viola's own built-in browser could not be started on this computer, so no page "
                "was opened and no request was sent to the website. Nothing is known about "
                "whether the site itself is reachable."
            ) from exc

    async def close(self) -> None:
        self._connected = False
        if self._cdp_session is not None:
            try:
                await self._driver.run(self._cdp_session.detach)
            except _PLAYWRIGHT_OPERATION_ERRORS:
                logger.debug("Playwright CDP session detach failed")
            self._cdp_session = None
        if self._browser is not None:
            try:
                await self._driver.run(self._browser.close)
            except _PLAYWRIGHT_OPERATION_ERRORS:
                logger.debug("Playwright CDP browser close failed")
            self._browser = None
        if self._playwright is not None:
            try:
                await self._driver.run(self._playwright.stop)
            except _PLAYWRIGHT_OPERATION_ERRORS:
                logger.debug("Playwright stop failed")
            self._playwright = None
        await self._driver.stop()
        if self._bridge is not None:
            # The relay is torn down last, after Playwright has let go of the
            # socket, and its failures are never allowed to mask the original
            # error the caller is already unwinding.
            bridge, self._bridge = self._bridge, None
            with contextlib.suppress(Exception):
                await bridge.stop()
        self._page = None

    async def reconnect(self, target_url_filter: str | None = None) -> None:
        await self.close()
        await self.connect(target_url_filter=target_url_filter)

    async def _select_page(self, *, target_url_filter: str | None = None) -> Any:
        pages = [page for context in self._browser.contexts for page in context.pages]
        if not pages:
            raise PlaywrightCDPConnectionError(
                "Viola's own built-in browser has no page open to attach to on this computer, "
                "so no request was sent to the website. Nothing is known about whether the "
                "site itself is reachable."
            )

        preferred_id = get_preferred_target_id()
        if preferred_id:
            for page in pages:
                page_target_id = await self._target_id_for_page(page)
                if target_id_matches(page_target_id, preferred_id):
                    return page

        non_local = [page for page in pages if not is_local_target_url(str(page.url or ""))]
        candidates = non_local or pages

        for page in candidates:
            if await self._page_title(page) == _STAGE_MARKER_TITLE:
                return page
        for page in candidates:
            if _STAGE_MARKER_URL_FRAGMENT in str(page.url or ""):
                return page

        if target_url_filter:
            for page in candidates:
                if target_url_filter in str(page.url or ""):
                    return page

        if non_local:
            return non_local[0]

        for page in pages:
            if str(page.url or "") == "about:blank":
                return page

        return pages[0]

    async def _target_id_for_page(self, page: Any) -> str:
        try:
            session = await page.context.new_cdp_session(page)
            try:
                info = await session.send("Target.getTargetInfo")
            finally:
                await session.detach()
            target_info = info.get("targetInfo") if isinstance(info, dict) else None
            if isinstance(target_info, dict):
                return str(target_info.get("targetId") or "")
        except _PLAYWRIGHT_OPERATION_ERRORS:
            logger.debug("Could not read Playwright CDP target id for page")
        return ""

    async def _page_title(self, page: Any) -> str:
        try:
            return str(await self._driver.run(page.title))
        except _PLAYWRIGHT_OPERATION_ERRORS:
            return ""

    def _attach_network_capture(self, page: Any) -> None:
        def on_request(request: Any) -> None:
            self._network_requests[id(request)] = {
                "method": getattr(request, "method", "GET"),
                "url": getattr(request, "url", ""),
                "request_body": getattr(request, "post_data", None),
                "timestamp": time.time(),
            }

        def on_response(response: Any) -> None:
            request = getattr(response, "request", None)
            base = self._network_requests.pop(id(request), {}) if request is not None else {}
            entry = {
                "method": base.get("method") or getattr(request, "method", "GET"),
                "url": base.get("url") or getattr(response, "url", ""),
                "status": getattr(response, "status", None),
                "request_body": base.get("request_body"),
                "response_body": "",
                "timestamp": base.get("timestamp") or time.time(),
            }
            owner = self._active_user_id
            if owner:
                self.record_network_entry(entry, user_id=owner)

        try:
            page.on("request", on_request)
            page.on("response", on_response)
        except _PLAYWRIGHT_OPERATION_ERRORS:
            logger.debug("Could not attach Playwright network capture")

    @property
    def connected(self) -> bool:
        if not self._connected or self._page is None:
            return False
        try:
            return not self._page.is_closed()
        except _PLAYWRIGHT_OPERATION_ERRORS:
            return False

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def page(self) -> Any:
        if not self.connected:
            raise PlaywrightCDPConnectionError(
                "Viola's own built-in browser is not attached on this computer, so no request "
                "was sent to the website. Nothing is known about whether the site itself is "
                "reachable."
            )
        return self._page

    async def navigate(self, url: str) -> dict[str, Any]:
        try:
            response = await self._driver.run(
                lambda: self.page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self._command_timeout * 1000),
                )
            )
            return {"result": {"status": getattr(response, "status", None) if response is not None else None}}
        except Exception as exc:
            raise self._command_error("Page.goto", exc) from exc

    async def screenshot(self, *, format: str = "png", quality: int | None = None) -> bytes:
        try:
            kwargs: dict[str, Any] = {"type": format}
            if quality is not None and format == "jpeg":
                kwargs["quality"] = quality
            return await self._driver.run(lambda: self.page.screenshot(**kwargs))
        except Exception as exc:
            raise self._command_error("Page.screenshot", exc) from exc

    async def get_accessibility_tree(self) -> dict[str, Any]:
        try:
            if self._cdp_session is None:
                self._cdp_session = await self._driver.run(lambda: self.page.context.new_cdp_session(self.page))
            return await self._driver.run(lambda: self._cdp_session.send("Accessibility.getFullAXTree"))
        except Exception as exc:
            raise self._command_error("Accessibility.getFullAXTree", exc) from exc

    async def aria_snapshot(self) -> str:
        try:
            return str(await self._driver.run(lambda: self.page.locator("body").aria_snapshot()))
        except Exception as exc:
            raise self._command_error("Locator.aria_snapshot", exc) from exc

    async def click(self, x: float, y: float) -> None:
        try:
            await self._driver.run(lambda: self.page.mouse.click(x, y))
        except Exception as exc:
            raise self._command_error("Mouse.click", exc) from exc

    async def type_text(self, text: str) -> None:
        try:
            insert_text = getattr(self.page.keyboard, "insert_text", None)
            if callable(insert_text):
                await self._driver.run(lambda: insert_text(text))
            else:
                await self._driver.run(lambda: self.page.keyboard.type(text))
        except Exception as exc:
            raise self._command_error("Keyboard.insert_text", exc) from exc

    async def press_key(self, key: str) -> None:
        try:
            await self._driver.run(lambda: self.page.keyboard.press(key))
        except Exception as exc:
            raise self._command_error("Keyboard.press", exc) from exc

    async def evaluate_js(self, expression: str) -> Any:
        try:
            return await self._driver.run(lambda: self.page.evaluate(expression))
        except Exception as exc:
            raise self._command_error("Page.evaluate", exc) from exc

    async def get_url(self) -> str:
        return str(self.page.url or "")

    async def get_title(self) -> str:
        try:
            return str(await self._driver.run(self.page.title))
        except Exception as exc:
            raise self._command_error("Page.title", exc) from exc

    async def stop_loading(self) -> None:
        try:
            await self._driver.run(lambda: self.page.evaluate("window.stop()"))
        except _PLAYWRIGHT_OPERATION_ERRORS:
            logger.debug("Playwright window.stop() returned an error")

    def get_api_log(
        self,
        domain_filter: str | None = None,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not user_id:
            return []
        entries: list[dict[str, Any]] = self._network_log_by_user.get(user_id) or []
        if domain_filter:
            needle = domain_filter.lower()
            entries = [entry for entry in entries if needle in str(entry.get("url", "")).lower()]
        return list(entries)

    def record_network_entry(self, entry: dict[str, Any], *, user_id: str | None = None) -> None:
        owner = user_id
        if not owner:
            logger.debug("Playwright CDP network entry dropped; no owner user_id")
            return
        bucket = self._network_log_by_user.setdefault(owner, [])
        bucket.append(entry)
        if len(bucket) > 200:
            del bucket[:-200]

    def clear_network_log(self, *, user_id: str | None = None) -> None:
        if user_id is None:
            self._network_log_by_user.clear()
            return
        self._network_log_by_user.pop(user_id, None)

    @property
    def active_user_id(self) -> str | None:
        return self._active_user_id

    def bind_user(self, user_id: str | None) -> None:
        self._active_user_id = user_id

    def _command_error(self, method: str, exc: BaseException) -> PlaywrightCDPCommandError:
        text = str(exc).lower()
        if "timeout" in text or "timed out" in text:
            return PlaywrightCDPCommandTimeoutError(method, exc)
        return PlaywrightCDPCommandError(method, exc)


__all__ = [
    "PlaywrightCDPClient",
    "PlaywrightCDPCommandError",
    "PlaywrightCDPCommandTimeoutError",
    "PlaywrightCDPConnectionError",
]
