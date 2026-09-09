"""
Device Profile Manager
======================

Manages per-device calibration profiles for wake word detection.

Each audio device setup gets its own profile containing:
- AEC delay calibration
- Learned threshold offset
- Calibration timestamps

This enables "set and forget" auto-tuning by:
1. Fingerprinting the current audio device setup
2. Loading stored profiles on startup
3. Triggering re-calibration when devices change

Usage:
    from voice.wake_detector.device_profile_manager import (
        get_device_fingerprint,
        load_profile,
        save_profile,
        get_or_create_profile,
    )

    # Get current device fingerprint
    device_id = get_device_fingerprint()

    # Load or create profile for this device
    profile = get_or_create_profile(device_id)

    # After calibration, update and save
    profile.aec_delay_samples = calibration_result.delay_samples
    save_profile(profile)
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.constants import SAMPLE_RATE_16K, SAMPLE_RATE_48K, TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

logger = get_logger(__name__)


def _default_profile_base_path() -> Path:
    """Writable calibration-profile root under the user data dir (PATH-1).

    Was the cwd-relative ``violawake_data/calibration`` — wrong-by-class on
    an installed build (install-dir write or PermissionError, requal-M3).
    """
    from violawake.config import wake_runtime_data_dir

    return wake_runtime_data_dir() / "calibration"


# --------------------------------------------------------------------------- #
# Data Structures                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class DeviceProfile:
    """
    Calibration profile for a specific audio device setup.

    Contains all device-specific calibration data that enables
    the wake word system to work optimally on this hardware.
    """

    # Device identification
    device_id: str
    device_name: str

    # Input device info
    input_device_index: int = -1
    input_device_name: str = ""
    input_sample_rate: int = SAMPLE_RATE_16K
    input_channels: int = 1

    # Output device info (for AEC reference)
    output_device_index: int = -1
    output_device_name: str = ""
    output_sample_rate: int = SAMPLE_RATE_48K
    output_channels: int = 2

    # AEC calibration
    aec_delay_samples: int | None = None
    aec_delay_ms: float | None = None
    aec_calibration_confidence: float = 0.0

    # Learned threshold offset (from threshold_learner.py)
    learned_offset: float = 0.0
    learned_offset_last_update: float | None = None

    # Metrics history (for trend analysis)
    acceptance_rate_history: list[float] = field(default_factory=list)

    # Timestamps
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    aec_calibrated_at: float | None = None

    # Profile version (for future migrations)
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceProfile:
        """Create from dictionary, handling missing/extra fields."""
        # Get known fields
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}

        # Filter to known fields only (handles extra fields in saved data)
        filtered_data = {k: v for k, v in data.items() if k in known_fields}

        return cls(**filtered_data)


@dataclass
class DeviceInfo:
    """Information about a single audio device."""

    index: int
    name: str
    sample_rate: int
    channels: int

    def fingerprint_string(self) -> str:
        """Generate string for fingerprinting."""
        # Normalize name (remove trailing spaces, convert to lowercase)
        normalized_name = self.name.strip().lower()
        return f"{normalized_name}|{self.sample_rate}|{self.channels}"


# --------------------------------------------------------------------------- #
# Device Fingerprinting                                                        #
# --------------------------------------------------------------------------- #


def get_device_fingerprint(
    input_device_index: int | None = None,
    output_device_index: int | None = None,
) -> str:
    """
    Generate a unique fingerprint for the current audio device setup.

    The fingerprint combines:
    - Input device name, sample rate, channels
    - Output device name, sample rate, channels

    This allows the system to detect when the user changes audio devices
    and trigger re-calibration if needed.

    Args:
        input_device_index: Input device index (None = system default)
        output_device_index: Output device index (None = system default)

    Returns:
        A short hash string identifying this device setup
    """
    input_info = _get_input_device_info(input_device_index)
    output_info = _get_output_device_info(output_device_index)

    # Combine fingerprint strings
    combined = f"in:{input_info.fingerprint_string()}|out:{output_info.fingerprint_string()}"

    # Create short hash for readable device_id
    hash_bytes = hashlib.sha256(combined.encode()).digest()[:8]
    device_id = hash_bytes.hex()

    logger.debug(
        "Device fingerprint: %s (input=%s, output=%s)",
        device_id,
        input_info.name,
        output_info.name,
    )

    return device_id


def warm_device_cache() -> str:
    """Prime PyAudio device enumeration on the caller's thread."""
    device_id = get_device_fingerprint()
    logger.debug("Warmed device profile cache for device %s", device_id)
    return device_id


