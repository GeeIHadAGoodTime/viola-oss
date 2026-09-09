"""Phone call tools — tool definitions and handlers for agent-tier use.

Provides:
    - consult_user: Mid-call user consultation (F1)
    - present_call_plan: Pre-call briefing and approval (F4)

consult_user routes through the call issuer channel stored on CallRecord.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger
from telephony.end_call_hangup import (
    EndCallHangupFrame,
    dispatch_telnyx_end_call_hangup,
)
from telephony.telnyx_dial_response import telnyx_dial_call_control_id
from telephony.user_settings_lookup import (
    first_setting_value,
    load_cloud_user_settings_blob,
)

logger = get_logger(__name__)

CONSULT_USER_FALLBACK_ANSWER = (
    "user did not respond in time. "
    "Do NOT tell the recipient you could not reach the user or anyone else — never narrate an internal "
    "timeout or missed contact to the call recipient. Instead, proceed smoothly by assuming the sensible "
    "default: for trivial ordering details such as pickup time, delivery preference, or size when no "
    "specific preference was stated in the task, assume the most natural choice (for example, 'as soon "
    "as it is ready' for a pickup time on a food order). Continue the call as if you had a clear answer, "
    "without mentioning any delay, hold, or attempt to reach someone."
)
_VOICE_CONSULT_TIMEOUT = 20.0
_MESSAGING_CONSULT_TIMEOUT = 300.0

# The live phone pipeline (pipecat's LLMService) enforces a per-function-call
# timeout: if a handler's result_callback has not fired within the registered
# ``timeout_secs`` (pipecat DEFAULT 10.0s when a registration omits it), pipecat
# delivers a None result and closes the function call — and the handler's REAL
# result, arriving later, is DISCARDED ("FunctionCallResultFrame ... is not
# running"), so the LLM is never re-run and the call dead-airs (capstone call
# 67105503, 2026-07-02: consult_user genuinely waited the 20s voice window,
# pipecat killed the call at its 10s default, the fallback answer at ~31s was
# discarded, and Viola stayed silent until the recipient hung up). Pipecat's
# clock starts BEFORE the handler runs, so a registered timeout merely EQUAL to
# the handler's internal wait still always loses the race. Any handler that can
# internally wait MUST therefore register a pipeline timeout that exceeds its
# worst-case internal wait — derived from the SAME constants, never a second
# hardcoded number that can drift. The margin absorbs the handler's pre/post-
# wait work (broadcasts, settings lookups) and event-loop contention (the
# capstone showed ~11s of overshoot beyond the nominal 20s wait while
# hold-mode listen windows cycled).
PIPELINE_FUNCTION_TIMEOUT_MARGIN_SECS = 30.0


def consult_user_pipeline_timeout_secs() -> float:
    """Pipeline (pipecat) function-call timeout for consult_user registrations.

    consult_user's worst-case internal wait is the MESSAGING consult window:
    voice-channel issuers wait the short voice window, but every other issuer
    shape — a messaging channel via ``_ask_call_issuer``, or a live web/desktop
    channel routed through the web-consult future wait — can hold up to the
    messaging window. The registered pipeline timeout must exceed that, or the
    pipeline discards the consult answer and the LLM never re-runs.
    """
    return max(_VOICE_CONSULT_TIMEOUT, _MESSAGING_CONSULT_TIMEOUT) + PIPELINE_FUNCTION_TIMEOUT_MARGIN_SECS


def _consult_handler_wait_budget_secs() -> float:
    """Hard ceiling on the consult handler's OWN total wait.

    Strictly inside the registered pipeline window (half the margin before the
    pipecat cutoff), so the graceful proceed-with-default fallback ALWAYS fires
    and reaches the LLM before the pipeline could close the function call and
    discard it — even if an issuer channel violates its ask() timeout contract
    or the event loop runs hot. The invariant: the pipeline never discards a
    consult outcome; an answer or the fallback is always delivered and re-run.
    """
    return max(_VOICE_CONSULT_TIMEOUT, _MESSAGING_CONSULT_TIMEOUT) + PIPELINE_FUNCTION_TIMEOUT_MARGIN_SECS / 2.0


@dataclass
class _PendingConsultation:
    future: asyncio.Future[str]
    question: str
    user_id: str


_PENDING_CONSULTATIONS: dict[str, _PendingConsultation] = {}

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

CONSULT_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "consult_user",
        "description": (
            "Ask the user (your boss) a question when you need guidance. "
            "Routes through the original call issuer channel, not the live phone recipient. "
            "Use when the next reply requires something only the user can decide and did not already give you: "
            "choosing among options, stating availability, giving approval, setting a spending limit, or supplying "
            "missing contact/account/signer/address/identity details. Also use when: unexpected price, unavailable "
            "option, need a decision, something doesn't match the task, OR the recipient offers a substitution "
            "outside the originally pre-decided task scope. Even when you have explicit "
            "task criteria that would let you refuse autonomously, fire consult_user "
            "FIRST when a substitution is offered — the user may want to accept the "
            "substitution, and refusing without consulting treats you as the decision-maker "
            "on user-scope questions. Never use for questions about your own identity or "
            "whether you are AI; answer those directly from the phone prompt. "
            "The user's reply arrives as the tool result in the same turn — when "
            "consult_user returns, IMMEDIATELY produce the substantive response that "
            "incorporates their answer. Do NOT emit a placeholder ('Give me a moment', "
            "'Let me check', 'One sec') after consult_user fires; the answer is already "
            "in your context and the recipient is on the line waiting for a real reply. "
            "If the result says the user did not respond in time, follow its instructions "
            "exactly: proceed with the sensible default for trivial details (pickup time, "
            "size, preference) and NEVER tell the recipient you could not reach anyone — "
            "that is an internal detail the recipient must never hear. Speak naturally as "
            "if you had a clear answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What to ask the user",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "How urgent the question is",
                },
            },
            "required": ["question"],
        },
    },
}

PRESENT_CALL_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "present_call_plan",
        "description": ("Present the call plan to the user BEFORE dialing. Always use this before make_phone_call."),
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string"},
                "business_name": {"type": "string"},
                "objective": {
                    "type": "string",
                    "description": "What you plan to accomplish",
                },
                "talking_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key points you plan to cover",
                },
                "fallback_plan": {
                    "type": "string",
                    "description": "What to do if primary objective fails",
                },
            },
            "required": ["phone_number", "objective", "talking_points"],
        },
    },
}

PRESS_BUTTON_TOOL = {
    "type": "function",
    "function": {
        "name": "press_button",
        "description": (
            "Press a digit on the phone keypad. Use when an automated menu asks for a "
            "digit/keypad entry. Pick the menu option whose label matches the task. "
            "Not for optional hold-line choices such as callback, keep holding, "
            "or disconnect when the recipient has already asked you to hold. "
            "For multi-digit entry (member IDs, PINs, codes, dates, ZIPs, phone numbers), "
            "emit ALL press_button calls in ONE assistant response as parallel tool calls — "
            "one per character in exact order, including any terminator like `#` or `*`. "
            "The phone system batches them; emitting one at a time misroutes the entry. "
            "Example: to enter `9823#`, one response with five press_button calls in "
            "order: 9, 8, 2, 3, #. "
            "Never use keypad tones to transmit payment credentials (card numbers, CVCs); "
            "those route through the secure confirmation flow, not press_button."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "digit": {
                    "type": "string",
                    "enum": [
                        "0",
                        "1",
                        "2",
                        "3",
                        "4",
                        "5",
                        "6",
                        "7",
                        "8",
                        "9",
                        "*",
                        "#",
                    ],
                    "description": "The digit to press",
                },
            },
            "required": ["digit"],
        },
    },
}

END_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "end_call",
        "description": (
            "Hang up the phone. You MUST emit this tool in the SAME response as your spoken "
            "goodbye — speaking a goodbye without end_call in that response leaves the line open. "
            "This LITERALLY terminates the call — there is no "
            "way to resume after end_call. Use ONLY after the deliverable is captured "
            "(booking confirmed, answer received, message left, or recipient cannot "
            "help) AND the recipient has signaled the call is closing. A terminal "
            "recording, including voicemail handled or mailbox full, counts as the "
            "recipient being unable to help and does not need a live closing signal. "
            "For a full mailbox, say that no message can be left and call this tool "
            "in the same assistant response. NEVER call "
            "this in the same turn as `press_button`, `enter_hold_mode`, or while "
            "waiting for the next IVR prompt — those are mid-call routing actions, "
            "not call endings. Do not use after merely accepting a proposed booking, "
            "reservation, appointment, or callback; wait until the recipient confirms "
            "it is booked/all set or says it cannot be completed. If they ask whether to put a slot down, answer and wait; "
            "do not save or end yet. Do not use when a proposed booking is invalid or "
            "contradictory; clarify or ask for a valid option instead. Verbal "
            "goodbye is not enough; you must call this tool explicitly to actually "
            "hang up. Never emit end_call as the only content of a live recipient turn; "
            "the same response must also include a short recipient-facing spoken close "
            "such as thanks or goodbye. If the final turn also needs a durable action, "
            "emit that tool first and end_call second in the same response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for ending the call.",
                },
            },
            "required": ["reason"],
        },
    },
}

SAVE_CALL_RESULT_TOOL = {
    "type": "function",
    "function": {
        "name": "save_call_result",
        "description": (
            "Queue a durable mid-call write in the background after the recipient confirms a concrete result. "
            "Do not use this for an offered slot while the recipient is still asking whether to put it down; "
            "a recipient saying they can hold, can book, can schedule, or can put something down is an offer, "
            "not a captured result. Answer yes/no and wait for them to say it is held, booked, confirmed, all set, "
            "or impossible. "
            "Use this immediately for appointments, reservations, callback notes, or owner notifications that "
            "must survive the call. It returns quickly so you can keep talking; do not wait until end_call unless "
            "the call is truly ending now. Never emit save_call_result as the only content of a live recipient turn; "
            "also speak the next natural reply to the recipient."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": ["calendar_event", "owner_notification", "callback_note"],
                    "description": "What durable action to queue.",
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the calendar event or owner note.",
                },
                "appointment_date": {
                    "type": "string",
                    "description": "Date of the appointment/reservation, preferably ISO YYYY-MM-DD.",
                },
                "appointment_time": {
                    "type": "string",
                    "description": "Time of the appointment/reservation.",
                },
                "business_name": {
                    "type": "string",
                    "description": "Business or person associated with the durable result.",
                },
                "confirmation_number": {
                    "type": "string",
                    "description": "Confirmation number or reference captured from the recipient.",
                },
                "message": {
                    "type": "string",
                    "description": "Owner-facing notification or callback note text.",
                },
                "callback_reason": {
                    "type": "string",
                    "description": "Why the user needs to call back.",
                },
            },
            "required": ["action_type"],
        },
    },
}

ALL_CALL_TOOLS = [
    CONSULT_USER_TOOL,
    PRESENT_CALL_PLAN_TOOL,
    PRESS_BUTTON_TOOL,
    SAVE_CALL_RESULT_TOOL,
    END_CALL_TOOL,
]


# ---------------------------------------------------------------------------
# Handlers (Pipecat function-call style — new FunctionCallParams API)
# ---------------------------------------------------------------------------


def _consult_timeout_for_channel(channel: Any) -> float:
    channel_type = str(getattr(channel, "channel_type", "") or "").strip().lower()
    return _VOICE_CONSULT_TIMEOUT if channel_type == "voice" else _MESSAGING_CONSULT_TIMEOUT


def _call_record_id(call_record: Any | None) -> str:
    return str(getattr(call_record, "call_id", "") or "").strip()


def _call_record_user_id(call_record: Any | None) -> str:
    return str(getattr(call_record, "user_id", "") or "").strip()


def _function_result_properties(*, run_llm: bool) -> Any:
    from pipecat.frames.frames import FunctionCallResultProperties

    return FunctionCallResultProperties(run_llm=run_llm)


def _continue_llm_function_result_properties() -> Any:
    return _function_result_properties(run_llm=True)


def _no_llm_function_result_properties() -> Any:
    from pipecat.frames.frames import FunctionCallResultProperties

    return FunctionCallResultProperties(run_llm=False)


def _function_call_context_messages(context: Any) -> list[Any]:
    if context is None:
        return []
    getter = getattr(context, "get_messages", None)
    try:
        messages = getter() if callable(getter) else getattr(context, "messages", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return []
    if messages is None:
        return []
    try:
        return list(messages)
    except TypeError:
        return []


def _message_value(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _content_has_text(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(_content_has_text(part) for part in content)
    if isinstance(content, dict):
        for key in ("text", "content", "input_text", "output_text"):
            if _content_has_text(content.get(key)):
                return True
        return False
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return bool(text.strip())
    return False


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        value = tool_call.get("id") or tool_call.get("call_id") or tool_call.get("tool_call_id")
    else:
        value = (
            getattr(tool_call, "id", None)
            or getattr(tool_call, "call_id", None)
            or getattr(tool_call, "tool_call_id", None)
        )
    return str(value or "").strip()


def _assistant_message_has_tool_call(message: Any, tool_call_id: str) -> bool:
    tool_calls = _message_value(message, "tool_calls")
    if tool_calls is None:
        tool_calls = _message_value(message, "toolCalls")
    if not isinstance(tool_calls, list):
        return False
    return any(_tool_call_id(tool_call) == tool_call_id for tool_call in tool_calls)


def _current_end_call_response_has_spoken_text(params: Any) -> bool:
    """Return False only when the active end_call response is known text-empty."""
    arguments = getattr(params, "arguments", None)
    if isinstance(arguments, dict) and "_viola_response_had_spoken_text" in arguments:
        return bool(arguments.get("_viola_response_had_spoken_text"))

    tool_call_id = str(getattr(params, "tool_call_id", "") or "").strip()
    if not tool_call_id:
        return True
    messages = _function_call_context_messages(getattr(params, "context", None))
    if not messages:
        return True

    for message in reversed(messages):
        if str(_message_value(message, "role") or "").strip().lower() != "assistant":
            continue
        if not _assistant_message_has_tool_call(message, tool_call_id):
            continue
        return _content_has_text(_message_value(message, "content"))
    return True


def submit_consult_user_reply(call_id: str, answer: str, user_id: str | None = None) -> tuple[bool, str]:
    """Resolve a pending phone-call consultation from the issuer chat UI."""
    safe_call_id = str(call_id or "").strip()
    safe_answer = str(answer or "").strip()
    if not safe_call_id:
        return False, "missing_call_id"
    if not safe_answer:
        return False, "empty_answer"

    pending = _PENDING_CONSULTATIONS.get(safe_call_id)
    if pending is None or pending.future.done():
        return False, "not_found"

    safe_user_id = str(user_id or "").strip()
    pending_user_id = str(pending.user_id or "").strip()
    if not safe_user_id:
        return False, "auth_required"
    if not pending_user_id or safe_user_id != pending_user_id:
        return False, "forbidden"

    pending.future.set_result(safe_answer)
    return True, "accepted"


def _issuer_channel_description(call_record: Any | None) -> str:
    if call_record is None:
        return "{}"
    info = getattr(call_record, "issuer_channel_info", None)
    if isinstance(info, dict) and info:
        return str(info)
    channel = getattr(call_record, "issuer_channel", None)
    if channel is None:
        return "{}"
    return str(
        {
            "channel_type": getattr(channel, "channel_type", ""),
            "channel_class": type(channel).__name__,
        }
    )


# Issuer channel types that get a genuine live wait for consult_user, via the
# web-consult future path (broadcast the question, block on the reply POST)
# rather than the instant-fallback `_ask_call_issuer` path. "web"/"desktop"
# are the browser and desktop-app issuers; "ios" is the iOS app's OWN direct
# call-composer flow (`PhoneCallComposerView` -> `POST /api/phone/call` with
# no live in-process channel at all — unlike a conversational "call the pizza
# place" turn, which already inherits channel_type "web" through
# `chat.channel.resolve_rest_channel` regardless of which client issued it).
# Before this set included "ios", every consult_user asked during a call the
# iOS app placed itself got the canned fallback answer in ~150ms with the
# mid-call question never shown to the user — the same production symptom
# `_issuer_channel_needs_web_consult` was already built to fix for web/desktop
# (2026-06-29), just never extended to iOS's composer path (#4791).
_WEB_CONSULT_CHANNEL_TYPES = frozenset({"web", "desktop", "ios"})


def _issuer_channel_needs_web_consult(call_record: Any | None) -> bool:
    if call_record is None:
        return False
    issuer_channel = getattr(call_record, "issuer_channel", None)
    if issuer_channel is not None:
        channel_type = str(getattr(issuer_channel, "channel_type", "") or "").strip().lower()
        return channel_type in _WEB_CONSULT_CHANNEL_TYPES and getattr(issuer_channel, "active_delivery", None) is False

    # Cloud-placed call: the pipeline runs server-side, so the live issuer
    # channel object is never carried across the HTTP boundary — only its
    # serialized descriptor (issuer_channel_info) survives (cloud_routes
    # forwards the dict, not the object). Without this branch a cloud call's
    # consult_user falls through to _ask_call_issuer, finds issuer_channel
    # is None, and returns the canned "could not reach the user" fallback in
    # ~150ms with no real wait — the symptom on the first production call
    # (2026-06-29). When the descriptor says the issuer is a recognized app
    # client, route consult through the web-consult future path so the
    # question is broadcast to that client and genuinely waits for the
    # in-app reply delivered via POST /v1/phone/call/{call_id}/reply.
    info = getattr(call_record, "issuer_channel_info", None)
    if isinstance(info, dict) and info:
        channel_type = str(info.get("channel_type", "") or "").strip().lower()
        return channel_type in _WEB_CONSULT_CHANNEL_TYPES
    return False


def _event_hub_has_consult_target(hub: Any, user_id: str) -> bool:
    if user_id:
        user_clients = getattr(hub, "_user_clients", None)
        if isinstance(user_clients, dict):
            return bool(user_clients.get(user_id))
    clients = getattr(hub, "_clients", None)
    if clients is not None:
        return bool(clients)
    return True


async def _broadcast_call_consultation(
    *,
    question: str,
    urgency: str,
    call_record: Any | None,
    answer: str | None = None,
    pending: bool = False,
) -> bool:
    try:
        from ui.websocket.event_hub import get_event_hub

        hub = get_event_hub()
        if not hub:
            return False

        user_id = _call_record_user_id(call_record)
        if not user_id:
            try:
                from core.user_context import get_current_user_id

                user_id = get_current_user_id()
            except (ImportError, LookupError):
                user_id = ""

        payload: dict[str, Any] = {
            "call_id": _call_record_id(call_record),
            "question": question,
            "urgency": urgency,
        }
        if answer is not None:
            payload["answer"] = answer
        if pending:
            payload["pending"] = True

        has_target = _event_hub_has_consult_target(hub, user_id)
        await hub.broadcast(
            "call_consultation",
            payload,
            user_id=user_id or None,
            force=True,
        )
        return has_target
    except Exception as exc:
        logger.debug("consult_user broadcast failed: %s", exc)
        return False


async def _ask_call_issuer(question: str, call_record: Any | None) -> str:
    issuer_channel = getattr(call_record, "issuer_channel", None) if call_record is not None else None
    if issuer_channel is None:
        logger.info(
            "consult_user issuer channel unavailable; using fallback response (issuer=%s)",
            _issuer_channel_description(call_record)[:160],
        )
        return CONSULT_USER_FALLBACK_ANSWER

    channel_type = str(getattr(issuer_channel, "channel_type", "") or "unknown")
    try:
        answer = await issuer_channel.ask(question, timeout=_consult_timeout_for_channel(issuer_channel))
    except Exception as exc:
        logger.warning("consult_user issuer channel failed (%s): %s", channel_type, exc)
        return CONSULT_USER_FALLBACK_ANSWER

    safe_answer = str(answer or "").strip()
    if not safe_answer:
        logger.info(
            "consult_user issuer channel returned no answer via %s; using fallback response",
            channel_type,
        )
        return CONSULT_USER_FALLBACK_ANSWER

    logger.info(
        "consult_user got issuer response via %s (%d chars)",
        channel_type,
        len(safe_answer),
    )
    return safe_answer


def _web_consult_timeout(call_record: Any | None) -> float:
    """Resolve the consult wait timeout for a web/desktop issuer on a phone call.

    If a live channel object is present, honor its type. When there is no live
    object — a cloud-placed call, where the pipeline runs server-side and only
    issuer_channel_info crossed the HTTP boundary — use the short VOICE timeout,
    NOT the 300s messaging one. The consult happens DURING a live phone call:
    the recipient (e.g. the pizza shop) is on the line and hears dead silence
    for the entire wait. Founder directive (2026-06-29): "if the user doesn't
    reply in a short time we don't want to leave the pizza shop on hold too
    long" — a short wait, then proceed with the sensible default (handled by
    CONSULT_USER_FALLBACK_ANSWER). A 300s hold on a live call is exactly the
    failure to avoid; the issuer watching on desktop has ample time to tap a
    reply within the voice window, and if not, Viola assumes the default and
    keeps the call moving.
    """
    issuer_channel = getattr(call_record, "issuer_channel", None) if call_record is not None else None
    if issuer_channel is not None:
        return _consult_timeout_for_channel(issuer_channel)
    return _VOICE_CONSULT_TIMEOUT


async def _ask_call_issuer_via_web_consult(question: str, call_record: Any | None, urgency: str) -> str:
    call_id = _call_record_id(call_record)
    if not call_id:
        return CONSULT_USER_FALLBACK_ANSWER

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    existing = _PENDING_CONSULTATIONS.get(call_id)
    if existing is not None and not existing.future.done():
        existing.future.set_result(CONSULT_USER_FALLBACK_ANSWER)
    _PENDING_CONSULTATIONS[call_id] = _PendingConsultation(
        future=future,
        question=question,
        user_id=_call_record_user_id(call_record),
    )

    try:
        delivered = await _broadcast_call_consultation(
            question=question,
            urgency=urgency,
            call_record=call_record,
            pending=True,
        )
        if not delivered:
            logger.info("consult_user web issuer has no active chat target; using fallback response")
            return CONSULT_USER_FALLBACK_ANSWER

        answer = await asyncio.wait_for(future, timeout=_web_consult_timeout(call_record))
    except TimeoutError:
        logger.info("consult_user web issuer timed out; using fallback response")
        return CONSULT_USER_FALLBACK_ANSWER
    finally:
        if _PENDING_CONSULTATIONS.get(call_id) is not None and _PENDING_CONSULTATIONS[call_id].future is future:
            _PENDING_CONSULTATIONS.pop(call_id, None)

    safe_answer = str(answer or "").strip()
    if not safe_answer:
        return CONSULT_USER_FALLBACK_ANSWER
    logger.info("consult_user got issuer response via web chat (%d chars)", len(safe_answer))
    return safe_answer


def make_consult_user_handler(call_record: Any):
    async def _handler(params) -> None:
        await consult_user_handler(params, call_record=call_record)

    return _handler


def make_save_call_result_handler(call_record: Any):
    async def _handler(params) -> None:
        await save_call_result_handler(params, call_record=call_record)

    return _handler


async def consult_user_handler(params, call_record: Any | None = None) -> None:
    """Pipecat function call handler — pauses call, asks user, injects answer.

    The handler is called by Pipecat when the LLM emits a function call for
    'consult_user'. Pipecat automatically handles FunctionCallInProgressFrame
    (mutes STT, suppresses idle timeouts). We route the question to the user,
    then call result_callback to inject the answer.

    Args:
        params: FunctionCallParams with function_name, arguments, llm, context,
                result_callback.
    """
    args = dict(getattr(params, "arguments", {}) or {})
    question = str(args.get("question") or "").strip()
    urgency = str(args.get("urgency") or "medium")

    logger.info("consult_user called: question=%s, urgency=%s", str(question)[:80], urgency)

    # Route question to the call issuer, not the call recipient. Runtime code
    # must not inject hold-cue speech here; if a pause should be acknowledged,
    # that is Viola's model output.
    #
    # The outer wait_for is the handler's OWN emergency ceiling, strictly
    # inside the registered pipeline timeout (see _consult_handler_wait_budget_
    # secs): no matter what the issuer route does, the handler resolves to an
    # answer or the graceful proceed-with-default fallback BEFORE the pipeline
    # could time the function call out and discard the outcome (the 67105503
    # dead-air shape). It does not change the normal consult wait semantics —
    # the inner voice/messaging windows are unchanged and much shorter.
    try:
        if _issuer_channel_needs_web_consult(call_record):
            safe_answer = str(
                await asyncio.wait_for(
                    _ask_call_issuer_via_web_consult(question, call_record, urgency),
                    timeout=_consult_handler_wait_budget_secs(),
                )
                or CONSULT_USER_FALLBACK_ANSWER
            )
        else:
            safe_answer = str(
                await asyncio.wait_for(
                    _ask_call_issuer(question, call_record),
                    timeout=_consult_handler_wait_budget_secs(),
                )
                or CONSULT_USER_FALLBACK_ANSWER
            )
            await _broadcast_call_consultation(
                question=question,
                urgency=urgency,
                call_record=call_record,
                answer=safe_answer,
            )
    except TimeoutError:
        logger.warning("consult_user handler hit its emergency wait ceiling; using fallback response")
        safe_answer = CONSULT_USER_FALLBACK_ANSWER

    # 3. Inject answer via result_callback (auto re-runs LLM)
    logger.info("consult_user answer: %s", safe_answer[:80])
    await params.result_callback(
        {"user_response": safe_answer},
        properties=_continue_llm_function_result_properties(),
    )


async def save_call_result_handler(params, call_record: Any | None = None) -> None:
    """Queue a durable in-call action and immediately return to the live call."""
    args = dict(getattr(params, "arguments", {}) or {})
    action_type = str(args.get("action_type") or "").strip()
    call_id = _call_record_id(call_record)
    user_id = _call_record_user_id(call_record)
    if call_record is None or not call_id:
        await params.result_callback(
            {
                "durable_status": "error",
                "error": "No active call record is available for this durable action.",
            },
            properties=_continue_llm_function_result_properties(),
        )
        return
    if not user_id:
        await params.result_callback(
            {
                "durable_status": "error",
                "error": "Authenticated user_id is required for durable call actions.",
            },
            properties=_continue_llm_function_result_properties(),
        )
        return

    try:
        from telephony.post_call_actions import schedule_mid_call_durable_action

        schedule_mid_call_durable_action(args, call_record, user_id)
    except ValueError as exc:
        await params.result_callback(
            {"durable_status": "error", "error": str(exc)},
            properties=_continue_llm_function_result_properties(),
        )
        return
    except RuntimeError as exc:
        logger.warning("save_call_result failed to queue for call %s: %s", call_id, exc)
        await params.result_callback(
            {"durable_status": "error", "error": "durable action could not be queued"},
            properties=_continue_llm_function_result_properties(),
        )
        return

    logger.info("save_call_result queued: call=%s action_type=%s", call_id, action_type)
    await params.result_callback(
        {
            "durable_status": "queued",
            "action_type": action_type,
            "background": True,
            "guidance": "The durable write is queued in the background; keep the live conversation moving.",
        },
        properties=_continue_llm_function_result_properties(),
    )


async def present_call_plan_handler(params) -> None:
    """Present the call plan to the user and get approval before dialing.

    Args:
        params: FunctionCallParams with arguments containing plan details.
    """
    args = params.arguments
    phone_number = args.get("phone_number", "")
    business_name = args.get("business_name", "")
    objective = args.get("objective", "")
    talking_points = args.get("talking_points", [])
    fallback_plan = args.get("fallback_plan", "")

    # Format the plan text
    label = business_name or phone_number
    plan_text = "I'm going to call %s.\n" % label
    plan_text += "Goal: %s\n" % objective
    if talking_points:
        plan_text += "Plan:\n" + "\n".join("  - %s" % pt for pt in talking_points)
    if fallback_plan:
        plan_text += "\nIf that doesn't work: %s" % fallback_plan
    plan_text += "\n\nShall I go ahead and dial?"

    logger.info("present_call_plan: %s", plan_text[:120])

    # Ask user for approval via multi-channel ask_user
    approved = False
    user_feedback = ""
    try:
        from intent.tools.ask_user import ask_user_handler

        result = await ask_user_handler(plan_text, context="pre-call briefing")
        if result.ok:
            user_feedback = result.data["answer"]
            answer_lower = user_feedback.lower()
            approved = any(
                word in answer_lower
                for word in [
                    "yes",
                    "yeah",
                    "go",
                    "sure",
                    "do it",
                    "dial",
                    "ok",
                    "yep",
                    "go ahead",
                    "approve",
                ]
            )
    except Exception as exc:
        logger.warning("present_call_plan ask_user failed: %s", exc)

    # Broadcast to WebSocket UI
    try:
        from ui.websocket.event_hub import get_event_hub

        hub = get_event_hub()
        if hub:
            try:
                from core.user_context import get_current_user_id

                _uid = get_current_user_id()
            except (ImportError, LookupError):
                _uid = None
            await hub.broadcast(
                "call_briefing",
                {
                    "phone_number": phone_number,
                    "business_name": business_name,
                    "objective": objective,
                    "talking_points": talking_points,
                    "approved": approved,
                },
                user_id=_uid,
            )
    except Exception as exc:
        logger.debug("present_call_plan broadcast failed: %s", exc)

    logger.info(
        "present_call_plan result: approved=%s, feedback=%s",
        approved,
        user_feedback[:80],
    )
    await params.result_callback({"approved": approved, "user_feedback": user_feedback})


def _normalize_us_e164(phone_number: str) -> str:
    from telephony.number_validation import normalize_us_e164

    return normalize_us_e164(
        phone_number,
        field_name="Set a US phone number in Settings before conferencing the user into calls",
    )


_CONFERENCE_USER_ALIASES = frozenset(
    {
        "",
        "i",
        "me",
        "myself",
        "user",
        "the user",
        "caller",
        "the caller",
        "founder",
        "the founder",
        "jay",
        "j",
        "j n",
        "jn",
        "jihad",
        "jihad shkoukani",
    }
)


def _conference_target_alias(target: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", target.lower())).strip()


def _looks_like_phone_number(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return len(digits) in {10, 11} and (len(digits) == 10 or digits.startswith("1"))


def _get_global_setting(settings_manager, key: str) -> object:
    settings = getattr(settings_manager, "settings", None)
    if isinstance(settings, dict):
        return settings.get(key, "")
    return ""


def _can_use_global_conference_settings(user_id: str) -> bool:
    from core.user_context import is_desktop_local_principal

    return is_desktop_local_principal(user_id)


def _resolve_conference_user_phone(user_id: str, target: str = "") -> str:
    if not user_id:
        raise ValueError("Authenticated user_id is required to resolve the conference phone number.")

    target_text = str(target or "").strip()
    if target_text and _looks_like_phone_number(target_text):
        return _normalize_us_e164(target_text)

    target_alias = _conference_target_alias(target_text)
    if target_alias not in _CONFERENCE_USER_ALIASES:
        raise ValueError("conference_in_user can only dial the configured user/founder phone or a literal US number.")

    from ui.settings_manager import get_settings_manager

    settings_manager = get_settings_manager()
    for key in ("user_phone_number", "founder_phone_number"):
        value = settings_manager.get(key, "", user_id=user_id)
        if isinstance(value, str) and value.strip():
            return _normalize_us_e164(value)

    if _can_use_global_conference_settings(user_id):
        # Local/device calls store phone preferences in settings.json, not the auth DB.
        for key in ("user_phone_number", "founder_phone_number"):
            value = _get_global_setting(settings_manager, key)
            if isinstance(value, str) and value.strip():
                return _normalize_us_e164(value)

        # Env-var fallback is the founder-of-this-desktop default — only legitimate
        # for local-desktop identities, never for named auth users. Same fix class as
        # services/user_profile.py (2026-05-20 founder-defaults leak): a named auth
        # user with no phone in their DB row must NOT fall through to the founder's
        # phone number, or in production any phone-lookup miss would dial the wrong
        # person.
        from config import env

        for env_name in ("VIOLA_FOUNDER_PHONE", "VIOLA_USER_PHONE"):
            value = env.get(env_name, "")
            if isinstance(value, str) and value.strip():
                return _normalize_us_e164(value)
    raise ValueError("Set user_phone_number or founder_phone_number in Settings before conferencing the user.")


async def _resolve_conference_user_phone_async(user_id: str, target: str = "") -> str:
    if not user_id:
        raise ValueError("Authenticated user_id is required to resolve the conference phone number.")

    target_text = str(target or "").strip()
    if target_text and _looks_like_phone_number(target_text):
        return _normalize_us_e164(target_text)

    target_alias = _conference_target_alias(target_text)
    if target_alias not in _CONFERENCE_USER_ALIASES:
        raise ValueError("conference_in_user can only dial the configured user/founder phone or a literal US number.")

    cloud_settings = await load_cloud_user_settings_blob(user_id)
    if cloud_settings is not None:
        value = first_setting_value(cloud_settings, ("user_phone_number", "founder_phone_number"))
        if isinstance(value, str) and value.strip():
            return _normalize_us_e164(value)
        if not _can_use_global_conference_settings(user_id):
            raise ValueError("Set user_phone_number or founder_phone_number in Settings before conferencing the user.")

    return _resolve_conference_user_phone(user_id, target)


def _conference_name_for_call(call_id: str) -> str:
    safe_call_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(call_id or "unknown")).strip("-")
    if not safe_call_id:
        safe_call_id = "unknown"
    return "viola-%s" % safe_call_id[:56]


def make_conference_user_handler(
    *,
    telnyx_client,
    call_record,
    connection_id: str,
    from_number_getter: Callable[[], str],
    max_duration: int,
):
    """Build the owner-takeover handler that dials the user into the live call."""

    async def conference_user_handler(params) -> None:
        reason = params.arguments.get("reason", "")
        target = params.arguments.get("target", "")
        caller_name = (getattr(call_record, "caller_name", "") or "the user").strip()
        call_id = getattr(call_record, "call_id", "unknown")
        logger.info("conference_in_user: call=%s reason=%s", call_id, reason[:120])

        try:
            founder_phone = await _resolve_conference_user_phone_async(
                getattr(call_record, "user_id", ""), str(target or "")
            )
            active_call_control_id = getattr(call_record, "telnyx_call_control_id", "") or ""
            if not active_call_control_id:
                raise ValueError("The live Telnyx call is not ready for conferencing yet.")

            from_number = from_number_getter()
            if not from_number:
                raise ValueError("No Telnyx caller ID is available for the conference leg.")
        except Exception as exc:
            logger.warning("conference_in_user unavailable for call %s: %s", call_id, exc)
            await params.result_callback({"conference_status": "error", "error": str(exc)})
            return

        conference_name = _conference_name_for_call(call_id)
        try:
            await telnyx_client.conferences.create(
                call_control_id=active_call_control_id,
                name=conference_name,
                beep_enabled="never",
                start_conference_on_create=True,
                max_participants=3,
                command_id="conference-create-%s" % call_id,
            )
            dial_response = await telnyx_client.calls.dial(
                connection_id=connection_id,
                from_=from_number,
                to=founder_phone,
                conference_config={
                    "conference_name": conference_name,
                    "start_conference_on_enter": True,
                    "end_conference_on_exit": False,
                    "supervisor_role": "none",
                },
                timeout_secs=30,
                time_limit_secs=max_duration,
                command_id="conference-dial-%s" % call_id,
            )
            # Record the owner leg's control id on the call so every primary-call
            # teardown path hangs it up in lockstep. Without this the leg is
            # bounded only by time_limit_secs and keeps billing after the main
            # call ends (#2800). end_conference_on_exit=False keeps the conference
            # up if the owner drops, so the primary-call lifecycle is the only
            # reliable teardown trigger. Mirrors the primary dial's
            # telnyx_dial_call_control_id capture in call_manager.
            leg_control_id = telnyx_dial_call_control_id(dial_response)
            if leg_control_id:
                call_record._conference_leg_call_control_id = leg_control_id
                call_record._conference_leg_hangup_dispatched = False
        except Exception as exc:
            logger.warning(
                "conference_in_user Telnyx conference setup failed for call %s: %s",
                call_id,
                exc,
            )
            await params.result_callback({"conference_status": "error", "error": str(exc)})
            return

        await params.result_callback(
            {
                "conference_status": "conference_dialing",
                "conference_name": conference_name,
                "cost_note": "This adds a second outbound Telnyx leg while the user is connected.",
                "speaker_attribution_note": (
                    "After the user joins, caller and recipient speech can be mixed. "
                    "Explicitly attribute important confirmations to the user or recipient, "
                    "and ask once if you are unsure who spoke."
                ),
            }
        )

    return conference_user_handler


class _ConferenceInvocationLLM:
    async def push_frame(self, frame: Any) -> None:
        del frame


class _ConferenceInvocationParams:
    def __init__(self, *, target: str, reason: str, llm: Any | None) -> None:
        self.arguments = {"target": target, "reason": reason}
        self.llm = llm if callable(getattr(llm, "push_frame", None)) else _ConferenceInvocationLLM()
        self.result: dict[str, Any] = {}

    async def result_callback(self, result: dict[str, Any]) -> None:
        self.result = dict(result or {})


async def conference_user_for_call(
    *,
    telnyx_client: Any,
    call_record: Any,
    connection_id: str,
    from_number_getter: Callable[[], str],
    max_duration: int,
    target: str = "",
    reason: str = "",
    llm: Any | None = None,
) -> dict[str, Any]:
    """Invoke the owner-takeover conference handler used by the UI button path."""
    params = _ConferenceInvocationParams(target=target, reason=reason, llm=llm)
    handler = make_conference_user_handler(
        telnyx_client=telnyx_client,
        call_record=call_record,
        connection_id=connection_id,
        from_number_getter=from_number_getter,
        max_duration=max_duration,
    )
    await handler(params)
    return params.result


# Kept for back-compat with callers that explicitly pass min_duration_seconds.
# PHONE-15: the temporal floor was replaced with a semantic turn-completion
# guard (see make_end_call_handler). The original 15s floor false-positively
# blocked legitimate quick-end flows (line confirmations, voicemail-leave-
# and-go) and, when refused, left the agent idle for the full Pipecat idle
# timeout — burning 5+ minutes of Telnyx airtime per refusal.
END_CALL_MIN_DURATION_SECONDS = 0


@dataclass
class _LatchedEndCall:
    """A text-empty end_call the PHONE-15 guard refused, remembered so the
    next spoken assistant turn (the closing line the guard asked for) can
    complete the hangup even if the model forgets to re-emit end_call.

    Carries exactly what dispatch needs so the fire path mirrors the accepted
    end_call: push an EndCallHangupFrame (media-drained ordering) when the LLM
    frame path is available, else fall back to a direct Telnyx hangup.
    """

    reason: str
    telnyx_client: Any
    call_control_id: str
    hangup_after_output_drain: bool


async def fire_latched_end_call_if_pending(call_record: Any, *, push_frame: Any = None) -> bool:
    """Complete a previously-refused text-empty end_call, once a spoken close exists.

    Called when a non-empty assistant turn completes. If the immediately-prior
    turn emitted end_call in a text-empty tool-only turn (which the PHONE-15
    guard refuses), that end-intent was latched on the call record. This turn's
    spoken text IS the closing line the guard required, so the hangup can now
    complete — the goodbye is heard, THEN the line drops (the EndCallHangupFrame
    trails the just-spoken TTS media and the drain processor waits for it to
    play before dispatching the Telnyx hangup).

    Single-shot: the latch is consumed on the first spoken turn after it was set,
    so it never fires two turns later. Returns True iff a latched end_call fired.
    """
    latched = getattr(call_record, "_pending_end_call", None)
    if not isinstance(latched, _LatchedEndCall):
        return False

    # Consume the latch regardless of outcome — it applies only to the
    # immediately-following spoken turn.
    with suppress(AttributeError, TypeError):
        call_record._pending_end_call = None

    # The model may have re-emitted end_call normally on this same turn; the
    # accepted handler already committed and dispatched. Don't double-fire.
    if bool(getattr(call_record, "_end_call_committed", False)):
        return False

    with suppress(AttributeError, TypeError):
        call_record._end_call_committed = True

    reason = latched.reason
    call_id = str(getattr(call_record, "call_id", "") or "")
    logger.info(
        "end_call: auto-completing latched text-empty end_call for %s after spoken close",
        call_id,
    )

    if latched.hangup_after_output_drain and callable(push_frame):
        try:
            await push_frame(EndCallHangupFrame(call_id=call_id, reason=reason))
            return True
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "end_call: latched media-drained hangup push failed for %s: %s; hanging up immediately",
                call_id,
                exc,
            )

    await dispatch_telnyx_end_call_hangup(
        telnyx_client=latched.telnyx_client,
        call_control_id=latched.call_control_id,
        call_record=call_record,
        reason_text=reason,
    )
    return True


def cancel_latched_end_call(call_record: Any, *, reason: str = "") -> bool:
    """Drop a latched end_call because the conversation is clearly continuing.

    The latch fires the hangup on the next spoken turn; that is only safe while
    the model is genuinely closing. If the recipient speaks again, or the model
    takes another interactive action (a work/hold/consult tool call) instead of
    closing, the end-intent is stale — cancel it so the call is never torn down
    mid-conversation. Returns True iff a latch was cleared.
    """
    latched = getattr(call_record, "_pending_end_call", None)
    if not isinstance(latched, _LatchedEndCall):
        return False
    with suppress(AttributeError, TypeError):
        call_record._pending_end_call = None
    logger.info(
        "end_call: latched end_call cancelled (%s) for %s",
        reason or "conversation_continued",
        str(getattr(call_record, "call_id", "") or ""),
    )
    return True


def make_end_call_handler(
    *,
    telnyx_client,
    call_control_id: str,
    call_record,
    min_duration_seconds: float = END_CALL_MIN_DURATION_SECONDS,
    hangup_after_output_drain: bool = False,
):
    """Build a Pipecat function-call handler that hangs up the live call.

    Safety guard (PHONE-15): refuses to hang up before the assistant has
    delivered at least one full turn (``call_record.first_assistant_turn_complete``).
    This guards against the original adversarial-review concern — a model
    hallucinating ``end_call`` before any actual conversation has happened —
    without false-positively blocking legitimate quick-end flows.

    The legacy ``min_duration_seconds`` parameter remains for explicit callers
    but defaults to 0 (semantic guard supersedes temporal). Callers passing a
    positive value get the legacy temporal-floor behavior layered on top.

    Args:
        telnyx_client: Active Telnyx client used for this call.
        call_control_id: Telnyx call_control_id of the live call.
        call_record: CallRecord whose status will be marked COMPLETED.
        min_duration_seconds: Optional explicit temporal floor (default 0).
        hangup_after_output_drain: If True, enqueue an EndCallHangupFrame so the
            live phone pipeline hangs up after final TTS media is written and
            Telnyx confirms that media has played.
    """

    async def end_call_handler(params) -> None:
        from datetime import UTC, datetime as _dt

        reason = params.arguments.get("reason", "task complete")
        reason_for_log = str(reason or "")

        # PHONE-15 semantic guard: refuse end_call before the assistant has
        # delivered its first turn. Voicemail-terminal flows (detected before
        # any turn) intentionally bypass this — they need to hang up after
        # leaving the message regardless of "turn" semantics.
        voicemail_detected = bool(getattr(call_record, "voicemail_detected", False))
        human_takeover_detected = bool(getattr(call_record, "human_takeover_detected", False))
        voicemail_terminal = voicemail_detected and not human_takeover_detected
        first_turn_complete = bool(getattr(call_record, "first_assistant_turn_complete", False))
        if not first_turn_complete and not voicemail_terminal:
            logger.warning(
                "end_call: refusing — call %s has no completed assistant turn yet; reason=%s",
                call_record.call_id,
                reason_for_log[:80],
            )
            try:
                await params.result_callback(
                    {
                        "call_status": "turn_not_started",
                        "guidance": "Deliver your first response before ending the call.",
                    }
                )
            except Exception as exc:
                logger.debug("end_call turn_not_started callback failed: %s", exc)
            return

        # Legacy temporal floor (only applies if explicitly opted into).
        if min_duration_seconds > 0:
            started_at = getattr(call_record, "started_at", None)
            if started_at is not None:
                try:
                    live_seconds = (_dt.now(tz=UTC) - started_at).total_seconds()
                except Exception:
                    live_seconds = float("inf")
                if live_seconds < min_duration_seconds and not voicemail_terminal:
                    logger.warning(
                        "end_call: refusing — call %s only %.1fs old (< %.0fs floor); reason=%s",
                        call_record.call_id,
                        live_seconds,
                        min_duration_seconds,
                        reason_for_log[:80],
                    )
                    try:
                        await params.result_callback(
                            {
                                "call_status": "too_early",
                                "live_seconds": round(live_seconds, 1),
                                "min_seconds": min_duration_seconds,
                                "guidance": "It's too early in the call to end. Continue the conversation, deliver your closing recap, then call end_call again.",
                            }
                        )
                    except Exception as exc:
                        logger.debug("end_call too_early callback failed: %s", exc)
                    return

        if not voicemail_terminal and not _current_end_call_response_has_spoken_text(params):
            logger.warning(
                "end_call: refusing silent live hangup for call %s; reason=%s",
                call_record.call_id,
                reason_for_log[:80],
            )
            # PHONE-15 path-drop recovery: the model DID express end-intent, just in
            # a text-empty tool-only turn. We still refuse the silent hangup (the
            # goodbye must be heard first), but we LATCH the intent so that once the
            # model speaks the closing line on its very next turn, the runtime can
            # complete the hangup even if the model forgets to re-emit end_call. The
            # latch is single-shot and is cancelled if the conversation continues
            # (recipient speaks again, or the model takes another interactive action).
            with suppress(AttributeError, TypeError):
                call_record._pending_end_call = _LatchedEndCall(
                    reason=reason_for_log,
                    telnyx_client=telnyx_client,
                    call_control_id=call_control_id,
                    hangup_after_output_drain=hangup_after_output_drain,
                )
            try:
                await params.result_callback(
                    {
                        "call_status": "closing_speech_missing",
                        "guidance": (
                            "Say a short recipient-facing closing line in this live call, then call end_call again."
                        ),
                    },
                    properties=_continue_llm_function_result_properties(),
                )
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                logger.debug("end_call closing_speech_missing callback failed: %s", exc)
            return

        logger.info("end_call: call=%s reason=%s", call_record.call_id, reason_for_log[:120])

        # Mark the call as closing the instant end_call commits. The downstream
        # end-call lifecycle guard reads this transport flag so the spoken-close
        # drain window cannot start a fresh recipient turn while carrier hangup
        # is pending. This is not the owner/recipient semantic fix; that belongs
        # in the phone-call context the model receives.
        with suppress(AttributeError, TypeError):
            call_record._end_call_committed = True
        # The model re-emitted end_call normally (this accepted turn has spoken
        # text), so any latched path-drop intent is now moot — drop it.
        with suppress(AttributeError, TypeError):
            call_record._pending_end_call = None

        try:
            await params.result_callback(
                {"call_status": "ending", "reason": reason_for_log},
                properties=_no_llm_function_result_properties(),
            )
        except Exception as exc:
            logger.debug("end_call result_callback failed: %s", exc)

        if hangup_after_output_drain and callable(getattr(params.llm, "push_frame", None)):
            try:
                await params.llm.push_frame(
                    EndCallHangupFrame(
                        call_id=str(getattr(call_record, "call_id", "")),
                        reason=reason_for_log,
                    )
                )
                logger.info("end_call: queued media-drained hangup for %s", call_record.call_id)
                return
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning(
                    "end_call: failed to queue media-drained hangup for %s: %s; hanging up immediately",
                    call_record.call_id,
                    exc,
                )

        await dispatch_telnyx_end_call_hangup(
            telnyx_client=telnyx_client,
            call_control_id=call_control_id,
            call_record=call_record,
            reason_text=reason_for_log,
        )

    return end_call_handler


async def press_button_handler(params) -> None:
    """Press a DTMF digit on the phone keypad.

    Pushes an OutputDTMFFrame into the Pipecat pipeline. The
    TelnyxOutputTransport base class (BaseOutputTransport._write_dtmf_audio)
    converts it to PCM audio and routes it through write_audio_frame, which
    sends it to Telnyx over the WebSocket media stream.

    Args:
        params: FunctionCallParams with arguments containing the digit to press.
    """
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import OutputDTMFFrame

    digit = params.arguments.get("digit", "")
    logger.info("Pressing DTMF: %s", digit)

    try:
        button = KeypadEntry(digit)
        await params.llm.push_frame(OutputDTMFFrame(button=button))
        logger.info("DTMF frame pushed for digit: %s", digit)
    except ValueError as exc:
        logger.warning("Invalid DTMF digit %r: %s", digit, exc)
        await params.result_callback({"error": "invalid digit: %s" % digit})
        return

    await params.result_callback({"pressed": digit})
