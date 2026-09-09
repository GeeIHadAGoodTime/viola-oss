"""TTS-to-Pipeline format converter.

Converts mono / 16-bit int16 LE PCM from a byte-capable TTS engine to
48 kHz / stereo / 16-bit int16 LE PCM (ChunkStamper pipeline format).

Kokoro currently emits 24 kHz samples, but the source sample rate is an
argument so the bridge follows the engine return value instead of baking
Kokoro's current rate into the pipeline contract.
"""

from __future__ import annotations

import numpy as np

from core.constants import AUDIO_INT16_MAX, SAMPLE_RATE_24K, SAMPLE_RATE_48K
from core.logging_config import get_logger

logger = get_logger(__name__)

_INT16_LE = np.dtype("<i2")


def convert_tts_to_pipeline(pcm_mono: bytes, source_rate: int = SAMPLE_RATE_24K) -> bytes:
    """Convert mono int16 LE PCM to 48 kHz stereo int16 LE PCM.

    Args:
        pcm_mono: Raw PCM bytes, mono, 16-bit signed int16, little-endian.
            Length must be a multiple of 2 (one int16 sample).
        source_rate: Sample rate of ``pcm_mono``.  Kokoro's current native
            rate is 24 kHz, but callers should pass the rate returned by the
            synthesis engine.

    Returns:
        Raw PCM bytes at 48 kHz, stereo, 16-bit signed int16, little-endian.
        An empty input returns empty bytes.
    """
    if not pcm_mono:
        return b""
    if source_rate <= 0:
        logger.warning("Invalid TTS source sample rate: %s", source_rate)
        return b""

    # Decode int16 LE bytes -> numpy int16 array
    samples_in = np.frombuffer(pcm_mono, dtype=_INT16_LE)
    n_in = len(samples_in)

    if n_in == 0:
        return b""

    # Resample source_rate -> 48 kHz using linear interpolation.  For Kokoro's
    # 24 kHz output this is a clean 2x ratio; keeping the generic math avoids
    # another hidden 24 kHz assumption.
    n_out = max(1, round(n_in * SAMPLE_RATE_48K / source_rate))
    if n_in == 1:
        samples_48k_f = np.full(n_out, float(samples_in[0]), dtype=np.float64)
    else:
        x_in = np.arange(n_in, dtype=np.float64) / float(source_rate)
        x_out = np.arange(n_out, dtype=np.float64) / float(SAMPLE_RATE_48K)
        samples_48k_f = np.interp(x_out, x_in, samples_in.astype(np.float64))

    # Clip to int16 range and convert back to int16
    samples_48k = np.clip(samples_48k_f, -AUDIO_INT16_MAX - 1, AUDIO_INT16_MAX).astype(_INT16_LE)

    # Duplicate mono -> stereo: interleave [L, R] for each sample
    stereo = np.empty(n_out * 2, dtype=_INT16_LE)
    stereo[0::2] = samples_48k  # Left channel
    stereo[1::2] = samples_48k  # Right channel (same as left)

    return stereo.tobytes()


__all__ = [
    "convert_tts_to_pipeline",
]
