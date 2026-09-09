"""RMS energy-based silence detection for wake word audio clips.

Determines whether a post-trigger audio segment contains only silence,
which indicates a false positive wake detection.
"""

from __future__ import annotations

import math

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)


def detect_silence(
    audio_frames: np.ndarray,
    threshold_dbfs: float = -40.0,
    min_silence_sec: float = 3.0,
    frame_ms: int = 100,
) -> bool:
    """Check if audio contains only silence (no speech activity).

    Args:
        audio_frames: Float32 audio array (mono, 16kHz).
        threshold_dbfs: dBFS threshold below which a frame is "silent".
        min_silence_sec: Consecutive seconds of silence to declare "all silent".
        frame_ms: Analysis frame size in milliseconds.

    Returns:
        True if the entire clip is silence (no frame exceeds threshold
        for at least min_silence_sec).
    """
    if audio_frames.size == 0:
        return True

    samples_per_frame = int(SAMPLE_RATE_16K * frame_ms / 1000)
    total_frames = len(audio_frames) // samples_per_frame

    if total_frames == 0:
        return True

    consecutive_silent = 0
    frames_for_silence = int(min_silence_sec * 1000 / frame_ms)

    for i in range(total_frames):
        start = i * samples_per_frame
        end = start + samples_per_frame
        frame = audio_frames[start:end]

        rms = np.sqrt(np.mean(frame**2))
        if rms < 1e-10:
            dbfs = -100.0
        else:
            dbfs = 20.0 * math.log10(rms)

        if dbfs < threshold_dbfs:
            consecutive_silent += 1
        else:
            consecutive_silent = 0

    # If the entire clip never exceeded threshold for long enough, it's silence
    # We check if we accumulated enough silent frames total
    total_silent = 0
    for i in range(total_frames):
        start = i * samples_per_frame
        end = start + samples_per_frame
        frame = audio_frames[start:end]
        rms = np.sqrt(np.mean(frame**2))
        if rms < 1e-10:
            dbfs = -100.0
        else:
            dbfs = 20.0 * math.log10(rms)
        if dbfs < threshold_dbfs:
            total_silent += 1

    # Silence = no frame exceeded threshold for the required duration
    duration_sec = len(audio_frames) / SAMPLE_RATE_16K
    if duration_sec < min_silence_sec:
        # Short clip: consider silent if ALL frames are below threshold
        return total_silent == total_frames

    return total_silent >= frames_for_silence
