"""Central hook event and result schema.

Claude Code treats hooks as structured runtime events that may add model
context, update tool input, or make a permission-style decision. This module is
the Python boundary for that shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

HookDecision = Literal["passthrough", "allow", "ask", "deny"]


class HookEventName(StrEnum):
    """Canonical Claude hook event names."""

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    NOTIFICATION = "Notification"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
    SETUP = "Setup"
    TEAMMATE_IDLE = "TeammateIdle"
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"
    ELICITATION = "Elicitation"
    ELICITATION_RESULT = "ElicitationResult"
    CONFIG_CHANGE = "ConfigChange"
    WORKTREE_CREATE = "WorktreeCreate"
    WORKTREE_REMOVE = "WorktreeRemove"
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    CWD_CHANGED = "CwdChanged"
    FILE_CHANGED = "FileChanged"


CANONICAL_HOOK_EVENTS: tuple[HookEventName, ...] = tuple(HookEventName)
HOOK_DECISIONS: tuple[HookDecision, ...] = ("passthrough", "allow", "ask", "deny")
TOOL_BLOCKING_HOOK_DECISIONS: frozenset[HookDecision] = frozenset({"deny"})

# F-043 [partial-surface]: Claude's buddy/swarm/teammate runtime is not yet
# implemented in Viola — Round 4 surface promotion will tackle it. We do
# expose the lifecycle event names because the agent loop already
# dispatches a SUBSET of them (TeammateIdle, TaskCompleted) for the
# proactive scheduler. Mark the names so settings validation /
# documentation / telemetry can distinguish "fully-wired Claude-parity
# event" from "partially-wired Viola buddy preview".
BUDDY_SURFACE_HOOK_EVENTS: frozenset[HookEventName] = frozenset(
    {
        HookEventName.TEAMMATE_IDLE,
        HookEventName.TASK_CREATED,
        HookEventName.TASK_COMPLETED,
    }
)


def is_buddy_surface_event(event: HookEventName | str) -> bool:
    """Return True for hook events that belong to the partial buddy surface.

    F-043: callers (settings validators, docs renderers, telemetry) can
    use this to gate ``[preview]`` UI badges and warning messages.
    """

    try:
        canonical = coerce_hook_event_name(event)
    except ValueError:
        return False
    return canonical in BUDDY_SURFACE_HOOK_EVENTS


_DECISION_PRIORITY: dict[str, int] = {
    "passthrough": 0,
    "allow": 1,
    "ask": 2,
    "deny": 3,
}


@dataclass(frozen=True)
class HookEvent:
    """A single hook invocation."""

    name: HookEventName | str
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    frame_uuid: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", coerce_hook_event_name(self.name))


@dataclass(frozen=True)
class HookResult:
    """Structured result returned by hook dispatch."""

    decision: HookDecision = "passthrough"
    updated_input: dict[str, Any] | None = None
    updated_mcp_tool_output: Any | None = None
    updated_mcp_tool_output_present: bool = field(default=False, compare=False)
    permission_request_result: dict[str, Any] | None = None
    # S4-F-06: PermissionRequest hooks can return persisted permission updates
    # (Claude's ``updatedPermissions``) and a deny-with-``interrupt`` signal
    # that should abort the active permission request.
    updated_permissions: tuple[dict[str, Any], ...] = ()
    interrupt: bool = False
    elicitation_response: dict[str, Any] | None = None
    elicitation_result_response: dict[str, Any] | None = None
    additional_context: str | tuple[str, ...] | None = None
    system_message: str | None = None
    initial_user_message: str | None = None
    prevent_continuation: bool = False
    watch_paths: tuple[str, ...] = ()
    reason: str | None = None
    retry: bool = False

    def __post_init__(self) -> None:
        if self.updated_mcp_tool_output is not None and not self.updated_mcp_tool_output_present:
            object.__setattr__(self, "updated_mcp_tool_output_present", True)

    @property
    def additional_contexts(self) -> tuple[str, ...]:
        """Return additional context as a normalized tuple of non-empty strings."""

        if self.additional_context is None:
            return ()
        if isinstance(self.additional_context, str):
            text = self.additional_context.strip()
            return (text,) if text else ()
        return tuple(str(item).strip() for item in self.additional_context if str(item).strip())

    @property
    def blocks_tool(self) -> bool:
        """Whether this result prevents the current tool call from running."""

        return self.decision in TOOL_BLOCKING_HOOK_DECISIONS or self.prevent_continuation

    def merge(self, other: HookResult) -> HookResult:
        """Merge two hook results in registration order."""

        decision = _higher_priority_decision(self.decision, other.decision)
        reason = other.reason or self.reason
        updated_input = other.updated_input if other.updated_input is not None else self.updated_input
        if other.updated_mcp_tool_output_present:
            updated_mcp_tool_output = other.updated_mcp_tool_output
            updated_mcp_tool_output_present = True
        else:
            updated_mcp_tool_output = self.updated_mcp_tool_output
            updated_mcp_tool_output_present = self.updated_mcp_tool_output_present
        permission_request_result = (
            other.permission_request_result
            if other.permission_request_result is not None
            else self.permission_request_result
        )
        elicitation_response = (
            other.elicitation_response if other.elicitation_response is not None else self.elicitation_response
        )
        elicitation_result_response = (
            other.elicitation_result_response
            if other.elicitation_result_response is not None
            else self.elicitation_result_response
        )
        contexts = (*self.additional_contexts, *other.additional_contexts)
        # S4-F-15: Claude yields a separate ``hook_system_message`` attachment
        # for every result that has one (``utils/hooks.ts:2769-2779``), so
        # multiple hooks contributing system messages must all be visible.
        # Concatenate instead of last-wins.
        if self.system_message and other.system_message:
            system_message: str | None = "%s\n\n%s" % (self.system_message, other.system_message)
        else:
            system_message = other.system_message or self.system_message
        initial_user_message = other.initial_user_message or self.initial_user_message
        watch_paths = _unique_strings((*self.watch_paths, *other.watch_paths))
        updated_permissions = (*self.updated_permissions, *other.updated_permissions)
        return HookResult(
            decision=decision,
            updated_input=updated_input,
            updated_mcp_tool_output=updated_mcp_tool_output,
            updated_mcp_tool_output_present=updated_mcp_tool_output_present,
            permission_request_result=permission_request_result,
            updated_permissions=updated_permissions,
            interrupt=self.interrupt or other.interrupt,
            elicitation_response=elicitation_response,
            elicitation_result_response=elicitation_result_response,
            additional_context=contexts or None,
            system_message=system_message,
            initial_user_message=initial_user_message,
            prevent_continuation=self.prevent_continuation or other.prevent_continuation,
            watch_paths=watch_paths,
            reason=reason,
            retry=self.retry or other.retry,
        )


def coerce_hook_event_name(value: HookEventName | str) -> HookEventName:
    """Return a canonical hook event enum or raise for unknown names."""

    if isinstance(value, HookEventName):
        return value
    try:
        return HookEventName(str(value).strip())
    except ValueError as exc:
        raise ValueError("Unknown hook event: %s" % value) from exc


def _hook_event_value(value: HookEventName | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, HookEventName):
        return value.value
    return str(value).strip() or None


_CLAUDE_CANONICAL_HOOK_KEYS: frozenset[str] = frozenset(
    {
        # event correlation
        "hookSpecificOutput",
        "hookEventName",
        # decision / permission shape
        "decision",  # top-level approve|block only
        "permissionBehavior",
        "permissionDecision",
        "permissionDecisionReason",
        "permissionRequestResult",
        "permissionRequestDecision",
        "hookPermissionDecisionReason",
        "updatedPermissions",
        "permissionUpdates",
        "interrupt",
        "abort",
        # control flow
        "continue",
        "stopReason",
        "preventContinuation",
        "retry",
        # output payloads
        "additionalContext",
        "additionalContexts",
        "systemMessage",
        "initialUserMessage",
        "updatedInput",
        "updatedMCPToolOutput",
        "updatedOutput",
        # elicitation
        "elicitationResponse",
        "elicitationResultResponse",
        "action",
        "content",
        # watch paths
        "watchPaths",
        # informational
        "reason",
    }
)


_NON_CANONICAL_ALIAS_KEYS: frozenset[str] = frozenset(
    {
        "permission_behavior",
        "permission_decision",
        "permission_decision_reason",
        "permission_request_result",
        "additional_context",
        "stop_reason",
        "prevent_continuation",
        "watch_paths",
        "initial_user_message",
        "updated_mcp_tool_output",
        "updated_input",
        "updated_output",
        "updated_permissions",
        "permission_updates",
        "system_message",
        "elicitation_response",
        "elicitation_result_response",
    }
)


def hook_result_from_value(
    value: Any,
    *,
    expected_event: HookEventName | str | None = None,
    strict: bool = False,
) -> HookResult:
    """Normalize legacy hook return values to :class:`HookResult`.

    When ``strict`` is True, snake_case / legacy alias keys are rejected
    via ``ValueError``. F-037: settings, plugin, and skill hooks claim
    parity with Claude's sync hook output schema
    (``entrypoints/sdk/coreSchemas.ts:907-930``); accepting non-Claude
    keys silently lets non-parity payloads through and weakens the
    contract. The in-process lifecycle registry still calls this helper
    without ``strict`` so legacy in-process handlers keep working.
    """

    if value is None:
        return HookResult()
    if isinstance(value, HookResult):
        return value
    if isinstance(value, str):
        return HookResult(additional_context=value)
    if isinstance(value, dict):
        if strict:
            non_canonical = [key for key in value.keys() if key in _NON_CANONICAL_ALIAS_KEYS]
            if non_canonical:
                raise ValueError(
                    "Hook output contains non-Claude keys (strict mode): %s. "
                    "Use the Claude camelCase keys instead." % sorted(non_canonical)
                )
        hook_specific = value.get("hookSpecificOutput")
        if isinstance(hook_specific, dict):
            hook_event_name = hook_specific.get("hookEventName")
            expected_event_value = _hook_event_value(expected_event)
            if expected_event_value and hook_event_name != expected_event_value:
                raise ValueError(
                    "Hook returned incorrect event name: expected %r but got %r"
                    % (expected_event_value, hook_event_name)
                )
            merged = dict(value)
            merged.update(hook_specific)
            value = merged

        # Pre-fix (S4-F-14): Viola also accepted top-level
        # ``decision: "allow"|"ask"|"deny"|"passthrough"``. Claude only
        # honors top-level ``approve|block`` (see ``utils/hooks.ts:525-542``);
        # ``allow/ask/deny`` live under ``hookSpecificOutput.permissionDecision``
        # or ``permissionDecision``/``permissionBehavior``. Restrict the
        # top-level ``decision`` field to Claude's canonical values.
        top_level_decision = value.get("decision")
        top_level_normalized: Any = None
        if top_level_decision is not None:
            text = str(top_level_decision).strip().lower()
            if text in {"approve", "approved"}:
                top_level_normalized = "allow"
            elif text in {"block", "blocked"}:
                top_level_normalized = "deny"
            elif isinstance(top_level_decision, str):
                raise ValueError("Unknown top-level hook decision: %s" % top_level_decision)
        permission_behavior = (
            value.get("permissionBehavior")
            or value.get("permission_behavior")
            or value.get("permissionDecision")
            or value.get("permission_decision")
            or top_level_normalized
        )
        decision = _normalize_decision(permission_behavior)
        additional_context = (
            value.get("additional_context") or value.get("additionalContext") or value.get("additionalContexts")
        )
        if isinstance(additional_context, list):
            additional_context = tuple(str(item) for item in additional_context)
        stop_reason = value.get("stopReason") or value.get("stop_reason")
        prevent = bool(value.get("prevent_continuation") or value.get("preventContinuation"))
        if value.get("continue") is False:
            prevent = True
        watch_paths = value.get("watch_paths") or value.get("watchPaths") or ()
        if isinstance(watch_paths, str):
            watch_paths = (watch_paths,)
        initial_user_message = value.get("initial_user_message")
        if initial_user_message is None:
            initial_user_message = value.get("initialUserMessage")
        if initial_user_message is not None:
            initial_user_message = str(initial_user_message).strip() or None
        updated_mcp_tool_output_present = False
        updated_mcp_tool_output = None
        for key in ("updated_mcp_tool_output", "updatedMCPToolOutput"):
            if key in value:
                updated_mcp_tool_output_present = True
                updated_mcp_tool_output = value.get(key)
                break
        permission_request_result = (
            value.get("permission_request_result")
            or value.get("permissionRequestResult")
            or value.get("permissionRequestDecision")
        )
        if permission_request_result is None and value.get("hookEventName") == "PermissionRequest":
            permission_request_result = value.get("decision")
        elicitation_response = value.get("elicitation_response") or value.get("elicitationResponse")
        if elicitation_response is None and value.get("hookEventName") == "Elicitation":
            action = value.get("action")
            if action:
                elicitation_response = {"action": action}
                if "content" in value:
                    elicitation_response["content"] = value.get("content")
        elicitation_result_response = value.get("elicitation_result_response") or value.get("elicitationResultResponse")
        if elicitation_result_response is None and value.get("hookEventName") == "ElicitationResult":
            action = value.get("action")
            if action:
                elicitation_result_response = {"action": action}
                if "content" in value:
                    elicitation_result_response["content"] = value.get("content")
        reason = (
            value.get("reason")
            or value.get("permissionDecisionReason")
            or value.get("permission_decision_reason")
            or value.get("hookPermissionDecisionReason")
            or stop_reason
        )
        updated_input = value.get("updated_input")
        if updated_input is None:
            updated_input = value.get("updatedInput")
        # Accept R5C-era aliases too — the field is now updated_mcp_tool_output (Claude canonical)
        if not updated_mcp_tool_output_present:
            for key in ("updated_output", "updatedOutput"):
                if key in value:
                    updated_mcp_tool_output_present = True
                    updated_mcp_tool_output = value.get(key)
                    break
        system_message = value.get("system_message")
        if system_message is None:
            system_message = value.get("systemMessage")
        # S4-F-06: Pull ``updatedPermissions`` and ``interrupt`` from either
        # the top level or the PermissionRequest decision payload. Claude's
        # PermissionContext consumes ``decision.updatedPermissions`` on allow
        # and ``decision.interrupt`` on deny (PermissionContext.ts:222-250).
        updated_permissions_raw = (
            value.get("updated_permissions")
            or value.get("updatedPermissions")
            or value.get("permission_updates")
            or value.get("permissionUpdates")
            or ()
        )
        if isinstance(permission_request_result, dict):
            inner = permission_request_result.get("updatedPermissions") or permission_request_result.get(
                "updated_permissions"
            )
            if inner and not updated_permissions_raw:
                updated_permissions_raw = inner
        updated_permissions: tuple[dict[str, Any], ...] = ()
        if isinstance(updated_permissions_raw, (list, tuple)):
            updated_permissions = tuple(dict(item) for item in updated_permissions_raw if isinstance(item, dict))
        interrupt_raw = value.get("interrupt") or value.get("abort")
        if not interrupt_raw and isinstance(permission_request_result, dict):
            interrupt_raw = permission_request_result.get("interrupt") or permission_request_result.get("abort")
        return HookResult(
            decision=decision,
            updated_input=updated_input if isinstance(updated_input, dict) else None,
            updated_mcp_tool_output=updated_mcp_tool_output,
            updated_mcp_tool_output_present=updated_mcp_tool_output_present,
            permission_request_result=(
                permission_request_result if isinstance(permission_request_result, dict) else None
            ),
            updated_permissions=updated_permissions,
            interrupt=bool(interrupt_raw),
            elicitation_response=elicitation_response if isinstance(elicitation_response, dict) else None,
            elicitation_result_response=(
                elicitation_result_response if isinstance(elicitation_result_response, dict) else None
            ),
            additional_context=additional_context,
            system_message=str(system_message).strip() if system_message else None,
            initial_user_message=initial_user_message,
            prevent_continuation=prevent,
            watch_paths=_unique_strings(tuple(str(path) for path in watch_paths)),
            reason=str(reason) if reason else None,
            retry=bool(value.get("retry")),
        )

    return HookResult(additional_context=str(value))


def hook_result_from_exception(event: HookEventName | str, handler_name: str, exc: BaseException) -> HookResult:
    """Convert a hook failure into a structured, non-model-visible result."""

    message = getattr(exc, "message", None) or str(exc)
    if exc.__class__.__name__ == "SafetyBlockError":
        return HookResult(
            decision="deny",
            reason=str(message),
            additional_context="Hook %s denied execution in %s: %s" % (handler_name, event, message),
        )
    return HookResult(
        reason=str(message),
    )


def _higher_priority_decision(left: HookDecision, right: HookDecision) -> HookDecision:
    if _DECISION_PRIORITY[right] > _DECISION_PRIORITY[left]:
        return right
    return left


def _normalize_decision(value: Any) -> HookDecision:
    decision = str(value or "passthrough").strip().lower()
    if decision in {"approve", "approved"}:
        return "allow"
    if decision in {"block", "blocked"}:
        return "deny"
    if decision in {"allow", "ask", "deny", "passthrough"}:
        return decision  # type: ignore[return-value]
    return "passthrough"


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


__all__ = [
    "BUDDY_SURFACE_HOOK_EVENTS",
    "CANONICAL_HOOK_EVENTS",
    "HOOK_DECISIONS",
    "TOOL_BLOCKING_HOOK_DECISIONS",
    "HookDecision",
    "HookEvent",
    "HookEventName",
    "HookResult",
    "coerce_hook_event_name",
    "hook_result_from_exception",
    "hook_result_from_value",
    "is_buddy_surface_event",
]
