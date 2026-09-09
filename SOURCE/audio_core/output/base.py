"""Abstract base class for audio output drivers.

Defines the interface that all audio output drivers must implement.
Drivers receive raw PCM data from the PlaybackScheduler and emit it
to the local sound hardware (or discard it, in the case of null output).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = [
    "AudioOutputDriver",
]


class AudioOutputDriver(ABC):
    """Abstract base class for audio output drivers.

    Implementations must be thread-safe: ``write()`` may be called from a
    background playback loop while ``start()``/``stop()`` are called from
    the application lifecycle thread.
    """

    #: Set by ``get_output_driver()`` when this instance was selected as a
    #: *silent* fallback -- no real sound-hardware driver was available --
    #: rather than an intentional, explicit choice. ``None`` (the default)
    #: means "not a fallback". Callers that care whether the driver is
    #: silently discarding PCM instead of playing it (e.g. health reporting)
    #: check this instead of inferring it from the class name. See
    #: ``audio_core.streaming.pipeline_wiring.get_output_health``.
    fallback_reason: str | None = None

    @abstractmethod
    def start(self, sample_rate: int, channels: int, sample_width: int) -> None:
        """Open the output device and prepare for playback.

        Args:
            sample_rate: Audio sample rate in Hz (e.g. 48000).
            channels: Number of audio channels (1 = mono, 2 = stereo).
            sample_width: Bytes per sample (e.g. 2 for 16-bit PCM).
        """
        ...

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write raw PCM data to the output device.

        This method may block until the device has consumed the data.

        Args:
            data: Raw PCM audio bytes.
        """
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop playback and release the output device."""
        ...
