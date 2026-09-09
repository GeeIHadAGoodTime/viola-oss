"""Public instant command runtime API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from core.logging_config import get_logger
from services.capability_registry import CapabilityRegistry

from ..instant_commands_patterns import INSTANT_PATTERNS
from .handlers import InstantCommandHandlers

log = get_logger("viola.intent.instant")


class InstantCommandHandler:
    """Handles instant commands that bypass AI processing."""

    def __init__(self, music_player, state=None):
        self.music = music_player
        self.state = state
        self._handlers = InstantCommandHandlers(self)

    def set_event_hub(self, hub) -> None:
        """Inject the EventHub so seek commands can broadcast to WS clients."""
        self._handlers.set_event_hub(hub)

    def check_instant_command(self, text: str) -> tuple[str, dict[str, object], str] | None:
        """Check if text matches an instant command pattern."""
        text_normalized = text.strip().lower()

        for pattern, command, params, description in INSTANT_PATTERNS:
            match = pattern.match(text_normalized)
            if match:
                if command == "home_appliance_query":
                    registry = CapabilityRegistry.get_instance()
                    if registry is None or not registry.is_domain_connected("smart_home"):
                        return None
                log.info("INSTANT COMMAND: %s (bypassing AI)", command)
                return (
                    command,
                    {
                        **params,
                        **match.groupdict(),
                        "_original_text": text_normalized,
                    },
                    description,
                )

        return None

    async def execute_instant_command(self, command: str, params: dict[str, object]) -> dict[str, object]:
        """Execute an instant command."""
        try:
            handler_map: dict[str, Callable[[dict[str, object]], Awaitable[dict[str, object]]]] = (
                InstantCommandHandlers.build_handler_map(self._handlers)
            )
            handler = handler_map.get(command)
            if not handler:
                return {
                    "ok": False,
                    "message": f"Unknown instant command: {command}",
                    "data": {},
                    "error": "unknown_command",
                }

            result = await handler(params)
            log.info("Instant command '%s' executed successfully", command)
            return result
        except Exception:
            log.exception("Instant command '%s' failed", command)
            return {
                "ok": False,
                "message": "Command execution failed",
                "data": {},
                "error": "Command execution failed",
            }
