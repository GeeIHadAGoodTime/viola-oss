"""Canonical agent loop for provider-native MCP tool calling.

``AgentExecutor.run()`` prepares task state, trace state, approval state, and
checkpoint state, then delegates iterative model/tool execution here. The loop
uses the active provider's ``route_command_native()`` implementation, executes
MCP tools, records step traces, and enforces the shared safety/observability
guards for every LLM provider.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json as _json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from core.logging_config import get_logger
from diagnostics import latency_spans
from intent.agent_message_manager import (
    reconstruct_executor_content_replacement_state,
    record_tool_result_replacements,
    tool_result_replacement_session_id,
)
from intent.agent_middleware import (
    ClickBackoffMiddleware,
    LoopContext,
    PaymentGateMiddleware,
    ProgressReporterMiddleware,
    SignatureGateMiddleware,
    SpinDetectorMiddleware,
    StepLoggerMiddleware,
    ToolBlacklistMiddleware,
    URLTrackerMiddleware,
    run_middleware_pipeline,
)
from intent.context_compaction import (
    compact_native_messages,
    is_token_limit_error,
    project_native_messages_for_provider,
)
from intent.hooks.dispatcher import (
    dispatch_lifecycle,
    dispatch_tool_hook,
    has_registered_hooks,
    hook_result_frames,
    take_initial_user_message,
    watch_paths,
)
from intent.hooks.schema import HookEvent, HookResult
from intent.response_cleanup import strip_json_template as _strip_json_template
from intent.subagents.lifecycle import (
    normalize_subagent_mode,
    subagent_lifecycle,
    subagent_transcript_path,
)
from intent.tool_types import AgentResult, ToolCall, ToolResult
from intent.tools.deferred_tool_schemas import expand_tools_with_deferred_references
from services.conversation.context_frames import (
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    TextBlock,
)
from services.conversation.frame_rendering import render_for_anthropic
from services.conversation.message_invariants import SYNTHETIC_TOOL_RESULT_CONTENT
from services.llm.operator_diagnostics import user_message_for_operator_diagnostic
from services.llm.prompts import build_provider_prompt_bundle

logger = get_logger(__name__)

if TYPE_CHECKING:
    from intent.agent_executor import AgentExecutor

# Sentinel for summary fallback
_SUMMARY_FALLBACK_SENTINEL = "[NO_SUMMARY]"

_DEFAULT_NATIVE_TURN_MAX_TOKENS = 1024


def _normalize_terminal_model_text(text: str | None) -> str:
    return _strip_json_template(str(text or ""))


def _trace_safe_copy(value: Any) -> Any:
    """Copy trace payloads without letting live runtime objects break the agent loop."""

    try:
        return copy.deepcopy(value)
    except (TypeError, ValueError, AttributeError, RuntimeError, RecursionError):
        try:
            return _json.loads(_json.dumps(value, default=lambda obj: "<%s>" % type(obj).__name__))
        except (TypeError, ValueError, AttributeError, RuntimeError, RecursionError):
            return "<%s>" % type(value).__name__


_REASONING_MODEL_TAGS = ("o1", "o3", "o4-", "gpt-5")
# Mirror Claude Code's MAX_OUTPUT_TOKENS_RECOVERY_LIMIT (query.ts:164). Allows up
# to 3 successive "max output tokens" / context-window incomplete continuations
# before surfacing the withheld error.
_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3
# On the first ``max_output_tokens`` cap hit, retry the SAME request at this
# escalated cap with no conversation mutation. Later retries remain bounded and
# uncapped, but still do not inject runtime instructions into the model stream.
# Claude's source value is 16k.
_ESCALATED_MAX_OUTPUT_TOKENS = 16_000

# S1-003: bounded re-entry budget when a Stop / SubagentStop hook blocks and the
# model responds to the continuation with a ``tool_call`` instead of an answer.
# Claude Code re-enters its main query loop (query.ts:1267-1305) for the full
# step ceiling.  We cap the post-loop continuation at this many tool calls so
# any wedged model still terminates instead of looping against the hook.
_TERMINAL_HOOK_TOOL_RETRIES = 3


class _AgentLoopBillingStop(RuntimeError):
    """Internal control flow after fail-closed billing handling sets a response."""


class _AgentLoopCostLimitStop(RuntimeError):
    """Internal control flow after a spend reservation denial sets a response."""


# In-loop compaction progress guard. Mirrors Claude Code's circuit-breaker
# pattern (services/compact/autoCompact.ts:70 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3):
# allow further compactions when prior ones made progress, bail when N
# consecutive attempts fail to reduce tokens by at least X%. The old
# hardcoded ``< 2`` cap fired even when compactions were succeeding (e.g.
# huge response_items lists where 3-4 rounds are legitimately needed) and
# also did not fire on no-progress loops (a compactor that returned the same
# message list twice still got a third attempt).
_COMPACTION_PROGRESS_MIN_FRACTION = 0.05  # 5% token reduction = "progress"
_COMPACTION_NO_PROGRESS_LIMIT = 2  # bail after this many consecutive no-progress attempts
_COMPACTION_HARD_CEILING = 6  # absolute defense-in-depth (always-allow at most this many)


def _compaction_should_allow(executor: Any) -> bool:
    """Return True when another in-loop compaction is allowed under the progress guard.

    Replaces the previous hardcoded ``_compaction_count < 2`` cap.  Policy:

    * Always allow the first compaction.
    * Allow further compactions while the no-progress streak is below
      ``_COMPACTION_NO_PROGRESS_LIMIT``.
    * Never exceed ``_COMPACTION_HARD_CEILING`` total compactions per run.

    State lives on ``executor`` and is reset at the top of each ``run()``:

    * ``_compaction_count`` — total compactions so far (existing attribute).
    * ``_compaction_no_progress_streak`` — consecutive no-progress attempts.
    """
    count = int(getattr(executor, "_compaction_count", 0) or 0)
    if count >= _COMPACTION_HARD_CEILING:
        return False
    streak = int(getattr(executor, "_compaction_no_progress_streak", 0) or 0)
    if streak >= _COMPACTION_NO_PROGRESS_LIMIT:
        return False
    return True


def _compaction_record_progress(executor: Any, pre_tokens: int, post_tokens: int) -> None:
    """Update the no-progress streak after a compaction attempt.

    Pass 0 for either count if it cannot be measured; in that case we treat
    the attempt conservatively as "no progress" so a stuck compactor still
    bails eventually instead of looping.
    """
    streak = int(getattr(executor, "_compaction_no_progress_streak", 0) or 0)
    pre = max(0, int(pre_tokens or 0))
    post = max(0, int(post_tokens or 0))
    if pre > 0:
        reduction_fraction = (pre - post) / float(pre)
    else:
        reduction_fraction = 0.0
    if reduction_fraction >= _COMPACTION_PROGRESS_MIN_FRACTION:
        executor._compaction_no_progress_streak = 0
    else:
        executor._compaction_no_progress_streak = streak + 1


class _WasteDetectorResult:
    """Result of a single waste-detector tick.

    Progress-scoped: a tool_call iteration is progress and never counts as
    waste regardless of output token count. A true stall is consecutive
    answer-type iterations returning tiny text on high-context input.
    """

    __slots__ = ("terminate",)

    def __init__(self, terminate: bool = False) -> None:
        self.terminate = terminate


def _waste_detector_tick(
    *,
    executor: Any,
    response: dict[str, Any],
    loop_out: int,
    loop_in: int,
    threshold: int = 5,
) -> _WasteDetectorResult:
    """Single-iteration waste-detector tick.

    Returns whether the loop should terminate this iteration. Does NOT inject
    runtime prompt fragments (CLAUDE.md no_prompt_fragments). The model gets
    raw context and decides; this guard only catches a true stall.
    """
    resp_type = response.get("type", "answer")
    is_tool_call = resp_type == "tool_call" or bool(response.get("tool"))

    if not is_tool_call and loop_out < 50 and loop_in > 5000:
        executor._consecutive_low_output = int(getattr(executor, "_consecutive_low_output", 0) or 0) + 1
        if executor._consecutive_low_output >= threshold:
            return _WasteDetectorResult(terminate=True)
        return _WasteDetectorResult(terminate=False)

    if is_tool_call or loop_out > 100:
        executor._consecutive_low_output = 0

    return _WasteDetectorResult(terminate=False)


def _step_ceiling_extension_target(executor: Any, iterations: int) -> int | None:
    """Return the next iteration cap without compacting for a step ceiling.

    Claude Code does not run full conversation compaction because a fixed turn
    count was reached. If the caller configured a soft cap below the hard agent
    ceiling, extend the soft cap and let token-pressure compaction make its own
    decision later.
    """

    try:
        current = int(getattr(executor, "_max_iterations", 0) or 0)
        configured = int(getattr(executor, "_configured_max_iterations", 0) or 0)
        hard = int(getattr(executor, "_hard_agent_iteration_ceiling", 0) or 0)
    except (TypeError, ValueError):
        return None
    if hard <= 0 or iterations >= hard or current >= hard:
        return None
    extension = configured if configured > 0 else _DEFAULT_MAX_ITERATIONS
    return min(hard, iterations + max(1, extension))


_TOOL_EXECUTION_HOOK_EVENTS = (
    "PreToolUse",
    "pre_tool_use",
    "PostToolUse",
    "post_tool_use",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
)


def _child_answer_from_tool_result(result: ToolResult) -> str:
    if not result.ok:
        raise RuntimeError(result.error or "Child agent failed.")
    data = result.data
    if isinstance(data, dict):
        for key in ("answer", "final_text", "content"):
            value = data.get(key)
            if value is not None:
                return str(value)
        return ""
    return str(data or "")


def _is_reasoning_model_name(model_name: str | None) -> bool:
    """Return True for OpenAI reasoning families that need uncapped output."""
    model_lower = (model_name or "").lower()
    return any(tag in model_lower for tag in _REASONING_MODEL_TAGS)


def _native_turn_max_tokens_for_model(model_name: str | None) -> int | None:
    """Reasoning models should not receive a Responses max_output_tokens cap."""
    if _is_reasoning_model_name(model_name):
        return None
    return _DEFAULT_NATIVE_TURN_MAX_TOKENS


def _build_initial_agent_messages(
    user_text: str,
    prompt_context_bundle: PromptFrameBundle | None = None,
) -> list[dict[str, Any]]:
    """Build the initial messages array for the Agent loop.

    Prior-turn context lives inside the canonical ``PromptFrameBundle`` and is
    rendered into role-based provider messages here. Claude normalizes one
    message chain (see ``src/query.ts:252-279``), so the agent loop accepts a
    single bundle and never a second legacy ``conversation_history`` lane.
    The R3-A F-030 fix removed the legacy alternate lane; any prior-turn
    source must be converted into the bundle at the API boundary before
    dispatch.
    """
    messages: list[dict[str, Any]] = []
    bundle_has_current_user_text = _prompt_bundle_has_current_user_text(prompt_context_bundle, user_text)

    if prompt_context_bundle is not None:
        rendered_context = render_for_anthropic(build_provider_prompt_bundle(context_bundle=prompt_context_bundle))
        for item in rendered_context.get("messages") or []:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str) and not content.strip():
                continue
            if content:
                messages.append({"role": str(item.get("role") or "user"), "content": content})

    if user_text and not bundle_has_current_user_text:
        messages.append({"role": "user", "content": user_text})

    return messages


def _prompt_bundle_has_current_user_text(prompt_context_bundle: PromptFrameBundle | None, user_text: str) -> bool:
    """Return True when the canonical bundle already ends with this user turn."""

    needle = str(user_text or "").strip()
    if not prompt_context_bundle or not needle:
        return False

    def _frame_text(frame: Any) -> str:
        parts: list[str] = []
        for block in getattr(frame, "blocks", ()) or ():
            if isinstance(block, TextBlock):
                parts.append(block.text)
        return "\n".join(parts).strip()

    current = getattr(prompt_context_bundle, "current_user_frame", None)
    if current is not None and getattr(current, "role", None) is FrameRole.USER:
        return _frame_text(current) == needle

    frames = prompt_context_bundle.to_messages()
    if frames:
        frame = frames[-1]
        if getattr(frame, "role", None) is FrameRole.USER:
            return _frame_text(frame) == needle
    return False


def _executor_hook_session_id(executor: AgentExecutor) -> str | None:
    session_id = getattr(executor, "_session_id", None)
    if session_id:
        return str(session_id)
    manager = getattr(executor, "_conversation_state_manager", None)
    manager_session_id = getattr(manager, "_session_id", None)
    return str(manager_session_id) if manager_session_id else None


def _hook_result_messages(
    executor: AgentExecutor,
    event_name: str,
    result: HookResult,
    *,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Render hook result context through canonical hook meta frames."""

    # NOTE: `result.system_message` belongs in the non-empty signal set.
    # A hook returning only `systemMessage` must still produce a model message;
    # render it here even though the dispatcher's frame helper supports
    # it. Claude emits `hook_system_message` attachments for every result with
    # `systemMessage` (`utils/hooks.ts:2769`), so we should at least render the
    # canonical hook meta frame so the model can observe it on the next turn.
    if (
        not result.additional_contexts
        and result.decision not in {"ask", "deny"}
        and not result.prevent_continuation
        and not result.system_message
    ):
        return []
    manager = getattr(executor, "_conversation_state_manager", None)
    session_id = _executor_hook_session_id(executor)
    event = HookEvent(
        name=event_name,
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
    )
    rendered_messages: list[dict[str, Any]] = []
    for frame in hook_result_frames(event, result, task_id=getattr(executor, "task_id", None)):
        if manager is not None and hasattr(manager, "add_message"):
            try:
                manager.add_message(frame)
            except Exception as exc:
                logger.warning("Hook frame persistence failed for %s: %s", event_name, exc)
        rendered = render_for_anthropic(PromptFrameBundle(frames=[frame]))
        rendered_messages.extend(
            dict(message) for message in rendered.get("messages") or [] if isinstance(message, dict)
        )
    return rendered_messages


def _hook_block_tool_result(tool_name: str, result: HookResult) -> ToolResult:
    """Convert a blocking hook result into a synthetic tool observation."""

    if result.prevent_continuation:
        category = "HOOK_PREVENTED_CONTINUATION"
        prefix = "Hook stopped continuation for %s" % tool_name
    elif result.decision == "ask":
        category = "HOOK_ASK"
        prefix = "Hook requested user confirmation before running %s" % tool_name
    else:
        category = "HOOK_DENIED"
        prefix = "Hook denied %s" % tool_name
    reason = result.reason or "No reason provided."
    return ToolResult(ok=False, error="%s: %s" % (prefix, reason), error_category=category)


def _tool_execution_hooks_registered(
    has_hook: Callable[[str], bool] = has_registered_hooks,
) -> bool:
    """Return True when batching would bypass a tool-side hook authority path."""

    return any(has_hook(event_name) for event_name in _TOOL_EXECUTION_HOOK_EVENTS)


def _hook_requests_model_continuation(result: HookResult) -> bool:
    """Return True when a terminal hook produced Claude-style model feedback."""

    return result.decision in {"ask", "deny"}


def _prepare_terminal_hook_continuation(executor: AgentExecutor) -> None:
    executor._raw_final_answer = None
    executor._final_answer = None
    executor._final_continue_listening = True


async def _terminal_hook_execute_tool_call(
    executor: AgentExecutor,
    response: dict[str, Any],
    loop_messages: list[dict[str, Any]],
    *,
    dispatch_fn: Callable[..., Any] | None = None,
    session_id: str | None = None,
    iteration: int = 0,
    task_id: str | None = None,
    queue_hook_context: Callable[..., None] | None = None,
    spin_detector: Any | None = None,
    taint_tracker: Any | None = None,
    page_url: str | None = None,
) -> bool:
    """Execute a single tool call returned during a Stop/SubagentStop block.

    Used by the post-loop continuation path (S1-003) when the model responds
    with a ``tool_call`` instead of an ``answer`` after a terminal hook
    requests model continuation.  Keep this path aligned with the ordinary
    per-tool lifecycle so terminal-hook feedback cannot create an unmediated
    tool execution lane.

    Returns True when the tool executed (success or graceful failure) and the
    result was appended to ``loop_messages``; False when the tool call was
    structurally invalid and the caller should abort the continuation loop.
    """

    tool_name = str(response.get("tool") or "").strip()
    tool_args = response.get("args") if isinstance(response.get("args"), dict) else {}
    tool_use_id = str(response.get("tool_use_id") or "").strip()
    if not tool_name:
        return False
    raw_content = response.get("_raw_content")
    if raw_content is not None:
        # Persist the assistant's tool-call frame so the next turn sees the
        # canonical request/response pairing (matches the in-loop behavior).
        loop_messages.append({"role": "assistant", "content": raw_content})
    else:
        loop_messages.append(
            {
                "role": "assistant",
                "content": {
                    "_openai_assistant": True,
                    "tool_calls": [
                        {
                            "id": tool_use_id or tool_name,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": __import__("json").dumps(tool_args, default=str),
                            },
                        }
                    ],
                },
            }
        )

    tool_error: str | None = None
    tool_result: ToolResult | None = None
    tool_executed = False
    if not _tool_allowed_by_executor(executor, tool_name):
        tool_error = "Tool '%s' is not allowed for this skill." % tool_name
        tool_result = ToolResult(ok=False, error=tool_error)
    elif tool_name in getattr(executor, "_rejected_tools", set()):
        tool_error = "Tool '%s' has been disabled for this task." % tool_name
        tool_result = ToolResult(ok=False, error=tool_error)

    if tool_result is None and spin_detector is not None:
        blocked_msg = getattr(spin_detector, "is_call_blocked", lambda *_args, **_kwargs: None)(
            tool_name,
            tool_args,
            page_url=page_url,
        )
        if blocked_msg:
            tool_error = str(blocked_msg)
            tool_result = ToolResult(ok=False, error=tool_error)

    if tool_result is None and taint_tracker is not None:
        taint_block = getattr(taint_tracker, "check_tool", lambda *_args, **_kwargs: None)(tool_name)
        if taint_block:
            tool_error = str(taint_block)
            tool_result = ToolResult(ok=False, error=tool_error)

    pre_tool_hook_result = HookResult()
    if tool_result is None:
        pre_tool_hook_result = dispatch_tool_hook(
            "PreToolUse",
            tool_name,
            tool_args,
            {
                "iteration": iteration,
                "task_id": task_id,
                "user_id": getattr(executor, "_user_id", None),
                "permission_mode": getattr(executor, "_permission_mode", None),
                "terminal_hook_continuation": True,
            },
            dispatch_fn=dispatch_fn,
            session_id=session_id,
        )
        if queue_hook_context is not None:
            queue_hook_context(
                "PreToolUse",
                pre_tool_hook_result,
                tool_name=tool_name,
                tool_input=tool_args,
            )
        if pre_tool_hook_result.updated_input is not None:
            tool_args = copy.deepcopy(pre_tool_hook_result.updated_input)
        if pre_tool_hook_result.blocks_tool:
            tool_result = _hook_block_tool_result(tool_name, pre_tool_hook_result)
            tool_error = tool_result.error
        elif pre_tool_hook_result.decision == "ask":
            tool_error = "PreToolUse requested approval during terminal hook continuation."
            tool_result = ToolResult(
                ok=False,
                error=tool_error,
                error_category="HOOK_ASK_APPROVAL_UNAVAILABLE",
            )

    if tool_result is None:
        validate_tool_args = getattr(executor, "_validate_tool_args", None)
        if callable(validate_tool_args):
            validation_error = validate_tool_args(tool_name, tool_args)
            if validation_error:
                tool_result = ToolResult(ok=False, error=validation_error)
                tool_error = validation_error

    if tool_result is None:
        try:
            tool_executed = True
            tool_result = await executor._execute_tool(
                tool_name,
                tool_args,
                tool_use_id=tool_use_id or None,
            )
        except Exception as tool_exc:
            logger.exception(
                "Terminal hook continuation tool '%s' raised: %s",
                tool_name,
                tool_exc,
            )
            tool_error = str(tool_exc)
            tool_result = ToolResult(ok=False, error=tool_error)

    if tool_executed and tool_result is not None:
        post_context = {
            "iteration": iteration,
            "task_id": task_id,
            "user_id": getattr(executor, "_user_id", None),
            "result": tool_result,
            "tool_error": tool_error or tool_result.error,
            "terminal_hook_continuation": True,
        }
        if tool_result.ok:
            post_tool_hook_result = dispatch_tool_hook(
                "PostToolUse",
                tool_name,
                tool_args,
                post_context,
                dispatch_fn=dispatch_fn,
                session_id=session_id,
            )
            if queue_hook_context is not None:
                queue_hook_context(
                    "PostToolUse",
                    post_tool_hook_result,
                    tool_name=tool_name,
                    tool_input=tool_args,
                )
            if post_tool_hook_result.prevent_continuation and tool_result.ok:
                tool_result = _hook_block_tool_result(tool_name, post_tool_hook_result)
                tool_error = tool_result.error
        else:
            failure_hook_result = dispatch_tool_hook(
                "PostToolUseFailure",
                tool_name,
                tool_args,
                {
                    **post_context,
                    "interrupted": tool_result.error_category == "CANCELLED",
                },
                dispatch_fn=dispatch_fn,
                session_id=session_id,
            )
            if queue_hook_context is not None:
                queue_hook_context(
                    "PostToolUseFailure",
                    failure_hook_result,
                    tool_name=tool_name,
                    tool_input=tool_args,
                )
        if tool_name in getattr(executor, "_c2_invalidation_tools", set()):
            executor.invalidate_context_cache()

    if tool_result is None:
        # Surface a synthetic error result so the next turn can recover or
        # explain itself; this matches the in-loop "tool execution failed"
        # frame shape.
        result_payload: dict[str, Any] = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id or tool_name,
                    "content": "[Tool execution failed during Stop-hook continuation]",
                    "is_error": True,
                }
            ],
        }
        loop_messages.append(result_payload)
        return True

    # Build the tool_result frame from the executed ToolResult.
    text_payload = ""
    if getattr(tool_result, "data", None) is not None:
        try:
            text_payload = (
                tool_result.data
                if isinstance(tool_result.data, str)
                else __import__("json").dumps(tool_result.data, default=str)
            )
        except Exception:
            text_payload = str(tool_result.data)
    elif getattr(tool_result, "error", None):
        text_payload = str(tool_result.error)
    is_error = not bool(getattr(tool_result, "ok", True))
    loop_messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id or tool_name,
                    "content": text_payload or "[no tool output]",
                    "is_error": is_error,
                }
            ],
        }
    )
    return True


def _current_subagent_id(executor: AgentExecutor) -> str | None:
    agent_id = str(getattr(executor, "_current_agent_id", "") or "").strip()
    return agent_id or None


def _terminal_hook_event_name(executor: AgentExecutor, outcome: str) -> str:
    if outcome == "llm_error":
        return "StopFailure"
    return "SubagentStop" if _current_subagent_id(executor) else "Stop"


