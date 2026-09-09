"""
AI Controller for handling AI-enhanced intent processing.

This module provides the AIController class which combines deterministic
rule-based intent parsing with optional GPT/LLM fallback for more complex
requests.

Includes canonical conversation frame-chain access, playback context injection
so the LLM knows what is currently playing, and the native tool-use agent loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import re
import time
from typing import Any, TypedDict
from urllib.parse import urlsplit

from core.logging_config import get_logger
from diagnostics.bus import get_diagnostics_bus

# Q-002: _strip_json_template and helpers moved to intent.response_cleanup
from intent.response_cleanup import strip_json_template as _strip_json_template
from intent.tools.deferred_tool_schemas import format_deferred_tools_block
from services.conversation import (
    ConversationStateManager,
    get_conversation_manager,
    get_request_conversation_manager,
)
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    SystemReminderBlock,
    TextBlock,
)
from services.conversation.frame_rendering import render_for_openai_responses
from services.llm.prompts import (
    append_system_text,
    build_provider_prompt_bundle,
    runtime_context_bundle,
)
from services.memory.action_recipes import (
    get_action_recipe_store,
    infer_action_intent,
)
from utils.text_encoding import repair_mojibake

logger = get_logger(__name__)
_diagnostics = get_diagnostics_bus()

# Per-request state isolated via contextvars to prevent race conditions
# when concurrent async requests share a single AIController instance.
_ctx_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_user_id", default=None)
_ctx_ask_tier_active: contextvars.ContextVar[bool] = contextvars.ContextVar("ask_tier_active", default=False)
_ctx_agent_model_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_model_override", default=None
)
_ctx_last_task_category: contextvars.ContextVar[str | None] = contextvars.ContextVar("last_task_category", default=None)
_ctx_system_context_components: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "system_context_components", default=None
)
_ctx_agent_native_tools: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "agent_native_tools", default=None
)
_ctx_agent_tool_surface: contextvars.ContextVar[Any | None] = contextvars.ContextVar("agent_tool_surface", default=None)
_ctx_agent_deferred_tool_pool: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "agent_deferred_tool_pool", default=None
)
_ctx_agent_prompt_instructions_preview: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_prompt_instructions_preview", default=None
)
_ctx_agent_prompt_bundle: contextvars.ContextVar[PromptFrameBundle | None] = contextvars.ContextVar(
    "agent_prompt_bundle", default=None
)


def _tool_schema_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name", "")).strip()


def _build_tool_schema_text_from_visible_tools(hub: Any, tools: list[dict[str, Any]]) -> str:
    """Render legacy text schemas from an already-filtered tool list."""
    if not tools:
        return ""

    infer_category = getattr(hub, "_infer_category", None)
    tool_to_schema_text = getattr(hub, "_tool_to_schema_text", None)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        name = _tool_schema_name(tool)
        if callable(infer_category):
            category = str(infer_category(name))
        else:
            category = "general"
        by_category.setdefault(category, []).append(tool)

    sections: list[str] = []
    for category in sorted(by_category):
        section_lines = ["### %s tools" % category.title()]
        for tool in sorted(by_category[category], key=lambda item: item.get("name", "")):
            if callable(tool_to_schema_text):
                section_lines.append(str(tool_to_schema_text(tool)))
            else:
                section_lines.append("- %s: %s" % (_tool_schema_name(tool), tool.get("description", "")))
        sections.append("\n".join(section_lines))
    return "\n\n".join(sections)


_PENDING_TOOL_CONFIRMATION_EXPIRY_SECONDS = 120.0
_MCP_HUB_INIT_RETRY_BACKOFF_SECONDS = 30.0
_INTERNAL_HISTORY_TOOL_TYPES = frozenset({"tool_call", "tool_use", "function_call"})
_INTERNAL_HISTORY_GAUNTLET_RE = re.compile(r"\bCG-[A-Z0-9][A-Z0-9_-]*-\d{9,}\b")
_INTERNAL_HISTORY_TOOL_JSON_RE = re.compile(
    r'\{\s*"type"\s*:\s*"(?:tool_call|tool_use|function_call)"',
    re.IGNORECASE,
)

_SIGNATURE_RESUME_CONSUMING_TOOLS = frozenset(
    {
        "browser_click",
        "browser_click_ref",
        "browser_fill_form",
        "browser_fill_ref",
        "browser_interact",
        "browser_press_key",
        "browser_run_script",
        "browser_evaluate",
        "browser_select",
        "browser_select_ref",
        "browser_type",
    }
)
_PAYMENT_RESUME_CONSUMING_TOOLS = frozenset(
    {
        "browser_click",
        "browser_click_ref",
        "browser_fill_form",
        "browser_fill_ref",
        "browser_interact",
        "browser_press_key",
        "browser_run_script",
        "browser_evaluate",
        "browser_select",
        "browser_select_ref",
        "browser_snapshot",
        "browser_type",
        "fill_payment_details",
        "payment_pay",
    }
)
_ACTIVE_RESUME_CONSUMED_TOOLS = {
    "signature": frozenset({"resume_signature_gate", "cancel_signature_gate"}),
    "payment": frozenset({"resume_payment_gate", "cancel_payment_gate"}),
}


def _resume_gate_type_from_checkpoint_context(context: dict[str, Any] | None) -> str:
    """Return the gate type whose resume/cancel tools were already consumed."""
    if not isinstance(context, dict):
        return ""
    explicit = str(context.get("pending_gate_type") or context.get("_pre_resume_gate_type") or "").strip().lower()
    if explicit in {"signature", "payment"}:
        return explicit
    payment_markers = {
        "confirm_token",
        "confirmation_url",
        "payment_order_summary",
        "payment_page_ref",
        "payment_page_url",
        "payment_merchant_url",
    }
    if any(context.get(key) not in (None, "", [], {}) for key in payment_markers):
        return "payment"
    signature_markers = {
        "signature_gate_page_url",
        "signature_token",
    }
    if any(context.get(key) not in (None, "", [], {}) for key in signature_markers):
        return "signature"
    gate_page = str(context.get("gate_page_url") or "").strip().lower()
    if gate_page and "signature" in gate_page:
        return "signature"
    return ""


def _runtime_tool_name(tool: dict[str, Any]) -> str:
    name = str(tool.get("name") or "").strip()
    if name:
        return name
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return ""


def _without_tool_names(
    tools: list[dict[str, Any]] | None, blocked_names: frozenset[str]
) -> list[dict[str, Any]] | None:
    if tools is None:
        return None
    return [
        json.loads(json.dumps(tool, default=str)) for tool in tools if _runtime_tool_name(tool) not in blocked_names
    ]


def _active_resume_hidden_reasons(
    tool_surface: Any | None,
    blocked_names: frozenset[str],
    reason: str,
) -> dict[str, list[str]]:
    hidden: dict[str, list[str]] = {}
    if tool_surface is not None:
        raw = getattr(tool_surface, "hidden_reasons", None)
        if raw is None and isinstance(tool_surface, dict):
            raw = tool_surface.get("hidden_reasons")
        if isinstance(raw, dict):
            for name, reasons in raw.items():
                if isinstance(reasons, list):
                    hidden[str(name)] = [str(item) for item in reasons if str(item).strip()]
    for name in blocked_names:
        values = hidden.setdefault(name, [])
        if reason not in values:
            values.append(reason)
    return hidden


def _filter_active_resume_surface(
    native_tools: list[dict[str, Any]] | None,
    tool_surface: Any | None,
    gate_type: str | None,
) -> tuple[list[dict[str, Any]] | None, Any | None]:
    blocked_names = _ACTIVE_RESUME_CONSUMED_TOOLS.get((gate_type or "").strip().lower())
    if not blocked_names:
        return native_tools, tool_surface

    reason = "consumed_by_active_%s_resume" % gate_type
    filtered_native = _without_tool_names(native_tools, blocked_names)
    if tool_surface is None:
        return filtered_native, tool_surface

    provider_native = _without_tool_names(getattr(tool_surface, "provider_native", None), blocked_names)
    step_log_visible = _without_tool_names(getattr(tool_surface, "step_log_visible", None), blocked_names)
    hub_visible = getattr(tool_surface, "hub_visible", None)
    collapsed_from = getattr(tool_surface, "collapsed_from", None)
    if isinstance(tool_surface, dict):
        provider_native = _without_tool_names(tool_surface.get("provider_native"), blocked_names)
        step_log_visible = _without_tool_names(tool_surface.get("step_log_visible"), blocked_names)
        hub_visible = tool_surface.get("hub_visible")
        collapsed_from = tool_surface.get("collapsed_from")

    try:
        from mcp_hub.tool_surface import ToolSurface

        filtered_surface = ToolSurface.build(
            hub_visible=hub_visible or step_log_visible or provider_native or [],
            hidden_reasons=_active_resume_hidden_reasons(tool_surface, blocked_names, reason),
            collapsed_from=collapsed_from if isinstance(collapsed_from, dict) else None,
            provider_native=provider_native,
            step_log_visible=step_log_visible,
        )
    except (ImportError, AttributeError, TypeError, ValueError):
        filtered_surface = tool_surface
        if hasattr(filtered_surface, "provider_native"):
            filtered_surface.provider_native = provider_native or []
        if hasattr(filtered_surface, "step_log_visible"):
            filtered_surface.step_log_visible = step_log_visible or []
        if hasattr(filtered_surface, "hidden_reasons"):
            filtered_surface.hidden_reasons = _active_resume_hidden_reasons(tool_surface, blocked_names, reason)
    return filtered_native, filtered_surface


def _normalize_tools_called(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    try:
        return [str(tool).strip() for tool in value if str(tool).strip()]  # type: ignore[union-attr]
    except TypeError:
        text = str(value).strip()
        return [text] if text else []


def _propagate_cap_state(result: dict[str, Any], agent_result: object) -> None:
    """Carry a managed-AI cap denial from an AgentResult into the response data.

    The agent path sets ``AgentResult.cap_state`` when the plan allowance is
    spent (``intent/agent_loop.py`` pre-loop gate, ``intent/agent_executor.py``
    run gate), but the AgentResult-to-response maps used to drop it. A capped
    turn therefore reached the client as bare cap copy with no structured state,
    and the client keys its upgrade affordance off ``cap_state`` -- so the user
    was told to upgrade and given nothing to act on (candidate C-077).

    Only a real denial is propagated: ``ManagedLlmBudgetGate.cap_state`` is an
    empty dict whenever the gate allowed the turn, and many call sites pass
    ``cap_state={}`` on success. Writing that empty dict through would strand an
    upgrade prompt on every normal answer.

    This changes no spend enforcement. It only surfaces state the gate already
    decided.
    """
    cap_state = getattr(agent_result, "cap_state", None)
    if not isinstance(cap_state, dict) or not cap_state:
        return
    data = result.get("data")
    if isinstance(data, dict):
        data["cap_state"] = cap_state


def _resume_failed_before_gate_consuming_action(agent_result: object, gate_type: str) -> bool:
    """Return true when a failed resumed run could not have consumed the gate."""
    if agent_result is None or bool(getattr(agent_result, "ok", False)):
        return False

    raw_tools = getattr(agent_result, "tools_called", None)
    if raw_tools is None:
        return int(getattr(agent_result, "iterations_used", 0) or 0) == 0

    tools_called = _normalize_tools_called(raw_tools)
    if not tools_called:
        return True

    consuming_tools = _SIGNATURE_RESUME_CONSUMING_TOOLS if gate_type == "signature" else _PAYMENT_RESUME_CONSUMING_TOOLS
    return not any(tool in consuming_tools for tool in tools_called)


# Signature-gate chat-reply regex handlers were removed 2026-04-26.
# Rationale: any regex (no matter how tight) hijacked unrelated commands when a
# stale signature checkpoint existed for the user â€” see TMR-008 regression
# ("cancel reminder" â†’ "I won't sign it") and the 2026-04-26 phone-test "yes"
# hijack into a stale browser-signature flow. The Viola LLM is fully capable of
# interpreting "yes" / "no" / "cancel" in context. Pending gate state is
# surfaced through the system prompt + UserContext + checkpoint metadata so the
# agent can decide whether to resume / cancel / treat the input as a new task.
# If the AI fails at this, fix the context surfacing; do not re-add a regex.

_GATE_CARD_DISMISS_MS = 90_000


def _is_hosted_confirm_url(value: str | None) -> bool:
    """Return True only for Viola-hosted confirmation-page URLs."""
    url = (value or "").strip()
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    path = parsed.path or url
    return "/confirm/" in path


def _explicit_setting_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value == 1


def _build_gate_card(
    *,
    gate_kind: str,
    body: str,
    confirmation_url: str | None = None,
    origin_channel: str | None = None,
) -> dict[str, Any]:
    """Structured card payload the chat UI renders next to a gate response.

    Payment approvals use Viola's hosted confirmation page. Signature gates
    stay in the chat-confirm flow until a hosted signature explainer exists.
    """
    if gate_kind == "signature":
        return {
            "type": "gate_review",
            "gate_kind": "signature",
            "title": "Legal signature required",
            "body": body,
            "cta": {
                "label": "Sign and continue",
                "action": "send_chat",
                "text": "yes sign it and continue",
            },
            "secondary_cta": {
                "label": "Don't sign",
                "action": "send_chat",
                "text": "no don't sign",
            },
            "origin_channel": origin_channel,
            "dismiss_after_ms": _GATE_CARD_DISMISS_MS,
        }

    card: dict[str, Any] = {
        "type": "gate_review",
        "gate_kind": "payment",
        "title": "Payment review required",
        "body": body,
        "secondary_cta": {
            "label": "Don't pay",
            "action": "send_chat",
            "text": "no don't pay",
        },
        "origin_channel": origin_channel,
        "dismiss_after_ms": _GATE_CARD_DISMISS_MS,
    }
    if confirmation_url:
        if _is_hosted_confirm_url(confirmation_url):
            card["subject_url"] = confirmation_url
            card["cta"] = {
                "label": "Review and pay",
                "action": "open_url",
                "url": confirmation_url,
            }
            card["tertiary_cta"] = {
                "label": "Confirm selected card",
                "action": "send_chat",
                "text": "yes confirm payment",
                "verb": "approve",
            }
    return card


def _gate_state_text_from_params(gate_state: dict[str, object]) -> str:
    name = str(gate_state.get("name") or "GATE_STATE").replace('"', "'")
    status = str(gate_state.get("status") or "awaiting_user").replace('"', "'")
    lines = ["name: %s" % name, "status: %s" % status]
    for key in (
        "gate_type",
        "task_id",
        "token",
        "confirmation_url",
        "summary",
        "last_blocker",
    ):
        value = gate_state.get(key)
        if value not in (None, "", [], {}):
            lines.append("%s: %s" % (key, value))
    for key in ("required_fields", "collected_fields"):
        value = gate_state.get(key)
        if isinstance(value, (list, tuple, set)):
            lines.append("%s: %s" % (key, ", ".join(str(item) for item in value)))
        elif value not in (None, "", [], {}):
            lines.append("%s: %s" % (key, value))
    flag = bool(gate_state.get("do_not_treat_prior_bail_messages_as_examples"))
    lines.append("do_not_treat_prior_bail_messages_as_examples: %s" % ("true" if flag else "false"))
    return "\n".join(lines)


def _gate_frame_from_record_params(assistant_text: str, params: dict[str, object] | None) -> Frame | None:
    if not isinstance(params, dict):
        return None
    if params.get("frame_kind") not in {FrameKind.SYSTEM_REMINDER.value, "gate_state"}:
        return None
    gate_state = params.get("gate_state")
    if not isinstance(gate_state, dict):
        return None
    try:
        role = FrameRole(str(params.get("frame_role") or FrameRole.META_USER.value))
    except ValueError:
        role = FrameRole.META_USER
    origin_raw = str(params.get("frame_origin") or "").strip()
    if not origin_raw or origin_raw in {"human", "model"}:
        origin_raw = None
    text = _gate_state_text_from_params(gate_state)
    block = (
        SystemReminderBlock(text=text, source_tag="gate-state")
        if role is FrameRole.META_USER
        else TextBlock(text=text or assistant_text)
    )
    task_id = str(gate_state.get("task_id") or "").strip() or None
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=role,
        blocks=(block,),
        is_meta=bool(params.get("frame_is_meta")) or role is FrameRole.META_USER,
        origin=origin_raw,
        task_id=task_id,
        schema_version=int(params.get("schema_version") or 1),
        extra=dict(gate_state),
    )


def _payment_link_delivery_reached_user(delivery: dict[str, Any] | None) -> bool:
    if not isinstance(delivery, dict):
        return False
    if delivery.get("sent") or delivery.get("already_sent"):
        return True
    email_default = delivery.get("email_default")
    return isinstance(email_default, dict) and bool(email_default.get("sent") or email_default.get("already_sent"))


async def _dispatch_payment_gate_card(
    *,
    card: dict[str, Any],
    confirmation_url: str | None,
    origin_channel: str | None,
    channel: Any | None,
    user_id: str,
    existing_delivery: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Actively deliver a payment confirmation card when the channel supports it."""
    if not confirmation_url:
        return existing_delivery
    if _payment_link_delivery_reached_user(existing_delivery):
        return existing_delivery
    try:
        from services.payments.confirmation_link_dispatch import (
            dispatch_confirmation_link,
        )

        delivery = await dispatch_confirmation_link(
            origin_channel=origin_channel,
            confirmation_url=confirmation_url,
            channel=channel,
            user_id=user_id,
            card=card,
        )
        return delivery or existing_delivery
    except Exception:
        logger.exception("Payment confirmation card dispatch failed")
        return existing_delivery


