"""
Intent Pipeline Room Tracking Logic.

This module contains the active room tracking and priority checking
logic extracted from the main IntentPipeline class to comply with code constraints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from .pipeline_contracts import PipelineResult

logger = get_logger(__name__)


class IntentPipelineRoomTracker:
    """Handles active room tracking and intent priority checking."""

    def __init__(self, pipeline_instance):
        """
        Initialize room tracker.

        Args:
            pipeline_instance: The IntentPipeline instance
        """
        self.pipeline = pipeline_instance

    def should_execute_intent(self, node_id: str, intent_name: str, is_hub: bool) -> tuple[bool, str]:
        """
        Check if an intent should execute based on active room priority.

        Args:
            node_id: Current node identifier
            intent_name: Name of the intent to check
            is_hub: Whether current node is the Hub

        Returns:
            (should_execute, reason) tuple
        """
        if not self.pipeline._active_room_tracker:
            return True, "No active room tracking"

        return self.pipeline._active_room_tracker.should_execute_intent(node_id, intent_name, is_hub)

    def record_interaction(self, node_id: str) -> None:
        """
        Record interaction for active room tracking.

        Args:
            node_id: Node identifier
        """
        if self.pipeline._active_room_tracker and node_id:
            self.pipeline._active_room_tracker.record_interaction(node_id)

    def check_intent_priority(self, intent_name: str, source: str = "unknown") -> tuple[bool, str | None, str | None]:
        """
        Check if intent should execute based on active room priority.

        Args:
            intent_name: Name of the intent
            source: Source of the intent (for logging)

        Returns:
            (should_execute, reason, error_message) tuple
        """
        if not self.pipeline._active_room_tracker or not self.pipeline.node_id:
            return True, None, None

        should_execute, reason = self.should_execute_intent(self.pipeline.node_id, intent_name, self.pipeline.is_hub)

        if not should_execute:
            logger.info(
                "⏸️ Intent '%s' deferred: %s (from node %s, source: %s)",
                intent_name,
                reason,
                self.pipeline.node_id,
                source,
            )
            return False, reason, f"Intent deferred: {reason}"

        logger.debug(
            "✅ Intent '%s' approved: %s (from node %s, source: %s)",
            intent_name,
            reason,
            self.pipeline.node_id,
            source,
        )
        return True, None, None

    def create_deferred_result(self, intent_name: str, reason: str) -> PipelineResult:
        """
        Create a deferred result for when intent execution is blocked.

        Args:
            intent_name: Name of the deferred intent
            reason: Reason for deferral

        Returns:
            Result dict
        """
        from .pipeline_contracts import PipelineResult

        return PipelineResult(
            ok=False,
            intent=intent_name,
            data={
                "message": "Command deferred - another room is active",
                "deferred": True,
                "reason": reason,
            },
            error=f"Intent deferred: {reason}",
            source="priority_check",
            requires_clarification=False,
            policy_flags=["room_priority"],
        )
