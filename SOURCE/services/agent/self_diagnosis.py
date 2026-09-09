"""Self-diagnosis engine — LLM-powered failure analysis.

When the agent loop encounters a failure, this engine:
1. Takes a DiagnosticContext snapshot
2. Calls the LLM with a diagnostic prompt (NOT the agent loop — just ask())
3. Parses the structured diagnosis
4. Returns a DiagnosisResult with user-facing explanation + developer detail

Critical safety properties:
- 10-second timeout on the diagnostic LLM call
- Recursion impossible: uses BaseLLMProvider.ask(), not route_command()
- If diagnosis itself fails, returns a fallback result (never crashes)
- No new dependencies (stdlib only)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger
from services.llm.no_result import build_ai_no_result

logger = get_logger(__name__)

_DIAGNOSTIC_TIMEOUT = 10.0  # seconds


@dataclass
class DiagnosisResult:
    """Result of self-diagnosis analysis."""

    root_cause: str
    category: str  # config | provider | tool | parse | timeout | rate_limit | unknown
    user_explanation: str
    developer_detail: str
    severity: str  # low | medium | high | critical
    is_transient: bool = False
    suggested_retry: bool = False
    diagnosis_succeeded: bool = True
    raw_llm_response: str | None = None
    no_result: dict[str, Any] | None = None
    error_state: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage."""
        data: dict[str, Any] = {
            "root_cause": self.root_cause,
            "category": self.category,
            "user_explanation": self.user_explanation,
            "developer_detail": self.developer_detail,
            "severity": self.severity,
            "is_transient": self.is_transient,
            "suggested_retry": self.suggested_retry,
            "diagnosis_succeeded": self.diagnosis_succeeded,
        }
        if self.no_result is not None:
            data["no_result"] = self.no_result
        if self.error_state is not None:
            data["error_state"] = self.error_state
        return data


# Sentinel to prevent recursive diagnosis
_DIAGNOSIS_IN_PROGRESS = False


def _diagnostic_prompt_for_provider(context: Any) -> str:
    """Build the provider-facing diagnostic prompt with support redaction applied."""

    try:
        raw_prompt = str(context.to_diagnostic_prompt())
    except (AttributeError, TypeError, ValueError, RuntimeError):
        raw_prompt = ""

    try:
        from diagnostics.support_redaction import redact_support_text

        redacted = redact_support_text(raw_prompt)
        if isinstance(redacted, str) and redacted.strip():
            return redacted
    except (ImportError, TypeError, ValueError, RuntimeError):
        logger.debug("Diagnostic prompt redaction failed", exc_info=True)

    stage = str(getattr(context, "execution_stage", "unknown") or "unknown")
    error_type = str(getattr(context, "error_type", "unknown") or "unknown")
    return (
        "FAILURE CONTEXT:\n"
        "Stage: %s\n"
        "Error type: %s\n"
        "Sensitive diagnostic details were withheld because redaction was unavailable."
    ) % (stage, error_type)


