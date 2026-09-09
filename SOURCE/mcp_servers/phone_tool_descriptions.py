"""Shared model-visible descriptions for Viola outbound phone-call tools."""

from __future__ import annotations

# The core ``phone`` MCP tool is the single model-visible phone-calling
# surface. It multiplexes call/status/end/transcript actions while delegating
# call origination to the cloud-only implementation in ``intent.tools.phone_call``.

OUTBOUND_PHONE_CALL_DESCRIPTION = (
    "Dial an outbound phone call through Viola's phone runtime and have Viola conduct an autonomous voice "
    "conversation to complete a concrete task for the user. Covers business or service calls such as "
    "booking appointments, placing orders, checking availability, making reservations, requesting quotes, "
    "confirming or canceling bookings, or asking about hours or services. Emergency or crisis calls such "
    "as 911 or 988 are excluded; the user must call emergency services or 988 directly. "
    "Repeated calls to the same number require "
    "the user's explicit instruction. If the destination number is unknown, ask the user for the phone number "
    "to call, including its country code, before calling. A call is a means to obtain or confirm a deliverable "
    "for the user; the task is not complete just because Viola says the opening words."
)

PHONE_NUMBER_DESCRIPTION = (
    "Destination phone number to dial, including the country code, e.g. '+13125551234'. If it is missing, ask "
    "the user for the number in plain, everyday words (with the country code) before calling."
)
TASK_DESCRIPTION = (
    "Outcome the in-call agent should accomplish. Describe the information, confirmation, decision, booking, "
    "order, quote, or message to bring back to the user, not only words to say."
)
CALLER_NAME_DESCRIPTION = "Optional caller name for the call."
EXTRA_CONTEXT_DESCRIPTION = (
    "Optional extra context for the call, such as preferences, account details the user provided, or constraints."
)

PHONE_CALL_SHARED_PARAMETER_DESCRIPTIONS = {
    "phone_number": PHONE_NUMBER_DESCRIPTION,
    "task": TASK_DESCRIPTION,
    "caller_name": CALLER_NAME_DESCRIPTION,
    "extra_context": EXTRA_CONTEXT_DESCRIPTION,
}

PHONE_ACTION_DESCRIPTION = "Phone action. Use one of: 'call', 'status', 'end', or 'transcript'."
WAIT_FOR_COMPLETION_DESCRIPTION = (
    "For action='call', false returns after Telnyx origination with a call_id; true waits for completion."
)
CALL_ID_DESCRIPTION = "Call ID for action='status', 'end', or 'transcript'."

CORE_PHONE_TOOL_DESCRIPTION = (
    "Make and manage AI-driven phone calls. "
    + OUTBOUND_PHONE_CALL_DESCRIPTION
    + " Actions: call (needs the destination phone number with its country code), status, end, transcript. action='call' starts a call; "
    "status monitors progress, transcript retrieves the outcome, and end terminates an active call early."
)