def _get_input_device_info(device_index: int | None = None) -> DeviceInfo:
    """Get information about the input (microphone) device."""
    try:
        # PortAudio init/terminate is not thread-safe. Serialize this transient
        # query through the guard so it can't race another thread's
        # Pa_Initialize/Pa_Terminate — concurrent device fingerprinting freed a
        # host-API function pointer mid-walk and the app crashed with a native
        # execute-at-0x0 (WER BEX64 / 0xC0000005). See audio_core/portaudio_guard.py.
        from audio_core.portaudio_guard import portaudio_instance

        with portaudio_instance() as p:
            if device_index is None:
                info = p.get_default_input_device_info()
                device_index = int(info["index"])
            else:
                info = p.get_device_info_by_index(device_index)

            return DeviceInfo(
                index=device_index,
                name=str(info.get("name", f"Input-{device_index}")),
                sample_rate=int(info.get("defaultSampleRate", SAMPLE_RATE_16K)),
                channels=int(info.get("maxInputChannels", 1)),
            )
    except Exception as e:
        logger.debug("Could not get input device info: %s", e, exc_info=True)
        return DeviceInfo(
            index=device_index or -1,
            name="unknown-input",
            sample_rate=SAMPLE_RATE_16K,
            channels=1,
        )


def _get_output_device_info(device_index: int | None = None) -> DeviceInfo:
    """Get information about the output (speaker) device."""
    try:
        # Serialized through the PortAudio guard for the same thread-safety
        # reason as _get_input_device_info above.
        from audio_core.portaudio_guard import portaudio_instance

        with portaudio_instance() as p:
            if device_index is None:
                info = p.get_default_output_device_info()
                device_index = int(info["index"])
            else:
                info = p.get_device_info_by_index(device_index)

            return DeviceInfo(
                index=device_index,
                name=str(info.get("name", f"Output-{device_index}")),
                sample_rate=int(info.get("defaultSampleRate", SAMPLE_RATE_48K)),
                channels=int(info.get("maxOutputChannels", 2)),
            )
    except Exception as e:
        logger.debug("Could not get output device info: %s", e, exc_info=True)
        return DeviceInfo(
            index=device_index or -1,
            name="unknown-output",
            sample_rate=SAMPLE_RATE_48K,
            channels=2,
        )


def get_device_display_name(device_index: int | None = None, is_input: bool = True) -> str:
    """Get a human-readable device name."""
    if is_input:
        info = _get_input_device_info(device_index)
    else:
        info = _get_output_device_info(device_index)
    return info.name


# --------------------------------------------------------------------------- #
# Profile Storage                                                              #
# --------------------------------------------------------------------------- #


def _get_profile_path(device_id: str, base_path: Path | None = None) -> Path:
    """Get the file path for a device profile."""
    if base_path is None:
        base_path = _default_profile_base_path()
    return base_path / device_id / "profile.json"


def load_profile(
    device_id: str,
    base_path: Path | None = None,
) -> DeviceProfile | None:
    """
    Load a device profile from disk.

    Args:
        device_id: The device fingerprint
        base_path: Base path for profile storage (None = default)

    Returns:
        DeviceProfile if found, None otherwise
    """
    profile_path = _get_profile_path(device_id, base_path)

    if not profile_path.exists():
        logger.debug("No profile found for device %s", device_id)
        return None

    try:
        with profile_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        profile = DeviceProfile.from_dict(data)
        logger.info("Loaded profile for device %s: %s", device_id, profile.device_name)
        return profile
    except Exception as e:
        logger.debug("Failed to load profile for device %s: %s", device_id, e, exc_info=True)
        return None


