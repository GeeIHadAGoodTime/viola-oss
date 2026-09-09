"""Resolve local versus hosted calling without loading hosted transports."""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)


def phone_mode_is_cloud() -> bool:
    """Return True when phone calls run on the cloud (the SaaS default).

    Read from SettingsManager first (runtime user-preference truth, per the
    Settings Resolution rule), falling back to AppConfig. ``"cloud"`` is the
    default on both. Any non-"local" value is treated as cloud so a missing /
    malformed value fails toward the cloud path (where the SaaS calls live)
    rather than silently reading the empty local store.
    """
    mode = ""
    try:
        from ui.settings_manager import get_settings_manager

        mode = str(get_settings_manager().get("phone_mode", "") or "").strip().lower()
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        logger.debug("phone_mode SettingsManager lookup failed; trying AppConfig", exc_info=True)

    if not mode:
        try:
            from config.settings import settings

            mode = str(getattr(settings, "phone_mode", "") or "").strip().lower()
        except (ImportError, AttributeError, TypeError, ValueError):
            logger.debug("phone_mode AppConfig lookup failed", exc_info=True)

    return mode != "local"
