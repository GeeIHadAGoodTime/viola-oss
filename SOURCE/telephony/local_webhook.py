"""Signed Telnyx call-control callbacks for independent local installations."""

from __future__ import annotations

import asyncio
import json
import time

from config.settings import settings
from fastapi import APIRouter, HTTPException, Request
from telephony.telnyx_signature import (
    TELNYX_TIMESTAMP_TOLERANCE_SECONDS,
    telnyx_timestamp_is_fresh,
    verify_telnyx_signature,
)

router = APIRouter(tags=["local-phone-webhook"])
_event_lock = asyncio.Lock()
_completed_events: dict[str, float] = {}
_AMD_EVENTS = frozenset(
    {
        "call.machine.detection.ended",
        "call.machine.premium.detection.ended",
        "call.machine.greeting.ended",
        "call.machine.premium.greeting.ended",
    }
)


@router.post("/webhooks/telnyx/local")
async def local_telnyx_webhook(request: Request):
    # This endpoint uses carrier signatures, never a desktop session or a
    # company account. Hosted deployments retain their own callback receiver.
    from intent.tools.phone_call import _get_manager, _uses_independent_local_phone

    if not _uses_independent_local_phone():
        raise HTTPException(404, "Local phone callbacks are not enabled")
    public_key = getattr(settings, "telnyx_webhook_public_key", "") or ""
    if not public_key:
        raise HTTPException(503, "Configure the Telnyx webhook public key")
    timestamp = request.headers.get("telnyx-timestamp", "")
    signature = request.headers.get("telnyx-signature-ed25519", "")
    body = await request.body()
    if not telnyx_timestamp_is_fresh(timestamp) or not verify_telnyx_signature(body, timestamp, signature, public_key):
        raise HTTPException(401, "Invalid carrier signature")
    try:
        data = json.loads(body)["data"]
        event_id, event_type = data["id"], data["event_type"]
        payload = data["payload"]
        call_control_id = payload["call_control_id"]
        if not all(isinstance(value, str) and value for value in (event_id, event_type, call_control_id)):
            raise ValueError("Missing event identity")
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Malformed carrier event") from None
    if event_type not in _AMD_EVENTS | {"call.answered", "call.hangup"}:
        return {"ok": True, "ignored": True}
    manager = _get_manager()
    if manager is None:
        raise HTTPException(503, "Local phone manager is not configured")
    # Serialize duplicate delivery while handling the event. Mark only after
    # success, so a failed handler or the dial-response race remains retryable.
    # Across restarts the manager's carrier facts are themselves idempotent.
    async with _event_lock:
        now = time.monotonic()
        for key, completed_at in list(_completed_events.items()):
            if now - completed_at > TELNYX_TIMESTAMP_TOLERANCE_SECONDS:
                del _completed_events[key]
        if event_id in _completed_events:
            return {"ok": True, "duplicate": True}
        if event_type == "call.answered":
            handled = await manager.handle_call_answered(call_control_id)
        elif event_type == "call.hangup":
            handled = await manager.handle_local_carrier_hangup(call_control_id, str(payload.get("hangup_cause", "")))
        else:
            # Unknown AMD classifications are still valid, consumed events.
            if manager.get_record_by_call_control_id(call_control_id) is None:
                raise HTTPException(503, "Call registration is not ready")
            await manager.handle_answering_machine_detection(
                call_control_id, str(payload.get("result", "")), event_type
            )
            handled = True
        if not handled:
            raise HTTPException(503, "Call registration is not ready")
        _completed_events[event_id] = now
    return {"ok": True}
