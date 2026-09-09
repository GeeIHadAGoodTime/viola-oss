# WARNING: Do NOT apply noise suppression upstream of ViolaWake.
# ViolaWake's MLP was trained on raw (post-AEC) audio. Noise suppression
# alters the spectral envelope, causing training-serving skew that degrades
# wake word detection. Use noise suppression ONLY in the STT path.
# See: docs/research/MASTER_OSS_ADOPTION_LIST.md — VP-7 notes.

"""RNNoise Lightweight Noise Suppression (VP-7).

Provides a :class:`RNNoiseSuppressor` as a lightweight alternative to
DeepFilterNet, intended for ARM / Raspberry Pi deployments where
DeepFilterNet's model is too heavy.

RNNoise uses a compact recurrent neural network (~85 KB) for real-time
noise suppression with minimal CPU overhead.  It processes 10 ms frames
at 48 kHz internally and handles resampling transparently.

The same public interface as :class:`~audio_core.noise_suppression.NoiseSuppressor`
is exposed so the two backends are interchangeable.

Usage::

    from audio_core.rnnoise_suppressor import RNNoiseSuppressor

    ns = RNNoiseSuppressor(enabled=True)
    cleaned = ns.process(raw_chunk, sample_rate=16000)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from core.constants import SAMPLE_RATE_16K, SAMPLE_RATE_48K
from core.logging_config import get_logger

if TYPE_CHECKING:
    import numpy as np

logger = get_logger(__name__)

# RNNoise operates on 10 ms frames at 48 kHz = 480 samples per frame
_RNNOISE_FRAME_SAMPLES = 480
_RNNOISE_SAMPLE_RATE = SAMPLE_RATE_48K

# Singleton for pipeline-wide reuse
_global_instance: RNNoiseSuppressor | None = None
_global_lock = threading.Lock()


class RNNoiseSuppressor:
    """RNNoise-based noise suppression stage.

    Designed for ARM/Pi deployment where DeepFilterNet is too heavy.
    Uses a manually-installed ``rnnoise`` binding exposing ``RNNoise()`` +
    ``process_frame()`` for lightweight real-time noise suppression. No
    maintained PyPI package currently matches this API (``rnnoise-python``
    does not exist on PyPI — see #3495), so this is not a pip dependency;
    the import degrades gracefully if nothing is installed.

    Parameters
    ----------
    enabled:
        Master switch.  When ``False``, :meth:`process` returns the input
        audio unmodified.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

        # Lazy-loaded state
        self._denoiser: object | None = None
        self._load_attempted = False
        self._load_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        """Whether noise suppression is enabled."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def is_loaded(self) -> bool:
        """Whether the RNNoise model has been loaded successfully."""
        return self._denoiser is not None

    def process(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE_16K) -> np.ndarray:
        """Apply RNNoise suppression to an audio chunk.

        Parameters
        ----------
        audio:
            Raw microphone audio as a 1-D numpy array (int16 or float32).
        sample_rate:
            Sample rate of *audio* in Hz.

        Returns
        -------
        np.ndarray
            Cleaned audio in the same dtype and length as the input.
        """
        import numpy as np

        if not self._enabled:
            return audio

        if not self._ensure_loaded():
            return audio

        original_dtype = audio.dtype
        original_len = len(audio)

        # Convert to float32
        if audio.dtype == np.int16:
            audio_f32 = audio.astype(np.float32) / 32768.0
        elif audio.dtype == np.float32:
            audio_f32 = audio.copy()
        else:
            audio_f32 = audio.astype(np.float32)

        try:
            # Resample to 48 kHz if needed (RNNoise native rate)
            if sample_rate != _RNNOISE_SAMPLE_RATE:
                audio_48k = self._resample(audio_f32, sample_rate, _RNNOISE_SAMPLE_RATE)
            else:
                audio_48k = audio_f32

            # Process in 480-sample (10 ms) frames
            enhanced = self._process_frames(audio_48k)

            # Resample back to original rate
            if sample_rate != _RNNOISE_SAMPLE_RATE:
                enhanced = self._resample(enhanced, _RNNOISE_SAMPLE_RATE, sample_rate)

            # Ensure output length matches input
            if len(enhanced) > original_len:
                enhanced = enhanced[:original_len]
            elif len(enhanced) < original_len:
                enhanced = np.pad(enhanced, (0, original_len - len(enhanced)))

            # Convert back to original dtype
            if original_dtype == np.int16:
                return (np.clip(enhanced, -1.0, 1.0) * 32767).astype(np.int16)
            return enhanced.astype(original_dtype)

        except Exception:
            logger.debug("RNNoise processing failed, passing audio through", exc_info=True)
            return audio

    def release(self) -> None:
        """Release model resources."""
        with self._load_lock:
            self._denoiser = None
            self._load_attempted = False
        logger.info("RNNoiseSuppressor released")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _ensure_loaded(self) -> bool:
        """Load the RNNoise model on first use."""
        if self._denoiser is not None:
            return True

        with self._load_lock:
            if self._denoiser is not None:
                return True
            if self._load_attempted:
                return False

            self._load_attempted = True
            try:
                import rnnoise  # type: ignore[import-untyped]

                self._denoiser = rnnoise.RNNoise()
                logger.info("RNNoise model loaded for lightweight noise suppression")
                return True
            except ImportError:
                logger.warning(
                    "No compatible 'rnnoise' binding is installed — RNNoise suppression "
                    "disabled. There is no maintained PyPI package matching this module's "
                    "API (rnnoise-python does not exist on PyPI; see #3495); install a "
                    "compatible binding manually if you want this backend."
                )
                return False
            except Exception:
                logger.exception("Failed to load RNNoise model")
                return False

    def _process_frames(self, audio: np.ndarray) -> np.ndarray:
        """Process audio in 480-sample frames through RNNoise."""
        import numpy as np

        n = len(audio)
        # Pad to a multiple of frame size
        pad_len = (_RNNOISE_FRAME_SAMPLES - (n % _RNNOISE_FRAME_SAMPLES)) % _RNNOISE_FRAME_SAMPLES
        if pad_len > 0:
            audio = np.pad(audio, (0, pad_len))

        output_frames = []
        for i in range(0, len(audio), _RNNOISE_FRAME_SAMPLES):
            frame = audio[i : i + _RNNOISE_FRAME_SAMPLES]
            # RNNoise expects int16 range [-32768, 32767] as float
            frame_scaled = frame * 32767.0
            try:
                filtered = self._denoiser.process_frame(frame_scaled)  # type: ignore[union-attr]
                output_frames.append(np.array(filtered, dtype=np.float32) / 32767.0)
            except Exception:
                # On error, pass frame through
                output_frames.append(frame)

        result = np.concatenate(output_frames)
        # Remove padding
        return result[:n]

    @staticmethod
    def _resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        """Resample audio using soxr (preferred) or scipy fallback."""
        import numpy as np

        if from_rate == to_rate:
            return audio

        try:
            import soxr  # type: ignore[import-untyped]

            return soxr.resample(audio, from_rate, to_rate, quality="HQ")
        except ImportError:
            pass

        try:
            from scipy.signal import resample

            target_len = int(len(audio) * to_rate / from_rate)
            return resample(audio, target_len).astype(np.float32)
        except ImportError:
            pass

        # Last resort: linear interpolation
        target_len = int(len(audio) * to_rate / from_rate)
        x_old = np.linspace(0, 1, len(audio))
        x_new = np.linspace(0, 1, target_len)
        return np.interp(x_new, x_old, audio).astype(np.float32)


# ---------------------------------------------------------------------- #
# Module-level convenience                                                #
# ---------------------------------------------------------------------- #


def get_rnnoise_suppressor(*, enabled: bool = True) -> RNNoiseSuppressor:
    """Return the global :class:`RNNoiseSuppressor` singleton."""
    global _global_instance
    if _global_instance is None:
        with _global_lock:
            if _global_instance is None:
                _global_instance = RNNoiseSuppressor(enabled=enabled)
    return _global_instance


__all__ = [
    "RNNoiseSuppressor",
    "get_rnnoise_suppressor",
]
