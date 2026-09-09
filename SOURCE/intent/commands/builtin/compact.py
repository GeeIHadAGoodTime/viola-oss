"""``/compact`` — trigger conversation summarization.

Parity reference: ``src/commands/compact/``. Claude's ``/compact`` runs the
conversation through a summarization pass, replaces older turns with a
compact boundary + summary, and re-runs ``SessionStart(source='compact')`` so
plugins / settings hooks can re-inject context for the resumed session.

F-003 (R3-A): this handler used to call ``manager.compact()`` synchronously
(swallowing the coroutine return value) and rendered the summary as a
``FrameRole.META_USER`` ``<system-reminder>`` block instead of a
Claude-style compact-summary user message. The fix awaits the async
``ConversationStateManager.compact()`` (which already dispatches
``PreCompact`` / ``PostCompact`` per S8-010), then fires the additional
``SessionStart(source='compact')`` lifecycle Claude runs after every
compact (``commands/compact/compact.ts:159-210``).

Coordination with R9-B/F-008 (``/clear``): both ``/compact`` and ``/clear``
go through the same lifecycle hook path (``dispatch_lifecycle``) so the
R9-B fix can layer ``SessionEnd`` + ``SessionStart(source='clear')`` on the
same surface without diverging compaction's pipeline.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger
from intent.commands.registry import CommandInvocation, CommandSpec

logger = get_logger(__name__)


@dataclass(frozen=True)
class _CompactionOutcome:
    ok: bool
    summary: str = ""
    reason: str = ""


def command_spec(*, pipeline: Any) -> CommandSpec:
    """Build the ``/compact`` spec bound to ``pipeline``."""

    def _handler(invocation: CommandInvocation) -> dict[str, object]:
        return _handle(invocation, pipeline)

    return CommandSpec(
        name="compact",
        aliases=("/compact",),
        source="builtin",
        handler=_handler,
        remote_safe=False,
        description="Compact (summarize) older conversation turns.",
        command_type="local",
        progress_message="Compacting conversation...",
        argument_hint="[instructions]",
        is_session_control=True,
        extra={"session_lifecycle": ("PreCompact", "PostCompact")},
    )


def _handle(invocation: CommandInvocation, pipeline: Any) -> dict[str, object]:
    instructions = _extract_instructions(invocation)
    outcome = _run_compaction(pipeline, invocation.session_id, instructions=instructions)
    if not outcome.ok:
        return {
            "message": "Conversation was not compacted: %s" % (outcome.reason or "no successful compaction result"),
            "data": {
                "command": "compact",
                "compacted": False,
                "instructions": instructions,
                "reason": outcome.reason,
            },
        }
    return {
        "message": "Conversation compacted.",
        "data": {
            "command": "compact",
            "compacted": True,
            "session_id": invocation.session_id,
            "instructions": instructions,
            "summary_excerpt": outcome.summary[:200],
        },
    }


def _extract_instructions(invocation: CommandInvocation) -> str:
    raw = invocation.args.get("raw_args") if invocation.args else None
    return raw.strip() if isinstance(raw, str) else ""


def _run_compaction(pipeline: Any, session_id: str | None, *, instructions: str = "") -> _CompactionOutcome:
    """Run the canonical async compaction pipeline + lifecycle hooks.

    Returns a structured success/failure outcome. ``False`` and explicit
    ``ok=False``/``compacted=False`` values are failures, not string summaries.
    """

    resolver: Callable[..., Any] | None = getattr(pipeline, "compact_session", None)
    if callable(resolver):
        try:
            if session_id is not None:
                outcome = resolver(session_id=session_id, instructions=instructions)
            else:
                outcome = resolver(instructions=instructions)
        except TypeError:
            try:
                outcome = resolver(session_id=session_id) if session_id is not None else resolver()
            except (RuntimeError, ValueError, TypeError):
                return _CompactionOutcome(False, reason="compaction resolver failed")
        except (RuntimeError, ValueError):
            return _CompactionOutcome(False, reason="compaction resolver failed")
        outcome = _await_if_coro(outcome)
        return _coerce_compaction_outcome(outcome)
    manager = getattr(pipeline, "conversation_state_manager", None)
    if manager is None:
        return _CompactionOutcome(False, reason="compaction is not configured for this session")
    # F-003: prefer the async canonical compact() that already dispatches
    # PreCompact/PostCompact per S8-010, then dispatch SessionStart('compact')
    # so settings/plugin hooks can re-prime the resumed session
    # (commands/compact/compact.ts:159-210).
    for method_name in ("compact", "summarize", "compact_history"):
        method = getattr(manager, method_name, None)
        if not callable(method):
            continue
        try:
            outcome = (
                method(custom_instructions=instructions, trigger="manual") if method_name == "compact" else method()
            )
        except TypeError:
            try:
                outcome = method()
            except Exception:
                logger.exception("Compaction method %s failed without args", method_name)
                continue
        except Exception:
            logger.exception("Compaction method %s failed", method_name)
            continue
        outcome = _await_if_coro(outcome)
        coerced = _coerce_compaction_outcome(outcome)
        if method_name == "compact" and coerced.ok:
            _dispatch_session_start_after_compact(pipeline, session_id)
        return coerced
    return _CompactionOutcome(False, reason="no compaction method succeeded")


def _await_if_coro(value: Any) -> Any:
    """If ``value`` is a coroutine, run it to completion synchronously.

    F-003: ``ConversationStateManager.compact()`` is ``async def``; the
    previous handler called it without awaiting, so the coroutine was
    discarded and no compaction happened. The slash-command handler runs
    on the request thread; we drive the coroutine to completion the same
    way the typed-compaction tests do.
    """

    if not inspect.iscoroutine(value):
        return value
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # Running inside an asyncio loop (e.g. FastAPI handler): schedule
        # the coroutine on the loop and block via run_coroutine_threadsafe.
        # In practice the slash-command dispatch is sync-from-async-context;
        # use a fresh loop in a thread to avoid re-entrancy.
        import concurrent.futures

        def _drive() -> Any:
            return asyncio.run(value)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_drive).result()
    return asyncio.run(value)


def _dispatch_session_start_after_compact(pipeline: Any, session_id: str | None) -> None:
    """Mirror Claude's ``processSessionStartHooks('compact', ...)`` after compact."""

    try:
        from intent.hooks import dispatch_lifecycle
    except ImportError:
        return
    context = {
        "source": "compact",
        "session_id": session_id,
    }
    try:
        dispatch_lifecycle("SessionStart", context)
    except Exception:
        logger.exception("SessionStart(source=compact) lifecycle hook dispatch failed")


def _coerce_compaction_outcome(value: Any) -> _CompactionOutcome:
    if value is False:
        return _CompactionOutcome(False, reason="compaction returned false")
    if value is None:
        return _CompactionOutcome(False, reason="compaction returned no result")
    if value is True:
        return _CompactionOutcome(True, summary="Compaction completed.")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return _CompactionOutcome(False, reason="compaction returned an empty summary")
        return _CompactionOutcome(True, summary=stripped)
    if isinstance(value, dict):
        for flag in ("ok", "compacted", "success"):
            if flag in value and value.get(flag) is False:
                return _CompactionOutcome(
                    False,
                    reason=str(value.get("reason") or value.get("error") or "compaction reported %s=false" % flag),
                )
        text = value.get("summary") or value.get("text") or value.get("content")
        if text is not None:
            return _CompactionOutcome(True, summary=str(text))
        if value.get("ok") is True or value.get("compacted") is True or value.get("success") is True:
            return _CompactionOutcome(True, summary="Compaction completed.")
        return _CompactionOutcome(False, reason="compaction returned no summary")
    return _CompactionOutcome(True, summary=str(value))


__all__ = ["command_spec"]
