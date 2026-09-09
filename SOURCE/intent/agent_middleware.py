"""Agent loop middleware pipeline.

Each middleware processes a tool call result or agent response,
can modify it, and passes it to the next middleware.  Middlewares
are independently testable and removable.

Architecture
------------
The main while loop in ``agent_executor.py`` (native + text paths) and
``agent_loop.py`` contain ~35 orchestration features inlined over
thousands of lines.  This module extracts the most impactful features
into composable middleware classes, each with a single responsibility.

Usage::

    middlewares = [
        SpinDetectorMiddleware(spin_detector),
        ToolBlacklistMiddleware(executor),
        ClickBackoffMiddleware(executor),
        URLTrackerMiddleware(executor),
        PaymentGateMiddleware(executor),
        ProgressReporterMiddleware(executor),
        StepLoggerMiddleware(executor, task_log, checkpoint),
    ]

    for mw in middlewares:
        result_text = await mw.process(ctx, tc_name, tool_result, ...)
        if ctx.should_stop:
            break
"""

from __future__ import annotations

import abc
import asyncio
import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

logger = get_logger(__name__)

POST_STEP_BROADCAST_TIMEOUT_SECONDS = 0.25


def _is_approval_block_error(error_category: str | None = None) -> bool:
    """Return True when a tool result is an approval-policy denial."""
    return str(error_category or "").upper() == "APPROVAL_BLOCKED"


if TYPE_CHECKING:
    from intent.agent_executor import AgentExecutor, AgentTaskLog
    from intent.spin_detector import SpinDetector
    from intent.task_checkpoint import TaskCheckpoint
    from intent.tool_types import ToolResult


# ---------------------------------------------------------------------------
# Shared loop context
# ---------------------------------------------------------------------------


@dataclass
class LoopContext:
    """Shared mutable state for the agent loop iteration.

    Passed through every middleware so each can read/write shared state.
    The orchestrator checks ``should_stop`` after each middleware pass.
    """

    iteration: int = 0
    tool_calls_made: list[str] = field(default_factory=list)
    tool_calls_args: list[str] = field(default_factory=list)
    urls_visited: list[str] = field(default_factory=list)
    blacklisted_tools: set[str] = field(default_factory=set)
    should_stop: bool = False
    stop_reason: str = ""
    # Factual correction messages to append after tool_result blocks
    correction_messages: list[str] = field(default_factory=list)
    # Track last page URL across iterations
    last_page_url: str | None = None
    # Track consecutive click failures
    consecutive_click_failures: int = 0
    # Track consecutive failures (cross-tool)
    consecutive_failures: int = 0
    # Diminishing-returns tracking
    last_result_hash: str = ""
    consecutive_no_progress: int = 0
    # LLM reasoning text from the provider response (for step logs)
    llm_reasoning: str = ""
    # Provider tool-use/call correlation id for step-log joins.
    tool_use_id: str | None = None
    # Replayable model history captured before the step log is emitted.
    model_messages_snapshot: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Base middleware
# ---------------------------------------------------------------------------


class Middleware(abc.ABC):
    """Base class for agent loop middleware.

    Each subclass implements ``process()`` which is called after a tool
    executes.  Middlewares can:
    - Modify ``context`` to signal stop, blacklist tools, add corrections
    - Return a (possibly modified) result text
    - Log, record telemetry, update checkpoints
    """

    @abc.abstractmethod
    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        """Process a tool result.  Return (possibly modified) result_text."""
        ...


# ---------------------------------------------------------------------------
# 1. SpinDetectorMiddleware
# ---------------------------------------------------------------------------


