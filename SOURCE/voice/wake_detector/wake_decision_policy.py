"""
Wake Decision Policy
====================

Central authority for all wake word detection decisions.

5-GATE PIPELINE (VAD gate added 2026-03-25, launch threshold tuned 2026-06-05)
==================================================================================

Gate 1: Zero-Input Guard - skip inference on garbage data (mic_rms < 1.0)
Gate 2: Score Threshold  - score >= 0.90 (quiet/playback floor)
Gate 3: VAD Gate         - Silero VAD blocks non-speech (confidence < 0.05)
Gate 4: Cooldown         - 2000ms debounce between triggers
Gate 5: Listening Gate   - block during voice command processing

Threshold raised from 0.50 to 0.80 on 2026-03-11 after a false activation flood
(13 FPs in 3 minutes at 0.50). Launch defaults use 0.90 after the 2026-06-05
fixture battery showed 0.95 playback gating missed real "Viola" clips while
0.90 still rejected the current music and near-miss fixtures.

Confirmation gate (2-of-3) was tried and reverted: the model produces single-frame
score spikes for both true and false detections, so frame-consistency checking
blocks real wake words as much as false ones.

Usage:
    from voice.wake_detector.wake_decision_policy import get_wake_policy, WakeContext

    policy = get_wake_policy()
    context = WakeContext(wake_score=0.92, mic_rms=1200)
    decision = policy.final_trigger_decision(context)
"""

from __future__ import annotations

import threading
import time as _time_mod
from typing import TYPE_CHECKING, Any

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger
from core.quiet_hours import is_quiet_hours, wake_threshold_for_now
from voice.wake_detector.wake.config import WakePolicyConfig
from voice.wake_detector.wake.context import WakeContext, WakeDecisionResult

if TYPE_CHECKING:
    from config import AppConfig

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Silero VAD constants
# ---------------------------------------------------------------------------
# Threshold below which VAD blocks a wake trigger (speech unlikely). This is a
# guardrail, not a speech-quality gate: real short "Viola" fixtures can produce
# low Silero probabilities even when the wake model score is strong.
_VAD_BLOCK_THRESHOLD: float = 0.05
# Silero VAD accepts 512 samples at 16 kHz (32 ms window)
_SILERO_WINDOW_SAMPLES: int = 512


def _check_state_hub_listening() -> bool:
    """
    Check if the central StateHub indicates listening mode.

    Defence-in-depth: even if local _is_listening_active fails to update,
    the StateHub may have the correct state.
    """
    try:
        from core.state_hub import get_state_hub
        from core.unified_state import VoiceMode

        hub = get_state_hub()
        if hub is None:
            return False
        state = hub.get_state()
        return state.voice.mode != VoiceMode.IDLE
    except Exception:
        return False


