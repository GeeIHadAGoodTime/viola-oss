"""Time-of-day awareness helpers for voice, wake, and notifications."""

from __future__ import annotations

import os
from datetime import UTC, datetime, time, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.constants import (
    QUIET_HOURS_DEFAULT_ENABLED,
    QUIET_HOURS_DEFAULT_END,
    QUIET_HOURS_DEFAULT_START,
    QUIET_HOURS_DEFAULT_TIMEZONE,
    QUIET_HOURS_TIME_OVERRIDE_ENV,
    QUIET_HOURS_TTS_VOLUME_SCALE,
    QUIET_HOURS_WAKE_THRESHOLD_BOOST,
    QUIET_HOURS_WAKE_THRESHOLD_MAX,
    WAKE_SENSITIVITY_MIN,
)
from core.logging_config import get_logger

logger = get_logger(__name__)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _settings_value(key: str, default: object, user_id: str | None) -> object:
    if user_id:
        try:
            from services.cloud_settings import get_cloud_settings_service

            return get_cloud_settings_service().get_setting_sync(user_id, key, default)
        except Exception as exc:
            logger.debug("Cloud quiet-hours setting lookup failed for %s: %s", key, exc)

    try:
        from ui.settings_manager import get_settings_manager

        return get_settings_manager().get(key, default, user_id=user_id)
    except Exception as exc:
        logger.debug("Quiet-hours setting lookup failed for %s: %s", key, exc)

    try:
        from config.settings import settings

        return getattr(settings, key, default)
    except Exception as exc:
        logger.debug("Quiet-hours AppConfig fallback failed for %s: %s", key, exc)
        return default


def _coerce_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _parse_time(value: object, default: str) -> time:
    text = str(value or default).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError):
        logger.warning("Invalid quiet-hours time '%s'; using default %s", text, default)
        hour_text, minute_text = default.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))


def _local_timezone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or UTC


def _resolve_timezone(timezone_name: str | None) -> tzinfo:
    raw = (timezone_name or "").strip()
    if raw in {"", "auto", "user.timezone"}:
        return _local_timezone()
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid quiet-hours timezone '%s'; using local timezone", raw)
        return _local_timezone()


def _parse_now_override(value: str, tz: tzinfo) -> datetime | None:
    text = value.strip()
    if not text:
        return None

    if "T" not in text and len(text) <= 5 and ":" in text:
        try:
            hour_text, minute_text = text.split(":", 1)
            parsed_time = time(hour=int(hour_text), minute=int(minute_text))
        except ValueError:
            parsed_time = None
        if parsed_time is not None:
            today = datetime.now(tz).date()
            return datetime.combine(today, parsed_time, tzinfo=tz)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            "Ignoring invalid %s value '%s'",
            QUIET_HOURS_TIME_OVERRIDE_ENV,
            text,
        )
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _resolve_now(now: datetime | None, timezone_name: str | None) -> datetime:
    tz = _resolve_timezone(timezone_name)
    if now is None:
        override = os.environ.get(QUIET_HOURS_TIME_OVERRIDE_ENV)
        if override:
            parsed = _parse_now_override(override, tz)
            if parsed is not None:
                return parsed
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _quiet_window_values(
    *,
    start: str | None,
    end: str | None,
    enabled: bool | None,
    timezone_name: str | None,
    user_id: str | None,
) -> tuple[bool, time, time, str | None]:
    if enabled is None:
        enabled = _coerce_enabled(_settings_value("quiet_hours_enabled", QUIET_HOURS_DEFAULT_ENABLED, user_id))
    if start is None:
        start = str(_settings_value("quiet_hours_start", QUIET_HOURS_DEFAULT_START, user_id))
    if end is None:
        end = str(_settings_value("quiet_hours_end", QUIET_HOURS_DEFAULT_END, user_id))
    if timezone_name is None:
        timezone_name = str(_settings_value("quiet_hours_timezone", QUIET_HOURS_DEFAULT_TIMEZONE, user_id))

    return (
        bool(enabled),
        _parse_time(start, QUIET_HOURS_DEFAULT_START),
        _parse_time(end, QUIET_HOURS_DEFAULT_END),
        timezone_name,
    )


def is_quiet_hours(
    *,
    now: datetime | None = None,
    start: str | None = None,
    end: str | None = None,
    enabled: bool | None = None,
    timezone_name: str | None = None,
    user_id: str | None = None,
) -> bool:
    """Return whether the local time is inside the configured quiet-hours window."""

    is_enabled, start_time, end_time, tz_name = _quiet_window_values(
        start=start,
        end=end,
        enabled=enabled,
        timezone_name=timezone_name,
        user_id=user_id,
    )
    if not is_enabled:
        return False

    if start_time == end_time:
        return False

    local_now = _resolve_now(now, tz_name).time()
    if start_time < end_time:
        return start_time <= local_now < end_time
    return local_now >= start_time or local_now < end_time


def _normalize_volume(value: object) -> float:
    raw = _coerce_float(value, 1.0)
    if raw > 1.0:
        raw /= 100.0
    return _clamp(raw, 0.0, 1.0)


def _coerce_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def tts_volume_for_now(
    base_volume: float | int | None = None,
    *,
    now: datetime | None = None,
    user_id: str | None = None,
    quiet_scale: float = QUIET_HOURS_TTS_VOLUME_SCALE,
    **quiet_window: Any,
) -> float:
    """Return normalized TTS volume for the current time, scaled in quiet hours."""

    raw_volume: object = base_volume
    if raw_volume is None:
        raw_volume = _settings_value("tts_volume", 1.0, user_id)
    volume = _normalize_volume(raw_volume)
    if is_quiet_hours(now=now, user_id=user_id, **quiet_window):
        return _clamp(volume * quiet_scale, 0.0, 1.0)
    return volume


def wake_threshold_for_now(
    base_threshold: float | None = None,
    *,
    now: datetime | None = None,
    user_id: str | None = None,
    quiet_boost: float = QUIET_HOURS_WAKE_THRESHOLD_BOOST,
    max_threshold: float = QUIET_HOURS_WAKE_THRESHOLD_MAX,
    **quiet_window: Any,
) -> float:
    """Return wake-word score threshold, boosted during quiet hours."""

    raw_threshold: object = base_threshold
    if raw_threshold is None:
        raw_threshold = _settings_value("wake_sensitivity", 0.80, user_id)
    threshold = _clamp(_coerce_float(raw_threshold, 0.80), WAKE_SENSITIVITY_MIN, 1.0)
    if is_quiet_hours(now=now, user_id=user_id, **quiet_window):
        return _clamp(threshold + quiet_boost, WAKE_SENSITIVITY_MIN, max_threshold)
    return threshold


def prompt_time_of_day_mode() -> str:
    """Return one short prompt sentence describing the current voice mode."""

    if is_quiet_hours():
        return (
            "Current time-of-day mode: quiet hours; keep spoken replies softer, "
            "shorter, and avoid reading long detail aloud unless the user asks."
        )
    return "Current time-of-day mode: normal; use the standard spoken voice style."


__all__ = [
    "is_quiet_hours",
    "prompt_time_of_day_mode",
    "tts_volume_for_now",
    "wake_threshold_for_now",
]
