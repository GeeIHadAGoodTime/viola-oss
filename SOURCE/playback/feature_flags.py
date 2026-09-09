"""
playback/feature_flags.py

Centralised feature flag model for playback orchestration.

The flags are intentionally lightweight and can be sourced from either the
global settings facade or environment variables.  We avoid importing the heavy
settings stack here to keep playback bootstrap fast and side-effect free.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _coerce_bool(value: Any, default: bool) -> bool:
    """
    Convert common string/number representations into booleans.

    Accepts 1/0, "true"/"false", "yes"/"no", "on"/"off".
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", "off"}:
            return False

    return default


@dataclass
class PlaybackFeatureFlags:
    """
    Feature flag bundle used by the playback orchestrator.

    Attributes:
        gapless_enabled: Enable gapless playback hand-offs when supported.
        artwork_sync_enabled: Maintain real-time artwork synchronisation between
            provider SDKs and the UI state.
        hot_buffer_enabled: Aggressively prefetch next tracks when providers
            expose a compliant buffer API.
        fallback_to_legacy_backend: When True, always fall back to the legacy
            VLC/simple backend even if provider-specific engines are available.
    """

    gapless_enabled: bool = True
    artwork_sync_enabled: bool = True
    hot_buffer_enabled: bool = True
    fallback_to_legacy_backend: bool = False

    @classmethod
    def from_settings(
        cls,
        settings_obj: Mapping[str, Any] | object | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> PlaybackFeatureFlags:
        """
        Build the feature flag bundle from the optional settings object and
        environment variables.

        Priority order (highest wins):
            1. Explicit keyword arguments (not exposed yet)
            2. Settings mapping / object attributes
            3. Environment variables (VIOLA_PLAYBACK_*)
            4. Defaults encoded in the dataclass definition
        """

        env_mapping = env or os.environ

        # Also load centralized settings for playback_* fields
        _app_settings = None
        if env is None:
            try:
                from config.settings import settings as _cfg

                _app_settings = _cfg
            except Exception:
                pass

        # Get default values from class definition
        _defaults = {
            "gapless_enabled": True,
            "artwork_sync_enabled": True,
            "hot_buffer_enabled": True,
            "fallback_to_legacy_backend": False,
        }

        def _lookup(name: str, default: bool) -> bool:
            env_key = f"VIOLA_PLAYBACK_{name.upper()}"
            env_value = env_mapping.get(env_key)
            if env_value is not None:
                return _coerce_bool(env_value, default)

            # Check centralized settings for playback_* fields
            if _app_settings is not None:
                settings_key = f"playback_{name}"
                settings_value = getattr(_app_settings, settings_key, None)
                if settings_value is not None:
                    return _coerce_bool(settings_value, default)

            if settings_obj is not None:
                # Support both attribute-style and dict-style access without
                # pulling in pydantic or the full settings subsystem.
                if isinstance(settings_obj, Mapping):
                    candidate = settings_obj.get(name)
                else:
                    candidate = getattr(settings_obj, name, None)
                if candidate is not None:
                    return _coerce_bool(candidate, default)

            return default

        return cls(
            gapless_enabled=_lookup("gapless_enabled", _defaults["gapless_enabled"]),
            artwork_sync_enabled=_lookup("artwork_sync_enabled", _defaults["artwork_sync_enabled"]),
            hot_buffer_enabled=_lookup("hot_buffer_enabled", _defaults["hot_buffer_enabled"]),
            fallback_to_legacy_backend=_lookup("fallback_to_legacy_backend", _defaults["fallback_to_legacy_backend"]),
        )

    def to_dict(self) -> dict[str, bool]:
        """Return a dict representation friendly for logging and telemetry."""
        return {
            "gapless_enabled": self.gapless_enabled,
            "artwork_sync_enabled": self.artwork_sync_enabled,
            "hot_buffer_enabled": self.hot_buffer_enabled,
            "fallback_to_legacy_backend": self.fallback_to_legacy_backend,
        }