class SelfDiagnosisEngine:
    """LLM-powered failure analysis engine.

    Uses the existing LLM provider's ask() method (NOT route_command)
    to analyze failures. This makes recursion impossible — ask() is a
    simple question/answer call that never triggers the agent loop.
    """

    def __init__(self, llm_provider: Any) -> None:
        """Initialize with an LLM provider instance.

        Args:
            llm_provider: Any object with an async ask() method
                          (BaseLLMProvider or compatible).
        """
        self._llm = llm_provider

    async def diagnose(
        self,
        context: Any,  # DiagnosticContext
    ) -> DiagnosisResult:
        """Analyze a failure and return a structured diagnosis.

        Args:
            context: DiagnosticContext with failure details.

        Returns:
            DiagnosisResult — always returns, never raises.
        """
        global _DIAGNOSIS_IN_PROGRESS

        # Guard 1: Recursion prevention
        if _DIAGNOSIS_IN_PROGRESS:
            logger.warning("Diagnosis recursion detected — returning fallback")
            return self._fallback_result(context, "Diagnosis recursion prevented")

        # Guard 2: LLM provider must have ask()
        if not hasattr(self._llm, "ask"):
            logger.warning("LLM provider has no ask() method — returning fallback")
            return self._fallback_result(context, "LLM provider lacks ask() method")

        _DIAGNOSIS_IN_PROGRESS = True
        try:
            # Build the diagnostic prompt from context without forwarding raw
            # request/tool/trace payloads back through the provider.
            diagnostic_prompt = _diagnostic_prompt_for_provider(context)

            diagnostic_system_prompt = """\
You are a diagnostic engine for Viola, a voice-controlled AI assistant.
A failure occurred during agent execution. Analyze the failure context below
and provide a structured diagnosis.

IMPORTANT RULES:
- Be specific about the root cause. Don't just restate the error.
- If the failure is a known issue (API key, timeout, rate limit), say so clearly.
- If you're not sure, say "uncertain" — don't guess.
- Keep the user explanation simple and jargon-free (1-2 sentences).
- Keep the developer detail technical and actionable.

Respond ONLY with a JSON object in this exact format:
{
  "root_cause": "brief technical root cause",
  "category": "one of: config | provider | tool | parse | timeout | rate_limit | unknown",
  "user_explanation": "simple 1-2 sentence explanation for the user",
  "developer_detail": "technical detail with actionable fix suggestion",
  "severity": "one of: low | medium | high | critical",
  "is_transient": true or false,
  "suggested_retry": true or false
}
"""

            # Call LLM with timeout
            try:
                response = await asyncio.wait_for(
                    self._llm.ask(
                        question=diagnostic_prompt,
                        system_prompt=diagnostic_system_prompt,
                        include_history=False,
                        max_tokens=500,
                        temperature=0.3,
                    ),
                    timeout=_DIAGNOSTIC_TIMEOUT,
                )
            except TimeoutError:
                logger.warning("Diagnostic LLM call timed out after %ss", _DIAGNOSTIC_TIMEOUT)
                return self._fallback_result(context, "Diagnostic call timed out")

            # Extract content from response
            if isinstance(response, dict):
                content = response.get("content", "")
                error = response.get("error")
                if error:
                    logger.warning("Diagnostic LLM returned error: %s", error)
                    return self._fallback_result(context, "LLM error: %s" % error)
            else:
                content = str(response)

            if not content or not content.strip():
                return self._fallback_result(context, "Empty LLM response")

            # Parse structured JSON response
            return self._parse_diagnosis(content, context)
        except Exception as exc:
            logger.warning("Self-diagnosis failed: %s", exc)
            return self._fallback_result(context, str(exc))
        finally:
            _DIAGNOSIS_IN_PROGRESS = False

    def _parse_diagnosis(self, content: str, context: Any) -> DiagnosisResult:
        """Parse LLM response into a DiagnosisResult."""
        # Try to extract JSON from the response
        # The LLM might wrap it in markdown code blocks
        clean = content.strip()
        if clean.startswith("```"):
            # Strip markdown code fences
            lines = clean.split("\n")
            # Remove first and last lines if they're fences
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            clean = "\n".join(lines)

        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            # Try to find JSON object in the text
            start = clean.find("{")
            end = clean.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(clean[start : end + 1])
                except json.JSONDecodeError:
                    logger.warning("Could not parse diagnosis JSON from LLM response")
                    return self._fallback_result(
                        context,
                        "Unparseable LLM response",
                        raw=content[:500],
                    )
            else:
                return self._fallback_result(
                    context,
                    "No JSON in LLM response",
                    raw=content[:500],
                )

        # Validate required fields
        required = (
            "root_cause",
            "category",
            "user_explanation",
            "developer_detail",
            "severity",
        )
        for key in required:
            if key not in data:
                return self._fallback_result(
                    context,
                    "Missing field '%s' in diagnosis" % key,
                    raw=content[:500],
                )

        # Validate enum fields
        valid_categories = {
            "config",
            "provider",
            "tool",
            "parse",
            "timeout",
            "rate_limit",
            "unknown",
        }
        valid_severities = {"low", "medium", "high", "critical"}

        category = data["category"] if data["category"] in valid_categories else "unknown"
        severity = data["severity"] if data["severity"] in valid_severities else "medium"

        return DiagnosisResult(
            root_cause=str(data["root_cause"])[:500],
            category=category,
            user_explanation=str(data["user_explanation"])[:300],
            developer_detail=str(data["developer_detail"])[:1000],
            severity=severity,
            is_transient=bool(data.get("is_transient", False)),
            suggested_retry=bool(data.get("suggested_retry", False)),
            diagnosis_succeeded=True,
            raw_llm_response=content[:1000],
        )

    def _fallback_result(
        self,
        context: Any,
        reason: str,
        raw: str | None = None,
    ) -> DiagnosisResult:
        """Generate a best-effort fallback diagnosis without the LLM.

        Uses heuristics based on the error type and stage to provide
        a basic diagnosis when the LLM call fails or is unavailable.
        """
        error_type = getattr(context, "error_type", None) or ""
        error_msg = getattr(context, "error_message", None) or ""
        stage = getattr(context, "execution_stage", None) or "unknown"

        # Heuristic classification
        category = "unknown"
        user_explanation = "Something went wrong while processing your request."
        developer_detail = "Diagnosis unavailable: %s" % reason
        severity = "medium"
        is_transient = False
        no_result: dict[str, Any] | None = None
        error_state: dict[str, Any] | None = None

        if "timeout" in error_type.lower() or "timeout" in error_msg.lower():
            category = "timeout"
            user_explanation = "That took longer than expected — try again?"
            severity = "low"
            is_transient = True
        elif "insufficient_quota" in error_msg.lower() or "exceeded your current quota" in error_msg.lower():
            # OpenAI insufficient_quota — Viola's managed-LLM bucket is dry, or BYOK billing is out.
            # Category matches dispatch.py's _PROVIDER_OVERLOAD_DIAGNOSTIC_CATEGORIES so the
            # provider_overloaded UX path can fire AND apply the ai_source-aware message
            # (friendly "high volume" for managed/codex/subscription; honest provider-side
            # message for BYOK). The user_explanation here is the canonical friendly text —
            # dispatch.py overrides for BYOK callers.
            category = "provider_api_key_quota"
            user_explanation = "Viola is experiencing high volume. Please try again in a moment."
            severity = "high"
            is_transient = True
        elif "rate" in error_type.lower() or "429" in error_msg:
            # General 429 (not specifically quota exhaustion) — same operator-facing
            # category vocabulary as dispatch.py so the provider_overloaded path can
            # fire. User-visible message is the same friendly text for managed users.
            category = "provider_rate_limit"
            user_explanation = "Viola is experiencing high volume. Please try again in a moment."
            severity = "low"
            is_transient = True
        elif "api_key" in error_msg.lower() or "auth" in error_type.lower() or "401" in error_msg:
            category = "config"
            user_explanation = "There's a configuration issue with the AI service."
            severity = "high"
        elif stage == "tool_execution":
            category = "tool"
            user_explanation = "A tool encountered an error. Please try again."
            severity = "medium"
            is_transient = True
        elif stage == "response_parse":
            category = "parse"
            user_explanation = ""
            severity = "low"
            is_transient = True
            no_result_payload = build_ai_no_result(
                "diagnosis_response_parse_failed",
                retryable=True,
                failure_stage=stage,
            )
            no_result = no_result_payload["no_result"]
            error_state = no_result_payload["error_state"]
        return DiagnosisResult(
            root_cause="%s: %s" % (error_type or "Error", error_msg[:200] if error_msg else "unknown"),
            category=category,
            user_explanation=user_explanation,
            developer_detail=developer_detail,
            severity=severity,
            is_transient=is_transient,
            suggested_retry=is_transient,
            diagnosis_succeeded=False,
            raw_llm_response=raw,
            no_result=no_result,
            error_state=error_state,
        )
