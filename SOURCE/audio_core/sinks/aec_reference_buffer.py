"""
AEC Reference Buffer for audio sinks.

Provides a thread-safe ring buffer to capture playback audio for use as
the reference signal in Acoustic Echo Cancellation (AEC).

Key design decisions:
1. Fixed-size ring buffer (not variable-length deque)
2. Fixed-frame extraction (not arbitrary-length reads)
3. Delay-compensated reads to account for speaker-to-mic latency
4. Stereo-to-mono conversion at write time
5. High-quality polyphase resampling for sample rate conversion

CHANGES (2025-12-07):
- Replaced linear interpolation with polyphase resampling
- Uses cached resampler instances for performance
- Added resampler_quality configuration option
- Prevents aliasing artifacts that were causing false wake triggers

Usage:
    from core.constants import SAMPLE_RATE_16K, SAMPLE_RATE_48K

    # In sink's __init__:
    self._aec_buffer = AECReferenceBuffer(
        buffer_duration_ms=500,
        sample_rate=SAMPLE_RATE_48K,
        delay_ms=50,
        resampler_quality="quality",  # or "fast"
    )

    # In sink's write loop:
    self._aec_buffer.write(payload, channels=2)

    # From wake word listener:
    ref_frame = sink.get_aec_reference_frame(frame_samples=160, target_rate=SAMPLE_RATE_16K)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Literal

import numpy as np

from core.constants import AUDIO_INT16_MAX, SAMPLE_RATE_48K
from core.logging_config import get_logger

if TYPE_CHECKING:
    from voice.wake_detector.resampler import Resampler

logger = get_logger(__name__)


class AECReferenceBuffer:
    """
    Thread-safe ring buffer for AEC reference audio.

    Stores recent playback audio and provides delay-compensated frame
    extraction for echo cancellation.

    RESAMPLING:
    - Uses polyphase resampler (not linear interpolation)
    - Quality mode configurable via constructor
    - Resampler cached for performance
    """

    def __init__(
        self,
        buffer_duration_ms: int = 500,
        sample_rate: int = SAMPLE_RATE_48K,
        delay_ms: int = 50,
        resampler_quality: Literal["fast", "quality"] = "quality",
    ) -> None:
        """
        Initialize the reference buffer.

        Args:
            buffer_duration_ms: Total buffer duration in milliseconds
            sample_rate: Playback sample rate in Hz
            delay_ms: Estimated speaker-to-mic delay for alignment
            resampler_quality: Resampling quality ("fast" or "quality")
        """
        self._sample_rate = sample_rate
        self._delay_samples = int(sample_rate * delay_ms / 1000)
        self._resampler_quality = resampler_quality

        # Ring buffer stores mono int16 samples
        buffer_samples = int(sample_rate * buffer_duration_ms / 1000)
        self._buffer: np.ndarray = np.zeros(buffer_samples, dtype=np.int16)
        self._write_pos = 0
        self._total_written = 0  # Track total samples written for debugging

        self._lock = threading.Lock()

        # Resampler cache (created on demand per target rate)
        self._resamplers: dict[int, Resampler] = {}

        logger.debug(
            "AEC reference buffer initialized: %d samples (%.0fms), " "delay=%d samples (%.0fms), resampler=%s",
            buffer_samples,
            buffer_duration_ms,
            self._delay_samples,
            delay_ms,
            resampler_quality,
        )

    @property
    def sample_rate(self) -> int:
        """Return the buffer's sample rate."""
        return self._sample_rate

    @property
    def buffer_samples(self) -> int:
        """Return the total buffer size in samples."""
        return len(self._buffer)

    def _get_resampler(self, target_rate: int) -> Resampler:
        """Get or create resampler for target rate."""
        if target_rate not in self._resamplers:
            from voice.wake_detector.resampler import get_resampler

            self._resamplers[target_rate] = get_resampler(
                self._sample_rate,
                target_rate,
                self._resampler_quality,
            )
        return self._resamplers[target_rate]

    def write(
        self,
        audio_data: bytes | np.ndarray,
        channels: int = 2,
        bits_per_sample: int = 16,
        is_float: bool = False,
    ) -> None:
        """
        Write playback audio to the reference buffer.

        Args:
            audio_data: Raw audio bytes or numpy array
            channels: Number of audio channels (will be mixed to mono)
            bits_per_sample: Bits per sample (16 or 32)
            is_float: Whether the audio is float32 format
        """
        try:
            # Convert bytes to numpy array
            if isinstance(audio_data, bytes):
                if is_float:
                    samples = np.frombuffer(audio_data, dtype=np.float32)
                elif bits_per_sample == 16:
                    samples = np.frombuffer(audio_data, dtype=np.int16)
                elif bits_per_sample == 32:
                    samples = np.frombuffer(audio_data, dtype=np.int32)
                else:
                    logger.warning("Unsupported bit depth: %s", bits_per_sample)
                    return
            else:
                samples = audio_data

            # Convert to int16 if needed
            if is_float or samples.dtype == np.float32:
                # Float is -1.0 to 1.0, scale to int16
                samples = (samples * AUDIO_INT16_MAX).astype(np.int16)
            elif samples.dtype == np.int32:
                # Scale int32 to int16 (divide by 2^16)
                samples = (samples.astype(np.int64) // 65536).astype(np.int16)
            elif samples.dtype != np.int16:
                samples = samples.astype(np.int16)

            # Mix stereo to mono: (L + R) / 2
            if channels >= 2:
                # Reshape to (n_frames, channels) and average
                n_frames = len(samples) // channels
                if n_frames > 0:
                    samples = samples[: n_frames * channels].reshape(n_frames, channels)
                    samples = samples.mean(axis=1).astype(np.int16)

            # Write to ring buffer
            n = len(samples)
            if n == 0:
                return

            with self._lock:
                buffer_len = len(self._buffer)

                # Handle wrap-around
                end_pos = (self._write_pos + n) % buffer_len

                if self._write_pos + n <= buffer_len:
                    # No wrap
                    self._buffer[self._write_pos : self._write_pos + n] = samples
                else:
                    # Wrap around
                    first_chunk = buffer_len - self._write_pos
                    self._buffer[self._write_pos :] = samples[:first_chunk]
                    self._buffer[:end_pos] = samples[first_chunk:]

                self._write_pos = end_pos
                self._total_written += n

        except Exception as e:
            logger.error("AEC reference buffer write error: %s", e)

    def get_frame(
        self,
        frame_samples: int,
        target_rate: int | None = None,
    ) -> np.ndarray:
        """
        Get a frame of reference audio with delay compensation.

        Args:
            frame_samples: Number of samples to return (at target_rate)
            target_rate: Target sample rate (None = use buffer's native rate)

        Returns:
            Audio frame as int16 numpy array, or zeros if buffer is empty
        """
        with self._lock:
            # Calculate source samples needed
            if target_rate is not None and target_rate != self._sample_rate:
                # Account for resampling ratio
                source_samples = int(frame_samples * self._sample_rate / target_rate)
            else:
                source_samples = frame_samples

            # Check if we have enough data
            if self._total_written < source_samples + self._delay_samples:
                return np.zeros(frame_samples, dtype=np.int16)

            buffer_len = len(self._buffer)

            # Calculate read position with delay offset
            read_pos = (self._write_pos - self._delay_samples - source_samples) % buffer_len

            # Extract samples (handle wrap-around)
            if read_pos + source_samples <= buffer_len:
                frame = self._buffer[read_pos : read_pos + source_samples].copy()
            else:
                first_chunk = buffer_len - read_pos
                frame = np.concatenate(
                    [
                        self._buffer[read_pos:],
                        self._buffer[: source_samples - first_chunk],
                    ]
                )

        # Resample if needed (using polyphase, not linear)
        if target_rate is not None and target_rate != self._sample_rate:
            resampler = self._get_resampler(target_rate)
            frame = resampler.resample(frame)

            # Ensure exact output length (resampling can be off by 1-2 samples)
            if len(frame) < frame_samples:
                frame = np.pad(frame, (0, frame_samples - len(frame)))
            elif len(frame) > frame_samples:
                frame = frame[:frame_samples]

        return frame.astype(np.int16)

    def _resample(
        self,
        samples: np.ndarray,
        source_len: int,
        target_len: int,
    ) -> np.ndarray:
        """
        DEPRECATED: Linear interpolation resampling.

        This method is kept for backwards compatibility but is no longer used.
        The get_frame() method now uses polyphase resampling via _get_resampler().

        WARNING: Linear interpolation causes aliasing. Use polyphase resampling
        for new code.
        """
        if source_len == target_len:
            return samples

        # Linear interpolation indices
        indices = np.linspace(0, source_len - 1, target_len)

        # Integer and fractional parts
        idx_floor = np.floor(indices).astype(int)
        idx_ceil = np.minimum(idx_floor + 1, source_len - 1)
        frac = indices - idx_floor

        # Interpolate
        resampled = (1 - frac) * samples[idx_floor] + frac * samples[idx_ceil]

        return resampled.astype(np.int16)

    def clear(self) -> None:
        """Clear the buffer."""
        with self._lock:
            self._buffer.fill(0)
            self._write_pos = 0
            self._total_written = 0

    def set_delay(self, delay_ms: int) -> None:
        """Update the delay compensation."""
        with self._lock:
            self._delay_samples = int(self._sample_rate * delay_ms / 1000)
            logger.debug(
                "AEC delay updated to %d samples (%.0fms)",
                self._delay_samples,
                delay_ms,
            )


__all__ = ["AECReferenceBuffer"]
