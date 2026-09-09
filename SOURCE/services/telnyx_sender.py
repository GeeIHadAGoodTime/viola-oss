"""Shared async Telnyx SMS sender.

A single low-level send path used by:

- ``auth/sms_routes.py`` — outbound phone-verification OTP codes.
- ``backend/telnyx_sms_webhook.py`` — outbound replies (HELP/STOP confirmations
  and two-way conversational replies).

``admin/alerts.py`` keeps its own *synchronous* operator-paging sender because it
runs from a non-async alert-dispatch thread; that call site is intentionally not
routed through here.

All launch sends go through Telnyx ``POST /v2/messages`` from the approved
``+18337073533`` toll-free number and are restricted to the founder launch
verification phone. Toll-Free Verification for ``+18337073533`` was approved
2026-05-05, so no A2P 10DLC campaign is required for this path.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
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

_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

SmsOptOutChecker = Callable[[str], bool | Awaitable[bool]]


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
    timeout: float = 10.0,
    phone_opted_out: SmsOptOutChecker | None = None,
    allow_opted_out: bool = False,
) -> str:
    """Send one SMS through Telnyx and return the provider message id.

    Raises :class:`TelnyxSendError` on any non-2xx response, transport failure, or
    missing configuration. The message body is truncated to :data:`SMS_MAX_CHARS`.
    """
    api_key, from_number, profile_id = _telnyx_credentials()
    recipient = normalize_e164(to_phone)
    if recipient not in LAUNCH_SMS_ALLOWED_RECIPIENTS:
        raise TelnyxSendError("SMS launch gate allows sending only to the approved launch verification number.")
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
                logger.info("Telnyx SMS accepted for phone=%s message_id=%s", _mask_phone(recipient), message_id)
                return str(message_id)
    raise TelnyxSendError("Telnyx accepted the SMS request but did not return a message id.")
