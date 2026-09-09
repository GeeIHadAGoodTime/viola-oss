"""Per-user calendar integration state."""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)

GOOGLE_CALENDAR_ENABLED_SETTING = "google_calendar_enabled"
GOOGLE_CALENDAR_DEFAULT_ENABLED = False


def is_google_calendar_enabled(user_id: str) -> bool:
    """Return whether this user has explicitly enabled Google Calendar sync."""
    if not user_id:
        return False

    try:
        from ui.settings_manager import get_settings_manager

        value = get_settings_manager().get_user_setting(
            user_id,
            GOOGLE_CALENDAR_ENABLED_SETTING,
            GOOGLE_CALENDAR_DEFAULT_ENABLED,
        )
    except Exception as exc:
        logger.debug("Google Calendar enabled-state lookup failed for user %s: %s", user_id, exc)
        return False

    if value is None:
        return GOOGLE_CALENDAR_DEFAULT_ENABLED
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return value is True


def set_google_calendar_enabled(user_id: str, enabled: bool) -> bool:
    """Persist the user's Google Calendar sync preference."""
    if not user_id:
        raise ValueError("user_id is required")

    try:
        from ui.settings_manager import get_settings_manager

        return bool(
            get_settings_manager().set_user_setting(
                user_id,
                GOOGLE_CALENDAR_ENABLED_SETTING,
                bool(enabled),
            )
        )
    except Exception as exc:
        logger.debug("Google Calendar enabled-state write failed for user %s: %s", user_id, exc)
        return False
