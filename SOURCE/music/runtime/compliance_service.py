from __future__ import annotations

from typing import Any

from models.player import QueueItem
from music.compliance.ytm_policy import ComplianceContext, YouTubeCompliancePolicy
from music.runtime.contracts import ComplianceService


class YouTubeComplianceService(ComplianceService):
    """
    Thin wrapper around `YouTubeCompliancePolicy` that satisfies the runtime contract.

    `MusicPlayer` and the playback controller no longer need to instantiate the policy
    directly; they depend on this service instead.
    """

    def __init__(
        self,
        *,
        logger: Any,
        violation_callback,
    ) -> None:
        self._violation_callback = violation_callback
        self._policy = YouTubeCompliancePolicy(
            logger=logger,
            violation_callback=violation_callback,
        )

    def evaluate(
        self,
        item: QueueItem,
        context: ComplianceContext,
    ) -> Any:
        return self._policy.evaluate(item, context=context)

    def record_violation(
        self,
        item: QueueItem,
        violation_type: str,
        metadata: dict[str, Any],
    ) -> None:
        self._violation_callback(item, violation_type, metadata)
