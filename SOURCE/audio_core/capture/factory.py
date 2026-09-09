"""
Factory for selecting the appropriate audio capture provider.

Auto-detects the platform and returns the best available provider.
Override via the VIOLA_AUDIO_CAPTURE environment variable.
"""

from __future__ import annotations

import config.env as env
from core.logging_config import get_logger

from .base import AudioCaptureProvider
from .coreaudio_capture import CoreAudioCaptureProvider
from .proctap_provider import ProcTapProvider
from .pulse_monitor import PulseMonitorProvider
from .test_tone import TestToneProvider
from .wasapi_loopback import WasapiLoopbackProvider

logger = get_logger(__name__)

_PROVIDER_MAP: dict[str, type[AudioCaptureProvider]] = {
    "proctap": ProcTapProvider,
    "wasapi": WasapiLoopbackProvider,
    "pulse": PulseMonitorProvider,
    "coreaudio": CoreAudioCaptureProvider,
    "test_tone": TestToneProvider,
}

__all__ = ["get_capture_provider"]


def get_capture_provider() -> AudioCaptureProvider:
    """
    Auto-detect platform and return an appropriate capture provider.

    Falls back to TestToneProvider if no system audio capture is available.

    Override via env var ``VIOLA_AUDIO_CAPTURE`` with one of:
    ``proctap``, ``wasapi``, ``pulse``, ``coreaudio``, ``test_tone``, ``none``.

    Raises:
        RuntimeError: If override is ``none`` (explicitly disabled).
        ValueError: If override value is not recognized.
    """
    override = env.get("VIOLA_AUDIO_CAPTURE")

    if override is not None:
        override = override.strip().lower()

        if override == "none":
            raise RuntimeError("Audio capture explicitly disabled via VIOLA_AUDIO_CAPTURE=none")

        provider_cls = _PROVIDER_MAP.get(override)
        if provider_cls is None:
            raise ValueError(
                "Unknown VIOLA_AUDIO_CAPTURE value '%s'. "
                "Expected one of: proctap, wasapi, pulse, coreaudio, test_tone, none" % override
            )
        logger.info("Audio capture override: using %s provider", override)
        return provider_cls()

    # Auto-detect: try platform-specific providers first.
    # ProcTap isolates Viola's audio from other system sounds.
    if ProcTapProvider.is_available():
        logger.info("Auto-detected ProcTap per-process capture provider")
        return ProcTapProvider()

    if WasapiLoopbackProvider.is_available():
        logger.info("Auto-detected WASAPI loopback capture provider")
        return WasapiLoopbackProvider()

    if PulseMonitorProvider.is_available():
        logger.info("Auto-detected PulseAudio monitor capture provider")
        return PulseMonitorProvider()

    if CoreAudioCaptureProvider.is_available():
        logger.info("Auto-detected CoreAudio capture provider (macOS)")
        return CoreAudioCaptureProvider()

    # Fallback to test tone. This is the silent fake-success shape (#2598):
    # without the fallback_reason marker, this looks identical to an
    # intentional VIOLA_AUDIO_CAPTURE=test_tone override -- the hub
    # broadcasts a synthetic 440Hz sine to every spoke as if it were real
    # system audio, with no operator-visible distinction. Marking the
    # instance lets pipeline_wiring.get_capture_health() and
    # /health/details report "degraded" instead of a false "ok".
    logger.warning(
        "No system audio capture available on this platform; falling back to "
        "TestToneProvider -- multiroom spokes will receive a SYNTHETIC 440Hz "
        "tone, NOT real system audio"
    )
    provider = TestToneProvider()
    provider.fallback_reason = "no_capture_provider_available"
    return provider
