from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger

_logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        _logger.error("Background task failed: %s", exc)


class TaskTracker:
    """Track background asyncio tasks for later cancellation."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def create_task(self, coro: Any) -> asyncio.Task[Any]:
        """Create and track a background task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _cleanup(t: asyncio.Task) -> None:
            self._tasks.discard(t)

        task.add_done_callback(_cleanup)
        task.add_done_callback(_log_task_exception)
        return task

    def cancel_all_nowait(self) -> None:
        """Best-effort cancel for all tracked tasks without awaiting."""
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