class WakeDecisionPolicy:
    """
    Central authority for wake word detection decisions.

    Implements a 5-gate pipeline:
      1. Zero-Input Guard   — reject garbage audio
      2. Score Threshold    - score >= 0.90 launch floor
      3. VAD Gate           - Silero VAD blocks non-speech
      4. Cooldown           - debounce between triggers
      5. Listening Gate     - block during voice command processing
    """

    def __init__(self, config: WakePolicyConfig | None = None):
        self._config = config or WakePolicyConfig()
        self._lock = threading.RLock()

        # Gate 3: Cooldown state
        self._last_trigger_time: float = 0.0

        # Gate 4: Listening state — when True, wake triggers are blocked
        self._is_listening_active = False

        # --- API-compatible state (kept for callers/diagnostics) ---
        self._is_playback_active = False
        self._playback_volume = 80
        self._latest_loopback_rms: float = 0.0
        self._latest_mic_rms: float = 0.0
        self._latest_post_aec_rms: float = 0.0
        self._latest_correlation: float = 0.0

        # Kept for listener code that reads _echo_state.echo_gating_active
        self._echo_state = _EchoStateStub()

        # --- Silero VAD (lazy-loaded) ---
        self._vad_model: object | None = None
        self._vad_load_lock = threading.Lock()
        self._vad_load_failed: bool = False

        logger.info(
            "WakeDecisionPolicy initialized (5-gate, Silero VAD): "
            "threshold=%.2f playback_threshold=%.2f cooldown=%dms vad_block=%.2f",
            self._config.base_threshold,
            self._config.playback_threshold,
            self._config.cooldown_ms,
            _VAD_BLOCK_THRESHOLD,
        )

    # ------------------------------------------------------------------ #
    # Gate 4: Listening state management                                   #
    # ------------------------------------------------------------------ #

    def set_listening_active(self, is_listening: bool) -> None:
        """Block wake triggers during voice command processing."""
        with self._lock:
            if self._is_listening_active != is_listening:
                logger.info(
                    "LISTENING_STATE_CHANGE: %s -> %s",
                    "active" if self._is_listening_active else "idle",
                    "active" if is_listening else "idle",
                )
            self._is_listening_active = is_listening

    @property
    def is_listening_active(self) -> bool:
        with self._lock:
            return self._is_listening_active

    # ------------------------------------------------------------------ #
    # Playback state (tracked for threshold gating and diagnostics)       #
    # ------------------------------------------------------------------ #

    def set_playback_active(self, is_active: bool, volume: int = 80) -> None:
        """Update playback state - used by effective_threshold and diagnostics."""
        with self._lock:
            self._is_playback_active = is_active
            self._playback_volume = volume

    @property
    def is_playback_active(self) -> bool:
        with self._lock:
            return self._is_playback_active

    # ------------------------------------------------------------------ #
    # Audio context (kept for diagnostics, not used by gates)              #
    # ------------------------------------------------------------------ #

    def update_audio_context(
        self,
        loopback_rms: float,
        correlation: float,
        mic_rms: float = 0.0,
        post_aec_rms: float = 0.0,
    ) -> None:
        """Update real-time audio context (for diagnostics only)."""
        with self._lock:
            self._latest_loopback_rms = loopback_rms
            self._latest_mic_rms = mic_rms
            self._latest_post_aec_rms = post_aec_rms
            self._latest_correlation = correlation

    def update_echo_state(self, loopback_rms: float, correlation: float) -> bool:
        """No-op — echo veto removed. Kept for API compatibility."""
        return False

    # ------------------------------------------------------------------ #
    # Threshold (fixed — no dynamic boosts)                                #
    # ------------------------------------------------------------------ #

    def effective_threshold(self, context: WakeContext) -> float:
        """Return the threshold, boosted during active playback and quiet hours."""
        with self._lock:
            playback_active = self._is_playback_active
        if context.loopback_rms > self._config.playback_presence_rms_threshold or playback_active:
            return wake_threshold_for_now(max(self._config.base_threshold, self._config.playback_threshold))
        return wake_threshold_for_now(self._config.base_threshold)

    def get_threshold_breakdown(self, context: WakeContext) -> dict[str, Any]:
        """Diagnostic breakdown of threshold computation."""
        eff = self.effective_threshold(context)
        playback_active = context.loopback_rms > self._config.playback_presence_rms_threshold
        return {
            "base_threshold": self._config.base_threshold,
            "playback_threshold": self._config.playback_threshold,
            "playback_active": playback_active,
            "quiet_hours_active": is_quiet_hours(),
            "effective_threshold": eff,
            "is_boosted": playback_active or eff > self._config.base_threshold,
        }

    # ------------------------------------------------------------------ #
    # VAD helpers — Silero VAD for speech/non-speech discrimination       #
    # ------------------------------------------------------------------ #

    def _ensure_vad_model(self) -> bool:
        """Lazily load Silero VAD model on first use. Thread-safe.

        Returns True if the model is ready, False on load failure.
        """
        if self._vad_model is not None:
            return True

        with self._vad_load_lock:
            # Double-check after acquiring lock
            if self._vad_model is not None:
                return True

            try:
                from voice.vad.silero_onnx import create_silero_vad

                t0 = _time_mod.perf_counter()
                model = create_silero_vad()
                if model is None:
                    raise RuntimeError("Silero VAD ONNX not available")
                elapsed_ms = (_time_mod.perf_counter() - t0) * 1000
                self._vad_model = model
                logger.info(
                    "Silero VAD loaded for wake policy in %.0f ms",
                    elapsed_ms,
                )
                return True
            except Exception:
                logger.exception("Failed to load Silero VAD for wake policy")
                self._vad_load_failed = True
                return False

    def compute_vad_confidence(self, audio_int16: np.ndarray | None = None, **kwargs: Any) -> float:
        """Run Silero VAD on an int16 audio chunk and return speech probability.

        For short inputs (<= 1536 samples), scores a single window.
        For longer inputs (e.g. the 1.5s wake buffer), slides a 512-sample
        window across the audio and returns the **maximum** speech probability.
        This ensures the VAD detects speech regardless of where it falls in
        the buffer — critical because the wake model scores a 1.5s ring
        buffer where the utterance may be at any position.

        Args:
            audio_int16: 16 kHz mono int16 numpy array. If None, returns 1.0
                         (no blocking).

        Returns:
            Speech probability in [0.0, 1.0].  Returns 1.0 (pass-through)
            when audio is unavailable or the model failed to load.
        """
        if audio_int16 is None or len(audio_int16) == 0:
            return 1.0

        if self._vad_load_failed:
            return 1.0

        if not self._ensure_vad_model():
            return 1.0

        try:
            # Convert int16 -> float32 normalised to [-1, 1]
            audio_f32 = audio_int16.astype(np.float32) / 32768.0

            W = _SILERO_WINDOW_SAMPLES  # 512

            if len(audio_f32) < W:
                window = np.zeros(W, dtype=np.float32)
                window[-len(audio_f32) :] = audio_f32
                return self._vad_model(window, SAMPLE_RATE_16K)

            if len(audio_f32) <= W * 3:
                return self._vad_model(audio_f32[-W:], SAMPLE_RATE_16K)

            # Long input (e.g. 24000-sample wake buffer): slide and take max.
            best = 0.0
            for start in range(0, len(audio_f32) - W + 1, W):
                prob = self._vad_model(audio_f32[start : start + W], SAMPLE_RATE_16K)
                if prob > best:
                    best = prob
                    if best >= 0.5:
                        break
            return best
        except Exception:
            logger.debug("Silero VAD inference failed, returning 1.0", exc_info=True)
            return 1.0

    def estimate_vad_from_audio(
        self,
        audio_int16: np.ndarray | None = None,
        **kwargs: Any,
    ) -> float:
        """Estimate speech probability from raw audio.

        This is the primary entry point called by violawake_listener on each
        wake-engine detection.  It delegates to :meth:`compute_vad_confidence`.

        Args:
            audio_int16: Recent microphone audio as int16 numpy array at 16 kHz.
                         Older RMS-only kwargs (mic_rms, loopback_rms, etc.)
                         are accepted for backward compatibility but ignored
                         when audio_int16 is provided.

        Returns:
            Speech probability in [0.0, 1.0].
        """
        return self.compute_vad_confidence(audio_int16=audio_int16)

    def get_vad_backend(self) -> str:
        if self._vad_model is not None:
            return "silero"
        if self._vad_load_failed:
            return "silero_failed"
        return "silero_pending"

    def should_baseline_vad_block(self, context: WakeContext) -> bool:
        """Return True when VAD indicates non-speech (confidence < threshold)."""
        return context.vad_confidence < _VAD_BLOCK_THRESHOLD

    def should_vad_block(self, context: WakeContext) -> bool:
        """Return True when VAD indicates non-speech (confidence < threshold).

        This is checked by callers to decide whether to suppress a wake
        trigger that scored above the model threshold but is likely
        not human speech (e.g. music transient, TV audio).
        """
        return context.vad_confidence < _VAD_BLOCK_THRESHOLD

    def should_echo_veto(self, context: WakeContext) -> bool:
        return False

    def should_confirm(self, context: WakeContext) -> bool:
        return False

    def update_snr_estimate(self, audio_rms: float, is_speech: bool) -> float | None:
        return None

    def update_barge_in_state(self, mic_rms: float, vad_confidence: float, correlation: float = 0.0) -> bool:
        return False

    def record_detection(self, score: float, timestamp: float | None = None, **kwargs: Any) -> None:
        """No-op — confirmation window removed."""

    def clear_confirmation_window(self) -> None:
        """No-op — confirmation window removed."""

    # ------------------------------------------------------------------ #
    # CORE: 5-gate final trigger decision                                  #
    # ------------------------------------------------------------------ #

    def final_trigger_decision(self, context: WakeContext) -> WakeDecisionResult:
        """
        Make the authoritative trigger decision using 5 gates.

        Gate 1: Zero-Input Guard   - reject garbage audio
        Gate 2: Score Threshold    - score >= threshold
        Gate 3: VAD Gate           - block when Silero VAD says non-speech
        Gate 4: Cooldown           - debounce between triggers
        Gate 5: Listening Gate     - block during voice command processing
        """
        threshold = self.effective_threshold(context)
        result = WakeDecisionResult(
            should_trigger=False,
            effective_threshold=threshold,
        )

        # Gate 4: Listening Gate (checked first — most important for race prevention)
        with self._lock:
            local_listening = self._is_listening_active
        hub_listening = _check_state_hub_listening()

        if local_listening or hub_listening:
            source = []
            if local_listening:
                source.append("local_flag")
            if hub_listening:
                source.append("state_hub")
            result.blocking_reason = "LISTENING_BLOCK: System is processing voice command (source=%s)" % ",".join(
                source
            )
            result.layers_failed.append("listening_gate")
            logger.info(
                "LISTENING_STATE: Wake blocked - local=%s, hub=%s, score=%.3f",
                local_listening,
                hub_listening,
                context.wake_score,
            )
            return result

        result.layers_passed.append("listening_gate")

        # Gate 1: Zero-Input Guard
        if context.mic_rms < self._config.zero_input_rms_threshold:
            result.blocking_reason = "Zero input: mic_rms=%.1f < %.1f" % (
                context.mic_rms,
                self._config.zero_input_rms_threshold,
            )
            result.layers_failed.append("zero_input")
            return result

        result.layers_passed.append("zero_input")

        # Gate 2: Score Threshold (0.90 launch floor)
        if context.wake_score < threshold:
            result.blocking_reason = "Score %.3f < threshold %.3f" % (
                context.wake_score,
                threshold,
            )
            result.layers_failed.append("primary_score")
            return result

        result.layers_passed.append("primary_score")

        # Gate 3: VAD Gate — block non-speech triggers (e.g. music transients)
        if self.should_vad_block(context):
            result.blocking_reason = "VAD block: speech_prob=%.3f < %.3f" % (
                context.vad_confidence,
                _VAD_BLOCK_THRESHOLD,
            )
            result.vad_blocked = True
            result.layers_failed.append("vad_gate")
            logger.info(
                "Wake blocked by VAD: speech_prob=%.3f, threshold=%.3f, score=%.3f",
                context.vad_confidence,
                _VAD_BLOCK_THRESHOLD,
                context.wake_score,
            )
            return result

        result.layers_passed.append("vad_gate")

        # Gate 4: Cooldown
        cooldown_seconds = self._config.cooldown_ms / 1000.0
        now = context.timestamp
        with self._lock:
            elapsed = now - self._last_trigger_time

        if elapsed < cooldown_seconds:
            result.blocking_reason = "Cooldown: %.1fs elapsed < %.1fs required" % (
                elapsed,
                cooldown_seconds,
            )
            result.layers_failed.append("cooldown")
            return result

        result.layers_passed.append("cooldown")

        # All 5 gates passed — approve trigger
        with self._lock:
            self._last_trigger_time = now

        result.should_trigger = True
        logger.info(
            "Wake trigger approved: score=%.3f, threshold=%.3f, layers=%s",
            context.wake_score,
            threshold,
            result.layers_passed,
        )
        return result

    # ------------------------------------------------------------------ #
    # Utility                                                              #
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """Reset all policy state."""
        with self._lock:
            self._last_trigger_time = 0.0
            self._latest_loopback_rms = 0.0
            self._latest_mic_rms = 0.0
            self._latest_post_aec_rms = 0.0
            self._latest_correlation = 0.0

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostic information about current policy state."""
        with self._lock:
            return {
                "is_playback_active": self._is_playback_active,
                "playback_volume": self._playback_volume,
                "echo_gating_active": False,
                "consecutive_high_correlation": 0,
                "recent_detections_count": 0,
                "is_listening_active": self._is_listening_active,
                "latest_loopback_rms": self._latest_loopback_rms,
                "latest_mic_rms": self._latest_mic_rms,
                "latest_post_aec_rms": self._latest_post_aec_rms,
                "latest_correlation": self._latest_correlation,
                "has_audible_playback_realtime": self._latest_loopback_rms
                > self._config.playback_presence_rms_threshold,
                "vad_backend": self.get_vad_backend(),
                "vad_block_threshold": _VAD_BLOCK_THRESHOLD,
                "config": {
                    "base_threshold": self._config.base_threshold,
                    "cooldown_ms": self._config.cooldown_ms,
                    "zero_input_rms_threshold": self._config.zero_input_rms_threshold,
                    "playback_threshold": self._config.playback_threshold,
                },
            }

    def get_authoritative_playback_state(self) -> dict[str, Any]:
        """Get consolidated playback state (diagnostics only)."""
        with self._lock:
            rms = self._latest_loopback_rms
            return {
                "is_playing": rms > self._config.playback_presence_rms_threshold,
                "source": "rms",
                "rms_indicates_playing": rms > self._config.playback_presence_rms_threshold,
                "callback_indicates_playing": self._is_playback_active,
                "disagreement": False,
                "rms_value": rms,
                "rms_threshold": self._config.playback_presence_rms_threshold,
                "volume": self._playback_volume,
            }


class _EchoStateStub:
    """Stub for listener code that reads _echo_state.echo_gating_active."""

    echo_gating_active: bool = False
    consecutive_high_correlation: int = 0


# --------------------------------------------------------------------------- #
# Global Policy Instance                                                       #
# --------------------------------------------------------------------------- #

_global_policy: WakeDecisionPolicy | None = None
_policy_lock = threading.Lock()


def get_wake_policy(config: AppConfig | None = None) -> WakeDecisionPolicy:
    """
    Get the global wake decision policy instance (singleton).

    Creates the policy on first call. If config is provided on first call,
    it will be used to configure the policy.
    """
    global _global_policy

    with _policy_lock:
        if _global_policy is None:
            policy_config = WakePolicyConfig.from_app_config(config) if config is not None else WakePolicyConfig()
            _global_policy = WakeDecisionPolicy(policy_config)
        return _global_policy


def reset_wake_policy() -> None:
    """Reset the global policy instance (primarily for testing)."""
    global _global_policy
    with _policy_lock:
        if _global_policy is not None:
            _global_policy.reset()


def set_wake_sensitivity(threshold: float) -> bool:
    """Point the running policy at a new base threshold.

    The policy's config is built once from AppConfig, so without this a user
    who changed ``wake_sensitivity`` — in the Settings UI or by asking Viola —
    kept the old threshold until the next restart. ``base_threshold`` is read
    fresh on every decision (see ``effective_threshold``), so updating it here
    changes the very next wake decision.

    Returns True when a live policy was updated, False when none exists yet
    (in which case the value is picked up when the policy is first built).
    """
    global _global_policy
    with _policy_lock:
        if _global_policy is None:
            return False
        _global_policy._config.base_threshold = float(threshold)
        return True


def create_wake_policy(config: AppConfig) -> WakeDecisionPolicy:
    """
    Create a new wake decision policy from config.

    Use this when you need a dedicated policy instance (e.g., for testing).
    For normal operation, use get_wake_policy() for the global instance.
    """
    policy_config = WakePolicyConfig.from_app_config(config)
    return WakeDecisionPolicy(policy_config)


__all__ = [
    "WakeContext",
    "WakeDecisionPolicy",
    "WakeDecisionResult",
    "WakePolicyConfig",
    "create_wake_policy",
    "get_wake_policy",
    "reset_wake_policy",
    "set_wake_sensitivity",
]
