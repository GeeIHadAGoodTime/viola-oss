"""
Home Assistant Capability Provider.

First concrete implementation of CapabilityProvider. Connects to a Home
Assistant instance via its REST API, discovers devices, and maps them to
the normalized SmartDevice representation.

Authentication is via Long-Lived Access Token stored in SecureSettingsManager.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from core.platform import get_data_dir
from services.capability_registry import HealthStatus, ProviderType

from .base import SmartDevice

logger = get_logger(__name__)

# File where the normalized device map is cached
_DEVICE_MAP_PATH = get_data_dir() / "smart_home_devices.json"

# HA entity domain → SmartDevice device_type
_ENTITY_TYPE_MAP: dict[str, str] = {
    "light": "light",
    "climate": "climate",
    "lock": "lock",
    "sensor": "sensor",
    "binary_sensor": "sensor",
    "switch": "switch",
    "cover": "cover",
    "media_player": "media_player",
    "fan": "climate",
    "vacuum": "vacuum",
}


class HomeAssistantProvider:
    """Home Assistant capability provider.

    Implements the CapabilityProvider protocol for Home Assistant.
    """

    def __init__(self, url: str = "", token: str = "") -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._devices: list[SmartDevice] = []

    @property
    def provider_id(self) -> str:
        return "home_assistant"

    @property
    def domain_id(self) -> str:
        return "smart_home"

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.MCP

    async def check_health(self) -> HealthStatus:
        """Ping the HA REST API to verify reachability."""
        if not self._url or not self._token:
            logger.debug("HA provider: no URL or token configured")
            return HealthStatus.ERROR

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
                response = await client.get(
                    "%s/api/" % self._url,
                    headers=self._auth_headers(),
                )
                if response.status_code == 200:
                    return HealthStatus.HEALTHY
                if response.status_code == 401:
                    logger.warning("HA health check: authentication failed (401)")
                    return HealthStatus.ERROR
                logger.warning("HA health check: unexpected status %d", response.status_code)
                return HealthStatus.DEGRADED
        except httpx.TimeoutException:
            logger.warning("HA health check: timeout")
            return HealthStatus.ERROR
        except httpx.ConnectError:
            logger.warning("HA health check: connection refused")
            return HealthStatus.ERROR
        except Exception:
            logger.warning("HA health check: unexpected error", exc_info=True)
            return HealthStatus.ERROR

    def get_tools(self) -> list[str]:
        """Return MCP tool names for HA integration."""
        return [
            "homeassistant__call_service",
            "homeassistant__get_state",
            "homeassistant__get_states",
            "homeassistant__fire_event",
        ]

    def get_setup_guide(self) -> dict[str, Any]:
        """Return setup instructions for Home Assistant."""
        return {
            "orthodox_path": ("Connect to your Home Assistant instance with a Long-Lived Access Token"),
            "auto_setup_possible": True,
            "requirements": [
                "A running Home Assistant instance on your network",
                "A Long-Lived Access Token (create in HA Profile > Security)",
            ],
            "estimated_effort": "5 minutes",
            "steps": [
                "Go to your Home Assistant profile page",
                "Scroll to Long-Lived Access Tokens",
                "Create a new token and copy it",
                "Paste the token when prompted by Viola",
            ],
        }

    async def auto_setup(self, params: dict[str, Any]) -> bool:
        """Attempt automatic setup with provided URL and token.

        Args:
            params: Must contain 'url' and 'token' keys.

        Returns:
            True if connection was successful.
        """
        url = params.get("url", "")
        token = params.get("token", "")

        if not url or not token:
            logger.warning("HA auto_setup: missing url or token")
            return False

        self._url = str(url).rstrip("/")
        self._token = str(token)

        health = await self.check_health()
        if health != HealthStatus.HEALTHY:
            logger.warning("HA auto_setup: health check failed (%s)", health)
            return False

        # Discover devices on successful connection
        try:
            await self.discover_devices()
        except Exception:
            logger.warning("HA auto_setup: device discovery failed", exc_info=True)
            # Connection works but discovery failed — still count as success
            pass

        logger.info("HA auto_setup: connected to %s", self._url)
        return True

    async def discover_devices(self) -> list[SmartDevice]:
        """Enumerate HA entities and map them to SmartDevice objects.

        Calls ``/api/states`` to get all entity states, then groups by
        area/room. Results are cached to ``data/smart_home_devices.json``.
        """
        if not self._url or not self._token:
            return []

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
                response = await client.get(
                    "%s/api/states" % self._url,
                    headers=self._auth_headers(),
                )
                response.raise_for_status()
                states = response.json()
        except Exception:
            logger.warning("HA device discovery: failed to fetch states", exc_info=True)
            return []

        devices: list[SmartDevice] = []
        for entity in states:
            if not isinstance(entity, dict):
                continue

            entity_id = entity.get("entity_id", "")
            if not entity_id or "." not in entity_id:
                continue

            entity_domain = entity_id.split(".")[0]
            device_type = _ENTITY_TYPE_MAP.get(entity_domain)
            if not device_type:
                continue

            attrs = entity.get("attributes", {})
            friendly_name = attrs.get("friendly_name", entity_id)

            # Extract room from area or entity_id
            room = self._extract_room(entity_id, attrs)

            # Build capabilities list based on attributes
            capabilities = self._extract_capabilities(entity_domain, attrs)

            device = SmartDevice(
                device_id="ha_%s" % entity_id.replace(".", "_"),
                device_type=device_type,
                display_name=str(friendly_name),
                room=room,
                aliases=self._build_aliases(str(friendly_name), room),
                capabilities=capabilities,
                provider="home_assistant",
                provider_entity_id=entity_id,
                state={"state": entity.get("state")},
            )
            devices.append(device)

        self._devices = devices
        self._save_device_map(devices)

        logger.info("HA discovery: found %d devices", len(devices))
        return devices

    def _auth_headers(self) -> dict[str, str]:
        """Build authorization headers for HA REST API."""
        return {
            "Authorization": "Bearer %s" % self._token,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_room(entity_id: str, attrs: dict[str, Any]) -> str:
        """Extract room name from entity attributes or ID."""
        # Prefer area_id if present (HA 2024.x+)
        area = attrs.get("area_id") or attrs.get("area")
        if area:
            return str(area).replace("_", " ")

        # Fall back to parsing entity_id: "light.living_room_overhead"
        parts = entity_id.split(".", 1)
        if len(parts) == 2:
            name_parts = parts[1].split("_")
            # Heuristic: room is usually the first 1-2 words
            if len(name_parts) >= 2:
                return " ".join(name_parts[:2]).replace("_", " ")

        return "unknown"

    @staticmethod
    def _extract_capabilities(domain: str, attrs: dict[str, Any]) -> list[str]:
        """Extract capabilities from entity domain and attributes."""
        caps: list[str] = []

        if domain == "light":
            if "brightness" in attrs:
                caps.append("brightness")
            if "color_temp" in attrs or "color_temp_kelvin" in attrs:
                caps.append("color_temp")
            if "rgb_color" in attrs or "hs_color" in attrs:
                caps.append("color")
            if not caps:
                caps.append("on_off")

        elif domain == "climate":
            if "temperature" in attrs:
                caps.append("temperature")
            if "fan_mode" in attrs:
                caps.append("fan_mode")
            if "hvac_modes" in attrs:
                caps.append("hvac_mode")

        elif domain == "lock":
            caps.extend(["lock", "unlock"])

        elif domain == "cover":
            caps.extend(["open", "close"])
            if "current_position" in attrs:
                caps.append("position")

        elif domain in ("sensor", "binary_sensor"):
            device_class = attrs.get("device_class", "")
            if device_class:
                caps.append(str(device_class))
            else:
                caps.append("state")

        elif domain == "switch":
            caps.append("on_off")

        elif domain == "vacuum":
            caps.extend(["start", "stop", "return_to_base", "locate", "status"])
            if "battery_level" in attrs:
                caps.append("battery_level")

        return caps

    @staticmethod
    def _build_aliases(friendly_name: str, room: str) -> list[str]:
        """Build natural language aliases for a device."""
        aliases: list[str] = []
        name_lower = friendly_name.lower()

        # Remove room name from the friendly name to get the device part
        room_lower = room.lower()
        device_part = name_lower.replace(room_lower, "").strip()
        if device_part and device_part != name_lower:
            aliases.append(device_part)

        # Add the full name lowered
        if name_lower not in aliases:
            aliases.append(name_lower)

        return aliases

    @staticmethod
    def _save_device_map(devices: list[SmartDevice]) -> None:
        """Save the normalized device map to disk."""
        try:
            _DEVICE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)

            # Group by room
            rooms: dict[str, list[dict[str, Any]]] = {}
            for d in devices:
                room_key = d.room or "unknown"
                if room_key not in rooms:
                    rooms[room_key] = []
                rooms[room_key].append(
                    {
                        "device_id": d.device_id,
                        "type": d.device_type,
                        "name": d.display_name,
                        "aliases": d.aliases,
                        "capabilities": d.capabilities,
                        "provider": d.provider,
                        "provider_entity_id": d.provider_entity_id,
                    }
                )

            data = [{"room": room, "devices": devs} for room, devs in sorted(rooms.items())]

            _DEVICE_MAP_PATH.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
            logger.debug("Saved device map with %d devices", len(devices))
        except Exception:
            logger.warning("Failed to save device map", exc_info=True)
