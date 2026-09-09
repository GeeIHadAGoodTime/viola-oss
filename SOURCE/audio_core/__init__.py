"""
Audio control interfaces and playback state.

Deterministic queue manager, state machine, and async service interface
for Viola's audio control system.

This module provides:
- QueueManager: Thread-safe, async-safe queue operations
- PlaybackStateMachine: Explicit FSM for playback state transitions
- AudioServiceAPI: Public service endpoints for audio control

Version: 1.0.0
API Contract: Frozen (see docs/audio/integration/batch_b_handbook.md)
"""

from __future__ import annotations

from core.exceptions import AudioServiceError

from .contracts import PlaybackService
from .decoder import (
    BufferConfiguration,
    BufferManager,
    BufferMetrics,
    BufferOverflowError,
    BufferUnderrunError,
    CodecNotSupportedError,
    DecoderConfiguration,
    DecoderIOError,
    DecoderStartupError,
    DecoderTelemetry,
    FFmpegDecoder,
    ProbeResult,
    TelemetryMetric,
)
from .device_validation import AudioDeviceStatus, validate_audio_devices
from .queue_controller import QueueManager
from .queue_types import QueueOperationResult
from .service_api import AudioServiceAPI
from .state_machine import PlaybackState, PlaybackStateMachine, StateTransitionError
from .sync_engine import (
    DriftCorrectionResult,
    DriftMeasurement,
    SyncEngine,
    SyncMode,
    SyncState,
)

__all__ = [
    "AudioDeviceStatus",
    "AudioServiceAPI",
    "AudioServiceError",
    "BufferConfiguration",
    "BufferManager",
    "BufferMetrics",
    "BufferOverflowError",
    "BufferUnderrunError",
    "CodecNotSupportedError",
    "DecoderConfiguration",
    "DecoderIOError",
    "DecoderStartupError",
    "DecoderTelemetry",
    "DriftCorrectionResult",
    "DriftMeasurement",
    "FFmpegDecoder",
    "PlaybackService",
    "PlaybackState",
    "PlaybackStateMachine",
    "ProbeResult",
    "QueueManager",
    "QueueOperationResult",
    "StateTransitionError",
    "SyncEngine",
    "SyncMode",
    "SyncState",
    "TelemetryMetric",
    "validate_audio_devices",
]

__version__ = "1.0.0"
