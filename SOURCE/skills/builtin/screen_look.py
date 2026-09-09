"""
Screen Look Skill

Provides screen awareness — Viola can look at what is on screen and
provide intelligent insights via a vision-capable LLM.  This skill
declaration makes screen awareness visible in the /v1/skills endpoint
and routes matching intents to the vision subsystem.
"""

from __future__ import annotations

from ..base import Intent, Response, Skill


class ScreenLookSkill(Skill):
    """Skill for screen awareness and visual analysis."""

    name = "screen_look"
    description = "Look at your screen and provide intelligent insights"
    priority = 85

    def patterns(self) -> list[str]:
        return [
            r"(?:^|\b)(?:take\s+a\s+)?look\s+(?:at\s+(?:this|my\s+screen|here)|here)\b",
            r"^what(?:'?s|\s+is)\s+on\s+my\s+screen\b",
            r"^what\s+am\s+I\s+(?:looking\s+at|missing)\b",
            r"^help\s+me\s+with\s+this\b",
            r"^what(?:'?s|\s+is)\s+the\s+answer\b",
            r"^is\s+this\s+(?:real|correct|right)\b",
            r"^look\s+here\b",
        ]

    async def execute(self, intent: Intent) -> Response:
        """Delegate to screen analysis pipeline."""
        return Response(
            message="Let me take a look...",
            spoken=True,
            data={"delegated_to": "screen_analyze"},
        )

    async def validate(self) -> bool:
        """Check if screen awareness feature is enabled in settings."""
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            return bool(sm.get("screen_awareness_enabled", True))
        except Exception:
            return True
