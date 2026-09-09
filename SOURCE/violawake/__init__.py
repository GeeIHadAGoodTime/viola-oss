"""
ViolaWake - Custom Wake Word Detection
=======================================

A commercially-clean wake word detection system for Viola.

Usage:
    from violawake import ViolaWake

    # Load trained model (DEFAULT_MODEL_PATH resolves the bundled model)
    engine = ViolaWake(str(DEFAULT_MODEL_PATH))

    # Process audio chunks (16kHz, 1.5 seconds)
    score = engine.process_audio(audio_chunk)
    if score > 0.8:
        logger.info("Wake word detected!")

License:
    MIT License - Free for commercial use.
Model trained only on permissively licensed data.
"""

from __future__ import annotations

from violawake.config import (
    CLIP_DURATION,
    CLIP_SAMPLES,
    DEFAULT_THRESHOLD,
    HOP_LENGTH,
    N_FFT,
    N_MELS,
    SAMPLE_RATE,
    WIN_LENGTH,
)
from violawake.engine import ViolaWake
from violawake.model import WakeWordModel

__version__ = "2.0.0"
__all__ = [
    "CLIP_DURATION",
    "CLIP_SAMPLES",
    "DEFAULT_THRESHOLD",
    "HOP_LENGTH",
    "N_FFT",
    "N_MELS",
    "SAMPLE_RATE",
    "WIN_LENGTH",
    "ViolaWake",
    "WakeWordModel",
]
