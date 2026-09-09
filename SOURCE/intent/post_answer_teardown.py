"""Fire-and-forget scheduler that moves post-answer bookkeeping off the turn path.

The agent loop's *answer* is fully determined the moment ``run_agent_loop`` has
the final ``AgentResult``. Everything after that -- persisting the *completed*
task checkpoint (Fernet-encrypt + serialize + disk write, ~64ms on a 30KB
history and larger on multi-step turns), the terminal "done" progress
broadcast -- is bookkeeping the user should never wait on. Historically it ran
synchronously before the result propagated back to the transport, so the user's
answer waited on it.

``schedule_post_answer_sync`` / ``schedule_post_answer_coro`` offload a piece of
that teardown onto a background task so the answer returns first and the
teardown finishes concurrently on the long-lived server loop. CPU/I-O-heavy
synchronous work (the checkpoint write) is run on a worker thread via
``asyncio.to_thread`` so it never blocks the event loop while the response is
still flushing; already-async teardown (the progress broadcast) runs as a plain
coroutine task.

Durability trade (SURFACED, not silently taken -- mirrors Lane A #464's trace
write-behind): a synchronous checkpoint write meant a crash immediately after
the answer still left the completed-checkpoint on disk. Deferred, a hard process
kill (SIGKILL / power loss) in the window between the answer returning and the
background task completing loses that write. This is acceptable *only for the
completed-success path*: a completed task is never resumed, so its checkpoint
has no recovery value -- the sole reader is diagnostics. Gate (payment/signature
waiting_for_user) and error checkpoints, whose durability IS load-bearing for
resume, are deliberately NOT deferred by callers and keep writing synchronously.

Read-your-writes: a strong reference to every in-flight task is held in a
module-level set (``asyncio.create_task`` keeps none of its own, so a
fire-and-forget task whose only referrer is a soon-GC'd executor can be
collected mid-flight and silently cancelled). The task also registers itself on
the owning executor's tracking set so a consumer that needs a hard completeness
barrier -- a test, an oracle reading the checkpoint seconds later -- can
``await executor.drain_post_answer_teardown()``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from core.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = get_logger(__name__)

# Strong references to in-flight teardown tasks. Guarantees a fire-and-forget
# task drains autonomously on the long-lived server loop even after the executor
# that scheduled it is dropped -- see module docstring.
_POST_ANSWER_TEARDOWN_TASKS: set[asyncio.Future[Any]] = set()


def _retain(task: asyncio.Future[Any], owner_set: set[asyncio.Future[Any]] | None) -> None:
    _POST_ANSWER_TEARDOWN_TASKS.add(task)
    if owner_set is not None:
        owner_set.add(task)

    def _discard(done: asyncio.Future[Any]) -> None:
        _POST_ANSWER_TEARDOWN_TASKS.discard(done)
        if owner_set is not None:
            owner_set.discard(done)

    task.add_done_callback(_discard)


def _run_sync_safely(label: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001, RUF100 - post-answer teardown must never crash the turn; log and swallow
        logger.debug("post-answer teardown %s failed", label, exc_info=True)


def schedule_post_answer_sync(
    label: str,
    fn: Callable[[], None],
    *,
    owner_set: set[asyncio.Future[Any]] | None = None,
) -> None:
    """Offload a *synchronous* teardown callable off the answer path.

    Runs ``fn`` on a worker thread (``asyncio.to_thread``) so its CPU/disk cost
    never blocks the event loop while the response is still flushing. If no loop
    is running (synchronous/test context) the callable runs inline so teardown is
    never silently dropped outside an async turn.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _run_sync_safely(label, fn)
        return

    async def _runner() -> None:
        await asyncio.to_thread(_run_sync_safely, label, fn)

    task = loop.create_task(_runner(), name="post-answer-teardown:%s" % label)
    _retain(task, owner_set)


def schedule_post_answer_coro(
    label: str,
    coro: Coroutine[Any, Any, Any],
    *,
    owner_set: set[asyncio.Future[Any]] | None = None,
) -> None:
    """Offload an *async* teardown coroutine off the answer path.

    Schedules ``coro`` as a fire-and-forget task; the answer returns without
    awaiting it. If no loop is running the coroutine is closed (nothing to run
    it) and the caller is expected to have a synchronous fallback.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return

    async def _guarded() -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001, RUF100 - post-answer teardown must never crash the turn; log and swallow
            logger.debug("post-answer teardown %s (coro) failed", label, exc_info=True)

    task = loop.create_task(_guarded(), name="post-answer-teardown:%s" % label)
    _retain(task, owner_set)


async def drain_owner(owner_set: set[asyncio.Future[Any]]) -> None:
    """Await every teardown task scheduled by one owner (read-your-writes barrier).

    Loops until the set stops growing so tasks scheduled while draining are also
    awaited. Bounded by the finite teardown work of a single turn.
    """
    while True:
        pending = [task for task in owner_set if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
