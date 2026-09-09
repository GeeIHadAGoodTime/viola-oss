"""
Polyphase Resampler for Audio Processing
=========================================

High-quality audio resampling using polyphase filter bank.

DESIGN:
- Uses scipy.signal.resample_poly for polyphase filtering
- Anti-aliasing filter automatically applied during downsampling
- Resampler instances are cached for performance
- Supports both "fast" and "quality" presets

WHY POLYPHASE:
- Linear interpolation (previously used) causes aliasing
- Polyphase filtering preserves signal quality during resampling
- Anti-aliasing filter prevents frequency folding in downsampling

USAGE:
    from voice.wake_detector.resampler import get_resampler, Resampler

    # Get cached resampler
    resampler = get_resampler(48000, 16000, "quality")
    output = resampler.resample(input_audio)

    # Or create directly
    resampler = Resampler(48000, 16000, quality="fast")
    output = resampler.resample(input_audio)
"""

from __future__ import annotations

import threading
from math import gcd
from typing import Literal

import numpy as np

from core.constants import AUDIO_INT16_MAX, AUDIO_INT16_SCALE
from core.logging_config import get_logger

logger = get_logger(__name__)


class Resampler:
    """
    High-quality polyphase resampler.

    Uses scipy.signal.resample_poly internally for proper anti-aliasing.

    Thread Safety:
    - Resample operations are thread-safe (no internal state modified)
    - Can be safely shared across threads
    """

    def __init__(
        self,
        source_rate: int,
        target_rate: int,
        quality: Literal["fast", "quality"] = "quality",
    ) -> None:
        """
        Initialize resampler.

        Args:
            source_rate: Input sample rate in Hz
            target_rate: Output sample rate in Hz
            quality: Resampling quality preset
                    - "fast": Lower quality, faster (good for AEC reference)
                    - "quality": Higher quality, slower (good for detection)
        """
        self._source_rate = source_rate
        self._target_rate = target_rate
        self._quality = quality

        # Compute resampling ratio as integers (for resample_poly)
        # Find GCD to reduce ratio
        divisor = gcd(source_rate, target_rate)
        self._up = target_rate // divisor
        self._down = source_rate // divisor

        # Filter parameters based on quality
        if quality == "fast":
            self._window = ("kaiser", 5.0)
            self._filter_order = 10  # Shorter filter for speed
        else:
            self._window = ("kaiser", 8.6)  # Kaiser beta for ~80dB stopband
            self._filter_order = 20  # Longer filter for quality

        logger.debug(
            "Resampler initialized: %sHz -> %sHz (up=%s, down=%s, quality=%s)",
            source_rate,
            target_rate,
            self._up,
            self._down,
            quality,
        )

    @property
    def source_rate(self) -> int:
        """Source sample rate."""
        return self._source_rate

    @property
    def target_rate(self) -> int:
        """Target sample rate."""
        return self._target_rate

    @property
    def ratio(self) -> float:
        """Resampling ratio (target/source)."""
        return self._target_rate / self._source_rate

    def resample(self, audio: np.ndarray) -> np.ndarray:
        """
        Resample audio to target rate.

        Args:
            audio: Input audio samples (int16 or float32)

        Returns:
            Resampled audio with same dtype as input
        """
        # Identity case
        if self._source_rate == self._target_rate:
            return audio.copy() if audio.flags.writeable else audio

        original_dtype = audio.dtype

        # Convert to float64 for processing (resample_poly expects float)
        if audio.dtype == np.int16:
            audio_float = audio.astype(np.float64) / AUDIO_INT16_SCALE
        elif audio.dtype == np.float32:
            audio_float = audio.astype(np.float64)
        elif audio.dtype == np.float64:
            audio_float = audio
        else:
            audio_float = audio.astype(np.float64)

        try:
            # VP-6: Prefer soxr for high-quality resampling when available
            import soxr  # type: ignore[import-untyped]

            resampled = soxr.resample(
                audio_float,
                self._source_rate,
                self._target_rate,
                quality="HQ",
            )
        except ImportError:
            # Fall back to scipy polyphase resampling
            try:
                from scipy.signal import resample_poly

                resampled = resample_poly(
                    audio_float,
                    self._up,
                    self._down,
                    window=self._window,
                )
            except ImportError:
                # Last resort: simple linear interpolation
                logger.warning("Neither soxr nor scipy available, falling back to linear interpolation")
                resampled = self._linear_resample(audio_float)

        # Convert back to original dtype
        if original_dtype == np.int16:
            # Clip to prevent overflow
            resampled = np.clip(resampled * AUDIO_INT16_SCALE, -32768, AUDIO_INT16_MAX)
            return resampled.astype(np.int16)
        elif original_dtype == np.float32:
            return resampled.astype(np.float32)
        else:
            return resampled.astype(original_dtype)

    def _linear_resample(self, audio: np.ndarray) -> np.ndarray:
        """
        Simple linear interpolation fallback.

        WARNING: This causes aliasing and should only be used
        when scipy is unavailable.
        """
        source_len = len(audio)
        target_len = int(source_len * self.ratio)

        if target_len == 0:
            return np.array([], dtype=audio.dtype)

        # Linear interpolation indices
        indices = np.linspace(0, source_len - 1, target_len)
        idx_floor = np.floor(indices).astype(int)
        idx_ceil = np.minimum(idx_floor + 1, source_len - 1)
        frac = indices - idx_floor

        # Interpolate
        return (1 - frac) * audio[idx_floor] + frac * audio[idx_ceil]

    def get_output_length(self, input_length: int) -> int:
        """
        Compute expected output length for given input length.

        Args:
            input_length: Number of input samples

        Returns:
            Number of output samples after resampling
        """
        return int(input_length * self.ratio)

    def __repr__(self) -> str:
        return f"Resampler({self._source_rate}Hz -> {self._target_rate}Hz, " f"quality={self._quality})"


