"""Future macOS CoreAudio capture provider.

This module is additive scaffolding only. It is intentionally NOT wired into
``audio_core.capture.factory``; the existing ``coreaudio_capture.py`` stub
remains the only macOS provider reachable by production code.
"""

from __future__ import annotations

import platform
import threading
from collections.abc import Callable
from dataclasses import dataclass

from core.constants import AUDIO_CHANNELS_STEREO, SAMPLE_RATE_48K

from .base import AudioCaptureProvider

IS_MACOS = platform.system() == "Darwin"

_BYTES_PER_SAMPLE = 2
_CHUNK_DURATION_MS = 20
_SAMPLES_PER_CHUNK = (SAMPLE_RATE_48K * _CHUNK_DURATION_MS) // 1000
_CHUNK_SIZE_BYTES = _SAMPLES_PER_CHUNK * AUDIO_CHANNELS_STEREO * _BYTES_PER_SAMPLE

__all__ = [
    "MacOSCoreAudioCaptureProvider",
    "MacOSLoopbackDevice",
    "discover_loopback_devices",
]


@dataclass(frozen=True)
class MacOSLoopbackDevice:
    """Candidate system-audio capture device discovered through CoreAudio.

    Future implementation work should populate this from CoreAudio Hardware
    Abstraction Layer queries, not from shelling out to user-facing utilities.
    The expected enumeration path is ``AudioObjectGetPropertyData`` over
    ``kAudioHardwarePropertyDevices`` followed by per-device reads for
    ``kAudioObjectPropertyName``, ``kAudioDevicePropertyDeviceUID``,
    ``kAudioDevicePropertyStreamConfiguration``, and
    ``kAudioDevicePropertyNominalSampleRate``.
    """

    name: str
    device_uid: str
    input_channels: int
    nominal_sample_rate: float
    vendor: str = ""


class MacOSCoreAudioCaptureProvider(AudioCaptureProvider):
    """Capture macOS system output for multi-room streaming.

    Contract:
        Deliver 20 ms chunks of 48 kHz, stereo, signed 16-bit PCM through the
        registered callback as ``callback(data, sample_rate, channels,
        sample_width)``. This matches the Windows WASAPI and Linux PulseAudio
        capture providers.

    Future CoreAudio implementation notes:
        - Enumerate hardware with ``AudioObjectGetPropertyData`` and the
          CoreAudio Audio Hardware Services constants listed in
          ``MacOSLoopbackDevice``.
        - macOS does not expose native system-output loopback. Detect and use a
          loopback-capable virtual input/output pair such as BlackHole,
          Loopback by Rogue Amoeba, or Soundflower. Detection should check
          device UID, display name, manufacturer, channel count, and nominal
          sample rate instead of assuming one fixed device name.
        - If the user creates an aggregate or multi-output device, confirm that
          its input stream really receives the app's routed output before
          advertising availability.
        - Preserve the AEC reference signal flow: the captured playback stream
          is the reference for echo cancellation and wake gating, while the
          microphone stream remains separate. Do not mix microphone audio into
          the system-output capture path.
        - Keep this provider desktop-local. Captured audio and AEC reference
          material are Tier-3/local runtime data unless a future explicit
          consent surface says otherwise.
    """

    def __init__(self) -> None:
        self._callback: Callable[[bytes, int, int, int], None] | None = None
        self._running = False
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._chunks_produced = 0
        self._last_rms = 0.0

    def set_callback(self, fn: Callable[[bytes, int, int, int], None]) -> None:
        """Register the PCM callback used by the multi-room fanout pipeline."""
        self._callback = fn

    def start(self) -> None:
        """Start CoreAudio capture.

        Future implementation should:
        1. Find an eligible virtual loopback device.
        2. Open an AudioUnit or AudioDeviceIOProc input stream.
        3. Convert/resample to 48 kHz stereo int16.
        4. Deliver complete ``_CHUNK_SIZE_BYTES`` frames to ``self._callback``.
        5. Track RMS/chunk metrics for ``/api/v1/debug/audio-pipeline``.
        """
        raise NotImplementedError(
            "macOS CoreAudio capture is scaffolded but not implemented or wired; "
            "test on real macOS hardware before activation."
        )

    def stop(self) -> None:
        """Stop CoreAudio capture and release device handles."""
        raise NotImplementedError("macOS CoreAudio capture shutdown is scaffolded but not implemented.")

    @classmethod
    def is_available(cls) -> bool:
        """Return whether this future provider can operate on the host.

        The scaffold always returns ``False`` so it cannot be selected
        accidentally. A future implementation should require macOS, an enabled
        feature gate, and a proven loopback-capable virtual device.
        """
        return False

    def get_metrics(self) -> dict:
        """Return provider metrics in the same shape as other capture providers."""
        return {
            "provider": "coreaudio_macos_scaffold",
            "rms": self._last_rms,
            "chunks_produced": self._chunks_produced,
            "running": self._running,
            "chunk_size_bytes": _CHUNK_SIZE_BYTES,
        }


def discover_loopback_devices() -> list[MacOSLoopbackDevice]:
    """Discover loopback-capable macOS devices.

    Future implementation should call CoreAudio directly and classify devices
    from stable metadata. Expected positive families include:
    BlackHole, Loopback, Soundflower, and verified aggregate devices that expose
    input channels receiving routed system output.
    """
    raise NotImplementedError("CoreAudio AudioObjectGetPropertyData enumeration is not implemented.")
