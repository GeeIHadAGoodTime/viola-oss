"""
Abstract base class for audio capture providers.

Defines the interface for capturing system audio output as PCM for
multi-room streaming.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

__all__ = ["AudioCaptureProvider"]


class AudioCaptureProvider(ABC):
    """
    Abstract interface for system audio capture.

    Implementations capture the hub device's audio output as raw PCM
    (48 kHz, stereo, 16-bit) and deliver it via a registered callback.
    """

    #: Set by ``get_capture_provider()`` when this instance was selected as a
    #: *silent* fallback -- no real capture provider was available on this
    #: platform -- rather than an intentional, explicit choice (a direct
    #: construction, or the ``VIOLA_AUDIO_CAPTURE`` override). ``None`` (the
    #: default) means "not a fallback"; callers that care whether the
    #: provider is faking real system audio (e.g. health reporting) check
    #: this instead of inferring it from the class name. See
    #: ``audio_core.streaming.pipeline_wiring.get_capture_health``.
    fallback_reason: str | None = None

    @abstractmethod
    def start(self) -> None:
        """Start capturing audio. Raises if already started."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop capturing audio and release resources."""
        ...

    @abstractmethod
    def set_callback(self, fn: Callable[[bytes, int, int, int], None]) -> None:
        """
        Register a callback to receive PCM audio data.

        Args:
            fn: Called with (data, sample_rate, channels, sample_width) from
                the capture thread.
        """
        ...

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Return True if this provider can operate on the current platform."""
        ...

    def get_metrics(self) -> dict:
        """Return provider-specific metrics for diagnostics.

        Subclasses should override to expose RMS, chunk counts, etc.
        """
        return {}