def save_profile(
    profile: DeviceProfile,
    base_path: Path | None = None,
) -> bool:
    """
    Save a device profile to disk.

    Args:
        profile: The profile to save
        base_path: Base path for profile storage (None = default)

    Returns:
        True if saved successfully
    """
    profile_path = _get_profile_path(profile.device_id, base_path)

    try:
        # Update timestamp
        profile.updated_at = time.time()

        # Create directory if needed
        profile_path.parent.mkdir(parents=True, exist_ok=True)

        # Write atomically (write to temp, then rename)
        temp_path = profile_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, indent=2)
        temp_path.replace(profile_path)

        logger.info("Saved profile for device %s", profile.device_id)
        return True

    except Exception as e:
        logger.error("Failed to save profile for device %s: %s", profile.device_id, e)
        return False


def delete_profile(
    device_id: str,
    base_path: Path | None = None,
) -> bool:
    """
    Delete a device profile.

    Args:
        device_id: The device fingerprint
        base_path: Base path for profile storage (None = default)

    Returns:
        True if deleted (or didn't exist)
    """
    profile_path = _get_profile_path(device_id, base_path)

    try:
        if profile_path.exists():
            profile_path.unlink()
            logger.info("Deleted profile for device %s", device_id)

        # Also remove directory if empty
        profile_dir = profile_path.parent
        if profile_dir.exists() and not any(profile_dir.iterdir()):
            profile_dir.rmdir()

        return True
    except Exception as e:
        logger.error("Failed to delete profile for device %s: %s", device_id, e)
        return False


def list_profiles(base_path: Path | None = None) -> list[str]:
    """
    List all stored device profile IDs.

    Args:
        base_path: Base path for profile storage (None = default)

    Returns:
        List of device_id strings
    """
    if base_path is None:
        base_path = _default_profile_base_path()

    if not base_path.exists():
        return []

    profiles = []
    for path in base_path.iterdir():
        if path.is_dir() and (path / "profile.json").exists():
            profiles.append(path.name)

    return profiles


# --------------------------------------------------------------------------- #
# Profile Management                                                           #
# --------------------------------------------------------------------------- #


def get_or_create_profile(
    device_id: str | None = None,
    input_device_index: int | None = None,
    output_device_index: int | None = None,
    base_path: Path | None = None,
) -> DeviceProfile:
    """
    Get existing profile or create new one for the current device setup.

    This is the main entry point for profile management.

    Args:
        device_id: Device fingerprint (None = auto-detect)
        input_device_index: Input device index (None = system default)
        output_device_index: Output device index (None = system default)
        base_path: Base path for profile storage (None = default)

    Returns:
        DeviceProfile (existing or newly created)
    """
    # Get device fingerprint if not provided
    if device_id is None:
        device_id = get_device_fingerprint(input_device_index, output_device_index)

    # Try to load existing profile
    profile = load_profile(device_id, base_path)
    if profile is not None:
        return profile

    # Create new profile
    input_info = _get_input_device_info(input_device_index)
    output_info = _get_output_device_info(output_device_index)

    profile = DeviceProfile(
        device_id=device_id,
        device_name=f"{input_info.name} / {output_info.name}",
        input_device_index=input_info.index,
        input_device_name=input_info.name,
        input_sample_rate=input_info.sample_rate,
        input_channels=input_info.channels,
        output_device_index=output_info.index,
        output_device_name=output_info.name,
        output_sample_rate=output_info.sample_rate,
        output_channels=output_info.channels,
    )

    logger.info("Created new profile for device: %s", profile.device_name)

    # Save immediately so it exists for next startup
    save_profile(profile, base_path)

    return profile


