"""Shared audio device selection utilities with graceful fallback"""

from __future__ import annotations

import importlib
from typing import Any

from audio_core.portaudio_guard import open_portaudio, open_stream, terminate_portaudio
from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)


def select_audio_device(hint: str | None = None) -> int:
    """

    Select audio device by hint substring with graceful fallback.

    Fallback chain:
    1. Try device matching hint
    2. Try first available input device
    3. Try system default (-1)
    4. Return -1 (system default) as final fallback

    Args:
        hint: Device name substring to match (case-insensitive)

    Returns:
        Device index (-1 for default)
    """
    try:
        pvrecorder_module = importlib.import_module("pvrecorder")
        pvrecorder_cls: Any = pvrecorder_module.PvRecorder
        devices = pvrecorder_cls.get_audio_devices()
    except Exception as e:
        logger.debug("Audio device enumeration unavailable (%s); using system default.", e)
        return -1

    if not devices:
        logger.warning("No audio devices found, using system default.")
        return -1

    # Strategy 1: Try to match hint (primary choice)
    if hint:
        hint_lower = hint.lower()
        for idx, name in enumerate(devices):
            try:
                if hint_lower in name.lower():
                    logger.info("✅ Selected audio device '%s' at index %s.", name, idx)
                    return idx
            except Exception as e:
                logger.debug("Failed to query device %d: %s", idx, e, exc_info=True)
                continue
        logger.warning("⚠️ Device matching '%s' not found, trying alternatives...", hint)

    # Strategy 2: Try first available device (fallback)
    try:
        # open_portaudio()/terminate_portaudio() serialize Pa_Initialize/Pa_Terminate
        # under the process-wide lock; a missing pyaudio raises ImportError, caught
        # below to fall through to the system-default strategy.
        p = open_portaudio()
        input_devices = []
        for i in range(p.get_device_count()):
            try:
                info = p.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    input_devices.append((i, info.get("name", "Unknown")))
            except Exception as e:
                logger.debug("Failed to query PyAudio device %d: %s", i, e, exc_info=True)
                continue

        if input_devices:
            device_idx, device_name = input_devices[0]
            logger.info(
                "✅ Fallback: Using first available input device '%s' at index %s.",
                device_name,
                device_idx,
            )
            terminate_portaudio(p)
            return device_idx

        terminate_portaudio(p)
    except Exception as e:
        logger.debug("PyAudio fallback failed: %s", e)

    # Strategy 3: System default (final fallback)
    logger.info("Using system default audio device.")
    return -1


def select_audio_device_with_fallback(hint: str | None = None, test_device: bool = False) -> tuple[int, bool]:
    """
    Select audio device with testing and automatic fallback.

    Args:
        hint: Device name substring to match
        test_device: Whether to test if device actually works

    Returns:
        Tuple of (device_index, success_flag)
    """
    device_idx = select_audio_device(hint)

    if test_device and device_idx != -1:
        # Test if device is actually usable
        try:
            pyaudio = importlib.import_module("pyaudio")
            p = open_portaudio()
            try:
                # Try to open stream briefly to test device
                stream = open_stream(
                    p,
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=SAMPLE_RATE_16K,
                    input=True,
                    input_device_index=device_idx,
                    frames_per_buffer=1024,
                )
                stream.close()
                terminate_portaudio(p)
                logger.debug("✅ Device %s tested successfully", device_idx)
                return (device_idx, True)
            except Exception as e:
                logger.warning("⚠️ Device %s failed test: %s, trying fallback...", device_idx, e)
                terminate_portaudio(p)
                # Try system default as fallback
                return (select_audio_device(None), True)
        except Exception as e:
            logger.debug("Device testing unavailable: %s", e)

    return (device_idx, True)
