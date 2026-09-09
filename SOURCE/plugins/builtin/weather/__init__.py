"""Weather Plugin — wraps existing weather fetch logic as a plugin."""

from __future__ import annotations

from typing import Any

from plugins.api import (
    Capability,
    PluginContext,
    ThirdPartyPlugin,
)


class WeatherPlugin(ThirdPartyPlugin):
    name = "weather"
    version = "1.0.0"
    description = "Current weather conditions and forecasts"
    author = "NOVVIOLA"
    required_permissions = ["network"]

    def __init__(self, context: PluginContext):
        super().__init__(context)

    def get_capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="weather",
                description="Fetch current weather and forecasts",
                permissions=["network"],
            )
        ]

    def get_config_schema(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "properties": {
                "default_city": {
                    "type": "string",
                    "description": "Default city for weather queries",
                    "default": "",
                },
                "unit": {
                    "type": "string",
                    "enum": ["F", "C"],
                    "description": "Temperature unit",
                    "default": "F",
                },
            },
        }

    def get_api_routes(self) -> list[tuple[str, str, Any]] | None:
        from .handler import api_current, api_forecast

        return [
            ("GET", "/current", api_current),
            ("GET", "/forecast", api_forecast),
        ]
