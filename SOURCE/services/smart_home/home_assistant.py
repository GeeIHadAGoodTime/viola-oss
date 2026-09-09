"""Home Assistant integration -- REST API client with entity caching.

Connects to a Home Assistant instance via its REST API using a
long-lived access token. Provides MCP tools for:

    - smart_home_list   : list entities (optionally filtered by domain)
    - smart_home_control: turn on/off, set temp/brightness/color, etc.
    - smart_home_state  : get current state of a specific entity
    - smart_home_scene  : activate a Home Assistant scene

Configuration in settings.json:
    home_assistant_url   : e.g. "http://homeassistant.local:8123"
    home_assistant_token : long-lived access token from HA profile

Entity list is cached for 5 minutes to avoid excessive HA queries.
All control tools registered as CONFIRM risk level in RISK_MAP.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.logging_config import get_logger

logger = get_logger(__name__)

_LOCK = threading.Lock()
_SINGLETON: HomeAssistantClient | None = None

_CACHE_TTL = 300  # 5 minutes
_REQUEST_TIMEOUT = 15.0
_SUPPORTED_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "climate",
        "sensor",
        "binary_sensor",
        "cover",
        "fan",
        "media_player",
        "lock",
        "vacuum",
        "automation",
        "scene",
        "script",
        "input_boolean",
        "input_number",
        "input_select",
    }
)

_COMMON_ACTIONS = {
    "turn_on": "homeassistant/turn_on",
    "turn_off": "homeassistant/turn_off",
    "toggle": "homeassistant/toggle",
    "set_temperature": "climate/set_temperature",
    "set_hvac_mode": "climate/set_hvac_mode",
    "set_brightness": "light/turn_on",  # brightness is a service_data param
    "set_color": "light/turn_on",  # color is a service_data param
    "set_position": "cover/set_cover_position",
    "open": "cover/open_cover",
    "close": "cover/close_cover",
    "lock": "lock/lock",
    "unlock": "lock/unlock",
    "set_speed": "fan/set_speed",
}


@dataclass
class EntityState:
    """Represents a Home Assistant entity's current state."""

    entity_id: str
    state: str
    domain: str
    friendly_name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    last_changed: str = ""
    last_updated: str = ""


class EntityCache:
    """Time-based cache for Home Assistant entity states."""

    def __init__(self, ttl: float = _CACHE_TTL) -> None:
        self._ttl = ttl
        self._data: list[EntityState] = []
        self._timestamp: float = 0.0
        self._lock = threading.Lock()

    @property
    def is_valid(self) -> bool:
        """Whether the cache is still within its TTL."""
        return bool(self._data) and (time.monotonic() - self._timestamp) < self._ttl

    def get(self) -> list[EntityState]:
        """Return cached entities (empty if expired)."""
        with self._lock:
            if self.is_valid:
                return list(self._data)
            return []

    def set(self, entities: list[EntityState]) -> None:
        """Update the cache."""
        with self._lock:
            self._data = entities
            self._timestamp = time.monotonic()

    def invalidate(self) -> None:
        """Force cache expiry."""
        with self._lock:
            self._timestamp = 0.0


class HomeAssistantUnsafeUrlError(Exception):
    """Raised when a per-request URL revalidation fails (e.g. DNS rebind)."""


