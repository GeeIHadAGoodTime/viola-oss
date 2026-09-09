"""Diagnostic context capture for agent self-diagnosis.

When anything goes wrong during agent execution, this module captures
a structured snapshot of everything relevant at the moment of failure.
This snapshot feeds the LLM diagnostic call in the self-diagnosis engine.
"""

from __future__ import annotations

import hashlib
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _normalize_user_id(user_id: str | None) -> str | None:
    raw = str(user_id or "").strip()
    if not raw or raw.lower() == "default":
        return None
    return raw


def _user_id_hash(user_id: str | None) -> str | None:
    normalized = _normalize_user_id(user_id)
    if normalized is None:
        return None
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class DiagnosticContext:
    """Structured snapshot of everything relevant at failure time."""

    timestamp: str
    user_request: str
    execution_stage: str  # preflight | llm_call | tool_execution | response_parse | agent_loop | unknown
    user_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    llm_response_raw: str | None = None
    llm_finish_reason: str | None = None
    tool_call_attempted: str | None = None
    tool_call_result: str | None = None
    agent_iteration: int = 0
    system_state: dict[str, Any] = field(default_factory=dict)
    settings_snapshot: dict[str, Any] = field(default_factory=dict)
    available_tools: list[str] = field(default_factory=list)
    recent_failures: list[str] = field(default_factory=list)
    # True ONLY when this context wraps a user-authored support/bug-report body --
    # a deliberate message the user typed to our own support channel. That body is
    # stored/forwarded VERBATIM (never redacted): redacting it destroys exactly what
    # the user chose to send us (a contact address, or a secret they are deliberately
    # disclosing to the founder). Auto-captured agent-diagnostic context is the
    # default (False) and stays redacted through the shared bug-ticket store.
    user_authored: bool = False

    @classmethod
    def capture_from_exception(
        cls,
        user_request: str,
        exception: BaseException,
        *,
        stage: str = "unknown",
        agent_iteration: int = 0,
        llm_response_raw: str | None = None,
        llm_finish_reason: str | None = None,
        tool_call_attempted: str | None = None,
        tool_call_result: str | None = None,
        settings: object | None = None,
        provider: object | None = None,
        mcp_hub: object | None = None,
        user_id: str | None = None,
    ) -> DiagnosticContext:
        """Capture context from an exception."""
        # Format traceback (last 10 frames max)
        tb_lines = traceback.format_exception(type(exception), exception, exception.__traceback__)
        tb_text = "".join(tb_lines)
        # Limit to ~last 10 frames
        tb_frames = tb_text.split("\n")
        if len(tb_frames) > 30:
            tb_text = "\n".join(tb_frames[-30:])

        ctx = cls(
            timestamp=datetime.now(tz=UTC).isoformat(),
            user_request=user_request[:500],
            execution_stage=stage,
            user_id=_normalize_user_id(user_id),
            error_type=type(exception).__name__,
            error_message=str(exception)[:1000],
            error_traceback=tb_text[:3000],
            llm_response_raw=llm_response_raw[:2000] if llm_response_raw else None,
            llm_finish_reason=llm_finish_reason,
            tool_call_attempted=tool_call_attempted,
            tool_call_result=tool_call_result[:500] if tool_call_result else None,
            agent_iteration=agent_iteration,
        )
        ctx._populate_system_state(settings, provider, mcp_hub)
        return ctx

    @classmethod
    def capture_from_response(
        cls,
        user_request: str,
        llm_response: dict[str, Any] | str,
        *,
        stage: str = "response_parse",
        detail: str = "",
        agent_iteration: int = 0,
        settings: object | None = None,
        provider: object | None = None,
        mcp_hub: object | None = None,
        user_id: str | None = None,
    ) -> DiagnosticContext:
        """Capture context from a problematic LLM response."""
        raw = str(llm_response)[:2000] if llm_response else None
        finish_reason = None
        if isinstance(llm_response, dict):
            finish_reason = llm_response.get("stop_reason") or llm_response.get("finish_reason")

        ctx = cls(
            timestamp=datetime.now(tz=UTC).isoformat(),
            user_request=user_request[:500],
            execution_stage=stage,
            user_id=_normalize_user_id(user_id),
            error_type="ResponseError" if detail else None,
            error_message=detail[:1000] if detail else None,
            llm_response_raw=raw,
            llm_finish_reason=str(finish_reason) if finish_reason else None,
            agent_iteration=agent_iteration,
        )
        ctx._populate_system_state(settings, provider, mcp_hub)
        return ctx

    @classmethod
    def capture_generic(
        cls,
        user_request: str,
        stage: str,
        detail: str,
        *,
        agent_iteration: int = 0,
        settings: object | None = None,
        provider: object | None = None,
        mcp_hub: object | None = None,
        user_id: str | None = None,
    ) -> DiagnosticContext:
        """Capture context for a generic (non-exception) failure."""
        ctx = cls(
            timestamp=datetime.now(tz=UTC).isoformat(),
            user_request=user_request[:500],
            execution_stage=stage,
            user_id=_normalize_user_id(user_id),
            error_message=detail[:1000] if detail else None,
            agent_iteration=agent_iteration,
        )
        ctx._populate_system_state(settings, provider, mcp_hub)
        return ctx

    def _populate_system_state(
        self,
        settings: object | None,
        provider: object | None,
        mcp_hub: object | None,
    ) -> None:
        """Fill in system_state, settings_snapshot, and available_tools."""
        # Settings snapshot
        if settings is not None:
            config = getattr(provider, "config", None) if provider else None
            self.settings_snapshot = {
                "agent_enabled": getattr(settings, "agent_enabled", None),
                "agent_iteration_cap": None,
                "agent_timeout_seconds": getattr(settings, "agent_timeout_seconds", None),
                "llm_backend": getattr(settings, "llm_backend", None),
                "llm_max_tokens_cap": getattr(settings, "llm_max_tokens_cap", None),
                "model": getattr(config, "model", None) if config else None,
                "provider_type": getattr(config, "provider", None) if config else None,
                "provider_available": (
                    provider.is_available() if provider and hasattr(provider, "is_available") else None
                ),
            }

        # Available tools
        if mcp_hub is not None:
            try:
                tools_list = mcp_hub.list_tools()
                self.available_tools = [t.get("name", "") for t in tools_list][:50]
            except Exception:
                self.available_tools = ["<error listing tools>"]

        # System state from AIDebugPayload (best-effort)
        try:
            from diagnostics.ai_debugger import collect_diagnostics

            payload = collect_diagnostics(app_state=None, include_raw_metrics=False)
            self.system_state = {
                "overall_status": payload.overall_status.value,
                "health_score": payload.health_score,
                "summary": payload.summary,
                "recent_failures": [str(f)[:100] for f in payload.recent_failures[-5:]],
            }
            self.recent_failures = [str(f)[:100] for f in payload.recent_failures[-5:]]
        except Exception:
            self.system_state = {"overall_status": "unavailable"}

    def to_diagnostic_prompt(self) -> str:
        """Format context as a readable prompt for the diagnostic LLM call."""
        lines: list[str] = []
        lines.append("FAILURE CONTEXT:")
        lines.append('User asked: "%s"' % self.user_request[:200])
        lines.append("Stage: %s" % self.execution_stage)
        lines.append("Iteration: %d" % self.agent_iteration)

        if self.error_type:
            lines.append("Error: %s: %s" % (self.error_type, self.error_message or ""))
        elif self.error_message:
            lines.append("Detail: %s" % self.error_message)

        if self.tool_call_attempted:
            lines.append("Tool attempted: %s" % self.tool_call_attempted)
        if self.tool_call_result:
            lines.append("Tool result: %s" % self.tool_call_result[:200])

        lines.append("")
        lines.append("SYSTEM STATE:")
        overall = self.system_state.get("overall_status", "unknown")
        health = self.system_state.get("health_score", "?")
        lines.append("Overall: %s (health score: %s)" % (overall, health))

        provider_type = self.settings_snapshot.get("provider_type", "unknown")
        model = self.settings_snapshot.get("model", "unknown")
        lines.append("LLM Provider: %s (%s)" % (provider_type, model))

        max_tokens = self.settings_snapshot.get("llm_max_tokens_cap", "?")
        lines.append("Max tokens cap: %s" % max_tokens)

        tool_count = len(self.available_tools)
        tool_names = ", ".join(self.available_tools[:10])
        if tool_count > 10:
            tool_names += ", ... (%d more)" % (tool_count - 10)
        lines.append("Tools available (%d): %s" % (tool_count, tool_names))

        if self.recent_failures:
            lines.append("Recent failures: %s" % "; ".join(self.recent_failures[:3]))
        else:
            lines.append("Recent failures: none")

        if self.llm_finish_reason:
            lines.append("LLM finish reason: %s" % self.llm_finish_reason)

        if self.llm_response_raw:
            lines.append("")
            lines.append("RAW LLM RESPONSE:")
            lines.append(self.llm_response_raw[:500])

        if self.error_traceback:
            lines.append("")
            lines.append("TRACEBACK (last frames):")
            # Only include last 10 lines of traceback
            tb_lines = self.error_traceback.strip().split("\n")
            lines.extend(tb_lines[-10:])

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage."""
        return {
            "timestamp": self.timestamp,
            "user_id_hash": _user_id_hash(self.user_id),
            "user_request": self.user_request,
            "execution_stage": self.execution_stage,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "error_traceback": self.error_traceback,
            "llm_response_raw": self.llm_response_raw,
            "llm_finish_reason": self.llm_finish_reason,
            "tool_call_attempted": self.tool_call_attempted,
            "tool_call_result": self.tool_call_result,
            "agent_iteration": self.agent_iteration,
            "system_state": self.system_state,
            "settings_snapshot": self.settings_snapshot,
            "available_tools": self.available_tools,
            "recent_failures": self.recent_failures,
        }
