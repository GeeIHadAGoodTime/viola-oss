"""Machine-wide system volume control for agent tools.

This controls the operating system output device, not Viola's music player
volume or user settings.  The implementation returns structured state so the
LLM can write the user-facing response itself.
"""

from __future__ import annotations

import asyncio
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

MIN_VOLUME_PERCENT = 0
MAX_VOLUME_PERCENT = 100
DEFAULT_VOLUME_STEP = 10
COMMAND_TIMEOUT_SECONDS = 5

_SUPPORTED_ACTIONS = frozenset({"get", "set", "mute", "unmute", "up", "down"})
_ACTION_ALIASES = {
    "read": "get",
    "status": "get",
    "volume_up": "up",
    "increase": "up",
    "louder": "up",
    "volume_down": "down",
    "decrease": "down",
    "quieter": "down",
}


class SystemVolumeError(RuntimeError):
    """Raised when system volume cannot be read or changed."""


@dataclass(frozen=True)
class VolumeState:
    """Current OS output volume state."""

    percent: int
    muted: bool


# Type-only backend contract kept for injectable platform implementations.
class VolumeBackend(Protocol):
    """Small interface implemented by each OS volume backend."""

    platform_name: str

    def get(self) -> VolumeState:
        """Read current volume state."""

    def set_percent(self, percent: int) -> None:
        """Set output volume percent."""

    def set_muted(self, muted: bool) -> None:
        """Set output mute state."""


def _percent_from_scalar(value: float) -> int:
    return max(MIN_VOLUME_PERCENT, min(MAX_VOLUME_PERCENT, round(value * MAX_VOLUME_PERCENT)))


def _validate_percent(value: object, *, field_name: str = "percent") -> int:
    if isinstance(value, bool) or value is None:
        raise SystemVolumeError("%s must be an integer from 0 to 100" % field_name)
    try:
        percent = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemVolumeError("%s must be an integer from 0 to 100" % field_name) from exc
    if percent < MIN_VOLUME_PERCENT or percent > MAX_VOLUME_PERCENT:
        raise SystemVolumeError("%s must be between 0 and 100" % field_name)
    return percent


