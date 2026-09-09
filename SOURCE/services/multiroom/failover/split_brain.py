"""
Hub Failover System - Split-Brain Resolver

Detects and resolves split-brain scenarios where both hubs believe
they are the primary.

Uses fencing tokens as the primary mechanism for resolution. The hub
with the higher fencing token wins. In case of equal tokens, the
tie-breaking algorithm from leader_election is used.

Usage:
    from services.multiroom.failover.split_brain import SplitBrainResolver

    resolver = SplitBrainResolver(hub_id="hub-1")
    if resolver.detect_split_brain(local_state, remote_state):
        winner = resolver.resolve(local_state, remote_state)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.logging_config import get_logger

from .types import FailoverState, HubRole

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SplitBrainResolution:
    """
    Result of split-brain resolution.

    Attributes:
        winner_hub_id: ID of the hub that should be primary
        loser_hub_id: ID of the hub that should be secondary
        resolution_reason: Why this hub won
        winning_fencing_token: Fencing token of the winner
    """

    winner_hub_id: str
    loser_hub_id: str
    resolution_reason: Literal[
        "higher_fencing_token",
        "higher_state_version",
        "lower_hub_id",
    ]
    winning_fencing_token: int


class SplitBrainResolver:
    """
    Detects and resolves split-brain scenarios.

    Split-brain occurs when both hubs believe they are the primary,
    typically due to network partition. This resolver uses fencing
    tokens to determine the authoritative primary.
    """

    def __init__(self, hub_id: str) -> None:
        """
        Initialize the split-brain resolver.

        Args:
            hub_id: This hub's unique identifier
        """
        self.hub_id = hub_id
        self._resolution_count = 0

        logger.debug("SplitBrainResolver initialized for hub %s", hub_id)

    def detect_split_brain(
        self,
        local_state: FailoverState,
        remote_state: FailoverState,
    ) -> bool:
        """
        Detect if a split-brain condition exists.

        Split-brain is detected when both hubs claim to be PRIMARY.

        Args:
            local_state: Local hub's failover state
            remote_state: Remote hub's failover state

        Returns:
            True if split-brain detected
        """
        is_split_brain = local_state.current_role == HubRole.PRIMARY and remote_state.current_role == HubRole.PRIMARY

        if is_split_brain:
            logger.warning(
                "Split-brain detected: both hubs claiming PRIMARY " "(local token=%d, remote token=%d)",
                local_state.fencing_token,
                remote_state.fencing_token,
            )

        return is_split_brain

    def resolve(
        self,
        local_state: FailoverState,
        remote_state: FailoverState,
        remote_hub_id: str,
    ) -> SplitBrainResolution:
        """
        Resolve a split-brain scenario.

        Resolution order:
        1. Higher fencing token wins
        2. Higher state version wins
        3. Lower hub_id wins (deterministic tie-breaker)

        Args:
            local_state: Local hub's failover state
            remote_state: Remote hub's failover state
            remote_hub_id: Remote hub's identifier

        Returns:
            SplitBrainResolution indicating the winner
        """
        self._resolution_count += 1

        # Rule 1: Higher fencing token wins
        if local_state.fencing_token > remote_state.fencing_token:
            logger.info(
                "Split-brain resolved: local wins by fencing token (%d > %d)",
                local_state.fencing_token,
                remote_state.fencing_token,
            )
            return SplitBrainResolution(
                winner_hub_id=self.hub_id,
                loser_hub_id=remote_hub_id,
                resolution_reason="higher_fencing_token",
                winning_fencing_token=local_state.fencing_token,
            )

        if remote_state.fencing_token > local_state.fencing_token:
            logger.info(
                "Split-brain resolved: remote wins by fencing token (%d > %d)",
                remote_state.fencing_token,
                local_state.fencing_token,
            )
            return SplitBrainResolution(
                winner_hub_id=remote_hub_id,
                loser_hub_id=self.hub_id,
                resolution_reason="higher_fencing_token",
                winning_fencing_token=remote_state.fencing_token,
            )

        # Rule 2: Higher state version wins
        if local_state.state_version > remote_state.state_version:
            logger.info(
                "Split-brain resolved: local wins by state version (%d > %d)",
                local_state.state_version,
                remote_state.state_version,
            )
            return SplitBrainResolution(
                winner_hub_id=self.hub_id,
                loser_hub_id=remote_hub_id,
                resolution_reason="higher_state_version",
                winning_fencing_token=local_state.fencing_token,
            )

        if remote_state.state_version > local_state.state_version:
            logger.info(
                "Split-brain resolved: remote wins by state version (%d > %d)",
                remote_state.state_version,
                local_state.state_version,
            )
            return SplitBrainResolution(
                winner_hub_id=remote_hub_id,
                loser_hub_id=self.hub_id,
                resolution_reason="higher_state_version",
                winning_fencing_token=remote_state.fencing_token,
            )

        # Rule 3: Lower hub_id wins (deterministic tie-breaker)
        if self.hub_id < remote_hub_id:
            logger.info(
                "Split-brain resolved: local wins by hub_id (%s < %s)",
                self.hub_id,
                remote_hub_id,
            )
            return SplitBrainResolution(
                winner_hub_id=self.hub_id,
                loser_hub_id=remote_hub_id,
                resolution_reason="lower_hub_id",
                winning_fencing_token=local_state.fencing_token,
            )

        logger.info(
            "Split-brain resolved: remote wins by hub_id (%s >= %s)",
            self.hub_id,
            remote_hub_id,
        )
        return SplitBrainResolution(
            winner_hub_id=remote_hub_id,
            loser_hub_id=self.hub_id,
            resolution_reason="lower_hub_id",
            winning_fencing_token=remote_state.fencing_token,
        )

    def should_step_down(
        self,
        local_state: FailoverState,
        remote_state: FailoverState,
        remote_hub_id: str,
    ) -> bool:
        """
        Check if this hub should step down to secondary.

        Convenience method that combines detection and resolution.

        Args:
            local_state: Local hub's failover state
            remote_state: Remote hub's failover state
            remote_hub_id: Remote hub's identifier

        Returns:
            True if this hub should step down
        """
        if not self.detect_split_brain(local_state, remote_state):
            return False

        resolution = self.resolve(local_state, remote_state, remote_hub_id)
        return resolution.winner_hub_id != self.hub_id

    def get_resolution_count(self) -> int:
        """
        Get the number of split-brain resolutions performed.

        Returns:
            Number of resolutions
        """
        return self._resolution_count


__all__ = [
    "SplitBrainResolution",
    "SplitBrainResolver",
]
