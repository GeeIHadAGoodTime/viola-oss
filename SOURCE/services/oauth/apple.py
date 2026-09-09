"""Apple identity-provider configuration probe for GoTrue-facing auth status."""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger("viola.services.oauth.apple")


def is_apple_configured() -> bool:
    """Check whether Apple OAuth environment/config values are present."""
    try:
        from config.settings import get_settings

        settings = get_settings()
        return bool(
            getattr(settings, "apple_client_id", None)
            and getattr(settings, "apple_team_id", None)
            and getattr(settings, "apple_key_id", None)
            and getattr(settings, "apple_private_key_path", None)
        )
    except Exception as exc:
        logger.debug("Apple OAuth not configured: %s", exc)
        return False
