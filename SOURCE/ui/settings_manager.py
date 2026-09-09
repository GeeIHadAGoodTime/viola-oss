"""
Settings Manager for Viola

Handles loading, saving, and managing user settings.
Supports both dev mode (JSON file) and production mode (app data folder).

REFACTORED: Now uses config/defaults.py for default values (single source of truth)

ENHANCED: Now uses SecureSettingsManager for encryption of sensitive data.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, TypeVar, overload

from config import env
from core.asyncio_safe import SyncBridgeLoopError, run_async_synchronously
from core.constants import DEFAULT_API_PORT
from core.logging_config import get_logger
from core.product import AiSource, coerce_ai_source
from core.subprocess_utils import run_silent
from ui.settings_schema import (
    BIPA_DELETE_KEYS as SETTINGS_BIPA_DELETE_KEYS,
    CLOUD_COMPAT_USER_SETTING_KEYS as SETTINGS_CLOUD_COMPAT_USER_SETTING_KEYS,
    CLOUD_CONSENT_KEYS as SETTINGS_CLOUD_CONSENT_KEYS,
    CLOUD_NON_SETTINGS_KEYS as SETTINGS_CLOUD_NON_SETTINGS_KEYS,
    CREDENTIAL_RELOCATE_KEYS as SETTINGS_CREDENTIAL_RELOCATE_KEYS,
    INFRASTRUCTURE_KEYS as SETTINGS_INFRASTRUCTURE_KEYS,
    SETTING_KEY_ALIASES as SETTINGS_KEY_ALIASES,
    SETTINGS_CLOUD_TIER3_ONLY,
    SETTINGS_CLOUD_USER_SCOPED,
    STALE_DELETE_KEYS as SETTINGS_STALE_DELETE_KEYS,
    USER_SETTING_KEYS as SETTINGS_USER_SETTING_KEYS,
)

logger = get_logger(__name__)

# Import defaults from single source of truth
from config import defaults

_ENCRYPTED_PLACEHOLDER = "***ENCRYPTED***"
_REDACTED_SECRET_PLACEHOLDER = "••••••"  # nosec B105
_SECRET_PLACEHOLDERS = frozenset(
    {
        _ENCRYPTED_PLACEHOLDER,
        _REDACTED_SECRET_PLACEHOLDER,
    }
)
_LEGACY_OPENAI_MODELS = frozenset({"gpt-4.1-nano", "gpt-4.1-mini"})
_MISSING = object()
_USER_SETTINGS_MIGRATION_MARKER = "__settings_migrated__"
SettingKeyTier = Literal["device", "user", "consent", "credential", "stale", "bipa", "unknown"]
DEFAULT_SETTING_STRING_VALUE_LIMIT_BYTES = 4 * 1024


class UserSettingsLoadError(RuntimeError):
    """A per-user settings load genuinely failed (bug #2786).

    Distinct from BOTH:
      - a legitimate empty-on-first-run blob (no rows for this user yet --
        ``_load_user_settings_blob`` returns ``{}`` directly, no exception), and
      - the EXPECTED on-serving-loop bridge skip (``SyncBridgeLoopError`` --
        the dispatch worker thread already has/gets the real value; see #709/#992
        and the ``settings-sync-bridge-loop-degrade`` gate).

    Raised for a real, unexpected failure to load the blob (a DB error, a
    malformed/missing repo). Callers that only READ a setting may catch this and
    fall back to the caller-supplied default (still logging loudly) since a stale
    read is recoverable on the next call. Callers that WRITE settings (merge new
    keys into the loaded blob, then save) MUST NOT catch-and-continue: saving on
    top of a load failure persists an incomplete blob and silently wipes every
    other setting the user had -- see ``_write_user_settings_values`` and
    ``_persist_cached_user_settings``, which fail closed (return ``False``,
    no save) on this error instead.
    """


class SecureSettingsUnavailableError(RuntimeError):
    """Refused to persist a secret setting because encryption is unavailable (#2785).

    Raised by ``_apply_global_setting_updates`` when a key classified as
    encrypted (``ENCRYPTED_FIELDS``, e.g. ``llm_api_key``/``telegram_bot_token``)
    is set to a real value but ``self._secure_manager`` is ``None`` -- either
    because the optional ``cryptography`` dependency could not be imported
    (``SECURE_SETTINGS_AVAILABLE=False``) or ``SecureSettingsManager.__init__``
    itself raised. Canon: secrets NEVER belong in ``settings.json``. Silently
    falling through to ``self.settings[key] = value`` would write the secret in
    PLAINTEXT to disk on the next ``save()`` -- this fails closed instead of
    doing that. Callers (``set()``/``update()``) do not catch this; it
    propagates to the HTTP layer the same way the #2783 cloud-isolation
    ``ValueError`` does, and is logged with the missing-dependency reason so
    it's actionable.
    """


def _atomic_write_json(path: Path, payload: object, *, indent: int = 2) -> None:
    """Write JSON to ``path`` atomically: temp file in the same dir, fsync, then
    ``os.replace``. A crash/kill at any point before the final ``os.replace``
    leaves the original ``path`` untouched (POSIX/Windows rename is atomic) -
    matches the house pattern in services/persistence/blob_store.py and
    intent/agent_executor.py._atomic_write_json_restricted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


SETTING_STRING_VALUE_LIMIT_OVERRIDES_BYTES: dict[str, int] = {
    "custom_instructions": 8 * 1024,
}
_CLASSIFICATION_WARNED_KEYS: set[str] = set()
_CLASSIFICATION_WARN_LOCK = threading.Lock()
LEGACY_USER_SETTINGS_SAFE_IMPORT_KEYS = frozenset(
    {
        "accent_color",
        "ptt_enabled",
        "ptt_hotkey",
        "ptt_hotkey_display",
        "ptt_hotkey_type",
        "theme",
        "time_display_format",
        "voice_mode",
        "wake_sensitivity",
    }
)
LEGACY_USER_SETTINGS_SCRUB_KEEP_KEYS = frozenset(
    {
        _USER_SETTINGS_MIGRATION_MARKER,
        *LEGACY_USER_SETTINGS_SAFE_IMPORT_KEYS,
    }
)

_GLOBAL_SETTINGS_KEYS = frozenset(
    {
        "_settings_last_saved",
        "_settings_version",
        "admin_token",
        "api_host",
        "api_port",
        "app_surface",
        "auth_enabled",
        "base_url",
        "browser_provider_enabled",
        "debug_auth_token",
        "debug_routes_enabled",
        "deployment_mode",
        "dev_mode",
        "disable_device_discovery",
        "early_access_updates",
        "jwt_secret",
        "jwt_secret_previous",
        "log_level",
        "security_auth_enabled",
        "sentry_dsn",
        "sentry_enabled",
        "sentry_environment",
        "sentry_require_consent",
        "sentry_traces_sample_rate",
        # Per-install marketing self-report ("How did you hear about Viola?").
        # Install-scoped like telemetry_install_id: the opt-in first_run
        # funnel ping reads it from the telemetry scheduler thread, which has
        # no user context — a per-user row would be invisible to it. One user
        # per install makes install scope the correct semantic.
        "attribution_self_report",
        "telemetry_install_id",
        # Anonymized diagnostic-minimum consent (crash + bug report). Install
        # scope for the SAME reason as telemetry_install_id above: the crash
        # handler / diagnostic dispatch runs on a background thread with no user
        # context, so a per-user row would be invisible to it — and an invisible
        # opt-out means a send after the user opted out (the telemetry-consent
        # dead-letter class). One user per install makes install scope correct.
        "diagnostics_disclosure_shown",
        "diagnostics_baseline_opted_out",
        "consent_diagnostics_identifiable",
        "wake_enabled",
        "wake_engine",
        "wake_word_engine",
    }
)

# System keys that CANNOT be written through user-facing settings APIs.
# These are managed exclusively by the billing service or server-side config.
# Prevents plan escalation (billing_plan_id, user_plan, and the legacy
# plan_limits mirror), privilege escalation (admin_token, dev_mode), deployment
# tampering (deployment_mode), and auth bypass (auth_enabled,
# security_auth_enabled).
_SYSTEM_KEY_PREFIXES = frozenset(
    {
        "billing_plan_id",
        "user_plan",
        "app_surface",
        "deployment_mode",
        "dev_mode",
        "admin_token",
        "auth_enabled",
        "plan_limits",
        "security_auth_enabled",
        # Policy gate: disabling this short-circuits the paid-action account
        # check (core/account_gate.paid_action_login_required). Must be set
        # via env/AppConfig or the internal set_system_value() path, never
        # via the public /v1/settings or /api/v1/cloud/settings/{key} routes.
        "require_account_for_paid_actions",
    }
)

# Secret-key patterns.  Settings keys matching one of these MUST flow through
# .env / the encrypted credential store, NEVER through the user-facing HTTP
# settings API. The HTTP layer (ui/settings_api.py) consults
# is_user_facing_secret_key() and returns 403 for matching writes. Internal
# code paths (encrypted-store rehydration, env-var fallback) bypass the HTTP
# layer and are unaffected, so boot-time key loading keeps working.
_USER_FACING_SECRET_SUFFIXES: tuple[str, ...] = (
    "_secret",
    "_secret_key",
    "_password",
    "_private_key",
    "_oauth_token",
    "_webhook_secret",
)

# Exact secret key names that must never flow through /settings even though
# their suffix isn't unambiguously secret (e.g. openai_api_key is the legacy
# managed-key field; llm_api_key remains user-settable for BYOK).
_USER_FACING_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "debug_auth_token",
        "jwt_secret",
        "jwt_secret_previous",
        "viola_security_token_secret",
        "viola_security_api_key",
    }
)

# Keys that LOOK secret (suffix would match) but are explicitly allowed through
# the HTTP API: BYOK keys, messaging bot tokens, and credentials users
# legitimately paste into the UI.
_USER_FACING_SECRET_ALLOWLIST: frozenset[str] = frozenset(
    {
        "llm_api_key",
        "telegram_bot_token",
        "slack_bot_token",
        "slack_app_token",
        "icloud_password",
        "spotify_refresh_token",
    }
)


def is_user_facing_secret_key(key: str) -> bool:
    """Return True if *key* is a credential that must never flow through the
    user-facing HTTP settings API.

    The HTTP handler (ui/settings_api.py) returns 403 for matching writes.
    Internal code paths (encrypted-store rehydration etc.) bypass the HTTP
    layer and are unaffected.
    """
    if not isinstance(key, str):
        return False
    if key in _USER_FACING_SECRET_ALLOWLIST:
        return False
    if key in _USER_FACING_SECRET_NAMES:
        return True
    lower = key.lower()
    return any(lower.endswith(suffix) for suffix in _USER_FACING_SECRET_SUFFIXES)


def _is_system_key(key: str) -> bool:
    """Return True if *key* is a protected system key that must not be
    written through user-facing settings APIs."""
    for prefix in _SYSTEM_KEY_PREFIXES:
        if key == prefix or key.startswith(prefix):
            return True
    return False


def _uses_global_settings_only(user_id: str | None) -> bool:
    """Return True for identities that should never hit the auth DB.

    Desktop/local runtime identities do not correspond to real auth-database
    user rows. Device/session pseudo-identities likewise have no auth user
    record.
    """
    if not user_id:
        return False
    from core.user_context import is_desktop_local_principal

    return is_desktop_local_principal(user_id)


def _is_cloud_deployment() -> bool:
    """Return True when this process is the shared multi-tenant cloud backend.

    On the cloud the process-global ``settings.json`` blob is shared across every
    tenant and is what the unauthenticated-default path reads, so a user-scoped
    read or write with no resolved user must never touch it (that is a
    cross-tenant leak). On desktop the single logged-in install legitimately uses
    the global blob as its store, so the guard does not apply there. Fails closed
    to desktop only on a genuine lookup error -- never silently treats cloud as
    desktop.
    """
    try:
        from config.settings import settings as _app_config

        deployment_mode = str(
            getattr(_app_config, "deployment_mode", None) or getattr(_app_config, "app_surface", "desktop")
        ).lower()
        return deployment_mode == "cloud"
    except Exception:  # noqa: BLE001, RUF100 - config probe fails safe to desktop mode
        logger.debug("Deployment mode lookup failed; treating as non-cloud")
        return False


def normalize_setting_key(key: str) -> str:
    return SETTINGS_KEY_ALIASES.get(key, key)


def setting_string_value_limit_bytes(key: str) -> int:
    canonical_key = normalize_setting_key(key)
    return SETTING_STRING_VALUE_LIMIT_OVERRIDES_BYTES.get(
        canonical_key,
        DEFAULT_SETTING_STRING_VALUE_LIMIT_BYTES,
    )


def classify_setting_key(key: str, *, strict: bool = False) -> SettingKeyTier:
    """Return the D7 storage tier for a settings key.

    Unknown keys intentionally fall back to the device tier so new keys do not
    silently become synced user data. A warning is emitted once per key for
    audit and follow-up classification. Strict callers raise instead so cloud
    entrypoints cannot quietly accept new setting names before tier review.
    """
    canonical_key = normalize_setting_key(key)
    if canonical_key in SETTINGS_CREDENTIAL_RELOCATE_KEYS:
        return "credential"
    if canonical_key in SETTINGS_STALE_DELETE_KEYS:
        return "stale"
    if canonical_key in SETTINGS_BIPA_DELETE_KEYS:
        return "bipa"
    if canonical_key in SETTINGS_CLOUD_CONSENT_KEYS:
        return "consent"
    if canonical_key in SETTINGS_USER_SETTING_KEYS:
        return "user"
    if canonical_key in SETTINGS_CLOUD_COMPAT_USER_SETTING_KEYS:
        return "user"
    if canonical_key in SETTINGS_INFRASTRUCTURE_KEYS:
        return "device"

    if strict:
        raise KeyError("Unclassified settings key: %s" % canonical_key)

    with _CLASSIFICATION_WARN_LOCK:
        should_log = canonical_key not in _CLASSIFICATION_WARNED_KEYS
        if should_log:
            _CLASSIFICATION_WARNED_KEYS.add(canonical_key)
    if should_log:
        logger.warning("Unclassified settings key %s; defaulting to device tier", canonical_key)
    return "device"


def is_cloud_user_scoped_setting_key(key: str, *, strict: bool = False) -> bool:
    """Return True only for keys the public cloud settings route may sync."""
    canonical_key = normalize_setting_key(key.strip())
    if not canonical_key:
        return False
    if canonical_key in SETTINGS_CLOUD_TIER3_ONLY or canonical_key in SETTINGS_CLOUD_CONSENT_KEYS:
        return False
    classify_setting_key(canonical_key, strict=strict)
    return canonical_key in SETTINGS_CLOUD_USER_SCOPED


class _GuardedSettingsDict(dict):
    """Dict subclass that blocks direct writes to system keys.

    The ``set()`` / ``set_bulk()`` methods on SettingsManager already enforce
    the blocklist, but code that bypasses those methods and writes to
    ``self.settings[key]`` directly would skip the check.  This wrapper
    catches that gap.

    Defense in depth: every standard dict mutation method must route
    through ``self[k] = v`` so the same guard fires. ``dict.__setitem__``
    remains a privileged internal escape hatch for ``set_system_value()``.
    """

    def __setitem__(self, key, value):
        if isinstance(key, str) and _is_system_key(key):
            logger.warning("Blocked direct dict write to system key: %s", key)
            return
        super().__setitem__(key, value)

    def update(self, __m=(), **kwargs):
        if hasattr(__m, "keys"):
            for k in __m:
                self[k] = __m[k]
        else:
            for k, v in __m:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def setdefault(self, key, default=None):
        if isinstance(key, str) and _is_system_key(key) and key not in self:
            logger.warning("Blocked setdefault write to system key: %s", key)
            return default
        return super().setdefault(key, default)

    def __ior__(self, other):
        if hasattr(other, "keys"):
            for k in other:
                self[k] = other[k]
        else:
            for k, v in other:
                self[k] = v
        return self

    def pop(self, key, *args):
        if isinstance(key, str) and _is_system_key(key):
            logger.warning("Blocked pop of system key: %s", key)
            if args:
                return args[0]
            raise KeyError(key)
        return super().pop(key, *args)

    def popitem(self):
        # popitem() returns an arbitrary (key, value) pair. To preserve
        # system-key protection we iterate in reverse and refuse the pop
        # if the candidate happens to be a system key.
        for k in reversed(list(self)):
            if isinstance(k, str) and _is_system_key(k):
                logger.warning("Skipped popitem candidate (system key): %s", k)
                continue
            value = self[k]
            del self[k]
            return (k, value)
        raise KeyError("dictionary is empty (only system keys remain)")

    def __delitem__(self, key):
        if isinstance(key, str) and _is_system_key(key):
            logger.warning("Blocked delete of system key: %s", key)
            return
        super().__delitem__(key)

    def clear(self):
        # Preserve system keys (they are server-managed, not user state).
        preserved = {k: self[k] for k in list(self) if isinstance(k, str) and _is_system_key(k)}
        super().clear()
        for k, v in preserved.items():
            dict.__setitem__(self, k, v)


