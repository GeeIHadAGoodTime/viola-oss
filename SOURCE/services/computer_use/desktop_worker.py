"""One thread for desktop control, off Viola's server event loop, with a time limit per action.

Desktop control used to run on the server's own event loop. A Win32 or UI
Automation call into an app that does not answer then froze all of Viola for as
long as the call took, and for good when it never returned: no Stop, no
/health/live, nothing (seen live 2026-11-15: the loop sat inside
GetWindowTextLengthW for a hung window). A 4 second inspect_window was also
logged on the 1.0.5 laptop.

Now every computer-tool action runs on one dedicated worker thread (one
thread, so COM and UI Automation keep one apartment; COM is initialised on it)
with its own event loop, and the tool handler awaits it with a time limit. On
timeout the caller gets a plain failure, the worker is abandoned (left to finish
or hang on its own, never waited for) and the next action gets a fresh one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import itertools
import queue
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_ACTION_TIMEOUT_S = 20.0
# Actions that legitimately take longer than one step: their own duration plus
# this margin, or a fixed allowance.
DURATION_MARGIN_S = 5.0
LAUNCH_APP_TIMEOUT_S = 30.0

_counter = itertools.count(1)


class DesktopActionTimeout(Exception):
    """The desktop action did not finish within its time limit."""

    def __init__(self, seconds: float) -> None:
        super().__init__("desktop action did not finish within %.1f s" % seconds)
        self.seconds = seconds


def _co_initialize() -> None:
    """Join the multithreaded COM apartment (UI Automation clients work from any MTA thread)."""
    try:
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:  # noqa: BLE001, RUF100 - no COM (not Windows) or already initialised
        pass


class _Worker:
    def __init__(self) -> None:
        self.number = next(_counter)
        self.jobs: queue.SimpleQueue[
            tuple[Callable[[], Awaitable[Any]], contextvars.Context, concurrent.futures.Future[Any]] | None
        ] = queue.SimpleQueue()
        self.abandoned = False
        self.thread = threading.Thread(target=self._run, name="viola-desktop-control-%d" % self.number, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        _co_initialize()
        loop = asyncio.new_event_loop()
        # Named, so a timeout log can show what a helper thread of this worker was stuck in.
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                thread_name_prefix="viola-desktop-control-%d-io" % self.number, initializer=_co_initialize
            )
        )
        asyncio.set_event_loop(loop)
        try:
            while not self.abandoned:
                job = self.jobs.get()
                if job is None:
                    break
                factory, context, future = job
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    task = loop.create_task(factory(), context=context)
                    future.set_result(loop.run_until_complete(task))
                except BaseException as exc:  # noqa: BLE001 - handed to the awaiting caller
                    future.set_exception(exc)
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001, RUF100 - a loop left busy by an abandoned job
                pass

    def submit(self, factory: Callable[[], Awaitable[Any]]) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self.jobs.put((factory, contextvars.copy_context(), future))
        return future


_lock = threading.Lock()
_worker: _Worker | None = None


def _current_worker() -> _Worker:
    global _worker
    with _lock:
        if _worker is None or _worker.abandoned or not _worker.thread.is_alive():
            _worker = _Worker()
        return _worker


def _abandon(worker: _Worker) -> None:
    """Leave a stuck worker behind; jobs still queued on it move to a fresh one."""
    global _worker
    with _lock:
        if worker.abandoned:
            return
        worker.abandoned = True
        replacement = _Worker()
        _worker = replacement
    moved = 0
    while True:
        try:
            job = worker.jobs.get_nowait()
        except queue.Empty:
            break
        if job is not None:
            replacement.jobs.put(job)
            moved += 1
    logger.warning(
        "Desktop control worker %d abandoned after a timeout; worker %d takes over (%d queued action(s) moved). "
        "It was stuck in: %s",
        worker.number,
        replacement.number,
        moved,
        _where_stuck(worker),
    )


def _where_stuck(worker: _Worker) -> str:
    """The stuck call, as file:line function frames only (no values), so a log names the hung API."""
    import sys
    import traceback

    frames_by_thread = sys._current_frames()
    prefix = "viola-desktop-control-%d" % worker.number
    parts = []
    for thread in threading.enumerate():
        if not thread.name.startswith(prefix):
            continue
        frame = frames_by_thread.get(thread.ident or -1)
        if frame is None:
            continue
        frames = traceback.extract_stack(frame)[-6:]
        parts.append(
            "[%s] %s"
            % (
                thread.name,
                " <- ".join(
                    "%s:%d %s" % (item.filename.replace("\\", "/").rsplit("/", 2)[-1], item.lineno or 0, item.name)
                    for item in reversed(frames)
                ),
            )
        )
    return " | ".join(parts) or "unknown"


async def run_desktop_action(factory: Callable[[], Awaitable[Any]], *, timeout: float) -> Any:
    """Run ``factory()`` on the desktop worker and await it without blocking this loop.

    Raises :class:`DesktopActionTimeout` after ``timeout`` seconds; the worker is
    then abandoned and never waited for.
    """
    worker = _current_worker()
    future = worker.submit(factory)
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout)
    except TimeoutError:
        _abandon(worker)
        raise DesktopActionTimeout(timeout) from None


def action_timeout(action: str, *, duration_ms: int | None = None, ms: int | None = None) -> float:
    """The time limit for one computer action."""
    name = str(action or "").strip().lower()
    if name == "launch_app":
        return LAUNCH_APP_TIMEOUT_S
    if name == "wait":
        own = ms if ms is not None else duration_ms
        return max(DEFAULT_ACTION_TIMEOUT_S, (max(0, int(own or 0)) / 1000.0) + DURATION_MARGIN_S)
    if name in {"drag", "key_hold", "mouse_button_hold", "mouse_move_relative", "mouse_move_angle"}:
        return max(DEFAULT_ACTION_TIMEOUT_S, (max(0, int(duration_ms or 0)) / 1000.0) + DURATION_MARGIN_S)
    return DEFAULT_ACTION_TIMEOUT_S


def reset_for_tests() -> None:
    global _worker
    with _lock:
        if _worker is not None:
            _worker.abandoned = True
            _worker.jobs.put(None)
        _worker = None
