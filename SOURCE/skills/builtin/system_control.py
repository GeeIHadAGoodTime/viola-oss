"""
System Control Skill

Provides system monitoring and control capabilities.
This skill declaration makes system visible in the /v1/skills endpoint.
"""

from __future__ import annotations

from ..base import Intent, Response, Skill


class SystemControlSkill(Skill):
    """Skill for system monitoring and control."""

    name = "system"
    description = "Control desktop, check disk space, CPU usage, RAM, and system info"
    priority = 70

    def patterns(self) -> list[str]:
        return [
            r"^(?:what(?:'?s| is)(?: (?:the|my))?\s+)?system\s+(?:info|information|status|stats|diagnostics|health|report)(?:\s+.*)?$",
            r"^(?:what(?:'?s| is)(?: the)?)?\s*(?:cpu|processor|ram|memory|disk|storage)\s+(?:usage|use|status|info)(?:\s+.*)?$",
            r"^(?:how much|check)(?: (?:the|my))?\s+(?:disk(?: space)?|storage|ram|memory|cpu)(?:\s+.*)?$",
            r"^(?:system|computer)\s+(?:status|info|stats|diagnostics)(?:\s+.*)?$",
            r"^(?:is (?:the )?(?:system|computer|pc) (?:ok|healthy|fine|running well))(?:\s+.*)?$",
            r"^(?:take|capture)\s+(?:a\s+)?screenshot(?:\s+.*)?$",
            r"^(?:open|launch|start)\s+(?:the\s+)?(?:task manager|calculator|notepad|file explorer)(?:\s+.*)?$",
        ]

    async def execute(self, intent: Intent) -> Response:
        """Delegate to system control instant command handlers."""
        return Response(
            message="Checking system...",
            spoken=True,
            data={"delegated_to": "system_info"},
        )

    async def validate(self) -> bool:
        """System skill is always available."""
        return True
