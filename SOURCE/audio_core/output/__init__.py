"""Audio output driver package.

Provides a pluggable audio output abstraction used by the Spoke side of
the multi-room streaming pipeline.  The factory selects the best driver
for the current environment.

Usage::

    from audio_core.output import get_output_driver

    driver = get_output_driver()
    driver.start(sample_rate=48000, channels=2, sample_width=2)
    driver.write(pcm_bytes)
    driver.stop()
"""

from __future__ import annotations

from .base import AudioOutputDriver
from .factory import get_output_driver
from .null_output import NullAudioOutput

__all__ = [
    "AudioOutputDriver",
    "NullAudioOutput",
    "get_output_driver",
]
