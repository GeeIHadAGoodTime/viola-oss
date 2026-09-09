"""
Startup validation for audio input/output devices.

Verifies that configured audio devices exist and are usable before the
application enters the main loop. Falls back to system defaults when
configured devices are unavailable, logging clear warnings so the user
knows which device is actually in use.

Usage (from bootstrap)::

    from audio_core.device_validation import validate_audio_devices

    status = validate_audio_devices()
    if not status.input_ok:
        logger.warning("Input device problem - see startup warnings")
    if not status.output_ok:
        logger.warning("Output device problem - see startup warnings")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from audio_core.portaudio_guard import sounddevice_guard
from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class AudioDeviceStatus:
    """Result of startup audio device validation."""

    input_ok: bool = False
    output_ok: bool = False
    input_name: str = ""
    output_name: str = ""
    input_fallback: bool = False
    output_fallback: bool = False


def _find_device_by_name(
    devices: list[dict[str, Any]],
    name: str,
    *,
    direction: str,
) -> int | None:
    """Search for a device by name substring match.

    Parameters
    ----------
    devices:
        List of device info dicts from ``sounddevice.query_devices()``.
    name:
        Device name (or substring) to search for.
    direction:
        ``"input"`` or ``"output"`` -- used to filter by channel count.

    Returns
    -------
    Device index or ``None`` if not found.
    """
    channel_key = "max_input_channels" if direction == "input" else "max_output_channels"

    # Exact match first
    for idx, dev in enumerate(devices):
        if not isinstance(dev, dict):
            continue
        dev_name = dev.get("name", "")
        if not isinstance(dev_name, str):
            continue
        channels = dev.get(channel_key, 0)
        if not isinstance(channels, (int, float)) or int(channels) <= 0:
            continue
        if dev_name == name:
            return idx

    # Substring match as fallback
    name_lower = name.lower()
    for idx, dev in enumerate(devices):
        if not isinstance(dev, dict):
            continue
        dev_name = dev.get("name", "")
        if not isinstance(dev_name, str):
            continue
        channels = dev.get(channel_key, 0)
        if not isinstance(channels, (int, float)) or int(channels) <= 0:
            continue
        if name_lower in dev_name.lower():
            return idx

    return None


def _get_default_device_name(
    sd: Any,
    devices: list[dict[str, Any]],
    *,
    direction: str,
) -> str:
    """Return the name of the system default device for the given direction."""
    try:
        with sounddevice_guard():
            defaults = sd.default.device
        idx = defaults[0] if direction == "input" else defaults[1]
        if idx is not None and isinstance(idx, int) and 0 <= idx < len(devices):
            dev = devices[idx]
            if isinstance(dev, dict):
                name = dev.get("name", "")
                if isinstance(name, str) and name:
                    return name
    except Exception:
        logger.debug("Could not query default device name, using system default")
    return "(system default)"


def _configured_device_name(setting_key: str, app_config_value: object) -> str | None:
    """Return the user-selected device name, treating blank values as default."""
    try:
        from ui.settings_manager import get_settings_manager

        raw_value = get_settings_manager().get(setting_key, "")
    except Exception:
        raw_value = app_config_value

    if raw_value in (None, ""):
        return None
    return str(raw_value)


def _validate_with_sounddevice() -> AudioDeviceStatus:
    """Run device validation using the sounddevice library."""
    with sounddevice_guard():
        import sounddevice as sd

        devices = list(sd.query_devices())

    status = AudioDeviceStatus()

    configured_input = _configured_device_name("input_device", settings.input_device)
    configured_output = _configured_device_name("output_device", settings.output_device)

    # --- Input device validation ---
    if configured_input is None:
        # None means "use system default" -- always OK
        status.input_ok = True
        status.input_name = _get_default_device_name(
            sd,
            devices,
            direction="input",
        )
        status.input_fallback = False
        logger.info(
            "Input device: using system default (%s)",
            status.input_name,
        )
    else:
        idx = _find_device_by_name(devices, configured_input, direction="input")
        if idx is not None:
            dev = devices[idx]
            status.input_ok = True
            status.input_name = dev.get("name", configured_input) if isinstance(dev, dict) else configured_input
            status.input_fallback = False
            logger.info("Input device validated: %s", status.input_name)
        else:
            # Configured device not found -- fall back to system default
            status.input_ok = True
            status.input_name = _get_default_device_name(
                sd,
                devices,
                direction="input",
            )
            status.input_fallback = True
            logger.warning(
                "Configured input device '%s' not found; " "falling back to system default (%s)",
                configured_input,
                status.input_name,
            )

    # --- Output device validation ---
    if configured_output is None:
        status.output_ok = True
        status.output_name = _get_default_device_name(
            sd,
            devices,
            direction="output",
        )
        status.output_fallback = False
        logger.info(
            "Output device: using system default (%s)",
            status.output_name,
        )
    else:
        idx = _find_device_by_name(
            devices,
            configured_output,
            direction="output",
        )
        if idx is not None:
            dev = devices[idx]
            status.output_ok = True
            status.output_name = dev.get("name", configured_output) if isinstance(dev, dict) else configured_output
            status.output_fallback = False
            logger.info("Output device validated: %s", status.output_name)
        else:
            status.output_ok = True
            status.output_name = _get_default_device_name(
                sd,
                devices,
                direction="output",
            )
            status.output_fallback = True
            logger.warning(
                "Configured output device '%s' not found; " "falling back to system default (%s)",
                configured_output,
                status.output_name,
            )

    # Check that system defaults actually exist (no audio hardware at all)
    try:
        with sounddevice_guard():
            default_input_idx = sd.default.device[0]
        if default_input_idx is None or (isinstance(default_input_idx, int) and default_input_idx < 0):
            if configured_input is None or status.input_fallback:
                status.input_ok = False
                status.input_name = "(no input device available)"
                logger.warning("No default input device available on this system")
    except Exception:
        if configured_input is None or status.input_fallback:
            status.input_ok = False
            status.input_name = "(no input device available)"
            logger.warning("Could not determine default input device")

    try:
        with sounddevice_guard():
            default_output_idx = sd.default.device[1]
        if default_output_idx is None or (isinstance(default_output_idx, int) and default_output_idx < 0):
            if configured_output is None or status.output_fallback:
                status.output_ok = False
                status.output_name = "(no output device available)"
                logger.warning(
                    "No default output device available on this system",
                )
    except Exception:
        if configured_output is None or status.output_fallback:
            status.output_ok = False
            status.output_name = "(no output device available)"
            logger.warning("Could not determine default output device")

    return status


def validate_audio_devices() -> AudioDeviceStatus:
    """Validate that configured audio devices exist and are usable.

    Checks the ``input_device`` and ``output_device`` values from
    ``config.settings``.  When a configured device cannot be found the
    function falls back to the system default and sets the corresponding
    ``*_fallback`` flag on the returned status object.

    When ``input_device`` or ``output_device`` is ``None`` in settings
    the system default is used and validation passes automatically
    (assuming a default device exists).

    Returns
    -------
    AudioDeviceStatus
        Dataclass summarising which devices were selected and whether
        any fallbacks occurred.
    """
    try:
        return _validate_with_sounddevice()
    except ImportError:
        logger.warning(
            "sounddevice not installed; audio device validation skipped. "
            "Install via 'pip install sounddevice' for device checks.",
        )
        return AudioDeviceStatus(
            input_ok=True,
            output_ok=True,
            input_name="(validation skipped - sounddevice not installed)",
            output_name="(validation skipped - sounddevice not installed)",
            input_fallback=False,
            output_fallback=False,
        )
    except Exception:
        logger.exception("Audio device validation failed unexpectedly")
        return AudioDeviceStatus(
            input_ok=False,
            output_ok=False,
            input_name="(validation error)",
            output_name="(validation error)",
            input_fallback=False,
            output_fallback=False,
        )


__all__ = ["AudioDeviceStatus", "validate_audio_devices"]
