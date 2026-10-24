"""Network information API routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from contracts.api_response import success_response
from fastapi import Depends, HTTPException, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.core.security import is_loopback_request
from utils.speaker_pairing_flow import build_local_address_payload, ensure_lan_access


class LanAccessRequest(BaseModel):
    """Body of ``POST /v1/network/lan-access``."""

    enabled: bool


async def _require_loopback_caller(request: Request) -> None:
    """Only the desktop itself may switch LAN access on or off."""
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Forbidden")


_CONNECT_PAGE_PATH = Path(__file__).resolve().parents[2] / "static" / "connect-page" / "index.html"


def register_network_routes(context: ApiContext) -> None:
    """Register network information routes."""
    router = context.router

    @router.get(
        "/v1/network/local-address",
        tags=["network"],
        dependencies=[Depends(require_auth)],
    )
    async def get_local_address() -> dict:
        """Return the hub's LAN address for spoke connection.

        This stays auth-protected because it exposes the desktop's LAN IP and
        spoke URL. The React Add Room panel must call it through ``apiFetch``.

        When auth is enabled, ``spoke_url`` carries a scoped spoke credential so
        the phone can connect to spoke WebSockets without receiving the desktop
        API key. ``pairing_code`` is the human-friendly word (or base36 fallback) a user
        can type at useviola.com/connect or the bundled /connect page to reach
        this hub without scanning a QR or typing the IP.

        Opening Add Room is the user asking for other devices to reach this
        hub, so this first opens LAN access (``ensure_lan_access``). The link
        and code are only returned when that address is actually being served;
        otherwise ``lan_access`` says why and the panel shows that instead.
        """
        status = await ensure_lan_access(enable=True)
        return success_response(build_local_address_payload(status))

    @router.get(
        "/v1/network/lan-access",
        tags=["network"],
        dependencies=[Depends(require_auth), Depends(_require_loopback_caller)],
    )
    async def get_lan_access() -> dict:
        """Report whether devices on the Wi-Fi can reach this hub (no change)."""
        status = await ensure_lan_access(enable=None)
        return success_response(status.to_dict())

    @router.post(
        "/v1/network/lan-access",
        tags=["network"],
        dependencies=[Depends(require_auth), Depends(_require_loopback_caller)],
    )
    async def set_lan_access(body: LanAccessRequest) -> dict:
        """Turn LAN access on or off from the desktop (Rooms panel, Settings).

        Loopback-only: a device on the network must not be able to open or
        close the hub's LAN listener, even with a paired-speaker credential.
        """
        status = await ensure_lan_access(enable=body.enabled)
        return success_response(status.to_dict())

    @router.get(
        "/v1/network/pair/pending",
        tags=["network"],
        dependencies=[Depends(require_auth)],
    )
    async def get_pending_pairing_codes() -> dict:
        """Return pairing codes awaiting confirmation, for desktop display.

        Auth-protected: only the signed-in desktop hub may read the codes.
        The LAN join flow is device-initiated — a joining browser POSTs
        ``/bootstrap/request`` (which mints a short-lived code) and is shown a
        screen to enter it. The code is the out-of-band secret that gates the
        spoke-credential mint, so it is never returned to the joining device.
        The desktop polls this endpoint and DISPLAYS the active code on its own
        screen so the user can read it to the joining device.
        """
        from ui.security.bootstrap import pending_pairing_codes

        return success_response({"pending": pending_pairing_codes()})

    @router.get(
        "/v1/network/spoke-devices",
        tags=["network"],
        dependencies=[Depends(require_auth)],
    )
    async def list_spoke_devices() -> dict:
        """List the speaker devices paired with this hub.

        One row per paired device: when it was first and last seen, the room it
        serves, and whether it is revoked. No credential material is returned —
        the token itself is never readable back out of the hub.
        """
        import asyncio

        from ui.security.spoke_device_registry import list_devices

        # Worker-thread hop: reading the registry resolves the secret
        # directory, which can harden it (icacls subprocess) on first use —
        # never on the event loop.
        return success_response({"devices": await asyncio.to_thread(list_devices)})

    @router.post(
        "/v1/network/spoke-devices/{device_id}/revoke",
        tags=["network"],
        dependencies=[Depends(require_auth)],
    )
    async def revoke_spoke_device(device_id: str) -> dict:
        """Cut off ONE paired speaker without touching the others.

        Before this, the only way to stop a paired device was rotating the
        signing secret, which unpairs every speaker in the house at once
        (#4434). Revocation is checked on every credential verification, so the
        device is refused the next time it connects.
        """
        import asyncio

        from ui.security.spoke_device_registry import revoke_device

        # Worker-thread hop: writing the registry resolves the secret
        # directory, which can harden it (icacls subprocess) on first use.
        revoked = await asyncio.to_thread(revoke_device, device_id)
        return success_response({"device_id": device_id, "revoked": True, "changed": revoked})

    @router.get("/connect", tags=["network"], include_in_schema=False, response_model=None)
    async def serve_connect_page() -> FileResponse | JSONResponse:
        """Serve the cameraless pairing page at a memorable URL.

        Unauthenticated on purpose: the page is pure client-side HTML that
        decodes the pairing word locally and redirects to the hub's LAN IP.
        No hub state is read or mutated.
        """
        if not _CONNECT_PAGE_PATH.is_file():
            return JSONResponse(status_code=404, content={"error": "connect_page_missing"})
        return FileResponse(_CONNECT_PAGE_PATH, media_type="text/html")
