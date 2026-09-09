"""
Server Settings Schema for Viola Cross-Device Sync.

This module defines the versioned schema for user settings that sync across devices.
Only a small allowlist of "sync-worthy" keys is validated and managed - device-local
settings (95+ keys) remain client-side only.

Schema Design Principles:
- Stored JSON always includes "schema_version" key
- Unknown keys are REJECTED (safest approach, keys added to allowlist as needed)
- Migrations are deterministic and idempotent
- Defaults filled for missing keys during normalization

Usage:
    >>> from auth.settings_schema import normalize_settings, validate_settings_patch
    >>> normalized = normalize_settings({"theme": "dark"})
    >>> errors = validate_settings_patch({"theme": "invalid"})
"""

from __future__ import annotations

from typing import Any

from core.json_types import JsonDict
from core.logging_config import get_logger
from core.product import PlanId

logger = get_logger("viola.auth.settings_schema")

# =============================================================================
# Schema Version & Constants
# =============================================================================

CURRENT_SCHEMA_VERSION = 3

# Allowed theme values
VALID_THEMES = frozenset({"light", "dark", "system", "high_contrast"})

# =============================================================================
# Schema Definition: Sync-worthy settings allowlist
# =============================================================================

# Default values for all sync-worthy settings
SETTINGS_DEFAULTS: JsonDict = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "theme": "system",
    "locale": "en_US",
    "autoplay_enabled": True,
    "allow_explicit": True,
    "tts_enabled": True,
    "tts_voice": "default",
    "default_music_volume": 80,
    "active_music_provider_id": None,
    "cloud_sync_provider_tokens": False,
    # Privacy consent (opt-in required for all cloud data transmission)
    "consent_cloud_stt": False,
    "consent_cloud_sync": False,
    "consent_error_reporting": False,
    # Custom instructions for LLM persona (freeform text, max 2000 chars)
    "custom_instructions": "",
}

ALLOWED_KEYS = frozenset(SETTINGS_DEFAULTS.keys())

# Reserved blob metadata managed by this module. These keys can appear in the
# settings JSON returned by this schema, but they are not user-scoped preference
# rows and must not be accepted as direct PATCH/sync keys.
SETTINGS_META_KEYS = frozenset({"schema_version"})

# Schema-managed settings that are not represented as sync_user_preferences
# rows. They either have their own canonical cloud surface or are retired
# compatibility flags retained in the normalized settings blob.
SETTINGS_NON_SYNC_ROW_KEYS = SETTINGS_META_KEYS | frozenset(
    {
        "cloud_sync_provider_tokens",
        "consent_cloud_stt",
        "consent_error_reporting",
    }
)

# Keys that users can PATCH (excludes schema metadata)
PATCHABLE_KEYS = ALLOWED_KEYS - SETTINGS_META_KEYS

# Keys that can be represented as per-key cloud preference rows.
SYNCABLE_KEYS = ALLOWED_KEYS - SETTINGS_NON_SYNC_ROW_KEYS


# =============================================================================
# Type Validators
# =============================================================================


def _validate_bool(value: Any, key: str) -> str | None:
    """Validate boolean field. Returns error message or None."""
    if not isinstance(value, bool):
        return f"'{key}' must be a boolean, got {type(value).__name__}"
    return None


def _validate_string(value: Any, key: str) -> str | None:
    """Validate string field. Returns error message or None."""
    if not isinstance(value, str):
        return f"'{key}' must be a string, got {type(value).__name__}"
    return None


def _validate_optional_string(value: Any, key: str) -> str | None:
    """Validate optional string field (string or null). Returns error message or None."""
    if value is not None and not isinstance(value, str):
        return f"'{key}' must be a string or null, got {type(value).__name__}"
    return None


def _validate_int_range(value: Any, key: str, min_val: int, max_val: int) -> str | None:
    """Validate integer within range. Returns error message or None."""
    if not isinstance(value, int) or isinstance(value, bool):
        return f"'{key}' must be an integer, got {type(value).__name__}"
    if value < min_val or value > max_val:
        return f"'{key}' must be between {min_val} and {max_val}, got {value}"
    return None


def _validate_theme(value: Any, key: str) -> str | None:
    """Validate theme field. Returns error message or None."""
    if not isinstance(value, str):
        return f"'{key}' must be a string, got {type(value).__name__}"
    if value not in VALID_THEMES:
        return f"'{key}' must be one of {sorted(VALID_THEMES)}, got '{value}'"
    return None


# Field validators mapping
FIELD_VALIDATORS: dict[str, Any] = {
    "theme": _validate_theme,
    "locale": _validate_string,
    "autoplay_enabled": _validate_bool,
    "allow_explicit": _validate_bool,
    "tts_enabled": _validate_bool,
    "tts_voice": _validate_string,
    "default_music_volume": lambda v, k: _validate_int_range(v, k, 0, 100),
    "active_music_provider_id": _validate_optional_string,
    "cloud_sync_provider_tokens": _validate_bool,
    "consent_cloud_stt": _validate_bool,
    "consent_cloud_sync": _validate_bool,
    "consent_error_reporting": _validate_bool,
    "custom_instructions": lambda v, k: (
        _validate_string(v, k)
        or (
            "'%s' must be at most 2000 characters, got %d" % (k, len(v))
            if isinstance(v, str) and len(v) > 2000
            else None
        )
    ),
}


