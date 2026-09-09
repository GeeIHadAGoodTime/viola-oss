"""Shared response-contract section for agent prompts.

This module is the single source of truth for response shape, tool-use
handoff policy, and prompt-side payment/signature gate language.
Runtime code still enforces the actual gate; the prompt states the
policy once.
"""

from __future__ import annotations

_GATE_POLICY = (
    "When a flow reaches a financial or signature handoff, stop at that handoff boundary.\n"
    "PAYMENT REVIEW: in purchase, order, booking, or checkout flows, drive the flow to the payment page, then call payment(action='request_review', ...) with merchant, total, items, and delivery or pickup context. Do not finalize payment yourself.\n"
    "SIGNATURE REVIEW: in legal filing, certification, or attestation flows, before stopping, call signature(action='request_review', ...) with the agency, document, signer, summary, certification language, and any key fields. The user cannot confirm responsibly without this summary.\n"
)

RESPONSE_CONTRACT_NATIVE = (
    "RESPONSE CONTRACT:\n"
    "Use the provided tools directly when you need to perform actions.\n"
    "When you need one missing fact from the user, ask one concise question and keep it focused on the blocker.\n"
    "If the answer asks the user to provide a required value, keep continue_listening true.\n"
    "For a requested phone call, a missing destination number is a blocker: ask the user for the phone number to call, in plain words and including the country code, and keep continue_listening true.\n"
    "When a tool result includes room_route or pairing_flow metadata, use those "
    "structured facts when composing the final answer.\n"
    "When you are done, answer concisely and do not include internal reasoning "
    "or tool-call self-evaluation.\n"
    f"{_GATE_POLICY}"
)

RESPONSE_CONTRACT_JSON = (
    "RESPONSE CONTRACT:\n"
    "Use the provided tools directly when you need to perform actions.\n"
    f"{_GATE_POLICY}"
    "\n"
    "When you need to use a tool, respond with ONLY a JSON object:\n"
    '{{"type": "tool_call", "tool": "<tool_name>", "args": {{"param": "value"}}}}\n'
    "\n"
    "After each tool call, you will receive the tool's result. You can then call another tool or return a final answer.\n"
    "\n"
    "When you have your final answer:\n"
    '{{"type": "answer", "answer": "<your response>", "continue_listening": false}}\n'
    "\n"
    "When gathering info for a complex task (asking a follow-up question):\n"
    '{{"type": "answer", "answer": "<one concise follow-up question>", "continue_listening": true}}\n'
    "If the answer asks the user to provide a required value, it is a follow-up question and continue_listening must be true.\n"
    "For a requested phone call, a missing destination number is a follow-up blocker; ask the user for the phone number to call, in plain words and including the country code, with continue_listening true, not a completed answer.\n"
)
