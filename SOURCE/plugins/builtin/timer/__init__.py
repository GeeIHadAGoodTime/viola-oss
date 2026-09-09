"""Timer Plugin — countdown timers with voice announcements."""

from __future__ import annotations

from typing import Any

from plugins.api import (
    Capability,
    PluginContext,
    ThirdPartyPlugin,
)

from .handler import TimerHandler


class TimerPlugin(ThirdPartyPlugin):
    name = "timer"
    version = "1.0.0"
    description = "Set, cancel, and check countdown timers"
    author = "NOVVIOLA"
    required_permissions = ["audio"]

    def __init__(self, context: PluginContext):
        super().__init__(context)
        self._handler = TimerHandler(tts_callback=context.tts_callback)

    def get_capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="timer",
                description="Countdown timers with voice announcement",
                permissions=["audio"],
            )
        ]

    def on_cleanup(self) -> None:
        self._handler.cancel_all()
