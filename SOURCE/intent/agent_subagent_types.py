"""Subagent type policy for background Viola agents."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from html import escape
from typing import Any

from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
)

FULL_TOOL_SURFACE = "*"
DEFAULT_SUBAGENT_TYPE = "default"
# Sentinel returned by ``canonical_subagent_type`` when the caller explicitly
# omits ``subagent_type`` from a tool call. Claude Code treats this as the
# request to fork the parent agent rather than fall back to the default
# general-purpose template. Viola's dispatch (``_handle_start_agent``) maps the
# sentinel onto its fork branch and never persists it as a registered type.
FORK_SUBAGENT_TYPE = "__fork__"


class UnknownSubagentTypeError(ValueError):
    """Raised when an external caller supplies an unregistered subagent type.

    Mirrors Claude Code's ``AgentTool.tsx`` behavior which throws on unknown
    or denied agent types instead of silently degrading to the full-tool
    default. Internal resume paths may opt back into the lenient lookup via
    ``canonical_subagent_type(..., strict=False)``.
    """

    def __init__(self, requested: str, available: list[str]) -> None:
        self.requested = requested
        self.available = list(available)
        message = "Subagent type '%s' is not registered. Available: %s." % (
            requested,
            ", ".join(available) or "<none>",
        )
        super().__init__(message)


SUBAGENT_IDENTITY_SENTENCE_TEMPLATE = (
    "You are a background sub-agent of type `{subagent_type}`. "
    "Work only on the delegated task, use only the tools available to you, "
    "do not ask the user questions, and finish with a concise result."
)

TASK_NOTIFICATION_SOURCE_TAG = "task-notification"

TASK_NOTIFICATION_FRAME_TEMPLATE: dict[str, Any] = {
    "kind": FrameKind.SYSTEM_REMINDER.value,
    "role": FrameRole.META_USER.value,
    "block": "SystemReminderBlock(XML <task-notification>)",
    "origin": "subagent",
    "is_meta": True,
    "extra": {
        "agent_id": "<agent_id>",
        "subagent_type": "<subagent_type>",
        "status": "running|completed|failed",
        "elapsed_ms": 0,
        "final_text": "<final_text>",
        "output_file": "logs/agent_tasks/<task_id>.jsonl",
    },
}
"""ContextBuilder renders this shape as a meta system-reminder frame."""

SUBAGENT_TYPE_FIELD_DESCRIPTION = (
    "Background agent type. Use Research for search/synthesis, Explore for read-only investigation, "
    "Order_executor for browser commerce/order execution, Phone_caller for phone-call work, "
    "or default for the full tool surface."
)

_BROWSER_ORDER_TOOLS = frozenset(
    {
        "browser_back",
        "browser_close",
        "browser_evaluate",
        "browser_fill_form",
        "browser_forward",
        "browser_get_api_log",
        "browser_get_form_fields",
        "browser_get_links",
        "browser_get_page_info",
        "browser_get_text",
        "browser_interact",
        "browser_navigate",
        "browser_press_key",
        "browser_refresh",
        "browser_run_script",
        "browser_screenshot",
        "browser_scroll",
        "browser_select",
        "browser_snapshot",
        "browser_status",
        "browser_type",
        "browser_wait",
        "fill_payment_details",
        "payment",
        "signature",
        "verify_state",
        "web_read",
        "web_search",
    }
)

_READ_ONLY_INVESTIGATION_TOOLS = frozenset(
    {
        "check_api_registry",
        "file_read",
        "memory",
        "ToolSearch",
        "tool_search",
        "web_read",
        "web_search",
        "workbench",
    }
)

_DEFAULT_ACTION = {
    "file_read": "list",
    "memory": "read",
    "payment": "list",
    "phone": "status",
    "signature": "request_review",
    "workbench": "search",
}

_READ_ONLY_MEMORY_ACTIONS = frozenset({"audit", "list", "read", "stats"})


@dataclass(frozen=True, slots=True)
class SubagentTypeDefinition:
    """Registry-backed agent type definition.

    Claude Code resolves agent definitions dynamically. Viola keeps its
    product-specific agents, but the lifecycle now reads them through this
    shared registry instead of scattering type policy across separate maps.
    """

    key: str
    display_name: str
    aliases: frozenset[str] = field(default_factory=frozenset)
    tool_allowlist: frozenset[str] = field(default_factory=frozenset)
    action_allowlists: Mapping[str, frozenset[str]] = field(default_factory=dict)
    identity_template: str = SUBAGENT_IDENTITY_SENTENCE_TEMPLATE

    def identity_sentence(self) -> str:
        return self.identity_template.format(subagent_type=self.display_name)


_SUBAGENT_TYPE_DEFINITIONS: dict[str, SubagentTypeDefinition] = {}
_TYPE_ALIASES: dict[str, str] = {}


def register_subagent_type(definition: SubagentTypeDefinition) -> None:
    """Register or replace a subagent type definition."""

    key = definition.key.strip().replace(" ", "_").replace("-", "_").lower()
    if not key:
        raise ValueError("Subagent type key cannot be empty")
    normalized = SubagentTypeDefinition(
        key=key,
        display_name=definition.display_name,
        aliases=frozenset(_normalize_type_alias(alias) for alias in definition.aliases),
        tool_allowlist=frozenset(definition.tool_allowlist),
        action_allowlists={name: frozenset(actions) for name, actions in definition.action_allowlists.items()},
        identity_template=definition.identity_template,
    )
    _SUBAGENT_TYPE_DEFINITIONS[key] = normalized
    _TYPE_ALIASES[key] = key
    for alias in normalized.aliases:
        _TYPE_ALIASES[alias] = key


def list_subagent_type_definitions() -> tuple[SubagentTypeDefinition, ...]:
    """Return registered subagent types in stable display order."""

    return tuple(_SUBAGENT_TYPE_DEFINITIONS[key] for key in sorted(_SUBAGENT_TYPE_DEFINITIONS))


def _normalize_type_alias(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw.replace(" ", "_").replace("-", "_").lower()


def _register_builtin_subagent_types() -> None:
    register_subagent_type(
        SubagentTypeDefinition(
            key=DEFAULT_SUBAGENT_TYPE,
            display_name=DEFAULT_SUBAGENT_TYPE,
            aliases=frozenset({"", DEFAULT_SUBAGENT_TYPE}),
            tool_allowlist=frozenset({FULL_TOOL_SURFACE}),
        )
    )
    register_subagent_type(
        SubagentTypeDefinition(
            key="explore",
            display_name="Explore",
            aliases=frozenset({"explore", "explorer", "investigate", "investigator"}),
            tool_allowlist=_READ_ONLY_INVESTIGATION_TOOLS,
            action_allowlists={
                "file_read": frozenset({"info", "list", "read", "search"}),
                "memory": _READ_ONLY_MEMORY_ACTIONS,
                "workbench": frozenset({"list", "path_for", "read", "search"}),
            },
        )
    )
    register_subagent_type(
        SubagentTypeDefinition(
            key="order_executor",
            display_name="Order_executor",
            aliases=frozenset({"order", "order_executor", "commerce", "commerce_executor"}),
            tool_allowlist=_BROWSER_ORDER_TOOLS | frozenset({"memory", "ToolSearch", "tool_search"}),
            action_allowlists={
                "memory": _READ_ONLY_MEMORY_ACTIONS,
                "payment": frozenset({"list", "open_secure_card_entry", "request_review"}),
                "signature": frozenset({"request_review"}),
            },
        )
    )
    register_subagent_type(
        SubagentTypeDefinition(
            key="phone_caller",
            display_name="Phone_caller",
            aliases=frozenset({"phone", "phone_caller", "caller"}),
            tool_allowlist=frozenset({"memory", "phone", "ToolSearch", "tool_search"}),
            action_allowlists={
                "memory": _READ_ONLY_MEMORY_ACTIONS,
                "phone": frozenset({"call", "end", "status", "transcript"}),
            },
        )
    )
    register_subagent_type(
        SubagentTypeDefinition(
            key="research",
            display_name="Research",
            aliases=frozenset({"research", "researcher"}),
            tool_allowlist=_READ_ONLY_INVESTIGATION_TOOLS | frozenset({"think"}),
            action_allowlists={
                "file_read": frozenset({"info", "list", "read", "search"}),
                "memory": _READ_ONLY_MEMORY_ACTIONS,
                "workbench": frozenset({"list", "path_for", "read", "search"}),
            },
        )
    )


_register_builtin_subagent_types()


def canonical_subagent_type(subagent_type: str | None, *, strict: bool = False) -> str:
    """Return Viola's canonical subagent type key.

    When ``strict=False`` (default — back-compat for internal callers) an
    unknown subagent type silently falls back to the full-tool default. When
    ``strict=True`` an unknown type raises :class:`UnknownSubagentTypeError`,
    matching Claude Code's ``AgentTool.tsx`` which throws rather than degrade
    to general-purpose. External tool callers (``start_agent`` MCP tool)
    should always use ``strict=True``; resume/recovery paths may opt for
    lenient lookup to keep older transcripts loadable.

    The :data:`FORK_SUBAGENT_TYPE` sentinel is preserved as-is so the dispatch
    layer can detect omitted-type-meaning-fork without going through this
    helper twice. Subagent tool/identity rendering treats forks as the
    default tool surface; the fork path replaces the
    tool list with the parent's exact array.
    """
    if subagent_type == FORK_SUBAGENT_TYPE:
        return FORK_SUBAGENT_TYPE
    normalized = _normalize_type_alias(subagent_type)
    if not normalized:
        # Empty/None still maps to the default type via the existing alias
        # for the empty string. Strict callers should special-case "omitted"
        # before reaching this helper (see _handle_start_agent's fork path).
        return _TYPE_ALIASES.get(normalized, DEFAULT_SUBAGENT_TYPE)
    canonical = _TYPE_ALIASES.get(normalized)
    if canonical is not None:
        return canonical
    if strict:
        raise UnknownSubagentTypeError(
            requested=str(subagent_type or ""),
            available=sorted({definition.display_name for definition in _SUBAGENT_TYPE_DEFINITIONS.values()}),
        )
    return DEFAULT_SUBAGENT_TYPE


def display_subagent_type(subagent_type: str | None) -> str:
    """Return the model-facing display name for a subagent type."""
    if subagent_type == FORK_SUBAGENT_TYPE:
        return "fork"
    return _SUBAGENT_TYPE_DEFINITIONS[canonical_subagent_type(subagent_type)].display_name


def subagent_identity_sentence(subagent_type: str | None) -> str:
    """Return the identity sentence appended to child system prompts."""
    if subagent_type == FORK_SUBAGENT_TYPE:
        # Fork children inherit the parent's full system prompt; this short
        # sentence is appended as response-contract guidance.
        return SUBAGENT_IDENTITY_SENTENCE_TEMPLATE.format(subagent_type="fork")
    return _SUBAGENT_TYPE_DEFINITIONS[canonical_subagent_type(subagent_type)].identity_sentence()


def allowed_tools_for_subagent(subagent_type: str | None) -> set[str]:
    """Return the allowed tool names for a subagent type.

    The default type and the fork sentinel both return {"*"} (the full visible
    tool surface). The fork path replaces this with the parent's
    exact tool array for cache-identical prefixes; until that lands, fork
    children see the same surface as the default agent.
    """
    if subagent_type == FORK_SUBAGENT_TYPE:
        return {FULL_TOOL_SURFACE}
    canonical = canonical_subagent_type(subagent_type)
    definition = _SUBAGENT_TYPE_DEFINITIONS.get(canonical, _SUBAGENT_TYPE_DEFINITIONS[DEFAULT_SUBAGENT_TYPE])
    return set(definition.tool_allowlist)


def is_full_tool_surface(allowed_tools: set[str] | frozenset[str] | None) -> bool:
    """Return True when an allowlist means full visible tool access."""
    return not allowed_tools or FULL_TOOL_SURFACE in allowed_tools


def tool_call_allowed_for_subagent(
    subagent_type: str | None,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Validate a concrete tool call against subagent type policy."""
    if subagent_type == FORK_SUBAGENT_TYPE:
        # Fork children inherit the parent's exact tool array; per-action
        # filtering is the parent's responsibility.
        return True, None
    canonical = canonical_subagent_type(subagent_type)
    allowed_tools = allowed_tools_for_subagent(canonical)
    if not is_full_tool_surface(allowed_tools) and tool_name not in allowed_tools:
        return (
            False,
            "Tool '%s' is not available to subagent type '%s'." % (tool_name, display_subagent_type(canonical)),
        )

    definition = _SUBAGENT_TYPE_DEFINITIONS.get(canonical, _SUBAGENT_TYPE_DEFINITIONS[DEFAULT_SUBAGENT_TYPE])
    action_rules = definition.action_allowlists.get(tool_name)
    if action_rules is None:
        return True, None

    args = tool_args or {}
    action = str(args.get("action") or _DEFAULT_ACTION.get(tool_name, "")).strip().lower()
    if action and action not in action_rules:
        return (
            False,
            "Action '%s' for tool '%s' is not available to subagent type '%s'."
            % (action, tool_name, display_subagent_type(canonical)),
        )
    return True, None


