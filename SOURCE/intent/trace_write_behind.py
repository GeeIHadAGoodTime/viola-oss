"""Write-behind async wrapper that moves trace I/O off the agent turn's path.

`TaskTraceWriter` does real work on every ``append_*`` call: it sanitizes the
payload, runs the recursive blob-ref promotion pass, zstd-compresses, Fernet-
encrypts, and appends to disk. Historically all of that ran *synchronously on
the event loop* -- the pre-provider ``trace_llm_attempt_start`` write delayed
the model call from even starting (~0.5-1.9s observed), and the post-provider
``trace_step`` write delayed the answer's return to the user (~1.1-5.6s). The
model never reads the trace; the user should never wait on it.

`AsyncTaskTraceWriter` wraps a synchronous `TaskTraceWriter` and offloads every
``append_*`` call to a single ordered background tail: each emit is snapshotted
(so later loop mutations cannot corrupt the record), then scheduled as an
asyncio task chained on the previous emit. The chained task awaits its
predecessor and then runs the real synchronous writer method on a worker thread
via ``asyncio.to_thread`` -- so the sanitize / blob / compress / encrypt / flush
work happens off the event loop while ordering within a task is preserved.

This is the same ordered-tail shape proven for the phone path in
``telephony.traced_openai_responses_llm_service`` (gate
``phone-llm-trace-nonblocking``), generalized to wrap the writer so every
agent-loop call site (``agent_executor._task_trace.*``) is offloaded without a
call-site change.

Durability trade (SURFACED, not silently taken): a synchronous flush meant a
crash mid-turn preserved the trace up to the last flushed event. With write-
behind, a hard process kill (SIGKILL / power loss) in the window between the
answer returning and the tail draining loses that turn's un-flushed trace tail
(typically the final ``trace_step`` + ``trace_complete``). The queue is bounded
by the turn's own event count and strictly ordered, and `drain()` gives any
consumer a bounded, awaitable completeness barrier; but an abnormal exit inside
the write-behind window is a real, accepted loss of the most-recent tail. Steady
state and clean shutdown lose nothing.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING, Any, Callable

from core.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from intent.task_trace import TaskTraceWriter

logger = get_logger(__name__)

# Strong references to in-flight tail tasks. asyncio.create_task keeps no strong
# reference of its own, so a fire-and-forget trace task whose only referrer is a
# soon-GC'd executor can be collected mid-flight and silently cancelled. Holding
# each task here until it finishes guarantees the tail drains autonomously even
# after run_agent_loop returns and the executor is dropped -- which is exactly
# what makes read-your-writes hold for a consumer reading seconds later.
_INFLIGHT_TAIL_TASKS: set[asyncio.Future[Any]] = set()


def _trace_safe_copy(value: Any) -> Any:
    """Freeze a payload at enqueue time so later loop mutations cannot corrupt it.

    Deep-copies mutable structures; immutable scalars (str/int/bool/None) copy in
    O(1). Falls back to a JSON round-trip and finally a type marker for objects
    that resist ``deepcopy`` (mirrors the executor's own trace snapshot helper),
    so a single un-copyable object never breaks a trace write.
    """
    try:
        return copy.deepcopy(value)
    except (TypeError, ValueError, AttributeError, RuntimeError, RecursionError):
        try:
            return json.loads(
                json.dumps(
                    value,
                    default=lambda item: "<%s>" % type(item).__name__,
                    ensure_ascii=True,
                )
            )
        except (TypeError, ValueError, AttributeError, RuntimeError, RecursionError):
            return "<%s>" % type(value).__name__


class AsyncTaskTraceWriter:
    """Ordered write-behind facade over a synchronous `TaskTraceWriter`.

    Every ``append_*`` method is intercepted (via ``__getattr__``), its arguments
    snapshotted, and the real call scheduled on the ordered background tail. Read
    accessors (``path``, ``task_id``, ``schema_version``) and control methods
    (``disable``) pass straight through to the wrapped writer. ``drain()`` awaits
    the tail so a consumer that needs read-your-writes has a bounded barrier.
    """

    __slots__ = ("_scheduler_cache", "_tail", "_writer")

    def __init__(self, writer: TaskTraceWriter) -> None:
        self._writer = writer
        self._tail: asyncio.Future[None] | None = None
        self._scheduler_cache: dict[str, Callable[..., None]] = {}

    # --- pass-through read/control surface -------------------------------

    @property
    def path(self) -> Path:
        return self._writer.path

    @property
    def task_id(self) -> str:
        return self._writer.task_id

    @property
    def schema_version(self) -> int:
        return self._writer.schema_version

    @property
    def user_id(self) -> str:
        return self._writer.user_id

    @property
    def sync_writer(self) -> TaskTraceWriter:
        """The wrapped synchronous writer (for tests/diagnostics only)."""
        return self._writer

    async def warm_up_keys_async(self) -> None:
        """Pre-resolve trace + blob keys on-path (one-time, cheap) before writes.

        Runs on the caller's loop so the wrapped writer's cached Fernet is
        populated before any worker-thread write, keeping the first background
        write from paying key-unwrap latency.
        """
        await self._writer.warm_up_keys_async()

    def disable(self, reason: str) -> None:
        """Disable the wrapped writer immediately (used on setup failure)."""
        self._writer.disable(reason)

    # --- ordered write-behind scheduling ---------------------------------

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names not found normally, so the explicit
        # surface above (and __slots__) is never shadowed. Every append_* call
        # is offloaded; any other writer attribute passes straight through.
        if name.startswith("append_"):
            cached = self._scheduler_cache.get(name)
            if cached is None:
                cached = self._make_scheduler(name)
                self._scheduler_cache[name] = cached
            return cached
        return getattr(self._writer, name)

    def _make_scheduler(self, method_name: str) -> Callable[..., None]:
        def _scheduled(*args: Any, **kwargs: Any) -> None:
            safe_args = tuple(_trace_safe_copy(arg) for arg in args)
            safe_kwargs = {key: _trace_safe_copy(value) for key, value in kwargs.items()}
            self._schedule(method_name, safe_args, safe_kwargs)

        _scheduled.__name__ = "scheduled_%s" % method_name
        return _scheduled

    def _schedule(self, method_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (synchronous/test context): run inline so trace
            # writes are never silently dropped outside an async turn.
            self._run_writer_method(method_name, args, kwargs)
            return
        previous = self._tail
        task = loop.create_task(
            self._run_chained(previous, method_name, args, kwargs),
            name="agent-trace-write-behind",
        )
        _INFLIGHT_TAIL_TASKS.add(task)
        task.add_done_callback(_INFLIGHT_TAIL_TASKS.discard)
        self._tail = task

    async def _run_chained(
        self,
        previous: asyncio.Future[None] | None,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        if previous is not None:
            results = await asyncio.gather(previous, return_exceptions=True)
            if results and isinstance(results[0], BaseException):
                logger.debug("prior trace write-behind emit failed: %s", results[0])
        await asyncio.to_thread(self._run_writer_method, method_name, args, kwargs)

    def _run_writer_method(self, method_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        try:
            getattr(self._writer, method_name)(*args, **kwargs)
        except Exception:  # noqa: BLE001, RUF100 - background trace write must never crash the turn; log and swallow
            logger.debug("trace write-behind emit %s failed", method_name, exc_info=True)

    async def drain(self) -> None:
        """Await the ordered tail so all enqueued writes have hit disk.

        Bounded by the turn's own event count. Loops until the tail stops moving
        so writes scheduled while draining are also awaited. Read-your-writes
        barrier for any consumer (verification agent, oracle, test) that must see
        a complete trace before reading it.
        """
        while True:
            tail = self._tail
            if tail is None:
                return
            await asyncio.gather(tail, return_exceptions=True)
            if self._tail is tail:
                return

    async def aclose(self) -> None:
        """Drain then flush/close the wrapped writer on the worker path."""
        await self.drain()
        self._schedule("close", (), {})
        await self.drain()
