"""
Settings API Endpoints for Viola UI

Provides REST API for managing settings.
"""

from __future__ import annotations

import re as _re
from typing import Any, NamedTuple

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import env as config_env
from contracts.api_response import failure_response, success_response
from core.constants import TIMEOUT_LONG, WAKE_SENSITIVITY_MIN
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from music.playlist_manager import get_playlist_manager
from services.llm.local_models import detect_local_ai_servers
from ui.settings_manager import (
    classify_setting_key,
    get_settings_manager,
    is_cloud_user_scoped_setting_key,
    is_user_facing_secret_key,
    setting_string_value_limit_bytes,
)

logger = get_logger(__name__)

# Maximum allowed length for any string setting value
_MAX_STRING_LENGTH = 10_000
_MAX_SETTINGS_BODY_BYTES = 64 * 1024

# Setting keys that represent volume (0-100 int range)
_VOLUME_KEYS = frozenset(
    {
        "default_music_volume",
        "microphone_volume",
        "speaker_volume",
    }
)

# Setting keys that represent TCP/UDP port numbers (1-65535 int range)
_PORT_KEYS = frozenset(
    {
        "api_port",
    }
)

# String settings that must be non-empty (empty string is not a valid value)
_REQUIRED_STRING_KEYS = frozenset(
    {
        "log_level",
        "voice_mode",
        "tts_voice",
    }
)

# Setting keys that represent filesystem paths — require traversal checks
_PATH_SETTINGS = frozenset({"local_music_folder"})
_WAKE_MODEL_RUNTIME_KEYS = frozenset(
    {
        "wake_word_active_model",
        "wake_word_model",
        "use_custom_wake_word",
        "custom_wake_word_name",
    }
)
_WAKE_DATA_CONTRIBUTION_PUBLIC_LAUNCH_ENABLED = False


class _CloudSyncConsentApi(NamedTuple):
    """The desktop cloud-sync consent mirror, resolved at call time (#4789)."""

    key: str
    outcomes: Any
    mirror_cloud_sync_consent: Any


def _cloud_sync_consent_api() -> _CloudSyncConsentApi | None:
    """Return the consent mirror, or None when it cannot be imported.

    Imported lazily AND defensively. Reaching the account's consent row means
    importing ``services.sync``, whose package init pulls asyncpg — and the frozen
    desktop bundle ships ``services/`` as data rather than analysed code, so a
    missing hidden import surfaces only in the installer (viola.spec names asyncpg
    for exactly this reason; #4421 is the same shape). A raw import here would turn
    that into a 500 on EVERY settings save, so it is caught: callers that are
    changing a consent key refuse (see ``_cloud_sync_consent_unavailable_response``),
    and every unrelated setting still saves.
    """
    try:
        from services.sync.desktop_consent import (
            CLOUD_SYNC_CONSENT_KEY,
            ConsentMirrorOutcome,
            mirror_cloud_sync_consent,
        )
    except ImportError:
        logger.exception("Desktop cloud-sync consent mirror is unavailable")
        return None
    return _CloudSyncConsentApi(CLOUD_SYNC_CONSENT_KEY, ConsentMirrorOutcome, mirror_cloud_sync_consent)


def _consent_change_denied_for_spoke(raw_request: Request) -> JSONResponse | None:
    """Refuse an account-consent change that arrived on a spoke credential.

    A paired LAN spoke is a trusted WINDOW into the hub: it may drive the agent
    and change local settings, but the sensitive denylist in
    ``auth/spoke_scopes.py`` keeps account, money, admin and secret surfaces on
    the hub itself — and ``/api/v1/cloud/settings`` is on that denylist precisely
    because it writes the account's cloud state.

    #4789 made ``/v1/settings`` a second door to that same cloud state, including a
    withdrawal that PURGES the account's synced Tier-2 data. ``/v1/settings`` is
    deliberately NOT on the spoke denylist (a spoke changing local preferences is
    a feature), so path-level denial is the wrong tool; the consent KEY is what has
    to stay hub-owner-only. Without this, pairing a second screen would hand it the
    power to revoke the owner's cloud consent and delete their cloud copy.
    """
    from auth.principals import SpokePrincipal

    principal = getattr(raw_request.state, "auth_principal", None)
    if not isinstance(principal, SpokePrincipal):
        return None
    message = "Cloud sync can only be changed on the computer running Viola."
    logger.warning("Refused a cloud-sync consent change from a spoke device: %s", principal.device_id)
    return JSONResponse(
        status_code=403,
        content=failure_response(
            "consent_change_requires_hub",
            message,
            data={"ok": False, "error": message},
        ),
    )


def _consent_keys_present(values: dict[str, Any]) -> list[str]:
    """Consent keys in *values*, used only when the mirror could not be imported.

    Deliberately the WIDER ``CLOUD_CONSENT_KEYS`` set rather than the server's
    narrow bootstrap set: with the mirror unavailable there is no way to read the
    narrow set, and refusing too much is the safe direction for a consent change.
    """
    from ui.settings_schema import CLOUD_CONSENT_KEYS

    return sorted(set(values) & set(CLOUD_CONSENT_KEYS))


def _cloud_sync_consent_unavailable_response(keys: list[str]) -> JSONResponse:
    message = "Consent settings could not be changed right now. Nothing was saved. Try again."
    logger.error("Refused a consent change with no mirror available: %s", keys)
    return JSONResponse(
        status_code=503,
        content=failure_response(
            "cloud_sync_consent_not_recorded",
            message,
            data={"ok": False, "error": message},
        ),
    )


def _cloud_sync_consent_refusal_response(consent_api: _CloudSyncConsentApi, mirror_result: Any) -> JSONResponse:
    """Turn a refused mirror into the envelope the Settings panel explains.

    401 when nobody is signed in (the user has to act); 502 when the cloud write
    was attempted and did not land (Viola's problem, retryable). The outcome enum
    comes off *consent_api* rather than a fresh import, so this module keeps exactly
    ONE import of the mirror and it stays the guarded one above.
    """
    if mirror_result.outcome is consent_api.outcomes.NO_ACCOUNT:
        status_code, error_code = 401, "cloud_sync_consent_signin_required"
    else:
        status_code, error_code = 502, "cloud_sync_consent_not_recorded"
    return JSONResponse(
        status_code=status_code,
        content=failure_response(
            error_code,
            mirror_result.message,
            data={"ok": False, "error": mirror_result.message},
        ),
    )


_DANGEROUS_HTML_PATTERN = _re.compile(
    r"<\s*(?:script|iframe|object|embed|form|link|style|svg|math|img\b[^>]*\bon)",
    _re.I,
)

# Allowlist for safe configuration-value characters.
# Rejects shell metacharacters (;|$`!), SQL injection chars ('), etc.
_SAFE_STRING_PATTERN = _re.compile(r"^[a-zA-Z0-9\s.\-_:/\\]+$")
_PHONE_NUMBER_PATTERN = _re.compile(r"^[+0-9().\-\s]{7,32}$")
_PHONE_NUMBER_SETTINGS = frozenset({"callback_phone", "user_phone_number", "founder_phone_number"})

# String settings that legitimately require characters outside the safe pattern.
# These are exempt from the _SAFE_STRING_PATTERN check.
_RELAXED_STRING_SETTINGS = frozenset(
    {
        # URLs need @, ?, &, =, #, % …
        "llm_base_url",
        "home_assistant_url",
        # API keys / secrets may contain arbitrary characters
        "llm_api_key",
        "openai_api_key",
        "home_assistant_token",
        # Audio device names can contain parentheses, commas, brackets
        "input_device",
        "output_device",
        # Weather location may include commas and apostrophes ("O'Fallon, MO")
        "weather_location",
        # Hotkey strings may include + and other combos
        "ptt_hotkey",
        "ptt_hotkey_display",
        "mute_hotkey",
        "mute_hotkey_display",
        # User-authored prompt guidance may include punctuation and newlines.
        "custom_instructions",
        # Hex color values include '#' which is not in the safe-string pattern.
        # AccentPicker.jsx normalizes to /^#[0-9a-fA-F]{6}$/ before submitting.
        "accent_color",
        # Messaging — bot tokens contain colons, dashes, mixed chars
        "telegram_bot_token",
        "slack_bot_token",
        "slack_app_token",
    }
)

