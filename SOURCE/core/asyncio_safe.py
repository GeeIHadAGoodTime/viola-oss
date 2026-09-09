"""Small helpers for closed-event-loop safe async interop.

The Postgres/asyncpg path binds pools and locks to the event loop that created
them. Synchronous callers that bridge into async code from agent/UI threads must
avoid reusing stale loop-bound singletons after that loop has closed.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import os
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class SyncBridgeLoopError(RuntimeError):
    """Raised when a sync->async bridge is attempted from the loop it targets.

    ``run_async_synchronously`` dispatches the coroutine to a specific loop
    (the cloud ``_MAIN_LOOP`` or the worker loop) and then blocks the calling
    thread on the result. When the caller is ALREADY running on that same loop
    (e.g. cloud serving-loop code calling a synchronous SettingsManager API),
    blocking would deadlock the loop on itself. We raise instead.

    It subclasses ``RuntimeError`` so existing ``except RuntimeError`` /
    ``except Exception`` handlers still catch it, but a caller that can degrade
    gracefully on the serving loop (e.g. fall back to defaults without logging a
    per-turn ERROR) can catch this specific type first. See #709.
    """


_LOOP_CLOSED_TEXT = "Event loop is closed"
_WORKER_LOOP_LOCK = threading.Lock()
_WORKER_LOOP: asyncio.AbstractEventLoop | None = None
_WORKER_THREAD: threading.Thread | None = None

# Optional reference to the FastAPI lifespan/main loop. When set, sync->async
# bridges dispatch to this loop instead of spinning up a separate worker loop.
# This prevents asyncpg pools from being created on a side loop and overwriting
# main-loop pools (which corrupted the shared pool and caused cross-loop errors
# on subsequent main-loop requests). Set via ``set_main_loop()`` from
# ``cloud_app`` lifespan startup.
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Capture the FastAPI lifespan loop so sync->async bridges dispatch to it.

    Call from cloud lifespan startup (``asyncio.get_running_loop()``). Pass
    ``None`` on shutdown so subsequent calls fall back to the worker loop.
    """
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    """Return the captured main loop, if any."""
    return _MAIN_LOOP


