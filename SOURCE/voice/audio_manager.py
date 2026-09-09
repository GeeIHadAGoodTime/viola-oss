"""
Unified Audio Device Management

Provides a consistent interface for audio device selection and management
across different platforms and audio libraries.
"""

from __future__ import annotations

from typing import Protocol, TypedDict

from config import AppConfig
from core.logging_config import get_logger

logger = get_logger(__name__)


class _SelectAudioDevice(Protocol):
    def __call__(self, *, hint: str | None = None) -> int: ...


class AudioDeviceManagerStats(TypedDict):
    selection_attempts: int
    selection_failures: int
    current_input_device: int | None
    current_output_device: int | None


def _get_audio_device_selector() -> _SelectAudioDevice | None:
    try:  # pragma: no cover
        from utils.audio_device import select_audio_device as selector

        return selector
    except Exception:
        logger.debug("select_audio_device import unavailable", exc_info=True)
        return None


def _get_pvrecorder_class():
    try:  # pragma: no cover
        from pvrecorder import PvRecorder

        return PvRecorder
    except Exception:
        logger.debug("PvRecorder import unavailable", exc_info=True)
        return None


class AudioDeviceManager:
    """
    Unified audio device management abstraction.

    Provides device selection, enumeration, and management across
    different audio backends (PyAudio, PvRecorder, etc.).
    """

    def __init__(self, config: AppConfig):
        """
        Initialize audio device manager.

        Args:
            config: Application configuration
        """
        self.config = config
        self._input_device: int | None = None
        self._output_device: int | None = None
        self._selection_attempts = 0
        self._selection_failures = 0

    def select_input_device(self, hint: str | None = None) -> int:
        """
        Select audio input device.

        Args:
            hint: Device name hint (optional)

        Returns:
            Device index (-1 for system default)
        """
        self._selection_attempts += 1
        try:
            selector = _get_audio_device_selector()
            if selector is None:
                raise RuntimeError("select_audio_device unavailable")

            device = selector(hint=hint)
            self._input_device = device
            return device
        except Exception as e:
            self._selection_failures += 1
            logger.debug("Failed to select input device: %s", e)
            return -1  # System default

    def select_output_device(self, hint: str | None = None) -> int:
        """
        Select audio output device.

        Args:
            hint: Device name hint (optional)

        Returns:
            Device index (-1 for system default)
        """
        # This selector always returns the system default; hint is not applied.
        self._output_device = -1
        return -1

    def get_input_device(self) -> int | None:
        """Get currently selected input device"""
        return self._input_device

    def get_output_device(self) -> int | None:
        """Get currently selected output device"""
        return self._output_device

    def list_input_devices(self) -> list[str]:
        """
        List available input devices.

        Returns:
            List of device names
        """
        try:
            recorder_cls = _get_pvrecorder_class()
            if recorder_cls is None:
                return []

            # PvRecorder may use get_audio_devices() or get_available_devices() depending on version
            if hasattr(recorder_cls, "get_audio_devices"):
                devices = recorder_cls.get_audio_devices()
            elif hasattr(recorder_cls, "get_available_devices"):
                devices = recorder_cls.get_available_devices()
            else:
                logger.debug("PvRecorder device enumeration method not found")
                return []
            return list(devices) if devices else []
        except Exception as e:
            logger.debug("Failed to list input devices: %s", e)
            return []

    def is_device_available(self, device_index: int) -> bool:
        """
        Check if a device index is available.

        Args:
            device_index: Device index to check

        Returns:
            True if device is available
        """
        devices = self.list_input_devices()
        return 0 <= device_index < len(devices)

    def get_stats(self) -> AudioDeviceManagerStats:
        """Expose device manager statistics."""
        return {
            "selection_attempts": self._selection_attempts,
            "selection_failures": self._selection_failures,
            "current_input_device": self._input_device,
            "current_output_device": self._output_device,
        }