# ── LLM provider lock ──────────────────────────────────────────────
# These settings control which LLM is used and are LOCKED to their managed
# defaults unless the user is BYOK (has their own llm_api_key).
# This prevents agents or automated callers from switching the provider and
# burning money on the wrong API.  See ADR-style directive in git history.
_LLM_LOCKED_SETTINGS = frozenset({"llm_provider", "llm_model", "agent_model"})

# Allowlists for settings that must match a fixed set of valid values.
# Maps setting key -> frozenset of valid string values.
_ENUM_SETTINGS: dict[str, frozenset[str]] = {
    "llm_provider": frozenset(
        {
            "openai",
            "anthropic",
            "google",
            "ollama",
            "openai_compatible",
        }
    ),
    "voice_mode": frozenset(
        {
            "disabled",
            "push_to_talk",
            "wake_word",
        }
    ),
    "ai_source": frozenset(
        {
            "managed",
            "byok",
            "codex",
            "local",
        }
    ),
    "phone_mode": frozenset(
        {
            "cloud",
            "local",
        }
    ),
    # Optional first-run "How did you hear about Viola?" self-report.
    # Mirrors FUNNEL_ATTRIBUTION_OPTIONS (the funnel store's server-side
    # normalization enum); "" means not answered / prefer not to say.
    "attribution_self_report": frozenset(
        {
            "",
            "reddit",
            "x",
            "youtube",
            "tiktok",
            "hn",
            "search",
            "friend",
            "blog",
            "other",
        }
    ),
}

_SECRET_SETTING_SUFFIXES = ("_token", "_api_key", "_access_token")
_SECRET_SETTING_NAMES = frozenset({"openai_api_key", "llm_api_key"})
_SECRET_REDACTION = "••••••"  # nosec B105


def _settings_dev_mode_bypass_enabled() -> bool:
    if config_env.get_bool("VIOLA_DEV_MODE", default=False):
        return True
    try:
        from config.settings import settings as app_config

        return bool(getattr(app_config, "dev_mode", False))
    except Exception:
        return False


def _settings_strict_classifier_enabled() -> bool:
    if _settings_dev_mode_bypass_enabled():
        return False
    try:
        from config.settings import settings as app_config

        deployment_mode = str(
            getattr(app_config, "deployment_mode", None) or getattr(app_config, "app_surface", "desktop")
        ).strip()
    except Exception:
        deployment_mode = "desktop"
    return deployment_mode.lower() == "cloud"


