"""
Hub State Authority - Canonical State Supremacy and Conflict Reconciliation

Implements Hub canonical state supremacy rule (Section 0.7 Hub Supremacy Rule,
Section 4.1 Orchestrator Invariant).
All provider returns MUST flow through this module before updating state.

Key responsibilities:
- Validate provider state against schema
- Merge provider state with canonical hub state
- Detect and reconcile conflicts
- Drop malformed state and schedule coarse refresh
- Emit telemetry for conflicts and validation failures
- Preserve existing FSM behavior from models/state_machine.py

This module consolidates:
- State types (StateMergeStrategy, StateConflict, etc.)
- ConflictDetector - detects conflicts between hub and provider state
- StateReconciler - merges states using configurable strategies
- HubStateValidator - validates provider state against schema
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any

from contracts.player_state import (
    PlayerStateContractError,
    PlayerStateValidator,
    serialize_player_state,
)
from core.logging_config import get_logger
from models.player import PlayerState

logger = get_logger(__name__)

try:
    from diagnostics.runtime_metrics import get_runtime_metrics
except Exception as e:  # pragma: no cover - optional dependency
    logger.debug("Failed to import get_runtime_metrics: %s", e, exc_info=True)
    get_runtime_metrics = None

try:
    from diagnostics.failure_envelope import emit_failure
except Exception as e:  # pragma: no cover - optional dependency
    logger.debug("Failed to import emit_failure: %s", e, exc_info=True)
    emit_failure = None


# =============================================================================
# State Types (consolidated from state_types.py)
# =============================================================================


class StateMergeStrategy(Enum):
    """Strategy for merging provider state with canonical hub state."""

    HUB_PRIORITY = "hub_priority"  # Hub state takes precedence
    PROVIDER_PRIORITY = "provider_priority"  # Provider state takes precedence (rare)
    MERGE = "merge"  # Intelligent merge of non-conflicting fields
    DROP = "drop"  # Drop provider state due to conflict


class StateConflictSeverity(Enum):
    """Severity of state conflicts."""

    INFO = "info"  # Informational conflict, merge successful
    WARNING = "warning"  # Warning-level conflict, merge with resolution
    ERROR = "error"  # Error-level conflict, state dropped
    CRITICAL = "critical"  # Critical conflict, requires immediate refresh


@dataclass
class StateConflict:
    """Represents a state conflict detected during reconciliation."""

    field: str
    hub_value: Any
    provider_value: Any
    severity: StateConflictSeverity
    resolution: str
    description: str
    timestamp: float = dc_field(default_factory=time.time)


@dataclass
class StateReconciliationResult:
    """Result of state reconciliation process."""

    success: bool
    canonical_state: PlayerState | None
    conflicts: list[StateConflict] = dc_field(default_factory=list)
    dropped: bool = False
    refresh_scheduled: bool = False
    error_message: str | None = None
    validation_errors: list[str] = dc_field(default_factory=list)


# =============================================================================
# Conflict Detector (consolidated from conflict_detector.py)
# =============================================================================


class _ConflictDetector:
    """Handles conflict detection logic for state reconciliation."""

    def detect_conflicts(self, hub_state: PlayerState, provider_state: PlayerState) -> list[StateConflict]:
        """
        Detect conflicts between hub state and provider state.

        Args:
            hub_state: Canonical hub state
            provider_state: Provider state to compare

        Returns:
            List of detected conflicts
        """
        conflicts = []

        # Check now_playing conflicts
        if self._now_playing_conflicts(hub_state, provider_state):
            conflicts.append(
                StateConflict(
                    field="now_playing",
                    hub_value=(getattr(hub_state.now_playing, "id", None) if hub_state.now_playing else None),
                    provider_value=(
                        getattr(provider_state.now_playing, "id", None) if provider_state.now_playing else None
                    ),
                    severity=StateConflictSeverity.WARNING,
                    resolution="hub_authoritative",
                    description="Now playing tracks differ",
                )
            )

        # Check queue conflicts
        queue_conflicts = self._queue_conflicts(hub_state, provider_state)
        conflicts.extend(queue_conflicts)

        # Check playback state conflicts
        playback_conflicts = self._playback_state_conflicts(hub_state, provider_state)
        conflicts.extend(playback_conflicts)

        # Check position conflicts (with tolerance)
        if self._position_conflicts(hub_state, provider_state):
            conflicts.append(
                StateConflict(
                    field="position",
                    hub_value=hub_state.position,
                    provider_value=provider_state.position,
                    severity=StateConflictSeverity.INFO,
                    resolution="provider_newer",
                    description="Playback positions differ significantly",
                )
            )

        return conflicts

    def _now_playing_conflicts(self, hub_state: PlayerState, provider_state: PlayerState) -> bool:
        """Check if now_playing fields conflict."""
        hub_track = hub_state.now_playing
        provider_track = provider_state.now_playing

        # Both None = no conflict
        if not hub_track and not provider_track:
            return False

        # One None, other not = conflict
        if bool(hub_track) != bool(provider_track):
            return True

        # Both exist - check if same track
        if hub_track and provider_track:
            hub_id = getattr(hub_track, "id", None)
            provider_id = getattr(provider_track, "id", None)
            return hub_id != provider_id

        return False

    def _queue_conflicts(self, hub_state: PlayerState, provider_state: PlayerState) -> list[StateConflict]:
        """Check for queue conflicts."""
        conflicts = []

        hub_queue = hub_state.queue or []
        provider_queue = provider_state.queue or []

        # Extract IDs for comparison
        hub_ids = set()
        for item in hub_queue:
            item_id = getattr(item, "id", None)
            if item_id:
                hub_ids.add(item_id)

        provider_ids = set()
        for item in provider_queue:
            item_id = getattr(item, "id", None)
            if item_id:
                provider_ids.add(item_id)

        # Check if provider queue is a superset (additive change from autoplay)
        is_additive = hub_ids.issubset(provider_ids) and len(provider_queue) > len(hub_queue)

        # Check if provider queue is a subset (deletion - provider is source of truth)
        # QUEUE ARCHITECTURE: Provider (MusicPlayer) is authoritative for queue mutations
        is_deletion = provider_ids.issubset(hub_ids) and len(provider_queue) < len(hub_queue)

        # Different lengths = conflict
        if len(hub_queue) != len(provider_queue):
            if is_additive:
                resolution = "provider_authoritative"
                description = "Queue grew (additive change, provider authoritative)"
                logger.info(
                    "Queue additive change detected: hub=%d, provider=%d",
                    len(hub_queue),
                    len(provider_queue),
                )
            elif is_deletion:
                resolution = "provider_authoritative"
                description = "Queue shrunk (deletion, provider authoritative)"
                logger.info(
                    "Queue deletion detected: hub=%d, provider=%d",
                    len(hub_queue),
                    len(provider_queue),
                )
            else:
                resolution = "hub_authoritative"
                description = "Queue lengths differ"

            conflicts.append(
                StateConflict(
                    field="queue",
                    hub_value=len(hub_queue),
                    provider_value=len(provider_queue),
                    severity=StateConflictSeverity.WARNING,
                    resolution=resolution,
                    description=description,
                )
            )
            return conflicts

        # Same length - check individual items
        for i, (hub_item, provider_item) in enumerate(zip(hub_queue, provider_queue, strict=False)):
            hub_id = getattr(hub_item, "id", None)
            provider_id = getattr(provider_item, "id", None)

            if hub_id != provider_id:
                conflicts.append(
                    StateConflict(
                        field=f"queue[{i}]",
                        hub_value=hub_id,
                        provider_value=provider_id,
                        severity=StateConflictSeverity.INFO,
                        resolution="hub_authoritative",
                        description=f"Queue item {i} differs",
                    )
                )

        return conflicts

    def _playback_state_conflicts(self, hub_state: PlayerState, provider_state: PlayerState) -> list[StateConflict]:
        """Check for playback state conflicts."""
        conflicts = []

        # Check is_playing
        if hub_state.is_playing != provider_state.is_playing:
            conflicts.append(
                StateConflict(
                    field="is_playing",
                    hub_value=hub_state.is_playing,
                    provider_value=provider_state.is_playing,
                    severity=StateConflictSeverity.ERROR,
                    resolution="hub_authoritative",
                    description="Playback state differs",
                )
            )

        # Check volume (allow small differences)
        if hub_state.volume is not None and provider_state.volume is not None:
            if abs(hub_state.volume - provider_state.volume) > 5:
                conflicts.append(
                    StateConflict(
                        field="volume",
                        hub_value=hub_state.volume,
                        provider_value=provider_state.volume,
                        severity=StateConflictSeverity.WARNING,
                        resolution="merge_prefer_hub",
                        description="Volume differs significantly",
                    )
                )

        return conflicts

    def _position_conflicts(self, hub_state: PlayerState, provider_state: PlayerState) -> bool:
        """Check if positions conflict (with tolerance)."""
        hub_pos = hub_state.position
        provider_pos = provider_state.position

        # Both None = no conflict
        if hub_pos is None and provider_pos is None:
            return False

        # One None, other not = conflict
        if (hub_pos is None) != (provider_pos is None):
            return True

        # Both exist - check difference (allow 5 second tolerance)
        if hub_pos is not None and provider_pos is not None:
            return abs(hub_pos - provider_pos) > 5

        return False


# =============================================================================
# State Reconciler (consolidated from state_reconciler.py)
# =============================================================================


class _StateReconciler:
    """Handles state reconciliation logic."""

    def __init__(self) -> None:
        self._merge_failures = 0

    def reconcile_state(
        self,
        hub_state: PlayerState,
        provider_state: PlayerState,
        conflicts: list[StateConflict],
        merge_strategy: StateMergeStrategy,
    ) -> StateReconciliationResult:
        """
        Reconcile provider state with hub state using specified strategy.

        Args:
            hub_state: Canonical hub state
            provider_state: Provider state
            conflicts: Detected conflicts
            merge_strategy: Merge strategy

        Returns:
            StateReconciliationResult with reconciliation outcome
        """
        try:
            if merge_strategy == StateMergeStrategy.HUB_PRIORITY:
                return StateReconciliationResult(
                    success=True,
                    canonical_state=hub_state,
                    conflicts=conflicts,
                )

            elif merge_strategy == StateMergeStrategy.PROVIDER_PRIORITY:
                return StateReconciliationResult(
                    success=True,
                    canonical_state=provider_state,
                    conflicts=conflicts,
                )

            elif merge_strategy == StateMergeStrategy.MERGE:
                merged_state = self._merge_states(hub_state, provider_state, conflicts)
                if merged_state:
                    return StateReconciliationResult(
                        success=True,
                        canonical_state=merged_state,
                        conflicts=conflicts,
                    )
                else:
                    return StateReconciliationResult(
                        success=False,
                        canonical_state=None,
                        conflicts=conflicts,
                        dropped=True,
                        refresh_scheduled=True,
                        error_message="State merge failed",
                    )

            else:  # DROP
                return StateReconciliationResult(
                    success=False,
                    canonical_state=None,
                    conflicts=conflicts,
                    dropped=True,
                    refresh_scheduled=True,
                    error_message="State dropped by strategy",
                )

        except Exception as exc:
            self._merge_failures += 1
            logger.exception("State reconciliation failed: %s", exc)
            return StateReconciliationResult(
                success=False,
                canonical_state=None,
                conflicts=conflicts,
                dropped=True,
                refresh_scheduled=True,
                error_message=str(exc),
            )

    def _merge_states(
        self,
        hub_state: PlayerState,
        provider_state: PlayerState,
        conflicts: list[StateConflict],
    ) -> PlayerState | None:
        """
        Merge hub state with provider state intelligently.

        Strategy:
        - Hub state is authoritative for control fields (is_playing, volume)
        - Provider state is authoritative for playback progress (position, duration)
        - Queue is merged based on conflicts
        - Metadata is merged
        """
        try:
            # Start with hub state as base
            merged = hub_state.model_copy()

            # Provider is authoritative for playback state — the music player
            # is the source of truth for whether it's playing or paused
            merged.is_playing = provider_state.is_playing

            # Provider is authoritative for playback progress
            if provider_state.position is not None:
                merged.position = provider_state.position
            merged.position_ms = provider_state.position_ms
            merged.position_percentage = provider_state.position_percentage
            if provider_state.duration is not None:
                merged.duration = provider_state.duration

            # Update backend info from provider if available
            if provider_state.backend:
                merged.backend = provider_state.backend
            if provider_state.backend_capabilities:
                merged.backend_capabilities = provider_state.backend_capabilities
            if provider_state.backend_display_name:
                merged.backend_display_name = provider_state.backend_display_name

            # Update playback capabilities from provider
            if provider_state.playback_capabilities:
                merged.playback_capabilities = provider_state.playback_capabilities

            # SP-BUG-10: Provider is authoritative for now_playing — the music
            # player is the ground truth for what track is currently loaded.
            # Previously, now_playing was only updated when there were NO
            # conflicts, but a conflict is always detected when one side is
            # None and the other is not (e.g., after fire-and-forget play
            # completes).  This caused now_playing to be permanently None
            # because the hub's stale None value always won.
            merged.now_playing = provider_state.now_playing

            # Merge queue based on conflicts
            # IMPORTANT: Use "is not None" not truthy check - empty list [] is valid (cleared queue)
            queue_conflicts = [c for c in conflicts if c.field == "queue"]
            if queue_conflicts:
                provider_wins = any(c.resolution == "provider_authoritative" for c in queue_conflicts)
                if provider_wins and provider_state.queue is not None:
                    merged.queue = provider_state.queue
                    logger.info(
                        "Queue updated from provider (additive change): %d items",
                        len(provider_state.queue),
                    )
                else:
                    logger.warning(
                        "Queue conflicts detected (%d), keeping hub state",
                        len(queue_conflicts),
                    )
            else:
                if provider_state.queue is not None:
                    merged.queue = provider_state.queue

            # Merge metadata
            if provider_state.metadata:
                if not merged.metadata:
                    merged.metadata = {}
                merged.metadata.update(provider_state.metadata)

            # Merge errors
            if provider_state.playback_errors:
                if not merged.playback_errors:
                    merged.playback_errors = []
                existing_error_ids = {err.get("id") for err in merged.playback_errors}
                for error in provider_state.playback_errors:
                    error_id = error.get("id")
                    if error_id and error_id not in existing_error_ids:
                        merged.playback_errors.append(error)

            return merged

        except Exception as exc:
            logger.exception("State merge failed: %s", exc)
            return None

    @property
    def merge_failure_count(self) -> int:
        """Get count of merge failures."""
        return self._merge_failures


# =============================================================================
# Hub State Validator (consolidated from hub_state_validator.py)
# =============================================================================


class _HubStateValidator:
    """Handles state validation for the Hub State Authority."""

    def __init__(self, authority_instance: HubStateAuthority) -> None:
        self.authority = authority_instance

    def validate_provider_state(
        self, provider_id: str, provider_state: Any
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        """
        Validate provider state against schema and constraints.

        Args:
            provider_id: Provider identifier
            provider_state: State provided by the provider

        Returns:
            (is_valid, error_message, validated_state) tuple
        """
        try:
            if provider_state is None:
                return False, "Provider state is None", None

            if not isinstance(provider_state, dict):
                return (
                    False,
                    f"Provider state must be dict, got {type(provider_state)}",
                    None,
                )

            try:
                validator = PlayerStateValidator()
                validated_state = validator.validate(provider_state)
                is_valid = validated_state is not None
                error_msg = None if is_valid else "Validation returned None"
                if not is_valid:
                    return False, f"Schema validation failed: {error_msg}", None

                return True, None, provider_state

            except ImportError:
                logger.warning("PlayerStateValidator not available, skipping schema validation")
                return True, None, provider_state

        except Exception as e:
            error_msg = f"Validation failed: {e!s}"
            logger.exception("Provider state validation error for %s: %s", provider_id, error_msg)
            return False, error_msg, None

    def handle_validation_failure(self, provider_id: str, provider_state: Any, error_message: str) -> None:
        """Handle validation failure by logging and scheduling refresh."""
        try:
            logger.warning("Provider %s state validation failed: %s", provider_id, error_message)

            try:
                if emit_failure:
                    emit_failure(
                        code="PROVIDER_STATE_INVALID",
                        component="hub_state_authority",
                        message=error_message,
                        provider_id=provider_id,
                        state_keys=(list(provider_state.keys()) if isinstance(provider_state, dict) else None),
                    )
            except Exception as e:
                logger.debug("Failed to emit validation failure telemetry: %s", e)

            self.authority._schedule_coarse_refresh(provider_id)

            try:
                if get_runtime_metrics:
                    metrics = get_runtime_metrics()
                    if hasattr(metrics, "increment"):
                        metrics.increment("hub_state.validation_failures", 1)
            except Exception as e:
                logger.debug("Failed to update validation metrics: %s", e)

        except Exception as e:
            logger.exception("Failed to handle validation failure: %s", e)


# =============================================================================
# Hub State Authority (main class)
# =============================================================================


class HubStateAuthority:
    """
    Hub State Authority - Enforces canonical state supremacy.

    All provider state updates must flow through this authority before being applied.
    The Hub maintains canonical state that supersedes provider state unless explicit
    merge rules exist.

    Features:
    - Schema validation of provider state
    - Conflict detection and reconciliation
    - Malformed state handling with refresh scheduling
    - Telemetry for conflicts and validation failures
    - FSM behavior preservation
    """

    def __init__(
        self,
        initial_canonical_state: PlayerState | None = None,
        on_refresh_scheduled: Callable[[str], None] | None = None,
    ) -> None:
        """
        Initialize Hub State Authority.

        Args:
            initial_canonical_state: Initial canonical state (defaults to empty PlayerState)
            on_refresh_scheduled: Callback invoked when coarse refresh is scheduled
        """
        self._lock = threading.Lock()
        self._canonical_state: PlayerState = initial_canonical_state or PlayerState()
        self._validator = PlayerStateValidator()
        self._on_refresh_scheduled = on_refresh_scheduled

        # Conflict tracking
        self._conflict_history: list[StateConflict] = []
        self._max_conflict_history = 100
        self._merge_failures = 0

        # Telemetry
        self._metrics = get_runtime_metrics() if get_runtime_metrics else None
        self._validation_failures = 0
        self._conflicts_detected = 0
        self._states_dropped = 0
        self._refreshes_scheduled = 0

        # Refresh scheduling
        self._pending_refresh_providers: set[str] = set()
        self._refresh_callbacks: dict[str, Callable[[], None]] = {}

        # Initialize internal helpers
        self._state_validator = _HubStateValidator(self)
        self._conflict_detector = _ConflictDetector()
        self._state_reconciler = _StateReconciler()

    def get_canonical_state(self) -> PlayerState:
        """
        Get the current canonical state.

        Returns:
            Canonical PlayerState
        """
        with self._lock:
            return PlayerState.model_validate(self._canonical_state.model_dump())

    def update_canonical_now_playing(self, item: Any) -> None:
        """
        Directly update the canonical now_playing state.

        Called when the music player makes an authoritative change (e.g., play with interrupt).
        This bypasses conflict detection for now_playing since the change is intentional.

        Args:
            item: QueueItem, PlayerState, or None to clear now_playing
        """
        with self._lock:
            if isinstance(item, PlayerState):
                self._canonical_state = item
                logger.debug(
                    "Canonical state replaced with PlayerState: now_playing=%s",
                    getattr(item.now_playing, "id", None) if item.now_playing else None,
                )
            elif item is None:
                self._canonical_state.now_playing = None
                self._canonical_state.is_playing = False
                logger.debug("Canonical now_playing cleared")
            else:
                self._canonical_state.now_playing = item
                self._canonical_state.is_playing = True
                logger.debug(
                    "Canonical now_playing updated: %s",
                    getattr(item, "id", None),
                )

    def update_canonical_volume(self, volume: int) -> None:
        """
        Directly update the canonical volume state.

        Called when volume is changed intentionally (e.g., ducking, user command).
        This bypasses conflict detection for volume since the change is intentional.

        Args:
            volume: New volume level (0-100)
        """
        with self._lock:
            old_volume = self._canonical_state.volume
            self._canonical_state.volume = max(0, min(100, volume))
            logger.info(
                "HUB_CANONICAL_VOLUME_UPDATED: %s -> %s",
                old_volume,
                self._canonical_state.volume,
            )

    def update_canonical_playback_state(self, is_playing: bool) -> None:
        """
        Directly update the canonical is_playing state.

        Called when playback state changes intentionally (pause, resume, stop).
        This bypasses conflict detection since the change is intentional.
        """
        with self._lock:
            old_value = self._canonical_state.is_playing
            self._canonical_state.is_playing = is_playing
            logger.info(
                "HUB_CANONICAL_PLAYBACK_UPDATED: %s -> %s",
                old_value,
                is_playing,
            )

    def reconcile_provider_state(
        self,
        provider_state: Any,
        provider_id: str = "unknown",
        merge_strategy: StateMergeStrategy = StateMergeStrategy.MERGE,
    ) -> StateReconciliationResult:
        """
        Reconcile provider state with canonical hub state.

        This is the main entry point for all provider state updates.
        Provider state must flow through this method before being applied.

        Args:
            provider_state: Provider state (PlayerState, dict, or raw payload)
            provider_id: Identifier for the provider (for telemetry)
            merge_strategy: Strategy for merging state

        Returns:
            StateReconciliationResult with reconciliation outcome
        """
        with self._lock:
            # Step 1: Validate provider state
            validation_result = self._validate_provider_state(provider_state, provider_id)
            if not validation_result.success:
                return validation_result

            # Step 2: Convert to PlayerState
            try:
                if isinstance(provider_state, PlayerState):
                    provider_state_obj = provider_state
                elif isinstance(provider_state, dict):
                    provider_state_obj = PlayerState.model_validate(provider_state)
                else:
                    provider_state_obj = self._validator.validate(provider_state)
            except (PlayerStateContractError, Exception) as exc:
                return self._handle_validation_failure(provider_id, str(exc), validation_errors=[str(exc)])

            # Step 2.5: Initialize canonical state if empty
            if (
                not self._canonical_state.now_playing
                and not self._canonical_state.queue
                and not self._canonical_state.is_playing
            ):
                self._canonical_state = provider_state_obj
                logger.debug("Initialized canonical state from provider: %s", provider_id)
                return StateReconciliationResult(
                    success=True,
                    canonical_state=self._canonical_state,
                    conflicts=[],
                )

            # Step 3: Detect conflicts and reconcile
            conflicts = self._conflict_detector.detect_conflicts(self._canonical_state, provider_state_obj)

            self._conflicts_detected += len(conflicts)
            self._conflict_history.extend(conflicts)
            if len(self._conflict_history) > self._max_conflict_history:
                self._conflict_history = self._conflict_history[-self._max_conflict_history :]

            reconciliation = self._state_reconciler.reconcile_state(
                self._canonical_state,
                provider_state_obj,
                conflicts,
                merge_strategy,
            )

            # Step 4: Update canonical state if reconciliation successful
            if reconciliation.success and reconciliation.canonical_state:
                old_state = self._canonical_state
                self._canonical_state = reconciliation.canonical_state

                self._emit_reconciliation_telemetry(
                    provider_id,
                    conflicts,
                    reconciliation,
                    old_state,
                    self._canonical_state,
                )

                if reconciliation.refresh_scheduled:
                    self._schedule_coarse_refresh(provider_id)

                return reconciliation
            else:
                return self._handle_reconciliation_failure(provider_id, reconciliation)

    def _validate_provider_state(self, provider_state: Any, provider_id: str) -> StateReconciliationResult:
        """Validate provider state against schema."""
        try:
            if isinstance(provider_state, PlayerState):
                try:
                    serialize_player_state(provider_state)
                    return StateReconciliationResult(success=True, canonical_state=None)
                except Exception as exc:
                    logger.debug("Provider state validation failed", exc_info=True)
                    return self._handle_validation_failure(provider_id, str(exc), validation_errors=[str(exc)])
            elif isinstance(provider_state, dict):
                self._validator.validate(provider_state)
                return StateReconciliationResult(success=True, canonical_state=None)
            else:
                self._validator.validate(provider_state)
                return StateReconciliationResult(success=True, canonical_state=None)
        except PlayerStateContractError as exc:
            return self._handle_validation_failure(provider_id, str(exc), validation_errors=list(exc.errors))
        except Exception as exc:
            logger.warning(
                "Provider state validation failed for %s: %s",
                provider_id,
                exc,
                exc_info=True,
            )
            return self._handle_validation_failure(provider_id, str(exc), validation_errors=[str(exc)])

    def _handle_provider_failure(
        self,
        provider_id: str,
        error_message: str,
        failure_type: str,
        extra_telemetry: dict[str, Any] | None = None,
        validation_errors: list[str] | None = None,
        conflicts: list[StateConflict] | None = None,
    ) -> StateReconciliationResult:
        """Handle provider failure - increment counters, emit telemetry, schedule refresh."""
        if failure_type == "validation":
            self._validation_failures += 1
        else:
            self._merge_failures += 1
        self._states_dropped += 1

        logger.warning(
            "Provider state %s failed for %s: %s",
            failure_type,
            provider_id,
            error_message,
        )

        telemetry_data = {
            "event": "hub.state_authority",
            "status": f"{failure_type}_failed",
            "provider": provider_id,
        }
        if extra_telemetry:
            telemetry_data.update(extra_telemetry)

        if self._metrics:
            try:
                self._metrics.heartbeat(*telemetry_data)
            except Exception as exc:
                logger.debug("Metrics heartbeat failed: %r", exc)

        failure_event = f"hub_state_{failure_type}_failed"
        failure_data: dict[str, Any] = {
            "event": "hub.state_authority",
            "message": f"Provider state {failure_type} failed for {provider_id}",
            "provider": provider_id,
            "severity": "WARNING",
        }

        if validation_errors:
            failure_data["errors"] = validation_errors
        if conflicts:
            failure_data["conflicts"] = [
                {
                    "field": c.field,
                    "severity": c.severity.value,
                    "resolution": c.resolution,
                }
                for c in conflicts
            ]

        if emit_failure:
            try:
                emit_failure(failure_event, *failure_data)
            except Exception as exc:
                logger.debug("emit_failure call failed: %r", exc)

        self._schedule_coarse_refresh(provider_id)

        return StateReconciliationResult(
            success=False,
            canonical_state=None,
            dropped=True,
            refresh_scheduled=True,
            error_message=error_message,
            validation_errors=validation_errors or [],
            conflicts=conflicts or [],
        )

    def _handle_validation_failure(
        self,
        provider_id: str,
        error_message: str,
        validation_errors: list[str],
    ) -> StateReconciliationResult:
        """Handle validation failure - drop state and schedule refresh."""
        return self._handle_provider_failure(
            provider_id=provider_id,
            error_message=error_message,
            failure_type="validation",
            extra_telemetry={"error_count": len(validation_errors)},
            validation_errors=validation_errors,
        )

    def _handle_reconciliation_failure(
        self, provider_id: str, reconciliation: StateReconciliationResult
    ) -> StateReconciliationResult:
        """Handle reconciliation failure - drop state and schedule refresh."""
        self._handle_provider_failure(
            provider_id=provider_id,
            error_message=reconciliation.error_message or "Reconciliation failed",
            failure_type="reconciliation",
            extra_telemetry={"conflict_count": len(reconciliation.conflicts)},
            conflicts=reconciliation.conflicts,
        )

        return StateReconciliationResult(
            success=False,
            canonical_state=None,
            dropped=True,
            refresh_scheduled=True,
            error_message=reconciliation.error_message,
            validation_errors=reconciliation.validation_errors,
            conflicts=reconciliation.conflicts,
        )

    def _schedule_coarse_refresh(self, provider_id: str) -> None:
        """Schedule coarse refresh for a provider."""
        if provider_id not in self._pending_refresh_providers:
            self._pending_refresh_providers.add(provider_id)
            self._refreshes_scheduled += 1

            logger.info("Scheduled coarse refresh for provider: %s", provider_id)

            if self._on_refresh_scheduled:
                try:
                    self._on_refresh_scheduled(provider_id)
                except Exception as exc:
                    logger.exception("Refresh scheduled callback failed: %s", exc)

            if self._metrics:
                try:
                    self._metrics.heartbeat(
                        "hub.state_authority",
                        status="refresh_scheduled",
                        provider=provider_id,
                    )
                except Exception as exc:
                    logger.debug("Metrics heartbeat for refresh_scheduled failed: %r", exc)

    def _emit_reconciliation_telemetry(
        self,
        provider_id: str,
        conflicts: list[StateConflict],
        reconciliation: StateReconciliationResult,
        old_state: PlayerState,
        new_state: PlayerState,
    ) -> None:
        """Emit telemetry for state reconciliation."""
        if not self._metrics:
            return

        try:
            self._metrics.heartbeat(
                "hub.state_authority",
                status="reconciled",
                provider=provider_id,
                conflict_count=len(conflicts),
                conflicts_resolved=len([c for c in conflicts if c.severity != StateConflictSeverity.CRITICAL]),
            )

            if conflicts:
                for conflict in conflicts:
                    if emit_failure:
                        try:
                            emit_failure(
                                "hub_state_conflict",
                                "hub.state_authority",
                                message=f"State conflict detected: {conflict.field}",
                                provider=provider_id,
                                field=conflict.field,
                                hub_value=str(conflict.hub_value),
                                provider_value=str(conflict.provider_value),
                                severity=conflict.severity.value.upper(),
                                resolution=conflict.resolution,
                            )
                        except Exception as exc:
                            logger.debug("emit_failure for conflict failed: %r", exc)

        except Exception as exc:
            logger.debug("Failed to emit reconciliation telemetry: %s", exc)

    def register_refresh_callback(self, provider_id: str, callback: Callable[[], None]) -> None:
        """Register a callback to be invoked when refresh is scheduled for a provider."""
        with self._lock:
            self._refresh_callbacks[provider_id] = callback

    def clear_pending_refresh(self, provider_id: str) -> None:
        """Clear pending refresh flag for a provider."""
        with self._lock:
            self._pending_refresh_providers.discard(provider_id)

    def get_telemetry(self) -> dict[str, Any]:
        """Get telemetry data for the hub state authority."""
        with self._lock:
            return {
                "validation_failures": self._validation_failures,
                "merge_failures": self._state_reconciler.merge_failure_count,
                "conflicts_detected": self._conflicts_detected,
                "states_dropped": self._states_dropped,
                "refreshes_scheduled": self._refreshes_scheduled,
                "pending_refresh_providers": list(self._pending_refresh_providers),
                "conflict_history_size": len(self._conflict_history),
            }

    def get_conflict_history(self, limit: int | None = None) -> list[StateConflict]:
        """Get conflict history."""
        with self._lock:
            history = self._conflict_history.copy()
            if limit:
                history = history[-limit:]
            return history


__all__ = [
    "HubStateAuthority",
    "StateConflict",
    "StateConflictSeverity",
    "StateMergeStrategy",
    "StateReconciliationResult",
]