# _ACTIONABLE_AGENT_CATEGORIES removed (2026-04-09).
# tool_choice is always "auto" now â€” the model decides whether to use tools.


# Type-only response contracts kept for local annotations and result-shape documentation.
class CommandExecutedDict(TypedDict, total=False):
    """Type definition for an executed command entry."""

    command: str
    status: str
    message: str


class CommandResultDict(TypedDict, total=False):
    """Type definition for a command result entry."""

    message: str
    data: dict[str, Any]


class ResultDataDict(TypedDict, total=False):
    """Type definition for the data field in result."""

    command_results: dict[str, CommandResultDict]
    answer: str
    response: dict[str, Any]
    error: str


class ProcessResultDict(TypedDict, total=False):
    """Type definition for process_request return value."""

    ok: bool
    commands_executed: list[CommandExecutedDict]
    data: ResultDataDict
    intent: str
    message: str
    response: str


class PendingToolConfirmationDict(TypedDict):
    """Stored tool call awaiting a user yes/no confirmation."""

    original_text: str
    response: dict[str, Any]
    created_at: float
    confidence: float
    prompt: str


# ---------------------------------------------------------------------------
# P3 fix: map LLM exceptions to user-friendly messages
# ---------------------------------------------------------------------------

_LLM_TIMEOUT_USER_MSG = "Hmm, I can't reach my AI backend right now â€” could be a network hiccup. Try again in a sec?"

_LLM_GENERIC_USER_MSG = "That one tripped me up â€” try again in a sec?"


def _llm_error_to_user_message(exc: BaseException) -> str:
    """Return a user-friendly message for an LLM-layer exception.

    Checks the exception type hierarchy and common error strings to
    distinguish timeout / network / provider-unavailable errors from
    generic failures, so the user sees actionable feedback instead of
    a vague "something went wrong" message.
    """
    exc_name = type(exc).__name__

    # 1. Explicit timeout / circuit-open from our own exception hierarchy
    if exc_name in (
        "LLMTimeoutError",
        "ServiceTimeoutError",
        "CircuitOpenError",
    ) or isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return _LLM_TIMEOUT_USER_MSG

    # 2. Provider unavailable (no key configured, consent missing, etc.)
    if exc_name == "ProviderUnavailableError":
        return "My AI service isn't available right now â€” check your settings or try again in a sec."

    # 3. Connection / network errors surfaced by httpx or the SDK
    msg_lower = str(exc).lower()
    _NETWORK_HINTS = (
        "connect",
        "timeout",
        "timed out",
        "unreachable",
        "connection refused",
        "name or service not known",
        "network",
        "eof occurred",
        "ssl",
        "reset by peer",
        "broken pipe",
    )
    if any(hint in msg_lower for hint in _NETWORK_HINTS):
        return _LLM_TIMEOUT_USER_MSG

    # 4. Authentication errors
    if exc_name in (
        "AuthenticationError",
        "PermissionDeniedError",
        "LLMAuthenticationError",
    ):
        return "Your API key appears to be invalid. Please check your settings."

    # 5. Generic fallback
    return _LLM_GENERIC_USER_MSG