_VALID_NOTIFICATION_STATUSES = frozenset(
    {"running", "completed", "failed", "killed"},
)


def build_task_notification_frame(
    *,
    agent_id: str,
    subagent_type: str | None,
    status: str,
    elapsed_ms: int,
    final_text: str = "",
    task_id: str | None = None,
    tool_use_id: str | None = None,
    timestamp_ms: int | None = None,
    total_tokens: int | None = None,
    tool_uses: int | None = None,
    worktree_path: str | None = None,
    worktree_branch: str | None = None,
    output_file: str | None = None,
    error: str | None = None,
) -> Frame:
    """Build the XML task-notification frame emitted when subagents update.

    The XML mirrors Claude Code's ``enqueueAgentNotification`` payload
    (``src/tasks/LocalAgentTask/LocalAgentTask.tsx:197-262``) — including
    ``<result>`` / ``<usage>`` / ``<worktree>`` sections and the ``killed``
    status — so the parent agent can distinguish a user-cancelled subagent
    from a crashed one.
    """
    normalized_status = status if status in _VALID_NOTIFICATION_STATUSES else "failed"
    display_type = display_subagent_type(subagent_type)
    task_identifier = str(task_id or agent_id)
    tool_identifier = str(tool_use_id or agent_id)
    effective_output_file = str(output_file or ("logs/agent_tasks/%s.jsonl" % task_identifier))
    duration_ms = max(0, int(elapsed_ms))
    payload: dict[str, Any] = {
        "notification_type": TASK_NOTIFICATION_SOURCE_TAG,
        "agent_id": str(agent_id),
        "task_id": task_identifier,
        "tool_use_id": tool_identifier,
        "subagent_type": display_type,
        "status": normalized_status,
        "elapsed_ms": duration_ms,
        "final_text": str(final_text or ""),
        "output_file": effective_output_file,
        "delivered": False,
    }
    if error:
        payload["error"] = str(error)
    if total_tokens is not None:
        payload["total_tokens"] = max(0, int(total_tokens))
    if tool_uses is not None:
        payload["tool_uses"] = max(0, int(tool_uses))
    if worktree_path:
        payload["worktree_path"] = str(worktree_path)
    if worktree_branch:
        payload["worktree_branch"] = str(worktree_branch)
    if normalized_status == "completed":
        summary = 'Agent "%s" completed' % display_type
    elif normalized_status == "failed":
        summary = 'Agent "%s" failed: %s' % (display_type, str(error or "Unknown error"))
    elif normalized_status == "killed":
        summary = 'Agent "%s" was stopped' % display_type
    elif normalized_status == "running":
        summary = 'Agent "%s" running' % display_type
    else:  # pragma: no cover - defensive
        summary = "%s subagent %s" % (display_type, normalized_status)
    lines = [
        "<task-notification>",
        "  <task-id>%s</task-id>" % escape(task_identifier),
        "  <tool-use-id>%s</tool-use-id>" % escape(tool_identifier),
        "  <output-file>%s</output-file>" % escape(effective_output_file),
        "  <status>%s</status>" % escape(normalized_status),
        "  <summary>%s</summary>" % escape(summary),
    ]
    if final_text:
        lines.append("  <result>%s</result>" % escape(str(final_text)))
    if total_tokens is not None or tool_uses is not None:
        usage_parts: list[str] = []
        if total_tokens is not None:
            usage_parts.append("<total_tokens>%d</total_tokens>" % max(0, int(total_tokens)))
        if tool_uses is not None:
            usage_parts.append("<tool_uses>%d</tool_uses>" % max(0, int(tool_uses)))
        usage_parts.append("<duration_ms>%d</duration_ms>" % duration_ms)
        lines.append("  <usage>%s</usage>" % "".join(usage_parts))
    if worktree_path:
        worktree_xml = "<worktree-path>%s</worktree-path>" % escape(str(worktree_path))
        if worktree_branch:
            worktree_xml += "<worktree-branch>%s</worktree-branch>" % escape(str(worktree_branch))
        lines.append("  <worktree>%s</worktree>" % worktree_xml)
    lines.append("</task-notification>")
    xml = "\n".join(lines)
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(
            SystemReminderBlock(
                text=xml,
                source_tag=None,
            ),
        ),
        is_meta=True,
        origin="subagent",
        tool_use_id=tool_identifier,
        task_id=task_identifier,
        timestamp_ms=timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
        ttl_turns=1,
        relevance="always",
        extra=payload,
    )


__all__ = [
    "DEFAULT_SUBAGENT_TYPE",
    "FORK_SUBAGENT_TYPE",
    "FULL_TOOL_SURFACE",
    "SUBAGENT_IDENTITY_SENTENCE_TEMPLATE",
    "SUBAGENT_TYPE_FIELD_DESCRIPTION",
    "TASK_NOTIFICATION_FRAME_TEMPLATE",
    "SubagentTypeDefinition",
    "UnknownSubagentTypeError",
    "allowed_tools_for_subagent",
    "build_task_notification_frame",
    "canonical_subagent_type",
    "display_subagent_type",
    "is_full_tool_surface",
    "list_subagent_type_definitions",
    "register_subagent_type",
    "subagent_identity_sentence",
    "tool_call_allowed_for_subagent",
]