def _terminal_hook_context(
    executor: AgentExecutor,
    *,
    event_name: str,
    outcome: str,
    task_id: str,
    tools_called: list[str],
    final_answer: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "outcome": outcome,
        "task_id": task_id,
        "tools_called": list(tools_called),
        "final_answer": final_answer,
        "last_assistant_message": final_answer,
        "user_id": getattr(executor, "_user_id", None),
    }
    if event_name == "StopFailure":
        context["error"] = outcome or "unknown"
        context["error_details"] = final_answer
    agent_id = _current_subagent_id(executor)
    if agent_id:
        user_id = str(getattr(executor, "_user_id", "") or "").strip()
        context.update(
            {
                "agent_id": agent_id,
                "agent_type": getattr(executor, "_subagent_type", None) or "",
                "agent_transcript_path": str(subagent_transcript_path(agent_id, user_id=user_id)),
                "parent_task_id": getattr(executor, "_parent_task_id", None),
            }
        )
    return context


def _task_completed_hook_context(
    executor: AgentExecutor,
    *,
    task_id: str,
    user_text: str,
    outcome: str,
    final_answer: str,
) -> dict[str, Any]:
    agent_id = _current_subagent_id(executor)
    return {
        "task_id": task_id,
        "task_subject": user_text[:120],
        "task_description": user_text,
        "task_result": final_answer,
        "outcome": outcome,
        "teammate_name": agent_id or getattr(executor, "_current_agent_name", None),
        "team_name": getattr(executor, "_session_id", None),
        "agent_id": agent_id,
        "agent_type": getattr(executor, "_subagent_type", None),
        "user_id": getattr(executor, "_user_id", None),
    }


def _teammate_idle_hook_context(executor: AgentExecutor, *, outcome: str, final_answer: str) -> dict[str, Any]:
    agent_id = _current_subagent_id(executor)
    return {
        "teammate_name": agent_id,
        "team_name": getattr(executor, "_session_id", None),
        "agent_id": agent_id,
        "agent_type": getattr(executor, "_subagent_type", None),
        "outcome": outcome,
        "final_answer": final_answer,
        "user_id": getattr(executor, "_user_id", None),
    }


def _file_changed_context_for_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: ToolResult,
    *,
    task_id: str,
    user_id: str | None,
) -> dict[str, Any] | None:
    if not tool_result.ok:
        return None
    path = _tool_arg_path(tool_args)
    action = str(tool_args.get("action") or "write").strip().lower()
    event = "change"
    if tool_name in {"delete_file"}:
        event = "unlink"
    elif tool_name == "file_write":
        if action not in {"write", "delete"}:
            return None
        event = "unlink" if action == "delete" else "change"
    elif tool_name not in {"write_file"}:
        return None
    if not path:
        return None
    return {
        "path": path,
        "file_path": path,
        "event": event,
        "task_id": task_id,
        "tool_name": tool_name,
        "user_id": user_id,
    }


def _cwd_changed_context_for_tool(
    executor: AgentExecutor,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: ToolResult,
    *,
    task_id: str,
    user_id: str | None,
) -> dict[str, Any] | None:
    if not tool_result.ok or tool_name != "run_command":
        return None
    new_cwd = str(tool_args.get("working_directory") or tool_args.get("cwd") or "").strip()
    if not new_cwd:
        return None
    old_cwd = str(getattr(executor, "_hook_current_cwd", None) or Path.cwd())
    if old_cwd == new_cwd:
        return None
    executor._hook_current_cwd = new_cwd
    return {
        "old_cwd": old_cwd,
        "new_cwd": new_cwd,
        "task_id": task_id,
        "tool_name": tool_name,
        "user_id": user_id,
    }


def _tool_arg_path(tool_args: dict[str, Any]) -> str | None:
    path = tool_args.get("path") or tool_args.get("file_path")
    if path is None:
        return None
    text = str(path).strip()
    return text or None


def _session_watch_paths(hook_registry: Any, session_id: str | None) -> tuple[str, ...]:
    if hook_registry is not None and hasattr(hook_registry, "watch_paths"):
        return tuple(hook_registry.watch_paths(session_id))
    return watch_paths(session_id)


def _build_watcher_dispatch(
    hook_dispatch_fn: Callable[..., Any],
) -> Callable[[HookEvent], HookResult]:
    def _dispatch_watcher_event(event: HookEvent) -> HookResult:
        return dispatch_lifecycle(
            event.name,
            dict(event.payload),
            dispatch_fn=hook_dispatch_fn,
            session_id=event.session_id,
            frame_uuid=event.frame_uuid,
        )

    return _dispatch_watcher_event


async def _ensure_hook_file_watcher(
    executor: AgentExecutor,
    *,
    session_id: str | None,
    paths: tuple[str, ...],
    hook_dispatch_fn: Callable[..., Any],
) -> Any:
    if not paths:
        return None
    from intent.hooks.watcher import HookFileWatcher

    watcher = getattr(executor, "_hook_file_watcher", None)
    if watcher is None:
        watcher = HookFileWatcher(dispatch_fn=_build_watcher_dispatch(hook_dispatch_fn))
        executor._hook_file_watcher = watcher
    watcher.register_paths(session_id, paths)
    if not watcher.is_running:
        await watcher.start()
    return watcher


class _NoopSpinDetector:
    def is_call_blocked(self, tool_name: str, tool_args: dict[str, Any], *, page_url: str | None = None) -> str | None:
        return None


def _extract_ask_user_text(tool_args: dict[str, Any]) -> str | None:
    """Extract a consultative question from ask_user-style arguments."""
    for key in ("question", "message", "text"):
        value = tool_args.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None


def _should_forward_tool_image_to_native(
    tool_name: str,
    tool_result: ToolResult,
    *,
    use_native: bool,
) -> bool:
    """Return True when an image block should be sent in the next native turn."""
    if not use_native or not isinstance(tool_result.data, dict) or not tool_result.data.get("image_base64"):
        return False
    if tool_name == "browser_screenshot" or tool_result.data.get("force_image_for_llm"):
        return True
    return "snapshot" not in tool_result.data


