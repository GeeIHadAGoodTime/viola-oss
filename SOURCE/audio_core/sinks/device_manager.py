"""
Audio sink device management.

The device manager provides a central registry of playback devices, exposes
hot-plug notifications, and translates configuration profiles into
`SinkConfiguration` instances consumable by sinks such as WASAPI.

Highlights
----------
* Polling-based hot-plug detection with optional platform specific hooks.
* YAML-backed configuration for persisted defaults and latency profiles.
* Thread-safe APIs suitable for use by the audio core orchestrator.
* Telemetry emission for device churn to aid Batch A governance reporting.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from audio_core.portaudio_guard import open_portaudio, terminate_portaudio
from core.constants import SAMPLE_RATE_44K, SAMPLE_RATE_48K
from core.logging_config import get_logger

from .sink_interface import (
    AudioDeviceDescriptor,
    PCMFormat,
    SinkConfiguration,
    SinkTelemetry,
)

DeviceCallback = Callable[[AudioDeviceDescriptor, bool], None]


class DeviceEnumerationError(RuntimeError):
    """Raised when device enumeration fails unexpectedly."""


class DeviceEnumerator(Protocol):
    """Protocol for device enumeration providers."""

    def enumerate(self) -> Sequence[AudioDeviceDescriptor]: ...


class _NullEnumerator:
    """Fallback enumerator returning no devices."""

    def enumerate(self) -> Sequence[AudioDeviceDescriptor]:
        return ()


class _PyAudioEnumerator:
    """Enumerate devices via PyAudio when available."""

    def __init__(self) -> None:
        # open_portaudio() imports pyaudio internally and serializes Pa_Initialize
        # under the process-wide lock; a missing pyaudio still raises ImportError,
        # which _coalesce_enumerator suppresses to fall back to the null enumerator.
        self._pyaudio = open_portaudio()

    def enumerate(self) -> Sequence[AudioDeviceDescriptor]:
        descriptors: list[AudioDeviceDescriptor] = []
        device_count = self._pyaudio.get_device_count()
        for index in range(device_count):
            try:
                info = self._pyaudio.get_device_info_by_index(index)
            except Exception as exc:  # pragma: no cover - defensive
                raise DeviceEnumerationError(str(exc)) from exc

            if int(info.get("maxOutputChannels", 0)) <= 0:
                continue

            default_sr = int(info.get("defaultSampleRate", SAMPLE_RATE_44K))
            try:
                default_format = PCMFormat(
                    sample_rate_hz=default_sr,
                    channels=max(1, int(info.get("maxOutputChannels", 2))),
                    bits_per_sample=16,
                )
            except ValueError:
                default_format = PCMFormat(
                    sample_rate_hz=SAMPLE_RATE_44K,
                    channels=2,
                    bits_per_sample=16,
                )

            descriptors.append(
                AudioDeviceDescriptor(
                    id=f"{info.get('name', 'Device')!s}_{index}",
                    name=str(info.get("name", f"Device {index}")),
                    api=str(info.get("hostApi", "pyaudio")),
                    is_default=bool(info.get("defaultOutputDevice")),
                    sample_rates_hz=(default_sr, SAMPLE_RATE_44K, SAMPLE_RATE_48K),
                    max_channels=int(info.get("maxOutputChannels", 2)),
                    default_format=default_format,
                    latency_ms=int(float(info.get("defaultLowOutputLatency", 0.05)) * 1000),
                    capabilities=("shared-mode",),
                )
            )

        return tuple(descriptors)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            terminate_portaudio(self._pyaudio)


class _SoundDeviceEnumerator:
    """Enumerate devices using the `sounddevice` module."""

    def __init__(self) -> None:
        import sounddevice as sd

        self._sd = sd
        query_hostapis = getattr(sd, "query_hostapis", None)
        self._host_apis = query_hostapis() if callable(query_hostapis) else []

    def enumerate(self) -> Sequence[AudioDeviceDescriptor]:
        from audio_core.portaudio_guard import sounddevice_guard

        with sounddevice_guard():
            devices = self._sd.query_devices()
        descriptors: list[AudioDeviceDescriptor] = []

        for index, info in enumerate(devices):
            # Skip string entries (invalid devices)
            if isinstance(info, str):
                continue

            if info.get("max_output_channels", 0) <= 0:
                continue

            hostapi_idx_value = info.get("hostapi")
            hostapi_idx = hostapi_idx_value if isinstance(hostapi_idx_value, int) else None
            host_name = "sounddevice"
            default_output_idx: int | None = None
            if hostapi_idx is not None and 0 <= hostapi_idx < len(self._host_apis):
                host_api_info = self._host_apis[hostapi_idx]
                if isinstance(host_api_info, dict):
                    host_name_value = host_api_info.get("name")
                    host_name = host_name_value if isinstance(host_name_value, str) else host_name
                    default_output_value = host_api_info.get("default_output_device")
                    default_output_idx = default_output_value if isinstance(default_output_value, int) else None

            default_sr_value = info.get("default_samplerate")
            default_sr = int(default_sr_value) if isinstance(default_sr_value, (int, float)) else SAMPLE_RATE_44K
            channels_value = info.get("max_output_channels")
            channels = int(channels_value) if isinstance(channels_value, int) and channels_value > 0 else 2
            name_value = info.get("name")
            name = name_value if isinstance(name_value, str) else f"Device {index}"
            latency_value = info.get("default_low_output_latency")
            latency_seconds = float(latency_value) if isinstance(latency_value, (int, float)) else 0.05

            descriptors.append(
                AudioDeviceDescriptor(
                    id=f"{name}_{index}",
                    name=name,
                    api=f"sounddevice/{host_name}",
                    is_default=default_output_idx == index,
                    sample_rates_hz=tuple(sorted({SAMPLE_RATE_44K, SAMPLE_RATE_48K, default_sr})),
                    max_channels=channels,
                    default_format=PCMFormat(
                        sample_rate_hz=default_sr,
                        channels=channels,
                        bits_per_sample=16,
                    ),
                    latency_ms=int(latency_seconds * 1000),
                    capabilities=("shared-mode",),
                )
            )

        return tuple(descriptors)


def _coalesce_enumerator() -> DeviceEnumerator:
    """Return the first available enumerator based on installed backends."""
    for ctor in (_SoundDeviceEnumerator, _PyAudioEnumerator):
        with contextlib.suppress(ImportError, OSError, AttributeError, DeviceEnumerationError):
            return ctor()
    return _NullEnumerator()


def _default_config_path() -> Path:
    # Anchor on the project/bundle root, not cwd — in a frozen install cwd is the
    # install dir and the bundled config lives under get_project_root()/config.
    from core.platform import get_project_root

    return get_project_root() / "config" / "audio_devices.yaml"


@dataclass(slots=True)
class DeviceManagerConfig:
    """Configurable values parsed from YAML."""

    preferred_device_id: str | None = None
    latency_profile: str = "standard"
    latency_profiles: dict[str, dict[str, object]] = field(default_factory=dict)


class DeviceManager:
    """
    Central registry for output devices with hot-plug detection.

    The manager polls the underlying enumerator and diffs results to detect
    additions/removals. Callbacks execute outside the internal lock to avoid
    deadlocks.
    """

    def __init__(
        self,
        *,
        telemetry: SinkTelemetry | None = None,
        enumerator: DeviceEnumerator | None = None,
        logger: logging.Logger | None = None,
        poll_interval_sec: float = 5.0,
        config_path: Path | None = None,
    ) -> None:
        self._logger = logger or get_logger("audio_core.sinks.device_manager")
        self._telemetry = telemetry
        self._enumerator = enumerator or _coalesce_enumerator()
        self._callbacks: list[DeviceCallback] = []
        self._lock = threading.RLock()
        self._devices: dict[str, AudioDeviceDescriptor] = {}
        self._poll_interval = max(0.0, poll_interval_sec)
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._config_path = Path(config_path) if config_path else _default_config_path()
        self._config = DeviceManagerConfig()
        self._load_config()

        self.refresh_devices(force_emit=True)

        if self._poll_interval > 0:
            self._poll_thread = threading.Thread(target=self._poll_loop, name="audio-device-manager", daemon=True)
            self._poll_thread.start()

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #

    def _load_config(self) -> None:
        with contextlib.suppress(FileNotFoundError, yaml.YAMLError, OSError):
            data = self._config_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(data) or {}
            defaults = parsed.get("defaults", {})
            latency_profiles = parsed.get("latency_profiles", {})
            self._config = DeviceManagerConfig(
                preferred_device_id=defaults.get("preferred_device_id"),
                latency_profile=defaults.get("latency_profile", "standard"),
                latency_profiles=latency_profiles or {},
            )

    def _save_config(self) -> None:
        payload = {
            "defaults": {
                "preferred_device_id": self._config.preferred_device_id,
                "latency_profile": self._config.latency_profile,
            },
            "latency_profiles": self._config.latency_profiles,
        }
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        """Stop background polling thread."""
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=self._poll_interval + 1.0)

    def register_callback(self, callback: DeviceCallback) -> None:
        """Register device hot-plug callback."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def unregister_callback(self, callback: DeviceCallback) -> None:
        """Remove device callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def list_devices(self) -> Sequence[AudioDeviceDescriptor]:
        """Return current snapshot of devices."""
        with self._lock:
            return tuple(sorted(self._devices.values(), key=lambda d: (not d.is_default, d.name)))

    def get_device(self, device_id: str) -> AudioDeviceDescriptor | None:
        """Return specific device or None."""
        with self._lock:
            return self._devices.get(device_id)

    def refresh_devices(self, *, force_emit: bool = False) -> Sequence[AudioDeviceDescriptor]:
        """Enumerate devices and emit callbacks for changes."""
        snapshot = self._enumerator.enumerate()
        added: list[AudioDeviceDescriptor] = []
        removed: list[AudioDeviceDescriptor] = []
        updated_map: dict[str, AudioDeviceDescriptor] = {}

        with self._lock:
            current_ids = set(self._devices.keys())
            new_ids = {device.id for device in snapshot}

            for device in snapshot:
                updated_map[device.id] = device
                if force_emit or device.id not in current_ids:
                    added.append(device)

            for stale_id in current_ids - new_ids:
                removed.append(self._devices[stale_id])

            self._devices = updated_map

        for device in added:
            self._emit_device_event(device, plugged_in=True)
        for device in removed:
            self._emit_device_event(device, plugged_in=False)

        return snapshot

    def _emit_device_event(self, device: AudioDeviceDescriptor, *, plugged_in: bool) -> None:
        event_type = "device_plugged" if plugged_in else "device_unplugged"
        self._logger.info("audio_device_event", extra={"event": event_type, "device": device.id})

        callbacks = self._snapshot_callbacks()
        for callback in callbacks:
            try:
                callback(device, plugged_in)
            except Exception:  # pragma: no cover - isolation
                self._logger.exception("Device callback failed")

        if self._telemetry:
            with contextlib.suppress(Exception):
                self._telemetry.emit_device_event(
                    sink_id=device.id,
                    event=event_type,
                    details={
                        "name": device.name,
                        "api": device.api,
                        "latency_ms": device.latency_ms,
                        "is_default": device.is_default,
                    },
                )

    def _snapshot_callbacks(self) -> tuple[DeviceCallback, ...]:
        with self._lock:
            return tuple(self._callbacks)

    # ------------------------------------------------------------------ #
    # Preferred device helpers
    # ------------------------------------------------------------------ #

    def get_preferred_device(self) -> AudioDeviceDescriptor | None:
        with self._lock:
            if not self._config.preferred_device_id:
                return None
            return self._devices.get(self._config.preferred_device_id)

    def set_preferred_device(self, device_id: str | None) -> None:
        with self._lock:
            if device_id and device_id not in self._devices:
                raise ValueError(f"Unknown device id {device_id}")
            self._config.preferred_device_id = device_id
            self._save_config()

    def resolve_device(self, fallback_to_default: bool = True) -> AudioDeviceDescriptor | None:
        preferred = self.get_preferred_device()
        if preferred:
            return preferred

        if not fallback_to_default:
            return None

        devices = self.list_devices()
        for device in devices:
            if device.is_default:
                return device
        return devices[0] if devices else None

    # ------------------------------------------------------------------ #
    # Configuration profiles → SinkConfiguration
    # ------------------------------------------------------------------ #

    def latency_profiles(self) -> Iterable[str]:
        return tuple(self._config.latency_profiles.keys())

    def set_latency_profile(self, profile_name: str) -> None:
        if profile_name not in self._config.latency_profiles:
            raise ValueError(f"Unknown latency profile {profile_name}")
        self._config.latency_profile = profile_name
        self._save_config()

    def build_sink_configuration(
        self,
        *,
        stream_format: PCMFormat,
        profile: str | None = None,
        device_id: str | None = None,
    ) -> SinkConfiguration:
        profile_name = profile or self._config.latency_profile
        profile_cfg = self._config.latency_profiles.get(profile_name)
        if profile_cfg is None:
            raise ValueError(f"Latency profile {profile_name} not defined")

        target_device: AudioDeviceDescriptor | None = None
        if device_id:
            target_device = self.get_device(device_id)
            if target_device is None:
                raise ValueError(f"Device {device_id} not available")
        else:
            target_device = self.resolve_device()

        latency_target = profile_cfg.get("latency_target_ms", 120)
        latency_ms = int(latency_target) if isinstance(latency_target, (int, float, str)) else 120
        buffer_frames = profile_cfg.get("buffer_frames")
        shared_mode = bool(profile_cfg.get("shared_mode", True))
        exclusive_mode = bool(profile_cfg.get("exclusive_mode", False))

        if shared_mode and exclusive_mode:
            raise ValueError("Latency profile cannot enable shared and exclusive modes")

        telemetry_label = target_device.name if target_device else None
        device_identifier = target_device.id if target_device else None

        return SinkConfiguration(
            format=stream_format,
            device_id=device_identifier,
            latency_target_ms=latency_ms,
            buffer_frames=(
                int(buffer_frames)
                if buffer_frames is not None and isinstance(buffer_frames, (int, float, str))
                else None
            ),
            shared_mode=shared_mode,
            exclusive_mode=exclusive_mode,
            telemetry_device_label=telemetry_label,
        )

    # ------------------------------------------------------------------ #
    # Background polling
    # ------------------------------------------------------------------ #

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_devices()
            except Exception:  # pragma: no cover - defensive guard around polling
                self._logger.exception("Device polling failed")
            finished = self._stop_event.wait(self._poll_interval)
            if finished:
                break


__all__ = [
    "AudioDeviceDescriptor",
    "DeviceEnumerationError",
    "DeviceEnumerator",
    "DeviceManager",
    "DeviceManagerConfig",
]