class _SecretStore(Protocol):
    def set_secret(self, key: str, value: str) -> None: ...

    def get_secret(self, key: str) -> str | None: ...

    def delete_secret(self, key: str) -> bool: ...


class _SettingsCredentialVault(Protocol):
    def set_credential(self, user_id: str, key: str, value: str) -> None: ...

    def get_credential(self, user_id: str, key: str) -> str | None: ...

    def delete_credential(self, user_id: str, key: str) -> bool: ...


TSetting = TypeVar("TSetting")


class _UserSettingsCache:
    """In-memory LRU cache for per-user settings loaded from the auth DB.

    Avoids a SQLite round-trip on every ``SettingsManager.get()`` call for
    user-scoped keys like ``weather_location`` or ``delivery_address``.

    Design:
        - Lazy: a user's settings blob is loaded on first access (one DB query),
          then served from memory for all subsequent reads.
        - Invalidated on writes: ``set_user_setting()`` updates the cache inline.
        - TTL-based staleness: entries older than ``_TTL`` seconds are re-fetched.
        - LRU eviction: once ``_MAX_ENTRIES`` is reached, the least-recently-used
          entry is evicted so disconnected users don't accumulate forever.
    """

    _TTL: float = 300.0  # 5 minutes -- balances freshness vs. DB load
    _MAX_ENTRIES: int = 500  # Hard cap -- prevents unbounded growth

    def __init__(self) -> None:
        # Ordered dict: user_id -> (settings_blob, loaded_at_monotonic)
        # Insertion/access order is maintained for LRU eviction.
        from collections import OrderedDict

        self._cache: OrderedDict[str, tuple[dict[str, object], float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, user_id: str) -> dict[str, object] | None:
        """Return the cached settings blob, or None if not cached / stale."""
        with self._lock:
            entry = self._cache.get(user_id)
            if entry is None:
                return None
            blob, loaded_at = entry
            if (time.monotonic() - loaded_at) > self._TTL:
                # Stale -- remove and signal miss
                del self._cache[user_id]
                return None
            # Move to end (most-recently-used)
            self._cache.move_to_end(user_id)
            return blob

    def put(self, user_id: str, blob: dict[str, object]) -> None:
        """Store a settings blob for a user, evicting LRU if needed."""
        with self._lock:
            if user_id in self._cache:
                del self._cache[user_id]
            self._cache[user_id] = (blob, time.monotonic())
            # Evict LRU entries if over capacity
            while len(self._cache) > self._MAX_ENTRIES:
                self._cache.popitem(last=False)

    def invalidate(self, user_id: str) -> None:
        """Remove a user's entry (called after a write)."""
        with self._lock:
            self._cache.pop(user_id, None)

    def update_key(self, user_id: str, key: str, value: object) -> None:
        """Update a single key in a cached blob without a full reload.

        If the user isn't cached, this is a no-op (next read will load fresh).
        """
        with self._lock:
            entry = self._cache.get(user_id)
            if entry is None:
                return
            blob, _loaded_at = entry
            blob[key] = value
            # Refresh timestamp so the updated blob stays live
            self._cache[user_id] = (blob, time.monotonic())
            self._cache.move_to_end(user_id)


# Try to import secure settings (may not be available if cryptography not installed)
_SecureSettingsManagerType: type | None

try:
    from utils.enhancements.secrets import (
        SecureSettingsManager as _SecureSettingsManagerType,
    )

    SECURE_SETTINGS_AVAILABLE = True
except ImportError:
    _SecureSettingsManagerType = None
    SECURE_SETTINGS_AVAILABLE = False
    # Fail CLOSED (#2785): secret settings writes are REFUSED (not written in
    # plaintext) while this is unavailable -- see SecureSettingsUnavailableError
    # and _apply_global_setting_updates.
    logger.warning(
        "SecureSettingsManager not available - secret setting writes (llm_api_key, "
        "telegram_bot_token, etc.) will be refused until this is resolved, rather "
        "than stored in plaintext"
    )

SecureSettingsManager = _SecureSettingsManagerType

# Import singleton pattern
try:
    from utils.singleton import create_singleton_getter
except ImportError:
    # Fallback for testing environments
    T = TypeVar("T")

    def create_singleton_getter(key: str, factory: Callable[[], T]) -> Callable[[], T]:
        _cache: dict[str, T] = {}

        def getter() -> T:
            if key not in _cache:
                _cache[key] = factory()
            return _cache[key]

        return getter


class SettingsManager:
    """
    Manages user settings with persistence and optional encryption.

    Sensitive settings (API keys, tokens, passwords) are automatically encrypted
    using SecureSettingsManager if available.
    """

    # Fields that should be encrypted
    ENCRYPTED_FIELDS = [
        "openai_api_key",
        "gpt_api_key",
        "api_key",
        "llm_api_key",  # New provider-agnostic key
        "token",
        "password",
        "secret",
        "credential",
        "private_key",
        "access_key",
    ]

    # Settings whose names contain ENCRYPTED_FIELDS substrings but are NOT secrets.
    # Without this exclusion, _is_encrypted_field() falsely matches them
    # (e.g. "show_api_key_warning" contains "api_key"), corrupting their values
    # to "***ENCRYPTED***" and breaking validation on save.
    _ENCRYPTED_FIELD_EXCLUSIONS = frozenset(
        {
            "show_api_key_warning",
        }
    )

    # Settings schema version for migrations
    SETTINGS_VERSION = 10
    REMOVED_LEGACY_SETTING_KEYS = frozenset(
        {
            "allow_explicit_" "content",
            "audio_" "quality",
            "consent_cloud_" "llm",
            "crossfade_" "duration",
            "crossfade_" "enabled",
            "default_" "volume",
            "browser_cdp_" "port",
            "discord_" "bot_token",
            "discord_" "enabled",
            "enable_" "multiroom",
            "import_browser_" "source",
            "long_form_" "delivery_channel",
            "matrix_" "access_token",
            "matrix_" "enabled",
            "matrix_" "homeserver",
            "matrix_" "password",
            "matrix_" "user_id",
            "normalize_" "volume",
            "use_visible_" "browser",
            *SETTINGS_STALE_DELETE_KEYS,
            *SETTINGS_BIPA_DELETE_KEYS,
        }
    )
    # Back-compat aliases for renamed settings. Writes, reads, import, and
    # per-user migrations resolve these to a single stored field.
    SETTING_KEY_ALIASES: dict[str, str] = dict(SETTINGS_KEY_ALIASES)
    SETTING_KEY_READ_ALIASES: dict[str, str] = {
        "agent_autonomy": "capability_tier",
    }
    # D7 settings tiers. Unknown keys default to device in classify_setting_key()
    # and should be added to one of these explicit sets before release.
    INFRASTRUCTURE_KEYS = SETTINGS_INFRASTRUCTURE_KEYS
    USER_SETTING_KEYS = SETTINGS_USER_SETTING_KEYS
    CLOUD_COMPAT_USER_SETTING_KEYS = SETTINGS_CLOUD_COMPAT_USER_SETTING_KEYS
    CLOUD_CONSENT_KEYS = SETTINGS_CLOUD_CONSENT_KEYS
    CREDENTIAL_RELOCATE_KEYS = SETTINGS_CREDENTIAL_RELOCATE_KEYS
    STALE_DELETE_KEYS = SETTINGS_STALE_DELETE_KEYS
    BIPA_DELETE_KEYS = SETTINGS_BIPA_DELETE_KEYS
    CLOUD_NON_SETTINGS_KEYS = SETTINGS_CLOUD_NON_SETTINGS_KEYS
    CLOUD_USER_SCOPED_KEYS = SETTINGS_CLOUD_USER_SCOPED
    CLOUD_TIER3_ONLY_KEYS = SETTINGS_CLOUD_TIER3_ONLY

    DEFAULT_SETTINGS = {
        # Voice Mode
        "voice_mode": defaults.DEFAULT_VOICE_MODE,
        "wake_word_engine": "violawake",  # ViolaWake is the only supported engine
        "wake_word_active_model": "",  # Active ONNX model path, empty = default
        "wake_word_model": "",  # Path to custom model, empty = default
        # Wake Word Selection
        "use_custom_wake_word": False,  # Toggle: default vs custom
        "custom_wake_word_name": "",  # Name of custom wake word
        # Push-to-Talk
        "ptt_enabled": True,
        "ptt_hotkey": defaults.DEFAULT_PTT_HOTKEY,
        "ptt_hotkey_type": "keyboard",  # "keyboard" or "mouse"
        "ptt_hotkey_display": "Space",  # Human-readable name
        # Mute (mic mute / pause wake word) — hard-gates the wake/STT
        # pipeline via WakeDetectorFacade.sync_to_state regardless of
        # voice_mode. Mirrors the PTT hotkey shape above.
        "mute_hotkey": defaults.DEFAULT_MUTE_HOTKEY,
        "mute_hotkey_display": defaults.DEFAULT_MUTE_HOTKEY_DISPLAY,
        "mic_muted": defaults.MIC_MUTED_DEFAULT,
        # Audio
        "input_device": "",  # Empty = default
        "output_device": "",  # Empty = default
        "microphone_volume": 100,
        "speaker_volume": 80,
        # Speech-to-Text
        "stt_engine": "whisper_local",  # "whisper_local", "whisper_api"
        "whisper_model": defaults.DEFAULT_WHISPER_MODEL,
        "whisper_device": defaults.DEFAULT_WHISPER_DEVICE,
        "whisper_language": "auto",  # "auto", "en", "es", etc.
        # Text-to-Speech
        "tts_enabled": defaults.TTS_ENABLED_DEFAULT,
        "tts_voice": "default",
        "tts_rate": defaults.TTS_RATE_DEFAULT,
        "tts_volume": defaults.TTS_VOLUME_DEFAULT,
        # TTS naturalness controls (stradivari) — see voice/synthesis/
        # All have working AppConfig defaults; entries here make them
        # round-trip through settings.json and the /api/settings PATCH path.
        "tts_post_fx_enabled": True,
        "tts_loudness_target_lufs": -16.0,
        "tts_voice_blend": [["af_river", 0.5], ["af_alloy", 0.5]],
        "tts_speed_jitter_pct": 0.02,
        "tts_intersentence_gap_ms": 180,
        "tts_pronunciation_overrides": {"Viola": "vee oh luh"},
        "tts_acronym_dict_enabled": True,
        "tts_brand_dict_enabled": True,
        "tts_prosody_hints_enabled": True,
        "tts_opener_cache_enabled": True,
        "tts_opener_cache_variants": 5,
        # Response Modality Policy
        "speak_all_replies": defaults.SPEAK_ALL_REPLIES_DEFAULT,
        "voice_muted": defaults.VOICE_MUTED_DEFAULT,
        "quiet_hours_enabled": True,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "quiet_hours_timezone": "auto",
        # Music
        "default_music_volume": defaults.DEFAULT_VOLUME,
        "autoplay_enabled": defaults.AUTOPLAY_ENABLED_DEFAULT,
        "allow_explicit": True,
        "ai_autoplay_enabled": defaults.AI_AUTOPLAY_ENABLED_DEFAULT,
        "autoplay_min_queue": defaults.AUTOPLAY_MIN_QUEUE_DEFAULT,
        "active_music_provider_id": None,  # Selected playback source; connection state is provider-owned.
        "local_music_folder": "",  # Path to local music folder for LocalMusicProvider
        # Playlists (stored separately in playlists.json)
        # Format: {"workout": "https://youtube.com/playlist?list=...", ...}
        # UI
        "theme": defaults.DEFAULT_THEME,
        "accent_color": defaults.DEFAULT_ACCENT_COLOR,
        "locale": defaults.DEFAULT_LOCALE,
        "show_notifications": defaults.DEFAULT_SHOW_NOTIFICATIONS,
        # Periodic "a newer version is available" check (offered-update, per the
        # offered-vs-forced auto-update policy). Defaults ON so the toggle
        # preserves the historical always-on behaviour; the user can opt out in
        # Settings > Updates. The non-disableable min_supported safety floor is
        # separate (utils.update_checker.schedule_min_supported_gate).
        "auto_update_check_enabled": True,
        # User has explicitly acknowledged the danger-mode confirmation
        # dialog (long-running tool execution without prompt-for-approval).
        # Default False = user has NOT seen / accepted; gating UI keeps the
        # extra confirm step until they tick the box once. Migrated from
        # legacy ``bypass_permissions_accepted`` in v6->v7.
        "dangerous_mode_acknowledged": False,
        # Open the remote-control surface (web UI / REST) automatically
        # at app start. Default False keeps remote control opt-in.
        # Migrated from legacy ``repl_bridge_enabled`` in v6->v7.
        "remote_control_at_startup": False,
        "minimize_to_tray": True,
        "start_on_boot": False,
        "wake_sensitivity": defaults.DEFAULT_WAKE_SENSITIVITY,
        "weather_location": "",  # Empty = auto-detect from IP
        # Pre-event calendar reminders (services/calendar/reminders.py). Fires
        # a spoken + push notification ahead of each upcoming calendar event
        # via the existing one-shot scheduler; scoped per-user like every
        # other setting here.
        "calendar_reminders_enabled": True,
        "calendar_reminder_lead_minutes": 10,
        # iPhone onboarding completion. A default is REQUIRED here, not just
        # an entry in ui/settings_schema.py's USER_SETTING_KEYS: the cloud
        # write path runs services/sync_surfaces/user_preferences.
        # _validate_user_preference_value, which validates the value against
        # DEFAULT_SETTINGS and rejects any key it does not know. Allowlisting
        # the key alone gets it past the route's 400 and then fails in the
        # storage adapter instead — a different error at a later layer, not a
        # fix. The default is False so a user who has never onboarded on a
        # phone reads "not onboarded", which is the truthful answer.
        "ios_onboarding_completed": False,
        "time_display_format": "auto",  # auto | 12h | 24h
        "show_api_key_warning": True,  # Show warning about missing API key
        # Smart-home discovery — opt-in mDNS + port scan for hubs on the LAN
        # (Home Assistant, Hubitat, Philips Hue, MQTT, Sonos, HomeKit bridge,
        # SmartThings).  Off by default — user clicks "Discover" in
        # Settings > Smart Home, or says "Viola, find my smart devices".
        "network_discovery_enabled": False,
        "home_assistant_url": "",
        "home_assistant_token": "",
        # Weekly-review service (services/meta_analysis/weekly_review.py) —
        # opt-in LLM analysis of user patterns, memory, and bug tickets.
        # When enabled, the daemon starts the service at boot and runs
        # one analysis a week; the user can view the latest summary in
        # Settings > AI & Agents.
        "weekly_review_enabled": False,
        # Phase 5 Intent Market — third-party intent plugin platform.
        # Both this toggle AND VIOLA_ENABLE_INTENT_MARKET=1 must be set
        # for /v1/market/* routes to accept traffic. Off by default.
        # See docs/integrator/INTENT_MARKET.md for the integrator spec.
        "intent_market_enabled": False,
        # Knowledge folder storage backend.
        # "vault" = AES-256-GCM append-only blob (default, best encryption-at-rest)
        # "folder" = plaintext files per item under data_dir/knowledge/<user>/
        #           — readable in Explorer/Finder, syncable via OS file sync,
        #           but storage-at-rest is only protected by OS account isolation.
        "knowledge_storage_mode": defaults.DEFAULT_KNOWLEDGE_STORAGE_MODE,
        "features": {
            "tray_icon": {
                "enabled": defaults.TRAY_ICON_ENABLED_DEFAULT,
                "activation": defaults.TRAY_ICON_ACTIVATION_DEFAULT,
            },
            "shortcuts": {
                "enabled": defaults.SHORTCUTS_ENABLED_DEFAULT,
                "activation": defaults.SHORTCUTS_ACTIVATION_DEFAULT,
            },
            "debug_tools": {
                "enabled": defaults.DEBUG_TOOLS_ENABLED_DEFAULT,
                "activation": defaults.DEBUG_TOOLS_ACTIVATION_DEFAULT,
            },
        },
        # Advanced
        "developer_mode": False,
        "log_level": "INFO",  # "DEBUG", "INFO", "WARNING", "ERROR"
        "api_port": DEFAULT_API_PORT,
        "telemetry_opt_in": False,
        "telemetry_install_id": "",
        # Anonymized diagnostic minimum (crash + bug-report diagnostics).
        # diagnostics_disclosure_shown: the first-run/post-update disclosure card
        #   has been shown once (arms the crash baseline; until then crash
        #   payloads queue locally). Default False.
        # diagnostics_baseline_opted_out: the user turned the anonymized baseline
        #   OFF (opt-out model; default False = participating).
        # consent_diagnostics_identifiable: the separate OPT-IN that unlocks the
        #   identifiable EXTRA (contact, verbatim body, stable install id,
        #   screenshots). Default False, revocable. Read fresh via
        #   diagnostics.diagnostic_consent — never inline. Install-scoped (in
        #   _GLOBAL_SETTINGS_KEYS) so the no-user-context crash handler reads the
        #   same value the settings panel wrote.
        "diagnostics_disclosure_shown": False,
        "diagnostics_baseline_opted_out": False,
        "consent_diagnostics_identifiable": False,
        # Optional "How did you hear about Viola?" self-report, asked once
        # during first-run onboarding. Closed enum (see _ENUM_SETTINGS in
        # ui/settings_api.py); "" = not answered / prefer not to say. Local
        # only — it never syncs to cloud; the opt-in first_run funnel ping
        # (telemetry/first_run.py) is the only thing that ever reads it out.
        "attribution_self_report": "",
        "early_access_updates": False,
        # Agent Browser
        # NOTE: this is the local-desktop agent autonomy mode, NOT a
        # subscription tier. Renamed 2026-05-11 to stop the masquerade —
        # `capability_tier` is accepted as a back-compat alias by set()
        # and read by get(); both forms resolve to the same stored value.
        "agent_enabled": True,
        "agent_autonomy": "solo",  # solo | ensemble | symphony
        "auto_propose_shortcuts": defaults.AUTO_PROPOSE_SHORTCUTS_DEFAULT,
        "browser_session_mode": "viola",  # "viola", "ephemeral"
        "session_purchase_ceiling_cents": None,  # Optional rolling 60-min merchant-purchase ceiling
        # Desktop computer use (local desktop only; enabled by Symphony tier)
        "computer_use_app_whitelist": list(defaults.COMPUTER_USE_APP_WHITELIST_DEFAULT),
        "computer_use_screenshot_format": defaults.COMPUTER_USE_SCREENSHOT_FORMAT_DEFAULT,
        "computer_use_screenshot_quality": defaults.COMPUTER_USE_SCREENSHOT_QUALITY_DEFAULT,
        "computer_use_max_chars_per_type": defaults.COMPUTER_USE_MAX_CHARS_PER_TYPE_DEFAULT,
        "computer_use_log_window_titles_enabled": defaults.COMPUTER_USE_LOG_WINDOW_TITLES_ENABLED_DEFAULT,
        # Delivery
        "delivery_address": {
            "full_text": "",
            "street": "",
            "city": "",
            "state": "",
            "zip": "",
        },
        "email_link_delivery_default": True,
        # =====================================================================
        # AI Configuration (Provider-Agnostic)
        # =====================================================================
        # Master toggle - enables if the selected AI source is available.
        "ai_enabled": True,
        # AI source: managed | codex | byok | local.
        "ai_source": defaults.DEFAULT_AI_SOURCE,
        "custom_instructions": "",
        # Provider-agnostic LLM settings
        "llm_provider": "openai",  # openai | anthropic | google | ollama | openai_compatible
        "llm_api_key": "",  # Encrypted API key for selected provider
        "llm_model": "",  # Model identifier (empty = use provider default or fail validation for custom openai_compatible)
        "llm_base_url": "",  # Custom base URL (for openai_compatible or ollama)
        # Graceful Degradation (Offline Support)
        "llm_fallback_strategy": "auto",  # "auto", "cloud_only", "local_only", "offline_only"
        "llm_prefer_local": False,  # Prefer local (Ollama) over cloud
        "reasoning_effort": defaults.DEFAULT_REASONING_EFFORT,  # Legacy single-tier effort
        "agent_reasoning_effort": defaults.DEFAULT_AGENT_REASONING_EFFORT,  # low | medium | high | xhigh
        "routing_reasoning_effort": defaults.DEFAULT_ROUTING_REASONING_EFFORT,  # none | low | medium | high | xhigh
        "phone_reasoning_effort": defaults.DEFAULT_PHONE_REASONING_EFFORT,
        # Phone-call persistence and disclosure controls.
        "record_phone_calls": defaults.PHONE_RECORD_CALLS_DEFAULT,
        "keep_phone_transcript": defaults.PHONE_KEEP_TRANSCRIPT_DEFAULT,
        "announce_ai_on_calls": defaults.PHONE_ANNOUNCE_AI_ON_CALLS_DEFAULT,
        "phone_ai_identity_enforcement": defaults.PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT,
        "call_history_retention_days": defaults.CALL_HISTORY_RETENTION_DAYS_DEFAULT,
        # Legacy alias kept for stale clients; runtime uses announce_ai_on_calls.
        "phone_call_ai_disclosure": True,
        "phone_mode": "cloud",
        # Require a real Viola account before actions that spend Viola funds
        # (managed LLM, Telnyx calls/SMS, managed recording storage).
        "require_account_for_paid_actions": defaults.REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT,
        "codex_reasoning_effort": defaults.DEFAULT_CODEX_REASONING_EFFORT,  # minimal | low | medium | high
        # =====================================================================
        # Legacy Settings (for backward compatibility / migration)
        # =====================================================================
        "enable_gpt": defaults.ENABLE_GPT_DEFAULT,  # Legacy: use ai_enabled instead
        "openai_api_key": "",  # Legacy: use llm_api_key instead
        "openai_key_source": "stored",  # Legacy: kept for migration
        "agent_model": "",  # User-facing agent model (empty = provider-aware default)
        # =====================================================================
        # Privacy Consent (opt-in required for all cloud data transmission)
        # =====================================================================
        "consent_cloud_stt": defaults.CONSENT_CLOUD_STT_DEFAULT,
        "consent_cloud_sync": defaults.CONSENT_CLOUD_SYNC_DEFAULT,
        "consent_error_reporting": defaults.CONSENT_ERROR_REPORTING_DEFAULT,
        "consent_session_replay": defaults.CONSENT_SESSION_REPLAY_DEFAULT,
        "consent_vision_clipboard": defaults.CONSENT_VISION_CLIPBOARD_DEFAULT,
        "wake_word_training_opt_in": False,  # Help improve wake-word accuracy by sharing voice samples; off by default.
        "wake_data_contribute": False,  # Opt-in: contribute anonymized wake word clips
        # =====================================================================
        # Audio Retention
        # =====================================================================
        "contributor_mode_enabled": False,  # Wake word sample contributor mode
        "wake_audio_logging_enabled": defaults.WAKE_AUDIO_LOGGING_ENABLED_DEFAULT,
        "wake_audio_retention_hours": defaults.WAKE_AUDIO_RETENTION_HOURS_DEFAULT,
        # =====================================================================
        # Companion Device Pairing (desktop <-> cloud account)
        # =====================================================================
        "companion_enabled": defaults.COMPANION_ENABLED_DEFAULT,
        "companion_cloud_token": defaults.COMPANION_CLOUD_TOKEN_DEFAULT,
        "companion_device_name": defaults.COMPANION_DEVICE_NAME_DEFAULT,
        # =====================================================================
        # Messaging Channels
        # =====================================================================
        "user_phone_number": "",
        "callback_phone": "",
        "founder_phone_number": "",
        "telegram_enabled": False,
        "telegram_bot_token": "",
        "telegram_owner_chat_id": "",
        # Slack is internal-pilot only (not in Settings UI) — kept here
        # for deployments that set VIOLA_EXPERIMENTAL_CHANNELS=1.
        "slack_enabled": False,
        "slack_bot_token": "",
        "slack_app_token": "",
        # WhatsApp/Signal defaults removed 2026-04-17 — product decision.
        # Settings version for tracking migrations
        "_settings_version": SETTINGS_VERSION,
        # ISO-8601 timestamp updated on every save()
        "_settings_last_saved": "",
    }
    PLUGIN_DEFAULT_SETTING_KEYS = frozenset(
        {
            "accent_color",
            "agent_autonomy",
            "agent_enabled",
            "agent_model",
            "agent_reasoning_effort",
            "auto_propose_shortcuts",
            "browser_session_mode",
            "llm_fallback_strategy",
            "llm_model",
            "llm_prefer_local",
            "locale",
            "reasoning_effort",
            "routing_reasoning_effort",
            "show_notifications",
            "theme",
        }
    )

    def __init__(self, settings_file: Path | None = None):
        """
        Initialize settings manager.

        Args:
            settings_file: Path to settings file, or None for default
        """
        if settings_file:
            self.settings_file = Path(settings_file)
        else:
            # Default location — respect VIOLA_DATA_DIR if configured
            from config.settings import settings as _cfg

            data_dir = Path(_cfg.data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            self.settings_file = data_dir / "settings.json"

        # Initialize secure settings if available
        self._secure_manager: _SecretStore | None = None
        self._encrypted_cache_path: Path | None = None
        self._credential_vault: _SettingsCredentialVault | None = None
        if SECURE_SETTINGS_AVAILABLE and SecureSettingsManager is not None:
            try:
                secret_store_dir = self._resolve_secret_store_dir(self.settings_file)
                self._secure_manager = SecureSettingsManager(fallback_key_file=secret_store_dir / ".master_key")
                # Persist encrypted cache next to the resolved secret store.
                self._encrypted_cache_path = secret_store_dir / ".secrets.enc"
                self._secure_manager.load_from_file(self._encrypted_cache_path)
                logger.info("Secure settings manager initialized")
            except (OSError, ValueError, RuntimeError, TypeError) as e:
                logger.warning("Failed to initialize secure settings: %s", e)

        # Per-user settings cache (avoids DB round-trip on every get())
        self._user_settings_cache = _UserSettingsCache()
        self._save_lock = threading.Lock()

        # Per-user write serialization for the DB-backed per-user settings blob
        # (#2781). Guards the load -> mutate -> cache.put -> DELETE+re-INSERT
        # read-modify-write in _write_user_settings_values /
        # _persist_cached_user_settings / _persist_setting_updates so two
        # concurrent writers for the SAME user (two devices, or two overlapping
        # /v1/settings POSTs) can't each snapshot a stale blob and then
        # delete-all+re-insert, erasing the other writer's key. RLock (not
        # Lock) because _persist_setting_updates holds it across its own
        # nested calls into the other locked methods. Mirrors the get-or-create
        # per-key lock pattern in music/recents_service.py's
        # MusicRecentsService._lock_for_user -- unbounded like the settings
        # cache above, same tradeoff (one small Lock per distinct user, never
        # reclaimed).
        self._user_settings_write_locks: dict[str, threading.RLock] = {}
        self._user_settings_write_locks_guard = threading.Lock()

        # Flag for post-load migration save
        self._needs_save_after_load = False

        self.settings = _GuardedSettingsDict(self.load())

        # Save if migrations were applied during load
        if self._needs_save_after_load:
            logger.info("Saving migrated settings...")
            self.save()
            self._needs_save_after_load = False

    @staticmethod
    def _looks_like_repo_local_settings(settings_file: Path) -> bool:
        return settings_file.name == "settings.json" and settings_file.parent.name == ".viola"

    @classmethod
    def _resolve_secret_store_dir(cls, settings_file: Path) -> Path:
        secret_store_dir = settings_file.parent
        local_cache_path = secret_store_dir / ".secrets.enc"
        if local_cache_path.exists() or not cls._looks_like_repo_local_settings(settings_file):
            return secret_store_dir

        repo_root = secret_store_dir.parent
        if not (repo_root / ".git").exists():
            return secret_store_dir

        try:
            worktree_proc = run_silent(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except Exception:
            return secret_store_dir

        current_root = repo_root.resolve()
        best_score = 0
        chosen_dir: Path | None = None
        for line in worktree_proc.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            candidate_root = Path(line.removeprefix("worktree ").strip())
            try:
                resolved_root = candidate_root.resolve()
            except OSError as exc:
                logger.debug("Skipping unresolved worktree root %s: %s", candidate_root, exc)
                continue
            if resolved_root == current_root:
                continue
            candidate_secret_dir = resolved_root / ".viola"
            score = 0
            if (candidate_secret_dir / ".secrets.enc").exists():
                score += 2
            if (candidate_secret_dir / ".master_key").exists():
                score += 1
            if score > best_score:
                best_score = score
                chosen_dir = candidate_secret_dir

        if chosen_dir is None:
            return secret_store_dir

        logger.info("Using shared secure settings store from %s", chosen_dir)
        return chosen_dir

    @classmethod
    def _default_settings_base(cls) -> dict[str, object]:
        settings: dict[str, object] = cls.DEFAULT_SETTINGS.copy()
        # Fresh installations honor deployment configuration. A value saved
        # by the user is merged over this base by load(), preserving priority.
        from config.settings import settings as app_config

        settings["phone_mode"] = app_config.phone_mode
        try:
            from plugins.config_manager import load_plugin_settings_defaults

            plugin_defaults = load_plugin_settings_defaults(allowed_keys=cls.PLUGIN_DEFAULT_SETTING_KEYS)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Failed to load plugin default settings: %s", exc)
            return settings

        for key, value in plugin_defaults.items():
            if key not in cls.DEFAULT_SETTINGS:
                continue
            if _is_system_key(key) or is_user_facing_secret_key(key):
                continue
            settings[key] = value
        return settings

    def load(self) -> dict[str, object]:
        """
        Load settings from file with graceful fallback.

        Fallback chain:
        1. Try loading from primary settings file
        2. Try loading from backup file if primary corrupted
        3. Try secure storage if file loading fails
        4. Return defaults as final fallback
        """
        # Strategy 1: Try primary settings file
        if self.settings_file.exists():
            try:
                with open(self.settings_file, encoding="utf-8") as f:
                    loaded = json.load(f)

                # Merge with defaults (in case new settings were added)
                settings = self._default_settings_base()
                settings.update(loaded)

                # Migrate encrypted fields from plaintext
                if self._secure_manager:
                    migrated_any = False
                    for key in self.ENCRYPTED_FIELDS:
                        if key in loaded and loaded[key] and loaded[key] not in _SECRET_PLACEHOLDERS:
                            # Migrate plaintext to encrypted storage
                            plaintext_value = loaded[key]
                            if isinstance(plaintext_value, str) and len(plaintext_value) > 0:
                                self._secure_manager.set_secret(key, plaintext_value)
                                settings[key] = _ENCRYPTED_PLACEHOLDER
                                migrated_any = True
                                logger.info("Migrated %s to encrypted storage", key)
                    if migrated_any and self._encrypted_cache_path:
                        self._secure_manager.save_to_file(self._encrypted_cache_path)

                # Run schema migrations
                settings = self._migrate_settings(settings, loaded)

                logger.info("Settings loaded from %s", self.settings_file)
                return settings

            except json.JSONDecodeError as e:
                logger.warning("Settings file corrupted: %s, trying backup...", e)
                # Strategy 2: Try backup file
                backup_file = self.settings_file.with_suffix(".json.bak")
                if backup_file.exists():
                    try:
                        with open(backup_file, encoding="utf-8") as f:
                            loaded = json.load(f)
                        settings = self._default_settings_base()
                        settings.update(loaded)
                        settings = self._migrate_settings(settings, loaded)
                        logger.info("Settings loaded from backup file")
                        return settings
                    except Exception as e2:
                        logger.warning("Backup file also corrupted: %s", e2)
            except Exception as e:
                logger.warning("Failed to load settings file: %s", e)

        # Strategy 3: Try secure storage if file loading failed
        if self._secure_manager:
            try:
                secure_settings = {}
                # Try to load encrypted secrets
                for key in self.ENCRYPTED_FIELDS:
                    try:
                        value = self._secure_manager.get_secret(key)
                        if value:
                            secure_settings[key] = value
                    except Exception as e:
                        logger.exception(
                            "Failed to load encrypted setting '%s' from secure storage: %s",
                            key,
                            e,
                        )

                if secure_settings:
                    settings = self._default_settings_base()
                    settings.update(secure_settings)
                    logger.info("Settings loaded from secure storage (fallback)")
                    return settings
            except Exception as e:
                logger.debug("Secure storage fallback failed: %s", e)

        # Strategy 4: Return defaults (final fallback)
        logger.info("📋 Using default settings (no file or secure storage available)")
        return self._default_settings_base()

    def save(self, settings: dict[str, object] | None = None) -> bool:
        """
        Save settings to file with graceful fallback.

        Fallback chain:
        1. Try saving to primary file
        2. Try creating backup of primary before overwriting
        3. Try saving to backup file if primary fails
        4. Try secure storage if file save fails
        5. Return False only if all methods fail

        Args:
            settings: Settings dict to save, or None to save current

        Returns:
            True if successful (any method), False if all failed
        """
        with self._save_lock:
            if settings is not None:
                self.settings = (
                    _GuardedSettingsDict(settings) if not isinstance(settings, _GuardedSettingsDict) else settings
                )

            # Stamp the current time so the UI can show "Last Updated"
            self.settings, _ = self._migrate_setting_aliases(self.settings)
            self.settings, _ = self._drop_removed_legacy_settings(self.settings)
            self.settings["_settings_last_saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")

            # Strategy 1: Try saving to primary file
            try:
                # Ensure directory exists
                self.settings_file.parent.mkdir(parents=True, exist_ok=True)

                # Create backup before overwriting (if file exists)
                if self.settings_file.exists():
                    try:
                        backup_file = self.settings_file.with_suffix(".json.bak")
                        import shutil

                        shutil.copy2(self.settings_file, backup_file)
                    except Exception as e:
                        logger.debug("Could not create backup: %s", e)

                # Write settings atomically (temp file + os.replace) so a crash/kill
                # mid-write can never leave a truncated/corrupt primary settings.json.
                _atomic_write_json(self.settings_file, self.settings)

                logger.info("Settings saved to %s", self.settings_file)
                return True

            except Exception as e:
                logger.warning("Failed to save settings to primary file: %s", e)
                # Strategy 2: Try backup file
                try:
                    backup_file = self.settings_file.with_suffix(".json.bak")
                    self.settings_file.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write_json(backup_file, self.settings)
                    logger.info("Settings saved to backup file %s", backup_file)
                    return True
                except Exception as e2:
                    logger.warning("Failed to save to backup file: %s", e2)
                    # Strategy 3: Try secure storage for encrypted fields
                    if self._secure_manager:
                        try:
                            saved_count = 0
                            for key in self.ENCRYPTED_FIELDS:
                                value = self.settings.get(key)
                                if isinstance(value, str) and value and value not in _SECRET_PLACEHOLDERS:
                                    self._secure_manager.set_secret(key, value)
                                    saved_count += 1
                            if saved_count > 0:
                                if self._encrypted_cache_path:
                                    self._secure_manager.save_to_file(self._encrypted_cache_path)
                                logger.info(
                                    "Saved %s encrypted settings to secure storage (fallback)",
                                    saved_count,
                                )
                                return True
                        except Exception as e3:
                            logger.debug("Secure storage fallback failed: %s", e3)

                    # All methods failed
                    logger.error("All save methods failed: primary=%s, backup=%s", e, e2)
                    return False

    @staticmethod
    def _run_async(coro):
        return run_async_synchronously(coro)

    def _get_auth_db(self):
        from auth.database import get_auth_db

        db = get_auth_db()
        if not db._initialized:
            try:
                self._run_async(db.initialize())
            except SyncBridgeLoopError:
                # Same #709/#758 class one call site up (#992): a caller is on
                # the cloud FastAPI serving loop (not the dispatch worker
                # thread), so bridging db.initialize() here would deadlock the
                # loop on itself and run_async_synchronously refuses. This is
                # an EXPECTED serving-loop condition -- boot
                # (backend/cloud_app.py init_auth_db) already initializes the
                # auth DB before serving starts, but the pool can be torn down
                # and re-flagged uninitialized mid-serving
                # (auth/postgres_database.py PostgresAuthDatabase.close() /
                # initialize()'s cross-loop pool recreation) -- not a failure
                # worth a per-turn ERROR. Degrade: return the DB handle
                # uninitialized; every caller either already wraps
                # _get_auth_db() in its own broad except
                # (_legacy_user_settings_import_allowed) or makes its own
                # SyncBridgeLoopError-guarded _run_async call right after
                # (_load_user_settings_blob, #758), so a still-uninitialized
                # handle is always handled downstream instead of crashing here.
                logger.debug(
                    "Skipping on-serving-loop auth DB initialize() (%s); will "
                    "initialize on the dispatch worker thread instead",
                    type(db).__name__,
                )
        return db

    @classmethod
    def _is_credential_setting_key(cls, key: str) -> bool:
        return classify_setting_key(cls.canonicalize_setting_key(key)) == "credential"

    def _get_credential_vault(self) -> _SettingsCredentialVault:
        if self._credential_vault is None:
            from services.settings.credential_vault import SettingsCredentialVault

            self._credential_vault = SettingsCredentialVault(self.settings_file.parent)
        return self._credential_vault

    def _read_user_credential(self, user_id: str, key: str) -> str | None:
        try:
            return self._get_credential_vault().get_credential(user_id, key)
        except (RuntimeError, ValueError, OSError) as exc:
            logger.warning(
                "Failed to read settings credential '%s' for user %s: %s",
                key,
                user_id,
                exc,
            )
            return None

    def _write_user_credential(self, user_id: str, key: str, value: object) -> bool:
        try:
            if value:
                self._get_credential_vault().set_credential(user_id, key, str(value))
            else:
                self._get_credential_vault().delete_credential(user_id, key)
            return True
        except (RuntimeError, ValueError, OSError) as exc:
            logger.warning(
                "Failed to write settings credential '%s' for user %s: %s",
                key,
                user_id,
                exc,
            )
            return False

    def _relocate_user_credentials_from_blob(
        self,
        user_id: str,
        settings_blob: dict[str, object],
    ) -> tuple[dict[str, object], bool]:
        changed = False
        for raw_key in list(settings_blob):
            key = self.canonicalize_setting_key(raw_key)
            if not self._is_credential_setting_key(key):
                continue
            raw_value = settings_blob.pop(raw_key)
            changed = True
            if isinstance(raw_value, str):
                value = raw_value.strip()
                if not value or value in _SECRET_PLACEHOLDERS:
                    continue
            elif raw_value:
                value = str(raw_value)
            else:
                continue
            self._write_user_credential(user_id, key, value)
        return settings_blob, changed

    def _load_user_settings_blob(self, user_id: str) -> dict[str, object]:
        """Load per-user settings from the auth DB.

        Uses direct synchronous SQLite calls (no async event-loop churn)
        because the underlying connection is thread-local and synchronous.
        On the cloud (Postgres) backend, ``db.connection`` is an async
        context manager method, not a sync connection — this sync path is
        not supported there. Return empty so the caller falls back to the
        process-global default. (The PostgresAuthDatabase exposes user
        prefs via async repositories; the LLM provider construction path
        only needs ``ai_enabled`` which has a True default.)

        Return value contract (bug #2786): ``{}`` means "confirmed no rows for
        this user" (legitimate first-run empty) OR "the dispatch worker thread
        already has this loaded elsewhere" (the expected on-serving-loop
        ``SyncBridgeLoopError`` skip, #709/#992 -- not a failure). A GENUINE
        load failure (a broken repo, a DB error) raises ``UserSettingsLoadError``
        instead of returning ``{}`` -- it must never be indistinguishable from
        "this user has no settings," because a caller that merges an update into
        a load-failure ``{}`` and saves it would silently wipe every other
        setting the user had (see ``UserSettingsLoadError``'s docstring).
        """
        import json as _json

        db = self._get_auth_db()
        conn = db.connection
        if not hasattr(conn, "execute"):
            repo = getattr(db, "user_settings", None)
            get_settings = getattr(repo, "get_settings", None)
            if not callable(get_settings):
                logger.error(
                    "Postgres user-settings repo for %s has no callable get_settings() "
                    "(repo=%r) -- this is a broken/misconfigured repo, not an empty-settings "
                    "user; refusing to mask it as {} (#2786)",
                    user_id,
                    repo,
                )
                raise UserSettingsLoadError(
                    f"user_settings repo is missing a callable get_settings() for user {user_id}"
                )
            try:
                loaded = self._run_async(get_settings(user_id))
            except SyncBridgeLoopError:
                # We are on the cloud serving event loop (a route handler read a
                # per-user setting directly, not via the dispatch worker thread).
                # A sync->async bridge here would deadlock the loop on itself, so
                # ``run_async_synchronously`` refuses. This is an EXPECTED serving
                # -loop condition, not a failure: the agent dispatch path runs on
                # a worker thread (backend/intent_bridge/bridge.py asyncio.to_thread)
                # where the same load succeeds. Degrade to process-global defaults
                # here instead of logging a per-turn ERROR traceback (#709). Before
                # f68aeac9 this whole cloud branch just ``return {}``; that commit
                # added the async load (correct on the worker thread) but made the
                # on-loop case raise+ERROR every /v1/command turn.
                logger.debug(
                    "Skipping on-serving-loop Postgres user-settings load for %s; "
                    "using defaults (loaded on the dispatch worker thread instead)",
                    user_id,
                )
                return {}
            except Exception as exc:
                # A GENUINE load failure (DB error, driver exception, timeout) --
                # unlike SyncBridgeLoopError above, there is no "the worker thread
                # already has it" guarantee here. Log it (kept for GlitchTip/trace
                # visibility) AND raise, so callers can tell "load failed" apart
                # from "confirmed empty" instead of both masquerading as {} (#2786;
                # this branch used to ``return {}`` here, which is exactly the
                # silent-data-loss shape the ticket reported).
                logger.exception("Failed to load Postgres user settings for %s", user_id)
                raise UserSettingsLoadError(f"failed to load Postgres user settings for {user_id}") from exc
            if not loaded:
                return {}
            raw_blob = loaded[0]
            if not isinstance(raw_blob, dict):
                return {}
            settings_blob = dict(raw_blob)
            settings_blob, renamed_aliases = self._migrate_setting_aliases(settings_blob)
            settings_blob, normalized = self._normalize_runtime_model_settings(settings_blob)
            settings_blob, dropped_removed_legacy = self._drop_removed_legacy_settings(settings_blob)
            settings_blob, relocated_credentials = self._relocate_user_credentials_from_blob(user_id, settings_blob)
            if renamed_aliases or normalized or dropped_removed_legacy or relocated_credentials:
                save_settings = getattr(repo, "save_settings", None)
                version = loaded[1] if isinstance(loaded, tuple) and len(loaded) > 1 else 1
                if callable(save_settings):
                    try:
                        self._run_async(save_settings(user_id, _json.dumps(settings_blob), int(version or 1)))
                    except Exception:
                        logger.exception("Failed to normalize Postgres user settings for %s", user_id)
            return settings_blob
        rows = conn.execute(
            "SELECT key, value FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        if not rows:
            return self._import_legacy_user_settings(user_id)

        settings_blob: dict[str, object] = {}
        migration_marker_seen = False
        for row in rows:
            key = str(row["key"])
            if key == _USER_SETTINGS_MIGRATION_MARKER:
                migration_marker_seen = True
                continue
            raw = row["value"]
            try:
                settings_blob[key] = _json.loads(raw) if isinstance(raw, str) else raw
            except (_json.JSONDecodeError, TypeError):
                settings_blob[key] = raw

        settings_blob, renamed_aliases = self._migrate_setting_aliases(settings_blob)
        settings_blob, normalized = self._normalize_runtime_model_settings(settings_blob)
        settings_blob, dropped_removed_legacy = self._drop_removed_legacy_settings(settings_blob)
        settings_blob, relocated_credentials = self._relocate_user_credentials_from_blob(user_id, settings_blob)
        normalized = normalized or renamed_aliases or dropped_removed_legacy or relocated_credentials

        if migration_marker_seen:
            if normalized:
                self._save_user_settings_blob(user_id, settings_blob)
            return settings_blob

        if settings_blob:
            # Mark as migrated so we don't re-check next time
            try:
                now = self._iso_now()
                conn.execute(
                    "INSERT OR REPLACE INTO user_preferences (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                    (user_id, _USER_SETTINGS_MIGRATION_MARKER, "true", now),
                )
                conn.commit()
            except Exception:
                logger.debug("Failed to write migration marker for %s", user_id)
            if normalized:
                self._save_user_settings_blob(user_id, settings_blob)
            return settings_blob

        return self._import_legacy_user_settings(user_id)

    def _save_user_settings_blob(self, user_id: str, settings_blob: dict[str, object]) -> bool:
        """Persist per-user settings to the auth DB in a single transaction.

        All writes are batched — one lock acquisition, one commit — on the
        SQLite (desktop/local) backend, where ``db.connection`` is a sync
        ``sqlite3.Connection`` and ``db.transaction()`` a sync context
        manager.

        On the cloud (Postgres) backend, ``db.connection``/``db.transaction()``
        are ``@asynccontextmanager``-decorated coroutine functions
        (auth/postgres_database.py) — not sync — so this branches to the
        async ``user_settings.save_settings`` repo write instead, mirroring
        the read-path branch ``_load_user_settings_blob`` already has
        (#709/#992/#3324; the write path never got the equivalent branch and
        every cloud write here previously raised internally, was swallowed by
        the broad ``except Exception`` below, and silently returned ``False``
        without ever reaching Postgres).
        """
        import json as _json

        db = self._get_auth_db()
        settings_blob, _ = self._migrate_setting_aliases(dict(settings_blob))
        clean_blob = {
            key: value
            for key, value in settings_blob.items()
            if (
                not self._is_credential_setting_key(key)
                and (self._is_user_scoped_key(key) or key in LEGACY_USER_SETTINGS_SAFE_IMPORT_KEYS)
            )
        }

        conn = db.connection
        if not hasattr(conn, "execute"):
            repo = getattr(db, "user_settings", None)
            save_settings = getattr(repo, "save_settings", None)
            if not callable(save_settings):
                logger.error(
                    "Cannot persist user settings for %s: Postgres auth DB is "
                    "missing user_settings.save_settings; write not durable",
                    user_id,
                )
                return False
            get_settings = getattr(repo, "get_settings", None)
            try:
                next_version = 1
                if callable(get_settings):
                    existing = self._run_async(get_settings(user_id))
                    if existing:
                        next_version = int(existing[1] or 0) + 1
                self._run_async(save_settings(user_id, _json.dumps(clean_blob, ensure_ascii=False), next_version))
            except SyncBridgeLoopError:
                # Mirrors the read-path degrade one call site up, but a write
                # cannot degrade to "return defaults" without silently losing
                # the change -- fail loudly instead of the old swallow-and-
                # return-False-with-no-log behavior, so this is diagnosable
                # rather than a mystery revert. Caller is on the cloud FastAPI
                # serving loop, not the dispatch worker thread; bridging here
                # would deadlock the loop on itself.
                logger.error(
                    "Refusing to silently drop a user-settings write for %s: "
                    "caller is on the cloud serving loop, which cannot bridge "
                    "to the async Postgres write path here.",
                    user_id,
                )
                return False
            except Exception:
                logger.exception("Failed to save Postgres user settings for %s", user_id)
                return False
            return True

        now = self._iso_now()
        try:
            with db.transaction():
                conn = db.connection
                # Clear existing preferences for this user and re-insert
                conn.execute(
                    "DELETE FROM user_preferences WHERE user_id = ? AND key != ?",
                    (user_id, _USER_SETTINGS_MIGRATION_MARKER),
                )
                if clean_blob:
                    conn.executemany(
                        "INSERT INTO user_preferences (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                        [(user_id, k, _json.dumps(v, ensure_ascii=False), now) for k, v in clean_blob.items()],
                    )
                # Write migration marker
                conn.execute(
                    "INSERT OR REPLACE INTO user_preferences (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                    (user_id, _USER_SETTINGS_MIGRATION_MARKER, "true", now),
                )
        except Exception:
            logger.debug("Failed to save user settings for %s", user_id, exc_info=True)
            return False
        return True

    @staticmethod
    def _iso_now() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat()

    def _normalize_runtime_model_settings(
        self,
        settings_blob: dict[str, object],
    ) -> tuple[dict[str, object], bool]:
        """Collapse stale or redundant runtime model pins to the current canon.

        This keeps active settings in the new two-knob shape:
        - ``llm_model``: conversation / routing tier
        - ``agent_model``: optional override only when the agent tier differs

        Older settings pinned OpenAI managed/BYOK execution to retired legacy models.
        Those stale values override the newer defaults, so normalize them on
        read. Also clear redundant ``agent_model`` pins when they match the
        effective route model, because an explicit override is unnecessary.
        """
        ai_source_raw = settings_blob.get("ai_source", defaults.DEFAULT_AI_SOURCE)
        ai_source = coerce_ai_source(ai_source_raw if isinstance(ai_source_raw, str) else None)
        provider_raw = settings_blob.get("llm_provider", "openai")
        provider = provider_raw if isinstance(provider_raw, str) and provider_raw else "openai"

        changed = False
        route_default = defaults.resolve_effective_model(ai_source=ai_source.value, provider=provider, agent=False)
        agent_default = defaults.resolve_effective_model(ai_source=ai_source.value, provider=provider, agent=True)

        llm_model = settings_blob.get("llm_model")
        agent_model = settings_blob.get("agent_model")
        if provider == "openai" and ai_source in {AiSource.MANAGED, AiSource.BYOK}:
            if isinstance(llm_model, str) and llm_model in _LEGACY_OPENAI_MODELS:
                settings_blob["llm_model"] = route_default
                llm_model = route_default
                changed = True

            if isinstance(agent_model, str) and agent_model in _LEGACY_OPENAI_MODELS:
                normalized_agent = "" if agent_default == route_default else agent_default
                settings_blob["agent_model"] = normalized_agent
                agent_model = normalized_agent
                changed = True
        effective_route_model = defaults.resolve_effective_model(
            ai_source=ai_source.value,
            provider=provider,
            agent=False,
            candidates=(llm_model if isinstance(llm_model, str) else "",),
        )
        if isinstance(agent_model, str) and agent_model and agent_model == effective_route_model:
            settings_blob["agent_model"] = ""
            changed = True

        # Coerce an out-of-set whisper_model back to the default. A bad settings
        # save once wrote the theme value ("dark") into whisper_model; faster-
        # whisper then raised "Invalid model size 'dark'" on load. Settings the
        # user can corrupt must never silently break speech-to-text — fall back
        # to DEFAULT_WHISPER_MODEL instead. (L8 — Settings persistence oracle.)
        whisper_model = settings_blob.get("whisper_model")
        if whisper_model is not None and whisper_model not in defaults.VALID_WHISPER_MODELS:
            logger.warning(
                "Invalid whisper_model %r in settings; coercing to default %r",
                whisper_model,
                defaults.DEFAULT_WHISPER_MODEL,
            )
            settings_blob["whisper_model"] = defaults.DEFAULT_WHISPER_MODEL
            changed = True

        return settings_blob, changed

    @classmethod
    def _drop_removed_legacy_settings(cls, settings_blob: dict[str, object]) -> tuple[dict[str, object], bool]:
        """Remove settings keys that were intentionally retired."""
        changed = False
        for key in cls.REMOVED_LEGACY_SETTING_KEYS:
            if key in settings_blob:
                settings_blob.pop(key, None)
                changed = True
        return settings_blob, changed

    @classmethod
    def canonicalize_setting_key(cls, key: str) -> str:
        """Return the canonical storage key for a legacy or current setting."""
        return cls.SETTING_KEY_ALIASES.get(key, key)

    @classmethod
    def _read_setting_from_blob(cls, blob: dict[str, object], key: str, default: object = None) -> object:
        if key in blob:
            return blob.get(key, default)
        legacy_key = cls.SETTING_KEY_READ_ALIASES.get(key)
        if legacy_key and legacy_key in blob:
            return blob.get(legacy_key, default)
        return default

    @classmethod
    def _migrate_setting_aliases(
        cls,
        settings_blob: dict[str, object],
        loaded_blob: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], bool]:
        """Rename legacy setting keys to their canonical storage keys."""
        changed = False
        for legacy_key, canonical_key in cls.SETTING_KEY_ALIASES.items():
            if legacy_key not in settings_blob:
                continue

            if loaded_blob is not None:
                if legacy_key in loaded_blob and canonical_key not in loaded_blob:
                    settings_blob[canonical_key] = settings_blob[legacy_key]
            elif canonical_key not in settings_blob:
                settings_blob[canonical_key] = settings_blob[legacy_key]

            settings_blob.pop(legacy_key, None)
            changed = True
        return settings_blob, changed

    def _normalize_runtime_setting_updates(
        self,
        updates: dict[str, object],
        user_id: str | None,
    ) -> dict[str, object]:
        """Canonicalize runtime-setting writes before they hit storage."""
        normalized_updates = dict(updates)
        runtime_keys = frozenset(
            {
                "ai_source",
                "llm_provider",
                "llm_model",
                "agent_model",
                "reasoning_effort",
            }
        )
        current_runtime = {
            "ai_source": self.get("ai_source", defaults.DEFAULT_AI_SOURCE, user_id=user_id),
            "llm_provider": self.get("llm_provider", "openai", user_id=user_id),
            "llm_model": self.get("llm_model", "", user_id=user_id),
            "agent_model": self.get("agent_model", "", user_id=user_id),
            "reasoning_effort": self.get("reasoning_effort", defaults.DEFAULT_REASONING_EFFORT, user_id=user_id),
        }

        ai_source_raw = normalized_updates.get("ai_source")
        if isinstance(ai_source_raw, str):
            canonical_source = coerce_ai_source(ai_source_raw)
            normalized_updates["ai_source"] = canonical_source.value
            current_source_raw = current_runtime["ai_source"]
            current_source = coerce_ai_source(current_source_raw if isinstance(current_source_raw, str) else None)
            current_model_raw = current_runtime["llm_model"]
            current_model = current_model_raw if isinstance(current_model_raw, str) else ""
            incoming_model_raw = normalized_updates.get("llm_model")
            incoming_model = incoming_model_raw if isinstance(incoming_model_raw, str) else ""
            model_unchanged = incoming_model_raw is None or incoming_model == current_model
            if model_unchanged:
                if canonical_source is AiSource.CODEX:
                    if not current_model or current_model in _LEGACY_OPENAI_MODELS:
                        normalized_updates["llm_model"] = defaults.DEFAULT_CODEX_MODEL
                elif current_source is AiSource.CODEX and current_model == defaults.DEFAULT_CODEX_MODEL:
                    normalized_updates["llm_model"] = defaults.DEFAULT_GPT_MODEL

        effective_source = coerce_ai_source(
            normalized_updates.get("ai_source")
            if isinstance(normalized_updates.get("ai_source"), str)
            else current_runtime["ai_source"]
        )
        effective_provider_raw = normalized_updates.get("llm_provider", current_runtime["llm_provider"])
        effective_provider = effective_provider_raw if isinstance(effective_provider_raw, str) else "openai"
        incoming_llm_model = normalized_updates.get("llm_model")
        incoming_agent_model = normalized_updates.get("agent_model")
        if effective_provider == "openai" and effective_source in {
            AiSource.MANAGED,
            AiSource.BYOK,
        }:
            if (isinstance(incoming_llm_model, str) and incoming_llm_model in _LEGACY_OPENAI_MODELS) or (
                isinstance(incoming_agent_model, str) and incoming_agent_model in _LEGACY_OPENAI_MODELS
            ):
                normalized_updates["reasoning_effort"] = defaults.DEFAULT_REASONING_EFFORT

        runtime_updates = {key: normalized_updates[key] for key in runtime_keys if key in normalized_updates}
        if not runtime_updates:
            return normalized_updates

        merged_runtime = dict(current_runtime)
        merged_runtime.update(runtime_updates)
        merged_runtime, changed = self._normalize_runtime_model_settings(merged_runtime)
        if not changed:
            return normalized_updates

        for key in ("llm_model", "agent_model", "reasoning_effort"):
            previous_value = current_runtime.get(key)
            current_value = merged_runtime.get(key)
            if current_value != previous_value or key in runtime_updates:
                normalized_updates[key] = current_value

        return normalized_updates

    def _legacy_user_settings_import_allowed(self, user_id: str) -> bool:
        """Return True only for the legacy operator bootstrap path."""
        if not user_id:
            return False

        explicit_user_id = (env.get("VIOLA_LEGACY_SETTINGS_IMPORT_USER_ID") or "").strip()
        if explicit_user_id:
            return user_id == explicit_user_id

        if _uses_global_settings_only(user_id):
            return True

        try:
            db = self._get_auth_db()
            conn = db.connection
            if not hasattr(conn, "execute"):
                return False
            first_user_row = conn.execute(
                "SELECT id FROM users ORDER BY created_at, id LIMIT 1",
            ).fetchone()
            user_count_row = conn.execute("SELECT COUNT(*) AS user_count FROM users").fetchone()
        except Exception as exc:
            logger.debug(
                "Legacy settings import eligibility check failed for %s: %s",
                user_id,
                exc,
            )
            return False

        if first_user_row is None or user_count_row is None:
            return False

        try:
            first_user_id = str(first_user_row["id"])
        except (IndexError, KeyError, TypeError):
            first_user_id = str(first_user_row[0])

        try:
            user_count = int(user_count_row["user_count"])
        except (IndexError, KeyError, TypeError, ValueError):
            user_count = int(user_count_row[0])

        return user_count == 1 and first_user_id == user_id

    def _import_legacy_user_settings(self, user_id: str) -> dict[str, object]:
        """Migrate safe legacy settings only for the local operator bootstrap."""
        if not self._legacy_user_settings_import_allowed(user_id):
            logger.info("Skipping legacy settings import for non-operator user %s", user_id)
            self._save_user_settings_blob(user_id, {})
            return {}

        legacy_settings: dict[str, object] = {}
        for key in self.settings:
            if key not in LEGACY_USER_SETTINGS_SAFE_IMPORT_KEYS:
                continue
            if key in self.REMOVED_LEGACY_SETTING_KEYS:
                continue
            value = self._get_legacy_user_setting_value(key)
            if value is _MISSING or value in (None, "", {}):
                continue
            default_value = self.DEFAULT_SETTINGS.get(key, _MISSING)
            if default_value is not _MISSING and value == default_value:
                continue
            legacy_settings[key] = value
        self._save_user_settings_blob(user_id, legacy_settings)
        return legacy_settings

    def get_user_setting(
        self,
        user_id: str | None,
        key: str,
        default: object = None,
        *,
        on_load_error: str = "default",
    ) -> object:
        """Read a per-user setting.

        ``on_load_error`` controls what happens on a GENUINE settings-load
        failure (``UserSettingsLoadError`` from ``_load_user_settings_blob``):
          * ``"default"`` (back-compat default): swallow it and return the
            caller-supplied default for this read (a stale read is recoverable
            on the next call). This is the #2786 behavior for ordinary reads.
          * ``"raise"``: re-raise ``UserSettingsLoadError`` so a SAFETY-relevant
            caller (e.g. a payment ceiling read, #4221) can tell "unreadable"
            apart from "unset" and fail closed instead of treating a read glitch
            as "no value configured".
        """

        key = self.canonicalize_setting_key(key)
        if not user_id:
            return default
        # Desktop device/session pseudo-identities have no auth
        # DB row — fall back to global settings.json rather than triggering an
        # expensive (and failing) auth DB lookup.
        if _uses_global_settings_only(user_id):
            return self._read_setting_from_blob(self.settings, key, default)
        # Try the in-memory cache first (O(1) dict lookup)
        cached = self._user_settings_cache.get(user_id)
        if cached is not None:
            return self._read_setting_from_blob(cached, key, default)
        # Cache miss -- load from DB (one query) and cache the full blob.
        # Take the per-user write lock (#2781) around the miss: without it, a
        # read racing a concurrent write for the same user can load a
        # pre-write snapshot and, on a slow DB round-trip, ``put()`` it into
        # the cache *after* the writer already committed and warmed the
        # cache with the correct post-write blob -- clobbering the write's
        # own cache entry with stale data that a THIRD caller (e.g. the next
        # write's own read-modify-write) would then read back and persist
        # over the writer's change, silently reverting it. Double-checked:
        # re-read the cache once inside the lock in case a writer (or another
        # blocked reader) already warmed it while this call waited.
        with self._lock_for_user_settings_write(user_id):
            cached = self._user_settings_cache.get(user_id)
            if cached is not None:
                return self._read_setting_from_blob(cached, key, default)
            try:
                settings_blob = self._load_user_settings_blob(user_id)
            except UserSettingsLoadError:
                # Genuine load failure (#2786): fall back to the caller's default for
                # THIS read, but do not cache it -- caching a load-failure {} would
                # poison the shared cache for up to the 5-minute TTL, serving the
                # default to every reader (including the dispatch worker thread,
                # which could otherwise load it correctly) instead of just this one.
                if on_load_error == "raise":
                    # Safety-relevant caller (#4221): it must distinguish an
                    # unreadable setting from an unset one and fail closed, so
                    # propagate rather than masquerading the glitch as the default.
                    raise
                logger.warning(
                    "get_user_setting('%s') for user %s: settings load failed, "
                    "returning caller default instead of the user's real value (#2786)",
                    key,
                    user_id,
                )
                return default
            self._user_settings_cache.put(user_id, settings_blob)
        return self._read_setting_from_blob(settings_blob, key, default)

    def set_user_setting(
        self,
        user_id: str | None,
        key: str,
        value: object,
        save_immediately: bool = True,
    ) -> bool:
        if not user_id:
            raise ValueError("user_id is required")
        resolved_user_id = self._resolve_user_id(user_id)
        updates = self._expand_setting_updates(key, value, resolved_user_id)
        return self._set_user_settings_values(
            user_id,
            updates,
            save_immediately=save_immediately,
        )

    def set_system_value(
        self,
        key: str,
        value: object,
        *,
        user_id: str | None = None,
        save_immediately: bool = True,
    ) -> bool:
        """Persist a server-controlled setting value.

        This bypasses the user-facing blocklist and is intended only for trusted
        server-side flows like billing, auth bootstrap, or deployment runtime.
        """
        if user_id and not _uses_global_settings_only(user_id):
            return self._write_user_settings_values(
                user_id,
                {key: value},
                save_immediately=save_immediately,
            )

        # The global path writes through ``self.settings`` which is a
        # ``_GuardedSettingsDict`` that refuses system-key writes. For the
        # trusted server-side path we must skip that guard — go directly to
        # ``dict.__setitem__`` so billing/auth bootstrap can land the value.
        if _is_system_key(key):
            dict.__setitem__(self.settings, key, value)
        else:
            self._apply_global_setting_updates({key: value})
        if not save_immediately:
            return True
        return self.save()

    @overload
    def get(self, key: str) -> object: ...

    @overload
    def get(self, key: str, default: TSetting) -> TSetting: ...

    def get(
        self,
        key: str,
        default: object = None,
        user_id: str | None = None,
        *,
        on_load_error: str = "default",
    ) -> object:
        """
        Get a setting value.

        If the key is not in INFRASTRUCTURE_KEYS and an authenticated user
        is available, reads from the per-user DB. Global settings are used
        only for infrastructure keys or when no user is authenticated.

        If the key is encrypted, retrieves from secure manager.
        Otherwise, retrieves from regular settings.

        ``on_load_error`` (``"default"`` | ``"raise"``) is forwarded to the
        per-user read so a safety-relevant caller can fail closed on a genuine
        settings-load failure instead of receiving the default (#4221).
        """
        # Back-compat aliases (e.g. capability_tier -> agent_autonomy).
        # Read-side resolution so older code keeps working during migration.
        key = self.canonicalize_setting_key(key)
        resolved_user_id = self._resolve_user_id(user_id)
        if (
            resolved_user_id
            and not _uses_global_settings_only(resolved_user_id)
            and self._is_credential_setting_key(key)
        ):
            credential_value = self._read_user_credential(resolved_user_id, key)
            if credential_value:
                return credential_value
            legacy_value = self.get_user_setting(resolved_user_id, key, _MISSING, on_load_error=on_load_error)
            if legacy_value is _MISSING:
                return default
            if isinstance(legacy_value, str) and legacy_value in _SECRET_PLACEHOLDERS:
                return default
            return legacy_value
        if resolved_user_id and not _uses_global_settings_only(resolved_user_id) and self._is_user_scoped_key(key):
            return self.get_user_setting(resolved_user_id, key, default, on_load_error=on_load_error)

        # Cloud mode: if no authenticated user and key is user-scoped,
        # return the default rather than the global value to prevent
        # leaking one user's preferences to unauthenticated requests.
        if resolved_user_id is None and self._is_user_scoped_key(key) and _is_cloud_deployment():
            return self.DEFAULT_SETTINGS.get(key, default)

        # Check if this is an encrypted field
        if self._is_encrypted_field(key):
            if self._secure_manager:
                value = self._secure_manager.get_secret(key)
                if value is not None:
                    return value
            # Fallback to regular settings (for migration)
            value = self._read_setting_from_blob(self.settings, key, default)
            if isinstance(value, str) and value in _SECRET_PLACEHOLDERS:
                return default
            return value

        return self._read_setting_from_blob(self.settings, key, default)

    # Canonical tier → browser_session_mode mapping.
    # Mirrors TIER_BROWSER_MODE in SettingsModal.jsx so API-driven
    # tier changes also update the browser mode automatically.
    _TIER_MODE_MAP: dict[str, str] = {
        "solo": "ephemeral",
        "ensemble": "viola",
        "symphony": "viola",
    }

    # Bot IDs belonging to the Claude Code manager system — never valid for Viola.
    _MANAGER_BOT_IDS = frozenset({"8515233452"})

    def set(self, key: str, value: object, save_immediately: bool = True) -> bool:
        """
        Set a setting value.

        If the key is sensitive, stores in encrypted format.
        Otherwise, stores in regular settings.

        Args:
            key: Setting key
            value: Setting value
            save_immediately: Whether to save to file immediately

        Returns:
            True if successful
        """
        # Block writes to system keys (plan, auth, deployment config).
        # These are managed exclusively by the billing/auth services.
        if _is_system_key(key):
            logger.warning("Blocked write to system key via set(): %s", key)
            return False

        # Guard: reject manager bot tokens written to Viola's telegram config
        if key == "telegram_bot_token" and isinstance(value, str) and ":" in value:
            bot_id = value.split(":")[0]
            if bot_id in self._MANAGER_BOT_IDS:
                logger.critical(
                    "BLOCKED: Attempted to set Viola's telegram_bot_token to "
                    "the Claude Code MANAGER bot (ID %s). This is a "
                    "cross-contamination bug. Token was NOT saved.",
                    bot_id,
                )
                return False

        # Auto-map capability_tier → browser_session_mode
        resolved_user_id = self._resolve_user_id()
        pending_updates = self._expand_setting_updates(key, value, resolved_user_id)

        if resolved_user_id and not _uses_global_settings_only(resolved_user_id) and self._is_user_scoped_key(key):
            return self._set_user_settings_values(
                resolved_user_id,
                pending_updates,
                save_immediately=save_immediately,
            )

        # Fail closed on cloud: a user-scoped key with no resolved user must NOT
        # fall through to the process-global blob, which is shared across every
        # tenant and served to the unauthenticated-default path. Writing there
        # leaks one caller's preference into shared global state (#2783). This
        # mirrors set_user_setting()'s missing-user guard and the get()-side
        # cloud default guard above. Desktop's single logged-in install
        # legitimately uses the global blob, so the guard only applies on cloud.
        if resolved_user_id is None and self._is_user_scoped_key(key) and _is_cloud_deployment():
            raise ValueError(
                "set() refused a user-scoped key with no resolved user on cloud "
                "(would leak into the shared global settings blob); establish user "
                "context or call set_user_setting(user_id, ...): %s" % key
            )

        self._apply_global_setting_updates(pending_updates)

        if save_immediately:
            return self.save()

        return True

    @classmethod
    def _is_user_scoped_key(cls, key: str) -> bool:
        key = cls.canonicalize_setting_key(key)
        # System/billing/auth keys are NEVER user-scoped, even if they are
        # absent from INFRASTRUCTURE_KEYS. Without this denial a pre-existing
        # user_preferences row for `require_account_for_paid_actions=False`
        # (written through the old /api/v1/cloud/settings/{key} bypass before
        # commit 21a2786d closed it) would still let SettingsManager.get()
        # serve the malicious value back. Found by codex follow-up audit
        # 2026-05-11.
        if _is_system_key(key):
            return False
        if key in cls.REMOVED_LEGACY_SETTING_KEYS:
            return False
        tier = classify_setting_key(key)
        if tier in {"stale", "bipa"}:
            return False
        if key in _GLOBAL_SETTINGS_KEYS:
            return False
        # D7's "device" tier is a sync/storage classification: local-only or
        # hardware-adjacent. It is not permission to share values across
        # authenticated users on the same install.
        return key in cls.DEFAULT_SETTINGS or tier in {"user", "consent", "credential"}

    @staticmethod
    def _resolve_user_id(user_id: str | None = None) -> str | None:
        if user_id:
            return user_id
        try:
            from core.user_context import get_current_user_id

            return get_current_user_id()
        except (ImportError, LookupError):
            return None

    def _lock_for_user_settings_write(self, user_id: str) -> threading.RLock:
        """Get-or-create the per-user write-serialization lock (#2781).

        Mirrors ``MusicRecentsService._lock_for_user`` in
        ``music/recents_service.py``: a get-or-create dict of per-key locks
        guarded by one small creation lock, never reclaimed.
        """
        with self._user_settings_write_locks_guard:
            lock = self._user_settings_write_locks.get(user_id)
            if lock is None:
                lock = threading.RLock()
                self._user_settings_write_locks[user_id] = lock
            return lock

    def _get_legacy_user_setting_value(self, key: str) -> object:
        if self._is_encrypted_field(key) and self._secure_manager:
            try:
                value = self._secure_manager.get_secret(key)
            except Exception as e:
                logger.debug("Failed to read encrypted legacy setting '%s': %s", key, e)
            else:
                if value is not None:
                    return value

        value = self.settings.get(key, _MISSING)
        if isinstance(value, str) and value in _SECRET_PLACEHOLDERS:
            return _MISSING
        return value

    # Back-compat attr kept for older in-repo callers; new code should use
    # SETTING_KEY_ALIASES/canonicalize_setting_key().
    _AGENT_AUTONOMY_ALIASES: dict[str, str] = SETTING_KEY_ALIASES

    def _expand_setting_updates(
        self,
        key: str,
        value: object,
        user_id: str | None,
    ) -> dict[str, object]:
        canonical_key = self.canonicalize_setting_key(key)
        if canonical_key in self.REMOVED_LEGACY_SETTING_KEYS:
            return {}
        updates = {canonical_key: value}

        if canonical_key == "agent_autonomy" and value in self._TIER_MODE_MAP:
            updates["browser_session_mode"] = self._TIER_MODE_MAP[value]

        return self._normalize_runtime_setting_updates(updates, user_id)

    def _set_user_settings_values(
        self,
        user_id: str,
        updates: dict[str, object],
        *,
        save_immediately: bool,
    ) -> bool:
        if _uses_global_settings_only(user_id):
            self._apply_global_setting_updates(updates)
            if not save_immediately:
                return True
            return self.save()

        for update_key in updates:
            if _is_system_key(update_key):
                logger.warning("Blocked write to system key via set_user_setting: %s", update_key)
                return False

        return self._write_user_settings_values(
            user_id,
            updates,
            save_immediately=save_immediately,
        )

    def _write_user_settings_values(
        self,
        user_id: str,
        updates: dict[str, object],
        *,
        save_immediately: bool,
    ) -> bool:
        """Write user-scoped settings without applying the public blocklist.

        The whole load -> mutate -> cache.put -> DELETE+re-INSERT sequence
        runs under the per-user write lock (#2781) so two concurrent writers
        for the SAME user (two devices, or two overlapping ``/v1/settings``
        POSTs) can't each snapshot a stale blob and then delete-all+re-insert,
        silently erasing the other writer's key. The lock is re-acquired
        fresh on each call (not cached by the caller) so a second writer that
        blocks here always re-reads the cache/DB *after* the first writer's
        commit, rather than working from a pre-lock snapshot.
        """
        with self._lock_for_user_settings_write(user_id):
            cached = self._user_settings_cache.get(user_id)
            if cached is not None:
                settings_blob = cached
            else:
                try:
                    settings_blob = self._load_user_settings_blob(user_id)
                except UserSettingsLoadError:
                    # Fail CLOSED (#2786): this is a merge-then-save path -- if the
                    # existing blob failed to load, merging these updates into {} and
                    # saving would silently wipe every other setting the user has.
                    # Refuse the write instead of masking the failure as "the user
                    # only had these settings." The caller (set_user_setting /
                    # update()) gets a plain False, same as any other save failure.
                    logger.error(
                        "Refusing to write user settings for %s: existing settings "
                        "blob failed to load, so merging %s in would overwrite/drop "
                        "the user's other settings (#2786)",
                        user_id,
                        sorted(updates.keys()),
                    )
                    return False

            for update_key, update_value in updates.items():
                if update_key in self.REMOVED_LEGACY_SETTING_KEYS:
                    continue
                if self._is_credential_setting_key(update_key):
                    if isinstance(update_value, str) and update_value in _SECRET_PLACEHOLDERS:
                        logger.debug(
                            "Ignoring redacted placeholder write for credential setting '%s'",
                            update_key,
                        )
                    else:
                        self._write_user_credential(user_id, update_key, update_value)
                    settings_blob.pop(update_key, None)
                    continue
                if (
                    self._is_encrypted_field(update_key)
                    and isinstance(update_value, str)
                    and update_value in _SECRET_PLACEHOLDERS
                ):
                    logger.debug(
                        "Ignoring redacted placeholder write for encrypted setting '%s'",
                        update_key,
                    )
                    continue
                settings_blob[update_key] = update_value

            self._user_settings_cache.put(user_id, settings_blob)
            if not save_immediately:
                return True
            return self._save_user_settings_blob(user_id, settings_blob)

    def _persist_setting_updates(
        self,
        updates: dict[str, object],
        *,
        resolved_user_id: str | None,
        save_immediately: bool,
    ) -> bool:
        """Persist already-normalized updates to the correct backing store.

        NOTE: this was previously duplicated verbatim in this class (dead
        code -- the later definition always shadowed the earlier one); the
        duplicate was removed while fixing #2781, no behavior change.
        """
        user_updates: dict[str, object] = {}
        global_updates: dict[str, object] = {}
        use_user_store = bool(resolved_user_id) and not _uses_global_settings_only(resolved_user_id)

        # Fail closed on cloud (#2783): the bulk sibling of set(). With no
        # resolved user, use_user_store is False, so every user-scoped key below
        # would route to global_updates and land in the process-global blob that
        # every tenant shares -- the same cross-tenant leak set() guards. Refuse
        # loudly instead. Desktop's single-user global fallback is untouched
        # (the guard is cloud-only).
        if resolved_user_id is None and _is_cloud_deployment():
            leaked_user_scoped = [k for k in updates if self._is_user_scoped_key(k)]
            if leaked_user_scoped:
                raise ValueError(
                    "update() refused user-scoped keys with no resolved user on "
                    "cloud (would leak into the shared global settings blob); "
                    "establish user context or use the per-user store: %s" % sorted(leaked_user_scoped)
                )

        for key, value in updates.items():
            if key in self.REMOVED_LEGACY_SETTING_KEYS:
                continue
            if _is_system_key(key):
                logger.warning("Blocked write to system key via update(): %s", key)
                return False

            if key == "telegram_bot_token" and isinstance(value, str) and ":" in value:
                bot_id = value.split(":")[0]
                if bot_id in self._MANAGER_BOT_IDS:
                    logger.critical(
                        "BLOCKED: Attempted to set Viola's telegram_bot_token to "
                        "the Claude Code MANAGER bot (ID %s). This is a "
                        "cross-contamination bug. Token was NOT saved.",
                        bot_id,
                    )
                    return False

            if use_user_store and self._is_user_scoped_key(key):
                user_updates[key] = value
            else:
                global_updates[key] = value

        # Hold the per-user write lock (#2781) across BOTH the mutate-cache
        # step (_set_user_settings_values) and the persist-to-DB step
        # (_persist_cached_user_settings) below, not just each individually --
        # otherwise a second concurrent update() call for the same user could
        # still interleave between the two steps and lose a key. RLock lets
        # the nested calls into those (also-locking) methods re-enter safely.
        user_write_lock = (
            self._lock_for_user_settings_write(resolved_user_id) if user_updates and resolved_user_id else None
        )
        with user_write_lock if user_write_lock is not None else contextlib.nullcontext():
            if user_updates:
                if not resolved_user_id:
                    return False
                if not self._set_user_settings_values(resolved_user_id, user_updates, save_immediately=False):
                    return False

            if global_updates:
                self._apply_global_setting_updates(global_updates)

            if not save_immediately:
                return True

            results: list[bool] = []
            if user_updates and resolved_user_id:
                results.append(self._persist_cached_user_settings(resolved_user_id))
            if global_updates:
                results.append(self.save())
            return all(results) if results else True

    def _persist_cached_user_settings(self, user_id: str) -> bool:
        if _uses_global_settings_only(user_id):
            return self.save()
        with self._lock_for_user_settings_write(user_id):
            cached = self._user_settings_cache.get(user_id)
            if cached is not None:
                return self._save_user_settings_blob(user_id, cached)

            # Normally unreachable: the caller (_persist_setting_updates) already
            # populated the cache via _write_user_settings_values just before this
            # runs. This is a defensive fallback for a cache eviction race (LRU
            # capacity, TTL). If the reload here fails, fail CLOSED (#2786) --
            # saving a fresh/partial reload would overwrite the user's real
            # settings with whatever this incomplete load produced.
            try:
                settings_blob = self._load_user_settings_blob(user_id)
            except UserSettingsLoadError:
                logger.error(
                    "Refusing to persist user settings for %s: cache was evicted and "
                    "the reload failed -- skipping save instead of writing a "
                    "possibly-incomplete blob over the user's real settings (#2786)",
                    user_id,
                )
                return False
            self._user_settings_cache.put(user_id, settings_blob)
            return self._save_user_settings_blob(user_id, settings_blob)

    def _apply_global_setting_updates(self, updates: dict[str, object]) -> None:
        for update_key, update_value in updates.items():
            if update_key in self.REMOVED_LEGACY_SETTING_KEYS:
                continue
            if self._is_encrypted_field(update_key):
                if isinstance(update_value, str) and update_value in _SECRET_PLACEHOLDERS:
                    logger.debug(
                        "Ignoring redacted placeholder write for encrypted setting '%s'",
                        update_key,
                    )
                    continue
                if self._secure_manager is None:
                    if not update_value:
                        # Clearing/deleting a secret that was never encrypted-stored
                        # (secure storage unavailable): there is nothing sensitive
                        # to persist, so just drop it from the plaintext blob.
                        self.settings.pop(update_key, None)
                        continue
                    # Fail CLOSED (#2785): secure settings storage is unavailable
                    # (missing 'cryptography' dependency, or SecureSettingsManager
                    # init failed -- see the warning logged at import time above).
                    # Falling through to `self.settings[update_key] = update_value`
                    # here would write this secret in PLAINTEXT to settings.json on
                    # the next save() -- canon says secrets NEVER belong there.
                    # Refuse the write instead of masking the failure.
                    logger.error(
                        "Refusing to persist secret setting '%s' in plaintext: "
                        "secure settings storage is unavailable. Install the "
                        "'cryptography' package (pip install cryptography) to "
                        "enable encrypted secret storage (#2785).",
                        update_key,
                    )
                    raise SecureSettingsUnavailableError(
                        "Cannot persist secret setting '%s': secure settings "
                        "storage is unavailable (the 'cryptography' package is "
                        "missing or SecureSettingsManager failed to initialize). "
                        "Refusing to write it in plaintext to settings.json." % update_key
                    )
                if update_value:
                    self._secure_manager.set_secret(update_key, str(update_value))
                    if self._encrypted_cache_path:
                        self._secure_manager.save_to_file(self._encrypted_cache_path)
                else:
                    self._secure_manager.delete_secret(update_key)
                    if self._encrypted_cache_path:
                        self._secure_manager.save_to_file(self._encrypted_cache_path)
                self.settings[update_key] = _ENCRYPTED_PLACEHOLDER if update_value else ""
                continue

            self.settings[update_key] = update_value

    def _migrate_settings(self, settings: dict[str, object], loaded: dict[str, object]) -> dict[str, object]:
        """
        Migrate settings from older schema versions.

        Handles automatic migration of legacy settings to new provider-agnostic format.

        Args:
            settings: Merged settings (defaults + loaded)
            loaded: Raw loaded settings from file

        Returns:
            Migrated settings dict
        """
        loaded_version_raw = loaded.get("_settings_version", 1)
        if isinstance(loaded_version_raw, int):
            loaded_version = loaded_version_raw
        elif isinstance(loaded_version_raw, str) and loaded_version_raw.isdigit():
            loaded_version = int(loaded_version_raw)
        else:
            loaded_version = 1
        needs_save = False
        loaded_wake_sensitivity_saved = "wake_sensitivity" in loaded or "wake_word_sensitivity" in loaded

        settings, dropped_removed_legacy = self._drop_removed_legacy_settings(settings)
        if dropped_removed_legacy:
            needs_save = True

        settings, renamed_aliases = self._migrate_setting_aliases(settings, loaded)
        if renamed_aliases:
            needs_save = True

        ai_source_raw = settings.get("ai_source", defaults.DEFAULT_AI_SOURCE)
        canonical_ai_source = coerce_ai_source(ai_source_raw if isinstance(ai_source_raw, str) else None)
        if canonical_ai_source.value != ai_source_raw:
            settings["ai_source"] = canonical_ai_source.value
            needs_save = True

        legacy_model = loaded.get("gpt_model")
        if (not settings.get("llm_model")) and isinstance(legacy_model, str) and legacy_model:
            settings["llm_model"] = legacy_model
            needs_save = True

        for stale_key in (
            "subscription_tier",
            "subscription_ai_enabled",
            "plan_limits",
            "gpt_model",
            "computer_use_enabled",
        ):
            if stale_key in settings:
                settings.pop(stale_key, None)
                needs_save = True

        # 2026-05-11 rename: capability_tier was misleadingly close to billing
        # tier vocabulary. Migrated to agent_autonomy with the same value
        # set (solo/ensemble/symphony). Read-side and write-side aliases
        # remain in place so external callers keep working during rollout.
        legacy_autonomy = settings.get("capability_tier")
        if legacy_autonomy is not None:
            if "agent_autonomy" not in loaded:
                settings["agent_autonomy"] = legacy_autonomy
            settings.pop("capability_tier", None)
            needs_save = True

        settings, normalized = self._normalize_runtime_model_settings(settings)
        if normalized:
            needs_save = True

        # Migration: v1 -> v2 (OpenAI-specific to provider-agnostic)
        if loaded_version < 2:
            logger.info("Migrating settings from v1 to v2 (provider-agnostic LLM)")

            # Migrate openai_api_key -> llm_api_key (if not already set)
            llm_api_key_raw = settings.get("llm_api_key")
            llm_api_key = llm_api_key_raw if isinstance(llm_api_key_raw, str) else ""
            if not llm_api_key:
                legacy_key_raw = loaded.get("openai_api_key", "")
                legacy_key = legacy_key_raw if isinstance(legacy_key_raw, str) else ""
                if legacy_key and legacy_key != "***ENCRYPTED***":
                    settings["llm_api_key"] = legacy_key
                    logger.info("Migrated openai_api_key -> llm_api_key")
                    needs_save = True
                elif legacy_key_raw == "***ENCRYPTED***" and self._secure_manager:
                    # Copy from secure storage
                    try:
                        encrypted_key = self._secure_manager.get_secret("openai_api_key")
                        if encrypted_key:
                            self._secure_manager.set_secret("llm_api_key", encrypted_key)
                            settings["llm_api_key"] = "***ENCRYPTED***"
                            logger.info("Migrated encrypted openai_api_key -> llm_api_key")
                            needs_save = True
                    except Exception as e:
                        logger.debug("Could not migrate encrypted key: %s", e)

            # Migrate gpt_model -> llm_model (if not already set)
            if not settings.get("llm_model") or settings.get("llm_model") == "":
                legacy_model = loaded.get("gpt_model", "")
                if legacy_model:
                    settings["llm_model"] = legacy_model
                    logger.info("Migrated gpt_model -> llm_model: %s", legacy_model)
                    needs_save = True

            # Migrate enable_gpt -> ai_enabled
            # AI should be enabled if: (1) enable_gpt was True AND (2) there's an API key
            legacy_enable = loaded.get("enable_gpt", True)
            has_key = bool(settings.get("llm_api_key")) or bool(loaded.get("openai_api_key"))
            settings["ai_enabled"] = legacy_enable and has_key
            logger.info(
                "Set ai_enabled=%s (enable_gpt=%s, has_key=%s)",
                settings["ai_enabled"],
                legacy_enable,
                has_key,
            )

            # Set provider to openai (since we're migrating from OpenAI-specific settings)
            if settings.get("llm_provider", "") == "" or settings.get("llm_provider") == "openai":
                settings["llm_provider"] = "openai"

            # Migrate openai_key_source handling
            key_source = loaded.get("openai_key_source", "stored")
            if key_source == "disabled":
                settings["ai_enabled"] = False
                logger.info("AI disabled due to openai_key_source=disabled")
            elif key_source == "environment":
                # Try to get key from environment

                env_key = env.get("VIOLA_OPENAI_API_KEY") or env.get("OPENAI_API_KEY")
                if env_key and (not settings.get("llm_api_key") or settings["llm_api_key"] == ""):
                    settings["llm_api_key"] = env_key
                    settings["ai_enabled"] = True
                    logger.info("Migrated API key from environment variable")
                    needs_save = True

            # Migrate llm_fallback_strategy
            legacy_strategy_raw = loaded.get("llm_fallback_strategy", "auto")
            legacy_strategy = legacy_strategy_raw if isinstance(legacy_strategy_raw, str) else "auto"
            strategy_mapping = {
                "openai_only": "cloud_only",
                "skip_openai": "local_only",
            }
            if legacy_strategy in strategy_mapping:
                settings["llm_fallback_strategy"] = strategy_mapping[legacy_strategy]
                logger.info(
                    "Migrated fallback strategy: %s -> %s",
                    legacy_strategy,
                    settings.get("llm_fallback_strategy"),
                )

            # Update version
            settings["_settings_version"] = 2
            needs_save = True

        # Migration: v2 -> v3 (default LLM provider changed from openai to anthropic)
        if loaded_version < 3:
            # If the user never explicitly configured a provider (llm_api_key
            # is empty in the on-disk file), reset llm_provider and llm_model
            # to match the new AppConfig defaults. Users who explicitly chose
            # OpenAI through the UI would have stored an API key in
            # settings.json, so their choice is preserved.
            loaded_llm_key = loaded.get("llm_api_key", "")
            user_set_key = bool(loaded_llm_key) and loaded_llm_key != "***ENCRYPTED***"

            # Also check if the key was encrypted in secure storage
            if loaded_llm_key == "***ENCRYPTED***" and self._secure_manager:
                try:
                    if self._secure_manager.get_secret("llm_api_key"):
                        user_set_key = True
                except Exception:
                    logger.debug("secure_manager.get_secret failed during settings migration check")

            if not user_set_key:
                from config.settings import settings as app_cfg

                old_provider = settings.get("llm_provider", "openai")
                new_provider = app_cfg.llm_backend
                if old_provider != new_provider:
                    settings["llm_provider"] = new_provider
                    logger.info(
                        "v2->v3 migration: llm_provider %s -> %s (no user-configured API key)",
                        old_provider,
                        new_provider,
                    )

                old_model = settings.get("llm_model", "")
                new_model = app_cfg.gpt_model
                if not old_model or old_model == "gpt-4o-mini":
                    settings["llm_model"] = new_model
                    logger.info(
                        "v2->v3 migration: llm_model %s -> %s",
                        old_model or "(empty)",
                        new_model,
                    )

                # Enable AI by default with the new provider
                settings["ai_enabled"] = True
                settings["enable_gpt"] = True

            settings["_settings_version"] = 3
            needs_save = True

        # Migration: v3 -> v4 (wake_sensitivity default bumped 0.80 -> 0.90)
        # Existing users whose settings.json predates the bump should keep
        # the prior 0.80 default; only brand-new installs (no settings file)
        # should see the new 0.90 default. `loaded` is the raw on-disk dict,
        # so its emptiness is the "existing user" signal we key off of.
        if loaded_version < 4:
            prior_wake_default = 0.80
            loaded_has_any_keys = bool(loaded)
            wake_sensitivity_saved = loaded_wake_sensitivity_saved
            if loaded_has_any_keys and not wake_sensitivity_saved:
                settings["wake_sensitivity"] = prior_wake_default
                logger.info(
                    "v3->v4 migration: pinned wake_sensitivity to prior default %.2f " "(new install default is %.2f)",
                    prior_wake_default,
                    defaults.DEFAULT_WAKE_SENSITIVITY,
                )
                needs_save = True

            settings["_settings_version"] = 4
            needs_save = True

        # Migration: v4 -> v5 (legacy-pinned wake_sensitivity 0.80 -> 0.90)
        # v3->v4 intentionally wrote 0.80 for existing installs that did not
        # already save a wake_sensitivity value. Founder direction on
        # 2026-04-28 makes 0.90 the default, so only the exact numeric legacy
        # pin catches up; any other saved value remains user tuning.
        if loaded_version < 5:
            prior_wake_default = 0.80
            wake_sensitivity_value = settings.get("wake_sensitivity", _MISSING)
            legacy_pinned_wake_default = (
                isinstance(wake_sensitivity_value, (int, float))
                and not isinstance(wake_sensitivity_value, bool)
                and wake_sensitivity_value == prior_wake_default
                and not loaded_wake_sensitivity_saved
            )
            if legacy_pinned_wake_default:
                settings["wake_sensitivity"] = defaults.DEFAULT_WAKE_SENSITIVITY
                logger.info(
                    "v4->v5 migration: upgraded legacy wake_sensitivity %.2f -> %.2f",
                    prior_wake_default,
                    defaults.DEFAULT_WAKE_SENSITIVITY,
                )
                needs_save = True

            settings["_settings_version"] = 5
            needs_save = True

        # v5->v6: provider fallback is no longer enabled by default. A persisted
        # llm_fallback_enabled=True is stale default churn from before the
        # fail-closed change — flip it so ai_source stays authoritative. Runs
        # once (version-gated); a deliberate later re-enable is preserved.
        if loaded_version < 6:
            if settings.get("llm_fallback_enabled") is True:
                settings["llm_fallback_enabled"] = False
                logger.info("v5->v6 migration: disabled stale llm_fallback_enabled (provider fallback is opt-in)")
            settings["_settings_version"] = 6
            needs_save = True

        # v6->v7 (S10-SETTINGS-001): rename three user-preference keys
        # whose canonical names shifted in the parity sweep. Without
        # these the old opt-outs/acks silently default to "off-by-fresh-
        # install" the next time the user upgrades, surprising people
        # who explicitly chose those preferences:
        #
        # 1. ``auto_updates_disabled`` (legacy bool) → ``auto_update_check_enabled``
        #    Inverted semantics — disabled=True means checks=False.
        # 2. ``bypass_permissions_accepted`` → ``dangerous_mode_acknowledged``
        #    Same boolean shape; renamed because "bypass permissions"
        #    overloaded the OS-level permissions vocabulary.
        # 3. ``repl_bridge_enabled`` → ``remote_control_at_startup``
        #    Renamed because the surface is no longer a REPL — it's a
        #    full remote-control web UI, and "at_startup" was the
        #    behaviour the boolean actually controlled.
        #
        # Each rule:
        #   - only fires if the legacy key was present in the on-disk file
        #     (``loaded``, not ``settings``, so we don't migrate the
        #     default-merged value);
        #   - never overwrites a value the user has already saved against
        #     the new key (explicit choice wins);
        #   - removes the legacy key from ``settings`` so it doesn't
        #     re-appear in the saved file under both names.
        if loaded_version < 7:
            # 1. auto_updates_disabled (inverted) → auto_update_check_enabled
            if "auto_updates_disabled" in loaded:
                legacy = loaded.get("auto_updates_disabled")
                if isinstance(legacy, bool) and "auto_update_check_enabled" not in loaded:
                    settings["auto_update_check_enabled"] = not legacy
                    logger.info(
                        "v6->v7 migration: auto_updates_disabled=%s -> auto_update_check_enabled=%s",
                        legacy,
                        not legacy,
                    )
                settings.pop("auto_updates_disabled", None)

            # 2. bypass_permissions_accepted → dangerous_mode_acknowledged
            if "bypass_permissions_accepted" in loaded:
                legacy_bp = loaded.get("bypass_permissions_accepted")
                if isinstance(legacy_bp, bool) and "dangerous_mode_acknowledged" not in loaded:
                    settings["dangerous_mode_acknowledged"] = legacy_bp
                    logger.info(
                        "v6->v7 migration: bypass_permissions_accepted=%s -> dangerous_mode_acknowledged",
                        legacy_bp,
                    )
                settings.pop("bypass_permissions_accepted", None)

            # 3. repl_bridge_enabled → remote_control_at_startup
            if "repl_bridge_enabled" in loaded:
                legacy_rb = loaded.get("repl_bridge_enabled")
                if isinstance(legacy_rb, bool) and "remote_control_at_startup" not in loaded:
                    settings["remote_control_at_startup"] = legacy_rb
                    logger.info(
                        "v6->v7 migration: repl_bridge_enabled=%s -> remote_control_at_startup",
                        legacy_rb,
                    )
                settings.pop("repl_bridge_enabled", None)

            settings["_settings_version"] = 7
            needs_save = True

        # v7->v8: phone call recording now defaults on so users can audit what
        # Viola said and did on their behalf. Legacy settings.json stores a
        # full merged blob, so an old default false cannot be distinguished
        # from an explicit old false; the Phone ToS 2.1 bump is the consent
        # moment for applying this new default.
        if loaded_version < 8:
            if settings.get("record_phone_calls") is False:
                settings["record_phone_calls"] = defaults.PHONE_RECORD_CALLS_DEFAULT
                logger.info("v7->v8 migration: enabled record_phone_calls default for phone auditability")
            settings["_settings_version"] = 8
            needs_save = True

        # v8->v9 (issue #496): the periodic version-check schedulers historically
        # ran unconditionally - NEITHER honoured auto_update_check_enabled - so a
        # stored ``False`` was inert and every install checked for updates anyway.
        # Now that run_check_once honours the toggle, a stored ``False`` left over
        # from the old default would silently STOP update checks on upgrade for
        # users who never chose that. Because the setting was never honoured, any
        # on-disk ``False`` is an inert default, not a real user choice, so flip it
        # to the new ``True`` default to preserve the historical always-on
        # behaviour. Version-gated to <9 so a deliberate future opt-out (saved
        # against v9+) is preserved. The min_supported safety floor is unaffected.
        if loaded_version < 9:
            # A genuine legacy opt-out must be preserved, not re-enabled: if the
            # user engaged the old ``auto_updates_disabled=True`` control (still
            # visible in the raw ``loaded`` file this pass, before the v7 block
            # popped it), that choice already produced
            # ``auto_update_check_enabled=False`` and we keep it. Only the inert
            # default ``False`` (which no scheduler ever honoured) is flipped.
            legacy_opt_out = loaded.get("auto_updates_disabled") is True
            if settings.get("auto_update_check_enabled") is False and not legacy_opt_out:
                settings["auto_update_check_enabled"] = True
                logger.info(
                    "v8->v9 migration: reset inert auto_update_check_enabled=False -> True "
                    "(old schedulers never honoured it; preserves always-on offered-update check)"
                )
            settings["_settings_version"] = 9
            needs_save = True

        # v9->v10 (issue #4792): desktop companion pairing was gated on
        # ``companion_enabled``, which NO shipped surface could ever write --
        # not the Qt UI, not the React settings modal, not iOS. So every
        # install on disk carries the old inert default ``False``, and no real
        # user could turn the phone/browser -> desktop relay on at all. The new
        # default is True (signing in on the desktop is the pairing act, and
        # the shipped relay UI already describes it as default-on with no
        # toggle), but a merged blob cannot distinguish that inert default from
        # a deliberate choice. Since the key was unwritable, an on-disk
        # ``False`` cannot BE a deliberate choice, so flip it. Version-gated to
        # <10 so a genuine opt-out saved against v10+ (once a real off switch
        # exists) is preserved. A signed-out desktop still pairs with nothing.
        if loaded_version < 10:
            if settings.get("companion_enabled") is False:
                settings["companion_enabled"] = defaults.COMPANION_ENABLED_DEFAULT
                logger.info(
                    "v9->v10 migration: reset inert companion_enabled=False -> %s "
                    "(no shipped surface could ever write it, so pairing was unreachable)",
                    defaults.COMPANION_ENABLED_DEFAULT,
                )
            settings["_settings_version"] = 10
            needs_save = True

        # Fix: show_api_key_warning was corrupted to "***ENCRYPTED***" by a
        # false-positive substring match in _is_encrypted_field ("api_key" is
        # a substring of "show_api_key_warning").  Reset to its boolean default.
        show_val = settings.get("show_api_key_warning")
        if isinstance(show_val, str):
            default_show_warning = self.DEFAULT_SETTINGS.get("show_api_key_warning", True)
            settings["show_api_key_warning"] = default_show_warning
            logger.info(
                "Fixed corrupted show_api_key_warning from %s value -> %s",
                type(show_val).__name__,
                default_show_warning,
            )
            needs_save = True

        # Save if migrations were applied
        if needs_save:
            # Don't call save() directly to avoid recursion - set flag
            self._needs_save_after_load = True

        return settings

    def _is_encrypted_field(self, key: str) -> bool:
        """
        Check if a field should be encrypted.

        Args:
            key: Field name

        Returns:
            True if field should be encrypted
        """
        if key in self._ENCRYPTED_FIELD_EXCLUSIONS:
            return False
        key_lower = key.lower()
        return any(pattern in key_lower for pattern in self.ENCRYPTED_FIELDS)

    def update(
        self,
        updates: dict[str, object],
        save_immediately: bool = True,
        user_id: str | None = None,
    ) -> bool:
        """
        Update multiple settings at once.

        Args:
            updates: Dict of settings to update
            save_immediately: Whether to save to file immediately

        Returns:
            True if successful
        """
        # Auto-map agent_autonomy → browser_session_mode (unless caller set both).
        # Accept the legacy `capability_tier` key as an alias.
        pending_updates = {}
        for raw_key, raw_value in updates.items():
            canonical_key = self.canonicalize_setting_key(raw_key)
            if canonical_key in self.REMOVED_LEGACY_SETTING_KEYS:
                continue
            pending_updates[canonical_key] = raw_value
        autonomy = pending_updates.get("agent_autonomy")
        if autonomy and autonomy in self._TIER_MODE_MAP and "browser_session_mode" not in pending_updates:
            pending_updates["browser_session_mode"] = self._TIER_MODE_MAP[autonomy]

        resolved_user_id = self._resolve_user_id(user_id)
        pending_updates = self._normalize_runtime_setting_updates(pending_updates, resolved_user_id)
        return self._persist_setting_updates(
            pending_updates,
            resolved_user_id=resolved_user_id,
            save_immediately=save_immediately,
        )

    def reset(self, save_immediately: bool = True) -> bool:
        """
        Reset all settings to defaults.

        Args:
            save_immediately: Whether to save to file immediately

        Returns:
            True if successful
        """
        self.settings = _GuardedSettingsDict(self._default_settings_base())

        if save_immediately:
            return self.save()

        return True

    def reset_user_settings(self, user_id: str | None, save_immediately: bool = True) -> bool:
        """Reset settings for one authenticated user without touching device defaults."""
        if not user_id:
            raise ValueError("user_id is required")

        resolved_user_id = self._resolve_user_id(user_id)
        if not resolved_user_id:
            raise ValueError("user_id is required")

        if _uses_global_settings_only(resolved_user_id):
            return self.reset(save_immediately=save_immediately)

        with self._lock_for_user_settings_write(resolved_user_id):
            self._user_settings_cache.put(resolved_user_id, {})
            if not save_immediately:
                return True

            success = self._save_user_settings_blob(resolved_user_id, {})
            if not success:
                self._user_settings_cache.invalidate(resolved_user_id)
            return success

    def export_settings(self, export_path: Path) -> bool:
        """Export settings to a file (for backup)."""
        try:
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
            logger.info("Settings exported to %s", export_path)
            return True
        except Exception as e:
            logger.error("Failed to export settings: %s", e)
            return False

    def import_settings(self, import_path: Path) -> bool:
        """Import settings from a file."""
        try:
            with open(import_path, encoding="utf-8") as f:
                imported = json.load(f)

            # Validate it's a dict
            if not isinstance(imported, dict):
                raise ValueError("Invalid settings file format")

            # Merge imported values into a fresh defaults snapshot so
            # encrypted settings follow the same write path as set().
            self.settings = _GuardedSettingsDict(self._default_settings_base())
            self.update(imported, save_immediately=True)
            logger.info("Settings imported from %s", import_path)
            return True

        except Exception as e:
            logger.error("Failed to import settings: %s", e)
            return False

    # =========================================================================
    # AI Settings Helpers
    # =========================================================================

    def is_ai_enabled(self) -> bool:
        """
        Check if AI features should be enabled.

        AI is enabled if:
        1. ai_enabled is True AND
        2. The selected AI source is available

        Returns:
            True if AI features should be active
        """
        ai_enabled_raw = self.get("ai_enabled", True)
        if isinstance(ai_enabled_raw, bool):
            ai_enabled = ai_enabled_raw
        else:
            ai_enabled = bool(ai_enabled_raw)
        if not ai_enabled:
            return False

        ai_source_raw = self.get("ai_source", defaults.DEFAULT_AI_SOURCE)
        try:
            from config.settings import settings as app_settings

            override = str(getattr(app_settings, "ai_source_override", "") or "").strip()
            if override:
                ai_source_raw = override
        except (ImportError, AttributeError):
            logger.debug("AI source override unavailable from AppConfig")
        ai_source = coerce_ai_source(ai_source_raw if isinstance(ai_source_raw, str) else None)

        if ai_source is AiSource.CODEX:
            try:
                from services.llm.codex_auth import is_codex_available

                return is_codex_available()
            except ImportError:
                logger.debug("codex-auth not installed — Codex source unavailable")
                return False
        if ai_source is AiSource.MANAGED:
            try:
                from config.settings import settings as app_settings

                if bool(app_settings.openai_api_key):
                    return True
            except Exception:
                logger.debug("Managed AI source unavailable from AppConfig")
            # Keyless managed AI (#269/#337): a REAL customer desktop install
            # ships NO local OpenAI key — the managed turn forwards through
            # Viola's cloud (server holds the key), authenticated by the
            # desktop GoTrue session. Requiring a local key here predates that
            # forward and silently killed the whole intent pipeline on keyless
            # installs: the provider router read this gate, logged "AI features
            # disabled in settings", and every chat/agent turn died with "No
            # handler matched your request" even though the factory could
            # build CloudManagedProvider fine. Mirror the availability
            # condition of LLMProviderFactory._create_cloud_managed_provider:
            # desktop surface + cloud URL + cloud-LLM consent.
            try:
                from config.settings import settings as app_settings
                from core.privacy_consent import is_cloud_llm_consented

                app_surface = str(getattr(app_settings, "app_surface", "desktop") or "desktop").strip().lower()
                cloud_url = (
                    getattr(app_settings, "cloud_url", "") or getattr(app_settings, "api_base_url", "") or ""
                ).strip()
                if app_surface != "cloud" and cloud_url and is_cloud_llm_consented():
                    return True
            # Availability probe: any config/consent read failure must fall
            # through to disabled, never raise.
            except Exception:  # noqa: BLE001, RUF100
                logger.debug("Keyless managed cloud-forward availability check failed")
            return False
        if ai_source is AiSource.BYOK:
            # Check if we have an API key (new or legacy)
            api_key_raw = self.get("llm_api_key", "")
            api_key = api_key_raw if isinstance(api_key_raw, str) else ""
            if api_key and api_key != "***ENCRYPTED***":
                return True

            # Check legacy openai_api_key as fallback
            legacy_key_raw = self.get("openai_api_key", "")
            legacy_key = legacy_key_raw if isinstance(legacy_key_raw, str) else ""
            if legacy_key and legacy_key != "***ENCRYPTED***":
                return True

            # Check secure storage for both keys
            if self._secure_manager:
                try:
                    stored_key = self._secure_manager.get_secret("llm_api_key")
                    if stored_key:
                        return True
                    # Also check legacy key in secure storage
                    legacy_stored = self._secure_manager.get_secret("openai_api_key")
                    if legacy_stored:
                        return True
                except Exception as e:
                    logger.exception("Failed to check API keys in secure storage: %s", e)

            # Check for Ollama (doesn't require API key)
            provider = self.get("llm_provider", "openai")
            if provider == "ollama":
                return True

            return False
        if ai_source is AiSource.LOCAL:
            return True

        return False

    @classmethod
    def is_ai_available(cls, ai_source: str | None = None) -> bool:
        """Check whether an LLM is available for the given or current ai_source.

        Public classmethod wrapper around ``is_ai_enabled``. Consults the live
        singleton manager so callers without an instance (factories, CLI
        helpers, regression tests) can ask the same question as the instance
        method.

        Args:
            ai_source: Optional AiSource value. When provided, overrides the
                currently-persisted ``ai_source`` for the availability check.
                When ``None``, uses the current ``ai_source`` from settings.

        Returns:
            True if the selected AI source is available (managed key present,
            Codex OAuth present, BYOK key present, or local enabled). Never
            raises — on any unexpected error, returns False.
        """
        try:
            mgr = get_settings_manager()
        except Exception:
            return False

        if ai_source is None:
            return bool(mgr.is_ai_enabled())

        # Caller specified a source to probe — evaluate that source directly
        # without mutating the persisted setting.
        source = coerce_ai_source(ai_source if isinstance(ai_source, str) else None)

        if source is AiSource.CODEX:
            try:
                from services.llm.codex_auth import is_codex_available

                return bool(is_codex_available())
            except ImportError:
                return False
            except Exception:
                return False

        if source is AiSource.MANAGED:
            try:
                from config.settings import settings as app_settings

                return bool(app_settings.openai_api_key)
            except Exception:
                return False

        if source is AiSource.BYOK:
            # Inline the BYOK availability probe so we don't mutate persisted
            # state on the manager. Mirrors the BYOK branch of is_ai_enabled.
            try:
                api_key_raw = mgr.get("llm_api_key", "")
                api_key = api_key_raw if isinstance(api_key_raw, str) else ""
                if api_key and api_key != _ENCRYPTED_PLACEHOLDER:
                    return True

                legacy_key_raw = mgr.get("openai_api_key", "")
                legacy_key = legacy_key_raw if isinstance(legacy_key_raw, str) else ""
                if legacy_key and legacy_key != _ENCRYPTED_PLACEHOLDER:
                    return True

                secure = getattr(mgr, "_secure_manager", None)
                if secure is not None:
                    try:
                        if secure.get_secret("llm_api_key") or secure.get_secret("openai_api_key"):
                            return True
                    except Exception:
                        logger.debug("Secure API key lookup failed during provider migration")

                provider = mgr.get("llm_provider", "openai")
                if provider == "ollama":
                    return True
            except Exception:
                return False
            return False

        if source is AiSource.LOCAL:
            return True

        return False

    def get_llm_config(self) -> dict[str, object]:
        """
        Get current LLM configuration.

        Returns:
            Dict with provider, api_key, model, base_url
        """
        return {
            "provider": self.get("llm_provider", "openai"),
            "api_key": self.get("llm_api_key", ""),
            "model": self.get("llm_model", ""),
            "base_url": self.get("llm_base_url", ""),
        }

    def set_llm_config(
        self,
        provider: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        save_immediately: bool = True,
    ) -> bool:
        """
        Set LLM configuration.

        Args:
            provider: Provider type (openai, anthropic, google, ollama, openai_compatible)
            api_key: API key for the provider
            model: Model identifier
            base_url: Custom base URL (for openai_compatible or ollama)
            save_immediately: Whether to save immediately

        Returns:
            True if successful
        """
        updates: dict[str, object] = {}

        if provider is not None:
            updates["llm_provider"] = provider

        if api_key is not None:
            updates["llm_api_key"] = api_key

        if model is not None:
            updates["llm_model"] = model

        if base_url is not None:
            updates["llm_base_url"] = base_url

        # Auto-enable AI if we have an API key
        if api_key:
            updates["ai_enabled"] = True

        if not updates:
            return True

        return self.update(updates, save_immediately=save_immediately)


# Use standardized singleton pattern
get_settings_manager = create_singleton_getter("settings_manager", SettingsManager)


def save_delivery_address(
    street: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    full_text: str = "",
) -> bool:
    """Persist the delivery address while preserving existing non-updated parts."""
    settings_manager = get_settings_manager()
    current_value = settings_manager.get("delivery_address", {})
    address = {
        "full_text": "",
        "street": "",
        "city": "",
        "state": "",
        "zip": "",
    }
    if isinstance(current_value, dict):
        address.update(current_value)

    updates = {
        "street": street.strip(),
        "city": city.strip(),
        "state": state.strip(),
        "zip": zip_code.strip(),
    }
    for key, value in updates.items():
        if value:
            address[key] = value

    full_text_value = full_text.strip()
    if full_text_value:
        address["full_text"] = full_text_value
    elif updates["street"]:
        address["full_text"] = ", ".join(
            part
            for part in (
                address["street"],
                address["city"],
                address["state"],
                address["zip"],
            )
            if part
        )

    return settings_manager.set("delivery_address", address)