# --------------------------------------------------------------------------- #
# Resampler Cache                                                              #
# --------------------------------------------------------------------------- #

_resampler_cache: dict[tuple[int, int, str], Resampler] = {}
_cache_lock = threading.Lock()
# Safety bound: in practice only a handful of rate combinations are used,
# but this prevents unbounded growth if callers pass arbitrary rates.
_MAX_RESAMPLER_CACHE: int = 64


def get_resampler(
    source_rate: int,
    target_rate: int,
    quality: Literal["fast", "quality"] = "quality",
) -> Resampler:
    """
    Get cached resampler instance.

    Resampler instances are cached by (source_rate, target_rate, quality)
    for performance. Creating a new Resampler each frame would be wasteful.
    The cache is bounded to ``_MAX_RESAMPLER_CACHE`` entries.

    Args:
        source_rate: Input sample rate in Hz
        target_rate: Output sample rate in Hz
        quality: Resampling quality preset

    Returns:
        Cached Resampler instance
    """
    key = (source_rate, target_rate, quality)

    with _cache_lock:
        if key not in _resampler_cache:
            # Evict oldest entry if at capacity
            if len(_resampler_cache) >= _MAX_RESAMPLER_CACHE:
                oldest_key = next(iter(_resampler_cache))
                del _resampler_cache[oldest_key]
            _resampler_cache[key] = Resampler(source_rate, target_rate, quality)
            logger.debug("Created cached resampler: %s", key)
        return _resampler_cache[key]


def clear_resampler_cache() -> None:
    """Clear the resampler cache (for testing)."""
    with _cache_lock:
        _resampler_cache.clear()


# --------------------------------------------------------------------------- #
# Convenience Functions                                                        #
# --------------------------------------------------------------------------- #


def resample_audio(
    audio: np.ndarray,
    source_rate: int,
    target_rate: int,
    quality: Literal["fast", "quality"] = "quality",
) -> np.ndarray:
    """
    Resample audio using cached resampler.

    Convenience function that handles caching internally.

    Args:
        audio: Input audio samples
        source_rate: Input sample rate in Hz
        target_rate: Output sample rate in Hz
        quality: Resampling quality preset

    Returns:
        Resampled audio
    """
    resampler = get_resampler(source_rate, target_rate, quality)
    return resampler.resample(audio)


__all__ = [
    "Resampler",
    "clear_resampler_cache",
    "get_resampler",
    "resample_audio",
]
