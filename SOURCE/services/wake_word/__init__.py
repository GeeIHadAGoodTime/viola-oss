"""
Wake word services.

This package now exposes only the shared telemetry queue used for
opt-in wake-word training sample uploads.
"""

from __future__ import annotations

from .training_telemetry import (
    TrainingTelemetryService,
    get_training_telemetry,
    start_telemetry_scheduler,
    stop_telemetry_scheduler,
)

__all__ = [
    "TrainingTelemetryService",
    "get_training_telemetry",
    "start_telemetry_scheduler",
    "stop_telemetry_scheduler",
]