def _setting_string_value_size_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _oversized_string_setting_errors(settings: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, value in settings.items():
        if not isinstance(value, str):
            continue
        limit = setting_string_value_limit_bytes(key)
        size = _setting_string_value_size_bytes(value)
        if size > limit:
            errors.append("Setting '%s' exceeds %d bytes (%d bytes received)" % (key, limit, size))
    return errors


def _validate_phone_number_setting(key: str, value: str) -> str | None:
    """Return an error message when a stored user phone hint is malformed."""
    if not value:
        return None
    digits = _re.sub(r"\D", "", value)
    if not _PHONE_NUMBER_PATTERN.match(value) or not (7 <= len(digits) <= 15):
        return "Setting '%s' must be a phone number with 7 to 15 digits" % key
    return None


def _is_secret_setting_key(key: str) -> bool:
    return key in _SECRET_SETTING_NAMES or key.endswith(_SECRET_SETTING_SUFFIXES)


def _strip_redacted_placeholders(settings: dict[str, Any]) -> dict[str, Any]:
    """Remove secret keys whose value is a harmless round-trip echo.

    The frontend receives secrets as ``"••••••"`` (non-empty secrets) or ``""``
    (unset secrets — ``_redact_settings_for_response`` only masks truthy values)
    and sends the whole blob back unchanged when the user saves. Without this
    filter the placeholder would overwrite the real secret, and an empty echo
    would trip the secret_key_forbidden guard and 403 the ENTIRE save — the
    default state of a fresh install (settings.json ships ``openai_api_key: ""``),
    so every GUI settings save failed (2026-07-06). Neither shape is a secret
    being *set*; both are no-op echoes and are dropped before validation.
    """
    return {
        k: v
        for k, v in settings.items()
        if not (_is_secret_setting_key(k) and isinstance(v, str) and v in ("", _SECRET_REDACTION))
    }


def _current_setting_value_for_request(settings_mgr: Any, key: str, user_id: str | None) -> object:
    if user_id is None:
        return settings_mgr.get(key)
    try:
        return settings_mgr.get(key, user_id=user_id)
    except TypeError as exc:
        if "user_id" not in str(exc):
            raise
        return settings_mgr.get(key)


def _strip_unchanged_setting_echoes(
    incoming: dict[str, Any],
    settings_mgr: Any,
    *,
    user_id: str | None,
    is_system_key: Any,
) -> tuple[list[str], list[str]]:
    stripped_unchanged: list[str] = []
    changed_system_keys: list[str] = []
    defaults = getattr(settings_mgr, "DEFAULT_SETTINGS", {})

    for key in list(incoming.keys()):
        system_key = bool(is_system_key(key))
        if key in defaults or system_key:
            current = _current_setting_value_for_request(settings_mgr, key, user_id)
            if incoming[key] == current:
                stripped_unchanged.append(key)
                incoming.pop(key, None)
                continue
        if system_key:
            changed_system_keys.append(key)

    return stripped_unchanged, changed_system_keys


def _redact_settings_for_response(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of settings with secret values masked for API responses."""
    redacted = dict(settings)
    for key, value in redacted.items():
        if _is_secret_setting_key(key) and isinstance(value, str) and value:
            redacted[key] = _SECRET_REDACTION
    return redacted


def _settings_response_payload(settings: dict[str, Any]) -> dict[str, Any]:
    """Build a settings response with non-persisted runtime voice status."""
    from diagnostics.voice_status import get_voice_status

    redacted = _redact_settings_for_response(settings)
    # Back-compat alias for one release window: echo `capability_tier` so
    # older clients that read by the legacy name still get the value
    # (codex follow-up audit A3, 2026-05-11). Plan to drop after one
    # release cycle.
    if "agent_autonomy" in redacted and "capability_tier" not in redacted:
        redacted["capability_tier"] = redacted["agent_autonomy"]
    return {
        "settings": redacted,
        "voice_status": get_voice_status(),
    }


def _enforce_llm_lock(
    incoming: dict[str, Any],
    settings_mgr: Any,
) -> tuple[dict[str, Any], list[str]]:
    """Strip LLM provider/model keys unless the user is BYOK.

    The default managed LLM is an infrastructure constant.
    Only users who bring their own API key (BYOK) may change the provider or
    model.  BYOK is detected by a non-empty ``llm_api_key`` either already
    stored in settings or being supplied in the same request.
    """
    locked_keys = _LLM_LOCKED_SETTINGS & incoming.keys()
    if not locked_keys:
        return incoming, []

    # Check BYOK: existing key in settings OR new key in this request
    existing_key = settings_mgr.get("llm_api_key", "") or ""
    incoming_key = incoming.get("llm_api_key", "") or ""
    # Ignore redacted placeholder — it means the frontend echoed back the mask
    if incoming_key == _SECRET_REDACTION:
        incoming_key = ""

    is_byok = bool(existing_key.strip()) or bool(incoming_key.strip())
    ai_source_raw = incoming.get("ai_source", settings_mgr.get("ai_source", ""))
    ai_source = ai_source_raw if isinstance(ai_source_raw, str) else ""
    provider_raw = incoming.get("llm_provider", settings_mgr.get("llm_provider", ""))
    provider = provider_raw if isinstance(provider_raw, str) else ""
    is_local_ai = ai_source == "local" and provider in {"", "ollama", "openai_compatible"}

    if is_byok or is_local_ai:
        return incoming, []

    # Not BYOK — strip the locked keys and report errors
    filtered = {k: v for k, v in incoming.items() if k not in locked_keys}
    errors = [
        "Setting '%s' is locked to its default value. "
        "Provide your own API key (llm_api_key) to use a custom LLM provider." % k
        for k in sorted(locked_keys)
    ]
    logger.warning(
        "Rejected LLM settings change (no BYOK key): %s",
        sorted(locked_keys),
    )
    return filtered, errors


def _validate_llm_cross_field_requirements(
    incoming: dict[str, Any],
    settings_mgr: Any,
) -> list[str]:
    """Validate cross-field LLM requirements after basic per-key validation."""
    llm_keys = frozenset({"ai_source", "llm_provider", "llm_model", "llm_base_url", "llm_api_key"})
    if not (llm_keys & incoming.keys()):
        return []

    provider_raw = incoming.get("llm_provider", settings_mgr.get("llm_provider", "openai"))
    provider = provider_raw if isinstance(provider_raw, str) else ""
    ai_source_raw = incoming.get("ai_source", settings_mgr.get("ai_source", "managed"))
    ai_source = ai_source_raw if isinstance(ai_source_raw, str) else ""
    model_raw = incoming.get("llm_model", settings_mgr.get("llm_model", ""))
    model = model_raw if isinstance(model_raw, str) else ""

    if provider == "openai_compatible" and ai_source in {"byok", "local"} and not model.strip():
        return ["Setting 'llm_model' is required when llm_provider is 'openai_compatible'."]

    return []


def _normalize_hotkey_for_compare(value: str) -> str:
    """Normalize a hotkey string for equality comparison.

    Lowercases each '+'-joined token and sorts them so 'Ctrl+Shift+M' and
    'Shift+Ctrl+M' compare equal, matching how ``hotkeys.js`` parses/matches
    a combo order-independently.
    """
    tokens = [t.strip().lower() for t in str(value or "").split("+") if t.strip()]
    return "+".join(sorted(tokens))


def _validate_hotkey_cross_field_requirements(
    incoming: dict[str, Any],
    settings_mgr: Any,
) -> list[str]:
    """PTT and Mute are two independent global hotkeys registered on the same
    keydown/keyup listener (see ui/react-app/src/SmartDisplay.jsx). Binding
    them to the identical combo would make one silently shadow the other
    depending on handler order — reject the save instead of allowing that.
    """
    hotkey_keys = frozenset({"ptt_hotkey", "mute_hotkey"})
    if not (hotkey_keys & incoming.keys()):
        return []

    ptt_hotkey = incoming.get("ptt_hotkey", settings_mgr.get("ptt_hotkey", "space"))
    mute_hotkey = incoming.get("mute_hotkey", settings_mgr.get("mute_hotkey", "ctrl+m"))
    if not isinstance(ptt_hotkey, str) or not isinstance(mute_hotkey, str):
        return []

    if _normalize_hotkey_for_compare(ptt_hotkey) == _normalize_hotkey_for_compare(mute_hotkey):
        return [
            "Setting 'mute_hotkey' cannot use the same key combination as 'ptt_hotkey' "
            "(%s) — choose a different key." % ptt_hotkey
        ]
    return []


def _validate_settings(
    incoming: dict[str, Any],
    defaults: dict[str, object],
) -> tuple[dict[str, Any], list[str]]:
    """Validate incoming settings against the defaults schema.

    Returns a (clean_settings, errors) tuple.  ``clean_settings`` contains only
    the values that passed validation.  ``errors`` is a list of human-readable
    messages for values that were rejected.

    Validation rules (minimal-abuse-prevention, NOT exhaustive allow-listing):
    * Type must match the type of the corresponding default value.
    * String values must not exceed ``_MAX_STRING_LENGTH`` characters.
    * Volume keys must be int in the 0-100 range.
    """
    clean: dict[str, Any] = {}
    errors: list[str] = []

    for key, value in incoming.items():
        default = defaults.get(key)
        if default is None and key in defaults:
            # Default is explicitly None (e.g. active_music_provider_id) — accept any scalar
            if isinstance(value, (str, int, float, bool, type(None))):
                if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
                    errors.append(
                        "Setting '%s' value exceeds maximum length of %d characters" % (key, _MAX_STRING_LENGTH)
                    )
                    continue
                if isinstance(value, str) and _DANGEROUS_HTML_PATTERN.search(value):
                    errors.append("Setting '%s' contains disallowed HTML content" % key)
                    continue
                if isinstance(value, str) and key in _PHONE_NUMBER_SETTINGS:
                    phone_error = _validate_phone_number_setting(key, value)
                    if phone_error:
                        errors.append(phone_error)
                        continue
                    clean[key] = value
                    continue
                if (
                    isinstance(value, str)
                    and value
                    and key not in _RELAXED_STRING_SETTINGS
                    and not _SAFE_STRING_PATTERN.match(value)
                ):
                    errors.append("Setting '%s' contains disallowed characters" % key)
                    continue
                clean[key] = value
            else:
                errors.append("Setting '%s' must be a scalar value, got %s" % (key, type(value).__name__))
            continue

        if default is None:
            # Key not in defaults at all — should have been filtered already, skip
            continue

        # --- Type check ---
        expected_type = type(default)

        # bool is a subclass of int in Python, so check bool first
        if expected_type is bool:
            if not isinstance(value, bool):
                errors.append("Setting '%s' must be a boolean, got %s" % (key, type(value).__name__))
                continue
        elif expected_type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append("Setting '%s' must be an integer, got %s" % (key, type(value).__name__))
                continue
        elif expected_type is float:
            if isinstance(value, bool):
                errors.append("Setting '%s' must be a number, got bool" % key)
                continue
            if not isinstance(value, (int, float)):
                errors.append("Setting '%s' must be a number, got %s" % (key, type(value).__name__))
                continue
            value = float(value)
        elif expected_type is str:
            if not isinstance(value, str):
                errors.append("Setting '%s' must be a string, got %s" % (key, type(value).__name__))
                continue
            if len(value) > _MAX_STRING_LENGTH:
                errors.append("Setting '%s' value exceeds maximum length of %d characters" % (key, _MAX_STRING_LENGTH))
                continue

            # --- Stored XSS prevention for string values ---
            if _DANGEROUS_HTML_PATTERN.search(value):
                errors.append("Setting '%s' contains disallowed HTML content" % key)
                continue

            if key in _PHONE_NUMBER_SETTINGS:
                phone_error = _validate_phone_number_setting(key, value)
                if phone_error:
                    errors.append(phone_error)
                    continue
                clean[key] = value
                continue

            # --- Path traversal prevention for filesystem path settings ---
            if key in _PATH_SETTINGS and value:
                from pathlib import PurePath

                normalized = str(PurePath(value))
                # Reject path traversal sequences
                if ".." in PurePath(normalized).parts:
                    errors.append("Setting '%s' must not contain path traversal sequences (..)" % key)
                    continue
                # On Windows, reject UNC paths that could reach network shares
                if normalized.startswith("\\\\"):
                    errors.append("Setting '%s' must not be a network (UNC) path" % key)
                    continue
                clean[key] = normalized  # Store normalized path
                continue  # Skip the default clean[key] assignment below

            # --- Injection prevention for general string settings ---
            if value and key not in _RELAXED_STRING_SETTINGS and not _SAFE_STRING_PATTERN.match(value):
                errors.append("Setting '%s' contains disallowed characters" % key)
                continue

        elif expected_type is dict:
            if not isinstance(value, dict):
                errors.append("Setting '%s' must be an object, got %s" % (key, type(value).__name__))
                continue

        # --- Range check for volume keys ---
        if key in _VOLUME_KEYS:
            if not isinstance(value, int) or value < 0 or value > 100:
                errors.append("Setting '%s' must be an integer between 0 and 100" % key)
                continue

        # --- Range check for port keys ---
        if key in _PORT_KEYS:
            if not isinstance(value, int) or value < 1 or value > 65535:
                errors.append("Setting '%s' must be a port number between 1 and 65535" % key)
                continue

        # --- Wake sensitivity floor ---
        if key == "wake_sensitivity":
            if not (WAKE_SENSITIVITY_MIN <= value <= 1.0):
                errors.append("Setting '%s' must be a number between %.2f and 1.00" % (key, WAKE_SENSITIVITY_MIN))
                continue

        # --- Non-empty check for required string keys ---
        if key in _REQUIRED_STRING_KEYS and isinstance(value, str) and not value.strip():
            errors.append("Setting '%s' must not be empty" % key)
            continue

        # --- Enum allowlist check for settings with a fixed set of valid values ---
        if key in _ENUM_SETTINGS and isinstance(value, str):
            allowed = _ENUM_SETTINGS[key]
            if value not in allowed:
                errors.append(
                    "Setting '%s' has invalid value '%s'. Valid values: %s" % (key, value, ", ".join(sorted(allowed)))
                )
                continue

        clean[key] = value

    return clean, errors


# Pydantic models for API
class SettingsUpdateRequest(BaseModel):
    """Request to update settings."""

    settings: dict[str, Any]


class SettingsResponse(BaseModel):
    """Response with settings."""

    ok: bool
    settings: dict[str, Any] = {}
    voice_status: dict[str, Any] = {}
    error: str | None = None


class DeviceInfo(BaseModel):
    """Audio device information."""

    index: int
    name: str
    channels: int


class DevicesResponse(BaseModel):
    """Response with available audio devices."""

    ok: bool
    input_devices: list[DeviceInfo] = []
    output_devices: list[DeviceInfo] = []
    error: str | None = None


def _trigger_local_library_rescan(folder: str) -> None:
    """Kick off a background local library rescan for the given folder."""
    import threading
    from pathlib import Path

    if not Path(folder).is_dir():
        logger.warning("Local music folder does not exist, skipping rescan: %s", folder)
        return

    def _rescan() -> None:
        try:
            from music.providers.local.db import get_local_library_repo
            from music.providers.local.scanner import scan_and_index

            repo = get_local_library_repo()
            repo.initialize()
            stats = scan_and_index(folder, repo)
            logger.info("Local library rescan complete: %s", stats)
        except Exception as exc:
            logger.exception("Local library rescan failed: %s", exc)

    thread = threading.Thread(target=_rescan, name="local-library-rescan-settings", daemon=True)
    thread.start()
    logger.info("Local library rescan triggered for folder: %s", folder)


_STT_RUNTIME_KEYS = frozenset({"stt_engine", "whisper_model", "whisper_device", "whisper_language"})
_AUDIO_DEVICE_RUNTIME_KEYS = frozenset({"input_device", "output_device"})


def _blank_to_none(value: object) -> object | None:
    return None if value in (None, "") else value


def _apply_audio_stt_settings_to_app_config(settings_mgr: Any) -> None:
    """Mirror SettingsManager values into AppConfig for legacy runtime readers."""
    from config.settings import settings as app_config

    stt_engine = settings_mgr.get("stt_engine", getattr(app_config, "stt_backend", "whisper_local"))
    app_config.stt_backend = str(stt_engine or "whisper_local")
    for key in ("whisper_model", "whisper_device", "whisper_language"):
        value = settings_mgr.get(key, getattr(app_config, key, ""))
        if value not in (None, ""):
            setattr(app_config, key, value)

    input_device = _blank_to_none(settings_mgr.get("input_device", getattr(app_config, "input_device", None)))
    output_device = _blank_to_none(settings_mgr.get("output_device", getattr(app_config, "output_device", None)))
    app_config.input_device = input_device
    app_config.output_device = output_device


def _find_voice_pipeline(app: Any) -> Any | None:
    state = getattr(app, "state", None)
    if state is None:
        return None
    candidates = [
        getattr(state, "voice_orchestrator", None),
        getattr(getattr(state, "bootstrap_result", None), "voice", None),
    ]
    for candidate in candidates:
        pipeline = getattr(candidate, "voice_pipeline", None)
        if pipeline is not None:
            return pipeline
    return None


def _restart_transcriber_after_settings_change(app: Any) -> None:
    pipeline = _find_voice_pipeline(app)
    if pipeline is None:
        logger.debug("No active voice pipeline to restart after STT settings change")
        return
    restart = getattr(pipeline, "_restart_transcriber", None)
    if not callable(restart):
        logger.debug("Active voice pipeline has no transcriber restart hook")
        return
    restarted = bool(restart())
    if restarted:
        logger.info("STT transcriber reinitialized after settings change")
    else:
        logger.warning("STT transcriber reinitialization failed after settings change")


def _restart_wake_detector_after_settings_change(app: Any) -> None:
    pipeline = _find_voice_pipeline(app)
    if pipeline is None:
        logger.debug("No active voice pipeline to restart after audio-device settings change")
        return
    detector = getattr(pipeline, "wake_detector", None)
    if detector is None or not callable(getattr(detector, "is_running", None)) or not detector.is_running():
        logger.debug("Wake detector is not running; audio-device change requires no live restart")
        return
    restart = getattr(pipeline, "_restart_wake_detector", None)
    if not callable(restart):
        logger.debug("Active voice pipeline has no wake detector restart hook")
        return
    restarted = bool(restart())
    if restarted:
        logger.info("Wake detector restarted after audio-device settings change")
    else:
        logger.warning("Wake detector restart failed after audio-device settings change")


def _validate_audio_devices_after_settings_change() -> None:
    try:
        from audio_core.device_validation import validate_audio_devices

        status = validate_audio_devices()
        logger.info(
            "Audio devices revalidated after settings change: input_ok=%s output_ok=%s",
            status.input_ok,
            status.output_ok,
        )
    except Exception as exc:
        logger.warning("Failed to revalidate audio devices after settings change: %s", exc)


def create_settings_router(*, music_service: object | None = None) -> APIRouter:
    """Create FastAPI router for settings endpoints.

    Args:
        music_service: Optional music service with a ``stop()`` method.
            When provided, provider switches will stop playback and
            clear the queue automatically.
    """
    # Accept JWT/session auth OR API key — desktop apps may not have a user account
    from auth.dependencies import require_auth_or_api_key

    router = APIRouter(
        prefix="/v1/settings",
        tags=["settings"],
        dependencies=[Depends(require_auth_or_api_key)],
    )
    settings_mgr = get_settings_manager()

    def _get_request_user_id(request: Request) -> str:
        user_context = getattr(request.state, "user_context", None)
        if user_context and getattr(user_context, "user_id", None):
            return user_context.user_id

        session = getattr(request.state, "session", None)
        if session and getattr(session, "user_id", None):
            return session.user_id

        raise HTTPException(status_code=401, detail="Not authenticated")

    def _get_broadcast_user_id(request: Request) -> str | None:
        try:
            return _get_request_user_id(request)
        except HTTPException:
            return None

    def _effective_settings_snapshot(request: Request) -> dict[str, Any]:
        """Return settings as this request's caller will read them back."""
        try:
            user_id = _get_request_user_id(request)
        except HTTPException:
            user_id = None

        snapshot = dict(settings_mgr.settings)
        if user_id is None:
            return snapshot

        is_user_scoped_key = getattr(settings_mgr, "_is_user_scoped_key", lambda _key: True)
        for key in settings_mgr.DEFAULT_SETTINGS:
            if is_user_scoped_key(key):
                fallback = settings_mgr.DEFAULT_SETTINGS.get(key)
            else:
                fallback = snapshot.get(key, settings_mgr.DEFAULT_SETTINGS.get(key))
            snapshot[key] = settings_mgr.get(key, fallback, user_id=user_id)
        return snapshot

    async def _handle_settings_mutation(
        *,
        request: SettingsUpdateRequest,
        raw_request: Request,
        request_log_message: str,
        mutation_kind_label: str,
        success_log_message: str,
        failure_log_message: str,
    ) -> JSONResponse:
        try:
            body_size = len(await raw_request.body())
            if body_size > _MAX_SETTINGS_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content=failure_response(
                        "settings_payload_too_large",
                        "Settings payload exceeds %d bytes" % _MAX_SETTINGS_BODY_BYTES,
                    ),
                )

            logger.info(request_log_message, list(request.settings.keys()))
            try:
                request_user_id = _get_request_user_id(raw_request)
            except HTTPException:
                request_user_id = None

            rejected_secrets = [
                k
                for k, v in request.settings.items()
                # A secret is only being SET when the value is a real string.
                # The sentinel ("••••••") and the empty string are round-trip
                # echoes of the GET response (unset secrets are returned as ""
                # because _redact_settings_for_response masks only truthy
                # values) — rejecting them 403'd every full-form save on a
                # fresh install (settings.json ships openai_api_key: "").
                # Echo shapes are stripped by _strip_redacted_placeholders
                # before any write, so they can never land in settings.json.
                if is_user_facing_secret_key(k) and not (isinstance(v, str) and v in ("", _SECRET_REDACTION))
            ]
            if rejected_secrets:
                rejected_secret_names = sorted(str(name) for name in rejected_secrets)
                logger.warning(
                    "Rejected settings %s with secret keys: %s",
                    mutation_kind_label,
                    rejected_secret_names,
                )
                return JSONResponse(
                    status_code=403,
                    content=failure_response(
                        "secret_key_forbidden",
                        "Secrets cannot be set via /settings. Use .env or the "
                        "encrypted credential store for: " + ", ".join(sorted(rejected_secrets)),
                    ),
                )

            # Normalize legacy aliases (e.g. capability_tier → agent_autonomy)
            # BEFORE filtering by DEFAULT_SETTINGS membership. Without this an
            # old client POSTing `capability_tier=X` would have the field
            # silently dropped — the SettingsManager alias would never fire.
            from ui.settings_manager import _is_system_key

            normalized_incoming = {}
            for raw_key, raw_value in request.settings.items():
                canonical = settings_mgr.canonicalize_setting_key(raw_key)
                normalized_incoming[canonical] = raw_value

            # The React UI echoes the full localSettings blob on every save.
            # Treat unchanged known values as no-ops before validation so one
            # stale invalid setting cannot block an unrelated setting change.
            stripped_unchanged, changed_system_keys = _strip_unchanged_setting_echoes(
                normalized_incoming,
                settings_mgr,
                user_id=request_user_id,
                is_system_key=_is_system_key,
            )
            if stripped_unchanged:
                logger.debug(
                    "Stripped %d unchanged setting echo(es) from settings save: %s",
                    len(stripped_unchanged),
                    sorted(stripped_unchanged),
                )
            if changed_system_keys:
                logger.warning(
                    "Rejected settings %s with changed system keys: %s",
                    mutation_kind_label,
                    sorted(changed_system_keys),
                )
                return JSONResponse(
                    status_code=403,
                    content=failure_response(
                        "system_key_forbidden",
                        "System/billing/auth settings cannot be modified via /settings. "
                        "Affected keys: " + ", ".join(sorted(changed_system_keys)),
                    ),
                )

            oversized_errors = _oversized_string_setting_errors(normalized_incoming)
            if oversized_errors:
                return JSONResponse(
                    status_code=413,
                    content=failure_response(
                        "setting_value_too_large",
                        "; ".join(oversized_errors),
                    ),
                )

            if _settings_strict_classifier_enabled():
                unknown_keys: list[str] = []
                for key in normalized_incoming:
                    try:
                        classify_setting_key(key, strict=True)
                    except KeyError:
                        unknown_keys.append(key)
                if unknown_keys:
                    logger.warning(
                        "Rejected settings %s with unknown keys in cloud strict mode: %s",
                        mutation_kind_label,
                        sorted(unknown_keys),
                    )
                    return JSONResponse(
                        status_code=400,
                        content=failure_response(
                            "unknown_setting_key",
                            "Unknown setting key(s): " + ", ".join(sorted(unknown_keys)),
                        ),
                    )

            known = {k: v for k, v in normalized_incoming.items() if k in settings_mgr.DEFAULT_SETTINGS}
            dropped = set(normalized_incoming) - set(known)
            legacy_dropped = dropped & settings_mgr.REMOVED_LEGACY_SETTING_KEYS
            for key in dropped - legacy_dropped:
                logger.warning("Ignoring unknown setting: %s", key)

            known, llm_lock_errors = _enforce_llm_lock(known, settings_mgr)
            if llm_lock_errors and not known:
                return JSONResponse(
                    status_code=403,
                    content=failure_response(
                        "llm_locked",
                        "; ".join(llm_lock_errors),
                    ),
                )

            validated, errors = _validate_settings(known, settings_mgr.DEFAULT_SETTINGS)
            cross_field_errors = _validate_llm_cross_field_requirements(validated, settings_mgr)
            hotkey_cross_field_errors = _validate_hotkey_cross_field_requirements(validated, settings_mgr)
            all_errors = llm_lock_errors + errors + cross_field_errors + hotkey_cross_field_errors
            if errors or cross_field_errors or hotkey_cross_field_errors:
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False,
                        "error": {
                            "code": "validation_error",
                            "message": "; ".join(all_errors),
                        },
                        "data": None,
                    },
                )

            validated = _strip_redacted_placeholders(validated)

            if not validated:
                return JSONResponse(
                    content=success_response(_settings_response_payload(_effective_settings_snapshot(raw_request))),
                )

            if request_user_id is None and _settings_strict_classifier_enabled():
                user_scoped_without_user = sorted(
                    key for key in validated if is_cloud_user_scoped_setting_key(key, strict=False)
                )
                if user_scoped_without_user:
                    logger.warning(
                        "Rejected settings %s without user_id for user-scoped keys: %s",
                        mutation_kind_label,
                        user_scoped_without_user,
                    )
                    return JSONResponse(
                        status_code=401,
                        content=failure_response(
                            "user_id_required",
                            "Authenticated user_id is required to modify user-scoped setting(s): "
                            + ", ".join(user_scoped_without_user),
                        ),
                    )

            old_folder = settings_mgr.get("local_music_folder", "")
            new_folder = validated.get("local_music_folder")

            old_provider = settings_mgr.get("active_music_provider_id", None)
            new_provider = validated.get("active_music_provider_id")

            old_wake_contribute = bool(
                settings_mgr.get(
                    "wake_word_training_opt_in",
                    settings_mgr.get("wake_data_contribute", False),
                )
            )
            new_wake_contribute = validated.get("wake_word_training_opt_in")
            legacy_new_wake_contribute = validated.get("wake_data_contribute")
            if new_wake_contribute is not None and "wake_data_contribute" not in validated:
                validated["wake_data_contribute"] = new_wake_contribute
            elif legacy_new_wake_contribute is not None and "wake_word_training_opt_in" not in validated:
                new_wake_contribute = legacy_new_wake_contribute
                validated["wake_word_training_opt_in"] = legacy_new_wake_contribute

            if not _WAKE_DATA_CONTRIBUTION_PUBLIC_LAUNCH_ENABLED and (
                "wake_word_training_opt_in" in validated or "wake_data_contribute" in validated
            ):
                if validated.get("wake_word_training_opt_in") or validated.get("wake_data_contribute"):
                    logger.warning("Wake data contribution is launch-excluded; forcing setting off")
                validated["wake_word_training_opt_in"] = False
                validated["wake_data_contribute"] = False
                new_wake_contribute = False

            old_autostart = settings_mgr.get("start_on_boot", False)
            old_voice_mode = settings_mgr.get("voice_mode", "push_to_talk")
            old_mic_muted = bool(settings_mgr.get("mic_muted", False))
            old_wake_model_vals = {key: settings_mgr.get(key) for key in _WAKE_MODEL_RUNTIME_KEYS if key in validated}

            _llm_keys = {"ai_source", "llm_provider", "llm_api_key", "llm_model", "llm_base_url"}
            old_llm_vals = {k: settings_mgr.get(k) for k in _llm_keys if k in validated}
            _ha_keys = {"home_assistant_url", "home_assistant_token"}
            old_ha_vals = {k: settings_mgr.get(k, "") for k in _ha_keys if k in validated}
            old_stt_vals = {k: settings_mgr.get(k, "") for k in _STT_RUNTIME_KEYS if k in validated}
            old_audio_device_vals = {k: settings_mgr.get(k, "") for k in _AUDIO_DEVICE_RUNTIME_KEYS if k in validated}

            # Cloud-sync consent is authoritative in cloud Postgres, not here.
            # Every cloud Tier-2 gate reads the sync_user_preferences row
            # (services/sync/consent.py), and revoking it purges the user's synced
            # data in the same transaction — so the cloud row has to move BEFORE
            # this file records the user's choice. Writing settings.json first and
            # the cloud "later" is exactly the #4789 bug: for months the toggle
            # only ever wrote the local file, so ON granted nothing and OFF
            # withdrew nothing. Mirror first, and refuse the whole save if the
            # cloud did not agree, so a consent surface can never claim a state
            # the account does not hold.
            consent_api = _cloud_sync_consent_api()
            if consent_api is None:
                unreachable = _consent_keys_present(validated)
                if unreachable:
                    return _cloud_sync_consent_unavailable_response(unreachable)
            elif consent_api.key in validated:
                spoke_refusal = _consent_change_denied_for_spoke(raw_request)
                if spoke_refusal is not None:
                    return spoke_refusal
                consent_mirror = await consent_api.mirror_cloud_sync_consent(bool(validated[consent_api.key]))
                if not consent_mirror.ok:
                    logger.warning(
                        "Refused settings %s: cloud-sync consent was not recorded (%s)",
                        mutation_kind_label,
                        consent_mirror.outcome.value,
                    )
                    return _cloud_sync_consent_refusal_response(consent_api, consent_mirror)

            success = settings_mgr.update(validated, save_immediately=True, user_id=request_user_id)

            if not success:
                from core.exceptions import ConfigurationError

                raise ConfigurationError("Failed to save settings to disk")

            # Persisting is not applying. ui/settings_effects.py holds the one
            # implementation of "make this setting real", shared with the voice
            # write path (intent/tools/settings_tools.py) so the two can never
            # drift into one applying and the other only storing.
            from ui.settings_effects import EffectOutcome, apply_setting_effect

            if new_folder is not None and new_folder != old_folder and new_folder:
                _trigger_local_library_rescan(str(new_folder))

            # Shared with the voice write path (intent/tools/settings_tools.py)
            # so a setting can never be applied by one and merely stored by the
            # other. ui/settings_effects.py owns the single implementation.
            if new_provider is not None and new_provider != old_provider:
                apply_setting_effect(
                    "active_music_provider_id",
                    new_provider,
                    settings_mgr=settings_mgr,
                    previous=old_provider,
                    music_service=music_service,
                )

            if new_wake_contribute is not None and old_wake_contribute and not new_wake_contribute:
                try:
                    import threading

                    from auth.gdpr import purge_wake_data_on_consent_withdrawal

                    threading.Thread(
                        target=purge_wake_data_on_consent_withdrawal,
                        name="wake-data-consent-purge",
                        daemon=True,
                    ).start()
                except Exception as exc:
                    logger.warning("Failed to purge wake data on consent withdrawal: %s", exc)

            if old_llm_vals:
                new_llm_vals = {k: settings_mgr.get(k) for k in old_llm_vals}
                if new_llm_vals != old_llm_vals:
                    try:
                        from services.llm.provider_router import get_active_router

                        router = get_active_router()
                        if router is not None:
                            router.reinitialize()
                            logger.info("LLM router reinitialized after settings change")
                        else:
                            logger.debug("No active LLM router to reinitialize")
                    except Exception as exc:
                        logger.warning("Failed to reinitialize LLM router: %s", exc)

            if old_ha_vals:
                new_ha_vals = {k: settings_mgr.get(k, "") for k in old_ha_vals}
                if new_ha_vals != old_ha_vals:
                    try:
                        from services.smart_home.home_assistant import reset_home_assistant_cache

                        reset_home_assistant_cache()
                        logger.info("Home Assistant client cache invalidated after settings change")
                    except Exception as exc:
                        logger.warning("Failed to invalidate Home Assistant client cache: %s", exc)

            if old_stt_vals or old_audio_device_vals:
                try:
                    _apply_audio_stt_settings_to_app_config(settings_mgr)
                except Exception as exc:
                    logger.warning("Failed to apply audio/STT settings to AppConfig: %s", exc)

            if old_stt_vals:
                new_stt_vals = {k: settings_mgr.get(k, "") for k in old_stt_vals}
                if new_stt_vals != old_stt_vals:
                    _restart_transcriber_after_settings_change(raw_request.app)

            if old_audio_device_vals:
                new_audio_device_vals = {k: settings_mgr.get(k, "") for k in old_audio_device_vals}
                if new_audio_device_vals != old_audio_device_vals:
                    _validate_audio_devices_after_settings_change()
                    _restart_wake_detector_after_settings_change(raw_request.app)

            if "telemetry_opt_in" in validated:
                try:
                    from config.settings import settings as app_config

                    # Legacy compat mirror only — NOT load-bearing for gating.
                    # SettingsManager's telemetry_opt_in (settings_mgr, above) is
                    # the sole authority TelemetryReporter reads
                    # (telemetry.reporter._is_telemetry_enabled_by_user, #2175);
                    # this AppConfig field is never re-synced at process startup,
                    # so nothing downstream may treat it as the user's opt-in.
                    app_config.telemetry_enabled = bool(settings_mgr.get("telemetry_opt_in", False))
                except Exception as exc:
                    logger.warning("Failed to mirror telemetry_opt_in to AppConfig: %s", exc)

            new_autostart = validated.get("start_on_boot")
            if new_autostart is not None and new_autostart != old_autostart:
                # Same shared implementation the voice path runs. It never
                # raises; a refusal comes back as a FAILED result, which is
                # logged rather than surfaced because the REST contract here
                # reports the persisted settings, not the apply outcome.
                autostart_effect = apply_setting_effect(
                    "start_on_boot",
                    new_autostart,
                    settings_mgr=settings_mgr,
                    previous=old_autostart,
                )
                if autostart_effect.outcome is EffectOutcome.FAILED:
                    logger.warning("Failed to update auto-start setting: %s", autostart_effect.detail)

            new_voice_mode = validated.get("voice_mode")
            new_mic_muted = validated.get("mic_muted")
            voice_mode_changed = new_voice_mode is not None and new_voice_mode != old_voice_mode
            mic_muted_changed = new_mic_muted is not None and bool(new_mic_muted) != old_mic_muted
            if voice_mode_changed or mic_muted_changed:
                # Shared with the voice write path. The apply reads the
                # already-persisted mic_muted itself, so passing the effective
                # voice_mode is enough to cover a mic_muted-only change too.
                effective_voice_mode = new_voice_mode if new_voice_mode is not None else old_voice_mode
                wake_effect = apply_setting_effect(
                    "voice_mode",
                    effective_voice_mode,
                    settings_mgr=settings_mgr,
                    previous=old_voice_mode,
                )
                if wake_effect.outcome is EffectOutcome.FAILED:
                    logger.warning(
                        "Failed to sync wake detector on voice_mode/mic_muted change: %s",
                        wake_effect.detail,
                    )

            if old_wake_model_vals:
                new_wake_model_vals = {key: settings_mgr.get(key) for key in old_wake_model_vals}
                if new_wake_model_vals != old_wake_model_vals:
                    try:
                        from config.wake_config import get_violawake_model_path
                        from voice.wake_detector.facade import WakeDetectorFacade

                        active_model = str(settings_mgr.get("wake_word_active_model", "") or "")
                        fallback_model = str(settings_mgr.get("wake_word_model", "") or "")
                        model_path = active_model or fallback_model
                        if not model_path:
                            default_path = get_violawake_model_path()
                            model_path = str(default_path) if default_path is not None else ""
                        if model_path:
                            reload_result = WakeDetectorFacade.reload_model(model_path)
                            logger.info("Wake detector reload after settings change: %s", reload_result)
                    except Exception as exc:
                        logger.warning("Failed to reload wake detector after settings change: %s", exc)

            hub = getattr(raw_request.app.state, "event_hub", None)
            payload = _settings_response_payload(_effective_settings_snapshot(raw_request))
            if hub:
                broadcast_user_id = _get_broadcast_user_id(raw_request)
                await hub.broadcast(
                    "settings_changed",
                    payload,
                    user_id=broadcast_user_id,
                    force=True,
                )

            logger.info(success_log_message)
            return JSONResponse(
                content=success_response(payload),
            )
        except Exception:
            logger.exception(failure_log_message)
            # A failure envelope on a 200 is a false success: the frontend's
            # apiFetch only raises on a non-2xx status, so a settings write that
            # blew up here came back to the caller looking saved. First run is
            # where that bites hardest — the autonomy step writes the agent's
            # own permission tier, and the user was advanced past it believing
            # their choice had been stored. The sibling /reset handler below
            # already answers 500 for exactly this case.
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "settings_error",
                    "Something went wrong. Try again.",
                    data={"ok": False, "error": "Something went wrong. Try again."},
                ),
            )

    @router.get("")
    async def get_settings(raw_request: Request):
        """Get all current settings."""
        try:
            return JSONResponse(
                content=success_response(_settings_response_payload(_effective_settings_snapshot(raw_request))),
            )
        except Exception:
            logger.exception("Failed to get settings")
            return JSONResponse(
                content={
                    "ok": False,
                    "error": {
                        "code": "settings_error",
                        "message": "Something went wrong. Try again.",
                    },
                    "data": None,
                },
            )

    @router.post("")
    async def update_settings(request: SettingsUpdateRequest, raw_request: Request):
        """Update settings."""
        return await _handle_settings_mutation(
            request=request,
            raw_request=raw_request,
            request_log_message="Updating settings: %s",
            mutation_kind_label="POST",
            success_log_message="Settings updated successfully",
            failure_log_message="Failed to update settings",
        )

    @router.patch("")
    async def patch_settings(request: SettingsUpdateRequest, raw_request: Request):
        """Partially update settings (PATCH semantics).

        Accepts a JSON body containing only the keys to update - keys absent
        from the body are left unchanged.  The update logic is identical to
        POST; PATCH simply makes the partial-update contract explicit for
        callers that follow REST conventions.
        """
        return await _handle_settings_mutation(
            request=request,
            raw_request=raw_request,
            request_log_message="Patching settings (partial update): %s",
            mutation_kind_label="PATCH",
            success_log_message="Settings patched successfully",
            failure_log_message="Failed to patch settings",
        )

    @router.post("/reset")
    async def reset_settings(raw_request: Request):
        """Reset all settings to defaults."""
        try:
            try:
                user_id = _get_request_user_id(raw_request)
            except HTTPException:
                user_id = None

            # Reset is the second desktop entry point to this consent (#4789). It
            # puts consent_cloud_sync back to its default of OFF and repaints the
            # switch that way, so it has to withdraw on the account for the same
            # reason the toggle does — otherwise "Reset all settings to defaults"
            # shows cloud sync off while the account keeps consenting and keeps the
            # synced copy. Only mirror when the desktop was actually SHOWING
            # consent: reset then means exactly "the user turned the visible switch
            # off", and a reset never reaches further than what it displays.
            snapshot = _effective_settings_snapshot(raw_request)
            consent_api = _cloud_sync_consent_api()
            if consent_api is None:
                granted_consents = [key for key in _consent_keys_present(snapshot) if bool(snapshot.get(key))]
                if granted_consents:
                    return _cloud_sync_consent_unavailable_response(granted_consents)
            elif bool(snapshot.get(consent_api.key, False)):
                spoke_refusal = _consent_change_denied_for_spoke(raw_request)
                if spoke_refusal is not None:
                    return spoke_refusal
                consent_mirror = await consent_api.mirror_cloud_sync_consent(False)
                if not consent_mirror.ok:
                    logger.warning(
                        "Refused settings reset: cloud-sync consent was not withdrawn (%s)",
                        consent_mirror.outcome.value,
                    )
                    return _cloud_sync_consent_refusal_response(consent_api, consent_mirror)

            if user_id:
                success = settings_mgr.reset_user_settings(user_id, save_immediately=True)
            else:
                success = settings_mgr.reset(save_immediately=True)

            if not success:
                from core.exceptions import ConfigurationError

                raise ConfigurationError("Failed to reset settings")

            # Broadcast settings reset to all connected WebSocket clients
            hub = getattr(raw_request.app.state, "event_hub", None)
            payload = _settings_response_payload(_effective_settings_snapshot(raw_request))
            if hub:
                broadcast_user_id = _get_broadcast_user_id(raw_request)
                await hub.broadcast(
                    "settings_changed",
                    payload,
                    user_id=broadcast_user_id,
                    force=True,
                )

            return JSONResponse(content=success_response(payload))
        except Exception:
            logger.exception("Failed to reset settings")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "settings_reset_failed",
                    "Something went wrong. Try again.",
                    data={"ok": False, "error": "Something went wrong. Try again."},
                ),
            )

    @router.get("/devices")
    async def get_audio_devices():
        """Get available audio input/output devices."""
        try:
            # This GET handler was one half of the original crash race (it
            # fingerprinted the output device via Pa_Terminate while the wake
            # detector ran Pa_Initialize). open_portaudio()/terminate_portaudio()
            # serialize both under the process-wide lock.
            # Lazy import: audio_core is a desktop-only package NOT bundled in the
            # cloud image. A module-level import here crashed every settings_api
            # route in the cloud (ModuleNotFoundError at request time) — same bug
            # class as the 2026-06-22 tiktoken incident. This GET runs desktop-only,
            # so importing inside it keeps the cloud import-clean.
            from audio_core.portaudio_guard import open_portaudio, terminate_portaudio

            p = open_portaudio()
            input_devices: list[dict[str, Any]] = []
            output_devices: list[dict[str, Any]] = []

            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)

                    max_input_channels = int(info.get("maxInputChannels", 0) or 0)
                    max_output_channels = int(info.get("maxOutputChannels", 0) or 0)
                    device_name = str(info.get("name", f"Device {i}"))

                    if max_input_channels > 0:
                        input_devices.append({"index": i, "name": device_name, "channels": max_input_channels})

                    if max_output_channels > 0:
                        output_devices.append({"index": i, "name": device_name, "channels": max_output_channels})
                except Exception as exc:
                    logger.debug("Skipping device %d due to error: %s", i, exc)
                    continue

            terminate_portaudio(p)

            return JSONResponse(
                content=success_response(
                    {
                        "ok": True,
                        "input_devices": input_devices,
                        "output_devices": output_devices,
                        "error": None,
                    }
                )
            )

        except Exception:
            logger.exception("Failed to get audio devices")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "audio_devices_error",
                    "Something went wrong. Try again.",
                    data={
                        "ok": False,
                        "input_devices": [],
                        "output_devices": [],
                        "error": "Something went wrong. Try again.",
                    },
                ),
            )

    @router.post("/refresh-devices")
    async def refresh_devices():
        """Refresh audio device list (re-enumerates hardware)."""
        return await get_audio_devices()

    # Playlist endpoints
    playlist_mgr = get_playlist_manager()

    def _playlist_response_payload(*, ok: bool, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": ok,
            "playlists": playlist_mgr.list_playlists(),
            "default_playlist": playlist_mgr.get_default_playlist() or "",
        }
        payload.update(extra)
        return payload

    def _settings_route_failure(code: str, message: str, *, status_code: int = 500) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content=failure_response(
                code,
                message,
                data={"ok": False, "error": message},
            ),
        )

    @router.get("/playlists")
    async def get_playlists():
        """Get all saved playlists."""
        try:
            return JSONResponse(content=success_response(_playlist_response_payload(ok=True)))
        except Exception:
            logger.exception("Failed to get playlists")
            return _settings_route_failure("playlist_error", "Something went wrong. Try again.")

    @router.post("/playlists")
    async def add_playlist(name: str, url: str, shuffle: bool = True):
        """Add a YouTube playlist with optional shuffle setting."""
        try:
            success = playlist_mgr.add_playlist(name, url, shuffle)
            return JSONResponse(content=success_response(_playlist_response_payload(ok=success)))
        except Exception:
            logger.exception("Failed to add playlist")
            return _settings_route_failure("playlist_error", "Something went wrong. Try again.")

    @router.delete("/playlists/{name}")
    async def delete_playlist(name: str):
        """Delete a playlist."""
        try:
            success = playlist_mgr.remove_playlist(name)
            return JSONResponse(content=success_response(_playlist_response_payload(ok=success)))
        except Exception:
            logger.exception("Failed to delete playlist '%s'", name)
            return _settings_route_failure("playlist_error", "Something went wrong. Try again.")

    @router.post("/playlists/sync")
    async def sync_playlists(request: Request):
        """Sync playlists from YouTube account."""
        user_id = _get_request_user_id(request)
        try:
            synced_count = playlist_mgr.sync_from_provider(user_id=user_id)
            return JSONResponse(
                content=success_response(_playlist_response_payload(ok=True, synced_count=synced_count))
            )
        except Exception as e:
            from music.providers.errors import MusicProviderUnavailableError

            if isinstance(e, MusicProviderUnavailableError):
                logger.warning("Provider unavailable during playlist sync: %s", e)
                return JSONResponse(
                    status_code=400,
                    content=failure_response(
                        "provider_unavailable",
                        "Music provider is unavailable",
                        data={
                            "ok": False,
                            "error": "Music provider is unavailable",
                            "provider_unavailable": True,
                        },
                    ),
                )
            logger.exception("Failed to sync playlists")
            return _settings_route_failure("playlist_sync_error", "Something went wrong. Try again.")

    @router.post("/playlists/rename")
    async def rename_playlist(old_name: str, new_name: str):
        """Rename a playlist (local name only)."""
        try:
            success = playlist_mgr.rename_playlist(old_name, new_name)
            return JSONResponse(content=success_response(_playlist_response_payload(ok=success)))
        except Exception:
            logger.exception("Failed to rename playlist")
            return _settings_route_failure("playlist_error", "Something went wrong. Try again.")

    @router.post("/playlists/star")
    async def star_playlist(name: str, starred: bool = True):
        """Star or unstar a playlist."""
        try:
            success = playlist_mgr.star_playlist(name, starred)
            return JSONResponse(content=success_response(_playlist_response_payload(ok=success)))
        except Exception:
            logger.exception("Failed to star playlist")
            return _settings_route_failure("playlist_error", "Something went wrong. Try again.")

    @router.post("/playlists/default")
    async def set_default_playlist(name: str):
        """Set a playlist as the default."""
        try:
            success = playlist_mgr.set_default_playlist(name)
            return JSONResponse(content=success_response(_playlist_response_payload(ok=success)))
        except Exception:
            logger.exception("Failed to set default playlist")
            return _settings_route_failure("playlist_error", "Something went wrong. Try again.")

    @router.post("/playlists/update-url")
    async def update_playlist_url(name: str, url: str):
        """Update a playlist's URL."""
        try:
            success = playlist_mgr.update_playlist_url(name, url)
            return JSONResponse(content=success_response(_playlist_response_payload(ok=success)))
        except Exception:
            logger.exception("Failed to update playlist URL")
            return _settings_route_failure("playlist_error", "Something went wrong. Try again.")

    @router.get("/detect-local-ai")
    async def detect_local_ai(request: Request):
        """Detect running local AI servers for the authenticated user context."""
        try:
            user_id = _get_request_user_id(request)
            logger.debug("Detecting local AI servers for user=%s", user_id)
            servers = await detect_local_ai_servers()
            return JSONResponse(content=success_response({"servers": servers}))
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to detect local AI servers")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "local_ai_detection_error",
                    "Failed to detect local AI servers",
                    data={"servers": []},
                ),
            )

    @router.post("/validate-llm")
    async def validate_llm(request: Request):
        """Validate an LLM provider connection with the given credentials.

        Accepts JSON body with: provider, api_key, base_url, model.
        Creates a temporary provider instance and calls test_connection().
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": {
                        "code": "invalid_json",
                        "message": "Request body must be valid JSON",
                    },
                    "data": None,
                },
            )

        provider_type = body.get("provider", "")
        api_key = body.get("api_key", "")
        base_url = body.get("base_url", "")
        model = body.get("model", "")
        if provider_type == "openai_compatible" and not api_key:
            base_url_text = base_url if isinstance(base_url, str) else ""
            if "localhost" in base_url_text.lower() or "127.0.0.1" in base_url_text.lower():
                api_key = (
                    "local-ai"  # pragma: allowlist secret -- placeholder for localhost LLM servers that accept any key
                )

        if not provider_type:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": {
                        "code": "missing_provider",
                        "message": "Provider type is required",
                    },
                    "data": None,
                },
            )

        try:
            from services.llm.factory import LLMProviderFactory
            from services.llm.providers.base import LLMConfig

            config = LLMConfig(
                provider=provider_type,
                api_key=api_key if api_key else None,
                model=model if model else "",
                base_url=base_url if base_url else None,
            )

            provider = LLMProviderFactory.create_provider(config)
            result = await provider.test_connection()

            return JSONResponse(
                content=success_response(
                    {
                        "valid": result.success,
                        "message": result.message,
                        "latency_ms": result.latency_ms,
                        "model": model or config.model,
                        "error_code": result.error_code,
                    }
                ),
            )
        except ValueError as exc:
            logger.warning("LLM validation rejected config for %s: %s", provider_type, exc)
            return JSONResponse(
                content=success_response(
                    {
                        "valid": False,
                        "message": str(exc),
                    }
                ),
            )
        except Exception:
            logger.exception("LLM validation failed")
            return JSONResponse(
                content=success_response(
                    {
                        "valid": False,
                        "message": "Connection failed. Check your settings and try again.",
                    }
                ),
            )

    @router.post("/test-messaging")
    async def test_messaging(request: Request):
        """Test connectivity for a messaging channel.

        Accepts JSON body with:
            channel: str  — one of telegram, slack
            config: dict  — channel-specific credentials

        Returns success_response with connected (bool) and message (str).
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "invalid_json",
                    "Request body must be valid JSON",
                ),
            )

        channel = body.get("channel", "")
        config = body.get("config", {})

        if not channel:
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "missing_channel",
                    "Channel name is required",
                ),
            )

        try:
            import asyncio

            result = await asyncio.to_thread(_test_messaging_channel, channel, config)
            return JSONResponse(
                content=success_response(result),
            )
        except Exception:
            logger.exception("Messaging test failed for channel %s", channel)
            return JSONResponse(
                content=success_response({"connected": False, "message": "Connection test failed. Try again."}),
            )

    return router


