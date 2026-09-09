"""Step logging hook — records each tool call to the structured step log (LA-4).

Extracts step logging from the inline agent executor code.  Records:
- Structured ``AgentStepRecord`` to the task log
- JSONL entry to ``logs/structured/agent-steps-YYYYMMDD.jsonl``
- WebSocket broadcast for live UI updates
- Telemetry per-tool call tracking
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.logging_config import get_logger
from intent.hooks.base import HookContext, PostToolHook
from intent.log_redaction import redact_card_data

logger = get_logger(__name__)


class StepLoggerHook(PostToolHook):
    """Logs each tool call as a structured step record.

    Requires access to the task_log, emit_step_jsonl callback, and
    broadcast callback — provided by the agent executor at construction.
    """

    def __init__(
        self,
        task_log: Any,
        emit_step_jsonl_fn: Any,
        broadcast_fn: Any,
        summarize_fn: Any,
        summarize_args_fn: Any,
        last_usage_fn: Any,
        last_page_url_fn: Any,
        step_record_cls: type,
        checkpoint_step_cls: type,
        append_step_fn: Any,
        checkpoint: Any,
    ) -> None:
        self._task_log = task_log
        self._emit_step_jsonl = emit_step_jsonl_fn
        self._broadcast = broadcast_fn
        self._summarize = summarize_fn
        self._summarize_args = summarize_args_fn
        self._last_usage = last_usage_fn
        self._last_page_url = last_page_url_fn
        self._step_record_cls = step_record_cls
        self._checkpoint_step_cls = checkpoint_step_cls
        self._append_step = append_step_fn
        self._checkpoint = checkpoint

    async def on_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        tool_error: str | None,
        context: HookContext,
    ) -> None:
        """Record step to task log, emit JSONL, broadcast WS event, log INFO."""
        tool_elapsed_ms = getattr(tool_result, "_elapsed_ms", 0)
        result_text = tool_result.to_llm_text() if hasattr(tool_result, "to_llm_text") else str(tool_result)
        safe_tool_input = redact_card_data(tool_input)

        # Strip base64 image data from summary
        summary_text = result_text
        if isinstance(getattr(tool_result, "data", None), dict) and "image_base64" in tool_result.data:
            summary_text = '{"ok": true, "title": "%s", "url": "%s", "screenshot": true}' % (
                tool_result.data.get("title", ""),
                tool_result.data.get("url", ""),
            )
        summary_text = redact_card_data(summary_text)

        # Build step record
        step_record = self._step_record_cls(
            task_id=context.task_id,
            step=context.iteration,
            timestamp=datetime.now(tz=UTC).isoformat(),
            tool_name=tool_name,
            tool_input=safe_tool_input,
            tool_result_summary=self._summarize(summary_text, 10000),
            llm_decision="tool_call",
            llm_reasoning=context.llm_reasoning,
            page_url=self._last_page_url(),
            duration_ms=tool_elapsed_ms,
            error=tool_error or (tool_result.error if not tool_result.ok else None),
        )
        self._task_log.add_step(step_record)
        self._emit_step_jsonl(step_record, self._last_usage())

        # Broadcast WS event
        await self._broadcast(
            tool_name,
            context.iteration,
            tool_elapsed_ms,
            "error" if tool_error else "ok",
            reasoning=context.llm_reasoning,
        )

        # Telemetry
        try:
            from admin.instrumentation import record_agent_tool_call

            record_agent_tool_call(tool_name, tool_elapsed_ms, tool_result.ok)
        except Exception:
            pass

        # One-line INFO summary
        logger.info(
            "Agent step %d: %s(%s) -> %s [%dms]",
            context.iteration,
            tool_name,
            self._summarize_args(safe_tool_input),
            self._summarize(summary_text, 50),
            tool_elapsed_ms,
        )

        # Checkpoint
        cp_step = self._checkpoint_step_cls(
            index=self._checkpoint.step_index,
            tool=tool_name,
            input=safe_tool_input,
            output=self._summarize(summary_text, 2000),
            duration_ms=tool_elapsed_ms,
        )
        self._append_step(self._checkpoint, cp_step)
