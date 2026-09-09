"""
Adaptive gain recalibration scheduler for wake word listeners.

This module periodically evaluates runtime wake word statistics and nudges the
listener threshold toward day/night profiles while guarding against abrupt
changes. Calibration history is persisted for post-mortem analysis and quick
rollback on anomalies.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger

logger = get_logger(__name__)

# Optional zoneinfo import (may not be available in limited Python builds)
_zoneinfo_available = False
_ZoneInfoCtor = Callable[[str], tzinfo]
_zone_info: _ZoneInfoCtor | None = None
try:
    from zoneinfo import ZoneInfo as _ZoneInfo

    _zone_info = _ZoneInfo
    _zoneinfo_available = True
except Exception as e:  # pragma: no cover - fallback for limited Python builds
    logger.debug("zoneinfo not available, using fallback: %s", e, exc_info=True)


def _resolve_timezone(tz_name: str) -> tzinfo:
    """Resolve timezone string to tzinfo, falling back to UTC."""
    if not _zoneinfo_available or _zone_info is None:
        return UTC
    try:
        return _zone_info(tz_name)
    except Exception:
        logger.debug("Unknown timezone '%s'; using UTC for gain scheduler", tz_name)
        return UTC


@dataclass(frozen=True)
class GainCalibrationProfile:
    """
    Day/night style calibration profile.

    Attributes:
        name: Human readable profile name.
        start_hour: Hour (0-23) when this profile becomes active.
        target_far_per_hour: Desired false accept rate per hour.
        max_false_accept_rate: Upper bound FAR before forcing strictness.
        max_missed_commands: Percentage of missed commands allowed.
        increase_step: Threshold increment when too many false accepts.
        decrease_step: Threshold decrement when too many misses.
        maintenance_step: Gentle adjustment step when nudging toward target.
        min_threshold: Lower clamp for threshold.
        max_threshold: Upper clamp for threshold.
        max_threshold_delta: Maximum delta per adjustment.
        min_detections: Minimum detection samples before acting.
    """

    name: str
    start_hour: int
    target_far_per_hour: float = 0.2
    max_false_accept_rate: float = 0.6
    max_missed_commands: float = 8.0
    increase_step: float = 0.05
    decrease_step: float = 0.04
    maintenance_step: float = 0.02
    min_threshold: float = 0.1
    max_threshold: float = 0.9
    max_threshold_delta: float = 0.15
    min_detections: int = 15

    def clamp(self, value: float) -> float:
        return max(self.min_threshold, min(self.max_threshold, value))


def _load_profiles(
    raw_profiles: Sequence[dict[str, Any]],
) -> list[GainCalibrationProfile]:
    """Create profile objects from configuration dictionaries."""
    profiles: list[GainCalibrationProfile] = []
    for entry in raw_profiles:
        try:
            profiles.append(
                GainCalibrationProfile(
                    name=str(entry.get("name", "profile")),
                    start_hour=int(entry.get("start_hour", 0)),
                    target_far_per_hour=float(entry.get("target_far_per_hour", 0.2)),
                    max_false_accept_rate=float(entry.get("max_false_accept_rate", 0.6)),
                    max_missed_commands=float(entry.get("max_missed_commands", 8.0)),
                    increase_step=float(entry.get("increase_step", 0.05)),
                    decrease_step=float(entry.get("decrease_step", 0.04)),
                    maintenance_step=float(entry.get("maintenance_step", 0.02)),
                    min_threshold=float(entry.get("min_threshold", 0.1)),
                    max_threshold=float(entry.get("max_threshold", 0.9)),
                    max_threshold_delta=float(entry.get("max_threshold_delta", 0.15)),
                    min_detections=int(entry.get("min_detections", 15)),
                )
            )
        except Exception as exc:
            logger.warning("Invalid gain calibration profile {entry}: %s", exc)

    if not profiles:
        profiles = [
            GainCalibrationProfile(
                name="day",
                start_hour=6,
                target_far_per_hour=0.25,
                max_false_accept_rate=0.7,
                max_missed_commands=6.0,
                increase_step=0.05,
                decrease_step=0.03,
                maintenance_step=0.015,
            ),
            GainCalibrationProfile(
                name="night",
                start_hour=21,
                target_far_per_hour=0.1,
                max_false_accept_rate=0.4,
                max_missed_commands=10.0,
                increase_step=0.04,
                decrease_step=0.05,
                maintenance_step=0.02,
                min_threshold=0.15,
                max_threshold=0.95,
            ),
        ]

    # Sort for deterministic profile selection throughout the day
    profiles.sort(key=lambda profile: profile.start_hour % 24)
    return profiles


class AdaptiveGainScheduler:
    """Background scheduler that keeps wake thresholds aligned with noise profiles."""

    def __init__(
        self,
        listener: Any,
        config: Any,
    ) -> None:
        self._listener = listener
        self._config = config
        self._enabled = bool(getattr(config, "wake_gain_scheduler_enabled", False))
        interval_minutes = int(getattr(config, "wake_gain_scheduler_interval_minutes", 60))
        self._interval_seconds = max(300, interval_minutes * 60)
        raw_profiles = getattr(config, "wake_gain_scheduler_profiles", []) or []
        self._profiles = _load_profiles(raw_profiles)

        tz_name = getattr(config, "calendar_timezone", "UTC")
        self._tz = _resolve_timezone(tz_name)

        data_dir = Path(getattr(config, "data_dir", "."))
        self._history_path = data_dir / "calibration" / "wake_gain_history.json"
        self._history_retention = max(10, int(getattr(config, "wake_gain_history_retention", 120)))
        self._history: list[dict[str, Any]] = []
        self._load_history()

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the recalibration scheduler."""
        if not self._enabled:
            logger.debug("Adaptive gain scheduler disabled via configuration.")
            return
        if not self._supports_listener():
            logger.debug("Adaptive gain scheduler skipped; listener lacks support.")
            return

        with self._lock:
            if self._thread and self._thread.is_alive():
                return

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="wake-gain-scheduler",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "Adaptive gain scheduler started (interval=%ss, profiles=%s)",
                self._interval_seconds,
                ", ".join(profile.name for profile in self._profiles),
            )

    def stop(self) -> None:
        """Stop the scheduler and wait for completion."""
        with self._lock:
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=TIMEOUT_LONG)
            self._thread = None

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        """
        Execute a single calibration cycle (synchronously).

        Args:
            now: Optional datetime override (defaults to current time).

        Returns:
            The history entry for this tick, or None if no update occurred.
        """
        if not self._enabled or not self._supports_listener():
            return None
        return self._run_once(now or self._now())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _supports_listener(self) -> bool:
        return hasattr(self._listener, "supports_adaptive_gain") and self._listener.supports_adaptive_gain()

    def _run_loop(self) -> None:
        """Background loop that runs until stop is signaled."""
        # Kick off immediately for a baseline snapshot
        self._run_once(self._now())
        while not self._stop_event.wait(self._interval_seconds):
            self._run_once(self._now())

    def _run_once(self, current_time: datetime) -> dict[str, Any] | None:
        """Execute a single recalibration pass."""
        profile = self._select_profile(current_time)
        stats = self._get_stats()
        if stats is None:
            return None

        current_threshold = float(stats.get("threshold", 0.5))
        total_detections = int(stats.get("total_detections", 0))
        far = float(stats.get("false_accept_rate_per_hour", 0.0))
        missed = float(stats.get("missed_commands", 0.0))

        if total_detections < profile.min_detections:
            logger.debug(
                "Skipping adaptive gain (%s) - only %d detections (min=%d)",
                profile.name,
                total_detections,
                profile.min_detections,
            )
            return None

        target_threshold = self._calculate_target_threshold(profile, current_threshold, far, missed)

        if math.isclose(target_threshold, current_threshold, rel_tol=1e-6, abs_tol=1e-6):
            action = "no_change"
            applied_threshold = current_threshold
        else:
            applied_threshold, action = self._apply_with_guardrails(
                profile,
                current_threshold,
                target_threshold,
                far,
                missed,
            )

        entry = {
            "timestamp": current_time.astimezone(UTC).isoformat(),
            "profile": profile.name,
            "current_threshold": round(current_threshold, 4),
            "applied_threshold": round(applied_threshold, 4),
            "false_accept_rate_per_hour": round(far, 4),
            "missed_commands": round(missed, 3),
            "total_detections": total_detections,
            "action": action,
        }

        self._append_history(entry)
        return entry

    def _calculate_target_threshold(
        self,
        profile: GainCalibrationProfile,
        current_threshold: float,
        false_accept_rate: float,
        missed_commands: float,
    ) -> float:
        """
        Decide on a proposed threshold given metrics and profile goals.

        Heuristics:
            - If FAR is within tolerance and misses acceptable -> gentle nudge toward target.
            - If FAR too high -> increase threshold.
            - If misses too high -> decrease threshold.
        """
        threshold = current_threshold

        far_tolerance = profile.target_far_per_hour * 0.25
        far_delta = false_accept_rate - profile.target_far_per_hour

        if false_accept_rate > profile.max_false_accept_rate:
            threshold += profile.increase_step
        elif missed_commands > profile.max_missed_commands:
            threshold -= profile.decrease_step
        elif abs(far_delta) > far_tolerance:
            adjustment = profile.maintenance_step
            if far_delta > 0:
                threshold += adjustment
            else:
                threshold -= adjustment

        return profile.clamp(threshold)

    def _apply_with_guardrails(
        self,
        profile: GainCalibrationProfile,
        current_threshold: float,
        proposed_threshold: float,
        false_accept_rate: float,
        missed_commands: float,
    ) -> tuple[float, str]:
        """Clamp and apply the new threshold while recording anomalies."""
        delta = proposed_threshold - current_threshold
        max_delta = profile.max_threshold_delta
        if abs(delta) > max_delta:
            logger.debug(
                "Threshold delta %.3f exceeds max %.3f; clamping",
                delta,
                max_delta,
            )
            proposed_threshold = current_threshold + max_delta * (1 if delta > 0 else -1)

        proposed_threshold = profile.clamp(proposed_threshold)

        try:
            applied = float(
                self._listener.apply_threshold(
                    proposed_threshold,
                    source=f"adaptive_gain:{profile.name}",
                )
            )
            action = "applied"
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to apply adaptive threshold: %s", exc)
            applied = current_threshold
            action = "error"

        if applied != current_threshold and (
            applied <= profile.min_threshold + 1e-6 or applied >= profile.max_threshold - 1e-6
        ):
            logger.debug(
                "Applied threshold %.3f landed on profile clamp boundaries (min=%.3f, max=%.3f)",
                applied,
                profile.min_threshold,
                profile.max_threshold,
            )

        return applied, action

    def _select_profile(self, current_time: datetime) -> GainCalibrationProfile:
        """Pick active profile based on start hours."""
        hour = current_time.hour
        # Profiles sorted ascending by start hour. The active profile is the latest whose
        # start hour is <= current hour, wrapping to the last profile otherwise.
        active = self._profiles[-1]
        for profile in self._profiles:
            if hour >= profile.start_hour:
                active = profile
            else:
                break
        return active

    def _get_stats(self) -> dict[str, Any] | None:
        try:
            return self._listener.get_adaptive_gain_stats()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Unable to fetch listener stats: %s", exc)
            return None

    def _append_history(self, entry: dict[str, Any]) -> None:
        self._history.append(entry)
        if len(self._history) > self._history_retention:
            self._history = self._history[-self._history_retention :]
        self._persist_history()

    def _load_history(self) -> None:
        try:
            if self._history_path.exists():
                with open(self._history_path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                    if isinstance(loaded, list):
                        self._history = loaded[-self._history_retention :]
        except Exception as exc:  # pragma: no cover - disk may be readonly
            logger.debug("Failed to load gain history: %s", exc)

    def _persist_history(self) -> None:
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_path, "w", encoding="utf-8") as handle:
                json.dump(self._history, handle, indent=2)
        except Exception as exc:  # pragma: no cover - disk may be readonly
            logger.debug("Failed to persist gain history: %s", exc)

    def _now(self) -> datetime:
        return datetime.now(tz=self._tz)


__all__ = ["AdaptiveGainScheduler", "GainCalibrationProfile"]
