"""
AEC Reference Adapter using WASAPI Loopback
============================================

Implements the AECReferenceSource protocol by capturing system audio
via WASAPI loopback and providing it to the wake word detector for
echo cancellation.

WASAPI is Windows-only, so this module is the Windows half of the reference
path. The capture-source-agnostic half -- ring buffer, downmix, resample, and
the AECReferenceSource protocol itself -- lives in
``audio_core/aec_reference_base.py`` so a second platform can reuse it without
touching this file. See ``audio_core/capture/system_audio_aec_reference.py``
for the provider-backed sibling that serves macOS and Linux.
"""

from __future__ import annotations

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

from ..aec_reference_base import (
    MIN_READY_SAMPLES,
    RingBufferAECReferenceAdapter,
    resample_mono_int16,
)
from .capture_module import NativeWasapiCaptureModule

logger = get_logger(__name__)

# Re-exported for the callers and tests that imported them from here before the
# shared half was hoisted out.
__all__ = ["MIN_READY_SAMPLES", "WasapiAECReferenceAdapter", "resample_mono_int16"]


class WasapiAECReferenceAdapter(RingBufferAECReferenceAdapter):
    """
    Adapter that captures WASAPI loopback audio and provides it
    as AEC reference frames.

    Implements the AECReferenceSource protocol for use with
    PersonalizedOpenWakeWordListener.set_aec_reference_source().
    """

    REFERENCE_LABEL = "WASAPI AEC reference"

    def __init__(self, target_sample_rate: int = SAMPLE_RATE_16K):
        super().__init__(target_sample_rate=target_sample_rate)
        self._capture_module: NativeWasapiCaptureModule | None = None

    def start(self) -> bool:
        """Start WASAPI loopback capture."""
        logger.info("AEC_ADAPTER_START: Attempting to start WASAPI loopback capture")
        try:
            from .capture_module import NativeWasapiCaptureModule

            if self._running:
                logger.debug("WASAPI AEC adapter already running")
                return True

            # Create a minimal config object for the capture module
            class MockConfig:
                sample_rate = self._target_rate

            config = MockConfig()
            logger.info("AEC_ADAPTER_START: Creating NativeWasapiCaptureModule")
            self._capture_module = NativeWasapiCaptureModule()
            self._load_diagnostics()

            def on_pcm_callback(pcm_bytes: bytes) -> None:
                self._on_pcm_callback(pcm_bytes)

            if self._capture_module is not None:
                logger.info("AEC_ADAPTER_START: Calling capture_module.start()")
                started = self._capture_module.start(config, on_pcm_callback)
                logger.info("AEC_ADAPTER_START: capture_module.start() returned %s", started)
                if not started:
                    logger.error("AEC_ADAPTER_FAIL: Failed to start WASAPI capture module")
                    self._capture_module = None
                    self._running = False
                    return False
                # Get actual device sample rate from capture module
                actual_rate = getattr(self._capture_module, "_device_sample_rate", None)
                if actual_rate:
                    self._device_sample_rate = actual_rate
                    logger.info("AEC_ADAPTER_START: Device sample rate: %sHz", actual_rate)

            self._running = True
            logger.info("AEC_ADAPTER_SUCCESS: WASAPI loopback AEC reference capture started")
            return True

        except Exception:
            logger.exception("AEC_ADAPTER_ERROR: Failed to start WASAPI AEC reference capture")
            self._running = False
            return False

    def stop(self) -> None:
        """Stop WASAPI loopback capture."""
        try:
            if self._capture_module and self._running:
                self._capture_module.stop()
                logger.info("WASAPI loopback AEC reference capture stopped")
        except Exception as e:
            logger.warning("Error stopping WASAPI AEC capture: %s", e)
        finally:
            self._running = False
            self._capture_module = None
