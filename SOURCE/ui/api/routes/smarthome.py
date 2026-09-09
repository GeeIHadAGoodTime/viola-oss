"""Smart-home discovery API routes.

Exposes the ecosystem-neutral LAN discovery scan (`services.network_discovery`)
over HTTP so the React UI and voice command layer can request a one-shot
scan of the user's network.  Finds Home Assistant, Hubitat, Philips Hue,
MQTT brokers, Sonos, HomeKit bridges, SmartThings, and Spotify Connect
devices via mDNS + targeted port scans.  Opt-in via the
``network_discovery_enabled`` setting; returns an empty list when off.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth

logger = get_logger(__name__)


class SmartHomeTestConnectionRequest(BaseModel):
    home_assistant_url: str | None = None
    home_assistant_token: str | None = None


def register_smarthome_routes(context: ApiContext) -> None:
    """Register smart-home discovery routes on the feature router."""

    router = context.router

    @router.post(
        "/v1/smarthome/discover",
        tags=["smarthome"],
        dependencies=[Depends(require_auth)],
    )
    async def discover_smart_home() -> dict[str, Any]:
        """Run a one-shot LAN scan for smart-home hubs/devices.

        Returns ``{"enabled": bool, "devices": [...]}``.  When
        ``network_discovery_enabled`` is off, ``enabled`` is False and
        ``devices`` is an empty list (no scan performed).
        """

        from services.network_discovery import scan_network

        enabled = _discovery_is_enabled()
        if not enabled:
            return success_response(
                {
                    "enabled": False,
                    "scan_performed": False,
                    "devices": [],
                }
            )

        services = await scan_network()
        devices = [
            {
                "service_type": svc.service_type,
                "display_name": svc.display_name,
                "ip": svc.ip,
                "port": svc.port,
                "metadata": svc.metadata or {},
            }
            for svc in services
        ]
        return success_response(
            {
                "enabled": True,
                "scan_performed": True,
                "devices": devices,
            }
        )

    @router.post(
        "/v1/smarthome/test-connection",
        tags=["smarthome"],
        dependencies=[Depends(require_auth)],
    )
    async def test_smart_home_connection(body: SmartHomeTestConnectionRequest) -> dict[str, Any]:
        """Probe smart-home credentials without mutating the cached client."""

        from services.smart_home.home_assistant import HomeAssistantClient
        from ui.settings_manager import get_settings_manager

        settings = get_settings_manager()
        base_url = (
            body.home_assistant_url if body.home_assistant_url is not None else settings.get("home_assistant_url", "")
        ) or ""
        token = (
            body.home_assistant_token
            if body.home_assistant_token is not None
            else settings.get("home_assistant_token", "")
        ) or ""
        base_url = str(base_url).strip().rstrip("/")
        token = str(token).strip()

        if not base_url or not token:
            return failure_response(
                "missing_credentials",
                "Smart-home URL and token are required to test the connection.",
                data={"connected": False, "message": "Missing URL or token."},
            )

        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return failure_response(
                "invalid_url",
                "Smart-home URL must be an http or https URL.",
                data={"connected": False, "message": "Invalid URL.", "details": {"error_code": "invalid_url"}},
            )

        client = HomeAssistantClient(base_url=base_url, token=token)
        try:
            async with httpx.AsyncClient(timeout=5.0) as http_client:
                response = await http_client.get(
                    f"{base_url}/api/",
                    headers=client._headers(),
                )
            if response.status_code == 200:
                message = "Connected."
                try:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("message"):
                        message = str(payload["message"])
                except Exception:
                    pass
                # Detect the real hub identity instead of showing a generic
                # placeholder. Home Assistant exposes the home's name and
                # version at /api/config; surface them so the UI can display
                # the actual connected hub ("Home Assistant - <home name>").
                hub_name = None
                hub_version = None
                try:
                    async with httpx.AsyncClient(timeout=5.0) as cfg_client:
                        cfg = await cfg_client.get(
                            f"{base_url}/api/config",
                            headers=client._headers(),
                        )
                    if cfg.status_code == 200:
                        cfg_payload = cfg.json()
                        if isinstance(cfg_payload, dict):
                            location = cfg_payload.get("location_name")
                            version = cfg_payload.get("version")
                            hub_name = str(location).strip() if location else None
                            hub_version = str(version).strip() if version else None
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    # Identity is best-effort enrichment; a bare "Connected"
                    # result must still succeed if /api/config is unavailable.
                    logger.debug("Home Assistant /api/config identity probe failed: %s", exc)
                return success_response(
                    {
                        "connected": True,
                        "message": message,
                        "hub_type": "home_assistant",
                        "hub_name": hub_name,
                        "hub_version": hub_version,
                        "details": {"status_code": response.status_code},
                    }
                )
            return success_response(
                {
                    "connected": False,
                    "message": "Connection test returned HTTP %d." % response.status_code,
                    "details": {"status_code": response.status_code, "error_code": "http_error"},
                }
            )
        except httpx.TimeoutException:
            return success_response(
                {
                    "connected": False,
                    "message": "Connection timed out.",
                    "details": {"error_code": "timeout"},
                }
            )
        except httpx.RequestError as exc:
            return success_response(
                {
                    "connected": False,
                    "message": "Connection failed.",
                    "details": {"error_code": "request_error", "error": str(exc)},
                }
            )


def _discovery_is_enabled() -> bool:
    """Surface the user toggle separately so the UI can show a helpful
    "discovery is off — enable in Settings" prompt when the scan returned
    an empty list but the feature wasn't disabled (i.e. truly no devices)."""

    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().get("network_discovery_enabled", False))
    except Exception:
        return False
