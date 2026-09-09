"""Agent executor - the iterative tool-use loop.

Implements the core agentic loop: given an initial tool call from the LLM,
executes the tool, feeds the result back to the LLM, and repeats until
the LLM returns a final answer or a command, or limits are reached.

Supports the canonical native tool-calling mode: structured ``tool_use`` /
``tool_result`` content blocks via ``route_command_native()``.

Each run produces structured diagnostics written to both
``logs/agent_tasks/<task_id>.json`` and the canonical per-task trace under
``data/traces/by_user/<user-hash>/YYYYMMDD/<task_id>.trace.jsonl.zst.enc``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import difflib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable

from config.env import get_bool as _env_get_bool
from core.asyncio_safe import is_event_loop_closed_error
from core.logging_config import get_logger
from core.platform import get_logs_dir
from intent.agent_message_manager import (
    AgentMessageManager,
    record_tool_result_replacements,
    tool_result_replacement_session_id,
)
from intent.approval import (
    ApprovalManager,
    ConfirmationDeferred,
)
from intent.context_compaction import (
    compact_messages,
    compact_native_messages,
    is_token_limit_error,
)
from intent.gate_protocol import (
    PAYMENT_GATE_PREFIX as _PAYMENT_GATE_PREFIX,
    SIGNATURE_GATE_PREFIX as _SIGNATURE_GATE_PREFIX,
    gate_message_body,
)
from intent.hooks import HookEvent, dispatch as _dispatch_hook, hook_result_frames
from intent.irreversible_actions import (
    describe_irreversible_action,
    irreversible_action_class,
)
from intent.log_redaction import redact_card_data, redact_pii
from intent.post_answer_teardown import (
    drain_owner as _drain_post_answer_owner,
    schedule_post_answer_coro,
    schedule_post_answer_sync,
)
from intent.response_cleanup import strip_json_template as _strip_json_template
from intent.spin_detector import SpinDetector
from intent.subagents.lifecycle import (
    SubagentNotification,
    build_forked_messages,
    bundle_with_drained_notifications,
    clone_prompt_bundle_for_subagent,
    ensure_subagent_transcript_seeded,
    make_subagent_session_id,
    normalize_subagent_mode,
    subagent_lifecycle,
    subagent_transcript_path,
)
from intent.task_checkpoint import (
    CheckpointStep,
    TaskCheckpoint,
    append_step,
    compress_messages,
    generate_task_id,
    mark_complete,
    save_checkpoint,
)
from intent.task_trace import TaskTraceWriter
from intent.token_budget import (
    TokenBudgetTracker,
    _estimate_message_tokens,
    apply_context_window_override,
)
from intent.tool_types import AgentResult, RiskLevel, ToolCall, ToolResult
from intent.trace_write_behind import AsyncTaskTraceWriter
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    SystemReminderBlock,
    TextBlock,
)
from services.conversation.message_invariants import (
    SYNTHETIC_TOOL_RESULT_CONTENT,
    build_synthetic_tool_result_block,
)
from services.idempotency import (
    classify_action,
    get_idempotency_store,
)
from services.llm.cost_circuit_breaker import get_circuit_breaker
from services.llm.openai_utils import (
    RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID as _RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID,
    RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS as _RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS,
    normalize_response_continuity as _normalize_openai_response_continuity,
)
from services.llm.operator_diagnostics import (
    classify_llm_operator_error,
    user_message_for_operator_diagnostic,
)
from services.llm.prompts import append_system_text
from utils.text_encoding import repair_mojibake

logger = get_logger(__name__)

_TOOLS_KILL_SWITCH_ERROR = "Tool execution is temporarily paused by the launch kill-switch."


def _launch_tools_kill_switch_open() -> bool:
    try:
        from backend.launch_kill_switches import get_store

        return get_store().is_enabled("tools")
    except Exception:
        logger.exception("Launch tools kill-switch check failed closed")
        return False


def _prior_context_role_messages(
    messages: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Return prior context as role-preserving messages without summarizing."""

    role_messages: list[dict[str, str]] = []
    for msg in messages or []:
        role = str(msg.get("role") or "user").strip().lower()
        if role not in {"system", "user", "assistant"}:
            role = "user"
        content = msg.get("content")
        if content is None:
            continue
        text = str(content)
        if not text:
            continue
        role_messages.append({"role": role, "content": text})
    return role_messages


def _compact_prior_context_lines(
    messages: list[dict[str, str]] | None,
    *,
    max_messages: int,
    max_chars: int,
) -> list[str]:
    """Return the legacy compact prior-context summary."""

    formatted: list[str] = []
    for msg in (messages or [])[-max_messages:]:
        role = str(msg.get("role") or "user").strip().lower()
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        if role == "assistant":
            label = "Assistant"
        elif role == "system":
            label = "System"
        else:
            label = "User"
        formatted.append("%s: %s" % (label, content[:max_chars]))
    return formatted


def _prior_context_frames(
    messages: list[dict[str, str]] | None,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
) -> list[Frame]:
    """Convert prior role messages into the canonical prompt-frame chain."""

    frames: list[Frame] = []
    for index, msg in enumerate(_prior_context_role_messages(messages)):
        role = msg["role"]
        content = msg["content"]
        if role == "assistant":
            frames.append(
                Frame(
                    kind=FrameKind.ASSISTANT_TEXT,
                    role=FrameRole.ASSISTANT,
                    blocks=(TextBlock(text=content),),
                    origin="prior_context",
                    task_id=task_id,
                    session_id=session_id,
                    extra={"prior_context_index": index},
                )
            )
        elif role == "system":
            frames.append(
                Frame(
                    kind=FrameKind.SYSTEM_REMINDER,
                    role=FrameRole.META_USER,
                    blocks=(
                        SystemReminderBlock(
                            text=content,
                            source_tag="prior_context",
                        ),
                    ),
                    is_meta=True,
                    origin="prior_context",
                    task_id=task_id,
                    session_id=session_id,
                    extra={"prior_context_index": index},
                )
            )
        else:
            frames.append(
                Frame(
                    kind=FrameKind.USER_INPUT,
                    role=FrameRole.USER,
                    blocks=(TextBlock(text=content),),
                    origin="prior_context",
                    task_id=task_id,
                    session_id=session_id,
                    extra={"prior_context_index": index},
                )
            )
    return frames


def _bundle_with_prior_context_frames(
    bundle: PromptFrameBundle | None,
    prior_frames: list[Frame],
) -> PromptFrameBundle:
    """Prepend lossless prior-context frames to the provider-bound bundle."""

    if not prior_frames:
        return bundle or PromptFrameBundle()

    source = bundle or PromptFrameBundle()
    existing_frames = source.frames or [
        *source.meta_user_frames,
        *source.history_frames,
        *([source.current_user_frame] if source.current_user_frame is not None else []),
    ]
    history_frames = [*prior_frames, *source.history_frames]
    frames = (
        [*prior_frames, *existing_frames]
        if source.frames
        else [
            *source.meta_user_frames,
            *history_frames,
            *([source.current_user_frame] if source.current_user_frame is not None else []),
        ]
    )
    return PromptFrameBundle(
        system_static_blocks=list(source.system_static_blocks),
        system_dynamic_blocks=list(source.system_dynamic_blocks),
        meta_user_frames=list(source.meta_user_frames),
        history_frames=history_frames,
        current_user_frame=source.current_user_frame,
        cache_boundary_present=source.cache_boundary_present,
        frames=frames,
        provider_normalized=source.provider_normalized,
    )


_PROVIDER_PRIVATE_REQUEST_KWARGS = frozenset(
    {
        "continuity",
        "messages_are_delta",
        "previous_response_id",
        "response_items",
        "responses_continuity",
    }
)
_PROVIDER_PRIVATE_RESPONSE_KEYS = frozenset(
    {
        "_continuity",
        "_converted_responses_payload",
        "_encrypted_reasoning_items",
        "_provider_attempts",
        "_provider_payload",
        "_raw_content",
        "_reasoning",
        "_reasoning_items",
        "_responses_payload",
        "_responses_continuity",
        "_thinking_blocks",
    }
)


def _trace_safe_copy(value: Any) -> Any:
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


def _estimated_tokens_for_messages(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages if isinstance(message, dict))


_PAID_ACTION_GATE_ERROR_CODES = frozenset(
    {
        "login_required_for_paid_action",
        "phone_tos_required",
    }
)
_SIGNATURE_REVIEW_ERROR_PREFIXES = frozenset(
    {
        "SIGNATURE GATE BLOCKED:",
        "LEGAL SIGNATURE DETECTED:",
    }
)
_PAYMENT_REVIEW_ERROR_PREFIXES = frozenset(
    {
        "PAYMENT METHOD DETECTED:",
        "PAYMENT SAFETY VIOLATION:",
    }
)


def _paid_action_gate_payload(result: ToolResult) -> dict[str, Any] | None:
    if result.ok or not isinstance(result.data, dict):
        return None
    payload = dict(result.data)
    nested_data = payload.get("data")
    if isinstance(nested_data, dict):
        payload = {**nested_data, **payload}
    error_code = payload.get("error_code")
    if error_code not in _PAID_ACTION_GATE_ERROR_CODES:
        return None
    payload["error_code"] = error_code
    if result.error and not payload.get("message"):
        payload["message"] = result.error
    return payload


def _review_required_payload_from_error(error: str | None) -> dict[str, str] | None:
    text = str(error or "").strip().upper()
    if any(text.startswith(prefix) for prefix in _SIGNATURE_REVIEW_ERROR_PREFIXES):
        return {
            "error_category": "signature_review_required",
            "required_tool": "signature",
            "required_action": "request_review",
            "boundary": "legal_signature",
        }
    if any(text.startswith(prefix) for prefix in _PAYMENT_REVIEW_ERROR_PREFIXES):
        return {
            "error_category": "payment_review_required",
            "required_tool": "payment",
            "required_action": "request_review",
            "boundary": "payment",
        }
    return None


def _enrich_review_required_tool_result(result: ToolResult) -> ToolResult:
    if result.ok:
        return result
    payload = _review_required_payload_from_error(result.error)
    if payload is None:
        return result

    data = dict(result.data) if isinstance(result.data, dict) else {}
    data.setdefault("ok", False)
    if result.error:
        data.setdefault("error", result.error)
    for key, value in payload.items():
        data.setdefault(key, value)
    result.data = data
    result.error_category = result.error_category or payload["error_category"]
    return result


def _bundle_with_hook_result_frames(
    bundle: PromptFrameBundle | None,
    event_name: str,
    result: Any,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> PromptFrameBundle:
    """Prepend model-visible hook output without dropping existing history."""

    from intent.hooks.schema import hook_result_from_value

    source = bundle or PromptFrameBundle()
    hook_result = hook_result_from_value(result, expected_event=event_name)
    event = HookEvent(
        name=event_name,
        payload={"agent_id": agent_id} if agent_id else {},
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
    )
    hook_frames = hook_result_frames(event, hook_result, task_id=task_id or agent_id)
    if not hook_frames:
        return source

    existing_frames = source.frames or [
        *source.meta_user_frames,
        *source.history_frames,
        *([source.current_user_frame] if source.current_user_frame is not None else []),
    ]
    return PromptFrameBundle(
        system_static_blocks=list(source.system_static_blocks),
        system_dynamic_blocks=list(source.system_dynamic_blocks),
        meta_user_frames=[*hook_frames, *source.meta_user_frames],
        history_frames=list(source.history_frames),
        current_user_frame=source.current_user_frame,
        cache_boundary_present=source.cache_boundary_present,
        frames=[*hook_frames, *existing_frames],
        provider_normalized=False,
    )


def _schema_tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if name is None and isinstance(tool.get("function"), dict):
        name = tool["function"].get("name")
    return str(name or "").strip()


def _shared_state_tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for tool in tools or []:
        name = _schema_tool_name(tool)
        if not name:
            continue
        lower_name = name.lower()
        if lower_name in _BACKGROUND_SHARED_STATE_TOOL_NAMES or lower_name.startswith(
            _BACKGROUND_SHARED_STATE_TOOL_PREFIXES
        ):
            names.append(name)
    return sorted(set(names))


def _background_shared_state_error(shared_names: list[str]) -> str:
    preview = ", ".join(shared_names[:5])
    suffix = "" if len(shared_names) <= 5 else ", ..."
    return (
        "start_agent cannot run this child in the background because it would inherit shared browser/tool state "
        "from the parent (%s%s). Background execution is unavailable for child runs that need "
        "browser/payment/desktop state."
    ) % (preview, suffix)


def _child_success_result(answer: str, child_result: AgentResult | None = None) -> ToolResult:
    text = str(answer or "")
    data: dict[str, Any] = {
        "answer": text,
        "final_text": text,
        "content": [{"type": "text", "text": text}],
    }
    if child_result is not None:
        data.update(
            {
                "payment_gate": bool(child_result.payment_gate),
                "signature_gate": bool(child_result.signature_gate),
                "gate_page_url": child_result.gate_page_url,
                "confirmation_url": child_result.confirmation_url,
                "continue_listening": child_result.continue_listening,
            }
        )
    return ToolResult(
        ok=True,
        data=data,
    )


def _child_answer_text(result: ToolResult | str) -> str:
    if isinstance(result, ToolResult):
        if not result.ok:
            raise RuntimeError(result.error or "Child agent failed.")
        data = result.data
        if isinstance(data, dict):
            for key in ("answer", "final_text"):
                value = data.get(key)
                if value is not None:
                    return str(value)
            content = data.get("content")
            if isinstance(content, list):
                return "\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict)).strip()
        return str(data or "")
    return str(result or "")


def _resume_messages_from_subagent_transcript(messages: Any, message: str) -> list[dict[str, Any]]:
    """Build provider messages for SendMessage resume from durable transcript state."""

    resumed: list[dict[str, Any]] = []
    for item in messages or ():
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            continue
        content = copy.deepcopy(item.get("content"))
        if isinstance(content, str):
            if not content.strip():
                continue
        elif isinstance(content, list):
            filtered_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "").strip()
                if block_type in {"thinking", "redacted_thinking"}:
                    continue
                if block_type == "text" and not str(block.get("text") or "").strip():
                    continue
                filtered_blocks.append(copy.deepcopy(block))
            if not filtered_blocks:
                continue
            content = filtered_blocks
        else:
            continue
        resumed.append({"role": role, "content": content})
    resumed.append({"role": "user", "content": str(message)})
    return resumed


class _PaymentConfirmationGateRefused(RuntimeError):
    """Raised when PAYMENT_GATE cannot safely bind to a merchant checkout page."""


def _gate_state_record_params(
    *,
    gate_type: str,
    final_answer: str,
    status: str,
    task_id: str,
    confirmation_url: str | None = None,
    last_blocker: str | None = None,
    required_fields: list[str] | None = None,
    collected_fields: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Build record params that persist gate state as a meta-user frame."""

    normalized_gate_type = str(gate_type or "").strip().lower()
    gate_name = "SIGNATURE_GATE" if normalized_gate_type == "signature" else "PAYMENT_GATE"
    gate_state = {
        "name": gate_name,
        "gate_type": normalized_gate_type or gate_type,
        "status": status,
        "task_id": str(task_id or ""),
        "summary": final_answer,
        "confirmation_url": confirmation_url or "",
        "last_blocker": last_blocker or "",
        "required_fields": list(required_fields or []),
        "collected_fields": dict(collected_fields or {}),
        "do_not_treat_prior_bail_messages_as_examples": True,
    }
    return {
        "frame_kind": FrameKind.SYSTEM_REMINDER.value,
        "frame_role": FrameRole.META_USER.value,
        "frame_origin": "agent_gate",
        "frame_is_meta": True,
        "schema_version": 1,
        "gate_state": gate_state,
    }


class _ToolExecutionCancelled(RuntimeError):
    """Raised when the executor cancellation event wins a tool wait."""


class _ToolExecutionTimedOut(RuntimeError):
    """Raised when a tool exceeds its per-call timeout budget."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__("Tool execution timed out after %.1fs" % timeout_seconds)
        self.timeout_seconds = timeout_seconds


class _ToolExecutionInterruptedByTakeover(RuntimeError):
    """Raised when user browser takeover wins a browser-tool wait."""


# Defense-in-depth only: managed LLM spend caps remain the primary governor.
# This hard ceiling bounds blast radius if spend governance fails open.
MAX_AGENT_ITERATIONS = 200
# The default leaves room for a multi-stage gated workflow without cutting off
# the interaction mid-flow.
_DEFAULT_MAX_ITERATIONS = 75
_CONTEXT_COMPACTION_USAGE_THRESHOLD = 0.80
# Safety net only: consecutive-identical-no-effect tool results above this
# count are treated as a true spin. Per TS parity the model gets raw results
# and decides; this is NOT a raw repeat-count gate (see _record_tool_progress_pattern).
_STUCK_TOOL_REPEAT_LIMIT = 5
_BROWSER_RUN_SCRIPT_READ_PROBE_LIMIT = 4
# Issue #278: the browser MCP tool (mcp_servers/browser/server.py
# `_check_dead_page` / `_http_error_page_payload`) inspects the RENDERED
# PAGE'S OWN TEXT and the REAL HTTP RESPONSE for known bot-check/WAF-challenge
# signals ("checking your browser...", "just a moment...", "domain for sale",
# a >=400 status such as the founder-reported Cloudflare 524 gateway-timeout
# hit navigating to YouTube) -- objective facts about the live page/response
# the tool observed, not a classification of the model's reply or the user's
# query. It surfaces these as a structured `page_health.status` /
# `http_error_page` field on browser_navigate / browser_snapshot results
# (see `_tool_progress_page_health_status`, which normalizes both into one
# signal). Consecutive observations of one of these statuses -- regardless
# of which browser tool produced them or which exact URL/args were used --
# means the browser session is not making forward progress. This is a
# session-wide, tool-agnostic loop-control signal (distinct from the
# per-(tool,args) spin gate below, which requires identical repeats of the
# SAME call).
_NO_PROGRESS_PAGE_HEALTH_STATUSES = frozenset({"bot_protection", "parked_or_expired_domain", "http_error_page"})
# Small on purpose: a real Cloudflare/WAF challenge does not resolve by
# blindly retrying navigate/snapshot, so there is no benefit to burning
# more managed-LLM turns waiting for one to "pass" on its own. Bounded
# per the ticket's minimal-fix spec ("same page/state N times -> halt").
_BOT_PROTECTION_HALT_STREAK = 3
_MEMORY_ALL_SCOPE_PATHS = frozenset({"", "memory", "memory/", "MEMORY.md", "memory/MEMORY.md"})
_MEMORY_SEARCH_ACTIONS = frozenset({"search", "grep"})
_MEMORY_ENUMERATION_ACTIONS = frozenset({"read", "list"})
_MEMORY_MUTATING_ACTIONS = frozenset({"write", "edit", "delete"})
_DEFAULT_TIMEOUT = (
    900.0  # total wall-clock seconds (15 min) â€” config source of truth is settings.agent_timeout_seconds
)
_MAX_PARSE_RETRIES = 2  # retries when LLM response is malformed
# INT-15: Per-tool execution timeout.  A single tool that takes longer than
# this is cancelled and an error is injected into the agent loop so the
# model can pivot.  Existing _cancel_event-based mid-tool cancellation
# still applies; this is the fall-back when no user cancellation fires.
_TOOL_EXECUTION_TIMEOUT_S = 120.0

MAX_DELEGATION_DEPTH = 1  # recursion guard for child agents (no recursive sub-agents)
_FOREGROUND_AUTO_BACKGROUND_SECONDS = 120.0

# Tools that dispatch or manipulate background agents. Filtered out for
# child agents (depth >= MAX_DELEGATION_DEPTH) so children can't spawn
# grandchildren or cancel sibling work.
_ORCHESTRATOR_DISPATCH_TOOLS = frozenset(
    {
        "spawn_subtask",
        "spawn_parallel_subtasks",
        "start_agent",
        "check_agents",
        "cancel_agent",
        "send_message",
    }
)
_LEGACY_DELEGATION_TOOLS = frozenset({"spawn_subtask", "spawn_parallel_subtasks"})
_BACKGROUND_SHARED_STATE_TOOL_PREFIXES: tuple[str, ...] = (
    "browser_",
    "desktop_",
    "computer",
    "payment",
    "signature",
)
_BACKGROUND_SHARED_STATE_TOOL_NAMES = frozenset(
    {
        "fill_payment_details",
        "verify_state",
    }
)

# A5: Tool-result size budgeting â€” truncate oversized tool outputs
MAX_TOOL_RESULT_CHARS = 50_000

# C1: Diminishing-returns heuristic â€” detect repeated identical results
_DIMINISHING_RETURNS_THRESHOLD = 5

# Item 5: Progress summary interval (tool calls between summaries)
_SUMMARY_INTERVAL = 10
_RESPONSES_COMPACTION_FAILURE_LIMIT = 3


def _responses_compaction_error_types() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (
        OSError,
        RuntimeError,
        TimeoutError,
        TypeError,
        ValueError,
    )
    try:
        from openai import APIConnectionError, APIError, APITimeoutError
    except ImportError:
        return errors
    return errors + (APIError, APIConnectionError, APITimeoutError)


_PARALLEL_READ_ONLY_TOOLS = frozenset(
    {
        "web_search",
        "web_read",
        "get_weather",
        "weather",
        "search_tracks",
        "toolsearch",
        "tool_search",
    }
)
_PARALLEL_READ_ONLY_ACTIONS: dict[str, frozenset[str]] = {
    "calendar": frozenset({"list", "today", "tomorrow", "week", "get", "find_free_time"}),
    "google_calendar": frozenset({"list_events", "list_calendars", "get_event", "find_free_time"}),
    "gmail": frozenset({"search", "read", "list_labels"}),
    "google_drive": frozenset({"search", "download", "find_folder", "get_comments"}),
    "google_docs": frozenset({"get_text", "get_suggestions"}),
    "google_sheets": frozenset({"get_text", "get_range", "get_metadata"}),
    "google_slides": frozenset({"get_text", "get_metadata", "get_images", "get_thumbnail"}),
}
_PARALLEL_EXCLUDED_PREFIXES = (
    "browser_",
    "desktop_",
    "computer",
    "payment",
    "signature",
)

# 2B: Read-before-write â€” browser observation tools and modifying tools
# Browser escalation tier mapping (#10)
_BROWSER_TIER_MAP: dict[str, str] = {
    "browser_navigate": "ref",
    "browser_interact": "ref",
    "browser_fill_form": "ref",
    "browser_run_script": "script",
    "browser_screenshot": "visual",
}

_SIGNATURE_GATE_OVERRIDE_ACTIONS = 4
_SIGNATURE_RESUME_PAYMENT_BOUNDARY = (
    "If continuing reaches checkout, payment, order review, card, bank, or charge controls, do not enter payment "
    'details or submit; call payment(action="request_review", ...) with the merchant, total, order summary, and '
    "page URL before any payment entry or payment action."
)
_NATIVE_TOOL_CALLING_UNSUPPORTED_TEXT = (
    "This feature requires the OpenAI backend. Set OPENAI_API_KEY in your .env file to enable it."
)
# Sentinel returned by _call_llm_for_answer when the LLM fails to produce a
# usable summary.  Callers compare against this exact string to detect the
# "empty answer" edge case and retry or fall back to tool-result synthesis.
_SUMMARY_FALLBACK_SENTINEL = "I investigated but couldn't form a clear summary."
_GENERIC_PAYMENT_DESCRIPTIONS = (
    "checkout page reached",
    "handing off to secure payment confirmation",
    "payment step detected on checkout page",
    "payment fields detected on checkout page",
    "checkout is ready for secure confirmation",
    "secure confirmation and payment review",
)
_AGENT_CHAIN_CAP_MESSAGE = "I've completed as much as I can in one task. Want me to continue?"
from intent.content_sanitizer import (
    BROWSER_CONTENT_TOOLS as _BROWSER_CONTENT_TOOLS,
    BROWSER_PAGE_CONTENT_TOOLS as _BROWSER_PAGE_CONTENT_TOOLS,
    canonicalize_tool_name_for_safety,
)

_CALENDAR_ADD_FAILURE_MESSAGE = (
    "I couldn't add the event because the start time format wasn't recognized. "
    "Please use a concrete date and time, such as tomorrow at 3pm or 15:00."
)
_PAYMENT_GATE_REQUEST_ACTIONS: frozenset[str] = frozenset(
    {
        "request_review",
        "checkout_ready",
        "handoff",
    }
)
_SIGNATURE_GATE_REQUEST_ACTIONS: frozenset[str] = frozenset(
    {
        "request_review",
        "sign_ready",
        "handoff",
    }
)


def _looks_like_truncated_final_answer(text: str | None) -> bool:
    """Deprecated: final-answer truncation is provider-signal driven."""

    return False


def _normalize_terminal_model_text(text: str | None) -> str:
    return _strip_json_template(str(text or ""))


def _remember_last_tool_result(executor: Any, tool_name: str, tool_result: ToolResult | None) -> None:
    """Record a compact last-result snapshot for deterministic final fallbacks."""

    executor._last_tool_name = tool_name
    executor._last_tool_ok = bool(getattr(tool_result, "ok", False)) if tool_result is not None else None
    executor._last_tool_error = getattr(tool_result, "error", None) if tool_result is not None else None
    executor._last_tool_data = getattr(tool_result, "data", None) if tool_result is not None else None
    evidence = _completion_evidence_from_tool_data(executor._last_tool_data)
    if evidence:
        evidence_snapshot = {
            "tool_name": tool_name,
            "evidence": copy.deepcopy(evidence),
        }
        executor._last_completion_evidence = copy.deepcopy(evidence_snapshot)
        if _completion_evidence_is_incomplete(evidence):
            executor._last_incomplete_completion_evidence = copy.deepcopy(evidence_snapshot)
        else:
            executor._last_incomplete_completion_evidence = None
    try:
        executor._last_tool_result_text = tool_result.to_llm_text() if tool_result is not None else ""
    except Exception:
        executor._last_tool_result_text = str(tool_result or "")
    if tool_name in {"resume_signature_gate", "resume_payment_gate"}:
        gate_kind = "signature" if tool_name == "resume_signature_gate" else "payment"
        setattr(executor, "_last_%s_resume_ok" % gate_kind, executor._last_tool_ok)
        setattr(executor, "_last_%s_resume_error" % gate_kind, executor._last_tool_error)
        setattr(executor, "_last_%s_resume_data" % gate_kind, executor._last_tool_data)
        setattr(
            executor,
            "_last_%s_resume_text" % gate_kind,
            executor._last_tool_result_text,
        )


def _terminal_response_from_tool_result(tool_name: str, tool_result: ToolResult | None) -> str | None:
    """R3-P1-F (2026-05-30): retired short-circuit. Always returns None.

    The prior implementation peeked at ``tool_result.data["terminal_response"]``
    for ``media`` ``search_play`` results and let the runtime emit a
    tool-authored prose string as the model's final answer — skipping
    the model's final assistant turn. That violated the parity rule
    that final answers come from the model (Claude Code TS issues one
    more assistant turn after the tool result; the model voices the
    one-line acknowledgement).

    The function body is kept (returning None) so the call site at
    ``intent/agent_loop.py:4206`` does not need to know the
    short-circuit is gone; a future cleanup can fold both away
    together. The matching ``terminal_response`` field has been
    deleted from ``intent/tools/media_tools.py``.
    """
    del tool_name, tool_result  # short-circuit retired
    return None


def _nested_gate_from_resume_tool_result(tool_name: str, tool_result: ToolResult | None) -> dict[str, Any] | None:
    """Return structured nested gate evidence from a gate-resume tool result."""

    if tool_name not in {"resume_signature_gate", "resume_payment_gate"}:
        return None
    if tool_result is None or not tool_result.ok or not isinstance(tool_result.data, dict):
        return None
    data = tool_result.data
    if data.get("payment_gate") is True:
        gate_type = "payment_gate"
    elif data.get("signature_gate") is True:
        gate_type = "signature_gate"
    else:
        return None
    answer = str(data.get("message") or data.get("answer") or "").strip()
    if not answer:
        return None
    return {
        "gate_type": gate_type,
        "answer": answer,
        "confirmation_url": str(data.get("confirmation_url") or "").strip(),
        "task_id": str(data.get("task_id") or "").strip(),
    }


# R5-P0-E (2026-05-30): stable honest sentinel for empty loop exits.
#
# Prior implementation synthesized a 3-line "Status:/Progress:/Next:" envelope
# in the model's voice using regex/substring scans of the last tool result and
# a per-reason prose ladder (`_gate_resume_fallback` + per-reason status/
# next_step branches). That violated CLAUDE.md "Viola's runtime must trust the
# model" — Viola was writing the assistant's reply for it whenever the model
# didn't emit a final answer. Parity target: Claude Code TS `src/query.ts:984`
# calls `createAssistantAPIErrorMessage` and surfaces the real error plainly,
# with the comment "Surface the real error instead of a misleading
# '[Request interrupted by user]' — this path is a model/runtime failure, not
# a user action." No Status/Progress/Next fabrication.
#
# The structured operator-diagnostic path (`reason="llm_error"` +
# `operator_diagnostic`) is preserved — it surfaces the categorized,
# user-actionable cause (provider quota/rate-limit/auth/etc.) from
# `services.llm.operator_diagnostics.user_message_for_operator_diagnostic`.
_AGENT_FINAL_ANSWER_FALLBACK_SENTINEL = (
    "I couldn't complete that — let me know if you want me to try again or take a different approach."
)


def _build_agent_final_answer_fallback(
    executor: Any,
    user_text: str,
    tools_called: list[str],
    *,
    reason: str,
    operator_diagnostic: dict[str, Any] | None = None,
) -> str:
    """Return a stable sentinel for empty loop exits.

    When ``reason="llm_error"`` and an ``operator_diagnostic`` is supplied,
    the categorized user message from
    ``services.llm.operator_diagnostics.user_message_for_operator_diagnostic``
    wins — that surfaces the real underlying cause (provider quota/rate-
    limit/auth/etc.). Otherwise the sentinel is returned verbatim: no
    per-reason prose ladder, no last-tool-message keyword scan, no canned
    Status/Progress/Next envelope synthesized in the model's voice.
    """

    del executor, user_text, tools_called  # sentinel ignores call-site context

    if reason == "llm_error" and operator_diagnostic:
        from services.llm.operator_diagnostics import (
            user_message_for_operator_diagnostic,
        )

        diagnostic_msg = user_message_for_operator_diagnostic(operator_diagnostic)
        if diagnostic_msg:
            return diagnostic_msg

    return _AGENT_FINAL_ANSWER_FALLBACK_SENTINEL


# Per-tool timeout overrides for tools whose natural duration exceeds the
# default agent_tool_timeout_seconds (180s). Phone calls run for the full
# max_call_duration of the underlying call (default 600s + 60s grace).
# The 180s default kills phone calls mid-conversation and leaves _run_call
# tasks orphaned in the background, polluting the active-call gate state
# for subsequent dial attempts. See feedback_no_black_box.md and the
# 2026-05-08 dental dial investigation.
_PER_TOOL_TIMEOUT_OVERRIDES_SECS: dict[str, float] = {
    "phone": 660.0,
    "make_phone_call": 660.0,
    "check_call_status": 30.0,
    "end_phone_call": 30.0,
    # Gate resume tools run a nested agent loop that can continue a live browser
    # filing flow. Keep the executor wrapper aligned with the MCP hub budget.
    "resume_signature_gate": 660.0,
    "resume_payment_gate": 660.0,
    "cancel_signature_gate": 660.0,
    "cancel_payment_gate": 660.0,
}

# Cheap, in-process, side-effecting local tools whose mutation is
# sub-millisecond once it actually runs (arming a timer is an insert under a
# lock in the process-global TimerService: services/timer_core.py add_timer).
# The generic per-tool timeout races the WHOLE hub-call chain against a
# wall-clock ``asyncio.sleep`` deadline (see _run_tool_with_cancel). Under box
# overload the event loop is starved of turns, so that deadline can elapse
# before the coroutine ever reaches the synchronous mutation -- and the timeout
# branch then CANCELS the tool task, DROPPING the side effect entirely. That is
# the #2010/#2549 failure: the user said "set a timer" under load and got NO
# timer, because a trivially-cheap arm lost a wall-clock race it should never
# have been subject to. Cancel-on-timeout is correct for a hung *remote* call;
# for a cheap local mutation the right behavior under load is to let the
# already-submitted work finish. These tools therefore get a bounded grace to
# complete before the timeout cancels them, so a starved-but-not-broken loop
# still lands the mutation. A genuinely-impossible case still fails honestly
# once the grace is exhausted (the honesty half is #2543).
_LOCAL_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({"timer"})
_LOCAL_SIDE_EFFECT_TIMEOUT_GRACE_SECS: float = 5.0

_ONE_SHOT_IRREVERSIBLE_CONFIRMATION_CLASSES: frozenset[str] = frozenset(
    {
        "file_delete",
        "file_write",
        "calendar_delete",
        "calendar_write",
        "send_email",
        "send_message",
        "send_sms",
        "shell_command",
    }
)


def _calendar_add_failure_user_message(tool_name: str, tool_args: dict[str, Any], result: ToolResult) -> str | None:
    if tool_name != "calendar" or result.ok:
        return None
    if str(result.error_category or "").upper() == "CONFIRMATION_REQUIRED" or result.error == "confirmation_required":
        return None
    action = str(tool_args.get("action", "")).strip().lower()
    if action not in {"add", "create"}:
        return None
    error_text = str(result.error or result.data or "").lower()
    if "start_time" in error_text or "start time" in error_text or "parse" in error_text or "format" in error_text:
        return _CALENDAR_ADD_FAILURE_MESSAGE
    return "I couldn't add the event because %s" % (result.error or "the calendar tool failed.")


_SIGNATURE_GATE_REPLY_FOOTER = "Reply yes to sign and continue, or tell me what to change"
_SIGNATURE_GATE_UNKNOWN_AGENCY = "the filing agency"
_SIGNATURE_GATE_UNKNOWN_DOCUMENT = "the signature document"
_SIGNATURE_GATE_UNKNOWN_SIGNER = "the name on the form"
_SIGNATURE_GATE_MAX_CHARS = 300


def _plain_signature_fragment(value: Any, *, max_chars: int = 120) -> str:
    """Normalize signature-gate fragments for voice-safe plain text."""
    if value is None:
        return ""
    text = repair_mojibake(str(value))
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-:;,.\"'")
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0].strip(" \t\r\n-:;,.\"'")
    return clipped or text[:max_chars].strip(" \t\r\n-:;,.\"'")


def _paraphrase_signature_certification(certification: str) -> str:
    cert = _plain_signature_fragment(certification, max_chars=130)
    lower = cert.lower()
    if "penalty of perjury" in lower and ("true" in lower or "correct" in lower):
        return "You certify the information is true and correct under penalty of perjury."
    if "true" in lower and "correct" in lower:
        return "You certify the information is true and correct."
    if "legal signature" in lower:
        return "Checking the box applies a legal signature."
    if "attest" in lower:
        return "You attest that the filing information is accurate."
    if cert:
        return "You certify: %s." % cert.rstrip(".")
    return "You certify the filing information is accurate."


def _build_signature_gate_message(
    *,
    authority: str = "",
    document: str = "",
    signer: str = "",
    certification: str = "",
    key_fields: str = "",
) -> str:
    authority = _plain_signature_fragment(authority, max_chars=65) or _SIGNATURE_GATE_UNKNOWN_AGENCY
    document = _plain_signature_fragment(document, max_chars=80) or _SIGNATURE_GATE_UNKNOWN_DOCUMENT
    signer = _plain_signature_fragment(signer, max_chars=45) or _SIGNATURE_GATE_UNKNOWN_SIGNER
    key_fields = _plain_signature_fragment(key_fields, max_chars=70)
    cert_sentence = _paraphrase_signature_certification(certification)

    body = "%s; agency %s; signer %s. %s" % (document, authority, signer, cert_sentence)
    body_budget = _SIGNATURE_GATE_MAX_CHARS - len(_SIGNATURE_GATE_PREFIX) - 3 - len(_SIGNATURE_GATE_REPLY_FOOTER)
    if key_fields:
        with_fields = "%s Key fields: %s." % (body, key_fields)
        if len(with_fields) <= body_budget:
            body = with_fields

    body = _plain_signature_fragment(body, max_chars=body_budget)
    if body and body[-1] not in ".!?":
        body = "%s." % body
    return "%s %s %s" % (_SIGNATURE_GATE_PREFIX, body, _SIGNATURE_GATE_REPLY_FOOTER)


def _is_payment_review_request(tool_name: str, tool_args: dict[str, Any]) -> bool:
    """Return True when the explicit payment-handoff tool path is requested."""
    if tool_name != "payment":
        return False
    action = str(tool_args.get("action") or "").strip().lower()
    return action in _PAYMENT_GATE_REQUEST_ACTIONS


def _is_signature_review_request(tool_name: str, tool_args: dict[str, Any]) -> bool:
    """Return True when the explicit signature-handoff tool path is requested."""
    if tool_name != "signature":
        return False
    action = str(tool_args.get("action") or "").strip().lower()
    return action in _SIGNATURE_GATE_REQUEST_ACTIONS


def _payment_gate_message_from_request(tool_args: dict[str, Any]) -> str:
    """Build a PAYMENT_GATE answer from the explicit payment review tool call."""
    merchant = str(tool_args.get("merchant") or "").strip()
    total = str(tool_args.get("total") or "").strip()
    summary = str(
        tool_args.get("order_summary")
        or tool_args.get("summary")
        or tool_args.get("description")
        or tool_args.get("notes")
        or ""
    ).strip()

    details: list[str] = []
    if merchant:
        details.append("Merchant: %s." % merchant)
    if total:
        details.append("Total: %s." % total)
    if summary:
        details.append(summary)
    if not details:
        details.append("Checkout is ready for secure confirmation and payment review.")
    return "PAYMENT_GATE: %s" % " ".join(details)


def _payment_review_url_from_request(tool_args: dict[str, Any] | None) -> str:
    """Extract a structured payment page URL from an explicit payment review call."""

    if not isinstance(tool_args, dict):
        return ""
    for key in ("page_url", "payment_url", "checkout_url", "merchant_url", "url"):
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_generic_payment_order_text(text: str | None) -> bool:
    """Return True when the text is a handoff placeholder, not order detail."""
    normalized = str(text or "").strip().lower()
    if not normalized:
        return True
    normalized = normalized.removeprefix(_PAYMENT_GATE_PREFIX.lower()).strip()
    return any(marker in normalized for marker in _GENERIC_PAYMENT_DESCRIPTIONS)


def _delivery_address_from_value(value: Any) -> dict[str, str] | str | None:
    """Normalize a delivery address supplied by the agent or saved settings."""

    if isinstance(value, dict):
        address = {
            "street": str(value.get("street") or "").strip(),
            "city": str(value.get("city") or "").strip(),
            "state": str(value.get("state") or "").strip().upper(),
            "zip": str(value.get("zip") or "").strip(),
        }
        apt = str(value.get("apt") or value.get("unit") or "").strip()
        if apt:
            address["apt"] = apt
        if any(address.values()):
            return address
        full_text = str(value.get("full_text") or value.get("address") or "").strip()
        if full_text:
            return full_text
        return None

    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _delivery_address_from_request(
    request: dict[str, Any],
) -> dict[str, str] | str | None:
    """Extract an explicitly structured address from payment(request_review)."""

    for key in ("delivery_address", "address", "deliveryAddress"):
        address = _delivery_address_from_value(request.get(key))
        if address:
            return address

    address_fields = {
        "street": request.get("street") or request.get("delivery_street"),
        "city": request.get("city") or request.get("delivery_city"),
        "state": request.get("state") or request.get("delivery_state"),
        "zip": request.get("zip") or request.get("zipcode") or request.get("delivery_zip"),
    }
    return _delivery_address_from_value(address_fields)


def _payment_request_items(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structured order items supplied by the payment tool call."""

    source_items = request.get("items") or request.get("line_items") or request.get("order_items")
    if isinstance(source_items, dict):
        source_items = [source_items]
    if not isinstance(source_items, list):
        return []

    items: list[dict[str, Any]] = []
    from services.payments.purchase_ceiling import parse_money_cents

    for raw_item in source_items[:8]:
        if isinstance(raw_item, str):
            name = raw_item.strip()
            if not name:
                continue
            clean_item: dict[str, Any] = {"name": name[:140], "description": name[:140]}
        elif isinstance(raw_item, dict):
            name = str(raw_item.get("name") or raw_item.get("description") or raw_item.get("title") or "").strip()
            if not name:
                continue
            clean_item = {"name": name[:140], "description": name[:140]}
            price = str(raw_item.get("price") or raw_item.get("amount") or "").strip()
            if price:
                clean_item["price"] = price[:32]
                price_cents = parse_money_cents(price)
                if price_cents is not None:
                    clean_item["price_cents"] = price_cents
            quantity = raw_item.get("quantity") or raw_item.get("qty")
            if isinstance(quantity, int | float) and quantity > 0:
                clean_item["quantity"] = int(quantity)
        else:
            continue
        items.append(clean_item)
    return items


def _signature_gate_message_from_request(tool_args: dict[str, Any]) -> str:
    """Build a SIGNATURE_GATE answer from the explicit signature review tool call."""
    authority = str(tool_args.get("authority") or tool_args.get("agency") or "").strip()
    document = str(tool_args.get("document") or tool_args.get("document_name") or "").strip()
    signer = str(tool_args.get("signer") or tool_args.get("signer_name") or "").strip()
    summary = str(
        tool_args.get("summary")
        or tool_args.get("key_fields")
        or tool_args.get("fields")
        or tool_args.get("notes")
        or ""
    ).strip()
    certification = str(
        tool_args.get("certification") or tool_args.get("certification_language") or tool_args.get("attestation") or ""
    ).strip()

    return _build_signature_gate_message(
        authority=authority,
        document=document,
        signer=signer,
        certification=certification,
        key_fields=summary,
    )


def _extract_ask_user_text(tool_args: dict[str, Any]) -> str | None:
    """Extract a consultative question from ask_user-style arguments."""
    for key in ("question", "message", "text"):
        value = tool_args.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None


def _set_final_response(
    executor: Any,
    answer: str | None,
    *,
    continue_listening: bool | None = None,
    command: str | None = None,
    params: dict[str, Any] | None = None,
    display_answer: str | None = None,
) -> None:
    """Store raw trace text and the user-display terminal response."""
    executor._raw_final_answer = answer
    executor._final_answer = display_answer if display_answer is not None else answer
    executor._final_continue_listening = continue_listening
    executor._final_command = command
    executor._final_params = params if params is not None else {}
    executor._parse_failed = False


def _is_approval_block_error(error_category: str | None = None) -> bool:
    """Return True when a tool result is an approval-policy denial."""
    return str(error_category or "").upper() == "APPROVAL_BLOCKED"


def _confirmation_deferred_tool_result(deferred: ConfirmationDeferred) -> ToolResult:
    return ToolResult(
        ok=False,
        error="confirmation_required",
        data=deferred.to_envelope(),
        error_category="CONFIRMATION_REQUIRED",
        retryable=True,
    )


def _remember_approval_blocked_tool(executor: Any, tool_name: str) -> None:
    """Track approval-blocked tools so final UX cannot hide the policy failure."""
    if not tool_name:
        return
    blocked = getattr(executor, "_approval_blocked_tools", None)
    if blocked is None:
        blocked = []
        executor._approval_blocked_tools = blocked
    if tool_name not in blocked:
        blocked.append(tool_name)


def _approval_blocked_user_answer(
    tool_names: list[str] | tuple[str, ...] | set[str],
) -> str:
    """Build a deterministic user-visible response for approval denials."""
    names = [name for name in dict.fromkeys(str(t) for t in tool_names if t)]
    if not names:
        names = ["the requested tool"]
    tool_label = ", ".join(names)
    return (
        "APPROVAL_BLOCKED: The approval system blocked %s. "
        "I can try a different route, or you can confirm the action again with the exact destination/content."
        % tool_label
    )


def _apply_approval_blocked_final_answer(executor: Any, final_answer: str | None) -> str | None:
    """Replace empty final answers after approval blocks."""
    blocked = list(getattr(executor, "_approval_blocked_tools", []) or [])
    if not blocked:
        return final_answer
    if str(final_answer or "").strip():
        return final_answer
    answer = _approval_blocked_user_answer(blocked)
    _set_final_response(executor, answer, continue_listening=True)
    return answer


def _final_response_overrides_llm_error(
    *,
    final_answer: str | None,
    final_command: str | None = None,
    last_response: dict[str, Any] | None = None,
    payment_gate: bool = False,
    signature_gate: bool = False,
    continue_listening: bool | None = None,
) -> bool:
    """Return True when a terminal final response should beat earlier no-result state."""

    answer_text = str(final_answer or "").strip()
    command_text = str(final_command or "").strip()
    if payment_gate or signature_gate:
        return True
    if command_text:
        return True
    response_type = ""
    if isinstance(last_response, dict):
        response_type = str(last_response.get("type") or "").strip().lower()
    if response_type in {
        "answer",
        "final_answer",
        "ask_user",
        "clarification",
    }:
        return bool(answer_text)
    return False


_COMMERCE_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        "browser_click",
        "browser_fill_form",
        "browser_interact",
        "browser_select",
        "browser_type",
        "fill_payment_details",
    }
)
_STRUCTURED_INCOMPLETE_STATUSES: frozenset[str] = frozenset(
    {
        "blocked",
        "failed",
        "incomplete",
        "no_effect",
        "not_completed",
    }
)


def _record_structured_incomplete_diagnostic(executor: Any, diagnostic: dict[str, Any]) -> None:
    params = getattr(executor, "_final_params", None)
    if not isinstance(params, dict):
        return
    safe_diagnostic = {
        str(key): redact_card_data(value) for key, value in diagnostic.items() if isinstance(key, str) and key
    }
    safe_diagnostic.setdefault("evidence_source", "structured_runtime_state")
    params["outcome_diagnostic"] = safe_diagnostic


def _completion_evidence_from_tool_data(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    for key in ("completion_evidence", "task_completion_evidence"):
        evidence = value.get(key)
        if isinstance(evidence, dict):
            return copy.deepcopy(evidence)

    if value.get("last_click_no_effect") is True:
        return {
            "domain": "browser_action",
            "completed": False,
            "reason": "last_click_no_effect",
            "source": "browser_no_effect",
        }
    if value.get("snapshot_unchanged_after_click") is True and value.get("url_unchanged_after_click") is True:
        return {
            "domain": "browser_action",
            "completed": False,
            "reason": "snapshot_and_url_unchanged_after_click",
            "source": "browser_no_effect",
        }

    commerce_state = value.get("commerce_state")
    if isinstance(commerce_state, dict):
        cart_count_raw = commerce_state.get("cart_item_count")
        try:
            cart_count = int(cart_count_raw)
        except (TypeError, ValueError):
            cart_count = None
        if cart_count == 0 and commerce_state.get("cart_state_observed") is True:
            return {
                "domain": "commerce_order",
                "completed": False,
                "reason": "cart_item_count_zero",
                "source": "commerce_state",
            }
    return None


def _completion_evidence_is_incomplete(evidence: dict[str, Any]) -> bool:
    completed = evidence.get("completed")
    if completed is False:
        return True
    status = str(evidence.get("status") or "").strip().lower()
    return status in _STRUCTURED_INCOMPLETE_STATUSES


def _task_log_has_commerce_side_effect_attempt(task_log: Any) -> bool:
    steps = getattr(task_log, "steps", [])
    if not isinstance(steps, list):
        return False
    for step in steps:
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if tool_name in _COMMERCE_SIDE_EFFECT_TOOLS:
            return True
    return False


def _terminal_structured_incomplete_diagnostic(executor: Any, task_log: Any) -> dict[str, Any] | None:
    """Return terminal incomplete evidence from typed runtime/tool state only."""

    if bool(getattr(executor, "_payment_gate_requested", False)) or bool(
        getattr(executor, "_signature_gate_requested", False)
    ):
        return None
    if not _task_log_has_commerce_side_effect_attempt(task_log):
        return None
    evidence = _completion_evidence_from_tool_data(getattr(executor, "_last_tool_data", None))
    tool_name = str(getattr(executor, "_last_tool_name", "") or "").strip()
    if evidence:
        if not _completion_evidence_is_incomplete(evidence):
            return None
    else:
        stored = getattr(executor, "_last_incomplete_completion_evidence", None)
        if isinstance(stored, dict) and isinstance(stored.get("evidence"), dict):
            stored_evidence = copy.deepcopy(stored["evidence"])
            if _completion_evidence_is_incomplete(stored_evidence):
                evidence = stored_evidence
                tool_name = str(stored.get("tool_name") or tool_name).strip()
        if not evidence:
            latest = getattr(executor, "_last_completion_evidence", None)
            latest_evidence = latest.get("evidence") if isinstance(latest, dict) else None
            if isinstance(latest_evidence, dict) and not _completion_evidence_is_incomplete(latest_evidence):
                return None
            page_url = str(getattr(executor, "_last_page_url", "") or "").strip()
            normalized_page_url = page_url.lower()
            if not normalized_page_url or normalized_page_url.startswith("about:blank"):
                evidence = {
                    "domain": "commerce_order",
                    "completed": False,
                    "reason": "browser_state_reset_before_completion",
                    "source": "browser_terminal_state",
                    "page_url": page_url or None,
                }
                tool_name = "browser_terminal_state"
            else:
                return None
    return {
        "reason": str(evidence.get("reason") or evidence.get("status") or "structured_incomplete"),
        "domain": str(evidence.get("domain") or "unknown"),
        "evidence_source": str(evidence.get("source") or "structured_tool_result"),
        "evidence": evidence,
        "tool_name": tool_name,
    }


def _payment_gate_answer_with_confirm_url(answer: str | None, confirmation_url: str | None) -> str | None:
    """Append the raw hosted confirmation URL to PAYMENT_GATE answers."""
    if not answer:
        return answer
    url = (confirmation_url or "").strip()
    if not url or "/confirm/" not in url or url in answer:
        return answer
    return "%s\n%s" % (answer.rstrip(), url)


async def _dispatch_payment_confirmation_link_for_executor(
    executor: Any,
    payment_confirmation_ctx: dict[str, Any] | None,
    confirmation_url: str | None,
) -> dict[str, Any] | None:
    """Dispatch confirmation links for channels not covered by final card rendering."""
    if not payment_confirmation_ctx:
        return None
    origin_channel = executor._get_origin_channel_type()
    if origin_channel not in {
        None,
        "web",
        "desktop",
        "phone",
        "voice",
        "voice-stream",
        "sms",
        "email",
    }:
        return None
    try:
        from services.payments.confirmation_link_dispatch import (
            dispatch_confirmation_link,
        )

        delivery = await dispatch_confirmation_link(
            origin_channel=origin_channel,
            confirmation_url=confirmation_url,
            channel=payment_confirmation_ctx.get("channel") or getattr(executor, "_channel", None),
            user_id=executor._get_effective_user_id(),
            metadata={
                "session_id": getattr(executor, "_session_id", None),
                "task_id": getattr(executor, "task_id", None),
                "order_summary": payment_confirmation_ctx.get("order_summary"),
            },
        )
        payment_confirmation_ctx["link_dispatch"] = delivery
        return delivery
    except Exception:
        logger.exception("Payment confirmation link dispatch failed")
        return None


_CHECKOUT_URL_PATTERNS = (
    "checkout",
    "/pay/",
    "/pay?",
    "/payment/",
    "/payment?",
    "/payment.html",
    "/payments.html",
    "/checkout.html",
    "/billing.html",
    "/billing/",
    "/billing?",
    "/cart/checkout",
    "/order/checkout",
    "/order/review",
)


def _url_is_checkout(url: str | None) -> bool:
    """Return True if the URL looks like a checkout/payment page."""
    if not url:
        return False
    lower = url.lower()
    return any(sig in lower for sig in _CHECKOUT_URL_PATTERNS)


def _url_is_explicit_payment_review_page(url: str | None) -> bool:
    """Return True for a payment-looking URL after the model called the payment tool."""

    candidate = str(url or "").strip()
    if not candidate:
        return False
    try:
        from urllib.parse import urlparse

        parsed = urlparse(candidate)
    except Exception:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = str(parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost"} and "/confirm/" in parsed.path.lower():
        return False
    payment_surface = "%s?%s" % (parsed.path.lower(), parsed.query.lower())
    return any(marker in payment_surface for marker in ("checkout", "payment", "billing", "cart"))


_AGENT_TASK_LOG_DIR = get_logs_dir() / "agent_tasks"
_AGENT_TASK_LOG_FILE_MODE = 0o600


def _atomic_write_json_restricted(path: Path, payload: Any) -> None:
    """Write JSON via a private temp file in the target directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    fd: int | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            os.chmod(tmp_path, _AGENT_TASK_LOG_FILE_MODE)
        except OSError:
            logger.debug("Could not restrict temp task log permissions: %s", tmp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(payload, f, indent=2, default=str, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.rename(tmp_path, path)
        except FileExistsError:
            os.replace(tmp_path, path)
        tmp_path = None
        try:
            os.chmod(path, _AGENT_TASK_LOG_FILE_MODE)
        except OSError:
            logger.debug("Could not restrict task log permissions: %s", path)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


_TOOL_PROGRESS_PHRASES: dict[str, str] = {
    "file_read": "Reading files...",
    "file_write": "Writing files...",
    "run_command": "Running a command...",
    "web_search": "Searching the web...",
    "web_read": "Reading that page...",
    "system_info": "Checking your system...",
    "read_emails": "Checking your email...",
    "send_email": "Sending the email...",
    "gmail_inbox": "Checking your Gmail inbox...",
    "gmail_search": "Searching your emails...",
    "gmail_read": "Reading that email...",
    "gmail_draft_reply": "Drafting a reply...",
    "gmail_send": "Sending the email...",
    "gmail_daily_summary": "Getting your email summary...",
    "gmail": "Checking Gmail...",
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
    "google_workspace": "Checking Google Workspace...",
}

# Browser tool names for page_url tracking.
# Any tool whose result may contain a "url" field belongs here so that
# _track_page_url() can cache the current page URL for the overlay.
# Includes hidden tools in case they appear in legacy call paths.
_BROWSER_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_run_script",
        "browser_screenshot",
        # Compound tools (visible to LLM, return url in response):
        "browser_interact",
        "browser_fill_form",
        "browser_snapshot",
        # Hidden from LLM but still tracked if somehow called:
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

_BLOCK_ON_INTERRUPT_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "command",
        "edit_file",
        "exec",
        "execute_shell",
        "file_write",
        "fill_payment_details",
        "make_phone_call",
        "patch",
        "phone_call_initiate",
        "place_order",
        "powershell",
        "send_email",
        "send_message",
        "send_money",
        "send_sms",
        "shell",
        "submit_payment",
        "write_file",
    }
)
_BLOCK_ON_INTERRUPT_PREFIXES: tuple[str, ...] = ("browser_", "payment", "signature")

# Browser snapshots pass through unchanged; model-visible browser state must
# remain the browser tool's structured output, not runtime-written guidance.
_SNAP_REF_RE = re.compile(r"@e\d+")


def _compress_browser_result(tool_name: str, result_text: str) -> str:
    """Preserve browser tool output exactly as returned."""
    return result_text


def _strip_image_from_result(result_text: str) -> str:
    """Remove image_base64/mime_type from JSON result text to avoid duplication."""

    def _strip_json_image_payload(text: str) -> str | None:
        obj = json.loads(text)
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict) and "image_base64" in data:
            data.pop("image_base64", None)
            data.pop("mime_type", None)
            return json.dumps(obj, ensure_ascii=False)
        return None

    try:
        stripped = _strip_json_image_payload(result_text)
        if stripped is not None:
            return stripped
    except (json.JSONDecodeError, ValueError) as exc:
        # Post R5-P0-G the sanitizer no longer wraps content in
        # [WEB_CONTENT_START]/[WEB_CONTENT_END] delimiters, so the prior
        # wrapper-aware fallback path is no longer reachable.
        _sample = str(locals().get("raw") or locals().get("content") or locals().get("response") or result_text or "")[
            :100
        ]
        logger.warning("JSON parse failed: %s (sample=%r)", exc, _sample)
    return result_text


# ---------------------------------------------------------------------------
# Browser ref invalidation helpers
# ---------------------------------------------------------------------------

# Matches @eN references (e.g. @e0, @e127)
_REF_PATTERN = re.compile(r"@e\d+")


# NOTE: The legacy ``_browser_nav_detected(tool_result_str)`` substring-scan
# helper was deleted as part of R5-P0-O. Navigation detection now reads the
# structured ``refs_invalidated`` flag set by the producer (browser tools) and
# is consumed by ``intent.agent_loop._browser_nav_detected_from_tool_results``.
# Do not reintroduce a substring scan of tool-result payloads to drive runtime
# state mutations — the ratchet
# ``r5_p0_o_no_tool_result_substring_scan_for_state`` will block it.


def _strip_old_browser_refs(native_messages: list[dict[str, Any]]) -> None:
    """Remove @eN references from all messages EXCEPT the most recent user message.

    When the browser navigates to a new page, old @eN refs point to elements
    on the *previous* page.  Keeping them in history causes the model to reuse
    stale ref numbers.  This function scrubs them from older messages so the
    model can only see refs from the latest snapshot.

    Mutates *native_messages* in-place.
    """
    if len(native_messages) < 2:
        return

    # Process every message except the last one (which contains the fresh snapshot)
    for msg in native_messages[:-1]:
        content = msg.get("content")
        if content is None:
            continue

        # String content (text-path messages)
        if isinstance(content, str):
            if _REF_PATTERN.search(content):
                msg["content"] = _REF_PATTERN.sub("", content)
            continue

        # List content (native-path messages with content blocks)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    for key in ("text", "content"):
                        val = block.get(key)
                        if isinstance(val, str) and _REF_PATTERN.search(val):
                            block[key] = _REF_PATTERN.sub("", val)
            continue

        # Dict content (OpenAI-format assistant messages)
        if isinstance(content, dict):
            for key in ("content", "text"):
                val = content.get(key)
                if isinstance(val, str) and _REF_PATTERN.search(val):
                    content[key] = _REF_PATTERN.sub("", val)


# ---------------------------------------------------------------------------
# Structured step logging
# ---------------------------------------------------------------------------


def _empty_task_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_write_tokens": 0,
        "web_search_requests": 0,
        "total_tokens": 0,
    }


def _coerce_nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _normalize_task_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    normalized = _empty_task_usage()
    if isinstance(usage, dict):
        normalized["input_tokens"] = _coerce_nonnegative_int(usage.get("input_tokens", usage.get("prompt_tokens")))
        normalized["output_tokens"] = _coerce_nonnegative_int(
            usage.get("output_tokens", usage.get("completion_tokens"))
        )
        normalized["cache_read_tokens"] = _coerce_nonnegative_int(
            usage.get("cache_read_tokens", usage.get("cached_tokens"))
        )
        normalized["cache_creation_tokens"] = _coerce_nonnegative_int(
            usage.get("cache_creation_tokens", usage.get("cache_write_tokens"))
        )
        normalized["cache_write_tokens"] = _coerce_nonnegative_int(
            usage.get("cache_write_tokens", normalized["cache_creation_tokens"])
        )
        normalized["web_search_requests"] = _coerce_nonnegative_int(usage.get("web_search_requests"))
        normalized["total_tokens"] = _coerce_nonnegative_int(usage.get("total_tokens"))
    if normalized["total_tokens"] <= 0:
        normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    return normalized


def _normalize_model_totals(
    model_totals: dict[str, Any] | None,
) -> dict[str, dict[str, int | float]]:
    normalized: dict[str, dict[str, int | float]] = {}
    if not isinstance(model_totals, dict):
        return normalized
    for model_name, row in model_totals.items():
        if not isinstance(row, dict):
            continue
        name = str(model_name or "").strip()
        if not name:
            continue
        normalized[name] = {
            "input_tokens": _coerce_nonnegative_int(row.get("input_tokens", row.get("inputTokens"))),
            "output_tokens": _coerce_nonnegative_int(row.get("output_tokens", row.get("outputTokens"))),
            "cache_read_input_tokens": _coerce_nonnegative_int(
                row.get("cache_read_input_tokens", row.get("cacheReadInputTokens"))
            ),
            "cache_creation_input_tokens": _coerce_nonnegative_int(
                row.get("cache_creation_input_tokens", row.get("cacheCreationInputTokens"))
            ),
            "web_search_requests": _coerce_nonnegative_int(
                row.get("web_search_requests", row.get("webSearchRequests"))
            ),
            "cost_usd": float(row.get("cost_usd", row.get("costUSD", 0.0)) or 0.0),
        }
    return normalized


def _task_usage_from_model_totals(
    model_totals: dict[str, dict[str, int | float]],
) -> dict[str, int]:
    usage = _empty_task_usage()
    for row in model_totals.values():
        usage["input_tokens"] += _coerce_nonnegative_int(row.get("input_tokens"))
        usage["output_tokens"] += _coerce_nonnegative_int(row.get("output_tokens"))
        usage["cache_read_tokens"] += _coerce_nonnegative_int(row.get("cache_read_input_tokens"))
        usage["cache_creation_tokens"] += _coerce_nonnegative_int(row.get("cache_creation_input_tokens"))
        usage["web_search_requests"] += _coerce_nonnegative_int(row.get("web_search_requests"))
    usage["cache_write_tokens"] = usage["cache_creation_tokens"]
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


@dataclass
class AgentStepRecord:
    """One step of an agent task execution."""

    task_id: str
    step: int
    timestamp: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_result_summary: str
    llm_decision: str  # "tool_call" or "final_answer"
    llm_reasoning: str  # any text the LLM emitted before the tool call
    page_url: str | None
    duration_ms: int
    error: str | None
    tool_use_id: str | None = None

    def __post_init__(self) -> None:
        self.tool_input = redact_card_data(self.tool_input)
        self.tool_result_summary = redact_card_data(self.tool_result_summary)
        self.tool_use_id = str(self.tool_use_id or "").strip() or None


@dataclass
class AgentTaskLog:
    """Accumulates step records for a single agent task run."""

    task_id: str
    user_text: str
    started_at: str
    steps: list[AgentStepRecord] = field(default_factory=list)
    outcome: str = ""  # success, timeout, error, payment_gate
    total_steps: int = 0
    total_duration_s: float = 0.0
    total_duration_ms: int = 0
    usage: dict[str, int] = field(default_factory=_empty_task_usage)
    total_cost_usd: float = 0.0
    model_totals: dict[str, dict[str, int | float]] = field(default_factory=dict)
    final_answer: str = ""
    completed_at: str = ""

    def add_step(self, record: AgentStepRecord) -> None:
        self.steps.append(record)
        self.total_steps = len(self.steps)

    def finalize(
        self,
        outcome: str,
        total_duration_s: float,
        *,
        usage: dict[str, Any] | None = None,
        total_cost_usd: float | None = None,
        model_totals: dict[str, Any] | None = None,
    ) -> None:
        self.outcome = outcome
        self.total_duration_s = round(total_duration_s, 2)
        self.total_duration_ms = int(max(0.0, total_duration_s) * 1000)
        self.model_totals = _normalize_model_totals(model_totals)
        self.usage = (
            _normalize_task_usage(usage) if usage is not None else _task_usage_from_model_totals(self.model_totals)
        )
        self.total_cost_usd = float(total_cost_usd or 0.0)
        self.total_steps = len(self.steps)
        self.completed_at = datetime.now(tz=UTC).isoformat()

    def write_to_disk(self) -> None:
        """Persist the task log as JSON to logs/agent_tasks/<task_id>.json."""
        try:
            path = _AGENT_TASK_LOG_DIR / ("%s.json" % self.task_id)
            _atomic_write_json_restricted(path, redact_card_data(asdict(self)))
            logger.debug("Agent task log written to %s", path)
        except Exception as exc:
            logger.warning("Failed to write agent task log: %s", exc)


@dataclass(frozen=True, slots=True)
class _AgentPlanLimitPolicy:
    plan_id: str
    plan_family: str


def _summarize(text: str, limit: int = 2000) -> str:
    """Truncate text to *limit* characters for log summaries."""
    s = str(text) if text is not None else ""
    return s[:limit] + ("..." if len(s) > limit else "")


def _summarize_args(args: dict[str, Any], limit: int = 80) -> str:
    """Compact representation of tool args for the one-line log."""
    try:
        raw = json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raw = str(args)
        _sample = str(locals().get("raw") or locals().get("content") or locals().get("response") or args or "")[:100]
        logger.warning("JSON parse failed: %s (sample=%r)", exc, _sample)
    return raw[:limit] + ("..." if len(raw) > limit else "")


# SEC-10: PII patterns redacted from reasoning text before persistence.
_PII_EMAIL_RE = re.compile(r"\b[\w\.+-]+@[\w\-]+\.[\w\.-]+\b")
_PII_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[\s\-]?)?(?:\(?\d{3}\)?[\s\-]?)?\d{3}[\s\-]?\d{4}\b")
_PII_CARD_RE = re.compile(r"\b(?:\d[ \-]?){13,19}\b")
_PII_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PII_STREET_RE = re.compile(
    r"\b\d+\s+[A-Za-z][A-Za-z0-9\s\.]*(?:\s+(?:St|Street|Ave|Avenue|Blvd|Rd|Road|Dr|Drive|Ln|Lane|Way|Ct|Court))\b",
    re.IGNORECASE,
)
_REASONING_PROD_CHAR_LIMIT = 200


def _redact_reasoning_for_logs(text: str | None) -> str:
    """Return reasoning text safe to persist in prod logs.

    SEC-10: the LLM's chain-of-thought can include PII it recalled from
    system context (addresses, emails, phone numbers). Step logs are
    persisted to disk and uploaded to the agent-xray backend; in prod we
    redact PII shapes and cap length so only a coarse preview survives.
    The coarse ``reasoning_source`` category is kept elsewhere intact.
    """
    if not text:
        return ""
    redacted = text
    redacted = _PII_EMAIL_RE.sub("[EMAIL]", redacted)
    redacted = _PII_CARD_RE.sub("[CARD]", redacted)
    redacted = _PII_SSN_RE.sub("[SSN]", redacted)
    redacted = _PII_PHONE_RE.sub("[PHONE]", redacted)
    redacted = _PII_STREET_RE.sub("[ADDRESS]", redacted)
    try:
        from config.settings import settings as _settings

        env = str(getattr(_settings, "env", "prod")).strip().lower()
    except Exception:
        env = "prod"
    if env == "prod" and len(redacted) > _REASONING_PROD_CHAR_LIMIT:
        redacted = redacted[:_REASONING_PROD_CHAR_LIMIT] + "...[truncated]"
    return redacted


def _log_full_prompts_enabled() -> bool:
    """Return True when structured step logs may include full prompt/context bytes."""
    return _env_get_bool("VIOLA_LOG_FULL_PROMPTS", False) or _env_get_bool("VIOLA_LOG_SYSTEM_CONTEXT", False)


def _redact_prompt_for_structured_log(value: Any) -> Any:
    """Keep opt-in prompt logging useful while masking low-ambiguity sensitive data."""
    return redact_pii(redact_card_data(value))


_EMAIL_TRACE_TOOL_NAMES = frozenset(
    {
        "email_send",
        "send_email",
        "share_response",
        "gmail",
        "gmail_search",
        "gmail_get",
        "gmail_send",
        "gmail_createDraft",
        "gmail_sendDraft",
        "gmail_modify",
        "gmail_batchModify",
        "gmail_modifyThread",
        "gmail_downloadAttachment",
        "gmail_listLabels",
        "gmail_createLabel",
    }
)
_EMAIL_TRACE_REDACT_KEYS = frozenset(
    {
        "body",
        "bcc",
        "cc",
        "content",
        "destination",
        "from",
        "html",
        "query",
        "raw",
        "recipient",
        "recipients",
        "snippet",
        "subject",
        "text",
        "to",
    }
)
_EMAIL_TRACE_REDACTED = "[email field redacted from task trace]"


def _is_email_trace_tool(tool_name: str, tool_input: dict[str, Any] | None = None) -> bool:
    name = canonicalize_tool_name_for_safety(tool_name)
    if name == "share_response":
        kind = str((tool_input or {}).get("destination_kind") or "").strip().lower()
        return kind in {"email", "mail", "gmail"}
    return name in _EMAIL_TRACE_TOOL_NAMES or name.startswith("gmail_")


def _redact_email_trace_value(value: Any, *, redact_plain_strings: bool = False) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, child in value.items():
            if str(key).strip().lower() in _EMAIL_TRACE_REDACT_KEYS:
                redacted[key] = _EMAIL_TRACE_REDACTED
            else:
                redacted[key] = _redact_email_trace_value(child)
        return redacted
    if isinstance(value, list):
        return [_redact_email_trace_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_email_trace_value(item) for item in value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped[0] in "[{":
            try:
                return json.dumps(
                    _redact_email_trace_value(json.loads(stripped)),
                    ensure_ascii=False,
                    default=str,
                )
            except (TypeError, ValueError):
                return _EMAIL_TRACE_REDACTED
        if redact_plain_strings and stripped:
            return _EMAIL_TRACE_REDACTED
    return value


def _redact_email_trace_payload(tool_name: str, payload: Any, tool_input: dict[str, Any] | None = None) -> Any:
    if not _is_email_trace_tool(tool_name, tool_input):
        return payload
    if isinstance(payload, str):
        return _redact_email_trace_value(payload, redact_plain_strings=True)
    return _redact_email_trace_value(payload)


def _build_reasoning_source_payload(
    reasoning: str | None,
) -> str | dict[str, Any] | None:
    """Return the persisted reasoning-source payload for step logs."""
    if not reasoning:
        return None
    try:
        from config.settings import settings as _settings_int21

        if hasattr(_settings_int21, "debug_reasoning"):
            _full = bool(_settings_int21.debug_reasoning)
        else:
            import os as _os_int21

            _full = bool(_os_int21.environ.get("VIOLA_DEBUG_REASONING"))
    except Exception:
        import os as _os_int21

        _full = bool(_os_int21.environ.get("VIOLA_DEBUG_REASONING"))
    if _full:
        return reasoning
    return {
        "category": "provider_reasoning",
        "length": len(reasoning),
    }


def _extract_reasoning_legacy(response: dict[str, Any]) -> str:
    """Extract LLM chain-of-thought text from a tool_call response.

    For Anthropic native: ``_raw_content`` is a list of content blocks â€”
    text blocks before tool_use blocks contain the reasoning.

    For OpenAI native: ``_raw_content`` is a dict with a ``content`` key
    that holds any text the model emitted alongside tool calls.

    Returns the extracted text, or ``""`` if none found.
    """
    explicit_reasoning = response.get("_reasoning")
    if isinstance(explicit_reasoning, str) and explicit_reasoning.strip():
        return explicit_reasoning.strip()

    raw = response.get("_raw_content")
    if raw is None:
        return ""

    # Anthropic native path: list of content block objects
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            # Anthropic SDK content blocks have a `.type` attribute
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
                if text:
                    parts.append(str(text))
            elif block_type == "tool_use":
                break  # reasoning appears before tool_use blocks
        return "\n".join(parts).strip()

    # OpenAI native path: dict with ``content`` key
    if isinstance(raw, dict):
        content = raw.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    return ""


def _text_parts_from_reasoning_value(value: Any) -> list[str]:
    parts: list[str] = []
    if value is None:
        return parts
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            parts.append(stripped)
        return parts
    if isinstance(value, dict):
        for key in ("text", "thinking", "content", "summary", "reasoning"):
            child = value.get(key)
            if child is not None:
                parts.extend(_text_parts_from_reasoning_value(child))
        return parts
    if isinstance(value, (list, tuple)):
        for item in value:
            parts.extend(_text_parts_from_reasoning_value(item))
        return parts
    text = getattr(value, "text", None)
    if isinstance(text, str) and text.strip():
        parts.append(text.strip())
    thinking = getattr(value, "thinking", None)
    if isinstance(thinking, str) and thinking.strip():
        parts.append(thinking.strip())
    return parts


def _extract_reasoning(response: dict[str, Any]) -> str:
    """Extract readable provider reasoning or thinking text from a response."""
    public_parts = _text_parts_from_reasoning_value(response.get("reasoning"))
    if public_parts:
        return "\n".join(public_parts).strip()

    explicit_reasoning = response.get("_reasoning")
    if isinstance(explicit_reasoning, str) and explicit_reasoning.strip():
        return explicit_reasoning.strip()

    thinking_parts = _text_parts_from_reasoning_value(response.get("_thinking_blocks"))
    if thinking_parts:
        return "\n".join(thinking_parts).strip()

    legacy_reasoning = _extract_reasoning_legacy(response)
    if legacy_reasoning:
        return legacy_reasoning

    raw = response.get("_raw_content")
    if raw is None:
        return ""

    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
                if text:
                    parts.append(str(text))
            elif block_type == "tool_use":
                break
        return "\n".join(parts).strip()

    if isinstance(raw, dict):
        content = raw.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    return ""


def _thinking_blocks_for_trace(value: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return blocks
    for block in value:
        if isinstance(block, dict):
            block_type = str(block.get("type") or "")
            if block_type == "thinking":
                text = str(block.get("thinking") or block.get("text") or "").strip()
                if text:
                    blocks.append({"type": "thinking", "text": text})
            elif block_type == "redacted_thinking":
                blocks.append({"type": "redacted_thinking", "redacted": True})
            continue
        block_type = str(getattr(block, "type", "") or "")
        if block_type == "thinking":
            text = str(getattr(block, "thinking", "") or getattr(block, "text", "") or "").strip()
            if text:
                blocks.append({"type": "thinking", "text": text})
        elif block_type == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "redacted": True})
    return blocks


def _normalize_reasoning_for_task_trace(
    response: dict[str, Any],
) -> dict[str, Any] | None:
    existing = response.get("reasoning")
    if isinstance(existing, dict) and existing.get("text"):
        payload = _trace_safe_copy(existing)
        payload.setdefault("kind", "provider_reasoning")
        payload.setdefault("format", "text")
        payload.setdefault("source", "provider_reasoning")
        return payload

    text = _extract_reasoning(response)
    blocks = _thinking_blocks_for_trace(response.get("_thinking_blocks"))
    if not text and not blocks:
        return None

    source = "provider_reasoning"
    if response.get("_reasoning"):
        source = "openai_reasoning_summary"
    elif blocks:
        source = "provider_thinking_blocks"
    elif response.get("_raw_content") is not None:
        source = "provider_raw_content"

    payload: dict[str, Any] = {
        "kind": "provider_reasoning",
        "format": "blocks" if blocks else "text",
        "source": source,
    }
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
    return payload


def _is_native_tool_calling_unsupported(exc: RuntimeError) -> bool:
    return "does not support native tool calling" in str(exc)


def _is_invalid_encrypted_content_error(exc: BaseException) -> bool:
    """Return True for OpenAI Responses invalid_encrypted_content failures."""
    values: list[str] = [str(exc)]
    for attr in ("code", "message", "body", "response"):
        value = getattr(exc, attr, None)
        if value is not None:
            values.append(str(value))
    return any("invalid_encrypted_content" in value.lower() for value in values)


def _drop_encrypted_reasoning_items(items: list[Any]) -> tuple[list[Any], int]:
    """Remove stale encrypted reasoning replay items from Responses continuity."""
    repaired: list[Any] = []
    dropped = 0
    for item in items:
        if isinstance(item, dict) and item.get("type") == "reasoning" and item.get("encrypted_content"):
            dropped += 1
            continue
        repaired.append(copy.deepcopy(item))
    return repaired, dropped


# Maximum number of diagnostic context entries retained in memory.
# Oldest entries are evicted FIFO when the cap is exceeded.
_MAX_DIAGNOSTIC_CONTEXTS = 50


def _normalize_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Convert a list of Anthropic SDK content block objects to plain dicts.

    The Anthropic API returns content as SDK Pydantic objects (ToolUseBlock,
    TextBlock).  Storing these directly in ``_native_messages`` alongside
    plain-dict tool_result blocks creates a mixed-format list that can
    confuse the SDK serializer when sent back to the API, producing
    'tool_use ids found without tool_result blocks' 400 errors (M7 Bug B).

    This function normalises everything to plain dicts at insertion time.
    """
    if not isinstance(content, list):
        return content  # type: ignore[return-value] # AGENT-01: native provider content accepts legacy shapes.

    normalised: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict):
            normalised.append(block)
        elif hasattr(block, "model_dump"):
            # Pydantic v2 SDK objects
            normalised.append(block.model_dump())
        elif hasattr(block, "to_dict"):
            normalised.append(block.to_dict())
        else:
            # Manual conversion for SDK objects with known attributes
            block_type = getattr(block, "type", None)
            if block_type == "tool_use":
                normalised.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}),
                    }
                )
            elif block_type == "text":
                normalised.append(
                    {
                        "type": "text",
                        "text": getattr(block, "text", ""),
                    }
                )
            else:
                normalised.append({"type": "text", "text": str(block)})
    return normalised


def _sanitize_checkpoint_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Repair orphaned tool calls in restored checkpoint messages.

    When a checkpoint is saved mid-execution, the last assistant message
    may contain Anthropic ``tool_use`` blocks or OpenAI ``function_call``
    items without a matching tool result. Replaying these to the LLM API
    causes ``BadRequestError``. This function:

    1. Collects all tool call IDs that have a matching result.
    2. Preserves assistant messages with unmatched tool_use blocks.
    3. Inserts synthetic error results for unmatched Anthropic/OpenAI
       tool calls so provider continuity can pair them.
    """
    if not messages:
        return []

    # Collect all tool_result/function_call_output IDs present in the conversation.
    result_ids: set[str] = set()
    for msg in messages:
        if msg.get("type") == "function_call_output":
            tid = msg.get("call_id", "")
            if tid:
                result_ids.add(tid)
        if msg.get("role") == "tool":
            tid = msg.get("tool_call_id") or msg.get("tool_use_id", "")
            if tid:
                result_ids.add(tid)
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid:
                        result_ids.add(tid)
        elif isinstance(content, dict) and content.get("_openai_assistant"):
            response_items = content.get("response_items")
            if isinstance(response_items, list):
                for item in response_items:
                    if isinstance(item, dict) and item.get("type") == "function_call_output":
                        tid = item.get("call_id", "")
                        if tid:
                            result_ids.add(tid)

    # Walk messages and repair orphaned tool calls.
    sanitized: list[dict[str, Any]] = []
    synthetic_tool_result_content = SYNTHETIC_TOOL_RESULT_CONTENT
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") == "assistant" and isinstance(content, list):
            sanitized.append(msg)
            anthropic_missing_ids: list[str] = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                block_id = str(block.get("id", "") or "")
                if not block_id or block_id in result_ids:
                    continue
                anthropic_missing_ids.append(block_id)
            if anthropic_missing_ids:
                sanitized.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": synthetic_tool_result_content,
                                "is_error": True,
                            }
                            for tid in anthropic_missing_ids
                        ],
                    }
                )
                for tid in anthropic_missing_ids:
                    result_ids.add(tid)
            continue
        elif msg.get("role") == "assistant" and isinstance(content, dict) and content.get("_openai_assistant"):
            sanitized.append(msg)

            tool_call_ids: list[str] = []
            tool_calls = content.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    tid = tool_call.get("id", "")
                    if tid and tid not in tool_call_ids:
                        tool_call_ids.append(tid)

            response_items = content.get("response_items")
            if isinstance(response_items, list):
                for item in response_items:
                    if not isinstance(item, dict) or item.get("type") != "function_call":
                        continue
                    tid = item.get("call_id", "")
                    if tid and tid not in tool_call_ids:
                        tool_call_ids.append(tid)

            missing_ids = [tid for tid in tool_call_ids if tid not in result_ids]
            if missing_ids:
                sanitized.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": synthetic_tool_result_content,
                                "is_error": True,
                            }
                            for tid in missing_ids
                        ],
                    }
                )
        else:
            sanitized.append(msg)

    return sanitized


def _usage_field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _usage_raw_envelope(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return _trace_safe_copy(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return _trace_safe_copy(dumped)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.debug("Usage envelope model_dump failed")
    raw_dict = getattr(value, "__dict__", None)
    if isinstance(raw_dict, dict):
        return _trace_safe_copy({key: val for key, val in raw_dict.items() if not str(key).startswith("_")})
    return {"_type": type(value).__name__, "value": str(value)}


def _normalize_usage(response: object) -> dict[str, Any]:
    """Extract uniform token counts from provider response shapes.

    Returns a superset dict containing BOTH the OpenAI canonical names
    (``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``) AND the
    Anthropic-style names downstream readers expect (``input_tokens`` /
    ``output_tokens`` / ``cache_read_tokens`` / ``cache_creation_tokens``).

    Pre-2026-05-02 this returned only the canonical names, but the
    settle-spend block in ``run`` (``_usage_d1.get("input_tokens", 0)``
    and similar) reads the Anthropic-style keys, so it was ALWAYS seeing 0
    after the normalize step. The condition
    ``if self._user_id and (_in_tokens or _out_tokens):`` was therefore
    always False and ``settle_spend`` never fired on the cloud agent
    path — the Pro $6/month managed-LLM cap silently went unenforced.
    Returning both name sets here keeps every downstream reader honest.
    """
    raw = None
    if isinstance(response, dict):
        raw = response.get("_usage") or response.get("usage")
    else:
        raw = getattr(response, "_usage", None) or getattr(response, "usage", None)

    if not raw:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_write_tokens": 0,
            "web_search_requests": 0,
            "_raw_usage": {},
        }

    def _value(key: str) -> Any:
        return _usage_field(raw, key)

    def _coerce_positive_int(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    def _int_value(*keys: str) -> int:
        for key in keys:
            value = _value(key)
            parsed = _coerce_positive_int(value)
            if parsed > 0:
                return parsed
        return 0

    prompt = _int_value("prompt_tokens", "input_tokens", "prompt")
    completion = _int_value("completion_tokens", "output_tokens", "completion")
    total = _int_value("total_tokens") or (prompt + completion)
    cache_read = _int_value("cache_read_tokens", "cached_tokens", "cache_read_input_tokens")
    cache_create = _int_value("cache_write_tokens", "cache_creation_tokens", "cache_creation_input_tokens")
    server_tool = _value("server_tool_use")
    web_search = _int_value("web_search_requests") or _coerce_positive_int(
        _usage_field(server_tool, "web_search_requests")
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        # Aliases for the settle-spend path and other native-provider readers.
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_create,
        "cache_write_tokens": cache_create,
        "web_search_requests": web_search,
        "_raw_usage": _usage_raw_envelope(raw),
    }


class AgentExecutor:
    """Executes the multi-step agent loop.

    Takes an initial tool_call from the LLM, executes tools iteratively,
    and returns a final AgentResult when the LLM is done reasoning.
    """

    def __init__(
        self,
        llm_caller: Any,
        approval_manager: ApprovalManager,
        mcp_hub: Any,
        tts_speaker: Any | None = None,
        channel: Any | None = None,
        max_iterations: int | None = _DEFAULT_MAX_ITERATIONS,
        total_timeout: float = _DEFAULT_TIMEOUT,
        tool_timeout: float = 45.0,
        overlay_controller: Any | None = None,
        depth: int = 0,
        parent_task_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        model_override: str | None = None,
        native_tools: list[dict[str, Any]] | None = None,
        tool_surface: Any | None = None,
        context_bundle: PromptFrameBundle | None = None,
        system_prompt_text: str | None = None,
        system_context: str | None = None,
        hook_registry: Any | None = None,
        conversation_state_manager: Any | None = None,
        current_agent_id: str | None = None,
        current_agent_name: str | None = None,
        subagent_mode: str | None = None,
        subagent_type: str | None = None,
        hook_settings_runner: Any | None = None,
        allowed_tools: Iterable[str] | None = None,
    ) -> None:
        """Initialize the agent executor.

        Args:
            llm_caller: Object with route_command_native() for tool-call routing.
            approval_manager: Approval gate for tool execution.
            mcp_hub: MCPClientHub for tool dispatch.
            tts_speaker: Optional TTS speaker for progress updates.
            channel: Optional MessageChannel for progress updates (preferred over tts_speaker).
            max_iterations: Optional caller/request turn cap. ``None`` falls
                back to the default loop cap; explicit values are clamped to
                MAX_AGENT_ITERATIONS.
            total_timeout: Total wall-clock timeout for the entire loop.
            tool_timeout: Per-tool execution timeout.
            overlay_controller: Optional BrowserOverlayController for display integration.
            depth: Nesting depth for recursive delegation (0 = root).
            parent_task_id: Task ID of the parent agent (None for root).
            user_id: Owning user for this executor.
            session_id: Owning session for this executor when available.
            model_override: Optional model name for hybrid routing (agent tasks
                can use a different model than ASK/ROUTE).
            native_tools: Request-scoped native tool schemas for provider calls.
            tool_surface: Request-scoped canonical tool-surface snapshot.
            context_bundle: Request-scoped prompt-frame bundle for provider prompts.
            system_prompt_text: Request-scoped native system prompt.
            system_context: Per-turn runtime context to append to the unified prompt.
            hook_registry: Optional request-owned lifecycle hook registry.
            conversation_state_manager: Optional frame-chain manager for this executor.
            current_agent_id: Current subagent id when this executor is a child.
            current_agent_name: Optional stable subagent name.
            subagent_mode: Child mode ("fresh" or "fork") when applicable.
            subagent_type: Current subagent type when this executor is a child.
            hook_settings_runner: Optional settings/session/plugin/skill hook runner.
            allowed_tools: Optional request-scoped tool allowlist from a SKILL.md command.
        """
        from services.conversation.frame_rendering import render_for_openai_responses
        from services.llm.prompts import build_provider_prompt_bundle
        from services.llm.prompts.viola_unified import VIOLA_UNIFIED_PROMPT

        # Callers may pass either raw per-turn context or an already-composed
        # unified prompt. Keep this constructor idempotent so prompt ownership
        # stays compatible across root, resume, and phone-tool paths.
        if isinstance(context_bundle, PromptFrameBundle):
            prompt_bundle = build_provider_prompt_bundle(context_bundle=context_bundle)
            rendered_prompt = render_for_openai_responses(prompt_bundle)
            system_prompt_text = str(rendered_prompt.get("instructions") or "").rstrip()
            prompt_context_bundle = context_bundle
            system_context_text = system_context or ""
            if hasattr(llm_caller, "_agent_system_prompt"):
                llm_caller._agent_system_prompt = None
            elif hasattr(llm_caller, "set_system_prompt"):
                llm_caller.set_system_prompt(None)
        else:
            context_suffix = system_context if system_context is not None else system_prompt_text
            context_suffix = context_suffix or ""
            if isinstance(context_suffix, str) and context_suffix.startswith(VIOLA_UNIFIED_PROMPT):
                system_prompt_text = context_suffix.rstrip()
            else:
                system_prompt_text = (VIOLA_UNIFIED_PROMPT + "\n\n" + str(context_suffix)).rstrip()
            prompt_context_bundle = PromptFrameBundle()
            system_context_text = system_context or ""
            if hasattr(llm_caller, "_agent_system_prompt"):
                llm_caller._agent_system_prompt = system_prompt_text
            elif hasattr(llm_caller, "set_system_prompt"):
                llm_caller.set_system_prompt(system_prompt_text)

        self._llm = llm_caller
        self._approval = approval_manager
        self._mcp_hub = mcp_hub
        self._tts = tts_speaker
        self._channel = channel
        self._configured_max_iterations = int(max_iterations) if max_iterations and int(max_iterations) > 0 else None
        self._hard_agent_iteration_ceiling = MAX_AGENT_ITERATIONS
        self._max_iterations = self._configured_max_iterations
        self._total_timeout = total_timeout
        self._tool_timeout = tool_timeout
        self._overlay = overlay_controller
        self._depth = depth
        self._parent_task_id = parent_task_id
        self.task_id = str(parent_task_id or generate_task_id())
        normalized_user_id = (user_id or "").strip()
        self._user_id = normalized_user_id if normalized_user_id and normalized_user_id.lower() != "default" else None
        self._session_id = (session_id or "").strip() or None
        self._model_override = model_override
        self._native_tools = list(native_tools) if native_tools is not None else None
        self._request_tool_surface = tool_surface
        self._prompt_context_bundle = prompt_context_bundle
        self._request_prompt_instructions_text = system_prompt_text
        self._request_system_prompt_text = system_prompt_text
        self._system_prompt_text = system_prompt_text
        self._system_context_text = system_context_text
        self._hook_registry = hook_registry
        self._conversation_state_manager = conversation_state_manager
        self._request_context_bundle = context_bundle
        self._current_agent_id = (current_agent_id or "").strip() or None
        self._current_agent_name = (current_agent_name or "").strip() or None
        self._subagent_mode = normalize_subagent_mode(subagent_mode)
        self._subagent_type = (subagent_type or "").strip() or None
        self._hook_settings_runner = hook_settings_runner
        self._subagent_stop_dispatched = False
        self._signature_gate_override_token: str | None = None
        self._signature_gate_override_actions: int = _SIGNATURE_GATE_OVERRIDE_ACTIONS
        self._agent_plan_policy = _AgentPlanLimitPolicy(
            plan_id="free",
            plan_family="free",
        )
        self._agent_chain_metrics_recorded = False

        # Parse retry state (reset per run)
        self._retry_count = 0
        self._parse_failed = False
        # Token usage from the most recent LLM response (set per _get_next_response)
        self._last_usage: dict[str, Any] = {}
        self._llm_cumulative_input_tokens: int = 0
        self._llm_cumulative_output_tokens: int = 0
        self._llm_cumulative_total_tokens: int = 0
        self._llm_total_input_tokens_seen: int = 0
        self._llm_total_output_tokens_seen: int = 0
        self._llm_last_input_tokens: int = 0
        self._llm_last_output_tokens: int = 0
        self._llm_context_usage_pct: float = 0.0
        self._virtual_message_count: int = 0
        self._last_continuity_mode: str | None = None
        self._last_server_context_tokens: int = 0
        self._session_cost_session_id: str | None = None
        self._session_cost_user_id: str | None = None
        self._responses_compaction_failure_streak: int = 0
        self._step_ceiling_compaction_attempted: bool = False
        self._pending_spin_intervention: str | None = None
        self._pending_correction_msgs: list[str] | None = None
        self._pending_consecutive_warning: str | None = None
        self._pending_error_registry_ctx: str | None = None
        self._pending_hard_loop_breaker: str | None = None
        self._pending_force_termination: str | None = None
        self._pending_compaction_meta: dict[str, Any] | None = None
        self._pending_trim_stats: dict[str, int] | None = None
        self._tool_progress_repetition_counts: dict[str, int] = {}
        self._tool_progress_seen_urls: set[str] = set()
        # Progress-scoped spin detection: track consecutive identical no-effect
        # results per (tool, normalized_args) key. Replaces the prior raw
        # repeat-count carve-out which fired on legitimate multi-page navigation.
        self._tool_progress_last_fingerprint: dict[str, str] = {}
        self._tool_progress_consecutive_identical: dict[str, int] = {}
        self._tool_progress_script_probe_state: dict[str, Any] = {
            "page_url": "",
            "count": 0,
        }
        # Issue #278: consecutive bot-check/WAF-challenge page_health
        # observations across ANY browser tool call (see
        # _NO_PROGRESS_PAGE_HEALTH_STATUSES / _BOT_PROTECTION_HALT_STREAK).
        self._consecutive_bot_protection_signals: int = 0
        self._memory_all_scope_no_match_queries: list[str] = []
        self._memory_enumeration_blocked_after_no_match: bool = False
        self._stuck_tool_compaction_attempted: bool = False
        self._pending_stuck_tool_progress: dict[str, Any] | None = None
        self._last_model_name: str = "unknown"
        self._last_no_result_reasoning: str = ""
        self._step_log_jsonl_path: Path | None = None
        self._step_log_metadata_emitted: bool = False
        self._task_trace: AsyncTaskTraceWriter | None = None
        # Lane C (#466): in-flight post-answer teardown tasks (completed-checkpoint
        # write, terminal broadcast) scheduled off the answer path. Tracked so a
        # consumer needing read-your-writes can await drain_post_answer_teardown().
        self._post_answer_teardown_tasks: set[Any] = set()
        self._task_trace_last_request: dict[str, Any] | None = None
        self._task_trace_last_response: dict[str, Any] | None = None
        self._task_trace_last_continuity_before: dict[str, Any] | None = None
        self._task_trace_last_continuity_after: dict[str, Any] | None = None
        self._task_trace_last_tool_execution: dict[str, Any] | None = None
        self._task_trace_llm_attempt_seq: int = 0
        self._task_trace_last_attempt_id: str | None = None
        self._task_trace_last_provider_metadata: dict[str, Any] | None = None

        def _explicit_bool_attr(obj: Any, name: str, default: bool | None = None) -> bool | None:
            if hasattr(type(obj), name):
                return bool(getattr(obj, name))
            value = getattr(obj, name, None)
            if isinstance(value, bool):
                return value
            return default

        # Native tool capability is stable for the executor lifetime. Routers
        # may expose route_command_native even when the active provider is
        # text-only, so require an explicit capability flag.
        supports_native_tools = _explicit_bool_attr(self._llm, "SUPPORTS_NATIVE_TOOLS")
        if supports_native_tools is None:
            supports_native_tools = _explicit_bool_attr(self._llm, "native_tools_supported", False)
        self._use_native = bool(hasattr(self._llm, "route_command_native") and supports_native_tools)
        logger.info(
            "Executor provider=%s use_native=%s",
            type(self._llm).__name__,
            self._use_native,
        )
        self._native_messages: list[dict[str, Any]] = []
        self._text_messages: list[dict[str, str]] = []
        self._responses_continuity: dict[str, Any] = {}
        self._responses_invalid_encrypted_content_repair_attempted: bool = False
        from services.conversation.tool_result_storage import (
            provision_content_replacement_state,
        )

        _tool_result_budget_enabled = str(
            os.environ.get("VIOLA_TOOL_RESULT_BUDGET_ENABLED", "true") or ""
        ).lower() not in {"0", "false", "no", "off"}
        self._content_replacement_state = provision_content_replacement_state(
            enabled=_tool_result_budget_enabled,
        )
        self._tool_result_replacement_records: list[Any] = []

        # Final answer from the agent (set by answer tool or forced by safety)
        self._raw_final_answer: str | None = None
        self._final_answer: str | None = None
        self._final_continue_listening: bool | None = None
        self._final_command: str | None = None
        self._final_params: dict[str, Any] = {}
        self._last_tool_name: str | None = None
        self._last_tool_ok: bool | None = None
        self._last_tool_error: str | None = None
        self._last_tool_data: Any = None
        self._last_tool_result_text: str = ""
        self._last_completion_evidence: dict[str, Any] | None = None
        self._last_incomplete_completion_evidence: dict[str, Any] | None = None
        self._last_tool_call_id: str | None = None
        self._last_signature_resume_ok: bool | None = None
        self._last_signature_resume_error: str | None = None
        self._last_signature_resume_data: Any = None
        self._last_signature_resume_text: str = ""
        self._last_payment_resume_ok: bool | None = None
        self._last_payment_resume_error: str | None = None
        self._last_payment_resume_data: Any = None
        self._last_payment_resume_text: str = ""
        # Track latest browser page URL across iterations
        self._last_page_url: str | None = None
        # Set when the agent hits the payment gate (skip browser cleanup)
        self._payment_gate_active: bool = False
        self._payment_confirmation_ctx: dict[str, Any] | None = None
        self._payment_cdp_bridge: dict[str, Any] | None = None
        self._payment_gate_requested: bool = False
        self._last_payment_review_request: dict[str, Any] | None = None
        self._last_payment_review_page_url: str | None = None
        self._payment_gate_override_token: str | None = None
        self._payment_confirmation_tool_context: dict[str, Any] | None = None
        self._signature_gate_active: bool = False
        self._signature_gate_requested: bool = False

        # Track whether we have shown the agentic overlay
        self._overlay_shown: bool = False
        # Cancellation flag â€” checked between tool calls
        self._cancelled: bool = False
        # Cancellation event â€” allows mid-tool abort via asyncio.wait()
        self._cancel_event: asyncio.Event = asyncio.Event()
        # User takeover latch. The browser stays interactive while the agent
        # waits between tool and LLM turns.
        self._takeover_active: bool = False
        self._takeover_interrupt_event: asyncio.Event = asyncio.Event()
        self._takeover_release_event = threading.Event()
        self._takeover_release_event.set()

        # Consecutive failure tracking (cross-tool cascading failures)
        self._consecutive_failures: int = 0
        self._consecutive_failure_threshold: int = 3

        # Diagnostic context accumulator (for self-diagnosis engine)
        self._diagnostic_contexts: list[Any] = []
        self._system_context_components: dict[str, str] = {}
        self._step_approval_path: str | None = None
        self._step_approval_tool: str | None = None

        # Per-step context reminders (Fix 2: prevent instruction drift)
        self._task_category: str = "general"
        self._original_command: str = ""

        # Progress-based timeout: auto-extend once if agent is making progress
        self._timeout_extended: bool = False

        # Historical compatibility slot. Production routing normally leaves
        # this as None: the model sees all visible tools and safety is enforced
        # by tier, approval, payment/signature gates, and per-task rejections.
        # SKILL.md prompt commands may set an explicit request-scoped allowlist.
        if allowed_tools is None:
            self._allowed_tools: set[str] | None = None
        else:
            self._allowed_tools = {str(tool).strip() for tool in allowed_tools if str(tool).strip()}

        # Fix 3: Per-task tool blacklist â€” tools rejected by the approval system.
        # Populated only from the structured APPROVAL_BLOCKED error category.
        # Reset at the start of each run() call.
        self._rejected_tools: set[str] = set()
        self._approval_blocked_tools: list[str] = []
        self._irreversible_confirmed_action_classes: set[str] = set()

        # Fix 4: Consecutive click failure counter.
        # Tracks failed browser_interact calls (timeout/error).
        # The counter is telemetry only; it does not inject model-visible
        # correction text.
        # Reset when a non-click tool is called or a click succeeds.
        self._consecutive_click_failures: int = 0

        # API registry pre-call gate (#5)
        self._api_registry_checked: bool = False

        # Browser escalation tier tracking (#10)
        self._browser_tiers_used: set[str] = set()

        # C1: Diminishing-returns heuristic â€” detect repeated identical results
        self._consecutive_no_progress: int = 0
        self._last_result_hash: str = ""

        # Memory verification: track recalled memory IDs for post-success promotion
        self._recalled_memory_ids: list[int] = []
        self._forced_tool_failure_error: str | None = None
        self._forced_tool_failure_outcome: str | None = None

        # 2B: Read-before-write â€” track browser observation recency
        # LA-3: Proactive token budget tracking â€” triggers compaction before
        # the model's context window is exceeded, avoiding wasteful 400 errors.
        self._token_tracker = TokenBudgetTracker(
            model=self._get_model_name_safe(),
            context_window=self._get_context_window_safe(),
        )

        # C2: Context memoization â€” avoid rebuilding static context every iteration
        self._cached_system_context: str | None = None
        self._c2_invalidation_tools: frozenset[str] = frozenset(
            {
                "file_write",
                "run_command",
                "computer",
                "self_manage",
                "memory",
                "schedule",
                "calendar",
                "smart_home",
                "api_credential",
                "telegram_send",
                "notify",
                "gmail",
                "phone",
                "payment",
                "signature",
                "playlist",
                "play_music",
                "timer",
                "mcp_servers",
                "browser_interact",
                "browser_fill_form",
                "browser_navigate",
                "browser_type",
            }
        )

    def invalidate_context_cache(self) -> None:
        """Invalidate memoized system context (C2).

        Called by B6 post-compaction hooks and internally after
        state-modifying tool calls.
        """
        self._cached_system_context = None

    def _get_registry_keys(self) -> list[str]:
        """Return registry keys that should reference this executor."""
        keys: list[str] = []
        if self._session_id:
            keys.append("session:%s" % self._session_id)
        if self._user_id:
            keys.append("user:%s" % self._user_id)
        task_id = getattr(self, "task_id", None)
        if task_id:
            keys.append("task:%s" % task_id)
        return keys

    def _ensure_task_id(self) -> str:
        task_id = str(getattr(self, "task_id", "") or "").strip()
        if task_id:
            return task_id
        task_id = generate_task_id()
        self.task_id = task_id
        return task_id

    def _get_effective_user_id(self) -> str:
        """Return the user_id for user-scoped side effects."""
        if self._user_id:
            return self._user_id
        if self._session_id:
            return "session:%s" % self._session_id
        return "transient:%s" % (getattr(self, "task_id", None) or id(self))

    def _hook_agent_id(self) -> str | None:
        """Return the current hook agent id, if this executor is a subagent."""

        agent_id = str(getattr(self, "_current_agent_id", "") or "").strip()
        if agent_id:
            return agent_id
        agent_name = str(getattr(self, "_current_agent_name", "") or "").strip()
        return agent_name or None

    def _hook_agent_type(self) -> str | None:
        """Return the current hook agent type, matching Claude's subagent field."""

        agent_type = str(getattr(self, "_subagent_type", "") or "").strip()
        return agent_type or None

    def _hook_transcript_path(self) -> str | None:
        """Return the best durable transcript artifact for settings hooks."""

        agent_id = self._hook_agent_id()
        if agent_id:
            try:
                return str(subagent_transcript_path(agent_id, user_id=self._get_effective_user_id()))
            except (OSError, RuntimeError, TypeError, ValueError):
                logger.debug(
                    "Unable to resolve subagent transcript path for hook agent %s",
                    agent_id,
                )
        task_trace = getattr(self, "_task_trace", None)
        task_trace_path = getattr(task_trace, "path", None)
        if task_trace_path:
            return str(task_trace_path)
        checkpoint = getattr(self, "_current_checkpoint", None)
        checkpoint_context = getattr(checkpoint, "context", None)
        if isinstance(checkpoint_context, dict):
            path = str(checkpoint_context.get("task_trace_path") or "").strip()
            if path:
                return path
        step_log_path = getattr(self, "_step_log_jsonl_path", None)
        if step_log_path:
            return str(step_log_path)
        return None

    @staticmethod
    def _app_surface_is_cloud() -> bool:
        from services.llm.managed_budget import app_surface_is_cloud

        return app_surface_is_cloud()

    def _resolve_agent_plan_policy(self) -> _AgentPlanLimitPolicy:
        plan_id_value = "free"
        try:
            from core.product import coerce_plan_id, plan_family_for_id
            from core.request_context import resolve_runtime_plan_id

            resolved_plan = resolve_runtime_plan_id(self._user_id) if self._user_id else None
            plan_id = coerce_plan_id(resolved_plan)
            plan_id_value = plan_id.value
            plan_family = plan_family_for_id(plan_id).value
        except Exception as exc:
            logger.warning("Agent plan lookup failed for user %s: %s", self._user_id, exc)
            plan_family = "free"
        return _AgentPlanLimitPolicy(
            plan_id=plan_id_value,
            plan_family=plan_family,
        )

    def _apply_agent_chain_cap(self, policy: _AgentPlanLimitPolicy, user_text: str | None = None) -> None:
        del policy, user_text
        hard_ceiling = int(getattr(self, "_hard_agent_iteration_ceiling", MAX_AGENT_ITERATIONS))
        if self._configured_max_iterations is None:
            self._max_iterations = min(_DEFAULT_MAX_ITERATIONS, hard_ceiling)
            return
        if self._configured_max_iterations > hard_ceiling:
            logger.warning(
                "Agent iteration cap clamped: requested=%d, hard ceiling=%d",
                self._configured_max_iterations,
                hard_ceiling,
            )
            self._max_iterations = hard_ceiling
            return
        self._max_iterations = int(self._configured_max_iterations)

    def _user_uses_managed_llm(self) -> bool:
        from services.llm.managed_budget import user_uses_managed_llm

        return user_uses_managed_llm(self._user_id)

    def _check_managed_llm_spend_cap(self) -> Any:
        """Sync-only legacy helper; async runtime must use the async variant."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            from services.llm.managed_budget import check_managed_llm_spend_cap

            return check_managed_llm_spend_cap(self._user_id, managed_llm=self._user_uses_managed_llm())
        raise RuntimeError(
            "AgentExecutor._check_managed_llm_spend_cap() is sync-only; "
            "use _check_managed_llm_spend_cap_async() inside an event loop."
        )

    async def _check_managed_llm_spend_cap_async(self) -> Any:
        from services.llm.managed_budget import check_managed_llm_spend_cap_async

        return await check_managed_llm_spend_cap_async(self._user_id, managed_llm=self._user_uses_managed_llm())

    @staticmethod
    def _managed_llm_budget_message(gate: Any) -> str:
        from services.llm.managed_budget import managed_llm_budget_message

        return managed_llm_budget_message(gate)

    def _record_agent_gate_denial_telemetry(
        self,
        *,
        gate_name: str,
        current_usage: int,
        limit_value: int,
    ) -> None:
        """Best-effort instrumentation after the gate has already denied."""
        try:
            from admin.instrumentation import record_gate_denial

            record_gate_denial(
                gate_name=gate_name,
                tier=self._agent_plan_policy.plan_family,
                current_usage=current_usage,
                limit_value=limit_value,
                user_id=self._user_id or "",
            )
        except Exception:
            logger.debug("Agent gate denial telemetry skipped", exc_info=True)

    @staticmethod
    def _sdk_probe_attr(obj: Any, attr_name: str) -> Any:
        """Read provider wrapper attrs without following MagicMock auto-children."""
        try:
            from unittest.mock import Mock

            if isinstance(obj, Mock) and attr_name not in getattr(obj, "__dict__", {}):
                return None
        except ImportError:
            logger.debug("unittest.mock import unavailable during safe attribute lookup")

        try:
            return getattr(obj, attr_name, None)
        except Exception:
            return None

    @classmethod
    def _sdk_provider_candidates(cls, root: Any) -> list[Any]:
        candidates: list[Any] = []
        stack: list[Any] = [root]
        seen: set[int] = set()
        while stack:
            obj = stack.pop()
            if obj is None:
                continue
            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            candidates.append(obj)

            for attr_name in (
                "_provider",
                "_fallback_provider",
                "_haiku_provider",
                "active_provider",
                "primary_provider",
            ):
                child = cls._sdk_probe_attr(obj, attr_name)
                if child is not None:
                    stack.append(child)

            providers = cls._sdk_probe_attr(obj, "_providers")
            if isinstance(providers, (list, tuple)):
                stack.extend(provider for provider in providers if provider is not None)
        return candidates

    def _sdk_dispatch_provider(self) -> Any | None:
        """Return the concrete OpenAI Agents provider hidden inside wrappers."""
        try:
            from services.llm.providers.openai_agents_provider import (
                OpenAIAgentsProvider,
            )
        except ImportError:
            return None

        for candidate in self._sdk_provider_candidates(self._llm):
            if isinstance(candidate, OpenAIAgentsProvider):
                return candidate
        return None

    @staticmethod
    def _normalize_responses_continuity_state(
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Normalize provider continuity metadata into a checkpoint-safe dict."""
        return _normalize_openai_response_continuity(payload)

    @classmethod
    def _extract_responses_continuity_from_response(cls, response: dict[str, Any]) -> dict[str, Any]:
        """Extract provider-exposed continuity metadata from a response envelope."""
        payload: dict[str, Any] = {}

        for key in ("_continuity", "_responses_continuity"):
            value = response.get(key)
            if isinstance(value, dict):
                payload.update(copy.deepcopy(value))
                break

        for source_key in ("previous_response_id", "response_id", "_response_id"):
            value = response.get(source_key)
            if isinstance(value, str) and value.strip():
                target_key = source_key if source_key != "_response_id" else "response_id"
                payload.setdefault(target_key, value.strip())
                break

        for source_key in (
            "response_items",
            "_response_items",
            "encrypted_reasoning_items",
            "_encrypted_reasoning_items",
            "reasoning_items",
            "_reasoning_items",
        ):
            value = response.get(source_key)
            if isinstance(value, list) and value:
                payload.setdefault("response_items", copy.deepcopy(value))
                break

        mode = response.get("_continuity_mode") or response.get("continuity_mode")
        if isinstance(mode, str) and mode.strip():
            payload.setdefault("mode", mode.strip())

        return cls._normalize_responses_continuity_state(payload)

    def _restore_responses_continuity_state(self, payload: dict[str, Any] | None) -> None:
        """Restore continuity state from a checkpoint or initial provider response."""
        payload_has_cursor = isinstance(payload, dict) and ("input_cursor" in payload or "cursor" in payload)
        state = self._normalize_responses_continuity_state(payload)
        if state:
            cursor = state.get("input_cursor", 0)
            if not isinstance(cursor, int):
                cursor = 0
            if not payload_has_cursor and self._native_messages:
                cursor = len(self._native_messages)
            state["input_cursor"] = min(max(cursor, 0), len(self._native_messages))
        self._responses_continuity = state

    def _export_responses_continuity_state(self) -> dict[str, Any]:
        """Return continuity state suitable for checkpoint persistence."""
        state = self._normalize_responses_continuity_state(self._responses_continuity)
        if not state:
            return {}
        state["input_cursor"] = min(state.get("input_cursor", 0), len(self._native_messages))
        return state

    async def clear_partial_state_for_fallback(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        executor: Any | None = None,
    ) -> None:
        """Drop partial assistant/tool state when the primary triggers fallback.

        Invoked when FallbackTriggeredError propagates.
        Tombstones assistant messages with unmatched tool_use ids so the
        fallback retry cannot pair stale tool_use ids with fresh tool_results.
        """

        del executor
        from intent.streaming_tool_executor import tombstone_partial_assistant_messages

        target = messages if messages is not None else self._native_messages
        tool_use_ids: set[str] = set()
        tool_result_ids: set[str] = set()
        for message in target:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_use_id = str(block.get("id") or "")
                    if tool_use_id:
                        tool_use_ids.add(tool_use_id)
                elif block.get("type") == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or "")
                    if tool_use_id:
                        tool_result_ids.add(tool_use_id)
        cleaned, _removed = tombstone_partial_assistant_messages(target, tool_use_ids - tool_result_ids)
        if target is self._native_messages:
            self._native_messages[:] = cleaned
        elif messages is not None:
            messages[:] = cleaned

    def _responses_continuity_active(self) -> bool:
        """Return True when the provider has opted into delta-based continuity."""
        return bool(self._export_responses_continuity_state().get("mode"))

    @staticmethod
    def _responses_continuity_size(state: dict[str, Any]) -> tuple[int, int]:
        """Return a rough size estimate for replayed response-items continuity."""
        response_items = state.get("response_items")
        if not isinstance(response_items, list) or not response_items:
            return 0, 0
        try:
            char_count = len(json.dumps(response_items, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            char_count = len(str(response_items))
        return len(response_items), char_count

    def _should_compact_responses_continuity(
        self,
        state: dict[str, Any] | None,
        *,
        force: bool = False,
    ) -> bool:
        """Decide whether stateless response-items continuity should be compacted.

        Claude Code auto-compacts on effective context-window pressure, not on
        a fixed count of response items or serialized characters. ``force`` is
        reserved for reactive overflow recovery after the provider rejects a
        request.
        """
        if not isinstance(state, dict):
            return False
        if state.get("mode") != _RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS:
            return False
        item_count, _char_count = self._responses_continuity_size(state)
        if item_count <= 0:
            return False
        if force:
            return True
        return self._should_compact_by_cumulative_usage()

    def _responses_compaction_failure_count(self) -> int:
        try:
            return max(0, int(getattr(self, "_responses_compaction_failure_streak", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _should_fallback_from_responses_compaction_failures(self) -> bool:
        """Return True after repeated response-items compaction failures."""
        return self._responses_compaction_failure_count() >= _RESPONSES_COMPACTION_FAILURE_LIMIT

    def _record_responses_compaction_failure(self, *, reason: str, detail: str) -> None:
        failure_count = self._responses_compaction_failure_count() + 1
        self._responses_compaction_failure_streak = failure_count
        logger.warning(
            "Responses continuity compaction failed: reason=%s failures=%d/%d detail=%s",
            reason,
            failure_count,
            _RESPONSES_COMPACTION_FAILURE_LIMIT,
            detail,
        )

    def _reset_responses_compaction_failures(self) -> None:
        self._responses_compaction_failure_streak = 0

    async def _compact_native_responses_continuity(self, *, reason: str, force: bool = False) -> bool:
        """Compact response-items continuity through the active provider when supported."""
        state = self._export_responses_continuity_state()
        if not self._should_compact_responses_continuity(state, force=force):
            return False
        if self._should_fallback_from_responses_compaction_failures():
            logger.warning(
                "Skipping Responses continuity compaction after %d consecutive failures; local fallback is required",
                self._responses_compaction_failure_count(),
            )
            return False

        compact_fn = getattr(self._llm, "compact_responses_continuity", None)
        if not callable(compact_fn):
            return False

        items_before, chars_before = self._responses_continuity_size(state)
        try:
            compacted_state = await compact_fn(
                continuity=copy.deepcopy(state),
                model_override=self._model_override,
            )
        except _responses_compaction_error_types() as exc:
            self._record_responses_compaction_failure(reason=reason, detail=str(exc))
            return False
        normalized = self._normalize_responses_continuity_state(compacted_state)
        if not normalized:
            self._record_responses_compaction_failure(reason=reason, detail="invalid_compacted_state")
            return False

        normalized["input_cursor"] = min(state.get("input_cursor", 0), len(self._native_messages))
        self._responses_continuity = normalized
        self._reset_responses_compaction_failures()
        items_after, chars_after = self._responses_continuity_size(normalized)
        self._pending_compaction_meta = {
            "method": "responses_compact",
            "reason": reason,
            "items_before": items_before,
            "items_after": items_after,
            "chars_before": chars_before,
            "chars_after": chars_after,
        }
        logger.info(
            "Responses continuity compaction applied: reason=%s items=%d->%d chars=%d->%d",
            reason,
            items_before,
            items_after,
            chars_before,
            chars_after,
        )
        self._reset_llm_usage_compaction_window()
        return True

    def _capture_responses_continuity_from_response(self, response: dict[str, Any]) -> None:
        """Update continuity state after a successful provider response."""
        state = self._extract_responses_continuity_from_response(response)
        if not state:
            self._responses_continuity = {}
            return
        state["input_cursor"] = len(self._native_messages)
        self._responses_continuity = state

    def _repair_invalid_encrypted_responses_continuity(self) -> bool:
        """Drop encrypted reasoning replay if OpenAI rejects it as invalid."""
        state = self._export_responses_continuity_state()
        if state.get("mode") != _RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS:
            return False
        response_items = state.get("response_items")
        if not isinstance(response_items, list) or not response_items:
            return False

        repaired_items, dropped = _drop_encrypted_reasoning_items(response_items)
        if dropped <= 0:
            return False

        items_before = len(response_items)
        if repaired_items:
            repaired_state = copy.deepcopy(state)
            repaired_state["response_items"] = repaired_items
            repaired_state["has_encrypted_reasoning"] = False
            repaired_state["input_cursor"] = min(state.get("input_cursor", 0), len(self._native_messages))
            self._responses_continuity = self._normalize_responses_continuity_state(repaired_state)
        else:
            self._responses_continuity = {}

        items_after = len(self._responses_continuity.get("response_items") or [])
        self._pending_compaction_meta = {
            "method": "responses_invalid_encrypted_content_repair",
            "reason": "invalid_encrypted_content",
            "items_before": items_before,
            "items_after": items_after,
            "dropped_encrypted_reasoning_items": dropped,
        }
        logger.warning(
            "Dropped %d encrypted reasoning replay item(s) after invalid_encrypted_content; response_items=%d->%d",
            dropped,
            items_before,
            items_after,
        )
        return True

    def _acknowledge_native_assistant_message(self) -> None:
        """Advance the local delta cursor after storing a provider assistant turn."""
        if self._responses_continuity:
            self._responses_continuity["input_cursor"] = len(self._native_messages)

    def _build_native_request_messages(
        self, *, trim_history: bool
    ) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
        """Build the outbound native payload without mutating local history."""
        continuity_state = self._export_responses_continuity_state()
        continuity_active = continuity_state.get("mode") in {
            _RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID,
            _RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS,
        }

        source_messages = self._native_messages
        continuity_seen_tool_uses: set[str] = set()
        if continuity_active:
            start = min(continuity_state.get("input_cursor", 0), len(self._native_messages))
            # The local history BEFORE the delta cursor represents turns the
            # provider has already observed via response_items /
            # previous_response_id. Their tool_use ids are valid pair-targets
            # for tool_result blocks in the delta — delta repair must treat
            # them as "seen" rather than stripping the results as orphans
            # (Claude TS analog: ensureToolResultPairing runs over the FULL
            # provider-bound stream, not a sliced delta).
            from services.conversation.message_invariants import (
                native_message_tool_use_ids,
            )

            for prior in self._native_messages[:start]:
                for tool_use_id in native_message_tool_use_ids(prior):
                    if tool_use_id:
                        continuity_seen_tool_uses.add(tool_use_id)

            # Also seed from response_items continuity state — those
            # function_call items live in provider memory but never appear in
            # _native_messages.
            response_items = continuity_state.get("response_items")
            if isinstance(response_items, list):
                for item in response_items:
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        call_id = str(item.get("call_id") or "")
                        if call_id:
                            continuity_seen_tool_uses.add(call_id)

            source_messages = self._native_messages[start:]
            trim_history = False
            if not source_messages:
                logger.warning(
                    "Responses continuity had no unsent local messages; falling back to full native replay for task %s",
                    getattr(self, "task_id", "?"),
                )
                source_messages = self._native_messages
                continuity_active = False
                continuity_seen_tool_uses = set()

        scratch = SimpleNamespace(_native_messages=copy.deepcopy(source_messages))
        AgentMessageManager(scratch).sanitize_for_api(
            trim_history=trim_history,
            delta_mode=continuity_active,
            continuity_seen_tool_uses=(continuity_seen_tool_uses if continuity_active else None),
        )

        continuity_kwargs: dict[str, Any] = {}
        if continuity_active:
            continuity_payload: dict[str, Any] = {
                "mode": continuity_state["mode"],
                "messages_are_delta": True,
            }
            previous_response_id = continuity_state.get("previous_response_id")
            if isinstance(previous_response_id, str) and previous_response_id:
                continuity_payload["previous_response_id"] = previous_response_id
                continuity_kwargs["previous_response_id"] = previous_response_id

            response_items = continuity_state.get("response_items")
            if isinstance(response_items, list) and response_items:
                copied_items = copy.deepcopy(response_items)
                continuity_payload["response_items"] = copied_items
                continuity_kwargs["response_items"] = copy.deepcopy(copied_items)

            continuity_kwargs["continuity"] = copy.deepcopy(continuity_payload)
            continuity_kwargs["responses_continuity"] = copy.deepcopy(continuity_payload)
            continuity_kwargs["messages_are_delta"] = True

        return scratch._native_messages, continuity_kwargs, continuity_active

    def _build_trace_continuity_before(self, continuity_state: dict[str, Any] | None) -> dict[str, Any]:
        """Combine provider continuity with incoming conversation context for traces."""

        trace_state = copy.deepcopy(continuity_state or {})
        prior_messages = getattr(self, "_prior_conversation_messages", [])
        if prior_messages:
            trace_state["conversation_context"] = {
                "source": getattr(self, "_prior_conversation_source", "conversation_history"),
                "message_count": len(prior_messages),
                "messages": copy.deepcopy(prior_messages),
            }
        return trace_state

    def _snapshot_checkpoint_state(
        self,
        checkpoint: TaskCheckpoint,
        messages: list[dict[str, Any]],
        *,
        prefer_provided_messages: bool = False,
    ) -> None:
        """Persist both local history and remote continuity handles into the checkpoint."""
        source_messages = (
            messages if prefer_provided_messages else (self._native_messages if self._use_native else messages)
        )
        checkpoint.llm_messages = compress_messages(source_messages)
        checkpoint.llm_continuity = self._export_responses_continuity_state() if self._use_native else {}

    def dispatch_hook(self, event_name: Any, **kwargs: Any) -> Any:
        """Dispatch a hook through the executor's request-scoped registry."""

        from intent.hooks.dispatcher import dispatch_hook as dispatch_normalized_hook
        from intent.hooks.schema import HookEvent, HookResult

        payload = dict(kwargs)
        tool_name = payload.pop("tool_name", None)
        tool_input = payload.pop("tool_input", payload.pop("args", None))
        session_id = payload.pop("session_id", None) or self._session_id
        frame_uuid = payload.pop("frame_uuid", None)
        if self._user_id is not None:
            payload.setdefault("user_id", self._user_id)
        if session_id is not None:
            payload.setdefault("session_id", session_id)
        transcript_path = self._hook_transcript_path()
        if transcript_path:
            payload.setdefault("transcript_path", transcript_path)
        agent_id = self._hook_agent_id()
        if agent_id:
            payload.setdefault("agent_id", agent_id)
        agent_type = self._hook_agent_type()
        if agent_type:
            payload.setdefault("agent_type", agent_type)

        event = HookEvent(
            name=event_name,
            payload=payload,
            session_id=session_id,
            frame_uuid=frame_uuid,
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else None,
        )
        aggregate = HookResult()
        settings_runner = getattr(self, "_hook_settings_runner", None)
        if settings_runner is not None:
            from intent.hooks.settings_runner import HookExecutionContext

            context = HookExecutionContext(
                cwd=os.getcwd(),
                permission_mode=getattr(self._approval, "permission_mode", None),
                transcript_path=self._hook_transcript_path(),
                agent_id=self._hook_agent_id(),
                agent_type=self._hook_agent_type(),
            )
            aggregate = aggregate.merge(settings_runner.dispatch(event, context))

        registry = getattr(self, "_hook_registry", None)
        if registry is not None:
            aggregate = aggregate.merge(
                dispatch_normalized_hook(
                    event,
                    dispatch_fn=registry.dispatch,
                    dispatch_state=getattr(registry, "dispatch_state", None),
                )
            )
            return aggregate
        aggregate = aggregate.merge(dispatch_normalized_hook(event))
        return aggregate

    @classmethod
    async def resume_from_checkpoint(
        cls,
        task_id: str,
        llm_caller: Any,
        approval_manager: ApprovalManager,
        mcp_hub: Any,
        tts_speaker: Any | None = None,
        channel: Any | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        resume_user_reply: str | None = None,
        native_tools: list[dict[str, Any]] | None = None,
        tool_surface: Any | None = None,
        system_prompt_text: str | None = None,
        context_bundle: Any | None = None,
        conversation_state_manager: Any | None = None,
        hook_registry: Any | None = None,
        hook_settings_runner: Any | None = None,
    ) -> AgentResult:
        """Resume a previously saved agent task from its checkpoint.

        Loads the checkpoint from disk, reconstructs message history,
        and re-enters the agent loop to continue execution.

        Args:
            task_id: Task ID to resume.
            llm_caller: LLM provider for continued execution.
            approval_manager: Approval gate for tool execution.
            mcp_hub: MCPClientHub for tool dispatch.
            tts_speaker: Optional TTS speaker for progress updates.
            channel: Optional MessageChannel for progress updates.

        Returns:
            AgentResult with the final outcome or error if resume failed.
        """
        from intent.task_checkpoint import (
            load_checkpoint,
            mark_complete,
            save_checkpoint,
        )

        checkpoint = load_checkpoint(task_id, user_id=user_id)
        if checkpoint is None:
            return AgentResult(
                ok=False,
                error="No checkpoint found for task %s" % task_id,
            )

        if checkpoint.status not in ("in_progress", "waiting_for_user", "interrupted"):
            return AgentResult(
                ok=False,
                error="Task %s has status '%s', cannot resume" % (task_id, checkpoint.status),
            )

        # Create a new executor instance
        executor = cls(
            llm_caller=llm_caller,
            approval_manager=approval_manager,
            mcp_hub=mcp_hub,
            tts_speaker=tts_speaker,
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            native_tools=native_tools,
            tool_surface=tool_surface,
            system_prompt_text=system_prompt_text,
            context_bundle=context_bundle,
            conversation_state_manager=conversation_state_manager,
            hook_registry=hook_registry,
            hook_settings_runner=hook_settings_runner,
        )

        restored_messages: list[dict[str, Any]] | None = None

        # Restore message history from checkpoint
        if checkpoint.llm_messages:
            # Sanitize: strip trailing orphaned tool_use blocks that
            # lack matching tool_result.  Without this, the API rejects
            # the conversation with BadRequestError.
            restored = _sanitize_checkpoint_messages(checkpoint.llm_messages)

            # Normalize content blocks: checkpoint files saved before the
            # compress_messages normalization fix (Appendix B5) may contain
            # mangled SDK objects.  Re-normalise every content-block list
            # to guarantee plain dicts that the native API accepts.
            for msg in restored:
                content = msg.get("content")
                if isinstance(content, list):
                    msg["content"] = _normalize_content_blocks(content)

            restored_messages = restored

        # `pending_gate_type` is the original (waiting_for_user) value; the AI
        # controller pre-transitions the checkpoint to `in_progress` before the
        # resume prompt bundle is built (so the resumed loop does not see a
        # GATE_STATE frame instructing it to re-resume itself) and stashes the
        # original gate type as `_pre_resume_gate_type` so we can still set up
        # the override token tied to THIS resume.
        pending_gate_type = str(checkpoint.context.get("pending_gate_type") or "").strip().lower()
        if not pending_gate_type:
            pending_gate_type = str(checkpoint.context.get("_pre_resume_gate_type") or "").strip().lower()
        _checkpoint_is_resuming_gate = checkpoint.status == "waiting_for_user" or str(
            checkpoint.context.get("_pre_resume_gate_type") or ""
        ).strip().lower() in {"signature", "payment"}

        # Build continuation context
        pending_q = checkpoint.context.get("pending_question", "")
        if _checkpoint_is_resuming_gate and pending_gate_type == "payment":
            try:
                from services.payments.confirmation import (
                    ConfirmationStatus,
                    build_public_confirmation_url,
                    get_confirmation_manager,
                )

                mgr = get_confirmation_manager()
                token = str(checkpoint.context.get("confirm_token") or "").strip()
                payment_page_url = str(
                    checkpoint.context.get("payment_merchant_url") or checkpoint.context.get("payment_page_url") or ""
                ).strip()
                session = mgr.get_session(token) if token else None
                if session is not None and session.status in (
                    ConfirmationStatus.PENDING,
                    ConfirmationStatus.CONFIRMED,
                    ConfirmationStatus.REJECTED,
                    ConfirmationStatus.REJECTED_PROCESSING,
                ):
                    url = build_public_confirmation_url(token)
                    session_metadata = getattr(session, "metadata", {})
                    if not isinstance(session_metadata, dict):
                        session_metadata = {}
                    merchant_url = executor._usable_payment_gate_resume_url(
                        str(session_metadata.get("merchant_url") or payment_page_url or "")
                    )
                    ctx = {
                        "mgr": mgr,
                        "token": token,
                        "url": url,
                        "channel": channel,
                        "page_ref": session.page_ref,
                        "page": getattr(session, "page", None),
                        "page_url": merchant_url,
                        "merchant_url": merchant_url,
                        "order_summary": session.order_summary,
                    }
                else:
                    order_summary = checkpoint.context.get("payment_order_summary")
                    if not isinstance(order_summary, dict):
                        raise RuntimeError("Missing payment confirmation order summary")
                    page_ref = str(checkpoint.context.get("payment_page_ref") or "")
                    merchant_url = executor._usable_payment_gate_resume_url(payment_page_url)
                    token, url = mgr.create_session(
                        order_summary,
                        page_ref,
                        user_id=executor._get_effective_user_id(),
                        task_id=task_id,
                        channel=(getattr(channel, "channel_type", None) if channel is not None else None),
                        metadata=({"merchant_url": merchant_url} if merchant_url else None),
                    )
                    await mgr.ensure_cloud_synced(token)
                    ctx = {
                        "mgr": mgr,
                        "token": token,
                        "url": url,
                        "channel": channel,
                        "page_ref": page_ref,
                        "page_url": merchant_url,
                        "merchant_url": merchant_url,
                        "order_summary": order_summary,
                    }
                    checkpoint.context["confirm_token"] = token
                    checkpoint.context["confirmation_url"] = url
                    checkpoint.updated_at = datetime.now(UTC).isoformat()
                    save_checkpoint(checkpoint, user_id=user_id)

                executor._payment_confirmation_ctx = ctx
                executor._payment_gate_active = True
                _set_final_response(
                    executor,
                    pending_q or "Payment confirmation is still waiting for review.",
                    continue_listening=False,
                )
                asyncio.ensure_future(executor._run_payment_confirmation_wait(ctx))
                result = AgentResult(
                    ok=True,
                    answer=executor._final_answer,
                    iterations_used=0,
                    tools_called=[],
                    continue_listening=False,
                )
                result.payment_gate = True
                result.confirmation_url = str(ctx["url"])
                result.origin_channel = executor._get_origin_channel_type()
                return result
            except Exception:
                logger.exception("Payment gate checkpoint resume failed")
                mark_complete(checkpoint, "completed", "payment_gate")
                return AgentResult(
                    ok=False,
                    answer="The payment confirmation session is no longer available. Please start checkout again.",
                    iterations_used=0,
                    tools_called=[],
                    error="payment confirmation resume failed",
                )

        if _checkpoint_is_resuming_gate and pending_gate_type == "signature":
            checkpoint.status = "in_progress"
            checkpoint.context.pop("pending_gate_user_reply", None)
            checkpoint.context.pop("pending_question", None)
            checkpoint.context.pop("pending_gate_type", None)
            checkpoint.context.pop("_pre_resume_gate_type", None)
            checkpoint.updated_at = datetime.now(UTC).isoformat()
            executor._signature_gate_override_token = "%s:%s" % (
                checkpoint.task_id,
                checkpoint.updated_at,
            )
            executor._signature_gate_override_actions = _SIGNATURE_GATE_OVERRIDE_ACTIONS
            save_checkpoint(checkpoint, user_id=user_id)
            user_reply = (resume_user_reply or "").strip()
            if user_reply:
                continuation = (
                    "You were previously working on this task and paused at a legal signature gate. "
                    "The user has now confirmed: '%s'. The signature approval is granted for this task. "
                    "Resume from the exact page you left off on, sign, and continue. "
                    "%s"
                ) % (
                    user_reply,
                    _SIGNATURE_RESUME_PAYMENT_BOUNDARY,
                )
            else:
                continuation = (
                    "You were previously working on this task and paused at a legal signature gate. "
                    "Resume from the exact page you left off on and continue carefully. "
                    "%s" % _SIGNATURE_RESUME_PAYMENT_BOUNDARY
                )
        elif pending_q:
            continuation = (
                "You were previously working on this task and asked the user: '%s'. "
                "The user has returned. Continue from where you left off." % pending_q
            )
        else:
            continuation = (
                "You were previously working on this task and were interrupted. "
                "Your progress has been saved. Continue from where you left off."
            )

        # Call run with the continuation context
        return await executor.run(
            user_text=continuation,
            initial_tool_call=None,
            system_context="RESUMING TASK: %s\nSteps completed: %d"
            % (
                checkpoint.task_description,
                len(checkpoint.steps),
            ),
            existing_checkpoint=checkpoint,
            restored_native_messages=restored_messages,
            restored_continuity=checkpoint.llm_continuity,
        )

    @staticmethod
    def _format_prior_context_lines(
        messages: list[dict[str, str]] | None,
        *,
        max_messages: int = 6,
        max_chars: int = 300,
        compact: bool = False,
    ) -> list[dict[str, str]] | list[str]:
        """Format prior turns losslessly unless explicit compaction is requested."""

        if compact:
            return _compact_prior_context_lines(
                messages,
                max_messages=max_messages,
                max_chars=max_chars,
            )
        return _prior_context_role_messages(messages)

    @staticmethod
    def _contextualize_prior_messages(
        messages: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        """Keep prior role history intact; doctrine supplies past-context semantics."""

        return _prior_context_role_messages(messages)

    @staticmethod
    def _native_message_has_function_call_result(message: dict[str, Any], call_id: str) -> bool:
        """Return True if a native-history message already answers ``call_id``."""
        if message.get("type") == "function_call_output" and message.get("call_id") == call_id:
            return True
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and block.get("tool_use_id") == call_id:
                    return True
                if block.get("type") == "function_call_output" and block.get("call_id") == call_id:
                    return True
        if isinstance(content, dict) and content.get("_openai_assistant"):
            response_items = content.get("response_items")
            if isinstance(response_items, list):
                for item in response_items:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "function_call_output"
                        and item.get("call_id") == call_id
                    ):
                        return True
        return False

    @staticmethod
    def _native_message_has_function_call(message: dict[str, Any], call_id: str) -> bool:
        """Return True if a native-history message contains a function call with ``call_id``."""
        if message.get("type") == "function_call" and message.get("call_id") == call_id:
            return True
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "function_call" and block.get("call_id") == call_id:
                    return True
                if block.get("type") == "tool_use" and block.get("id") == call_id:
                    return True
        if isinstance(content, dict) and content.get("_openai_assistant"):
            tool_calls = content.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict) and tool_call.get("id") == call_id:
                        return True
            response_items = content.get("response_items")
            if isinstance(response_items, list):
                for item in response_items:
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        if item.get("call_id") == call_id or item.get("id") == call_id:
                            return True
        return False

    def _has_trailing_pending_function_call(self, call_id: str | None) -> bool:
        """Return True when the most recent native history has an unanswered call."""
        target = str(call_id or "").strip()
        if not target:
            return False
        for message in reversed(self._native_messages):
            if self._native_message_has_function_call_result(message, target):
                return False
            if self._native_message_has_function_call(message, target):
                return True
        return False

    def _append_synthetic_function_call_output(self, call_id: str) -> None:
        """Append a synthetic Responses function output for a pending native call."""
        output = json.dumps({"answer": self._final_answer or ""}, separators=(",", ":"), sort_keys=True)
        self._native_messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": output,
                        "is_error": False,
                    }
                ],
            }
        )

    async def continue_after_event(
        self,
        event_text: str,
        *,
        override_token: str | None = None,
        synthetic_tool_call_id: str | None = None,
    ) -> AgentResult:
        """Resume this executor after an external approval/rejection event.

        Prototype choice: ConfirmationSession retains a strong reference to
        the parent executor (Option B) so ``run()`` may return at PAYMENT_GATE
        unchanged while the confirmation waiter continues this same executor.
        The continuation reuses the same MCP hub, channel, provider, task_id,
        checkpoint, native history, and tool surface instead of creating a
        submit/re-entry child executor with a separate prompt.
        """
        if synthetic_tool_call_id and self._has_trailing_pending_function_call(synthetic_tool_call_id):
            self._append_synthetic_function_call_output(synthetic_tool_call_id)

        event_message = {"role": "user", "content": event_text}
        if self._use_native:
            self._native_messages.append(event_message)
        else:
            self._text_messages.append(event_message)

        self._payment_gate_active = False
        self._raw_final_answer = None
        self._final_answer = None
        self._final_command = None
        self._final_params = {}
        if override_token:
            self._payment_gate_override_token = override_token

        checkpoint = getattr(self, "_current_checkpoint", None)
        result = await self.run(
            event_text,
            initial_tool_call=None,
            system_context=getattr(self, "_system_context_text", "") or getattr(self, "_cached_system_context", ""),
            existing_checkpoint=(checkpoint if isinstance(checkpoint, TaskCheckpoint) else None),
            restored_native_messages=(self._native_messages if self._use_native else None),
            restored_continuity=(self._export_responses_continuity_state() if self._use_native else None),
            restored_text_messages=(self._text_messages if not self._use_native else None),
            restored_messages_include_user_text=True,
            conversation_history_source="unified_prompt_continuation",
            post_turn_memory_extraction=False,
        )
        checkpoint_for_memory = checkpoint if isinstance(checkpoint, TaskCheckpoint) else None
        await self._run_post_turn_memory_extraction(
            user_text=event_text,
            final_answer=str(result.answer or ""),
            task_id=str(
                getattr(checkpoint_for_memory, "task_id", "") or getattr(self, "task_id", "") or "continuation"
            ),
            tools_called=list(result.tools_called or []),
            model_visible_messages=self._post_turn_model_visible_messages(
                checkpoint=checkpoint_for_memory,
                final_answer=str(result.answer or ""),
            ),
        )
        return result

    @staticmethod
    def _post_turn_memory_transcript(
        user_text: str,
        final_answer: str,
        model_visible_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        if model_visible_messages:
            try:
                from services.memory.extract_memories import (
                    model_visible_messages_to_transcript,
                )

                transcript = model_visible_messages_to_transcript(model_visible_messages)
            except (ImportError, RuntimeError, TypeError, ValueError) as exc:
                logger.debug("Model-visible memory transcript fallback: %s", exc)
            else:
                if transcript.strip():
                    return transcript
        parts = []
        if str(user_text or "").strip():
            parts.append("user: %s" % str(user_text).strip())
        if str(final_answer or "").strip():
            parts.append("assistant: %s" % str(final_answer).strip())
        return "\n".join(parts)

    def _post_turn_model_visible_messages(
        self,
        *,
        checkpoint: TaskCheckpoint | None,
        final_answer: str,
    ) -> list[dict[str, Any]]:
        source_messages: list[dict[str, Any]] = []
        if checkpoint is not None and checkpoint.llm_messages:
            source_messages = checkpoint.llm_messages
        elif self._use_native and self._native_messages:
            source_messages = self._native_messages
        elif self._text_messages:
            source_messages = [dict(message) for message in self._text_messages]

        messages = copy.deepcopy(source_messages)
        clean_final = str(final_answer or "").strip()
        if clean_final:
            tail_text = ""
            if messages and isinstance(messages[-1], dict):
                try:
                    tail_text = json.dumps(messages[-1].get("content"), default=str, ensure_ascii=False)
                except (TypeError, ValueError):
                    tail_text = str(messages[-1].get("content") or "")
            if clean_final not in tail_text:
                messages.append({"role": "assistant", "content": clean_final})

        task_id = str(getattr(checkpoint, "task_id", "") or getattr(self, "task_id", "") or "")
        visible_index = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("type") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            visible_index += 1
            message.setdefault(
                "_memory_cursor",
                {
                    "task_id": task_id,
                    "visible_index": visible_index,
                },
            )
        return messages

    async def _run_post_turn_memory_extraction(
        self,
        *,
        user_text: str,
        final_answer: str,
        task_id: str,
        tools_called: list[str],
        model_visible_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if getattr(self, "_current_agent_id", None):
            return
        transcript = self._post_turn_memory_transcript(user_text, final_answer, model_visible_messages)
        if not transcript.strip():
            return
        try:
            from services.memory.extract_memories import extract_memories_after_turn

            await extract_memories_after_turn(
                user_id=str(self._get_effective_user_id()),
                transcript=transcript,
                task_id=task_id,
                tools_called=tools_called,
                model_visible_messages=model_visible_messages,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.debug("Post-turn memory extraction skipped: %s", exc)

    async def run(
        self,
        user_text: str,
        initial_tool_call: ToolCall | None,
        system_context: str = "",
        prior_turns: list[dict[str, str]] | None = None,
        task_category_override: str | None = None,
        existing_checkpoint: TaskCheckpoint | None = None,
        restored_native_messages: list[dict[str, Any]] | None = None,
        restored_continuity: dict[str, Any] | None = None,
        restored_text_messages: list[dict[str, str]] | None = None,
        restored_messages_include_user_text: bool = False,
        initial_response: dict[str, Any] | None = None,
        conversation_history_source: str | None = None,
        post_turn_memory_extraction: bool = True,
        **legacy_kwargs: Any,
    ) -> AgentResult:
        """Execute the agent loop.

        Args:
            user_text: Original user request text
            initial_tool_call: First tool call parsed from LLM response
            system_context: System context string for LLM prompts
            prior_turns: Prior conversation turns from the
                conversational mode loop.  Each entry is
                ``{"role": "user"|"assistant", "content": "..."}``.
            task_category_override: Deprecated compatibility parameter. It is
                ignored because production routing always uses all visible tools.
            existing_checkpoint: Optional checkpoint to continue updating.
            restored_native_messages: Optional checkpoint-restored native history.
            restored_continuity: Optional checkpoint-restored continuity metadata.
            restored_text_messages: Optional checkpoint-restored text history.
            restored_messages_include_user_text: Whether restored history already
                includes ``user_text`` as its trailing user event.
            initial_response: Optional original provider response for the initial tool call.
            conversation_history_source: Trace label describing where
                ``prior_turns`` came from.
            post_turn_memory_extraction: Internal gate-resume control.  Normal
                runs extract after the canonical loop returns; continuation
                callers suppress the nested call and extract at the resume
                boundary to avoid double scheduling.

        Returns:
            AgentResult with final answer/command and execution metadata
        """
        del task_category_override

        from diagnostics import latency_spans

        latency_spans.event("EXECUTOR_RUN_BEGIN", task_id=str(getattr(self, "task_id", "?")))

        # F-030 (R3-A): Legacy ``conversation_history`` kwarg lane removed.
        # The canonical loop accepts only ``prior_turns`` (still kept for
        # trace/bundle continuity), and the agent loop itself accepts only
        # the typed ``PromptFrameBundle``. Reject the old keyword loudly so
        # callers that still pass it are surfaced rather than silently
        # routed onto the dead alternate lane.
        if "conversation_history" in legacy_kwargs:
            raise TypeError(
                "AgentExecutor.run no longer accepts 'conversation_history'; "
                "convert prior turns into the PromptFrameBundle and pass "
                "prior_turns= only for trace metadata"
            )
        if legacy_kwargs:
            unexpected = ", ".join(sorted(legacy_kwargs))
            raise TypeError("unexpected AgentExecutor.run keyword(s): %s" % unexpected)
        # C5: Emergency killswitch â€” disable the entire agent loop via env var

        if os.environ.get("VIOLA_DISABLE_AGENT_LOOP", "").lower() in (
            "true",
            "1",
            "yes",
        ):
            return AgentResult(
                ok=False,
                answer="Agent loop is currently disabled. Set VIOLA_DISABLE_AGENT_LOOP=false to re-enable.",
                iterations_used=0,
                tools_called=[],
            )

        # F-001 (R3-A): ``system_context`` is a str-only contract that flows
        # into ``_system_context_text`` and then through ``.encode()`` to
        # compute the system-context hash. A truthy non-string (notably the
        # ``PromptFrameBundle`` that the direct-agent path used to pass as
        # the third positional argument) used to crash this path before the
        # first model turn. Claude keeps ``systemPrompt``, ``systemContext``,
        # and ``messages`` as distinct fields in query state
        # (``src/query.ts:252-279``); fail closed on a type mismatch instead
        # of letting a structured bundle silently land in the string slot.
        if not isinstance(system_context, str):
            raise TypeError(
                "AgentExecutor.run expects system_context: str but got %s. "
                "PromptFrameBundle / structured context must be passed via "
                "the constructor's context_bundle= argument, not as a "
                "positional/keyword system_context override." % type(system_context).__name__
            )

        start_time = time.monotonic()
        tools_called: list[str] = []
        tools_called_args: list[str] = []  # JSON-serialized args for diversity check
        iterations = 0

        self._agent_plan_policy = self._resolve_agent_plan_policy()
        self._apply_agent_chain_cap(self._agent_plan_policy, user_text)

        managed_budget_gate = await self._check_managed_llm_spend_cap_async()
        if not managed_budget_gate.allowed:
            self._record_agent_gate_denial_telemetry(
                gate_name="managed_llm_spend_cap",
                current_usage=managed_budget_gate.spent_cents,
                limit_value=managed_budget_gate.budget_cents,
            )
            return AgentResult(
                ok=False,
                answer=self._managed_llm_budget_message(managed_budget_gate),
                iterations_used=0,
                tools_called=[],
                error=managed_budget_gate.reason or "managed LLM spend cap reached",
                cap_state=managed_budget_gate.cap_state,
            )

        # Auto-approve all CONFIRM-level tools for this agent execution.
        # DANGEROUS tools still require explicit approval.
        # Financial safety is enforced by PAYMENT_GATE, not per-tool prompts.
        from mcp_hub.approval_bridge import AGENT_AUTO_APPROVE

        self._approval.add_pre_approved(AGENT_AUTO_APPROVE)
        logger.debug(
            "Agent run: auto-approved %d CONFIRM tools: %s",
            len(AGENT_AUTO_APPROVE),
            sorted(AGENT_AUTO_APPROVE),
        )

        # Initialize task log
        task_id = existing_checkpoint.task_id if existing_checkpoint is not None else generate_task_id()
        self.task_id = task_id
        task_log = AgentTaskLog(
            task_id=task_id,
            user_text=user_text,
            started_at=datetime.now(tz=UTC).isoformat(),
        )
        self._step_log_jsonl_path = self._build_structured_step_log_path(task_log.started_at)
        self._step_log_metadata_emitted = False
        self._last_model_name = self._get_model_name_safe()
        self._token_tracker.configure(
            model=self._last_model_name,
            context_window=self._get_context_window_safe(),
        )

        # Initialize checkpoint for crash recovery / resume continuity.
        checkpoint = existing_checkpoint or TaskCheckpoint(
            task_id=task_id,
            task_description=user_text[:200],
            parent_task_id=self._parent_task_id,
        )
        checkpoint.context = dict(checkpoint.context or {})
        existing_trace_path = checkpoint.context.get("task_trace_path")
        # Lane A (#464): the writer's serialize/blob/encrypt/flush is offloaded
        # off the turn's answer path via the write-behind wrapper. Every
        # ``self._task_trace.append_*`` below enqueues onto an ordered background
        # tail instead of running the CPU/I-O synchronously on the event loop.
        self._task_trace = AsyncTaskTraceWriter(
            TaskTraceWriter.for_task(
                task_id=task_id,
                user_scope=self._get_effective_user_id(),
                started_at=task_log.started_at,
                session_id=self._session_id,
                explicit_path=existing_trace_path,
            )
        )
        checkpoint.context["task_trace_path"] = str(self._task_trace.path)
        checkpoint.context["task_trace_schema_version"] = self._task_trace.schema_version
        self._task_trace_last_request = None
        self._task_trace_last_response = None
        self._task_trace_last_continuity_before = None
        self._task_trace_last_continuity_after = None
        self._task_trace_last_tool_execution = None
        self._task_trace_llm_attempt_seq = 0
        self._task_trace_last_attempt_id = None
        self._task_trace_last_provider_metadata = None
        # Store checkpoint on self so _handle_spawn_subtask can link children
        self._current_checkpoint = checkpoint

        # Initialize spin detector
        spin_detector = SpinDetector()

        # Layer 6 (taint gate) lives in run_agent_loop, which constructs the
        # tracker and restores cross-turn taint from history (SEC-003). A
        # second, unused tracker here was the "defined-but-unwired security
        # control" shape — removed in the SEC-001 fix.

        # Item 2: Session memory for structured tool-call context
        # INT-14: SessionMemory is scoped to the executor's user_id so that
        # logs + any future shared storage have auditable ownership.
        session_memory = None
        session_memory_user_id = str(self._user_id or "").strip()
        if session_memory_user_id:
            try:
                from services.conversation.session_memory import SessionMemory

                session_memory = SessionMemory(
                    user_id=session_memory_user_id,
                    session_id=str(task_id),
                )
                session_memory.set_task_description(user_text[:200])
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("SessionMemory construction failed: %s", exc)
                session_memory = None
        else:
            logger.debug("SessionMemory disabled without executor user_id")
        # Item 3: Auto-retry tracker (keyed by (tool_name, args_json))
        _retry_tracker: dict[tuple[str, str], int] = {}
        # Item 5: Progress summary tracking
        _tool_summaries: list[str] = []
        _summary_count = 0

        self._last_page_url = None
        self._payment_gate_active = False
        self._payment_confirmation_ctx = None
        self._payment_gate_requested = False
        self._last_payment_review_request = None
        self._last_payment_review_page_url = None
        self._signature_gate_active = False
        self._signature_gate_requested = False
        self._overlay_shown = False
        self._agent_task_description = user_text[:120]
        self._original_command = user_text
        system_context_components = self._sdk_probe_attr(self._llm, "_system_context_components")
        if not isinstance(system_context_components, Mapping):
            system_context_components = {}
        self._system_context_components = dict(system_context_components)
        self._step_approval_path = None
        self._step_approval_tool = None
        # Fix 3 + Fix 4: reset per-run counters/state for new task
        self._rejected_tools = set()
        self._memory_all_scope_no_match_queries = []
        self._memory_enumeration_blocked_after_no_match = False
        self._consecutive_click_failures = 0
        self._irreversible_confirmed_action_classes = set()
        # Compatibility hook for older web_search counter code. The current
        # path keeps web routing model-directed.
        from intent.tools.web_search import clear_web_search_counter

        clear_web_search_counter()

        # Historical log metadata only. Production does not classify tasks or
        # select focused tool sets; the model receives the full visible surface.
        self._task_category = "general"
        _session_cost_token = None
        # Register as active executor (for WS cancel handler)
        _set_active_executor(self, keys=self._get_registry_keys())
        try:
            from services.llm.session_cost_tracker import (
                get_session_cost_tracker,
                set_active_session_cost_session,
            )

            _cost_session_id = str(getattr(self, "_session_id", "") or task_id)
            _cost_user_id = self._user_id or None
            self._session_cost_session_id = _cost_session_id
            self._session_cost_user_id = _cost_user_id
            _session_cost_token = set_active_session_cost_session(_cost_session_id, user_id=_cost_user_id)
            get_session_cost_tracker(user_id=_cost_user_id, session_id=_cost_session_id).start_session(
                _cost_session_id,
                restore=True,
            )
        except (
            AttributeError,
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            logger.debug("Session cost tracker binding failed, continuing", exc_info=True)

        # Track agent activity for smart-stop
        try:
            from core.activity_tracker import ACTIVITY_AGENT, get_activity_tracker

            get_activity_tracker().record_start(ACTIVITY_AGENT)
        except Exception:
            logger.debug("Activity tracker record_start failed, continuing")

        # Telemetry: record agent task start
        try:
            from admin.instrumentation import record_agent_task_start

            record_agent_task_start()
        except Exception:
            logger.debug("Telemetry record_agent_task_start failed, continuing")

        # Reset retry state for each run
        self._retry_count = 0
        self._parse_failed = False
        self._agent_chain_metrics_recorded = False
        self._llm_error_occurred = False
        self._compaction_count = 0  # C1: auto-compaction counter
        self._compaction_no_progress_streak = 0
        self._llm_cumulative_input_tokens = 0
        self._llm_cumulative_output_tokens = 0
        self._llm_cumulative_total_tokens = 0
        self._llm_total_input_tokens_seen = 0
        self._llm_total_output_tokens_seen = 0
        self._llm_last_input_tokens = 0
        self._llm_last_output_tokens = 0
        self._llm_context_usage_pct = 0.0
        self._last_server_context_tokens = 0
        self._responses_compaction_failure_streak = 0
        self._virtual_message_count = 0
        self._step_ceiling_compaction_attempted = False
        self._tool_progress_repetition_counts = {}
        self._tool_progress_seen_urls = set()
        self._tool_progress_last_fingerprint = {}
        self._tool_progress_consecutive_identical = {}
        self._tool_progress_script_probe_state = {"page_url": "", "count": 0}
        self._consecutive_bot_protection_signals = 0
        self._stuck_tool_compaction_attempted = False
        self._pending_stuck_tool_progress = None
        self._diagnostic_contexts = []
        self._consecutive_failures = 0
        self._approval_blocked_tools = []

        # Runtime telemetry: track spin interventions for structured logs.
        self._pending_spin_intervention: str | None = None
        # D1: Waste detector for low-output agent iterations
        self._consecutive_low_output: int = 0

        # C2: Memoize system context per RUN (turn); reuse across iterations
        # within this turn. MUST overwrite any cache from a prior run() invocation
        # — prior code used `if cache is None` which left turn 1's cache in place
        # for turn 2 and later, suppressing per-turn context updates (e.g. the
        # GATE_STATE frame surfaced by build_pending_gate_frames when a prior
        # turn ended with a waiting signature/payment checkpoint). LLC live
        # Keep the per-run context cache fresh so a resumed turn cannot inherit
        # stale state from an earlier invocation.
        self._cached_system_context = system_context

        # Runtime telemetry: capture the full information surface.
        self._system_context_text = system_context or ""
        full_prompt_text = str(
            self._request_system_prompt_text
            or getattr(self._llm, "_agent_system_prompt", "")
            or self._system_context_text
        )
        self._system_prompt_text = full_prompt_text
        self._system_prompt_hash = (
            hashlib.sha256(full_prompt_text.encode()).hexdigest()[:16] if full_prompt_text else ""
        )
        self._full_prompt_hash = self._system_prompt_hash
        self._system_context_hash = (
            hashlib.sha256(self._system_context_text.encode()).hexdigest()[:16] if self._system_context_text else ""
        )
        self._system_prompt_len = len(full_prompt_text)
        self._prompt_variant_full = "unified"
        self._last_tool_choice = "auto"
        self._last_message_count = 0
        self._snapshot_compressed_this_step = False
        self._last_llm_reasoning = ""
        self._last_no_result_reasoning = ""
        self._forced_tool_failure_error = None
        self._forced_tool_failure_outcome = None

        # Runtime telemetry: capture step-log fields.
        # These use a "pending" pattern: set during current iteration,
        # captured on the NEXT step's JSONL record.
        self._pending_correction_msgs: list[str] | None = None
        self._pending_consecutive_warning: str | None = None
        self._pending_error_registry_ctx: str | None = None
        self._pending_hard_loop_breaker: str | None = None
        self._pending_force_termination: str | None = None
        self._pending_compaction_meta: dict[str, Any] | None = None
        self._pending_trim_stats: dict[str, int] | None = None
        # Per-step flags (reset each tool call, captured on current step)
        self._step_had_screenshot: bool = False
        self._step_snapshot_pre_len: int = 0
        # Prior conversation context (step 1 only)
        self._prior_conversation_turns = len(prior_turns) if prior_turns else 0
        self._prior_conversation_summary = ""
        self._prior_conversation_messages = copy.deepcopy(prior_turns or [])
        self._prior_conversation_source = conversation_history_source or (
            "conversation_history" if prior_turns else "none"
        )
        if prior_turns:
            self._prior_conversation_summary = "\n".join(
                self._format_prior_context_lines(
                    prior_turns,
                    max_messages=6,
                    max_chars=200,
                    compact=True,
                )
            )

        # B3: set settle info on provider so it can call settle() after
        # each API response with actual token counts.
        if hasattr(self._llm, "_settle_user_id"):
            from config.settings import settings as _settings

            if self._user_id:
                self._llm._settle_user_id = self._user_id
            else:
                from core.user_context import get_device_user_id

                # mt-ok: anonymous-bucket settle ID for desktop pre-auth spend tracking
                self._llm._settle_user_id = get_device_user_id()
            self._llm._settle_estimated_tokens = _settings.llm_max_tokens_cap

        # Runtime telemetry: record the prompt variant for structured logs.
        self._prompt_variant = "native" if self._use_native else "text"

        # Build conversation history for multi-turn
        # Text-based path uses simple {role, content} string messages.
        # Prepend accumulated conversation history so the agent has full context
        # from prior conversational turns (e.g. "My name is X" â†’ "File my LLC").
        raw_prior: list[dict[str, str]] = list(prior_turns) if prior_turns else []
        prior: list[dict[str, str]] = self._contextualize_prior_messages(raw_prior)
        if raw_prior and restored_native_messages is None and restored_text_messages is None:
            self._prompt_context_bundle = _bundle_with_prior_context_frames(
                self._prompt_context_bundle,
                _prior_context_frames(raw_prior, session_id=self._session_id, task_id=task_id),
            )
        if restored_text_messages is not None:
            messages: list[dict[str, str]] = copy.deepcopy(restored_text_messages)
            if (user_text and not restored_messages_include_user_text) or not messages:
                messages.append({"role": "user", "content": user_text})
        else:
            messages = prior + [
                {"role": "user", "content": user_text},
            ]
        self._text_messages = messages

        # Native path: structured Anthropic-format messages. Prior context
        # remains role-typed; renderer normalization handles text blocks.
        self._responses_continuity = {}
        if self._use_native:
            if restored_native_messages is not None:
                self._native_messages = copy.deepcopy(restored_native_messages)
                # Resume-time compaction: the restored history from a paused
                # task can be enormous (e.g. LLC turn 1 with 45 steps and
                # 30+ browser snapshots -> 300KB+ message chain). Without
                # proactive compaction the resumed loop's first LLM call
                # hits a 400 BadRequest from payload bloat before any tool
                # can run. TS parity: compact.ts triggers compaction once
                # the message budget is exceeded; here we trigger it
                # eagerly because the FIRST call after restore IS the bloat
                # point. compact_native_messages is a no-op when the
                # budgeted message count is small (<=6).
                try:
                    compacted, _compaction_meta = await compact_native_messages(
                        self._native_messages,
                        user_id=self._user_id or "",
                        session_id=self._session_id,
                    )
                    if compacted:
                        self._native_messages = compacted
                except Exception:
                    logger.exception("Resume-time compaction failed (non-fatal); proceeding with raw restored history")
                self._restore_responses_continuity_state(restored_continuity)
                if user_text and not restored_messages_include_user_text:
                    self._native_messages.append({"role": "user", "content": user_text})
            else:
                prior_messages = copy.deepcopy(prior)
                if initial_tool_call is not None:
                    # Pre-classified path: include the initial assistant response
                    self._native_messages = [
                        *prior_messages,
                        {"role": "user", "content": user_text},
                        {
                            "role": "assistant",
                            "content": _normalize_content_blocks(initial_tool_call.raw_content),
                        },
                    ]
                    if isinstance(initial_response, dict):
                        self._restore_responses_continuity_state(
                            self._extract_responses_continuity_from_response(initial_response),
                        )
                        self._acknowledge_native_assistant_message()
                else:
                    # Direct agent path: prior messages plus the current user
                    # request; the loop appends the provider response.
                    self._native_messages = [
                        *prior_messages,
                        {"role": "user", "content": user_text},
                    ]

        initial_loop_messages = (
            copy.deepcopy(self._native_messages) if self._use_native and restored_native_messages is not None else None
        )
        self._initial_model_messages = copy.deepcopy(self._native_messages if self._use_native else messages)
        if self._task_trace is not None:
            # ASYNC-1 followup (P1): pre-resolve trace + blob Fernet keys via the
            # async key chain BEFORE the first sync ``append_*`` / blob ``put``.
            # Without this warm-up, ``_apply_blob_refs_and_caps`` -> blob.put
            # -> KeyProvider.unwrap_trace_key -> _resolve_maybe_awaitable ->
            # run_async_synchronously trips the cross-loop guard on the
            # cloud's main loop and aborts the agent loop with
            # ``RuntimeError("Cannot synchronously wait on the shared
            # asyncio worker loop from itself")``. After warm-up, the sync
            # hot-path uses the cached Fernet on TaskTrace + BlobStore.
            try:
                await self._task_trace.warm_up_keys_async()
            except Exception as _warm_exc:
                # LOUD, never a warning buried in the log: losing the trace
                # loses this run's oracle and strips trace context off any bug
                # report the user files afterwards (#4793). ``disable`` also
                # drops a durable marker beside the missing trace.
                logger.exception(
                    "trace key warm-up FAILED; task trace disabled for this run (task_id=%s): %s",
                    getattr(self._task_trace, "task_id", "unknown"),
                    _warm_exc,
                    extra={
                        "event": "task_trace_key_warmup_failed",
                        "task_id": getattr(self._task_trace, "task_id", "unknown"),
                        "error_type": type(_warm_exc).__name__,
                    },
                )
                disable_trace = getattr(self._task_trace, "disable", None)
                if callable(disable_trace):
                    disable_trace("key_warmup_failed: %s: %s" % (type(_warm_exc).__name__, _warm_exc))
            trace_request = {
                "user_text": getattr(self, "_original_command", ""),
                "system_prompt_text": getattr(self, "_system_prompt_text", ""),
                "system_context_text": getattr(self, "_system_context_text", ""),
                "system_context_components": getattr(self, "_system_context_components", {}),
                "prior_conversation_messages": getattr(self, "_prior_conversation_messages", []),
                "prior_conversation_source": getattr(self, "_prior_conversation_source", "none"),
                "prior_conversation_summary": getattr(self, "_prior_conversation_summary", ""),
                "prior_conversation_turns": getattr(self, "_prior_conversation_turns", 0),
                "initial_model_messages": self._initial_model_messages,
            }
            trace_runtime_tool_schemas = self._build_tool_schema_snapshot()
            trace_runtime = {
                "route_mode": "native" if self._use_native else "text",
                "provider_class": type(self._llm).__name__,
                "model_name": self._get_model_name_safe(),
                "temperature": self._get_temperature_safe(),
                "task_category": getattr(self, "_task_category", "general"),
                "prompt_variant": getattr(self, "_prompt_variant", "unknown"),
                "prompt_variant_full": getattr(self, "_prompt_variant_full", "unknown"),
                "system_prompt_hash": getattr(self, "_system_prompt_hash", ""),
                "full_prompt_hash": getattr(self, "_full_prompt_hash", getattr(self, "_system_prompt_hash", "")),
                "system_context_hash": getattr(self, "_system_context_hash", ""),
                "system_prompt_len": getattr(self, "_system_prompt_len", 0),
                "tool_surface": self._serialize_tool_surface_for_trace(self._get_runtime_tool_surface()),
                "tool_schemas": trace_runtime_tool_schemas,
                "tool_schema_hash": self._hash_tool_schema_snapshot(trace_runtime_tool_schemas),
                "tool_schema_fingerprint": self._build_tool_schema_fingerprint(trace_runtime_tool_schemas),
                "allowed_tools": (sorted(self._allowed_tools) if self._allowed_tools else []),
                "rejected_tools": (sorted(self._rejected_tools) if self._rejected_tools else []),
            }
            trace_exists = self._task_trace.path.exists() and self._task_trace.path.stat().st_size > 0
            if existing_checkpoint is not None and trace_exists:
                self._task_trace.append_resume(
                    ts=datetime.now(tz=UTC).isoformat(),
                    request=trace_request,
                    continuity=self._build_trace_continuity_before(
                        self._export_responses_continuity_state() if self._use_native else {}
                    ),
                    checkpoint={
                        "status": checkpoint.status,
                        "updated_at": checkpoint.updated_at,
                        "step_index": checkpoint.step_index,
                    },
                )
            else:
                self._task_trace.append_start(
                    started_at=task_log.started_at,
                    request=trace_request,
                    runtime=trace_runtime,
                    continuity=self._build_trace_continuity_before(
                        self._export_responses_continuity_state() if self._use_native else {}
                    ),
                    initial_response=(copy.deepcopy(initial_response) if isinstance(initial_response, dict) else None),
                )

        # Step 1 executes an already-routed tool call. Seed the step log with
        # the metadata for that pre-loop routing call so the first JSONL record
        # doesn't inherit constructor defaults or the next turn's values.
        if initial_tool_call is not None:
            _step1_tc = getattr(self._llm, "_agent_tool_choice", None)
            if not isinstance(_step1_tc, str) or not _step1_tc:
                _step1_tc = "auto"
            self._set_step_log_llm_context(
                tool_choice=_step1_tc,
                message_count=len(messages),
            )
            if isinstance(initial_response, dict):
                if self._use_native:
                    _seed_messages = copy.deepcopy(self._initial_model_messages[:-1] or self._initial_model_messages)
                    self._record_task_trace_llm_exchange(
                        call_kind="initial_response",
                        request_mode="native",
                        response=initial_response,
                        first_turn=True,
                        continuity_before=self._build_trace_continuity_before({}),
                        native_messages=_seed_messages,
                        native_kwargs={},
                    )
                else:
                    self._record_task_trace_llm_exchange(
                        call_kind="initial_response",
                        request_mode="text",
                        response=initial_response,
                        first_turn=True,
                        continuity_before=self._build_trace_continuity_before({}),
                        text_history=copy.deepcopy(messages[:-1]),
                        text_input=messages[-1]["content"] if messages else "",
                        system_context=system_context,
                    )

        # LA-3: Reset and seed the token budget tracker with initial messages.
        self._token_tracker.reset()
        if self._use_native:
            self._token_tracker.add_messages(self._native_messages)
        else:
            self._token_tracker.add_messages(messages)
        # Include system context in the budget estimate
        self._token_tracker.add_message({"role": "system", "content": system_context})

        logger.debug(
            "Agent loop starting: task_id=%s, native=%s, system_context_len=%d, initial_tool=%s",
            task_id,
            self._use_native,
            len(system_context),
            initial_tool_call.tool if initial_tool_call is not None else "none",
        )

        # Keep agent startup silent; user-facing updates come from explicit
        # answer/approval/confirmation flows, not canned progress narration.

        # Process the initial tool call
        current_tool_call: ToolCall | None = initial_tool_call
        outcome = "success"
        # Register spawn_subtask callback for the core-tools MCP server.
        # The callback is a closure that captures the current checkpoint.
        _spawn_subtask_token = None
        from mcp_servers.core_tools.server import (
            reset_spawn_subtask_callback,
            set_spawn_subtask_callback,
        )

        async def _spawn_callback(task: str, max_steps: int, return_format: str) -> str:
            result = await self._run_child_agent(task, max_steps, return_format, checkpoint)
            return _child_answer_text(result)

        _spawn_subtask_token = set_spawn_subtask_callback(_spawn_callback)

        # Orchestrator tool visibility is filtered per-request in
        # _get_request_native_tools based on self._depth, instead of mutating
        # the shared mcp_hub state (hide_tools is one-way).

        # Delegation tools are now available for all task categories including web.
        # The agent decides when delegation is appropriate.
        self._web_hidden_tools: list[str] = []

        try:
            from intent.agent_loop import run_agent_loop

            provider = self._sdk_dispatch_provider() or self._llm
            logger.info(
                "Agent dispatch: canonical loop provider=%s task_id=%s",
                type(self._llm).__name__,
                getattr(self, "task_id", "?"),
            )
            # F-030 (R3-A): The agent loop accepts only the canonical
            # PromptFrameBundle. Prior conversation turns are already
            # represented inside the bundle via the typed conversation state
            # manager; they must not be re-injected as a separate
            # ``conversation_history`` lane. Claude normalizes one message
            # chain (``src/query.ts:252-279``); the legacy alternate lane
            # bypassed frame-chain normalization, system-reminder wrapping,
            # compaction metadata, and provider repair.
            from diagnostics import latency_spans

            with latency_spans.span(
                "AGENT_LOOP",
                task_id=str(getattr(self, "task_id", "?")),
                provider=type(provider).__name__,
            ):
                agent_result = await run_agent_loop(
                    executor=self,
                    provider=provider,
                    task_log=task_log,
                    user_text=user_text,
                    system_prompt=getattr(self, "_system_prompt_text", "") or "",
                    checkpoint=checkpoint,
                    prompt_context_bundle=getattr(self, "_prompt_context_bundle", None),
                    initial_tool_call=current_tool_call,
                    initial_messages=initial_loop_messages,
                    start_time=start_time,
                )
            if post_turn_memory_extraction:
                await self._run_post_turn_memory_extraction(
                    user_text=user_text,
                    final_answer=str(agent_result.answer or ""),
                    task_id=task_id,
                    tools_called=list(agent_result.tools_called or []),
                    model_visible_messages=self._post_turn_model_visible_messages(
                        checkpoint=checkpoint,
                        final_answer=str(agent_result.answer or ""),
                    ),
                )
            return agent_result

        except asyncio.CancelledError:
            outcome = "cancelled"
            logger.warning(
                "Agent loop cancelled (asyncio.CancelledError) after %d iterations",
                iterations,
            )
            # M7 Bug B: repair orphaned tool_use blocks before saving
            # checkpoint, so a resumed session starts with clean history.
            if self._use_native:
                self._sanitize_native_messages_for_api(trim_history=False)
            self._snapshot_checkpoint_state(checkpoint, messages)
            checkpoint.status = "failed"
            save_checkpoint(checkpoint)
            # C6: Lifecycle hook â€” task failed (CancelledError)
            _dispatch_hook("task_failed", reason=outcome, user_id=self._user_id)
            self._finalize_log(task_log, outcome, start_time)
            # Cancellation MUST propagate. Returning a value here swallows it,
            # and a swallowed CancelledError does not merely lose an exception:
            # it makes the canceller believe the work finished normally.
            #
            # Measured on this repo's Python 3.11.9: asyncio.wait_for around a
            # coroutine that catches CancelledError and returns RETURNS that
            # value instead of raising TimeoutError. So the relay's own
            # "Desktop handler timed out" branch (services/companion_client/
            # client.py) was unreachable, and this cheerful string was handed
            # back to the cloud as the desktop's real answer AND written into
            # the desktop's idempotency ledger as a completed turn -- which is
            # how "Got it, I stopped. I saved the checkpoint..." came back
            # verbatim for later, unrelated requests (2026-08-02 relay runs).
            #
            # A user who presses stop does NOT reach here: that path returns
            # normally from run_agent_loop (intent/agent_loop.py, the
            # executor._cancelled branch) with "Task cancelled." This handler
            # is for cancellation imposed from OUTSIDE the loop, and the
            # honest thing to tell the outside is that it was cancelled.
            raise
        except Exception as exc:
            outcome = "error"
            # M7 Bug B: repair orphaned tool_use blocks before saving
            # checkpoint, so a resumed session starts with clean history.
            if self._use_native:
                try:
                    self._sanitize_native_messages_for_api(trim_history=False)
                except Exception:
                    logger.debug("M7 sanitize in exception handler failed, continuing")
            # Leave checkpoint resumable for crash recovery
            self._snapshot_checkpoint_state(checkpoint, messages)
            checkpoint.status = "failed"
            save_checkpoint(checkpoint)
            # C6: Lifecycle hook â€” task failed (error)
            _dispatch_hook("task_failed", reason=outcome, user_id=self._user_id)
            self._finalize_log(task_log, outcome, start_time)
            self._capture_diagnostic(
                user_text=user_text,
                exception=exc,
                stage="agent_loop",
                iteration=iterations,
            )
            logger.exception(
                "Agent loop failed after %d iterations: %s",
                iterations,
                exc,
            )
            # Auto-file a bug report so Viola tracks her own failures
            try:
                from intent.tools.bug_reporting import file_bug_report_handler

                asyncio.ensure_future(
                    file_bug_report_handler(
                        "Auto-report: agent loop failed on '%s' after %d steps: %s"
                        % (user_text[:100], iterations, type(exc).__name__)
                    )
                )
            except Exception:
                logger.debug("Auto-bug-report failed, continuing")
            return AgentResult(
                ok=False,
                answer="Something went sideways - I saved progress so the task can be retried.",
                iterations_used=iterations,
                tools_called=tools_called,
                error="Agent loop error: %s" % type(exc).__name__,
            )
        finally:
            # Remove agent-scoped auto-approval
            self._approval.clear_pre_approved(AGENT_AUTO_APPROVE)

            # Clean up browser after agent loop completes (skip if a gate is active â€”
            # user needs to see the browser to review/sign/pay)
            if not self._payment_gate_active and not self._signature_gate_active:
                await self._cleanup_browser()
                # Determine outcome for toast
                hide_outcome = (
                    "cancelled"
                    if self._cancelled
                    else ("error" if outcome == "error" or outcome == "llm_error" else "done")
                )
                self._hide_overlay(outcome=hide_outcome)
            elif self._signature_gate_active:
                self._update_overlay_status("signature_review")
                self._update_overlay_phase("user_input")
            else:
                # Payment gate: keep overlay visible but update status + phase
                self._update_overlay_status("payment_review")
                self._update_overlay_phase("user_input")
                payment_ctx = getattr(self, "_payment_confirmation_ctx", None)
                if payment_ctx is not None:
                    # mt-ok: executor self is per-user-task; ctx carries user_id
                    asyncio.ensure_future(self._run_payment_confirmation_wait(payment_ctx))
                else:
                    asyncio.ensure_future(self._start_payment_stream())

            # Track agent activity stopped
            try:
                from core.activity_tracker import ACTIVITY_AGENT, get_activity_tracker

                get_activity_tracker().record_stop(ACTIVITY_AGENT, user_id=self._user_id)
            except Exception:
                logger.debug("Activity tracker record_stop failed, continuing")

            # Clear spawn_subtask callback
            if _spawn_subtask_token is not None:
                reset_spawn_subtask_callback(_spawn_subtask_token)

            # Restore any tools hidden for this web task
            for _tool in getattr(self, "_web_hidden_tools", []):
                self._mcp_hub._hidden_tools.discard(_tool)

            # Clear active executor singleton
            _set_active_executor(self, keys=self._get_registry_keys(), remove=True)
            if _session_cost_token is not None:
                try:
                    from services.llm.session_cost_tracker import (
                        reset_active_session_cost_session,
                    )

                    reset_active_session_cost_session(_session_cost_token)
                except (
                    AttributeError,
                    ImportError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    logger.debug(
                        "Session cost tracker binding reset failed, continuing",
                        exc_info=True,
                    )

            # Lane C (#466, sharpened #493): the terminal "done/error" progress
            # broadcast is a UI notification, not part of the answer. Schedule it
            # as the LAST act of teardown -- after every remaining awaited step in
            # this finally (browser cleanup, gate-card dispatch, TTS) -- so the
            # fire-and-forget coroutine is not handed to the loop until run() is
            # about to return. Scheduled earlier (its old position, before
            # ``await self._cleanup_browser()``), the coroutine interleaved with
            # those awaits and ran INSIDE the SERVER_ROOT answer window, adding
            # ~130 ms of wall time on the answer path (measured during
            # verification). From here it lands after the answer unwinds. The
            # broadcast itself is unchanged -- the UI still receives the terminal
            # event (and its trace progress row is still appended), just after the
            # user's answer rather than before it.
            terminal_status = self._agent_progress_terminal_status(outcome)
            schedule_post_answer_coro(
                "terminal_progress",
                self._broadcast_terminal_agent_progress(
                    status=terminal_status,
                    step_number=iterations,
                ),
                owner_set=self._post_answer_teardown_tasks,
            )

    def _emit_agent_stop_event(
        self,
        *,
        task_id: str,
        outcome: str,
        reason: str,
        iterations: int,
        limit: int,
        tools_called: list[str],
    ) -> None:
        """Emit a structured event for deterministic loop stops."""
        stop_data = {
            "event": "agent_stop",
            "task_id": task_id,
            "outcome": outcome,
            "reason": reason,
            "step": iterations,
            "iterations_used": iterations,
            "max_iterations": limit,
            "tools_called_tail": tools_called[-20:],
            "total_tools_called": len(tools_called),
            "completion_state": "stopped",
            "ts": datetime.now(UTC).isoformat(),
        }
        try:
            if self._task_trace is not None:
                self._task_trace.append_event("trace_stop", copy.deepcopy(stop_data))
        except Exception:
            logger.debug("Task trace stop event write failed, continuing", exc_info=True)
        try:
            with open(self._get_structured_step_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(redact_card_data(stop_data), default=str) + "\n")
        except Exception:
            logger.debug("JSONL agent_stop write failed, continuing")

    def _stop_for_step_ceiling(
        self,
        *,
        task_log: AgentTaskLog,
        checkpoint: TaskCheckpoint,
        messages: list[dict[str, Any]],
        user_text: str,
        tools_called: list[str],
        iterations: int,
        start_time: float,
        prefer_provided_messages: bool = False,
    ) -> AgentResult:
        """Persist partial state and return when the hard step ceiling fires."""
        limit = int(self._max_iterations or MAX_AGENT_ITERATIONS)
        logger.warning(
            "Agent step ceiling reached for task %s after %d iterations (limit=%d)",
            task_log.task_id,
            iterations,
            limit,
        )
        if self._use_native:
            self._sanitize_native_messages_for_api(trim_history=False)
        answer = _build_agent_final_answer_fallback(
            self,
            user_text,
            tools_called,
            reason="step_ceiling",
        )
        _set_final_response(self, answer, continue_listening=True)
        self._final_params["stop_reason"] = "max_agent_iterations"
        self._final_params["max_iterations"] = limit
        self._snapshot_checkpoint_state(
            checkpoint,
            messages,
            prefer_provided_messages=prefer_provided_messages,
        )
        checkpoint.status = "timed_out"
        checkpoint.outcome = "step_ceiling"
        checkpoint.updated_at = datetime.now(UTC).isoformat()
        save_checkpoint(checkpoint)
        self._emit_agent_stop_event(
            task_id=task_log.task_id,
            outcome="step_ceiling",
            reason="max_agent_iterations",
            iterations=iterations,
            limit=limit,
            tools_called=tools_called,
        )
        _dispatch_hook("task_failed", reason="step_ceiling", user_id=self._user_id)
        self._finalize_log(task_log, "step_ceiling", start_time)
        return AgentResult(
            ok=False,
            answer=answer,
            params=dict(self._final_params),
            iterations_used=iterations,
            tools_called=tools_called,
            error="Agent step ceiling reached (%d iterations)" % limit,
            continue_listening=True,
        )

    def _finalize_log(
        self,
        task_log: AgentTaskLog,
        outcome: str,
        start_time: float,
    ) -> None:
        """Finalize and persist the task log."""
        total_duration = time.monotonic() - start_time
        raw_final_answer = getattr(self, "_raw_final_answer", None)
        final_answer_text = str(
            raw_final_answer
            if raw_final_answer is not None
            else (self._final_answer if self._final_answer is not None else self._final_command or "")
        )
        last_response = self._task_trace_last_response if isinstance(self._task_trace_last_response, dict) else {}
        final_control = self._build_task_trace_control_state(
            llm_response=last_response,
            continue_listening=self._final_continue_listening,
            final_answer=self._final_answer,
            final_command=self._final_command,
        )
        if outcome == "llm_error" and _final_response_overrides_llm_error(
            final_answer=self._final_answer,
            final_command=self._final_command,
            last_response=last_response,
            payment_gate=final_control.get("typed_outcome") == "payment_gate",
            signature_gate=final_control.get("typed_outcome") == "signature_gate",
            continue_listening=self._final_continue_listening,
        ):
            typed_outcome = str(final_control.get("typed_outcome") or "")
            outcome = typed_outcome if typed_outcome in {"payment_gate", "signature_gate"} else "success"
        if outcome == "success":
            incomplete_diagnostic = _terminal_structured_incomplete_diagnostic(self, task_log)
            if incomplete_diagnostic:
                outcome = "incomplete"
                _record_structured_incomplete_diagnostic(self, incomplete_diagnostic)
        complete_control = copy.deepcopy(final_control)
        if outcome == "llm_error":
            complete_control["completion_state"] = "error"
        elif outcome in {"spin_terminated", "timeout", "step_ceiling"}:
            complete_control["completion_state"] = "stopped"
        elif outcome == "incomplete":
            complete_control["typed_outcome"] = "final_answer"
            complete_control["completion_state"] = "incomplete"
            complete_control["outcome_origin"] = final_control.get("outcome_origin") or "model"
        elif outcome == "payment_gate":
            complete_control["typed_outcome"] = "payment_gate"
            complete_control["completion_state"] = "handoff"
        elif outcome == "signature_gate":
            complete_control["typed_outcome"] = "signature_gate"
            complete_control["completion_state"] = "handoff"
        elif self._final_continue_listening:
            complete_control["typed_outcome"] = "ask_user"
            complete_control["completion_state"] = "awaiting_user"
        elif outcome == "success":
            complete_control["completion_state"] = "completed"
        should_capture_terminal_model_step = bool(
            last_response
            and str(last_response.get("type") or "").strip().lower() != "tool_call"
            and final_control["typed_outcome"]
            in {
                "ask_user",
                "final_answer",
                "payment_gate",
                "signature_gate",
            }
        )
        trace_total_steps = task_log.total_steps
        synthetic_step: AgentStepRecord | None = None
        if should_capture_terminal_model_step and (
            not task_log.steps or task_log.steps[-1].tool_name != "final_answer"
        ):
            synthetic_step = AgentStepRecord(
                task_id=task_log.task_id,
                step=task_log.total_steps + 1,
                timestamp=datetime.now(tz=UTC).isoformat(),
                tool_name="final_answer",
                tool_input={},
                tool_result_summary=_summarize(final_answer_text, 10000),
                llm_decision="final_answer",
                llm_reasoning=getattr(self, "_last_llm_reasoning", ""),
                page_url=getattr(self, "_last_page_url", None),
                duration_ms=0,
                error=None,
            )
            task_log.add_step(synthetic_step)
            terminal_messages = copy.deepcopy(
                self._native_messages if self._use_native else getattr(self, "_text_messages", [])
            )
            terminal_response = (
                self._task_trace_last_response if isinstance(self._task_trace_last_response, dict) else {}
            )
            terminal_raw_content = terminal_response.get("_raw_content")
            if self._use_native and terminal_raw_content:
                terminal_messages.append(
                    {
                        "role": "assistant",
                        "content": _normalize_content_blocks(terminal_raw_content),
                    }
                )
            elif not self._use_native and final_answer_text:
                terminal_messages.append({"role": "assistant", "content": final_answer_text})
            self._emit_step_jsonl(
                synthetic_step,
                self._last_usage or {},
                model_messages_snapshot=terminal_messages,
            )
            trace_total_steps = task_log.total_steps
        task_usage_summary = self._build_task_usage_summary()
        task_log.finalize(
            outcome,
            total_duration,
            usage=task_usage_summary["usage"],
            total_cost_usd=task_usage_summary["total_cost_usd"],
            model_totals=task_usage_summary["model_totals"],
        )
        task_log.final_answer = final_answer_text
        try:
            from services.user_capabilities.context import audit_tracked_run_completions

            audit_tracked_run_completions(
                status=outcome,
                task_id=task_log.task_id,
                steps=[
                    {
                        "step": step.step,
                        "tool": step.tool_name,
                        "args": step.tool_input,
                        "error": step.error,
                    }
                    for step in task_log.steps
                ],
            )
        except Exception:
            logger.debug("Capability run_complete audit hook skipped")
        if self._task_trace is not None:
            self._task_trace.append_complete(
                completed_at=datetime.now(tz=UTC).isoformat(),
                outcome=outcome,
                final_answer=final_answer_text,
                total_steps=trace_total_steps,
                total_duration_s=round(total_duration, 2),
                final_context_usage_pct=self._context_usage_pct_for_logs(),
                continuity=(self._export_responses_continuity_state() if self._use_native else {}),
                gate_state={
                    "payment_active": self._payment_gate_active,
                    "payment_requested": self._payment_gate_requested,
                    "signature_active": self._signature_gate_active,
                    "signature_requested": self._signature_gate_requested,
                },
                control=complete_control,
            )
        task_log.write_to_disk()
        logger.info(
            "Agent task %s completed: %d steps, %.1fs, outcome=%s",
            task_log.task_id,
            task_log.total_steps,
            total_duration,
            outcome,
        )

        if not self._agent_chain_metrics_recorded:
            self._agent_chain_metrics_recorded = True
            try:
                from admin.instrumentation import record_agent_chain_completed

                record_agent_chain_completed(
                    chain_length=trace_total_steps,
                    cap=MAX_AGENT_ITERATIONS if outcome == "step_ceiling" else None,
                    plan_family=self._agent_plan_policy.plan_family,
                    outcome=outcome,
                    capped=outcome == "step_ceiling",
                )
            except Exception:
                logger.debug("Telemetry record_agent_chain_completed failed, continuing")

        # Emit the task-complete structured event with outcome and final answer.
        try:
            jsonl_dir = get_logs_dir() / "structured"
            jsonl_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(UTC).strftime("%Y%m%d")
            jsonl_path = jsonl_dir / ("agent-steps-%s.jsonl" % today)
            suspicious_short = (
                outcome == "success"
                and task_log.total_steps < 3
                and any(step.tool_name.startswith("browser_") for step in task_log.steps)
            )
            complete_data = {
                "event": "task_complete",
                "task_id": task_log.task_id,
                "status": (
                    "in_progress"
                    if outcome == "llm_error"
                    else (
                        "stopped"
                        if outcome in {"spin_terminated", "timeout", "step_ceiling"}
                        else ("incomplete" if outcome == "incomplete" else "completed")
                    )
                ),
                "outcome": outcome,
                "suspicious_short": suspicious_short,
                "timed_out": outcome == "timeout",
                "step_ceiling": outcome == "step_ceiling",
                "final_answer": _summarize(final_answer_text, 500),
                "total_steps": trace_total_steps,
                "total_duration_s": round(total_duration, 1),
                "total_duration_ms": task_log.total_duration_ms,
                "usage": task_log.usage,
                "total_cost_usd": task_log.total_cost_usd,
                "model_totals": task_log.model_totals,
                "task_category": getattr(self, "_task_category", "unknown"),
                "prompt_variant": getattr(self, "_prompt_variant", "unknown"),
                "user_text": getattr(self, "_original_command", ""),
                "ts": datetime.now(UTC).isoformat(),
                # Task-level telemetry summary.
                "model_name": self._get_model_name_safe(),
                "prompt_variant_full": getattr(self, "_prompt_variant_full", "unknown"),
                "system_prompt_hash": getattr(self, "_system_prompt_hash", ""),
                "full_prompt_hash": getattr(self, "_full_prompt_hash", getattr(self, "_system_prompt_hash", "")),
                "system_context_hash": getattr(self, "_system_context_hash", ""),
                "compaction_count": getattr(self, "_compaction_count", 0),
                "final_context_usage_pct": self._context_usage_pct_for_logs(),
                "llm_cumulative_input_tokens": getattr(self, "_llm_cumulative_input_tokens", 0),
                "llm_total_input_tokens_seen": getattr(self, "_llm_total_input_tokens_seen", 0),
                "responses_continuity_mode": getattr(self, "_last_continuity_mode", None),
            }
            line = json.dumps(complete_data, default=str) + "\n"
            with open(self._get_structured_step_log_path(), "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            logger.debug("JSONL task_complete write failed, continuing")

        # Telemetry: record agent outcome
        try:
            from admin.instrumentation import record_agent_task_success

            if outcome == "success":
                record_agent_task_success()
        except Exception:
            logger.debug("Telemetry record_agent_task_success failed, continuing")

        if outcome == "success":
            try:
                self._maybe_register_apis(task_log)
            except Exception:
                logger.debug("API registration skipped due to error")

            # Verify recalled memories after successful task completion
            if self._recalled_memory_ids:
                try:
                    from services.memory.store import get_memory_store

                    _mem_store = get_memory_store()
                    _user_id = self._get_effective_user_id()
                    _verified = _mem_store.promote_verified_memories(_user_id, self._recalled_memory_ids)
                    if _verified:
                        logger.info(
                            "Verified %d recalled memories for user %s",
                            _verified,
                            _user_id,
                        )
                except Exception:
                    logger.debug("Memory verification after success failed", exc_info=True)

    def _emit_step_jsonl(
        self,
        record: AgentStepRecord,
        usage: dict[str, int],
        *,
        model_messages_snapshot: list[dict[str, Any]] | None = None,
    ) -> None:
        """Emit a structured JSONL log entry for an agent step.

        Writes directly to a dedicated JSONL file (bypasses the logging
        framework which may not have a JSONL handler in the Qt process).
        Also emits via the standard logger for the text log.
        """
        step_visible_tools = self._get_step_log_visible_tools()
        safe_tool_input = redact_card_data(record.tool_input)
        safe_tool_result = redact_card_data(record.tool_result_summary)
        step_data = {
            "event": "agent_step",
            "task_id": record.task_id,
            "tool_use_id": record.tool_use_id,
            "step": record.step,
            "tool_name": record.tool_name,
            "tool_input": safe_tool_input,
            "tool_result": safe_tool_result,
            "llm_decision": record.llm_decision,
            "llm_reasoning": _redact_reasoning_for_logs(record.llm_reasoning),
            "duration_ms": record.duration_ms,
            "error": record.error,
            "page_url": record.page_url,
            "llm_usage": usage or {},
            "llm_cumulative_input_tokens": getattr(self, "_llm_cumulative_input_tokens", 0),
            "llm_cumulative_output_tokens": getattr(self, "_llm_cumulative_output_tokens", 0),
            "llm_total_input_tokens_seen": getattr(self, "_llm_total_input_tokens_seen", 0),
            "server_context_tokens": getattr(self, "_last_server_context_tokens", 0),
            "responses_continuity_mode": getattr(self, "_last_continuity_mode", None),
            "task_category": self._task_category,
            "focused_set": None,
            "tool_surface_mode": "all_tools",
            "compat_metadata": self._build_task_trace_compat_metadata(),
            "tools_available_count": getattr(self, "_all_mode_tool_count", None) or len(step_visible_tools),
            "tools_available_names": sorted(t.get("name", "") for t in step_visible_tools),
            "result_summary": _summarize(safe_tool_result, 200),
            "browser_tiers_used": (sorted(self._browser_tiers_used) if self._browser_tiers_used else []),
            "ts": datetime.now(UTC).isoformat(),
            # Step-level telemetry: user text on step 1, intervention, and prompt variant.
            "prompt_variant": getattr(self, "_prompt_variant", "unknown"),
            # Full runtime information surface.
            # 1. Model & parameters
            "model_name": self._get_model_name_safe(),
            "temperature": self._get_temperature_safe(),
            "tool_choice": getattr(self, "_last_tool_choice", "auto"),
            # 2. Context state
            "message_count": getattr(self, "_last_message_count", 0),
            "context_usage_pct": self._context_usage_pct_for_logs(),
            "context_window": self._token_tracker.context_window,
            "compaction_count": getattr(self, "_compaction_count", 0),
            # 3. System prompt identity
            "system_prompt_hash": getattr(self, "_system_prompt_hash", ""),
            "full_prompt_hash": getattr(self, "_full_prompt_hash", getattr(self, "_system_prompt_hash", "")),
            "system_context_hash": getattr(self, "_system_context_hash", ""),
            "system_prompt_len": getattr(self, "_system_prompt_len", 0),
            "prompt_variant_full": getattr(self, "_prompt_variant_full", "unknown"),
            # 4. Filtering & rejected tools
            "rejected_tools": (sorted(self._rejected_tools) if self._rejected_tools else []),
            # 5. Message history state
            "conversation_turn_count": self._count_conversation_turns(),
            # 6. Snapshot compression
            "snapshot_compressed": getattr(self, "_snapshot_compressed_this_step", False),
            # 7. Per-step: screenshot image sent to model
            "had_screenshot_image": getattr(self, "_step_had_screenshot", False),
            "snapshot_pre_compress_len": getattr(self, "_step_snapshot_pre_len", 0),
        }
        _reasoning_source_payload = _build_reasoning_source_payload(record.llm_reasoning)
        if _reasoning_source_payload is not None:
            step_data["llm_reasoning_source"] = _reasoning_source_payload
        _trace_compaction_payload = (
            copy.deepcopy(self._pending_compaction_meta) if self._pending_compaction_meta else None
        )
        _trace_control = self._build_task_trace_control_state(
            llm_response=self._task_trace_last_response,
            continue_listening=self._final_continue_listening,
            final_answer=self._final_answer,
            final_command=self._final_command,
        )
        step_data["typed_outcome"] = _trace_control["typed_outcome"]
        step_data["completion_state"] = _trace_control["completion_state"]
        step_data["outcome_origin"] = _trace_control["outcome_origin"]

        # Include user text only on step 1 to avoid bloating every line.
        if not self._step_log_metadata_emitted:
            step_data["user_text"] = getattr(self, "_original_command", "")
            # Full prompt/context bytes can contain account profile and address
            # PII. The daily structured log is a fast index by default; set
            # VIOLA_LOG_FULL_PROMPTS=1 for local forensic captures.
            if _log_full_prompts_enabled():
                step_data["system_prompt_text"] = _redact_prompt_for_structured_log(
                    getattr(self, "_system_prompt_text", "")
                )
                step_data["system_context_text"] = _redact_prompt_for_structured_log(
                    getattr(self, "_system_context_text", "")
                )
                step_data["system_context_components"] = _redact_prompt_for_structured_log(
                    getattr(self, "_system_context_components", {})
                )
                step_data["prior_conversation_messages"] = _redact_prompt_for_structured_log(
                    getattr(self, "_prior_conversation_messages", [])
                )
                step_data["initial_model_messages"] = _redact_prompt_for_structured_log(
                    getattr(self, "_initial_model_messages", [])
                )
            # GAP 11: prior conversation context (multi-turn)
            prior_turns = getattr(self, "_prior_conversation_turns", 0)
            if prior_turns > 0:
                step_data["prior_conversation_turns"] = prior_turns
                if _log_full_prompts_enabled():
                    step_data["prior_conversation_summary"] = _redact_prompt_for_structured_log(
                        getattr(self, "_prior_conversation_summary", "")
                    )

            # Tool-schema fingerprint.
            # Captures what the model ACTUALLY sees for each tool's parameters.
            # Without this, missing param descriptions and injected fields
            # (like reasoning) are invisible in surface replays.
            tool_schemas = self._build_tool_schema_snapshot()
            step_data["tool_schemas"] = tool_schemas
            step_data["tool_schema_hash"] = self._hash_tool_schema_snapshot(tool_schemas)
            step_data["tool_schema_fingerprint"] = self._build_tool_schema_fingerprint(tool_schemas)
            tool_surface = self._get_runtime_tool_surface()
            if tool_surface is not None:
                if hasattr(tool_surface, "to_dict"):
                    step_data["tool_surface"] = tool_surface.to_dict()
                elif isinstance(tool_surface, dict):
                    step_data["tool_surface"] = dict(tool_surface)

        if (
            getattr(self, "_step_approval_path", None)
            and getattr(self, "_step_approval_tool", None) == record.tool_name
        ):
            step_data["approval_path"] = self._step_approval_path

        # Step-log telemetry fields.
        # These use a "pending" pattern: set during previous iteration,
        # captured here on the current step record.

        # Spin intervention.
        if self._pending_spin_intervention:
            step_data["spin_intervention"] = self._pending_spin_intervention
            self._pending_spin_intervention = None

        # GAP 1: factual correction messages (tool redirects, blacklists)
        if self._pending_correction_msgs:
            step_data["correction_messages"] = self._pending_correction_msgs
            self._pending_correction_msgs = None

        # GAP 2: Consecutive failure warning
        if self._pending_consecutive_warning:
            step_data["consecutive_failure_warning"] = self._pending_consecutive_warning
            self._pending_consecutive_warning = None

        # GAP 3: Error registry context injection
        if self._pending_error_registry_ctx:
            step_data["error_registry_context"] = self._pending_error_registry_ctx
            self._pending_error_registry_ctx = None

        # GAP 10: Force termination message
        if self._pending_force_termination:
            step_data["force_termination"] = self._pending_force_termination
            self._pending_force_termination = None

        # GAP 12: Hard loop breaker
        if self._pending_hard_loop_breaker:
            step_data["hard_loop_breaker"] = self._pending_hard_loop_breaker
            self._pending_hard_loop_breaker = None

        # GAP 4: Compaction content tracking
        if self._pending_compaction_meta:
            step_data["compaction_method"] = self._pending_compaction_meta.get("method", "")
            step_data["compaction_messages_before"] = self._pending_compaction_meta.get("messages_before", 0)
            step_data["compaction_messages_after"] = self._pending_compaction_meta.get("messages_after", 0)
            step_data["compaction_summary_preview"] = self._pending_compaction_meta.get("summary_text", "")[:500]
            self._pending_compaction_meta = None

        # GAP 6: History trimming tracking
        if self._pending_trim_stats:
            step_data["trimmed_messages"] = self._pending_trim_stats.get("trimmed", 0)
            step_data["fifo_evicted_messages"] = self._pending_trim_stats.get("fifo_evicted", 0)
            step_data["screenshots_evicted"] = self._pending_trim_stats.get("screenshots_evicted", 0)
            self._pending_trim_stats = None

        step_data = redact_card_data(step_data)

        # Zero-tools monitoring: warn when agent has no tools available.
        # This was the root cause of the March 23 collapse (326 blocked calls).
        if step_data["tools_available_count"] == 0:
            logger.warning(
                "ZERO TOOLS AVAILABLE for task %s step %s â€” agent is blind",
                record.task_id,
                record.step,
            )

        if self._task_trace is not None:
            try:
                if _trace_compaction_payload is not None:
                    _compaction_event_payload = copy.deepcopy(_trace_compaction_payload)
                    if self._task_trace_last_request:
                        _compaction_event_payload["next_llm_input"] = copy.deepcopy(self._task_trace_last_request)
                    self._task_trace.append_compaction(
                        ts=step_data["ts"],
                        step=record.step,
                        compaction=_compaction_event_payload,
                    )
                _step_kind = "tool_call"
                if record.tool_name == "think":
                    _step_kind = "think"
                elif _trace_control["typed_outcome"] in {
                    "ask_user",
                    "final_answer",
                    "payment_gate",
                    "signature_gate",
                }:
                    _step_kind = _trace_control["typed_outcome"]
                trace_step_data = copy.deepcopy(step_data)
                if model_messages_snapshot is not None:
                    trace_step_data["messages"] = copy.deepcopy(model_messages_snapshot)
                self._task_trace.append_step(
                    step=record.step,
                    ts=step_data["ts"],
                    step_kind=_step_kind,
                    agent_step=trace_step_data,
                    attempt_id=getattr(self, "_task_trace_last_attempt_id", None),
                    llm_input=copy.deepcopy(self._task_trace_last_request),
                    llm_output=copy.deepcopy(self._task_trace_last_response),
                    continuity_before=copy.deepcopy(self._task_trace_last_continuity_before),
                    continuity_after=copy.deepcopy(self._task_trace_last_continuity_after),
                    control=copy.deepcopy(_trace_control),
                    tool_execution=copy.deepcopy(self._task_trace_last_tool_execution),
                )
            except Exception:
                logger.debug("Task trace step write failed, continuing", exc_info=True)

        # Direct JSONL file write (works in both Qt and backend processes)
        try:
            jsonl_dir = get_logs_dir() / "structured"
            jsonl_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(UTC).strftime("%Y%m%d")
            jsonl_path = jsonl_dir / ("agent-steps-%s.jsonl" % today)
            line = json.dumps(step_data, default=str) + "\n"
            with open(self._get_structured_step_log_path(), "a", encoding="utf-8") as f:
                f.write(line)
            self._step_log_metadata_emitted = True
        except Exception:
            logger.debug("JSONL step log write failed, continuing")  # Never let JSONL writing break the agent loop

        # Also emit via logger for the text log
        logger.info(
            "agent_step task=%s step=%d tool=%s",
            record.task_id,
            record.step,
            record.tool_name,
        )
        self._task_trace_last_tool_execution = None
        self._step_approval_path = None
        self._step_approval_tool = None

    # ------------------------------------------------------------------ Structured telemetry helpers

    def _build_structured_step_log_path(self, started_at: str | None = None) -> Path:
        """Build the structured step log path for a task."""
        try:
            ts = datetime.fromisoformat(started_at) if started_at else datetime.now(UTC)
        except Exception:
            ts = datetime.now(UTC)
        return get_logs_dir() / "structured" / ("agent-steps-%s.jsonl" % ts.astimezone(UTC).strftime("%Y%m%d"))

    def _get_structured_step_log_path(self) -> Path:
        """Return the pinned JSONL file used for both step and outcome writes."""
        if self._step_log_jsonl_path is None:
            self._step_log_jsonl_path = self._build_structured_step_log_path()
        return self._step_log_jsonl_path

    def _get_runtime_tool_surface(self) -> Any | None:
        """Return the controller-provided tool surface when available."""
        from unittest.mock import Mock

        tool_surface = self._request_tool_surface
        if tool_surface is None:
            tool_surface = getattr(self._llm, "_tool_surface", None)
        if isinstance(tool_surface, Mock):
            return None
        if isinstance(tool_surface, dict):
            return tool_surface
        if (
            tool_surface is not None
            and hasattr(tool_surface, "step_log_visible")
            and hasattr(tool_surface, "fingerprint")
        ):
            return tool_surface
        return None

    def _get_step_log_visible_tools(self) -> list[dict[str, Any]]:
        """Return the tools that should be represented in step logs."""
        tool_surface = self._get_runtime_tool_surface()
        if tool_surface is not None:
            if hasattr(tool_surface, "step_log_visible"):
                return list(tool_surface.step_log_visible or [])
            if isinstance(tool_surface, dict):
                return list(tool_surface.get("step_log_visible") or [])
        try:
            if self._mcp_hub:
                return self._mcp_hub.list_tools()
        except Exception:
            return []
        return []

    def _get_request_native_tools(self) -> list[dict[str, Any]] | None:
        """Return the request-scoped native schemas sent to the provider."""
        tools: list[dict[str, Any]] | None = None
        if self._native_tools is not None:
            tools = list(self._native_tools)
        else:
            tool_surface = self._get_runtime_tool_surface()
            if tool_surface is not None:
                if hasattr(tool_surface, "provider_native"):
                    tools = list(tool_surface.provider_native or [])
                elif isinstance(tool_surface, dict):
                    provider_native = tool_surface.get("provider_native")
                    if isinstance(provider_native, list):
                        tools = list(provider_native)
            if tools is None:
                provider_tools = getattr(self._llm, "_native_tools", None)
                if isinstance(provider_tools, list):
                    tools = list(provider_tools)
        if tools is None:
            return None
        # F-012 (R3-A): fork subagents must see the parent's *exact* native
        # tool array on every turn, not just the first.
        # ``tools/AgentTool/AgentTool.tsx:622-632`` shows Claude carries
        # ``options.tools`` verbatim through the forked sub-loop and
        # ``forkSubagent.ts:73-76`` rejects recursive fork spawns at
        # execution time. Without the fork-exact carry-over,
        # ``_get_request_native_tools`` would re-derive the tool surface on
        # turn 2+ and strip orchestrator tools (when at MAX_DELEGATION_DEPTH)
        # or legacy delegation tools, diverging the child's tool set
        # mid-task. Skip the depth-based strip for fork children; the
        # runtime spawn guard at ``_native_tools_for_child`` and
        # ``MAX_DELEGATION_DEPTH`` continue to block grandchildren.
        if getattr(self, "_subagent_mode", None) == "fork":
            return tools
        if self._depth >= MAX_DELEGATION_DEPTH:
            tools = [t for t in tools if t.get("name") not in _ORCHESTRATOR_DISPATCH_TOOLS]
        else:
            tools = [t for t in tools if t.get("name") not in _LEGACY_DELEGATION_TOOLS]
        return tools

    def _available_tool_count(self) -> int:
        native_tools = self._get_request_native_tools()
        if native_tools is not None:
            return len(native_tools)
        if self._mcp_hub is not None:
            try:
                return len(self._mcp_hub.list_tools())
            except Exception:
                return 0
        return 0

    def _build_tool_schema_snapshot(self) -> list[dict[str, Any]]:
        """Capture the visible tool schemas for the current task."""
        raw_tools = self._get_step_log_visible_tools()

        tool_schemas: list[dict[str, Any]] = []
        for tool in raw_tools:
            name = str(tool.get("name", "")).strip()
            if not name:
                continue
            tool_schemas.append(
                {
                    "name": name,
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {},
                }
            )
        return sorted(tool_schemas, key=lambda item: item["name"])

    def _hash_tool_schema_snapshot(self, tool_schemas: list[dict[str, Any]]) -> str:
        """Return a stable hash of the logged tool schema snapshot."""
        payload = json.dumps(tool_schemas, sort_keys=True, default=str, ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _build_tool_schema_fingerprint(self, tool_schemas: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Build a compact summary of what the model sees for each tool's parameters.

        Captures per-tool: param count, params with/without descriptions, whether
        reasoning was injected.  This makes missing descriptions and synthetic
        parameters visible in surface replays.

        Only called on step 1 to avoid per-step bloat.
        """
        if tool_schemas is None:
            tool_schemas = self._build_tool_schema_snapshot()

        tools_detail = []
        no_desc_count = 0
        no_param_desc_count = 0
        has_reasoning_count = 0

        for tool in tool_schemas:
            schema = tool.get("input_schema") or {}
            props = schema.get("properties", {})
            required = schema.get("required", [])
            tool_desc = (tool.get("description") or "").strip()

            params_with_desc = 0
            params_without_desc = 0
            has_reasoning = "reasoning" in props

            for pname, pdef in props.items():
                if pname == "reasoning":
                    continue  # Don't count injected reasoning
                if pdef.get("description"):
                    params_with_desc += 1
                else:
                    params_without_desc += 1

            if not tool_desc:
                no_desc_count += 1
            if params_without_desc > 0:
                no_param_desc_count += 1
            if has_reasoning:
                has_reasoning_count += 1

            tools_detail.append(
                {
                    "name": tool["name"],
                    "has_description": bool(tool_desc),
                    "param_count": len(props) - (1 if has_reasoning else 0),
                    "params_with_desc": params_with_desc,
                    "params_without_desc": params_without_desc,
                    "has_reasoning_injected": has_reasoning,
                    "required": sorted(required),
                }
            )

        return {
            "total_tools": len(tools_detail),
            "tools_missing_description": no_desc_count,
            "tools_with_undescribed_params": no_param_desc_count,
            "tools_with_reasoning_injected": has_reasoning_count,
            "tools": tools_detail,
        }

    def _build_task_usage_summary(self) -> dict[str, Any]:
        """Return task-level token/cost totals from the active cost tracker."""
        usage = _empty_task_usage()
        total_cost_usd = 0.0
        model_totals: dict[str, dict[str, int | float]] = {}
        session_id = str(
            getattr(self, "_session_cost_session_id", None)
            or getattr(self, "_session_id", None)
            or getattr(self, "task_id", "")
            or ""
        ).strip()
        try:
            if session_id:
                from services.llm.session_cost_tracker import get_session_cost_tracker

                tracker = get_session_cost_tracker(
                    user_id=getattr(self, "_session_cost_user_id", None) or getattr(self, "_user_id", None),
                    session_id=session_id,
                )
                snapshot = tracker.snapshot()
                model_totals = _normalize_model_totals(snapshot.get("model_usage"))
                usage = _task_usage_from_model_totals(model_totals)
                total_cost_usd = float(snapshot.get("total_cost_usd") or 0.0)
        except (
            AttributeError,
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            logger.debug("Task usage summary cost snapshot skipped", exc_info=True)

        if usage["total_tokens"] <= 0:
            fallback_usage = dict(getattr(self, "_last_usage", {}) or {})
            if getattr(self, "_llm_total_input_tokens_seen", 0):
                fallback_usage["input_tokens"] = getattr(self, "_llm_total_input_tokens_seen", 0)
            if getattr(self, "_llm_total_output_tokens_seen", 0):
                fallback_usage["output_tokens"] = getattr(self, "_llm_total_output_tokens_seen", 0)
            if (
                not fallback_usage.get("input_tokens")
                and not fallback_usage.get("output_tokens")
                and getattr(self, "_llm_cumulative_total_tokens", 0)
            ):
                fallback_usage["total_tokens"] = getattr(self, "_llm_cumulative_total_tokens", 0)
            usage = _normalize_task_usage(fallback_usage)

        return {
            "usage": usage,
            "total_cost_usd": total_cost_usd,
            "model_totals": model_totals,
        }

    @staticmethod
    def _usage_int(usage: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                return parsed
        return 0

    def _record_llm_usage_for_context(self, usage: dict[str, Any] | None) -> None:
        """Track provider-observed context usage across Responses API turns."""
        if not isinstance(usage, dict):
            return
        usage.setdefault("_raw_usage", _usage_raw_envelope(usage))
        input_tokens = self._usage_int(usage, "input_tokens", "prompt_tokens")
        output_tokens = self._usage_int(usage, "output_tokens", "completion_tokens")
        total_tokens = self._usage_int(usage, "total_tokens")
        if total_tokens <= 0:
            total_tokens = input_tokens + output_tokens

        self._llm_last_input_tokens = input_tokens
        self._llm_last_output_tokens = output_tokens
        self._last_server_context_tokens = input_tokens
        if input_tokens > 0:
            self._llm_cumulative_input_tokens += input_tokens
            self._llm_total_input_tokens_seen += input_tokens
        if output_tokens > 0:
            self._llm_cumulative_output_tokens += output_tokens
            self._llm_total_output_tokens_seen += output_tokens
        if total_tokens > 0:
            self._llm_cumulative_total_tokens += total_tokens

        # R13 launch-readiness contract: `_llm_context_usage_pct` reflects the
        # session-cumulative pressure on the model's context window. P3 (Region-I)
        # moved the compaction TRIGGER to last-input-tokens for TS parity, but the
        # telemetry field stays cumulative because step logs and trace consumers
        # depend on the session-wide ratio. See
        # `scripts/check_r13_agent_loop_termination.py:86` for the gate that pins
        # this literal expression.
        context_window = self._context_window_for_usage()
        if context_window > 0:
            self._llm_context_usage_pct = self._llm_last_input_tokens / float(context_window)
        else:
            self._llm_context_usage_pct = 0.0

    def _reset_llm_usage_compaction_window(self) -> None:
        """Reset the usage window that drives the next compaction threshold."""
        self._llm_cumulative_input_tokens = 0
        self._llm_cumulative_output_tokens = 0
        self._llm_cumulative_total_tokens = 0
        self._llm_last_input_tokens = 0
        self._llm_last_output_tokens = 0
        self._last_server_context_tokens = 0
        self._llm_context_usage_pct = 0.0

    def _context_window_for_usage(self) -> int:
        tracker = getattr(self, "_token_tracker", None)
        value = getattr(tracker, "context_window", 0)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            # The tracker already applies the VIOLA_AGENT_CONTEXT_WINDOW override
            # at configure time; trust its resolved window.
            return parsed
        # Trackerless fallback (e.g. unit-test executors built via __new__).
        # Apply the override here too so the knob is the unconditional final
        # word on the resolved window regardless of SOURCE — parity with Claude
        # Code TS's CLAUDE_CODE_AUTO_COMPACT_WINDOW.
        try:
            safe = int(self._get_context_window_safe())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            safe = 0
        return apply_context_window_override(safe)

    def _context_usage_pct_for_logs(self) -> float:
        """Return provider-token context usage as a percent for traces/logs."""
        if self._llm_last_input_tokens > 0:
            return round(self._llm_context_usage_pct * 100, 1)
        tracker = getattr(self, "_token_tracker", None)
        try:
            return round(float(getattr(tracker, "usage_pct", 0.0) or 0.0) * 100, 1)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _memory_action(tool_args: dict[str, Any]) -> str:
        return str(tool_args.get("action") or "read").strip().lower() or "read"

    @staticmethod
    def _memory_requested_all_scope(tool_args: dict[str, Any]) -> bool:
        return str(tool_args.get("path") or "").strip() in _MEMORY_ALL_SCOPE_PATHS

    @staticmethod
    def _memory_result_data(tool_result: ToolResult) -> dict[str, Any]:
        data = getattr(tool_result, "data", None)
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _memory_no_match_queries(self) -> list[str]:
        queries = getattr(self, "_memory_all_scope_no_match_queries", None)
        if not isinstance(queries, list):
            queries = []
            self._memory_all_scope_no_match_queries = queries
        return queries

    def _record_memory_all_scope_no_match(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: ToolResult,
    ) -> None:
        if tool_name != "memory" or not getattr(tool_result, "ok", False):
            return
        action = self._memory_action(tool_args)
        if action not in _MEMORY_SEARCH_ACTIONS:
            return
        data = self._memory_result_data(tool_result)
        scope = str(data.get("scope") or "").strip()
        if scope != "all_memory" and not self._memory_requested_all_scope(tool_args):
            return
        matched = data.get("matched")
        match_count = data.get("match_count")
        try:
            match_count_int = int(match_count)
        except (TypeError, ValueError):
            match_count_int = -1
        if matched is not False and match_count_int != 0:
            return
        query = str(tool_args.get("query") or data.get("query") or "").strip()
        queries = self._memory_no_match_queries()
        if query and query not in queries:
            queries.append(query)

    def _memory_exhaustion_short_circuit(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> ToolResult | None:
        if tool_name != "memory":
            return None
        action = self._memory_action(tool_args)
        if action in _MEMORY_MUTATING_ACTIONS:
            return None
        queries = self._memory_no_match_queries()
        if not queries:
            return None

        query = str(tool_args.get("query") or "").strip()
        repeated_search = action in _MEMORY_SEARCH_ACTIONS and query and query in queries
        broad_enumeration = action in _MEMORY_ENUMERATION_ACTIONS
        if not repeated_search and not broad_enumeration:
            return None

        payload: dict[str, Any] = {
            "content": "",
            "files": [],
            "match_count": 0,
            "matched": False,
            "scope": "all_memory",
            "memory_scope_exhausted": True,
            "exhausted_queries": list(queries),
            "requested_action": action,
        }
        requested_path = str(tool_args.get("path") or "").strip()
        if requested_path:
            payload["requested_path"] = requested_path
        if query:
            payload["query"] = query
        if broad_enumeration:
            payload["message"] = "Memory already returned no matches for this task; broad memory reads are skipped."
            self._memory_enumeration_blocked_after_no_match = True
            rejected_tools = getattr(self, "_rejected_tools", None)
            if isinstance(rejected_tools, set):
                rejected_tools.add("memory")
        else:
            payload["message"] = "Memory already returned no matches for this query in this task."
        return ToolResult(
            ok=False,
            data=payload,
            error="memory_scope_exhausted",
            error_category="memory_scope_exhausted",
            retryable=False,
        )

    def _should_compact_by_cumulative_usage(self, *, force: bool = False) -> bool:
        """Gate compaction from the current provider-observed prompt pressure."""
        context_window = self._context_window_for_usage()
        if context_window <= 0 or self._llm_last_input_tokens <= 0:
            return False
        if force:
            return True
        return self._llm_last_input_tokens >= int(context_window * _CONTEXT_COMPACTION_USAGE_THRESHOLD)

    @staticmethod
    def _tool_result_looks_empty(tool_result: ToolResult, result_text: str | None = None) -> bool:
        error = getattr(tool_result, "error", None)
        if error not in (None, "", [], {}):
            return False
        if getattr(tool_result, "ok", True) is False:
            return False
        data = getattr(tool_result, "data", None)
        if isinstance(data, dict) and (data.get("ok") is False or data.get("error")):
            return False
        if data in (None, "", [], {}):
            return True
        if isinstance(data, dict):
            for key in ("text", "content", "result", "value", "output"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return False
                if isinstance(value, (list, dict)) and value:
                    return False
            if any(key in data for key in ("text", "content", "result", "value", "output")):
                return True
        text = str(result_text or "").strip()
        return not text or text in {"{}", "[]"} or '"text": ""' in text or '"content": ""' in text

    @staticmethod
    def _tool_progress_page_health_status(tool_result: ToolResult) -> str | None:
        """Return a browser MCP tool's own no-progress page signal, if present.

        Two structured, tool-reported facts feed this -- both computed by
        `mcp_servers/browser/server.py` from the ACTUAL HTTP response / live
        page, never from the model's reply:

        - `page_health.status` (`_check_dead_page`, from the rendered page's
          title/description/snapshot text -- e.g. a Cloudflare/WAF JS
          challenge interstitial: "checking your browser...", "just a
          moment...").
        - `http_error_page` (`_http_error_page_payload`, from the real HTTP
          status code -- e.g. the founder-reported CF 524 gateway-timeout
          page hit navigating to YouTube, issue #278).

        Both mean the same thing from the loop-control perspective: the
        browser observed a page that is not the content the task needs, and
        retrying navigation to it made no forward progress.
        """
        data = getattr(tool_result, "data", None)
        if not isinstance(data, dict):
            return None
        if data.get("http_error_page") is True:
            return "http_error_page"
        page_health = data.get("page_health")
        if not isinstance(page_health, dict):
            return None
        status = page_health.get("status")
        return status if isinstance(status, str) else None

    def _record_tool_progress_pattern(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: ToolResult,
        result_text: str | None = None,
    ) -> dict[str, Any]:
        """Track consecutive-identical-no-effect tool results for loop termination.

        Progress-scoped safety net only (TS parity: the model gets raw results
        and decides; Viola adds this thin guard for true spins). The detector
        fires only when the SAME (tool, normalized_args) returns a fingerprint-
        identical result, or an explicit no-effect signal, ``N+1`` times in a
        row. Repeated calls with DIFFERENT result content (e.g. browser_snapshot
        across distinct form pages) are progress and never trip the gate.

        Issue #278: a second, session-wide gate rides alongside the per-key
        one above. It tracks consecutive `page_health.status` in
        `_NO_PROGRESS_PAGE_HEALTH_STATUSES` (bot-check/WAF challenge, parked
        domain) across ANY browser tool call, regardless of exact tool name
        or args. A repeated bot-check page is non-empty and often varies its
        exact args (retried URL, wait, re-snapshot) so it would never trip
        the per-key gate above -- but it is still zero forward progress.
        """
        page_url = str(
            getattr(self, "_last_page_url", None) or tool_args.get("url") or tool_args.get("page_url") or ""
        ).strip()
        if page_url:
            self._tool_progress_seen_urls.add(page_url)

        page_health_status = self._tool_progress_page_health_status(tool_result)
        bot_protection_streak = getattr(self, "_consecutive_bot_protection_signals", 0)
        if page_health_status in _NO_PROGRESS_PAGE_HEALTH_STATUSES:
            bot_protection_streak += 1
        elif tool_name.startswith("browser_"):
            # Any other browser observation (including a bot-check-free page
            # on a different site/URL) is either progress or an unrelated
            # browser error -- either way, the challenge streak is over.
            bot_protection_streak = 0
        self._consecutive_bot_protection_signals = bot_protection_streak
        bot_protection_stuck = bot_protection_streak >= _BOT_PROTECTION_HALT_STREAK

        empty_result = self._tool_result_looks_empty(tool_result, result_text)
        normalized_args = {
            key: value
            for key, value in tool_args.items()
            if key
            not in {
                "selector",
                "selectors",
                "xpath",
                "ref",
                "element",
                "query",
                "text_query",
            }
        }
        subset_key = json.dumps(
            {"tool": tool_name, "args": normalized_args},
            sort_keys=True,
            default=str,
        )[:500]

        # Legacy counter retained for telemetry/visibility — not used for the gate.
        legacy_counts = self._tool_progress_repetition_counts
        repeat_count = int(legacy_counts.get(subset_key, 0) or 0) + 1
        legacy_counts[subset_key] = repeat_count

        fingerprint = self._tool_progress_result_fingerprint(
            tool_name=tool_name,
            tool_result=tool_result,
            result_text=result_text,
            empty_result=empty_result,
        )
        explicit_no_effect = self._tool_progress_has_no_effect_signal(tool_name, tool_result)
        no_effect = empty_result or explicit_no_effect
        script_probe_count = self._record_browser_run_script_probe(
            tool_name=tool_name,
            page_url=page_url,
            tool_result=tool_result,
        )
        script_probe_stuck = script_probe_count > _BROWSER_RUN_SCRIPT_READ_PROBE_LIMIT

        prev_fingerprint = self._tool_progress_last_fingerprint.get(subset_key)
        if prev_fingerprint is not None and fingerprint == prev_fingerprint:
            consecutive = self._tool_progress_consecutive_identical.get(subset_key, 1) + 1
        else:
            consecutive = 1
        self._tool_progress_last_fingerprint[subset_key] = fingerprint
        self._tool_progress_consecutive_identical[subset_key] = consecutive

        # True-spin gate: consecutive identical results AND the result carries a
        # no-effect signal (empty body or explicit tool-reported no-effect flag).
        # Identical-but-substantive results (rare; e.g. legitimate idempotent
        # reads) do NOT trip — only stale/empty/no-effect repetition does.
        tool_repeat_stuck = consecutive > _STUCK_TOOL_REPEAT_LIMIT and no_effect
        stuck = tool_repeat_stuck or script_probe_stuck or bot_protection_stuck
        if bot_protection_stuck:
            stuck_reason = "bot_protection"
        elif script_probe_stuck:
            stuck_reason = "script_probe"
        elif tool_repeat_stuck:
            stuck_reason = "tool_repeat"
        else:
            stuck_reason = None

        return {
            "stuck": stuck,
            "stuck_reason": stuck_reason,
            "repeat_count": repeat_count,
            "consecutive_identical": consecutive,
            "repeat_limit": _STUCK_TOOL_REPEAT_LIMIT,
            "pattern": {
                "tool": tool_name,
                "args": normalized_args,
                "no_effect": no_effect,
                "page_url": page_url,
                "script_probe_count": script_probe_count,
                "script_probe_limit": _BROWSER_RUN_SCRIPT_READ_PROBE_LIMIT,
                "script_probe_stuck": script_probe_stuck,
                "page_health_status": page_health_status,
                "bot_protection_streak": self._consecutive_bot_protection_signals,
                "bot_protection_streak_limit": _BOT_PROTECTION_HALT_STREAK,
                "bot_protection_stuck": bot_protection_stuck,
            },
            "no_effect": no_effect,
            "visited_url_count": len(self._tool_progress_seen_urls),
        }

    def _record_browser_run_script_probe(
        self,
        *,
        tool_name: str,
        page_url: str,
        tool_result: ToolResult,
    ) -> int:
        state = getattr(self, "_tool_progress_script_probe_state", None)
        if not isinstance(state, dict):
            state = {"page_url": "", "count": 0}
            self._tool_progress_script_probe_state = state
        if tool_name != "browser_run_script":
            state["page_url"] = ""
            state["count"] = 0
            return 0

        data = getattr(tool_result, "data", None)
        if not isinstance(data, dict):
            data = {}
        changed_state = any(
            bool(data.get(key))
            for key in (
                "navigated",
                "dom_mutated_after_script",
                "dom_mutated",
                "refs_invalidated",
            )
        )
        if changed_state:
            state["page_url"] = ""
            state["count"] = 0
            return 0

        if page_url and page_url == str(state.get("page_url") or ""):
            count = int(state.get("count") or 0) + 1
        else:
            count = 1
        state["page_url"] = page_url
        state["count"] = count
        return count

    @staticmethod
    def _tool_progress_result_fingerprint(
        *,
        tool_name: str,
        tool_result: ToolResult,
        result_text: str | None,
        empty_result: bool,
    ) -> str:
        """Stable fingerprint for progress detection.

        Snapshots/get_text/status on different pages must produce different
        fingerprints (so the agent's normal multi-page rhythm is not flagged as
        a spin). Click no-effect on the same target must produce IDENTICAL
        fingerprints across attempts (so a true spin trips).
        """
        if empty_result:
            return f"empty::{tool_name}"
        data = getattr(tool_result, "data", None)
        if isinstance(data, dict):
            for marker_key in (
                "page_url",
                "url",
                "title",
                "fingerprint",
                "snapshot_hash",
            ):
                marker_value = data.get(marker_key)
                if isinstance(marker_value, str) and marker_value:
                    return f"{marker_key}::{marker_value[:200]}"
            if tool_name == "browser_interact" and data.get("last_click_no_effect"):
                target = str(data.get("no_effect_target") or "")[:200]
                return f"no_effect::{target}"
        text = (result_text or "").strip()
        if not text:
            return f"empty::{tool_name}"
        return f"hash::{hash(text[:4000])}"

    @staticmethod
    def _tool_progress_has_no_effect_signal(tool_name: str, tool_result: ToolResult) -> bool:
        """Explicit tool-reported no-effect flag (structured, not prose)."""
        data = getattr(tool_result, "data", None)
        if not isinstance(data, dict):
            return False
        if data.get("ok") is False or data.get("error"):
            return True
        if tool_name == "browser_interact":
            if data.get("last_click_no_effect"):
                return True
            if data.get("snapshot_unchanged_after_click") and data.get("url_unchanged_after_click"):
                return True
        return False

    def _set_step_log_llm_context(
        self,
        *,
        tool_choice: str,
        message_count: int,
        continuity_mode: str | None = None,
        server_context_tokens: int | None = None,
        virtual_message_count: int | None = None,
    ) -> None:
        """Capture the LLM-call metadata that produced the upcoming tool step."""
        self._last_tool_choice = tool_choice
        if continuity_mode:
            self._last_continuity_mode = continuity_mode
        if server_context_tokens is not None:
            try:
                self._last_server_context_tokens = max(0, int(server_context_tokens))
            except (TypeError, ValueError):
                logger.debug("Invalid server context token count for trace metadata")
        visible_count = message_count
        if virtual_message_count is not None:
            try:
                visible_count = max(visible_count, int(virtual_message_count))
            except (TypeError, ValueError):
                logger.debug("Invalid virtual message count for trace metadata")
        self._last_message_count = visible_count

    @staticmethod
    def _serialize_tool_surface_for_trace(tool_surface: Any | None) -> Any | None:
        """Convert runtime tool-surface objects to JSON-safe dictionaries."""
        if tool_surface is None:
            return None
        if hasattr(tool_surface, "to_dict"):
            try:
                return tool_surface.to_dict()
            except Exception:
                return str(tool_surface)
        if isinstance(tool_surface, dict):
            return copy.deepcopy(tool_surface)
        return str(tool_surface)

    def _build_task_trace_tool_surface_snapshot(self) -> dict[str, Any]:
        """Capture the effective tool surface for the current LLM turn."""
        tool_schemas = self._build_tool_schema_snapshot()
        snapshot: dict[str, Any] = {
            "visible_tool_names": [tool["name"] for tool in tool_schemas],
            "visible_tool_count": len(tool_schemas),
            "tool_schema_hash": self._hash_tool_schema_snapshot(tool_schemas),
            "allowed_tools": (sorted(self._allowed_tools) if self._allowed_tools else None),
            "rejected_tools": (sorted(self._rejected_tools) if self._rejected_tools else []),
        }
        runtime_tool_surface = self._get_runtime_tool_surface()
        serialized_tool_surface = self._serialize_tool_surface_for_trace(runtime_tool_surface)
        if serialized_tool_surface is not None:
            snapshot["runtime_tool_surface"] = serialized_tool_surface
        return snapshot

    def _build_task_trace_control_state(
        self,
        *,
        llm_response: dict[str, Any] | None = None,
        continue_listening: bool | None = None,
        final_answer: str | None = None,
        final_command: str | None = None,
        payment_gate: bool = False,
        signature_gate: bool = False,
    ) -> dict[str, Any]:
        """Classify the runtime-visible outcome for canonical tracing."""
        response = llm_response if isinstance(llm_response, dict) else {}
        raw_response_type = str(response.get("type") or "").strip().lower()
        answer_text = str(final_answer or "").strip()
        command_text = str(final_command or "").strip()

        typed_outcome = "unknown"
        completion_state = "unknown"
        outcome_origin = "runtime_inferred"

        if payment_gate:
            typed_outcome = "payment_gate"
            completion_state = "handoff"
            outcome_origin = "model_tool"
        elif signature_gate:
            typed_outcome = "signature_gate"
            completion_state = "handoff"
            outcome_origin = "model_tool"
        elif raw_response_type == "tool_call":
            typed_outcome = "action"
            completion_state = "in_progress"
            outcome_origin = "model"
        elif raw_response_type in {"ask_user", "clarification"} or bool(continue_listening):
            typed_outcome = "ask_user"
            completion_state = "awaiting_user"
            if raw_response_type in {
                "ask_user",
                "clarification",
            } or raw_response_type in {"answer", "text", "command"}:
                outcome_origin = "model"
        elif raw_response_type == "final_answer":
            typed_outcome = "final_answer"
            completion_state = "completed"
            outcome_origin = "model"
        elif raw_response_type in {"answer", "text", "command"} or answer_text or command_text:
            typed_outcome = "final_answer"
            completion_state = "completed"
            outcome_origin = "model" if raw_response_type in {"answer", "text", "command"} else "runtime_inferred"

        return {
            "typed_outcome": typed_outcome,
            "completion_state": completion_state,
            "outcome_origin": outcome_origin,
            "raw_response_type": raw_response_type,
            "continue_listening": bool(continue_listening),
        }

    def _build_task_trace_compat_metadata(self) -> dict[str, Any]:
        """Document compatibility-only log fields that no longer drive routing."""
        return {
            "task_category": {
                "value": getattr(self, "_task_category", "unknown"),
                "compatibility_only": True,
                "meaning": "historical step-log label, not a live classifier gate",
            },
            "focused_set": {
                "value": None,
                "compatibility_only": True,
                "meaning": "retired focused routing label; model sees the all-visible tool surface",
            },
            "tool_surface_mode": "all_visible_tools",
        }

    def _build_task_trace_llm_request(
        self,
        *,
        call_kind: str,
        request_mode: str,
        first_turn: bool,
        native_messages: list[dict[str, Any]] | None = None,
        native_kwargs: dict[str, Any] | None = None,
        text_history: list[dict[str, str]] | None = None,
        text_input: str | None = None,
        system_context: str | None = None,
        tool_choice_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "call_kind": call_kind,
            "request_mode": request_mode,
            "first_turn": first_turn,
            "tool_surface": self._build_task_trace_tool_surface_snapshot(),
        }
        if tool_choice_override is not None:
            request_payload["tool_choice_override"] = _trace_safe_copy(tool_choice_override)

        if request_mode == "native":
            request_payload["messages"] = _trace_safe_copy(native_messages or [])
            request_kwargs, provider_private = self._split_trace_native_kwargs(native_kwargs or {})
            request_payload["request_kwargs"] = request_kwargs
            if provider_private:
                request_payload["provider_private_kwargs"] = provider_private
        else:
            request_payload["history"] = _trace_safe_copy(text_history or [])
            request_payload["input"] = text_input or ""
            request_payload["system_context"] = system_context or ""
        return request_payload

    @classmethod
    def _split_trace_native_kwargs(cls, native_kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        shared: dict[str, Any] = {}
        provider_private: dict[str, Any] = {}
        for key, value in native_kwargs.items():
            if key in _PROVIDER_PRIVATE_REQUEST_KWARGS:
                provider_private[key] = cls._provider_private_trace_summary(key, value)
            else:
                shared[key] = _trace_safe_copy(value)
        return shared, provider_private

    @classmethod
    def _task_trace_response_payload(cls, response: dict[str, Any]) -> dict[str, Any]:
        payload = _trace_safe_copy(response)
        reasoning = _normalize_reasoning_for_task_trace(payload)
        if reasoning is not None:
            payload["reasoning"] = reasoning
        provider_private: dict[str, Any] = {}
        for key in list(payload.keys()):
            if key not in _PROVIDER_PRIVATE_RESPONSE_KEYS:
                continue
            provider_private[key.lstrip("_")] = cls._provider_private_trace_summary(key, payload.pop(key))
        if provider_private:
            payload["provider_private"] = provider_private
        return payload

    @staticmethod
    def _provider_private_trace_summary(key: str, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            summary: dict[str, Any] = {
                "type": "dict",
                "keys": sorted(str(item) for item in value.keys())[:20],
                "redacted": True,
            }
            mode = value.get("mode")
            if isinstance(mode, str):
                summary["mode"] = mode
            response_items = value.get("response_items")
            if isinstance(response_items, list):
                summary["response_items_count"] = len(response_items)
            previous_response_id = value.get("previous_response_id")
            if isinstance(previous_response_id, str) and previous_response_id:
                summary["previous_response_id_present"] = True
            if isinstance(value.get("input_cursor"), int):
                summary["input_cursor"] = value["input_cursor"]
            return summary
        if isinstance(value, list):
            return {"type": "list", "items": len(value), "redacted": True}
        if isinstance(value, str):
            return {"type": "str", "present": bool(value), "redacted": bool(value)}
        return {
            "type": type(value).__name__,
            "present": value is not None,
            "redacted": True,
        }

    def _task_trace_provider_metadata(self, provider_obj: Any | None = None) -> dict[str, Any]:
        provider = provider_obj or self._llm
        model_name = self._get_model_name_safe()
        provider_name = type(provider).__name__ if provider is not None else "unknown"
        config = getattr(provider, "config", None)
        config_provider = getattr(config, "provider", None)
        if isinstance(config_provider, str) and config_provider:
            provider_name = config_provider
        metadata: dict[str, Any] = {
            "provider": provider_name,
            "executor_provider_class": type(self._llm).__name__,
            "provider_class": (type(provider).__name__ if provider is not None else "unknown"),
            "model": model_name,
            "model_name": model_name,
            "temperature": self._get_temperature_safe(),
        }
        inner = getattr(provider, "_provider", None)
        if inner is not None:
            metadata["inner_provider_class"] = type(inner).__name__
        compat = getattr(provider, "_compat_provider", None)
        if compat is not None:
            metadata["compat_provider_class"] = type(compat).__name__
        return metadata

    @staticmethod
    def _request_payload_with_provider_metadata(
        request: dict[str, Any],
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        request_payload = _trace_safe_copy(request)
        request_payload["provider"] = _trace_safe_copy(provider_metadata)
        return request_payload

    @staticmethod
    def _trace_error_payload(exc: BaseException) -> dict[str, Any]:
        status_code = None
        for attr in ("status_code", "status", "code"):
            raw = getattr(exc, attr, None)
            if isinstance(raw, int):
                status_code = raw
                break
            if isinstance(raw, str):
                try:
                    status_code = int(raw)
                    break
                except ValueError:
                    continue

        request_id = getattr(exc, "request_id", None)
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if not request_id and headers is not None:
            try:
                request_id = headers.get("x-request-id") or headers.get("request-id")
            except Exception:
                request_id = None

        message = str(exc)
        payload = {
            "type": type(exc).__name__,
            "status_code": status_code,
            "request_id": request_id,
            "message": message[:1000],
            "fingerprint": hashlib.sha256(("%s:%s" % (type(exc).__name__, message)).encode()).hexdigest()[:16],
        }
        for attr_name, trace_key in (
            ("viola_provider_failure", "provider_failure"),
            ("viola_provider_payload", "provider_payload"),
            ("viola_provider_attempts", "provider_attempts"),
        ):
            value = getattr(exc, attr_name, None)
            if isinstance(value, dict):
                payload[trace_key] = _trace_safe_copy(value)
        return payload

    def _append_task_trace_llm_attempt_start(
        self,
        *,
        call_kind: str,
        request_mode: str,
        first_turn: bool,
        continuity_before: dict[str, Any] | None,
        request: dict[str, Any],
        provider_payload: dict[str, Any],
        payload_stage: str,
        provider_obj: Any | None = None,
    ) -> str | None:
        task_trace = getattr(self, "_task_trace", None)
        if task_trace is None:
            return None

        self._task_trace_llm_attempt_seq = getattr(self, "_task_trace_llm_attempt_seq", 0) + 1
        attempt_id = "%s:llm:%04d" % (
            getattr(self, "task_id", task_trace.task_id),
            self._task_trace_llm_attempt_seq,
        )
        self._task_trace_last_attempt_id = attempt_id
        ts = datetime.now(tz=UTC).isoformat()
        provider_metadata = self._task_trace_provider_metadata(provider_obj)
        self._task_trace_last_provider_metadata = _trace_safe_copy(provider_metadata)
        try:
            task_trace.append_llm_attempt_start(
                ts=ts,
                attempt_id=attempt_id,
                call_kind=call_kind,
                request_mode=request_mode,
                provider=_trace_safe_copy(provider_metadata),
                first_turn=first_turn,
                continuity_before=_trace_safe_copy(continuity_before or {}),
                request=self._request_payload_with_provider_metadata(request, provider_metadata),
            )
            task_trace.append_llm_provider_payload(
                ts=ts,
                attempt_id=attempt_id,
                call_kind=call_kind,
                payload_stage=payload_stage,
                payload=_trace_safe_copy(provider_payload),
                exact=True,
            )
        except Exception:
            logger.debug("Task trace LLM attempt start write failed, continuing", exc_info=True)
        return attempt_id

    def _append_task_trace_llm_attempt_response(
        self,
        *,
        attempt_id: str | None,
        call_kind: str,
        response: dict[str, Any],
        continuity_after: dict[str, Any] | None,
    ) -> None:
        task_trace = getattr(self, "_task_trace", None)
        if task_trace is None or not attempt_id:
            return
        try:
            task_trace.append_llm_attempt_response(
                ts=datetime.now(tz=UTC).isoformat(),
                attempt_id=attempt_id,
                call_kind=call_kind,
                response=self._task_trace_response_payload(response),
                continuity_after=_trace_safe_copy(continuity_after or {}),
            )
            for key in (
                "_converted_responses_payload",
                "_responses_payload",
                "_provider_payload",
            ):
                provider_payload = response.get(key)
                if isinstance(provider_payload, dict):
                    task_trace.append_llm_provider_payload(
                        ts=datetime.now(tz=UTC).isoformat(),
                        attempt_id=attempt_id,
                        call_kind=call_kind,
                        payload_stage=key.lstrip("_"),
                        payload=_trace_safe_copy(provider_payload),
                        exact=True,
                    )
                    break
        except Exception:
            logger.debug("Task trace LLM response write failed, continuing", exc_info=True)

    def _append_task_trace_llm_attempt_failure(
        self,
        *,
        attempt_id: str | None,
        call_kind: str,
        exc: BaseException,
    ) -> None:
        task_trace = getattr(self, "_task_trace", None)
        if task_trace is None or not attempt_id:
            return
        try:
            task_trace.append_llm_attempt_failure(
                ts=datetime.now(tz=UTC).isoformat(),
                attempt_id=attempt_id,
                call_kind=call_kind,
                error=self._trace_error_payload(exc),
                continuity_after=(self._export_responses_continuity_state() if self._use_native else {}),
            )
        except Exception:
            logger.debug("Task trace LLM failure write failed, continuing", exc_info=True)

    def _append_task_trace_llm_retry_fallback(
        self,
        *,
        call_kind: str,
        kind: str,
        reason: str,
        next_action: str,
        attempt_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        task_trace = getattr(self, "_task_trace", None)
        if task_trace is None:
            return
        try:
            task_trace.append_llm_retry_fallback(
                ts=datetime.now(tz=UTC).isoformat(),
                attempt_id=attempt_id or getattr(self, "_task_trace_last_attempt_id", None),
                call_kind=call_kind,
                kind=kind,
                reason=reason,
                next_action=next_action,
                detail=copy.deepcopy(detail or {}),
            )
        except Exception:
            logger.debug("Task trace LLM retry/fallback write failed, continuing", exc_info=True)

    def _record_task_trace_llm_exchange(
        self,
        *,
        call_kind: str,
        request_mode: str,
        response: dict[str, Any],
        first_turn: bool,
        continuity_before: dict[str, Any] | None = None,
        native_messages: list[dict[str, Any]] | None = None,
        native_kwargs: dict[str, Any] | None = None,
        text_history: list[dict[str, str]] | None = None,
        text_input: str | None = None,
        system_context: str | None = None,
        tool_choice_override: dict[str, Any] | None = None,
    ) -> None:
        """Capture the provider-visible request/response surface for tracing."""
        request_payload = self._build_task_trace_llm_request(
            call_kind=call_kind,
            request_mode=request_mode,
            first_turn=first_turn,
            native_messages=native_messages,
            native_kwargs=native_kwargs,
            text_history=text_history,
            text_input=text_input,
            system_context=system_context,
            tool_choice_override=tool_choice_override,
        )
        provider_metadata = getattr(self, "_task_trace_last_provider_metadata", None)
        if not isinstance(provider_metadata, dict):
            provider_metadata = self._task_trace_provider_metadata()
        request_payload = self._request_payload_with_provider_metadata(request_payload, provider_metadata)

        self._task_trace_last_request = request_payload
        self._task_trace_last_response = self._task_trace_response_payload(response)
        self._task_trace_last_continuity_before = _trace_safe_copy(continuity_before or {})
        self._task_trace_last_continuity_after = self._export_responses_continuity_state() if self._use_native else {}
        self._append_task_trace_llm_attempt_response(
            attempt_id=getattr(self, "_task_trace_last_attempt_id", None),
            call_kind=call_kind,
            response=response,
            continuity_after=self._task_trace_last_continuity_after,
        )

    def _record_task_trace_tool_execution(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str | None,
        tool_result: ToolResult,
        model_visible_result: Any,
        duration_ms: int,
        tool_error: str | None,
    ) -> None:
        """Capture the exact tool call/result surface that fed the next turn."""
        trace_tool_input = _redact_email_trace_payload(tool_name, copy.deepcopy(tool_input), tool_input)
        trace_model_visible_result = _redact_email_trace_payload(
            tool_name,
            copy.deepcopy(model_visible_result),
            tool_input,
        )
        trace_tool_data = _redact_email_trace_payload(tool_name, copy.deepcopy(tool_result.data), tool_input)
        trace_mcp_content = _redact_email_trace_payload(
            tool_name,
            copy.deepcopy(tool_result.mcp_content),
            tool_input,
        )
        trace_mcp_meta = _redact_email_trace_payload(
            tool_name,
            copy.deepcopy(tool_result.mcp_meta),
            tool_input,
        )
        self._task_trace_last_tool_execution = {
            "tool_name": tool_name,
            "tool_input": redact_card_data(trace_tool_input),
            "tool_use_id": tool_use_id,
            "duration_ms": duration_ms,
            "model_visible_result": redact_card_data(trace_model_visible_result),
            "tool_result": {
                "ok": tool_result.ok,
                "data": redact_card_data(trace_tool_data),
                "mcp_content": redact_card_data(trace_mcp_content),
                "mcp_meta": redact_card_data(trace_mcp_meta),
                "error": (redact_card_data(tool_result.error or "") if tool_result.error else tool_result.error),
                "error_category": tool_result.error_category,
                "retryable": tool_result.retryable,
                "required_tier": tool_result.required_tier,
            },
            "tool_error": (redact_card_data(tool_error or "") if tool_error else tool_error),
            "approval_path": getattr(self, "_step_approval_path", None),
            "page_url": getattr(self, "_last_page_url", None),
        }
        if self._task_trace is not None:
            try:
                self._task_trace.append_tool_execution(
                    ts=datetime.now(tz=UTC).isoformat(),
                    step=None,
                    tool_name=tool_name,
                    ok=tool_result.ok,
                    duration_ms=duration_ms,
                    result=copy.deepcopy(self._task_trace_last_tool_execution),
                    error=tool_error or tool_result.error,
                )
            except Exception:
                logger.debug("Task trace tool execution write failed, continuing", exc_info=True)

    def _append_task_trace_gate_event(
        self,
        *,
        gate: str,
        state: str,
        message: str,
        tool_name: str | None = None,
        page_url: str | None = None,
        gate_origin: str | None = None,
    ) -> None:
        """Append a canonical gate event when handoff state changes."""
        task_trace = getattr(self, "_task_trace", None)
        if task_trace is None:
            return
        task_trace.append_gate(
            ts=datetime.now(tz=UTC).isoformat(),
            gate=gate,
            state=state,
            message=message,
            tool_name=tool_name,
            page_url=page_url or getattr(self, "_last_page_url", None),
            gate_origin=gate_origin,
        )

    def _get_model_name_safe(self) -> str:
        """Extract model name from the LLM handler without crashing."""
        if self._last_model_name and self._last_model_name != "unknown":
            return self._last_model_name
        try:
            effective_model = self._sdk_probe_attr(self._llm, "effective_model")
            if isinstance(effective_model, str) and effective_model:
                return effective_model
            config = self._sdk_probe_attr(self._llm, "config")
            if config is not None:
                config_model = getattr(config, "model", None)
                if isinstance(config_model, str) and config_model:
                    return config_model
            # Direct handler (GptHandler)
            get_model = self._sdk_probe_attr(self._llm, "_get_model")
            if callable(get_model):
                model = get_model()
                if isinstance(model, str) and model:
                    return model
            # ProviderAgnosticRouter wraps a handler in _provider
            inner = self._sdk_probe_attr(self._llm, "_provider")
            inner_effective_model = self._sdk_probe_attr(inner, "effective_model")
            if isinstance(inner_effective_model, str) and inner_effective_model:
                return inner_effective_model
            inner_config = self._sdk_probe_attr(inner, "config")
            if inner_config is not None:
                inner_model = getattr(inner_config, "model", None)
                if isinstance(inner_model, str) and inner_model:
                    return inner_model
            inner_get_model = self._sdk_probe_attr(inner, "_get_model")
            if inner is not None and callable(inner_get_model):
                model = inner_get_model()
                if isinstance(model, str) and model:
                    return model
        except Exception:
            logger.debug("Could not resolve provider model name")
        return "unknown"

    def _get_context_window_safe(self) -> int | None:
        """Extract provider-advertised context window when available."""
        for obj in (self._llm, getattr(self._llm, "_provider", None)):
            if obj is None:
                continue
            try:
                context_window = getattr(obj, "context_window", None)
                if isinstance(context_window, int) and context_window > 0:
                    return context_window
                config = getattr(obj, "config", None)
                config_window = getattr(config, "context_window", None)
                if isinstance(config_window, int) and config_window > 0:
                    return config_window
            except Exception:
                continue
        return None

    def _get_temperature_safe(self) -> float | None:
        """Extract temperature from the LLM handler."""
        try:
            # GptHandler uses 0.3 for non-o1 models, None for o1
            is_o1_model = self._sdk_probe_attr(self._llm, "_is_o1_model")
            get_model = self._sdk_probe_attr(self._llm, "_get_model")
            if callable(is_o1_model) and callable(get_model):
                model = get_model()
                if isinstance(model, str) and is_o1_model(model):
                    return None
            return 0.3
        except Exception:
            return 0.3

    def _count_conversation_turns(self) -> int:
        """Count conversation turns in the current message history."""
        try:
            if self._use_native:
                return len(self._native_messages)
        except Exception:
            logger.debug("Could not count native message history")
        return 0

    # ------------------------------------------------------------------ LA-4
    # Post-tool hook chain infrastructure.  The hooks package provides
    # independently testable hook classes for each piece of post-tool
    # logic (step logging, spin observation, approval bridge,
    # token tracking).  This method builds and runs the chain.
    #
    # MIGRATION NOTE: The inline post-tool bookkeeping in run() remains
    # as the active code path.  _build_hook_chain() and
    # _run_post_tool_hooks() exist for new code and testing.  Once the
    # hooks are validated in production, the inline code should be
    # replaced with a single ``await self._run_post_tool_hooks(...)``
    # call per tool execution.

    def _build_hook_chain(
        self,
        task_log: AgentTaskLog,
        checkpoint: Any,
        spin_detector: SpinDetector,
    ) -> HookChain:
        """Build the post-tool hook chain for a single agent run.

        Returns a :class:`HookChain` with all hooks in the canonical order
        documented in ``intent/hooks/__init__.py``.
        """
        from intent.hooks import HookChain
        from intent.hooks.approval_bridge import ApprovalBridgeHook
        from intent.hooks.runtime_state import RuntimeStateHook
        from intent.hooks.spin_detector import SpinDetectorHook
        from intent.hooks.step_logger import StepLoggerHook
        from intent.hooks.token_tracker import TokenTrackerHook

        return HookChain(
            [
                # 1. Browser URL, API registry, escalation tiers
                RuntimeStateHook(track_page_url_fn=self._track_page_url),
                # 2. Structured step logging, JSONL, WS broadcast, telemetry
                StepLoggerHook(
                    task_log=task_log,
                    emit_step_jsonl_fn=self._emit_step_jsonl,
                    broadcast_fn=self._broadcast_agent_step,
                    summarize_fn=_summarize,
                    summarize_args_fn=_summarize_args,
                    last_usage_fn=lambda: self._last_usage,
                    last_page_url_fn=lambda: self._last_page_url,
                    step_record_cls=AgentStepRecord,
                    checkpoint_step_cls=CheckpointStep,
                    append_step_fn=append_step,
                    checkpoint=checkpoint,
                ),
                # 3. Spin detection recording
                SpinDetectorHook(spin_detector=spin_detector),
                # 4. Approval rejection blacklisting and click backoff
                ApprovalBridgeHook(),
                # 5. Token budget tracking
                TokenTrackerHook(tracker=self._token_tracker),
            ]
        )

    async def _run_post_tool_hooks(
        self,
        hook_chain: HookChain,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: ToolResult,
        tool_error: str | None,
        *,
        iteration: int,
        task_id: str,
        messages: list[dict[str, Any]],
        tools_called: list[str],
    ) -> list[str]:
        """Run the post-tool hook chain and return correction messages.

        Builds a :class:`HookContext`, runs all hooks, and returns any
        correction messages generated by the hooks.
        """
        from intent.hooks.base import HookContext

        ctx = HookContext(
            iteration=iteration,
            task_id=task_id,
            messages=messages,
            visible_tool_allowlist=None,
            tools_called=tools_called,
            use_native=self._use_native,
            browser_tiers_used=self._browser_tiers_used,
            rejected_tools=self._rejected_tools,
            allowed_tools=self._allowed_tools,
            consecutive_failures=self._consecutive_failures,
        )
        await hook_chain.run(
            tool_name,
            tool_input,
            tool_result,
            tool_error,
            ctx,
        )
        # Sync mutable state back from context
        self._consecutive_failures = ctx.consecutive_failures
        return ctx.correction_messages

    def _track_page_url(self, tool_name: str, tool_result: ToolResult) -> None:
        """Extract and cache the current page URL from browser tool results."""
        if tool_name not in _BROWSER_TOOLS:
            return
        if not tool_result.ok or not isinstance(tool_result.data, dict):
            return
        url = tool_result.data.get("url")
        if isinstance(url, str) and url:
            self._last_page_url = url
            self._update_overlay_for_browser(url)

    def _update_overlay_for_browser(self, url: str) -> None:
        """Show or update the agentic overlay when browser tools are used."""
        if self._overlay is None:
            return
        try:
            if not self._overlay_shown:
                self._overlay.show_agentic(
                    url=url,
                    task_description=self._agent_task_description,
                    user_id=self._get_effective_user_id(),
                )
                self._overlay_shown = True
            else:
                self._overlay.update_agentic_status(url=url)
        except Exception as exc:
            logger.debug("Overlay update failed: %s", exc)

    def _get_parent_native_tools_exact(self) -> list[dict[str, Any]] | None:
        """Return the parent's raw provider-native tool list without depth filtering."""

        if self._native_tools is not None:
            return copy.deepcopy(self._native_tools)
        tool_surface = self._get_runtime_tool_surface()
        if tool_surface is not None:
            if hasattr(tool_surface, "provider_native"):
                return copy.deepcopy(list(tool_surface.provider_native or []))
            if isinstance(tool_surface, dict):
                provider_native = tool_surface.get("provider_native")
                if isinstance(provider_native, list):
                    return copy.deepcopy(provider_native)
        provider_tools = getattr(self._llm, "_native_tools", None)
        if isinstance(provider_tools, list):
            return copy.deepcopy(provider_tools)
        try:
            if self._mcp_hub is not None:
                hub_tools = self._mcp_hub.list_tools()
                if isinstance(hub_tools, list):
                    return copy.deepcopy(hub_tools)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.debug("Could not read parent tool surface from MCP hub", exc_info=True)
        return None

    def _native_tools_for_child(self, *, subagent_mode: str, subagent_type: str | None) -> list[dict[str, Any]] | None:
        """Return the child provider tool schemas for a fork or typed subagent."""

        tools = self._get_parent_native_tools_exact()
        if tools is None:
            return None
        if normalize_subagent_mode(subagent_mode) == "fork":
            return tools

        from intent.agent_subagent_types import (
            allowed_tools_for_subagent,
            is_full_tool_surface,
        )

        allowed = allowed_tools_for_subagent(subagent_type)
        if is_full_tool_surface(allowed):
            return [
                tool
                for tool in tools
                if str(tool.get("name") or "").strip() not in {"check_agents", "cancel_agent", "send_message"}
            ]
        return [tool for tool in tools if str(tool.get("name") or "").strip() in allowed]

    def _child_prompt_bundle(
        self,
        *,
        mode: str,
        subagent_type: str | None,
        prompt_context_bundle: PromptFrameBundle | None = None,
    ) -> PromptFrameBundle:
        """Build the prompt-frame bundle visible to a child agent."""

        from intent.agent_subagent_types import subagent_identity_sentence

        source = prompt_context_bundle if prompt_context_bundle is not None else self._request_context_bundle
        bundle = clone_prompt_bundle_for_subagent(source, normalize_subagent_mode(mode))
        return append_system_text(
            bundle,
            subagent_identity_sentence(subagent_type),
            origin="subagent_identity",
        )

    def _child_lifecycle_context(
        self,
        *,
        agent_id: str,
        agent_type: str | None,
        session_id: str,
        task: str,
        mode: str,
        result: str | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "agent_id": agent_id,
            "agent_type": agent_type or "",
            "agent_transcript_path": str(subagent_transcript_path(agent_id, user_id=self._get_effective_user_id())),
            "task": task,
            "parent_task_id": getattr(self, "task_id", None),
            "user_id": self._get_effective_user_id(),
            "session_id": session_id,
            "mode": normalize_subagent_mode(mode),
        }
        if result is not None:
            context["result"] = result
            context["last_assistant_message"] = result
        if status is not None:
            context["status"] = status
        if error is not None:
            context["error"] = error
        return context

    def _emit_subagent_task_notification_frame(
        self,
        *,
        agent_id: str,
        subagent_type: str | None,
        status: str,
        elapsed_ms: int,
        final_text: str = "",
        error: str | None = None,
        target_agent_id: str | None = None,
    ) -> Any:
        """Queue and optionally persist a Claude-shaped task-notification frame."""

        from intent.agent_subagent_types import build_task_notification_frame

        user_id = self._get_effective_user_id()
        output_file = str(subagent_transcript_path(agent_id, user_id=user_id))
        frame = build_task_notification_frame(
            agent_id=agent_id,
            subagent_type=subagent_type,
            status=status,
            elapsed_ms=elapsed_ms,
            final_text=final_text,
            output_file=output_file,
            error=error,
        )
        notification = SubagentNotification(
            agent_id=agent_id,
            user_id=user_id,
            event_type="task-notification",
            payload=copy.deepcopy(frame.extra),
            frame=frame,
            frame_uuid=frame.uuid,
            parent_session_id=self._session_id,
            target_agent_id=target_agent_id,
            created_at_ms=frame.timestamp_ms,
        )
        subagent_lifecycle.enqueue_notification(notification)
        manager = getattr(self, "_conversation_state_manager", None)
        if manager is not None and hasattr(manager, "add_message"):
            try:
                manager.add_message(frame)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                logger.debug("Subagent task notification frame persistence failed", exc_info=True)
        return frame

    def _with_drained_subagent_notifications(
        self,
        bundle: PromptFrameBundle | None,
    ) -> PromptFrameBundle:
        """Drain task notifications addressed to this executor into a prompt bundle."""

        user_id = self._get_effective_user_id()
        notifications = subagent_lifecycle.drain_notifications(
            user_id=user_id,
            parent_session_id=self._session_id,
            current_agent_id=getattr(self, "_current_agent_id", None),
        )
        frames = []
        for notification in notifications:
            if notification.frame is None:
                continue
            frame = copy.deepcopy(notification.frame)
            frame.extra["delivered"] = True
            frames.append(frame)
        return bundle_with_drained_notifications(bundle, frames)

    async def _run_child_agent(
        self,
        task: str,
        max_steps: int,
        return_format: str,
        parent_checkpoint: TaskCheckpoint,
        external_cancel_event: asyncio.Event | None = None,
        *,
        subagent_mode: str = "fresh",
        subagent_type: str | None = None,
        task_id_override: str | None = None,
        mode: str | None = None,
        agent_id: str | None = None,
        name: str | None = None,
        prompt_context_bundle: PromptFrameBundle | None = None,
        resume_messages: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
        allow_shared_state_tools: bool = True,
    ) -> ToolResult:
        """Spawn a child AgentExecutor for an independent subtask.

        The child gets its own fresh context, timeout budget, checkpoint,
        spin detector, and rejected_tools set.  Budget is fully isolated
        from the parent -- the child's timeout is wall-clock based and does
        NOT deduct from the parent's remaining budget.
        After the child finishes, the parent's timeout is extended by the
        wall-clock time the child consumed so the parent is not penalised.

        **Shared state:**

        - ``mcp_hub``: Foreground ``spawn_subtask`` children may share the hub
          because the parent is blocked and gets a browser-state footer after
          completion. Background ``start_agent`` callers must pass
          ``allow_shared_state_tools=False`` so browser/payment/desktop tools
          fail closed before the parent is unblocked.
        - ``approval_manager``: Approval / pre-approved state is shared so
          user approval decisions are consistent across parent and child.
          However, ``_rejected_tools`` is per-instance and fully isolated.

        Args:
            task: Self-contained task description.
            max_steps: Deprecated compatibility hint; not an iteration cap.
            return_format: "summary" or "data".
            parent_checkpoint: Parent's checkpoint (for linking child IDs).

        Returns:
            ToolResult containing the child's final answer.
        """
        if mode is not None:
            subagent_mode = mode
        child_mode = normalize_subagent_mode(subagent_mode)
        child_agent_id = str(task_id_override or agent_id or "").strip() or "agent_%s" % uuid.uuid4().hex[:12]
        child_name = str(name or "").strip() or None
        child_session_id = make_subagent_session_id(self._session_id, child_agent_id)
        child_depth = self._depth + 1
        logger.info(
            "Spawning child agent: depth=%d, max_steps_hint=%d, task=%s",
            child_depth,
            max_steps,
            task[:100],
        )

        # â”€â”€ Timeout isolation (B4 fix) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Child timeout is seconds-bounded, not step-bounded. After the child
        # returns, the parent's timeout is extended by the consumed wall time.
        child_timeout = _DEFAULT_TIMEOUT
        child_wall_start = time.monotonic()

        child_native_tools = self._native_tools_for_child(
            subagent_mode=child_mode,
            subagent_type=subagent_type,
        )
        if not allow_shared_state_tools:
            shared_names = _shared_state_tool_names(child_native_tools)
            if shared_names:
                return ToolResult(
                    ok=False,
                    error=_background_shared_state_error(shared_names),
                    error_category="SUBAGENT_SHARED_STATE_TOOLS",
                    retryable=False,
                )
        child_prompt_bundle = self._child_prompt_bundle(
            mode=child_mode,
            subagent_type=subagent_type,
            prompt_context_bundle=prompt_context_bundle,
        )

        try:
            ensure_subagent_transcript_seeded(
                agent_id=child_agent_id,
                user_id=self._get_effective_user_id(),
                session_id=child_session_id,
                mode=child_mode,
                status="running",
                agent_type=subagent_type,
                parent_session_id=self._session_id,
                parent_agent_id=getattr(self, "_current_agent_id", None) or getattr(self, "task_id", None),
                name=child_name,
                messages=resume_messages or None,
            )
        except (OSError, TypeError, ValueError):
            logger.debug("Subagent transcript seed failed for %s", child_agent_id, exc_info=True)

        child = AgentExecutor(
            llm_caller=self._llm,
            approval_manager=self._approval.clone_for_subagent(),
            # NOTE(B4): mcp_hub (and therefore browser state) is shared.
            # If both parent and child use browser tools they will compete
            # for the same browser instance.  Architectural fix deferred.
            mcp_hub=self._mcp_hub,
            tts_speaker=None,  # children are silent
            channel=None,
            total_timeout=child_timeout,
            tool_timeout=self._tool_timeout,
            overlay_controller=None,  # children don't touch overlay
            depth=child_depth,
            parent_task_id=self.task_id,
            user_id=self._get_effective_user_id(),
            session_id=child_session_id,
            model_override=model_override or self._model_override,
            native_tools=child_native_tools,
            tool_surface=self._request_tool_surface,
            context_bundle=child_prompt_bundle,
            conversation_state_manager=self._conversation_state_manager,
            current_agent_id=child_agent_id,
            current_agent_name=child_name,
            subagent_mode=child_mode,
            subagent_type=subagent_type,
            hook_registry=self._hook_registry,
            hook_settings_runner=self._hook_settings_runner,
            allowed_tools=self._allowed_tools,
        )
        child.task_id = child_agent_id

        external_cancel_relay: asyncio.Task[None] | None = None
        if external_cancel_event is not None:

            async def _relay_external_cancel() -> None:
                await external_cancel_event.wait()
                child.cancel()

            external_cancel_relay = asyncio.create_task(_relay_external_cancel())

        # The child needs an initial tool call from the LLM.
        # Simulate by making an LLM call with the subtask as user input.
        try:
            from intent.hooks.dispatcher import dispatch_lifecycle

            start_hook = dispatch_lifecycle(
                "SubagentStart",
                self._child_lifecycle_context(
                    agent_id=child_agent_id,
                    agent_type=subagent_type,
                    session_id=child_session_id,
                    task=task,
                    mode=child_mode,
                ),
                session_id=child_session_id,
            )
            child_prompt_bundle = _bundle_with_hook_result_frames(
                child_prompt_bundle,
                "SubagentStart",
                start_hook,
                session_id=child_session_id,
                task_id=child_agent_id,
                agent_id=child_agent_id,
            )
            child._request_context_bundle = copy.deepcopy(child_prompt_bundle)
            child._prompt_context_bundle = copy.deepcopy(child_prompt_bundle)

            # Build system context for the child (minimal -- no parent history)
            from services.llm.prompts import build_minimal_agent_prompt

            # Propagate the parent's channel to subtask prompts so children
            # keep the same unified runtime context as the parent.
            _parent_channel = getattr(self, "_channel", None)
            _child_channel_type = (
                getattr(_parent_channel, "channel_type", None) if _parent_channel is not None else None
            )
            # Inherit parent's per-turn context (memory, profile, prefs,
            # active agents) so the child does not have to re-fetch it.
            _parent_context = (
                getattr(self, "_system_context_text", None) or getattr(self, "_cached_system_context", None) or ""
            )
            child_system = build_minimal_agent_prompt(
                system_context=_parent_context,
                native_tools=self._use_native,
                channel_type=_child_channel_type,
            )

            if child._use_native:
                if resume_messages is not None:
                    child_messages = copy.deepcopy(resume_messages)
                elif child_mode == "fork":
                    child_messages = build_forked_messages(self._native_messages, task)
                else:
                    child_messages = [{"role": "user", "content": task}]
            else:
                logger.error("Child agent requested without native tool calling; refusing legacy text routing")
                return ToolResult(
                    ok=False,
                    error=_NATIVE_TOOL_CALLING_UNSUPPORTED_TEXT,
                    error_category="SUBAGENT_PROVIDER_UNSUPPORTED",
                    retryable=False,
                )

            # Run the child's first model turn and all follow-up turns through
            # the single canonical loop. Direct-answer children must receive
            # the same finalization, usage settlement, compaction, and terminal
            # hook behavior as tool-using children.
            child_result = await child.run(
                user_text=task,
                initial_tool_call=None,
                system_context=child_system,
                restored_native_messages=child_messages,
                restored_messages_include_user_text=True,
            )
            if not child_result.ok:
                child_error = str(child_result.error or child_result.answer or "Child agent failed.").strip()
                try:
                    subagent_lifecycle.update_status(child_agent_id, "failed", user_id=self._get_effective_user_id())
                except (RuntimeError, TypeError, ValueError):
                    logger.debug(
                        "Subagent failure status update failed for %s",
                        child_agent_id,
                        exc_info=True,
                    )
                return ToolResult(
                    ok=False,
                    error="Subtask failed: %s" % child_error[:200],
                    error_category="SUBAGENT_FAILED",
                    retryable=False,
                )

            # Link child to parent checkpoint
            if hasattr(child, "task_id"):
                parent_checkpoint.child_task_ids.append(child.task_id)

            child_answer = child_result.answer or "Subtask completed but produced no summary."
            answer_text = str(child_answer)
            child_record_messages = copy.deepcopy(child._native_messages if child._use_native else child._text_messages)
            if answer_text:
                last_message = child_record_messages[-1] if child_record_messages else {}
                last_role = str(last_message.get("role") or "") if isinstance(last_message, dict) else ""
                last_content = last_message.get("content") if isinstance(last_message, dict) else None
                if last_role != "assistant" or answer_text not in str(last_content or ""):
                    child_record_messages.append({"role": "assistant", "content": answer_text})
            if child_record_messages:
                subagent_lifecycle.record_provider_messages(
                    child_agent_id,
                    child_record_messages,
                    user_id=self._get_effective_user_id(),
                    session_id=child_session_id,
                    mode=child_mode,
                    replace=True,
                )
            subagent_lifecycle.update_status(child_agent_id, "completed", user_id=self._get_effective_user_id())
            if not getattr(child, "_subagent_stop_dispatched", False):
                try:
                    dispatch_lifecycle(
                        "SubagentStop",
                        self._child_lifecycle_context(
                            agent_id=child_agent_id,
                            agent_type=subagent_type,
                            session_id=child_session_id,
                            task=task,
                            mode=child_mode,
                            result=answer_text,
                            status="completed",
                        ),
                        session_id=child_session_id,
                    )
                except (RuntimeError, TypeError, ValueError):
                    logger.debug(
                        "SubagentStop hook dispatch failed for %s",
                        child_agent_id,
                        exc_info=True,
                    )
            return _child_success_result(answer_text, child_result=child_result)

        except Exception as exc:
            self._append_task_trace_llm_attempt_failure(
                attempt_id=locals().get("_child_attempt_id"),
                call_kind="child_initial_tool",
                exc=exc,
            )
            logger.exception("Child agent failed for task: %s", task[:80])
            try:
                subagent_lifecycle.update_status(child_agent_id, "failed", user_id=self._get_effective_user_id())
            except (RuntimeError, TypeError, ValueError):
                logger.debug(
                    "Subagent failure status update failed for %s",
                    child_agent_id,
                    exc_info=True,
                )
            return ToolResult(
                ok=False,
                error="Subtask failed: %s" % str(exc)[:200],
                error_category="SUBAGENT_FAILED",
                retryable=False,
            )

        finally:
            if external_cancel_relay is not None:
                external_cancel_relay.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await external_cancel_relay
            # â”€â”€ B4: compensate parent timeout for child wall-clock â”€â”€â”€
            # The parent's wall-clock timer kept ticking while it awaited
            # the child.  Extend the parent's timeout by the time the
            # child consumed so the parent retains its full remaining
            # budget for its own work.
            child_wall_elapsed = time.monotonic() - child_wall_start
            self._total_timeout += child_wall_elapsed
            logger.debug(
                "Child finished in %.1fs; extended parent timeout by same " "amount to %.1fs",
                child_wall_elapsed,
                self._total_timeout,
            )

    @staticmethod
    def _consume_abandoned_tool_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("Tool execution task was cancelled")
        except Exception as exc:
            logger.debug("Abandoned tool task finished with error: %s", exc)

    async def _cancel_waiter_task(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _cancel_tool_task(self, task: asyncio.Task[Any], tool_name: str) -> None:
        if task.done():
            return
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=1.0)
        if done:
            self._consume_abandoned_tool_task(task)
            return
        task.add_done_callback(self._consume_abandoned_tool_task)
        logger.warning(
            "Tool '%s' did not acknowledge cancellation within 1.0s; returning control to the agent loop",
            tool_name,
        )

    @staticmethod
    def _is_browser_takeover_tool(tool_name: str) -> bool:
        return tool_name in _BROWSER_TOOLS or tool_name == "fill_payment_details" or tool_name.startswith("browser_")

    @staticmethod
    def _is_local_side_effect_tool(tool_name: str) -> bool:
        """Cheap, in-process, must-complete side-effecting tools (e.g. arming a
        timer) that must not be cancel-dropped by a wall-clock timeout under
        load. See ``_LOCAL_SIDE_EFFECT_TOOLS`` (#2010/#2549)."""
        return str(tool_name or "").strip().lower() in _LOCAL_SIDE_EFFECT_TOOLS

    @staticmethod
    def get_tool_interrupt_behavior(tool_name: str) -> str:
        lower_name = str(tool_name or "").strip().lower()
        if not lower_name:
            return "block"
        if lower_name in _BLOCK_ON_INTERRUPT_TOOLS or lower_name.startswith(_BLOCK_ON_INTERRUPT_PREFIXES):
            return "block"
        if lower_name in _PARALLEL_READ_ONLY_TOOLS or lower_name in _PARALLEL_READ_ONLY_ACTIONS:
            return "cancel"
        return "block"

    @staticmethod
    def synthetic_cancel_tool_result(tool_use_id: str, reason: str = "user_interrupted") -> dict[str, Any]:
        reason_text = str(reason or "user_interrupted")
        block = build_synthetic_tool_result_block(tool_use_id, reason_text)
        block["_synthetic_reason"] = reason_text
        return block

    async def _interrupt_active_browser_operation(self, tool_name: str) -> None:
        if not self._is_browser_takeover_tool(tool_name):
            return
        try:
            from mcp_servers.browser_cdp.server import cancel_active_operation

            await cancel_active_operation()
        except Exception:
            logger.debug(
                "Browser operation interruption hook failed for %s",
                tool_name,
                exc_info=True,
            )

    async def _run_tool_with_cancel(
        self,
        coro: Awaitable[dict[str, Any]],
        *,
        tool_name: str,
        timeout_seconds: float,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        """Run a hub tool call until completion, cancellation, or timeout."""

        def _signal_abort() -> None:
            if abort_signal is None:
                return
            setter = getattr(abort_signal, "set", None)
            if callable(setter):
                setter()
                return
            cancel = getattr(abort_signal, "cancel", None)
            if callable(cancel):
                cancel()

        interrupt_behavior = self.get_tool_interrupt_behavior(tool_name)
        if self._cancel_event.is_set() and interrupt_behavior == "cancel":
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise _ToolExecutionCancelled("Tool execution cancelled before submission")

        tool_task = asyncio.ensure_future(coro)
        cancel_wait = asyncio.ensure_future(self._cancel_event.wait()) if interrupt_behavior == "cancel" else None
        takeover_wait: asyncio.Task[bool] | None = None
        takeover_event = getattr(self, "_takeover_interrupt_event", None)
        if self._is_browser_takeover_tool(tool_name) and takeover_event is not None:
            if not getattr(self, "_takeover_active", False):
                takeover_event.clear()
            takeover_wait = asyncio.ensure_future(takeover_event.wait())
        timeout_task = asyncio.ensure_future(asyncio.sleep(timeout_seconds))
        try:
            wait_set: set[asyncio.Task[Any]] = {tool_task, timeout_task}
            if cancel_wait is not None:
                wait_set.add(cancel_wait)
            if takeover_wait is not None:
                wait_set.add(takeover_wait)
            done, _pending = await asyncio.wait(
                wait_set,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if tool_task in done:
                return tool_task.result()
            if takeover_wait is not None and takeover_wait in done:
                _signal_abort()
                await self._interrupt_active_browser_operation(tool_name)
                await self._cancel_tool_task(tool_task, tool_name)
                raise _ToolExecutionInterruptedByTakeover("Browser action interrupted by user takeover")
            if cancel_wait is not None and cancel_wait in done:
                self._cancelled = True
                _signal_abort()
                await self._cancel_tool_task(tool_task, tool_name)
                raise _ToolExecutionCancelled("Tool execution cancelled by user")
            if timeout_task in done:
                # #2010/#2549: a cheap, in-process, side-effecting local tool
                # (arming a timer) must not have its side effect DROPPED just
                # because a wall-clock deadline elapsed while an overloaded
                # event loop starved the coroutine of turns. Grant the in-flight
                # local task a bounded grace to finish before cancelling it, so
                # a starved-but-not-broken loop still lands the mutation. Only
                # if it still cannot finish do we fall through to the honest
                # TIMEOUT (never a false success -- the honesty half is #2543).
                if self._is_local_side_effect_tool(tool_name) and not tool_task.done():
                    grace_done, _grace_pending = await asyncio.wait(
                        {tool_task},
                        timeout=_LOCAL_SIDE_EFFECT_TIMEOUT_GRACE_SECS,
                    )
                    if tool_task in grace_done:
                        logger.warning(
                            "Tool '%s' exceeded %.1fs budget but completed within the "
                            "local side-effect grace; preserving its side effect under load",
                            tool_name,
                            timeout_seconds,
                        )
                        return tool_task.result()
                _signal_abort()
                await self._cancel_tool_task(tool_task, tool_name)
                raise _ToolExecutionTimedOut(timeout_seconds)
            raise RuntimeError("Tool wait completed without a terminal task")
        except (
            _ToolExecutionCancelled,
            _ToolExecutionTimedOut,
            _ToolExecutionInterruptedByTakeover,
        ):
            raise
        except BaseException:
            if not tool_task.done():
                await self._cancel_tool_task(tool_task, tool_name)
            raise
        finally:
            if cancel_wait is not None:
                await self._cancel_waiter_task(cancel_wait)
            await self._cancel_waiter_task(timeout_task)
            if takeover_wait is not None:
                await self._cancel_waiter_task(takeover_wait)

    def _irreversible_tool_schema(self, tool_name: str) -> dict[str, Any]:
        schemas = getattr(self._mcp_hub, "_tool_schemas", {}) or {}
        schema = schemas.get(tool_name, {}) if isinstance(schemas, dict) else {}
        return schema if isinstance(schema, dict) else {}

    def _irreversible_confirmation_class(self, tool_name: str, tool_args: dict[str, Any]) -> str | None:
        return irreversible_action_class(
            tool_name,
            tool_args,
            self._irreversible_tool_schema(tool_name),
        )

    def _remember_irreversible_confirmation(self, action_class: str) -> None:
        if action_class in _ONE_SHOT_IRREVERSIBLE_CONFIRMATION_CLASSES:
            return
        confirmed = getattr(self, "_irreversible_confirmed_action_classes", None)
        if confirmed is None:
            confirmed = set()
            self._irreversible_confirmed_action_classes = confirmed
        confirmed.add(action_class)

    def _is_irreversible_confirmation_remembered(self, action_class: str) -> bool:
        if action_class in _ONE_SHOT_IRREVERSIBLE_CONFIRMATION_CLASSES:
            return False
        confirmed = getattr(self, "_irreversible_confirmed_action_classes", None)
        return isinstance(confirmed, set) and action_class in confirmed

    async def _broadcast_consult_user_event(
        self,
        *,
        tool_name: str,
        action_class: str,
        planned_action: str,
        status: str,
        response: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "action_class": action_class,
            "planned_action": planned_action,
            "message": planned_action,
            "status": status,
            "choices": ["yes", "no", "cancel"],
        }
        task_id = getattr(self, "task_id", None)
        if task_id:
            payload["task_id"] = task_id
        if response is not None:
            payload["response"] = response

        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is not None:
                await hub.broadcast(
                    "consult_user",
                    payload,
                    user_id=self._get_effective_user_id(),
                    force=True,
                )
        except Exception as exc:
            logger.debug("Consult-user WS broadcast failed for %s: %s", tool_name, exc)

    async def _confirm_irreversible_tool_call(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        action_class = self._irreversible_confirmation_class(tool_name, tool_args)
        if action_class is None:
            return True
        if self._is_irreversible_confirmation_remembered(action_class):
            logger.info(
                "Irreversible confirmation reused for class=%s tool=%s",
                action_class,
                tool_name,
            )
            return True

        planned_action = describe_irreversible_action(tool_name, tool_args)
        await self._broadcast_consult_user_event(
            tool_name=tool_name,
            action_class=action_class,
            planned_action=planned_action,
            status="pending",
        )

        approval = getattr(self, "_approval", None)
        request_approval = getattr(approval, "request_approval", None)
        if not callable(request_approval):
            logger.info(
                "No ApprovalManager available for irreversible confirmation: class=%s tool=%s",
                action_class,
                tool_name,
            )
            await self._broadcast_consult_user_event(
                tool_name=tool_name,
                action_class=action_class,
                planned_action=planned_action,
                status="unavailable",
            )
            return False

        try:
            approved = await request_approval(
                action_description=planned_action,
                risk=RiskLevel.DANGEROUS,
                tool_name=tool_name,
                tool_args=tool_args,
                task_id=getattr(self, "task_id", None),
            )
        except ConfirmationDeferred:
            await self._broadcast_consult_user_event(
                tool_name=tool_name,
                action_class=action_class,
                planned_action=planned_action,
                status="deferred",
            )
            raise

        if approved:
            self._remember_irreversible_confirmation(action_class)
            await self._broadcast_consult_user_event(
                tool_name=tool_name,
                action_class=action_class,
                planned_action=planned_action,
                status="approved",
            )
            return True

        await self._broadcast_consult_user_event(
            tool_name=tool_name,
            action_class=action_class,
            planned_action=planned_action,
            status="denied",
        )
        return False

    async def _execute_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        tool_use_id: str | None = None,
        on_progress: Any | None = None,
        abort_signal: asyncio.Event | None = None,
    ) -> ToolResult:
        """Execute a tool through the MCP hub.

        Special handling for ``spawn_subtask`` -- intercepted here to create
        a child ``AgentExecutor`` directly, avoiding circular dependency
        through the MCP server.

        Approval is handled internally by the hub's ApprovalBridge.

        On failure, the result is enriched with structured error context
        (category, retryable) from the ViolaError hierarchy and the error
        classification system. Tool failures are also wired to
        ``emit_failure()`` so they flow through coalescing, the error
        registry, metrics, and Sentry.

        Args:
            tool_name: Name of the tool to execute.
            tool_args: Arguments to pass to the tool.

        Returns:
            ToolResult with ok/data/error and enriched error fields.
        """
        if tool_name != "answer" and not _launch_tools_kill_switch_open():
            return ToolResult(
                ok=False,
                error=_TOOLS_KILL_SWITCH_ERROR,
                error_category="SUBSYSTEM_DISABLED",
                retryable=True,
            )

        # Intercept spawn_subtask: handle via child AgentExecutor
        if tool_name == "spawn_subtask":
            return await self._handle_spawn_subtask(tool_args)

        # Item 4: Intercept spawn_parallel_subtasks
        if tool_name == "spawn_parallel_subtasks":
            return await self._handle_spawn_parallel_subtasks(tool_args)

        # Orchestrator dispatch tools — async background-agent registry.
        if tool_name == "start_agent":
            return await self._handle_start_agent(tool_args)
        if tool_name == "check_agents":
            return await self._handle_check_agents(tool_args)
        if tool_name == "cancel_agent":
            return await self._handle_cancel_agent(tool_args)
        if tool_name == "send_message":
            return await self._handle_send_message(tool_args)

        # Intercept "answer" tool: the LLM is signalling it wants to
        # respond with text, not invoke a real MCP tool.  This happens
        # when the model emits a tool_call
        # for the ASK-tier "answer" function.  We extract the text and
        # return it as a successful result; the caller is responsible
        # for setting self._final_answer to terminate the loop.
        if tool_name == "answer":
            answer_text = tool_args.get("answer", tool_args.get("text", ""))
            if not isinstance(answer_text, str):
                answer_text = str(answer_text) if answer_text else ""
            # 2H: Post-task suggestions â€” append follow-up if provided
            suggest = tool_args.get("suggest_followup", "")
            if suggest and isinstance(suggest, str) and suggest.strip():
                answer_text = "%s %s" % (answer_text.rstrip(), suggest.strip())
                # Set continue_listening so Viola keeps the mic open
                tool_args["continue_listening"] = True
                logger.info("2H: appended follow-up suggestion: %.80s", suggest)
            logger.info(
                "Agent intercepted 'answer' tool call (len=%d) â€” treating as final response",
                len(answer_text),
            )
            return ToolResult(ok=True, data={"answer": answer_text})

        if _is_signature_review_request(tool_name, tool_args):
            gate_message = _signature_gate_message_from_request(tool_args)
            _set_final_response(self, gate_message, continue_listening=False)
            self._signature_gate_requested = True
            self._append_task_trace_gate_event(
                gate="signature",
                state="requested",
                message=gate_message,
                tool_name=tool_name,
                gate_origin="model_tool",
            )
            logger.info("Signature handoff requested explicitly via signature tool")
            return ToolResult(
                ok=True,
                data={
                    "answer": gate_message,
                    "signature_gate": True,
                    "gate_origin": "model_tool",
                },
            )

        if _is_payment_review_request(tool_name, tool_args):
            gate_message = _payment_gate_message_from_request(tool_args)
            self._last_payment_review_request = copy.deepcopy(tool_args)
            review_page_url = (
                _payment_review_url_from_request(tool_args) or str(getattr(self, "_last_page_url", None) or "").strip()
            )
            self._last_payment_review_page_url = review_page_url or None
            _set_final_response(self, gate_message, continue_listening=False)
            self._payment_gate_requested = True
            self._append_task_trace_gate_event(
                gate="payment",
                state="requested",
                message=gate_message,
                tool_name=tool_name,
                page_url=review_page_url or None,
                gate_origin="model_tool",
            )
            logger.info("Payment handoff requested explicitly via payment tool")
            return ToolResult(
                ok=True,
                data={
                    "answer": gate_message,
                    "payment_gate": True,
                    "gate_origin": "model_tool",
                },
            )

        # Payment safety interceptor: block card-like data in any fill tool.
        # The model must recover by calling the explicit payment review tool.
        _pay_values_to_check: list[str] = []
        if tool_name == "browser_fill_form":
            for field in tool_args.get("fields", []):
                _pay_values_to_check.append(str(field.get("value", field.get("text", ""))))
        elif tool_name == "browser_interact" and tool_args.get("value"):
            _pay_values_to_check.append(str(tool_args["value"]))

        for val in _pay_values_to_check:
            stripped = val.replace(" ", "").replace("-", "")
            _is_card = bool(re.match(r"^\d{13,19}$", stripped))
            _is_expiry = bool(re.match(r"^\d{2}/\d{2,4}$", val.strip()))
            if _is_card or _is_expiry:
                _kind = "card number" if _is_card else "expiry date"
                logger.warning(
                    "PAYMENT SAFETY: blocked %s in %s; payment review tool required",
                    _kind,
                    tool_name,
                )
                return ToolResult(
                    ok=False,
                    error=(
                        "PAYMENT SAFETY VIOLATION: payment card data was blocked. "
                        'Call payment(action="request_review", ...) with the merchant, total, and order summary '
                        "before attempting any card, expiry, CVV, payment method, or final order action."
                    ),
                    data={
                        "ok": False,
                        "error_category": "payment_review_required",
                        "required_tool": "payment",
                        "required_action": "request_review",
                        "blocked_field_kind": _kind,
                    },
                    error_category="payment_review_required",
                )
        idempotency_action = classify_action(tool_name, tool_args)
        if idempotency_action:
            try:
                user_scope = self._get_effective_user_id()
                duplicate = get_idempotency_store().check_action_duplicate(
                    idempotency_action,
                    tool_args,
                    user_id=user_scope,
                )
                if duplicate is not None:
                    logger.info(
                        "Idempotency duplicate blocked for action_class=%s behavior=%s age=%.1fs",
                        duplicate.action_class,
                        duplicate.behavior,
                        duplicate.age_seconds,
                    )
                    payload = {
                        "idempotency_duplicate": True,
                        "action_class": duplicate.action_class,
                        "behavior": duplicate.behavior,
                        "age_seconds": round(duplicate.age_seconds),
                    }
                    if duplicate.behavior == "silent_dedup":
                        payload["message"] = duplicate.message
                        return ToolResult(ok=True, data=payload)

                    _set_final_response(
                        self,
                        duplicate.message,
                        continue_listening=duplicate.behavior in {"confirm", "soft_dedup"},
                    )
                    payload["answer"] = duplicate.message
                    return ToolResult(ok=True, data=payload)
            except Exception:
                logger.debug("Action idempotency check skipped for %s", tool_name, exc_info=True)

        irreversible_action = self._irreversible_confirmation_class(tool_name, tool_args)
        irreversible_confirmation_granted = False
        if irreversible_action is not None:
            try:
                confirmed = await self._confirm_irreversible_tool_call(tool_name, tool_args)
            except ConfirmationDeferred as deferred:
                return _confirmation_deferred_tool_result(deferred)
            if not confirmed:
                _remember_approval_blocked_tool(self, tool_name)
                return ToolResult(
                    ok=False,
                    error="Irreversible action was not confirmed by the user.",
                    error_category="IRREVERSIBLE_CONFIRMATION_DENIED",
                    retryable=False,
                )
            irreversible_confirmation_granted = True

        memory_exhaustion = self._memory_exhaustion_short_circuit(tool_name, tool_args)
        if memory_exhaustion is not None:
            return memory_exhaustion

        # Set payment/signature session context so the browser server's gate
        # overrides know which session is making this tool call.
        task_id = self._ensure_task_id()
        _gate_session_id = self._session_id or task_id
        try:
            from mcp_servers.browser.server import (
                set_payment_session,
                set_signature_session,
            )

            _ps_token = set_payment_session(_gate_session_id)
            _sg_token = set_signature_session(_gate_session_id)
        except Exception:
            # ratchet: critical-path-visibility — payment/signature gate overrides
            # silently lose their session binding otherwise (safety-critical).
            logger.exception(
                "Payment/signature session token setup failed; browser gate overrides will not bind this tool call"
            )
            _ps_token = None  # type: ignore[assignment] # PAY-25: failed token setup leaves no active payment context.
            _sg_token = None  # type: ignore[assignment] # SIGNATURE-01: failed token setup leaves no active signature context.

        # Mid-tool cancellation: wrap the hub call in a task so that
        # _cancel_event can abort it mid-execution rather than waiting
        # for the tool to complete or timeout.
        # INT-15 / SEC-05: a third wait-arm fires when the tool exceeds
        # ``self._tool_timeout`` (default 45s, configurable per executor) so
        # a hung tool cannot stall the loop even when no user cancellation
        # is in flight. Runaway tools (infinite loop, hung network call)
        # are cancelled at the configured budget instead of blocking the
        # agent loop forever.
        try:
            tool_user_context: dict[str, Any] = {
                "user_id": self._user_id,
                "session_id": self._session_id,
                "task_id": task_id,
                "browser_task_id": task_id,
                "gate_session_id": _gate_session_id,
                "payment_gate_active": bool(getattr(self, "_payment_gate_active", False)),
            }
            if tool_name in {"ToolSearch", "tool_search"}:
                deferred_pool = getattr(self, "_request_deferred_tool_pool", None)
                if deferred_pool is not None:
                    from intent.tools.deferred_tool_schemas import (
                        deferred_tool_pool_to_payload,
                    )

                    tool_user_context["deferred_tool_pool"] = deferred_tool_pool_to_payload(deferred_pool)
            if tool_name == "share_response":
                tool_user_context["share_response_context"] = {
                    "conversation_history": getattr(self, "_prior_conversation_messages", []),
                    "conversation_history_source": getattr(self, "_prior_conversation_source", "none"),
                }
            signature_override_token = getattr(self, "_signature_gate_override_token", None)
            if signature_override_token:
                tool_user_context["signature_gate_override_token"] = signature_override_token
                tool_user_context["signature_gate_override_actions"] = getattr(
                    self,
                    "_signature_gate_override_actions",
                    _SIGNATURE_GATE_OVERRIDE_ACTIONS,
                )
            payment_override_token = getattr(self, "_payment_gate_override_token", None)
            if payment_override_token:
                tool_user_context["payment_gate_override_token"] = payment_override_token
            payment_confirmation_context = getattr(self, "_payment_confirmation_tool_context", None)
            if isinstance(payment_confirmation_context, dict):
                tool_user_context["payment_confirmation"] = payment_confirmation_context
            if tool_name == "run_command" and irreversible_confirmation_granted:
                tool_user_context["shell_permission_granted"] = True

            # Look up per-tool timeout override; phone tools naturally exceed
            # the default 180s budget because they wait for the underlying
            # call to complete (up to max_call_duration). Without an override,
            # the agent kills the phone tool at 180s but the _run_call task
            # keeps running, leaking the active-call slot.
            tool_timeout_secs = _PER_TOOL_TIMEOUT_OVERRIDES_SECS.get(tool_name, float(self._tool_timeout))
            try:
                from services.user_capabilities.context import set_current_surface

                _surface_token = set_current_surface(self._get_origin_channel_type())
            except Exception:
                _surface_token = None
            try:
                from mcp_hub.errors import McpAuthError

                tool_abort_signal = abort_signal if abort_signal is not None else asyncio.Event()
                hub_call_kwargs: dict[str, Any] = {
                    "channel": self._channel,
                    "allowed_tools": self._allowed_tools,
                    "user_context": tool_user_context,
                    "approval_already_granted": irreversible_confirmation_granted,
                }
                if tool_use_id:
                    hub_call_kwargs["tool_use_id"] = tool_use_id
                if on_progress is not None:
                    hub_call_kwargs["on_progress"] = on_progress
                hub_call_kwargs["abort_signal"] = tool_abort_signal
                hub_result = await self._run_tool_with_cancel(
                    self._mcp_hub.call_tool(
                        tool_name,
                        tool_args,
                        **hub_call_kwargs,
                    ),
                    tool_name=tool_name,
                    timeout_seconds=tool_timeout_secs,
                    abort_signal=tool_abort_signal,
                )
            except _ToolExecutionCancelled:
                # Cancellation fired mid-tool â€” abort
                logger.info("Mid-tool cancellation: aborting %s", tool_name)
                return ToolResult(
                    ok=False,
                    error="Tool execution cancelled by user",
                    error_category="CANCELLED",
                )
            except _ToolExecutionInterruptedByTakeover:
                logger.info("Browser takeover interrupted in-flight tool: %s", tool_name)
                return ToolResult(
                    ok=False,
                    error="Browser action interrupted by user takeover",
                    error_category="USER_TAKEOVER",
                    retryable=True,
                )
            except _ToolExecutionTimedOut as exc:
                # INT-15 / SEC-05: per-tool timeout exceeded â€” hub call
                # was still running.  Return a structured timeout result
                # so the agent loop can continue and Viola never narrates
                # a hang. retryable=True lets upstream retry logic try again.
                logger.warning(
                    "Tool '%s' exceeded %.1fs budget; cancelling",
                    tool_name,
                    exc.timeout_seconds,
                )
                return ToolResult(
                    ok=False,
                    error="Tool execution timed out after %.1fs" % exc.timeout_seconds,
                    error_category="TIMEOUT",
                    retryable=True,
                )
            except McpAuthError as auth_exc:
                server_name = getattr(auth_exc, "server_name", "") or "MCP"
                logger.info("MCP auth required for %s (server=%s)", tool_name, server_name)
                return ToolResult(
                    ok=False,
                    error=str(auth_exc) or "MCP server needs authorization.",
                    error_category="MCP_AUTH",
                    retryable=False,
                )
            finally:
                if _surface_token is not None:
                    try:
                        from services.user_capabilities.context import (
                            reset_current_surface,
                        )

                        reset_current_surface(_surface_token)
                    except Exception:
                        logger.debug("User capability surface context reset failed")

            # Normal completion â€” extract the hub result
            self._step_approval_path = getattr(self._mcp_hub, "last_approval_path", None)
            self._step_approval_tool = tool_name
            result = ToolResult(
                ok=hub_result.get("success", False),
                data=hub_result.get("data"),
                mcp_content=hub_result.get("mcp_content"),
                mcp_meta=hub_result.get("mcp_meta"),
                error=hub_result.get("error") or None,
                # Propagate enriched error context from client_hub
                error_category=hub_result.get("error_category"),
                retryable=hub_result.get("retryable", False),
                required_tier=hub_result.get("required_tier"),
            )
            result = _enrich_review_required_tool_result(result)
            if _is_approval_block_error(result.error_category):
                _remember_approval_blocked_tool(self, tool_name)

            await self._broadcast_paid_action_gate(tool_name, result)

            # Keep runtime page state aligned with the latest browser result
            # before gate detection so trace/checkpoint artifacts point at the
            # actual boundary page, not the previous page.
            self._track_page_url(tool_name, result)
            self._record_memory_all_scope_no_match(tool_name, tool_args, result)

            if result.ok and idempotency_action:
                try:
                    get_idempotency_store().record_action(
                        idempotency_action,
                        tool_args,
                        user_id=self._get_effective_user_id(),
                    )
                except Exception:
                    logger.debug(
                        "Action idempotency record skipped for %s",
                        tool_name,
                        exc_info=True,
                    )

            # GAP-6 fix: wire tool failures to emit_failure() for coalescing,
            # error registry, metrics, and diagnostics bus integration.
            if not result.ok:
                self._emit_tool_failure(tool_name, tool_args, result)
                # Proactive OAuth: if a Google-service tool failed because it's
                # not configured, auto-open the OAuth sign-in page so the user
                # can connect without a second round-trip of "want me to set it up?"
                self._maybe_trigger_oauth_on_failure(tool_name, result)

            return result
        finally:
            # Reset gate-session context vars to prevent leaking across calls
            if _ps_token is not None:
                try:
                    from mcp_servers.browser.server import reset_payment_session

                    reset_payment_session(_ps_token)
                except Exception:
                    logger.debug("Payment session reset failed")
            if _sg_token is not None:
                try:
                    from mcp_servers.browser.server import reset_signature_session

                    reset_signature_session(_sg_token)
                except Exception:
                    logger.debug("Signature session reset failed")

    def _emit_tool_failure(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """Emit a structured failure envelope for a tool execution error.

        Connects tool-level failures to the coalescing, error registry,
        metrics, and diagnostics bus infrastructure (GAP-6 fix).
        """
        try:
            from diagnostics.failure_envelope import emit_failure

            emit_failure(
                code="tool_execution_failed",
                component="agent.tool.%s" % tool_name,
                message=result.error or "Tool returned error",
                severity="WARNING",
                retryable=result.retryable,
                tool_name=tool_name,
                tool_args_summary=str(tool_args)[:200],
                error_category=result.error_category or "UNKNOWN",
            )
        except Exception:
            # Never let failure-emission failures break the agent loop
            logger.debug("Failed to emit tool failure envelope for %s", tool_name)

    # Tools that belong to Google OAuth domains.  When these fail with a
    # "not configured" error, we auto-open the Google sign-in page.
    _GOOGLE_OAUTH_TOOLS: dict[str, str] = {
        "calendar": "calendar",
    }

    def _maybe_trigger_oauth_on_failure(
        self,
        tool_name: str,
        result: ToolResult,
    ) -> None:
        """Auto-open Google OAuth sign-in when a Google-service tool fails
        because the service is not configured.

        This provides a proactive UX: instead of the agent telling the user
        "Gmail is not configured" and stopping, it opens the sign-in page
        and enriches the error message so the agent can say "I'm opening
        the sign-in page."
        """
        domain_id = self._GOOGLE_OAUTH_TOOLS.get(tool_name)
        if not domain_id:
            return

        error_text = (result.error or "").lower()
        # Check for common "not configured / not set up" patterns
        not_configured_signals = (
            "not configured",
            "not set up",
            "not enabled",
            "credentials",
        )
        if not any(signal in error_text for signal in not_configured_signals):
            return

        try:
            from intent.capability_resolver import trigger_google_oauth_flow

            opened = trigger_google_oauth_flow(domain_id)
            if opened:
                # Enrich the error message so the agent can relay it naturally
                service_name = "Google Calendar"
                result.error = (
                    "%s is not connected yet. I've opened the sign-in page in "
                    "your browser -- just log in and I'll take care of the rest. "
                    "Try again after signing in." % service_name
                )
                logger.info(
                    "Auto-triggered OAuth flow for %s after %s tool failure",
                    domain_id,
                    tool_name,
                )
        except Exception:
            logger.debug("Failed to auto-trigger OAuth for %s", tool_name, exc_info=True)

    async def _handle_start_agent(self, tool_args: dict[str, Any]) -> ToolResult:
        """Dispatch a long-running background agent and return its id immediately.

        The child runs in a separate asyncio task at high reasoning effort,
        inherits the parent's per-turn context block, and streams its
        thinking + answer to its own stream_id. The parent (orchestrator)
        is unblocked and can keep talking to the user.
        """
        from intent.agent_subagent_types import (
            FORK_SUBAGENT_TYPE,
            UnknownSubagentTypeError,
            canonical_subagent_type,
        )

        task = str(tool_args.get("prompt") or tool_args.get("task") or "").strip()
        reason = str(tool_args.get("description") or tool_args.get("reason") or "").strip()
        if not task:
            return ToolResult(ok=False, error="start_agent requires a 'prompt' description.")
        if not reason:
            return ToolResult(ok=False, error="start_agent requires a 'description' (one sentence).")
        if self._depth >= MAX_DELEGATION_DEPTH:
            return ToolResult(
                ok=False,
                error="start_agent is unavailable inside a child agent — answer with what you have.",
                error_category="SUBAGENT_TOOL_NOT_ALLOWED",
            )

        raw_subagent_type = str(tool_args.get("subagent_type", "") or "").strip()
        raw_mode_arg = str(tool_args.get("mode", "") or "").strip().lower()
        raw_context_mode = str(tool_args.get("context_mode", "") or "").strip().lower()
        raw_mode = raw_context_mode or (raw_mode_arg if raw_mode_arg in {"fresh", "fork"} else "")
        if not raw_subagent_type:
            effective_subagent_type = FORK_SUBAGENT_TYPE
            effective_mode = raw_mode or "fork"
        else:
            try:
                effective_subagent_type = canonical_subagent_type(raw_subagent_type, strict=True)
            except UnknownSubagentTypeError as exc:
                return ToolResult(ok=False, error=str(exc))
            effective_mode = raw_mode or "fresh"
        # F-045 (R3-A): Claude forces every Agent spawn async when fork is
        # enabled (``tools/AgentTool/forkSubagent.ts:21-26``,
        # ``tools/AgentTool/AgentTool.tsx:555-567``). Typed Viola spawns
        # previously defaulted to foreground; under fork parity that
        # blocks the parent on a child that may itself fork tools, which
        # is the divergence the audit calls out. Force background for
        # every ``start_agent`` invocation regardless of subagent_type.
        # ``run_in_background=False`` on the tool args is now a no-op for
        # parity; explicit ``foreground=true`` would have to be a Viola
        # extension outside the parity surface.
        run_in_background = True

        if run_in_background:
            shared_names = _shared_state_tool_names(
                self._native_tools_for_child(
                    subagent_mode=effective_mode,
                    subagent_type=effective_subagent_type,
                )
            )
            if shared_names:
                return ToolResult(
                    ok=False,
                    error=_background_shared_state_error(shared_names),
                    error_category="SUBAGENT_SHARED_STATE_TOOLS",
                    retryable=False,
                )

        from services.agent_runtime.registry import (
            AgentLimitExceededError,
            agent_registry,
        )
        from services.llm.stream_bus import register_stream

        user_id = self._get_effective_user_id()
        stream_id = "agent_%s" % uuid.uuid4().hex[:12]
        agent_id = stream_id
        register_stream(stream_id, owner_id=user_id)

        parent_checkpoint = self._current_checkpoint

        # Capture the parent's channel so agent results deliver to the
        # surface the user originated from (voice -> TTS, telegram -> bot).
        # Web's channel.send buffers and is harmless after the original
        # request returned; SSE finalize_stream is what the web UI actually
        # consumes.
        delivery_channel = getattr(self, "_channel", None)

        async def _runner(cancel_event: asyncio.Event) -> str:
            from intent.hooks import dispatch_lifecycle
            from services.llm.stream_bus import (
                command_stream_context,
                finalize_stream,
            )

            hook_context = {
                "agent_id": agent_id,
                "stream_id": stream_id,
                "task": task,
                "reason": reason,
                "subagent_type": effective_subagent_type,
                "mode": effective_mode,
                "parent_task_id": getattr(self, "task_id", None),
                "user_id": user_id,
                "session_id": self._session_id,
            }
            started_at = time.monotonic()
            self._emit_subagent_task_notification_frame(
                agent_id=agent_id,
                subagent_type=effective_subagent_type,
                status="running",
                elapsed_ms=0,
            )
            try:
                dispatch_lifecycle("SubagentStart", hook_context)
            except Exception:
                logger.debug("SubagentStart hook dispatch failed; continuing")

            with command_stream_context(stream_id):
                try:
                    child_result = await self._run_child_agent(
                        task=task,
                        max_steps=0,
                        return_format="summary",
                        parent_checkpoint=parent_checkpoint,
                        external_cancel_event=cancel_event,
                        subagent_mode=effective_mode,
                        subagent_type=effective_subagent_type,
                        model_override=str(tool_args.get("model") or "").strip() or None,
                        allow_shared_state_tools=False,
                    )
                    answer = _child_answer_text(child_result)
                    finalize_stream(stream_id, content=answer)
                    if delivery_channel is not None and answer:
                        try:
                            await delivery_channel.send(answer)
                        except Exception as exc:
                            logger.debug("Background agent channel.send failed: %s", exc)
                    try:
                        dispatch_lifecycle(
                            "SubagentStop",
                            {**hook_context, "result": answer, "status": "completed"},
                        )
                    except Exception:
                        logger.debug("SubagentStop hook dispatch failed; continuing")
                    self._emit_subagent_task_notification_frame(
                        agent_id=agent_id,
                        subagent_type=effective_subagent_type,
                        status="completed",
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                        final_text=answer,
                    )
                    return answer

                except asyncio.CancelledError:
                    finalize_stream(stream_id, error=True, message="Cancelled")
                    try:
                        dispatch_lifecycle(
                            "SubagentStop",
                            {**hook_context, "status": "killed", "error": "Cancelled"},
                        )
                    except Exception:
                        logger.debug("SubagentStop hook dispatch failed; continuing")
                    self._emit_subagent_task_notification_frame(
                        agent_id=agent_id,
                        subagent_type=effective_subagent_type,
                        status="killed",
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                        error="Cancelled",
                    )
                    raise

                except Exception as exc:
                    finalize_stream(stream_id, error=True, message=str(exc))
                    try:
                        dispatch_lifecycle(
                            "SubagentStop",
                            {**hook_context, "status": "failed", "error": str(exc)},
                        )
                    except Exception:
                        logger.debug("SubagentStop hook dispatch failed; continuing")
                    self._emit_subagent_task_notification_frame(
                        agent_id=agent_id,
                        subagent_type=effective_subagent_type,
                        status="failed",
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                        error=str(exc),
                    )
                    raise

        try:
            registry_start = getattr(agent_registry, "start", None)
            if run_in_background and not callable(registry_start):
                run_in_background = False
            if run_in_background:
                agent_id = await registry_start(
                    user_id=user_id,
                    task=task,
                    reason=reason,
                    stream_id=stream_id,
                    runner=_runner,
                    agent_id=agent_id,
                    mode=effective_mode,
                    session_id=make_subagent_session_id(self._session_id, agent_id),
                    parent_session_id=self._session_id,
                    parent_agent_id=getattr(self, "_current_agent_id", None) or getattr(self, "task_id", None),
                    subagent_type=effective_subagent_type,
                    name=str(tool_args.get("name") or "").strip() or None,
                )

            else:
                agent_id, cancel_event, background_signal = await agent_registry.register_foreground(
                    user_id=user_id,
                    task=task,
                    reason=reason,
                    agent_id=stream_id,
                    mode=effective_mode,
                    session_id="agent:%s" % stream_id,
                    parent_session_id=self._session_id,
                    parent_agent_id=getattr(self, "task_id", None),
                    subagent_type=effective_subagent_type,
                    auto_background_seconds=_FOREGROUND_AUTO_BACKGROUND_SECONDS,
                )

                async def _registered_foreground_runner() -> str:
                    try:
                        answer = await _runner(cancel_event)
                    except asyncio.CancelledError:
                        await agent_registry.mark_failed_explicit(
                            user_id,
                            agent_id,
                            "Cancelled",
                            killed=True,
                        )
                        raise
                    except Exception as exc:
                        await agent_registry.mark_failed_explicit(user_id, agent_id, str(exc))
                        raise
                    await agent_registry.mark_complete_explicit(user_id, agent_id, answer)
                    return answer

                runner_task = asyncio.create_task(_registered_foreground_runner())
                await agent_registry.attach_runner_task(
                    user_id=user_id,
                    agent_id=agent_id,
                    runner_task=runner_task,
                )
        except AgentLimitExceededError as exc:
            return ToolResult(ok=False, error=str(exc))

        if not run_in_background:
            background_wait_task = asyncio.create_task(background_signal.wait())
            done, _pending = await asyncio.wait(
                {runner_task, background_wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if runner_task not in done:
                return ToolResult(
                    ok=True,
                    data={
                        "agent_id": agent_id,
                        "stream_id": stream_id,
                        "status": "running",
                        "backgrounded": True,
                    },
                )

            background_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_wait_task
            try:
                answer = await runner_task
            except asyncio.CancelledError:
                return ToolResult(ok=False, error="Subagent cancelled.")
            except Exception as exc:
                return ToolResult(ok=False, error="Subagent failed: %s" % str(exc)[:200])
            return ToolResult(
                ok=True,
                data={
                    "agent_id": agent_id,
                    "stream_id": stream_id,
                    "status": "completed",
                    "result": answer,
                    "final_text": answer,
                    "content": [{"type": "text", "text": answer}],
                    "prompt": task,
                    "description": reason,
                },
            )

        return ToolResult(
            ok=True,
            data={
                "agent_id": agent_id,
                "stream_id": stream_id,
                "status": "running",
                "mode": effective_mode,
                "session_id": make_subagent_session_id(self._session_id, agent_id),
            },
        )

    async def _handle_check_agents(self, tool_args: dict[str, Any]) -> ToolResult:
        """Return JSON list of this user's active + recent_completed agents."""
        from services.agent_runtime.registry import agent_registry

        user_id = self._get_effective_user_id()
        active = await agent_registry.list_active(user_id)
        recent = await agent_registry.list_recent_completed(user_id, limit=5)

        def _serialize(t: Any) -> dict[str, Any]:
            return {
                "agent_id": t.agent_id,
                "task": t.task,
                "reason": t.reason,
                "status": t.status,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "result": t.result,
                "error_detail": t.error_detail,
            }

        payload = {
            "active": [_serialize(t) for t in active],
            "recent_completed": [_serialize(t) for t in recent],
        }
        return ToolResult(ok=True, data=json.dumps(payload))

    async def _handle_cancel_agent(self, tool_args: dict[str, Any]) -> ToolResult:
        """Cancel a running background agent owned by this user."""
        from services.agent_runtime.registry import agent_registry

        agent_id = str(tool_args.get("agent_id", "") or "").strip()
        if not agent_id:
            return ToolResult(ok=False, error="cancel_agent requires an 'agent_id'.")

        user_id = self._get_effective_user_id()
        ok = await agent_registry.cancel(user_id=user_id, agent_id=agent_id)
        if not ok:
            return ToolResult(
                ok=False,
                error="No running agent found with id '%s' for this session." % agent_id,
            )
        return ToolResult(
            ok=True,
            data=json.dumps({"agent_id": agent_id, "final_status": "killed"}),
        )

    async def _handle_send_message(self, tool_args: dict[str, Any]) -> ToolResult:
        """Queue a message for an active subagent or resume one from transcript."""

        from services.agent_runtime.registry import agent_registry
        from services.llm.stream_bus import (
            command_stream_context,
            finalize_stream,
            register_stream,
        )

        target = str(tool_args.get("to") or tool_args.get("agent_id") or tool_args.get("name") or "").strip()
        message = str(tool_args.get("message") or "").strip()
        summary = str(tool_args.get("summary") or "").strip()
        if not target:
            return ToolResult(
                ok=False,
                error="send_message requires a 'to' agent id or name.",
                error_category="SUBAGENT_NOT_FOUND",
                retryable=False,
            )
        if not message:
            return ToolResult(
                ok=False,
                error="send_message requires a non-empty 'message'.",
                error_category="SUBAGENT_MESSAGE_EMPTY",
                retryable=False,
            )

        user_id = self._get_effective_user_id()
        resolved_id = subagent_lifecycle.resolve_agent_id(target, user_id=user_id)
        transcript = None
        if resolved_id:
            transcript = subagent_lifecycle.get_transcript(resolved_id, user_id=user_id)
            running = await agent_registry.get(user_id=user_id, agent_id=resolved_id)
            if running is not None and str(getattr(running, "status", "")) in {
                "running",
                "idle",
            }:
                queued_id = subagent_lifecycle.enqueue_message(resolved_id, message, user_id=user_id)
                if queued_id is None:
                    return ToolResult(
                        ok=False,
                        error="Agent '%s' cannot accept messages." % target,
                        error_category="SUBAGENT_NOT_FOUND",
                        retryable=False,
                    )
                if str(getattr(running, "status", "")) == "idle" and hasattr(agent_registry, "mark_running"):
                    await agent_registry.mark_running(user_id, queued_id)
                return ToolResult(
                    ok=True,
                    data=json.dumps({"agent_id": queued_id, "status": "queued"}),
                )
        if transcript is None:
            transcript = subagent_lifecycle.get_transcript(target, user_id=user_id)
        if transcript is None:
            return ToolResult(
                ok=False,
                error="No running or resumable agent found for '%s'." % target,
                error_category="SUBAGENT_NOT_FOUND",
                retryable=False,
            )
        if transcript.content_replacements:
            return ToolResult(
                ok=False,
                error="Cannot resume agent '%s': content replacement reconstruction is not supported yet." % target,
                error_category="SUBAGENT_RESUME_UNSUPPORTED",
                retryable=False,
            )
        if transcript.worktree_path:
            try:
                saved_worktree = Path(transcript.worktree_path).resolve()
                current_worktree = Path.cwd().resolve()
            except OSError:
                saved_worktree = None
                current_worktree = None
            if saved_worktree is None or current_worktree is None or saved_worktree != current_worktree:
                return ToolResult(
                    ok=False,
                    error="Cannot resume agent '%s': saved worktree is not the active worktree." % target,
                    error_category="SUBAGENT_RESUME_UNSUPPORTED",
                    retryable=False,
                )

        agent_id = transcript.agent_id
        session_id = transcript.session_id or make_subagent_session_id(self._session_id, agent_id)
        stream_id = agent_id
        register_stream(stream_id, owner_id=user_id)
        resume_messages = _resume_messages_from_subagent_transcript(transcript.messages, message)
        reason = summary or "Message sent to resumable subagent"
        parent_checkpoint = self._current_checkpoint

        async def _runner(cancel_event: asyncio.Event) -> str:
            with command_stream_context(stream_id):
                child_result = await self._run_child_agent(
                    task=message,
                    max_steps=0,
                    return_format="summary",
                    parent_checkpoint=parent_checkpoint,
                    external_cancel_event=cancel_event,
                    subagent_mode=transcript.mode,
                    subagent_type=transcript.agent_type,
                    agent_id=agent_id,
                    name=transcript.name,
                    resume_messages=resume_messages,
                )
                answer = _child_answer_text(child_result)
                finalize_stream(stream_id, content=answer)
                return answer

        started_id = await agent_registry.start(
            user_id=user_id,
            task=message,
            reason=reason,
            stream_id=stream_id,
            runner=_runner,
            agent_id=agent_id,
            mode=transcript.mode,
            session_id=session_id,
            parent_session_id=transcript.parent_session_id,
            parent_agent_id=transcript.parent_agent_id,
            subagent_type=transcript.agent_type,
            name=transcript.name,
        )
        return ToolResult(
            ok=True,
            data=json.dumps({"agent_id": started_id, "status": "resumed", "stream_id": stream_id}),
        )

    async def _handle_spawn_subtask(
        self,
        tool_args: dict[str, Any],
    ) -> ToolResult:
        """Handle spawn_subtask by creating a child AgentExecutor.

        Args:
            tool_args: Must contain 'task'. Optional legacy 'max_steps' hint and 'return_format'.

        Returns:
            ToolResult with the child's final answer.
        """
        task = tool_args.get("task", "")
        if not task:
            return ToolResult(ok=False, error="spawn_subtask requires a 'task' argument")

        max_steps = int(tool_args.get("max_steps", 0) or 0)
        return_format = tool_args.get("return_format", "summary")

        # Check depth limit
        if self._depth >= MAX_DELEGATION_DEPTH - 1:
            return ToolResult(
                ok=False,
                error="Maximum delegation depth (%d) reached. "
                "Handle this task directly instead of delegating." % MAX_DELEGATION_DEPTH,
            )

        child_result = await self._run_child_agent(
            task=task,
            max_steps=max_steps,
            return_format=return_format,
            parent_checkpoint=self._current_checkpoint,
        )
        if isinstance(child_result, ToolResult) and not child_result.ok:
            return child_result
        child_answer = _child_answer_text(child_result)

        # Append browser-state warning for parent
        result_text = child_answer + (
            "\n\nNote: browser state may have changed during subtask execution. " "Re-navigate if needed."
        )

        return ToolResult(ok=True, data=result_text)

    async def _handle_spawn_parallel_subtasks(self, tool_args: dict) -> ToolResult:
        """Item 4: Run multiple independent subtasks concurrently."""
        import asyncio as _asyncio

        tasks_list = tool_args.get("tasks", [])
        if not tasks_list:
            return ToolResult(ok=False, error="No tasks provided")
        if len(tasks_list) > 2:
            return ToolResult(ok=False, error="Maximum 2 parallel subtasks allowed")

        # Safety: reject browser tasks (shared state)
        _browser_tools = frozenset(
            {
                "browser_navigate",
                "browser_snapshot",
                "browser_click",
                "browser_interact",
                "browser_fill_form",
                "browser_type",
                "browser_evaluate",
                "browser_run_script",
                "browser_wait",
            }
        )
        for i, t in enumerate(tasks_list):
            hint = str(t.get("task", "")).lower()
            tools_hint = t.get("tools", [])
            if any(bt in hint for bt in _browser_tools) or any(bt in tools_hint for bt in _browser_tools):
                return ToolResult(
                    ok=False,
                    error="Task %d references browser tools which cannot run in parallel because browser state is shared."
                    % i,
                )

        async def _run_one(task_spec: dict) -> dict:
            task_text = task_spec.get("task", "")
            try:
                result = await self._handle_spawn_subtask({"task": task_text})
                return {
                    "task": task_text[:80],
                    "ok": result.ok,
                    "data": result.data,
                    "error": result.error,
                }
            except Exception as exc:
                return {
                    "task": task_text[:80],
                    "ok": False,
                    "data": None,
                    "error": str(exc),
                }

        results = await _asyncio.gather(*[_run_one(t) for t in tasks_list], return_exceptions=False)
        return ToolResult(ok=True, data=json.dumps(results, ensure_ascii=False, default=str))

    @staticmethod
    def _tool_call_cache_key(tool_call: dict[str, Any], index: int) -> str:
        return str(tool_call.get("tool_use_id") or tool_call.get("id") or index)

    def _mcp_annotation_lookup(self, tool_name: str) -> dict[str, Any] | None:
        hub = self._mcp_hub
        if hub is None:
            return None
        schemas = getattr(hub, "_tool_schemas", None)
        if isinstance(schemas, dict):
            schema = schemas.get(tool_name)
            if isinstance(schema, dict):
                annotation = schema.get("_annotations")
                if isinstance(annotation, dict):
                    return annotation
        annotations = getattr(hub, "_tool_annotations", None)
        if not isinstance(annotations, dict):
            return None
        annotation = annotations.get(tool_name)
        return annotation if isinstance(annotation, dict) else None

    def _is_parallel_safe_tool_call(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Return True only for read-only independent calls."""
        if not tool_name:
            return False
        from intent.tool_concurrency import is_concurrency_safe_tool

        return bool(
            is_concurrency_safe_tool(
                tool_name,
                tool_args,
                annotation_lookup=self._mcp_annotation_lookup,
            )
        )

    def _partition_tool_calls_for_execution(self, tool_calls: list[dict[str, Any]]) -> list[Any]:
        from intent.tool_concurrency import partition_tool_calls

        def _safe(tc: dict[str, Any]) -> bool:
            tool_name = str(tc.get("tool", ""))
            tool_args = tc.get("args", {})
            if not isinstance(tool_args, dict):
                return False
            return self._is_parallel_safe_tool_call(tool_name, tool_args)

        return list(partition_tool_calls(tool_calls, is_safe=_safe))

    def _can_execute_tool_batch_in_parallel(self, tool_calls: list[dict[str, Any]]) -> bool:
        if len(tool_calls) < 2:
            return False
        for tool_call in tool_calls:
            tool_name = str(tool_call.get("tool", ""))
            tool_args = tool_call.get("args", {})
            if not isinstance(tool_args, dict):
                return False
            if not self._is_parallel_safe_tool_call(tool_name, tool_args):
                return False
        return True

    async def _execute_parallel_tool_batch(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        spin_detector: SpinDetector,
        taint_tracker: Any,
        page_url: str | None,
        base_index: int = 0,
        canonical_hook_dispatch: Any | None = None,
        canonical_hook_context: dict[str, Any] | None = None,
        tool_allowed: Any | None = None,
        rejected_tools: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Execute a safe read-only multi-call batch concurrently.

        F-032 (R3-A): when ``canonical_hook_dispatch`` is provided, the
        runner dispatches the canonical ``PreToolUse`` / ``PostToolUse``
        hooks per tool inside the concurrent batch, matching Claude's
        ``runToolsConcurrently`` shape (``toolOrchestration.ts:152-176``).
        The caller (``intent.agent_loop``) is responsible for queuing the
        rendered hook context onto the next-turn message list with the
        same ordering as the serial path; we just fire the hooks here.
        """
        from intent.tool_concurrency import get_max_tool_use_concurrency

        concurrency_cap = max(1, int(get_max_tool_use_concurrency()))
        concurrency_sem = asyncio.Semaphore(concurrency_cap)
        prepared: list[dict[str, Any]] = []
        cached: dict[str, dict[str, Any]] = {}

        for index, tool_call in enumerate(tool_calls):
            key = self._tool_call_cache_key(tool_call, base_index + index)
            tool_name = str(tool_call.get("tool", ""))
            tool_args = tool_call.get("args", {})
            if not isinstance(tool_args, dict):
                tool_args = {}

            started = time.monotonic()
            tool_result: ToolResult | None = None
            tool_error: str | None = None

            if callable(tool_allowed) and not bool(tool_allowed(tool_name)):
                tool_error = "Tool '%s' is not allowed for this skill." % tool_name
                tool_result = ToolResult(ok=False, error=tool_error)
            elif tool_name in (rejected_tools or set()):
                tool_error = "Tool '%s' has been disabled for this task." % tool_name
                tool_result = ToolResult(ok=False, error=tool_error)
            elif validation_error := self._validate_tool_args(tool_name, tool_args):
                tool_result = ToolResult(ok=False, error=validation_error)
                tool_error = validation_error
            elif blocked_msg := spin_detector.is_call_blocked(tool_name, tool_args, page_url=page_url):
                tool_result = ToolResult(ok=False, error=blocked_msg)
                tool_error = blocked_msg
            elif taint_block := taint_tracker.check_tool(tool_name):
                tool_result = ToolResult(ok=False, error=taint_block)
                tool_error = taint_block

            if tool_result is not None:
                cached[key] = {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_result": tool_result,
                    "tool_error": tool_error,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
                continue

            # F-032: fire the canonical PreToolUse hook BEFORE the
            # concurrent runner so blocks/updates apply deterministically
            # per left-to-right partition order. PostToolUse fires after
            # the runner joins (still inside this method).
            canonical_pre_result: Any = None
            if canonical_hook_dispatch is not None:
                try:
                    canonical_pre_result = canonical_hook_dispatch(
                        "PreToolUse",
                        tool_name,
                        tool_args,
                        canonical_hook_context or {},
                    )
                    if canonical_pre_result is not None and getattr(canonical_pre_result, "updated_input", None):
                        tool_args = copy.deepcopy(canonical_pre_result.updated_input)
                    if canonical_pre_result is not None and getattr(canonical_pre_result, "blocks_tool", False):
                        # Synthesize a hook-block tool result; skip execution.
                        from intent.agent_loop import _hook_block_tool_result as _hbtr

                        block_result = _hbtr(tool_name, canonical_pre_result)
                        cached[key] = {
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "tool_result": block_result,
                            "tool_error": block_result.error,
                            "pre_hook_result": canonical_pre_result,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        }
                        continue
                except Exception:
                    logger.exception(
                        "Canonical PreToolUse hook dispatch failed in parallel batch for tool %s",
                        tool_name,
                    )

            try:
                _dispatch_hook("pre_tool_use", tool_name=tool_name, args=tool_args)
            except Exception as hook_exc:
                from intent.hooks.safety import SafetyBlockError

                if isinstance(hook_exc, SafetyBlockError):
                    cached[key] = {
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "tool_result": ToolResult(ok=False, error=hook_exc.message),
                        "tool_error": hook_exc.message,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                    continue
                raise

            prepared.append(
                {
                    "key": key,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_use_id": str(tool_call.get("tool_use_id") or tool_call.get("id") or "") or None,
                    "started": started,
                    "pre_hook_result": canonical_pre_result,
                }
            )

        async def _run_one(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            async with concurrency_sem:
                started = float(item["started"])
                tool_name = str(item["tool_name"])
                tool_args = item["tool_args"]
                tool_error: str | None = None
                try:
                    tool_result = await self._execute_tool(
                        tool_name,
                        tool_args,
                        tool_use_id=item.get("tool_use_id"),
                    )
                except Exception as exc:
                    tool_error = str(exc)
                    tool_result = ToolResult(ok=False, error=tool_error)
                    self._capture_diagnostic(
                        user_text=self._agent_task_description or "",
                        exception=exc,
                        stage="tool_execution",
                        tool_name=tool_name,
                        tool_result_text=tool_error,
                    )
                # F-032: canonical PostToolUse fires per-tool in the
                # concurrent batch (parity with ``toolOrchestration.ts:
                # 152-176`` where ``runToolUse`` includes hook dispatch).
                # The caller renders the resulting context after joining
                # so ordering with adjacent unsafe groups is preserved.
                canonical_post_result: Any = None
                if canonical_hook_dispatch is not None:
                    try:
                        post_event = "PostToolUse" if tool_result and tool_result.ok else "PostToolUseFailure"
                        canonical_post_result = canonical_hook_dispatch(
                            post_event,
                            tool_name,
                            tool_args,
                            {
                                **(canonical_hook_context or {}),
                                "result": tool_result,
                                "error": tool_error,
                            },
                        )
                    except Exception:
                        logger.exception(
                            "Canonical PostToolUse hook dispatch failed in parallel batch for tool %s",
                            tool_name,
                        )
                return (
                    str(item["key"]),
                    {
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "tool_result": tool_result,
                        "tool_error": tool_error,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "pre_hook_result": item.get("pre_hook_result"),
                        "post_hook_result": canonical_post_result,
                    },
                )

        if prepared:
            for key, value in await asyncio.gather(*[_run_one(item) for item in prepared]):
                cached[key] = value
        return cached

    # Defense-in-depth for parallel_tool_calls bug. Primary fix:
    # parallel_tool_calls=False in openai_direct.py.
    # Tools where empty/missing string args are clearly wrong (LLM bug).
    # Only covers tools with observed malformed parallel calls.
    _REQUIRED_STRING_ARGS: dict[str, list[str]] = {
        "browser_type": ["selector", "text"],
        "browser_navigate": ["url"],
        "browser_click": ["selector"],
        "browser_wait": ["selector"],
        "browser_evaluate": ["script"],
        "browser_run_script": ["script"],
    }

    # are hidden but still callable â€” compound resolution in call_tool()
    # Gmail (11 tools â†’ gmail compound)
    # Google Chat (8 tools â†’ google_chat compound)
    # Google Docs (6 tools â†’ google_docs compound)
    # Google Drive (8 tools â†’ google_drive compound)
    # Google Sheets (3 tools â†’ google_sheets compound)
    # Google Slides (4 tools â†’ google_slides compound)
    # Google People (3 tools â†’ google_people compound)
    # Google Auth (2 tools â†’ google_auth compound)
    def _validate_tool_args(self, tool_name: str, tool_args: dict[str, Any]) -> str | None:
        """Check for obviously missing required arguments.

        Returns an error message if validation fails, None if args look OK.
        This catches provider/model failures where a tool_use block has
        truncated or missing arguments.
        """
        # Check that the tool name exists in the request-scoped surface. This
        # must match the native_tools payload passed to route_command_native();
        # provider globals are only a compatibility fallback.
        known_names: list[str] | None = None
        _provider_tools = self._get_request_native_tools()
        if isinstance(_provider_tools, (list, tuple)) and _provider_tools:
            known_names = sorted(t.get("name", "") for t in _provider_tools if isinstance(t, dict) and t.get("name"))
        elif self._mcp_hub is not None:
            raw = self._mcp_hub.list_tools()
            known_names = sorted(t.get("name", "") for t in raw if t.get("name"))

        if known_names is not None and tool_name not in known_names:
            # Build a concise available-tools string (truncated at 20 entries).
            display = known_names[:20]
            tools_str = ", ".join(display)
            if len(known_names) > 20:
                tools_str += ", â€¦ (%d more)" % (len(known_names) - 20)

            # Surface similar registered names as neutral facts. Do not inject
            # tool-choice instructions; the model has the available tool list.
            suggestions = difflib.get_close_matches(tool_name, known_names, n=3, cutoff=0.5)
            suggestion_str = ""
            if suggestions:
                suggestion_str = " Similar registered tools: %s." % ", ".join(suggestions)

            return ("Tool '%s' does not exist.%s " "Available tools: %s.") % (
                tool_name,
                suggestion_str,
                tools_str,
            )

        required = self._REQUIRED_STRING_ARGS.get(tool_name)
        if not required:
            return None
        for arg_name in required:
            val = tool_args.get(arg_name)
            if val is None or (isinstance(val, str) and not val.strip()):
                base = "Missing required parameter '%s' for %s." % (arg_name, tool_name)
                return base
        return None

    # Step reminder injection removed per constraint audit 2026-03-25.
    # The agent manages its own context without injected reminders.

    def _build_consecutive_failure_warning(self) -> str | None:
        """Observe consecutive failures without injecting model steering text."""
        if self._consecutive_failures >= self._consecutive_failure_threshold:
            logger.info(
                "Consecutive tool failures observed: count=%d",
                self._consecutive_failures,
            )
        return None

    def _build_error_registry_telemetry(self, tool_name: str) -> str | None:
        """Summarize active error-registry patterns for logs and step telemetry."""
        try:
            from diagnostics.error_registry import get_error_registry

            registry = get_error_registry()
            patterns = registry.get_active_patterns(max_age_hours=1.0, min_count=3)
            if not patterns:
                return None

            relevant: list[str] = []
            for pattern in patterns:
                # Match on tool name appearing in the component
                if tool_name in pattern.component or tool_name in pattern.code:
                    relevant.append(
                        "[%s] failed %d times recently; code=%s" % (pattern.component, pattern.count, pattern.code)
                    )
            if not relevant:
                return None

            return "\n".join(relevant[:3])
        except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
            return None

    def _maybe_register_apis(self, task_log: AgentTaskLog) -> None:
        """Register useful APIs discovered via network interception."""
        # Check if we have access to the MCP hub and browser manager
        if not self._mcp_hub:
            return

        try:
            # Try to get the API log from the browser manager
            # This is a best-effort operation
            from mcp_servers.browser.browser_manager import BrowserManager

            mgr = BrowserManager()
            api_log = mgr.get_api_log(user_id=self._get_effective_user_id())
            if not api_log:
                return

            # Filter for potentially useful endpoints
            useful = [e for e in api_log if _is_useful_api(e)]
            if not useful:
                return

            # Save to API registry
            _save_to_api_registry(useful, task_log.user_text)

            logger.info(
                "Registered %d APIs from network capture for task: %s",
                len(useful),
                task_log.user_text[:50],
            )
        except Exception:
            logger.debug("API registration failed (non-blocking)")

    _KEEP_FULL_EXCHANGES = 3  # last N tool exchanges kept verbatim
    _MAX_SCREENSHOT_IMAGES = 2  # max screenshot images retained in context
    # H4: Hard cap â€” belt-and-suspenders guard against extreme bloat when the
    # exchange-based trim cannot apply (e.g., all-assistant messages).
    _MAX_NATIVE_HISTORY = 100  # absolute max messages; FIFO eviction from index 1

    def _trim_native_messages(self) -> None:
        """Sliding window: keep last N tool exchanges full, summarize older ones.

        Anthropic requires strict user/assistant alternation. The message structure is:
        [0] user (original request)
        [1] assistant (first tool_call)
        [2] user (tool_result)
        [3] assistant (next tool_call or reasoning)
        ...

        We keep message [0] (original user request) always intact.
        We keep the last KEEP_FULL_EXCHANGES pairs of (user tool_result + assistant)
        intact. For older pairs, we replace the user tool_result content with a
        1-line summary.

        Additionally, screenshot images are evicted aggressively: only the most
        recent _MAX_SCREENSHOT_IMAGES are kept as images. Older screenshots are
        replaced with text descriptions to prevent context bloat.
        """
        msgs = self._native_messages
        trimmed_messages = 0
        fifo_evicted = 0
        # Hard cap: if the list grew past the absolute maximum, evict oldest entries
        # (preserving msg[0] which is the original user request) before doing the
        # smarter exchange-based summarisation below.
        if len(msgs) > self._MAX_NATIVE_HISTORY:
            overflow = len(msgs) - self._MAX_NATIVE_HISTORY
            # Round up to even number to preserve assistant/user pairs.
            # Deleting an odd number can split a tool_use from its tool_result,
            # causing Anthropic API 400 errors.
            if overflow % 2 != 0:
                overflow += 1
            fifo_evicted = len(self._native_messages[1 : 1 + overflow])
            del self._native_messages[1 : 1 + overflow]
            msgs = self._native_messages

        if len(msgs) <= 2 + (self._KEEP_FULL_EXCHANGES * 2):
            # Even if not enough to trim exchanges, still evict old screenshots
            screenshots_evicted = self._evict_old_screenshots()
            self._record_pending_trim_stats(
                trimmed=trimmed_messages,
                fifo_evicted=fifo_evicted,
                screenshots_evicted=screenshots_evicted,
            )
            return

        # Find all user messages that contain tool_result blocks (not the original)
        tool_result_indices: list[int] = []
        for i, msg in enumerate(msgs):
            if i == 0:
                continue  # preserve original user request
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content
            ):
                tool_result_indices.append(i)

        # Keep the last KEEP_FULL_EXCHANGES tool result messages full
        if len(tool_result_indices) > self._KEEP_FULL_EXCHANGES:
            indices_to_trim = tool_result_indices[: -self._KEEP_FULL_EXCHANGES]

            for idx in indices_to_trim:
                content = msgs[idx].get("content")
                if not isinstance(content, list):
                    continue

                summarized: list[dict[str, Any]] = []
                message_trimmed = False
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        summarized.append(block)
                        continue

                    old_content = block.get("content", "")
                    if isinstance(old_content, str) and old_content.startswith("[summarized]"):
                        summarized.append(block)
                        continue
                    # If content is a list (e.g., has image blocks), extract just text
                    if isinstance(old_content, list):
                        text_parts = [
                            b.get("text", "") for b in old_content if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        old_content = " ".join(text_parts)

                    # Structured summary: "[Step N: tool_name -> OK/ERR]" (~40 chars)
                    # saves ~110 chars per step vs old 150-char truncation
                    tool_id = block.get("tool_use_id", "")
                    is_error = block.get("is_error", False)
                    status = "ERR" if is_error else "OK"
                    # Try to find the matching tool_use block for this result
                    # to get the tool name for a better summary
                    tool_name_hint = ""
                    if idx > 0:
                        prev_content = msgs[idx - 1].get("content", [])
                        if isinstance(prev_content, list):
                            for pb in prev_content:
                                if isinstance(pb, dict) and pb.get("type") == "tool_use" and pb.get("id") == tool_id:
                                    tool_name_hint = pb.get("name", "")
                                    break
                    if tool_name_hint:
                        summary = "[%s -> %s]" % (tool_name_hint, status)
                    else:
                        summary_text = str(old_content)[:80].replace("\n", " ")
                        summary = _SNAP_REF_RE.sub("[ref]", summary_text)
                    summarized.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "content": "[summarized] %s" % summary,
                        }
                    )
                    message_trimmed = True

                msgs[idx] = {"role": "user", "content": summarized}
                if message_trimmed:
                    trimmed_messages += 1

        # Fix any orphaned tool_use blocks (tool_use without matching tool_result).
        # This can happen when the hard-cap trim splits a pair or when the agent
        # loop exits with a pending tool_use.
        self._fix_orphaned_tool_uses()

        # Second pass: evict old screenshot images even within kept exchanges
        screenshots_evicted = self._evict_old_screenshots()
        self._record_pending_trim_stats(
            trimmed=trimmed_messages,
            fifo_evicted=fifo_evicted,
            screenshots_evicted=screenshots_evicted,
        )

    @staticmethod
    def _extract_tool_use_id(block: Any) -> str | None:
        """Extract tool_use id from a content block (dict or SDK object)."""
        if isinstance(block, dict):
            if block.get("type") == "tool_use":
                return block.get("id", "") or None
        else:
            # Anthropic SDK content block objects (ToolUseBlock, TextBlock)
            # have .type, .id, .name, .input as attributes, not dict keys.
            if getattr(block, "type", None) == "tool_use":
                return getattr(block, "id", "") or None
        return None

    def _fix_orphaned_tool_uses(self) -> None:
        """Ensure every tool_use block has a matching tool_result.

        Anthropic's API requires that every ``tool_use`` in an assistant message
        is immediately followed by a user message containing a ``tool_result``
        with a matching ``tool_use_id``.  Violations produce a 400 error that
        makes ALL subsequent agent calls fail.

        This can happen when:
        - The hard-cap trim splits a tool_use/tool_result pair
        - The agent loop exits with a pending tool_use (max turns, exception)

        Handles both plain dicts and Anthropic SDK content block objects
        (which use attributes like .type, .id instead of dict keys).
        """
        msgs = self._native_messages
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            if msg.get("role") != "assistant":
                i += 1
                continue

            content = msg.get("content")
            if not isinstance(content, list):
                i += 1
                continue

            # Collect tool_use IDs in this assistant message
            # Handles both dict blocks and Anthropic SDK objects
            tool_use_ids: list[str] = []
            for block in content:
                tid = self._extract_tool_use_id(block)
                if tid and tid not in tool_use_ids:
                    tool_use_ids.append(tid)

            if not tool_use_ids:
                i += 1
                continue

            # Check next message for matching tool_results
            if i + 1 < len(msgs) and msgs[i + 1].get("role") == "user":
                next_content = msgs[i + 1].get("content", [])
                if isinstance(next_content, list):
                    found_ids = {
                        b.get("tool_use_id", "")
                        for b in next_content
                        if isinstance(b, dict) and b.get("type") == "tool_result"
                    }
                    missing = [tid for tid in tool_use_ids if tid not in found_ids]
                    if missing:
                        # Append missing tool_results to existing user message
                        for tid in missing:
                            next_content.append(build_synthetic_tool_result_block(tid, "history_trimmed"))
                        msgs[i + 1]["content"] = next_content
                        logger.debug(
                            "Patched %d orphaned tool_use(s) at message %d",
                            len(missing),
                            i,
                        )
                else:
                    # User message content isn't a list â€” insert synthetic tool_results
                    synthetic = [
                        build_synthetic_tool_result_block(tid, "prior_tool_execution_incomplete")
                        for tid in tool_use_ids
                    ]
                    msgs.insert(i + 1, {"role": "user", "content": synthetic})
                    logger.debug("Inserted synthetic tool_result message at %d", i + 1)
            else:
                # No user message follows â€” insert synthetic tool_results
                synthetic = [
                    build_synthetic_tool_result_block(tid, "agent_terminated_before_tool_execution")
                    for tid in tool_use_ids
                ]
                msgs.insert(i + 1, {"role": "user", "content": synthetic})
                logger.debug(
                    "Inserted synthetic tool_result message at %d (no following user msg)",
                    i + 1,
                )

            i += 2  # Skip past the assistant + user pair

    @staticmethod
    def _build_synthetic_tool_result_blocks(tool_use_ids: list[str], reason: str) -> list[dict[str, Any]]:
        """Create synthetic tool_result blocks for missing native tool executions.

        These are error results (is_error=True) so the LLM knows the tool
        did not execute successfully and should not rely on the "result".
        """
        reason_text = str(reason or "")
        if "agent terminated" in reason_text:
            reason_key = "agent_terminated_before_tool_execution"
        elif "history" in reason_text:
            reason_key = "history_trimmed"
        else:
            reason_key = "prior_tool_execution_incomplete"
        return [build_synthetic_tool_result_block(tid, reason_key) for tid in tool_use_ids]

    def _sanitize_native_messages_for_api(self, *, trim_history: bool) -> None:
        """Repair native Anthropic history immediately before an API call."""
        if trim_history:
            self._trim_native_messages()

        self._fix_alternation()

        msgs = self._native_messages
        repaired_blocks = 0
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            if msg.get("role") != "assistant":
                i += 1
                continue

            content = msg.get("content")
            if not isinstance(content, list):
                i += 1
                continue

            tool_use_ids: list[str] = []
            for block in content:
                tid = self._extract_tool_use_id(block)
                if tid and tid not in tool_use_ids:
                    tool_use_ids.append(tid)

            if not tool_use_ids:
                i += 1
                continue

            if i + 1 < len(msgs) and msgs[i + 1].get("role") == "user":
                next_content = msgs[i + 1].get("content")
                if isinstance(next_content, list):
                    found_ids = {
                        block.get("tool_use_id", "")
                        for block in next_content
                        if isinstance(block, dict) and block.get("type") == "tool_result"
                    }
                    missing_ids = [tid for tid in tool_use_ids if tid not in found_ids]
                    if missing_ids:
                        synthetic_blocks = self._build_synthetic_tool_result_blocks(
                            missing_ids,
                            "prior tool execution did not complete",
                        )
                        msgs[i + 1]["content"] = synthetic_blocks + next_content
                        repaired_blocks += len(missing_ids)
                else:
                    synthetic_blocks = self._build_synthetic_tool_result_blocks(
                        tool_use_ids,
                        "prior tool execution did not complete",
                    )
                    original_text = str(next_content) if next_content is not None else ""
                    if original_text:
                        synthetic_blocks.append({"type": "text", "text": original_text})
                    msgs[i + 1]["content"] = synthetic_blocks
                    repaired_blocks += len(tool_use_ids)
            else:
                synthetic_blocks = self._build_synthetic_tool_result_blocks(
                    tool_use_ids,
                    "agent terminated before tool execution",
                )
                msgs.insert(i + 1, {"role": "user", "content": synthetic_blocks})
                repaired_blocks += len(tool_use_ids)
                i += 1

            i += 1

        if repaired_blocks:
            logger.debug(
                "Sanitized native history with %d synthetic tool_result block(s)",
                repaired_blocks,
            )

        self._fix_alternation()

    def _fix_alternation(self) -> None:
        """Ensure strict user/assistant alternation and first-message-is-user.

        Anthropic's API requires:
        1. First message must have role "user"
        2. Messages must strictly alternate user/assistant
        3. No consecutive same-role messages

        This method merges consecutive same-role messages and ensures the
        first message is "user".  Runs as a final guard before API calls.
        """
        msgs = self._native_messages
        if not msgs:
            return

        # Ensure first message is "user"
        if msgs[0].get("role") != "user":
            msgs.insert(0, {"role": "user", "content": "[conversation start]"})
            logger.debug("Inserted synthetic user message at index 0 to fix alternation")

        # Merge consecutive same-role messages
        i = 1
        merged_count = 0
        while i < len(msgs):
            if msgs[i].get("role") == msgs[i - 1].get("role"):
                # Consecutive same role â€” merge into the previous message
                prev_content = msgs[i - 1].get("content", "")
                curr_content = msgs[i].get("content", "")
                # If both are strings, concatenate
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    msgs[i - 1]["content"] = prev_content + "\n" + curr_content
                elif isinstance(prev_content, list) and isinstance(curr_content, list):
                    prev_content.extend(curr_content)
                elif isinstance(prev_content, list) and isinstance(curr_content, str):
                    prev_content.append({"type": "text", "text": curr_content})
                elif isinstance(prev_content, str) and isinstance(curr_content, list):
                    msgs[i - 1]["content"] = [{"type": "text", "text": prev_content}] + curr_content
                del msgs[i]
                merged_count += 1
            else:
                i += 1

        if merged_count:
            logger.debug(
                "Merged %d consecutive same-role messages to fix alternation",
                merged_count,
            )

    def _record_pending_trim_stats(self, *, trimmed: int, fifo_evicted: int, screenshots_evicted: int) -> None:
        """Accumulate history-loss stats until the next step JSONL emission."""
        if trimmed <= 0 and fifo_evicted <= 0 and screenshots_evicted <= 0:
            return

        new_stats = {
            "trimmed": trimmed,
            "fifo_evicted": fifo_evicted,
            "screenshots_evicted": screenshots_evicted,
        }
        if self._pending_trim_stats:
            for key, value in new_stats.items():
                self._pending_trim_stats[key] = self._pending_trim_stats.get(key, 0) + value
            return
        self._pending_trim_stats = new_stats

    def _evict_old_screenshots(self) -> int:
        """Replace all but the most recent N screenshot images with text placeholders.

        Screenshots are base64-encoded PNGs that cost thousands of image tokens.
        Only the most recent _MAX_SCREENSHOT_IMAGES are useful for decision-making.
        """
        msgs = self._native_messages

        # Collect (msg_index, block_index) for all tool_result blocks with images
        image_locations: list[tuple[int, int, int]] = []  # (msg_idx, block_idx, sub_idx)
        for i, msg in enumerate(msgs):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for j, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                inner = block.get("content")
                if not isinstance(inner, list):
                    continue
                for k, sub in enumerate(inner):
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        image_locations.append((i, j, k))

        # Keep only the last N images
        if len(image_locations) <= self._MAX_SCREENSHOT_IMAGES:
            return 0

        to_evict = image_locations[: -self._MAX_SCREENSHOT_IMAGES]
        for msg_idx, block_idx, sub_idx in to_evict:
            block = msgs[msg_idx]["content"][block_idx]
            inner = block["content"]
            # Get text description from the text block in the same tool_result
            text_desc = ""
            for sub in inner:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    text_desc = sub.get("text", "")
                    break
            # Replace entire content with text-only summary
            block["content"] = "[screenshot evicted] %s" % text_desc
        return len(to_evict)

    async def _get_next_response(
        self,
        messages: list[dict[str, str]],
        system_context: str,
        first_turn: bool = False,
        tool_choice_override: dict | None = None,
    ) -> ToolCall | None:
        """Call LLM with current message history and parse response.

        Args:
            messages: Current message history (text path) or ignored (native path).
            system_context: System context string (text path).
            first_turn: Whether this is the first agent loop turn. Retained for
                compatibility with providers that may want to bias first-turn
                behavior, but native OpenAI paths no longer force tool calls on
                turn 1.
            tool_choice_override: Force a specific tool_choice for this call
                (e.g. ``{"type": "function", "function": {"name": "browser_navigate"}}``).
                Overrides both first_turn logic and default "auto".

        Returns:
            ToolCall if LLM wants another tool, None if it returned a final answer/command.
            Sets self._final_answer or self._final_command on final response.
            Sets self._parse_failed to True if the response was malformed.
        """
        self._raw_final_answer = None
        self._final_answer = None
        self._final_command = None
        self._final_params = {}
        self._final_continue_listening: bool | None = None
        self._parse_failed = False

        budget_gate = await self._check_managed_llm_spend_cap_async()
        if not budget_gate.allowed:
            self._record_agent_gate_denial_telemetry(
                gate_name="managed_llm_spend_cap",
                current_usage=budget_gate.spent_cents,
                limit_value=budget_gate.budget_cents,
            )
            _set_final_response(
                self,
                self._managed_llm_budget_message(budget_gate),
                continue_listening=False,
            )
            return None

        # LA-3: Proactive token budget check â€” compact BEFORE hitting the
        # model's context window, avoiding a wasted 400-error API call.
        # Recount from current messages to stay accurate after external mutations.
        from intent.agent_loop import (
            _compaction_record_progress,
            _compaction_should_allow,
        )

        native_continuity_state = self._export_responses_continuity_state() if self._use_native else {}
        native_continuity_active = self._use_native and bool(native_continuity_state.get("mode"))
        if (
            self._use_native
            and self._should_compact_responses_continuity(native_continuity_state)
            and _compaction_should_allow(self)
        ):
            pre_compaction_tokens = _estimated_tokens_for_messages(self._native_messages)
            self._compaction_count += 1
            try:
                if await self._compact_native_responses_continuity(reason="proactive_response_items"):
                    native_continuity_state = self._export_responses_continuity_state()
                    native_continuity_active = bool(native_continuity_state.get("mode"))
                    _compaction_record_progress(
                        self,
                        pre_compaction_tokens,
                        _estimated_tokens_for_messages(self._native_messages),
                    )
                else:
                    _compaction_record_progress(self, pre_compaction_tokens, pre_compaction_tokens)
            except Exception as _continuity_compact_exc:
                _compaction_record_progress(self, pre_compaction_tokens, pre_compaction_tokens)
                logger.warning(
                    "Responses continuity compaction failed, continuing without it: %s",
                    _continuity_compact_exc,
                )
        if self._use_native and native_continuity_active:
            _budget_msgs = self._native_messages[
                min(
                    native_continuity_state.get("input_cursor", 0),
                    len(self._native_messages),
                ) : len(self._native_messages)
            ]
        else:
            _budget_msgs = self._native_messages if self._use_native else messages
        self._token_tracker.recount(_budget_msgs)
        if (not native_continuity_active) and self._token_tracker.should_compact() and _compaction_should_allow(self):
            pre_compaction_tokens = self._token_tracker.total_tokens
            self._compaction_count += 1
            logger.info(
                "Proactive compaction %d triggered at %d%% token usage",
                self._compaction_count,
                int(self._token_tracker.usage_pct * 100),
            )
            try:
                if self._use_native:
                    self._native_messages, _compaction_meta = await compact_native_messages(
                        self._native_messages,
                        user_id=self._user_id,
                        session_id=tool_result_replacement_session_id(self),
                        replacement_writer=lambda records: record_tool_result_replacements(self, records),
                        content_replacement_state=getattr(self, "_content_replacement_state", None),
                    )
                    if _compaction_meta:
                        self._pending_compaction_meta = _compaction_meta
                else:
                    messages[:] = await compact_messages(messages, user_id=self._user_id)
                _budget_msgs = self._native_messages if self._use_native else messages
                self._token_tracker.recount(_budget_msgs, after_compaction=True)
                _compaction_record_progress(self, pre_compaction_tokens, self._token_tracker.total_tokens)
                # After compaction, inject session memory context so the model
                # retains key facts that compaction may have stripped. Claude
                # TS analog: src/utils/api.ts `prependUserContext` wraps user
                # context as a META_USER `<system-reminder>` block — never a
                # raw `{"role": "system"}` message (the CanonicalChainValidator
                # rejects system-role entries in history) and never a synthetic
                # `{"role": "user"}` turn (which collides with real user input
                # at audit time). We mirror that by wrapping the memory in a
                # `<system-reminder>` envelope and emitting it as a META_USER
                # turn — the message-history validators accept user-role meta
                # turns and the system-reminder envelope marks provenance for
                # downstream rendering / smoosh.
                if session_memory is not None:
                    _sm_ctx = session_memory.to_context_string()
                    if _sm_ctx.strip():
                        _sm_reminder = (
                            "<system-reminder>\n" "[Session context after compaction: %s]\n" "</system-reminder>"
                        ) % _sm_ctx
                        if self._use_native:
                            self._native_messages.append({"role": "user", "content": _sm_reminder})
                        else:
                            messages.append({"role": "user", "content": _sm_reminder})
                        logger.debug(
                            "Session memory injected after compaction as system-reminder (%d chars)",
                            len(_sm_ctx),
                        )
            except Exception as _proactive_exc:
                _compaction_record_progress(self, pre_compaction_tokens, pre_compaction_tokens)
                logger.warning("Proactive compaction failed, continuing: %s", _proactive_exc)

        native_request_messages: list[dict[str, Any]] = []
        native_request_kwargs: dict[str, Any] = {}
        provider_continuity_before = self._export_responses_continuity_state() if self._use_native else {}
        if self._use_native:
            (
                native_request_messages,
                native_request_kwargs,
                native_continuity_active,
            ) = self._build_native_request_messages(
                trim_history=True,
            )
            provider_continuity_before = self._export_responses_continuity_state()
        trace_continuity_before = self._build_trace_continuity_before(provider_continuity_before)

        # Track tool choice and message count for the step log.
        _effective_tc = getattr(self._llm, "_agent_tool_choice", None)
        if not isinstance(_effective_tc, str) or not _effective_tc:
            _effective_tc = "auto"
        if tool_choice_override:
            _effective_tc = str(tool_choice_override.get("function", {}).get("name", "override"))
        self._set_step_log_llm_context(
            tool_choice=_effective_tc,
            message_count=(len(native_request_messages) if self._use_native else len(messages)),
        )

        attempt_id: str | None = None
        try:
            _native_kwargs: dict[str, Any] = {}
            _text_kwargs: dict[str, Any] = {}
            if self._use_native:
                # Native path: pass structured messages to route_command_native
                logger.debug(
                    "Calling LLM (native): message_count=%d, first_turn=%s, tc_override=%s, continuity=%s",
                    len(native_request_messages),
                    first_turn,
                    tool_choice_override,
                    native_continuity_active,
                )
                try:
                    _native_kwargs: dict[str, Any] = {
                        "messages": native_request_messages,
                        "first_turn": first_turn,
                        "executor": self,
                        "reasoning_tier": ("background_agent" if getattr(self, "_depth", 0) > 0 else "agent"),
                    }
                    request_native_tools = self._get_request_native_tools()
                    if request_native_tools is not None:
                        _native_kwargs["native_tools"] = request_native_tools
                    request_system_prompt = getattr(self, "_system_prompt_text", "")
                    if request_system_prompt:
                        _native_kwargs["system_prompt"] = request_system_prompt
                    _native_kwargs.update(native_request_kwargs)
                    if tool_choice_override:
                        _native_kwargs["tool_choice_override"] = tool_choice_override
                    if self._model_override:
                        _native_kwargs["model_override"] = self._model_override
                    _trace_native_request_kwargs = {
                        key: copy.deepcopy(value)
                        for key, value in _native_kwargs.items()
                        if key not in {"messages", "first_turn", "executor"}
                    }
                    _trace_request = self._build_task_trace_llm_request(
                        call_kind="agent_loop",
                        request_mode="native",
                        first_turn=first_turn,
                        native_messages=native_request_messages,
                        native_kwargs=_trace_native_request_kwargs,
                        tool_choice_override=tool_choice_override,
                    )
                    attempt_id = self._append_task_trace_llm_attempt_start(
                        call_kind="agent_loop",
                        request_mode="native",
                        first_turn=first_turn,
                        continuity_before=trace_continuity_before,
                        request=_trace_request,
                        provider_payload={
                            "method": "route_command_native",
                            "kwargs": {
                                key: copy.deepcopy(value) for key, value in _native_kwargs.items() if key != "executor"
                            },
                        },
                        payload_stage="executor_to_provider",
                    )
                    response = await self._llm.route_command_native(
                        **_native_kwargs,
                    )
                except RuntimeError as exc:
                    if not _is_native_tool_calling_unsupported(exc):
                        raise
                    self._append_task_trace_llm_attempt_failure(
                        attempt_id=attempt_id,
                        call_kind="agent_loop",
                        exc=exc,
                    )
                    self._append_task_trace_llm_retry_fallback(
                        call_kind="agent_loop",
                        kind="fallback",
                        reason="native_tool_calling_unsupported",
                        next_action="return_runtime_answer",
                        attempt_id=attempt_id,
                    )
                    logger.exception("Native tool calling not supported by active provider")
                    _set_final_response(
                        self,
                        _NATIVE_TOOL_CALLING_UNSUPPORTED_TEXT,
                        continue_listening=False,
                    )
                    return None
            else:
                raise RuntimeError(
                    "AgentExecutor._get_next_response reached the text path with "
                    "provider=%s. Either the provider lost route_command_native "
                    "or _use_native was mutated mid-run; both are bugs." % type(self._llm).__name__
                )
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "AgentExecutor._get_next_response reached the text path" in str(exc):
                raise
            self._append_task_trace_llm_attempt_failure(
                attempt_id=attempt_id,
                call_kind="agent_loop",
                exc=exc,
            )
            continuity_mode = native_continuity_state.get("mode") if self._use_native else None
            if (
                is_token_limit_error(exc)
                and continuity_mode == _RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS
                and _compaction_should_allow(self)
            ):
                pre_compaction_tokens = _estimated_tokens_for_messages(self._native_messages)
                self._compaction_count += 1
                logger.warning(
                    "Token limit hit in stateless continuity mode (compaction %d), compacting replay prefix",
                    self._compaction_count,
                )
                self._append_task_trace_llm_retry_fallback(
                    call_kind="agent_loop",
                    kind="retry",
                    reason="responses_continuity_compaction",
                    next_action="compact_responses_continuity_and_retry",
                    attempt_id=attempt_id,
                    detail={"compaction_count": self._compaction_count},
                )
                try:
                    if await self._compact_native_responses_continuity(reason="token_limit_response_items", force=True):
                        _compaction_record_progress(
                            self,
                            pre_compaction_tokens,
                            _estimated_tokens_for_messages(self._native_messages),
                        )
                        return await self._get_next_response(messages, system_context, first_turn, tool_choice_override)
                    _compaction_record_progress(self, pre_compaction_tokens, pre_compaction_tokens)
                except Exception as compact_exc:
                    _compaction_record_progress(self, pre_compaction_tokens, pre_compaction_tokens)
                    logger.warning(
                        "Responses continuity compaction failed, falling back to error: %s",
                        compact_exc,
                    )
            if (
                self._use_native
                and continuity_mode == _RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS
                and _is_invalid_encrypted_content_error(exc)
                and not self._responses_invalid_encrypted_content_repair_attempted
            ):
                self._responses_invalid_encrypted_content_repair_attempted = True
                if self._repair_invalid_encrypted_responses_continuity():
                    self._append_task_trace_llm_retry_fallback(
                        call_kind="agent_loop",
                        kind="retry",
                        reason="invalid_encrypted_content_repair",
                        next_action="drop_encrypted_reasoning_and_retry",
                        attempt_id=attempt_id,
                    )
                    return await self._get_next_response(messages, system_context, first_turn, tool_choice_override)
            # C1: Auto-compaction on token-limit overflow under the shared progress guard.
            if is_token_limit_error(exc) and (not native_continuity_active) and _compaction_should_allow(self):
                pre_compaction_tokens = _estimated_tokens_for_messages(
                    self._native_messages if self._use_native else messages
                )
                self._compaction_count += 1
                logger.warning(
                    "Token limit hit (compaction %d), auto-compacting context",
                    self._compaction_count,
                )
                self._append_task_trace_llm_retry_fallback(
                    call_kind="agent_loop",
                    kind="retry",
                    reason="token_limit_compaction",
                    next_action="compact_context_and_retry",
                    attempt_id=attempt_id,
                    detail={"compaction_count": self._compaction_count},
                )
                try:
                    if self._use_native:
                        self._native_messages, _compaction_meta = await compact_native_messages(
                            self._native_messages,
                            user_id=self._user_id,
                            session_id=tool_result_replacement_session_id(self),
                            replacement_writer=lambda records: record_tool_result_replacements(self, records),
                            content_replacement_state=getattr(self, "_content_replacement_state", None),
                        )
                        if _compaction_meta:
                            self._pending_compaction_meta = _compaction_meta
                    else:
                        messages[:] = await compact_messages(messages, user_id=self._user_id)
                    _compaction_record_progress(
                        self,
                        pre_compaction_tokens,
                        _estimated_tokens_for_messages(self._native_messages if self._use_native else messages),
                    )
                    return await self._get_next_response(messages, system_context, first_turn)
                except Exception as compact_exc:
                    _compaction_record_progress(self, pre_compaction_tokens, pre_compaction_tokens)
                    logger.warning("Compaction failed, falling back to error: %s", compact_exc)

            operator_diagnostic = classify_llm_operator_error(exc)
            # When a model fallback chain ran (OpenAI -> Anthropic etc.) the
            # FINAL exc is the last-tried provider's; the earlier OpenAI
            # insufficient_quota signal that was stamped via
            # `_note_provider_overload` is preserved in the request-scoped
            # `_REQUEST_PROVIDER_OVERLOAD`. Prefer that earlier category over
            # a generic last-exception category like `provider_rate_limit`,
            # so the user sees the actual root cause not the secondary
            # rate-limit on the fallback model.
            from services.llm.operator_diagnostics import (
                recent_provider_overload_category,
            )

            preserved_category = recent_provider_overload_category()
            if (
                preserved_category
                and preserved_category != operator_diagnostic.get("category")
                and operator_diagnostic.get("category") in {"provider_rate_limit", "llm_provider_error"}
            ):
                operator_diagnostic = dict(operator_diagnostic)
                operator_diagnostic["category"] = preserved_category
                operator_diagnostic["source"] = "preserved_request_overload_signal"
            logger.warning(
                "LLM call failed during agent loop: %s diagnostic=%s preserved=%s",
                exc,
                operator_diagnostic.get("category"),
                preserved_category,
            )
            fallback_answer = user_message_for_operator_diagnostic(operator_diagnostic)
            if not fallback_answer:
                last_tool = str(getattr(self, "_last_tool_name", "") or "").strip()
                fallback_answer = _build_agent_final_answer_fallback(
                    self,
                    getattr(self, "_agent_task_description", "") or "",
                    [last_tool] if last_tool else [],
                    reason="llm_error",
                    operator_diagnostic=operator_diagnostic,
                )
            if not fallback_answer:
                fallback_answer = "The AI service hit a snag. Try again in a moment."
            _set_final_response(self, fallback_answer, continue_listening=False)
            self._final_params["operator_diagnostic"] = operator_diagnostic
            self._llm_error_occurred = True
            self._capture_diagnostic(
                user_text=getattr(self, "_agent_task_description", "") or "",
                exception=exc,
                stage="llm_call",
            )
            return None

        # Capture token usage from provider response (if available)
        if isinstance(response, dict) and self._use_native:
            self._capture_responses_continuity_from_response(response)
            self._record_task_trace_llm_exchange(
                call_kind="agent_loop",
                request_mode="native",
                response=response,
                first_turn=first_turn,
                continuity_before=trace_continuity_before,
                native_messages=native_request_messages,
                native_kwargs=_trace_native_request_kwargs,
                tool_choice_override=tool_choice_override,
            )
        elif isinstance(response, dict):
            self._record_task_trace_llm_exchange(
                call_kind="agent_loop",
                request_mode="text",
                response=response,
                first_turn=first_turn,
                continuity_before=trace_continuity_before,
                text_history=copy.deepcopy(messages[:-1]),
                text_input=messages[-1]["content"] if messages else "",
                system_context=system_context,
                tool_choice_override=tool_choice_override,
            )
        if not isinstance(response, dict):
            self._last_usage = {}
            self._record_llm_usage_for_context(self._last_usage)
            self._last_model_name = "unknown"
            self._last_no_result_reasoning = "provider_returned_non_dict_response"
            return None
        self._last_usage = _normalize_usage(response)
        self._record_llm_usage_for_context(self._last_usage)
        self._last_model_name = (
            str(response.get("_model_name") or self._get_model_name_safe()) if isinstance(response, dict) else "unknown"
        )
        if isinstance(response, dict):
            context_window = response.get("_context_window")
            if isinstance(context_window, int) and context_window > 0:
                self._token_tracker.configure(model=self._last_model_name, context_window=context_window)
        return self._parse_llm_response(response)

    def _parse_llm_response(self, response: dict[str, Any]) -> ToolCall | None:
        """Parse an LLM response dict into a ToolCall or final result.

        Shared by both native and text-based paths since the response dict
        format is normalised by the provider.

        Returns ToolCall for another tool iteration, or None for terminal.
        """
        response_type = response.get("type", "")
        self._last_llm_reasoning = _extract_reasoning(response)

        if not response_type and isinstance(response.get("name"), str):
            raw_args = response.get("arguments", response.get("args", {}))
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    # ratchet: critical-path-visibility — the tool would otherwise be
                    # invoked with silently-dropped arguments (empty {}).
                    logger.warning(
                        "Tool-call arguments were not valid JSON; dropping args and calling '%s' with empty arguments",
                        response.get("name", ""),
                    )
                    raw_args = {}
            response = {
                "type": "tool_call",
                "tool": response.get("name", ""),
                "args": raw_args if isinstance(raw_args, dict) else {},
            }
            response_type = "tool_call"

        if not response_type and isinstance(response.get("tool"), str):
            raw_args = response.get("args", {})
            response = {
                "type": "tool_call",
                "tool": response.get("tool", ""),
                "args": raw_args if isinstance(raw_args, dict) else {},
            }
            response_type = "tool_call"

        # Check if an "answer" response actually contains an embedded tool_call JSON
        if response_type == "answer" and not self._use_native:
            answer_text = response.get("answer", "")
            if isinstance(answer_text, str) and "{" in answer_text:
                from intent.json_utils import extract_json_from_llm_response

                embedded = extract_json_from_llm_response(answer_text)
                if embedded and embedded.get("type") == "tool_call":
                    logger.debug("Extracted embedded tool_call from answer text")
                    response = embedded
                    response_type = "tool_call"

        if response_type == "tool_call":
            tool_call = ToolCall.from_dict(response)
            if tool_call is not None:
                # Extract LLM reasoning text emitted before the tool call.
                tool_call.reasoning = self._last_llm_reasoning
                logger.debug("Parsed tool_call: tool=%s", tool_call.tool)
                return tool_call
            # Malformed tool call â€” flag as parse failure for retry
            logger.warning("Malformed tool_call response from LLM: %s", response)
            self._parse_failed = True
            _set_final_response(
                self,
                "The model returned a malformed tool response.",
                continue_listening=False,
            )
            self._capture_diagnostic_from_response(
                user_text=self._agent_task_description or "",
                response=response,
                detail="Malformed tool_call â€” could not parse ToolCall from response",
            )
            return None

        if response_type == "answer":
            answer = response.get("answer", "")
            raw = answer if isinstance(answer, str) else str(answer)
            _set_final_response(self, _strip_json_template(raw))
            # Propagate continue_listening from the LLM's final response
            cl_val = response.get("continue_listening")
            if cl_val is not None:
                self._final_continue_listening = bool(cl_val)
            logger.debug("LLM returned final answer (len=%d)", len(self._final_answer))
            return None

        if response_type == "ai_no_result":
            no_result = response.get("no_result")
            if not isinstance(no_result, dict):
                no_result = {
                    "reason": response.get("reason", "unknown_ai_no_result"),
                    "retryable": bool(response.get("retryable", True)),
                }
            error_state = response.get("error_state")
            if not isinstance(error_state, dict):
                error_state = {"type": "ai_no_result", **no_result}
            self._llm_error_occurred = True
            self._last_no_result_reasoning = self._last_llm_reasoning
            self._raw_final_answer = None
            self._final_answer = None
            self._final_params = {
                "no_result": no_result,
                "error_state": error_state,
            }
            operator_diagnostic = no_result.get("operator_diagnostic") or error_state.get("operator_diagnostic")
            if isinstance(operator_diagnostic, dict):
                self._final_params["operator_diagnostic"] = operator_diagnostic
                operator_message = user_message_for_operator_diagnostic(operator_diagnostic)
                if operator_message:
                    _set_final_response(self, operator_message, continue_listening=False)
            self._capture_diagnostic_from_response(
                user_text=self._agent_task_description or "",
                response=response,
                detail="LLM returned no usable assistant content",
            )
            return None

        if response_type in {"ask_user", "clarification"}:
            question = response.get("question", response.get("answer", response.get("text", "")))
            raw = question if isinstance(question, str) else str(question)
            _set_final_response(self, _strip_json_template(raw), continue_listening=True)
            logger.debug(
                "LLM returned ask_user question via %s (len=%d)",
                response_type,
                len(self._final_answer),
            )
            return None

        if response_type == "final_answer":
            answer = response.get("answer", response.get("text", ""))
            raw = answer if isinstance(answer, str) else str(answer)
            _set_final_response(self, _normalize_terminal_model_text(raw), continue_listening=False)
            logger.debug("LLM returned typed final answer (len=%d)", len(self._final_answer))
            return None

        if response_type in {"payment_gate", "signature_gate"}:
            gate_message = response.get("message", response.get("answer", response.get("text", "")))
            raw = gate_message if isinstance(gate_message, str) else str(gate_message)
            _set_final_response(self, _strip_json_template(raw), continue_listening=False)
            logger.warning(
                "LLM returned unsupported typed gate response; treating as plain answer: %s",
                response_type,
            )
            return None

        if response_type == "command":
            if not self._use_native:
                command_name = str(response.get("command", "") or "")
                known_tools = self._get_request_native_tools()
                if known_tools is None and self._mcp_hub is not None:
                    try:
                        hub_tools = self._mcp_hub.list_tools()
                        if isinstance(hub_tools, list):
                            known_tools = hub_tools
                    except Exception:
                        known_tools = None
                if isinstance(known_tools, (list, tuple)) and any(
                    isinstance(tool, dict) and tool.get("name") == command_name for tool in known_tools
                ):
                    params = response.get("params", {})
                    tool_call = ToolCall.from_dict(
                        {
                            "type": "tool_call",
                            "tool": command_name,
                            "args": params if isinstance(params, dict) else {},
                        }
                    )
                    if tool_call is not None:
                        tool_call.reasoning = self._last_llm_reasoning
                        logger.debug(
                            "Parsed legacy command as text tool_call: tool=%s",
                            tool_call.tool,
                        )
                        return tool_call
            self._final_command = response.get("command", "")
            params = response.get("params", {})
            self._final_params = params if isinstance(params, dict) else {}
            # Also capture answer if present (e.g., recommendation + play command)
            answer = response.get("answer")
            if isinstance(answer, str):
                _set_final_response(self, answer)
            logger.debug("LLM returned command chars=%d", len(self._final_command or ""))
            return None

        # Unknown type â€” flag as parse failure for retry
        logger.warning(
            "Unknown LLM response type in agent loop: %s (response: %s)",
            response_type,
            str(response)[:200],
        )
        self._parse_failed = True
        _set_final_response(self, str(response.get("answer", response.get("message", ""))))
        if not self._final_answer:
            _set_final_response(
                self,
                "The model returned an unsupported response type.",
                continue_listening=False,
            )
        self._capture_diagnostic_from_response(
            user_text=self._agent_task_description or "",
            response=response,
            detail="Unknown LLM response type: %s" % response_type,
        )
        return None

    async def _call_llm_for_answer(
        self,
        messages: list[dict[str, str]],
        system_context: str,
    ) -> str:
        """Make a final LLM call specifically to get a summary answer.

        AGT-007 root cause fix: When using native tool calling, OpenAI nulls
        ``message.content`` whenever tool_calls are present.  If the model
        decides to call a tool instead of summarizing, we get no text back.
        Fix: temporarily suppress tools on the provider so the API call has
        no tools and the model MUST return plain text.
        """
        budget_gate = await self._check_managed_llm_spend_cap_async()
        if not budget_gate.allowed:
            self._record_agent_gate_denial_telemetry(
                gate_name="managed_llm_spend_cap",
                current_usage=budget_gate.spent_cents,
                limit_value=budget_gate.budget_cents,
            )
            return self._managed_llm_budget_message(budget_gate)

        attempt_id: str | None = None
        try:
            answer_trace_continuity_before = self._export_responses_continuity_state() if self._use_native else {}
            if self._use_native:
                answer_native_messages, answer_native_kwargs, _ = self._build_native_request_messages(trim_history=True)
                try:
                    _answer_native_kwargs: dict[str, Any] = {
                        "messages": answer_native_messages,
                        "first_turn": False,
                        "native_tools": [],
                        "executor": self,
                    }
                    _answer_native_kwargs.update(answer_native_kwargs)
                    request_system_prompt = getattr(self, "_system_prompt_text", "")
                    if request_system_prompt:
                        _answer_native_kwargs["system_prompt"] = request_system_prompt
                    if self._model_override:
                        _answer_native_kwargs["model_override"] = self._model_override
                    _answer_trace_request_kwargs = {
                        key: copy.deepcopy(value)
                        for key, value in _answer_native_kwargs.items()
                        if key not in {"messages", "first_turn", "executor"}
                    }
                    _answer_trace_request = self._build_task_trace_llm_request(
                        call_kind="summary_answer",
                        request_mode="native",
                        first_turn=False,
                        native_messages=answer_native_messages,
                        native_kwargs=_answer_trace_request_kwargs,
                    )
                    attempt_id = self._append_task_trace_llm_attempt_start(
                        call_kind="summary_answer",
                        request_mode="native",
                        first_turn=False,
                        continuity_before=answer_trace_continuity_before,
                        request=_answer_trace_request,
                        provider_payload={
                            "method": "route_command_native",
                            "kwargs": {
                                key: copy.deepcopy(value)
                                for key, value in _answer_native_kwargs.items()
                                if key != "executor"
                            },
                        },
                        payload_stage="executor_to_provider",
                    )
                    response = await self._llm.route_command_native(
                        **_answer_native_kwargs,
                    )
                except RuntimeError as exc:
                    if not _is_native_tool_calling_unsupported(exc):
                        raise
                    self._append_task_trace_llm_attempt_failure(
                        attempt_id=attempt_id,
                        call_kind="summary_answer",
                        exc=exc,
                    )
                    self._append_task_trace_llm_retry_fallback(
                        call_kind="summary_answer",
                        kind="fallback",
                        reason="native_tool_calling_unsupported",
                        next_action="return_runtime_answer",
                        attempt_id=attempt_id,
                    )
                    logger.exception("Native tool calling not supported by active provider")
                    return _NATIVE_TOOL_CALLING_UNSUPPORTED_TEXT
            else:
                logger.error("Summary answer requested without native tool calling")
                return _NATIVE_TOOL_CALLING_UNSUPPORTED_TEXT
            if isinstance(response, dict) and self._use_native:
                self._capture_responses_continuity_from_response(response)
            if isinstance(response, dict):
                if self._use_native:
                    self._record_task_trace_llm_exchange(
                        call_kind="summary_answer",
                        request_mode="native",
                        response=response,
                        first_turn=False,
                        continuity_before=answer_trace_continuity_before,
                        native_messages=answer_native_messages,
                        native_kwargs=_answer_trace_request_kwargs,
                    )
                else:
                    self._record_task_trace_llm_exchange(
                        call_kind="summary_answer",
                        request_mode="text",
                        response=response,
                        first_turn=False,
                        continuity_before={},
                        text_history=copy.deepcopy(messages[:-1]),
                        text_input=messages[-1]["content"] if messages else "",
                        system_context=system_context,
                    )

            # AGT-007 fix: extract answer text from multiple response shapes.
            # The LLM may return {"type": "answer", "answer": "..."} (normal),
            # {"type": "tool_call", ...} (model tried to call a tool instead of
            # summarizing), or {"type": "text", "content": "..."} (fallback
            # from provider when tool_calls parse fails).  We must handle all
            # three to avoid the "empty answer" false-failure.
            resp_type = response.get("type", "")
            answer = response.get("answer", "")

            # 1. Normal answer path
            if isinstance(answer, str) and answer:
                return _normalize_terminal_model_text(answer)

            # 1b. Typed terminal responses that place text outside "answer"
            if resp_type in {"ask_user", "clarification"}:
                question = response.get("question", response.get("text", ""))
                if isinstance(question, str) and question:
                    return _strip_json_template(question)

            if resp_type in {"payment_gate", "signature_gate"}:
                gate_message = response.get("message", response.get("text", ""))
                if isinstance(gate_message, str) and gate_message:
                    return _strip_json_template(gate_message)

            # 2. "text" type response (provider fallback) â€” answer lives in "content"
            if resp_type == "text":
                content = response.get("content", "")
                if isinstance(content, str) and content:
                    return _normalize_terminal_model_text(content)

            # 3. tool_call response â€” LLM tried to call a tool instead of
            #    summarizing. Extract any reasoning/content text that
            #    accompanied the tool call.
            if resp_type == "tool_call":
                reasoning = _extract_reasoning(response)
                if reasoning:
                    return _strip_json_template(reasoning)
                raw = response.get("_raw_content")
                if isinstance(raw, dict):
                    raw_text = raw.get("content", "")
                    if isinstance(raw_text, str) and raw_text.strip():
                        return _strip_json_template(raw_text.strip())

            return _SUMMARY_FALLBACK_SENTINEL
        except Exception as exc:
            self._append_task_trace_llm_attempt_failure(
                attempt_id=attempt_id,
                call_kind="summary_answer",
                exc=exc,
            )
            logger.warning("Final summary LLM call failed: %s", exc)
            return "I investigated your request but ran into an issue summarizing."

    async def _speak(self, text: str) -> None:
        """Send progress text through the channel (preferred) or TTS fallback."""
        # Prefer channel if available
        if self._channel is not None:
            try:
                await self._channel.send(text)
                return
            except Exception as exc:
                logger.debug("Agent channel send failed: %s", exc)
        # Fallback to TTS
        if self._tts is None:
            return
        try:
            speak = getattr(self._tts, "speak", None)
            if callable(speak):
                await speak(text)
        except Exception as exc:
            logger.debug("Agent TTS failed: %s", exc)

    @staticmethod
    def _looks_progress_label_like(title: str, *, markdown_title: bool = False) -> bool:
        if not title or "\n" in title or title.startswith("```"):
            return False
        if len(title) > 80 or title.endswith((".", "?", "!")):
            return False
        words = title.split()
        if not words:
            return False
        if markdown_title:
            return True
        return len(words) <= 8 and title[0].isupper()

    def _progress_label_from_reasoning(self, reasoning: str | None) -> str:
        if not reasoning:
            return ""
        for raw_line in str(reasoning).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("```"):
                return ""
            markdown_title = line.startswith("**") and line.endswith("**") and len(line) > 4
            if line.startswith("**"):
                line = line[2:]
            if line.endswith("**"):
                line = line[:-2]
            title = line.strip()
            if self._looks_progress_label_like(title, markdown_title=markdown_title):
                return title
            return ""
        return ""

    def _format_agent_progress(
        self,
        tool_name: str,
        step_number: int,
        status: str,
        *,
        reasoning: str | None = None,
    ) -> str:
        labels = {
            "browser_navigate": "opening page",
            "browser_snapshot": "reading page",
            "browser_interact": "using page controls",
            "browser_fill_form": "filling form",
            "browser_run_script": "using page automation",
            "web_search": "searching",
            "web_read": "reading page",
            "calendar": "checking calendar",
            "play_music": "starting music",
            "search_tracks": "searching music",
            "think": "planning",
        }
        action = self._progress_label_from_reasoning(reasoning) or labels.get(
            tool_name, tool_name.replace("_", " ") if tool_name else "working"
        )
        if status == "working":
            return "step %d: %s" % (step_number, action)
        if status == "ok":
            return action
        return "%s needs attention" % action

    async def _broadcast_agent_progress(
        self,
        tool_name: str,
        step_number: int,
        status: str,
        *,
        duration_ms: int = 0,
        reasoning: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_output: str | None = None,
    ) -> str:
        progress = self._format_agent_progress(
            tool_name,
            step_number,
            status,
            reasoning=reasoning,
        )
        ts = datetime.now(tz=UTC).isoformat()
        try:
            if self._task_trace is not None:
                self._task_trace.append_progress(
                    ts=ts,
                    step=step_number,
                    message=progress,
                    status=status,
                    tool_name=tool_name,
                )
        except Exception as exc:
            logger.debug("Agent progress trace append failed: %s", exc)

        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "step_number": step_number,
            "status": status,
            "duration_ms": duration_ms,
            "progress": progress,
            "message": progress,
        }
        if tool_input is not None:
            payload["tool_input"] = redact_card_data(copy.deepcopy(tool_input))
        if tool_output is not None:
            payload["tool_output"] = redact_card_data(tool_output)
        try:
            from services.llm.stream_bus import (
                get_current_command_stream_id,
                get_current_command_stream_metadata,
                publish_tool_event,
            )

            stream_id = get_current_command_stream_id()
            if stream_id:
                stream_metadata = get_current_command_stream_metadata()
                payload.update(
                    {
                        key: value
                        for key, value in stream_metadata.items()
                        if key in {"stream_id", "thread_id", "message_id"}
                    }
                )
                publish_tool_event(payload, stream_id=stream_id)
        except Exception as exc:
            logger.debug("Agent progress stream publish failed: %s", exc)

        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is not None:
                await hub.broadcast(
                    "agent_progress",
                    payload,
                    user_id=self._get_effective_user_id(),
                    force=True,
                )
        except Exception as exc:
            logger.debug("Agent progress WS broadcast failed: %s", exc)

        self._update_overlay_status(progress)
        return progress

    async def _broadcast_agent_step(
        self,
        tool_name: str,
        step_number: int,
        duration_ms: int,
        status: str,
        *,
        reasoning: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_output: str | None = None,
    ) -> None:
        """Broadcast the live UI progress event for a completed agent step."""
        await self._broadcast_agent_progress(
            tool_name,
            step_number,
            status,
            duration_ms=duration_ms,
            reasoning=reasoning,
            tool_input=tool_input,
            tool_output=tool_output,
        )

    async def _broadcast_paid_action_gate(self, tool_name: str, result: ToolResult) -> None:
        payload = _paid_action_gate_payload(result)
        if payload is None:
            return
        error_code = str(payload.get("error_code") or "")
        event_type = "phone_tos_required" if error_code == "phone_tos_required" else "phone_login_required"
        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is None:
                return
            await hub.broadcast(
                event_type,
                {
                    **payload,
                    "tool_name": tool_name,
                    "message": payload.get("message") or result.error or "",
                },
                user_id=self._get_effective_user_id(),
                force=True,
            )
        except Exception as exc:
            logger.debug("Paid-action gate WS broadcast failed for %s: %s", tool_name, exc)

    @staticmethod
    def _agent_progress_terminal_status(outcome: str) -> str:
        if outcome == "cancelled":
            return "cancelled"
        if outcome in {"success", "payment_gate", "signature_gate"}:
            return "complete"
        return "error"

    async def _broadcast_terminal_agent_progress(self, *, status: str, step_number: int) -> None:
        ts = datetime.now(tz=UTC).isoformat()
        try:
            if self._task_trace is not None:
                self._task_trace.append_progress(
                    ts=ts,
                    step=step_number,
                    message="",
                    status=status,
                    tool_name="",
                )
        except Exception as exc:
            logger.debug("Terminal agent progress trace append failed: %s", exc)

        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is not None:
                await hub.broadcast(
                    "agent_progress",
                    {
                        "tool_name": "",
                        "step_number": step_number,
                        "status": status,
                        "duration_ms": 0,
                        "progress": "",
                        "message": "",
                        "terminal": True,
                    },
                    user_id=self._get_effective_user_id(),
                    force=True,
                )
        except Exception as exc:
            logger.debug("Terminal agent progress WS broadcast failed: %s", exc)

    def _hide_overlay(self, *, outcome: str = "") -> None:
        """Hide the agentic overlay if it was shown.

        Parameters
        ----------
        outcome : str
            Forwarded to the overlay controller so the frontend can show
            a completion toast (``"done"``, ``"error"``, ``"cancelled"``).
        """
        if self._overlay is None or not self._overlay_shown:
            return
        try:
            self._overlay.hide(outcome=outcome)
        except Exception as exc:
            logger.debug("Overlay hide failed: %s", exc)
        self._overlay_shown = False

    def _update_overlay_status(self, status: str) -> None:
        """Update the agentic overlay status without hiding it."""
        if self._overlay is None or not self._overlay_shown:
            return
        try:
            self._overlay.update_agentic_status(status=status)
        except Exception as exc:
            logger.debug("Overlay status update failed: %s", exc)

    def _update_overlay_phase(self, phase: str) -> None:
        """Update the overlay phase (acting/thinking/user_input)."""
        if self._overlay is None or not self._overlay_shown:
            return
        try:
            self._overlay.update_agentic_status(phase=phase)
        except Exception as exc:
            logger.debug("Overlay phase update failed: %s", exc)

    async def _cleanup_browser(self) -> None:
        """No-op â€” legacy browser cleanup removed (CLOG-5).

        Browser lifecycle is managed by the MCP browser server now.
        Method kept as a stub because callers still await it.
        """

    async def _start_payment_stream(self) -> None:
        """Start a browser streaming session so the user can complete payment remotely.

        Fire-and-forget: on any failure, falls back to TTS-only payment
        notification so the payment gate still works even without streaming.
        """
        try:
            page = self._get_active_browser_page()
            if page is None:
                logger.debug("No active browser page for payment stream")
                await self._speak(
                    "Your order is ready for payment. " "Please check the browser window to complete checkout."
                )
                return

            from services.browser.stream_session import (
                build_stream_url,
                get_stream_session_manager,
            )

            mgr = get_stream_session_manager()
            token = await mgr.create_session(page, user_id=self._user_id)
            url = build_stream_url(token)

            message = "Your order is ready. Tap to complete payment: %s" % url
            await self._speak(message)
            if self._channel is not None:
                try:
                    await self._channel.send(message)
                except Exception:
                    logger.debug("Failed to deliver stream URL via channel")
        except Exception:
            logger.exception("Failed to start payment stream")
            try:
                await self._speak(
                    "Your order is ready for payment. " "Please check the browser window to complete checkout."
                )
            except Exception:
                logger.debug("TTS fallback also failed for payment stream")

    def _get_origin_channel_type(self) -> str | None:
        """Return the originating request channel for gate-card replies."""
        channel = self._channel
        if channel is None:
            try:
                from messaging.channel import get_request_channel

                channel = get_request_channel()
            except Exception:
                channel = None
        channel_type = str(getattr(channel, "channel_type", "") or "").strip().lower()
        if channel_type == "http":
            return "web"
        return channel_type or None

    def _finalize_gate_checkpoint(
        self,
        checkpoint: TaskCheckpoint,
        *,
        final_answer: str | None,
        outcome: str,
        payment_gate: bool,
        signature_gate: bool,
        payment_confirmation_ctx: dict[str, Any] | None,
    ) -> None:
        """Persist durable gate waiting state or complete when no wait exists."""
        if signature_gate:
            checkpoint.status = "waiting_for_user"
            checkpoint.outcome = "signature_gate"
            checkpoint.context["pending_question"] = final_answer or ""
            checkpoint.context["pending_gate_type"] = "signature"
            checkpoint.context["signature_gate_page_url"] = self._last_page_url or ""
            checkpoint.updated_at = datetime.now(UTC).isoformat()
            save_checkpoint(checkpoint)
            return

        if payment_gate and payment_confirmation_ctx:
            checkpoint.status = "waiting_for_user"
            checkpoint.outcome = "payment_gate"
            checkpoint.context["pending_question"] = final_answer or ""
            checkpoint.context["pending_gate_type"] = "payment"
            checkpoint.context["confirm_token"] = payment_confirmation_ctx["token"]
            checkpoint.context["confirmation_url"] = payment_confirmation_ctx["url"]
            checkpoint.context["payment_order_summary"] = payment_confirmation_ctx["order_summary"]
            checkpoint.context["payment_page_ref"] = payment_confirmation_ctx["page_ref"]
            checkpoint.context["payment_page_url"] = (
                payment_confirmation_ctx.get("page_url") or self._last_page_url or ""
            )
            checkpoint.context["payment_merchant_url"] = payment_confirmation_ctx.get("merchant_url") or ""
            checkpoint.updated_at = datetime.now(UTC).isoformat()
            save_checkpoint(checkpoint)
            return

        if outcome == "llm_error":
            save_checkpoint(checkpoint)
            return

        if outcome == "incomplete":
            checkpoint.status = "incomplete"
            checkpoint.outcome = outcome
            checkpoint.updated_at = datetime.now(UTC).isoformat()
            save_checkpoint(checkpoint)
            return

        if outcome == "tool_error":
            checkpoint.status = "failed"
            checkpoint.outcome = outcome
            checkpoint.updated_at = datetime.now(UTC).isoformat()
            save_checkpoint(checkpoint)
            return

        mark_complete(checkpoint, "completed", outcome)

    async def drain_post_answer_teardown(self) -> None:
        """Await this turn's post-answer teardown (read-your-writes barrier).

        Lane C (#466): the completed-checkpoint write and terminal broadcast are
        scheduled off the answer path so the user's answer returns first. A
        consumer that must observe the fully-persisted end state -- a test, an
        oracle reading the checkpoint seconds later -- awaits this before reading.
        The live answer path never calls it (that would re-block the user); the
        tail drains autonomously on the long-lived server loop.
        """
        await _drain_post_answer_owner(self._post_answer_teardown_tasks)

    def _schedule_completed_checkpoint_persist(self, checkpoint: TaskCheckpoint, outcome: str) -> None:
        """Persist a *completed* task checkpoint off the answer path (Lane C #466).

        The in-memory completion status is stamped synchronously (cheap); only
        the Fernet-encrypt + serialize + disk write (~64ms on a 30KB history,
        larger on multi-step turns) is offloaded to a worker thread. Safe because
        a completed checkpoint is single-use: it is never resumed, so a lost
        write on an abnormal process exit costs only a diagnostic record, and no
        later turn mutates this object. Gate (waiting_for_user) and error
        checkpoints -- whose durability IS load-bearing for resume -- keep
        writing synchronously via ``_finalize_gate_checkpoint`` and never reach
        here.
        """
        checkpoint.status = "completed"
        checkpoint.outcome = outcome
        checkpoint.updated_at = datetime.now(UTC).isoformat()
        schedule_post_answer_sync(
            "completed_checkpoint",
            lambda: save_checkpoint(checkpoint),
            owner_set=self._post_answer_teardown_tasks,
        )

    def _acquire_payment_confirmation_channel(self) -> Any | None:
        """Find the request channel used to deliver payment confirmation updates."""
        if self._channel is None:
            try:
                from messaging.channel import get_request_channel

                request_channel = get_request_channel()
                if request_channel is not None:
                    self._channel = request_channel
                    logger.info(
                        "Payment channel from request contextvar: %s",
                        getattr(request_channel, "channel_type", "unknown"),
                    )
            except Exception:
                logger.debug("request_channel lookup failed", exc_info=True)

        if self._channel is None:
            try:
                from messaging.hub import get_messaging_hub

                hub = get_messaging_hub()
                if hub is not None:
                    for ch in hub.active_channels():
                        self._channel = ch
                        logger.info(
                            "Acquired messaging channel from hub: %s",
                            getattr(ch, "channel_type", "unknown"),
                        )
                        break
            except Exception:
                logger.debug("Could not acquire messaging channel from hub", exc_info=True)

        return self._channel

    def _payment_gate_session_id(self) -> str:
        """Return the session key used by browser-side payment gate overrides."""

        return str(self._session_id or getattr(self, "task_id", None) or id(self))

    def _enrich_payment_ceiling_context(self, order_summary: dict[str, Any]) -> None:
        """Attach per-user/session ceiling context to a confirmation summary."""

        from services.payments.purchase_ceiling import (
            CEILING_UNREADABLE,
            get_session_purchase_ceiling_cents,
            get_session_spend_cents,
            order_total_cents,
        )

        user_id = self._get_effective_user_id()
        gate_session_id = self._payment_gate_session_id()
        total_cents = order_total_cents(order_summary)
        if total_cents is not None:
            order_summary["total_cents"] = total_cents
        order_summary["gate_session_id"] = gate_session_id
        session_ceiling = get_session_purchase_ceiling_cents(user_id)
        session_spent = get_session_spend_cents(user_id, gate_session_id)
        # An unreadable ceiling (#4221) must not be shown/serialized as a number
        # or as "no cap." Enforcement re-reads the ceiling fresh at confirm time
        # and fails closed on the sentinel; here we only surface a display-safe
        # marker so the confirmation summary stays JSON-clean.
        ceiling_unreadable = session_ceiling is CEILING_UNREADABLE
        order_summary["session_ceiling_unreadable"] = ceiling_unreadable
        order_summary["session_purchase_ceiling_cents"] = None if ceiling_unreadable else session_ceiling
        order_summary["session_spent_cents"] = session_spent
        if not ceiling_unreadable and session_ceiling is not None:
            order_summary["session_remaining_cents"] = max(0, session_ceiling - session_spent)

    def _apply_payment_review_request_to_summary(self, order_summary: dict[str, Any]) -> None:
        """Merge structured fields from an explicit payment(request_review) call."""

        request = getattr(self, "_last_payment_review_request", None)
        if not isinstance(request, dict):
            return

        merchant = str(request.get("merchant") or "").strip()
        total = str(request.get("total") or "").strip()
        summary = str(
            request.get("order_summary")
            or request.get("summary")
            or request.get("description")
            or request.get("notes")
            or ""
        ).strip()

        if merchant and not order_summary.get("merchant"):
            order_summary["merchant"] = merchant
        if total:
            order_summary["total"] = total
        items = _payment_request_items(request)
        if items and not order_summary.get("items"):
            order_summary["items"] = items
        address = _delivery_address_from_request(request)
        if address and not order_summary.get("delivery_address"):
            order_summary["delivery_address"] = address
        if summary and _is_generic_payment_order_text(str(order_summary.get("description") or "")):
            order_summary["description"] = summary

    def _apply_context_delivery_address_to_summary(self, order_summary: dict[str, Any]) -> None:
        """Backfill the delivery address from saved settings when no structured tool arg exists."""

        existing = order_summary.get("delivery_address")
        if isinstance(existing, dict) and any(existing.get(key) for key in ("street", "city", "state", "zip")):
            return
        if isinstance(existing, str) and existing.strip():
            return

        try:
            from ui.settings_manager import get_settings_manager

            saved = get_settings_manager().get(
                "delivery_address",
                {},
                user_id=self._get_effective_user_id(),
            )
        except Exception:
            saved = None
        address = _delivery_address_from_value(saved)
        if address:
            order_summary["delivery_address"] = address

    async def _extract_dom_order_details(self, page: Any, order_summary: dict[str, Any]) -> None:
        """Extract visible checkout summary details from the browser DOM."""

        if page is None:
            return
        try:
            details = await page.evaluate("""
                () => {
                    const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const textOf = (el) => norm(el && (el.innerText || el.textContent || ''));
                    const firstText = (selectors) => {
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            const text = textOf(el);
                            if (text) return text;
                        }
                        return '';
                    };
                    const host = (location.hostname || '').replace(/^www\\./, '').toLowerCase();
                    let merchant = firstText([
                        '[data-testid*="merchant" i]',
                        '[data-testid*="restaurant" i]',
                        '[data-testid*="store" i]',
                        '[class*="merchant" i]',
                        '[class*="restaurant" i]',
                        '[class*="store-name" i]',
                    ]);
                    if (!merchant && host.includes('dominos')) merchant = "Domino's Pizza";
                    if (!merchant) merchant = norm((document.title || '').split(/[|\\-]/)[0]);

                    const summaryText = firstText([
                        '[data-testid*="order-summary" i]',
                        '[data-testid*="cart-summary" i]',
                        '[data-testid*="checkout-summary" i]',
                        '[class*="order-summary" i]',
                        '[class*="cart-summary" i]',
                        '[class*="checkout-summary" i]',
                        'aside',
                    ]);

                    const itemNodes = Array.from(document.querySelectorAll([
                        '[data-testid*="cart-item" i]',
                        '[data-testid*="order-item" i]',
                        '[data-testid*="line-item" i]',
                        '[class*="cart-item" i]',
                        '[class*="order-item" i]',
                        '[class*="line-item" i]',
                        '[class*="product" i]',
                    ].join(',')));
                    const items = [];
                    const seen = new Set();
                    for (const el of itemNodes) {
                        const text = textOf(el);
                        if (!text || text.length < 4 || text.length > 240 || seen.has(text)) continue;
                        seen.add(text);
                        items.push({name: text, description: text});
                        if (items.length >= 8) break;
                    }

                    const domTotal = firstText([
                        '[data-testid*="order-total" i]',
                        '[data-testid*="total" i]',
                        '.order-total',
                        '.cart-total',
                        '.checkout-total',
                        '.summary-total',
                        '.grand-total',
                        '[class*="order"][class*="total"]',
                        '[class*="total"][class*="price"]',
                    ]);

                    const deliveryAddress = firstText([
                        '[data-testid*="delivery-address" i]',
                        '[class*="delivery-address" i]',
                    ]);

                    return {
                        merchant: merchant,
                        items: items,
                        summary_text: summaryText ? summaryText.slice(0, 700) : '',
                        dom_total: domTotal,
                        delivery_address: deliveryAddress,
                    };
                }
                """)
        except Exception:
            logger.debug("DOM order summary extraction failed", exc_info=True)
            return

        if not isinstance(details, dict):
            return

        merchant = str(details.get("merchant") or "").strip()
        if merchant and not order_summary.get("merchant"):
            order_summary["merchant"] = merchant[:120]

        items = details.get("items")
        if isinstance(items, list) and items and not order_summary.get("items"):
            from services.payments.purchase_ceiling import parse_money_cents

            clean_items = []
            for item in items[:8]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                clean_item: dict[str, Any] = {
                    "name": name[:140],
                    "description": name[:140],
                }
                price = str(item.get("price") or "").strip()
                if price:
                    clean_item["price"] = price[:32]
                    price_cents = parse_money_cents(price)
                    if price_cents is not None:
                        clean_item["price_cents"] = price_cents
                quantity = item.get("quantity")
                if isinstance(quantity, int | float) and quantity > 0:
                    clean_item["quantity"] = int(quantity)
                clean_items.append(clean_item)
            if clean_items:
                order_summary["items"] = clean_items

        summary_text = str(details.get("summary_text") or "").strip()
        if summary_text and _is_generic_payment_order_text(str(order_summary.get("description") or "")):
            order_summary["description"] = summary_text[:700]

        dom_total = str(details.get("dom_total") or "").strip()
        if dom_total and not order_summary.get("dom_total"):
            from services.payments.purchase_ceiling import parse_money_cents

            order_summary["dom_total"] = dom_total
            order_summary["dom_total_cents"] = parse_money_cents(dom_total)
            order_summary["total_source"] = "dom"

        address = _delivery_address_from_value(details.get("delivery_address"))
        if address and not order_summary.get("delivery_address"):
            order_summary["delivery_address"] = address

    async def _enrich_payment_order_summary(self, page: Any, order_summary: dict[str, Any]) -> None:
        """Populate confirmation-page order fields before creating the session."""

        self._apply_payment_review_request_to_summary(order_summary)
        await self._extract_dom_order_details(page, order_summary)
        await self._extract_dom_total(page, order_summary)
        self._apply_context_delivery_address_to_summary(order_summary)

    async def _prepare_payment_confirmation_session(self) -> dict[str, Any]:
        """Create the hosted payment confirmation session before AgentResult exists."""
        from services.payments.confirmation import get_confirmation_manager

        channel = self._acquire_payment_confirmation_channel()
        mgr = get_confirmation_manager()
        gate_message = self._final_answer or ""
        order_text = gate_message_body(gate_message, "payment")

        order_summary: dict[str, Any] = {
            "description": order_text,
            "raw_gate_message": gate_message,
        }

        # Two different questions, two different signals:
        #   (a) "Should we bypass the merchant-URL requirement?"
        #       → Based on the executor's *operating* context. The in-call
        #       phone agent has session_id="phone:<call_id>" (set in
        #       telephony/call_manager.py at the AgentExecutor construction
        #       site); the desktop agent that issued the dial does not.
        #   (b) "Where do we deliver the confirmation link?"
        #       → Based on the issuer's channel (web card / SMS / email).
        #       That's what _get_origin_channel_type() answers.
        # Conflating these caused the payment handoff bypass to fail when a
        # web-issued /v1/command spawned a phone call: origin_channel="web"
        # so the bypass was skipped, the merchant-URL check raised, and
        # Viola read the payment handoff aloud to the recipient instead
        # of the runtime intercepting it.
        origin_channel = self._get_origin_channel_type()
        session_id = getattr(self, "_session_id", None) or ""
        is_phone_call_context = isinstance(session_id, str) and session_id.startswith("phone:")
        is_voice_channel = is_phone_call_context or origin_channel in {
            "phone",
            "voice",
            "voice-stream",
        }

        if is_voice_channel:
            self._enrich_payment_ceiling_context(order_summary)
            token, url = mgr.create_session(
                order_summary,
                "",
                user_id=self._get_effective_user_id(),
                task_id=getattr(self, "task_id", None),
                channel=(getattr(self._channel, "channel_type", None) if self._channel is not None else None),
                page=None,
                metadata=None,
                executor=self,
            )
            await mgr.ensure_cloud_synced(token)
            return {
                "mgr": mgr,
                "token": token,
                "url": url,
                "channel": channel,
                "page_ref": "",
                "page_url": "",
                "merchant_url": "",
                "page": None,
                "order_summary": order_summary,
            }

        request_url = _payment_review_url_from_request(getattr(self, "_last_payment_review_request", None))
        explicit_review_url = self._usable_explicit_payment_review_url(
            request_url or getattr(self, "_last_payment_review_page_url", None)
        )
        preferred_page_url = self._last_page_url or explicit_review_url or ""
        page = self._get_active_browser_page()
        if page is None:
            page = await self._get_active_browser_page_from_cdp(preferred_page_url)
        page_ref = str(id(page)) if page else ""
        current_page_url = self._page_url(page)
        page_url = current_page_url or preferred_page_url
        merchant_url = (
            self._usable_merchant_resume_url(preferred_page_url)
            or self._usable_merchant_resume_url(page_url)
            or explicit_review_url
            or self._usable_explicit_payment_review_url(page_url)
        )
        if not merchant_url:
            logger.warning(
                "Payment confirmation refused gate emit without checkout merchant URL: current_url=%s preferred_url=%s",
                current_page_url[:120],
                preferred_page_url[:120],
            )
            raise _PaymentConfirmationGateRefused("payment confirmation requires a checkout merchant URL")
        if current_page_url and not self._same_merchant_resume_page(current_page_url, merchant_url):
            logger.warning(
                "Payment confirmation refused gate emit because current browser page does not match merchant checkout: "
                "merchant_url=%s current_url=%s",
                merchant_url[:120],
                current_page_url[:120],
            )
            raise _PaymentConfirmationGateRefused("payment confirmation browser page mismatch")
        await self._enrich_payment_order_summary(page, order_summary)
        self._enrich_payment_ceiling_context(order_summary)

        token, url = mgr.create_session(
            order_summary,
            page_ref,
            user_id=self._get_effective_user_id(),
            task_id=getattr(self, "task_id", None),
            channel=(getattr(self._channel, "channel_type", None) if self._channel is not None else None),
            page=page,
            metadata={"merchant_url": merchant_url} if merchant_url else None,
            executor=self,
        )
        await mgr.ensure_cloud_synced(token)
        return {
            "mgr": mgr,
            "token": token,
            "url": url,
            "channel": channel,
            "page_ref": page_ref,
            "page_url": page_url,
            "merchant_url": merchant_url,
            "page": page,
            "order_summary": order_summary,
        }

    async def _start_payment_confirmation(self) -> None:
        """Compatibility wrapper for old callers; new gate finalization pre-creates ctx."""
        try:
            ctx = await self._prepare_payment_confirmation_session()
        except Exception:
            logger.exception("Failed to prepare payment confirmation session")
            await self._start_payment_stream()
            return
        await self._run_payment_confirmation_wait(ctx)

    def _payment_confirmation_user_context(
        self,
        session: Any,
        token: str | None,
        card_label: str,
    ) -> dict[str, Any]:
        """Build MCP metadata for code-owned post-approval payment fill."""
        payment_confirmation: dict[str, Any] = {
            "card_label": card_label,
            "confirmation_token": token,
            "cvc_one_shot": getattr(session, "local_payment_cvc", None),
            "payment_session_id": self._payment_gate_session_id(),
            "ceiling_override": getattr(session, "ceiling_override_for_purchase", False),
        }
        return {
            "user_id": self._get_effective_user_id(),
            "session_id": self._session_id,
            "gate_session_id": self._payment_gate_session_id(),
            "payment_confirmation": payment_confirmation,
        }

    def _payment_browser_resume_user_context(self) -> dict[str, Any]:
        """Build non-sensitive MCP metadata for same-provider browser resume checks."""
        return {
            "user_id": self._get_effective_user_id(),
            "session_id": self._session_id,
            "gate_session_id": self._payment_gate_session_id(),
        }

    @staticmethod
    def _mcp_browser_result_url(result: Any) -> str:
        """Extract a browser URL from a legacy MCP result dict."""
        if not isinstance(result, dict):
            return ""
        for source in (result.get("data"), result):
            if isinstance(source, dict):
                url = source.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
        display = result.get("display")
        if isinstance(display, str) and display.strip():
            try:
                data = json.loads(display)
            except Exception:
                data = None
            if isinstance(data, dict):
                url = data.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
        return ""

    async def _mcp_browser_current_url(self) -> str:
        """Read the current URL from the browser provider used by MCP tool calls."""
        if self._mcp_hub is None:
            return ""
        try:
            result = await self._mcp_hub.call_tool(
                "browser_status",
                {},
                channel=self._channel,
                allowed_tools=None,
                user_context=self._payment_browser_resume_user_context(),
            )
        except Exception:
            logger.debug("Payment confirmation could not read MCP browser status", exc_info=True)
            return ""
        return self._mcp_browser_result_url(result)

    async def _mcp_browser_page_matches_resume_url(self, merchant_url: str) -> bool:
        """Return True when the same MCP browser provider still has checkout open."""
        current_url = await self._mcp_browser_current_url()
        if self._same_merchant_resume_page(current_url, merchant_url):
            logger.info(
                "Payment confirmation found live merchant checkout in MCP browser provider: %s",
                current_url[:120],
            )
            return True
        return False

    def _browser_mcp_uses_local_playwright_manager(self) -> bool:
        """Return True only when browser MCP calls share this process' Playwright manager."""
        hub = self._mcp_hub
        if hub is None:
            return False
        try:
            routes = getattr(hub, "_tool_routes", {})
            configs = getattr(hub, "_configs", {})
            if not isinstance(routes, dict) or not isinstance(configs, dict):
                return False
            server_name = (
                routes.get("browser_navigate") or routes.get("browser_status") or routes.get("fill_payment_details")
            )
            if not server_name or server_name == "_compound":
                return False
            config = configs.get(server_name)
            transport = str(getattr(config, "transport", "") or "").strip().lower()
            module = str(getattr(config, "module", "") or "").strip()
            return transport == "inprocess" and module in {
                "mcp_servers.browser",
                "mcp_servers.browser.server",
            }
        except Exception:
            logger.debug(
                "Payment confirmation could not inspect browser MCP route",
                exc_info=True,
            )
            return False

    async def _fill_payment_details_after_confirmation(
        self,
        session: Any,
        token: str | None,
        card_label: str,
        page: Any,
    ) -> str:
        """Fill approved payment details on either a local page or the browser MCP page."""
        fill_args = {
            "card_label": card_label,
            "confirmation_token": token,
            "cvc_one_shot": getattr(session, "local_payment_cvc", None),
            "payment_session_id": self._payment_gate_session_id(),
            "ceiling_override": getattr(session, "ceiling_override_for_purchase", False),
        }
        if page is not None:
            from intent.tools.payment_fill import handle_fill_payment_details

            return await handle_fill_payment_details(fill_args, page=page)

        if self._mcp_hub is None:
            return json.dumps(
                {
                    "ok": False,
                    "code": "browser_page_unavailable",
                    "message": "No active checkout browser page was available.",
                },
                separators=(",", ":"),
                sort_keys=True,
            )

        result = await self._mcp_hub.call_tool(
            "fill_payment_details",
            {"card_label": card_label},
            channel=self._channel,
            allowed_tools=None,
            user_context=self._payment_confirmation_user_context(session, token, card_label),
            approval_already_granted=True,
        )
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict):
            return json.dumps(data, separators=(",", ":"), sort_keys=True)
        if isinstance(data, str):
            return data
        display = str(result.get("display") or "") if isinstance(result, dict) else ""
        if display:
            return display
        return json.dumps(
            {
                "ok": False,
                "code": "payment_fill_failed",
                "message": (str(result.get("error") or "Payment fill failed.") if isinstance(result, dict) else ""),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    async def _run_payment_confirmation_wait(self, ctx: dict[str, Any]) -> None:
        """Wait for hosted payment approval, then fill, submit, or re-enter."""
        from services.payments.confirmation import ConfirmationStatus

        mgr = ctx["mgr"]
        token: str | None = ctx.get("token")
        url = str(ctx.get("url") or "")
        channel = ctx.get("channel")
        if channel is not None:
            self._channel = channel
        skip_browser_cleanup = False
        reentry_count = 0
        token_wait_counts: dict[str, int] = {}

        try:
            if url and not ctx.get("link_dispatch"):
                await _dispatch_payment_confirmation_link_for_executor(self, ctx, url)

            # ---- Confirmation wait + re-entry loop ----
            while True:
                if token:
                    token_wait_counts[token] = token_wait_counts.get(token, 0) + 1
                    if token_wait_counts[token] > 2:
                        logger.warning(
                            "Confirmation re-entry circuit breaker tripped for token %s",
                            token[:8],
                        )
                        await self._speak(
                            "I had trouble completing this - I've left the checkout page open for you to finish manually."
                        )
                        mgr.mark_reentry_failed(token)
                        skip_browser_cleanup = True
                        break
                session = await mgr.wait_for_approval(token)

                if session is None:
                    logger.warning("Confirmation session lost for token %s", token[:8])
                    await self._speak("Your order confirmation timed out. Start the order again when you're ready.")
                    break

                # -- CONFIRMED: fill payment and submit order --
                if session.status == ConfirmationStatus.CONFIRMED:
                    logger.info("Order confirmed by user via confirmation page")
                    card_label = session.selected_card_label
                    page = await self._get_confirmation_browser_page(ctx, session)
                    if not card_label:
                        logger.warning(
                            "Confirmed payment session missing resume prerequisites: card_label=%s page_available=%s",
                            bool(card_label),
                            page is not None,
                        )
                        await self._speak(
                            "I could not verify the selected payment card safely, so I did not submit the order."
                        )
                        break

                    continuation_executor = getattr(session, "executor", None) or self
                    override_token = "%s:%s:approved" % (
                        continuation_executor._payment_gate_session_id(),
                        token or "",
                    )
                    payment_context = continuation_executor._payment_confirmation_user_context(
                        session,
                        token,
                        card_label,
                    )
                    continuation_executor._payment_confirmation_tool_context = payment_context.get(
                        "payment_confirmation",
                        {},
                    )
                    merchant_url = continuation_executor._merchant_resume_url_from_context(ctx, session)
                    channel_type = (
                        getattr(continuation_executor._channel, "channel_type", None)
                        if continuation_executor._channel is not None
                        else "none"
                    )
                    is_phone_confirmation = str(getattr(continuation_executor, "_session_id", "") or "").startswith(
                        "phone:"
                    )
                    if is_phone_confirmation:
                        event_text = (
                            "<CHANNEL>\n"
                            "channel_type: %s\n"
                            "delivery: mixed\n"
                            "session_id: %s\n"
                            "</CHANNEL>\n\n"
                            "Payment confirmation event: the user approved this order through the secure "
                            "confirmation page. Approved saved card label: %s. Continue the live phone call "
                            "from the existing conversation and tool history. When the recipient is ready for "
                            "payment details, call transmit_payment_to_call with the approved card label. "
                            "You will not see the card digits; the runtime speaks them directly to the call. "
                            "Do not ask the user or recipient for card digits."
                        ) % (
                            channel_type,
                            continuation_executor._session_id or "",
                            card_label,
                        )
                    else:
                        event_text = (
                            "<CHANNEL>\n"
                            "channel_type: %s\n"
                            "delivery: mixed\n"
                            "session_id: %s\n"
                            "</CHANNEL>\n\n"
                            "Payment confirmation event: the user approved this order through the secure "
                            "confirmation page. Approved saved card label: %s. Merchant checkout URL: %s. "
                            "Continue from the existing conversation and tool history. Inspect the merchant "
                            "page, securely fill the approved payment details when the payment fields are visible, "
                            "then submit the final order only if the page still matches the approved order."
                        ) % (
                            channel_type,
                            continuation_executor._session_id or "",
                            card_label,
                            merchant_url or "unknown",
                        )
                        mgr.mark_consumed(token)
                    await self._speak(
                        "Payment approved. Continuing securely."
                        if is_phone_confirmation
                        else "Order confirmed! Placing your order now."
                    )
                    try:
                        submit_result = await continuation_executor.continue_after_event(
                            event_text,
                            override_token=override_token,
                            synthetic_tool_call_id=getattr(continuation_executor, "_last_tool_call_id", None),
                        )
                        if submit_result.payment_gate:
                            skip_browser_cleanup = True
                        from services.payments.purchase_ceiling import (
                            order_total_cents,
                            record_session_purchase,
                        )

                        if not is_phone_confirmation and submit_result.ok and not submit_result.payment_gate:
                            record_session_purchase(
                                continuation_executor._get_effective_user_id(),
                                continuation_executor._payment_gate_session_id(),
                                order_total_cents(session.order_summary),
                            )
                        submit_answer = str(getattr(submit_result, "answer", "") or "").strip()
                        await self._speak(submit_answer or "Your order has been submitted.")
                    except Exception:
                        logger.exception("Unified continuation after confirmation failed")
                        await self._speak(
                            "I filled your payment details but couldn't click "
                            "the submit button. The checkout page is still open."
                        )
                        skip_browser_cleanup = True
                    finally:
                        continuation_executor._payment_confirmation_tool_context = None
                        if (
                            getattr(
                                continuation_executor,
                                "_payment_gate_override_token",
                                None,
                            )
                            == override_token
                        ):
                            continuation_executor._payment_gate_override_token = None
                    break

                # -- REJECTED: attempt cart re-entry --
                elif session.status == ConfirmationStatus.REJECTED:
                    feedback = session.feedback_message or "something about the order"
                    logger.info(
                        "Order rejected by user (attempt %d): %s",
                        reentry_count + 1,
                        feedback,
                    )

                    reentry_count += 1
                    await self._speak("Updating your order â€” one sec.")
                    mgr.mark_reentry_processing(token)

                    continuation_executor = getattr(session, "executor", None) or self
                    merchant_url = continuation_executor._merchant_resume_url_from_context(ctx, session)
                    channel_type = (
                        getattr(continuation_executor._channel, "channel_type", None)
                        if continuation_executor._channel is not None
                        else "none"
                    )
                    event_text = (
                        "<CHANNEL>\n"
                        "channel_type: %s\n"
                        "delivery: mixed\n"
                        "session_id: %s\n"
                        "</CHANNEL>\n\n"
                        'Payment confirmation event: the user rejected the order and asked for changes: "%s". '
                        "Merchant checkout URL: %s. Existing conversation and tool history remain available."
                    ) % (
                        channel_type,
                        continuation_executor._session_id or "",
                        feedback,
                        merchant_url or "unknown",
                    )
                    try:
                        reentry_result = await continuation_executor.continue_after_event(
                            event_text,
                            synthetic_tool_call_id=getattr(continuation_executor, "_last_tool_call_id", None),
                        )
                    except Exception:
                        logger.exception("Unified continuation after rejection failed")
                        reentry_result = AgentResult(
                            ok=False,
                            answer="I couldn't make that change.",
                            iterations_used=0,
                            tools_called=[],
                            error="Unified continuation after rejection failed",
                        )

                    if reentry_result.payment_gate:
                        skip_browser_cleanup = True
                        break
                    if not reentry_result.ok:
                        logger.info("Unified re-entry continuation failed, falling back to CDP stream")
                        await self._speak(
                            "I couldn't make that change â€” I've opened the checkout page for you. You can fix it directly."
                        )
                        mgr.mark_reentry_failed(token)
                        skip_browser_cleanup = True
                        break
                    await self._speak(str(reentry_result.answer or "I updated the order."))
                    break

                # -- SUPERSEDED / EXPIRED / other: exit cleanly --
                elif session.status == ConfirmationStatus.SUPERSEDED:
                    logger.info("Confirmation session superseded by a newer session")
                    break

                elif session.status == ConfirmationStatus.EXPIRED:
                    logger.info("Confirmation session expired (user didn't respond)")
                    await self._speak("Your order confirmation timed out. Start the order again when you're ready.")
                    break

                else:
                    logger.warning("Unexpected confirmation status: %s", session.status)
                    break

        except Exception:
            logger.exception("Failed to start payment confirmation flow")
            # Fall back to CDP stream
            try:
                await self._start_payment_stream()
                return
            except Exception:
                logger.exception("CDP stream fallback also failed")
                await self._speak(
                    "Your order is ready for payment. " "Please check the browser window to complete checkout."
                )
                return
        finally:
            # Always clean up regardless of how we got here (including
            # CancelledError which bypasses the except Exception block).
            try:
                if token and mgr:
                    await mgr.destroy_session(token)
            except Exception:
                logger.debug("Session cleanup in finally failed")
            if not skip_browser_cleanup:
                try:
                    await self._cleanup_browser()
                except Exception:
                    logger.debug("Browser cleanup in finally failed")
            try:
                await self._release_payment_cdp_bridge()
            except Exception:
                logger.debug("Payment CDP bridge cleanup failed")
            self._payment_gate_active = False
            self._hide_overlay(outcome="done")

    async def _extract_dom_total(self, page: Any, order_summary: dict[str, Any]) -> None:
        """Attempt to extract the order total from the live checkout page DOM.

        Safety core (payments, #4221): total SELECTION goes through the strict,
        tested extractor in ``services.payments.purchase_ceiling`` -- the old
        CSS-selector scrape here matched ``[class*="total"]`` shapes like a
        ``subtotal-price`` element, letting a wrong low line stand in for the
        order total. The strict extractor only accepts a genuine total-labelled,
        currency-marked amount (never a subtotal/savings/rewards line) and
        returns None (UNKNOWN -> the ceiling fails closed) otherwise.
        """
        if page is None:
            return
        try:
            from services.payments.purchase_ceiling import (
                _format_cents_as_text,
                extract_dom_total_cents,
            )

            dom_total_cents = await extract_dom_total_cents(page)
            if dom_total_cents is not None:
                dom_total = _format_cents_as_text(dom_total_cents)
                order_summary["dom_total"] = dom_total
                order_summary["dom_total_cents"] = dom_total_cents
                order_summary["total_source"] = "dom"
                logger.info("Extracted DOM total: %s", dom_total)
            elif not order_summary.get("dom_total"):
                order_summary["total_source"] = "agent"
        except Exception:
            logger.debug("DOM price extraction failed, using agent total")
            if not order_summary.get("dom_total"):
                order_summary["total_source"] = "agent"

    @staticmethod
    def _page_is_open(page: Any) -> bool:
        if page is None:
            return False
        is_closed = getattr(page, "is_closed", None)
        try:
            return not bool(is_closed()) if callable(is_closed) else True
        except Exception:
            return False

    @staticmethod
    def _page_url(page: Any) -> str:
        try:
            url = getattr(page, "url", "")
            return str(url or "")
        except Exception:
            return ""

    @staticmethod
    def _usable_merchant_resume_url(url: str | None) -> str:
        """Return a checkout-shaped merchant URL safe to use for payment resume."""
        candidate = str(url or "").strip()
        lower = candidate.lower()
        if not lower.startswith(("http://", "https://")):
            return ""
        if not _url_is_checkout(candidate):
            return ""
        if "/confirm/" in lower and ("127.0.0.1" in lower or "localhost" in lower):
            return ""
        return candidate

    @staticmethod
    def _usable_explicit_payment_review_url(url: str | None) -> str:
        """Return a model-tool payment review URL safe to pin to a payment gate."""

        candidate = str(url or "").strip()
        if not _url_is_explicit_payment_review_page(candidate):
            return ""
        return candidate

    @staticmethod
    def _usable_payment_gate_resume_url(url: str | None) -> str:
        """Return a merchant URL usable after an explicit payment gate."""

        return AgentExecutor._usable_merchant_resume_url(url) or AgentExecutor._usable_explicit_payment_review_url(url)

    def _merchant_resume_url_from_context(self, ctx: dict[str, Any], session: Any | None = None) -> str:
        """Find the local-only merchant checkout URL pinned to a payment gate."""
        session_metadata = getattr(session, "metadata", {})
        if not isinstance(session_metadata, dict):
            session_metadata = {}
        for candidate in (
            ctx.get("merchant_url"),
            ctx.get("page_url"),
            session_metadata.get("merchant_url"),
        ):
            usable = self._usable_payment_gate_resume_url(str(candidate or ""))
            if usable:
                return usable
        return ""

    @staticmethod
    def _same_merchant_resume_page(current_url: str | None, merchant_url: str | None) -> bool:
        """Accept the same merchant checkout page even if query or route suffix changed."""
        current = str(current_url or "").strip()
        target = str(merchant_url or "").strip()
        if not current or not target:
            return False
        if current == target:
            return True
        try:
            from urllib.parse import urlparse

            current_parsed = urlparse(current)
            target_parsed = urlparse(target)
        except Exception:
            return False
        if current_parsed.scheme not in {
            "http",
            "https",
        } or target_parsed.scheme not in {"http", "https"}:
            return False
        current_host = str(current_parsed.hostname or "").lower().removeprefix("www.")
        target_host = str(target_parsed.hostname or "").lower().removeprefix("www.")
        if not current_host or not target_host:
            return False
        if current_host != target_host and not (
            current_host.endswith("." + target_host) or target_host.endswith("." + current_host)
        ):
            return False
        current_path = (current_parsed.path or "/").rstrip("/") or "/"
        target_path = (target_parsed.path or "/").rstrip("/") or "/"
        if current_path == target_path:
            return True
        if current_path == "/" or target_path == "/":
            return False
        return current_path.startswith(target_path + "/") or target_path.startswith(current_path + "/")

    def _get_browser_page_by_ref(self, page_ref: str | None) -> Any:
        """Resolve a live browser page by the in-memory confirmation page id."""
        wanted = str(page_ref or "").strip()
        if not wanted:
            return None
        if not self._browser_mcp_uses_local_playwright_manager():
            return None
        try:
            from mcp_servers.browser.server import manager as mcp_mgr

            candidates: list[Any] = []
            page = getattr(mcp_mgr, "_page", None)
            if page is not None:
                candidates.append(page)
            user_pages = getattr(mcp_mgr, "_user_pages", None)
            if isinstance(user_pages, dict):
                candidates.extend(user_pages.values())
            seen: set[int] = set()
            for candidate in candidates:
                candidate_id = id(candidate)
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                if str(candidate_id) == wanted and self._page_is_open(candidate):
                    return candidate
        except Exception:
            logger.debug("Confirmation page-ref lookup failed")
        return None

    def _select_cdp_checkout_page(self, pages: list[Any], preferred_url: str) -> Any:
        """Choose the checkout page from a CDP browser connection."""
        preferred = str(preferred_url or "").strip()
        best_page = None
        best_score = -1
        for page in pages:
            if not self._page_is_open(page):
                continue
            page_url = self._page_url(page)
            lower_url = page_url.lower()
            if not page_url or "about:blank" in lower_url:
                continue
            if "/confirm/" in lower_url and ("127.0.0.1" in lower_url or "localhost" in lower_url):
                continue
            score = 0
            if preferred and self._same_merchant_resume_page(page_url, preferred):
                score += 100
            if _url_is_checkout(page_url):
                score += 25
            elif _url_is_explicit_payment_review_page(page_url):
                score += 20
            if page_url.startswith(("http://", "https://")):
                score += 5
            if score > best_score:
                best_page = page
                best_score = score
        return best_page

    async def _release_payment_cdp_bridge(self) -> None:
        """Release the local CDP connection without closing the owned browser."""
        bridge = getattr(self, "_payment_cdp_bridge", None)
        self._payment_cdp_bridge = None
        if not isinstance(bridge, dict):
            return
        playwright = bridge.get("playwright")
        stop = getattr(playwright, "stop", None)
        if callable(stop):
            await stop()

    async def _get_active_browser_page_from_cdp(self, preferred_url: str = "") -> Any:
        """Attach to the existing CDP-launched checkout browser and return its live page."""
        bridge = getattr(self, "_payment_cdp_bridge", None)
        if isinstance(bridge, dict):
            page = bridge.get("page")
            if self._page_is_open(page) and (
                not preferred_url or self._same_merchant_resume_page(self._page_url(page), preferred_url)
            ):
                return page

        try:
            from config.settings import settings
        except Exception:
            return None

        candidate_ports: list[int] = []
        try:
            # Desktop agentic browsing always uses the embedded CDP browser.
            port = int(getattr(settings, "cdp_port", 0) or 0)
            if port > 0:
                candidate_ports.append(port)
        except Exception:
            return None

        if not candidate_ports:
            return None

        try:
            from playwright.async_api import async_playwright
        except Exception:
            logger.debug("Playwright unavailable for payment CDP page bridge")
            return None

        for port in candidate_ports:
            playwright = None
            try:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:%d" % port, timeout=5000)
                pages: list[Any] = []
                for context in getattr(browser, "contexts", []) or []:
                    pages.extend(list(getattr(context, "pages", []) or []))
                page = self._select_cdp_checkout_page(pages, preferred_url)
                if page is None:
                    await playwright.stop()
                    continue
                self._payment_cdp_bridge = {
                    "playwright": playwright,
                    "browser": browser,
                    "page": page,
                    "port": port,
                }
                logger.info(
                    "Payment confirmation pinned checkout page via CDP bridge (port=%d url=%s)",
                    port,
                    self._page_url(page)[:120],
                )
                return page
            except Exception:
                if playwright is not None:
                    try:
                        await playwright.stop()
                    except Exception:
                        logger.debug("Payment CDP bridge partial cleanup failed")
                logger.debug("Payment CDP page bridge could not attach on port %d", port)
        return None

    async def _navigate_confirmation_browser_page(self, merchant_url: str) -> Any:
        """Navigate the browser MCP page back to the pinned merchant checkout URL."""
        merchant_url = self._usable_payment_gate_resume_url(merchant_url)
        if not merchant_url:
            return None
        if not self._browser_mcp_uses_local_playwright_manager():
            logger.info("Payment confirmation skipping local merchant checkout restore; browser provider is not local")
            return None
        try:
            from mcp_servers.browser.server import manager as mcp_mgr

            user_id = self._get_effective_user_id()
            await mcp_mgr.navigate(merchant_url, user_id)
            page = await mcp_mgr.get_page(user_id)
            if self._page_is_open(page) and self._same_merchant_resume_page(self._page_url(page), merchant_url):
                logger.info(
                    "Payment confirmation restored merchant checkout page for resume: %s",
                    self._page_url(page)[:120],
                )
                return page
        except Exception:
            logger.exception("Payment confirmation could not restore merchant checkout page")
        return None

    async def _navigate_confirmation_browser_provider(self, merchant_url: str) -> bool:
        """Navigate the same MCP browser provider that owns the agent's browser state."""
        merchant_url = self._usable_payment_gate_resume_url(merchant_url)
        if not merchant_url or self._mcp_hub is None:
            return False
        try:
            result = await self._mcp_hub.call_tool(
                "browser_navigate",
                {"url": merchant_url},
                channel=self._channel,
                allowed_tools=None,
                user_context=self._payment_browser_resume_user_context(),
            )
        except Exception:
            logger.exception("Payment confirmation could not restore merchant checkout through MCP browser")
            return False
        current_url = self._mcp_browser_result_url(result)
        if self._same_merchant_resume_page(current_url, merchant_url):
            logger.info(
                "Payment confirmation restored merchant checkout through MCP browser provider: %s",
                current_url[:120],
            )
            return True
        logger.warning(
            "Payment confirmation MCP browser restore did not reach checkout page: target=%s current=%s",
            merchant_url[:120],
            current_url[:120],
        )
        return False

    async def _get_confirmation_browser_page(self, ctx: dict[str, Any], session: Any | None = None) -> Any:
        """Return the live checkout page tied to a confirmation session."""
        merchant_url = self._merchant_resume_url_from_context(ctx, session)
        for page in (getattr(session, "page", None), ctx.get("page")):
            if self._page_is_open(page) and (
                not merchant_url or self._same_merchant_resume_page(self._page_url(page), merchant_url)
            ):
                ctx["page"] = page
                page_url = self._page_url(page)
                if page_url:
                    ctx["page_url"] = page_url
                if merchant_url:
                    ctx["merchant_url"] = merchant_url
                return page
        for page_ref in (ctx.get("page_ref"), getattr(session, "page_ref", None)):
            page = self._get_browser_page_by_ref(str(page_ref or ""))
            if page is not None and (
                not merchant_url or self._same_merchant_resume_page(self._page_url(page), merchant_url)
            ):
                return page
        page = self._get_active_browser_page() if self._browser_mcp_uses_local_playwright_manager() else None
        if self._page_is_open(page) and (
            not merchant_url or self._same_merchant_resume_page(self._page_url(page), merchant_url)
        ):
            return page
        preferred_url = merchant_url or str(ctx.get("page_url") or self._last_page_url or "")
        page = await self._get_active_browser_page_from_cdp(preferred_url)
        if self._page_is_open(page) and (
            not merchant_url or self._same_merchant_resume_page(self._page_url(page), merchant_url)
        ):
            return page
        if merchant_url:
            if await self._mcp_browser_page_matches_resume_url(merchant_url):
                ctx["page"] = None
                ctx["page_url"] = merchant_url
                ctx["merchant_url"] = merchant_url
                ctx["mcp_browser_page_alive"] = True
                return None
            page = await self._navigate_confirmation_browser_page(merchant_url)
            if self._page_is_open(page):
                ctx["page"] = page
                ctx["page_url"] = self._page_url(page) or merchant_url
                ctx["merchant_url"] = merchant_url
                if session is not None:
                    try:
                        session.page = page
                    except Exception:
                        logger.debug("Browser session page assignment failed")
                return page
            provider_restored = await self._navigate_confirmation_browser_provider(merchant_url)
            if provider_restored:
                ctx["page"] = None
                ctx["page_url"] = merchant_url
                ctx["merchant_url"] = merchant_url
                ctx["mcp_browser_page_alive"] = True
                return None
        return None

    async def _check_page_alive(self, page: Any) -> bool:
        """Check if the browser page is still responsive with a valid cart.

        Returns True if the page responds to CDP and no "cart expired"
        signals are detected. This is a fast heuristic pre-check â€” the
        re-entry agent itself will also detect if the cart is gone.
        """
        try:
            result = await asyncio.wait_for(
                page.evaluate("""
                    () => {
                        const body = document.body?.innerText?.toLowerCase() || '';
                        const expired_signals = [
                            'session expired', 'session timed out', 'cart is empty',
                            'your cart is empty', 'start a new order', 'order expired',
                            'timed out', 'no items in your cart'
                        ];
                        for (const signal of expired_signals) {
                            if (body.includes(signal)) return { alive: true, cart_valid: false };
                        }
                        return { alive: true, cart_valid: true };
                    }
                """),
                timeout=5.0,
            )
            return bool(result and result.get("cart_valid", False))
        except Exception:
            return False

    def _get_active_browser_page(self) -> Any:
        """Return the active Playwright page from whichever browser manager has one."""
        if not self._browser_mcp_uses_local_playwright_manager():
            return None
        effective_user_id = self._get_effective_user_id()
        # MCP browser manager (primary path for agent tool calls)
        try:
            from mcp_servers.browser.server import manager as mcp_mgr

            if (
                mcp_mgr._active_user_id == effective_user_id
                and mcp_mgr._page is not None
                and not mcp_mgr._page.is_closed()
            ):
                return mcp_mgr._page
        except Exception:
            logger.debug("Could not read browser manager page")
        return None

    # ------------------------------------------------------------------
    # Diagnostic context capture (for self-diagnosis engine)
    # ------------------------------------------------------------------

    def _capture_diagnostic(
        self,
        user_text: str,
        exception: BaseException,
        stage: str,
        iteration: int = 0,
        tool_name: str | None = None,
        tool_result_text: str | None = None,
    ) -> None:
        """Capture a DiagnosticContext from an exception (best-effort, never raises)."""
        try:
            from services.agent.diagnostic_context import DiagnosticContext

            ctx = DiagnosticContext.capture_from_exception(
                user_request=user_text,
                exception=exception,
                stage=stage,
                agent_iteration=iteration,
                tool_call_attempted=tool_name,
                tool_call_result=tool_result_text,
                user_id=self._get_effective_user_id(),
            )
            self._diagnostic_contexts.append(ctx)
            # FIFO cap: evict oldest entry when limit exceeded
            if len(self._diagnostic_contexts) > _MAX_DIAGNOSTIC_CONTEXTS:
                self._diagnostic_contexts.pop(0)
        except Exception:
            logger.debug("Failed to capture diagnostic context", exc_info=True)

    def _capture_diagnostic_from_response(
        self,
        user_text: str,
        response: dict[str, Any] | str,
        detail: str = "",
    ) -> None:
        """Capture a DiagnosticContext from a problematic LLM response (best-effort)."""
        try:
            from services.agent.diagnostic_context import DiagnosticContext

            ctx = DiagnosticContext.capture_from_response(
                user_request=user_text,
                llm_response=response,
                detail=detail,
                user_id=self._get_effective_user_id(),
            )
            self._diagnostic_contexts.append(ctx)
            # FIFO cap: evict oldest entry when limit exceeded
            if len(self._diagnostic_contexts) > _MAX_DIAGNOSTIC_CONTEXTS:
                self._diagnostic_contexts.pop(0)
        except Exception:
            logger.debug("Failed to capture diagnostic context from response", exc_info=True)

    def cancel(self) -> None:
        """Request graceful cancellation of the agent loop.

        The loop checks ``_cancelled`` between tool calls and exits
        cleanly on the next iteration.  The ``_cancel_event`` is also
        set so that in-progress tool calls can be aborted mid-execution
        via ``asyncio.wait()`` in ``_execute_tool()``.
        """
        self._cancelled = True
        self._cancel_event.set()
        self._takeover_release_event.set()
        logger.info("Agent cancellation requested")

    def request_user_takeover(self) -> None:
        """Pause agent progress so the user can interact with the browser."""
        if self._cancelled:
            return
        self._takeover_active = True
        self._takeover_interrupt_event.set()
        self._takeover_release_event.clear()
        self._update_overlay_status("user_takeover")
        self._update_overlay_phase("user_input")
        logger.info("Agent browser takeover requested")

    def continue_from_user_takeover(self) -> None:
        """Resume agent progress after a user browser takeover."""
        self._takeover_active = False
        self._takeover_interrupt_event.clear()
        self._takeover_release_event.set()
        self._update_overlay_status("working")
        self._update_overlay_phase("acting")
        logger.info("Agent browser takeover released")

    async def _wait_for_takeover_release(self) -> bool:
        """Wait while the user has browser takeover active.

        Returns ``False`` if cancellation happens while waiting.
        """
        if not self._takeover_active:
            return True
        logger.info("Agent paused for browser takeover")
        while self._takeover_active and not self._cancelled:
            await asyncio.to_thread(self._takeover_release_event.wait, 0.25)
        return not self._cancelled


# ---------------------------------------------------------------------------
# Playbook / API registration helpers (module-level, non-blocking)
# ---------------------------------------------------------------------------


def _is_useful_api(entry: dict) -> bool:
    """Determine if a captured API call is potentially reusable."""
    url = entry.get("url", "").lower()
    method = entry.get("method", "").upper()

    # Skip tracking/analytics
    _SKIP = ("analytics", "tracking", "telemetry", "pixel", "beacon")
    if any(s in url for s in _SKIP):
        return False

    # Skip auth endpoints
    _AUTH = ("login", "auth", "token", "oauth", "signin", "session")
    if any(s in url for s in _AUTH):
        return False

    # Useful if: mutating request OR structured API path
    if method in ("POST", "PUT", "PATCH"):
        return True

    _API_PATTERNS = ("/api/", "/v1/", "/v2/", "/v3/", "/graphql")
    if any(p in url for p in _API_PATTERNS):
        return True

    return False


def _save_to_api_registry(entries: list[dict], task_description: str) -> None:
    """Save discovered API endpoints to the registry."""
    import json as _json
    from urllib.parse import urlparse

    from core.platform import get_data_dir

    registry_dir = get_data_dir() / "knowledge" / "api_registry"
    registry_dir.mkdir(parents=True, exist_ok=True)

    # Group by domain
    by_domain: dict[str, list] = {}
    for entry in entries:
        parsed = urlparse(entry["url"])
        domain = parsed.netloc
        if domain not in by_domain:
            by_domain[domain] = []
        by_domain[domain].append(
            {
                "url": entry["url"],
                "method": entry["method"],
                "request_example": str(entry.get("request_body", ""))[:500],
                "response_example": str(entry.get("response_body", ""))[:1000],
                "discovered_during": task_description,
                "discovered_at": entry.get("timestamp", ""),
            }
        )

    # Save per-domain registry files
    for domain, endpoints in by_domain.items():
        safe_name = domain.replace(".", "_").replace(":", "_")
        path = registry_dir / ("%s.json" % safe_name)

        # Merge with existing if present
        existing: dict[str, Any] = {"domain": domain, "endpoints": []}
        if path.exists():
            try:
                with open(path) as f:
                    existing = _json.load(f)
            except Exception:
                logger.debug("Failed to load existing API registry, starting fresh")

        existing["endpoints"].extend(endpoints)

        # Cap at 50 endpoints per domain
        existing["endpoints"] = existing["endpoints"][-50:]

        with open(path, "w") as f:
            _json.dump(existing, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Module-level active executor registry (for cancellation from WS handler)
# ---------------------------------------------------------------------------

_executor_stacks: dict[str, list[AgentExecutor]] = {}
_last_active_executor_key: str | None = None


def get_active_executor(
    session_id: str | None = None,
    *,
    user_id: str | None = None,
    task_id: str | None = None,
) -> AgentExecutor | None:
    """Return the active AgentExecutor scoped by ``user_id`` + ``session_id``.

    Multi-tenant rule: callers MUST identify the owner of the executor they
    want to act on (cancel, takeover, continue, channel reuse).  At least
    one of ``user_id``, ``session_id``, or ``task_id`` is required.  A
    bare lookup with no identifier returns ``None`` rather than silently
    handing back the last-active executor — that fallback allowed one
    websocket to cancel/control another user's agent.

    Lookup precedence: ``task:<id>`` → ``session:<id>`` → ``user:<id>``.
    When both ``user_id`` and ``session_id`` are supplied, the resolved
    executor must own both keys; otherwise ``None`` is returned.
    """
    candidates: list[AgentExecutor] = []
    seen: set[int] = set()

    def _push(stack: list[AgentExecutor] | None) -> None:
        if not stack:
            return
        executor = stack[-1]
        marker = id(executor)
        if marker in seen:
            return
        seen.add(marker)
        candidates.append(executor)

    if task_id:
        _push(_executor_stacks.get("task:%s" % task_id))
    if session_id:
        _push(_executor_stacks.get("session:%s" % session_id))
        _push(_executor_stacks.get(session_id))
    if user_id:
        _push(_executor_stacks.get("user:%s" % user_id))

    if not candidates:
        return None

    for executor in candidates:
        if user_id and getattr(executor, "_user_id", None) and executor._user_id != user_id:
            continue
        if session_id and getattr(executor, "_session_id", None) and executor._session_id != session_id:
            continue
        return executor
    return None


def _set_active_executor(
    executor: AgentExecutor,
    keys: list[str] | None = None,
    remove: bool = False,
) -> None:
    global _last_active_executor_key

    active_keys = [key for key in (keys or []) if key]
    if remove:
        for key in active_keys:
            stack = _executor_stacks.get(key)
            if not stack:
                continue
            _executor_stacks[key] = [entry for entry in stack if entry is not executor]
            if not _executor_stacks[key]:
                _executor_stacks.pop(key, None)
                if _last_active_executor_key == key:
                    _last_active_executor_key = None
        if _last_active_executor_key is None:
            for key in reversed(list(_executor_stacks.keys())):
                if _executor_stacks.get(key):
                    _last_active_executor_key = key
                    break
        return

    for key in active_keys:
        stack = _executor_stacks.setdefault(key, [])
        if not stack or stack[-1] is not executor:
            stack.append(executor)
        _last_active_executor_key = key
