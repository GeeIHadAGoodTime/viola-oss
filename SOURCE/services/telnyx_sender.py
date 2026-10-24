"""Shared async Telnyx SMS sender.

A single low-level send path used by:

- ``auth/sms_routes.py`` — outbound phone-verification OTP codes.
- ``backend/telnyx_sms_webhook.py`` — outbound replies (HELP/STOP confirmations
  and two-way conversational replies).

``admin/alerts.py`` keeps its own *synchronous* operator-paging sender because it
runs from a non-async alert-dispatch thread; that call site is intentionally not
routed through here.

All sends go through Telnyx ``POST /v2/messages`` from the approved
``+18337073533`` toll-free number. Toll-Free Verification for ``+18337073533``
was approved 2026-05-05, so no A2P 10DLC campaign is required for this path.

Who may be texted (1.0.5): every send names its ``purpose`` and the purpose
decides the recipient rule. The caller proves the rule; this module refuses a
send with no purpose, an unknown purpose, or a skipped opt-out check.

- ``verification_code``: the number a signed-in user typed to verify it
  (rate limited by ``auth/sms_routes.py``).
- ``approval_link``: only the signed-in user's own verified phone with current
  SMS consent (resolved server-side, never taken from a request).
- ``compliance_reply``: HELP/STOP/START replies to the number that texted us.
- ``conversation_reply``: two-way agent replies, still limited to the founder
  launch number (:data:`LAUNCH_SMS_ALLOWED_RECIPIENTS`).

``VIOLA_SMS_RECIPIENT_ALLOWLIST`` (comma-separated E.164) is an optional
operator switch: when set, every send except a compliance reply must go to a
listed number, which restores the old founder-only launch gate without a deploy.

Cost constants for usage billing live here too
(:data:`TELNYX_TOLL_FREE_SMS_BASE_MICRODOLLARS` and
:data:`TELNYX_TOLL_FREE_SMS_CARRIER_FEE_MICRODOLLARS`); see
:func:`estimate_sms_cost_microdollars`.
"""

from __future__ import annotations

import inspect
import math
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from config.settings import get_settings
from core.logging_config import get_logger

logger = get_logger(__name__)

TELNYX_MESSAGES_API_URL = "https://api.telnyx.com/v2/messages"
APPROVED_SMS_FROM_NUMBER = "+18337073533"
LAUNCH_SMS_ALLOWED_TO_NUMBER = "+14143697284"
LAUNCH_SMS_ALLOWED_RECIPIENTS = frozenset({LAUNCH_SMS_ALLOWED_TO_NUMBER})

# Telnyx single-segment SMS is 160 GSM-7 chars; concatenated messages are billed
# per segment. We hard-cap conversational replies so a runaway agent reply cannot
# fan out into dozens of billed segments.
SMS_MAX_CHARS = 1200

SMS_PURPOSE_VERIFICATION_CODE = "verification_code"
SMS_PURPOSE_APPROVAL_LINK = "approval_link"
SMS_PURPOSE_COMPLIANCE_REPLY = "compliance_reply"
SMS_PURPOSE_CONVERSATION_REPLY = "conversation_reply"
SMS_PURPOSES = frozenset(
    {
        SMS_PURPOSE_VERIFICATION_CODE,
        SMS_PURPOSE_APPROVAL_LINK,
        SMS_PURPOSE_COMPLIANCE_REPLY,
        SMS_PURPOSE_CONVERSATION_REPLY,
    }
)
SMS_RECIPIENT_ALLOWLIST_ENV = "VIOLA_SMS_RECIPIENT_ALLOWLIST"

# Per-segment cost of one outbound US toll-free SMS, in microdollars (1e-6 USD).
# Telnyx list price checked 2026-10-24 (telnyx.com/pricing/messaging): $0.0055
# base per message part, plus the carrier pass-through fee per part (AT&T
# $0.0035, T-Mobile $0.0045, Verizon $0.0045). The carrier fee uses the highest
# carrier so the estimate never undercharges. Update these two numbers when
# Telnyx changes its price, or override at runtime with the env vars below.
TELNYX_TOLL_FREE_SMS_BASE_MICRODOLLARS = 5500
TELNYX_TOLL_FREE_SMS_CARRIER_FEE_MICRODOLLARS = 4500
SMS_BASE_COST_ENV = "VIOLA_SMS_BASE_COST_MICRODOLLARS"
SMS_CARRIER_FEE_ENV = "VIOLA_SMS_CARRIER_FEE_MICRODOLLARS"

# GSM 03.38 basic character set (one septet each) and the extension table
# (escape + septet, so two each). Anything else forces UCS-2 encoding.
_GSM7_BASIC = frozenset(
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)
_GSM7_EXTENDED = frozenset("^{}\\[~]|\u20ac")
_GSM7_REPLACEMENTS = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2026": "...",
    "\u00a0": " ",
}

