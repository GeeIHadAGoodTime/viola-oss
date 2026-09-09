"""Structured no-result envelopes for LLM and audio failure paths."""

from __future__ import annotations

from typing import Any

NO_RESULT_RETRY_INSTRUCTION = (
    "The previous model call returned no assistant content. Re-run the same request and return the "
    "required structured response: tool_call, answer, ignore, or final_answer as appropriate. "
    "Do not return empty content."
)


def reason_for_error_category(category_name: str | None, operation: str) -> str:
    """Return a stable no-result reason for a classified provider error."""

    normalized_category = str(category_name or "").upper()
    normalized_operation = operation.strip().lower().replace(" ", "_") or "llm"
    if normalized_category.startswith("EXPECTED_TIMEOUT"):
        suffix = "timeout"
    elif normalized_category.startswith("EXPECTED_NETWORK"):
        suffix = "network_error"
    elif normalized_category.startswith("EXPECTED_RATE_LIMIT"):
        suffix = "rate_limited"
    elif normalized_category.startswith("EXPECTED_"):
        suffix = "expected_error"
    else:
        suffix = "exception"
    return "%s_%s" % (normalized_operation, suffix)


def build_ai_no_result(
    reason: str,
    *,
    retryable: bool = True,
    retry_attempted: bool | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    """Return the canonical structured signal for empty or unusable LLM output."""

    no_result: dict[str, Any] = {
        "reason": reason,
        "retryable": retryable,
    }
    if retry_attempted is not None:
        no_result["retry_attempted"] = retry_attempted
    for key, value in metadata.items():
        if value is not None:
            no_result[key] = value

    return {
        "type": "ai_no_result",
        "reason": reason,
        "retryable": retryable,
        "no_result": dict(no_result),
        "error_state": {
            "type": "ai_no_result",
            **no_result,
        },
    }


def build_ai_error_no_result(
    operation: str,
    *,
    category_name: str | None = None,
    exception: BaseException | None = None,
    retry_attempted: bool | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    """Return the canonical no-result envelope for a classified LLM exception."""

    retryable = bool(category_name and str(category_name).upper().startswith("EXPECTED_"))
    return build_ai_no_result(
        reason_for_error_category(category_name, operation),
        retryable=retryable,
        retry_attempted=retry_attempted,
        error_category=category_name,
        exception_type=type(exception).__name__ if exception is not None else None,
        **metadata,
    )