class AIController:
    """
    High-level controller that orchestrates intent interpretation with optional AI fallback.

    This class wraps an IntentInterpreter and provides enhanced functionality
    including playlist handling, GPT-based command processing, conversation
    history management, playback context injection, bridging of AI-routed
    non-music commands to instant command handlers, and agentic tool-use loops.
    """

    def __init__(self, interpreter: Any) -> None:
        """
        Initialize AIController with an interpreter.

        Args:
            interpreter: An IntentInterpreter instance or compatible object
        """
        self.interpreter = interpreter
        self.music = getattr(interpreter, "music", None) or getattr(interpreter, "music_player", None)
        # OpenAI client - can be set externally for GPT-based processing
        self.client: Any = None

        self._history_timestamps: dict[str, float] = {}
        self._history_lock: asyncio.Lock = asyncio.Lock()
        self._pending_tool_confirmations: dict[str, PendingToolConfirmationDict] = {}
        # Default manager for setup code and single-user desktop. Runtime
        # requests route through the ``_conversation_state_manager``
        # property below, which prefers the per-user manager published by
        # ``IntentPipeline._process_inner`` via a contextvar. This prevents
        # the cross-tenant bleed that used to occur when one AIController
        # was shared across multiple messaging users (CHAN-R1).
        self._default_conversation_state_manager: ConversationStateManager | None = None

        # Instant command handler reference, set by pipeline after construction
        self._instant_handler: Any = None

        # Agent loop references (set by pipeline after construction)
        self._voice_pipeline: Any = None
        self._tts_speaker: Any = None
        # Channel-agnostic messaging â€” stored ONLY as a fallback for
        # single-user desktop / tests. Per-request reads resolve via the
        # ``_channel`` property below which consults the task-local
        # contextvar published by ``IntentPipeline._process_inner``. This
        # prevents the shared-AIController cross-tenant leak where
        # concurrent different-user requests racing on an instance
        # attribute caused ``self._channel.send(progress)`` to deliver
        # user A's message to user B's channel (CHAN-R7).
        self._default_channel: Any = None
        from intent.hooks.lifecycle import get_default_hook_registry

        self.hook_registry = get_default_hook_registry()
        self.hook_settings_runner: Any = None

        # MCP Client Hub (lazy-initialized on first agent use)
        self._mcp_hub: Any = None
        self._mcp_hub_init_failed_at: float | None = None
        self._browser_init_task: asyncio.Task[None] | None = None
        self._mcp_browser_config_signature: tuple[Any, ...] | None = None

        # Unified context builder â€” single source of truth for system context.
        from intent.context_builder import ContextBuilder

        self._context_builder = ContextBuilder()
        if self.music is not None:
            self._context_builder.set_music_player(self.music)

        # Background-agent registry â€” per-user lifecycle tracker for
        # async orchestrator. Singleton on the AIController so it survives
        # across /v1/command requests.
        from services.agent_runtime.registry import agent_registry as _agent_registry

        self._agent_registry = _agent_registry
        # Per-request state (_current_user_id, _ask_tier_active,
        # _agent_model_override, _last_task_category, _system_context_components)
        # is stored in module-level contextvars to prevent race conditions
        # when concurrent async requests share this AIController instance.

    def _set_request_agent_surface(
        self,
        *,
        native_tools: list[dict[str, Any]] | None,
        tool_surface: Any | None,
        prompt_bundle: PromptFrameBundle | None,
        deferred_tool_pool: Any | None = None,
        prompt_instructions_preview: str | None = None,
    ) -> None:
        """Publish this request's agent surface without relying on provider globals."""
        _ctx_agent_native_tools.set(list(native_tools) if native_tools is not None else None)
        _ctx_agent_tool_surface.set(tool_surface)
        _ctx_agent_deferred_tool_pool.set(deferred_tool_pool)
        _ctx_agent_prompt_bundle.set(prompt_bundle)
        _ctx_agent_prompt_instructions_preview.set(prompt_instructions_preview)

    def _get_request_native_tools(self) -> list[dict[str, Any]] | None:
        tools = _ctx_agent_native_tools.get()
        return list(tools) if tools is not None else None

    def _get_request_tool_surface(self) -> Any | None:
        return _ctx_agent_tool_surface.get()

    def _get_request_deferred_tool_pool(self) -> Any | None:
        return _ctx_agent_deferred_tool_pool.get()

    def _get_request_agent_prompt_instructions_preview(self) -> str | None:
        return _ctx_agent_prompt_instructions_preview.get()

    def _get_request_agent_prompt_bundle(self) -> PromptFrameBundle | None:
        return _ctx_agent_prompt_bundle.get()

    def _agent_mode_enabled_by_user_setting(self) -> bool:
        try:
            from config.settings import settings as runtime_settings
            from ui.settings_manager import get_settings_manager

            default_enabled = bool(getattr(runtime_settings, "agent_enabled", True))
            if not default_enabled:
                return False
            return _explicit_setting_enabled(get_settings_manager().get("agent_enabled", default_enabled))
        except Exception:
            logger.exception("Agent mode disabled because user settings could not be loaded")
            return False

    def set_conversation_state_manager(self, manager: ConversationStateManager) -> None:
        """Inject the default conversation state manager.

        Per-request reads and writes prefer the per-user manager published
        by ``IntentPipeline._process_inner`` via a contextvar; this default
        is only consulted when no request is active (setup, tests).
        """
        self._default_conversation_state_manager = manager
        self._context_builder.set_conversation_state_manager(manager)

    @property
    def _channel(self) -> Any:
        """Return the per-request channel, falling back to the default.

        The request-scoped channel is published by
        ``IntentPipeline._process_inner`` using ``use_request_channel``
        so every direct access here â€” send, channel_type reads, the
        interactive-tools gate â€” picks the caller's real channel even
        when multiple users share this controller (CHAN-R7).
        """
        from messaging.channel import get_request_channel

        requested = get_request_channel()
        if requested is not None:
            return requested
        return self._default_channel

    @_channel.setter
    def _channel(self, channel: Any) -> None:
        """Back-compat setter â€” writes the default slot.

        Legacy callers that do ``controller._channel = ch`` pre-CHAN-R7
        now update the default, and the per-request contextvar still
        wins while a request is in flight.
        """
        self._default_channel = channel

    @property
    def _conversation_state_manager(self) -> ConversationStateManager | None:
        """Return the per-request manager, falling back to the default.

        The request-scoped manager is published by
        ``IntentPipeline._process_inner`` using ``use_request_manager``.
        Any code path inside an agent/LLM turn that reads
        ``self._conversation_state_manager`` transparently gets the
        correct user's history, preventing cross-tenant bleed even when
        the surrounding AIController + IntentPipeline are shared across
        multiple users (e.g. the messaging MessageRouter path).
        """
        requested = get_request_conversation_manager()
        if requested is not None:
            return requested
        return self._default_conversation_state_manager

    @_conversation_state_manager.setter
    def _conversation_state_manager(self, manager: ConversationStateManager | None) -> None:
        """Back-compat setter â€” delegates to the default slot.

        Older callers that directly assign
        ``self._conversation_state_manager = ...`` (pre-CHAN-R1) behave
        like ``set_conversation_state_manager`` and update the default.
        """
        self._default_conversation_state_manager = manager
        self._context_builder.set_conversation_state_manager(manager)

    @property
    def _suppress_memory_context(self) -> bool:
        """Proxy to ContextBuilder's suppress_memory flag."""
        return self._context_builder._suppress_memory_context

    @_suppress_memory_context.setter
    def _suppress_memory_context(self, value: bool) -> None:
        self._context_builder.set_suppress_memory(value)

    @property
    def _mcp_hub_init_failed(self) -> bool:
        """Compatibility shim for legacy bool checks against hub init state."""
        return self._mcp_hub_init_failed_at is not None

    @_mcp_hub_init_failed.setter
    def _mcp_hub_init_failed(self, value: bool) -> None:
        self._mcp_hub_init_failed_at = time.monotonic() if value else None

    @staticmethod
    def _browser_config_signature(config: Any | None) -> tuple[Any, ...] | None:
        if config is None:
            return None
        return (
            getattr(config, "name", None),
            getattr(config, "transport", None),
            getattr(config, "module", None),
            getattr(config, "command", None),
            tuple(getattr(config, "args", ()) or ()),
        )

    async def _ensure_mcp_hub(self) -> Any:
        """Lazily initialize the MCP Client Hub for tool dispatch.

        Creates the hub once and caches it. The per-request channel is
        passed through ``hub.call_tool(channel=...)`` so the hub's approval
        bridge can use it for interactive confirmation.

        Returns:
            MCPClientHub instance, or None if initialization failed.
        """
        from mcp_hub.runtime_config import build_runtime_mcp_server_configs

        runtime_configs = build_runtime_mcp_server_configs()
        browser_signature = self._browser_config_signature(runtime_configs.browser_config)

        if self._mcp_hub is not None:
            if browser_signature == self._mcp_browser_config_signature:
                return self._mcp_hub
            logger.info(
                "Browser MCP route changed (%s -> %s); reinitializing MCP hub",
                self._mcp_browser_config_signature,
                browser_signature,
            )
            await self.shutdown()
        if self._mcp_hub_init_failed_at is not None:
            retry_after = self._mcp_hub_init_failed_at + _MCP_HUB_INIT_RETRY_BACKOFF_SECONDS
            if time.monotonic() < retry_after:
                return None

        try:
            from intent.approval import ApprovalManager
            from mcp_hub import ApprovalBridge, MCPClientHub

            # Create approval manager without a channel â€” the channel is
            # threaded per-call via hub.call_tool(channel=...).
            approval_mgr = ApprovalManager()
            bridge = ApprovalBridge(approval_mgr, hook_registry=self.hook_registry)
            hub = MCPClientHub(approval_bridge=bridge)

            await hub.initialize(runtime_configs.fast_configs)

            if any(_cfg.name == "google-workspace" for _cfg in runtime_configs.fast_configs):
                try:
                    from services.oauth.workspace_bridge import (
                        export_tokens_for_workspace,
                    )

                    workspace_user_id = _ctx_user_id.get()
                    if workspace_user_id:
                        asyncio.ensure_future(export_tokens_for_workspace(workspace_user_id))
                except Exception:
                    logger.debug("Workspace token seeding deferred to login")

            # Connect browser server in background â€” tools become available
            # once the subprocess is ready (~20-30s).  Until then, browser
            # tools simply aren't in the routing table (same as disabled).
            if runtime_configs.browser_config is not None:
                self._browser_init_task = asyncio.create_task(
                    self._connect_browser_deferred(hub, runtime_configs.browser_config)
                )

            # Wire the hub into the delegation tool so it can route
            # calls to external compute providers.
            from intent.tools.delegation import set_mcp_hub as _set_delegation_hub

            _set_delegation_hub(hub)

            # Wire the hub into self-management tools so register_mcp_server
            # and list_mcp_servers can interact with the live hub.
            from intent.tools.self_management import set_mcp_hub as _set_self_mgmt_hub

            _set_self_mgmt_hub(hub)

            # Wire the hub into tool_search so it can read live schemas at
            # call time.
            from intent.tools.tool_search import set_mcp_hub as _set_tool_search_hub

            _set_tool_search_hub(hub)

            from services.user_capabilities import (
                set_mcp_hub as _set_user_capabilities_hub,
            )

            _set_user_capabilities_hub(hub)

            self._mcp_hub = hub
            self._mcp_browser_config_signature = browser_signature
            self._mcp_hub_init_failed_at = None
            self._context_builder.set_mcp_hub(hub)
            self._context_builder.set_approval_manager(approval_mgr)
            status_provider = getattr(hub, "get_server_health_flags", None)
            status_setter = getattr(self.client, "set_mcp_server_status_provider", None)
            if callable(status_provider) and callable(status_setter):
                status_setter(status_provider)
            # Keep reference to the bridge's ApprovalManager so the
            # AgentExecutor pre-approves tools on the *same* instance
            # the hub checks during call_tool().
            self._hub_approval_mgr = approval_mgr

            try:
                hub.refresh_dynamic_tool_definitions()
            except Exception:
                logger.debug("Dynamic API tool registration skipped (non-critical)")

            logger.info(
                "MCP Client Hub initialized: %d tools available",
                len(hub.list_tools()),
            )

            return hub
        except Exception as exc:
            logger.warning(
                "MCP hub initialization failed (agent mode unavailable): %s",
                exc,
            )
            self._mcp_hub_init_failed_at = time.monotonic()
            return None

    async def _connect_browser_deferred(self, hub: Any, config: Any) -> None:
        """Connect browser MCP server in background after hub init.

        Called as an asyncio.Task so the first agent command isn't blocked
        by the browser subprocess startup (~20-30s).  Once connected,
        browser tools appear in the hub's routing table automatically.
        """
        try:
            await hub.connect_server("browser", config)
            try:
                hub.hide_tools({"browser_end_task"}, reason="internal_lifecycle")
            except Exception:
                logger.debug("Could not hide browser lifecycle hook from tool surface")
            # Routing simplification (2026-03-28): do not hide browser tools after
            # registration. The agent should see the full browser surface and decide
            # which primitives to use.
            logger.info("Browser server connected (background) with full tool visibility")
        except Exception:
            logger.exception("Browser server background init failed")

    @staticmethod
    def _clear_mcp_helper_hubs_if_owned(hub: Any) -> None:
        """Clear module-global MCP helpers only when they still point at hub."""

        helper_specs = (
            ("intent.tools.delegation", "set_mcp_hub"),
            ("intent.tools.self_management", "set_mcp_hub"),
            ("intent.tools.tool_search", "set_mcp_hub"),
            ("services.user_capabilities.service", "set_mcp_hub"),
        )
        for module_name, setter_name in helper_specs:
            try:
                module = __import__(module_name, fromlist=[setter_name])
                if getattr(module, "_mcp_hub", None) is not hub:
                    continue
                setter = getattr(module, setter_name)
                setter(None)
            except (AttributeError, ImportError) as exc:
                logger.debug("MCP helper hub clear skipped for %s: %s", module_name, exc)

    async def shutdown(self) -> None:
        """Shut down the MCP hub and release resources."""
        if self._browser_init_task is not None and not self._browser_init_task.done():
            self._browser_init_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._browser_init_task
        self._browser_init_task = None
        self._mcp_browser_config_signature = None
        if self._mcp_hub is not None:
            hub = self._mcp_hub
            try:
                await hub.shutdown()
            except Exception as exc:
                logger.debug("MCP hub shutdown error: %s", exc)
            self._clear_mcp_helper_hubs_if_owned(hub)
            self._mcp_hub = None
            self._mcp_hub_init_failed_at = None
            self._context_builder.set_mcp_hub(None)
            self._context_builder.set_approval_manager(None)
            status_setter = getattr(self.client, "set_mcp_server_status_provider", None)
            if callable(status_setter):
                status_setter(None)

    async def _play_playlist(self, playlist_name: str) -> str:
        """
        Play a playlist by name, queueing all tracks.

        This method resolves the playlist, plays the first track immediately,
        and enqueues the remaining tracks.

        Args:
            playlist_name: The name of the playlist to play

        Returns:
            A status message indicating what's playing
        """
        try:
            from music.playlist_manager import get_playlist_manager

            playlist_mgr = get_playlist_manager()
        except ImportError:
            logger.error("Playlist manager not available")
            return "Playlist manager not available"

        # Resolve user_id from ambient context for per-user playlist isolation
        try:
            from core.user_context import get_current_user_id

            _uid: str | None = get_current_user_id()
        except LookupError:
            _uid = None

        playlist = playlist_mgr.get_playlist(playlist_name, user_id=_uid)
        if not playlist:
            return f"Playlist '{playlist_name}' not found"

        # Get videos/tracks from playlist
        try:
            videos = await playlist_mgr.get_playlist_videos(playlist_name, user_id=_uid)
        except Exception as exc:
            logger.error("Failed to get playlist videos: %s", exc)
            return f"Failed to load playlist: {exc}"

        if not videos:
            return "Playlist is empty"

        if not self.music:
            return "No music player available"

        from music.playlist_queue import queue_playlist_tracks

        successes, _failures = await queue_playlist_tracks(self.music, videos)

        if successes == 0:
            return f"Failed to play playlist '{playlist_name}'"

        first_title = videos[0].get("title", playlist_name) if isinstance(videos[0], dict) else playlist_name
        return f"Playing playlist: {first_title}"

    # ------------------------------------------------------------------
    # C2: Conversation history management
    # ------------------------------------------------------------------

    def _resolve_request_user_id(
        self,
        requested_user_id: str | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Resolve an explicit per-request user_id without using a global default."""
        candidates = [
            requested_user_id,
            (context or {}).get("user_id"),
            (context or {}).get("session_user_id"),
            getattr(self._channel, "user_id", None),
            getattr(self._voice_pipeline, "user_id", None),
        ]
        for candidate in candidates:
            if isinstance(candidate, str):
                normalized = candidate.strip()
                if normalized and normalized != "default":
                    return normalized

        session_candidates = [
            (context or {}).get("session_id"),
            getattr(self._channel, "session_id", None),
            getattr(self._voice_pipeline, "session_id", None),
        ]
        for session_id in session_candidates:
            if isinstance(session_id, str) and session_id.strip():
                return "session:%s" % session_id.strip()

        raise ValueError("AIController requires explicit user_id for request processing")

    def _request_channel_type(self) -> str:
        return str(getattr(self._channel, "channel_type", "none") if self._channel is not None else "none")

    def _current_session_id(self, context: dict[str, Any] | None = None) -> str | None:
        manager = self._conversation_state_manager
        manager_session_id = getattr(manager, "session_id", None) if manager is not None else None
        if isinstance(manager_session_id, str) and manager_session_id.strip():
            return manager_session_id.strip()
        candidates = [
            (context or {}).get("session_id"),
            getattr(self._channel, "session_id", None),
            getattr(self._voice_pipeline, "session_id", None),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    async def _maybe_expire_history(self, user_id: str) -> None:
        """Expire pending runtime confirmations and stamp the request time.

        Must be called at the start of every request cycle.

        Args:
            user_id: User identifier for per-user runtime isolation.
        """
        async with self._history_lock:
            now = time.monotonic()
            pending = self._pending_tool_confirmations.get(user_id)
            if pending and (now - pending["created_at"]) > _PENDING_TOOL_CONFIRMATION_EXPIRY_SECONDS:
                self._pending_tool_confirmations.pop(user_id, None)
            self._history_timestamps[user_id] = now

    def _warn_legacy_request_history(self, context: dict[str, Any] | None, *, source: str) -> None:
        # Defensive guard: request-supplied role/content lists are not a supported
        # prompt path. Stored frames from ContextBuilder are the only source for
        # prior-turn model context. If any caller still attaches a "history" list
        # to the request context, log a warning so we notice and ignore the payload.
        if isinstance(context, dict) and isinstance(context.get("history"), list) and context["history"]:
            logger.warning(
                "Ignoring legacy role/content request history in %s; using PromptFrameBundle frames",
                source,
            )

    @staticmethod
    def _is_silent_recovery_exchange(params: dict[str, object] | None) -> bool:
        """True when the exchange was a runtime-emitted operator-diagnostic fallback.

        Silent-recovery messages (e.g. "I'm having trouble reaching my AI provider...")
        are emitted by the runtime when an upstream provider call fails — they are not
        produced by the model. Persisting them as assistant conversation turns leaks the
        failure state into the next task's input and misleads the agent on retry. The
        agent loop attaches the diagnostic onto `command_params.error_state` /
        `command_params.no_result` via `user_message_for_operator_diagnostic`; the
        presence of that diagnostic metadata is the authoritative signal.
        """

        if not isinstance(params, dict):
            return False
        command_params = params.get("command_params")
        if not isinstance(command_params, dict):
            return False
        for key in ("error_state", "no_result"):
            section = command_params.get(key)
            if not isinstance(section, dict):
                continue
            diagnostic = section.get("diagnostic") or section.get("operator_diagnostic")
            if isinstance(diagnostic, dict) and str(diagnostic.get("category") or "").strip():
                return True
        return False

    async def _record_context_frame_if_supported(self, frame: Frame, *, user_id: str) -> bool:
        """Write a typed Frame through the real canonical state-manager API."""

        manager = self._conversation_state_manager
        if manager is None:
            return False

        add_message = getattr(manager, "add_message", None)
        if not callable(add_message):
            return False
        try:
            result = add_message(frame)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception:
            logger.debug(
                "Conversation state manager add_message(Frame) failed for user_id=%s",
                user_id,
            )
            return False

    async def _record_resume_entry_frame(
        self,
        *,
        session_id: str,
        checkpoint_id: str,
        user_id: str,
        channel: str,
    ) -> None:
        """Persist checkpoint resume as an explicit session meta frame."""

        frame = Frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            blocks=(
                SystemReminderBlock(
                    text="Resuming task checkpoint %s in session %s." % (checkpoint_id, session_id),
                    source_tag="session-resume",
                ),
            ),
            is_meta=True,
            origin="session_resume",
            session_id=session_id,
            extra={
                "resume_entrypoint": "checkpoint",
                "checkpoint_id": checkpoint_id,
                "session_id": session_id,
                "channel": channel,
            },
        )
        await self._record_context_frame_if_supported(frame, user_id=user_id)

    def _voice_conversation_manager_for_user(self, user_id: str | None) -> ConversationStateManager | None:
        manager = self._conversation_state_manager
        if manager is not None:
            return manager
        resolved = self._resolve_request_user_id(user_id)
        try:
            return get_conversation_manager(resolved)
        except Exception:
            logger.debug("Unable to resolve voice conversation manager for user_id=%s", resolved)
            return None

    def _apply_voice_current_user_frame(
        self,
        context_bundle: PromptFrameBundle,
        *,
        text: str,
        user_id: str,
    ) -> PromptFrameBundle:
        """Attach the active voice transcript frame to provider-bound context."""

        from core.voice_canonical_carrier import current_voice_turn_frame

        manager = self._voice_conversation_manager_for_user(user_id)
        session_id = manager.session_id if manager is not None else ""
        frame = current_voice_turn_frame(text, session_id=session_id)
        if frame is not None:
            context_bundle.current_user_frame = frame
        return context_bundle

    async def record_voice_event(
        self,
        event_type: str,
        metadata: object | None = None,
        *,
        user_id: str | None = None,
    ) -> bool:
        """Record a voice runtime event through the canonical frame writer."""

        manager = self._voice_conversation_manager_for_user(user_id)
        if manager is None:
            return False

        add_voice_event = getattr(manager, "add_voice_event", None)
        try:
            if callable(add_voice_event):
                result = add_voice_event(event_type, metadata)
            else:
                from core.voice_canonical_carrier import VoiceEventFrame

                add_message = getattr(manager, "add_message", None)
                if not callable(add_message):
                    return False
                result = add_message(VoiceEventFrame(event_type, metadata, manager.session_id))
            if inspect.isawaitable(result):
                await result
            return True
        except Exception:
            logger.debug("Failed to record voice event %s", event_type)
            return False

    @staticmethod
    def _is_internal_history_artifact(role: str, content: str) -> bool:
        """Detect internal tool protocol fragments that must not seed later prompts."""

        role_name = str(role or "").strip().lower()
        text = str(content or "").strip()
        if not text:
            return False
        if "[TOOL_RESULT:" in text:
            return True
        if _INTERNAL_HISTORY_GAUNTLET_RE.search(text):
            return True
        if role_name == "assistant" and _INTERNAL_HISTORY_TOOL_JSON_RE.search(text):
            return True
        if role_name != "assistant":
            return False
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        return str(parsed.get("type") or "").strip().lower() in _INTERNAL_HISTORY_TOOL_TYPES

    def _try_record_gate_state_frame(
        self,
        manager: Any,
        *,
        user_text: str,
        assistant_text: str,
        user_id: str,
        intent: str | None,
        params: dict[str, object] | None,
        metadata: dict[str, object],
    ) -> bool:
        """Record gate state through the canonical typed frame writer."""
        frame = _gate_frame_from_record_params(assistant_text, params)
        if frame is None:
            return False

        add_message = getattr(manager, "add_message", None)
        if not callable(add_message):
            return False

        try:
            add_message("user", user_text, metadata=metadata, origin=None)
            add_message(frame)
            return True
        except (TypeError, ValueError, AttributeError):
            logger.debug("Typed gate-state add_message(Frame) unavailable; falling back to legacy exchange")
            return False

    async def _record_exchange(
        self,
        user_text: str,
        assistant_text: str,
        user_id: str | None = None,
        *,
        intent: str | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Append a user/assistant exchange and trim to max size.

        Args:
            user_text: The user's message text.
            assistant_text: The assistant's response text.
            user_id: User identifier for per-user history isolation.
        """
        resolved_user_id = self._resolve_request_user_id(user_id)
        if self._is_internal_history_artifact("assistant", assistant_text):
            logger.warning("Skipped recording internal tool artifact as assistant conversation history")
            return
        if self._is_silent_recovery_exchange(params):
            # Silent-recovery fallback messages are runtime-emitted strings, not real
            # model answers. Store them only as RECOVERY frames so behavioral history
            # filtering drops them from the next task's prompt.
            recorded_recovery = False
            if self._conversation_state_manager is not None:
                try:
                    record_exchange = getattr(self._conversation_state_manager, "record_exchange", None)
                    if callable(record_exchange):
                        signature = inspect.signature(record_exchange)
                        recovery_kwargs: dict[str, object] = {}
                        if "user_id" in signature.parameters:
                            recovery_kwargs["user_id"] = resolved_user_id
                        if "intent" in signature.parameters:
                            recovery_kwargs["intent"] = intent
                        if "params" in signature.parameters:
                            recovery_kwargs["params"] = params or {}
                        if "user_origin" in signature.parameters:
                            recovery_kwargs["user_origin"] = "recovery"
                        if "assistant_origin" in signature.parameters:
                            recovery_kwargs["assistant_origin"] = "recovery"
                        if "user_is_meta" in signature.parameters:
                            recovery_kwargs["user_is_meta"] = True
                        if "assistant_is_meta" in signature.parameters:
                            recovery_kwargs["assistant_is_meta"] = True
                        result = record_exchange(user_text, assistant_text, **recovery_kwargs)
                        if inspect.isawaitable(result):
                            await result
                        recorded_recovery = True
                    else:
                        add_message = getattr(self._conversation_state_manager, "add_message", None)
                        if callable(add_message):
                            signature = inspect.signature(add_message)
                            add_kwargs: dict[str, object] = {}
                            if "metadata" in signature.parameters:
                                add_kwargs["metadata"] = {
                                    "intent": intent,
                                    "params": dict(params or {}),
                                    "recovery_exchange": True,
                                }
                            user_kwargs = dict(add_kwargs)
                            assistant_kwargs = dict(add_kwargs)
                            if "origin" in signature.parameters:
                                user_kwargs["origin"] = "recovery"
                                assistant_kwargs["origin"] = "recovery"
                            if "is_meta" in signature.parameters:
                                user_kwargs["is_meta"] = True
                                assistant_kwargs["is_meta"] = True
                            add_message(
                                "user",
                                user_text,
                                **user_kwargs,
                            )
                            add_message(
                                "assistant",
                                assistant_text,
                                **assistant_kwargs,
                            )
                            recorded_recovery = True
                except Exception:
                    logger.debug("Failed to record silent-recovery frame", exc_info=True)
            if recorded_recovery:
                logger.info(
                    "Recorded silent-recovery fallback as non-behavioral recovery frame (user_id=%s)",
                    resolved_user_id,
                )
            else:
                logger.info(
                    "Skipped legacy history write for silent-recovery fallback (typed writer unavailable, user_id=%s)",
                    resolved_user_id,
                )
            return
        metadata: dict[str, object] = {}
        if intent:
            metadata["intent"] = intent
        if isinstance(params, dict) and params:
            metadata["params"] = dict(params)

        if self._conversation_state_manager is not None:
            try:
                if self._try_record_gate_state_frame(
                    self._conversation_state_manager,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    user_id=resolved_user_id,
                    intent=intent,
                    params=params,
                    metadata=metadata,
                ):
                    return
                record_exchange = getattr(self._conversation_state_manager, "record_exchange", None)
                if callable(record_exchange):
                    signature = inspect.signature(record_exchange)
                    kwargs: dict[str, object] = {}
                    if "user_id" in signature.parameters:
                        kwargs["user_id"] = resolved_user_id
                    if "intent" in signature.parameters:
                        kwargs["intent"] = intent
                    if "params" in signature.parameters:
                        kwargs["params"] = params or {}
                    record_exchange(user_text, assistant_text, **kwargs)
                else:
                    signature = inspect.signature(self._conversation_state_manager.add_message)
                    add_kwargs: dict[str, object] = {}
                    if "user_id" in signature.parameters:
                        add_kwargs["user_id"] = resolved_user_id
                    if "metadata" in signature.parameters:
                        self._conversation_state_manager.add_message("user", user_text, metadata=metadata, **add_kwargs)
                        self._conversation_state_manager.add_message(
                            "assistant",
                            assistant_text,
                            metadata=metadata,
                            **add_kwargs,
                        )
                    else:
                        self._conversation_state_manager.add_message("user", user_text, **add_kwargs)
                        self._conversation_state_manager.add_message("assistant", assistant_text, **add_kwargs)
            except (TypeError, ValueError, AttributeError):
                logger.debug("Conversation state manager lacks a compatible turn writer; exchange not recorded")
            else:
                return

    def _maybe_record_action_recipe(
        self,
        *,
        user_text: str,
        assistant_text: str,
        user_id: str,
        ok: bool,
        command: str | None = None,
        command_params: dict[str, object] | None = None,
        tools_called: list[str] | None = None,
        payment_gate: bool = False,
        signature_gate: bool = False,
        source: str = "agent",
    ) -> None:
        """Record successful action-shaped turns for later "usual" recalls."""

        if not ok:
            return

        tools = [str(tool) for tool in (tools_called or []) if tool]
        intent = infer_action_intent(user_text, command=command, tools_called=tools)
        if intent == "general.action" and payment_gate:
            intent = "gate.payment"
        elif intent == "general.action" and signature_gate:
            intent = "gate.signature"
        if intent == "general.action" and not (command or payment_gate or signature_gate):
            return
        if not (command or payment_gate or signature_gate or tools):
            return

        params_payload: dict[str, object] = {
            "tools_called": tools,
        }
        if command:
            params_payload["command"] = command
        if isinstance(command_params, dict) and command_params:
            params_payload["command_params"] = command_params
        if payment_gate:
            params_payload["payment_gate"] = True
        if signature_gate:
            params_payload["signature_gate"] = True

        try:
            get_action_recipe_store().record(
                user_id=user_id,
                intent=intent,
                user_text=user_text,
                assistant_text=assistant_text,
                params=params_payload,
                steps=[{"tool": tool} for tool in tools],
                source=source,
            )
        except Exception:
            logger.debug("Failed to record action recipe", exc_info=True)

    def _build_capability_context(self, text: str) -> str:
        """Build dynamic capability context from the registry for the LLM.

        Production no longer uses a domain classifier here; capability context
        is requested for the general all-tools path.
        Returns empty string if registry is not available.
        """
        try:
            from services.capability_registry import CapabilityRegistry

            registry = CapabilityRegistry.get_instance()
            if registry is None:
                return ""

            return registry.get_llm_capability_context(None)
        except Exception:
            logger.debug("Capability context build failed", exc_info=True)
            return ""

    # ------------------------------------------------------------------
    # Tiered model routing
    # ------------------------------------------------------------------

    def _get_model_for_request(self, is_agent_mode: bool) -> str | None:
        """Return model override for this request, or None for default.

        When a user has configured ``agent_model`` in settings, agent-mode
        requests use that model while ASK/ROUTE requests use the default.

        Args:
            is_agent_mode: Whether the current request is in agent mode
                (tool schemas are present and agent_enabled is True).

        Returns:
            The agent model string if agent mode is active and an agent
            model is configured, otherwise None (use default).
        """
        if not is_agent_mode:
            return None

        from config.defaults import resolve_effective_model
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        explicit_agent_model = str(sm.get("agent_model", "") or "").strip()
        explicit_route_model = str(sm.get("llm_model", "") or "").strip()

        # If the user set llm_model but NOT agent_model, respect their
        # conversation-tier choice. Otherwise the agent-tier fallback can
        # silently upgrade the request past what the user explicitly chose
        # (e.g. codex: user picks gpt-5.4-mini, default agent fallback is
        # DEFAULT_CODEX_MODEL="gpt-5.4", so every agent turn gets bumped to
        # gpt-5.4 with no signal to the user). Provider-asymmetric defaults
        # (Anthropic agent=Sonnet, conversation=Haiku) still apply when the
        # user has not pinned llm_model either.
        if explicit_route_model and not explicit_agent_model:
            return None

        ai_source = str(sm.get("ai_source", "managed") or "managed")
        provider = str(sm.get("llm_provider", "openai") or "openai")
        route_model = resolve_effective_model(
            ai_source=ai_source,
            provider=provider,
            agent=False,
            candidates=(explicit_route_model,),
        )
        agent_model = resolve_effective_model(
            ai_source=ai_source,
            provider=provider,
            agent=True,
            candidates=(explicit_agent_model,),
        )
        if not agent_model or agent_model == route_model:
            return None

        logger.debug(
            "Hybrid model routing: using %s for agent request",
            agent_model,
        )
        return agent_model

    # ------------------------------------------------------------------
    # Context assembly methods (REMOVED â€” now in ContextBuilder)
    # ------------------------------------------------------------------
    # The following methods were removed in the ContextBuilder unification:
    # - _MEMORY_CATEGORY_HEADERS, _MEMORY_DUMP_THRESHOLD, _MEMORY_WORD_BUDGET
    # - _build_memory_context()
    # - _build_account_owner_context()
    # - _build_profile_context()
    # - _build_delivery_address_context()
    # - _build_user_model_context()
    # - _get_playback_context()
    # All context assembly now lives in intent/context_builder.py:ContextBuilder

    # ------------------------------------------------------------------
    # Main request processing
    # ------------------------------------------------------------------

    async def process_request(
        self,
        text: str,
        context: dict[str, Any] | None = None,
        user_key: str | None = None,
    ) -> ProcessResultDict:
        """
        Process a user request, using GPT if available for complex commands.

        This method first checks if the request can be handled deterministically.
        If not, and a GPT client is available, it falls back to AI processing.
        Conversation history is maintained across calls (expires after 5 min).

        Args:
            text: The user's request text
            context: Optional context dict containing history or other metadata

        Returns:
            A dict with the processing result including:
            - ok: bool indicating success
            - commands_executed: list of executed commands
            - data: result data including command_results
        """
        resolved_user_id = self._resolve_request_user_id(user_key, context=context)
        # Store user_key for data isolation in context builders and tools
        _ctx_user_id.set(resolved_user_id)

        # Latency lane B (#465): fire the memory side-query NOW -- the earliest
        # point the user text + user_id are known -- so its ~1.2 s network wait
        # overlaps the pre-context glue (history expiry, provider-state reset,
        # prompt-prep) instead of blocking the turn at context-build. build_frames
        # consumes the result for THIS turn's prompt (semantics-preserving). No-op
        # when memory is suppressed/disabled; never touches provider state.
        self._context_builder.prefetch_memory(resolved_user_id, text)

        # C2: Manage conversation history expiry and timestamp
        await self._maybe_expire_history(resolved_user_id)

        # Signature-gate reply hijack removed 2026-04-26 (see comment block at
        # _SIGNATURE_GATE_*_RE removal site). The AI handles all replies in
        # context â€” including resuming or cancelling a pending gate â€” through
        # the normal agent loop with surfaced checkpoint metadata.

        # First try deterministic interpretation
        result: ProcessResultDict = {
            "ok": True,
            "commands_executed": [],
            "data": {"command_results": {}},
        }

        # R3-P1-G (2026-05-30): the pre-LLM ``_is_local_path`` check
        # that fast-pathed ``play <local-path>`` to ``_handle_local_play``
        # is DELETED. The model has a ``media`` tool with a ``local``
        # provider; it sees raw user text and routes ``play /tmp/x.mp3``
        # to ``media(provider="local", query=...)`` from there. Pre-LLM
        # path-classifiers (and any ``_is_*``-shape dispatch that
        # short-circuits the model) violate the parity rule that the
        # model is trusted with raw context.

        # Routing simplification (2026-03-28): the intent pipeline already owns
        # deterministic rule execution. Bypass the duplicate parser here so the
        # agent path receives the same unresolved requests the pipeline saw.

        # Fall back to GPT if available
        if self.client:
            # Canonical path: every provider enters the native agent loop.
            _native_route = getattr(self.client, "route_command_native", None)
            if callable(_native_route) and getattr(self.client, "SUPPORTS_NATIVE_TOOLS", False) is True:
                try:
                    # Unified context assembly via ContextBuilder
                    from diagnostics import latency_spans

                    _ch_type = getattr(self._channel, "channel_type", "none") if self._channel is not None else "none"
                    with latency_spans.span("CONTEXT_BUILD_FRAMES"):
                        context_bundle = self._context_builder.build_frames(
                            user_id=resolved_user_id,
                            user_text=text,
                            channel_type=_ch_type,
                        )
                    context_bundle = self._apply_voice_current_user_frame(
                        context_bundle,
                        text=text,
                        user_id=resolved_user_id,
                    )
                    # Populate telemetry components (cached from build_frames() above)
                    _ctx_system_context_components.set(self._context_builder.get_context_components())
                    if self.client is not None:
                        try:
                            self.client._system_context_components = dict(_ctx_system_context_components.get() or {})
                        except Exception:
                            logger.debug("Failed to attach system context components to client, continuing")

                    # Reset provider state from previous request cycle
                    if self.client:
                        if hasattr(self.client, "set_system_prompt"):
                            self.client.set_system_prompt(None)
                        elif hasattr(self.client, "_agent_system_prompt"):
                            self.client._agent_system_prompt = None
                        if hasattr(self.client, "_native_tools"):
                            self.client._native_tools = None
                        if hasattr(self.client, "_agent_tool_choice"):
                            self.client._agent_tool_choice = "auto"
                            _provider = getattr(self.client, "_provider", None)
                            if _provider is not None:
                                _provider._agent_tool_choice = "auto"
                        if hasattr(self.client, "_ask_tier_native"):
                            self.client._ask_tier_native = False
                    _ctx_ask_tier_active.set(False)
                    self._set_request_agent_surface(native_tools=None, tool_surface=None, prompt_bundle=None)

                    # Agent mode: use the unified prompt plus runtime context.
                    with latency_spans.span("AGENT_PROMPT_PREP"):
                        await self._maybe_use_agent_prompt(context_bundle, user_text=text)

                    # â”€â”€ Direct agent path (2026-04-09) â”€â”€
                    # When agent mode is active, skip the route_command
                    # classification call and go straight to the agent
                    # loop.  The model decides tool_call vs text inside
                    # the executor â€” one path, like Claw Code.
                    _agent_bundle = self._get_request_agent_prompt_bundle()
                    _agent_mode_active = _agent_bundle is not None and self._mcp_hub is not None
                    if _agent_mode_active:
                        # Plan limit: spend caps are checked inside
                        # AgentExecutor.run(); keep only the read-only spend
                        # check here so this preflight does not double-count.
                        try:
                            from services.llm.managed_budget import (
                                check_managed_llm_spend_cap_async,
                            )

                            with latency_spans.span("SPEND_PREFLIGHT"):
                                _spend_check = await check_managed_llm_spend_cap_async(resolved_user_id)
                            if not _spend_check.allowed:
                                logger.info(
                                    "Plan limit hit for %s: %s",
                                    resolved_user_id,
                                    _spend_check.reason,
                                )
                                result["data"]["intent"] = "answer"
                                result["data"]["message"] = ""
                                result["data"]["cap_state"] = _spend_check.cap_state
                                return result
                        except Exception:
                            # Fail-closed: deny if limiter is unavailable (except desktop mode)
                            from config.settings import settings as _cfg

                            if getattr(_cfg, "app_surface", "desktop") != "desktop":
                                logger.exception("Plan limiter unavailable â€” denying request (fail-closed)")
                                result["data"]["intent"] = "answer"
                                result["data"][
                                    "message"
                                ] = "I can't verify your usage budget right now. Please try again shortly."
                                return result
                            logger.debug(
                                "Plan limiter unavailable in desktop mode",
                                exc_info=True,
                            )

                        agent_result = await self._handle_agent_loop(
                            text,
                            None,  # no pre-classified response
                            context_bundle,
                            user_id=resolved_user_id,
                            request_context=context,
                        )
                        if agent_result is not None:
                            return agent_result
                        logger.error(
                            "Canonical agent loop unavailable after native setup; refusing legacy fallback",
                        )
                        result["ok"] = False
                        result["success"] = False
                        result["intent"] = "ai_no_result"
                        result["error"] = "agent_loop_unavailable"
                        result["message"] = "I can't run the tool loop for that request right now."
                        result["data"]["answer"] = result["message"]
                        result["data"]["no_result"] = {
                            "reason": "agent_loop_unavailable",
                            "retryable": True,
                        }
                        result["continue_listening"] = True
                        return result

                    logger.error(
                        "Native provider did not publish an agent prompt or MCP hub; refusing legacy classifier fallback",
                    )
                    result["ok"] = False
                    result["success"] = False
                    result["intent"] = "ai_no_result"
                    result["error"] = "agent_loop_not_prepared"
                    result["message"] = "I can't prepare the tool loop for that request right now."
                    result["data"]["answer"] = result["message"]
                    result["data"]["no_result"] = {
                        "reason": "agent_loop_not_prepared",
                        "retryable": True,
                    }
                    result["continue_listening"] = True
                    return result

                except asyncio.CancelledError:
                    # Propagate for the same reason as _handle_agent_loop below
                    # it: a returned value here reads to the canceller as a
                    # finished turn, and on the relay path that canceller is a
                    # wait_for whose TimeoutError branch then never runs.
                    logger.warning("Native agent loop cancelled for: %s", text[:80])
                    raise
                except Exception as e:
                    logger.error("Native agent loop failed: %s", e)
                    result["ok"] = False
                    # P3 fix: surface a specific user-friendly message for
                    # timeout / network / provider-unavailable errors so the
                    # user gets fast feedback instead of a generic error.
                    _friendly = _llm_error_to_user_message(e)
                    result["message"] = _friendly
                    result["data"]["answer"] = _friendly
                    return result
            else:
                # Raw client (OpenAI API style)
                return await self._process_with_gpt(text, result)

        # No LLM client configured â€” provide a helpful message instead of
        # returning an empty result that produces a silent command_failed.
        result["ok"] = False
        result["message"] = (
            "I need an AI API key to handle that. Add one in your .env file â€” check useviola.com/setup for instructions."
        )
        result["data"]["answer"] = result["message"]
        return result

    # ------------------------------------------------------------------
    # Streaming answer for voice TTS overlap
    # ------------------------------------------------------------------

    async def stream_answer_tokens(
        self,
        text: str,
        queue: asyncio.Queue[str | None],
        context: dict[str, Any] | None = None,
        user_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Stream LLM answer tokens to *queue* for concurrent TTS playback.

        Calls the LLM provider's streaming route and pushes text tokens into
        *queue* as they arrive.  A ``None`` sentinel is pushed when streaming
        is complete.

        After streaming finishes, runs the same post-processing as
        ``process_request()`` (history recording, continue_listening, etc.)
        and returns the full result dict.

        Returns ``None`` if streaming is not available for this request
        (agent mode, native tools, unsupported provider), signalling the
        caller to fall back to the batch ``process_request()`` path.
        """
        from config.settings import settings

        resolved_user_id = self._resolve_request_user_id(user_key, context=context)
        _ctx_user_id.set(resolved_user_id)
        await self._maybe_expire_history(resolved_user_id)

        # Streaming is only supported for the non-agent route_command path
        if not self.client or not hasattr(self.client, "route_command_streaming"):
            await queue.put(None)
            return None

        # Check if agent mode is active â€” not streamable
        streaming_context_bundle = self._build_streaming_context_bundle(text, resolved_user_id)
        streaming_context_bundle = self._apply_voice_current_user_frame(
            streaming_context_bundle,
            text=text,
            user_id=resolved_user_id,
        )
        await self._maybe_use_agent_prompt(streaming_context_bundle, user_text=text)
        _agent_bundle = self._get_request_agent_prompt_bundle()
        _agent_mode_active = _agent_bundle is not None and self._mcp_hub is not None
        if _agent_mode_active:
            await queue.put(None)
            return None

        # Build route kwargs (same as process_request)
        _override = _ctx_agent_model_override.get()
        if hasattr(self.client, "_settle_user_id"):
            self.client._settle_user_id = resolved_user_id
            self.client._settle_estimated_tokens = settings.llm_max_tokens_cap
        self._warn_legacy_request_history(context, source="process_streaming_request.route_command")
        _route_kwargs: dict[str, Any] = {
            "context_bundle": streaming_context_bundle,
        }

        from services.llm.managed_budget import (
            check_managed_llm_spend_cap_async,
            managed_llm_budget_message,
        )

        _managed_budget_gate = await check_managed_llm_spend_cap_async(resolved_user_id)
        if not _managed_budget_gate.allowed:
            answer_text = managed_llm_budget_message(_managed_budget_gate)
            await queue.put(answer_text)
            await queue.put(None)
            return {
                "ok": True,
                "commands_executed": [],
                "data": {
                    "command_results": {},
                    "intent": "answer",
                    "answer": answer_text,
                    "cap_state": _managed_budget_gate.cap_state,
                },
                "intent": "answer",
                "message": answer_text,
                "response": answer_text,
                "continue_listening": False,
                "spoken": True,
            }

        result: dict[str, Any] = {
            "ok": True,
            "commands_executed": [],
            "data": {"command_results": {}},
        }

        try:
            done_event: dict[str, Any] | None = None
            streamed_tokens: list[str] = []

            async for event in self.client.route_command_streaming(text, **_route_kwargs):
                if event.get("done"):
                    done_event = event
                    break
                token = event.get("token")
                if token:
                    streamed_tokens.append(token)
                    await queue.put(token)

            # Signal end of tokens
            await queue.put(None)

            if done_event is None:
                logger.warning("Streaming route_command produced no done event")
                return None

            response_type = str(done_event.get("type", "unknown"))

            # Only handle answer-type responses via streaming
            if response_type != "answer":
                # Non-answer (tool_call, ignore) falls back to batch.
                return None

            # --- Answer post-processing (mirrors process_request) ---
            answer_text = str(done_event.get("answer", ""))
            answer_text = answer_text.strip()

            # CB-10: Strip JSON template artifacts
            answer_text = _strip_json_template(answer_text)

            if not answer_text:
                logger.warning("Streaming LLM returned empty answer for: %s", text[:50])

            result["intent"] = "answer"
            result["message"] = answer_text
            result["data"]["answer"] = answer_text
            result["response"] = answer_text
            result["ok"] = True

            result["continue_listening"] = bool(done_event.get("continue_listening", False))

            # Pass through content card and voice summary
            card = done_event.get("card")
            if card and isinstance(card, dict):
                result["card"] = card
            voice_summary = done_event.get("voice_summary")
            if voice_summary and isinstance(voice_summary, str):
                result["voice_summary"] = voice_summary

            # Token usage propagation
            _resp_usage = done_event.get("_usage")
            if isinstance(_resp_usage, dict):
                result["_usage"] = _resp_usage

            # Record exchange in conversation history
            if answer_text:
                await self._record_exchange(text, answer_text, resolved_user_id, intent="answer")

            result["spoken"] = True  # Signal TTS was handled during streaming
            return result

        except Exception:
            logger.debug("stream_answer_tokens failed, falling back to batch", exc_info=True)
            # Ensure sentinel is pushed so TTS consumer doesn't hang
            try:
                await queue.put(None)
            except Exception:
                logger.debug("stream sentinel push failed during fallback", exc_info=True)
            return None

    def _build_streaming_context_bundle(self, text: str, user_id: str) -> PromptFrameBundle:
        """Build the prompt-frame bundle for streaming route_command.

        Delegates to ContextBuilder so streaming uses the same context as
        ``process_request()``.
        """
        _ch_type = getattr(self._channel, "channel_type", "none") if self._channel is not None else "none"
        return self._context_builder.build_frames(user_id=user_id, user_text=text, channel_type=_ch_type)

    # ------------------------------------------------------------------
    # Agent tool-use loop
    # ------------------------------------------------------------------

    def _categorize_tools(self, tools: list[dict]) -> dict[str, list[str]]:
        """Group tools by capability category for the system prompt.

        Args:
            tools: List of tool descriptor dicts with at least a ``name`` key.

        Returns:
            Mapping from category name to list of tool names.  Empty categories
            are excluded.
        """
        categories: dict[str, list[str]] = {
            "File management": [],
            "Web browsing": [],
            "Email & messaging": [],
            "Calendar & scheduling": [],
            "Cloud storage": [],
            "System & shell": [],
            "Search & research": [],
            "Memory": [],
            "External delegation": [],
            "Other": [],
        }
        for tool in tools:
            name = tool.get("name", "")
            if not name:
                continue
            if name.startswith("browser_"):
                categories["Web browsing"].append(name)
            elif any(
                k in name
                for k in [
                    "file",
                    "directory",
                    "read_file",
                    "write_file",
                    "search_files",
                    "delete_file",
                ]
            ):
                categories["File management"].append(name)
            elif any(k in name for k in ["email", "gmail", "sms", "share"]):
                categories["Email & messaging"].append(name)
            elif any(k in name for k in ["calendar", "event", "timer"]) or "schedule" in name:
                categories["Calendar & scheduling"].append(name)
            elif any(k in name for k in ["drive", "gdrive", "docs", "sheets"]):
                categories["Cloud storage"].append(name)
            elif any(k in name for k in ["shell", "command", "system"]):
                categories["System & shell"].append(name)
            elif "memory" in name:
                categories["Memory"].append(name)
            elif "search" in name:
                categories["Search & research"].append(name)
            elif "delegate" in name:
                categories["External delegation"].append(name)
            else:
                categories["Other"].append(name)
        return {k: v for k, v in categories.items() if v}

    def _render_prompt_instructions(
        self,
        context_bundle: PromptFrameBundle,
        *,
        schema_text: str = "",
        native_tools: bool = True,
        custom_instructions: str = "",
        channel_type: str | None = None,
        response_contract: str = "",
    ) -> str:
        rendered = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                schema_text=schema_text,
                native_tools=native_tools,
                custom_instructions=custom_instructions,
                channel_type=channel_type,
                session_id=self._current_session_id(),
                response_contract=response_contract,
            )
        )
        return str(rendered.get("instructions") or "").strip()

    def _set_ask_fallback_prompt(self, context_bundle: PromptFrameBundle) -> None:
        """Configure the lightweight ASK prompt for agent-loop fallback.

        Called when the agent tool-use loop is unavailable (e.g. MCP hub
        down) and we need a direct conversational answer instead.  Sets
        ``_agent_system_prompt`` on the provider to the unified prompt plus
        the current runtime context so the LLM responds conversationally
        without tools.

        For providers with ``SUPPORTS_NATIVE_TOOLS``, uses native function
        calling with [answer, ignore] tools instead of JSON-in-prompt.
        """
        if not self.client:
            return

        _supports_native = getattr(self.client, "SUPPORTS_NATIVE_TOOLS", False)

        if _supports_native:
            try:
                _actual_provider = getattr(self.client, "_provider", self.client)
                _provider_module = type(_actual_provider).__module__ or ""
                if "anthropic" in _provider_module:
                    from services.llm.route_tool_schemas import (
                        get_ask_only_tools_anthropic,
                    )

                    ask_tools = get_ask_only_tools_anthropic()
                else:
                    from services.llm.route_tool_schemas import (
                        get_ask_only_tools_openai,
                    )

                    ask_tools = get_ask_only_tools_openai()

                channel_type = getattr(self._channel, "channel_type", None) if self._channel else None
                ask_prompt = self._render_prompt_instructions(
                    context_bundle,
                    channel_type=channel_type,
                )

                if hasattr(self.client, "set_system_prompt"):
                    self.client.set_system_prompt(ask_prompt)
                elif hasattr(self.client, "_agent_system_prompt"):
                    self.client._agent_system_prompt = ask_prompt

                if hasattr(self.client, "_native_tools"):
                    self.client._native_tools = ask_tools

                self.client._ask_tier_native = True
                _ctx_ask_tier_active.set(True)
                logger.debug("ASK fallback: native mode active")
                return
            except ImportError as exc:
                logger.debug(
                    "ASK fallback: native mode unavailable (%s), falling through to JSON-in-prompt",
                    exc,
                )

        ask_response_contract = (
            "For this direct-answer fallback, respond with a single JSON object.\n"
            'Answer: {"type": "answer", "answer": "<plain response>", "continue_listening": false}\n'
            'Question: {"type": "answer", "answer": "<one focused question>", "continue_listening": true}\n'
            'Ignore ambient speech only when clearly not directed at Viola: {"type": "ignore", '
            '"reason": "<why>", "continue_listening": false}\n'
            "Return raw JSON only."
        )
        channel_type = getattr(self._channel, "channel_type", None) if self._channel else None
        ask_prompt = self._render_prompt_instructions(
            context_bundle,
            channel_type=channel_type,
            response_contract=ask_response_contract,
        )

        if hasattr(self.client, "set_system_prompt"):
            self.client.set_system_prompt(ask_prompt)
        elif hasattr(self.client, "_agent_system_prompt"):
            self.client._agent_system_prompt = ask_prompt

        _ctx_ask_tier_active.set(True)
        logger.debug("ASK fallback: JSON-in-prompt mode active")

    async def _maybe_use_agent_prompt(self, context_bundle: PromptFrameBundle, user_text: str = "") -> None:
        """Set up agent system prompt if agent mode is enabled and tools are registered.

        For providers with ``SUPPORTS_NATIVE_TOOLS`` (e.g. Anthropic), the tool
        schemas are passed via the API ``tools`` parameter instead of being
        embedded in the system prompt.  The system prompt is kept clean â€”
        behavioural instructions only.

        Also computes the tiered model override: when agent mode is active and
        the user has configured VIOLA_USER_MODEL, that model will be used for
        the LLM call instead of the provider's default.
        """
        from config.settings import settings

        # Reset model override at the start of each request cycle
        _ctx_agent_model_override.set(None)

        # Only skip agent mode when a forced ASK fallback is already active.
        if _ctx_ask_tier_active.get():
            return

        if not getattr(settings, "agent_enabled", False):
            return

        if not self._agent_mode_enabled_by_user_setting():
            return

        try:
            hub = await self._ensure_mcp_hub()
            if hub is None:
                return

            # Read capability tier from user settings for permission gating.
            try:
                from ui.settings_manager import get_settings_manager

                _sm = get_settings_manager()
                agent_autonomy = _sm.get("agent_autonomy", "symphony")
            except Exception:
                logger.exception("Agent prompt setup skipped because user settings could not be loaded")
                return

            tools_list = hub.list_tools(
                tier=agent_autonomy,
                interactive=self._channel is not None,
            )
            if not tools_list:
                return

            # Classifier removed (2026-04-09).  task_category is always
            # "general" â€” the model decides what to do from tool descriptions.
            task_category = "general"

            # Store on self so process_request() can propagate it into the
            # result dict for all response paths (answer, tool_call).
            _ctx_last_task_category.set(task_category)

            # API registry pre-check removed (2026-04-09).  The classifier
            # always returns "general" now, so the category gate never fires.
            # The agent can still call check_api_registry as a tool if needed.

            channel_type = getattr(self._channel, "channel_type", "none") if self._channel is not None else "none"
            interactive_tools = self._channel is not None
            request_user_id = _ctx_user_id.get()
            if not request_user_id:
                raise ValueError("Agent tool routing requires explicit user_id")

            # Detect whether a voice session is active (desktop wake or browser
            # voice).  When True the prompt builder injects voice conversation
            # rules (acknowledgment discipline, voice-native language, etc.).
            _voice_session_active = channel_type == "voice"

            # Check if any external delegation providers are connected
            from intent.tools.delegation import _get_available_providers

            _has_delegation = len(_get_available_providers()) > 0

            # Branch: native tools vs JSON-in-prompt
            use_native = getattr(self.client, "SUPPORTS_NATIVE_TOOLS", False)

            # Task classifiers/focused tool sets are retired. The only live
            # visibility filters are capability tier, runtime-hidden tools, and
            # channel interactivity (ask_user unavailable without a channel).

            # Wait for browser server to finish deferred initialization so
            # browser tools appear in the tool list the model receives.
            # Typically completes in ~2s after hub init â€” without this wait
            # the model can't navigate to pages when web_search is insufficient.
            if self._browser_init_task is not None:
                try:
                    await asyncio.wait_for(self._browser_init_task, timeout=30.0)
                except TimeoutError:
                    logger.warning("Browser server init timed out (30s) â€” proceeding without browser tools")
                except Exception:
                    logger.debug("Browser init task already completed or failed")

            if use_native:
                # Defer the large MCP catalog. The provider sees discovery
                # tools first; tool_search loads selected schemas on demand.
                if hasattr(hub, "get_deferred_tool_pool"):
                    deferred_pool = hub.get_deferred_tool_pool(
                        tier=agent_autonomy,
                        interactive=interactive_tools,
                        user_id=request_user_id,
                    )
                else:
                    from intent.tools.deferred_tool_schemas import (
                        list_tools_deferred as _list_tools_deferred,
                    )

                    deferred_pool = _list_tools_deferred(
                        hub,
                        tier=agent_autonomy,
                        interactive=interactive_tools,
                        user_id=request_user_id,
                    )
                from intent.tools.tool_search import (
                    set_deferred_tool_pool as _set_deferred_tool_pool,
                )

                _set_deferred_tool_pool(deferred_pool)
                visible_tools = list(deferred_pool.visible_tools)
                all_visible_tools = hub.list_tools(
                    tier=agent_autonomy,
                    interactive=interactive_tools,
                )
                # Pass actual tool count to executor for accurate step log reporting
                self._actual_tool_count = len(visible_tools)

                native_tool_format = getattr(self.client, "NATIVE_TOOL_FORMAT", "mcp")
                if native_tool_format == "anthropic":
                    from services.llm.providers.anthropic_provider import (
                        _mcp_tools_to_anthropic,
                    )

                    native_tools = _mcp_tools_to_anthropic(visible_tools)
                else:
                    native_tools = visible_tools

                full_native_tools = list(native_tools)

                # Natural loop termination: do NOT inject answer/ignore
                # pseudo-tools in native agent mode.  When the LLM outputs
                # text without tool calls, the provider returns
                # {"type": "answer"} and the loop terminates naturally â€”
                # matching Browser Use, OpenAI Agents SDK, and every other
                # agentic framework.  The answer pseudo-tool confused the
                # model into calling it for mid-task status updates, killing
                # the task prematurely (evidence: task ede2ef625302).
                # The _looks_mid_task heuristic on the text-only path
                # handles nudging if the LLM tries to narrate instead of act.

                # CHAN-R2: tell the model which delivery medium it's on so
                # it adjusts tone / length / format (voice is spoken aloud,
                # Discord supports markdown, phone is latency-bound, etc.).
                # Falls back to voice framing when no channel is bound
                # (tests, direct pipeline.process() calls without a channel).
                agent_channel_type = getattr(self._channel, "channel_type", None) if self._channel else None
                agent_prompt_bundle = build_provider_prompt_bundle(
                    context_bundle=context_bundle,
                    native_tools=True,
                    custom_instructions=self._context_builder.get_custom_instructions(),
                    channel_type=agent_channel_type,
                    session_id=self._current_session_id(),
                )
                deferred_tools_text = format_deferred_tools_block(deferred_pool.deferred_refs)
                if deferred_tools_text:
                    agent_prompt_bundle = append_system_text(
                        agent_prompt_bundle,
                        deferred_tools_text,
                        origin="deferred_tools",
                    )

                rendered_agent_prompt = render_for_openai_responses(agent_prompt_bundle)
                agent_prompt_preview = str(rendered_agent_prompt.get("instructions") or "")
                logger.info(
                    "Agent prompt bundle: category=%s, tokens=%d",
                    task_category,
                    len(agent_prompt_preview) // 4,
                )

                request_tool_surface = None
                if hasattr(hub, "build_tool_surface"):
                    request_tool_surface = hub.build_tool_surface(
                        tier=agent_autonomy,
                        interactive=interactive_tools,
                        provider_native=full_native_tools,
                        step_log_visible=all_visible_tools,
                    )
                self._set_request_agent_surface(
                    native_tools=full_native_tools,
                    tool_surface=request_tool_surface,
                    prompt_bundle=agent_prompt_bundle,
                    deferred_tool_pool=deferred_pool,
                    prompt_instructions_preview=agent_prompt_preview,
                )
                # Always tool_choice="auto" â€” the model decides whether to
                # use tools.  No more "required" workaround per category.
                self.client._agent_tool_choice = "auto"
                _provider = getattr(self.client, "_provider", None)
                if _provider is not None:
                    _provider._agent_tool_choice = "auto"

                if hasattr(self.client, "_preferred_first_tool"):
                    self.client._preferred_first_tool = None

                logger.debug(
                    "Agent mode (native tools): visible=%d/%d tools (category=%s), prompt_len=%d",
                    len(full_native_tools),
                    len(tools_list),
                    task_category,
                    len(agent_prompt_preview),
                )
            else:
                # JSON-in-prompt path (OpenAI-compatible and others)
                if hasattr(hub, "get_deferred_tool_pool"):
                    deferred_pool = hub.get_deferred_tool_pool(
                        tier=agent_autonomy,
                        interactive=interactive_tools,
                        user_id=request_user_id,
                    )
                else:
                    from intent.tools.deferred_tool_schemas import (
                        list_tools_deferred as _list_tools_deferred,
                    )

                    deferred_pool = _list_tools_deferred(
                        hub,
                        tier=agent_autonomy,
                        interactive=interactive_tools,
                        user_id=request_user_id,
                    )
                from intent.tools.tool_search import (
                    set_deferred_tool_pool as _set_deferred_tool_pool,
                )

                _set_deferred_tool_pool(deferred_pool)
                text_visible_tools = list(deferred_pool.visible_tools)
                schema_text = hub.get_tool_schemas(
                    tier=agent_autonomy,
                    interactive=interactive_tools,
                    user_id=request_user_id,
                )

                if not schema_text:
                    return

                # NOTE: _split_tool_schemas was removed â€” it was dead code (all
                # production providers set SUPPORTS_NATIVE_TOOLS=True, so the
                # text path is never reached).  The parser was also broken
                # (naive line-by-line split can't handle multi-line schemas).
                # CHAN-R2 (text-tools path mirror): same channel-awareness
                # as the native-tools path above. Without this, text-only
                # providers (JSON-in-prompt) emit voice-framed responses
                # on messaging channels.
                _text_channel_type = getattr(self._channel, "channel_type", None) if self._channel else None
                agent_prompt_bundle = build_provider_prompt_bundle(
                    context_bundle=context_bundle,
                    schema_text=schema_text,
                    native_tools=False,
                    custom_instructions=self._context_builder.get_custom_instructions(),
                    channel_type=_text_channel_type,
                    session_id=self._current_session_id(),
                )

                rendered_agent_prompt = render_for_openai_responses(agent_prompt_bundle)
                agent_prompt_preview = str(rendered_agent_prompt.get("instructions") or "")
                logger.info(
                    "Agent prompt bundle: category=%s, tokens=%d",
                    task_category,
                    len(agent_prompt_preview) // 4,
                )
                request_tool_surface = None
                request_visible_tools = None
                if hasattr(hub, "build_tool_surface"):
                    all_visible_tools = hub.list_tools(
                        tier=agent_autonomy,
                        interactive=interactive_tools,
                    )
                    request_visible_tools = text_visible_tools
                    request_tool_surface = hub.build_tool_surface(
                        tier=agent_autonomy,
                        interactive=interactive_tools,
                        provider_native=request_visible_tools,
                        step_log_visible=all_visible_tools,
                    )
                    self.client._tool_surface = request_tool_surface
                self._set_request_agent_surface(
                    native_tools=request_visible_tools,
                    tool_surface=request_tool_surface,
                    prompt_bundle=agent_prompt_bundle,
                    deferred_tool_pool=deferred_pool,
                    prompt_instructions_preview=agent_prompt_preview,
                )

                logger.debug(
                    "Agent mode (JSON-in-prompt): category=%s, schema_len=%d, prompt_len=%d",
                    task_category,
                    len(schema_text),
                    len(agent_prompt_preview),
                )

            # Compute tiered model override for agent requests
            _ctx_agent_model_override.set(
                self._get_model_for_request(
                    is_agent_mode=True,
                )
            )

        except Exception:
            logger.exception("Agent prompt setup FAILED â€” ALL tasks will bypass agent mode")
            raise  # Don't silently swallow â€” let caller handle the failure

    async def _notify_task_completion(
        self,
        summary: str,
        task_description: str,
        success: bool,
        user_id: str | None = None,
    ) -> None:
        """Notify user of agent task completion via messaging and WebSocket.

        Supplements the existing TTS speech with cross-channel notifications
        so the user sees the result even if they left the room.
        """
        # 1. WebSocket broadcast for React UI toast
        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is not None:
                broadcast_user_id = user_id or _ctx_user_id.get(None)
                await hub.broadcast(
                    "task_complete",
                    {
                        "summary": summary[:500],
                        "task": task_description[:200],
                        "success": success,
                    },
                    user_id=broadcast_user_id,
                    force=True,
                )
        except Exception as exc:
            logger.debug("Task completion WS broadcast failed: %s", exc)

        # 2. Messaging channel notification (Telegram, Discord, etc.)
        try:
            from ui.server import get_app

            app = get_app()
            if app is not None:
                router = getattr(getattr(app, "state", None), "message_router", None)
                if router is not None:
                    for listener in getattr(router, "_listeners", []):
                        channels = getattr(listener, "_channels", {})
                        for channel in channels.values():
                            try:
                                prefix = "Task complete" if success else "Task failed"
                                await channel.send("%s: %s" % (prefix, summary[:1000]))
                            except Exception:
                                logger.debug("Messaging channel send failed, continuing")
                            break  # Only notify the first (owner) channel per listener
                        break  # Only notify via the first listener with channels
        except Exception as exc:
            logger.debug("Task completion messaging notification failed: %s", exc)

    # ------------------------------------------------------------------
    # Task Resume from Checkpoint
    # ------------------------------------------------------------------

    async def _try_resume_checkpoint(
        self,
        checkpoint_id: str,
        user_id: str,
        *,
        resume_user_reply: str | None = None,
    ) -> ProcessResultDict | None:
        """Attempt to resume a checkpointed task from where it left off.

        Uses ``AgentExecutor.resume_from_checkpoint`` to restore the
        full LLM message history and continue the agent loop, instead
        of restarting the task from scratch.

        Returns a ``ProcessResultDict`` on success, or ``None`` if the
        checkpoint cannot be loaded (caller should fall through to the
        normal answer path).
        """
        try:
            from intent.task_checkpoint import load_checkpoint

            checkpoint = load_checkpoint(checkpoint_id, user_id=user_id)
            if checkpoint is None:
                logger.warning(
                    "Cannot resume: checkpoint %s not found or corrupt",
                    checkpoint_id,
                )
                return None

            task_text = checkpoint.task_description
            if not task_text:
                logger.warning(
                    "Cannot resume: checkpoint %s has empty task_description",
                    checkpoint_id,
                )
                return None
            checkpoint_context = getattr(checkpoint, "context", None)
            if not isinstance(checkpoint_context, dict):
                checkpoint_context = {}
            checkpoint_metadata = getattr(checkpoint, "metadata", None)
            if not isinstance(checkpoint_metadata, dict):
                checkpoint_metadata = {}
            resume_session_id = (
                str(
                    checkpoint_context.get("session_id")
                    or checkpoint_metadata.get("session_id")
                    or self._current_session_id()
                    or ""
                ).strip()
                or None
            )
            if resume_session_id is not None:
                await self._record_resume_entry_frame(
                    session_id=resume_session_id,
                    checkpoint_id=checkpoint_id,
                    user_id=user_id,
                    channel=self._request_channel_type(),
                )

            # If the checkpoint has saved LLM messages, use the proper
            # resume path that restores conversation history.
            if checkpoint.llm_messages or checkpoint.llm_continuity:
                logger.info(
                    "Resuming checkpoint %s with %d saved messages, continuity=%s: %s (step %d)",
                    checkpoint_id,
                    len(checkpoint.llm_messages),
                    bool(checkpoint.llm_continuity),
                    task_text,
                    checkpoint.step_index,
                )
                from intent.agent_executor import AgentExecutor
                from intent.approval import ApprovalManager

                mcp_hub = self._mcp_hub
                if mcp_hub is None:
                    logger.warning("Cannot resume: MCP hub not available")
                    return None

                approval = getattr(self, "_hub_approval_mgr", None)
                if approval is None:
                    approval = ApprovalManager(
                        channel=self._channel,
                        voice_pipeline=self._voice_pipeline,
                        tts_speaker=self._tts_speaker,
                    )

                # Pre-transition the checkpoint OUT of waiting_for_user BEFORE
                # building the resume prompt bundle. The resumed agent loop IS
                # the active resume — its system_context must not include a
                # GATE_STATE frame from build_pending_gate_frames telling it to
                # call resume_signature_gate (which would re-enter this path,
                # fail with "No pending signature gate", and revoke the override
                # the outer resume call just granted). Stash the original gate
                # type on the checkpoint context so AgentExecutor.resume_from_
                # checkpoint can still set up the signature/payment override
                # token tied to this resume.
                pre_resume_gate_type = _resume_gate_type_from_checkpoint_context(checkpoint_context)
                pre_resume_gate_context: dict[str, Any] = {}
                pretransitioned_gate_type = ""
                if checkpoint.status == "waiting_for_user" and pre_resume_gate_type in {
                    "signature",
                    "payment",
                }:
                    from datetime import UTC, datetime as _dt

                    from intent.task_checkpoint import save_checkpoint as _save_cp

                    checkpoint.status = "in_progress"
                    pre_resume_gate_context = dict(checkpoint_context)
                    checkpoint.context["_pre_resume_gate_type"] = pre_resume_gate_type
                    checkpoint.context.pop("pending_gate_type", None)
                    checkpoint.updated_at = _dt.now(UTC).isoformat()
                    _save_cp(checkpoint, user_id=user_id)
                    pretransitioned_gate_type = pre_resume_gate_type

                (
                    request_native_tools,
                    request_tool_surface,
                    request_prompt_bundle,
                ) = await self._prepare_resume_agent_surface(
                    task_text=task_text,
                    user_id=user_id,
                    force_fresh_prompt=True,
                    consumed_gate_type=pre_resume_gate_type,
                )

                agent_result = await AgentExecutor.resume_from_checkpoint(
                    task_id=checkpoint_id,
                    llm_caller=self.client,
                    approval_manager=approval,
                    mcp_hub=mcp_hub,
                    tts_speaker=self._tts_speaker,
                    channel=self._channel,
                    user_id=user_id,
                    session_id=resume_session_id,
                    resume_user_reply=resume_user_reply,
                    native_tools=request_native_tools,
                    tool_surface=request_tool_surface,
                    context_bundle=request_prompt_bundle,
                    hook_registry=self.hook_registry,
                    hook_settings_runner=getattr(self, "hook_settings_runner", None),
                )

                # Pretransition rollback: if we pretransitioned the checkpoint
                # state to in_progress + stashed _pre_resume_gate_type, but the
                # resumed loop failed before any gate-consuming browser/payment
                # action ran, restore waiting_for_user so the agent's retry of
                # resume_signature_gate finds a waiting checkpoint. The first
                # provider call can increment iterations_used before failing, so
                # tool execution evidence is the safety boundary here.
                if (
                    pretransitioned_gate_type in {"signature", "payment"}
                    and agent_result is not None
                    and _resume_failed_before_gate_consuming_action(agent_result, pretransitioned_gate_type)
                ):
                    from datetime import UTC as _UTC2, datetime as _dt2

                    from intent.task_checkpoint import (
                        load_checkpoint as _load_cp2,
                        save_checkpoint as _save_cp2,
                    )

                    _rb = _load_cp2(checkpoint_id, user_id=user_id)
                    if _rb is not None and _rb.status == "in_progress":
                        _rb.status = "waiting_for_user"
                        _rb.context["pending_gate_type"] = pretransitioned_gate_type
                        for _context_key in (
                            "pending_question",
                            "signature_gate_page_url",
                            "gate_page_url",
                            "signature_token",
                            "token",
                            "confirm_token",
                            "confirmation_url",
                            "payment_order_summary",
                            "payment_page_ref",
                            "payment_page_url",
                            "payment_merchant_url",
                        ):
                            if _context_key in pre_resume_gate_context and not _rb.context.get(_context_key):
                                _rb.context[_context_key] = pre_resume_gate_context[_context_key]
                        _rb.context.pop("_pre_resume_gate_type", None)
                        _rb.updated_at = _dt2.now(_UTC2).isoformat()
                        _save_cp2(_rb, user_id=user_id)
                        logger.info(
                            "Rolled back pretransition for checkpoint %s after resume failed before gate-consuming action",
                            checkpoint_id,
                        )

                result: ProcessResultDict = {
                    "ok": agent_result.ok,
                    "success": agent_result.ok,
                    "commands_executed": [],
                    "data": {
                        "command_results": {},
                        "answer": agent_result.answer or "",
                        "tools_called": agent_result.tools_called,
                        "payment_gate": bool(getattr(agent_result, "payment_gate", False)),
                        "signature_gate": bool(getattr(agent_result, "signature_gate", False)),
                        "confirmation_url": getattr(agent_result, "confirmation_url", None),
                    },
                    "intent": "agent",
                    "message": agent_result.answer or "",
                    "response": agent_result.answer or "",
                }
                # A resumed task can hit the cap too (candidate C-077).
                _propagate_cap_state(result, agent_result)
                await self._record_exchange(
                    task_text,
                    agent_result.answer or "",
                    user_id,
                    intent=infer_action_intent(tools_called=agent_result.tools_called or []),
                    params={
                        "tools_called": agent_result.tools_called or [],
                        "resumed_checkpoint": checkpoint_id,
                    },
                )
                return result

            # No saved messages â€” fall back to re-processing the task
            # from scratch (still better than doing nothing).
            logger.info(
                "Resuming checkpoint %s (no saved messages, restarting): %s",
                checkpoint_id,
                task_text,
            )
            return await self.process_request(task_text, user_key=user_id)
        except Exception:
            logger.exception("Failed to resume checkpoint %s", checkpoint_id)
            return None

    async def _prepare_resume_agent_surface(
        self,
        *,
        task_text: str,
        user_id: str,
        force_fresh_prompt: bool = False,
        consumed_gate_type: str | None = None,
    ) -> tuple[list[dict[str, Any]] | None, Any | None, PromptFrameBundle | None]:
        """Rebuild the request-scoped native tool surface for a checkpoint resume."""
        native_tools = self._get_request_native_tools()
        tool_surface = self._get_request_tool_surface()
        prompt_bundle = None if force_fresh_prompt else self._get_request_agent_prompt_bundle()

        if native_tools is not None and tool_surface is not None and prompt_bundle is not None:
            return native_tools, tool_surface, prompt_bundle

        if self.client is None or not getattr(self.client, "SUPPORTS_NATIVE_TOOLS", False):
            return native_tools, tool_surface, prompt_bundle

        if not self._agent_mode_enabled_by_user_setting():
            return native_tools, tool_surface, prompt_bundle

        hub = self._mcp_hub
        if hub is None:
            return native_tools, tool_surface, prompt_bundle

        try:
            from ui.settings_manager import get_settings_manager

            agent_autonomy = get_settings_manager().get("agent_autonomy", "symphony")
        except Exception:
            agent_autonomy = "symphony"

        interactive_tools = self._channel is not None
        if self._browser_init_task is not None:
            try:
                await asyncio.wait_for(self._browser_init_task, timeout=30.0)
            except TimeoutError:
                logger.warning("Browser server init timed out (30s) during checkpoint resume")
            except Exception:
                logger.debug("Browser init task already completed or failed")

        deferred_pool = None
        try:
            if hasattr(hub, "get_deferred_tool_pool"):
                deferred_pool = hub.get_deferred_tool_pool(
                    tier=agent_autonomy,
                    interactive=interactive_tools,
                    user_id=user_id,
                )
            else:
                from intent.tools.deferred_tool_schemas import (
                    list_tools_deferred as _list_tools_deferred,
                )

                deferred_pool = _list_tools_deferred(
                    hub,
                    tier=agent_autonomy,
                    interactive=interactive_tools,
                    user_id=user_id,
                )
            from intent.tools.tool_search import (
                set_deferred_tool_pool as _set_deferred_tool_pool,
            )

            _set_deferred_tool_pool(deferred_pool)
            visible_tools = list(deferred_pool.visible_tools)
            all_visible_tools = hub.list_tools(
                tier=agent_autonomy,
                interactive=interactive_tools,
            )
        except TypeError:
            visible_tools = hub.list_tools()
            all_visible_tools = visible_tools
        if not visible_tools:
            return native_tools, tool_surface, prompt_bundle

        if native_tools is None:
            native_tool_format = getattr(self.client, "NATIVE_TOOL_FORMAT", "mcp")
            if native_tool_format == "anthropic":
                from services.llm.providers.anthropic_provider import (
                    _mcp_tools_to_anthropic,
                )

                native_tools = list(_mcp_tools_to_anthropic(visible_tools))
            else:
                native_tools = list(visible_tools)

        if tool_surface is None and hasattr(hub, "build_tool_surface"):
            tool_surface = hub.build_tool_surface(
                tier=agent_autonomy,
                interactive=interactive_tools,
                provider_native=native_tools,
                step_log_visible=all_visible_tools,
            )

        if prompt_bundle is None:
            channel_type = getattr(self._channel, "channel_type", None) if self._channel else None
            try:
                resume_bundle = self._context_builder.build_frames(
                    user_id=user_id,
                    user_text=task_text,
                    channel_type=channel_type or "none",
                )
            except Exception:
                logger.debug(
                    "Resume context build failed; using task description only",
                    exc_info=True,
                )
                resume_bundle = runtime_context_bundle("RESUMING TASK: %s" % task_text, origin="task_resume")
            prompt_bundle = build_provider_prompt_bundle(
                context_bundle=resume_bundle,
                native_tools=True,
                custom_instructions=self._context_builder.get_custom_instructions(),
                channel_type=channel_type,
                session_id=self._current_session_id(),
            )
            deferred_tools_text = (
                format_deferred_tools_block(deferred_pool.deferred_refs) if deferred_pool is not None else ""
            )
            if deferred_tools_text:
                prompt_bundle = append_system_text(
                    prompt_bundle,
                    deferred_tools_text,
                    origin="deferred_tools",
                )
        prompt_preview = ""
        if prompt_bundle is not None:
            prompt_preview = str(render_for_openai_responses(prompt_bundle).get("instructions") or "")

        native_tools, tool_surface = _filter_active_resume_surface(
            native_tools,
            tool_surface,
            consumed_gate_type,
        )

        self._set_request_agent_surface(
            native_tools=native_tools,
            tool_surface=tool_surface,
            prompt_bundle=prompt_bundle,
            deferred_tool_pool=deferred_pool,
            prompt_instructions_preview=prompt_preview,
        )
        return native_tools, tool_surface, prompt_bundle

    async def _handle_agent_loop(
        self,
        text: str,
        response: dict[str, Any] | None,
        context_bundle: PromptFrameBundle,
        user_id: str,
        request_context: dict[str, Any] | None = None,
    ) -> ProcessResultDict | None:
        """Run the agent executor for a user request.

        Args:
            response: The LLM's first-turn response dict, or ``None``
                when the caller wants the agent executor to make the
                first LLM call itself (direct agent path).

        Returns a ProcessResultDict if the agent completes, or None to fall through.
        """
        from config.settings import settings

        if not self._agent_mode_enabled_by_user_setting():
            return None

        # Classifier removed (2026-04-09).  task_category is always "general".
        task_category = "general"

        # --- Preflight gate: catch dead-obvious config issues ---
        from services.agent.preflight import AgentPreflightValidator

        preflight = AgentPreflightValidator().validate(
            settings=settings,
            provider=self.client,
        )
        if not preflight.passed:
            logger.warning(
                "Agent preflight failed: %s â€” %s",
                preflight.blocker,
                preflight.developer_detail,
            )
            return None

        try:
            from intent.agent_executor import AgentExecutor, _gate_state_record_params
            from intent.approval import ApprovalManager
            from intent.tool_types import ToolCall
            from services.conversation.session_identity import make_user_id

            # Direct agent path: response is None â€” executor makes the
            # first LLM call.  tool_call stays None.
            tool_call: ToolCall | None = None
            if response is not None:
                tool_call = ToolCall.from_dict(response)
                if tool_call is None:
                    logger.warning("Malformed tool_call in LLM response: %s", response)
                    return None

            # Short-circuit: if the LLM's initial tool_call is the
            # pseudo-tool "answer", it means the model wants to respond
            # with text, not invoke a real MCP tool.  Return the answer
            # directly without spinning up the full AgentExecutor.
            if tool_call is not None and tool_call.tool == "answer":
                answer_text = tool_call.args.get("answer", tool_call.args.get("text", ""))
                if isinstance(answer_text, str) and answer_text.strip():
                    logger.info(
                        "Agent loop short-circuit: LLM called 'answer' tool â€” returning as response (len=%d)",
                        len(answer_text),
                    )
                    cleaned = repair_mojibake(answer_text)
                    result: ProcessResultDict = {
                        "ok": True,
                        "success": True,
                        "commands_executed": [],
                        "data": {"command_results": {}, "answer": cleaned},
                        "intent": "answer",
                        "message": cleaned,
                        "response": cleaned,
                        "continue_listening": bool(tool_call.args.get("continue_listening", False)),
                        "task_category": task_category,
                    }
                    await self._record_exchange(text, cleaned, user_id, intent="answer")
                    return result

            mcp_hub = self._mcp_hub
            if mcp_hub is None:
                logger.warning("Agent mode requires MCP hub but hub is not available")
                return None

            # Reuse the ApprovalManager that the hub's ApprovalBridge
            # checks â€” otherwise pre-approved tools are set on a
            # different instance and the bridge never sees them.
            approval = getattr(self, "_hub_approval_mgr", None)
            if approval is None:
                approval = ApprovalManager(
                    channel=self._channel,
                    voice_pipeline=self._voice_pipeline,
                    tts_speaker=self._tts_speaker,
                )

            # Get overlay controller for display integration
            overlay = None
            try:
                from services.browser_overlay_controller import get_overlay_controller

                overlay = get_overlay_controller()
            except Exception:
                logger.debug("Overlay controller unavailable, continuing without it")

            agent_prompt_bundle = self._get_request_agent_prompt_bundle() or context_bundle
            executor = AgentExecutor(
                llm_caller=self.client,
                approval_manager=approval,
                mcp_hub=mcp_hub,
                tts_speaker=self._tts_speaker,
                channel=self._channel,
                total_timeout=getattr(settings, "agent_timeout_seconds", 60.0),
                tool_timeout=getattr(settings, "agent_tool_timeout_seconds", 30.0),
                overlay_controller=overlay,
                user_id=make_user_id(user_id),
                model_override=_ctx_agent_model_override.get(),
                session_id=self._current_session_id(request_context),
                native_tools=self._get_request_native_tools(),
                tool_surface=self._get_request_tool_surface(),
                context_bundle=agent_prompt_bundle,
                conversation_state_manager=self._conversation_state_manager,
                hook_registry=self.hook_registry,
                hook_settings_runner=getattr(self, "hook_settings_runner", None),
            )
            executor._request_deferred_tool_pool = self._get_request_deferred_tool_pool()

            # B6/C2: Register cache invalidation hook so compaction clears memoized context
            if self._conversation_state_manager is not None:
                self._conversation_state_manager.register_post_compaction_hook(executor.invalidate_context_cache)

            _agent_prompt_text = ""
            if agent_prompt_bundle is not None:
                try:
                    _agent_prompt_text = str(render_for_openai_responses(agent_prompt_bundle).get("instructions") or "")
                except Exception:
                    logger.exception("Failed to render agent prompt bundle to text")
            agent_result = await executor.run(
                text,
                tool_call,
                _agent_prompt_text,
                task_category_override=task_category,
                initial_response=response,
            )

            # Map AgentResult â†’ ProcessResultDict
            # "success" is required by IntentBridge's _normalize() to
            # recognize the result as valid (it checks result.get("success")).
            result: ProcessResultDict = {
                "ok": agent_result.ok,
                "success": agent_result.ok,
                "commands_executed": [],
                "data": {
                    "command_results": {},
                    # Propagate tools_called so the response quality gate can
                    # distinguish "agent answered after calling real MCP tools"
                    # from "agent hallucinated an action with no tool invoked".
                    "tools_called": agent_result.tools_called or [],
                },
            }
            _propagate_cap_state(result, agent_result)
            _ui_action_payload = (
                agent_result.params.get("ui_action_payload") if isinstance(agent_result.params, dict) else None
            )
            if isinstance(_ui_action_payload, dict) and _ui_action_payload.get("ui_action"):
                result["data"].update(_ui_action_payload)
                result["data"]["command_results"]["routed"] = {
                    "message": agent_result.answer or "",
                    "data": _ui_action_payload,
                }
            # Payment gate: agent stopped before committing money
            if agent_result.payment_gate:
                payment_msg = agent_result.answer or "Your order is ready for review."
                confirmation_url = getattr(agent_result, "confirmation_url", None)
                origin_channel = getattr(agent_result, "origin_channel", None)
                payment_card = _build_gate_card(
                    gate_kind="payment",
                    body=payment_msg,
                    confirmation_url=confirmation_url,
                    origin_channel=origin_channel,
                )
                link_delivery = await _dispatch_payment_gate_card(
                    card=payment_card,
                    confirmation_url=confirmation_url,
                    origin_channel=origin_channel,
                    channel=self._channel,
                    user_id=user_id,
                    existing_delivery=getattr(agent_result, "confirmation_link_delivery", None),
                )
                if link_delivery:
                    payment_card["confirmation_link_delivery"] = link_delivery
                    result["data"]["confirmation_link_delivery"] = link_delivery
                    if _payment_link_delivery_reached_user(link_delivery):
                        payment_card["confirmation_link_dispatched"] = True
                result["intent"] = "answer"
                result["message"] = payment_msg
                result["data"]["answer"] = payment_msg
                result["data"]["payment_gate"] = True
                result["data"]["card"] = payment_card
                result["response"] = payment_msg
                result["continue_listening"] = False
                logger.info(
                    "Agent payment gate triggered after %d iterations",
                    agent_result.iterations_used,
                )
                # Speak TTS prompt for payment review
                tts_prompt = "Your order is ready for review. Please confirm payment in the browser."
                if self._tts_speaker is not None:
                    try:
                        speak_fn = getattr(self._tts_speaker, "speak", None)
                        if callable(speak_fn):
                            await speak_fn(tts_prompt)
                    except Exception as tts_exc:
                        logger.debug("Payment gate TTS failed: %s", tts_exc)
                elif self._channel is not None:
                    try:
                        await self._channel.send(tts_prompt)
                    except Exception as ch_exc:
                        logger.debug("Payment gate channel send failed: %s", ch_exc)

                payment_intent = infer_action_intent(tools_called=agent_result.tools_called or [])
                if payment_intent == "general.action":
                    payment_intent = "gate.payment"
                payment_params: dict[str, object] = (
                    dict(agent_result.params) if isinstance(agent_result.params, dict) else {}
                )
                if payment_params.get("frame_kind") != FrameKind.SYSTEM_REMINDER.value:
                    payment_params.update(
                        _gate_state_record_params(
                            gate_type="payment",
                            final_answer=payment_msg,
                            status="awaiting_confirmation",
                            task_id=str(payment_params.get("task_id") or ""),
                            confirmation_url=confirmation_url,
                            last_blocker="payment_gate_final_answer",
                            required_fields=["payment_confirmation"],
                            collected_fields={
                                "confirmation_url": confirmation_url or "",
                                "tools_called": agent_result.tools_called or [],
                            },
                        )
                    )
                payment_params.update(
                    {
                        "tools_called": agent_result.tools_called or [],
                        "payment_gate": True,
                        "iterations_used": agent_result.iterations_used,
                    }
                )
                self._maybe_record_action_recipe(
                    user_text=text,
                    assistant_text=payment_msg,
                    user_id=user_id,
                    ok=agent_result.ok,
                    tools_called=agent_result.tools_called or [],
                    payment_gate=True,
                )
                await self._record_exchange(
                    text,
                    payment_msg,
                    user_id,
                    intent=payment_intent,
                    params=payment_params,
                )
                return result

            if agent_result.signature_gate:
                signature_msg = agent_result.answer or (
                    "Signature review is waiting for your decision. Reply yes to sign and continue, or tell me what to change."
                )
                result["intent"] = "answer"
                result["message"] = signature_msg
                result["data"]["answer"] = signature_msg
                result["data"]["signature_gate"] = True
                result["data"]["card"] = _build_gate_card(
                    gate_kind="signature",
                    body=signature_msg,
                    confirmation_url=getattr(agent_result, "confirmation_url", None),
                    origin_channel=getattr(agent_result, "origin_channel", None),
                )
                result["response"] = signature_msg
                result["continue_listening"] = True
                logger.info(
                    "Agent signature gate triggered after %d iterations",
                    agent_result.iterations_used,
                )
                tts_prompt = signature_msg
                if self._tts_speaker is not None:
                    try:
                        speak_fn = getattr(self._tts_speaker, "speak", None)
                        if callable(speak_fn):
                            await speak_fn(tts_prompt)
                    except Exception as tts_exc:
                        logger.debug("Signature gate TTS failed: %s", tts_exc)
                elif self._channel is not None:
                    try:
                        await self._channel.send(tts_prompt)
                    except Exception as ch_exc:
                        logger.debug("Signature gate channel send failed: %s", ch_exc)

                signature_intent = infer_action_intent(tools_called=agent_result.tools_called or [])
                if signature_intent == "general.action":
                    signature_intent = "gate.signature"
                signature_params: dict[str, object] = (
                    dict(agent_result.params) if isinstance(agent_result.params, dict) else {}
                )
                if signature_params.get("frame_kind") != FrameKind.SYSTEM_REMINDER.value:
                    signature_params.update(
                        _gate_state_record_params(
                            gate_type="signature",
                            final_answer=signature_msg,
                            status="awaiting_signature",
                            task_id=str(signature_params.get("task_id") or ""),
                            confirmation_url=getattr(agent_result, "confirmation_url", None),
                            last_blocker="signature_gate_final_answer",
                            required_fields=["signature_decision"],
                            collected_fields={
                                "confirmation_url": getattr(agent_result, "confirmation_url", None) or "",
                                "tools_called": agent_result.tools_called or [],
                            },
                        )
                    )
                signature_params.update(
                    {
                        "tools_called": agent_result.tools_called or [],
                        "signature_gate": True,
                        "iterations_used": agent_result.iterations_used,
                    }
                )
                self._maybe_record_action_recipe(
                    user_text=text,
                    assistant_text=signature_msg,
                    user_id=user_id,
                    ok=agent_result.ok,
                    tools_called=agent_result.tools_called or [],
                    signature_gate=True,
                )
                await self._record_exchange(
                    text,
                    signature_msg,
                    user_id,
                    intent=signature_intent,
                    params=signature_params,
                )
                return result

            if agent_result.command:
                # Agent ended with a command (e.g., play_music)
                executed_entry: CommandExecutedDict = {
                    "command": agent_result.command,
                    "status": "ok",
                    "message": agent_result.answer or "",
                }
                result["commands_executed"].append(executed_entry)
                result["data"]["command_results"]["routed"] = {
                    "message": agent_result.answer or "",
                    "data": {
                        "command": agent_result.command,
                        "params": agent_result.params,
                    },
                }
                logger.info(
                    "Agent loop completed with command '%s' after %d iterations (tools: %s)",
                    agent_result.command,
                    agent_result.iterations_used,
                    agent_result.tools_called,
                )
            elif agent_result.answer:
                # Agent ended with an answer
                cleaned_answer = repair_mojibake(agent_result.answer)
                result["intent"] = "answer"
                result["message"] = cleaned_answer
                result["data"]["answer"] = cleaned_answer
                result["response"] = cleaned_answer
                logger.info(
                    "Agent loop completed with answer after %d iterations (tools: %s)",
                    agent_result.iterations_used,
                    agent_result.tools_called,
                )
            else:
                no_result = agent_result.params.get("no_result") if isinstance(agent_result.params, dict) else None
                if isinstance(no_result, dict):
                    error_state = agent_result.params.get("error_state")
                    if not isinstance(error_state, dict):
                        error_state = {"type": "ai_no_result", **no_result}
                    result["ok"] = False
                    result["success"] = False
                    result["intent"] = "ai_no_result"
                    result["data"]["no_result"] = no_result
                    result["data"]["error_state"] = error_state
                    result["data"]["tools_called"] = agent_result.tools_called or []
                    result["error"] = agent_result.error or "ai_no_result"
                else:
                    result["ok"] = False
                    result["data"]["error"] = agent_result.error or "Agent produced no result"

            # --- Self-diagnosis pipeline (only on failure) ---
            if not agent_result.ok and hasattr(executor, "_diagnostic_contexts") and executor._diagnostic_contexts:
                await self._run_self_diagnosis(
                    executor._diagnostic_contexts,
                    result,
                    user_id=user_id,
                )

            # Expose task_category in result so pipeline can preserve it
            # for follow-up routing (e.g. LLC consultâ†’execute stays "web").
            result["task_category"] = task_category

            # Determine continue_listening for agent results from STRUCTURAL
            # signals only — never by parsing the model's free-text answer.
            #   (1) The model's explicit continue_listening (its structured channel).
            #   (2) The model called ask_user — a structured tool signal that it is
            #       gathering input, so the mic stays open for the reply.
            # When neither structural signal is present we default to False rather
            # than regex-scanning the prose for "?", numbered options, or
            # clarification phrasing. Keyword-parsing the model's own reply to set a
            # conversation-control flag (and the old clarification-suppression
            # override that flipped the model's explicit True back to False) is the
            # boxing class CLAUDE.md forbids: the model owns this decision through
            # its structured continue_listening field. Claude Code TS likewise gates
            # turn continuation on structured message/tool/stop_reason state
            # (utils/queryHelpers.ts:isResultSuccessful), never on answer-text regex.
            if agent_result.continue_listening is not None:
                result["continue_listening"] = agent_result.continue_listening
            elif "ask_user" in (agent_result.tools_called or []):
                result["continue_listening"] = True
            else:
                result["continue_listening"] = False

            # Record exchange
            assistant_text = repair_mojibake(agent_result.answer or "") or agent_result.command or ""
            if assistant_text:
                record_intent = infer_action_intent(
                    command=agent_result.command,
                    tools_called=agent_result.tools_called or [],
                )
                record_params: dict[str, object] = {
                    "tools_called": agent_result.tools_called or [],
                    "iterations_used": agent_result.iterations_used,
                }
                if agent_result.command:
                    record_params["command"] = agent_result.command
                if isinstance(agent_result.params, dict) and agent_result.params:
                    record_params["command_params"] = agent_result.params
                self._maybe_record_action_recipe(
                    user_text=text,
                    assistant_text=assistant_text,
                    user_id=user_id,
                    ok=agent_result.ok,
                    command=agent_result.command,
                    command_params=(agent_result.params if isinstance(agent_result.params, dict) else None),
                    tools_called=agent_result.tools_called or [],
                )
                await self._record_exchange(
                    text,
                    assistant_text,
                    user_id,
                    intent=record_intent,
                    params=record_params,
                )

            # Notify via messaging and WebSocket (supplements TTS speech)
            await self._notify_task_completion(
                summary=repair_mojibake(agent_result.answer or agent_result.error or "Task completed"),
                task_description=text[:200],
                success=agent_result.ok,
                user_id=user_id,
            )

            return result

        except asyncio.CancelledError:
            # Propagate. Returning an answer here tells whoever cancelled us
            # that the turn finished, and for a relayed turn that caller is
            # asyncio.wait_for on the desktop's companion client: it hands the
            # returned dict back instead of raising TimeoutError, so the cloud
            # receives "That request was interrupted" as a completed desktop
            # answer and the desktop records it as a finished turn in its
            # idempotency ledger. This handler sits directly above
            # AgentExecutor.run, which re-raises for the same reason.
            logger.warning("Agent loop cancelled (CancelledError) for: %s", text[:80])
            raise
        except Exception as exc:
            logger.exception("Agent loop failed")
            # Best-effort self-diagnosis for unhandled exceptions
            if hasattr(exc, "__traceback__"):
                try:
                    from services.agent.diagnostic_context import DiagnosticContext

                    ctx = DiagnosticContext.capture_from_exception(
                        user_request=text,
                        exception=exc,
                        stage="agent_loop",
                        settings=settings,
                        provider=self.client,
                        mcp_hub=self._mcp_hub,
                        user_id=user_id,
                    )
                    diag_result = await self._run_self_diagnosis([ctx], None, user_id=user_id)
                    if diag_result is not None:
                        return diag_result
                except Exception:
                    logger.debug("Self-diagnosis failed for unhandled exception", exc_info=True)
            return None

    async def _run_self_diagnosis(
        self,
        contexts: list,
        result: dict | None,
        *,
        user_id: str | None = None,
    ) -> dict | None:
        """Run self-diagnosis pipeline on captured diagnostic contexts.

        Best-effort: never raises, never blocks the user response.

        Args:
            contexts: List of DiagnosticContext objects from the agent run.
            result: The ProcessResultDict to optionally enrich with diagnosis
                    (may be None for unhandled exceptions).

        Returns:
            When result is None and diagnosis succeeds, a minimal error result
            dict containing the user_explanation. Otherwise None.
        """
        if not contexts:
            return None

        try:
            from services.agent.bug_tickets import BugTicketStore
            from services.agent.self_diagnosis import SelfDiagnosisEngine

            # Provider-issue contexts (OpenAI 429, insufficient_quota, RateLimitError)
            # take priority over later contexts. The agent loop sometimes captures a
            # later RuntimeError (e.g. "Cannot synchronously wait on the shared
            # asyncio worker loop from itself") that overwrites the original quota
            # error when picking contexts[-1] alone. Without this preference,
            # self_diagnosis's heuristic runs on the wrong context, the
            # provider_api_key_quota branch never matches, and the user sees the
            # generic "Something went wrong" instead of the canonical
            # "Viola is experiencing high volume." message.
            def _is_provider_issue_context(c: object) -> bool:
                et = (getattr(c, "error_type", None) or "").lower()
                em = (getattr(c, "error_message", None) or "").lower()
                if "ratelimit" in et or "ratelimit" in em:
                    return True
                if "insufficient_quota" in em or "exceeded your current quota" in em:
                    return True
                if "429" in em:
                    return True
                return False

            provider_contexts = [c for c in contexts if _is_provider_issue_context(c)]
            ctx = provider_contexts[-1] if provider_contexts else contexts[-1]
            if user_id and not getattr(ctx, "user_id", None):
                ctx.user_id = user_id

            # Create diagnosis engine with the current LLM provider
            engine = SelfDiagnosisEngine(self.client)
            diagnosis = await engine.diagnose(ctx)

            # File bug ticket
            try:
                store = BugTicketStore()
                ticket = store.file_ticket(ctx, diagnosis, user_id=_ctx_user_id.get())
                logger.info(
                    "Self-diagnosis filed ticket #%d (category=%s, severity=%s, hits=%d)",
                    ticket.id,
                    diagnosis.category,
                    diagnosis.severity,
                    ticket.hit_count,
                )
            except Exception:
                logger.debug("Failed to file bug ticket", exc_info=True)

            # Enrich user-facing result with diagnosis explanation when the
            # diagnosis produced model-authored text; otherwise preserve the
            # structured no-result state for upstream response builders.
            diagnosis_data = diagnosis.to_dict()
            no_result = getattr(diagnosis, "no_result", None)
            error_state = getattr(diagnosis, "error_state", None)
            if result is not None:
                data = result.setdefault("data", {})
                # Provider-overload diagnoses (provider_api_key_quota, provider_rate_limit)
                # MUST override whatever generic "Something went wrong" string the agent put
                # in result.message. The old gate `if "error" in current_msg.lower()` missed
                # the canonical "Something went wrong while processing your request." string
                # because it contains no "error" keyword, so the friendly high-volume message
                # never surfaced. Override unconditionally for provider issues.
                _provider_overload_categories = {
                    "provider_api_key_quota",
                    "provider_rate_limit",
                }
                if diagnosis.user_explanation:
                    current_msg = result.get("message") or result.get("response") or ""
                    is_provider_issue = str(diagnosis.category or "") in _provider_overload_categories
                    if is_provider_issue or (current_msg and "error" in current_msg.lower()) or not current_msg:
                        result["message"] = diagnosis.user_explanation
                        result["response"] = diagnosis.user_explanation
                if isinstance(no_result, dict) and "no_result" not in data:
                    data["no_result"] = no_result
                if isinstance(error_state, dict) and "error_state" not in data:
                    data["error_state"] = error_state
                data["diagnosis"] = diagnosis_data
                # Also expose under "operator_diagnostic" so cloud_intent.dispatch.py's
                # _diagnostic_from_container (which scans for "operator_diagnostic" /
                # "diagnostic" keys, NOT "diagnosis") can find provider-overload diagnoses
                # and route through the ai_source-aware provider_overloaded UX path
                # (friendly message for managed/codex, honest message for BYOK).
                if str(diagnosis_data.get("category") or "") in _provider_overload_categories:
                    data["operator_diagnostic"] = {
                        "category": diagnosis_data.get("category"),
                        "source": "openai_api_key",
                        "message": diagnosis_data.get("user_explanation"),
                        "reason": diagnosis_data.get("root_cause"),
                        "should_fallback": False,
                    }
            elif result is None and diagnosis.user_explanation:
                # Unhandled exception path: build a minimal result for the caller
                return {
                    "ok": False,
                    "success": False,
                    "intent": "error",
                    "message": diagnosis.user_explanation,
                    "response": diagnosis.user_explanation,
                    "commands_executed": [],
                    "data": {"diagnosis": diagnosis.to_dict()},
                    "continue_listening": False,
                }
            elif result is None and isinstance(no_result, dict):
                return {
                    "ok": False,
                    "success": False,
                    "intent": "ai_no_result",
                    "message": "",
                    "response": "",
                    "commands_executed": [],
                    "data": {
                        "diagnosis": diagnosis_data,
                        "no_result": no_result,
                        "error_state": (
                            error_state if isinstance(error_state, dict) else {"type": "ai_no_result", **no_result}
                        ),
                    },
                    "error": "ai_no_result",
                    "continue_listening": bool(no_result.get("retryable", True)),
                }

        except Exception:
            logger.debug("Self-diagnosis pipeline failed (non-fatal)", exc_info=True)
        return None

    # R3-P1-G (2026-05-30): ``_is_local_path`` (pre-LLM path classifier
    # that string-matched ``play <local-path>`` and short-circuited to
    # ``_handle_local_play``) and ``_handle_local_play`` are DELETED.
    # The ratchet ``check-no-pre-llm-query-classifier`` keeps any
    # ``if self._is_*``-shape dispatch from re-appearing in this
    # controller before the LLM call.

    async def _process_with_gpt(self, text: str, result: ProcessResultDict) -> ProcessResultDict:
        """Reject raw OpenAI clients; all LLM routing must use provider wrappers."""
        if callable(getattr(self.client, "route_command_native", None)):
            result["ok"] = False
            result["error"] = "native_tools_unverified"
            result["message"] = (
                "This provider has not verified native tool calling. In Settings, save an "
                "OpenAI-compatible connection profile and run Test active profile before using it."
            )
            result["data"]["answer"] = result["message"]
            return result
        logger.error(
            "AIController received a raw LLM client without route_command; "
            "refusing legacy Chat Completions path for request: %s",
            text[:120],
        )
        result["ok"] = False
        result["message"] = "The AI provider is not configured for Viola's routing path."
        result["data"]["answer"] = result["message"]
        return result