_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

SmsOptOutChecker = Callable[[str], bool | Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class SmsSegments:
    """How carriers will split and bill one SMS body."""

    encoding: str  # "GSM-7" or "UCS-2"
    units: int  # septets for GSM-7, UTF-16 code units for UCS-2
    segments: int


@dataclass(frozen=True, slots=True)
class SmsSendReceipt:
    """What Telnyx accepted for one send."""

    message_id: str
    parts: int | None = None


def count_sms_segments(text: str) -> SmsSegments:
    """Return the encoding and billed segment count for *text*.

    GSM-7: 160 septets fit one segment; a longer message is split into 153-septet
    parts (7 septets go to the concatenation header). Extension-table characters
    take two septets. Any character outside GSM-7 switches the whole message to
    UCS-2: 70 code units in one segment, 67 per part when split.
    """
    body = str(text or "")
    if all(ch in _GSM7_BASIC or ch in _GSM7_EXTENDED for ch in body):
        units = sum(2 if ch in _GSM7_EXTENDED else 1 for ch in body)
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        units = len(body.encode("utf-16-le")) // 2
        single, multi, encoding = 70, 67, "UCS-2"
    segments = 1 if units <= single else math.ceil(units / multi)
    return SmsSegments(encoding=encoding, units=units, segments=segments)


def gsm7_safe(text: str) -> str:
    """Return *text* folded to the GSM-7 alphabet so it never forces UCS-2.

    Curly quotes and dashes become ASCII; other characters outside GSM-7 are
    dropped. Use it on untrusted display text (merchant names) before it goes
    into an SMS body, so one emoji cannot double the billed segments.
    """
    out: list[str] = []
    for ch in str(text or ""):
        ch = _GSM7_REPLACEMENTS.get(ch, ch)
        out.extend(c for c in ch if c in _GSM7_BASIC or c in _GSM7_EXTENDED)
    return "".join(out)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s", name)
        return default
    return value if value >= 0 else default


def sms_cost_per_segment_microdollars() -> int:
    """Telnyx base price plus the carrier fee estimate for one segment."""
    return _env_int(SMS_BASE_COST_ENV, TELNYX_TOLL_FREE_SMS_BASE_MICRODOLLARS) + _env_int(
        SMS_CARRIER_FEE_ENV, TELNYX_TOLL_FREE_SMS_CARRIER_FEE_MICRODOLLARS
    )


def estimate_sms_cost_microdollars(segments: int) -> int:
    """Provider cost of an SMS with *segments* parts, in microdollars."""
    return max(0, int(segments)) * sms_cost_per_segment_microdollars()


def _operator_recipient_allowlist() -> frozenset[str]:
    raw = os.environ.get(SMS_RECIPIENT_ALLOWLIST_ENV, "")
    numbers: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            numbers.add(normalize_e164(item))
        except ValueError:
            logger.warning("Ignoring invalid number in %s", SMS_RECIPIENT_ALLOWLIST_ENV)
    return frozenset(numbers)


def _check_recipient_rule(recipient: str, purpose: str) -> None:
    if purpose not in SMS_PURPOSES:
        raise TelnyxSendError("SMS purpose must be one of: %s." % ", ".join(sorted(SMS_PURPOSES)))
    if purpose == SMS_PURPOSE_CONVERSATION_REPLY and recipient not in LAUNCH_SMS_ALLOWED_RECIPIENTS:
        raise TelnyxSendError("Two-way SMS replies are limited to the approved launch verification number.")
    if purpose == SMS_PURPOSE_COMPLIANCE_REPLY:
        return
    allowlist = _operator_recipient_allowlist()
    if allowlist and recipient not in allowlist:
        raise TelnyxSendError("SMS is limited to the operator recipient allowlist right now.")


class TelnyxSendError(RuntimeError):
    """Raised when Telnyx rejects or cannot accept an SMS send."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _mask_phone(phone: str) -> str:
    return "***%s" % phone[-4:] if phone and len(phone) >= 4 else "***"


def normalize_e164(raw_phone: str) -> str:
    """Return *raw_phone* as a strict E.164 string, or raise ``ValueError``."""
    value = str(raw_phone or "").strip()
    if value.startswith("+"):
        value = "+" + re.sub(r"\D", "", value[1:])
    else:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10:
            value = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            value = "+" + digits
        else:
            value = "+" + digits
    if not _E164_RE.fullmatch(value):
        raise ValueError("Enter a phone number in E.164 format, like +14145551234.")
    return value


def _telnyx_credentials() -> tuple[str, str, str | None]:
    """Return ``(api_key, from_number, messaging_profile_id)`` or raise.

    SMS requires the dedicated toll-free ``telnyx_sms_from_number``. Falling
    back to the voice number would send A2P SMS from the wrong Telnyx posture.
    """
    settings = get_settings()
    api_key = (getattr(settings, "telnyx_api_key", None) or "").strip()
    from_number_raw = (getattr(settings, "telnyx_sms_from_number", None) or "").strip()
    profile_id = (getattr(settings, "telnyx_messaging_profile_id", None) or "").strip() or None
    if not api_key or not from_number_raw:
        raise TelnyxSendError("SMS is not configured.")
    try:
        from_number = normalize_e164(from_number_raw)
    except ValueError as exc:
        raise TelnyxSendError("SMS sender number is not valid E.164.") from exc
    if from_number != APPROVED_SMS_FROM_NUMBER:
        raise TelnyxSendError("SMS sender is not the approved Telnyx toll-free number.")
    return api_key, from_number, profile_id


def telnyx_sms_configured() -> bool:
    """Return ``True`` when the minimum Telnyx send credentials are present."""
    try:
        _telnyx_credentials()
        return True
    except TelnyxSendError:
        return False


def _telnyx_error_message(status_code: int, payload: Any) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                detail = first.get("detail") or first.get("title") or first.get("code")
                if detail:
                    return "Telnyx rejected the SMS: %s" % detail
        message = payload.get("message")
        if message:
            return "Telnyx rejected the SMS: %s" % message
    return "Telnyx rejected the SMS with HTTP %d." % status_code


async def send_sms(
    to_phone: str,
    text: str,
    *,
    purpose: str,
    timeout: float = 10.0,
    phone_opted_out: SmsOptOutChecker | None = None,
    allow_opted_out: bool = False,
) -> str:
    """Send one SMS through Telnyx and return the provider message id.

    See :func:`send_sms_receipt` for the rules.
    """
    receipt = await send_sms_receipt(
        to_phone,
        text,
        purpose=purpose,
        timeout=timeout,
        phone_opted_out=phone_opted_out,
        allow_opted_out=allow_opted_out,
    )
    return receipt.message_id


async def send_sms_receipt(
    to_phone: str,
    text: str,
    *,
    purpose: str,
    timeout: float = 10.0,
    phone_opted_out: SmsOptOutChecker | None = None,
    allow_opted_out: bool = False,
) -> SmsSendReceipt:
    """Send one SMS through Telnyx and return what Telnyx accepted.

    Raises :class:`TelnyxSendError` on any non-2xx response, transport failure,
    missing configuration, a missing or unknown ``purpose``, a recipient the
    purpose does not allow, or a skipped opt-out check. Only
    ``compliance_reply`` may set ``allow_opted_out`` (a STOP confirmation goes
    to a number that just opted out). The body is truncated to
    :data:`SMS_MAX_CHARS`.
    """
    api_key, from_number, profile_id = _telnyx_credentials()
    recipient = normalize_e164(to_phone)
    _check_recipient_rule(recipient, purpose)
    if allow_opted_out and purpose != SMS_PURPOSE_COMPLIANCE_REPLY:
        raise TelnyxSendError("Only compliance replies may skip the opt-out check.")
    if not allow_opted_out:
        if phone_opted_out is None:
            raise TelnyxSendError("SMS opt-out status must be checked before sending.")
        opted_out_result = phone_opted_out(recipient)
        if inspect.isawaitable(opted_out_result):
            opted_out = await opted_out_result
        else:
            opted_out = bool(opted_out_result)
        if opted_out:
            raise TelnyxSendError("SMS recipient has opted out.")
    body = str(text or "")[:SMS_MAX_CHARS]

    payload: dict[str, Any] = {
        "from": from_number,
        "to": recipient,
        "text": body,
        "type": "SMS",
    }
    if profile_id:
        payload["messaging_profile_id"] = profile_id
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(TELNYX_MESSAGES_API_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Telnyx SMS request failed for phone=%s: %s", _mask_phone(recipient), exc.__class__.__name__)
        raise TelnyxSendError("Could not reach Telnyx. Please try again.") from exc

    try:
        response_body = response.json()
    except ValueError:
        response_body = None

    if response.status_code >= 400:
        logger.warning(
            "Telnyx SMS rejected for phone=%s status=%d",
            _mask_phone(recipient),
            response.status_code,
        )
        raise TelnyxSendError(
            _telnyx_error_message(response.status_code, response_body),
            status_code=response.status_code,
        )

    if isinstance(response_body, dict):
        data = response_body.get("data")
        if isinstance(data, dict):
            message_id = data.get("id")
            if message_id:
                parts_raw = data.get("parts")
                parts = parts_raw if isinstance(parts_raw, int) and not isinstance(parts_raw, bool) else None
                logger.info(
                    "Telnyx SMS accepted for phone=%s message_id=%s purpose=%s parts=%s",
                    _mask_phone(recipient),
                    message_id,
                    purpose,
                    parts,
                )
                return SmsSendReceipt(message_id=str(message_id), parts=parts)
    raise TelnyxSendError("Telnyx accepted the SMS request but did not return a message id.")