def _validate_step(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise SystemVolumeError("step must be an integer from 1 to 100")
    try:
        step = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemVolumeError("step must be an integer from 1 to 100") from exc
    if step < 1 or step > MAX_VOLUME_PERCENT:
        raise SystemVolumeError("step must be between 1 and 100")
    return step


def _coerce_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on", "muted", "mute"}:
            return True
        if normalized in {"false", "0", "no", "off", "unmuted", "unmute"}:
            return False
    raise SystemVolumeError("mute must be a boolean when provided")


def _normalize_action(action: str | None) -> str:
    normalized = (action or "get").strip().lower().replace("-", "_")
    normalized = _ACTION_ALIASES.get(normalized, normalized)
    if normalized not in _SUPPORTED_ACTIONS:
        raise SystemVolumeError("Unknown action: %s. Use get, set, mute, unmute, up, or down." % (action or ""))
    return normalized


def _run_native_command(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise SystemVolumeError("Required command not found: %s" % args[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemVolumeError("%s timed out while controlling system volume" % args[0]) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if not detail:
            detail = "exit code %d" % completed.returncode
        raise SystemVolumeError("%s failed: %s" % (args[0], detail))

    return completed.stdout.strip()


class WindowsPycawVolumeBackend:
    """Windows Core Audio backend using pycaw/comtypes."""

    platform_name = "windows"

    def _call_with_com(self, func):
        try:
            from comtypes import CoInitialize, CoUninitialize
        except ImportError as exc:
            raise SystemVolumeError(
                "Windows system volume control requires pycaw and comtypes. Install the desktop requirements."
            ) from exc

        CoInitialize()
        try:
            return func()
        finally:
            try:
                CoUninitialize()
            except (OSError, RuntimeError):
                logger.debug("COM uninitialization failed after system volume call")

    @staticmethod
    def _endpoint():
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError as exc:
            raise SystemVolumeError(
                "Windows system volume control requires pycaw and comtypes. Install the desktop requirements."
            ) from exc

        devices = AudioUtilities.GetSpeakers()
        endpoint_volume = getattr(devices, "EndpointVolume", None)
        if endpoint_volume is not None:
            return endpoint_volume

        activate = getattr(devices, "Activate", None)
        if callable(activate):
            interface = activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return interface.QueryInterface(IAudioEndpointVolume)

        raw_device = getattr(devices, "_dev", None)
        raw_activate = getattr(raw_device, "Activate", None)
        if callable(raw_activate):
            interface = raw_activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return interface.QueryInterface(IAudioEndpointVolume)

        raise SystemVolumeError("Could not access the Windows default audio endpoint volume interface")

    def get(self) -> VolumeState:
        def read_state() -> VolumeState:
            endpoint = self._endpoint()
            return VolumeState(
                percent=_percent_from_scalar(float(endpoint.GetMasterVolumeLevelScalar())),
                muted=bool(endpoint.GetMute()),
            )

        return self._call_with_com(read_state)

    def set_percent(self, percent: int) -> None:
        def write_percent() -> None:
            endpoint = self._endpoint()
            endpoint.SetMasterVolumeLevelScalar(percent / MAX_VOLUME_PERCENT, None)

        self._call_with_com(write_percent)

    def set_muted(self, muted: bool) -> None:
        def write_muted() -> None:
            endpoint = self._endpoint()
            endpoint.SetMute(1 if muted else 0, None)

        self._call_with_com(write_muted)


class MacOSVolumeBackend:
    """macOS output volume backend using osascript."""

    platform_name = "macos"

    def get(self) -> VolumeState:
        output = _run_native_command(
            [
                "osascript",
                "-e",
                "set v to output volume of (get volume settings)\n"
                "set m to output muted of (get volume settings)\n"
                'return (v as text) & "," & (m as text)',
            ]
        )
        parts = [part.strip().lower() for part in output.split(",", maxsplit=1)]
        if len(parts) != 2:
            raise SystemVolumeError("Could not parse macOS volume state: %s" % output)
        return VolumeState(percent=_validate_percent(parts[0]), muted=parts[1] == "true")

    def set_percent(self, percent: int) -> None:
        _run_native_command(["osascript", "-e", "set volume output volume %d" % percent])

    def set_muted(self, muted: bool) -> None:
        _run_native_command(["osascript", "-e", "set volume output muted %s" % ("true" if muted else "false")])


class LinuxPactlVolumeBackend:
    """Linux PulseAudio/PipeWire backend using pactl."""

    platform_name = "linux"

    def get(self) -> VolumeState:
        volume_output = _run_native_command(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        mute_output = _run_native_command(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])

        volume_match = re.search(r"(\d+)%", volume_output)
        if volume_match is None:
            raise SystemVolumeError("Could not parse pactl volume output: %s" % volume_output)

        mute_match = re.search(r"Mute:\s*(yes|no)", mute_output, re.IGNORECASE)
        if mute_match is None:
            raise SystemVolumeError("Could not parse pactl mute output: %s" % mute_output)

        return VolumeState(
            percent=_validate_percent(volume_match.group(1)),
            muted=mute_match.group(1).lower() == "yes",
        )

    def set_percent(self, percent: int) -> None:
        _run_native_command(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "%d%%" % percent])

    def set_muted(self, muted: bool) -> None:
        _run_native_command(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"])


def _backend_for_current_os() -> VolumeBackend:
    system = platform.system().lower()
    if system == "windows":
        return WindowsPycawVolumeBackend()
    if system == "darwin":
        return MacOSVolumeBackend()
    if system == "linux":
        return LinuxPactlVolumeBackend()
    raise SystemVolumeError("System volume control is not supported on %s" % (platform.system() or "this OS"))


def _run_action(
    backend: VolumeBackend,
    *,
    action: str,
    percent: object = None,
    mute: object = None,
    step: object = DEFAULT_VOLUME_STEP,
) -> dict[str, object]:
    normalized_action = _normalize_action(action)
    requested_mute = _coerce_optional_bool(mute)
    before = backend.get()

    applied_percent: int | None = None
    applied_mute: bool | None = None

    if normalized_action == "set":
        applied_percent = _validate_percent(percent)
        backend.set_percent(applied_percent)
        if requested_mute is not None:
            applied_mute = requested_mute
            backend.set_muted(requested_mute)
    elif normalized_action == "mute":
        applied_mute = True
        backend.set_muted(True)
    elif normalized_action == "unmute":
        applied_mute = False
        backend.set_muted(False)
    elif normalized_action == "up":
        delta = _validate_step(step)
        applied_percent = min(MAX_VOLUME_PERCENT, before.percent + delta)
        backend.set_percent(applied_percent)
    elif normalized_action == "down":
        delta = _validate_step(step)
        applied_percent = max(MIN_VOLUME_PERCENT, before.percent - delta)
        backend.set_percent(applied_percent)

    after = backend.get()
    payload: dict[str, object] = {
        "action": normalized_action,
        "platform": backend.platform_name,
        "percent": after.percent,
        "muted": after.muted,
    }

    if normalized_action != "get":
        payload.update(
            {
                "previous_percent": before.percent,
                "previous_muted": before.muted,
            }
        )
    if applied_percent is not None:
        payload["applied_percent"] = applied_percent
    if applied_mute is not None:
        payload["applied_mute"] = applied_mute

    return payload


async def desktop_volume(
    action: str = "get",
    percent: int | None = None,
    mute: bool | None = None,
    step: int = DEFAULT_VOLUME_STEP,
) -> ToolResult:
    """Read or change the machine-wide system output volume."""
    try:
        backend = _backend_for_current_os()
        payload = await asyncio.to_thread(
            _run_action,
            backend,
            action=action,
            percent=percent,
            mute=mute,
            step=step,
        )
        return ToolResult(ok=True, data=payload)
    except SystemVolumeError as exc:
        return ToolResult(ok=False, error=str(exc))
    except Exception as exc:
        logger.exception("desktop_volume failed for action=%s", action)
        return ToolResult(ok=False, error="System volume control failed: %s" % exc)