def _messages_include_tool_result(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
    return False


def _sanitize_agent_messages_for_checkpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return checkpoint-safe loop history with orphaned native tool calls repaired."""
    sanitized = copy.deepcopy(messages)
    index = 0
    while index < len(sanitized):
        message = sanitized[index]
        if message.get("role") != "assistant":
            index += 1
            continue
        content = message.get("content")
        tool_call_ids: list[str] = []
        if isinstance(content, dict) and content.get("_openai_assistant"):
            for call in content.get("tool_calls") or []:
                if isinstance(call, dict):
                    call_id = str(call.get("id") or "")
                    if call_id and call_id not in tool_call_ids:
                        tool_call_ids.append(call_id)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call_id = str(block.get("id") or "")
                    if call_id and call_id not in tool_call_ids:
                        tool_call_ids.append(call_id)
        if not tool_call_ids:
            index += 1
            continue

        next_message = sanitized[index + 1] if index + 1 < len(sanitized) else None
        next_content = next_message.get("content") if isinstance(next_message, dict) else None
        found_ids: set[str] = set()
        if isinstance(next_content, list):
            found_ids = {
                str(block.get("tool_use_id") or "")
                for block in next_content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            }
        missing_ids = [call_id for call_id in tool_call_ids if call_id not in found_ids]
        if not missing_ids:
            index += 1
            continue
        synthetic = [
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": SYNTHETIC_TOOL_RESULT_CONTENT,
                "is_error": True,
            }
            for call_id in missing_ids
        ]
        if isinstance(next_message, dict) and next_message.get("role") == "user":
            if isinstance(next_content, list):
                next_message["content"] = synthetic + next_content
            else:
                blocks = list(synthetic)
                if next_content:
                    blocks.append({"type": "text", "text": str(next_content)})
                next_message["content"] = blocks
        else:
            sanitized.insert(index + 1, {"role": "user", "content": synthetic})
            index += 1
        index += 1
    return sanitized


def _snapshot_agent_checkpoint(
    executor: AgentExecutor,
    checkpoint: Any,
    messages: list[dict[str, Any]],
    compress_messages_fn: Any,
) -> None:
    """Persist the Agent loop's live message history instead of executor._native_messages."""
    checkpoint.llm_messages = compress_messages_fn(_sanitize_agent_messages_for_checkpoint(messages))
    checkpoint.llm_continuity = (
        executor._export_responses_continuity_state() if getattr(executor, "_use_native", False) else {}
    )


def _cap_tool_result_data(tool_result: ToolResult, max_chars: int) -> ToolResult:
    """Mirror shared MAX_TOOL_RESULT_CHARS enforcement for arbitrary tool data."""
    if not tool_result.ok or tool_result.data is None:
        return tool_result
    data_str = str(tool_result.data)
    if len(data_str) <= max_chars:
        return tool_result
    return ToolResult(
        ok=tool_result.ok,
        data=data_str[:max_chars] + "\n\n[...truncated at %d chars - %d total]" % (max_chars, len(data_str)),
        mcp_content=tool_result.mcp_content,
        mcp_meta=tool_result.mcp_meta,
        error=tool_result.error,
        truncated=True,
        error_category=tool_result.error_category,
        retryable=tool_result.retryable,
        required_tier=tool_result.required_tier,
    )


def _tool_result_with_updated_mcp_tool_output(tool_result: ToolResult, updated_mcp_tool_output: Any) -> ToolResult:
    """Return a tool result whose model-visible data came from PostToolUse."""

    return ToolResult(
        ok=tool_result.ok,
        data=copy.deepcopy(updated_mcp_tool_output),
        error=tool_result.error,
        truncated=False,
        error_category=tool_result.error_category,
        retryable=tool_result.retryable,
        required_tier=tool_result.required_tier,
    )


def _is_mcp_tool(executor: AgentExecutor, tool_name: str) -> bool:
    """Return True when ``tool_name`` is routed through an external MCP server.

    Claude's ``services/tools/toolExecution.ts:1494-1498`` only applies
    ``updatedMCPToolOutput`` when ``isMcpTool(tool)`` — i.e., the tool came
    from a connected MCP server, not a built-in tool. Viola's MCP hub
    namespaces external server tools as ``{server_name}__{tool}`` (see
    ``mcp_hub/client_hub.py:_discover_tools``), but built-in servers (core,
    browser, computer, etc.) register tools under their natural names.

    The most reliable test is to look up the tool in ``mcp_hub._tool_routes``
    and check the resolved server name against a known set of external
    namespaces. We treat anything routed to a server that registers tools
    with the ``{server}__`` prefix as MCP. As a structural fallback, the
    double-underscore prefix in the tool name is itself a strong signal
    (no built-in tool uses it).

    ``updated_mcp_tool_output`` is applied to
    every PostToolUse result, letting a hook rewrite browser/shell/native
    output that Claude would leave intact.
    """

    if not tool_name:
        return False
    if "__" in tool_name:
        # External MCP namespacing (Claude also uses ``mcp__server__tool``).
        return True
    hub = getattr(executor, "_mcp_hub", None)
    if hub is None:
        return False
    routes = getattr(hub, "_tool_routes", None)
    if not isinstance(routes, dict):
        return False
    server_name = routes.get(tool_name)
    if not server_name:
        return False
    # Built-in servers register tools without the ``{server}__`` namespace.
    # Their server names appear in ``_configs`` with ``namespace=False``.
    configs = getattr(hub, "_configs", None)
    if isinstance(configs, dict):
        config = configs.get(server_name)
        if config is not None:
            return bool(getattr(config, "namespace", False))
    # Unknown config → conservatively treat as non-MCP so a hook cannot
    # silently mutate a built-in tool's result.
    return False


def _build_permission_suggestions(
    *,
    permission_policy: Any,
    tool_name: str,
    args: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build Claude-shape ``permission_suggestions`` for a PermissionRequest hook.

    Mirrors Claude's input shape (PermissionRequestHookInput at
    ``src/entrypoints/sdk/coreSchemas.ts:425-431``): a list of suggested
    persisted permission updates the hook can accept, reject, or rewrite.

    Each suggestion is ``{"type": "add", "behavior": "allow", "tool": "..."}``,
    safe to ignore but visible enough that a hook can short-circuit a prompt
    by acknowledging one.
    """

    suggestions: list[dict[str, Any]] = [
        {
            "type": "add",
            "behavior": "allow",
            "tool": tool_name,
            "scope": "session",
        },
    ]
    # Surface the input shape so a hook can decide whether to suggest a
    # narrower rule like ``Bash(git *)``.
    if isinstance(args, dict):
        for key in ("command", "path", "file_path", "url"):
            value = args.get(key)
            if isinstance(value, str) and value:
                suggestions.append(
                    {
                        "type": "add",
                        "behavior": "allow",
                        "tool": tool_name,
                        "content_hint": value,
                        "scope": "session",
                    }
                )
                break
    return suggestions


def _apply_updated_permissions(
    *,
    permission_policy: Any,
    hook_result: HookResult,
    tool_name: str,
) -> None:
    """Persist Claude-style ``updatedPermissions`` from a PermissionRequest hook.

    Each entry is ``{"type": "add" | "remove", "behavior": "allow" | "deny" |
    "ask", "tool": "<name>", "scope": "session" | "project" | "user"}``.

    Only ``type=="add"`` with a valid behavior is materialized as a runtime
    ``PermissionRule``. Persistent storage of non-session scopes is out of
    scope for the agent loop; we honor session scope by appending to the
    policy's mutable rule list so subsequent tool calls in this session see
    the new rule (matches Claude's "session" scope expectation).
    """

    if not hook_result.updated_permissions:
        return
    rule_list = getattr(permission_policy, "_rules", None)
    if rule_list is None:
        return
    new_rules: list[Any] = []
    from intent.permissions.policy import PermissionRule

    for entry in hook_result.updated_permissions:
        if not isinstance(entry, dict):
            continue
        op = str(entry.get("type") or "add").strip().lower()
        behavior = str(entry.get("behavior") or "allow").strip().lower()
        rule_tool = str(entry.get("tool") or entry.get("toolName") or tool_name).strip()
        if not rule_tool or behavior not in {"allow", "ask", "deny"} or op != "add":
            continue
        new_rules.append(
            PermissionRule(
                tool_name=rule_tool,
                behavior=behavior,  # type: ignore[arg-type] # AGENT-02: hook payload is validated before provenance capture.
                source="hook:PermissionRequest",
                reason=str(entry.get("reason") or "PermissionRequest hook accepted this permission"),
            )
        )
    if new_rules:
        try:
            permission_policy._rules = tuple([*new_rules, *rule_list])
        except Exception as exc:
            logger.debug("Could not persist updatedPermissions: %s", exc)


def _answer_text_from_tool_args(tool_args: dict[str, Any]) -> tuple[str | None, bool]:
    answer = tool_args.get("answer", tool_args.get("text", ""))
    text = answer if isinstance(answer, str) else str(answer) if answer is not None else ""
    continue_listening = bool(tool_args.get("continue_listening"))
    if tool_args.get("suggest_followup"):
        continue_listening = True
    return text, continue_listening


def _agent_response_parse_failure_reason(response: Any) -> str | None:
    if not isinstance(response, dict):
        return "non_dict_response"
    response_type = str(response.get("type") or "")
    if response_type == "tool_call":
        tool_name = response.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return "malformed_tool_call"
        args = response.get("args", {})
        if args is not None and not isinstance(args, dict):
            return "malformed_tool_args"
        return None
    known = {
        "ai_no_result",
        "answer",
        "ask_user",
        "clarification",
        "final_answer",
        "payment_gate",
        "signature_gate",
        "text",
    }
    if response_type not in known:
        return "unknown_response_type:%s" % (response_type or "<empty>")
    return None


def _browser_nav_detected_from_tool_results(
    tool_names: list[str],
    tool_results_content: list[dict[str, Any]],
) -> bool:
    """Detect a page navigation in the most recent batch of tool results.

    Reads the structured ``refs_invalidated`` flag emitted by browser tools
    (``mcp_servers/browser/server.py`` and ``mcp_servers/browser_cdp/server.py``)
    on navigate / back / forward / refresh / click-causing-navigation /
    Enter-key-causing-navigation. The producer side is now authoritative — no
    substring scans of tool_result payload text are performed here.

    See ratchet ``r5_p0_o_no_tool_result_substring_scan_for_state``.
    """
    for block in tool_results_content:
        if not isinstance(block, dict):
            continue
        content = block.get("content")
        # Structured envelope: a tool result envelope from the agent path
        # carries a parsed-dict payload that may surface the flag directly.
        if isinstance(content, dict) and content.get("refs_invalidated") is True:
            return True
        # MCP / native envelope: payload is a JSON string. Parse it instead
        # of substring-scanning — the producer always emits valid JSON.
        if isinstance(content, str) and content:
            try:
                parsed = _json.loads(content)
            except (ValueError, TypeError):
                continue
            if _payload_signals_refs_invalidated(parsed):
                return True
    return False


def _payload_signals_refs_invalidated(payload: Any) -> bool:
    """Return True when a parsed tool-result payload carries the structured flag."""
    if isinstance(payload, dict):
        if payload.get("refs_invalidated") is True:
            return True
        # MCP envelope: {"data": {"refs_invalidated": true, ...}}
        data = payload.get("data")
        if isinstance(data, dict) and data.get("refs_invalidated") is True:
            return True
    return False


_MEMORY_ID_RE = re.compile(r"\bid=(\d+)\b")


def _set_final_response(
    executor: AgentExecutor,
    answer: str | None,
    *,
    continue_listening: bool | None = None,
    params: dict[str, Any] | None = None,
    display_answer: str | None = None,
) -> None:
    """Store the final response contract in one place.

    The optional ``display_answer`` is the user-visible copy; ``answer`` is
    retained as raw trace text. ``params`` is forwarded to
    ``executor._final_params`` so AIController._record_exchange can read
    provenance/diagnostic fields (e.g.
    ``command_params.error_state.diagnostic.category``) and refuse to persist
    runtime-synthesized fallback answers as Assistant turns. This is the same
    contract as the agent_executor._set_final_response helper at
    intent/agent_executor.py.
    """
    executor._raw_final_answer = answer
    executor._final_answer = display_answer if display_answer is not None else answer
    executor._final_continue_listening = continue_listening
    if params is not None:
        merged_params = dict(getattr(executor, "_final_params", {}) or {})
        for key, value in params.items():
            existing = merged_params.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                nested = dict(existing)
                nested.update(value)
                merged_params[key] = nested
            else:
                merged_params[key] = value
        executor._final_params = merged_params


def _apply_typed_review_gate_response(executor: AgentExecutor, response_type: str, raw_gate_message: str) -> str:
    """Route explicit typed gate responses through gate finalization."""
    gate_message = _strip_json_template(raw_gate_message)
    _set_final_response(executor, gate_message, continue_listening=False)
    if response_type == "payment_gate":
        executor._payment_gate_requested = True
        gate = "payment"
    elif response_type == "signature_gate":
        executor._signature_gate_requested = True
        gate = "signature"
    else:
        return gate_message

    append_gate = getattr(executor, "_append_task_trace_gate_event", None)
    if callable(append_gate):
        append_gate(
            gate=gate,
            state="requested",
            message=gate_message,
            gate_origin="typed_response",
        )
    return gate_message


def _get_mcp_server_status(provider: Any, mcp_hub: Any | None = None) -> dict[str, bool]:
    """Return live MCP readiness, preferring the hub over provider shims."""
    hub_status = getattr(mcp_hub, "get_server_health_flags", None)
    if callable(hub_status):
        try:
            status = hub_status()
        except Exception:
            logger.exception("MCP hub health flag lookup failed")
        else:
            if isinstance(status, dict):
                return {str(key): bool(value) for key, value in status.items()}

    provider_status = getattr(provider, "mcp_server_status", {})
    if isinstance(provider_status, dict):
        return {str(key): bool(value) for key, value in provider_status.items()}
    return {}


def _get_execution_path_tool_status(
    provider: Any,
    request_native_tools: list[dict[str, Any]] | None = None,
    *,
    mcp_server_status: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Return tool availability for the provider's actual native execution path."""
    mcp_status = mcp_server_status if isinstance(mcp_server_status, dict) else _get_mcp_server_status(provider)
    if isinstance(request_native_tools, list):
        request_tool_count = len(request_native_tools)
        if request_tool_count:
            return {
                "execution_path": "request_native_tools",
                "has_tools": True,
                "tool_count": request_tool_count,
                "mcp_server_status": mcp_status,
            }

    status_getter = getattr(provider, "get_native_tool_availability", None)
    if callable(status_getter):
        try:
            status = status_getter()
        except Exception as exc:
            logger.warning(
                "Native tool availability check failed for %s: %s",
                type(provider).__name__,
                exc,
            )
        else:
            if isinstance(status, dict):
                status = dict(status)
                status["mcp_server_status"] = mcp_status
                return status

    native_tools = getattr(provider, "_native_tools", None)
    if not isinstance(native_tools, list):
        compat_provider = getattr(provider, "_compat_provider", None)
        native_tools = getattr(compat_provider, "_native_tools", None)
    if isinstance(native_tools, list):
        return {
            "execution_path": "native_tools",
            "has_tools": bool(native_tools),
            "tool_count": len(native_tools),
            "mcp_server_status": mcp_status,
        }

    active_native_tool_count = getattr(provider, "active_native_tool_count", None)
    if isinstance(active_native_tool_count, int):
        return {
            "execution_path": "active_native_tool_count",
            "has_tools": active_native_tool_count > 0,
            "tool_count": active_native_tool_count,
            "mcp_server_status": mcp_status,
        }

    if isinstance(mcp_status, dict):
        return {
            "execution_path": "mcp_sessions",
            "has_tools": any(bool(value) for value in mcp_status.values()),
            "tool_count": 0,
            "mcp_server_status": mcp_status,
        }

    return {"execution_path": "unknown", "has_tools": False, "tool_count": 0}


def _zero_native_tools_allowed(executor: Any) -> bool:
    """Return True only for explicit tests/callers that intentionally use no tools."""
    return bool(getattr(executor, "_allow_zero_native_tools", False))


def _provider_model_name(provider: Any, executor: Any) -> str:
    effective = getattr(provider, "effective_model", "")
    if isinstance(effective, str) and effective.strip():
        return effective.strip()
    get_model_name = getattr(executor, "_get_model_name_safe", None)
    if callable(get_model_name):
        return str(get_model_name() or "")
    return "unknown"


def _tool_call_to_response(tool_call: ToolCall | None) -> dict[str, Any] | None:
    """Rebuild the normalized provider response for a preselected tool call."""
    if tool_call is None:
        return None
    response: dict[str, Any] = {
        "type": "tool_call",
        "tool": tool_call.tool,
        "args": tool_call.args,
    }
    if tool_call.tool_use_id:
        response["tool_use_id"] = tool_call.tool_use_id
    if tool_call.raw_content is not None:
        response["_raw_content"] = tool_call.raw_content
    if tool_call.all_tool_calls:
        response["_all_tool_calls"] = tool_call.all_tool_calls
    if tool_call.reasoning:
        response["_reasoning"] = tool_call.reasoning
    return response


# _PASSIVE_TOOLS and _ACTIONABLE_AGENT_CATEGORIES remain removed; the agent
# loop does not restore classifier-selected tool surfaces or runtime prompt
# nudges.

# ---------------------------------------------------------------------------
# Content sanitization for prompt injection defense
# ---------------------------------------------------------------------------
from intent.content_sanitizer import (
    BROWSER_CONTENT_TOOLS as _BROWSER_CONTENT_TOOLS,
    BROWSER_PAGE_CONTENT_TOOLS as _BROWSER_PAGE_CONTENT_TOOLS,
    BrowserTaintTracker,
    is_non_browser_untrusted_source as _is_non_browser_untrusted_source,
    sanitize_web_content as _sanitize_web_content,
)

# Browser tier map (mirrors agent_executor)
_BROWSER_TIER_MAP: dict[str, str] = {
    "browser_navigate": "navigate",
    "browser_snapshot": "snapshot",
    "browser_interact": "interact",
    "browser_fill_form": "fill",
    "browser_screenshot": "screenshot",
    "browser_run_script": "script",
    "browser_scroll": "scroll",
    "browser_wait": "wait",
}

# Progress phrases per tool (subset â€” keep it short)
_TOOL_PROGRESS_PHRASES: dict[str, str] = {
    "web_search": "Searching the web...",
    "web_read": "Reading that page...",
    "browser_navigate": "Opening the page...",
    "browser_interact": "Clicking...",
    "browser_fill_form": "Filling in the form...",
    "play_music": "Getting that ready for you...",
}


def _tool_allowed_by_executor(executor: AgentExecutor, tool_name: str) -> bool:
    """Return whether the current request-scoped allowlist permits a tool."""

    allowed = getattr(executor, "_allowed_tools", None)
    if not allowed:
        return True
    normalized_allowed = {_normalize_tool_permission_name(name) for name in allowed if str(name).strip()}
    if "*" in normalized_allowed:
        return True
    normalized_tool = _normalize_tool_permission_name(tool_name)
    candidates = {
        normalized_tool,
        _normalize_tool_permission_name(normalized_tool.split("__")[-1]),
        _normalize_tool_permission_name(normalized_tool.rsplit(".", 1)[-1]),
    }
    return bool(normalized_allowed.intersection(candidates))


def _normalize_tool_permission_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


# _summarize and _summarize_args moved to intent.agent_middleware.
# StepLoggerMiddleware handles all step-level logging and summarization.


async def run_agent_loop(
    executor: AgentExecutor,
    provider: Any,
    task_log: Any,
    user_text: str,
    checkpoint: Any,
    system_prompt: str | None = None,
    prompt_context_bundle: PromptFrameBundle | None = None,
    initial_tool_call: ToolCall | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
    start_time: float | None = None,
) -> AgentResult:
    """Run the canonical agent loop.

    The loop relies on provider-native MCP tool calling and owns the safety and
    observability behavior that used to be split across multiple loop paths:
    - Pre-iteration: zero-tools warning, cancellation, timeout+auto-extend
    - Per-tool: TTS progress, ask_user conversion, tool set enforcement,
      rejected tools, validation, execution, snapshot compression, image handling
    - Post-tool: URL tracking, tier tracking, step logging,
      WS broadcast, telemetry, checkpointing, blacklisting
    - Post-iteration: corrections, payment gate
      breaker, force termination, failure warning, @ref stripping, step reminders
    - Loop exit: max iterations summary, payment gate verification, result building
    """
    if not hasattr(provider, "route_command_native"):
        raise RuntimeError("Agent loop invoked for provider %s without route_command_native" % type(provider).__name__)

    # Import executor internals we need
    from intent.agent_executor import (
        _DEFAULT_MAX_ITERATIONS,
        MAX_TOOL_RESULT_CHARS,
        _build_agent_final_answer_fallback,
        _calendar_add_failure_user_message,
        _compress_browser_result,
        _dispatch_hook,
        _estimated_tokens_for_messages,
        _extract_reasoning,
        _nested_gate_from_resume_tool_result,
        _normalize_usage,
        _PaymentConfirmationGateRefused,
        _record_structured_incomplete_diagnostic,
        _remember_last_tool_result,
        _strip_image_from_result,
        _strip_old_browser_refs,
        _terminal_response_from_tool_result,
        _terminal_structured_incomplete_diagnostic,
        compress_messages,
        mark_complete,
        save_checkpoint,
    )
    from intent.spin_detector import SpinDetector

    hook_registry = getattr(executor, "_hook_registry", None)
    hook_dispatch_fn = getattr(executor, "dispatch_hook", _dispatch_hook)
    hook_user_id = getattr(executor, "_user_id", None)

    def _has_registered_hook(event_name: str) -> bool:
        if hook_registry is not None and hasattr(hook_registry, "has_hooks"):
            return bool(hook_registry.has_hooks(event_name, user_id=hook_user_id))
        return has_registered_hooks(event_name, user_id=hook_user_id)

    def _take_initial_user_message(session_id: str | None) -> str | None:
        if hook_registry is not None and hasattr(hook_registry, "take_initial_user_message"):
            return hook_registry.take_initial_user_message(session_id)
        return take_initial_user_message(session_id)

    task_id = executor.task_id
    session_memory = None
    session_memory_user_id = str(getattr(executor, "_user_id", "") or "").strip()
    if session_memory_user_id:
        try:
            from services.conversation.session_memory import SessionMemory

            session_memory = SessionMemory(
                user_id=session_memory_user_id,
                session_id=str(task_id),
            )
            session_memory.set_task_description(user_text[:200])
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Agent loop SessionMemory construction failed: %s", exc)
            session_memory = None
    else:
        logger.debug("Agent loop SessionMemory disabled without executor user_id")

    # State
    iterations = 0
    tools_called: list[str] = []
    outcome = "success"
    run_start_time = start_time if start_time is not None else time.monotonic()
    hook_session_id = _executor_hook_session_id(executor) or str(task_id)
    _billing_fail_count = 0
    _next_first_turn = False
    spin_detector = SpinDetector()
    executor._spin_detector = spin_detector

    # L9 per-command managed-LLM spend guard (defense-in-depth). Bounds the
    # blast radius of a SINGLE command so one runaway (e.g. the 2026-07-06
    # "play drake" browser-navigation loop) cannot drain most of the user's
    # weekly/monthly managed allowance before the period cap trips. Built lazily
    # on the first managed turn (needs the user's remaining allowance) and None
    # for BYOK/Codex/local sources, where Viola is not charged.
    _per_command_spend_guard: Any | None = None
    _per_command_guard_built = False

    def _agent_turn_output_estimate(max_tokens: int | None) -> int:
        if isinstance(max_tokens, int) and max_tokens > 0:
            return max_tokens
        return _ESCALATED_MAX_OUTPUT_TOKENS

    def _agent_turn_model_name(turn_kwargs: dict[str, Any], response: object | None = None) -> str:
        if isinstance(response, dict):
            response_model = response.get("_model_name")
            if isinstance(response_model, str) and response_model.strip():
                return response_model.strip()
        override = turn_kwargs.get("model_override")
        if isinstance(override, str) and override.strip():
            return override.strip()
        return _provider_model_name(provider, executor)

    def _estimate_agent_turn_usage(
        *,
        turn_messages: list[dict[str, Any]],
        turn_kwargs: dict[str, Any],
        max_tokens: int | None,
    ) -> Any:
        from services.llm.spend_accounting import (
            LlmTokenUsage,
            estimate_openai_payload_usage,
        )

        output_tokens = _agent_turn_output_estimate(max_tokens)
        estimate_payload = {
            key: value for key, value in turn_kwargs.items() if key not in {"executor", "messages", "first_turn"}
        }
        estimate_payload["messages"] = turn_messages
        estimate_payload["max_output_tokens"] = output_tokens
        payload_usage = estimate_openai_payload_usage(
            estimate_payload,
            default_output_tokens=output_tokens,
        )
        input_tokens = max(
            int(payload_usage.input_tokens or 0),
            _estimated_tokens_for_messages(turn_messages),
        )
        system_prompt = turn_kwargs.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt:
            input_tokens = max(input_tokens, max(1, len(system_prompt) // 4))
        return LlmTokenUsage(input_tokens=max(1, input_tokens), output_tokens=output_tokens)

    def _actual_agent_turn_usage(response: object, fallback: Any) -> Any:
        from services.llm.spend_accounting import LlmTokenUsage

        usage = _normalize_usage(response) if isinstance(response, dict) else {}
        if not isinstance(usage, dict):
            return fallback
        input_tokens = executor._usage_int(usage, "input_tokens", "prompt_tokens")
        output_tokens = executor._usage_int(usage, "output_tokens", "completion_tokens")
        cached_tokens = executor._usage_int(usage, "cache_read_tokens", "cached_tokens", "cache_read_input_tokens")
        cache_write_tokens = executor._usage_int(
            usage,
            "cache_write_tokens",
            "cache_creation_tokens",
            "cache_creation_input_tokens",
        )
        web_search_requests = executor._usage_int(usage, "web_search_requests")
        if input_tokens or output_tokens or cached_tokens or cache_write_tokens or web_search_requests:
            return LlmTokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
                web_search_requests=web_search_requests,
            )
        return fallback

    def _handle_agent_loop_billing_failure(exc: BaseException) -> bool:
        nonlocal _billing_fail_count, outcome

        _billing_fail_count += 1
        logger.warning(
            "Agent loop billing failed (attempt %d): %s",
            _billing_fail_count,
            exc,
        )
        outcome = "cost_limit"
        _set_final_response(
            executor,
            "I can't verify your usage budget right now. Please try again shortly.",
            continue_listening=False,
        )
        return True

    async def _handle_spend_reservation_denial(reservation: Any, exc: BaseException) -> None:
        nonlocal outcome

        try:
            await reservation.settle(failed=True)
        except Exception as settle_exc:
            if _handle_agent_loop_billing_failure(settle_exc):
                raise _AgentLoopBillingStop() from settle_exc

        gate = getattr(reservation, "_last_gate", None)
        if gate is None:
            from services.llm.managed_budget import ManagedLlmBudgetGate

            gate = ManagedLlmBudgetGate(
                allowed=False,
                spent_cents=int(getattr(exc, "current", 0) or 0),
                budget_cents=int(getattr(exc, "limit", 0) or 0),
                resets_at=str(getattr(exc, "reset_at", "") or ""),
                reason=str(exc) or "managed LLM spend cap reached",
            )
        executor._record_agent_gate_denial_telemetry(
            gate_name="managed_llm_spend_cap",
            current_usage=int(getattr(gate, "spent_cents", 0) or 0),
            limit_value=int(getattr(gate, "budget_cents", 0) or 0),
        )
        _set_final_response(
            executor,
            executor._managed_llm_budget_message(gate),
            continue_listening=False,
        )
        executor._final_params["cap_state"] = getattr(gate, "cap_state", {})
        outcome = "cost_limit"
        raise _AgentLoopCostLimitStop() from exc

    def _ensure_per_command_spend_guard() -> Any | None:
        """Build the per-command spend guard once, on the first managed turn."""
        nonlocal _per_command_spend_guard, _per_command_guard_built
        if _per_command_guard_built:
            return _per_command_spend_guard
        _per_command_guard_built = True
        try:
            from services.llm.managed_budget import build_per_command_spend_guard

            _per_command_spend_guard = build_per_command_spend_guard(executor._user_id)
        except Exception:  # noqa: BLE001, RUF100 - fail closed on ANY construction error, never disable the bound
            # Fail closed: an unbuildable guard must NOT silently disable the
            # per-command bound. Fall back to a ceiling-only guard so blast
            # radius stays capped even when guard construction errors.
            from billing.per_command_spend_guard import (
                PER_COMMAND_CEILING_CENTS,
                PerCommandSpendGuard,
            )

            logger.warning(
                "Per-command spend guard construction failed; using ceiling-only guard",
                exc_info=True,
            )
            _per_command_spend_guard = PerCommandSpendGuard(
                cap_cents=PER_COMMAND_CEILING_CENTS,
                degraded=True,
            )
        return _per_command_spend_guard

    def _halt_for_per_command_spend_cap(guard: Any) -> None:
        """Stop the command when its own per-command spend cap is reached.

        Distinct from the weekly/monthly cap: the user's overall balance is
        fine; this single command hit a per-request safety limit. Sets the
        distinct final response + ``cost_limit`` outcome and raises the shared
        cost-limit stop so the loop breaks cleanly.
        """
        nonlocal outcome
        from services.llm.managed_budget import per_command_spend_cap_message

        guard.mark_halted()
        logger.warning(
            "Per-command managed spend cap reached for %s (spent=%dc cap=%dc degraded=%s); halting command",
            executor._user_id,
            int(getattr(guard, "spent_cents", 0) or 0),
            int(getattr(guard, "cap_cents", 0) or 0),
            bool(getattr(guard, "degraded", False)),
        )
        try:
            executor._record_agent_gate_denial_telemetry(
                gate_name="per_command_spend_cap",
                current_usage=int(getattr(guard, "spent_cents", 0) or 0),
                limit_value=int(getattr(guard, "cap_cents", 0) or 0),
            )
        except Exception:  # noqa: BLE001, RUF100 - best-effort telemetry must never block the halt
            logger.debug("Per-command spend cap telemetry write failed", exc_info=True)
        _set_final_response(
            executor,
            per_command_spend_cap_message(),
            continue_listening=False,
        )
        executor._final_params["stop_reason"] = "per_command_spend_cap"
        executor._final_params["per_command_spend_cap"] = {
            "spent_cents": int(getattr(guard, "spent_cents", 0) or 0),
            "cap_cents": int(getattr(guard, "cap_cents", 0) or 0),
            "degraded": bool(getattr(guard, "degraded", False)),
        }
        outcome = "cost_limit"
        raise _AgentLoopCostLimitStop()

    async def _reserve_agent_turn_spend(
        *,
        turn_messages: list[dict[str, Any]],
        turn_kwargs: dict[str, Any],
        max_tokens: int | None,
        call_kind: str,
    ) -> tuple[Any | None, Any | None]:
        if not executor._user_id:
            return None, None

        # Enforce the per-command spend cap BEFORE this turn's provider call so
        # the worst-case overshoot is the cap plus at most one turn's spend.
        _guard = _ensure_per_command_spend_guard()
        if _guard is not None and _guard.exceeded():
            _halt_for_per_command_spend_cap(_guard)

        from core.exceptions import LLMQuotaExceededError
        from services.llm.spend_accounting import LlmSpendReservation

        estimated_usage = _estimate_agent_turn_usage(
            turn_messages=turn_messages,
            turn_kwargs=turn_kwargs,
            max_tokens=max_tokens,
        )
        reservation = LlmSpendReservation(
            user_id=executor._user_id,
            model=_agent_turn_model_name(turn_kwargs),
            estimated_usage=estimated_usage,
            operation=call_kind,
            fail_closed_on_settle_error=False,
            reserve_tokens=False,
        )
        try:
            await reservation.reserve()
        except LLMQuotaExceededError as exc:
            if getattr(exc, "limit_type", "") != "managed_llm_spend_cap":
                await _settle_agent_turn_spend_failed(reservation)
                raise
            await _handle_spend_reservation_denial(reservation, exc)
        return reservation, estimated_usage

    async def _settle_agent_turn_spend_failed(reservation: Any | None) -> None:
        if reservation is None:
            return
        try:
            await reservation.settle(failed=True)
        except Exception as settle_exc:
            if _handle_agent_loop_billing_failure(settle_exc):
                raise _AgentLoopBillingStop() from settle_exc

    async def _settle_agent_turn_spend_success(
        *,
        reservation: Any | None,
        estimated_usage: Any | None,
        response: object,
        turn_kwargs: dict[str, Any],
    ) -> None:
        if reservation is None:
            return
        if estimated_usage is None:
            return
        reservation.model = _agent_turn_model_name(turn_kwargs, response)
        actual_usage = _actual_agent_turn_usage(response, estimated_usage)
        try:
            await reservation.settle(actual_usage)
        except Exception as settle_exc:
            if _handle_agent_loop_billing_failure(settle_exc):
                raise _AgentLoopBillingStop() from settle_exc

        # Accumulate this turn's settled managed spend into the per-command
        # guard. Only managed-LLM reservations carry a durable spend hold
        # (``_spend_reservation``); BYOK/Codex/local turns never charge Viola,
        # so they never move the per-command counter.
        _guard = _per_command_spend_guard
        if _guard is not None and getattr(reservation, "_spend_reservation", None) is not None:
            from services.llm.spend_accounting import estimated_spend_cents

            _guard.record(estimated_spend_cents(reservation.model, actual_usage))

    # Layer 6: deterministic browser taint tracker (M3 prompt injection defense).
    # Once any browser/web tool returns content, high-risk tools are blocked
    # for the remainder of this agent task.  Structural gate â€” no regex bypass.
    taint_tracker = BrowserTaintTracker()

    # Initialize attributes the agent loop reads/writes on executor that the
    # previous inline path set dynamically but are never in __init__.
    if not hasattr(executor, "_final_continue_listening"):
        executor._final_continue_listening = False
    if not hasattr(executor, "_final_command"):
        executor._final_command = None
    if not hasattr(executor, "_final_params"):
        executor._final_params = {}
    if not hasattr(executor, "_llm_error_occurred"):
        executor._llm_error_occurred = False
    if not hasattr(executor, "_parse_failed"):
        executor._parse_failed = False
    if not hasattr(executor, "_retry_count"):
        executor._retry_count = 0
    if not hasattr(executor, "_pending_correction_msgs"):
        executor._pending_correction_msgs = None
    if not hasattr(executor, "_pending_consecutive_warning"):
        executor._pending_consecutive_warning = None
    if not hasattr(executor, "_pending_error_registry_ctx"):
        executor._pending_error_registry_ctx = None
    if not hasattr(executor, "_pending_hard_loop_breaker"):
        executor._pending_hard_loop_breaker = None
    if not hasattr(executor, "_pending_force_termination"):
        executor._pending_force_termination = None
    if not hasattr(executor, "_pending_stuck_tool_progress"):
        executor._pending_stuck_tool_progress = None
    if not hasattr(executor, "_pending_compaction_meta"):
        executor._pending_compaction_meta = None
    if not hasattr(executor, "_pending_trim_stats"):
        executor._pending_trim_stats = None
    executor._forced_tool_failure_error = None
    executor._forced_tool_failure_outcome = None
    executor._consecutive_low_output = 0
    executor._snapshot_compressed_this_step = False

    # â”€â”€ Middleware pipeline â”€â”€
    # Each middleware processes tool results independently.  The pipeline
    # replaces the 240-line inline bookkeeping block with composable,
    # testable components.
    mw_ctx = LoopContext()
    middlewares = [
        URLTrackerMiddleware(executor=executor),
        SpinDetectorMiddleware(spin_detector),
        ToolBlacklistMiddleware(
            rejected_tools=executor._rejected_tools,
            task_id=task_id,
            executor=executor,
        ),
        ClickBackoffMiddleware(),
        PaymentGateMiddleware(executor=executor),
        SignatureGateMiddleware(executor=executor),
        ProgressReporterMiddleware(speak_fn=None),  # TTS handled inline pre-execution
        StepLoggerMiddleware(executor=executor, task_log=task_log, checkpoint=checkpoint, task_id=task_id),
    ]
    # Some providers still reuse the Agent loop but execute native turns through a
    # compat-backed route_command_native() path that does not require live MCP
    # server sessions. For those providers, forcing stdio server preconnect here
    # can abort the task before the real tool path starts.
    _requires_mcp_preconnect = bool(getattr(provider, "sdk_requires_mcp_preconnect", True))
    if _requires_mcp_preconnect:
        ensure_servers_ready = getattr(provider, "ensure_servers_ready", None)
        if callable(ensure_servers_ready):
            maybe_ready = ensure_servers_ready()
            if inspect.isawaitable(maybe_ready):
                await maybe_ready
    mcp_status = _get_mcp_server_status(provider, getattr(executor, "_mcp_hub", None))
    request_native_tools = executor._get_request_native_tools()
    tool_status = _get_execution_path_tool_status(
        provider,
        request_native_tools,
        mcp_server_status=mcp_status,
    )
    logger.info(
        "Agent loop starting: task_id=%s, model=%s, mcp=%s, native_tool_status=%s",
        task_id,
        _provider_model_name(provider, executor),
        mcp_status,
        tool_status,
    )
    request_tools_empty = isinstance(request_native_tools, list) and len(request_native_tools) == 0
    if (request_tools_empty or int(tool_status.get("tool_count") or 0) == 0) and not _zero_native_tools_allowed(
        executor
    ):
        logger.error(
            "ZERO native tools available at agent start for task '%s' " "(execution_path=%s, mcp=%s, request_tools=%s)",
            task_id,
            tool_status.get("execution_path", "unknown"),
            mcp_status,
            "empty" if isinstance(request_native_tools, list) else "missing",
        )
        raise RuntimeError("ZERO_NATIVE_TOOLS: refusing to start agent loop without provider-visible native tools")

    def _publish_deferred_tool_pool() -> None:
        pool = getattr(executor, "_request_deferred_tool_pool", None)
        if pool is None:
            return
        try:
            from intent.tools.tool_search import set_deferred_tool_pool

            set_deferred_tool_pool(pool)
        except Exception:
            logger.exception("Failed to publish request deferred tool pool")

    # Build initial messages from the canonical PromptFrameBundle and the
    # current user request.
    if initial_messages is not None:
        loop_messages = copy.deepcopy(initial_messages)
    else:
        loop_messages = _build_initial_agent_messages(
            prompt_context_bundle=prompt_context_bundle,
            user_text=user_text,
        )

    # SEC-003: taint is recreated per agent-loop run, so untrusted content that
    # survives in the conversation history would bypass the gate on the next
    # turn. Re-derive taint from the incoming history's tool provenance so the
    # gate persists exactly as long as the tainted tool result remains in
    # context (structural — keys off the recorded tool name, not the content).
    taint_tracker.restore_from_history(loop_messages)
    pending_hook_context_messages: list[dict[str, Any]] = []
    native_turn_max_tokens = _native_turn_max_tokens_for_model(executor._get_model_name_safe())

    def _record_subagent_transcript() -> None:
        agent_id = str(getattr(executor, "_current_agent_id", "") or "").strip()
        if not agent_id:
            return
        user_id = str(getattr(executor, "_user_id", "") or "").strip()
        if not user_id or user_id.lower() == "default":
            raise RuntimeError("Subagent transcript recording requires a concrete user_id")
        session_id = str(getattr(executor, "_session_id", "") or "").strip() or "subagent:%s" % agent_id
        if getattr(executor, "_use_native", False):
            executor._native_messages = copy.deepcopy(loop_messages)
        else:
            executor._text_messages = copy.deepcopy(loop_messages)
        subagent_lifecycle.record_provider_messages(
            agent_id,
            loop_messages,
            user_id=user_id,
            session_id=session_id,
            mode=normalize_subagent_mode(getattr(executor, "_subagent_mode", "fresh")),
            replace=True,
        )
        replacement_records: list[dict[str, Any]] = []
        for record in getattr(executor, "_tool_result_replacement_records", None) or []:
            if isinstance(record, dict):
                replacement_records.append(copy.deepcopy(record))
            else:
                to_dict = getattr(record, "to_dict", None)
                if callable(to_dict):
                    replacement_records.append(dict(to_dict()))
        if replacement_records:
            subagent_lifecycle.update_resume_metadata(
                agent_id,
                user_id=user_id,
                content_replacements=replacement_records,
            )

    def _drain_subagent_messages() -> None:
        agent_id = str(getattr(executor, "_current_agent_id", "") or "").strip()
        if not agent_id:
            return
        user_id = str(getattr(executor, "_user_id", "") or "").strip()
        if not user_id or user_id.lower() == "default":
            raise RuntimeError("Subagent message draining requires a concrete user_id")
        pending = subagent_lifecycle.drain_messages(agent_id, user_id=user_id)
        if not pending:
            return
        for pending_text in pending:
            loop_messages.append({"role": "user", "content": pending_text})
        _record_subagent_transcript()

    _record_subagent_transcript()

    def _queue_hook_context(
        event_name: str,
        result: HookResult,
        *,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        defer_until_tool_result: bool = True,
    ) -> None:
        messages = _hook_result_messages(
            executor,
            event_name,
            result,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        if not messages:
            return
        if defer_until_tool_result:
            pending_hook_context_messages.extend(messages)
        else:
            loop_messages.extend(messages)

    def _flush_pending_hook_context() -> None:
        if not pending_hook_context_messages:
            return
        loop_messages.extend(pending_hook_context_messages)
        pending_hook_context_messages.clear()

    hook_dispatch_kwargs = {
        "dispatch_fn": hook_dispatch_fn,
        "session_id": hook_session_id,
    }

    def _insert_startup_hook_messages(messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        insert_at = len(loop_messages)
        if (
            user_text
            and loop_messages
            and loop_messages[-1].get("role") == "user"
            and loop_messages[-1].get("content") == user_text
        ):
            insert_at = len(loop_messages) - 1
        loop_messages[insert_at:insert_at] = messages

    _session_end_dispatched = False

    def _dispatch_session_end(reason: str) -> None:
        nonlocal _session_end_dispatched
        if _session_end_dispatched:
            return
        _session_end_dispatched = True
        try:
            session_end_context = {
                "reason": reason,
                "outcome": outcome,
                "task_id": task_id,
                "tools_called": list(tools_called),
                "user_id": executor._user_id,
                "session_id": hook_session_id,
            }
            # SessionEnd context belongs to the completed turn; queueing it for a "next
            # turn" that never came (the agent loop returns its result before
            # `finally` fires). Claude treats SessionEnd as an outside-REPL
            # teardown hook (utils/hooks.ts:4097-4140) that logs failures and
            # clears session state. Mirror that: dispatch synchronously, log
            # any blocking/system-message output instead of queuing it, then
            # clear session-scoped hook state so dynamic watch paths and
            # initial-user-message queues don't leak across sessions.
            session_end_hook = dispatch_lifecycle("SessionEnd", session_end_context, **hook_dispatch_kwargs)
            for ctx in session_end_hook.additional_contexts:
                logger.info("SessionEnd hook context: %s", ctx)
            if session_end_hook.system_message:
                logger.info(
                    "SessionEnd hook system message: %s",
                    session_end_hook.system_message,
                )
            if session_end_hook.reason and session_end_hook.decision in {"ask", "deny"}:
                logger.info(
                    "SessionEnd hook decision %s: %s",
                    session_end_hook.decision,
                    session_end_hook.reason,
                )
            # Clear session-scoped hook state on teardown (Claude
            # ``utils/hooks/sessionHooks.ts:437-447`` and
            # ``utils/hooks/fileChangedWatcher.ts:177-186`` dispose state).
            try:
                hook_registry = getattr(executor, "_hook_registry", None)
                dispatch_state = getattr(hook_registry, "dispatch_state", None)
                if dispatch_state is not None and hook_session_id is not None:
                    # Drain queued initial-user-messages so the next session
                    # in the same registry starts fresh.
                    while dispatch_state.take_initial_user_message(hook_session_id) is not None:
                        pass
                    # Clear accumulated watch paths for this session if the
                    # state supports per-session removal.
                    paths_map = getattr(dispatch_state, "_watch_paths", None)
                    if isinstance(paths_map, dict):
                        paths_map.pop(str(hook_session_id), None)
            except Exception as state_exc:
                logger.debug("SessionEnd state clear skipped: %s", state_exc)
        except Exception as exc:
            logger.warning("SessionEnd hook dispatch failed: %s", exc)

    startup_hook_messages: list[dict[str, Any]] = []
    # Setup and SessionStart use provider-compatible enum values. Claude expects Setup's
    # trigger to be ``init`` or ``maintenance`` (utils/hooks.ts:3902-3922) and
    # SessionStart's source to be ``startup|resume|clear|compact``
    # (utils/hooks.ts:3867-3891). Use Claude's enum values for matcher
    # compatibility; carry the legacy ``viola_origin`` field so existing
    # handlers can still observe Viola's distinction.
    setup_context = {
        "trigger": "init",
        "viola_origin": "agent_loop",
        "task_id": task_id,
        "user_id": executor._user_id,
        "session_id": hook_session_id,
    }
    setup_hook = dispatch_lifecycle("Setup", setup_context, **hook_dispatch_kwargs)
    startup_hook_messages.extend(_hook_result_messages(executor, "Setup", setup_hook))
    # Detect resume by presence of an existing checkpoint with a prior turn.
    _resume_state = bool(
        getattr(executor, "_responses_continuity_active", lambda: False)()
        or getattr(executor, "_native_messages", None)
    )
    session_start_context = {
        "source": "resume" if _resume_state else "startup",
        "viola_origin": "agent_loop",
        "task_id": task_id,
        "user_id": executor._user_id,
        "user_text": user_text,
        "session_id": hook_session_id,
    }
    session_start_hook = dispatch_lifecycle("SessionStart", session_start_context, **hook_dispatch_kwargs)
    startup_hook_messages.extend(_hook_result_messages(executor, "SessionStart", session_start_hook))
    initial_user_message = _take_initial_user_message(hook_session_id)
    if initial_user_message:
        startup_hook_messages.append({"role": "user", "content": initial_user_message})
    await _ensure_hook_file_watcher(
        executor,
        session_id=hook_session_id,
        paths=_session_watch_paths(hook_registry, hook_session_id),
        hook_dispatch_fn=hook_dispatch_fn,
    )
    _insert_startup_hook_messages(startup_hook_messages)
    reconstruct_executor_content_replacement_state(executor, loop_messages)

    _max_output_recovery_count = 0
    _pending_initial_response = _tool_call_to_response(initial_tool_call)
    _loop_sent_message_count = 0
    if getattr(executor, "_responses_continuity_active", lambda: False)():
        _continuity_state = executor._export_responses_continuity_state()
        _continuity_cursor = _continuity_state.get("input_cursor", 0)
        if isinstance(_continuity_cursor, int):
            _loop_sent_message_count = min(max(_continuity_cursor, 0), len(loop_messages))
        if _pending_initial_response is not None:
            _loop_sent_message_count = len(loop_messages)
    _response_from_pending_initial = False

    async def _build_native_turn_kwargs(
        *,
        messages: list[dict[str, Any]],
        first_turn: bool,
        max_tokens: int | None,
        tool_choice_override: dict[str, Any] | None = None,
        compact_before: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        """Build one native turn using canonical continuity/delta state."""
        nonlocal _loop_sent_message_count
        if getattr(executor, "_use_native", False) and compact_before:
            try:
                state_before = executor._export_responses_continuity_state()
                pending_delta_messages = messages[_loop_sent_message_count:] if state_before.get("mode") else []
                if (
                    not _messages_include_tool_result(pending_delta_messages)
                    and executor._should_compact_responses_continuity(state_before)
                    and _compaction_should_allow(executor)
                ):
                    fallback_due = getattr(
                        executor,
                        "_should_fallback_from_responses_compaction_failures",
                        None,
                    )
                    if callable(fallback_due) and fallback_due():
                        compacted = await _fallback_response_items_to_local_compaction(
                            messages=messages,
                            reason="agent_loop_response_items_failure_fallback",
                            trigger="auto",
                        )
                        if compacted:
                            executor._reset_llm_usage_compaction_window()
                    else:
                        if _has_registered_hook("PreCompact"):
                            pre_compact_hook = dispatch_lifecycle(
                                "PreCompact",
                                {
                                    "reason": "agent_loop_response_items",
                                    "trigger": "auto",
                                    "task_id": task_id,
                                    "user_id": executor._user_id,
                                    "session_id": hook_session_id,
                                },
                                dispatch_fn=hook_dispatch_fn,
                                session_id=hook_session_id,
                            )
                            _queue_hook_context(
                                "PreCompact",
                                pre_compact_hook,
                                defer_until_tool_result=False,
                            )
                        compacted = await executor._compact_native_responses_continuity(
                            reason="agent_loop_response_items"
                        )
                        if compacted:
                            executor._compaction_count = getattr(executor, "_compaction_count", 0) + 1
                            # Use the provider-reported chars-before/after metadata as a
                            # token-proxy to score the compaction's progress.  Bailing
                            # out after consecutive no-progress attempts prevents the
                            # loop from hammering a compactor that returns essentially
                            # the same payload twice.
                            meta = getattr(executor, "_pending_compaction_meta", None)
                            if isinstance(meta, dict):
                                _compaction_record_progress(
                                    executor,
                                    int(meta.get("chars_before") or 0),
                                    int(meta.get("chars_after") or 0),
                                )
                            else:
                                _compaction_record_progress(executor, 0, 0)
                            if _has_registered_hook("PostCompact"):
                                post_compact_hook = dispatch_lifecycle(
                                    "PostCompact",
                                    {
                                        "reason": "agent_loop_response_items",
                                        "trigger": "auto",
                                        "task_id": task_id,
                                        "user_id": executor._user_id,
                                        "session_id": hook_session_id,
                                    },
                                    dispatch_fn=hook_dispatch_fn,
                                    session_id=hook_session_id,
                                )
                                _queue_hook_context(
                                    "PostCompact",
                                    post_compact_hook,
                                    defer_until_tool_result=False,
                                )
                            executor._reset_llm_usage_compaction_window()
                if executor._should_compact_by_cumulative_usage() and _compaction_should_allow(executor):
                    compacted = await _compact_local_native_messages(
                        messages=messages,
                        reason="agent_loop_cumulative_input_tokens",
                        trigger="auto",
                        force=True,
                        allow_continuity_reset=True,
                    )
                    if compacted:
                        executor._reset_llm_usage_compaction_window()
            except Exception as exc:
                logger.warning(
                    "Agent loop Responses continuity compaction failed, continuing without it: %s",
                    exc,
                )
            try:
                compacted_local = await _compact_local_native_messages(
                    messages=messages,
                    reason="agent_loop_context",
                    trigger="auto",
                    force=False,
                )
                if compacted_local:
                    executor._reset_llm_usage_compaction_window()
            except Exception as exc:
                logger.warning(
                    "Agent loop local context compaction failed, continuing without it: %s",
                    exc,
                )
        provider_continuity_before = (
            executor._export_responses_continuity_state() if getattr(executor, "_use_native", False) else {}
        )
        trace_builder = getattr(executor, "_build_trace_continuity_before", None)
        continuity_before = (
            trace_builder(provider_continuity_before) if callable(trace_builder) else provider_continuity_before
        )
        use_delta = bool(provider_continuity_before.get("mode"))
        raw_turn_messages = messages[_loop_sent_message_count:] if use_delta else list(messages)
        turn_messages = project_native_messages_for_provider(
            raw_turn_messages,
            content_replacement_state=getattr(executor, "_content_replacement_state", None),
            replacement_writer=lambda records: record_tool_result_replacements(executor, records),
            session_id=tool_result_replacement_session_id(executor),
        )
        turn_kwargs: dict[str, Any] = {
            "messages": turn_messages,
            "first_turn": first_turn,
            "max_tokens": max_tokens,
            "reasoning_tier": ("background_agent" if getattr(executor, "_depth", 0) > 0 else "agent"),
        }
        # F-002 (R3-A): thread the executor cancellation event into the
        # provider call so that ``execute_with_policy``'s ``abort_signal``
        # check fires before every attempt and inside retry sleeps. Claude
        # forwards ``toolUseContext.abortController.signal`` into model
        # calls and retry loops (``src/query.ts:659-665``,
        # ``src/services/api/withRetry.ts:170-190``). Providers that do not
        # consume the kwarg ignore it via ``**kwargs``; providers that do
        # consume it (openai_compatible, anthropic — see F-029) pass it
        # straight into the policy context.
        _abort_event = getattr(executor, "_cancel_event", None)
        if _abort_event is not None:
            turn_kwargs["abort_signal"] = _abort_event
            turn_kwargs["cancel_event"] = _abort_event
        if system_prompt:
            turn_kwargs["system_prompt"] = system_prompt
        request_native_tools = executor._get_request_native_tools()
        if request_native_tools is not None:
            allowed_names: set[str] = set()
            try:
                step_log_tools = executor._get_step_log_visible_tools()
                allowed_names = {
                    str(
                        tool.get("name")
                        or (tool.get("function", {}).get("name") if isinstance(tool.get("function"), dict) else "")
                        or ""
                    ).strip()
                    for tool in step_log_tools
                    if isinstance(tool, dict)
                }
                allowed_names.discard("")
            except Exception:
                logger.exception("Deferred tool allowed-name calculation failed; denying deferred expansion")
            expand_user_id = executor._user_id
            get_effective_user_id = getattr(executor, "_get_effective_user_id", None)
            if callable(get_effective_user_id):
                try:
                    expand_user_id = get_effective_user_id()
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    logger.debug(
                        "Could not resolve effective user id for deferred tool expansion",
                        exc_info=True,
                    )
            expanded_native_tools = expand_tools_with_deferred_references(
                request_native_tools,
                messages,
                allowed_names=allowed_names,
                user_id=expand_user_id,
            )
            if expanded_native_tools is not None:
                request_native_tools = expanded_native_tools
                try:
                    executor._native_tools = list(expanded_native_tools)
                except Exception:
                    logger.debug(
                        "Could not update executor native tools after deferred resolution",
                        exc_info=True,
                    )
                llm_caller = getattr(executor, "_llm", None)
                if hasattr(llm_caller, "_native_tools"):
                    try:
                        llm_caller._native_tools = list(expanded_native_tools)
                    except Exception:
                        logger.debug(
                            "Could not update provider native tools after deferred resolution",
                            exc_info=True,
                        )
            turn_kwargs["native_tools"] = request_native_tools
        if prompt_context_bundle is not None:
            turn_kwargs["prompt_context_bundle"] = prompt_context_bundle
        if executor._model_override:
            turn_kwargs["model_override"] = executor._model_override
        if tool_choice_override is not None:
            turn_kwargs["tool_choice_override"] = tool_choice_override
        if use_delta:
            continuity_payload: dict[str, Any] = {
                "mode": provider_continuity_before["mode"],
                "messages_are_delta": True,
            }
            previous_response_id = provider_continuity_before.get("previous_response_id")
            if isinstance(previous_response_id, str) and previous_response_id:
                continuity_payload["previous_response_id"] = previous_response_id
                turn_kwargs["previous_response_id"] = previous_response_id
            response_items = provider_continuity_before.get("response_items")
            if isinstance(response_items, list) and response_items:
                copied_items = copy.deepcopy(response_items)
                continuity_payload["response_items"] = copied_items
                turn_kwargs["response_items"] = copy.deepcopy(copied_items)
            turn_kwargs["messages_are_delta"] = True
            turn_kwargs["continuity"] = copy.deepcopy(continuity_payload)
            turn_kwargs["responses_continuity"] = copy.deepcopy(continuity_payload)
        return turn_kwargs, turn_messages, continuity_before

    def _acknowledge_sent_messages(messages: list[dict[str, Any]]) -> None:
        """Advance the agent-loop-side delta cursor after a successful provider call."""
        nonlocal _loop_sent_message_count
        _loop_sent_message_count = len(messages)

    async def _compact_local_native_messages(
        *,
        messages: list[dict[str, Any]],
        reason: str,
        trigger: str,
        force: bool = False,
        allow_continuity_reset: bool = False,
    ) -> bool:
        """Run typed compaction on native history, resetting server continuity when requested."""
        nonlocal _loop_sent_message_count
        if not getattr(executor, "_use_native", False):
            return False
        continuity_state = getattr(executor, "_export_responses_continuity_state", lambda: {})()
        continuity_mode = str(continuity_state.get("mode") or "")
        if continuity_mode and not allow_continuity_reset:
            return False
        if not _compaction_should_allow(executor):
            return False
        tracker = getattr(executor, "_token_tracker", None)
        provider_messages = project_native_messages_for_provider(
            messages,
            content_replacement_state=getattr(executor, "_content_replacement_state", None),
            replacement_writer=lambda records: record_tool_result_replacements(executor, records),
            session_id=tool_result_replacement_session_id(executor),
        )
        # Snapshot the pre-compaction token estimate so we can score this
        # attempt's progress and update the no-progress streak below.
        pre_tokens = 0
        if not force and tracker is not None:
            try:
                tracker.recount(provider_messages)
                if not tracker.should_compact():
                    return False
                pre_tokens = int(getattr(tracker, "total_tokens", 0) or 0)
            except Exception as exc:
                logger.debug("Native token tracker recount failed before compaction: %s", exc)
                return False
        elif tracker is not None:
            try:
                tracker.recount(provider_messages)
                pre_tokens = int(getattr(tracker, "total_tokens", 0) or 0)
            except Exception as exc:
                logger.debug(
                    "Native token tracker recount failed before forced compaction: %s",
                    exc,
                )
        if _has_registered_hook("PreCompact"):
            pre_compact_hook = dispatch_lifecycle(
                "PreCompact",
                {
                    "reason": reason,
                    "trigger": trigger,
                    "task_id": task_id,
                    "user_id": executor._user_id,
                    "session_id": hook_session_id,
                },
                dispatch_fn=hook_dispatch_fn,
                session_id=hook_session_id,
            )
            _queue_hook_context("PreCompact", pre_compact_hook, defer_until_tool_result=False)
        compacted_messages, compaction_meta = await compact_native_messages(
            messages,
            user_id=executor._user_id or "",
            session_id=tool_result_replacement_session_id(executor),
            replacement_writer=lambda records: record_tool_result_replacements(executor, records),
            content_replacement_state=getattr(executor, "_content_replacement_state", None),
        )
        if not compaction_meta:
            return False
        messages[:] = compacted_messages
        if continuity_mode and allow_continuity_reset:
            executor._responses_continuity = {}
            compaction_meta = {
                **compaction_meta,
                "method": "local_compact_reset_responses_continuity",
                "continuity_mode_before": continuity_mode,
                "continuity_previous_response_id_before": continuity_state.get("previous_response_id"),
            }
        executor._compaction_count = getattr(executor, "_compaction_count", 0) + 1
        executor._pending_compaction_meta = compaction_meta
        _loop_sent_message_count = 0
        post_tokens = 0
        if tracker is not None:
            try:
                tracker.recount(
                    project_native_messages_for_provider(
                        messages,
                        content_replacement_state=getattr(executor, "_content_replacement_state", None),
                        replacement_writer=lambda records: record_tool_result_replacements(executor, records),
                        session_id=tool_result_replacement_session_id(executor),
                    ),
                    after_compaction=True,
                )
                post_tokens = int(getattr(tracker, "total_tokens", 0) or 0)
            except Exception as exc:
                logger.debug("Native token tracker recount failed after compaction: %s", exc)
        if _has_registered_hook("PostCompact"):
            post_compact_hook = dispatch_lifecycle(
                "PostCompact",
                {
                    "reason": reason,
                    "trigger": trigger,
                    "task_id": task_id,
                    "user_id": executor._user_id,
                    "session_id": hook_session_id,
                },
                dispatch_fn=hook_dispatch_fn,
                session_id=hook_session_id,
            )
            _queue_hook_context("PostCompact", post_compact_hook, defer_until_tool_result=False)
        # Record progress: if pre/post token counts are unmeasurable we treat
        # the attempt as no-progress so a stuck compactor still bails.
        _compaction_record_progress(executor, pre_tokens, post_tokens)
        executor._reset_llm_usage_compaction_window()
        return True

    async def _fallback_response_items_to_local_compaction(
        *,
        messages: list[dict[str, Any]],
        reason: str,
        trigger: str,
    ) -> bool:
        failure_count = 0
        failure_count_fn = getattr(executor, "_responses_compaction_failure_count", None)
        if callable(failure_count_fn):
            failure_count = int(failure_count_fn() or 0)
        logger.warning(
            "Responses continuity compaction failed %d consecutive times; falling back to local native compaction",
            failure_count,
        )
        compacted = await _compact_local_native_messages(
            messages=messages,
            reason=reason,
            trigger=trigger,
            force=True,
            allow_continuity_reset=True,
        )
        if compacted:
            reset_failures = getattr(executor, "_reset_responses_compaction_failures", None)
            if callable(reset_failures):
                reset_failures()
        return compacted

    async def _call_native_turn(
        *,
        messages: list[dict[str, Any]],
        first_turn: bool,
        max_tokens: int | None,
        tool_choice_override: dict[str, Any] | None = None,
        call_kind: str = "agent_loop",
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Call one native turn, forcing response-items compaction on overflow."""
        with latency_spans.span("STEP_PREP_BUILD_KWARGS", call_kind=call_kind):
            turn_kwargs, turn_messages, continuity_before = await _build_native_turn_kwargs(
                messages=messages,
                first_turn=first_turn,
                max_tokens=max_tokens,
                tool_choice_override=tool_choice_override,
            )
        with latency_spans.span("TRACE_REQ_BUILD", call_kind=call_kind):
            trace_request_kwargs = {
                key: _trace_safe_copy(value)
                for key, value in turn_kwargs.items()
                if key not in {"messages", "first_turn"}
            }
            trace_request = executor._build_task_trace_llm_request(
                call_kind=call_kind,
                request_mode="native",
                first_turn=first_turn,
                native_messages=turn_messages,
                native_kwargs=trace_request_kwargs,
                tool_choice_override=tool_choice_override,
            )
        effective_tool_choice = getattr(provider, "_agent_tool_choice", None)
        if not isinstance(effective_tool_choice, str) or not effective_tool_choice:
            effective_tool_choice = "auto"
        if tool_choice_override:
            effective_tool_choice = str(tool_choice_override.get("function", {}).get("name", "override"))
        _continuity_mode = str(continuity_before.get("mode") or "") if isinstance(continuity_before, dict) else ""
        _virtual_message_count = len(messages) if _continuity_mode else len(turn_messages)
        executor._virtual_message_count = max(
            int(getattr(executor, "_virtual_message_count", 0) or 0),
            _virtual_message_count,
        )
        executor._set_step_log_llm_context(
            tool_choice=effective_tool_choice,
            message_count=len(turn_messages),
            continuity_mode=_continuity_mode or None,
            server_context_tokens=getattr(executor, "_last_server_context_tokens", 0),
            virtual_message_count=getattr(executor, "_virtual_message_count", _virtual_message_count),
        )
        with latency_spans.span("SPEND_RESERVE", call_kind=call_kind):
            spend_reservation, estimated_usage = await _reserve_agent_turn_spend(
                turn_messages=turn_messages,
                turn_kwargs=turn_kwargs,
                max_tokens=max_tokens,
                call_kind=call_kind,
            )
        with latency_spans.span("TRACE_ATTEMPT_START_WRITE", call_kind=call_kind):
            attempt_id = executor._append_task_trace_llm_attempt_start(
                call_kind=call_kind,
                request_mode="native",
                first_turn=first_turn,
                continuity_before=continuity_before,
                request=trace_request,
                provider_payload={
                    "method": "route_command_native",
                    "kwargs": _trace_safe_copy(turn_kwargs),
                },
                payload_stage="executor_to_provider",
                provider_obj=provider,
            )
        try:
            with latency_spans.span("PROVIDER_CALL", attempt_id=str(attempt_id), call_kind=call_kind):
                response = await provider.route_command_native(
                    **turn_kwargs,
                )
        except Exception as exc:
            executor._append_task_trace_llm_attempt_failure(
                attempt_id=attempt_id,
                call_kind=call_kind,
                exc=exc,
            )
            await _settle_agent_turn_spend_failed(spend_reservation)
            invalid_encrypted_content = False
            if getattr(executor, "_use_native", False) and continuity_before.get("mode") == "response_items":
                from intent.agent_executor import _is_invalid_encrypted_content_error

                invalid_encrypted_content = _is_invalid_encrypted_content_error(exc)

            if invalid_encrypted_content and not getattr(
                executor, "_responses_invalid_encrypted_content_repair_attempted", False
            ):
                executor._responses_invalid_encrypted_content_repair_attempted = True
                repair_invalid_continuity = getattr(executor, "_repair_invalid_encrypted_responses_continuity", None)
                if not callable(repair_invalid_continuity) or not repair_invalid_continuity():
                    raise
                logger.warning("Agent invalid encrypted Responses continuity; dropped encrypted reasoning and retrying")
                executor._append_task_trace_llm_retry_fallback(
                    call_kind=call_kind,
                    kind="retry",
                    reason="agent_invalid_encrypted_content_repair",
                    next_action="drop_encrypted_reasoning_and_retry",
                    attempt_id=attempt_id,
                )
                turn_kwargs, turn_messages, continuity_before = await _build_native_turn_kwargs(
                    messages=messages,
                    first_turn=first_turn,
                    max_tokens=max_tokens,
                    tool_choice_override=tool_choice_override,
                    compact_before=False,
                )
                retry_trace_kwargs = {
                    key: _trace_safe_copy(value)
                    for key, value in turn_kwargs.items()
                    if key not in {"messages", "first_turn"}
                }
                retry_trace_request = executor._build_task_trace_llm_request(
                    call_kind=call_kind,
                    request_mode="native",
                    first_turn=first_turn,
                    native_messages=turn_messages,
                    native_kwargs=retry_trace_kwargs,
                    tool_choice_override=tool_choice_override,
                )
                spend_reservation, estimated_usage = await _reserve_agent_turn_spend(
                    turn_messages=turn_messages,
                    turn_kwargs=turn_kwargs,
                    max_tokens=max_tokens,
                    call_kind=call_kind,
                )
                retry_attempt_id = executor._append_task_trace_llm_attempt_start(
                    call_kind=call_kind,
                    request_mode="native",
                    first_turn=first_turn,
                    continuity_before=continuity_before,
                    request=retry_trace_request,
                    provider_payload={
                        "method": "route_command_native",
                        "kwargs": _trace_safe_copy(turn_kwargs),
                    },
                    payload_stage="executor_to_provider",
                    provider_obj=provider,
                )
                attempt_id = retry_attempt_id or attempt_id
                try:
                    with latency_spans.span("PROVIDER_CALL", attempt_id=str(attempt_id), call_kind=call_kind):
                        response = await provider.route_command_native(
                            **turn_kwargs,
                        )
                except Exception as retry_exc:
                    executor._append_task_trace_llm_attempt_failure(
                        attempt_id=attempt_id,
                        call_kind=call_kind,
                        exc=retry_exc,
                    )
                    await _settle_agent_turn_spend_failed(spend_reservation)
                    raise
            elif (
                getattr(executor, "_use_native", False)
                and is_token_limit_error(exc)
                and continuity_before.get("mode") == "response_items"
                and _compaction_should_allow(executor)
            ):
                fallback_due = getattr(
                    executor,
                    "_should_fallback_from_responses_compaction_failures",
                    None,
                )
                used_response_items_compactor = False
                if callable(fallback_due) and fallback_due():
                    logger.warning(
                        "Agent token limit hit after repeated response-items compaction failures; "
                        "resetting continuity through local compaction",
                    )
                    executor._append_task_trace_llm_retry_fallback(
                        call_kind=call_kind,
                        kind="retry",
                        reason="agent_token_limit_response_items_failure_fallback",
                        next_action="compact_local_context_and_retry",
                        attempt_id=attempt_id,
                        detail={"compaction_count": getattr(executor, "_compaction_count", 0)},
                    )
                    compacted = await _fallback_response_items_to_local_compaction(
                        messages=messages,
                        reason="agent_token_limit_response_items_failure_fallback",
                        trigger="reactive",
                    )
                else:
                    executor._compaction_count = getattr(executor, "_compaction_count", 0) + 1
                    logger.warning(
                        "Agent token limit hit in response-items continuity mode; compacting and retrying",
                    )
                    executor._append_task_trace_llm_retry_fallback(
                        call_kind=call_kind,
                        kind="retry",
                        reason="agent_token_limit_response_items",
                        next_action="compact_responses_continuity_and_retry",
                        attempt_id=attempt_id,
                        detail={"compaction_count": executor._compaction_count},
                    )
                    if _has_registered_hook("PreCompact"):
                        pre_compact_hook = dispatch_lifecycle(
                            "PreCompact",
                            {
                                "reason": "agent_token_limit_response_items",
                                "trigger": "auto",
                                "task_id": task_id,
                                "user_id": executor._user_id,
                                "session_id": hook_session_id,
                            },
                            dispatch_fn=hook_dispatch_fn,
                            session_id=hook_session_id,
                        )
                        _queue_hook_context(
                            "PreCompact",
                            pre_compact_hook,
                            defer_until_tool_result=False,
                        )
                    compacted = await executor._compact_native_responses_continuity(
                        reason="agent_token_limit_response_items",
                        force=True,
                    )
                    used_response_items_compactor = bool(compacted)
                    if not compacted and callable(fallback_due) and fallback_due():
                        compacted = await _fallback_response_items_to_local_compaction(
                            messages=messages,
                            reason="agent_token_limit_response_items_failure_fallback",
                            trigger="reactive",
                        )
                if not compacted:
                    raise
                executor._reset_llm_usage_compaction_window()
                # Score this compaction's progress so the no-progress guard can
                # short-circuit consecutive doomed retries against the same payload.
                if used_response_items_compactor:
                    meta = getattr(executor, "_pending_compaction_meta", None)
                    if isinstance(meta, dict):
                        _compaction_record_progress(
                            executor,
                            int(meta.get("chars_before") or 0),
                            int(meta.get("chars_after") or 0),
                        )
                    else:
                        _compaction_record_progress(executor, 0, 0)
                if used_response_items_compactor and _has_registered_hook("PostCompact"):
                    post_compact_hook = dispatch_lifecycle(
                        "PostCompact",
                        {
                            "reason": "agent_token_limit_response_items",
                            "trigger": "auto",
                            "task_id": task_id,
                            "user_id": executor._user_id,
                            "session_id": hook_session_id,
                        },
                        dispatch_fn=hook_dispatch_fn,
                        session_id=hook_session_id,
                    )
                    _queue_hook_context("PostCompact", post_compact_hook, defer_until_tool_result=False)
                turn_kwargs, turn_messages, continuity_before = await _build_native_turn_kwargs(
                    messages=messages,
                    first_turn=first_turn,
                    max_tokens=max_tokens,
                    tool_choice_override=tool_choice_override,
                    compact_before=False,
                )
                retry_trace_kwargs = {
                    key: _trace_safe_copy(value)
                    for key, value in turn_kwargs.items()
                    if key not in {"messages", "first_turn"}
                }
                retry_trace_request = executor._build_task_trace_llm_request(
                    call_kind=call_kind,
                    request_mode="native",
                    first_turn=first_turn,
                    native_messages=turn_messages,
                    native_kwargs=retry_trace_kwargs,
                    tool_choice_override=tool_choice_override,
                )
                spend_reservation, estimated_usage = await _reserve_agent_turn_spend(
                    turn_messages=turn_messages,
                    turn_kwargs=turn_kwargs,
                    max_tokens=max_tokens,
                    call_kind=call_kind,
                )
                retry_attempt_id = executor._append_task_trace_llm_attempt_start(
                    call_kind=call_kind,
                    request_mode="native",
                    first_turn=first_turn,
                    continuity_before=continuity_before,
                    request=retry_trace_request,
                    provider_payload={
                        "method": "route_command_native",
                        "kwargs": _trace_safe_copy(turn_kwargs),
                    },
                    payload_stage="executor_to_provider",
                    provider_obj=provider,
                )
                attempt_id = retry_attempt_id or attempt_id
                try:
                    with latency_spans.span("PROVIDER_CALL", attempt_id=str(attempt_id), call_kind=call_kind):
                        response = await provider.route_command_native(
                            **turn_kwargs,
                        )
                except Exception as retry_exc:
                    executor._append_task_trace_llm_attempt_failure(
                        attempt_id=attempt_id,
                        call_kind=call_kind,
                        exc=retry_exc,
                    )
                    await _settle_agent_turn_spend_failed(spend_reservation)
                    raise
            elif getattr(executor, "_use_native", False) and is_token_limit_error(exc):
                logger.warning("Agent token limit hit; compacting local native history and retrying")
                executor._append_task_trace_llm_retry_fallback(
                    call_kind=call_kind,
                    kind="retry",
                    reason="agent_token_limit_local_context",
                    next_action="compact_local_context_and_retry",
                    attempt_id=attempt_id,
                    detail={"compaction_count": getattr(executor, "_compaction_count", 0)},
                )
                compacted = await _compact_local_native_messages(
                    messages=messages,
                    reason="agent_token_limit_local_context",
                    trigger="reactive",
                    force=True,
                    allow_continuity_reset=True,
                )
                if not compacted:
                    raise
                executor._reset_llm_usage_compaction_window()
                turn_kwargs, turn_messages, continuity_before = await _build_native_turn_kwargs(
                    messages=messages,
                    first_turn=first_turn,
                    max_tokens=max_tokens,
                    tool_choice_override=tool_choice_override,
                    compact_before=False,
                )
                retry_trace_kwargs = {
                    key: _trace_safe_copy(value)
                    for key, value in turn_kwargs.items()
                    if key not in {"messages", "first_turn"}
                }
                retry_trace_request = executor._build_task_trace_llm_request(
                    call_kind=call_kind,
                    request_mode="native",
                    first_turn=first_turn,
                    native_messages=turn_messages,
                    native_kwargs=retry_trace_kwargs,
                    tool_choice_override=tool_choice_override,
                )
                spend_reservation, estimated_usage = await _reserve_agent_turn_spend(
                    turn_messages=turn_messages,
                    turn_kwargs=turn_kwargs,
                    max_tokens=max_tokens,
                    call_kind=call_kind,
                )
                retry_attempt_id = executor._append_task_trace_llm_attempt_start(
                    call_kind=call_kind,
                    request_mode="native",
                    first_turn=first_turn,
                    continuity_before=continuity_before,
                    request=retry_trace_request,
                    provider_payload={
                        "method": "route_command_native",
                        "kwargs": _trace_safe_copy(turn_kwargs),
                    },
                    payload_stage="executor_to_provider",
                    provider_obj=provider,
                )
                attempt_id = retry_attempt_id or attempt_id
                try:
                    with latency_spans.span("PROVIDER_CALL", attempt_id=str(attempt_id), call_kind=call_kind):
                        response = await provider.route_command_native(
                            **turn_kwargs,
                        )
                except Exception as retry_exc:
                    executor._append_task_trace_llm_attempt_failure(
                        attempt_id=attempt_id,
                        call_kind=call_kind,
                        exc=retry_exc,
                    )
                    await _settle_agent_turn_spend_failed(spend_reservation)
                    raise
            else:
                raise
        with latency_spans.span("SPEND_SETTLE", call_kind=call_kind):
            await _settle_agent_turn_spend_success(
                reservation=spend_reservation,
                estimated_usage=estimated_usage,
                response=response,
                turn_kwargs=turn_kwargs,
            )
        return response, turn_messages, continuity_before, turn_kwargs

    # Register spawn_subtask callback
    _spawn_subtask_token = None
    from mcp_servers.core_tools.server import (
        reset_spawn_subtask_callback,
        set_spawn_subtask_callback,
    )

    async def _spawn_callback(task: str, max_steps: int, return_format: str) -> str:
        subagent_context = {
            "task": task,
            "max_steps": max_steps,
            "return_format": return_format,
            "parent_task_id": task_id,
            "user_id": executor._user_id,
            "session_id": hook_session_id,
        }
        if _has_registered_hook("SubagentStart"):
            start_hook = dispatch_lifecycle(
                "SubagentStart",
                subagent_context,
                dispatch_fn=hook_dispatch_fn,
                session_id=hook_session_id,
            )
            _queue_hook_context("SubagentStart", start_hook)
        try:
            child_result = await executor._run_child_agent(task, max_steps, return_format, checkpoint)
            child_answer = _child_answer_from_tool_result(child_result)
        except Exception as exc:
            if _has_registered_hook("SubagentStop"):
                stop_hook = dispatch_lifecycle(
                    "SubagentStop",
                    {**subagent_context, "error": str(exc)},
                    dispatch_fn=hook_dispatch_fn,
                    session_id=hook_session_id,
                )
                _queue_hook_context("SubagentStop", stop_hook)
            raise
        if _has_registered_hook("SubagentStop"):
            stop_hook = dispatch_lifecycle(
                "SubagentStop",
                {**subagent_context, "result": child_answer},
                dispatch_fn=hook_dispatch_fn,
                session_id=hook_session_id,
            )
            _queue_hook_context("SubagentStop", stop_hook)
        return child_answer

    # NOTE: registration of ``_spawn_callback`` is deferred to the top of the
    # ``try`` block below (not here) so its ContextVar token is ALWAYS paired
    # with the ``finally`` that resets it. The pre-loop billing gate can
    # ``return`` before that ``try`` (spend cap reached / billing unavailable);
    # registering here would leak the token on those paths because the
    # ``finally`` never runs. ``run_agent_loop`` is awaited in the caller's own
    # asyncio context (child agents run via ``await child.run()``), so a leaked
    # token leaves the ContextVar pointing at THIS executor's stale
    # ``_spawn_callback`` after we return — a parent's later ``spawn_subtask``
    # would then dispatch through a finished child executor. The callback is
    # only ever invoked from core_tools during in-loop tool execution, so
    # nothing needs it before the ``try``.

    # Start with the full visible tool surface. Approval denials are tracked
    # separately; category/classifier allowlists do not filter tools.
    update_tool_filters = getattr(provider, "update_tool_filters", None)
    if callable(update_tool_filters):
        maybe_updated = update_tool_filters(
            allowed_tools=executor._allowed_tools,
            rejected_tools=executor._rejected_tools,
        )
        if inspect.isawaitable(maybe_updated):
            await maybe_updated

    # â”€â”€ Pre-loop billing gate: managed spend before starting â”€â”€
    # Must use the async variant: this runs inside the request-handler async
    # context (uvicorn worker loop). The sync `_check_managed_llm_spend_cap`
    # wraps `require_enabled` -> `run_async_synchronously`, which detects that
    # `asyncio.get_running_loop()` is the target worker loop and raises
    # `RuntimeError: Cannot synchronously wait on the shared asyncio worker
    # loop from itself` (core/asyncio_safe.py:182). The guard then surfaces as
    # `ControlReadError: PostgreSQL operator control store is unavailable`, the
    # gate fail-closes, and every default-funnel agent task gets the canned
    # STORE_UNAVAILABLE_MESSAGE. Captured live on viola-cloud:1bf2614f at
    # 2026-05-26 20:42:41 via b065bd03 instrumentation.
    if executor._user_id:
        try:
            _spend_gate = await executor._check_managed_llm_spend_cap_async()
            if not _spend_gate.allowed:
                logger.info(
                    "Agent pre-loop: managed spend cap already reached for %s period=%s",
                    executor._user_id,
                    _spend_gate.period,
                )
                executor._final_params["cap_state"] = _spend_gate.cap_state
                _dispatch_session_end("pre_loop_spend_cap")
                return AgentResult(
                    ok=False,
                    answer=executor._managed_llm_budget_message(_spend_gate),
                    iterations_used=0,
                    tools_called=[],
                    error=_spend_gate.reason or "managed LLM spend cap reached",
                    cap_state=_spend_gate.cap_state,
                )
        except Exception as _pre_exc:
            logger.warning("Agent pre-loop billing check failed: %s", _pre_exc)
            _handle_agent_loop_billing_failure(_pre_exc)
            _dispatch_session_end("pre_loop_billing_unavailable")
            return AgentResult(
                ok=False,
                answer=executor._final_answer
                or "I can't verify your usage budget right now. Please try again shortly.",
                iterations_used=0,
                tools_called=[],
                error="managed LLM spend governance unavailable",
                cap_state=executor._final_params.get("cap_state"),
            )

    try:
        # Register the spawn_subtask callback here (inside the try) so the
        # ``finally`` below always resets its ContextVar token — see the note
        # above the pre-loop billing gate.
        _spawn_subtask_token = set_spawn_subtask_callback(_spawn_callback)
        while True:
            pending_stuck_progress = getattr(executor, "_pending_stuck_tool_progress", None)
            if isinstance(pending_stuck_progress, dict):
                if pending_stuck_progress.get("stuck_reason") == "bot_protection":
                    # Issue #278: a bot-check/WAF-challenge or parked-domain
                    # page (mcp_servers/browser/server.py `page_health`,
                    # observed on the live page itself) does not resolve via
                    # context compaction -- compaction fixes a bloated
                    # context, not a site that is blocking automated access.
                    # Halt immediately instead of spending a compaction
                    # round-trip (and the LLM call after it) on a retry that
                    # cannot succeed.
                    executor._pending_stuck_tool_progress = None
                    final_answer = _build_agent_final_answer_fallback(
                        executor,
                        user_text,
                        tools_called,
                        reason="browser_bot_protection_detected",
                    )
                    _set_final_response(executor, final_answer, continue_listening=True)
                    executor._final_params["stop_reason"] = "browser_bot_protection_detected"
                    executor._final_params["bot_protection_pattern"] = pending_stuck_progress
                    outcome = "spin_terminated"
                    break
                if not getattr(executor, "_stuck_tool_compaction_attempted", False) and _compaction_should_allow(
                    executor
                ):
                    executor._stuck_tool_compaction_attempted = True
                    try:
                        compacted_for_stuck = await _compact_local_native_messages(
                            messages=loop_messages,
                            reason="agent_repeated_tool_pattern",
                            trigger="stuck_tool_pattern",
                            force=True,
                            allow_continuity_reset=True,
                        )
                    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                        logger.warning("Agent repeated-tool compaction failed: %s", exc)
                        compacted_for_stuck = False
                    executor._pending_stuck_tool_progress = None
                    if compacted_for_stuck:
                        executor._reset_llm_usage_compaction_window()
                    else:
                        final_answer = _build_agent_final_answer_fallback(
                            executor,
                            user_text,
                            tools_called,
                            reason="stuck_tool_pattern",
                        )
                        _set_final_response(executor, final_answer, continue_listening=True)
                        executor._final_params["stop_reason"] = "stuck_tool_pattern"
                        outcome = "spin_terminated"
                        break
                else:
                    executor._pending_stuck_tool_progress = None
                    final_answer = _build_agent_final_answer_fallback(
                        executor,
                        user_text,
                        tools_called,
                        reason="stuck_tool_pattern",
                    )
                    _set_final_response(executor, final_answer, continue_listening=True)
                    executor._final_params["stop_reason"] = "stuck_tool_pattern"
                    executor._final_params["stuck_tool_pattern"] = pending_stuck_progress
                    outcome = "spin_terminated"
                    break
            if executor._max_iterations is not None and iterations >= executor._max_iterations:
                extended_cap = _step_ceiling_extension_target(executor, iterations)
                if extended_cap is not None:
                    executor._max_iterations = extended_cap
                    logger.info(
                        "Agent step ceiling extended task %s without compaction; new cap=%d",
                        task_id,
                        executor._max_iterations,
                    )
                    continue
                outcome = "step_ceiling"
                return executor._stop_for_step_ceiling(
                    task_log=task_log,
                    checkpoint=checkpoint,
                    messages=loop_messages,
                    user_text=user_text,
                    tools_called=tools_called,
                    iterations=iterations,
                    start_time=run_start_time,
                    prefer_provided_messages=True,
                )
            iterations += 1
            latency_spans.event("STEP_BEGIN", iteration=iterations, task_id=str(task_id))

            # â”€â”€ Zero-tools warning (iteration 1) â”€â”€
            if iterations == 1:
                if not bool(tool_status.get("has_tools")):
                    status = tool_status.get("mcp_server_status", mcp_status)
                    logger.error(
                        "ZERO native tools available at agent start for task '%s' (execution_path=%s, mcp=%s)",
                        task_id,
                        tool_status.get("execution_path", "unknown"),
                        status,
                    )
                elif not any(bool(value) for value in mcp_status.values()):
                    logger.info(
                        "Agent loop continuing without MCP session health because execution path still has tools "
                        "(execution_path=%s, tool_count=%s)",
                        tool_status.get("execution_path", "unknown"),
                        tool_status.get("tool_count", 0),
                    )

            # â”€â”€ Cancellation check â”€â”€
            # F-002 (R3-A): cancellation is a terminal abort, not a success.
            # Claude returns an explicit abort reason (``query.ts:1011-1051``,
            # ``query.ts:1484-1515``) when the abort signal fires during
            # streaming or tool execution. Mapping cancel onto
            # ``AgentResult(ok=True)`` made the host think a long task
            # completed cleanly even though the user pulled the plug.
            # Report ``ok=False`` with explicit ``error="cancelled"``.
            if executor._cancelled:
                logger.info("Agent cancelled by user after %d iterations", iterations - 1)
                await executor._speak("Okay, I've stopped.")
                outcome = "cancelled"
                try:
                    spin_detector.reset()
                except Exception as exc:
                    logger.debug("Agent spin detector reset failed during cancellation: %s", exc)
                _record_subagent_transcript()
                _snapshot_agent_checkpoint(executor, checkpoint, loop_messages, compress_messages)
                mark_complete(checkpoint, "cancelled", "User cancelled")
                hook_dispatch_fn("StopFailure", reason=outcome, user_id=executor._user_id)
                executor._finalize_log(task_log, outcome, run_start_time)
                return AgentResult(
                    ok=False,
                    answer="Task cancelled.",
                    iterations_used=iterations - 1,
                    tools_called=tools_called,
                    error="cancelled",
                )

            # â”€â”€ Timeout check with progress-based extension â”€â”€
            _drain_subagent_messages()

            elapsed = time.monotonic() - run_start_time
            if elapsed > executor._total_timeout:
                if not executor._timeout_extended:
                    executor._timeout_extended = True
                    executor._total_timeout *= 1.5
                    logger.info(
                        "Agent reached %.0fs, auto-extending timeout to %.0fs",
                        elapsed,
                        executor._total_timeout,
                    )
                else:
                    logger.warning(
                        "Agent timed out after %.1fs (%d iterations)",
                        elapsed,
                        iterations,
                    )
                    outcome = "timeout"
                    timeout_answer = _build_agent_final_answer_fallback(
                        executor,
                        user_text,
                        tools_called,
                        reason="timeout",
                    )
                    _set_final_response(executor, timeout_answer, continue_listening=True)
                    _record_subagent_transcript()
                    _snapshot_agent_checkpoint(executor, checkpoint, loop_messages, compress_messages)
                    checkpoint.status = "timed_out"
                    save_checkpoint(checkpoint)
                    result = AgentResult(
                        ok=False,
                        answer=timeout_answer,
                        iterations_used=iterations,
                        tools_called=tools_called,
                        error="Took a bit longer than expected (%.0fs) - progress was saved and can be resumed"
                        % elapsed,
                    )
                    hook_dispatch_fn("StopFailure", reason=outcome, user_id=executor._user_id)
                    executor._finalize_log(task_log, outcome, run_start_time)
                    return result

            # Call the provider for one turn.
            if not await executor._wait_for_takeover_release():
                continue
            executor._update_overlay_phase("thinking")
            _turn_reasoning = ""
            try:
                _response_from_pending_initial = False
                if _pending_initial_response is not None:
                    response = _pending_initial_response
                    _pending_initial_response = None
                    _response_from_pending_initial = True
                    # mt-ok: local turn buffer, per-iteration, not a stored conversation.
                    _turn_messages = []
                    _turn_trace_continuity_before = {}
                    _turn_kwargs = {}
                else:
                    # Atomic spend admission happens inside _call_native_turn:
                    # reserve before the provider call, settle with actual usage
                    # after it returns, and release the reservation on errors.
                    # Intentional single-loop behavior: the agent loop keeps
                    # provider tool choice automatic for normal turns.
                    _first_turn_for_call = _next_first_turn
                    _next_first_turn = False
                    (
                        response,
                        _turn_messages,
                        _turn_trace_continuity_before,
                        _turn_kwargs,
                    ) = await _call_native_turn(
                        messages=loop_messages,
                        first_turn=_first_turn_for_call,
                        max_tokens=native_turn_max_tokens,
                        call_kind="agent_loop",
                    )
                    if isinstance(response, dict) and getattr(executor, "_use_native", False):
                        executor._capture_responses_continuity_from_response(response)
                    _acknowledge_sent_messages(loop_messages)
                    if response is None and iterations == 1 and not tools_called:
                        (
                            response,
                            _turn_messages,
                            _turn_trace_continuity_before,
                            _turn_kwargs,
                        ) = await _call_native_turn(
                            messages=loop_messages,
                            first_turn=True,
                            max_tokens=native_turn_max_tokens,
                            call_kind="agent_loop_empty_retry",
                        )
                        if isinstance(response, dict) and getattr(executor, "_use_native", False):
                            executor._capture_responses_continuity_from_response(response)
                        _acknowledge_sent_messages(loop_messages)
                    if response is None:
                        response = {
                            "type": "ai_no_result",
                            "no_result": {
                                "reason": (
                                    "first_llm_empty_response" if iterations == 1 else "empty_assistant_content"
                                ),
                                "retryable": False,
                                "retry_attempted": iterations == 1,
                            },
                            "error_state": {
                                "type": "ai_no_result",
                                "reason": (
                                    "first_llm_empty_response" if iterations == 1 else "empty_assistant_content"
                                ),
                            },
                        }
                    while (
                        isinstance(response, dict)
                        and response.get("_recoverable_provider_error") == "max_output_tokens"
                        and _max_output_recovery_count < _MAX_OUTPUT_TOKENS_RECOVERY_LIMIT
                    ):
                        # F-031 (R3-A): Claude does a two-stage cap recovery
                        # (query.ts:1188-1251). Stage 1 retries the SAME
                        # request at ESCALATED_MAX_TOKENS, with no
                        # conversation mutation. Later retries remove the cap
                        # without adding runtime prompt fragments; the model
                        # sees the same task context and decides how to answer.
                        _max_output_recovery_count += 1
                        _stage_one_escalated = _max_output_recovery_count == 1
                        if _stage_one_escalated:
                            executor._append_task_trace_llm_retry_fallback(
                                call_kind="agent_loop",
                                kind="retry",
                                reason="max_output_tokens",
                                next_action="escalate_max_output_tokens_same_request",
                                detail={
                                    "retry_count": _max_output_recovery_count,
                                    "escalated_max_output_tokens": _ESCALATED_MAX_OUTPUT_TOKENS,
                                    "stage": 1,
                                },
                            )
                            (
                                response,
                                _turn_messages,
                                _turn_trace_continuity_before,
                                _turn_kwargs,
                            ) = await _call_native_turn(
                                messages=loop_messages,
                                first_turn=True,
                                max_tokens=_ESCALATED_MAX_OUTPUT_TOKENS,
                                call_kind="agent_loop_max_output_escalated_retry",
                            )
                        else:
                            executor._append_task_trace_llm_retry_fallback(
                                call_kind="agent_loop",
                                kind="retry",
                                reason="max_output_tokens",
                                next_action="retry_uncapped_same_request",
                                detail={
                                    "retry_count": _max_output_recovery_count,
                                    "stage": 2,
                                },
                            )
                            (
                                response,
                                _turn_messages,
                                _turn_trace_continuity_before,
                                _turn_kwargs,
                            ) = await _call_native_turn(
                                messages=loop_messages,
                                first_turn=True,
                                max_tokens=None,
                                call_kind="agent_loop_max_output_retry",
                            )
                        if isinstance(response, dict) and getattr(executor, "_use_native", False):
                            executor._capture_responses_continuity_from_response(response)
                        _acknowledge_sent_messages(loop_messages)
                # tool_choice="auto" always â€” no first_turn override.
                executor._last_usage = _normalize_usage(response) if isinstance(response, dict) else {}
                executor._record_llm_usage_for_context(executor._last_usage)
                executor._last_model_name = (
                    str(response.get("_model_name") or executor._get_model_name_safe())
                    if isinstance(response, dict)
                    else "unknown"
                )
                if isinstance(response, dict) and _turn_kwargs:
                    executor._record_task_trace_llm_exchange(
                        call_kind="agent_loop",
                        request_mode="native",
                        response=response,
                        first_turn=False,
                        continuity_before=_turn_trace_continuity_before,
                        native_messages=_turn_messages,
                        native_kwargs=_turn_kwargs,
                    )
                _turn_reasoning = _extract_reasoning(response) if isinstance(response, dict) else ""
            except (_AgentLoopBillingStop, _AgentLoopCostLimitStop):
                break
            except Exception as exc:
                executor._append_task_trace_llm_attempt_failure(
                    attempt_id=locals().get("_turn_attempt_id"),
                    call_kind="agent_loop",
                    exc=exc,
                )
                logger.exception("Agent turn %d failed: %s", iterations, exc)
                executor._llm_error_occurred = True
                outcome = "llm_error"
                break

            # Per-turn billing: reservation settlement happened in
            # _call_native_turn; keep the post-settle cap recheck so a turn
            # that consumes the last available cents stops cleanly before the
            # next provider call.
            _usage_loop = executor._last_usage or {}
            _loop_out = _usage_loop.get("output_tokens", 0) or 0
            _loop_in = _usage_loop.get("input_tokens", 0) or 0
            _loop_cached = _usage_loop.get("cache_read_tokens", 0) or 0
            _loop_cache_write = (
                _usage_loop.get("cache_write_tokens", 0) or _usage_loop.get("cache_creation_tokens", 0) or 0
            )
            _loop_web_search = _usage_loop.get("web_search_requests", 0) or 0
            if executor._user_id and (_loop_in or _loop_out or _loop_cache_write or _loop_web_search):
                try:
                    # Async variant (see pre-loop fix 39a99061).
                    _spend_gate = await executor._check_managed_llm_spend_cap_async()
                    if not _spend_gate.allowed:
                        logger.info(
                            "Agent loop: managed spend cap reached for %s after step %d period=%s",
                            executor._user_id,
                            iterations,
                            _spend_gate.period,
                        )
                        _set_final_response(
                            executor,
                            executor._managed_llm_budget_message(_spend_gate),
                            continue_listening=False,
                        )
                        executor._final_params["cap_state"] = _spend_gate.cap_state
                        break
                except Exception as _pl_exc:
                    if _handle_agent_loop_billing_failure(_pl_exc):
                        break

            # Waste detector — progress-scoped true-stall safety net.
            # tool_call iterations IS progress; only consecutive empty/tiny
            # answer-text on high context counts as waste. Meta-nudge prompt
            # fragment removed per CLAUDE.md no_prompt_fragments. See
            # _waste_detector_tick docstring + LLC trace a56f64645a87.
            _waste_result = _waste_detector_tick(
                executor=executor,
                response=response,
                loop_out=_loop_out,
                loop_in=_loop_in,
            )
            if _waste_result.terminate:
                logger.warning(
                    "Agent waste detector: %d consecutive empty answer iterations; terminating",
                    executor._consecutive_low_output,
                )
                _set_final_response(
                    executor,
                    _build_agent_final_answer_fallback(
                        executor,
                        user_text,
                        tools_called,
                        reason="empty_final",
                    ),
                    continue_listening=True,
                )
                outcome = "waste_terminated"
                mark_complete(checkpoint, "failed", "Waste detector: empty answer streak")
                hook_dispatch_fn("StopFailure", reason=outcome, user_id=executor._user_id)
                executor._finalize_log(task_log, outcome, run_start_time)
                return AgentResult(
                    ok=False,
                    answer=executor._final_answer,
                    iterations_used=iterations,
                    tools_called=tools_called,
                    error="Agent terminated: consecutive empty answer iterations",
                )

            # â”€â”€ Parse Agent response â”€â”€
            parse_failure_reason = _agent_response_parse_failure_reason(response)
            if parse_failure_reason is not None:
                logger.warning(
                    "Agent response parse failed without runtime retry: %s",
                    parse_failure_reason,
                )
                executor._parse_failed = True
                executor._llm_error_occurred = True
                outcome = "llm_error"
                _set_final_response(
                    executor,
                    "The AI service returned a malformed response. Try again in a moment.",
                    continue_listening=False,
                )
                break

            resp_type = response.get("type", "answer")

            # If the model returned a text answer (no tool call), loop is done.
            # Gate judgment belongs to the unified prompt; deterministic
            # browser/payment safety still happens at action time.
            if resp_type == "answer":
                answer_text = response.get("answer", "")
                cl_value = response.get("continue_listening")
                raw_text = answer_text if isinstance(answer_text, str) else str(answer_text or "")
                _set_final_response(
                    executor,
                    _normalize_terminal_model_text(raw_text),
                    continue_listening=bool(cl_value) if cl_value is not None else None,
                )
                break

            if resp_type == "ai_no_result":
                no_result = response.get("no_result")
                if not isinstance(no_result, dict):
                    no_result = {
                        "reason": response.get("reason", "unknown_ai_no_result"),
                        "retryable": bool(response.get("retryable", True)),
                    }
                error_state = response.get("error_state")
                if not isinstance(error_state, dict):
                    error_state = {"type": "ai_no_result", **no_result}
                executor._llm_error_occurred = not bool(tools_called)
                executor._final_params = {
                    "no_result": no_result,
                    "error_state": error_state,
                }
                operator_diagnostic = no_result.get("operator_diagnostic") or error_state.get("operator_diagnostic")
                if isinstance(operator_diagnostic, dict):
                    executor._final_params["operator_diagnostic"] = operator_diagnostic
                    operator_message = user_message_for_operator_diagnostic(operator_diagnostic)
                    if operator_message:
                        _set_final_response(executor, operator_message, continue_listening=False)
                outcome = "llm_error" if executor._llm_error_occurred else "success"
                break

            if resp_type in {"ask_user", "clarification"}:
                question = response.get("question", response.get("answer", response.get("text", "")))
                raw = question if isinstance(question, str) else str(question)
                _set_final_response(executor, _strip_json_template(raw), continue_listening=True)
                break

            if resp_type == "final_answer":
                answer_text = response.get("answer", response.get("text", ""))
                raw = answer_text if isinstance(answer_text, str) else str(answer_text)
                _set_final_response(
                    executor,
                    _normalize_terminal_model_text(raw),
                    continue_listening=False,
                )
                break

            if resp_type == "answer":
                answer_text = response.get("answer", response.get("text", ""))
                raw = answer_text if isinstance(answer_text, str) else str(answer_text)
                _set_final_response(
                    executor,
                    _normalize_terminal_model_text(raw),
                    continue_listening=bool(response.get("continue_listening", False)),
                )
                break

            if resp_type in {"payment_gate", "signature_gate"}:
                gate_message = response.get("message", response.get("answer", response.get("text", "")))
                raw = gate_message if isinstance(gate_message, str) else str(gate_message)
                _apply_typed_review_gate_response(executor, resp_type, raw)
                logger.info("LLM returned explicit typed review gate response: %s", resp_type)
                break

            if resp_type == "text":
                raw_content = response.get("content", "")
                content = raw_content.strip() if isinstance(raw_content, str) else str(raw_content or "").strip()
                if content:
                    logger.warning(
                        "Agent loop received text fallback response with content_len=%d; treating as answer",
                        len(content),
                    )
                    _set_final_response(
                        executor,
                        _normalize_terminal_model_text(content),
                        continue_listening=False,
                    )
                    break
                logger.warning(
                    "Agent loop received unsupported text fallback response with content_len=0; treating as LLM error"
                )
                executor._llm_error_occurred = True
                outcome = "llm_error"
                _set_final_response(
                    executor,
                    "The AI service hit a snag. Try again in a moment.",
                    continue_listening=False,
                )
                break

            # Extract tool call(s) from Agent response
            if resp_type != "tool_call":
                # Unexpected response type â€” treat as answer
                _set_final_response(
                    executor,
                    response.get("answer", str(response)),
                    continue_listening=None,
                )
                break

            tc_name = response.get("tool", "")
            tc_args = response.get("args", {})
            tc_id = response.get("tool_use_id", "")
            all_tool_calls = response.get("_all_tool_calls") or [
                {"tool": tc_name, "args": tc_args, "tool_use_id": tc_id}
            ]

            # â”€â”€ Append assistant's tool-call turn to loop_messages â”€â”€
            # The Responses API requires function_call items before their
            # function_call_output items.  Without this, iteration 2+ gets
            # "No tool call found for function call output with call_id".
            _raw = response.get("_raw_content")
            _continuity_active_for_initial = bool(
                _response_from_pending_initial and getattr(executor, "_responses_continuity_active", lambda: False)()
            )
            if not _continuity_active_for_initial and _raw and isinstance(_raw, dict) and _raw.get("_openai_assistant"):
                loop_messages.append({"role": "assistant", "content": _raw})
            elif not _continuity_active_for_initial:
                # Build a synthetic assistant turn from the tool calls
                _tc_list = []
                for _tc in all_tool_calls:
                    _tc_list.append(
                        {
                            "id": _tc.get("tool_use_id", ""),
                            "type": "function",
                            "function": {
                                "name": _tc["tool"],
                                "arguments": _json.dumps(_tc.get("args", {}), default=str),
                            },
                        }
                    )
                loop_messages.append(
                    {
                        "role": "assistant",
                        "content": {"_openai_assistant": True, "tool_calls": _tc_list},
                    }
                )
            _record_subagent_transcript()

            # â”€â”€ Execute each tool call â”€â”€
            if not await executor._wait_for_takeover_release():
                continue
            executor._update_overlay_phase("acting")
            tool_results_content: list[dict[str, Any]] = []
            _correction_msgs: list[str] = []
            _media_terminal_fired = False
            _answer_tool_terminal_fired = False
            _parallel_tool_results: dict[str, dict[str, Any]] = {}
            _parallel_safe_batches_by_start: dict[int, Any] = {}
            # Claude parity (S6-004 + F-032 R3-A): partition the assistant's
            # tool_use batch into consecutive safe/unsafe groups. Safe
            # groups run concurrently only when their left-to-right
            # partition is reached; later reads must not overtake earlier
            # unsafe writes.
            #
            # F-032: registered PreToolUse/PostToolUse hooks no longer
            # collapse the safe batch to serial. ``_execute_parallel_tool_batch``
            # accepts a ``canonical_hook_dispatch`` callback and fires
            # ``PreToolUse`` / ``PostToolUse`` per tool inside the
            # concurrent runner. This matches Claude's
            # ``runToolsConcurrently`` shape
            # (``toolOrchestration.ts:152-176``) where ``runToolUse``
            # (with hook dispatch) is invoked inside the parallel runner.
            _tool_calls_for_partition = all_tool_calls if len(all_tool_calls) >= 2 else []
            if _tool_calls_for_partition:
                _batches = executor._partition_tool_calls_for_execution(_tool_calls_for_partition)
                _has_safe_group_of_2_plus = any(
                    batch.is_concurrency_safe and len(batch.tool_calls) >= 2 for batch in _batches
                )
                if _has_safe_group_of_2_plus:
                    logger.info(
                        "Agent loop step %d: partitioned %d tool calls into %d batch(es) "
                        "(safe groups will run in parallel; unsafe tools run serially)",
                        iterations,
                        len(all_tool_calls),
                        len(_batches),
                    )
                    _batch_start = 0
                    for _batch in _batches:
                        if _batch.is_concurrency_safe and len(_batch.tool_calls) >= 2:
                            _parallel_safe_batches_by_start[_batch_start] = _batch
                        _batch_start += len(_batch.tool_calls)

            for _tc_index, tc in enumerate(all_tool_calls):
                tc_name = tc["tool"]
                tc_args = tc.get("args", {})
                tc_id = tc.get("tool_use_id", "")
                _parallel_batch = _parallel_safe_batches_by_start.get(_tc_index)
                if _parallel_batch is not None:
                    # F-032: thread the canonical hook dispatcher into
                    # the concurrent runner so registered hooks fire
                    # per-tool without collapsing the safe batch.
                    def _canonical_hook_dispatch_for_batch(
                        event_name: str,
                        _tool_name: str,
                        _tool_args: dict[str, Any],
                        _context: dict[str, Any],
                    ) -> HookResult:
                        return dispatch_tool_hook(
                            event_name,
                            _tool_name,
                            _tool_args,
                            {
                                "iteration": iterations,
                                "task_id": task_id,
                                "user_id": executor._user_id,
                                "permission_mode": getattr(executor, "_permission_mode", None),
                                **(_context or {}),
                            },
                            dispatch_fn=hook_dispatch_fn,
                            session_id=hook_session_id,
                        )

                    _group_results = await executor._execute_parallel_tool_batch(
                        _parallel_batch.tool_calls,
                        spin_detector=_NoopSpinDetector(),
                        taint_tracker=taint_tracker,
                        page_url=executor._last_page_url,
                        base_index=_tc_index,
                        canonical_hook_dispatch=_canonical_hook_dispatch_for_batch,
                        canonical_hook_context={},
                        tool_allowed=lambda name: _tool_allowed_by_executor(executor, str(name)),
                        rejected_tools=executor._rejected_tools,
                    )
                    _parallel_tool_results.update(_group_results)
                _parallel_cached = _parallel_tool_results.get(executor._tool_call_cache_key(tc, _tc_index))

                tool_start = time.monotonic()

                if tc_name == "answer":
                    answer_text, continue_listening = _answer_text_from_tool_args(tc_args)
                    _set_final_response(
                        executor,
                        answer_text if answer_text else None,
                        continue_listening=continue_listening,
                    )
                    _answer_tool_terminal_fired = True
                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc_id,
                            "content": "Accepted final answer.",
                            "is_error": False,
                        }
                    )
                    logger.info(
                        "Agent loop: LLM called 'answer' tool - treating as final response (len=%d)",
                        len(answer_text or ""),
                    )
                    break

                # â”€â”€ ask_user handling â”€â”€
                # Intentional single-loop behavior:
                # Providers get the consultative question directly instead of
                # a first-iteration think-first detour. This keeps rejected ask_user
                # deterministic and avoids spending another LLM turn just to restate
                # the same question.
                if not _tool_allowed_by_executor(executor, tc_name):
                    _reject_msg = "Tool '%s' is not allowed for this skill." % tc_name
                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc_id,
                            "content": ToolResult(ok=False, error=_reject_msg).to_llm_text(),
                            "is_error": True,
                        }
                    )
                    logger.warning(
                        "Agent loop step %d: BLOCKED %s by skill allowlist",
                        iterations,
                        tc_name,
                    )
                    continue

                if tc_name == "ask_user" and tc_name in executor._rejected_tools:
                    _ask_text = _extract_ask_user_text(tc_args)
                    if _ask_text:
                        logger.info("Agent loop: ask_user rejected â€” converting to consultative answer")
                        _set_final_response(executor, _ask_text, continue_listening=True)
                        tool_results_content.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tc_id,
                                "content": "Converted to consultative question.",
                                "is_error": False,
                            }
                        )
                        break

                # â”€â”€ Rejected tools â”€â”€
                if tc_name in executor._rejected_tools:
                    if tc_name == "ask_user" and tc_args:
                        _ask_text = _extract_ask_user_text(tc_args)
                        if _ask_text:
                            _set_final_response(executor, _ask_text, continue_listening=True)
                            break
                    _reject_msg = "Tool '%s' has been disabled for this task." % tc_name
                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc_id,
                            "content": ToolResult(ok=False, error=_reject_msg).to_llm_text(),
                            "is_error": True,
                        }
                    )
                    logger.warning(
                        "Agent loop step %d: BLOCKED %s â€” rejected",
                        iterations,
                        tc_name,
                    )
                    continue

                if _parallel_cached is not None:
                    tc_name = str(_parallel_cached["tool_name"])
                    tc_args = _parallel_cached["tool_args"]
                tools_called.append(tc_name)
                tool_error: str | None = _parallel_cached.get("tool_error") if _parallel_cached is not None else None
                tool_result: ToolResult | None = (
                    _parallel_cached["tool_result"] if _parallel_cached is not None else None
                )
                _tool_executed = bool(_parallel_cached.get("tool_executed")) if _parallel_cached is not None else False
                pre_tool_hook_result = HookResult()

                # F-032: when the parallel batch fired the canonical
                # PreToolUse/PostToolUse hooks inside the concurrent
                # runner, surface the rendered context onto the
                # next-turn loop_messages in left-to-right order.
                if _parallel_cached is not None:
                    _parallel_pre = _parallel_cached.get("pre_hook_result")
                    if _parallel_pre is not None:
                        pre_tool_hook_result = _parallel_pre
                        _queue_hook_context(
                            "PreToolUse",
                            _parallel_pre,
                            tool_name=tc_name,
                            tool_input=tc_args,
                        )
                    _parallel_post = _parallel_cached.get("post_hook_result")
                    if _parallel_post is not None:
                        _post_event = "PostToolUse" if (tool_result and tool_result.ok) else "PostToolUseFailure"
                        _queue_hook_context(
                            _post_event,
                            _parallel_post,
                            tool_name=tc_name,
                            tool_input=tc_args,
                        )

                if tool_result is None and _parallel_cached is None:
                    pre_tool_hook_result = dispatch_tool_hook(
                        "PreToolUse",
                        tc_name,
                        tc_args,
                        {
                            "iteration": iterations,
                            "task_id": task_id,
                            "user_id": executor._user_id,
                            "permission_mode": getattr(executor, "_permission_mode", None),
                        },
                        dispatch_fn=hook_dispatch_fn,
                        session_id=hook_session_id,
                    )
                    _queue_hook_context(
                        "PreToolUse",
                        pre_tool_hook_result,
                        tool_name=tc_name,
                        tool_input=tc_args,
                    )
                    if pre_tool_hook_result.updated_input is not None:
                        tc_args = copy.deepcopy(pre_tool_hook_result.updated_input)
                    if pre_tool_hook_result.blocks_tool:
                        tool_result = _hook_block_tool_result(tc_name, pre_tool_hook_result)
                        tool_error = tool_result.error
                        permission_hook = dispatch_tool_hook(
                            "PermissionDenied",
                            tc_name,
                            tc_args,
                            {
                                "iteration": iterations,
                                "task_id": task_id,
                                "user_id": executor._user_id,
                                "reason": tool_error,
                            },
                            dispatch_fn=hook_dispatch_fn,
                            session_id=hook_session_id,
                        )
                        _queue_hook_context(
                            "PermissionDenied",
                            permission_hook,
                            tool_name=tc_name,
                            tool_input=tc_args,
                        )
                        if permission_hook.retry:
                            retry_result = HookResult(
                                additional_context="PermissionDenied hook returned retry=true for tool '%s'." % tc_name,
                            )
                            _queue_hook_context(
                                "PermissionDenied",
                                retry_result,
                                tool_name=tc_name,
                                tool_input=tc_args,
                            )

                # â”€â”€ Layer 6: deterministic taint gate (M3 defense) â”€â”€
                # Hard-block high-risk tools when browser content is in context.
                _taint_block = None if _parallel_cached is not None else taint_tracker.check_tool(tc_name)
                if tool_result is None and _taint_block:
                    tool_result = ToolResult(ok=False, error=_taint_block)
                    tool_error = _taint_block
                    logger.warning(
                        "Agent loop step %d: TAINT_GATE blocked %s",
                        iterations,
                        tc_name,
                    )
                elif (
                    tool_result is None
                    and _parallel_cached is None
                    and (validation_error := executor._validate_tool_args(tc_name, tc_args))
                ):
                    tool_result = ToolResult(ok=False, error=validation_error)
                    tool_error = validation_error

                if tool_result is None:
                    _restore_hooked_check_approval: Callable[[], None] | None = None
                    _needs_permission_hook_bridge = _parallel_cached is None and (
                        pre_tool_hook_result.decision in {"allow", "ask"} or _has_registered_hook("PermissionRequest")
                    )
                    if _needs_permission_hook_bridge:
                        bridge = getattr(getattr(executor, "_mcp_hub", None), "_bridge", None)
                        original_check_approval = getattr(bridge, "check_approval", None)
                        permission_policy = getattr(bridge, "_permission_policy", None)
                        build_permission_context = getattr(bridge, "_build_permission_context", None)
                        append_permission_frame = getattr(bridge, "_append_permission_frame", None)
                        approval_manager = getattr(bridge, "_approval", None)
                        if (
                            bridge is not None
                            and callable(original_check_approval)
                            and permission_policy is not None
                            and callable(build_permission_context)
                            and approval_manager is not None
                        ):

                            async def _check_approval_with_hook_semantics(
                                name: str,
                                args: dict[str, Any],
                                channel: Any | None = None,
                            ) -> bool:
                                if name != tc_name:
                                    return bool(await original_check_approval(name, args, channel=channel))

                                from mcp_hub.approval_bridge import (
                                    _request_permission_decision,
                                )

                                from intent.permissions.policy import (
                                    PermissionHookProvenance,
                                )

                                hook_provenance = ()
                                if pre_tool_hook_result.decision in {"allow", "ask"}:
                                    hook_provenance = (
                                        PermissionHookProvenance(
                                            event="PreToolUse",
                                            decision=pre_tool_hook_result.decision,  # type: ignore[arg-type] # AGENT-02: hook decision is schema-normalized upstream.
                                            reason=pre_tool_hook_result.reason,
                                            updated_input=pre_tool_hook_result.updated_input,
                                        ),
                                    )
                                risk = bridge.get_call_risk(name, args)
                                permission_context = build_permission_context(
                                    name,
                                    args,
                                    risk=risk,
                                    channel=channel,
                                    hook_provenance=hook_provenance,
                                )
                                permission_decision = permission_policy.check(permission_context)
                                bridge.last_permission_decision = permission_decision
                                if permission_decision.updated_input is not None:
                                    args.update(permission_decision.updated_input)
                                if permission_decision.behavior == "allow":
                                    return True
                                if permission_decision.behavior == "deny":
                                    if callable(append_permission_frame):
                                        append_permission_frame(permission_decision)
                                    return False

                                # Feed the hook Claude's
                                # ``permission_suggestions`` so a hook can
                                # mirror Claude's accept-with-allow-list
                                # behavior. The suggestions surface common
                                # follow-up permissions Claude would offer at
                                # the UI; here we expose the deny/ask rules
                                # the policy might prompt to add.
                                _permission_suggestions = _build_permission_suggestions(
                                    permission_policy=permission_policy,
                                    tool_name=name,
                                    args=args,
                                )
                                permission_request_hook = dispatch_tool_hook(
                                    "PermissionRequest",
                                    name,
                                    args,
                                    {
                                        "iteration": iterations,
                                        "task_id": task_id,
                                        "user_id": executor._user_id,
                                        "permission_mode": getattr(executor, "_permission_mode", None),
                                        "permission_decision": permission_decision.to_dict(),
                                        "permission_suggestions": _permission_suggestions,
                                    },
                                    dispatch_fn=hook_dispatch_fn,
                                    session_id=hook_session_id,
                                )
                                _queue_hook_context(
                                    "PermissionRequest",
                                    permission_request_hook,
                                    tool_name=name,
                                    tool_input=args,
                                )
                                request_result = permission_request_hook.permission_request_result
                                # An interrupting deny hook
                                # must abort the in-flight permission request
                                # before the interactive prompt fires
                                # (PermissionContext.ts:245-249).
                                if permission_request_hook.interrupt:
                                    if callable(append_permission_frame):
                                        append_permission_frame(permission_decision)
                                    logger.info(
                                        "PermissionRequest hook interrupted approval for tool '%s'",
                                        name,
                                    )
                                    return False
                                if isinstance(request_result, dict):
                                    behavior = str(request_result.get("behavior") or "").strip().lower()
                                    if behavior in {"allow", "deny"}:
                                        # The interrupt decision lives
                                        # inside the permission_request_result
                                        # in Claude's schema as well.
                                        if behavior == "deny" and (
                                            request_result.get("interrupt") or request_result.get("abort")
                                        ):
                                            if callable(append_permission_frame):
                                                append_permission_frame(permission_decision)
                                            return False
                                        request_updated_input = request_result.get("updatedInput")
                                        if request_updated_input is None:
                                            request_updated_input = request_result.get("updated_input")
                                        request_provenance = (
                                            PermissionHookProvenance(
                                                event="PermissionRequest",
                                                decision=behavior,  # type: ignore[arg-type] # AGENT-02: permission behavior is normalized before storage.
                                                reason=str(request_result.get("message") or "")
                                                or permission_request_hook.reason,
                                                updated_input=(
                                                    request_updated_input
                                                    if isinstance(request_updated_input, dict)
                                                    else None
                                                ),
                                            ),
                                        )
                                        request_context = build_permission_context(
                                            name,
                                            args,
                                            risk=risk,
                                            channel=channel,
                                            hook_provenance=request_provenance,
                                        )
                                        request_decision = permission_policy.check(request_context)
                                        bridge.last_permission_decision = request_decision
                                        if request_decision.updated_input is not None:
                                            args.update(request_decision.updated_input)
                                        # Persist Claude-style
                                        # ``updatedPermissions`` once the hook
                                        # successfully resolves an allow.
                                        if request_decision.behavior == "allow":
                                            _apply_updated_permissions(
                                                permission_policy=permission_policy,
                                                hook_result=permission_request_hook,
                                                tool_name=name,
                                            )
                                            return True
                                        if callable(append_permission_frame):
                                            append_permission_frame(request_decision)
                                        return False

                                if callable(append_permission_frame):
                                    append_permission_frame(permission_decision)
                                description = bridge.build_description(name, args)
                                original_channel = getattr(approval_manager, "channel", None)
                                if channel is not None and original_channel is None:
                                    approval_manager._channel = channel
                                try:
                                    approved = await _request_permission_decision(
                                        approval_manager,
                                        permission_decision,
                                        action_description=description,
                                        tool_name=name,
                                        tool_args=args,
                                        task_id=getattr(executor, "task_id", None),
                                    )
                                    final_decision = permission_policy.resolve_user_response(
                                        permission_context,
                                        permission_decision,
                                        approved=approved,
                                    )
                                    bridge.last_permission_decision = final_decision
                                    if final_decision.updated_input is not None:
                                        args.update(final_decision.updated_input)
                                    if final_decision.behavior == "allow":
                                        return True
                                    if callable(append_permission_frame):
                                        append_permission_frame(final_decision)
                                    return False
                                finally:
                                    if channel is not None and original_channel is None:
                                        approval_manager._channel = original_channel

                            bridge.check_approval = _check_approval_with_hook_semantics

                            def _restore_check_approval() -> None:
                                bridge.check_approval = original_check_approval

                            _restore_hooked_check_approval = _restore_check_approval
                        elif pre_tool_hook_result.decision == "ask":
                            _hook_error = "PreToolUse requested approval, but no approval bridge was available."
                            tool_result = ToolResult(
                                ok=False,
                                error=_hook_error,
                                error_category="HOOK_ASK_APPROVAL_UNAVAILABLE",
                                retryable=False,
                            )
                            tool_error = _hook_error
                    # â”€â”€ Execute tool â”€â”€
                    if tool_result is None:
                        _tool_executed = True
                        try:
                            _publish_deferred_tool_pool()
                            with latency_spans.span("TOOL_EXEC", tool=str(tc_name)):
                                tool_result = await executor._execute_tool(
                                    tc_name,
                                    tc_args,
                                    tool_use_id=tc_id or None,
                                )
                        except Exception as exc:
                            tool_error = str(exc)
                            tool_result = ToolResult(ok=False, error=tool_error)
                            executor._capture_diagnostic(
                                user_text=getattr(executor, "_agent_task_description", "") or user_text or "",
                                exception=exc,
                                stage="tool_execution",
                                iteration=iterations,
                                tool_name=tc_name,
                                tool_result_text=tool_error,
                            )
                        finally:
                            if _restore_hooked_check_approval is not None:
                                _restore_hooked_check_approval()

                tool_result = _cap_tool_result_data(tool_result, MAX_TOOL_RESULT_CHARS)

                tool_elapsed_ms = (
                    int(_parallel_cached["duration_ms"])
                    if _parallel_cached is not None
                    else int((time.monotonic() - tool_start) * 1000)
                )

                # Failure handling: tool_result.error_category / retryable /
                # recovery_hint are populated by the executor + MCP hub (typed
                # fields on ToolResult) and surfaced to the model via
                # to_llm_text(). The model decides whether to retry by emitting
                # a fresh tool_use -- the runtime never silently re-executes a
                # tool the model did not ask to re-execute. (Parity target:
                # Claude Code TS routes failures by structured error code, not
                # by regex over arbitrary error text. See R5-P0-B.)

                if _tool_executed:
                    post_context = {
                        "iteration": iterations,
                        "task_id": task_id,
                        "user_id": executor._user_id,
                        "result": tool_result,
                        "tool_error": tool_error or tool_result.error,
                    }
                    if tool_result.ok:
                        post_tool_hook_result = dispatch_tool_hook(
                            "PostToolUse",
                            tc_name,
                            tc_args,
                            post_context,
                            dispatch_fn=hook_dispatch_fn,
                            session_id=hook_session_id,
                        )
                        _queue_hook_context(
                            "PostToolUse",
                            post_tool_hook_result,
                            tool_name=tc_name,
                            tool_input=tc_args,
                        )
                        if post_tool_hook_result.updated_mcp_tool_output_present and _is_mcp_tool(executor, tc_name):
                            # Rewriting is limited to the applicable
                            # successful tool result. Claude only applies the
                            # rewrite to MCP tools (toolExecution.ts:1494-1498).
                            tool_result = _tool_result_with_updated_mcp_tool_output(
                                tool_result,
                                post_tool_hook_result.updated_mcp_tool_output,
                            )
                        elif post_tool_hook_result.updated_mcp_tool_output_present:
                            logger.debug(
                                "PostToolUse hook returned updated_mcp_tool_output for non-MCP tool %s; "
                                "ignoring (Claude parity)",
                                tc_name,
                            )
                        if post_tool_hook_result.prevent_continuation and tool_result.ok:
                            tool_result = _hook_block_tool_result(tc_name, post_tool_hook_result)
                            tool_error = tool_result.error
                        if post_tool_hook_result.prevent_continuation:
                            executor._forced_tool_failure_error = tool_error or post_tool_hook_result.reason
                            executor._forced_tool_failure_outcome = "tool_error"
                    else:
                        failure_hook_result = dispatch_tool_hook(
                            "PostToolUseFailure",
                            tc_name,
                            tc_args,
                            {
                                **post_context,
                                "interrupted": tool_result.error_category == "CANCELLED",
                            },
                            dispatch_fn=hook_dispatch_fn,
                            session_id=hook_session_id,
                        )
                        _queue_hook_context(
                            "PostToolUseFailure",
                            failure_hook_result,
                            tool_name=tc_name,
                            tool_input=tc_args,
                        )
                        if failure_hook_result.prevent_continuation:
                            executor._forced_tool_failure_error = tool_error or failure_hook_result.reason
                            executor._forced_tool_failure_outcome = "tool_error"
                    if tc_name in executor._c2_invalidation_tools:
                        executor.invalidate_context_cache()

                cwd_changed_context = _cwd_changed_context_for_tool(
                    executor,
                    tc_name,
                    tc_args,
                    tool_result,
                    task_id=task_id,
                    user_id=executor._user_id,
                )
                if cwd_changed_context is not None:
                    cwd_hook_context = {
                        **cwd_changed_context,
                        "session_id": hook_session_id,
                    }
                    cwd_changed_hook = dispatch_lifecycle("CwdChanged", cwd_hook_context, **hook_dispatch_kwargs)
                    _queue_hook_context("CwdChanged", cwd_changed_hook)

                file_changed_context = _file_changed_context_for_tool(
                    tc_name,
                    tc_args,
                    tool_result,
                    task_id=task_id,
                    user_id=executor._user_id,
                )
                if file_changed_context is not None:
                    file_hook_context = {
                        **file_changed_context,
                        "session_id": hook_session_id,
                    }
                    file_changed_hook = dispatch_lifecycle("FileChanged", file_hook_context, **hook_dispatch_kwargs)
                    _queue_hook_context("FileChanged", file_changed_hook)

                tool_result = _cap_tool_result_data(tool_result, MAX_TOOL_RESULT_CHARS)
                result_text = tool_result.to_llm_text()

                _calendar_failure_message = _calendar_add_failure_user_message(tc_name, tc_args, tool_result)
                if _calendar_failure_message is not None:
                    # R5-P0-A (2026-05-30): no longer mutates tool_result.recovery_hint
                    # — the prose channel into the LLM was deleted. The user-facing
                    # message still reaches the user via _set_final_response; the
                    # model sees the structured error on the next turn if the loop
                    # continues. Re-classifying the error text into a prose hint is
                    # a separate anti-pattern (R5-P0-B / _calendar_add_failure_user_message)
                    # that should be addressed by deleting the helper, not by
                    # routing its output through ToolResult.
                    _set_final_response(executor, _calendar_failure_message, continue_listening=True)
                    executor._forced_tool_failure_error = tool_result.error or "calendar tool failed"
                    executor._forced_tool_failure_outcome = "tool_error"

                if session_memory is not None:
                    try:
                        session_memory.update_after_tool(
                            tc_name,
                            tc_args,
                            {
                                "ok": tool_result.ok,
                                "data": tool_result.data,
                                "error": tool_result.error,
                            },
                        )
                    except Exception as exc:
                        logger.debug(
                            "Agent loop SessionMemory update failed for %s: %s",
                            tc_name,
                            exc,
                        )

                _remember_last_tool_result(executor, tc_name, tool_result)
                _nested_gate = _nested_gate_from_resume_tool_result(tc_name, tool_result)
                if _nested_gate is not None:
                    _nested_gate_type = str(_nested_gate["gate_type"])
                    _nested_answer = str(_nested_gate["answer"])
                    _nested_confirmation_url = str(_nested_gate.get("confirmation_url") or "")
                    if _nested_gate_type == "payment_gate":
                        executor._payment_gate_requested = True
                        if _nested_confirmation_url:
                            from intent.agent_executor import _payment_gate_answer_with_confirm_url

                            _nested_answer = (
                                _payment_gate_answer_with_confirm_url(
                                    _nested_answer,
                                    _nested_confirmation_url,
                                )
                                or _nested_answer
                            )
                        _set_final_response(
                            executor,
                            _nested_answer,
                            continue_listening=False,
                            params={
                                "nested_gate_result": dict(_nested_gate),
                                "payment_gate_confirmation_url": _nested_confirmation_url,
                            },
                        )
                    elif _nested_gate_type == "signature_gate":
                        executor._signature_gate_requested = True
                        _set_final_response(
                            executor,
                            _nested_answer,
                            continue_listening=False,
                            params={"nested_gate_result": dict(_nested_gate)},
                        )
                _terminal_tool_response = _terminal_response_from_tool_result(tc_name, tool_result)
                if _terminal_tool_response is not None and len(all_tool_calls) == 1:
                    _set_final_response(executor, _terminal_tool_response, continue_listening=False)
                    _media_terminal_fired = True

                # Surface ui_action hints (e.g. knowledge open_folder) so the
                # ai_controller can merge them into the response envelope and
                # the React app can dispatch them. Without this, native-MCP tool
                # results that include ui_action never reach the client.
                if isinstance(tool_result.data, dict) and tool_result.data.get("ui_action"):
                    executor._final_params["ui_action_payload"] = tool_result.data

                # â”€â”€ Compress browser snapshots â”€â”€
                executor._snapshot_compressed_this_step = False
                _pre_compress_len = len(_strip_image_from_result(result_text))
                result_text = _compress_browser_result(tc_name, result_text)
                executor._snapshot_compressed_this_step = len(_strip_image_from_result(result_text)) < _pre_compress_len

                # â”€â”€ Sanitize untrusted content + set taint (prompt injection defense) â”€â”€
                if tc_name in _BROWSER_CONTENT_TOOLS:
                    result_text = _sanitize_web_content(result_text)
                    if tc_name in _BROWSER_PAGE_CONTENT_TOOLS:
                        # Layer 6: mark taint so high-risk tools are blocked
                        taint_tracker.mark_tainted(tc_name)
                elif _is_non_browser_untrusted_source(tc_name):
                    # SEC-001/SEC-008: email, calendar, memory, and file content
                    # are untrusted sources too. Bound them through the same
                    # sanitizer (size/charset) and taint the context so the
                    # high-risk gate fires on these vectors — and so navigation
                    # sinks are gated to close exfil-via-navigation (SEC-004).
                    result_text = _sanitize_web_content(result_text)
                    taint_tracker.mark_tainted(tc_name)

                content_for_llm: Any = result_text
                # Claude parity (S6-011): per-tool result mappers. MCP tools
                # return raw MCP content (text/image/resource blocks) and
                # ToolSearch returns ``tool_reference`` blocks — neither
                # should be wrapped in Viola's legacy ``{ok, data}`` JSON
                # envelope. Other tools keep the legacy envelope.
                try:
                    from intent.tool_result_mappers import (
                        map_tool_result_to_content,
                    )

                    _mapped = map_tool_result_to_content(
                        tool_name=tc_name,
                        tool_result=tool_result,
                        executor=executor,
                    )
                except (AttributeError, ImportError, TypeError, ValueError) as exc:
                    logger.debug(
                        "Per-tool result mapper raised for %s; falling back to legacy envelope: %s",
                        tc_name,
                        exc,
                    )
                    _mapped = None
                if _mapped is not None:
                    content_for_llm = _mapped
                elif (
                    tc_name in {"ToolSearch", "tool_search"}
                    and isinstance(tool_result.data, dict)
                    and isinstance(tool_result.data.get("tool_references"), list)
                ):
                    # Preserve legacy ToolSearch error path for parity tests
                    # that still check for tool_reference blocks even on
                    # partial failure.
                    tool_reference_blocks = [
                        dict(item)
                        for item in tool_result.data.get("tool_references", [])
                        if isinstance(item, dict) and item.get("type") == "tool_reference"
                    ]
                    if tool_reference_blocks:
                        content_for_llm = tool_reference_blocks
                _has_image = isinstance(tool_result.data, dict) and bool(tool_result.data.get("image_base64"))
                _has_snapshot = isinstance(tool_result.data, dict) and "snapshot" in tool_result.data
                _forward_image = _should_forward_tool_image_to_native(
                    tc_name,
                    tool_result,
                    use_native=getattr(executor, "_use_native", False),
                )
                executor._step_had_screenshot = _forward_image
                executor._step_snapshot_pre_len = _pre_compress_len
                if _has_image:
                    if _forward_image:
                        _img_block = {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": tool_result.data.get("mime_type", "image/png"),
                                "data": tool_result.data["image_base64"],
                            },
                        }
                        if _has_snapshot:
                            content_for_llm = [
                                {
                                    "type": "text",
                                    "text": _strip_image_from_result(result_text),
                                },
                                _img_block,
                            ]
                        else:
                            content_for_llm = [
                                {
                                    "type": "text",
                                    "text": '{"ok": true, "title": "%s", "url": "%s"}'
                                    % (
                                        tool_result.data.get("title", ""),
                                        tool_result.data.get("url", ""),
                                    ),
                                },
                                _img_block,
                            ]
                    else:
                        content_for_llm = _strip_image_from_result(result_text)

                # â”€â”€ Append tool result â”€â”€
                executor._record_task_trace_tool_execution(
                    tool_name=tc_name,
                    tool_input=tc_args,
                    tool_use_id=tc_id,
                    tool_result=tool_result,
                    model_visible_result=content_for_llm,
                    duration_ms=tool_elapsed_ms,
                    tool_error=tool_error,
                )
                tool_results_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": content_for_llm,
                        "is_error": not tool_result.ok,
                    }
                )
                try:
                    progress_state = executor._record_tool_progress_pattern(
                        tool_name=tc_name,
                        tool_args=tc_args,
                        tool_result=tool_result,
                        result_text=result_text,
                    )
                    if progress_state.get("stuck"):
                        executor._pending_stuck_tool_progress = progress_state
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    logger.debug("Repeated tool progress tracking failed", exc_info=True)

                # â”€â”€ Post-execution bookkeeping via middleware pipeline â”€â”€
                # Browser tier tracking (lightweight, not worth a middleware)
                if tc_name == "check_api_registry":
                    executor._api_registry_checked = True
                _tier = _BROWSER_TIER_MAP.get(tc_name)
                if _tier:
                    executor._browser_tiers_used.add(_tier)

                try:
                    # Update shared context for this tool call
                    mw_ctx.iteration = iterations
                    mw_ctx.tool_calls_made = tools_called
                    mw_ctx.correction_messages = _correction_msgs
                    mw_ctx.consecutive_click_failures = executor._consecutive_click_failures
                    mw_ctx.consecutive_failures = executor._consecutive_failures
                    mw_ctx.llm_reasoning = _turn_reasoning
                    mw_ctx.tool_use_id = str(tc_id or "").strip() or None
                    mw_ctx.model_messages_snapshot = copy.deepcopy(
                        _sanitize_agent_messages_for_checkpoint(
                            [
                                *loop_messages,
                                {
                                    "role": "user",
                                    "content": copy.deepcopy(tool_results_content),
                                },
                            ]
                        )
                    )

                    # Run the middleware pipeline (URL tracking,
                    # tool blacklisting, click backoff, payment gate, progress
                    # reporting, step logging)
                    await run_middleware_pipeline(
                        middlewares=middlewares,
                        context=mw_ctx,
                        tool_name=tc_name,
                        tool_result=tool_result,
                        tool_args=tc_args,
                        tool_error=tool_error,
                        result_text=result_text,
                        tool_elapsed_ms=tool_elapsed_ms,
                    )

                    # Sync state back from middleware context to executor
                    executor._consecutive_failures = mw_ctx.consecutive_failures
                    executor._consecutive_click_failures = mw_ctx.consecutive_click_failures
                    _correction_msgs = mw_ctx.correction_messages

                    spin_detector.is_spinning()
                    executor._build_consecutive_failure_warning()

                    if tc_name == "memory" and tc_args.get("action") == "recall" and tool_result.ok:
                        try:
                            for _mid_match in _MEMORY_ID_RE.finditer(result_text or ""):
                                executor._recalled_memory_ids.append(int(_mid_match.group(1)))
                        except Exception as exc:
                            logger.debug("Memory recall ID tracking failed: %s", exc)

                    if tool_error or not tool_result.ok:
                        err_ctx = executor._build_error_registry_telemetry(tc_name)
                        if err_ctx:
                            executor._pending_error_registry_ctx = err_ctx

                    # Update provider tool filters if blacklist middleware changed them.
                    from intent.agent_executor import _is_approval_block_error

                    if _is_approval_block_error(getattr(tool_result, "error_category", None)):
                        await provider.update_tool_filters(
                            allowed_tools=executor._allowed_tools,
                            rejected_tools=executor._rejected_tools,
                        )

                except Exception as _bookkeeping_exc:
                    logger.warning(
                        "Agent loop bookkeeping failed for %s: %s",
                        tc_name,
                        _bookkeeping_exc,
                    )
                if executor._forced_tool_failure_error is not None:
                    break

            # Claude parity (S6-006): synthesize ``is_error`` tool_result
            # blocks for any tool_use IDs that did NOT produce a result
            # this iteration. Without this, an early ``break``
            # (forced-failure, ask_user conversion, answer-tool terminal,
            # rejected tool, user interrupt) leaves orphan ``tool_use``
            # blocks the provider rejects with
            # ``tool_use ids must be matched by tool_result``. Claude
            # generates ``createSyntheticErrorMessage`` for the same case
            # at ``services/tools/StreamingToolExecutor.ts:153-205``.
            _emitted_result_ids = {
                str(block.get("tool_use_id") or "")
                for block in tool_results_content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            }
            for _expected in all_tool_calls:
                _expected_id = str(_expected.get("tool_use_id") or "")
                if not _expected_id or _expected_id in _emitted_result_ids:
                    continue
                _reason = (
                    "sibling_error"
                    if executor._forced_tool_failure_error
                    else "user_interrupted" if executor._cancelled else "abandoned"
                )
                tool_results_content.append(executor.synthetic_cancel_tool_result(_expected_id, _reason))
                _emitted_result_ids.add(_expected_id)

            # â”€â”€ Check if ask_user conversion ended the loop â”€â”€
            if executor._final_answer and executor._final_continue_listening:
                for _cm in _correction_msgs:
                    tool_results_content.append({"type": "text", "text": _cm})
                if tool_results_content:
                    _had_browser_nav = _browser_nav_detected_from_tool_results(
                        [str(call.get("tool", "")) for call in all_tool_calls],
                        tool_results_content,
                    )
                    loop_messages.append({"role": "user", "content": tool_results_content})
                    if _had_browser_nav:
                        _strip_old_browser_refs(loop_messages)
                    _record_subagent_transcript()
                    tool_results_content = []
                    _flush_pending_hook_context()
                break
            if _answer_tool_terminal_fired and executor._final_answer is not None:
                for _cm in _correction_msgs:
                    tool_results_content.append({"type": "text", "text": _cm})
                if tool_results_content:
                    _had_browser_nav = _browser_nav_detected_from_tool_results(
                        [str(call.get("tool", "")) for call in all_tool_calls],
                        tool_results_content,
                    )
                    loop_messages.append({"role": "user", "content": tool_results_content})
                    if _had_browser_nav:
                        _strip_old_browser_refs(loop_messages)
                    _record_subagent_transcript()
                    tool_results_content = []
                    _flush_pending_hook_context()
                break

            # â”€â”€ Flush correction messages â”€â”€
            for _cm in _correction_msgs:
                tool_results_content.append({"type": "text", "text": _cm})

            # â”€â”€ Append tool results to conversation â”€â”€
            # Gates can terminate the loop immediately after a tool executes.
            # Persist the function_call_output before those breaks so checkpoint
            # resume can replay a valid Responses history.
            if tool_results_content:
                _had_browser_nav = _browser_nav_detected_from_tool_results(
                    [str(call.get("tool", "")) for call in all_tool_calls],
                    tool_results_content,
                )
                loop_messages.append({"role": "user", "content": tool_results_content})
                if _had_browser_nav:
                    _strip_old_browser_refs(loop_messages)
                _record_subagent_transcript()
                tool_results_content = []
                _flush_pending_hook_context()

            # â”€â”€ Payment gate check (middleware may have flagged it) â”€â”€
            if mw_ctx.should_stop and mw_ctx.stop_reason in {
                "payment_gate",
                "signature_gate",
            }:
                logger.info(
                    "%s triggered (via middleware) - terminating Agent loop",
                    mw_ctx.stop_reason,
                )
                mw_ctx.should_stop = False
                break

            if bool(getattr(executor, "_payment_gate_requested", False)) or bool(
                getattr(executor, "_signature_gate_requested", False)
            ):
                logger.info("Review gate requested by explicit tool call - terminating Agent loop")
                break

            # media search_play played successfully as the sole tool call —
            # the runtime already has the spoken result, so skip the extra
            # model turn that would only restate it (saves one LLM round-trip).
            if _media_terminal_fired and executor._final_answer:
                logger.info("Agent loop: media search_play terminal response — skipping final answer turn")
                break

            # â”€â”€ Append tool results to conversation â”€â”€
            # CRITICAL: Without this, the model never sees what tools returned.
            if tool_results_content:
                loop_messages.append({"role": "user", "content": tool_results_content})
                _record_subagent_transcript()
                _flush_pending_hook_context()

        # â”€â”€ Normal loop exit: build result â”€â”€
        final_answer = executor._final_answer or ""
        payment_gate = bool(getattr(executor, "_payment_gate_requested", False))
        signature_gate = bool(getattr(executor, "_signature_gate_requested", False))
        if not final_answer:
            final_answer = _build_agent_final_answer_fallback(
                executor,
                user_text,
                tools_called,
                reason=("llm_error" if executor._llm_error_occurred else "empty_final"),
            )
            _set_final_response(
                executor,
                final_answer,
                continue_listening=True,
                params={
                    "error_state": {
                        "diagnostic": {
                            "category": "agent_final_answer_fallback",
                            "reason": ("llm_error" if executor._llm_error_occurred else "empty_final"),
                        },
                    },
                },
            )

        from intent.agent_executor import _apply_approval_blocked_final_answer

        final_answer = _apply_approval_blocked_final_answer(executor, final_answer)

        if payment_gate:
            outcome = "payment_gate"
        if signature_gate:
            outcome = "signature_gate"

        from intent.agent_executor import _final_response_overrides_llm_error

        final_overrides_llm_error = _final_response_overrides_llm_error(
            final_answer=final_answer,
            final_command=None,
            last_response=getattr(executor, "_task_trace_last_response", None),
            payment_gate=payment_gate,
            signature_gate=signature_gate,
            continue_listening=executor._final_continue_listening,
        )
        if executor._llm_error_occurred and not final_overrides_llm_error:
            outcome = "llm_error"
        elif executor._forced_tool_failure_error is not None:
            outcome = executor._forced_tool_failure_outcome or "tool_error"

        terminal_hook_name = _terminal_hook_event_name(executor, outcome)
        if _has_registered_hook(terminal_hook_name):
            terminal_hook = dispatch_lifecycle(
                terminal_hook_name,
                _terminal_hook_context(
                    executor,
                    event_name=terminal_hook_name,
                    outcome=outcome,
                    task_id=task_id,
                    tools_called=tools_called,
                    final_answer=final_answer,
                ),
                dispatch_fn=hook_dispatch_fn,
                session_id=hook_session_id,
            )
            if terminal_hook_name != "StopFailure":
                if terminal_hook_name == "SubagentStop":
                    executor._subagent_stop_dispatched = True
                _queue_hook_context(terminal_hook_name, terminal_hook, defer_until_tool_result=False)
                if _hook_requests_model_continuation(terminal_hook):
                    _flush_pending_hook_context()
                    _record_subagent_transcript()
                    _prepare_terminal_hook_continuation(executor)
                    # S1-003: Claude Code re-enters the main loop body when a
                    # Stop hook blocks (``src/query.ts:1267-1305``), so a model
                    # that responds with a ``tool_call`` after the block gets
                    # its tool executed and the conversation continues.  Our
                    # prior implementation only honored ``answer``/``final_answer``
                    # and treated everything else as ``llm_error``.  The
                    # bounded continuation loop below executes up to
                    # ``_TERMINAL_HOOK_TOOL_RETRIES`` tool calls before
                    # settling, preserving the Stop-hook contract without
                    # reintroducing a parallel loop body.
                    _terminal_tool_retries = 0
                    response = None
                    while True:
                        try:
                            (
                                response,
                                _turn_messages,
                                _turn_trace_continuity_before,
                                _turn_kwargs,
                            ) = await _call_native_turn(
                                messages=loop_messages,
                                first_turn=_terminal_tool_retries == 0,
                                max_tokens=native_turn_max_tokens,
                                call_kind=(
                                    "agent_loop_terminal_hook_retry"
                                    if _terminal_tool_retries == 0
                                    else "agent_loop_terminal_hook_tool_retry"
                                ),
                            )
                        except (_AgentLoopBillingStop, _AgentLoopCostLimitStop):
                            response = None
                            break
                        if isinstance(response, dict) and getattr(executor, "_use_native", False):
                            executor._capture_responses_continuity_from_response(response)
                        _acknowledge_sent_messages(loop_messages)
                        if not isinstance(response, dict):
                            break
                        retry_type = response.get("type", "answer")
                        if retry_type != "tool_call":
                            break
                        if _terminal_tool_retries >= _TERMINAL_HOOK_TOOL_RETRIES:
                            logger.warning(
                                "Terminal hook continuation exceeded tool retry budget (%d); "
                                "settling on llm_error to avoid spin",
                                _TERMINAL_HOOK_TOOL_RETRIES,
                            )
                            break
                        _terminal_tool_retries += 1
                        tool_executed = await _terminal_hook_execute_tool_call(
                            executor,
                            response,
                            loop_messages,
                            dispatch_fn=hook_dispatch_fn,
                            session_id=hook_session_id,
                            iteration=iterations,
                            task_id=task_id,
                            queue_hook_context=_queue_hook_context,
                            spin_detector=spin_detector,
                            taint_tracker=taint_tracker,
                            page_url=executor._last_page_url,
                        )
                        if not tool_executed:
                            logger.warning(
                                "Terminal hook continuation tool call could not be executed; "
                                "falling back to llm_error"
                            )
                            break
                        _flush_pending_hook_context()
                    if isinstance(response, dict):
                        executor._last_usage = _normalize_usage(response)
                        executor._record_llm_usage_for_context(executor._last_usage)
                        executor._last_model_name = str(response.get("_model_name") or executor._get_model_name_safe())
                        retry_type = response.get("type", "answer")
                        if retry_type in {"answer", "final_answer"}:
                            retry_answer = response.get("answer", response.get("text", ""))
                            retry_text = retry_answer if isinstance(retry_answer, str) else str(retry_answer or "")
                            retry_continue = response.get("continue_listening")
                            _set_final_response(
                                executor,
                                _normalize_terminal_model_text(retry_text),
                                continue_listening=(bool(retry_continue) if retry_continue is not None else None),
                            )
                            final_answer = executor._final_answer or ""
                            payment_gate = bool(getattr(executor, "_payment_gate_requested", False))
                            signature_gate = bool(getattr(executor, "_signature_gate_requested", False))
                            outcome = (
                                "payment_gate" if payment_gate else "signature_gate" if signature_gate else "success"
                            )
                        elif retry_type in {"ask_user", "clarification"}:
                            retry_question = response.get(
                                "question",
                                response.get("answer", response.get("text", "")),
                            )
                            retry_text = (
                                retry_question if isinstance(retry_question, str) else str(retry_question or "")
                            )
                            _set_final_response(
                                executor,
                                _strip_json_template(retry_text),
                                continue_listening=True,
                            )
                            final_answer = executor._final_answer or ""
                            payment_gate = False
                            signature_gate = False
                            outcome = "success"
                        elif retry_type in {"payment_gate", "signature_gate"}:
                            gate_message = response.get(
                                "message",
                                response.get("answer", response.get("text", "")),
                            )
                            raw_gate_message = (
                                gate_message if isinstance(gate_message, str) else str(gate_message or "")
                            )
                            _apply_typed_review_gate_response(executor, retry_type, raw_gate_message)
                            final_answer = executor._final_answer or ""
                            payment_gate = bool(getattr(executor, "_payment_gate_requested", False))
                            signature_gate = bool(getattr(executor, "_signature_gate_requested", False))
                            outcome = (
                                "payment_gate" if payment_gate else "signature_gate" if signature_gate else "success"
                            )
                        elif retry_type == "text":
                            raw_content = response.get("content", "")
                            content = (
                                raw_content.strip() if isinstance(raw_content, str) else str(raw_content or "").strip()
                            )
                            if content:
                                _set_final_response(
                                    executor,
                                    _normalize_terminal_model_text(content),
                                    continue_listening=False,
                                )
                                final_answer = executor._final_answer or ""
                                payment_gate = bool(getattr(executor, "_payment_gate_requested", False))
                                signature_gate = bool(getattr(executor, "_signature_gate_requested", False))
                                outcome = (
                                    "payment_gate"
                                    if payment_gate
                                    else ("signature_gate" if signature_gate else "success")
                                )
                            else:
                                executor._llm_error_occurred = True
                                outcome = "llm_error"
                                final_answer = _build_agent_final_answer_fallback(
                                    executor,
                                    user_text,
                                    tools_called,
                                    reason="llm_error",
                                )
                                _set_final_response(executor, final_answer, continue_listening=True)
                        else:
                            executor._llm_error_occurred = True
                            outcome = "llm_error"
                            final_answer = _build_agent_final_answer_fallback(
                                executor,
                                user_text,
                                tools_called,
                                reason="llm_error",
                            )
                            _set_final_response(executor, final_answer, continue_listening=True)
                    else:
                        executor._llm_error_occurred = True
                        outcome = "llm_error"
                        final_answer = _build_agent_final_answer_fallback(
                            executor,
                            user_text,
                            tools_called,
                            reason="llm_error",
                        )
                        _set_final_response(executor, final_answer, continue_listening=True)

        if outcome == "success":
            incomplete_diagnostic = _terminal_structured_incomplete_diagnostic(executor, task_log)
            if incomplete_diagnostic:
                outcome = "incomplete"
                _record_structured_incomplete_diagnostic(executor, incomplete_diagnostic)

        if (
            outcome in ("success", "payment_gate", "signature_gate")
            and _current_subagent_id(executor)
            and _has_registered_hook("TaskCompleted")
        ):
            task_completed_hook = dispatch_lifecycle(
                "TaskCompleted",
                _task_completed_hook_context(
                    executor,
                    task_id=task_id,
                    user_text=user_text,
                    outcome=outcome,
                    final_answer=final_answer,
                ),
                dispatch_fn=hook_dispatch_fn,
                session_id=hook_session_id,
            )
            _queue_hook_context("TaskCompleted", task_completed_hook, defer_until_tool_result=False)
            if _hook_requests_model_continuation(task_completed_hook):
                _flush_pending_hook_context()
                _record_subagent_transcript()
                _prepare_terminal_hook_continuation(executor)
                final_answer = executor._final_answer or final_answer

        if (
            outcome in ("success", "payment_gate", "signature_gate")
            and _current_subagent_id(executor)
            and _has_registered_hook("TeammateIdle")
        ):
            teammate_idle_hook = dispatch_lifecycle(
                "TeammateIdle",
                _teammate_idle_hook_context(executor, outcome=outcome, final_answer=final_answer),
                dispatch_fn=hook_dispatch_fn,
                session_id=hook_session_id,
            )
            _queue_hook_context("TeammateIdle", teammate_idle_hook, defer_until_tool_result=False)
            if _hook_requests_model_continuation(teammate_idle_hook):
                _flush_pending_hook_context()
                _record_subagent_transcript()
                _prepare_terminal_hook_continuation(executor)
                final_answer = executor._final_answer or final_answer

        payment_confirmation_ctx: dict[str, Any] | None = None
        if payment_gate:
            prebuilt_confirmation_url = str(
                getattr(executor, "_final_params", {}).get("payment_gate_confirmation_url") or ""
            ).strip()
            if prebuilt_confirmation_url:
                from intent.agent_executor import _payment_gate_answer_with_confirm_url

                final_answer = (
                    _payment_gate_answer_with_confirm_url(final_answer, prebuilt_confirmation_url) or final_answer
                )
                _set_final_response(executor, final_answer, continue_listening=False)
            else:
                try:
                    payment_confirmation_ctx = await executor._prepare_payment_confirmation_session()
                    from intent.agent_executor import (
                        _dispatch_payment_confirmation_link_for_executor,
                        _payment_gate_answer_with_confirm_url,
                    )

                    final_answer = _payment_gate_answer_with_confirm_url(
                        final_answer,
                        str(payment_confirmation_ctx.get("url") or ""),
                    )
                    await _dispatch_payment_confirmation_link_for_executor(
                        executor,
                        payment_confirmation_ctx,
                        str(payment_confirmation_ctx.get("url") or ""),
                    )
                    _set_final_response(executor, final_answer, continue_listening=False)
                except _PaymentConfirmationGateRefused as exc:
                    logger.warning("Payment confirmation gate refused: %s", exc)
                    final_answer = (
                        "Payment review is not ready because the browser is no longer on the merchant checkout page. "
                        "No order has been placed. Ask me to continue and I can return to checkout."
                    )
                    _set_final_response(executor, final_answer, continue_listening=False)
                    payment_gate = False
                    outcome = "incomplete"
                    _record_structured_incomplete_diagnostic(
                        executor,
                        {
                            "reason": "payment_confirmation_refused",
                            "domain": "commerce_order",
                            "evidence_source": "payment_confirmation_session",
                            "error": str(exc),
                        },
                    )
                except Exception:
                    logger.exception("Payment confirmation session preparation failed")

        _record_subagent_transcript()
        # Populate the checkpoint from live executor/loop state synchronously
        # (this read must precede the return, before any next turn reuses the
        # executor); the encrypt+serialize+disk *write* is deferred below.
        _snapshot_agent_checkpoint(executor, checkpoint, loop_messages, compress_messages)
        # Lane C (#466): on the clean warm-answer path, the completed-checkpoint
        # write is post-answer bookkeeping the user should never wait on -- defer
        # it off the response path (a completed task is never resumed, so the
        # write has no recovery value). Gate (waiting_for_user) and every
        # non-success outcome keep persisting synchronously because their
        # durability IS load-bearing for resume.
        _defer_completed_checkpoint = outcome == "success" and not payment_gate and not signature_gate
        if _defer_completed_checkpoint:
            executor._schedule_completed_checkpoint_persist(checkpoint, outcome)
        else:
            executor._finalize_gate_checkpoint(
                checkpoint,
                final_answer=final_answer,
                outcome=outcome,
                payment_gate=payment_gate,
                signature_gate=signature_gate,
                payment_confirmation_ctx=payment_confirmation_ctx,
            )
        terminal_llm_error = executor._llm_error_occurred and not final_overrides_llm_error
        terminal_cost_limit = outcome == "cost_limit"
        terminal_incomplete = outcome == "incomplete"
        final_ok = (
            not terminal_llm_error
            and executor._forced_tool_failure_error is None
            and not terminal_cost_limit
            and not terminal_incomplete
        )
        if terminal_llm_error:
            final_error = "LLM call failed"
        elif terminal_cost_limit:
            final_error = "managed LLM spend governance unavailable"
        elif terminal_incomplete:
            final_error = "Task incomplete"
        else:
            final_error = executor._forced_tool_failure_error
        result = AgentResult(
            ok=final_ok,
            answer=final_answer,
            command=None,
            params=executor._final_params,
            iterations_used=iterations,
            tools_called=tools_called,
            error=final_error,
            continue_listening=(True if signature_gate else executor._final_continue_listening),
            cap_state=executor._final_params.get("cap_state"),
        )
        if payment_gate:
            result.payment_gate = True
            if payment_confirmation_ctx:
                result.confirmation_url = str(payment_confirmation_ctx["url"])
                result.confirmation_link_delivery = payment_confirmation_ctx.get("link_dispatch")
            elif getattr(executor, "_final_params", {}).get("payment_gate_confirmation_url"):
                result.confirmation_url = str(executor._final_params["payment_gate_confirmation_url"])
            result.origin_channel = executor._get_origin_channel_type()
            executor._payment_confirmation_ctx = payment_confirmation_ctx
            executor._payment_gate_active = True
        if signature_gate:
            result.signature_gate = True
            result.origin_channel = executor._get_origin_channel_type()
            executor._signature_gate_active = True
        if (payment_gate or signature_gate) and executor._last_page_url:
            result.gate_page_url = executor._last_page_url
        if outcome in {
            "timeout",
            "spin_terminated",
            "llm_error",
            "tool_error",
            "waste_terminated",
            "cost_limit",
        }:
            hook_dispatch_fn("StopFailure", reason=outcome, user_id=executor._user_id)
        executor._finalize_log(task_log, outcome, run_start_time)
        return result

    except Exception as exc:
        logger.exception("Agent loop failed: %s", exc)
        outcome = "error"
        _record_subagent_transcript()
        _snapshot_agent_checkpoint(executor, checkpoint, loop_messages, compress_messages)
        checkpoint.context["interruption_reason"] = "agent_loop_error"
        checkpoint.context["interruption_error"] = str(exc)[:500]
        mark_complete(checkpoint, "interrupted", outcome)
        hook_dispatch_fn("StopFailure", reason=outcome, user_id=executor._user_id)
        executor._capture_diagnostic(
            user_text=user_text,
            exception=exc,
            stage="agent_loop",
            iteration=iterations,
        )
        executor._finalize_log(task_log, outcome, run_start_time)
        return AgentResult(
            ok=False,
            answer="Something went wrong during the task. Try again?",
            iterations_used=iterations,
            tools_called=tools_called,
            error=str(exc),
        )
    finally:
        _dispatch_session_end("agent_loop_teardown")
        hook_file_watcher = getattr(executor, "_hook_file_watcher", None)
        if hook_file_watcher is not None:
            try:
                await hook_file_watcher.stop()
            except (RuntimeError, OSError, ValueError) as exc:
                logger.debug("Hook file watcher stop skipped: %s", exc)
        if _spawn_subtask_token is not None:
            reset_spawn_subtask_callback(_spawn_subtask_token)