def is_event_loop_closed_error(exc: BaseException) -> bool:
    """Return True when *exc* or its causal chain is the closed-loop failure."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RuntimeError) and _LOOP_CLOSED_TEXT in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _run_worker_loop(ready: threading.Event, holder: dict[str, asyncio.AbstractEventLoop]) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    holder["loop"] = loop
    ready.set()
    try:
        loop.run_forever()
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    global _WORKER_LOOP, _WORKER_THREAD

    with _WORKER_LOOP_LOCK:
        if (
            _WORKER_LOOP is not None
            and not _WORKER_LOOP.is_closed()
            and _WORKER_THREAD is not None
            and _WORKER_THREAD.is_alive()
        ):
            return _WORKER_LOOP

        ready = threading.Event()
        holder: dict[str, asyncio.AbstractEventLoop] = {}
        thread = threading.Thread(
            target=_run_worker_loop,
            args=(ready, holder),
            name="viola-asyncio-safe-worker",
            daemon=True,
        )
        thread.start()
        ready.wait(timeout=5)
        loop = holder.get("loop")
        if loop is None:
            raise RuntimeError("Failed to start asyncio worker loop")
        _WORKER_LOOP = loop
        _WORKER_THREAD = thread
        return loop


def shutdown_asyncio_worker_loop() -> None:
    """Stop the shared sync-to-async worker loop.

    Runtime shutdown should close loop-bound resources such as asyncpg pools
    before calling this. The atexit path exists only to avoid leaving a daemon
    loop running during interpreter teardown.
    """

    global _WORKER_LOOP, _WORKER_THREAD

    with _WORKER_LOOP_LOCK:
        loop = _WORKER_LOOP
        thread = _WORKER_THREAD
        _WORKER_LOOP = None
        _WORKER_THREAD = None

    if loop is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2)


_WORKER_LOOP_FALLBACK_WARNED = False


def _is_cloud_surface() -> bool:
    return (os.environ.get("VIOLA_APP_SURFACE") or "desktop").strip().lower() == "cloud"


def _run_on_worker_loop(
    coro: Coroutine[Any, Any, T],
    *,
    timeout: float | None,
    timeout_result: Any,
    timeout_log_message: str | None,
    logger: Any | None,
    prefer_isolated_loop: bool = False,
) -> T | Any:
    # Desktop serving-loop starvation fix (#560). A pure-background coroutine
    # (e.g. the memory side-query's second LLM round trip) that is dispatched
    # onto the desktop SERVING loop can be starved to its full timeout: on
    # desktop, ``ai_controller.build_frames`` runs synchronously ON the serving
    # loop thread and ``RuntimeContextRegistry.build_all(concurrent memory)``
    # then blocks that same thread on ``future.result(...)``. A coroutine
    # queued on that loop can never run while the loop thread is parked in that
    # wait, so ``run_coroutine_threadsafe(...).result(timeout=15)`` always times
    # out at 15 s (observed: LOOP_LAG ~15.6 s, zero side-query HTTP). Callers
    # that are self-contained background work -- they never touch the serving
    # loop's request-scoped resources -- opt into the isolated worker loop so
    # they run independently of whatever blocks the serving loop.
    #
    # Cloud KEEPS the serving loop (``prefer_isolated_loop`` ignored): the
    # deadlock does not occur there (build runs off the serving loop, CL-2d17),
    # and the spend/rate-limit asyncpg pool is bound to the serving loop (#419).
    if prefer_isolated_loop and not _is_cloud_surface():
        target_loop: asyncio.AbstractEventLoop | None = _get_worker_loop()
        using_fallback = False
    else:
        # Prefer the main FastAPI loop when it has been captured. This keeps
        # asyncpg pools (and any other loop-bound resources) on a single loop
        # across all sync->async bridges, which is required for the cloud
        # /v1/command + /billing path. Fall back to the dedicated worker loop
        # when no main loop is registered (desktop, CLI, tests).
        target_loop = _MAIN_LOOP
        using_fallback = False
        if target_loop is None or target_loop.is_closed() or not target_loop.is_running():
            if _is_cloud_surface():
                coro.close()
                raise RuntimeError(
                    "Cloud sync-to-async bridge requires a registered running main event loop. "
                    "Call core.asyncio_safe.set_main_loop() from cloud lifespan startup before "
                    "using sync wrappers around Postgres-backed async resources."
                )
            target_loop = _get_worker_loop()
            using_fallback = True

    if using_fallback:
        # Cloud surfaces MUST register the main loop at lifespan startup
        # (``backend/cloud_app.lifespan`` calls ``set_main_loop``). If a
        # cloud sync->async bridge falls through to the worker loop, the
        # asyncpg pool on the main loop will get overwritten by a parallel
        # one created on the worker — recreating ASYNC-1's cross-loop bug.
        # Warn loudly the first time this happens so the regression is
        # visible in production logs.
        global _WORKER_LOOP_FALLBACK_WARNED
        if not _WORKER_LOOP_FALLBACK_WARNED:
            _WORKER_LOOP_FALLBACK_WARNED = True
            if logger is not None:
                logger.warning(
                    "asyncio_safe falling back to worker loop (no main loop registered, "
                    "surface=%s). Cloud surfaces should call core.asyncio_safe.set_main_loop() "
                    "at lifespan startup to avoid asyncpg cross-loop pool corruption (ASYNC-1).",
                    (os.environ.get("VIOLA_APP_SURFACE") or "desktop").strip().lower(),
                )

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is target_loop:
        coro.close()
        raise SyncBridgeLoopError("Cannot synchronously wait on the shared asyncio worker loop from itself")

    future = asyncio.run_coroutine_threadsafe(coro, target_loop)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        if logger is not None and timeout_log_message:
            logger.warning(timeout_log_message, timeout)
        return timeout_result


def reset_worker_loop_fallback_warned_for_tests() -> None:
    """Reset the one-shot fallback warning. Test-only hook."""
    global _WORKER_LOOP_FALLBACK_WARNED
    _WORKER_LOOP_FALLBACK_WARNED = False


def run_async_synchronously(
    coro: Coroutine[Any, Any, T],
    *,
    timeout: float | None = None,
    timeout_result: Any = None,
    timeout_log_message: str | None = None,
    logger: Any | None = None,
    prefer_isolated_loop: bool = False,
) -> T | Any:
    """Run *coro* from sync code, including when the caller is already on a loop.

    Existing stores use this bridge for sync public APIs over async Postgres
    repositories. The coroutine runs on a shared worker loop so loop-bound
    resources, especially asyncpg pools, are reused instead of recreated for
    every sync call. ``timeout`` preserves the older bounded-wait behavior used
    by user-model and preference lookups.

    ``prefer_isolated_loop`` (#560): opt a self-contained background coroutine
    onto the dedicated isolated worker loop instead of the desktop serving loop,
    so it cannot be starved by a serving-loop thread that is itself blocked
    waiting on this coroutine's result. Ignored on the cloud surface, which must
    keep serving-loop dispatch for asyncpg pool reuse (#419). Use only for work
    that never touches the serving loop's request-scoped, loop-bound resources.
    """
    return _run_on_worker_loop(
        coro,
        timeout=timeout,
        timeout_result=timeout_result,
        timeout_log_message=timeout_log_message,
        logger=logger,
        prefer_isolated_loop=prefer_isolated_loop,
    )


atexit.register(shutdown_asyncio_worker_loop)


__all__ = [
    "SyncBridgeLoopError",
    "get_main_loop",
    "is_event_loop_closed_error",
    "run_async_synchronously",
    "set_main_loop",
    "shutdown_asyncio_worker_loop",
]
