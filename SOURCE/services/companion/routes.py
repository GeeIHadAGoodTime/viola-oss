from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from auth.dependencies import get_current_user
from auth.ip_utils import extract_client_ip
from core.db_backend import reset_db_user_id, set_db_user_id
from fastapi import APIRouter, Depends, HTTPException, WebSocket
from ui.api.routes.websocket_auth import verify_websocket_auth
from ui.core.security import check_websocket_origin, reject_websocket

from .bridge import CompanionUnsupportedProtocolError, get_companion_bridge
from .registry import get_companion_device_registry
from .security import (
    MAX_COMPANION_CAPABILITIES_JSON_BYTES,
    MAX_COMPANION_COMMAND_PAYLOAD_JSON_BYTES,
    CompanionOfflineError,
    CompanionPayloadTooLarge,
    CompanionSecurityError,
    validate_companion_json_payload,
)
from .ws_auth_contract import extract_companion_ws_device_token

router = APIRouter(tags=["companion"])


class CompanionRegisterRequest(BaseModel):
    device_name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="unknown", max_length=80)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class CompanionCommandRequest(BaseModel):
    command_type: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, ge=0.1, le=300.0)
    max_attempts: int = Field(default=1, ge=1, le=5)


def _extract_ws_device_token(websocket: WebSocket) -> str | None:
    # The device token rides its OWN header (X-Companion-Device-Token, or the
    # legacy x-companion-token alias) -- NEVER the Authorization bearer, which
    # carries the GoTrue user-session JWT (verify_websocket_auth reads it), and
    # NEVER the URL query string (device tokens are long-lived credentials that
    # must not land in access logs). Single source of truth is ws_auth_contract
    # so the client and server can never drift apart again.
    return extract_companion_ws_device_token(websocket.headers.get)


@router.post("/api/v1/companion/register")
async def register_companion(
    body: CompanionRegisterRequest,
    user=Depends(get_current_user),
) -> dict[str, Any]:
    try:
        capabilities = validate_companion_json_payload(
            body.capabilities,
            field="companion capabilities",
            max_bytes=MAX_COMPANION_CAPABILITIES_JSON_BYTES,
        )
    except CompanionPayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    registry = get_companion_device_registry()
    device, token = await registry.register_device(
        user_id=user.id,
        device_name=body.device_name,
        platform=body.platform,
        capabilities=capabilities,
    )
    return {
        "device": device.to_public_dict(online=False, latency_ms=None),
        "device_token": token,
    }


@router.get("/api/v1/companion/devices")
async def list_companion_devices(user=Depends(get_current_user)) -> dict[str, Any]:
    devices = await get_companion_bridge().list_devices(user_id=user.id)
    return {"devices": devices}


@router.delete("/api/v1/companion/{device_id}")
async def remove_companion_device(device_id: str, user=Depends(get_current_user)) -> dict[str, Any]:
    registry = get_companion_device_registry()
    removed = await registry.revoke_device(user_id=user.id, device_id=device_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Companion device not found")
    await get_companion_bridge().close_device(device_id)
    return {"ok": True, "device_id": device_id}


@router.post("/api/v1/companion/{device_id}/command")
async def send_companion_command(
    device_id: str,
    body: CompanionCommandRequest,
    user=Depends(get_current_user),
) -> dict[str, Any]:
    try:
        payload = validate_companion_json_payload(
            body.payload,
            field="companion command payload",
            max_bytes=MAX_COMPANION_COMMAND_PAYLOAD_JSON_BYTES,
        )
    except CompanionPayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    bridge = get_companion_bridge()
    try:
        command = await bridge.send_command(
            user_id=user.id,
            device_id=device_id,
            message_type=body.command_type,
            payload=payload,
            timeout_seconds=body.timeout_seconds,
            max_attempts=body.max_attempts,
        )
    except CompanionOfflineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CompanionSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except CompanionUnsupportedProtocolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "request_id": command.request_id,
        "status": command.status,
        "queued": True,
    }


@router.get("/api/v1/companion/{device_id}/result/{request_id}")
async def get_companion_command_result(
    device_id: str,
    request_id: str,
    user=Depends(get_current_user),
) -> dict[str, Any]:
    command = await get_companion_bridge().get_command_result(
        user_id=user.id,
        device_id=device_id,
        request_id=request_id,
    )
    if command is None:
        raise HTTPException(status_code=404, detail="Companion command not found")
    return command.to_dict()


@router.websocket("/ws/companion/{device_id}")
async def companion_websocket(websocket: WebSocket, device_id: str) -> None:
    client_host = extract_client_ip(websocket)
    if not check_websocket_origin(websocket, client_host=client_host):
        await reject_websocket(websocket, code=1008, reason="Origin not allowed")
        return

    auth_result = await verify_websocket_auth(websocket, required=False)
    if auth_result is None:
        await reject_websocket(websocket, code=4401, reason="user_session_required")
        return
    user, _session = auth_result

    device_token = _extract_ws_device_token(websocket)
    if not device_token:
        await reject_websocket(websocket, code=4401, reason="device_token_required")
        return

    registry = get_companion_device_registry()
    bridge = get_companion_bridge()
    db_user_token = set_db_user_id(user.id)
    try:
        try:
            await bridge.assert_ws_invalid_token_limit(client_ip=client_host or "unknown", device_id=device_id)
        except CompanionSecurityError:
            await registry.append_audit_event(
                user_id=user.id,
                device_id=device_id,
                action="device_auth_rate_limited",
                details={"reason": "invalid_device_token_rate_limited", "client_ip": client_host or "unknown"},
            )
            await reject_websocket(websocket, code=4403, reason="invalid_device_token_rate_limited")
            return

        device = await registry.verify_device_for_user(
            user_id=user.id,
            device_id=device_id,
            token=device_token,
        )
        if device is None:
            await registry.append_audit_event(
                user_id=user.id,
                device_id=device_id,
                action="device_auth_failed",
                details={"reason": "invalid_device_token", "client_ip": client_host or "unknown"},
            )
            await reject_websocket(websocket, code=4403, reason="invalid_device_token")
            return
        await bridge.handle_websocket(websocket, device=device)
    finally:
        reset_db_user_id(db_user_token)
