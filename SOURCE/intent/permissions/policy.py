"""Claude Code-style permission policy decisions for tool execution."""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from intent.tool_types import RiskLevel
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
)

PermissionMode = Literal[
    "default",
    "acceptEdits",
    "bypassPermissions",
    "dontAsk",
    "plan",
    "auto",
    "bubble",
]
PermissionBehavior = Literal["allow", "deny", "ask"]

_VALID_MODES: frozenset[str] = frozenset(
    {
        "default",
        "acceptEdits",
        "bypassPermissions",
        "dontAsk",
        "plan",
        "auto",
        "bubble",
    }
)
_DENIAL_MAX_CONSECUTIVE = 3
_DENIAL_MAX_TOTAL = 20


@dataclass(frozen=True)
class PermissionDenialCounters:
    """Classifier/user denial counters for one permission key."""

    consecutive_denials: int = 0
    total_denials: int = 0

    def record_denial(self) -> PermissionDenialCounters:
        return PermissionDenialCounters(
            consecutive_denials=self.consecutive_denials + 1,
            total_denials=self.total_denials + 1,
        )

    def record_success(self) -> PermissionDenialCounters:
        if self.consecutive_denials == 0:
            return self
        return PermissionDenialCounters(
            consecutive_denials=0,
            total_denials=self.total_denials,
        )

    def should_fallback_to_prompting(self) -> bool:
        return self.consecutive_denials >= _DENIAL_MAX_CONSECUTIVE or self.total_denials >= _DENIAL_MAX_TOTAL

    def to_dict(self) -> dict[str, int]:
        return {
            "consecutive_denials": self.consecutive_denials,
            "total_denials": self.total_denials,
        }


@dataclass(frozen=True)
class PermissionClassifierResult:
    """Classifier output folded into a normal permission decision."""

    behavior: PermissionBehavior
    classifier: str
    reason: str | None = None
    updated_input: dict[str, Any] | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "behavior": self.behavior,
            "classifier": self.classifier,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        if self.updated_input is not None:
            payload["has_updated_input"] = True
        return payload


@dataclass(frozen=True)
class PermissionHookProvenance:
    """Permission-relevant hook result provenance."""

    event: str
    source: str = "hook"
    decision: PermissionBehavior | None = None
    reason: str | None = None
    updated_input: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": self.event,
            "source": self.source,
        }
        if self.decision is not None:
            payload["decision"] = self.decision
        if self.reason:
            payload["reason"] = self.reason
        if self.updated_input is not None:
            payload["has_updated_input"] = True
        return payload


@dataclass(frozen=True)
class PermissionRule:
    """A first-class allow/ask/deny rule with source metadata.

    Supports Claude's ``Tool(content)`` permission rule syntax. Examples::

        Bash(git *)        # matches Bash tool when the command starts with "git "
        Read(*.ts)         # matches Read tool when the path matches *.ts
        Bash               # matches any Bash invocation
        *                  # matches any tool

    Internally the parsed form lives on ``_parsed_tool_name`` and
    ``_parsed_content_pattern`` (computed in ``__post_init__``); callers
    continue to set ``tool_name`` to the human-readable rule string.
    """

    tool_name: str
    behavior: PermissionBehavior
    source: str
    reason: str | None = None
    mode: PermissionMode | None = None
    updated_input: dict[str, Any] | None = None

    def matches(self, context: PermissionContext, mode: PermissionMode) -> bool:
        if self.mode is not None and self.mode != mode:
            return False
        parsed_tool, parsed_content = _parse_rule_tool(self.tool_name)
        if parsed_tool != "*" and parsed_tool != context.tool_name:
            return False
        if parsed_content is None:
            return True
        return _matches_tool_content(context, parsed_content)


def _parse_rule_tool(raw: str) -> tuple[str, str | None]:
    """Split a rule string into ``(tool_name, content_pattern_or_None)``.

    ``Bash(git *)`` → ``("Bash", "git *")``
    ``Bash``        → ``("Bash", None)``
    ``*``           → ``("*", None)``
    """

    text = str(raw or "").strip()
    if not text:
        return ("", None)
    if text.endswith(")"):
        open_index = text.find("(")
        if open_index > 0:
            tool = text[:open_index].strip()
            content = text[open_index + 1 : -1].strip()
            if tool:
                return (tool, content)
    return (text, None)


