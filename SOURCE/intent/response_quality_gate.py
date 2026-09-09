"""Response Quality Gate - the sensitive-data solicitation safety boundary.

This gate has a single remaining job: HARD-BLOCK any assistant message that
solicits credit-card / bank / SSN / CVV / payment credentials in chat. Viola
uses Stripe (an external checkout page) and never collects financial details
conversationally, so soliciting them is never legitimate - it is a security/
compliance boundary, not a quality judgment.

DEBOX (2026-05-29, boxing audit W2 intent lane): the template-leak rewrite, the
hallucinated-action disclaimer, and the fabricated-data disclaimer were removed.
Those regex-scanned the model's own free-text answer and rewrote/appended to it
when a keyword pattern fired - second-guessing the model's output, which is the
boxing class CLAUDE.md forbids. Unsupported-claim avoidance belongs in the
unified prompt + structured tool evidence, not a post-hoc regex mutation. Claude
Code TS uses structural success/error checks (utils/queryHelpers.ts), never a
final-answer quality regex.

The sensitive-data block stays because it is a named security boundary, not a
prompt-shaped quality override: it defends against the concrete attack of the
model being steered into harvesting financial credentials in conversation.
"""

from __future__ import annotations

import re

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Sensitive data solicitation detection (SAFETY CRITICAL)
# ---------------------------------------------------------------------------
# Catches when the assistant asks the user for sensitive financial or personal
# data. This should NEVER happen: Viola uses Stripe for payments (external
# checkout page) and never collects credit cards, SSNs, or passwords in
# conversation. This check REPLACES the entire response and fires
# unconditionally - it is a hard security boundary, not a quality heuristic.
_SENSITIVE_SOLICITATION_RE = re.compile(
    r"(?:"
    # Direct request for explicitly dangerous data (credit card, SSN, bank).
    # "your/the/a" is optional here - soliciting these is NEVER legitimate.
    r"(?:provide|enter|give|share|input|submit|type|confirm)\s+"
    r"(?:me\s+)?(?:your\s+|the\s+|a\s+)?"
    r"(?:credit\s+card|debit\s+card|card\s+number|card\s+info|card\s+details"
    r"|bank\s+(?:account|details|info|number|routing)"
    r"|social\s+security|ssn|cvv|cvc|expir(?:y|ation)"
    r"|pin\s+(?:number|code)|security\s+code)"
    r"|"
    # "payment details/info" requires "your" to avoid false positives on
    # merchant-flow descriptions like "enter payment details on their checkout"
    r"(?:provide|enter|give|share|input|submit|type|confirm)\s+"
    r"(?:me\s+)?your\s+payment\s+(?:info|details|method|credentials)"
    r"|"
    # Question form: "what is your credit card number", "what's your SSN"
    r"what(?:'s|\s+is)\s+your\s+" r"(?:credit\s+card|card\s+number|ssn|social\s+security|bank\s+account|pin)" r"|"
    # Need/require pattern: "I'll need your credit card", "I will need your card details"
    # Note: bare "payment" is omitted - "I'll need your payment" is a legitimate
    # commerce-flow phrase (PAYMENT_GATE explanations).  We still catch the
    # dangerous forms via "payment\s+(?:info|details|credentials)" below.
    r"(?:I(?:'ll|\s+will)?\s+need|I\s+require|we(?:'ll|\s+will)?\s+need)\s+"
    r"(?:your|the)\s+"
    r"(?:credit\s+card|card\s+(?:number|info|details)|bank|ssn|social\s+security"
    r"|payment\s+(?:info|details|credentials))"
    r"|"
    # Ready to collect: "ready to input your payment details"
    r"ready\s+to\s+(?:input|process|collect|take)\s+" r"(?:your|the)\s+(?:payment|card|credit|financial)" r")",
    re.IGNORECASE,
)

_SENSITIVE_SOLICITATION_REPLACEMENT = (
    "I never ask for credit card or financial details in chat. "
    "For purchases, I'll build your cart on the merchant's site and send you "
    "a secure payment link to complete checkout."
)


def apply_response_quality_gate(
    message: str,
    command_results: object = None,
    tools_called: list[str] | None = None,
) -> str:
    """Block sensitive-data solicitation in an assistant response.

    The ``command_results`` / ``tools_called`` parameters are retained for
    caller compatibility but are unused: the sensitive-data block is a hard
    security boundary that fires regardless of tool-call backing.

    Returns the original message, or the safe replacement when the response
    solicits financial/credential data.
    """
    if not isinstance(message, str) or not message.strip():
        return message

    if _SENSITIVE_SOLICITATION_RE.search(message):
        logger.warning("Quality gate: BLOCKED sensitive data solicitation in response")
        return _SENSITIVE_SOLICITATION_REPLACEMENT

    return message