def needs_calibration(profile: DeviceProfile, max_age_hours: float = 168.0) -> bool:
    """
    Check if a profile needs (re)calibration.

    Args:
        profile: The device profile to check
        max_age_hours: Maximum age before recalibration (default: 1 week)

    Returns:
        True if calibration is needed
    """
    # No AEC calibration at all
    if profile.aec_delay_samples is None:
        logger.debug("Device %s needs calibration: no AEC delay", profile.device_id)
        return True

    # Calibration too old
    if profile.aec_calibrated_at is not None:
        age_hours = (time.time() - profile.aec_calibrated_at) / 3600
        if age_hours > max_age_hours:
            logger.debug(
                "Device %s needs calibration: age %.1fh > %dh",
                profile.device_id,
                age_hours,
                max_age_hours,
            )
            return True

    # Low confidence calibration
    if profile.aec_calibration_confidence < 0.5:
        logger.debug(
            "Device %s needs calibration: low confidence %s",
            profile.device_id,
            format(profile.aec_calibration_confidence, ".2f"),
        )
        return True

    return False


def update_profile_from_calibration(
    profile: DeviceProfile,
    delay_samples: int,
    delay_ms: float,
    confidence: float,
) -> None:
    """
    Update profile with calibration results.

    Args:
        profile: Profile to update
        delay_samples: Calibrated delay in samples
        delay_ms: Calibrated delay in milliseconds
        confidence: Calibration confidence [0.0, 1.0]
    """
    profile.aec_delay_samples = delay_samples
    profile.aec_delay_ms = delay_ms
    profile.aec_calibration_confidence = confidence
    profile.aec_calibrated_at = time.time()
    profile.updated_at = time.time()

    logger.info(
        "Updated profile %s with AEC delay: %s samples (%sms), confidence=%s",
        profile.device_id,
        delay_samples,
        format(delay_ms, ".1f"),
        format(confidence, ".2f"),
    )


def update_profile_learned_offset(
    profile: DeviceProfile,
    offset: float,
) -> None:
    """
    Update profile with learned threshold offset.

    Args:
        profile: Profile to update
        offset: Learned threshold offset
    """
    profile.learned_offset = offset
    profile.learned_offset_last_update = time.time()
    profile.updated_at = time.time()

    logger.info("Updated profile %s learned offset: %+.3f", profile.device_id, offset)


def add_acceptance_rate_sample(
    profile: DeviceProfile,
    acceptance_rate: float,
    max_history: int = 1440,  # 24 hours at 1/min
) -> None:
    """
    Add an acceptance rate sample to the profile history.

    Args:
        profile: Profile to update
        acceptance_rate: Current acceptance rate [0.0, 1.0]
        max_history: Maximum history length
    """
    profile.acceptance_rate_history.append(acceptance_rate)

    # Trim history if needed
    if len(profile.acceptance_rate_history) > max_history:
        profile.acceptance_rate_history = profile.acceptance_rate_history[-max_history:]


# --------------------------------------------------------------------------- #
# Device Change Detection                                                      #
# --------------------------------------------------------------------------- #


