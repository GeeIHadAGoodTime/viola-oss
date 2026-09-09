"""Quiet-hours setting command parsing and persistence helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_TIME_RE = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|\d{1,2}:\d{2}"
_QUIET_HOURS_SET_RE = re.compile(
    r"^\s*"
    r"(?:(?:please|can\s+you|could\s+you|go\s+ahead\s+and|hey\s+viola)\s+)?"
    r"(?:set|change|update|turn\s+on|enable)\s+"
    r"(?:my\s+)?quiet[-\s]+hours\s+"
    r"(?:from\s+)?"
    r"(?P<start>%s)\s*"
    r"(?:to|until|-|through)\s*"
    r"(?P<end>%s)"
    r"(?:\s+please)?[\s.!?]*$" % (_TIME_RE, _TIME_RE),
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class QuietHoursUpdate:
    start: str
    end: str

    @property
    def settings(self) -> dict[str, object]:
        return {
            "quiet_hours_enabled": True,
            "quiet_hours_start": self.start,
            "quiet_hours_end": self.end,
        }

    @property
    def message(self) -> str:
        return "Quiet hours are set from %s to %s." % (_format_time(self.start), _format_time(self.end))

    def to_data(self) -> dict[str, object]:
        return {
            "intent": "set_quiet_hours",
            **self.settings,
        }


def _parse_time(raw: str) -> str | None:
    text = re.sub(r"\s+", "", raw.strip().lower()).replace(".", "")
    match = re.fullmatch(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<meridiem>am|pm)?", text)
    if match is None:
        return None

    hour = int(match.group("hour"))
    minute_text = match.group("minute")
    minute = int(minute_text) if minute_text is not None else 0
    meridiem = match.group("meridiem")

    if minute > 59:
        return None
    if meridiem:
        if hour < 1 or hour > 12:
            return None
        if meridiem == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif minute_text is None or hour > 23:
        return None

    return "%02d:%02d" % (hour, minute)


def _format_time(value: str) -> str:
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return "%d:%02d %s" % (display_hour, minute, suffix)


def parse_quiet_hours_update(text: str) -> QuietHoursUpdate | None:
    match = _QUIET_HOURS_SET_RE.match(text or "")
    if match is None:
        return None

    start = _parse_time(match.group("start"))
    end = _parse_time(match.group("end"))
    if start is None or end is None or start == end:
        return None
    return QuietHoursUpdate(start=start, end=end)


async def apply_cloud_quiet_hours_update(
    user_id: str,
    update: QuietHoursUpdate,
    *,
    settings_service: Any | None = None,
) -> None:
    if not user_id:
        raise ValueError("user_id is required for cloud quiet-hours settings")

    service = settings_service
    if service is None:
        from services.cloud_settings import get_cloud_settings_service

        service = get_cloud_settings_service()

    for key, value in update.settings.items():
        accepted = await service.set_setting(user_id, key, value)
        if not accepted:
            raise RuntimeError("quiet-hours setting rejected: %s" % key)


__all__ = [
    "QuietHoursUpdate",
    "apply_cloud_quiet_hours_update",
    "parse_quiet_hours_update",
]
