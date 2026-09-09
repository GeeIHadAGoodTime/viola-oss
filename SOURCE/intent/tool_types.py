"""Type definitions for the agentic tool-use system.

Provides dataclass models for tool results, calls, advisory risk levels,
and the agent execution result.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class RiskLevel(enum.Enum):
    """Descriptive risk classification for tool operations.

    These labels feed the first-class permission policy engine in
    ``intent.permissions.policy``; they are metadata, not the policy engine.
    Current defaults preserve SEC-8 behavior:
    - SAFE: read-only or no side effects
    - CONFIRM: logging/audit tier for reversible or bounded actions
    - DANGEROUS: potentially destructive or irreversible actions
    """

    SAFE = "safe"
    CONFIRM = "confirm"
    DANGEROUS = "dangerous"


def _payload_reports_failure(data: Any) -> bool:
    """True when a tool payload states its own failure in a structured field.

    Reads only our own typed envelope booleans -- never prose, never the model's
    words. A literal ``False`` under ``ok`` or ``success`` is the payload saying
    the operation did not happen; anything else (absent, None, truthy) is not
    treated as a failure claim.
    """
    if not isinstance(data, dict):
        return False
    return data.get("ok") is False or data.get("success") is False


def _payload_error_text(data: Any) -> str | None:
    """Best available error text carried inside a failing payload."""
    if not isinstance(data, dict):
        return None
    for key in ("error", "message"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass
class ToolResult:
    """Result from executing a tool.

    Fed back to the LLM as observation context for the next iteration.

    Enriched error fields (error_category, retryable, required_tier) are
    populated by the agent executor / MCP hub when a tool fails. They are
    included in the LLM-facing JSON as structured *codes* so the model can
    reason about failure causes and recovery strategies.

    The previous ``recovery_hint`` prose channel was deleted by R5-P0-A
    (2026-05-30): per-failure directive sentences ("Ask the user to sign
    in...", "Re-authenticate with the cloud backend...") are the same
    next_step-prose anti-pattern R3-P1-E retired under a different field
    name. The structured ``error_category`` carries the signal; recovery
    policy lives in the unified system prompt, not on every tool result.
    See ``services/api/errors.ts:820,831,845,859`` for the Claude Code
    parity target (typed string codes, never directive sentences).
    """

    ok: bool
    data: Any = None
    error: str | None = None
    # Native MCP result surfaces. These stay out of ``data`` so the LLM-facing
    # mapper can emit content blocks without also exposing the raw hub payload.
    mcp_content: list[dict[str, Any]] | None = None
    mcp_meta: dict[str, Any] | None = None
    truncated: bool = False
    # --- Enriched error context (populated on failure) ---
    error_category: str | None = None
    retryable: bool = False
    required_tier: str | None = None
    # Set when a tool ran but cannot establish whether the real-world effect
    # happened. Distinct from ok=False (proven not to have happened) and from
    # ok=True (proven to have happened): "I don't know yet" is a third answer,
    # and collapsing it into either of the other two is how a tool ends up
    # telling a user an action succeeded when nobody checked.
    unverified: bool = False

    def __post_init__(self) -> None:
        """Refuse to hold a success verdict over a payload that reports failure.

        ``to_llm_text`` already repaired this for the text the model reads, but
        only there: ``ToolResult.ok`` stayed True for every consumer that reads
        the attribute instead of the rendered text, and several do -- the native
        tool_result block's ``is_error`` flag (intent/agent_loop.py), the
        PostToolUse vs PostToolUseFailure hook split, and the tool-call metrics.
        So a handler that wrapped a failure envelope in ``ToolResult(ok=True,
        data=...)`` was still recorded, flagged, and hooked as a success. Making
        this an invariant of the type closes the class at every consumer at once
        instead of at one rendering seam.
        """
        if self.ok and _payload_reports_failure(self.data):
            self.ok = False

    def to_llm_text(self) -> str:
        """Format result for LLM consumption.

        On failure, includes structured error context (category, retryable,
        required_tier) when available so the LLM can make informed
        decisions about retries, escalation, or user messaging.
        """
        if not self.ok:
            import json

            error_obj: dict[str, Any] = {
                "ok": False,
                "error": self.error or _payload_error_text(self.data) or "Unknown error",
            }
            if self.unverified:
                error_obj["unverified"] = True
            if self.error_category:
                error_obj["error_category"] = self.error_category
            if self.retryable:
                error_obj["retryable"] = True
            if self.required_tier:
                error_obj["required_tier"] = self.required_tier
            # Include data when present (e.g. fill_form partial failures have
            # filled fields, snapshot, and per-field errors the model needs).
            if self.data is not None:
                error_obj["data"] = self.data
            return json.dumps(error_obj, default=str, ensure_ascii=False)

        import json

        try:
            data_str = json.dumps(self.data, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            data_str = str(self.data)

        truncated_note = ', "truncated": true' if self.truncated else ""
        unverified_note = ', "unverified": true' if self.unverified else ""
        # Inner ok:false / success:false is normalised into self.ok by
        # __post_init__, so this branch is already the honest one. Recomputed
        # here so the rendered text stays correct even if a caller mutates
        # ``data`` after construction.
        outer_ok = not _payload_reports_failure(self.data)
        return '{"ok": %s, "data": %s%s%s}' % (
            "true" if outer_ok else "false",
            data_str,
            truncated_note,
            unverified_note,
        )


@dataclass
class ToolCall:
    """Parsed tool invocation from LLM response."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    # Anthropic native tool calling fields
    tool_use_id: str | None = field(default=None, repr=False)
    raw_content: Any = field(default=None, repr=False)
    all_tool_calls: list[dict[str, Any]] | None = field(default=None, repr=False)
    reasoning: str = field(default="", repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall | None:
        """Parse a tool call from LLM JSON response.

        Returns None if the data doesn't represent a valid tool call.
        """
        if data.get("type") != "tool_call":
            return None
        tool = data.get("tool")
        if not isinstance(tool, str) or not tool:
            return None
        args = data.get("args", {})
        if not isinstance(args, dict):
            args = {}
        return cls(
            tool=tool,
            args=args,
            tool_use_id=data.get("tool_use_id"),
            raw_content=data.get("_raw_content"),
            all_tool_calls=data.get("_all_tool_calls"),
        )


@dataclass
class AgentResult:
    """Final result from the agent execution loop.

    Bridges back to the pipeline's ProcessResultDict / PipelineResult.
    """

    ok: bool
    answer: str | None = None
    command: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    iterations_used: int = 0
    cap_state: dict[str, Any] | None = None
    tools_called: list[str] = field(default_factory=list)
    error: str | None = None
    payment_gate: bool = False
    signature_gate: bool = False
    gate_page_url: str | None = None  # Last observed page URL when a gate fired
    confirmation_url: str | None = None
    origin_channel: str | None = None
    confirmation_link_delivery: dict[str, Any] | None = None
    continue_listening: bool | None = None  # Propagated from LLM's final response
