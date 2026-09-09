"""Smart Home Plugin — declares the smart-home capability and config surface.

Actual device control is exposed to Viola's agent through the first-class
``smart_home`` tool (mcp_servers/core_tools/server.py), which talks to a
configured Home Assistant (or similar) bridge. This plugin contributes the
capability declaration and the configuration schema; it does not pre-empt the
model with regex intent matching.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from plugins.api import (
    Capability,
    PluginContext,
    ThirdPartyPlugin,
)

log = get_logger(__name__)


class SmartHomePlugin(ThirdPartyPlugin):
    """Smart home device control plugin."""

    name = "smart-home"
    version = "0.1.0"
    description = "Control smart home devices (lights, thermostat, switches)"
    author = "Community"
    required_permissions = ["network"]

    def __init__(self, context: PluginContext):
        super().__init__(context)
        self._bridge_url: str = ""

    def on_init(self) -> None:
        """Load bridge URL from plugin config."""
        config = self.context.manager.config_manager.get_config(self.name)
        self._bridge_url = config.get("bridge_url", "") if config else ""
        if self._bridge_url:
            log.info("Smart-home bridge configured: %s", self._bridge_url)
        else:
            log.info("Smart-home plugin loaded (no bridge configured yet)")

    def get_capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="smart-home",
                description="Control lights, thermostat, and switches",
                permissions=["network"],
            )
        ]

    def get_config_schema(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "properties": {
                "bridge_url": {
                    "type": "string",
                    "description": "URL of your smart home bridge (e.g., http://homeassistant.local:8123)",
                    "default": "",
                },
                "bridge_type": {
                    "type": "string",
                    "enum": ["home-assistant", "custom"],
                    "description": "Type of smart home bridge",
                    "default": "home-assistant",
                },
            },
        }

    def get_api_routes(self) -> list[tuple[str, str, Any]] | None:
        return None
