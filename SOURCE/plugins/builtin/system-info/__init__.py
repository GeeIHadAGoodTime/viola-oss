"""System Info Plugin — system resource monitoring."""

from __future__ import annotations

from typing import Any

from plugins.api import (
    Capability,
    PluginContext,
    ThirdPartyPlugin,
)


class System_InfoPlugin(ThirdPartyPlugin):
    """Note: class name uses underscore because plugin loader expects
    manifest.name.title() + 'Plugin', and 'system-info'.title() = 'System-Info'.
    We override by using the exact class name the loader constructs."""

    name = "system-info"
    version = "1.0.0"
    description = "System resource monitoring"
    author = "NOVVIOLA"
    required_permissions = ["system"]

    def __init__(self, context: PluginContext):
        super().__init__(context)

    def get_capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="system-info",
                description="Monitor CPU, memory, disk, and uptime",
                permissions=["system"],
            )
        ]

    def get_config_schema(self) -> dict[str, Any] | None:
        return None
