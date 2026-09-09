"""AEC reference sourced from a platform ``AudioCaptureProvider`` (macOS, Linux).

WHY THIS EXISTS (#333)
----------------------
Echo cancellation for the wake word needs a *reference* signal: what the
speakers are playing, so the canceller can subtract it from what the microphone
hears. Without it, saying "Viola" over music is the detector trying to pick a
voice out of its own playback.

``core/voice_orchestrator.py::create_and_start_aec_adapter`` only ever built
``WasapiAECReferenceAdapter``, and WASAPI loopback refuses to start off Windows
(``audio_core/wasapi/capture_module.py``, ``if not IS_WINDOWS: return False``).
So on macOS the factory got ``None`` back and the orchestrator logged
``aec_source=NO_REF -- AEC running as passthrough``: every Mac install has run
wake detection with **no** echo cancellation at all, while Windows had it.

The missing piece was never the capture itself. ``audio_core/capture/`` already
has real system-audio providers for the other platforms -- a Core Audio process
tap on macOS (verified on macOS 15.6.1) and a PulseAudio monitor source on Linux
-- built for multi-room streaming. Nothing connected them to the AEC path. This
module is that connection: it adapts any ``AudioCaptureProvider`` into the
``AECReferenceSource`` protocol, reusing the shared ring-buffer half in
``audio_core/aec_reference_base.py``. The Windows path is untouched.

THE TEST-TONE TRAP
------------------
``audio_core.capture.factory.get_capture_provider()`` deliberately falls back to
``TestToneProvider`` -- a synthetic 440 Hz sine -- when no real provider is
available, because a multi-room spoke playing a tone is better than silence.
That fallback is *actively harmful* as an AEC reference: the canceller would
subtract a tone nobody is playing, corrupting the microphone signal and making
wake detection worse than having no reference at all. So this module does NOT
call the factory. :func:`select_system_audio_capture_provider` picks only
providers that capture real system output, and returns ``None`` rather than
degrading to a fake -- no reference is a safe passthrough; a wrong reference is
not.
"""

from __future__ import annotations

import sys
import threading

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

from ..aec_reference_base import RingBufferAECReferenceAdapter
from .base import AudioCaptureProvider

logger = get_logger(__name__)

__all__ = [
    "SystemAudioAECReferenceAdapter",
    "select_system_audio_capture_provider",
]

# PCM sample width the ring buffer understands (16-bit signed).
_EXPECTED_SAMPLE_WIDTH = 2


def select_system_audio_capture_provider() -> AudioCaptureProvider | None:
    """Return a provider capturing REAL system audio output, or ``None``.

    Deliberately not ``factory.get_capture_provider()``: that helper falls back
    to a synthetic tone, which must never become an AEC reference (see the
    module docstring). Windows is excluded because the WASAPI loopback adapter
    already owns that platform and is the better device-level reference there;
    this selector covers the platforms that previously had nothing.
    """
    if sys.platform == "win32":
        return None

    if sys.platform == "darwin":
        from .coreaudio_capture import CoreAudioCaptureProvider

        if CoreAudioCaptureProvider.is_available():
            logger.info("[AEC] macOS: using CoreAudio process-tap system capture as reference")
            return CoreAudioCaptureProvider()
        logger.warning(
            "[AEC] macOS: CoreAudio process-tap capture unavailable (needs macOS 14.4+ "
            "and the audio-capture permission); wake AEC will run as passthrough"
        )
        return None

    from .pulse_monitor import PulseMonitorProvider

    if PulseMonitorProvider.is_available():
        logger.info("[AEC] Linux: using PulseAudio monitor source as reference")
        return PulseMonitorProvider()

    logger.warning("[AEC] No real system-audio capture on this platform; AEC runs as passthrough")
    return None


class SystemAudioAECReferenceAdapter(RingBufferAECReferenceAdapter):
    """Adapt an ``AudioCaptureProvider`` into the ``AECReferenceSource`` protocol.

    The provider hands over ``(data, sample_rate, channels, sample_width)`` on
    its own capture thread; everything after that -- downmix, resample to the
    detector rate, ring buffer, frame serving -- is the shared base's job.
    """

    REFERENCE_LABEL = "system-audio AEC reference"

    def __init__(
        self,
        provider: AudioCaptureProvider | None = None,
        target_sample_rate: int = SAMPLE_RATE_16K,
    ):
        """
        Args:
            provider: Capture provider to use. When ``None``, one is selected
                for the current platform at :meth:`start` time.
            target_sample_rate: Rate the wake detector wants reference frames at.
        """
        super().__init__(target_sample_rate=target_sample_rate)
        self._provider = provider
        self._provider_lock = threading.Lock()
        self._unsupported_format_logged = False

    def start(self) -> bool:
        """Acquire and start the platform capture provider."""
        with self._provider_lock:
            if self._running:
                logger.debug("System-audio AEC adapter already running")
                return True

            provider = self._provider or select_system_audio_capture_provider()
            if provider is None:
                return False

            self._load_diagnostics()

            try:
                provider.set_callback(self._on_capture_chunk)
                provider.start()
            except Exception:
                logger.exception(
                    "AEC_ADAPTER_ERROR: %s failed to start system-audio capture",
                    type(provider).__name__,
                )
                self._provider = None
                self._running = False
                return False

            self._provider = provider
            self._running = True
            logger.info(
                "AEC_ADAPTER_SUCCESS: system-audio AEC reference capture started via %s",
                type(provider).__name__,
            )
            return True

    def stop(self) -> None:
        """Stop the capture provider and release it."""
        with self._provider_lock:
            provider = self._provider
            try:
                if provider is not None and self._running:
                    provider.stop()
                    logger.info("System-audio AEC reference capture stopped")
            except Exception as exc:  # noqa: BLE001, RUF100 - teardown must not raise
                logger.warning("Error stopping system-audio AEC capture: %s", exc)
            finally:
                self._running = False
                self._provider = None

    def _on_capture_chunk(
        self,
        data: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None:
        """Provider callback: record the real format, then hand bytes to the ring buffer."""
        if sample_width != _EXPECTED_SAMPLE_WIDTH:
            # Log once rather than per chunk: this fires at the capture rate.
            if not self._unsupported_format_logged:
                self._unsupported_format_logged = True
                logger.warning(
                    "System-audio AEC reference ignoring %d-byte samples; expected %d-byte PCM",
                    sample_width,
                    _EXPECTED_SAMPLE_WIDTH,
                )
            return

        # The provider tells us the truth about rate and channels, so the base
        # never has to guess either one.
        self._device_sample_rate = sample_rate
        self._channels_hint = channels if channels > 0 else None

        self._on_pcm_callback(data)
