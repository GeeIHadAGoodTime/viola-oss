"""
Async Helper Utilities
======================
Threading and async utilities for preventing event loop conflicts.

Key Features:
- Safe cross-thread async execution
- Event loop detection and management
- Background task scheduling
- Thread-safe callbacks

Part of the unified service architecture for NOVVIOLA.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from contextlib import suppress
from typing import Any, TypeVar, cast

from core.constants import TIMEOUT_DEFAULT
from core.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


def _ensure_coroutine(awaitable: Awaitable[T]) -> Coroutine[Any, Any, T]:
    """Convert an awaitable to a coroutine for consistent scheduling."""
    if asyncio.iscoroutine(awaitable):
        return cast(Coroutine[Any, Any, T], awaitable)

    async def _runner() -> T:
        return await awaitable

    return _runner()


# ---------- Event Loop Utilities ----------


def get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """
    Get the current event loop or create one if none exists.

    This is useful for threads that need to run async code.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop


def run_async_from_thread(
    coro: Awaitable[T],
    loop: asyncio.AbstractEventLoop | None = None,
    timeout: float | None = None,
) -> T:
    """
    Run an async coroutine from a non-async thread safely.

    Args:
        coro: Coroutine to execute
        loop: Event loop to use (None = run in a fresh loop)
        timeout: Optional timeout in seconds

    Returns:
        Result of coroutine

    Raises:
        RuntimeError: If called from within an active event loop without providing a target loop
        TimeoutError: If timeout exceeded
    """
    if loop is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if timeout is not None:
                return asyncio.run(asyncio.wait_for(_ensure_coroutine(coro), timeout))
            return asyncio.run(_ensure_coroutine(coro))
        raise RuntimeError(
            "run_async_from_thread cannot be used inside a running event loop; await the coroutine instead."
        )

    if not loop.is_running():
        raise RuntimeError("Target event loop must be running to execute coroutine")

    scheduled = asyncio.wait_for(_ensure_coroutine(coro), timeout) if timeout is not None else _ensure_coroutine(coro)
    future = asyncio.run_coroutine_threadsafe(scheduled, loop)
    return future.result(timeout=timeout)


