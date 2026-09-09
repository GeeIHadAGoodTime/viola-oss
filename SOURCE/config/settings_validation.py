"""
Configuration validation and post-initialization logic.

This module contains the validation and setup logic extracted from the AppConfig
__post_init__ method to comply with code constraints.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import warnings
from collections.abc import Callable
from typing import Any, cast

from config import env
from config.defaults import DEFAULT_GPT_MODEL
from core.constants import WAKE_SENSITIVITY_MIN
from core.logging_config import get_logger

from .settings import (
    AppConfig,
    BuildProfile,
    SettingsValidationError,
    _apply_environment_overrides,
)

_logger = get_logger(__name__)

_ZoneInfoCtor = Callable[[str], datetime.tzinfo]
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except Exception:  # pragma: no cover - Python <3.9 fallback
    _zone_info: _ZoneInfoCtor | None = None
else:
    _zone_info = _ZoneInfo

_WINDOWS_TIMEZONE_ALIASES = {
    "atlantic daylight time": "America/Halifax",
    "atlantic standard time": "America/Halifax",
    "central daylight time": "America/Chicago",
    "central standard time": "America/Chicago",
    "eastern daylight time": "America/New_York",
    "eastern standard time": "America/New_York",
    "mountain daylight time": "America/Denver",
    "mountain standard time": "America/Denver",
    "pacific daylight time": "America/Los_Angeles",
    "pacific standard time": "America/Los_Angeles",
    "us eastern standard time": "America/Indianapolis",
    "us mountain standard time": "America/Phoenix",
    "coordinated universal time": "UTC",
    "utc": "UTC",
}


def _normalize_timezone_name(value: str) -> str:
    normalized = value.strip()
    return _WINDOWS_TIMEZONE_ALIASES.get(normalized.lower(), normalized)


def _resolve_calendar_timezone(raw_value: str | None) -> str:
    """
    Resolve the calendar timezone configuration value.

    Accepts:
        - Explicit IANA timezone string (e.g., "Europe/Berlin")
        - "auto" to detect the local timezone
        - None/empty -> fallback to UTC
    """

    logger = get_logger("viola.config")

    value = (raw_value or "").strip()
    if not value:
        value = "UTC"

    if value.lower() == "auto":
        detected: str | None = None

        try:  # pragma: no cover - optional dependency
            from tzlocal import get_localzone_name
        except Exception as e:
            logger.debug("tzlocal not available (non-critical): %s", e)
            get_localzone_name = None

        if get_localzone_name:
            try:
                detected = get_localzone_name()
            except Exception as e:
                logger.debug("tzlocal detection failed: %s", e)
                detected = None

        if not detected:
            try:
                local_tz = datetime.datetime.now().astimezone().tzinfo
                if local_tz is not None:
                    detected = getattr(local_tz, "key", None) or local_tz.tzname(None)
            except Exception as e:
                logger.debug("Fallback timezone detection failed: %s", e)
                detected = None

        value = detected or "UTC"

    value = _normalize_timezone_name(value)

    if _zone_info is not None:
        try:
            _zone_info(value)
        except Exception:
            logger.warning("Invalid calendar timezone '%s'; falling back to UTC", value)
            return "UTC"

    return value


def _process_environment_variables(config: AppConfig) -> None:
    """Process environment variable overrides for config fields."""
    _apply_environment_overrides(config)


def _process_legacy_aliases(config: AppConfig) -> None:
    """Process legacy environment variable aliases."""
    # Legacy aliases (VIOLA_PORT/VIOLA_HOST) remain supported
    if "VIOLA_API_PORT" not in os.environ:
        alias_port = env.get("VIOLA_PORT")
        if alias_port:
            try:
                parsed_port = int(alias_port)
            except Exception:
                raise SettingsValidationError("VIOLA_PORT must be an integer between 1 and 65535") from None
            if not (1 <= parsed_port <= 65535):
                raise SettingsValidationError("VIOLA_PORT must be an integer between 1 and 65535")
            config.api_port = parsed_port

    host_explicitly_set = "VIOLA_API_HOST" in os.environ
    if not host_explicitly_set:
        alias_host = env.get("VIOLA_HOST")
        if alias_host:
            config.api_host = alias_host
            host_explicitly_set = True

    # Multiroom hub requires LAN binding. If the user enabled multiroom but
    # did NOT override VIOLA_API_HOST, auto-promote the default loopback
    # bind to BIND_ALL_INTERFACES so spokes on other devices can reach the
    # hub. Users who set VIOLA_API_HOST explicitly get the host they asked
    # for (including intentional 127.0.0.1 behind a reverse proxy).
    if not host_explicitly_set and env.get_bool("VIOLA_ENABLE_MULTIROOM", default=False):
        from core.constants import BIND_ALL_INTERFACES, LOCALHOST

        if config.api_host == LOCALHOST:
            config.api_host = BIND_ALL_INTERFACES
            _logger.info(
                "Multiroom enabled — auto-promoted api_host %s -> %s. " "Set VIOLA_API_HOST explicitly to opt out.",
                LOCALHOST,
                BIND_ALL_INTERFACES,
            )

    # VIOLA_E2E_MODE is an alias for VIOLA_EMBEDDED_ONLY
    if "VIOLA_EMBEDDED_ONLY" not in os.environ:
        e2e_mode = env.get("VIOLA_E2E_MODE", "").lower()
        if e2e_mode in ("true", "1", "yes"):
            config.embedded_only = True

    # CLOUD_URL → VIOLA_CLOUD_URL alias (legacy .env.cloud naming)
    if "VIOLA_CLOUD_URL" not in os.environ:
        cloud_url_alias = env.get("CLOUD_URL")
        if cloud_url_alias:
            config.cloud_url = cloud_url_alias

    # Transactional email secrets historically used non-prefixed cloud names.
    if "VIOLA_RESEND_API_KEY" not in os.environ:
        resend_api_key = env.get("RESEND_API_KEY")
        if resend_api_key:
            config.resend_api_key = resend_api_key
    if "VIOLA_RESEND_WEBHOOK_SECRET" not in os.environ:
        resend_webhook_secret = env.get("RESEND_WEBHOOK_SECRET")
        if resend_webhook_secret:
            config.resend_webhook_secret = resend_webhook_secret
    if "VIOLA_EMAIL_FROM_ADDRESS" not in os.environ:
        email_from = env.get("EMAIL_FROM")
        if email_from:
            config.email_from_address = email_from
    if "VIOLA_WEBSITE_BASE_URL" not in os.environ:
        website_base_url = env.get("WEBSITE_BASE_URL")
        if website_base_url:
            config.website_base_url = website_base_url

    # Legacy wake keyword path alias (Porcupine-era var name)
    if not config.wake_keyword_path:
        legacy_keyword_path = env.get("PV_PORCUPINE_KEYWORD_PATH")
        if legacy_keyword_path:
            config.wake_keyword_path = legacy_keyword_path


def _process_deprecated_flags(config: AppConfig) -> None:
    """Process deprecated environment flags with warnings."""
    if env.get("VIOLA_EXPERIMENTAL_LEGACY_EVENT_FLOW") is not None:
        warnings.warn(
            "VIOLA_EXPERIMENTAL_LEGACY_EVENT_FLOW is ignored; typed event flow is always enabled.",
            DeprecationWarning,
            stacklevel=2,
        )

    if env.get("VIOLA_QT_USE_LEGACY_EVENT_FLOW") == "1":
        warnings.warn(
            "VIOLA_QT_USE_LEGACY_EVENT_FLOW is ignored; typed event flow is always enabled.",
            DeprecationWarning,
            stacklevel=2,
        )


def _process_api_keys(config: AppConfig) -> None:
    """Process API key configuration from environment."""
    if not config.openai_api_key and env.get("OPENAI_API_KEY"):
        config.openai_api_key = env.get("OPENAI_API_KEY")

    if not config.ollama_base_url and env.get("OLLAMA_BASE_URL"):
        config.ollama_base_url = env.get("OLLAMA_BASE_URL")

    # GPT model aliases (non-VIOLA prefix, only if not already overridden)
    if config.gpt_model == DEFAULT_GPT_MODEL:
        for model_env_key in ("OPENAI_GPT_MODEL", "OPENAI_MODEL"):
            alt_model = (env.get(model_env_key) or "").strip()
            if alt_model:
                config.gpt_model = alt_model
                break

    # Spotify credentials (non-VIOLA prefix)
    if not config.spotify_client_id and env.get("SPOTIFY_CLIENT_ID"):
        config.spotify_client_id = env.get("SPOTIFY_CLIENT_ID")
    if not config.spotify_client_secret and env.get("SPOTIFY_CLIENT_SECRET"):
        config.spotify_client_secret = env.get("SPOTIFY_CLIENT_SECRET")
    if not config.spotify_refresh_token and env.get("SPOTIFY_REFRESH_TOKEN"):
        config.spotify_refresh_token = env.get("SPOTIFY_REFRESH_TOKEN")
    if not config.spotify_redirect_uri and env.get("SPOTIFY_REDIRECT_URI"):
        config.spotify_redirect_uri = env.get("SPOTIFY_REDIRECT_URI")
    if not config.spotify_device_id and env.get("SPOTIFY_DEVICE_ID"):
        config.spotify_device_id = env.get("SPOTIFY_DEVICE_ID")

    # Google OAuth credentials (non-VIOLA prefix — legacy .env.example format)
    # These map GOOGLE_CLIENT_ID → google_client_id for backwards compatibility.
    # New installs should use VIOLA_GOOGLE_CLIENT_ID (set via the VIOLA_ prefix loop above).
    if not config.google_client_id and env.get("GOOGLE_CLIENT_ID"):
        config.google_client_id = env.get("GOOGLE_CLIENT_ID")
    if not config.google_client_secret and env.get("GOOGLE_CLIENT_SECRET"):
        config.google_client_secret = env.get("GOOGLE_CLIENT_SECRET")
    # Telnyx credentials (non-VIOLA prefix)
    if not config.telnyx_api_key and env.get("TELNYX_API_KEY"):
        config.telnyx_api_key = env.get("TELNYX_API_KEY")
    if not config.telnyx_phone_number and env.get("TELNYX_PHONE_NUMBER"):
        config.telnyx_phone_number = env.get("TELNYX_PHONE_NUMBER")
    if not config.telnyx_sms_from_number and env.get("TELNYX_SMS_FROM_NUMBER"):
        config.telnyx_sms_from_number = env.get("TELNYX_SMS_FROM_NUMBER")
    if not config.telnyx_sip_connection_id and env.get("TELNYX_SIP_CONNECTION_ID"):
        config.telnyx_sip_connection_id = env.get("TELNYX_SIP_CONNECTION_ID")
    if not config.telnyx_messaging_profile_id and env.get("TELNYX_MESSAGING_PROFILE_ID"):
        config.telnyx_messaging_profile_id = env.get("TELNYX_MESSAGING_PROFILE_ID")
    if not config.telnyx_public_ws_url and env.get("TELNYX_PUBLIC_WS_URL"):
        config.telnyx_public_ws_url = env.get("TELNYX_PUBLIC_WS_URL")
    if not config.telnyx_webhook_public_key and env.get("TELNYX_WEBHOOK_PUBLIC_KEY"):
        config.telnyx_webhook_public_key = env.get("TELNYX_WEBHOOK_PUBLIC_KEY")
    if not config.telnyx_stream_shared_secret and env.get("TELNYX_STREAM_SHARED_SECRET"):
        config.telnyx_stream_shared_secret = env.get("TELNYX_STREAM_SHARED_SECRET")
    if not config.telnyx_media_allowlist and env.get("TELNYX_MEDIA_ALLOWLIST"):
        config.telnyx_media_allowlist = env.get("TELNYX_MEDIA_ALLOWLIST")
    if not config.elevenlabs_api_key and env.get("ELEVENLABS_API_KEY"):
        config.elevenlabs_api_key = env.get("ELEVENLABS_API_KEY")

    # Stripe credentials and launch catalog price IDs. VIOLA_ names are
    # preferred; non-prefixed aliases keep older cloud secret names usable.
    stripe_aliases = (
        ("stripe_secret_key", ("STRIPE_SECRET_KEY",)),
        ("stripe_webhook_secret", ("STRIPE_WEBHOOK_SECRET",)),
        ("stripe_publishable_key", ("STRIPE_PUBLISHABLE_KEY",)),
        (
            "stripe_price_pro_monthly",
            ("STRIPE_PRICE_PRO_MONTHLY", "STRIPE_PRICE_PREMIUM_MONTHLY"),
        ),
        (
            "stripe_price_pro_annual",
            ("STRIPE_PRICE_PRO_ANNUAL", "STRIPE_PRICE_PREMIUM_YEARLY"),
        ),
        (
            "stripe_price_max_monthly",
            ("STRIPE_PRICE_MAX_MONTHLY", "STRIPE_PRICE_FAMILY_MONTHLY"),
        ),
        (
            "stripe_price_max_annual",
            ("STRIPE_PRICE_MAX_ANNUAL", "STRIPE_PRICE_FAMILY_YEARLY"),
        ),
    )
    for attr, aliases in stripe_aliases:
        if getattr(config, attr):
            continue
        for alias in aliases:
            value = env.get(alias)
            if value:
                setattr(config, attr, value)
                break


def _validate_api_port(config: AppConfig) -> None:
    """Validate API port.

    Port 0 is rejected by default. Tests that need an OS-assigned ephemeral
    port must set VIOLA_ALLOW_EPHEMERAL_PORT=1 (set by the pytest harness).
    """
    try:
        port = int(config.api_port)
    except Exception as exc:
        raise SettingsValidationError("api_port must be an integer between 1 and 65535") from exc

    allow_ephemeral = env.get_bool("VIOLA_ALLOW_EPHEMERAL_PORT", default=False)
    if port == 0:
        if not allow_ephemeral:
            raise SettingsValidationError(
                "api_port=0 is not allowed. Set VIOLA_ALLOW_EPHEMERAL_PORT=1 for test harnesses "
                "that need an OS-assigned port."
            )
    elif not (1 <= port <= 65535):
        raise SettingsValidationError("api_port must be between 1 and 65535")
    config.api_port = port


def _validate_wake_sensitivity(config: AppConfig) -> None:
    """Validate wake sensitivity."""
    try:
        ws = float(config.wake_sensitivity)
        if ws < WAKE_SENSITIVITY_MIN:
            ws = WAKE_SENSITIVITY_MIN
        if ws > 1.0:
            ws = 1.0
        config.wake_sensitivity = ws
    except Exception as exc:
        raise SettingsValidationError(
            "wake_sensitivity must be a float between %.2f and 1" % WAKE_SENSITIVITY_MIN
        ) from exc


def _validate_wake_gain_scheduler(config: AppConfig) -> None:
    """Validate wake gain scheduler settings."""
    logger = get_logger("viola.config")
    try:
        interval = int(config.wake_gain_scheduler_interval_minutes)
        config.wake_gain_scheduler_interval_minutes = max(5, interval)
    except Exception as e:
        logger.warning(
            "Invalid wake_gain_scheduler_interval_minutes '%s', using default 45: %s",
            config.wake_gain_scheduler_interval_minutes,
            e,
        )
        config.wake_gain_scheduler_interval_minutes = 45

    try:
        retention = int(config.wake_gain_history_retention)
        config.wake_gain_history_retention = max(10, retention)
    except Exception as e:
        logger.warning(
            "Invalid wake_gain_history_retention '%s', using default 180: %s",
            config.wake_gain_history_retention,
            e,
        )
        config.wake_gain_history_retention = 180

    if not isinstance(config.wake_gain_scheduler_profiles, list):
        config.wake_gain_scheduler_profiles = []


def _validate_stt_latency_settings(config: AppConfig) -> None:
    """Validate STT latency settings."""
    logger = get_logger("viola.config")
    try:
        warn = float(config.stt_latency_warn_seconds)
        config.stt_latency_warn_seconds = max(0.1, warn)
    except Exception as e:
        logger.warning(
            "Invalid stt_latency_warn_seconds '%s', using default 4.0: %s",
            config.stt_latency_warn_seconds,
            e,
        )
        config.stt_latency_warn_seconds = 4.0

    try:
        alert = float(config.stt_latency_alert_seconds)
        config.stt_latency_alert_seconds = max(config.stt_latency_warn_seconds, alert)
    except Exception as e:
        logger.warning(
            "Invalid stt_latency_alert_seconds '%s', using default: %s",
            config.stt_latency_alert_seconds,
            e,
        )
        config.stt_latency_alert_seconds = max(config.stt_latency_warn_seconds, 8.0)

    try:
        ttl = int(config.stt_language_hint_ttl_seconds)
        config.stt_language_hint_ttl_seconds = max(60, ttl)
    except Exception as e:
        logger.warning(
            "Invalid stt_language_hint_ttl_seconds '%s', using default 1800: %s",
            config.stt_language_hint_ttl_seconds,
            e,
        )
        config.stt_language_hint_ttl_seconds = 1800


def _validate_tts_voice_settings(config: AppConfig) -> None:
    """Validate TTS audio-shaping settings."""
    logger = get_logger("viola.config")
    try:
        target = float(config.tts_loudness_target_lufs)
        config.tts_loudness_target_lufs = min(-10.0, max(-30.0, target))
    except Exception as e:
        logger.warning(
            "Invalid tts_loudness_target_lufs '%s', using default -16.0: %s",
            config.tts_loudness_target_lufs,
            e,
        )
        config.tts_loudness_target_lufs = -16.0

    try:
        jitter = float(config.tts_speed_jitter_pct)
        config.tts_speed_jitter_pct = min(0.10, max(0.0, jitter))
    except Exception as e:
        logger.warning(
            "Invalid tts_speed_jitter_pct '%s', using default 0.02: %s",
            config.tts_speed_jitter_pct,
            e,
        )
        config.tts_speed_jitter_pct = 0.02

    try:
        gap = int(config.tts_intersentence_gap_ms)
        config.tts_intersentence_gap_ms = min(1000, max(0, gap))
    except Exception as e:
        logger.warning(
            "Invalid tts_intersentence_gap_ms '%s', using default 180: %s",
            config.tts_intersentence_gap_ms,
            e,
        )
        config.tts_intersentence_gap_ms = 180

    config.tts_voice_blend = _normalize_tts_voice_blend(config.tts_voice_blend)


def _normalize_tts_voice_blend(raw: object) -> list[list[Any]] | None:
    if raw in (None, "", []):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pairs: list[list[Any]] = []
            for item in raw.split(","):
                if ":" not in item:
                    return None
                voice_id, weight = item.split(":", 1)
                pairs.append([voice_id.strip(), weight.strip()])
            raw = pairs
    if not isinstance(raw, list):
        return None

    normalized: list[list[Any]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        voice_id, weight = item
        if not isinstance(voice_id, str) or not voice_id.strip():
            return None
        try:
            numeric_weight = float(weight)
        except Exception:
            return None
        if numeric_weight <= 0.0:
            continue
        normalized.append([voice_id.strip(), numeric_weight])

    return normalized or None


def _validate_agent_timeouts(config: AppConfig) -> None:
    """Validate agent timeout ranges.

    ``agent_timeout_seconds`` and ``agent_tool_timeout_seconds`` must be
    strictly positive and <= 86400s (24h). Negative/zero values would break
    the agent loop's backstop; unbounded values would let stuck agents spin
    forever.
    """
    for name in ("agent_timeout_seconds", "agent_tool_timeout_seconds"):
        raw = getattr(config, name)
        try:
            value = float(raw)
        except Exception as exc:
            raise SettingsValidationError("%s must be a number between 1 and 86400" % name) from exc
        if not (1.0 <= value <= 86400.0):
            raise SettingsValidationError("%s must be between 1 and 86400 seconds (got %r)" % (name, raw))
        setattr(config, name, value)


def _validate_phone_dial_timeout_override(config: AppConfig) -> None:
    """Coerce/validate ``phone_dial_timeout_override`` (test-induction knob).

    The field defaults to ``None``, so ``VIOLA_PHONE_DIAL_TIMEOUT_OVERRIDE``
    lands as a raw string via the generic env-override path (``_coerce_value``
    keeps strings for ``None``-typed defaults). Normalize: None/empty means
    "unset" (production default — the dial POST keeps TIMEOUT_VERY_LONG);
    otherwise it must parse to a strictly positive float.
    """
    raw = getattr(config, "phone_dial_timeout_override", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        config.phone_dial_timeout_override = None
        return
    try:
        value = float(raw)
    except Exception as exc:
        raise SettingsValidationError(
            "phone_dial_timeout_override must be a positive number of seconds (got %r)" % raw
        ) from exc
    if not (0.0 < value <= 86400.0):
        raise SettingsValidationError("phone_dial_timeout_override must be > 0 and <= 86400 seconds (got %r)" % raw)
    config.phone_dial_timeout_override = value


_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
_HTTPS_URL_RE = re.compile(r"^https://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _validate_wake_data_upload_endpoint(config: AppConfig) -> None:
    """Validate wake training upload and model-update URLs when set.

    Empty string is the documented "feature disabled" value.

    SEC-046: the upload endpoint receives raw wake-word voice clips (biometric
    voiceprints). Plaintext ``http://`` would expose them to a network MITM, so
    the upload endpoint MUST be ``https://``. The model-update URL only fetches
    a public model artifact, so http(s) is acceptable there.
    """
    upload_url = (getattr(config, "wake_data_upload_endpoint", "") or "").strip()
    if upload_url and not _HTTPS_URL_RE.match(upload_url):
        raise SettingsValidationError(
            "wake_data_upload_endpoint must be an https:// URL — voice clips are "
            "biometric and must not be uploaded over plaintext http (got %r)" % upload_url
        )

    model_url = (getattr(config, "wake_data_model_update_url", "") or "").strip()
    if model_url and not _URL_RE.match(model_url):
        raise SettingsValidationError(
            "wake_data_model_update_url must be an http:// or https:// URL (got %r)" % model_url
        )


_WEAK_DB_PASSWORDS: frozenset[str] = frozenset(
    {
        "",
        "viola_dev",
        "change_me",
        "changeme",
        "password",
        "postgres",
        "admin",
        "root",
        "test",
        "dev",
        "local",
    }
)


def _validate_cloud_database(config: AppConfig) -> None:
    """When running in cloud mode, require a non-placeholder Postgres password.

    Checks both ``POSTGRES_PASSWORD`` (used by docker-compose substitution:
    ``${POSTGRES_PASSWORD:-viola_dev}``) and any password embedded in
    ``VIOLA_DATABASE_URL``. A weak/placeholder value in either silently
    yields an insecure production database — fail fast instead.

    A VIOLA_DATABASE_URL that explicitly sets its own password overrides the
    POSTGRES_PASSWORD env var check (operator opt-in), but the embedded
    password is still checked against the weak-password list.
    """
    surface = str(getattr(config, "app_surface", "desktop")).lower()
    if surface != "cloud":
        return

    db_url = (env.get("VIOLA_DATABASE_URL") or "").strip()
    embedded_password: str | None = None
    if db_url:
        # Parse postgres URL (user:pw@host:port/db) without pulling in psycopg;
        # urllib.parse handles the scheme + userinfo split correctly.
        # pragma: allowlist secret
        from urllib.parse import urlparse

        try:
            parsed = urlparse(db_url)
        except ValueError as exc:
            raise SettingsValidationError("VIOLA_DATABASE_URL is not a valid URL (%s)" % exc) from exc
        if parsed.scheme not in ("postgres", "postgresql"):
            raise SettingsValidationError(
                "VIOLA_DATABASE_URL must use postgres:// or postgresql:// "
                "scheme when app_surface=cloud (got scheme=%r)" % parsed.scheme
            )
        embedded_password = parsed.password or None

    # Resolve the effective password: URL-embedded wins over POSTGRES_PASSWORD
    # because the URL is what the application actually connects with.
    if embedded_password is not None:
        effective_password = embedded_password
        source = "VIOLA_DATABASE_URL"
    else:
        effective_password = (env.get("POSTGRES_PASSWORD") or "").strip()
        source = "POSTGRES_PASSWORD"

    if effective_password.lower() in _WEAK_DB_PASSWORDS:
        # Don't echo the actual password in the error.
        raise SettingsValidationError(
            "%s contains a weak/placeholder value when app_surface=cloud. "
            "Set a strong random secret before starting the cloud backend." % source
        )


def _validate_cloud_public_abuse_dependencies(config: AppConfig) -> None:
    """Require shared abuse-control dependencies for non-dev cloud startup."""
    surface = str(getattr(config, "app_surface", "desktop")).lower()
    env_name = str(getattr(config, "env", "dev")).lower()
    if surface != "cloud" or env_name == "dev":
        return

    if not (getattr(config, "redis_url", None) or "").strip():
        raise SettingsValidationError(
            "VIOLA_REDIS_URL is required when app_surface=cloud and VIOLA_ENV != dev. "
            "Cloud auth nonces and public abuse limiters require shared Redis state."
        )

    limiter_backend = str(getattr(config, "rate_limiter_backend", "memory") or "memory").strip().lower()
    if limiter_backend != "redis":
        raise SettingsValidationError(
            "VIOLA_RATE_LIMITER_BACKEND=redis is required when app_surface=cloud and VIOLA_ENV != dev. "
            "Cloud abuse limiters must use shared Redis state."
        )


def _validate_plan_limiter_backend(config: AppConfig) -> None:
    backend = str(getattr(config, "plan_limiter_backend", "sqlite")).strip().lower()
    if backend not in {"postgres", "sqlite"}:
        raise SettingsValidationError("VIOLA_PLAN_LIMITER_BACKEND must be 'postgres' or 'sqlite'")
    config.plan_limiter_backend = backend


def _validate_sentry_settings(config: AppConfig) -> None:
    try:
        traces_sample_rate = float(getattr(config, "sentry_traces_sample_rate", 0.0))
    except (TypeError, ValueError):
        raise SettingsValidationError("VIOLA_SENTRY_TRACES_SAMPLE_RATE must be a number between 0.0 and 1.0") from None
    if traces_sample_rate < 0.0 or traces_sample_rate > 1.0:
        raise SettingsValidationError("VIOLA_SENTRY_TRACES_SAMPLE_RATE must be between 0.0 and 1.0")
    config.sentry_traces_sample_rate = traces_sample_rate


def _validate_default_volume(config: AppConfig) -> None:
    """Validate default volume setting."""
    logger = get_logger("viola.config")
    try:
        default_volume = int(config.default_volume)
        if default_volume < 0:
            default_volume = 0
        if default_volume > 100:
            default_volume = 100
        config.default_volume = default_volume
    except Exception as e:
        logger.warning(
            "Invalid default_volume '%s', using default 50: %s",
            config.default_volume,
            e,
        )
        config.default_volume = 50


def _setup_directories(config: AppConfig) -> None:
    """Create necessary directories."""
    logger = get_logger("viola.config")
    for path_str in (config.data_dir, config.temp_dir):
        if path_str and not os.path.exists(path_str):
            try:
                os.makedirs(path_str, exist_ok=True)
            except Exception as e:
                # Log warning - directory creation failed but app can still function
                # with reduced functionality (e.g., no persistent cache)
                logger.warning("Failed to create directory '%s': %s", path_str, e)


def _setup_wake_keyword_path(config: AppConfig) -> None:
    """Set up wake keyword path with validation."""
    if config.wake_keyword_path:
        wake_path = os.path.expanduser(config.wake_keyword_path)
        if not os.path.exists(wake_path):
            raise SettingsValidationError(f"Wake keyword file not found: {wake_path}")
        config.wake_keyword_path = wake_path


def _setup_calendar_timezone(config: AppConfig) -> None:
    """Set up calendar timezone."""
    config.calendar_timezone = _resolve_calendar_timezone(config.calendar_timezone)


def _validate_build_profile(config: AppConfig) -> None:
    """Validate build profile setting."""
    allowed_profiles: set[str] = {"personal", "beta", "monetized"}
    raw_profile = str(config.build_profile or "personal").strip().lower()
    if raw_profile not in allowed_profiles:
        raise SettingsValidationError("build_profile must be one of: personal, beta, monetized")
    config.build_profile = cast(BuildProfile, raw_profile)


_PLACEHOLDER_PATTERNS = (
    "sk-your-",
    "sk-example",
    "change_me",
    "dev_secret_change_in_production",
    "your-key-here",
    "your_jwt_secret",
    "replace-me",
    "replace_me",
)


def _reject_placeholder_secrets(config: AppConfig) -> None:
    """Reject known placeholder values for critical secrets.

    Prevents accidental deployment with .env.example defaults that provide
    no real security. Matches case-insensitively against common placeholder
    stems (sk-example, sk-your, CHANGE_ME, REPLACE_ME, etc.).
    """
    checks = [
        ("openai_api_key", config.openai_api_key),
        ("jwt_secret", config.jwt_secret),
        ("jwt_secret_previous", config.jwt_secret_previous),
    ]
    for name, value in checks:
        if not value:
            continue
        val_lower = value.lower()
        for pattern in _PLACEHOLDER_PATTERNS:
            if pattern in val_lower:
                raise SettingsValidationError(
                    "%s contains a placeholder value ('%s'). "
                    "Set a real secret before starting Viola." % (name, pattern)
                )


def _parse_jwt_previous_expires_at(raw_value: str) -> datetime.datetime:
    """Parse VIOLA_JWT_SECRET_PREVIOUS_EXPIRES_AT as an aware UTC timestamp."""
    raw = raw_value.strip()
    if not raw:
        raise SettingsValidationError("VIOLA_JWT_SECRET_PREVIOUS_EXPIRES_AT must be a non-empty ISO 8601 timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise SettingsValidationError(
            "VIOLA_JWT_SECRET_PREVIOUS_EXPIRES_AT must be an ISO 8601 timestamp, " "for example 2026-05-16T17:00:00Z"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _validate_jwt_secret_previous_overlap(config: AppConfig) -> None:
    """Validate the bounded overlap window for JWT-secret rotation."""
    current = (config.jwt_secret or "").strip()
    previous = config.jwt_secret_previous.strip() if isinstance(config.jwt_secret_previous, str) else ""
    if not previous or previous == current:
        return

    expires_raw = (
        config.jwt_secret_previous_expires_at.strip() if isinstance(config.jwt_secret_previous_expires_at, str) else ""
    )
    if not expires_raw:
        _logger.warning(
            "VIOLA_JWT_SECRET_PREVIOUS is configured without VIOLA_JWT_SECRET_PREVIOUS_EXPIRES_AT. "
            "Set a bounded ISO 8601 deadline; the recommended rotation overlap is 7 days."
        )
        return

    expires_at = _parse_jwt_previous_expires_at(expires_raw)
    if expires_at <= datetime.datetime.now(datetime.UTC):
        raise SettingsValidationError(
            "VIOLA_JWT_SECRET_PREVIOUS_EXPIRES_AT is in the past. "
            "Remove VIOLA_JWT_SECRET_PREVIOUS or set a future bounded overlap deadline."
        )


def _ensure_admin_token(config: AppConfig) -> None:
    """Keep admin APIs fail-closed when no explicit token is configured.

    Admin actions must use the operator-provided VIOLA_ADMIN_TOKEN.  Missing
    tokens intentionally reject all admin API requests.
    """
    if config.admin_token:
        return

    _logger.info(
        "VIOLA_ADMIN_TOKEN is not configured; admin API requests will fail closed.",
    )


def _validate_test_mode(config: AppConfig) -> None:
    """Block test_mode in production builds and warn in development."""
    if not config.test_mode:
        return

    # Block test_mode in production / cloud builds
    if config.build_profile in ("monetized", "cloud"):
        _logger.critical(
            "VIOLA_TEST_MODE=1 is set in a '%s' build — forcibly disabling. " "Test mode must NEVER reach production.",
            config.build_profile,
        )
        config.test_mode = False
        return

    # Non-production build: warn loudly so developers notice
    _logger.warning(
        "TEST MODE ACTIVE — VLC playback disabled, OAuth verification skipped, "
        "shell allowlist bypassed. Unset VIOLA_TEST_MODE to disable.",
    )


def _is_loopback_bind_host(host: str | None) -> bool:
    return (host or "").strip().lower() in {"127.0.0.1", "::1", "localhost"}


def _security_auth_enabled_from_env() -> bool:
    """Resolve the effective local security-auth posture from environment."""
    auth_enabled_env = env.get("VIOLA_SECURITY_AUTH_ENABLED")
    if auth_enabled_env is None:
        return env.get("VIOLA_SECURITY_DEV_MODE", "false").lower() != "true"
    return auth_enabled_env.lower() == "true"


def _validate_desktop_network_exposure(config: AppConfig) -> None:
    """Fail closed if desktop binds beyond loopback while security auth is off."""
    if str(getattr(config, "app_surface", "desktop")).lower() != "desktop":
        return

    if _is_loopback_bind_host(config.api_host):
        return

    if _security_auth_enabled_from_env():
        return

    if config.pytest_in_progress:
        _logger.warning(
            "Skipping desktop LAN/auth startup guard under pytest for api_host=%s",
            config.api_host,
        )
        return

    if env.get_bool("VIOLA_ALLOW_INSECURE_DESKTOP_BIND", default=False):
        _logger.warning(
            "VIOLA_ALLOW_INSECURE_DESKTOP_BIND=1 bypassed the desktop LAN/auth startup guard for api_host=%s",
            config.api_host,
        )
        return

    raise SettingsValidationError(
        "Desktop API cannot bind to %s while VIOLA security auth is disabled. "
        "Enable VIOLA_SECURITY_AUTH_ENABLED=true (or unset VIOLA_SECURITY_DEV_MODE), "
        "or bind VIOLA_API_HOST to 127.0.0.1/localhost. "
        "Multiroom LAN exposure requires authentication." % config.api_host
    )


def apply_post_init_validation(config: AppConfig) -> None:
    """Apply all post-initialization validation and setup."""
    _process_environment_variables(config)
    _process_legacy_aliases(config)
    _process_deprecated_flags(config)
    _process_api_keys(config)
    _validate_api_port(config)
    _validate_wake_sensitivity(config)
    _validate_wake_gain_scheduler(config)
    _validate_stt_latency_settings(config)
    _validate_tts_voice_settings(config)
    _validate_agent_timeouts(config)
    _validate_phone_dial_timeout_override(config)
    _validate_default_volume(config)
    _validate_build_profile(config)
    _validate_test_mode(config)
    _validate_desktop_network_exposure(config)
    _validate_wake_data_upload_endpoint(config)
    _validate_jwt_secret_previous_overlap(config)
    _reject_placeholder_secrets(config)
    _validate_cloud_database(config)
    _validate_cloud_public_abuse_dependencies(config)
    _validate_plan_limiter_backend(config)
    _validate_sentry_settings(config)
    _setup_directories(config)
    _setup_wake_keyword_path(config)
    _setup_calendar_timezone(config)
    _ensure_admin_token(config)
