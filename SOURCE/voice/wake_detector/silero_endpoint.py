"""
SileroVAD-based endpoint detection for post-wake voice recording.

Provides accurate turn-boundary detection using a neural voice activity
detector, replacing crude RMS energy thresholding. Falls back to RMS
when the Silero ONNX model is unavailable.

Usage:
    detector = get_silero_endpoint_detector()
    speech_prob = detector.process_chunk(audio_int16_chunk)
    # speech_prob > 0.5 => speech active
    # speech_prob < 0.3 for silence_duration => end of turn
"""

from __future__ import annotations

import threading

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)

# SileroVAD accepts 512 samples at 16 kHz (32ms window)
_SILERO_WINDOW_SAMPLES: int = 512

# Speech detection threshold: above this = speech active
SPEECH_THRESHOLD: float = 0.5

# Silence endpoint threshold: below this for silence_duration = end of turn
SILENCE_THRESHOLD: float = 0.3

# Singleton instance
_instance: SileroEndpointDetector | None = None
_instance_lock = threading.Lock()


class SileroEndpointDetector:
    """Neural VAD endpoint detector using SileroVAD (ONNX).

    Thread-safe, lazy-loads the model on first use. If loading fails,
    all calls to process_chunk() return -1.0 (signals caller to use RMS).
    """

    def __init__(self) -> None:
        self._model = None
        self._load_failed: bool = False
        self._available: bool = False

    @property
    def is_available(self) -> bool:
        """True if VAD model loaded successfully."""
        return self._available

    def _ensure_model(self) -> bool:
        """Lazily load SileroVAD ONNX model. Thread-safe, only tries once."""
        if self._model is not None:
            return True
        if self._load_failed:
            return False

        try:
            import time as _time_mod

            from voice.vad.silero_onnx import create_silero_vad

            t0 = _time_mod.perf_counter()
            model = create_silero_vad()
            if model is None:
                raise RuntimeError("Silero VAD ONNX model not available")
            elapsed_ms = (_time_mod.perf_counter() - t0) * 1000
            self._model = model
            self._available = True
            logger.info("SileroVAD endpoint detector loaded in %.0f ms", elapsed_ms)
            return True
        except Exception:
            logger.warning(
                "SileroVAD unavailable for endpoint detection, " "falling back to RMS-based silence detection",
                exc_info=True,
            )
            self._load_failed = True
            return False

    def process_chunk(self, audio_int16: np.ndarray) -> float:
        """Run VAD on an int16 audio chunk and return speech probability.

        Args:
            audio_int16: 16 kHz mono int16 numpy array (any length).
                Uses the last 512 samples for inference.

        Returns:
            Speech probability [0.0, 1.0].
            Returns -1.0 if model unavailable (signals caller to use RMS).
        """
        if not self._ensure_model():
            return -1.0

        try:
            audio_f32 = audio_int16.astype(np.float32) / 32768.0

            if len(audio_f32) >= _SILERO_WINDOW_SAMPLES:
                window = audio_f32[-_SILERO_WINDOW_SAMPLES:]
            else:
                window = np.zeros(_SILERO_WINDOW_SAMPLES, dtype=np.float32)
                window[-len(audio_f32) :] = audio_f32

            return self._model(window, SAMPLE_RATE_16K)
        except Exception:
            logger.debug(
                "SileroVAD inference failed, signaling fallback",
                exc_info=True,
            )
            return -1.0

    def reset_state(self) -> None:
        """Reset VAD internal state between recordings."""
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                logger.debug("Failed to reset SileroVAD states", exc_info=True)


def get_silero_endpoint_detector() -> SileroEndpointDetector:
    """Get or create the singleton SileroVAD endpoint detector."""
    global _instance
    if _instance is not None:
        return _instance

    with _instance_lock:
        if _instance is not None:
            return _instance
        _instance = SileroEndpointDetector()
        return _instance