class HomeAssistantClient:
    """REST API client for Home Assistant.

    Usage::

        client = get_home_assistant()
        entities = await client.list_entities(domain="light")
        await client.call_service("light.living_room", "turn_on", brightness=200)
        state = await client.get_state("light.living_room")
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        availability_message: str | None = None,
        unavailable_reason: str | None = None,
        pre_request_validator: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self.unavailable_reason = unavailable_reason or availability_message
        self._cache = EntityCache()
        # When set, called with the base URL immediately before every outbound
        # HTTP request. Cloud mode passes a DNS revalidator that keeps a
        # tight TOCTOU window: between revalidation and httpx connect, an
        # attacker controlling DNS would have to flip the answer in
        # milliseconds. Desktop mode leaves this None (LAN access is
        # expected and would otherwise fail the public-address check).
        self._pre_request_validator = pre_request_validator

    @property
    def is_configured(self) -> bool:
        """Whether the HA connection is configured."""
        return bool(self._base_url and self._token)

    def _headers(self) -> dict[str, str]:
        """Build HTTP headers with authorization."""
        return {
            "Authorization": "Bearer %s" % self._token,
            "Content-Type": "application/json",
        }

    async def _revalidate_url(self) -> None:
        """Re-run the cloud URL validator immediately before an outbound request.

        The one-time validation in ``CloudIntegrationsService`` happens when
        the client is constructed; long-lived clients or attacker-controlled
        DNS can rebind the hostname between construction and request. This
        hook narrows the TOCTOU window to milliseconds. Raises
        ``HomeAssistantUnsafeUrlError`` when revalidation fails, which the
        request methods translate into a clean error result so the agent
        falls back gracefully instead of issuing the request.
        """
        validator = self._pre_request_validator
        if validator is None:
            return
        try:
            await validator(self._base_url)
        except Exception as exc:
            raise HomeAssistantUnsafeUrlError(str(exc)) from exc

    # ------------------------------------------------------------------ entities

    async def list_entities(
        self,
        domain: str | None = None,
    ) -> list[EntityState]:
        """List all entities, optionally filtered by domain.

        Uses cached data if available (5-minute TTL).

        Args:
            domain: Filter to a specific domain (e.g., 'light', 'switch').

        Returns:
            List of EntityState objects.
        """
        if not self.is_configured:
            logger.warning("Home Assistant not configured")
            return []

        # Check cache
        cached = self._cache.get()
        if not cached:
            cached = await self._fetch_all_states()
            if cached:
                self._cache.set(cached)

        if domain:
            domain = domain.lower()
            return [e for e in cached if e.domain == domain]
        return cached

    async def _fetch_all_states(self) -> list[EntityState]:
        """Fetch all entity states from Home Assistant."""
        try:
            await self._revalidate_url()
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(
                    "%s/api/states" % self._base_url,
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()

            entities = []
            for item in data:
                entity_id = item.get("entity_id", "")
                domain = entity_id.split(".")[0] if "." in entity_id else ""

                # Only include supported domains
                if domain not in _SUPPORTED_DOMAINS:
                    continue

                attributes = item.get("attributes", {})
                entities.append(
                    EntityState(
                        entity_id=entity_id,
                        state=item.get("state", "unknown"),
                        domain=domain,
                        friendly_name=attributes.get("friendly_name", entity_id),
                        attributes=attributes,
                        last_changed=item.get("last_changed", ""),
                        last_updated=item.get("last_updated", ""),
                    )
                )

            logger.info("HA: fetched %d entities", len(entities))
            return entities
        except HomeAssistantUnsafeUrlError as exc:
            logger.warning("HA outbound blocked by URL revalidation: %s", exc)
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HA API error: %s %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return []
        except Exception as exc:
            logger.warning("HA entity fetch failed: %s", exc)
            return []

    # ------------------------------------------------------------------ state

    async def get_state(self, entity_id: str) -> EntityState | None:
        """Get the current state of a specific entity.

        Bypasses cache for real-time accuracy.
        """
        if not self.is_configured:
            return None

        try:
            await self._revalidate_url()
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(
                    "%s/api/states/%s" % (self._base_url, entity_id),
                    headers=self._headers(),
                )
                response.raise_for_status()
                item = response.json()

            domain = entity_id.split(".")[0] if "." in entity_id else ""
            attributes = item.get("attributes", {})
            return EntityState(
                entity_id=entity_id,
                state=item.get("state", "unknown"),
                domain=domain,
                friendly_name=attributes.get("friendly_name", entity_id),
                attributes=attributes,
                last_changed=item.get("last_changed", ""),
                last_updated=item.get("last_updated", ""),
            )
        except HomeAssistantUnsafeUrlError as exc:
            logger.warning(
                "HA state fetch blocked by URL revalidation for %s: %s",
                entity_id,
                exc,
            )
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug("HA entity not found: %s", entity_id)
            else:
                logger.warning("HA state error for %s: %s", entity_id, exc)
            return None
        except Exception as exc:
            logger.warning("HA state fetch failed for %s: %s", entity_id, exc)
            return None

    # ------------------------------------------------------------------ control

    async def call_service(
        self,
        entity_id: str,
        action: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Call a Home Assistant service on an entity.

        Args:
            entity_id: The entity to control (e.g., 'light.living_room').
            action: The action name (e.g., 'turn_on', 'set_temperature').
            **params: Additional service data parameters.

        Returns:
            Result dict with 'ok' and optional 'state' or 'error'.
        """
        if not self.is_configured:
            return {"ok": False, "error": "Home Assistant not configured"}

        # Resolve action to HA service
        service_path = _COMMON_ACTIONS.get(action)
        if not service_path:
            # Try direct domain/service format
            domain = entity_id.split(".")[0] if "." in entity_id else "homeassistant"
            service_path = "%s/%s" % (domain, action)

        # Build service data
        service_data: dict[str, Any] = {"entity_id": entity_id}

        # Map common parameters to HA service_data
        if "temperature" in params:
            service_data["temperature"] = params["temperature"]
        if "brightness" in params:
            # HA uses 0-255, user might pass 0-100
            brightness = params["brightness"]
            if isinstance(brightness, (int, float)) and brightness <= 100:
                brightness = int(brightness * 255 / 100)
            service_data["brightness"] = brightness
        if "color" in params:
            color = params["color"]
            if isinstance(color, str):
                service_data["color_name"] = color
            elif isinstance(color, (list, tuple)) and len(color) == 3:
                service_data["rgb_color"] = list(color)
        if "position" in params:
            service_data["position"] = params["position"]
        if "hvac_mode" in params:
            service_data["hvac_mode"] = params["hvac_mode"]
        if "speed" in params:
            service_data["speed"] = params["speed"]

        # Add any remaining params
        for key, value in params.items():
            if key not in service_data and key != "entity_id":
                service_data[key] = value

        try:
            await self._revalidate_url()
            url = "%s/api/services/%s" % (self._base_url, service_path)
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(
                    url,
                    headers=self._headers(),
                    json=service_data,
                )
                response.raise_for_status()

            # Invalidate cache after state change
            self._cache.invalidate()

            logger.info(
                "HA service called: %s on %s (params=%s)",
                action,
                entity_id,
                params,
            )
            return {"ok": True, "action": action, "entity_id": entity_id}
        except HomeAssistantUnsafeUrlError as exc:
            error = "HA outbound blocked by URL revalidation: %s" % exc
            logger.warning(error)
            return {"ok": False, "error": error}
        except httpx.HTTPStatusError as exc:
            error = "HA service error: %s" % exc.response.text[:200]
            logger.warning(error)
            return {"ok": False, "error": error}
        except Exception as exc:
            error = "HA service call failed: %s" % exc
            logger.warning(error)
            return {"ok": False, "error": error}

    # ------------------------------------------------------------------ scenes

    async def activate_scene(self, scene_name: str) -> dict[str, Any]:
        """Activate a Home Assistant scene.

        Args:
            scene_name: Scene entity_id (e.g., 'scene.movie_time')
                        or friendly name (resolved via entity list).

        Returns:
            Result dict.
        """
        # Resolve friendly name to entity_id if needed
        entity_id = scene_name
        if not scene_name.startswith("scene."):
            # Search for matching scene
            entities = await self.list_entities(domain="scene")
            for e in entities:
                if e.friendly_name.lower() == scene_name.lower():
                    entity_id = e.entity_id
                    break
            else:
                # Try constructing entity_id
                entity_id = "scene.%s" % scene_name.lower().replace(" ", "_")

        return await self.call_service(entity_id, "turn_on")

    # ------------------------------------------------------------------ health

    async def check_connection(self) -> dict[str, Any]:
        """Test the connection to Home Assistant.

        Returns:
            Dict with connection status and HA version.
        """
        if not self.is_configured:
            return {"connected": False, "error": "Not configured"}

        try:
            await self._revalidate_url()
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(
                    "%s/api/" % self._base_url,
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()

            return {
                "connected": True,
                "message": data.get("message", ""),
                "version": data.get("version", "unknown"),
            }
        except HomeAssistantUnsafeUrlError as exc:
            return {"connected": False, "error": "Outbound blocked: %s" % exc}
        except Exception as exc:
            return {"connected": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP tool handlers
# ---------------------------------------------------------------------------


async def smart_home_list_handler(domain: str = "") -> Any:
    """List smart home entities, optionally filtered by domain.

    Args:
        domain: Filter by domain (light, switch, climate, sensor, etc.).
                Empty string = all entities.
    """
    from intent.tool_types import ToolResult

    client = get_home_assistant()
    if not client.is_configured:
        return ToolResult(
            ok=False,
            data=None,
            error=("Home Assistant is not configured. Set home_assistant_url and " "home_assistant_token in Settings."),
        )

    entities = await client.list_entities(domain=domain or None)
    if not entities:
        return ToolResult(ok=True, data="No entities found%s." % (" for domain '%s'" % domain if domain else ""))

    lines = ["%d entity(ies)%s:" % (len(entities), " (domain: %s)" % domain if domain else "")]
    for e in entities:
        attrs = []
        if e.domain == "light" and "brightness" in e.attributes:
            brightness = e.attributes["brightness"]
            if brightness is not None:
                attrs.append("brightness=%d%%" % int(brightness * 100 / 255))
        if e.domain == "climate" and "temperature" in e.attributes:
            attrs.append("temp=%s" % e.attributes["temperature"])
        if e.domain == "climate" and "current_temperature" in e.attributes:
            attrs.append("current=%s" % e.attributes["current_temperature"])

        attr_str = " (%s)" % ", ".join(attrs) if attrs else ""
        lines.append("  %s: %s [%s]%s" % (e.entity_id, e.friendly_name, e.state, attr_str))

    return ToolResult(ok=True, data="\n".join(lines))


async def smart_home_control_handler(
    entity_id: str,
    action: str,
    params: str = "",
) -> Any:
    """Control a smart home device.

    Args:
        entity_id: The entity to control (e.g., 'light.living_room').
        action: Action to perform (turn_on, turn_off, toggle, set_temperature,
                set_brightness, set_color, etc.).
        params: JSON string of additional parameters (e.g., '{"brightness": 80}').
    """
    import json as _json

    from intent.tool_types import ToolResult

    client = get_home_assistant()
    if not client.is_configured:
        return ToolResult(
            ok=False,
            data=None,
            error="Home Assistant is not configured.",
        )

    # Parse params
    extra_params: dict[str, Any] = {}
    if params:
        try:
            extra_params = _json.loads(params)
        except (ValueError, TypeError):
            return ToolResult(
                ok=False,
                data=None,
                error="Invalid params JSON: %s" % params,
            )

    result = await client.call_service(entity_id, action, **extra_params)
    if result.get("ok"):
        return ToolResult(
            ok=True,
            data="Done: %s on %s" % (action, entity_id),
        )
    return ToolResult(
        ok=False,
        data=None,
        error=result.get("error", "Service call failed"),
    )


async def smart_home_state_handler(entity_id: str) -> Any:
    """Get the current state of a smart home entity.

    Args:
        entity_id: The entity ID (e.g., 'light.living_room').
    """
    import json as _json

    from intent.tool_types import ToolResult

    client = get_home_assistant()
    if not client.is_configured:
        return ToolResult(
            ok=False,
            data=None,
            error="Home Assistant is not configured.",
        )

    state = await client.get_state(entity_id)
    if state is None:
        return ToolResult(
            ok=False,
            data=None,
            error="Entity '%s' not found." % entity_id,
        )

    # Build readable state
    info = {
        "entity_id": state.entity_id,
        "friendly_name": state.friendly_name,
        "state": state.state,
        "domain": state.domain,
    }

    # Add relevant attributes
    for key in (
        "temperature",
        "current_temperature",
        "brightness",
        "color_temp",
        "rgb_color",
        "hvac_mode",
        "battery_level",
        "unit_of_measurement",
        "device_class",
    ):
        if key in state.attributes:
            info[key] = state.attributes[key]

    return ToolResult(ok=True, data=_json.dumps(info, default=str))


async def smart_home_scene_handler(scene_name: str) -> Any:
    """Activate a Home Assistant scene.

    Args:
        scene_name: Scene entity_id or friendly name (e.g., 'Movie Time').
    """
    from intent.tool_types import ToolResult

    client = get_home_assistant()
    if not client.is_configured:
        return ToolResult(
            ok=False,
            data=None,
            error="Home Assistant is not configured.",
        )

    result = await client.activate_scene(scene_name)
    if result.get("ok"):
        return ToolResult(
            ok=True,
            data="Scene '%s' activated." % scene_name,
        )
    return ToolResult(
        ok=False,
        data=None,
        error=result.get("error", "Failed to activate scene"),
    )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def get_home_assistant(
    base_url: str | None = None,
    token: str | None = None,
) -> HomeAssistantClient:
    """Return the process-wide HomeAssistantClient singleton."""
    try:
        from config.settings import settings as app_settings

        app_surface = str(getattr(app_settings, "app_surface", "desktop")).lower()
    except Exception:
        app_surface = "desktop"

    if app_surface == "cloud":
        try:
            from core.user_context import get_current_user_id
            from services.cloud_integrations import get_cloud_integrations_service

            return get_cloud_integrations_service().get_home_assistant_client_sync(get_current_user_id())
        except LookupError:
            return HomeAssistantClient()

    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                # Load from settings if not provided
                if base_url is None or token is None:
                    try:
                        from ui.settings_manager import get_settings_manager

                        sm = get_settings_manager()
                        if base_url is None:
                            base_url = sm.get("home_assistant_url", "")
                        if token is None:
                            token = sm.get("home_assistant_token", "")
                    except Exception:
                        pass
                _SINGLETON = HomeAssistantClient(
                    base_url=base_url,
                    token=token,
                )
    return _SINGLETON


def reset_home_assistant_cache() -> None:
    """Dispose of the cached Home Assistant client."""
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None


def reset_home_assistant_for_tests() -> None:
    """Dispose of the singleton. For test suites only."""
    reset_home_assistant_cache()
