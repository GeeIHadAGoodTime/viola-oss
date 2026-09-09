"""Runtime profile detection and capability planning for Viola.

This module centralises heuristics that decide how the application should
configure itself based on the host hardware. The goal is to let a single build
scale from desktop-class PCs down to Raspberry Pi Zero voice pods without
manual reconfiguration, while keeping the heuristics transparent and
overrideable.

Vision alignment:
- ⚙️ Functional excellence on powerful devices.
- 🔒 Privacy-first operation with clear LLM routing decisions.
- 🍓 Hardware freedom through lightweight profiles on constrained hardware.
- 🔄 Unified presence via shared capability flags downstream services can inspect.

Key profiles:
- DESKTOP_FULL: Full-featured desktop experience
- PI_TOUCH: Raspberry Pi with display
- PI_VOICE: Raspberry Pi voice-only
- PI_ZERO_VOICE: Minimal Pi Zero configuration
- SPOKE_RELAY: Headless relay-only spoke with all local processing disabled
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from config import env
from contracts.api_response import ResponseEnvelope, success_response
from core.cpu_quota import effective_cpu_count
from core.json_types import to_json_value
from core.logging_config import get_logger

logger = get_logger(__name__)


def make_runtime_profile_payload(state: Any) -> ResponseEnvelope:
    """Build the response payload for GET /v1/system/profile."""
    if psutil is not None:
        memory = psutil.virtual_memory()
        memory_info: dict[str, Any] = {
            "total": memory.total,
            "available": memory.available,
            "percent": memory.percent,
        }
    else:
        memory_info = {"error": "psutil not available"}

    try:
        import GPUtil  # type: ignore[import-not-found]

        gpu_info = [
            {
                "id": gpu.id,
                "name": gpu.name,
                "memory_total": gpu.memoryTotal,
                "memory_free": gpu.memoryFree,
                "memory_used": gpu.memoryUsed,
                "temperature": gpu.temperature,
                "uuid": gpu.uuid,
            }
            for gpu in GPUtil.getGPUs()
        ]
    except ModuleNotFoundError:
        gpu_info = []

    profile = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": sys.version,
        },
        "memory": memory_info,
        "gpu": gpu_info,
        "paths": {
            "cwd": str(Path.cwd()),
            "executable": sys.executable,
        },
        "capabilities": to_json_value(getattr(state, "capabilities", {})),
    }
    return success_response(to_json_value(profile))


try:  # pragma: no cover - optional dependency
    import psutil
except Exception as e:  # pragma: no cover - optional dependency
    logger.debug("psutil not available (non-critical): %s", e, exc_info=True)
    psutil = None

try:  # pragma: no cover - reuse existing Pi detection when available
    from performance.pi_optimizations import is_raspberry_pi as _is_raspberry_pi
except Exception as e:  # pragma: no cover - fallback heuristics
    logger.debug(
        "Pi detection module not available, using fallback (non-critical): %s",
        e,
        exc_info=True,
    )

    def _is_raspberry_pi() -> bool:
        if sys.platform != "linux":
            return False
        try:
            with open("/proc/device-tree/model") as handle:
                return "raspberry" in handle.read().lower()
        except Exception as e:
            logger.debug("Failed to read /proc/device-tree/model: %s", e, exc_info=True)
            pass
        try:
            with open("/proc/cpuinfo") as handle:
                blob = handle.read().lower()
                return "bcm" in blob and "arm" in blob
        except Exception as e:
            logger.debug("Failed to read /proc/cpuinfo: %s", e, exc_info=True)
            pass
        return False


from core.metrics import runtime_capability_toggle_total, runtime_profile_selected_total


class RuntimeProfileName(str, Enum):
    """Supported runtime profiles."""

    DESKTOP_FULL = "desktop_full"
    PI_TOUCH = "pi_touch"
    PI_VOICE = "pi_voice"
    PI_ZERO_VOICE = "pi_zero_voice"
    SPOKE_RELAY = "spoke_relay"


@dataclass(frozen=True)
class ProfileCapabilities:
    """Capabilities toggled by a runtime profile."""

    has_display: bool
    enable_music: bool
    enable_voice: bool
    enable_wake_word: bool
    enable_tts: bool
    prefer_remote_llm: bool
    prefer_remote_stt: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_display": self.has_display,
            "enable_music": self.enable_music,
            "enable_voice": self.enable_voice,
            "enable_wake_word": self.enable_wake_word,
            "enable_tts": self.enable_tts,
            "prefer_remote_llm": self.prefer_remote_llm,
            "prefer_remote_stt": self.prefer_remote_stt,
        }


@dataclass(frozen=True)
class HardwareProbe:
    """Snapshot of the host hardware used for profile detection."""

    platform: str
    machine: str
    cpu_count: int
    memory_bytes: int | None
    has_display: bool
    is_raspberry_pi: bool
    pi_model: str | None = None


@dataclass(frozen=True)
class RuntimeProfile:
    """Resolved runtime profile."""

    name: RuntimeProfileName
    capabilities: ProfileCapabilities
    metadata: dict[str, Any] = field(default_factory=dict)


def _read_total_memory_bytes() -> int | None:
    """Best-effort attempt to read total system memory in bytes."""

    if psutil is not None:  # pragma: no branch - fast path when psutil present
        try:
            return int(psutil.virtual_memory().total)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Failed to read memory via psutil", exc_info=True)
            pass

    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as handle:
                for line in handle:
                    if line.lower().startswith("memtotal:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            # Value is in kB
                            return int(parts[1]) * 1024
        except Exception:  # pragma: no cover - defensive
            logger.debug("Failed to read /proc/meminfo", exc_info=True)
            return None
    return None


def _detect_display_presence() -> bool:
    """Detect whether a graphics display is available."""

    # NOTE: Using env.get() here is acceptable per CLAUDE.md - these are system-level
    # hardware detection variables, not application configuration settings.
    # See CLAUDE.md section 2: "Direct env.get() should only be used for system-level configuration"
    if env.get("VIOLA_FORCE_HEADLESS", "").lower() in {"1", "true", "yes"}:
        return False

    for env_var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM", "MIR_SOCKET"):
        if env.get(env_var):
            return True

    if sys.platform.startswith("win"):
        try:  # pragma: no cover - Windows specifics are hard to exercise in CI
            import ctypes

            user32 = ctypes.windll.user32
            return bool(user32.GetSystemMetrics(0))
        except Exception as e:
            logger.debug(
                "Failed to check Windows display presence (non-critical): %s",
                e,
                exc_info=True,
            )
            return True  # Assume yes if we cannot verify

    if sys.platform == "darwin":
        return True

    return os.path.exists("/dev/fb0")


def _read_pi_model() -> str | None:
    """Read Raspberry Pi model string when available."""

    try:
        with open("/proc/device-tree/model") as handle:
            return handle.read().strip()
    except Exception as e:
        logger.debug("Failed to read Pi model: %s", e, exc_info=True)
        return None


def _normalise_profile_name(name: str) -> RuntimeProfileName | None:
    """Normalise arbitrary user input into a profile enum."""

    name = name.strip().lower()
    for candidate in RuntimeProfileName:
        if candidate.value == name:
            return candidate
    return None


def probe_hardware() -> HardwareProbe:
    """Collect hardware characteristics used for profile selection.

    cpu_count is quota-aware (core.cpu_quota.effective_cpu_count): inside a
    cgroup-CPU-capped container (Docker --cpus / docker-compose
    deploy.resources.limits.cpus) this reports the container's real CPU
    budget rather than the host's raw os.cpu_count() (#4433/C-613). Desktop
    and any other non-quota-capped host see no change -- effective_cpu_count()
    falls back to os.cpu_count() there.
    """

    cpu_count = effective_cpu_count()
    memory_bytes = _read_total_memory_bytes()
    has_display = _detect_display_presence()
    is_pi = _is_raspberry_pi()
    pi_model = _read_pi_model() if is_pi else None

    return HardwareProbe(
        platform=platform.platform(),
        machine=platform.machine(),
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
        has_display=has_display,
        is_raspberry_pi=is_pi,
        pi_model=pi_model,
    )


def detect_runtime_profile(
    *,
    forced: str | None = None,
    probe: HardwareProbe | None = None,
) -> RuntimeProfile:
    """Detect the best-fit runtime profile for the current environment."""

    # NOTE: Using env.get() here is acceptable per CLAUDE.md - this is system-level
    # runtime profile override for hardware-specific configuration.
    # See CLAUDE.md section 2: "Direct env.get() should only be used for system-level configuration"
    env_override = env.get("VIOLA_RUNTIME_PROFILE")
    # NOTE: Using env.get() here is acceptable per CLAUDE.md - this is a
    # system-level hardware/runtime mode switch, not application settings.
    spoke_mode = env.get("VIOLA_SPOKE_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    chosen_name = forced or (RuntimeProfileName.SPOKE_RELAY.value if spoke_mode else env_override)

    if chosen_name:
        coerced = _normalise_profile_name(chosen_name)
        if not coerced:
            logger.warning(
                "Unknown runtime profile override '%s'; falling back to auto-detect",
                chosen_name,
            )
        else:
            profile = _build_profile(coerced, probe or probe_hardware())
            runtime_profile_selected_total.inc(profile=coerced.value, override="true")
            logger.info(
                "🔧 Runtime profile forced via override: %s | capabilities=%s",
                coerced.value,
                profile.capabilities.as_dict(),
            )
            return profile

    hw = probe or probe_hardware()
    profile_name = _auto_select_profile(hw)
    profile = _build_profile(profile_name, hw)
    runtime_profile_selected_total.inc(profile=profile_name.value, override="false")
    logger.info(
        "🔧 Runtime profile auto-detected: %s (cpu=%s cores, mem=%s MB, display=%s, pi=%s)",
        profile_name.value,
        hw.cpu_count,
        int(hw.memory_bytes / (1024 * 1024)) if hw.memory_bytes else "?",
        hw.has_display,
        hw.is_raspberry_pi,
    )
    return profile


def _auto_select_profile(hw: HardwareProbe) -> RuntimeProfileName:
    """Select a profile based on hardware heuristics."""

    if not hw.is_raspberry_pi:
        return RuntimeProfileName.DESKTOP_FULL

    total_mem = hw.memory_bytes or 0
    if hw.cpu_count <= 2 or (total_mem and total_mem <= 650 * 1024 * 1024):
        return RuntimeProfileName.PI_ZERO_VOICE

    if hw.has_display:
        return RuntimeProfileName.PI_TOUCH

    return RuntimeProfileName.PI_VOICE


def _build_profile(name: RuntimeProfileName, hw: HardwareProbe) -> RuntimeProfile:
    """Construct the runtime profile dataclass for the selected name."""

    if name is RuntimeProfileName.DESKTOP_FULL:
        capabilities = ProfileCapabilities(
            has_display=hw.has_display,
            enable_music=True,
            enable_voice=True,
            enable_wake_word=True,
            enable_tts=True,
            prefer_remote_llm=False,
            prefer_remote_stt=False,
        )
    elif name is RuntimeProfileName.PI_TOUCH:
        capabilities = ProfileCapabilities(
            has_display=True,
            enable_music=True,
            enable_voice=True,
            enable_wake_word=True,
            enable_tts=True,
            prefer_remote_llm=True,
            prefer_remote_stt=False,
        )
    elif name is RuntimeProfileName.PI_VOICE:
        capabilities = ProfileCapabilities(
            has_display=False,
            enable_music=True,
            enable_voice=True,
            enable_wake_word=True,
            enable_tts=True,
            prefer_remote_llm=True,
            prefer_remote_stt=True,
        )
    elif name is RuntimeProfileName.PI_ZERO_VOICE:
        capabilities = ProfileCapabilities(
            has_display=False,
            enable_music=True,
            enable_voice=True,
            enable_wake_word=False,
            enable_tts=False,
            prefer_remote_llm=True,
            prefer_remote_stt=True,
        )
    else:  # RuntimeProfileName.SPOKE_RELAY
        capabilities = ProfileCapabilities(
            has_display=False,
            enable_music=False,
            enable_voice=False,
            enable_wake_word=False,
            enable_tts=False,
            prefer_remote_llm=True,
            prefer_remote_stt=True,
        )

    metadata: dict[str, Any] = {
        "platform": hw.platform,
        "machine": hw.machine,
        "cpu_count": hw.cpu_count,
        "memory_bytes": hw.memory_bytes,
        "has_display": hw.has_display,
        "is_raspberry_pi": hw.is_raspberry_pi,
        "pi_model": hw.pi_model,
    }

    return RuntimeProfile(name=name, capabilities=capabilities, metadata=metadata)


def apply_profile_to_settings(settings: Any, profile: RuntimeProfile) -> None:
    """Mutate the settings object according to the runtime profile."""

    if not settings:
        return

    try:
        settings.runtime_profile = profile.name.value
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Failed to set runtime_profile (non-critical): %s", e, exc_info=True)

    try:
        settings.runtime_capabilities = profile.capabilities.as_dict()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Failed to set runtime_capabilities (non-critical): %s", e, exc_info=True)

    if profile.name is not RuntimeProfileName.DESKTOP_FULL:
        if not getattr(settings, "lightweight_mode", False):
            logger.info("🍓 Enabling lightweight mode for profile %s", profile.name.value)
            settings.lightweight_mode = True

        if profile.name is RuntimeProfileName.PI_ZERO_VOICE:
            if getattr(settings, "whisper_model", "") != "tiny":
                logger.info("🍓 Forcing Whisper model to 'tiny' for Pi Zero profile")
                settings.whisper_model = "tiny"
            if getattr(settings, "enable_gpt", True):
                logger.info("🍓 Preferring remote LLM for Pi Zero profile; ensure cloud backend is configured")
        elif profile.capabilities.prefer_remote_llm:
            if getattr(settings, "llm_backend", "openai") == "ollama":
                logger.info(
                    "🍓 Runtime profile %s prefers remote LLMs but local backend is forced; leaving as-is (user override)",
                    profile.name.value,
                )

    if not profile.capabilities.enable_wake_word and hasattr(settings, "wake_enabled"):
        if getattr(settings, "wake_enabled", False):
            logger.info(
                "🍓 Disabling wake word for profile %s due to hardware limits",
                profile.name.value,
            )
            settings.wake_enabled = False

    if not profile.capabilities.enable_tts and hasattr(settings, "tts_enabled"):
        if getattr(settings, "tts_enabled", True):
            logger.info(
                "🍓 Disabling local TTS for profile %s; responses should be streamed from hub",
                profile.name.value,
            )
            settings.tts_enabled = False

    if profile.capabilities.prefer_remote_stt and hasattr(settings, "stt_backend"):
        backend = settings.stt_backend
        if backend == "whisper_local":
            logger.info(
                "🍓 Profile %s prefers remote STT; keep local backend but expect offload",
                profile.name.value,
            )

    capabilities = profile.capabilities.as_dict()
    for capability_name, value in capabilities.items():
        if isinstance(value, bool):
            normalized_value = "true" if value else "false"
        else:
            normalized_value = str(value)
        runtime_capability_toggle_total.inc(capability=capability_name, value=normalized_value)

    logger.info(
        "Runtime profile %s applied with capabilities: %s",
        profile.name.value,
        capabilities,
    )
