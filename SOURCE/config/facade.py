"""
Unified Settings Facade
Single interface for accessing all configuration values.

Provides clear precedence: user_settings > env > defaults > constants
"""

from __future__ import annotations

import os
from typing import Protocol, TypeAlias

# Import all configuration sources
from config import constants, defaults
from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger

_logger = get_logger("viola.config.facade")

ScalarSetting: TypeAlias = str | int | float | bool | None


class SettingsManagerProtocol(Protocol):
    def get(self, key: str) -> object: ...

    def set(self, key: str, value: object) -> None: ...

    def model_dump_sanitized(self) -> object: ...


def get_settings_manager():
    """Proxy for ui.settings_manager.get_settings_manager for patchability."""
    from ui.settings_manager import get_settings_manager as _get_settings_manager

    return _get_settings_manager()


class SettingsFacade:
    """
    Unified interface for all settings access.

    Precedence (highest to lowest):
    1. User settings (runtime, persisted)
    2. Environment variables (VIOLA_*)
    3. Defaults (config/defaults.py)
    4. Constants (config/constants.py - immutable)

    Usage:
        from config.facade import settings_facade
        volume = settings_facade.get("default_volume", 80)
    """

    def __init__(self):
        """Initialize with lazy loading of user settings."""
        self._user_settings = None
        self._settings_manager = None

    @property
    def user_settings(self) -> SettingsManagerProtocol | None:
        """Lazy load settings manager only when needed."""
        if self._settings_manager is None:
            try:
                self._settings_manager = get_settings_manager()
            except Exception as e:
                # Settings manager may not be available in all contexts (e.g., early bootstrap)
                # Log at debug level to help diagnose issues without cluttering production logs
                _logger.debug("Settings manager not available in this context: %s", e)
        return self._settings_manager

    @user_settings.setter
    def user_settings(self, value: SettingsManagerProtocol | None) -> None:
        self._settings_manager = value

    @user_settings.deleter
    def user_settings(self) -> None:
        self._settings_manager = None

    def get(self, key: str, default: object = None) -> object:
        """
        Get setting value with clear precedence.

        Args:
            key: Setting key (e.g., "default_volume", "autoplay_min_queue")
            default: Fallback value if not found anywhere

        Returns:
            Setting value from highest precedence source
        """
        # 1. User settings (highest priority)
        if self.user_settings:
            value = self.user_settings.get(key)
            if value is not None:
                return value

        # 2. Environment variables
        env_key = f"VIOLA_{key.upper()}"
        if env_key in os.environ:
            env_value = os.environ[env_key]
            # Try to parse as appropriate type
            return self._parse_env_value(env_value)

        # 3. Defaults module (user-configurable defaults)
        defaults_key = f"{key.upper()}_DEFAULT" if not key.endswith("_DEFAULT") else key.upper()
        if hasattr(defaults, defaults_key):
            return getattr(defaults, defaults_key)

        # Also try without _DEFAULT suffix
        if hasattr(defaults, key.upper()):
            return getattr(defaults, key.upper())

        # 4. Constants module (system constants)
        if hasattr(constants, key.upper()):
            return getattr(constants, key.upper())

        # 5. Provided default
        return default

    def _parse_env_value(self, value: str) -> ScalarSetting:
        """Parse environment variable to appropriate type."""
        # Boolean
        if value.lower() in ("true", "yes", "1", "on"):
            return True
        if value.lower() in ("false", "no", "0", "off"):
            return False

        # Integer
        try:
            return int(value)
        except ValueError as e:
            _logger.debug("Failed to parse '%s' as int (non-critical): %s", value, e)
            pass

        # Float
        try:
            return float(value)
        except ValueError as e:
            _logger.debug("Failed to parse '%s' as float (non-critical): %s", value, e)
            pass

        # String (default)
        return value

    def get_constant(self, key: str, default: object = None) -> object:
        """Get immutable constant value (from constants.py only)."""
        return getattr(constants, key.upper(), default)

    def get_default(self, key: str, default: object = None) -> object:
        """Get default value (from defaults.py only)."""
        defaults_key = f"{key.upper()}_DEFAULT" if not key.endswith("_DEFAULT") else key.upper()
        if hasattr(defaults, defaults_key):
            return getattr(defaults, defaults_key)
        if hasattr(defaults, key.upper()):
            return getattr(defaults, key.upper())
        return default

    def set(self, key: str, value: object) -> None:
        """Set a user setting value (if settings manager is available)."""
        if self.user_settings is not None:
            self.user_settings.set(key, value)

    def model_dump_sanitized(self) -> JsonDict:
        """Return a sanitized dict of all settings (secrets redacted)."""
        if self.user_settings is not None:
            dumped_value = to_json_value(self.user_settings.model_dump_sanitized())
            return dumped_value if isinstance(dumped_value, dict) else {}
        return {}

    def to_public_dict(self) -> JsonDict:
        """Return a public-safe dict of settings (alias for model_dump_sanitized)."""
        return self.model_dump_sanitized()


# Global singleton instance
_settings_facade: SettingsFacade | None = None


def get_settings_facade() -> SettingsFacade:
    """Get global settings facade instance."""
    global _settings_facade
    if _settings_facade is None:
        _settings_facade = SettingsFacade()
    return _settings_facade


# Convenience alias for direct import
settings_facade = get_settings_facade()
