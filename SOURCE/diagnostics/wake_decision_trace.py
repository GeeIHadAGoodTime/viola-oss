"""
Enhanced Wake Decision Trace
============================

Comprehensive forensic tracing for wake word detection decisions.

Captures complete context for every wake evaluation including:
- Full audio input state
- AEC processing state
- Model output details
- Step-by-step threshold computation
- Layer-by-layer verdicts with margins
- Final decision and classification

Usage:
    from diagnostics.wake_decision_trace import get_decision_tracer

    tracer = get_decision_tracer()

    # Start a trace when model fires
    trace_id = tracer.start_trace(correlation_id)

    # Record audio state
    tracer.record_audio_state(trace_id, mic_rms=1200, loopback_rms=3500)

    # Record AEC state
    tracer.record_aec_state(trace_id, ...)

    # Record layer results
    tracer.record_layer_result(trace_id, "vad_gate", passed=True, ...)

    # Finalize with decision
    tracer.finalize_trace(trace_id, decision="approved", ...)

    # Get trace for analysis
    trace = tracer.get_trace(trace_id)
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Enums and Types                                                              #
# --------------------------------------------------------------------------- #


class TriggerClassification(Enum):
    """Classification of trigger events for analysis."""

    TRUE_POSITIVE = "true_positive"  # Real user speaking wake word
    MUSIC_LYRICS = "music_lyrics"  # Song lyrics containing wake word
    TTS_ECHO = "tts_echo"  # System's own TTS output
    ENVIRONMENTAL_NOISE = "environmental_noise"  # Background noise
    SIMILAR_WORD = "similar_word"  # Similar-sounding word
    RANDOM_SPIKE = "random_spike"  # Random model spike
    UNKNOWN = "unknown"  # Unclassified


class DecisionOutcome(Enum):
    """Outcome of a wake detection evaluation."""

    APPROVED = "approved"  # Trigger accepted
    BLOCKED = "blocked"  # Trigger rejected by policy
    BELOW_THRESHOLD = "below_threshold"  # Score too low


# --------------------------------------------------------------------------- #
# Data Structures                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class AudioInputState:
    """Audio input state at decision time."""

    mic_rms: float = 0.0
    mic_peak: int = 0
    is_clipping: bool = False
    noise_floor_rms: float = 0.0
    snr_db: float = 0.0
    sample_rate: int = 16000
    buffer_latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "mic_rms": round(self.mic_rms, 1),
            "mic_peak": self.mic_peak,
            "is_clipping": self.is_clipping,
            "noise_floor_rms": round(self.noise_floor_rms, 1),
            "snr_db": round(self.snr_db, 1),
            "sample_rate": self.sample_rate,
            "buffer_latency_ms": round(self.buffer_latency_ms, 2),
        }


@dataclass
class AECState:
    """AEC state at decision time."""

    backend: str = "Unknown"
    is_active: bool = False
    delay_ms: float = 0.0
    reference_rms: float = 0.0
    post_aec_rms: float = 0.0
    reduction_ratio: float = 0.0
    correlation: float = 0.0
    gating_active: bool = False
    converged: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "backend": self.backend,
            "is_active": self.is_active,
            "delay_ms": round(self.delay_ms, 1),
            "reference_rms": round(self.reference_rms, 1),
            "post_aec_rms": round(self.post_aec_rms, 1),
            "reduction_ratio": round(self.reduction_ratio, 3),
            "correlation": round(self.correlation, 3),
            "gating_active": self.gating_active,
            "converged": self.converged,
        }


@dataclass
class ModelOutput:
    """Model inference output."""

    raw_score: float = 0.0
    inference_ms: float = 0.0
    model_version: str = ""
    feature_extraction_backend: str = ""  # "librosa" or "scipy_fallback"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "raw_score": round(self.raw_score, 4),
            "inference_ms": round(self.inference_ms, 2),
            "model_version": self.model_version,
            "feature_extraction_backend": self.feature_extraction_backend,
        }


@dataclass
class ThresholdBreakdown:
    """Step-by-step threshold computation."""

    base_threshold: float = 0.85

    # Each modifier with reason
    playback_boost: float = 1.0
    playback_boost_reason: str = ""
    volume_boost: float = 1.0
    volume_boost_reason: str = ""
    snr_boost: float = 1.0
    snr_boost_reason: str = ""
    echo_boost: float = 1.0
    echo_boost_reason: str = ""
    barge_in_adjustment: float = 0.0
    barge_in_reason: str = ""
    learned_offset: float = 0.0
    learned_offset_reason: str = ""

    # Final result
    effective_threshold: float = 0.85
    clamped: bool = False
    clamp_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "base_threshold": round(self.base_threshold, 3),
            "adjustments": {
                "playback_boost": {
                    "factor": round(self.playback_boost, 3),
                    "reason": self.playback_boost_reason,
                },
                "volume_boost": {
                    "factor": round(self.volume_boost, 3),
                    "reason": self.volume_boost_reason,
                },
                "snr_boost": {
                    "factor": round(self.snr_boost, 3),
                    "reason": self.snr_boost_reason,
                },
                "echo_boost": {
                    "factor": round(self.echo_boost, 3),
                    "reason": self.echo_boost_reason,
                },
                "barge_in": {
                    "adjustment": round(self.barge_in_adjustment, 3),
                    "reason": self.barge_in_reason,
                },
                "learned_offset": {
                    "offset": round(self.learned_offset, 3),
                    "reason": self.learned_offset_reason,
                },
            },
            "effective_threshold": round(self.effective_threshold, 3),
            "clamped": self.clamped,
            "clamp_reason": self.clamp_reason,
        }


@dataclass
class LayerVerdict:
    """Verdict from a single defense layer."""

    layer_name: str
    passed: bool
    reason: str | None = None

    # Layer-specific metrics
    metrics: dict[str, Any] = field(default_factory=dict)

    # How close to boundary? (score - threshold for score layers, confidence - threshold for VAD)
    margin: float | None = None

    # Timing
    evaluation_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "layer_name": self.layer_name,
            "passed": self.passed,
            "reason": self.reason,
            "metrics": self.metrics,
            "margin": round(self.margin, 4) if self.margin is not None else None,
            "evaluation_ms": round(self.evaluation_ms, 3),
        }


@dataclass
class DecisionContext:
    """Full context snapshot at decision time."""

    # Playback state
    is_playback_active: bool = False
    playback_volume: int = 0
    is_playing_music: bool = False
    is_tts_speaking: bool = False

    # Listening state
    is_listening_active: bool = False
    listening_start_time: float | None = None

    # Environment
    ambient_noise_level: str = "normal"  # "quiet", "normal", "noisy"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "is_playback_active": self.is_playback_active,
            "playback_volume": self.playback_volume,
            "is_playing_music": self.is_playing_music,
            "is_tts_speaking": self.is_tts_speaking,
            "is_listening_active": self.is_listening_active,
            "listening_start_time": self.listening_start_time,
            "ambient_noise_level": self.ambient_noise_level,
        }


@dataclass
class DecisionTiming:
    """Timing breakdown for the decision process."""

    start_time: float = 0.0
    audio_capture_ms: float = 0.0
    aec_processing_ms: float = 0.0
    feature_extraction_ms: float = 0.0
    model_inference_ms: float = 0.0
    policy_evaluation_ms: float = 0.0
    total_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "start_time": self.start_time,
            "breakdown_ms": {
                "audio_capture": round(self.audio_capture_ms, 2),
                "aec_processing": round(self.aec_processing_ms, 2),
                "feature_extraction": round(self.feature_extraction_ms, 2),
                "model_inference": round(self.model_inference_ms, 2),
                "policy_evaluation": round(self.policy_evaluation_ms, 2),
            },
            "total_ms": round(self.total_ms, 2),
        }


@dataclass
class ClassificationResult:
    """Classification of the trigger event."""

    category: TriggerClassification = TriggerClassification.UNKNOWN
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "category": self.category.value,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
        }


@dataclass
class WakeDecisionTrace:
    """Complete forensic trace of a wake detection evaluation."""

    # Identity
    trace_id: str
    correlation_id: str
    timestamp: float

    # Audio input state
    audio_input: AudioInputState = field(default_factory=AudioInputState)

    # AEC state
    aec: AECState = field(default_factory=AECState)

    # Model output
    model: ModelOutput = field(default_factory=ModelOutput)

    # Threshold computation breakdown
    threshold: ThresholdBreakdown = field(default_factory=ThresholdBreakdown)

    # Layer-by-layer verdicts (ALL layers, not just blocking one)
    layers: dict[str, LayerVerdict] = field(default_factory=dict)

    # Final decision
    decision: DecisionOutcome = DecisionOutcome.BELOW_THRESHOLD
    blocking_layer: str | None = None

    # Classification (for triggers)
    classification: ClassificationResult = field(default_factory=ClassificationResult)

    # Full context snapshot
    context: DecisionContext = field(default_factory=DecisionContext)

    # Timing
    timing: DecisionTiming = field(default_factory=DecisionTiming)

    # Finalized flag
    is_finalized: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "is_finalized": self.is_finalized,
            "audio_input": self.audio_input.to_dict(),
            "aec": self.aec.to_dict(),
            "model": self.model.to_dict(),
            "threshold": self.threshold.to_dict(),
            "layers": {name: v.to_dict() for name, v in self.layers.items()},
            "layers_passed": [name for name, v in self.layers.items() if v.passed],
            "layers_failed": [name for name, v in self.layers.items() if not v.passed],
            "decision": self.decision.value,
            "blocking_layer": self.blocking_layer,
            "classification": self.classification.to_dict(),
            "context": self.context.to_dict(),
            "timing": self.timing.to_dict(),
        }


# --------------------------------------------------------------------------- #
# Decision Tracer                                                               #
# --------------------------------------------------------------------------- #


class DecisionTracer:
    """
    Comprehensive forensic tracer for wake word decisions.

    Creates detailed traces that capture every aspect of the
    decision-making process for post-mortem analysis.

    Thread-safe for concurrent access.
    """

    # Configuration
    MAX_TRACES = 200  # Keep last N traces
    MAX_PENDING_MS = 5000  # Abandon pending traces after 5s

    def __init__(self, max_traces: int = MAX_TRACES) -> None:
        """
        Initialize decision tracer.

        Args:
            max_traces: Maximum number of traces to retain
        """
        self._lock = threading.RLock()
        self._traces: deque[WakeDecisionTrace] = deque(maxlen=max_traces)
        self._pending: dict[str, WakeDecisionTrace] = {}
        self._max_traces = max_traces

        logger.info("DecisionTracer initialized: max_traces=%d", max_traces)

    def start_trace(self, correlation_id: str | None = None) -> str:
        """
        Start a new decision trace.

        Call this when the wake model first fires to begin
        capturing the full decision context.

        Args:
            correlation_id: External correlation ID (generates one if None)

        Returns:
            Trace ID for subsequent calls
        """
        trace_id = str(uuid.uuid4())[:8]
        corr_id = correlation_id or trace_id

        trace = WakeDecisionTrace(
            trace_id=trace_id,
            correlation_id=corr_id,
            timestamp=time.time(),
        )
        trace.timing.start_time = time.time()

        with self._lock:
            self._pending[trace_id] = trace
            # Clean up stale pending traces
            self._cleanup_stale_pending()

        logger.debug("Started trace %s (correlation=%s)", trace_id, corr_id)
        return trace_id

    def _cleanup_stale_pending(self) -> None:
        """Remove pending traces older than MAX_PENDING_MS."""
        now = time.time()
        cutoff = now - (self.MAX_PENDING_MS / 1000.0)

        stale = [tid for tid, trace in self._pending.items() if trace.timestamp < cutoff]
        for tid in stale:
            del self._pending[tid]
            logger.debug("Abandoned stale trace %s", tid)

    def record_audio_state(
        self,
        trace_id: str,
        mic_rms: float = 0.0,
        mic_peak: int = 0,
        is_clipping: bool = False,
        noise_floor_rms: float = 0.0,
        snr_db: float = 0.0,
        sample_rate: int = 16000,
        buffer_latency_ms: float = 0.0,
    ) -> None:
        """Record audio input state for a trace."""
        with self._lock:
            trace = self._pending.get(trace_id)
            if trace is None:
                return

            trace.audio_input = AudioInputState(
                mic_rms=mic_rms,
                mic_peak=mic_peak,
                is_clipping=is_clipping,
                noise_floor_rms=noise_floor_rms,
                snr_db=snr_db,
                sample_rate=sample_rate,
                buffer_latency_ms=buffer_latency_ms,
            )

    def record_aec_state(
        self,
        trace_id: str,
        backend: str = "Unknown",
        is_active: bool = False,
        delay_ms: float = 0.0,
        reference_rms: float = 0.0,
        post_aec_rms: float = 0.0,
        reduction_ratio: float = 0.0,
        correlation: float = 0.0,
        gating_active: bool = False,
        converged: bool = False,
    ) -> None:
        """Record AEC state for a trace."""
        with self._lock:
            trace = self._pending.get(trace_id)
            if trace is None:
                return

            trace.aec = AECState(
                backend=backend,
                is_active=is_active,
                delay_ms=delay_ms,
                reference_rms=reference_rms,
                post_aec_rms=post_aec_rms,
                reduction_ratio=reduction_ratio,
                correlation=correlation,
                gating_active=gating_active,
                converged=converged,
            )

    def record_model_output(
        self,
        trace_id: str,
        raw_score: float,
        inference_ms: float = 0.0,
        model_version: str = "",
        feature_extraction_backend: str = "",
    ) -> None:
        """Record model inference output for a trace."""
        with self._lock:
            trace = self._pending.get(trace_id)
            if trace is None:
                return

            trace.model = ModelOutput(
                raw_score=raw_score,
                inference_ms=inference_ms,
                model_version=model_version,
                feature_extraction_backend=feature_extraction_backend,
            )

    def record_threshold_breakdown(
        self,
        trace_id: str,
        base_threshold: float,
        effective_threshold: float,
        playback_boost: float = 1.0,
        playback_boost_reason: str = "",
        volume_boost: float = 1.0,
        volume_boost_reason: str = "",
        snr_boost: float = 1.0,
        snr_boost_reason: str = "",
        echo_boost: float = 1.0,
        echo_boost_reason: str = "",
        barge_in_adjustment: float = 0.0,
        barge_in_reason: str = "",
        learned_offset: float = 0.0,
        learned_offset_reason: str = "",
        clamped: bool = False,
        clamp_reason: str = "",
    ) -> None:
        """Record threshold computation breakdown for a trace."""
        with self._lock:
            trace = self._pending.get(trace_id)
            if trace is None:
                return

            trace.threshold = ThresholdBreakdown(
                base_threshold=base_threshold,
                playback_boost=playback_boost,
                playback_boost_reason=playback_boost_reason,
                volume_boost=volume_boost,
                volume_boost_reason=volume_boost_reason,
                snr_boost=snr_boost,
                snr_boost_reason=snr_boost_reason,
                echo_boost=echo_boost,
                echo_boost_reason=echo_boost_reason,
                barge_in_adjustment=barge_in_adjustment,
                barge_in_reason=barge_in_reason,
                learned_offset=learned_offset,
                learned_offset_reason=learned_offset_reason,
                effective_threshold=effective_threshold,
                clamped=clamped,
                clamp_reason=clamp_reason,
            )

    def record_layer_result(
        self,
        trace_id: str,
        layer_name: str,
        passed: bool,
        reason: str | None = None,
        metrics: dict[str, Any] | None = None,
        margin: float | None = None,
        evaluation_ms: float = 0.0,
    ) -> None:
        """Record a layer evaluation result for a trace."""
        with self._lock:
            trace = self._pending.get(trace_id)
            if trace is None:
                return

            trace.layers[layer_name] = LayerVerdict(
                layer_name=layer_name,
                passed=passed,
                reason=reason,
                metrics=metrics or {},
                margin=margin,
                evaluation_ms=evaluation_ms,
            )

    def record_context(
        self,
        trace_id: str,
        is_playback_active: bool = False,
        playback_volume: int = 0,
        is_playing_music: bool = False,
        is_tts_speaking: bool = False,
        is_listening_active: bool = False,
        listening_start_time: float | None = None,
        ambient_noise_level: str = "normal",
    ) -> None:
        """Record decision context for a trace."""
        with self._lock:
            trace = self._pending.get(trace_id)
            if trace is None:
                return

            trace.context = DecisionContext(
                is_playback_active=is_playback_active,
                playback_volume=playback_volume,
                is_playing_music=is_playing_music,
                is_tts_speaking=is_tts_speaking,
                is_listening_active=is_listening_active,
                listening_start_time=listening_start_time,
                ambient_noise_level=ambient_noise_level,
            )

    def finalize_trace(
        self,
        trace_id: str,
        decision: str,
        blocking_layer: str | None = None,
        classification: str = "unknown",
        classification_confidence: float = 0.0,
        classification_evidence: dict[str, Any] | None = None,
    ) -> WakeDecisionTrace | None:
        """
        Finalize a trace and move it to completed traces.

        Args:
            trace_id: The trace ID
            decision: Final decision ("approved", "blocked", "below_threshold")
            blocking_layer: Layer that blocked (if blocked)
            classification: Trigger classification
            classification_confidence: Confidence in classification
            classification_evidence: Evidence for classification

        Returns:
            The finalized trace, or None if trace not found
        """
        with self._lock:
            trace = self._pending.pop(trace_id, None)
            if trace is None:
                logger.warning("Cannot finalize unknown trace: %s", trace_id)
                return None

            # Set decision
            try:
                trace.decision = DecisionOutcome(decision)
            except ValueError:
                trace.decision = DecisionOutcome.BELOW_THRESHOLD

            trace.blocking_layer = blocking_layer

            # Set classification
            try:
                trace.classification.category = TriggerClassification(classification)
            except ValueError:
                trace.classification.category = TriggerClassification.UNKNOWN

            trace.classification.confidence = classification_confidence
            trace.classification.evidence = classification_evidence or {}

            # Calculate timing
            end_time = time.time()
            trace.timing.total_ms = (end_time - trace.timing.start_time) * 1000

            trace.is_finalized = True

            # Add to completed traces
            self._traces.append(trace)

            logger.debug(
                "Finalized trace %s: decision=%s, layers_passed=%d, layers_failed=%d",
                trace_id,
                decision,
                len([v for v in trace.layers.values() if v.passed]),
                len([v for v in trace.layers.values() if not v.passed]),
            )

            return trace

    def get_trace(self, trace_id: str) -> WakeDecisionTrace | None:
        """Get a specific trace by ID."""
        with self._lock:
            # Check pending first
            if trace_id in self._pending:
                return self._pending[trace_id]

            # Check completed traces
            for trace in self._traces:
                if trace.trace_id == trace_id:
                    return trace

            return None

    def get_recent_traces(
        self,
        n: int = 20,
        outcome: str | None = None,
    ) -> list[WakeDecisionTrace]:
        """
        Get recent completed traces.

        Args:
            n: Maximum number of traces to return
            outcome: Filter by outcome ("approved", "blocked", "below_threshold")

        Returns:
            List of traces (newest first)
        """
        with self._lock:
            traces = list(self._traces)

            if outcome is not None:
                try:
                    outcome_enum = DecisionOutcome(outcome)
                    traces = [t for t in traces if t.decision == outcome_enum]
                except ValueError:
                    pass

            return list(reversed(traces[-n:]))

    def get_traces_by_correlation(self, correlation_id: str) -> list[WakeDecisionTrace]:
        """Get all traces for a correlation ID."""
        with self._lock:
            return [t for t in self._traces if t.correlation_id == correlation_id]

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostics summary for API exposure."""
        with self._lock:
            completed = list(self._traces)

            # Count by outcome
            outcome_counts = {
                "approved": 0,
                "blocked": 0,
                "below_threshold": 0,
            }
            for trace in completed:
                outcome_counts[trace.decision.value] += 1

            # Count by blocking layer
            blocking_layer_counts: dict[str, int] = {}
            for trace in completed:
                if trace.blocking_layer:
                    blocking_layer_counts[trace.blocking_layer] = blocking_layer_counts.get(trace.blocking_layer, 0) + 1

            return {
                "completed_traces": len(completed),
                "pending_traces": len(self._pending),
                "max_traces": self._max_traces,
                "outcome_counts": outcome_counts,
                "blocking_layer_counts": blocking_layer_counts,
                "recent_traces": [t.to_dict() for t in self.get_recent_traces(5)],
            }

    def reset(self) -> None:
        """Reset all traces."""
        with self._lock:
            self._traces.clear()
            self._pending.clear()

        logger.info("DecisionTracer reset")


# --------------------------------------------------------------------------- #
# Singleton Instance                                                           #
# --------------------------------------------------------------------------- #

_tracer: DecisionTracer | None = None
_tracer_lock = threading.Lock()


def get_decision_tracer(max_traces: int = DecisionTracer.MAX_TRACES) -> DecisionTracer:
    """
    Get the global decision tracer instance.

    Args:
        max_traces: Maximum traces to retain (only used on first call)

    Returns:
        Global DecisionTracer instance
    """
    global _tracer
    with _tracer_lock:
        if _tracer is None:
            _tracer = DecisionTracer(max_traces=max_traces)
        return _tracer


def reset_decision_tracer() -> None:
    """Reset the global decision tracer (for testing)."""
    global _tracer
    with _tracer_lock:
        if _tracer is not None:
            _tracer.reset()


__all__ = [
    "AECState",
    "AudioInputState",
    "ClassificationResult",
    "DecisionContext",
    "DecisionOutcome",
    "DecisionTiming",
    "DecisionTracer",
    "LayerVerdict",
    "ModelOutput",
    "ThresholdBreakdown",
    "TriggerClassification",
    "WakeDecisionTrace",
    "get_decision_tracer",
    "reset_decision_tracer",
]