def _resolve_messaging_credential(config: dict[str, Any], key: str) -> str:
    """Resolve a messaging credential from the test config dict.

    When the Settings UI saves a token, SettingsManager stores only a placeholder
    in responses. If the frontend sends a placeholder back, fetch the real value
    from the credential store instead of testing the mask.

    Also strips whitespace to handle accidental copy-paste issues.
    """
    credential_placeholders = {"***ENCRYPTED***", _SECRET_REDACTION}

    value = (config.get(key) or "").strip()
    if not value or value in credential_placeholders:
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            stored = sm.get(key)
            if stored and isinstance(stored, str) and stored not in credential_placeholders:
                value = stored.strip()
        except Exception:
            logger.debug("settings_manager unavailable for settings_api key fallback")
    return value


def _test_messaging_channel(channel: str, config: dict[str, Any]) -> dict[str, Any]:
    """Run a lightweight connectivity check for a messaging channel.

    Executes synchronously — callers should wrap in asyncio.to_thread().
    """
    import httpx

    if channel == "telegram":
        token = _resolve_messaging_credential(config, "telegram_bot_token")
        if not token:
            return {"connected": False, "message": "Bot token is required"}
        try:
            resp = httpx.get(
                "https://api.telegram.org/bot%s/getMe" % token,
                timeout=TIMEOUT_LONG,
            )
            data = resp.json()
            if data.get("ok"):
                bot_name = data.get("result", {}).get("username", "unknown")
                return {
                    "connected": True,
                    "message": "Connected as @%s" % bot_name,
                }
            return {
                "connected": False,
                "message": data.get("description", "Invalid token"),
            }
        except Exception as exc:
            return {"connected": False, "message": "Request failed: %s" % exc}

    if channel == "slack":
        token = _resolve_messaging_credential(config, "slack_bot_token")
        if not token:
            return {"connected": False, "message": "Bot token is required"}
        try:
            resp = httpx.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": "Bearer %s" % token},
                timeout=TIMEOUT_LONG,
            )
            data = resp.json()
            if data.get("ok"):
                bot_name = data.get("user", "unknown")
                team = data.get("team", "")
                msg = "Connected as %s" % bot_name
                if team:
                    msg += " in %s" % team
                return {"connected": True, "message": msg}
            return {
                "connected": False,
                "message": data.get("error", "Invalid token"),
            }
        except Exception as exc:
            return {"connected": False, "message": "Request failed: %s" % exc}

    # Signal / WhatsApp test-connection handlers removed 2026-04-17
    # (product decision — WhatsApp/Signal no longer supported).

    return {"connected": False, "message": "Unknown channel: %s" % channel}
