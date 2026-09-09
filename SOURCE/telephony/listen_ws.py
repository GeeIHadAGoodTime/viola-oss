"""WebSocket endpoint for call listen-in and takeover.

/ws/call-listen/{call_id} — connects to an active call's audio tee
processors for real-time audio monitoring and optional voice takeover.

Protocol:
    Server → Client (binary): 0x00 + PCM (inbound) or 0x01 + PCM (outbound)
    Server → Client (text):   {"type": "call_status", ...}
    Client → Server (text):   {"type": "takeover"} or {"type": "release"}
    Client → Server (binary): Raw int16 PCM at 16kHz (takeover mic audio)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json

from core.events.bus import get_event_bus
from core.events.types import (
    CallListenerJoined,
    CallTakeoverReleased,
    CallTakeoverStarted,
    CallTranscriptDelta,
)
from core.logging_config import get_logger
from fastapi import WebSocket, WebSocketDisconnect
from ui.api.routes.websocket_auth import get_websocket_session_context
from ui.core.security import check_websocket_origin, reject_websocket

logger = get_logger(__name__)
_LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_loopback_client_host(client_host: str | None) -> bool:
    return bool(client_host) and client_host.lower() in _LOOPBACK_CLIENT_HOSTS


def _ws_query_param(websocket: WebSocket, name: str) -> str | None:
    query_params = getattr(websocket, "query_params", {})
    getter = getattr(query_params, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return str(value) if value is not None else None


def _unsubscribe_event(bus, subscription) -> None:
    try:
        unsubscribe = getattr(bus, "unsubscribe", None)
        if callable(unsubscribe):
            unsubscribe(subscription)
        elif hasattr(subscription, "dispose"):
            subscription.dispose()
        elif callable(subscription):
            subscription()
    except Exception as exc:
        logger.debug("Failed to unsubscribe call listener event handler: %s", exc)


def _find_active_call(call_id: str):
    """Find an active CallRecord with tee processors.

    Searches cloud first for hosted deployments, then the desktop-local
    CallManager used by /v1/command calls on the 8756 desktop app.
    Returns the CallRecord or None.
    """
    try:
        from telephony.cloud_routes import _get_cloud_manager

        cloud_manager = _get_cloud_manager()
        if cloud_manager is not None:
            record = cloud_manager._active_calls.get(call_id)
            if record is not None:
                return record
    except Exception as exc:
        logger.debug("Failed to find cloud active call %s: %s", call_id, exc)

    try:
        from intent.tools.phone_call import _get_manager

        local_manager = _get_manager()
        if local_manager is not None:
            return local_manager._active_calls.get(call_id)
    except Exception as exc:
        logger.debug("Failed to find desktop active call %s: %s", call_id, exc)

    return None


def _resolve_desktop_listener_user_id(websocket: WebSocket, security_config, token: str | None) -> str | None:
    """Map trusted desktop-local listen sockets onto the local user."""
    from config.settings import settings
    from core.user_context import get_current_or_device_user_id

    if str(getattr(settings, "app_surface", "desktop")).lower() == "cloud":
        return None

    client_host = websocket.client.host if websocket.client else None
    if not _is_loopback_client_host(client_host):
        return None

    api_key = _ws_query_param(websocket, "api_key")
    if api_key and security_config.auth_api_key and hmac.compare_digest(api_key, security_config.auth_api_key):
        return get_current_or_device_user_id()

    if not getattr(security_config, "auth_enabled", True) or not getattr(
        security_config, "websocket_auth_enabled", True
    ):
        return get_current_or_device_user_id()

    if token or _ws_query_param(websocket, "token"):
        return get_current_or_device_user_id()

    return None


async def _resolve_listener_user_id(websocket: WebSocket, security_config, token: str | None) -> str | None:
    session_context = await get_websocket_session_context(websocket)
    if session_context is not None:
        return session_context.user_id
    return _resolve_desktop_listener_user_id(websocket, security_config, token)


async def _reject_call_not_found(websocket: WebSocket, call_id: str) -> None:
    await websocket.send_text(
        json.dumps(
            {
                "type": "error",
                "message": "Call not found or not active: %s" % call_id,
            }
        )
    )
    # ws-established-close: only called from ws_call_listen() after its own websocket.accept().
    await websocket.close(1008)


async def ws_call_listen(websocket: WebSocket, call_id: str) -> None:
    """WebSocket endpoint for listening in on an active call.

    Registers the WebSocket as a listener on both inbound and outbound
    audio tee processors. Handles takeover/release commands.

    Mount this on the FastAPI app:
        app.add_api_websocket_route("/ws/call-listen/{call_id}", ws_call_listen)
    """
    client_host = websocket.client.host if websocket.client else None
    if not check_websocket_origin(websocket, client_host):
        await reject_websocket(websocket, code=1008, reason="Origin not allowed")
        return

    from ui.security.auth import AuthenticationPlugin
    from ui.security.config import get_security_config

    security_config = get_security_config()
    auth_plugin = AuthenticationPlugin(security_config)
    token = None
    if auth_plugin.is_enabled() and security_config.websocket_auth_enabled:
        token = websocket.headers.get(getattr(auth_plugin, "token_header_name", "X-Auth-Token"))
        if not await auth_plugin.verify_websocket(websocket, token):
            await reject_websocket(websocket, code=1008, reason="Authentication required")
            return

    # #1822: the session-cookie branch of verify_websocket() above binds the
    # ambient current_user_id ContextVar (ui/security/auth.py
    # _attach_desktop_local_authenticated_session /
    # _attach_gotrue_authenticated_session) and captures the reset Token onto
    # websocket.state.current_user_context_token. This socket has no ASGI
    # middleware teardown of its own (AuthMiddleware no-ops for non-http
    # scope), so this handler owns unwinding it -- _reset_user_context_token()
    # below is called on every exit path (record-not-found, ownership
    # violation, tee-unavailable, and the normal disconnect/error finally).
    user_context_token = getattr(getattr(websocket, "state", None), "current_user_context_token", None)

    def _reset_user_context_token() -> None:
        if user_context_token is not None:
            from core.user_context import reset_current_user_id

            websocket.state.current_user_context_token = None
            reset_current_user_id(user_context_token)

    await websocket.accept()
    logger.info("Call listen WS connected: call_id=%s", call_id)

    record = _find_active_call(call_id)
    if record is None:
        await _reject_call_not_found(websocket, call_id)
        _reset_user_context_token()
        return

    listener_user_id = await _resolve_listener_user_id(websocket, security_config, token)
    record_user_id = str(getattr(record, "user_id", "") or "")
    if not listener_user_id or record_user_id != listener_user_id:
        logger.warning(
            "Call listen ownership violation: user=%s attempted access to call_id=%s owner=%s",
            listener_user_id or "<unknown>",
            call_id,
            record_user_id or "<unset>",
        )
        await _reject_call_not_found(websocket, call_id)
        _reset_user_context_token()
        return

    inbound_tee = getattr(record, "_inbound_tee", None)
    outbound_tee = getattr(record, "_outbound_tee", None)

    if inbound_tee is None or outbound_tee is None:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "error",
                    "message": "Listen-in not available for this call",
                }
            )
        )
        await websocket.close(1008)
        _reset_user_context_token()
        return

    # Register as listener on both tees
    await inbound_tee.add_listener(websocket)
    await outbound_tee.add_listener(websocket)

    event_bus = None
    transcript_subscription = None
    transcript_sender_task = None
    transcript_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def transcript_sender() -> None:
        while True:
            payload = await transcript_queue.get()
            if payload is None:
                return
            try:
                await websocket.send_text(json.dumps(payload))
            except WebSocketDisconnect:
                return
            except Exception as exc:
                logger.warning("Call listen transcript WS send failed: %s", exc)
                return

    def transcript_handler(event: CallTranscriptDelta) -> None:
        if event.call_id != call_id:
            return
        payload = {
            "type": "transcript",
            "role": event.role,
            "text": event.text,
            "partial": event.partial,
            "ts": event.ts,
        }
        try:
            loop.call_soon_threadsafe(transcript_queue.put_nowait, payload)
        except Exception as exc:
            logger.debug("Call listen transcript queue unavailable: %s", exc)

    try:
        event_bus = get_event_bus()
        if event_bus:
            transcript_subscription = event_bus.subscribe(CallTranscriptDelta, transcript_handler)
            # mt-ok: handler scope already verified listener_user_id == record.user_id above
            transcript_sender_task = asyncio.create_task(transcript_sender())
    except Exception as exc:
        logger.warning("Call listen transcript subscription failed: %s", exc)

    # Notify UI
    try:
        if event_bus:
            event_bus.publish(CallListenerJoined(call_id=call_id))
    except Exception as exc:  # noqa: BLE001, RUF100 - notify-only; must never break the call
        logger.warning("Failed to publish CallListenerJoined for call %s: %s", call_id, exc)

    await websocket.send_text(
        json.dumps(
            {
                "type": "call_status",
                "status": "listening",
                "call_id": call_id,
            }
        )
    )

    try:
        while True:
            message = await websocket.receive()

            if "text" in message:
                try:
                    data = json.loads(message["text"])
                    msg_type = data.get("type", "")

                    if msg_type == "takeover":
                        await _handle_takeover(record, websocket)

                    elif msg_type == "release":
                        await _handle_release(record, websocket)

                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Call listen WS received malformed control frame for call %s: %s",
                        call_id,
                        exc,
                    )

            elif "bytes" in message:
                # Takeover mode: browser mic audio → output transport
                if getattr(inbound_tee, "takeover_active", False):
                    await _inject_takeover_audio(record, message["bytes"])

    except WebSocketDisconnect:
        logger.info("Call listen WS disconnected: call_id=%s", call_id)
    except Exception as exc:
        logger.warning("Call listen WS error: %s", exc)
    finally:
        if event_bus and transcript_subscription:
            _unsubscribe_event(event_bus, transcript_subscription)
        if transcript_sender_task:
            transcript_sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await transcript_sender_task

        await inbound_tee.remove_listener(websocket)
        await outbound_tee.remove_listener(websocket)

        # Release takeover if active
        if getattr(inbound_tee, "takeover_active", False):
            await _handle_release(record, websocket)

        _reset_user_context_token()


async def _handle_takeover(record, websocket: WebSocket) -> None:
    """Switch to takeover mode — pause AI pipeline, accept browser audio."""
    inbound_tee = getattr(record, "_inbound_tee", None)
    if inbound_tee is None:
        return

    inbound_tee.takeover_active = True
    outbound_tee = getattr(record, "_outbound_tee", None)
    if outbound_tee:
        outbound_tee.takeover_active = True

    # The takeover_active flag on the tee blocks AI processing during takeover.
    task = getattr(record, "_pipeline_task", None)
    if task:
        logger.info("Takeover started for call %s", record.call_id)

    try:
        bus = get_event_bus()
        if bus:
            bus.publish(CallTakeoverStarted(call_id=record.call_id))
    except Exception as exc:  # noqa: BLE001, RUF100 - notify-only; must never break the call
        logger.warning("Failed to publish CallTakeoverStarted for call %s: %s", record.call_id, exc)

    await websocket.send_text(
        json.dumps(
            {
                "type": "call_status",
                "status": "takeover",
                "call_id": record.call_id,
            }
        )
    )


async def _handle_release(record, websocket: WebSocket) -> None:
    """Release takeover — resume AI pipeline."""
    inbound_tee = getattr(record, "_inbound_tee", None)
    if inbound_tee:
        inbound_tee.takeover_active = False

    outbound_tee = getattr(record, "_outbound_tee", None)
    if outbound_tee:
        outbound_tee.takeover_active = False

    logger.info("Takeover released for call %s", record.call_id)

    try:
        bus = get_event_bus()
        if bus:
            bus.publish(CallTakeoverReleased(call_id=record.call_id))
    except Exception as exc:  # noqa: BLE001, RUF100 - notify-only; must never break the call
        logger.warning("Failed to publish CallTakeoverReleased for call %s: %s", record.call_id, exc)

    await websocket.send_text(
        json.dumps(
            {
                "type": "call_status",
                "status": "listening",
                "call_id": record.call_id,
            }
        )
    )


async def _inject_takeover_audio(record, audio_bytes: bytes) -> None:
    """Inject browser mic audio into the output transport.

    Wraps raw PCM bytes as OutputAudioRawFrame and pushes to
    the transport output, bypassing the paused AI pipeline.
    """
    try:
        from pipecat.frames.frames import OutputAudioRawFrame

        frame = OutputAudioRawFrame(audio=audio_bytes, sample_rate=16000, num_channels=1)

        # Push to the pipeline task's output
        task = getattr(record, "_pipeline_task", None)
        if task:
            await task.queue_frame(frame)
    except Exception as exc:
        logger.debug("Takeover audio injection failed: %s", exc)
