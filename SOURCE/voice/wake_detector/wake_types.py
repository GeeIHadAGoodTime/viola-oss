"""
Wake Word Detection Types
=========================

Core data structures and error types for the wake word detection system.

This module provides:
- Error types for wake detection failures
- Result objects for detection and calibration
- Configuration schemas for wake components
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.constants import SAMPLE_RATE_16K, SAMPLE_RATE_48K


def _default_calibration_dir():
    """Writable calibration dir under the user data dir (PATH-1)."""
    from violawake.config import wake_runtime_data_dir

    return wake_runtime_data_dir() / "calibration"


# --------------------------------------------------------------------------- #
# Error Types                                                                  #
# --------------------------------------------------------------------------- #


class WakeError(Exception):
    """Base exception for wake word detection errors."""

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class CalibrationError(WakeError):
    """
    Error during AEC delay calibration.

    Error Codes:
    - calibration_in_progress: Another calibration is already running
    - audio_init_failed: Failed to initialize audio devices
    - noise_too_high: Ambient noise too high for calibration
    - marker_not_found: Calibration marker not detected in recording
    - ambiguous_peaks: Multiple correlation peaks (unreliable result)
    - weak_correlation: Correlation too weak for reliable delay measurement
    - recording_failed: Failed to record microphone input
    """

    def __init__(self, message: str, error_code: str = "unknown"):
        super().__init__(message, error_code)


class DetectionError(WakeError):
    """Error during wake word detection."""

    pass


class ModelLoadError(WakeError):
    """Error loading wake word model."""

    pass


# --------------------------------------------------------------------------- #
# Detection Types                                                              #
# --------------------------------------------------------------------------- #


class DetectionDecision(Enum):
    """
    Decision outcome from wake detection policy.
    """

    NO_TRIGGER = "no_trigger"  # Score below threshold
    VAD_BLOCKED = "vad_blocked"  # Blocked by VAD gate
    ECHO_VETOED = "echo_vetoed"  # Blocked by echo correlation
    CONFIRM_PENDING = "confirm_pending"  # Waiting for confirmation
    TRIGGER = "trigger"  # Wake word detected, trigger callback


@dataclass
class DetectionContext:
    """
    Context data for a single detection frame.

    Contains all signals and metrics for a wake detection decision.
    """

    # Frame identification
    frame_count: int = 0
    timestamp: float = field(default_factory=time.time)

    # Audio data
    audio: Any = None  # np.ndarray, kept as Any to avoid numpy import

    # Audio metrics
    mic_rms: float = 0.0
    loopback_rms: float = 0.0
    post_aec_rms: float = 0.0
    correlation: float = 0.0

    # Detection scores
    cnn_score: float = 0.0
    rnn_score: float = 0.0
    ensemble_score: float = 0.0

    # VAD
    vad_confidence: float = 0.0

    # Thresholds
    base_threshold: float = 0.80
    effective_threshold: float = 0.80

    # Playback state
    playback_active: bool = False
    playback_volume: int = 80

    # SNR
    snr_estimate_db: float = 30.0

    # Timing
    processing_start_time: float = 0.0


@dataclass
class DetectionResult:
    """
    Result of a wake word detection evaluation.

    Contains the decision and all diagnostic information.
    """

    # Primary result
    decision: DetectionDecision = DetectionDecision.NO_TRIGGER
    should_trigger: bool = False

    # Scores
    score: float = 0.0
    effective_threshold: float = 0.80

    # Blocking info
    blocking_reason: str | None = None

    # Layer results
    vad_blocked: bool = False
    echo_vetoed: bool = False
    confirmation_pending: bool = False

    # Diagnostics
    layers_passed: list[str] = field(default_factory=list)
    layers_failed: list[str] = field(default_factory=list)

    # Timing
    total_latency_ms: float = 0.0

    # Context (optional, for debugging)
    context: DetectionContext | None = None


# --------------------------------------------------------------------------- #
# Calibration Types                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class CalibrationResult:
    """
    Result of AEC delay calibration.

    Contains success/failure status and calibration parameters.
    """

    # Status
    success: bool = False
    failure_reason: str | None = None

    # Delay measurement
    delay_samples: int = 0
    delay_ms: float = 0.0

    # Quality metrics
    confidence: float = 0.0  # Overall confidence [0.0, 1.0]
    correlation_peak: float = 0.0  # Peak correlation value
    noise_floor_db: float = -60.0  # Noise floor in dB

    # Secondary peaks (for diagnostics)
    secondary_peaks: list[tuple[int, float]] = field(default_factory=list)

    # Timestamp
    calibrated_at: float = field(default_factory=time.time)

    @classmethod
    def failure(cls, reason: str) -> CalibrationResult:
        """Create a failed calibration result."""
        return cls(
            success=False,
            failure_reason=reason,
        )


@dataclass
class CalibrationConfig:
    """Configuration for delay calibration."""

    # Recording parameters
    playback_sample_rate: int = SAMPLE_RATE_48K
    recording_sample_rate: int = SAMPLE_RATE_16K

    # Validation thresholds
    min_correlation: float = 0.5  # Minimum correlation for valid result
    max_noise_rms: float = 500.0  # Maximum noise RMS (int16 scale)
    max_secondary_peak_ratio: float = 0.6  # Secondary peak must be < 60% of primary

    # Retry parameters
    max_retries: int = 3
    retry_delay_seconds: float = 1.0

    # Paths — PATH-1: calibration is mutable runtime state and lives under
    # the user data dir, never a cwd/install-relative violawake_data path.
    calibration_data_path: str = field(default_factory=lambda: str(_default_calibration_dir()))


# --------------------------------------------------------------------------- #
# Configuration Types                                                          #
# --------------------------------------------------------------------------- #


@dataclass
class AECConfig:
    """Configuration for AEC processing."""

    enabled: bool = True
    backend: str = "auto"  # "auto", "viola", "pyaec", "speex", "noop"
    delay_ms: int = 50
    filter_length_ms: int = 200
    denoise_enabled: bool = True
    resampler_quality: str = "quality"  # "fast" or "quality"
    reference_buffer_duration_ms: int = 500
    delay_calibration_on_startup: bool = True


@dataclass
class VADConfig:
    """Configuration for VAD processing."""

    backend: str = "auto"  # "auto", "webrtc", "silero", "heuristic"
    aggressiveness: int = 2  # 0-3 for WebRTC
    min_confidence_playback: float = 0.3
    extreme_score_bypass: float = 0.95


@dataclass
class DetectionConfig:
    """Configuration for detection processing."""

    # Thresholds
    base_threshold: float = 0.80
    min_threshold: float = 0.3
    max_threshold: float = 0.95

    # Ensemble
    ensemble_enabled: bool = False
    cnn_weight: float = 0.6
    rnn_weight: float = 0.4

    # Confirmation
    confirmation_window_ms: int = 300
    confirmation_min_detections: int = 2

    # Debounce
    debounce_seconds: float = 2.0


@dataclass
class ThresholdBoostConfig:
    """Configuration for dynamic threshold boosting."""

    # Volume scaling
    volume_scaling_enabled: bool = True
    max_boost_factor: float = 1.5

    # SNR scaling
    snr_scaling_enabled: bool = True
    low_snr_threshold_db: float = 10.0
    low_snr_boost: float = 0.2


# NOTE: BargeInConfig is defined in listener/barge_in_detector.py
# (with more complete configuration options). Import from there, not here.


@dataclass
class PreprocessorConfig:
    """Configuration for audio preprocessing."""

    # Noise gate
    noise_gate_enabled: bool = True
    noise_gate_threshold_db: float = -50.0
    noise_gate_attack_ms: float = 5.0
    noise_gate_release_ms: float = 50.0

    # Compressor
    compressor_enabled: bool = True
    compressor_threshold_db: float = -20.0
    compressor_ratio: float = 4.0
    compressor_attack_ms: float = 5.0
    compressor_release_ms: float = 50.0

    # Pre-emphasis
    pre_emphasis_enabled: bool = True
    pre_emphasis_coefficient: float = 0.97


# --------------------------------------------------------------------------- #
# Exports                                                                      #
# --------------------------------------------------------------------------- #

__all__ = [
    # Configuration
    "AECConfig",
    # NOTE: BargeInConfig removed - use listener.barge_in_detector.BargeInConfig instead
    "CalibrationConfig",
    "CalibrationError",
    # Calibration types
    "CalibrationResult",
    "DetectionConfig",
    "DetectionContext",
    # Detection types
    "DetectionDecision",
    "DetectionError",
    "DetectionResult",
    "ModelLoadError",
    "PreprocessorConfig",
    "ThresholdBoostConfig",
    "VADConfig",
    # Errors
    "WakeError",
]
