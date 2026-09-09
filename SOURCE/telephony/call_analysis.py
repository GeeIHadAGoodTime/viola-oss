"""Call outcome analysis — determines success/failure and retry recommendations.

Analyzes CallRecord state, transcript content, and summary to produce a
structured CallOutcome used for smart retry decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from telephony.call_manager import CallRecord

logger = get_logger(__name__)


@dataclass
class CallOutcome:
    """Structured analysis of a completed call."""

    success: bool = False
    completion_level: str = "failed"  # complete, partial, failed, no_answer
    what_worked: list[str] = field(default_factory=list)
    what_failed: list[str] = field(default_factory=list)
    retry_recommended: bool = False
    retry_suggestions: list[str] = field(default_factory=list)


@dataclass
class CallExtraction:
    """Structured data extracted from a call transcript via LLM."""

    items_ordered: list[str] = field(default_factory=list)
    total_price: str | None = None
    appointment_date: str | None = None
    appointment_time: str | None = None
    party_size: int | None = None
    confirmation_number: str | None = None
    estimated_wait: str | None = None
    business_name: str | None = None
    delivery_address: str | None = None
    callback_number: str | None = None
    payment_method: str | None = None
    needs_callback: bool = False
    callback_reason: str | None = None


# Keyword sets for heuristic analysis
_SUCCESS_KEYWORDS = frozenset(
    [
        "confirmed",
        "booked",
        "reserved",
        "all set",
        "order placed",
        "scheduled",
        "appointment",
        "confirmation number",
        "you're all set",
        "perfect",
        "sounds good",
        "we got you",
    ]
)

_PARTIAL_KEYWORDS = frozenset(
    [
        "unavailable",
        "can't",
        "cannot",
        "not available",
        "sold out",
        "no openings",
        "call back",
        "try again",
        "full",
        "closed",
        "busy",
        "wait list",
    ]
)

_FAILURE_KEYWORDS = frozenset(
    [
        "wrong number",
        "disconnected",
        "not in service",
        "hang up",
        "hung up",
        "voicemail",
        "leave a message",
        "after the beep",
    ]
)


def analyze_call_outcome(record: CallRecord) -> CallOutcome:
    """Analyze a completed call and determine its outcome.

    Uses call status, transcript keywords, and summary content to
    produce a structured outcome. Starts with simple heuristics —
    will be tuned with real call data.

    Args:
        record: Completed CallRecord with transcript and status.

    Returns:
        CallOutcome with success assessment and retry recommendations.
    """
    from telephony.call_manager import CallStatus

    outcome = CallOutcome()

    # 1. Status-based classification
    if record.status == CallStatus.NO_ANSWER:
        outcome.completion_level = "no_answer"
        outcome.what_failed.append("No one answered the call")
        outcome.retry_recommended = True
        outcome.retry_suggestions.append("Try calling at a different time")
        return outcome

    if record.status == CallStatus.TIMEOUT:
        outcome.completion_level = "failed"
        outcome.what_failed.append("Call exceeded maximum duration")
        outcome.retry_recommended = True
        outcome.retry_suggestions.append("Be more concise and focused on the main objective")
        return outcome

    if record.status == CallStatus.FAILED:
        outcome.completion_level = "failed"
        outcome.what_failed.append("Call failed: %s" % (record.error or "unknown error"))
        outcome.retry_recommended = True
        outcome.retry_suggestions.append("Verify the phone number and try again")
        return outcome

    if record.status == CallStatus.CANCELLED:
        outcome.completion_level = "failed"
        outcome.what_failed.append("Call was cancelled")
        return outcome

    if record.status == CallStatus.VOICEMAIL:
        outcome.completion_level = "no_answer"
        outcome.what_failed.append("Reached voicemail")
        outcome.retry_recommended = True
        outcome.retry_suggestions.append("Try calling during business hours")
        return outcome

    # 2. Transcript-based analysis (for completed calls)
    transcript_text = " ".join(entry.get("text", "") for entry in record.transcript).lower()
    summary_text = (record.summary or "").lower()
    combined = transcript_text + " " + summary_text

    # Check for success indicators
    success_hits = [kw for kw in _SUCCESS_KEYWORDS if kw in combined]
    partial_hits = [kw for kw in _PARTIAL_KEYWORDS if kw in combined]
    failure_hits = [kw for kw in _FAILURE_KEYWORDS if kw in combined]

    if failure_hits:
        outcome.completion_level = "failed"
        outcome.what_failed.extend(failure_hits)
        outcome.retry_recommended = True
        if "wrong number" in combined:
            outcome.retry_suggestions.append("Verify the phone number before retrying")
        elif "voicemail" in combined or "leave a message" in combined:
            outcome.retry_suggestions.append("Try calling during business hours")
        else:
            outcome.retry_suggestions.append("Try a different approach or time")
    elif success_hits and not partial_hits:
        outcome.success = True
        outcome.completion_level = "complete"
        outcome.what_worked.extend(success_hits)
    elif partial_hits:
        outcome.completion_level = "partial"
        outcome.what_worked.extend(success_hits)
        outcome.what_failed.extend(partial_hits)
        outcome.retry_recommended = True
        if "unavailable" in combined or "sold out" in combined:
            outcome.retry_suggestions.append("Ask about alternatives or different options")
        if "call back" in combined:
            outcome.retry_suggestions.append("Call back at the suggested time")
        if not outcome.retry_suggestions:
            outcome.retry_suggestions.append("Retry with a modified approach")
    elif not record.transcript:
        outcome.completion_level = "no_answer"
        outcome.what_failed.append("No conversation recorded")
        outcome.retry_recommended = True
        outcome.retry_suggestions.append("Try calling again")
    else:
        # Transcript exists but no strong signal — default to partial success
        outcome.completion_level = "partial"
        outcome.what_worked.append("Conversation took place")
        outcome.retry_recommended = False

    logger.info(
        "Call %s outcome: %s (success=%s, retry=%s)",
        record.call_id,
        outcome.completion_level,
        outcome.success,
        outcome.retry_recommended,
    )
    return outcome
