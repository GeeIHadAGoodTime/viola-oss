"""Factory for audio output drivers.

Returns the best available driver for the current environment:
``sounddevice`` if installed, otherwise the null (silent) driver.
"""

from __future__ import annotations

from core.logging_config import get_logger

from .base import AudioOutputDriver

logger = get_logger(__name__)

__all__ = [
    "get_output_driver",
]


def get_output_driver() -> AudioOutputDriver:
    """Return an audio output driver.

    Attempts to import ``sounddevice`` and create a
    :class:`~audio_core.output.sounddevice_output.SounddeviceAudioOutput`.
    Falls back to :class:`~audio_core.output.null_output.NullAudioOutput`
    when ``sounddevice`` is unavailable.
    """
    try:
        from importlib import import_module

        import_module("sounddevice")

        from .sounddevice_output import SounddeviceAudioOutput

        logger.debug("sounddevice available; using SounddeviceAudioOutput")
        return SounddeviceAudioOutput()
    except ImportError:
        # This is the silent fake-success shape (#2598): without the
        # fallback_reason marker, NullAudioOutput.write() happily returns
        # and every downstream check ("did playback start OK?") sees
        # success -- while every byte of PCM is discarded and the device
        # produces no sound. Marking the instance lets
        # pipeline_wiring.get_output_health() and /health/details report
        # "degraded" instead of a false "ok".
        logger.warning(
            "sounddevice not available; falling back to NullAudioOutput -- "
            "audio playback will report success but produce NO SOUND "
            "(all PCM will be silently discarded)"
        )
        from .null_output import NullAudioOutput

        driver = NullAudioOutput()
        driver.fallback_reason = "sounddevice_unavailable"
        return driver
