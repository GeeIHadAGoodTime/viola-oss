"""
Runtime service contracts for the modular MusicPlayer runtime.

These interfaces provide the seam between the MusicPlayer facade and the
controller-centric runtime architecture. Only canonical models (`models.player`,
`models.state_manager`) are exposed so callers can rely on a stable data
surface while we extract subsystems.
"""

from __future__ import annotations

from .autoplay_service import AutoplayService
from .compliance_service import YouTubeComplianceService
from .contracts import (
    ComplianceService,
    PlayerControlSurface,
    ProviderOrchestrator,
    QueueEngine,
    QueueMutationEvent,
    ResolutionDecision,
    ResolutionRequest,
    StateService,
    WorkerManager,
)
from .control_surface import PlayerControlService
from .play_command import PlayCommandService
from .playback_executor import PlaybackExecutor
from .provider_orchestrator import ProviderOrchestratorService
from .queue_command import QueueCommandService
from .queue_engine import PlaylistQueueEngine
from .queue_resolver import QueueItemResolver
from .state_pipeline import PlayerStatePipeline
from .state_service import PlayerStateService
from .telemetry_service import ComplianceEventBridge, RuntimeTelemetryService
from .worker_manager import RuntimeWorkerManager

__all__ = [
    "AutoplayService",
    "ComplianceEventBridge",
    "ComplianceService",
    "PlayCommandService",
    "PlaybackExecutor",
    "PlayerControlService",
    "PlayerControlSurface",
    "PlayerStatePipeline",
    "PlayerStateService",
    "PlaylistQueueEngine",
    "ProviderOrchestrator",
    "ProviderOrchestratorService",
    "QueueCommandService",
    "QueueEngine",
    "QueueItemResolver",
    "QueueMutationEvent",
    "ResolutionDecision",
    "ResolutionRequest",
    "RuntimeTelemetryService",
    "RuntimeWorkerManager",
    "StateService",
    "WorkerManager",
    "YouTubeComplianceService",
]
