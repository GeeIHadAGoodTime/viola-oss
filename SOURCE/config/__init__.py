"""Re-export unified configuration helpers from config.settings."""

from __future__ import annotations

from .settings import (
    AppConfig,
    EnvFileWatcher,
    SettingsValidationError,
    ensure_required_secrets,
    get_missing_required_secrets,
    get_public_config_payload,
    get_settings,
    hot_reload_env,
    settings,
)

# Backward-compat alias: existing code may import ConfigurationError from config.
# SettingsValidationError is the canonical settings-layer exception (ValueError subclass).
ConfigurationError = SettingsValidationError

__all__ = [
    "AppConfig",
    "ConfigurationError",
    "EnvFileWatcher",
    "SettingsValidationError",
    "ensure_required_secrets",
    "get_missing_required_secrets",
    "get_public_config_payload",
    "get_settings",
    "hot_reload_env",
    "settings",
]
