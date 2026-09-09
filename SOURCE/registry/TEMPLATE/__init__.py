"""Template plugin — copy this directory to plugins/user/<name>/ to get started."""

from __future__ import annotations

from typing import Any

from plugins.api import (
    Capability,
    PluginContext,
    ThirdPartyPlugin,
)


class My_PluginPlugin(ThirdPartyPlugin):
    """Rename this class to match your plugin name (e.g. MyPluginPlugin)."""

    name = "my-plugin"
    version = "0.1.0"
    description = "A custom Viola plugin"
    author = "Your Name"
    required_permissions: list[str] = []

    def __init__(self, context: PluginContext):
        super().__init__(context)

    def get_capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="my-capability",
                description="What this plugin can do",
                permissions=[],
            )
        ]

    def get_config_schema(self) -> dict[str, Any] | None:
        return None
