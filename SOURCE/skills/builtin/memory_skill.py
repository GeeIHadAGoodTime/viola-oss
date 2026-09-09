"""
Memory Skill

Provides personal information remembering and recall capabilities.
This skill declaration makes memory visible in the /v1/skills endpoint.
"""

from __future__ import annotations

from ..base import Intent, Response, Skill


class MemorySkill(Skill):
    """Skill for remembering and recalling personal information."""

    name = "memory"
    description = "Remember and recall personal information, preferences, and notes"
    priority = 75

    def patterns(self) -> list[str]:
        return [
            r"^(?:remember|note|save|store)\s+(?:that\s+)?(?:my\s+)?.+$",
            r"^(?:what(?:'?s| is)(?: my)?)?\s*(?:my\s+)?(?:name|birthday|address|phone|email|preference)(?:\s+.*)?$",
            r"^(?:do you (?:remember|know|recall))\s+.+\??$",
            r"^(?:forget|delete|remove)\s+(?:that|my)\s+.+$",
            r"^(?:add a?\s+)?note(?:\s+.*)?$",
            r"^(?:show|list|what are)(?: my)?\s+notes(?:\s+.*)?$",
        ]

    async def execute(self, intent: Intent) -> Response:
        """Delegate to memory/notes instant command handlers."""
        return Response(
            message="Accessing memory...",
            spoken=True,
            data={"delegated_to": "memory_recall"},
        )

    async def validate(self) -> bool:
        """Memory skill is always available."""
        return True