def schedule_in_loop(
    coro: Awaitable[T],
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[T], None] | None = None,
) -> asyncio.Task[T]:
    """
    Schedule a coroutine to run in a specific event loop (thread-safe).

    Args:
        coro: Coroutine to schedule
        loop: Target event loop
        callback: Optional callback when coroutine completes

    Returns:
        The created asyncio.Task
    """
    if not loop.is_running():
        raise RuntimeError("Target event loop must be running to schedule tasks")

    holder: Future[asyncio.Task[T]] = Future()

    def _log_task_exception(t: asyncio.Task[T]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Fire-and-forget task failed: %s", exc)

    def _create_task() -> None:
        task = loop.create_task(_ensure_coroutine(coro))
        task.add_done_callback(_log_task_exception)

        if callback:

            def _invoke(task_result: asyncio.Task[T]) -> None:
                if task_result.cancelled():
                    return
                try:
                    result = task_result.result()
                except Exception as e:
                    logger.exception("Async task failed in schedule_in_loop")
                    return
                try:
                    if callback is not None:
                        callback(result)
                except Exception as e:
                    logger.exception("Callback raised in schedule_in_loop")

            task.add_done_callback(_invoke)

        holder.set_result(task)

    loop.call_soon_threadsafe(_create_task)
    try:
        return holder.result(timeout=TIMEOUT_DEFAULT)
    except TimeoutError:
        logger.warning("schedule_in_loop timed out after %ss", TIMEOUT_DEFAULT)
        raise


# ---------- Thread-Safe Async Callbacks ----------


class AsyncCallbackBridge:
    """
    Bridge for calling async code from synchronous callbacks.

    This solves the wake word callback problem where we need to run async code
    from a thread that doesn't have an event loop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self._loop = loop
        self._loop_lock = threading.Lock()
        self._loop_thread: threading.Thread | None = None

    def _start_loop_thread(self, loop: asyncio.AbstractEventLoop) -> None:
        def _runner() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="async-callback-loop", daemon=True)
        thread.start()
        self._loop_thread = thread

        # Wait briefly for the loop to start running
        # This ensures that run_coroutine_threadsafe will work immediately
        import time

        for _ in range(10):  # Wait up to 100ms
            if loop.is_running():
                break
            time.sleep(0.01)

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Get or detect the event loop."""
        with self._loop_lock:
            if self._loop and self._loop.is_closed():
                self._loop = None
                self._loop_thread = None

            if self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    self._loop = asyncio.new_event_loop()
                    self._start_loop_thread(self._loop)

            return self._loop

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Explicitly set the loop used for scheduling."""
        with self._loop_lock:
            self._loop = loop
            self._loop_thread = None

    def schedule(self, coro: Awaitable[T], callback: Callable[[T], None] | None = None) -> None:
        """Schedule an async coroutine from a sync context."""
        try:
            loop = self._loop

            # If we have a loop but it's not running, we need to start it
            if loop is not None and not loop.is_running():
                logger.warning("[AsyncBridge] Event loop exists but not running. Starting background thread...")
                self._start_loop_thread(loop)
                # Wait for loop to start
                import time

                for _ in range(50):  # Wait up to 500ms
                    if loop.is_running():
                        break
                    time.sleep(0.01)
                if not loop.is_running():
                    logger.error("[AsyncBridge] Failed to start event loop in background thread!")

            # Now ensure we have a running loop
            loop = self.ensure_loop()

            # Check if loop is running - if so, use schedule_in_loop
            if loop.is_running():
                logger.debug("[AsyncBridge] Scheduling coroutine in running loop")
                schedule_in_loop(coro, loop, callback)
            else:
                # This should not happen after the above fix, but handle it gracefully
                logger.error(
                    "[AsyncBridge] Loop still not running after ensure_loop()! "
                    "Coroutine will not execute. This is a bug."
                )
                # Try one more time with run_coroutine_threadsafe as a fallback
                future = asyncio.run_coroutine_threadsafe(_ensure_coroutine(coro), loop)

                def _log_future_exception(_future):
                    if _future.cancelled():
                        return
                    try:
                        exc = _future.exception()
                    except Exception:
                        return
                    if exc:
                        logger.error("Fallback scheduled task failed: %s", exc)

                future.add_done_callback(_log_future_exception)

                # If callback is provided, add it as a done callback
                if callback:

                    def _invoke(_future):
                        if _future.cancelled():
                            return
                        try:
                            result = _future.result()
                            callback(result)
                        except Exception as e:
                            logger.exception("Callback raised in schedule")

                    future.add_done_callback(_invoke)
        except Exception as exc:
            logger.error("Failed to schedule async callback: %s", exc)
            import traceback

            logger.error("Stack trace: %s", traceback.format_exc())

    def schedule_and_wait(self, coro: Awaitable[T], timeout: float | None = None) -> T:
        """Schedule an async coroutine and wait for result (blocking)."""
        loop = self.ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            (_ensure_coroutine(coro) if timeout is None else asyncio.wait_for(_ensure_coroutine(coro), timeout)),
            loop,
        )
        return future.result(timeout=timeout)


# ---------- Background Task Manager ----------


class BackgroundTaskManager:
    """
    Manager for long-running background tasks.

    Features:
    - Task registration and lifecycle management
    - Graceful cancellation
    - Task health monitoring
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    async def start_task(self, name: str, coro: Awaitable[T], replace: bool = True) -> asyncio.Task[T]:
        """Start a background task."""
        async with self._lock:
            if replace:
                await self.cancel_task(name)

            task = asyncio.create_task(_ensure_coroutine(coro), name=name)
            self._tasks[name] = task
            logger.debug("Started background task: %s", name)
            return task

    async def cancel_task(self, name: str, wait: bool = True) -> bool:
        """Cancel a background task."""
        async with self._lock:
            task = self._tasks.pop(name, None)
            if task is None:
                return False

            if not task.done():
                task.cancel()
                if wait:
                    with suppress(asyncio.CancelledError):
                        await task

            logger.debug("Cancelled background task: %s", name)
            return True

    async def cancel_all(self, wait: bool = True) -> None:
        """Cancel all background tasks."""
        async with self._lock:
            for name in list(self._tasks.keys()):
                await self.cancel_task(name, wait=wait)

    def get_task_names(self) -> list[str]:
        """Get names of all active tasks."""
        return list(self._tasks.keys())

    def is_running(self, name: str) -> bool:
        """Check if a task is running."""
        task = self._tasks.get(name)
        return bool(task and not task.done())


# ---------- Decorator for Safe Async Callbacks ----------


def safe_async_callback(func: Callable[..., Awaitable[T]]) -> Callable[..., None]:
    """
    Decorator to make async callbacks safe to call from sync code.

    This prevents "asyncio.run() cannot be called from a running event loop" errors.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_ensure_coroutine(func(*args, **kwargs)))
            return

        task = loop.create_task(_ensure_coroutine(func(*args, **kwargs)))

        def _on_complete(done: asyncio.Task[T]) -> None:
            if done.cancelled():
                return
            if done.exception():
                logger.exception("Async callback raised an exception")

        task.add_done_callback(_on_complete)

    return wrapper


# ---------- Global bridge instance ----------

_global_bridge: AsyncCallbackBridge | None = None


def get_async_bridge() -> AsyncCallbackBridge:
    """Get the global async callback bridge."""
    global _global_bridge
    if _global_bridge is None:
        _global_bridge = AsyncCallbackBridge()
    return _global_bridge


def set_async_bridge_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Set the event loop for the global async bridge."""
    bridge = get_async_bridge()
    bridge.set_loop(loop)