class SpinDetectorMiddleware(Middleware):
    """Observes repeated tool-call patterns without steering tool choice.

    Wraps the existing ``SpinDetector`` class, feeding it success/failure
    signals and checking for spin interventions.

    Extracted from: agent_executor.py lines ~2555-2608 (native) and
    ~3193-3226 (text), plus agent_loop.py lines ~644-671.
    """

    def __init__(self, spin_detector: SpinDetector) -> None:
        self._spin_detector = spin_detector

    @property
    def spin_detector(self) -> SpinDetector:
        return self._spin_detector

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        # Soft-failure detection: browser tools catch exceptions and return
        # ok=True with data={"error": "..."}.  Spin detector must see these
        # as failures.
        _soft_fail = (
            tool_result.ok
            and isinstance(tool_result.data, dict)
            and "error" in tool_result.data
            and not any(k in tool_result.data for k in ("filled", "clicked", "selected", "output"))
        )
        if tool_error or not tool_result.ok or _soft_fail:
            _fail_msg = str(
                tool_error or tool_result.error or (tool_result.data.get("error", "") if _soft_fail else "")
            )[:200]
            self._spin_detector.record_failure(
                tool_name,
                tool_args,
                _fail_msg,
                page_url=context.last_page_url,
            )
            context.consecutive_failures += 1
        else:
            _base_sig = tool_args
            self._spin_detector.record_success(
                tool_name,
                input_sig=_base_sig,
                page_url=context.last_page_url,
            )
            context.consecutive_failures = 0
            # Compatibility hook for older web_search counter code.
            if tool_name.startswith("browser_"):
                try:
                    from intent.tools.web_search import notify_browser_navigate

                    notify_browser_navigate()
                except (ImportError, AttributeError) as exc:
                    logger.debug("notify_browser_navigate unavailable: %s", exc)

        return result_text

    def check_intervention(self) -> str | None:
        """Check for spin intervention message (call after all per-tool processing)."""
        return self._spin_detector.is_spinning()


# ---------------------------------------------------------------------------
# 2. ToolBlacklistMiddleware
# ---------------------------------------------------------------------------


class ToolBlacklistMiddleware(Middleware):
    """Manages tool blacklisting after deterministic approval rejection.

    Extracted from: agent_executor.py lines ~2610-2649 (native) and
    ~3227-3262 (text), plus agent_loop.py lines ~673-699.
    """

    def __init__(
        self,
        rejected_tools: set[str],
        task_id: str = "",
        executor: AgentExecutor | None = None,
    ) -> None:
        self._rejected_tools = rejected_tools
        self._task_id = task_id
        self._executor = executor

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        # Fix 3: blacklist tool after approval rejection
        if (
            _is_approval_block_error(getattr(tool_result, "error_category", None))
            and tool_name not in self._rejected_tools
        ):
            if self._executor is not None:
                try:
                    from intent.agent_executor import _remember_approval_blocked_tool

                    _remember_approval_blocked_tool(self._executor, tool_name)
                except Exception as exc:
                    logger.debug("ToolBlacklist approval-block memory failed for %s: %s", tool_name, exc)
            self._rejected_tools.add(tool_name)
            logger.warning(
                "ToolBlacklist: blacklisting %s after approval rejection (task=%s)",
                tool_name,
                self._task_id,
            )
            context.correction_messages.append("Tool %s is not available for this task." % tool_name)

        return result_text


# ---------------------------------------------------------------------------
# 3. ClickBackoffMiddleware
# ---------------------------------------------------------------------------


class ClickBackoffMiddleware(Middleware):
    """Observes browser click failures without steering tool choice.

    Live evidence (PR #54 trace ``11987b3be29c``): D-pizza task burned the
    full iteration budget on raw CSS selector clicks (``#order-online``,
    ``h2:contains(...)``, ``button[innerText=...]``). Current guidance lives in
    browser tool descriptions rather than a counter-based correction message.

    Extracted from: agent_executor.py lines ~2651-2675 (native) and
    ~3264-3286 (text), plus agent_loop.py lines ~701-709.
    """

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        if tool_name != "browser_interact":
            return result_text

        # Treat structured soft-failures as failures so selector misses count
        # in telemetry too.
        _soft_fail = (
            tool_result.ok
            and isinstance(tool_result.data, dict)
            and (tool_result.data.get("ok") is False or "error" in tool_result.data)
            and not any(k in tool_result.data for k in ("clicked", "filled", "selected"))
        )

        if tool_error or not tool_result.ok or _soft_fail:
            context.consecutive_click_failures += 1
            logger.debug(
                "Click failure observed: count=%d",
                context.consecutive_click_failures,
            )
        else:
            context.consecutive_click_failures = 0

        return result_text


# ---------------------------------------------------------------------------
# 4. URLTrackerMiddleware
# ---------------------------------------------------------------------------