def _matches_tool_content(context: PermissionContext, pattern: str) -> bool:
    """Match a content pattern against the tool's primary content argument.

    Per Claude's rule grammar, ``Bash(git *)`` checks the command; ``Read(*.ts)``
    checks the path. We pull the most likely content field for the tool based
    on common argument names, then fall back to ``content``/``query``/``text``.
    """

    candidates: list[str] = []
    tool_input = context.tool_input or {}
    if isinstance(tool_input, Mapping):
        for key in ("command", "path", "file_path", "url", "query", "content", "text", "args"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
                # First match is the canonical "content" for this tool; stop.
                break
    if not candidates:
        return False
    candidate = candidates[0]
    if pattern == "*":
        return True
    # fnmatch matches the entire string; "git *" matches "git diff" only if
    # the full string follows "git " plus anything. That's the Claude semantics.
    return fnmatch.fnmatchcase(candidate, pattern)


@dataclass(frozen=True)
class PermissionContext:
    """Inputs needed to make a permission decision."""

    user_id: str
    session_id: str
    tool_name: str
    tool_input: Mapping[str, Any]
    cwd: str | None = None
    channel: str | None = None
    agent_id: str | None = None
    mode: PermissionMode | None = None
    risk_level: RiskLevel | str | None = None
    rule_overrides: Sequence[PermissionRule] = field(default_factory=tuple)
    hook_provenance: Sequence[PermissionHookProvenance] = field(default_factory=tuple)


@dataclass(frozen=True)
class PermissionDecision:
    """Result returned by the permission policy engine."""

    behavior: PermissionBehavior
    source: str
    mode: PermissionMode
    reason: str | None = None
    updated_input: dict[str, Any] | None = None
    classifier: PermissionClassifierResult | dict[str, Any] | None = None
    risk_level: RiskLevel | str | None = None
    frame: Frame | None = None
    denial_counters: PermissionDenialCounters | None = None
    hook_provenance: tuple[PermissionHookProvenance, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "behavior": self.behavior,
            "source": self.source,
            "mode": self.mode,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.updated_input is not None:
            payload["updated_input"] = dict(self.updated_input)
        if self.classifier is not None:
            if isinstance(self.classifier, PermissionClassifierResult):
                payload["classifier"] = self.classifier.to_dict()
            else:
                payload["classifier"] = dict(self.classifier)
        if self.risk_level is not None:
            payload["risk_level"] = _risk_value(self.risk_level)
        if self.denial_counters is not None:
            payload["denial_counters"] = self.denial_counters.to_dict()
        if self.hook_provenance:
            payload["hook_provenance"] = [hook.to_dict() for hook in self.hook_provenance]
        if self.frame is not None:
            payload["has_frame"] = True
        return payload


PermissionClassifier = Callable[[PermissionContext], PermissionClassifierResult | None]


class PermissionPolicy:
    """Standalone permission policy engine shared by tool execution surfaces."""

    def __init__(
        self,
        *,
        mode: PermissionMode = "default",
        rules: Sequence[PermissionRule] = (),
        classifier: PermissionClassifier | None = None,
    ) -> None:
        self.mode: PermissionMode = _normalize_mode(mode)
        self._rules: tuple[PermissionRule, ...] = tuple(rules)
        self._classifier = classifier
        self._denial_state: dict[tuple[str, str, str, str], PermissionDenialCounters] = {}

    def check(self, context: PermissionContext) -> PermissionDecision:
        """Return the policy decision for a tool call.

        Authority precedence (matches Claude's ``toolHooks.ts:322-405``):

        1. Configured **deny** rule wins over everything — even a hook allow.
        2. Configured **ask** rule overrides a hook ``allow`` (the prompt
           still fires), but yields to a hook ``deny`` (deny is strictly
           stronger).
        3. Hook ``deny`` / ``ask`` are authoritative when no deny/ask rule
           applies.
        4. Hook ``allow`` skips the interactive prompt only after deny/ask
           rules still pass; an existing ``allow`` rule still applies.
        5. Mode / classifier / risk fallbacks run last.

        Pre-fix (S4-F-04 / S6-001 / S9-10): the function returned the hook
        decision immediately on any provenance entry — so a hook ``allow``
        could short-circuit a configured ``deny`` or ``ask`` rule. Claude
        explicitly rechecks rules after hook allow.
        """

        mode = _normalize_mode(context.mode or self.mode)
        updated_input: dict[str, Any] | None = None
        hook_provenance = tuple(context.hook_provenance)
        hook_decision: PermissionHookProvenance | None = None
        for hook in hook_provenance:
            if hook.updated_input is not None:
                updated_input = dict(hook.updated_input)
            if hook.decision is not None:
                hook_decision = hook
                # Do NOT early-return on hook allow — Claude continues to
                # check deny/ask rules after a hook allow.

        # Deny rules take precedence over any hook decision (including hook deny,
        # since both produce deny; rule wins on attribution because rules are
        # the user's persisted policy).
        deny_rule = self._matching_rule(context, mode, "deny")
        if deny_rule is not None:
            return self._decision(
                behavior="deny",
                source=deny_rule.source,
                mode=mode,
                reason=deny_rule.reason,
                context=context,
                updated_input=deny_rule.updated_input or updated_input,
                hook_provenance=hook_provenance,
            )

        # Hook deny is authoritative when no deny rule applies.
        if hook_decision is not None and hook_decision.decision == "deny":
            return self._decision(
                behavior="deny",
                source=hook_decision.source,
                mode=mode,
                reason=hook_decision.reason or "Hook denied this tool call",
                context=context,
                updated_input=updated_input,
                hook_provenance=hook_provenance,
            )

        # Ask rules override hook allow (Claude's "ask still prompts" rule),
        # but yield to hook ask (both are ask).
        ask_rule = self._matching_rule(context, mode, "ask")
        if ask_rule is not None:
            return self._decision(
                behavior="ask",
                source=ask_rule.source,
                mode=mode,
                reason=ask_rule.reason,
                context=context,
                updated_input=ask_rule.updated_input or updated_input,
                hook_provenance=hook_provenance,
            )

        # Hook ask is authoritative when no ask rule applies.
        if hook_decision is not None and hook_decision.decision == "ask":
            return self._decision(
                behavior="ask",
                source=hook_decision.source,
                mode=mode,
                reason=hook_decision.reason or "Hook requested approval",
                context=context,
                updated_input=updated_input,
                hook_provenance=hook_provenance,
            )

        # Hook allow short-circuits remaining fallback checks, but allow
        # rules still apply if defined (they look the same end-state).
        if hook_decision is not None and hook_decision.decision == "allow":
            self.record_success(context)
            return self._decision(
                behavior="allow",
                source=hook_decision.source,
                mode=mode,
                reason=hook_decision.reason or "Hook allowed this tool call",
                context=context,
                updated_input=updated_input,
                hook_provenance=hook_provenance,
            )

        allow_rule = self._matching_rule(context, mode, "allow")
        if allow_rule is not None:
            return self._decision(
                behavior=allow_rule.behavior,
                source=allow_rule.source,
                mode=mode,
                reason=allow_rule.reason,
                context=context,
                updated_input=allow_rule.updated_input or updated_input,
                hook_provenance=hook_provenance,
            )

        if mode == "bypassPermissions":
            self.record_success(context)
            return self._decision(
                behavior="allow",
                source="mode",
                mode=mode,
                reason="bypassPermissions mode allows this tool after deny/ask rules",
                context=context,
                updated_input=updated_input,
                hook_provenance=hook_provenance,
            )

        if mode == "auto":
            classifier_result = self._classifier(context) if self._classifier is not None else None
            if classifier_result is not None:
                return self._classifier_decision(
                    context=context,
                    mode=mode,
                    classifier_result=classifier_result,
                    updated_input=classifier_result.updated_input or updated_input,
                    hook_provenance=hook_provenance,
                )

        risk = _risk_value(context.risk_level)
        if risk in {RiskLevel.SAFE.value, RiskLevel.CONFIRM.value}:
            self.record_success(context)
            return self._decision(
                behavior="allow",
                source="risk_metadata",
                mode=mode,
                reason="%s risk is allowed by the current permission mode" % risk,
                context=context,
                updated_input=updated_input,
                hook_provenance=hook_provenance,
            )

        if mode == "dontAsk":
            counters = self.record_denial(context)
            return self._decision(
                behavior="deny",
                source="mode",
                mode=mode,
                reason="dontAsk mode denies actions that would require a permission prompt",
                context=context,
                updated_input=updated_input,
                denial_counters=counters,
                hook_provenance=hook_provenance,
            )

        if mode == "plan":
            counters = self.record_denial(context)
            return self._decision(
                behavior="deny",
                source="mode",
                mode=mode,
                reason="plan mode denies tool execution until the plan is approved",
                context=context,
                updated_input=updated_input,
                denial_counters=counters,
                hook_provenance=hook_provenance,
            )

        if mode == "bubble":
            return self._decision(
                behavior="ask",
                source="mode",
                mode=mode,
                reason="bubble mode requires the parent permission surface to decide",
                context=context,
                updated_input=updated_input,
                hook_provenance=hook_provenance,
            )

        return self._decision(
            behavior="ask",
            source="risk_metadata",
            mode=mode,
            reason="dangerous risk requires explicit user approval",
            context=context,
            updated_input=updated_input,
            hook_provenance=hook_provenance,
        )

    def resolve_user_response(
        self,
        context: PermissionContext,
        pending_decision: PermissionDecision,
        *,
        approved: bool,
    ) -> PermissionDecision:
        """Convert a prompted ask decision into a final allow/deny decision."""

        if approved:
            counters = self.record_success(context)
            return self._decision(
                behavior="allow",
                source="user",
                mode=pending_decision.mode,
                reason="User approved the permission prompt",
                context=context,
                updated_input=pending_decision.updated_input,
                classifier=pending_decision.classifier,
                denial_counters=counters,
                hook_provenance=pending_decision.hook_provenance,
            )
        counters = self.record_denial(context)
        return self._decision(
            behavior="deny",
            source="user",
            mode=pending_decision.mode,
            reason="User denied or did not answer the permission prompt",
            context=context,
            updated_input=pending_decision.updated_input,
            classifier=pending_decision.classifier,
            denial_counters=counters,
            hook_provenance=pending_decision.hook_provenance,
        )

    def record_denial(self, context: PermissionContext) -> PermissionDenialCounters:
        key = self._denial_key(context)
        counters = self._denial_state.get(key, PermissionDenialCounters()).record_denial()
        self._denial_state[key] = counters
        return counters

    def record_success(self, context: PermissionContext) -> PermissionDenialCounters:
        key = self._denial_key(context)
        counters = self._denial_state.get(key, PermissionDenialCounters()).record_success()
        self._denial_state[key] = counters
        return counters

    def denial_counters(self, context: PermissionContext) -> PermissionDenialCounters:
        return self._denial_state.get(self._denial_key(context), PermissionDenialCounters())

    def _classifier_decision(
        self,
        *,
        context: PermissionContext,
        mode: PermissionMode,
        classifier_result: PermissionClassifierResult,
        updated_input: dict[str, Any] | None,
        hook_provenance: tuple[PermissionHookProvenance, ...],
    ) -> PermissionDecision:
        if classifier_result.behavior == "allow":
            counters = self.record_success(context)
            return self._decision(
                behavior="allow",
                source="classifier",
                mode=mode,
                reason=classifier_result.reason,
                context=context,
                updated_input=updated_input,
                classifier=classifier_result,
                denial_counters=counters,
                hook_provenance=hook_provenance,
            )
        if classifier_result.behavior == "ask":
            return self._decision(
                behavior="ask",
                source="classifier",
                mode=mode,
                reason=classifier_result.reason,
                context=context,
                updated_input=updated_input,
                classifier=classifier_result,
                hook_provenance=hook_provenance,
            )

        counters = self.record_denial(context)
        if counters.should_fallback_to_prompting():
            return self._decision(
                behavior="ask",
                source="classifier",
                mode=mode,
                reason=_denial_limit_reason(counters, classifier_result.reason),
                context=context,
                updated_input=updated_input,
                classifier=classifier_result,
                denial_counters=counters,
                hook_provenance=hook_provenance,
            )
        return self._decision(
            behavior="deny",
            source="classifier",
            mode=mode,
            reason=classifier_result.reason or "Classifier denied this tool call",
            context=context,
            updated_input=updated_input,
            classifier=classifier_result,
            denial_counters=counters,
            hook_provenance=hook_provenance,
        )

    def _matching_rule(
        self,
        context: PermissionContext,
        mode: PermissionMode,
        behavior: PermissionBehavior,
    ) -> PermissionRule | None:
        for rule in tuple(context.rule_overrides) + self._rules:
            if rule.behavior == behavior and rule.matches(context, mode):
                return rule
        return None

    def _decision(
        self,
        *,
        behavior: PermissionBehavior,
        source: str,
        mode: PermissionMode,
        context: PermissionContext,
        reason: str | None = None,
        updated_input: dict[str, Any] | None = None,
        classifier: PermissionClassifierResult | dict[str, Any] | None = None,
        denial_counters: PermissionDenialCounters | None = None,
        hook_provenance: tuple[PermissionHookProvenance, ...] = (),
    ) -> PermissionDecision:
        frame = None
        if behavior in {"ask", "deny"}:
            frame = _permission_frame(
                behavior=behavior,
                source=source,
                mode=mode,
                context=context,
                reason=reason,
                classifier=classifier,
                denial_counters=denial_counters,
                updated_input=updated_input,
                hook_provenance=hook_provenance,
            )
        return PermissionDecision(
            behavior=behavior,
            source=source,
            mode=mode,
            reason=reason,
            updated_input=updated_input,
            classifier=classifier,
            risk_level=context.risk_level,
            frame=frame,
            denial_counters=denial_counters,
            hook_provenance=hook_provenance,
        )

    @staticmethod
    def _denial_key(context: PermissionContext) -> tuple[str, str, str, str]:
        return (
            context.user_id,
            context.session_id,
            context.agent_id or "",
            context.tool_name,
        )


def _permission_frame(
    *,
    behavior: PermissionBehavior,
    source: str,
    mode: PermissionMode,
    context: PermissionContext,
    reason: str | None,
    classifier: PermissionClassifierResult | dict[str, Any] | None,
    denial_counters: PermissionDenialCounters | None,
    updated_input: dict[str, Any] | None,
    hook_provenance: tuple[PermissionHookProvenance, ...],
) -> Frame:
    action = "denied" if behavior == "deny" else "requires approval"
    reason_text = reason or "No reason supplied"
    text = "Permission %s for tool '%s' (mode=%s, source=%s): %s" % (
        action,
        context.tool_name,
        mode,
        source,
        reason_text,
    )
    classifier_payload: dict[str, Any] | None = None
    if isinstance(classifier, PermissionClassifierResult):
        classifier_payload = classifier.to_dict()
    elif classifier is not None:
        classifier_payload = dict(classifier)
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=text, source_tag="permission"),),
        is_meta=True,
        origin="permission",
        permission_mode=mode,
        session_id=context.session_id,
        extra={
            "permission": {
                "behavior": behavior,
                "source": source,
                "mode": mode,
                "tool_name": context.tool_name,
                "reason": reason,
                "risk_level": _risk_value(context.risk_level),
                "classifier": classifier_payload,
                "denial_counters": denial_counters.to_dict() if denial_counters is not None else None,
                "has_updated_input": updated_input is not None,
                "hook_provenance": [hook.to_dict() for hook in hook_provenance],
            }
        },
    )


def _normalize_mode(mode: str) -> PermissionMode:
    if mode in _VALID_MODES:
        return mode  # type: ignore[return-value]
    return "default"


def _risk_value(risk: RiskLevel | str | None) -> str | None:
    if isinstance(risk, RiskLevel):
        return risk.value
    if isinstance(risk, str) and risk:
        return risk
    return None


def _denial_limit_reason(counters: PermissionDenialCounters, classifier_reason: str | None) -> str:
    if counters.total_denials >= _DENIAL_MAX_TOTAL:
        warning = "%d actions were blocked this session" % counters.total_denials
    else:
        warning = "%d consecutive actions were blocked" % counters.consecutive_denials
    if classifier_reason:
        return "%s. Latest blocked action: %s" % (warning, classifier_reason)
    return warning
