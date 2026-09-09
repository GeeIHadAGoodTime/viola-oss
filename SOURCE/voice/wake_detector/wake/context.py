"""
Wake Context Data Structures
============================

Context and result types for wake decision evaluation.
Simplified to 4-gate pipeline (2026-02-10).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class WakeContext:
    """
    Context for wake decision evaluation.

    Contains all signals needed by the 5-gate policy.
    """

    # Primary signals
    wake_score: float = 0.0
    base_threshold: float = 0.80

    # Audio metrics (kept for diagnostics and AEC tracking)
    mic_rms: float = 0.0
    loopback_rms: float = 0.0
    post_aec_rms: float = 0.0
    correlation: float = 0.0

    # VAD signal (kept for diagnostics, not used by gates)
    vad_confidence: float = 0.0

    # Confirmation gate: recent inference scores (last N frames)
    recent_scores: list[float] = field(default_factory=list)

    # Timing
    timestamp: float = field(default_factory=time.time)
    frame_count: int = 0


@dataclass
class WakeDecisionResult:
    """
    Result of a wake decision evaluation.

    Contains the decision and diagnostic information.
    """

    should_trigger: bool
    effective_threshold: float
    blocking_reason: str | None = None

    # Legacy fields (kept for API compatibility with tests/diagnostics)
    vad_blocked: bool = False
    echo_vetoed: bool = False
    confirmation_pending: bool = False

    # Metrics
    layers_passed: list[str] = field(default_factory=list)
    layers_failed: list[str] = field(default_factory=list)