class URLTrackerMiddleware(Middleware):
    """Tracks URLs visited for cycle detection.

    Updates ``context.last_page_url`` and ``context.urls_visited`` after
    browser tool calls.

    Extracted from: agent_executor.py ``_track_page_url`` method calls,
    plus agent_loop.py line ~577.
    """

    # Browser tool names whose result may contain a "url" field
    _BROWSER_TOOLS = frozenset(
        {
            "browser_navigate",
            "browser_run_script",
            "browser_screenshot",
            "browser_interact",
            "browser_fill_form",
            "browser_snapshot",
            "browser_get_links",
            "browser_get_form_fields",
            "browser_click",
            "browser_type",
            "browser_back",
            "browser_close",
            "browser_get_page_info",
            "browser_get_text",
            "browser_scroll",
            "browser_press_key",
            "browser_status",
            "browser_wait",
            "browser_evaluate",
            "browser_get_api_log",
            "browser_forward",
            "browser_refresh",
            "verify_state",
        }
    )

    def __init__(self, executor: AgentExecutor | None = None) -> None:
        self._executor = executor

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        # Delegate to executor's _track_page_url if available
        if self._executor is not None:
            try:
                self._executor._track_page_url(tool_name, tool_result)
                context.last_page_url = self._executor._last_page_url
            except Exception as exc:
                logger.warning("URLTracker _track_page_url failed: %s", exc)
        elif tool_name in self._BROWSER_TOOLS and tool_result.ok:
            # Standalone tracking (no executor)
            if isinstance(tool_result.data, dict):
                url = tool_result.data.get("url") or tool_result.data.get("new_url")
                if url:
                    context.last_page_url = url
                    if url not in context.urls_visited:
                        context.urls_visited.append(url)

        return result_text


# ---------------------------------------------------------------------------
# 5. PaymentGateMiddleware
# ---------------------------------------------------------------------------


class PaymentGateMiddleware(Middleware):
    """Handles payment gate stop conditions.

    Explicit ``payment(action="request_review")`` calls mark the executor as
    ready for payment handoff; this middleware only observes that structured
    state.

    Extracted from: agent_executor.py lines ~2722-2726 (native) and
    ~3324-3329 (text), plus agent_loop.py lines ~728-734.
    """

    def __init__(self, executor: AgentExecutor | None = None) -> None:
        self._executor = executor

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        if self._executor is not None:
            if bool(getattr(self._executor, "_payment_gate_requested", False)):
                logger.info("PaymentGate: payment review requested - signalling loop stop")
                context.should_stop = True
                context.stop_reason = "payment_gate"

        return result_text


# ---------------------------------------------------------------------------
# 6. SignatureGateMiddleware
# ---------------------------------------------------------------------------


class SignatureGateMiddleware(Middleware):
    """Handles signature gate stop conditions.

    Explicit ``signature(action="request_review")`` calls mark the executor as
    ready for signature handoff; this middleware only observes that structured
    state.
    """

    def __init__(self, executor: AgentExecutor | None = None) -> None:
        self._executor = executor

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        if self._executor is not None:
            if bool(getattr(self._executor, "_signature_gate_requested", False)):
                logger.info("SignatureGate: signature review requested - signalling loop stop")
                context.should_stop = True
                context.stop_reason = "signature_gate"

        return result_text


# ---------------------------------------------------------------------------
# 7. ProgressReporterMiddleware
# ---------------------------------------------------------------------------

_TOOL_PROGRESS_PHRASES: dict[str, str] = {
    "file_read": "Reading files...",
    "file_write": "Writing files...",
    "run_command": "Running a command...",
    "web_search": "Searching the web...",
    "web_read": "Reading that page...",
    "system_info": "Checking your system...",
    "send_email": "Sending the email...",
    "browser_navigate": "Opening that website...",
    "browser_screenshot": "Taking a screenshot...",
    "browser_run_script": "Running browser automation...",
    "browser_interact": "Clicking that element...",
    "browser_fill_form": "Filling in the form...",
    "browser_snapshot": "Reading the page...",
    "memory": "Accessing memory...",
    "schedule": "Managing schedule...",
    "phone": "Handling phone call...",
    "computer": "Using the desktop...",
    "self_manage": "Managing system...",
    "timer": "Managing timer...",
    "calendar": "Managing calendar...",
    "payment": "Processing payment...",
    "signature": "Reviewing the legal signature step...",
    "playlist": "Managing playlist...",
    "api_credential": "Managing credentials...",
    "spawn_subtask": "Delegating a subtask...",
}

# Progress summary interval (tool calls between summaries)
_SUMMARY_INTERVAL = 10


