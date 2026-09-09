"""YouTube Music compliance policy surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from models.player import QueueItem
from music.compliance.youtube_tos import (
    TOSComplianceResult,
    ViolationType,
    YouTubeMusicTOSEnforcer,
)


@dataclass(frozen=True)
class ComplianceContext:
    """Execution context used when evaluating compliance."""

    allow_test_override: bool = False
    auto_fix_playback_mode: bool = True


@dataclass(frozen=True)
class ComplianceVerdict:
    """Output from compliance policy evaluation."""

    allowed: bool
    violations: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


class YouTubeCompliancePolicy:
    """High-level wrapper that enforces YouTube Music playback policy."""

    def __init__(
        self,
        *,
        logger: Any,
        violation_callback: Callable[[QueueItem, ViolationType, dict[str, Any]], None],
    ) -> None:
        self._logger = logger
        self._enforcer = YouTubeMusicTOSEnforcer(
            logger_instance=logger,
            violation_callback=violation_callback,
        )

    def evaluate(
        self,
        item: QueueItem,
        context: ComplianceContext | None = None,
    ) -> TOSComplianceResult:
        """
        Evaluate compliance for a queue item.

        Args:
            item: Queue item to validate.
            context: Optional ComplianceContext with overrides.
        """
        ctx = context or ComplianceContext()
        if ctx.allow_test_override:
            self._logger.debug(
                "Compliance override enabled (test mode) for item_id=%s playback_mode=%s",
                getattr(item, "id", "unknown"),
                getattr(item, "playback_mode", None),
            )
            return TOSComplianceResult(
                is_compliant=True,
                details={
                    "provider": getattr(item, "provider", None),
                    "reason": "test_mode_override",
                    "playback_mode": getattr(item, "playback_mode", None),
                },
            )

        return self._enforcer.validate_compliance(item, auto_fix_playback_mode=ctx.auto_fix_playback_mode)
