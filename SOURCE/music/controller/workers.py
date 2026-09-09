"""Threaded worker utilities for music playback services."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError
from typing import Any

from core.constants import TIMEOUT_LONG


class ThreadWorker:
    """Lightweight wrapper around a daemon thread with lifecycle helpers."""

    def __init__(
        self,
        *,
        name: str,
        target: Callable[[], None],
        stop_callback: Callable[[], None] | None = None,
        enabled: bool = True,
    ) -> None:
        self._target = target
        self._stop_callback = stop_callback
        self._thread: threading.Thread | None = None
        if enabled:
            self._thread = threading.Thread(target=target, name=name, daemon=True)

    def start(self) -> None:
        if self._thread and not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        if self._stop_callback:
            self._stop_callback()

    def join(self, timeout: float | None = None) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


class PlaybackWorker(ThreadWorker):
    """Worker wrapper for the playback loop."""

    def __init__(self, *, target: Callable[[], None], enabled: bool) -> None:
        super().__init__(name="MusicWorker", target=target, enabled=enabled)


class IntegrityMonitorWorker(ThreadWorker):
    """Worker wrapper for the integrity monitor loop."""

    def __init__(
        self,
        *,
        target: Callable[[], None],
        stop_callback: Callable[[], None],
        enabled: bool,
    ) -> None:
        super().__init__(
            name="MusicIntegrity",
            target=target,
            stop_callback=stop_callback,
            enabled=enabled,
        )


class BackgroundResolverWorker:
    """
    Runs BackgroundResolver work on a dedicated asyncio loop to avoid blocking.
    """

    def __init__(self, resolver: Any) -> None:
        self._resolver = resolver
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="BackgroundResolverWorker",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def resolve(self, query: str, source: Any, *, timeout: float = TIMEOUT_LONG) -> Any:
        if self._resolver is None:
            return None

        async def _resolve_async() -> Any:
            return await self._resolver.resolve(query, source)

        future = asyncio.run_coroutine_threadsafe(_resolve_async(), self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            return None

    def stop(self, *, join_timeout: float = 1.0) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(join_timeout)