class ProgressReporterMiddleware(Middleware):
    """Records backend progress summaries without speaking tool chatter."""

    def __init__(
        self,
        speak_fn: Any | None = None,
        summary_interval: int = _SUMMARY_INTERVAL,
    ) -> None:
        self._speak_fn = speak_fn
        self._summary_interval = summary_interval
        self._tool_summaries: list[str] = []
        self._summary_count = 0

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        # Periodic progress summary
        self._summary_count += 1
        self._tool_summaries.append("%s(%s)" % (tool_name, "ok" if tool_result.ok else "err"))
        if self._summary_count > 0 and self._summary_count % self._summary_interval == 0:
            _start_step = self._summary_count - self._summary_interval + 1
            logger.info(
                "Progress [steps %d-%d]: %s",
                _start_step,
                self._summary_count,
                "; ".join(self._tool_summaries[-self._summary_interval :]),
            )
            self._tool_summaries = self._tool_summaries[-self._summary_interval :]

        return result_text


# ---------------------------------------------------------------------------
# 7. StepLoggerMiddleware
# ---------------------------------------------------------------------------


def _summarize(text: str, limit: int = 2000) -> str:
    """Truncate text for logging (local copy to avoid circular import)."""
    if not text:
        return "(empty)"
    text = text.replace("\n", " ").strip()
    return text[:limit] + "..." if len(text) > limit else text


def _summarize_args(args: dict[str, Any] | Any, limit: int = 80) -> str:
    """Compact argument summary for logging."""
    if not isinstance(args, dict):
        return str(args)[:60]
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append("%s=%s" % (k, s))
    return ", ".join(parts)[:limit]


