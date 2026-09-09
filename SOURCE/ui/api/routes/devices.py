"""
Device Discovery API Routes.

FastAPI router for discovering and connecting to Viola devices on the network.

Endpoints:
    GET  /api/v1/devices/discovered          - List discovered devices
    POST /api/v1/devices/{device_id}/connect  - Connect a discovered device as a room
    POST /api/v1/devices/{device_id}/disconnect - Disconnect a device (remove from rooms)
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, Request
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth

logger = get_logger(__name__)

# Local-IP detection for same-machine host normalization
_LOCAL_IP: str | None = None


def _get_local_ip() -> str:
    """Return the LAN IP of this machine (cached)."""
    global _LOCAL_IP
    if _LOCAL_IP is not None:
        return _LOCAL_IP
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            _LOCAL_IP = str(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:
        _LOCAL_IP = "127.0.0.1"
    return _LOCAL_IP


def _normalize_host_for_connection(discovered_host: str) -> str:
    """Normalize a discovered host for HTTP connections.

    If the discovered host is the same machine (matches our LAN IP or is
    already a loopback address), return ``127.0.0.1`` so we connect via
    loopback -- which always works regardless of the server's bind address.
    """
    if discovered_host in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"
    if discovered_host == _get_local_ip():
        return "127.0.0.1"
    return discovered_host


router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


class ConnectDeviceRequest(BaseModel):
    """Request body for connecting a discovered device."""

    room_name: str | None = Field(
        None,
        min_length=1,
        max_length=100,
        description="Display name for the room (defaults to device hostname)",
    )


@router.get(
    "/discovered",
    dependencies=[Depends(require_auth)],
)
async def list_discovered_devices(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> Any:
    """
    List Viola devices discovered on the local network.

    Returns devices found by the DiscoveryService via mDNS/UDP.
    Each device includes connection status (whether already registered as a room).
    """
    try:
        from services.multiroom.discovery import get_discovery_service

        discovery = get_discovery_service()
        if not discovery.is_running():
            return success_response({"devices": [], "count": 0, "scanning": False})

        discovered = discovery.get_discovered_devices()

        # Check which devices are already registered as rooms
        registered_device_ids: set[str] = set()
        try:
            from core.room_registry import get_room_registry

            registry = get_room_registry(user_id=user_id)
            for room in registry.get_rooms():
                # Room IDs for connected devices match device_id
                registered_device_ids.add(room.room_id)
        except Exception as exc:
            logger.debug("Failed to check room registry: %s", exc)

        devices = []
        for device in discovered:
            devices.append(
                {
                    "device_id": device.device_id,
                    "name": device.room_name,
                    "host": device.host,
                    "port": device.port,
                    "last_seen": device.last_seen,
                    "api_version": device.api_version,
                    "capabilities": device.capabilities,
                    "is_connected": device.device_id in registered_device_ids,
                }
            )

        logger.debug("Found %d discovered devices", len(devices))
        return success_response(
            {
                "devices": devices,
                "count": len(devices),
                "scanning": True,
            }
        )
    except ImportError:
        logger.debug("DiscoveryService not available")
        return success_response({"devices": [], "count": 0, "scanning": False})
    except Exception:
        logger.exception("Failed to list discovered devices")
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "discovery_failed", "Couldn't scan for devices. Make sure your devices are online and try again."
            ),
        )


@router.post(
    "/{device_id}/connect",
    dependencies=[Depends(require_auth)],
)
async def connect_device(
    device_id: str,
    body: ConnectDeviceRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> Any:
    """
    Connect a discovered device by registering it as a room.

    Takes a discovered device_id and registers it in the room registry
    via StateHub's AddRoom command.
    """
    try:
        from core.room_registry import get_room_registry
        from services.multiroom.discovery import get_discovery_service

        # Verify device exists in discovery
        discovery = get_discovery_service()
        device = discovery.get_device(device_id)
        if device is None:
            return JSONResponse(
                status_code=404,
                content=failure_response(
                    "device_not_found",
                    f"Device '{device_id}' not found in discovered devices",
                ),
            )

        # Check if already connected
        registry = get_room_registry(user_id=user_id)
        if registry.has_room(device_id):
            return JSONResponse(
                status_code=409,
                content=failure_response(
                    "already_connected",
                    f"Device '{device_id}' is already connected as a room",
                ),
            )

        # Register as a room
        room_name = body.room_name or device.room_name
        registry.register_remote_room(room_id=device_id, room_name=room_name)

        # Register an HTTP client for the remote room.
        # Normalize host: if the discovered device is on the same machine
        # (same IP as us or loopback), use 127.0.0.1 since the remote
        # instance may be bound to 127.0.0.1 only.
        try:
            from services.multiroom.room_connections import get_room_connection_manager

            connect_host = _normalize_host_for_connection(device.host)
            conn_mgr = get_room_connection_manager()
            conn_mgr.register_connection(device_id, connect_host, device.port)
        except Exception as exc:
            logger.warning("Failed to register room connection for %s: %s", device_id, exc)

        # Start periodic state synchronization for the remote room
        try:
            from services.multiroom.state_sync import get_state_sync_manager

            sync_mgr = get_state_sync_manager()
            sync_mgr.start_sync(device_id)
        except Exception as exc:
            logger.debug("State sync start skipped: %s", exc)

        logger.info("Device connected as room", device_id=device_id, room_name=room_name)
        return success_response(
            {
                "room_id": device_id,
                "room_name": room_name,
                "device_id": device_id,
                "host": device.host,
                "port": device.port,
            }
        )
    except ImportError as exc:
        logger.debug("Required service not available: %s", exc)
        return JSONResponse(
            status_code=503,
            content=failure_response("service_unavailable", "Multi-room services not available"),
        )
    except Exception:
        logger.exception("Failed to connect device")
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "connect_failed", "Couldn't connect to the speaker. Check your network and try again."
            ),
        )


@router.post(
    "/{device_id}/disconnect",
    dependencies=[Depends(require_auth)],
)
async def disconnect_device(
    device_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> Any:
    """
    Disconnect a device by removing it from the room registry.

    Uses StateHub's RemoveRoom command.
    """
    try:
        from core.room_registry import get_room_registry

        registry = get_room_registry(user_id=user_id)

        if not registry.has_room(device_id):
            return JSONResponse(
                status_code=404,
                content=failure_response(
                    "room_not_found",
                    f"Device '{device_id}' is not connected as a room",
                ),
            )

        removed = registry.unregister_room(device_id)
        if not removed:
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "disconnect_failed",
                    "Cannot disconnect this device (may be the primary/local device)",
                ),
            )

        # Remove the HTTP client for the remote room
        try:
            from services.multiroom.room_connections import get_room_connection_manager

            conn_mgr = get_room_connection_manager()
            conn_mgr.remove_connection(device_id)
        except Exception as exc:
            logger.warning("Failed to remove room connection for %s: %s", device_id, exc)

        # Stop periodic state synchronization for the remote room
        try:
            from services.multiroom.state_sync import get_state_sync_manager

            sync_mgr = get_state_sync_manager()
            sync_mgr.stop_sync(device_id)
        except Exception as exc:
            logger.debug("State sync stop skipped: %s", exc)

        logger.info("Device disconnected", device_id=device_id)
        return success_response({"device_id": device_id, "disconnected": True})
    except Exception:
        logger.exception("Failed to disconnect device")
        return JSONResponse(
            status_code=500,
            content=failure_response("disconnect_failed", "Couldn't disconnect the speaker. Please try again."),
        )


def create_devices_router() -> APIRouter:
    """Factory function for the devices router."""
    return router


__all__ = [
    "ConnectDeviceRequest",
    "create_devices_router",
    "router",
]
