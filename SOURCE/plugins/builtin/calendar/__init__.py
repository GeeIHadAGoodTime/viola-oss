"""Calendar Plugin — wraps existing calendar service as a plugin."""

from __future__ import annotations

from typing import Any

from plugins.api import (
    Capability,
    PluginContext,
    ThirdPartyPlugin,
)


class CalendarPlugin(ThirdPartyPlugin):
    name = "calendar"
    version = "1.0.0"
    description = "Query and manage your calendar events"
    author = "NOVVIOLA"
    required_permissions = []

    def __init__(self, context: PluginContext):
        super().__init__(context)

    def get_capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="calendar",
                description="Query and manage calendar events",
                permissions=[],
            )
        ]

    def get_config_schema(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "properties": {
                "default_calendar": {
                    "type": "string",
                    "enum": ["auto", "local", "google", "graph", "caldav", "all"],
                    "description": "Preferred calendar provider or sync target",
                    "default": "auto",
                },
            },
        }

    def get_api_routes(self) -> list[tuple[str, str, Any]] | None:
        from .handler import api_add_event, api_events_today, api_next_event

        return [
            ("GET", "/today", api_events_today),
            ("GET", "/next", api_next_event),
            ("POST", "/add", api_add_event),
        ]
