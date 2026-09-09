"""Phone call API routes -- call history, recording playback, cost tracking.

Provides REST endpoints for:
    - Call history listing and retrieval
    - Audio recording streaming (inbound, outbound, stereo)
    - Developer cost tracking (gated behind VIOLA_DEV_MODE)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from auth.csrf import csrf_required
from auth.dependencies import get_current_user_optional
from auth.models import User
from contracts.api_response import failure_response, success_response
from core.events.bus import get_event_bus
from core.events.types import CallTranscriptDelta
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from telephony.call_history import (
    CallHistoryEntry,
    delete_call_history,
    delete_call_history_for_user,
    get_call_dir,
    get_call_history,
    list_all_call_history,
    list_call_history,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Desktop -> cloud phone-data proxy (capstone, 2026-06-29)
# ---------------------------------------------------------------------------
# When phone_mode == "cloud" (the SaaS default), a phone call lives ONLY in the
# cloud container — both the live call (cloud CallManager) and the persisted
# history/transcript (cloud call_history store). The desktop's LOCAL phone-data
# routes therefore read an EMPTY local store for cloud calls (founder-observed
# 2026-06-29: history stuck at ~June 18). So every owner-scoped phone-data route
# below PROXIES to the cloud's /api/phone/* when phone_mode is cloud, attaching
# the logged-in user's cloud GoTrue bearer SERVER-SIDE (never in the browser —
# SEC-017). The cloud owner-scopes the response by that bearer's identity.
#
# The bearer is the same one the conversational command path resolves
# (intent/tools/phone_call._resolve_cloud_bearer_token via the active desktop
# session). When phone_mode == "local" (BYOK desktop dialing through the user's
# own Telnyx) the call data is local, so these routes read the local store with
# no proxy.


async def _maybe_proxy_phone_to_cloud(
    method: str,
    cloud_path: str,
    *,
    params: dict[str, object] | None = None,
    json_body: dict[str, object] | None = None,
) -> JSONResponse | None:
    """Proxy a phone-data request to the cloud, or return None to read locally.

    Returns a ``JSONResponse`` (the cloud's response re-emitted with its status)
    when ``phone_mode`` is cloud and the proxy ran. Returns ``None`` when
    ``phone_mode`` is local (the caller then reads the local store).

    When the cloud cannot be reached or the user has no resolvable cloud bearer
    (e.g. signed out of the cloud), this returns a clear failure JSONResponse
    rather than silently falling back to the empty local store — so a cloud-mode
    desktop never shows nothing-because-localhost-is-empty.
    """
    from telephony.phone_mode import phone_mode_is_cloud

    if not phone_mode_is_cloud():
        return None

    from telephony.desktop_cloud_proxy import CloudProxyUnavailable, proxy_phone_request

    try:
        status_code, body = await proxy_phone_request(method, cloud_path, params=params, json_body=json_body)
    except CloudProxyUnavailable as exc:
        logger.warning("Cloud phone proxy unavailable for %s %s: %s", method, cloud_path, exc)
        return JSONResponse(
            failure_response(
                "cloud_phone_unavailable",
                "Phone data is served from the cloud and could not be reached. "
                "Make sure you are signed in and try again.",
            ),
            status_code=502,
        )

    if isinstance(body, (dict, list)):
        return JSONResponse(body, status_code=status_code)
    return JSONResponse(
        failure_response("cloud_phone_bad_response", "Unexpected cloud phone response."),
        status_code=502,
    )


async def _require_call_auth(request: Request) -> None:
    """Require authentication for call history endpoints (C6 fix).

    Uses the app-level auth plugin (same pattern as payment_cards).
    """
    try:
        auth_plugin = getattr(request.app.state, "auth_plugin", None)
        if auth_plugin is None:
            from ui.security.auth import AuthenticationPlugin
            from ui.security.config import get_security_config

            auth_plugin = AuthenticationPlugin(get_security_config())
        authenticated = await auth_plugin.verify_request(request)
    except Exception:
        logger.exception("Auth check failed for call history endpoint")
        authenticated = False

    if not authenticated:
        logger.warning(
            "Call history auth denied for %s %s",
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=401,
            detail=failure_response("auth_required", "Authentication required."),
        )


router = APIRouter(
    prefix="/v1/calls",
    tags=["calls"],
    dependencies=[Depends(_require_call_auth)],
)

tos_router = APIRouter(prefix="/v1/phone", tags=["phone-tos"])
phone_calls_router = APIRouter(
    prefix="/v1/phone",
    tags=["phone-calls"],
    dependencies=[Depends(_require_call_auth)],
)


class OperatorMessageBody(BaseModel):
    text: str = ""


class TakeoverCallBody(BaseModel):
    reason: str = "Owner requested live takeover from the phone-call UI."
    target: str = ""


def _route_user_id(user: User | None) -> str:
    return str(getattr(user, "id", "") or "").strip()


def _auth_required_response(message: str = "Authentication required.") -> JSONResponse:
    return JSONResponse(
        failure_response("auth_required", message),
        status_code=401,
    )


def _active_call_not_found_response() -> JSONResponse:
    return JSONResponse(
        failure_response(
            "call_not_found",
            "Call not found or already ended.",
        ),
        status_code=404,
    )


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


async def _resolve_user_plan_family(user: User | None) -> str:
    """Resolve canonical phone-usage plan family for the current user."""
    if user is None:
        return "free"

    try:
        from auth.database import get_auth_db
        from auth.models import resolve_user_entitlement

        db = get_auth_db()
        subscription = await db.subscriptions.get_subscription(user.id)
        entitlement = resolve_user_entitlement(user, subscription)
        if entitlement.has_paid_access:
            return entitlement.plan_family.value
    except Exception:
        logger.debug("Falling back to free phone usage plan family", exc_info=True)

    return "free"


# ---------------------------------------------------------------------------
# Phone ToS endpoints
# ---------------------------------------------------------------------------


@tos_router.post("/accept-tos")
async def accept_phone_tos(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Accept the Phone Calling Terms of Service."""
    from auth.ip_utils import extract_client_ip
    from telephony.phone_tos import get_phone_tos

    if user is None or not user.id:
        return JSONResponse(
            failure_response(
                "authentication_required",
                "Authentication required before accepting the Phone Calling Terms of Service.",
            ),
            status_code=401,
        )
    user_id = user.id
    await csrf_required(request)
    # Capture request provenance for the consent audit. The cloud phone_tos
    # table has ip_address_hash / user_agent_hash columns explicitly for
    # this — matches the shape used by auth_events. Resolved through the
    # trusted-proxy-aware helper so a forged X-Forwarded-For from an
    # untrusted LAN client cannot poison the stored hash.
    client_ip = extract_client_ip(request)
    user_agent = request.headers.get("user-agent")
    tos = get_phone_tos()
    try:
        await tos.accept(user_id, ip_address=client_ip, user_agent=user_agent)
    except ValueError as exc:
        return JSONResponse(
            failure_response("authentication_required", str(exc)),
            status_code=401,
        )
    return JSONResponse(success_response({"accepted": True}))


@tos_router.get("/tos-status")
async def get_tos_status(
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Get the Phone Calling ToS acceptance status for the current user."""
    from telephony.phone_tos import get_phone_tos

    if user is None or not user.id:
        return JSONResponse(
            failure_response(
                "authentication_required",
                "Authentication required before checking Phone Calling Terms of Service status.",
            ),
            status_code=401,
        )
    user_id = user.id
    tos = get_phone_tos()
    return JSONResponse(success_response(await tos.get_status(user_id)))


@phone_calls_router.delete("/calls/{call_id}")
async def end_active_phone_call(
    call_id: str,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """End an active desktop phone call.

    Multi-tenant: the authenticated user is required; CallManager
    owner-checks the record before hanging up so a known/guessed
    call_id from a different tenant cannot terminate it.

    Cloud-mode: the live call lives in the cloud CallManager, so proxy the
    hang-up to /api/phone/calls/{id}; the cloud owner-checks before ending.
    """
    proxied = await _maybe_proxy_phone_to_cloud("DELETE", "/api/phone/calls/%s" % call_id)
    if proxied is not None:
        return proxied

    from intent.tools.phone_call import _get_manager

    manager = _get_manager()
    if manager is None:
        return JSONResponse(
            failure_response(
                "phone_calling_not_configured",
                "Phone calling is not configured.",
            ),
            status_code=503,
        )

    user_id = user.id if user else None
    if not user_id:
        return JSONResponse(
            failure_response("auth_required", "Authentication required."),
            status_code=401,
        )
    ended = await manager.end_call(call_id, user_id=user_id)
    if not ended:
        return JSONResponse(
            failure_response(
                "call_not_found",
                "Call not found or already ended.",
            ),
            status_code=404,
        )

    return JSONResponse(success_response({"call_id": call_id, "message": "Call ended."}))


@phone_calls_router.post("/calls/{call_id}/operator-message")
async def send_operator_message(
    call_id: str,
    body: OperatorMessageBody,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Send a private operator note into a live phone agent's next LLM turn.

    Cloud-mode: the live call lives in the cloud CallManager, so proxy the note
    to /api/phone/calls/{id}/operator-message; the cloud owner-checks the record
    before enqueuing it.
    """
    text = " ".join(str(body.text or "").split()).strip()
    if not text or len(text) > 1000:
        return JSONResponse(
            failure_response(
                "invalid_message",
                "Operator message must be between 1 and 1000 characters.",
            ),
            status_code=400,
        )

    proxied = await _maybe_proxy_phone_to_cloud(
        "POST",
        "/api/phone/calls/%s/operator-message" % call_id,
        json_body={"text": text},
    )
    if proxied is not None:
        return proxied

    user_id = _route_user_id(user)
    if not user_id:
        return _auth_required_response()

    from telephony.listen_ws import _find_active_call

    record = _find_active_call(call_id)
    if record is None:
        return _active_call_not_found_response()

    record_user_id = str(getattr(record, "user_id", "") or "").strip()
    if record_user_id != user_id:
        logger.warning(
            "Operator message ownership violation: user=%s attempted access to call_id=%s owner=%s",
            user_id,
            call_id,
            record_user_id or "<unset>",
        )
        return _active_call_not_found_response()

    llm = getattr(record, "_llm_service", None)
    enqueue_operator_note = getattr(llm, "enqueue_operator_note", None)
    if llm is None or not callable(enqueue_operator_note):
        return JSONResponse(
            failure_response(
                "operator_message_unsupported",
                "This call cannot receive operator messages.",
            ),
            status_code=503,
        )

    await enqueue_operator_note(text)
    transcript_text = "[operator] %s" % text
    transcript_entry = {
        "role": "system",
        "text": transcript_text,
        "ts": _now_iso(),
    }
    try:
        transcript = getattr(record, "transcript", None)
        if isinstance(transcript, list):
            transcript.append(transcript_entry)
    except Exception as exc:
        logger.debug("Call %s: failed to append operator note to transcript: %s", call_id, exc)

    try:
        bus = get_event_bus()
        if bus:
            bus.publish(
                CallTranscriptDelta(
                    call_id=call_id,
                    role="system",
                    text=transcript_text,
                    partial=False,
                    ts=transcript_entry["ts"],
                )
            )
    except Exception as exc:
        logger.debug("Call %s: failed to publish operator transcript delta: %s", call_id, exc)

    return JSONResponse(success_response({"call_id": call_id, "message": "Note delivered to agent."}))


@phone_calls_router.post("/calls/{call_id}/takeover")
async def take_over_active_phone_call(
    call_id: str,
    body: TakeoverCallBody,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Ring the owner into an active phone call through Telnyx conference.

    Cloud-mode: the live call lives in the cloud CallManager (and the Telnyx
    conference is dialed there), so proxy to /api/phone/calls/{id}/takeover; the
    cloud owner-checks the record before any dial.
    """
    proxied = await _maybe_proxy_phone_to_cloud(
        "POST",
        "/api/phone/calls/%s/takeover" % call_id,
        json_body={"reason": body.reason, "target": body.target},
    )
    if proxied is not None:
        return proxied

    user_id = _route_user_id(user)
    if not user_id:
        return _auth_required_response()

    from telephony.listen_ws import _find_active_call

    record = _find_active_call(call_id)
    if record is None:
        return _active_call_not_found_response()

    record_user_id = str(getattr(record, "user_id", "") or "").strip()
    if record_user_id != user_id:
        logger.warning(
            "Call takeover ownership violation: user=%s attempted access to call_id=%s owner=%s",
            user_id,
            call_id,
            record_user_id or "<unset>",
        )
        return _active_call_not_found_response()

    from intent.tools.phone_call import _get_manager

    manager = _get_manager()
    if manager is None:
        return JSONResponse(
            failure_response(
                "phone_calling_not_configured",
                "Phone calling is not configured.",
            ),
            status_code=503,
        )

    try:
        import telnyx

        from telephony.call_tools import (
            conference_user_for_call,
            submit_consult_user_reply,
        )

        telnyx_client = telnyx.AsyncTelnyx(api_key=manager.config.api_key)
        result = await conference_user_for_call(
            telnyx_client=telnyx_client,
            call_record=record,
            connection_id=manager.config.sip_connection_id,
            from_number_getter=lambda: manager.config.phone_number,
            max_duration=manager.config.max_call_duration,
            target=body.target,
            reason=body.reason,
            llm=getattr(record, "_llm_service", None),
        )
    except (ImportError, RuntimeError, ValueError) as exc:
        logger.warning("Call takeover failed for %s: %s", call_id, exc)
        return JSONResponse(
            failure_response("takeover_failed", "Owner takeover could not be started."),
            status_code=503,
        )

    if result.get("conference_status") == "error":
        return JSONResponse(
            failure_response(
                "takeover_failed",
                str(result.get("error") or "Owner takeover could not be started."),
            ),
            status_code=409,
        )

    submit_consult_user_reply(call_id, "I am joining the call now.", user_id=user_id)
    result["call_id"] = call_id
    result["message"] = "Owner phone is ringing to join the live call."
    return JSONResponse(success_response(result))


@tos_router.post("/call/{call_id}/reply")
async def reply_to_phone_call_consultation(
    call_id: str,
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Submit a web-chat answer for a pending mid-call consultation."""
    await _require_call_auth(request)
    user_id = _route_user_id(user)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Authentication required."}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    answer = str(body.get("answer") or "").strip()
    if not answer:
        return JSONResponse({"ok": False, "error": "answer is required."}, status_code=400)

    from telephony.call_tools import submit_consult_user_reply

    ok, reason = submit_consult_user_reply(call_id, answer, user_id=user_id)
    if ok:
        return JSONResponse({"ok": True})
    if reason == "auth_required":
        return JSONResponse({"ok": False, "error": "Authentication required."}, status_code=401)
    if reason == "forbidden":
        return JSONResponse({"ok": False, "error": "Forbidden."}, status_code=403)
    if reason == "not_found":
        return JSONResponse(
            {"ok": False, "error": "No pending consultation for this call."},
            status_code=404,
        )
    return JSONResponse({"ok": False, "error": reason}, status_code=400)


# ---------------------------------------------------------------------------
# Phone usage endpoint
# ---------------------------------------------------------------------------


@router.get("/usage")
async def get_phone_usage(
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Return current phone usage statistics for the authenticated user."""
    from telephony.usage import company_phone_billing_available, get_phone_billing

    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()
    tier = await _resolve_user_plan_family(user) if company_phone_billing_available() else "free"
    billing = get_phone_billing()
    summary = await billing.get_usage_summary(user_id, tier)
    return JSONResponse({"ok": True, **summary})


# ---------------------------------------------------------------------------
# Live cloud phone-event relay control (capstone live transcript, 2026-06-30)
# ---------------------------------------------------------------------------
# A cloud-mode phone call's live events (transcript / cost / consult / state)
# are broadcast onto the CLOUD /ws/events hub, which the desktop's localhost
# React layer never sees. The desktop's LOCAL backend therefore subscribes to
# the cloud /ws/events SERVER-SIDE (telephony/phone_cloud_event_relay.py) using
# the same server-side bearer the REST proxy uses (never a browser-held cloud
# token, SEC-017) and republishes the phone events onto the desktop's LOCAL
# /ws/events hub, where the existing useWebSocket connection renders them.
#
# These control routes let the desktop phone tab (useCloudPhoneEvents) start the
# relay while the user cares about phone events and tear it down when there is no
# call. They never carry a cloud token — the bearer is resolved server-side
# inside the relay. On phone_mode == "local" (no cloud call) the relay is not
# needed, so start is a clean no-op.


@phone_calls_router.post("/cloud-events/start")
async def start_cloud_phone_event_relay(
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Ensure the server-side cloud->local phone-event relay is running.

    Idempotent. Returns ``{active: bool}`` — ``active`` is whether the relay is
    (now) running. The relay only applies to cloud-mode calls; in local mode the
    live events are already on the local hub, so this is a clean no-op.
    """
    user_id = _route_user_id(user)
    if not user_id:
        return _auth_required_response()

    # The relay is a DESKTOP construct: it bridges the cloud /ws/events into the
    # desktop's local hub. On the cloud surface itself (same-origin web client)
    # the events are already on the local socket, so there is nothing to relay.
    from config.settings import settings as _settings

    if str(getattr(_settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud":
        return JSONResponse(success_response({"active": False, "reason": "cloud_surface"}))

    from telephony.phone_mode import phone_mode_is_cloud

    if not phone_mode_is_cloud():
        return JSONResponse(success_response({"active": False, "reason": "phone_mode_local"}))

    from telephony.phone_cloud_event_relay import ensure_phone_cloud_event_relay_running

    running = await ensure_phone_cloud_event_relay_running()
    return JSONResponse(success_response({"active": bool(running)}))


@phone_calls_router.post("/cloud-events/stop")
async def stop_cloud_phone_event_relay_route(
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Drop the phone tab's interest in the relay (idempotent).

    This releases the UI LEASE, it does not force the socket down. A call placed
    by this desktop holds its own lease, so navigating off the phone tab mid-call
    (or right after one, before the next) can no longer tear down the stream the
    next ``call_started`` has to arrive on — the hole that made the second call
    of a session render nothing. Returns whether the relay is still running for
    somebody else.
    """
    user_id = _route_user_id(user)
    if not user_id:
        return _auth_required_response()

    from telephony.phone_mode import phone_mode_is_cloud

    if not phone_mode_is_cloud():
        return JSONResponse(success_response({"active": False, "reason": "phone_mode_local"}))

    from telephony.phone_cloud_event_relay import (
        get_phone_cloud_event_relay,
        release_phone_cloud_event_relay_ui_lease,
    )

    await release_phone_cloud_event_relay_ui_lease()
    return JSONResponse(success_response({"active": get_phone_cloud_event_relay().is_running}))


# ---------------------------------------------------------------------------
# Call history endpoints
# ---------------------------------------------------------------------------


def _current_user_id(user: User | None) -> str:
    return str(getattr(user, "id", "") or "").strip()


def _call_not_found_response() -> JSONResponse:
    return JSONResponse(
        failure_response("call_not_found", "Call not found."),
        status_code=404,
    )


def _auth_required_response() -> JSONResponse:
    return JSONResponse(
        failure_response("auth_required", "Authentication required."),
        status_code=401,
    )


def _get_phone_call_manager_for_queue():
    try:
        from config.settings import settings

        surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    except Exception:
        surface = "desktop"

    if surface == "cloud":
        try:
            from telephony.cloud_routes import _get_cloud_manager

            manager = _get_cloud_manager()
            if manager is not None:
                return manager
        except Exception as exc:
            logger.debug("Cloud phone queue manager lookup failed: %s", exc)

    try:
        from intent.tools.phone_call import _get_manager

        return _get_manager()
    except Exception as exc:
        logger.debug("Desktop phone queue manager lookup failed: %s", exc)
        return None


@router.get("/active")
async def get_active_call(
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Return the authenticated user's currently-live call, or null.

    The desktop learns ``activeCallId`` from transient ``call_started`` /
    ``call_consultation`` WebSocket events. A phone tab opened *after* those
    events fired (the founder-observed mid-call symptom, first production call
    2026-06-29) has no way to recover the live call and falls back to the
    history list. This endpoint lets the tab reconstruct the live-call screen
    on open / WS reconnect by asking "is a call live for me right now?".

    Returns ``{"active_call": null}`` when no call is live — never an error —
    so the frontend can poll it cheaply and treat the no-call case as normal.

    Cloud-mode: the live call lives in the cloud CallManager, so proxy to
    /api/phone/active (owner-scoped by the server-side bearer).
    """
    proxied = await _maybe_proxy_phone_to_cloud("GET", "/api/phone/active")
    if proxied is not None:
        return proxied

    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()

    manager = _get_phone_call_manager_for_queue()
    if manager is None:
        # No phone subsystem configured == no live call, not an error.
        return JSONResponse(success_response({"active_call": None}))

    getter = getattr(manager, "get_active_call_for_user", None)
    if not callable(getter):
        return JSONResponse(success_response({"active_call": None}))

    record = getter(user_id)
    if record is None:
        return JSONResponse(success_response({"active_call": None}))

    started_at = getattr(record, "started_at", None)
    payload = {
        "call_id": str(getattr(record, "call_id", "") or ""),
        "phone_number": str(getattr(record, "phone_number", "") or ""),
        "task": str(getattr(record, "task", "") or ""),
        "status": (
            record.status.value
            if hasattr(getattr(record, "status", None), "value")
            else str(getattr(record, "status", "") or "")
        ),
        "started_at": (started_at.isoformat() if started_at is not None else ""),
        "duration_seconds": float(getattr(record, "duration_seconds", 0.0) or 0.0),
        "estimated_cost_usd": float(getattr(record, "estimated_cost_usd", 0.0) or 0.0),
        "current_cost_usd": float(getattr(record, "estimated_cost_usd", 0.0) or 0.0),
    }
    return JSONResponse(success_response({"active_call": payload}))


@router.get("/queue")
async def list_call_queue(
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """List confirmed outbound calls waiting behind the live call.

    Cloud-mode: cloud calls queue in the cloud CallManager, so proxy to
    /api/phone/queue (owner-scoped by the server-side bearer).
    """
    proxied = await _maybe_proxy_phone_to_cloud("GET", "/api/phone/queue")
    if proxied is not None:
        return proxied

    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()

    manager = _get_phone_call_manager_for_queue()
    if manager is None:
        return JSONResponse(
            failure_response("phone_calling_not_configured", "Phone calling is not configured."),
            status_code=503,
        )

    return JSONResponse(success_response({"queue": await manager.list_call_queue(user_id=user_id)}))


@router.delete("/queue/{position}")
async def delete_call_queue_position(
    position: int,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Remove one confirmed outbound call from the queue by visible position.

    Cloud-mode: the cloud call is queued in the cloud CallManager, so proxy to
    DELETE /api/phone/queue/{position} (owner-scoped by the server-side bearer).
    """
    proxied = await _maybe_proxy_phone_to_cloud("DELETE", "/api/phone/queue/%s" % position)
    if proxied is not None:
        return proxied

    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()

    manager = _get_phone_call_manager_for_queue()
    if manager is None:
        return JSONResponse(
            failure_response("phone_calling_not_configured", "Phone calling is not configured."),
            status_code=503,
        )

    removed = await manager.remove_queued_call(position, user_id=user_id)
    if removed is None:
        return JSONResponse(
            failure_response("queued_call_not_found", "Queued call not found."),
            status_code=404,
        )
    return JSONResponse(
        success_response(
            {
                "removed": removed,
                "queue": await manager.list_call_queue(user_id=user_id),
            }
        )
    )


def _get_owned_call_history(call_id: str, user_id: str) -> CallHistoryEntry | None:
    entry = get_call_history(call_id)
    if entry is None:
        return None
    if entry.user_id != user_id:
        logger.warning(
            "Call ownership violation: user=%s attempted access to call_id=%s owner=%s",
            user_id or "<unauthenticated>",
            call_id,
            entry.user_id or "<unset>",
        )
        return None
    return entry


def _require_owned_call_history(call_id: str, user_id: str) -> CallHistoryEntry:
    entry = _get_owned_call_history(call_id, user_id)
    if entry is None:
        raise HTTPException(404, "Call not found.")
    return entry


def _call_history_payload(entries) -> dict:
    return success_response(
        {
            "count": len(entries),
            "calls": [
                {
                    "call_id": e.call_id,
                    "phone_number": e.phone_number,
                    "task": e.task,
                    "status": e.status,
                    "duration_seconds": e.duration_seconds,
                    # A call that never connected has no started_at (#3554), so
                    # the list has to carry the timestamps it DOES have or the
                    # call log has nothing to render a date from.
                    "created_at": e.created_at,
                    "started_at": e.started_at,
                    "ended_at": e.ended_at,
                    "summary": e.summary,
                    "has_recording": bool(e.recording_paths),
                    "disclosure_spoken": bool(e.disclosure_spoken),
                }
                for e in entries
            ],
        }
    )


async def _list_calls_for_current_user(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """List call records for the authenticated owner only.

    Cloud-mode calls live only in the cloud container, so proxy to
    /api/phone/history; the cloud owner-scopes by the server-side bearer.
    """
    proxied = await _maybe_proxy_phone_to_cloud("GET", "/api/phone/history", params={"limit": limit, "offset": offset})
    if proxied is not None:
        return proxied

    user_id = _current_user_id(user)
    if not user_id:
        return JSONResponse(
            failure_response("auth_required", "Authentication required."),
            status_code=401,
        )

    entries = list_call_history(user_id=user_id, limit=limit, offset=offset)
    return JSONResponse(_call_history_payload(entries))


@router.get("/history")
async def list_calls(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    return await _list_calls_for_current_user(limit=limit, offset=offset, user=user)


@phone_calls_router.get("/history")
async def list_phone_calls(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    return await _list_calls_for_current_user(limit=limit, offset=offset, user=user)


@phone_calls_router.delete("/all-history")
async def delete_all_phone_call_history(
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Delete every persisted phone-call history item for the authenticated owner."""
    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()

    deleted_count = delete_call_history_for_user(user_id)
    return JSONResponse(success_response({"deleted_count": deleted_count}))


@phone_calls_router.get("/calls/{call_id}/transcript")
async def get_phone_call_transcript(
    call_id: str,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Return a persisted phone-call transcript for the authenticated owner.

    Cloud-mode: a past cloud call's transcript lives only in the cloud
    container's per-user transcript partition, so proxy to
    /api/phone/history/transcript?call_id=... (owner-scoped, 404 on a foreign
    id). The local /v1/.../transcript would 404 for a cloud call.
    """
    proxied = await _maybe_proxy_phone_to_cloud("GET", "/api/phone/history/transcript", params={"call_id": call_id})
    if proxied is not None:
        return proxied

    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()

    entry = _get_owned_call_history(call_id, user_id)
    if entry is None:
        return _call_not_found_response()

    # The metadata.json transcript field is encrypted-or-redacted by
    # design (PII at rest). The plaintext turn-by-turn transcript lives
    # in the authenticated owner's transcript partition — prefer that,
    # fall back to the metadata field only if the dedicated file is missing.
    transcript: list[dict[str, str]] | str = entry.transcript
    try:
        from telephony.call_manager import _load_persisted_call_transcript

        payload = _load_persisted_call_transcript(entry.call_id, user_id=user_id)
        if payload is not None:
            tr = payload.get("transcript")
            if isinstance(tr, list):
                transcript = tr
    except Exception:
        logger.exception("Failed to load plaintext transcript for call %s", entry.call_id)

    return JSONResponse(
        success_response(
            {
                "call_id": entry.call_id,
                "phone_number": entry.phone_number,
                "task": entry.task,
                "status": entry.status,
                "started_at": entry.started_at,
                "ended_at": entry.ended_at,
                "duration_seconds": entry.duration_seconds,
                "summary": entry.summary,
                "transcript": transcript,
            }
        )
    )


@router.get("/{call_id}")
async def get_call(
    call_id: str,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Get full call metadata + transcript."""
    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()

    entry = _get_owned_call_history(call_id, user_id)
    if entry is None:
        return _call_not_found_response()

    from dataclasses import asdict

    return JSONResponse(success_response(asdict(entry)))


@router.delete("/{call_id}")
async def delete_call(
    call_id: str,
    user: User | None = Depends(get_current_user_optional),
) -> JSONResponse:
    """Delete a call record and all its files."""
    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()

    if _get_owned_call_history(call_id, user_id) is None:
        return _call_not_found_response()

    deleted = delete_call_history(call_id)
    if not deleted:
        return _call_not_found_response()
    return JSONResponse(success_response({"message": "Call %s deleted" % call_id}))


# ---------------------------------------------------------------------------
# Audio streaming endpoints
# ---------------------------------------------------------------------------


def _get_wav_path(
    call_id: str,
    variant: str,
    user_id: str,
    entry: CallHistoryEntry | None = None,
) -> Path:
    """Resolve a WAV file path and validate it exists.

    Recording files land in two possible locations depending on which
    storage backend wrote them:
      1. ``data/call_recordings/<call_id>_<variant>.wav`` (current default,
         see ``telephony/recording_storage.py:80``)
      2. Colocated next to metadata.json in
         ``<call_history>/<call_id>/<call_id>_<variant>.wav`` (legacy layout)

    The call's metadata.json may also carry an authoritative
    ``recording_paths[variant]`` URI. Check that FIRST so a moved file
    is followed automatically.
    """
    entry = entry or _require_owned_call_history(call_id, user_id)

    # Authoritative: ask the call's metadata.json
    try:
        paths = getattr(entry, "recording_paths", None) or {}
        uri = paths.get(variant) if isinstance(paths, dict) else None
        if isinstance(uri, str) and uri:
            if uri.startswith("file://"):
                candidate = Path(uri[7:])
            else:
                candidate = Path(uri)
            if candidate.exists():
                return candidate
    except Exception:
        logger.debug(
            "Failed to resolve %s/%s recording via metadata; falling back to known dirs",
            call_id,
            variant,
        )

    # Fallback 1: current default writer location
    fallback1 = Path("data") / "call_recordings" / ("%s_%s.wav" % (call_id, variant))
    if fallback1.exists():
        return fallback1

    # Fallback 2: legacy colocation
    call_dir = get_call_dir(call_id)
    fallback2 = call_dir / ("%s_%s.wav" % (call_id, variant))
    if fallback2.exists():
        return fallback2

    raise HTTPException(404, "Recording not found: %s/%s" % (call_id, variant))


def _ensure_stereo_full_wav(
    call_id: str,
    user_id: str,
    entry: CallHistoryEntry | None = None,
) -> Path:
    """Materialize a stereo (caller=L, Viola=R) WAV for the call.

    The recorder writes inbound + outbound as separate mono files. To
    deliver a single "full conversation" stream we synthesize the
    stereo WAV on first request and cache it next to the mono files.

    Returns the path to the stereo WAV. Raises 404 if neither mono
    leg exists; if only one leg exists it's served as stereo with the
    other channel silent.
    """
    entry = entry or _require_owned_call_history(call_id, user_id)
    try:
        inbound = _get_wav_path(call_id, "inbound", user_id, entry)
    except HTTPException:
        inbound = None
    try:
        outbound = _get_wav_path(call_id, "outbound", user_id, entry)
    except HTTPException:
        outbound = None
    if inbound is None and outbound is None:
        raise HTTPException(404, "No recordings exist for call %s" % call_id)

    leg_for_cache_dir = inbound or outbound
    stereo_path = leg_for_cache_dir.with_name("%s_full.wav" % call_id)

    # Cache: regenerate only if the stereo file is missing or older than
    # the most recent mono leg.
    def _mtime(p: Path | None) -> float:
        try:
            return p.stat().st_mtime if p else 0.0
        except OSError:
            return 0.0

    newest_mono = max(_mtime(inbound), _mtime(outbound))
    if stereo_path.exists() and _mtime(stereo_path) >= newest_mono:
        return stereo_path

    import wave

    def _read_mono(path: Path | None) -> tuple[int, int, bytes]:
        if path is None:
            return (16000, 2, b"")
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1:
                raise HTTPException(500, "Expected mono recording at %s" % path)
            return (w.getframerate(), w.getsampwidth(), w.readframes(w.getnframes()))

    in_rate, in_width, in_pcm = _read_mono(inbound)
    out_rate, out_width, out_pcm = _read_mono(outbound)

    rate = in_rate if inbound else out_rate
    width = in_width if inbound else out_width
    if inbound and outbound and (in_rate != out_rate or in_width != out_width):
        raise HTTPException(500, "Inbound/outbound recordings have mismatched format")

    sample_size = width
    in_frames = len(in_pcm) // sample_size if in_pcm else 0
    out_frames = len(out_pcm) // sample_size if out_pcm else 0
    n_frames = max(in_frames, out_frames)

    silence = b"\x00" * sample_size
    stereo = bytearray()
    for i in range(n_frames):
        left = in_pcm[i * sample_size : (i + 1) * sample_size] if i < in_frames else silence
        right = out_pcm[i * sample_size : (i + 1) * sample_size] if i < out_frames else silence
        stereo.extend(left)
        stereo.extend(right)

    with wave.open(str(stereo_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(stereo))

    return stereo_path


@router.get("/{call_id}/audio")
async def stream_full_audio(
    call_id: str,
    user: User | None = Depends(get_current_user_optional),
) -> Response:
    """Stream full stereo WAV recording (caller=L, Viola=R).

    Materialized on first request from the separate inbound + outbound
    mono files written by the recorder.
    """
    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()
    entry = _get_owned_call_history(call_id, user_id)
    if entry is None:
        return _call_not_found_response()
    return FileResponse(
        _ensure_stereo_full_wav(call_id, user_id, entry),
        media_type="audio/wav",
        filename="%s_full.wav" % call_id,
    )


@router.get("/{call_id}/audio/inbound")
async def stream_inbound_audio(
    call_id: str,
    user: User | None = Depends(get_current_user_optional),
) -> Response:
    """Stream caller audio only (mono)."""
    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()
    entry = _get_owned_call_history(call_id, user_id)
    if entry is None:
        return _call_not_found_response()
    return FileResponse(
        _get_wav_path(call_id, "inbound", user_id, entry),
        media_type="audio/wav",
        filename="%s_inbound.wav" % call_id,
    )


@router.get("/{call_id}/audio/outbound")
async def stream_outbound_audio(
    call_id: str,
    user: User | None = Depends(get_current_user_optional),
) -> Response:
    """Stream Viola audio only (mono)."""
    user_id = _current_user_id(user)
    if not user_id:
        return _auth_required_response()
    entry = _get_owned_call_history(call_id, user_id)
    if entry is None:
        return _call_not_found_response()
    return FileResponse(
        _get_wav_path(call_id, "outbound", user_id, entry),
        media_type="audio/wav",
        filename="%s_outbound.wav" % call_id,
    )


# ---------------------------------------------------------------------------
# Developer cost tracking (gated behind dev mode)
# ---------------------------------------------------------------------------


def _check_dev_mode() -> None:
    """Raise 403 if not in dev mode."""
    from config.settings import settings

    if not getattr(settings, "dev_mode", False) and not getattr(settings, "debug_mode", False):
        raise HTTPException(403, "Cost endpoints require dev/debug mode")


dev_router = APIRouter(prefix="/v1/dev/calls", tags=["dev-calls"])


@dev_router.get("/costs")
async def aggregate_costs(
    since_days: int = Query(30, ge=1, le=365),
) -> JSONResponse:
    """Aggregate cost summary across all calls."""
    _check_dev_mode()

    from telephony.cost_aggregate import CostAggregator

    aggregator = CostAggregator()
    summary = aggregator.get_summary(since_days=since_days)
    return JSONResponse({"ok": True, **summary})


@dev_router.get("/costs/{call_id}")
async def per_call_cost(call_id: str) -> JSONResponse:
    """Per-call cost breakdown."""
    _check_dev_mode()

    entry = get_call_history(call_id)
    if entry is None:
        raise HTTPException(404, "Call not found: %s" % call_id)
    return JSONResponse({"ok": True, "cost": entry.cost_breakdown})


@dev_router.get("/costs/export")
async def export_costs() -> JSONResponse:
    """Export all call costs as JSON (CSV-friendly structure)."""
    _check_dev_mode()

    entries = list_all_call_history(limit=1000)
    rows = []
    for e in entries:
        cost = e.cost_breakdown
        if cost:
            rows.append(
                {
                    "call_id": e.call_id,
                    "phone_number": e.phone_number,
                    "started_at": e.started_at,
                    "duration_seconds": e.duration_seconds,
                    "total_cost_usd": cost.get("total_cost_usd", 0),
                    "telnyx_cost_usd": cost.get("telnyx", {}).get("cost_usd", 0),
                    "llm_cost_usd": cost.get("llm", {}).get("cost_usd", 0),
                    "llm_model": cost.get("llm", {}).get("model", ""),
                    "prompt_tokens": cost.get("llm", {}).get("prompt_tokens", 0),
                    "completion_tokens": cost.get("llm", {}).get("completion_tokens", 0),
                }
            )
    return JSONResponse({"ok": True, "rows": rows})