class DeviceChangeDetector:
    """
    Monitors for audio device changes and triggers callbacks.

    Polls :func:`get_device_fingerprint`, which enumerates through
    ``audio_core.portaudio_guard.portaudio_instance`` — a fresh
    ``Pa_Initialize``/``Pa_Terminate`` per query. That is what makes polling
    work at all: PortAudio only builds its device table during
    ``Pa_Initialize``, so an enumeration path that keeps PortAudio initialized
    (every ``sounddevice`` call) would return the same frozen list forever and
    this loop would never fire.

    Callbacks run on the poll thread, one after another, and are isolated from
    each other. A callback MUST NOT close a stream another thread is reading —
    see the handoff contract in :mod:`audio_core.device_change`, which is the
    intended consumer.

    Usage:
        detector = DeviceChangeDetector()
        detector.on_device_change(my_callback)
        detector.start()
    """

    def __init__(self, poll_interval_seconds: float = 5.0):
        """
        Initialize detector.

        Args:
            poll_interval_seconds: How often to check for device changes
        """
        self._poll_interval = poll_interval_seconds
        self._callbacks: list[Callable[..., Any]] = []
        self._last_fingerprint: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Guards _callbacks and _poll_interval against concurrent
        # register/unregister from other threads while the poll loop iterates.
        self._lock = threading.RLock()

    def set_poll_interval(self, seconds: float) -> None:
        """Set the poll cadence. Takes effect on the next loop iteration."""
        with self._lock:
            self._poll_interval = max(0.5, float(seconds))

    def on_device_change(self, callback: Callable[..., Any]) -> None:
        """
        Register callback for device changes.

        Callback signature: callback(old_device_id: str | None, new_device_id: str)
        """
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[..., Any]) -> None:
        """Remove a registered callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    @property
    def is_running(self) -> bool:
        """True while the poll thread is active."""
        with self._lock:
            return self._running

    @property
    def last_fingerprint(self) -> str | None:
        """Most recently observed device fingerprint."""
        with self._lock:
            return self._last_fingerprint

    def start(self) -> None:
        """Start monitoring for device changes.

        The initial fingerprint is taken on the poll thread, not here: it costs a
        full PortAudio init/enumerate/terminate cycle, and doing that on the
        caller's thread put it on the boot path, which is the sequence the
        PortAudio-guard crash came from.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="audio-device-watch")
            self._thread.start()
        logger.info("Device change detector started")

    def stop(self) -> None:
        """Stop monitoring for device changes."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            thread = self._thread
            self._thread = None
        self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=TIMEOUT_SHUTDOWN)
        logger.info("Device change detector stopped")

    def _snapshot_callbacks(self) -> tuple[Callable[..., Any], ...]:
        with self._lock:
            return tuple(self._callbacks)

    def _current_interval(self) -> float:
        with self._lock:
            return self._poll_interval

    def _poll_loop(self) -> None:
        """Background polling loop."""
        # Seed the baseline here rather than in start(), so the first
        # enumeration never blocks the caller (see start()).
        try:
            with self._lock:
                self._last_fingerprint = get_device_fingerprint()
        except Exception:
            logger.exception("Could not read the initial device fingerprint; will retry on the next poll")

        while not self._stop_event.wait(timeout=self._current_interval()):
            try:
                current_fingerprint = get_device_fingerprint()
                with self._lock:
                    previous = self._last_fingerprint
                    changed = current_fingerprint != previous
                    if changed:
                        self._last_fingerprint = current_fingerprint

                if not changed:
                    continue

                if previous is None:
                    # First successful read after a failed seed — establish the
                    # baseline without reporting a spurious "change".
                    logger.debug("Device fingerprint baseline established: %s", current_fingerprint)
                    continue

                logger.info("Device change detected: %s -> %s", previous, current_fingerprint)

                for callback in self._snapshot_callbacks():
                    try:
                        callback(previous, current_fingerprint)
                    except Exception:
                        logger.exception("Error in device change callback")

            except Exception:
                logger.exception("Error in device change detection")


# --------------------------------------------------------------------------- #
# Global Instance                                                              #
# --------------------------------------------------------------------------- #

_global_detector: DeviceChangeDetector | None = None
_global_lock = threading.Lock()


def get_device_change_detector() -> DeviceChangeDetector:
    """Get the global device change detector instance."""
    global _global_detector
    with _global_lock:
        if _global_detector is None:
            _global_detector = DeviceChangeDetector()
        return _global_detector


# --------------------------------------------------------------------------- #
# Exports                                                                      #
# --------------------------------------------------------------------------- #

__all__ = [
    # Device change detection
    "DeviceChangeDetector",
    "DeviceInfo",
    # Data structures
    "DeviceProfile",
    "add_acceptance_rate_sample",
    "delete_profile",
    "get_device_change_detector",
    "get_device_display_name",
    # Fingerprinting
    "get_device_fingerprint",
    # Profile management
    "get_or_create_profile",
    "list_profiles",
    # Profile storage
    "load_profile",
    "needs_calibration",
    "save_profile",
    "update_profile_from_calibration",
    "update_profile_learned_offset",
    "warm_device_cache",
]
