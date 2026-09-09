"""Keep MCP memory transport cancel scopes inside one dedicated owner task."""

from __future__ import annotations

import asyncio
from typing import Any

from core.constants import TIMEOUT_LONG, TIMEOUT_MINUTE
from core.logging_config import get_logger

logger = get_logger(__name__)


class InProcessConnection:
    """Own a memory session from startup through disconnect, across HTTP requests.

    MCP's memory transport enters AnyIO task groups. Their cancel scopes cannot
    remain attached to the request that happens to initialize a persistent hub.
    Only this connection's owner enters and exits those contexts; request tasks
    receive the initialized session and signal its owner when disconnecting.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._stop = asyncio.Event()
        self._owner: asyncio.Task | None = None

    async def start(self, server: Any) -> Any:
        ready = asyncio.get_running_loop().create_future()
        self._owner = asyncio.create_task(self._run(server, ready), name="mcp-inprocess-owner:%s" % self.name)
        try:
            return await asyncio.wait_for(ready, timeout=TIMEOUT_MINUTE)
        except BaseException:
            # Failed or cancelled initialization must finish its own teardown;
            # it must not leave a live server or clear the caller's cancellation.
            self._owner.cancel()
            await asyncio.gather(self._owner, return_exceptions=True)
            if ready.done() and not ready.cancelled():
                ready.exception()
            raise

    async def _run(self, server: Any, ready: asyncio.Future) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        try:
            # This helper already initializes the session. Both scope entry and
            # scope exit happen in this task, including startup failure paths.
            async with create_connected_server_and_client_session(server) as session:
                if not ready.done():
                    ready.set_result(session)
                await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(
                    exc
                    if isinstance(exc, Exception)
                    else RuntimeError("In-process MCP startup cancelled: " + self.name)
                )
            elif not isinstance(exc, asyncio.CancelledError):
                logger.exception("In-process MCP owner failed for '%s'", self.name)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise

    async def close(self) -> None:
        if self._owner is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._owner), timeout=TIMEOUT_LONG)
        except TimeoutError:
            self._owner.cancel()
            await asyncio.gather(self._owner, return_exceptions=True)
        except asyncio.CancelledError:
            self._owner.cancel()
            await asyncio.gather(self._owner, return_exceptions=True)
            raise
