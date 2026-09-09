"""
Dictation Skill

Provides live dictation — type by voice into any application.
This skill declaration makes dictation visible in the /v1/skills endpoint
and routes matching intents to the dictation subsystem.
"""

from __future__ import annotations

from ..base import Intent, Response, Skill


class DictationSkill(Skill):
    """Skill for live voice-to-text dictation."""

    name = "dictation"
    description = "Dictate text anywhere — type by voice into any application"
    priority = 90  # High priority — instant activation

    def patterns(self) -> list[str]:
        return [
            r"(?:^|\b)(?:start\s+)?dictat(?:e|ing|ion)(?:\s+mode)?(?:\b|$)",
            r"^(?:type|write)\s+(?:here|this|what\s+I\s+say)\b",
            r"^start\s+dictating\b",
            r"^take\s+dictation\b",
        ]

    async def execute(self, intent: Intent) -> Response:
        """Delegate to dictation controller."""
        return Response(
            message="Starting dictation...",
            spoken=True,
            data={"delegated_to": "dictation_start"},
        )

    async def validate(self) -> bool:
        """Check if dictation feature is enabled in settings."""
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            return bool(sm.get("dictation_enabled", True))
        except Exception:
            return True
