"""Local CDP relay that lets Playwright attach to Qt WebEngine.

Why this exists
---------------
Playwright's ``chromium.connect_over_cdp()`` always configures browser-level
download behavior while attaching.  The path is entirely inside Playwright's
bundled Node driver and has no public opt-out:

* ``server/chromium/chromium.js`` ``_connectOverCDPImpl`` builds the default
  context as ``persistent = {noDefaultViewport: true}`` and calls
  ``validateBrowserContextOptions(persistent, browserOptions)``.
* ``server/browserContext.js`` ``validateBrowserContextOptions`` then defaults
  ``acceptDownloads`` to ``"accept"`` for every browser whose name is not
  ``"electron"`` -- and the CDP connect path hardcodes ``name: "chromium"``.
* ``server/chromium/crBrowser.js`` ``CRBrowserContext._initialize`` sends
  ``Browser.setDownloadBehavior`` unless ``acceptDownloads`` is
  ``"internal-browser-default"``, which only the Electron/launchApp paths set.

Qt WebEngine implements a subset of the DevTools protocol and has no
browser-context management at all, so it answers that command with
``Browser context management is not supported``.  Playwright treats a failed
context initialize as a failed attach, so ``connect_over_cdp`` raises and the
whole interactive-browser surface on the desktop app dies with it -- browsing,
form filling, and the agent's screen vision alike, because the perception layer
is only constructed once the attach succeeds.

Rather than reintroduce a hand-rolled raw-CDP client (deleted in 1a1321f86 when
desktop CDP was consolidated onto Playwright), this module interposes a tiny
loopback WebSocket relay between Playwright and Qt WebEngine.  Every frame is
forwarded verbatim in both directions; exactly one browser-level command is
answered locally.

Answering it locally is truthful rather than a papering-over: the command
configures where Chromium writes downloaded files for a browser context, Qt
WebEngine has no such context to configure, and Viola's agent never drives
Playwright's download API on this surface.  A relay measurement against the
live Qt WebEngine endpoint confirmed this is the *only* command Playwright's
attach and subsequent page driving send that Qt WebEngine rejects: with it
shimmed, navigation, accessibility snapshots, evaluation, clicking, typing and
per-page CDP sessions all work against real sites, with zero other protocol
errors.

The relay binds to loopback only and fronts an endpoint that is already
loopback-only and unauthenticated, so it widens no trust boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# The single browser-level command Qt WebEngine cannot answer.  Keep this set
# minimal and explicit: each entry is a measured incompatibility, not a guess.
_LOCALLY_ANSWERED_METHODS = frozenset({"Browser.setDownloadBehavior"})

_BRIDGE_HOST = "127.0.0.1"
_UPSTREAM_HOST = "127.0.0.1"


class QtCDPBridgeError(RuntimeError):
    """The relay could not be established in front of the Qt WebEngine endpoint."""


async def _resolve_upstream_ws(port: int, timeout: float) -> str:
    """Read the browser-level WebSocket URL from the DevTools HTTP endpoint."""
    import httpx

    url = "http://%s:%d/json/version" % (_UPSTREAM_HOST, port)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    ws_url = str(payload.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        raise QtCDPBridgeError("DevTools endpoint on port %d did not advertise a browser WebSocket" % port)
    return ws_url


class QtWebEngineCDPBridge:
    """Forwards CDP between Playwright and Qt WebEngine, shimming one command.

    Start it with :meth:`start`, point ``connect_over_cdp`` at
    :attr:`endpoint`, and always :meth:`stop` it when the Playwright
    connection is torn down.
    """

    def __init__(self, upstream_port: int, *, connect_timeout: float = 10.0) -> None:
        self._upstream_port = upstream_port
        self._connect_timeout = connect_timeout
        self._server: Any | None = None
        self._upstream_ws_url = ""
        self._port = 0
        self._sessions: set[asyncio.Task[None]] = set()

    @property
    def endpoint(self) -> str:
        """The ``ws://`` URL Playwright should connect to."""
        if not self._port:
            raise QtCDPBridgeError("Qt CDP bridge is not running")
        return "ws://%s:%d" % (_BRIDGE_HOST, self._port)

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> str:
        """Resolve the upstream endpoint, bind a loopback relay, return its URL."""
        try:
            from websockets.asyncio.server import serve
        except ImportError as exc:  # pragma: no cover - dependency is pinned for desktop
            raise QtCDPBridgeError(
                "The websockets package is required to drive the built-in browser. Please reinstall Viola."
            ) from exc

        self._upstream_ws_url = await _resolve_upstream_ws(self._upstream_port, self._connect_timeout)
        # A relay must be transparent: no frame size ceiling (CDP screenshot and
        # accessibility payloads are megabytes), no compression the two real
        # endpoints did not ask for, and no keepalive policy of its own -- an
        # injected ping timeout would tear down a browsing session that
        # Playwright and Qt WebEngine both still consider healthy.
        self._server = await serve(
            self._handle_client,
            _BRIDGE_HOST,
            0,
            max_size=None,
            ping_interval=None,
            compression=None,
        )
        self._port = self._bound_port(self._server)
        logger.info(
            "Qt WebEngine CDP bridge listening on %s (upstream port %d)",
            self.endpoint,
            self._upstream_port,
        )
        return self.endpoint

    @staticmethod
    def _bound_port(server: Any) -> int:
        for sock in getattr(server, "sockets", None) or []:
            addr = sock.getsockname()
            if isinstance(addr, tuple) and len(addr) >= 2:
                return int(addr[1])
        raise QtCDPBridgeError("Qt CDP bridge did not bind a port")

    async def stop(self) -> None:
        """Close the relay and every in-flight forwarding session.

        Idempotent, and safe to call from a teardown path that is itself
        unwinding an error: nothing in here is allowed to raise past the
        caller, because the caller is usually already reporting a failure.
        """
        server, self._server = self._server, None
        self._port = 0
        current = asyncio.current_task()
        sessions = [task for task in self._sessions if task is not current]
        self._sessions.clear()
        for task in sessions:
            task.cancel()
        if sessions:
            await asyncio.gather(*sessions, return_exceptions=True)
        if server is not None:
            with contextlib.suppress(Exception):
                server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

    async def _handle_client(self, client: Any) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._sessions.add(task)
        try:
            await self._relay(client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001, RUF100 - one relay session dying must never take down the listener
            logger.debug("Qt WebEngine CDP bridge session ended with an error", exc_info=True)
        finally:
            if task is not None:
                self._sessions.discard(task)

    async def _relay(self, client: Any) -> None:
        from websockets.asyncio.client import connect

        async with connect(
            self._upstream_ws_url,
            max_size=None,
            ping_interval=None,
            compression=None,
        ) as upstream:
            downstream = asyncio.create_task(self._pump_upstream_to_client(upstream, client))
            try:
                await self._pump_client_to_upstream(client, upstream)
            finally:
                downstream.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await downstream

    async def _pump_client_to_upstream(self, client: Any, upstream: Any) -> None:
        async for raw in client:
            reply = _local_answer(raw)
            if reply is not None:
                await client.send(reply)
                continue
            await upstream.send(raw)

    @staticmethod
    async def _pump_upstream_to_client(upstream: Any, client: Any) -> None:
        async for raw in upstream:
            await client.send(raw)


def _local_answer(raw: Any) -> str | None:
    """Return a CDP reply for a locally-answered command, else ``None`` to forward.

    Only well-formed JSON commands carrying an integer ``id`` and a method in
    :data:`_LOCALLY_ANSWERED_METHODS` are answered here; everything else -- and
    anything unparseable -- is forwarded untouched so no other behavior is
    altered.
    """
    if not isinstance(raw, str):
        return None
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    if message.get("method") not in _LOCALLY_ANSWERED_METHODS:
        return None
    message_id = message.get("id")
    if not isinstance(message_id, int):
        return None
    reply: dict[str, Any] = {"id": message_id, "result": {}}
    session_id = message.get("sessionId")
    if session_id:
        reply["sessionId"] = session_id
    logger.debug("Qt WebEngine CDP bridge answered %s locally", message.get("method"))
    return json.dumps(reply)


__all__ = ["QtCDPBridgeError", "QtWebEngineCDPBridge"]
