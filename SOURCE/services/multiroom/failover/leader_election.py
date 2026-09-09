"""
Hub Failover System - Leader Election

Implements simple primary/secondary leader election for hub failover.

Uses a deterministic tie-breaking algorithm:
1. Higher fencing token wins
2. If equal, higher state version wins
3. If equal, lower hub_id wins (lexicographic)

Usage:
    from services.multiroom.failover.leader_election import LeaderElection

    election = LeaderElection(hub_id="hub-1")
    if election.should_become_primary(local_state, remote_state):
        election.promote_to_primary()
"""

from __future__ import annotations

from collections.abc import Callable

from core.logging_config import get_logger

from .types import FailoverState, HubRole

logger = get_logger(__name__)


class LeaderElection:
    """
    Manages leader election for hub failover.

    Implements a simple primary/secondary model with deterministic
    tie-breaking to avoid split-brain scenarios.
    """

    def __init__(
        self,
        hub_id: str,
        initial_role: HubRole = HubRole.SECONDARY,
    ) -> None:
        """
        Initialize the leader election manager.

        Args:
            hub_id: This hub's unique identifier
            initial_role: Initial role (default: SECONDARY)
        """
        self.hub_id = hub_id
        self._state = FailoverState(
            current_role=initial_role,
            fencing_token=0,
            state_version=0,
        )
        self._on_role_change_callback: list[Callable[[HubRole, HubRole], None]] = []

        logger.info(
            "LeaderElection initialized: hub=%s role=%s",
            hub_id,
            initial_role.value,
        )

    @property
    def current_role(self) -> HubRole:
        """Get the current role."""
        return self._state.current_role

    @property
    def is_primary(self) -> bool:
        """Check if this hub is currently primary."""
        return self._state.current_role == HubRole.PRIMARY

    @property
    def state(self) -> FailoverState:
        """Get the current failover state."""
        return self._state

    def on_role_change(self, callback: Callable[[HubRole, HubRole], None]) -> None:
        """
        Register a callback for role changes.

        Args:
            callback: Function to call when role changes
        """
        self._on_role_change_callback.append(callback)

    def update_state(
        self,
        fencing_token: int | None = None,
        state_version: int | None = None,
        partner_hub_id: str | None = None,
        last_heartbeat_time: float | None = None,
    ) -> None:
        """
        Update the failover state.

        Args:
            fencing_token: New fencing token
            state_version: New state version
            partner_hub_id: Partner hub ID
            last_heartbeat_time: Last heartbeat timestamp
        """
        if fencing_token is not None:
            self._state.fencing_token = fencing_token
        if state_version is not None:
            self._state.state_version = state_version
        if partner_hub_id is not None:
            self._state.partner_hub_id = partner_hub_id
        if last_heartbeat_time is not None:
            self._state.last_heartbeat_time = last_heartbeat_time

    def promote_to_primary(self, new_fencing_token: int | None = None) -> bool:
        """
        Promote this hub to primary.

        Args:
            new_fencing_token: Optional new fencing token for the epoch

        Returns:
            True if promotion successful, False if already primary
        """
        if self._state.current_role == HubRole.PRIMARY:
            logger.debug("Hub %s already primary, skipping promotion", self.hub_id)
            return False

        old_role = self._state.current_role
        self._state.current_role = HubRole.PRIMARY

        if new_fencing_token is not None:
            self._state.fencing_token = new_fencing_token

        logger.info(
            "Hub %s promoted to PRIMARY (was %s, token=%d)",
            self.hub_id,
            old_role.value,
            self._state.fencing_token,
        )

        self._notify_role_change(old_role, HubRole.PRIMARY)
        return True

    def demote_to_secondary(self) -> bool:
        """
        Demote this hub to secondary.

        Returns:
            True if demotion successful, False if already secondary
        """
        if self._state.current_role == HubRole.SECONDARY:
            logger.debug("Hub %s already secondary, skipping demotion", self.hub_id)
            return False

        old_role = self._state.current_role
        self._state.current_role = HubRole.SECONDARY

        logger.info(
            "Hub %s demoted to SECONDARY (was %s)",
            self.hub_id,
            old_role.value,
        )

        self._notify_role_change(old_role, HubRole.SECONDARY)
        return True

    def should_become_primary(
        self,
        local_fencing_token: int,
        local_state_version: int,
        remote_fencing_token: int,
        remote_state_version: int,
        remote_hub_id: str,
    ) -> bool:
        """
        Determine if this hub should become primary.

        Uses deterministic tie-breaking:
        1. Higher fencing token wins
        2. If equal, higher state version wins
        3. If equal, lower hub_id wins (lexicographic)

        Args:
            local_fencing_token: Local fencing token
            local_state_version: Local state version
            remote_fencing_token: Remote fencing token
            remote_state_version: Remote state version
            remote_hub_id: Remote hub's ID

        Returns:
            True if local hub should be primary
        """
        # Rule 1: Higher fencing token wins
        if local_fencing_token > remote_fencing_token:
            logger.debug(
                "Local wins: fencing token %d > %d",
                local_fencing_token,
                remote_fencing_token,
            )
            return True
        if local_fencing_token < remote_fencing_token:
            logger.debug(
                "Remote wins: fencing token %d < %d",
                local_fencing_token,
                remote_fencing_token,
            )
            return False

        # Rule 2: Higher state version wins
        if local_state_version > remote_state_version:
            logger.debug(
                "Local wins: state version %d > %d",
                local_state_version,
                remote_state_version,
            )
            return True
        if local_state_version < remote_state_version:
            logger.debug(
                "Remote wins: state version %d < %d",
                local_state_version,
                remote_state_version,
            )
            return False

        # Rule 3: Lower hub_id wins (lexicographic tie-breaker)
        should_win = self.hub_id < remote_hub_id
        logger.debug(
            "Tie-breaker: local hub_id %s %s remote %s",
            self.hub_id,
            "<" if should_win else ">=",
            remote_hub_id,
        )
        return should_win

    def evaluate_election(
        self,
        remote_fencing_token: int,
        remote_state_version: int,
        remote_hub_id: str,
        remote_role: HubRole,
    ) -> HubRole:
        """
        Evaluate election state and determine appropriate role.

        This is called when receiving heartbeat from partner hub.

        Args:
            remote_fencing_token: Remote hub's fencing token
            remote_state_version: Remote hub's state version
            remote_hub_id: Remote hub's ID
            remote_role: Remote hub's current role

        Returns:
            The role this hub should take
        """
        # If remote is primary and has higher token, defer to it
        if remote_role == HubRole.PRIMARY:
            if remote_fencing_token > self._state.fencing_token:
                logger.debug(
                    "Deferring to remote primary with higher token: %d > %d",
                    remote_fencing_token,
                    self._state.fencing_token,
                )
                return HubRole.SECONDARY

        # If we're primary, check if we should remain so
        if self._state.current_role == HubRole.PRIMARY:
            should_remain = self.should_become_primary(
                self._state.fencing_token,
                self._state.state_version,
                remote_fencing_token,
                remote_state_version,
                remote_hub_id,
            )
            return HubRole.PRIMARY if should_remain else HubRole.SECONDARY

        # If remote is not claiming primary and we should be primary
        if remote_role != HubRole.PRIMARY:
            should_become = self.should_become_primary(
                self._state.fencing_token,
                self._state.state_version,
                remote_fencing_token,
                remote_state_version,
                remote_hub_id,
            )
            return HubRole.PRIMARY if should_become else HubRole.SECONDARY

        return HubRole.SECONDARY

    def _notify_role_change(self, old_role: HubRole, new_role: HubRole) -> None:
        """Notify callbacks of role change."""
        for callback in self._on_role_change_callback:
            try:
                callback(old_role, new_role)
            except Exception:
                logger.exception("Error in role change callback")


__all__ = [
    "LeaderElection",
]