# =============================================================================
# Core Functions
# =============================================================================


def _defaults_for_plan(plan_id: PlanId | str | None = None) -> JsonDict:
    return dict(SETTINGS_DEFAULTS)


def normalize_settings(raw: JsonDict, *, plan_id: PlanId | str | None = None) -> JsonDict:
    """
    Normalize settings to current schema.

    Ensures schema_version is present and fills defaults for missing keys.
    This is idempotent - calling it multiple times has no additional effect.

    Args:
        raw: Raw settings dict (may be missing keys or schema_version)

    Returns:
        Normalized settings dict with all required keys and schema_version
    """
    result: JsonDict = {}

    # Start with defaults. Paid plans have already paid for managed AI, so the
    # cloud-LLM consent default is enabled only for their synced settings.
    for key, default in _defaults_for_plan(plan_id).items():
        if key in raw:
            result[key] = raw[key]
        else:
            result[key] = default

    # Always set current schema version
    result["schema_version"] = CURRENT_SCHEMA_VERSION

    return result


def migrate_settings(raw: JsonDict, *, plan_id: PlanId | str | None = None) -> JsonDict:
    """
    Migrate settings from older schema versions to current.

    Migration is deterministic and idempotent. Returns normalized settings
    at CURRENT_SCHEMA_VERSION.

    Args:
        raw: Raw settings dict (may have old schema_version or none)

    Returns:
        Migrated and normalized settings dict
    """
    # Get current schema version (0 if not present)
    current_version = raw.get("schema_version", 0)

    if not isinstance(current_version, int):
        current_version = 0

    # Apply migrations in order
    migrated = dict(raw)

    # Migration: v0 -> v1 (initial schema)
    # v0 = any blob without schema_version (legacy data)
    if current_version < 1:
        # Add schema_version
        migrated["schema_version"] = 1
        # Ensure cloud_sync_provider_tokens exists with default false
        if "cloud_sync_provider_tokens" not in migrated:
            migrated["cloud_sync_provider_tokens"] = False
        logger.debug("Migrated settings from v%d to v1", current_version)

    # Migration: v1 -> v2 (add privacy consent fields)
    if current_version < 2:
        consent_defaults = _defaults_for_plan(plan_id)
        for consent_key in (
            "consent_cloud_stt",
            "consent_cloud_sync",
            "consent_error_reporting",
        ):
            if consent_key not in migrated:
                migrated[consent_key] = consent_defaults[consent_key]
        migrated["schema_version"] = 2
        logger.debug("Migrated settings from v%d to v2 (consent fields)", current_version)

    # Migration: v2 -> v3 (add custom_instructions)
    if current_version < 3:
        if "custom_instructions" not in migrated:
            migrated["custom_instructions"] = ""
        migrated["schema_version"] = 3
        logger.debug("Migrated settings from v%d to v3 (custom_instructions)", current_version)

    # Normalize to ensure all defaults are present
    return normalize_settings(migrated, plan_id=plan_id)


def validate_settings_patch(patch: JsonDict) -> dict[str, str]:
    """
    Validate a settings PATCH payload.

    Checks for:
    - Unknown keys (rejected)
    - Type/range validation on known keys

    Args:
        patch: Settings patch dict from PATCH request

    Returns:
        Dict of {field_name: error_message} for any validation errors.
        Empty dict means validation passed.
    """
    errors: dict[str, str] = {}

    for key, value in patch.items():
        # Skip schema_version in patch (managed internally)
        if key == "schema_version":
            errors[key] = "'schema_version' cannot be set directly"
            continue

        # Check if key is allowed
        if key not in PATCHABLE_KEYS:
            errors[key] = f"unknown setting key '{key}'"
            continue

        # Validate value type/range
        validator = FIELD_VALIDATORS.get(key)
        if validator:
            error = validator(value, key)
            if error:
                errors[key] = error

    return errors


def filter_to_allowed_keys(settings: JsonDict) -> JsonDict:
    """
    Filter settings to only allowed keys.

    This is used when reading from storage to strip any unknown keys
    that may have been stored before stricter validation was added.

    Args:
        settings: Settings dict potentially with extra keys

    Returns:
        Settings dict with only allowed keys
    """
    return {k: v for k, v in settings.items() if k in ALLOWED_KEYS}


def settings_changed(old: JsonDict, new: JsonDict) -> bool:
    """
    Check if settings have meaningfully changed (for migration persistence).

    Compares only allowed keys, ignoring unknown keys.

    Args:
        old: Previous settings
        new: New settings

    Returns:
        True if any allowed key values differ
    """
    for key in ALLOWED_KEYS:
        if old.get(key) != new.get(key):
            return True
    return False
