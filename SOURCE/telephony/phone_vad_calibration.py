"""Live VAD calibration instrumentation for the inbound phone leg.

Why this exists: the barge-in over-yield (a real call's background/echo audio
flushing Viola's pending turn) is a turn-start discrimination problem. The gate in
``pipecat.audio.vad.vad_analyzer`` is ``confidence >= params.confidence AND
volume >= params.min_volume`` (an AND of a Silero confidence and a smoothed volume),
and the phone leg currently runs it at ``confidence=0.3`` with ``min_volume`` and
``start_secs`` left at framework defaults (0.6 / 0.2s).

Setting those three constants correctly requires the LIVE per-turn distribution of
(confidence, volume, duration) for real recipient turns vs background, because the
persisted post-disclosure recording is attenuated and cannot be trusted for absolute
volume. A raw-ONNX offline measurement is also a footgun (see
memory: silero-vad-8k-context-footgun / the 2026-07-04 investigation): bare
256-sample chunks with no context/state carry collapse 8kHz scores to ~0.2 and look
like false degeneracy.

So this analyzer is a NON-BEHAVIORAL wrapper: it computes exactly what the base
analyzer computes (no extra model runs, no changed thresholds) and forwards a
rate-limited sample of (confidence, volume, state) to a callback so the live values
land in the phone latency trace. The turn-taking behavior is byte-for-byte the base
analyzer's. Threshold changes are a SEPARATE, later change driven by the data this
produces.
"""

from __future__ import annotations

from typing import Callable

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

from core.logging_config import get_logger

logger = get_logger(__name__)

# Emit at most one non-transition sample per this many computed VAD frames
# (~0.5s at 8kHz/512-frame cadence). Every state transition is always emitted.
_SAMPLE_EVERY_N = 16

# Callback receives keyword-only calibration fields; must never raise into the
# audio path (the wrapper swallows callback errors).
VADSampleCallback = Callable[..., None]


class CalibratingSileroVADAnalyzer(SileroVADAnalyzer):
    """SileroVADAnalyzer that forwards live (confidence, volume, state) samples.

    Behavior is identical to the base analyzer; this only observes. The same
    ``VADParams`` drive detection, so removing the callback restores the exact
    base analyzer.
    """

    def __init__(
        self,
        *,
        sample_rate: int | None = None,
        params: VADParams | None = None,
        on_sample: VADSampleCallback | None = None,
    ) -> None:
        super().__init__(sample_rate=sample_rate, params=params)
        self._on_sample = on_sample
        self._last_confidence: float = 0.0
        self._last_volume: float = 0.0
        self._conf_fresh: bool = False
        self._prev_state: VADState | None = None
        self._frames_since_emit: int = 0

    def voice_confidence(self, buffer) -> float:
        """Stash the freshly-computed confidence (single base model run)."""
        confidence = super().voice_confidence(buffer)
        self._last_confidence = confidence
        self._conf_fresh = True
        return confidence

    def _get_smoothed_volume(self, audio: bytes) -> float:
        """Stash the smoothed volume the base gate compares against min_volume."""
        volume = super()._get_smoothed_volume(audio)
        self._last_volume = volume
        return volume

    async def analyze_audio(self, buffer: bytes) -> VADState:
        self._conf_fresh = False
        state = await super().analyze_audio(buffer)

        # Only sample when the base actually recomputed confidence this call.
        if self._conf_fresh and self._on_sample is not None:
            is_transition = state != self._prev_state
            self._frames_since_emit += 1
            if is_transition or self._frames_since_emit >= _SAMPLE_EVERY_N:
                self._frames_since_emit = 0
                try:
                    self._on_sample(
                        confidence=round(float(self._last_confidence), 4),
                        volume=round(float(self._last_volume), 4),
                        state=state.name if hasattr(state, "name") else str(state),
                        confidence_threshold=self._params.confidence,
                        min_volume=self._params.min_volume,
                        is_transition=is_transition,
                    )
                except Exception:  # never let instrumentation break the audio path
                    logger.exception("VAD calibration callback failed")

        self._prev_state = state
        return state