class StepLoggerMiddleware(Middleware):
    """Logs structured step data to JSONL and broadcasts WS events.

    Also handles:
    - Step record creation and addition to task_log
    - JSONL emission via executor._emit_step_jsonl
    - WebSocket broadcast via executor._broadcast_agent_step
    - Telemetry recording
    - Checkpoint step recording

    Extracted from: agent_executor.py lines ~2488-2553 (native) and
    ~3127-3191 (text), plus agent_loop.py lines ~589-642.
    """

    def __init__(
        self,
        executor: AgentExecutor,
        task_log: AgentTaskLog,
        checkpoint: TaskCheckpoint,
        task_id: str = "",
    ) -> None:
        self._executor = executor
        self._task_log = task_log
        self._checkpoint = checkpoint
        self._task_id = task_id
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _track_background_task(self, task: asyncio.Task[None], *, label: str) -> None:
        self._background_tasks.add(task)

        def _done(completed: asyncio.Task[None]) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                logger.debug("%s cancelled", label)
            except Exception as exc:
                logger.warning("%s failed: %s", label, exc)

        task.add_done_callback(_done)

    def _schedule_background(self, coro: Any, *, label: str) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            logger.warning("Could not schedule %s: no running event loop", label)
            return
        self._track_background_task(task, label=label)

    async def drain_background_tasks(self) -> None:
        """Wait for scheduled post-step bookkeeping; used by focused tests."""
        if not self._background_tasks:
            return
        await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)

    async def _broadcast_step_bounded(
        self,
        *,
        tool_name: str,
        iteration: int,
        tool_elapsed_ms: int,
        status: str,
        reasoning: str,
        tool_args: dict[str, Any],
        tool_output: str,
    ) -> None:
        broadcaster = getattr(self._executor, "_broadcast_agent_step", None)
        if not callable(broadcaster):
            return

        try:
            await asyncio.wait_for(
                broadcaster(
                    tool_name,
                    iteration,
                    tool_elapsed_ms,
                    status,
                    reasoning=reasoning,
                    tool_input=tool_args,
                    tool_output=tool_output,
                ),
                timeout=POST_STEP_BROADCAST_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.debug(
                "Agent step broadcast exceeded %.2fs for %s; continuing without blocking the loop",
                POST_STEP_BROADCAST_TIMEOUT_SECONDS,
                tool_name,
            )
        except Exception as exc:
            logger.debug("Agent step broadcast failed for %s: %s", tool_name, exc)

    async def _update_checkpoint_background(
        self,
        *,
        cp_step: Any,
        model_messages_snapshot: list[dict[str, Any]] | None,
        llm_continuity: dict[str, Any],
    ) -> None:
        from intent.task_checkpoint import append_step, compress_messages

        def _write_checkpoint_update() -> None:
            if model_messages_snapshot is not None:
                self._checkpoint.llm_messages = compress_messages(model_messages_snapshot)
                self._checkpoint.llm_continuity = copy.deepcopy(llm_continuity)
            append_step(self._checkpoint, cp_step)

        try:
            await asyncio.to_thread(_write_checkpoint_update)
        except Exception as exc:
            logger.warning("Agent checkpoint update failed: %s", exc)

    async def process(
        self,
        context: LoopContext,
        tool_name: str,
        tool_result: ToolResult,
        tool_args: dict[str, Any],
        tool_error: str | None,
        result_text: str,
        tool_elapsed_ms: int,
    ) -> str:
        from intent.agent_executor import AgentStepRecord
        from intent.task_checkpoint import CheckpointStep, append_step

        try:
            # Build log summary (strip base64 image data)
            summary_text = result_text
            if isinstance(tool_result.data, dict) and "image_base64" in tool_result.data:
                from intent.agent_executor import _strip_image_from_result

                summary_text = _strip_image_from_result(result_text)

            # Step record
            step_record = AgentStepRecord(
                task_id=self._task_id,
                step=context.iteration,
                timestamp=datetime.now(tz=UTC).isoformat(),
                tool_name=tool_name,
                tool_input=tool_args,
                tool_result_summary=_summarize(summary_text, 10000),
                llm_decision="tool_call",
                llm_reasoning=context.llm_reasoning,
                page_url=context.last_page_url,
                duration_ms=tool_elapsed_ms,
                error=tool_error or (tool_result.error if not tool_result.ok else None),
                tool_use_id=context.tool_use_id,
            )
            self._task_log.add_step(step_record)

            # Emit JSONL
            self._executor._emit_step_jsonl(
                step_record,
                self._executor._last_usage,
                model_messages_snapshot=(
                    copy.deepcopy(context.model_messages_snapshot)
                    if context.model_messages_snapshot is not None
                    else None
                ),
            )

            # UI/stream broadcast is useful but must not block the next model
            # turn. The JSONL append above and tool-execution trace append in
            # agent_loop remain synchronous forensic records.
            self._schedule_background(
                self._broadcast_step_bounded(
                    tool_name=tool_name,
                    iteration=context.iteration,
                    tool_elapsed_ms=tool_elapsed_ms,
                    status="error" if tool_error else "ok",
                    reasoning=context.llm_reasoning,
                    tool_args=copy.deepcopy(tool_args),
                    tool_output=_summarize(summary_text, 4000),
                ),
                label="Agent step broadcast",
            )

            # Telemetry
            try:
                from telemetry import get_accumulator

                get_accumulator().increment_agent_tool(tool_name, latency_ms=tool_elapsed_ms, success=tool_result.ok)
            except Exception as exc:
                logger.warning("StepLogger telemetry failed: %s", exc)

            # Info log
            logger.info(
                "Agent step %d: %s(%s) -> %s [%dms]",
                context.iteration,
                tool_name,
                _summarize_args(tool_args),
                _summarize(summary_text, 50),
                tool_elapsed_ms,
            )

            # Checkpoint snapshot compression can be much slower than the tool
            # result itself. Keep it off the critical path; terminal/gate paths
            # still take synchronous snapshots before marking completion.
            cp_step = CheckpointStep(
                index=self._checkpoint.step_index,
                tool=tool_name,
                input=tool_args,
                output=_summarize(summary_text, 2000),
                duration_ms=tool_elapsed_ms,
            )
            llm_continuity = (
                self._executor._export_responses_continuity_state()
                if getattr(self._executor, "_use_native", False)
                else {}
            )
            self._schedule_background(
                self._update_checkpoint_background(
                    cp_step=cp_step,
                    model_messages_snapshot=context.model_messages_snapshot,
                    llm_continuity=llm_continuity,
                ),
                label="Agent checkpoint update",
            )

        except Exception as exc:
            logger.warning(
                "StepLogger: bookkeeping failed for %s: %s",
                tool_name,
                exc,
            )

        return result_text


# ---------------------------------------------------------------------------
# Pipeline runner helper
# ---------------------------------------------------------------------------


async def run_middleware_pipeline(
    middlewares: list[Middleware],
    context: LoopContext,
    tool_name: str,
    tool_result: ToolResult,
    tool_args: dict[str, Any],
    tool_error: str | None,
    result_text: str,
    tool_elapsed_ms: int,
) -> str:
    """Run all middlewares in sequence on a single tool result.

    Returns the (possibly modified) result_text after all middlewares.
    Stops early if any middleware sets ``context.should_stop``.
    """
    for mw in middlewares:
        result_text = await mw.process(
            context=context,
            tool_name=tool_name,
            tool_result=tool_result,
            tool_args=tool_args,
            tool_error=tool_error,
            result_text=result_text,
            tool_elapsed_ms=tool_elapsed_ms,
        )
        if context.should_stop:
            break
    return result_text
