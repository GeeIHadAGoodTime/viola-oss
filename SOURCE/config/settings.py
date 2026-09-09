"""
Consolidated configuration module for NOVVIOLA.

This module is the single source of truth for application settings. It:

- Loads environment overrides (including `.env`) once on import.
- Exposes the `AppConfig` dataclass via `get_settings()` / `settings`.
- Provides helpers for secret validation and sanitized public payloads.
The legacy logic previously living in `config/__init__.py` now resides here.

All settings are accessed directly via ``settings.field_name``.

Key segregation (AppConfig vs SettingsManager)
----------------------------------------------
Two config systems coexist with distinct roles:

  AppConfig (.env -> config/settings.py)
    Secrets and infrastructure: API keys, tokens, DB URLs, ports, hosts,
    feature gates (wake_enabled, browser_provider_enabled).

  SettingsManager (settings.json -> ui/settings_manager.py)
    Runtime user preferences: weather_location, voice mode, theme, volume,
    language, etc.

Overlapping keys explicitly audited and placed in AppConfig
(infrastructure/secret), NOT SettingsManager:
  - wake_enabled, wake_engine     : deployment feature gates
  - browser_provider_enabled      : build-profile feature gate
  - disable_device_discovery      : infrastructure toggle
  - tts_backend, stt_backend      : infrastructure / model selection
  - gpt_model, llm_backend        : infrastructure / provider selection
    (``gpt_model`` remains the low-level AppConfig/env field; public settings
    and HTTP payloads use ``llm_model``)
  - default_volume                : initial seed only; runtime value lives in SettingsManager
  - weather_location              : seed default; runtime source of truth is SettingsManager

See docs/CONFIG_AUDIT_FINDINGS.md for full audit and migration plan.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from config.defaults import (
    COMPUTER_USE_LOG_WINDOW_TITLES_ENABLED_DEFAULT,
    COMPUTER_USE_MAX_CHARS_PER_TYPE_DEFAULT,
    COMPUTER_USE_SCREENSHOT_FORMAT_DEFAULT,
    COMPUTER_USE_SCREENSHOT_QUALITY_DEFAULT,
    DEFAULT_LLM_FALLBACK_CHAIN,
    DEFAULT_LLM_FALLBACK_ENABLED,
    DEFAULT_LOCAL_LLM_ENABLED,
    DEFAULT_LOCAL_LLM_MODEL,
    DEFAULT_VOLUME,
    DEFAULT_WAKE_SENSITIVITY,
    ENABLE_GPT_DEFAULT,
)
from core.constants import (
    DEFAULT_API_PORT,
    DEFAULT_CORS_ORIGIN,
    HUB_BUFFER_DEFAULT_MS,
    LOCALHOST,
    LOCALHOST_NAME,
    OLLAMA_DEFAULT_BASE_URL,
    SAMPLE_RATE_16K,
    VIOLA_VERSION,
)
from core.logging_config import get_logger

from . import env

DOTENV_OVERRIDE_KEYS_ENV = "VIOLA_DOTENV_OVERRIDE_KEYS"


def _default_data_dir() -> Path:
    """Platform-aware default data directory (lazy import to avoid circular deps)."""
    from core.platform import get_data_dir

    return get_data_dir()


def _default_cache_dir() -> Path:
    """Platform-aware default cache directory (lazy import to avoid circular deps)."""
    from core.platform import get_cache_dir

    return get_cache_dir()


# --------------------------------------------------------------------------- #
# Environment loading
# --------------------------------------------------------------------------- #


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


def _dotenv_override_keys() -> set[str]:
    raw = os.environ.get(DOTENV_OVERRIDE_KEYS_ENV, "")
    return {item.strip() for item in raw.replace(";", ",").replace(" ", ",").split(",") if item.strip()}


def _load_env_file() -> None:
    """Load a local .env file into the process environment if present.

    VIOLA_-prefixed variables from .env ALWAYS override existing env vars
    so that explicit per-project configuration takes precedence over stale
    system-level environment variables (a common source of hard-to-debug
    configuration bugs on Windows).

    Non-VIOLA variables follow standard .env semantics: they are only set
    if not already present in the environment.
    """
    env_path = Path(".env")
    if not env_path.exists():
        return

    override_keys = _dotenv_override_keys()
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            if key == DOTENV_OVERRIDE_KEYS_ENV or key in override_keys:
                continue
            # VIOLA_ vars: .env always wins (prevents stale system env bugs)
            if key.startswith("VIOLA_") or key not in os.environ:
                os.environ[key] = value
    except Exception as exc:  # pragma: no cover - defensive
        # Use stdlib logging to avoid circular import (this runs during module init)
        logging.warning("Failed to load .env: %s", exc)


_load_env_file()


# --------------------------------------------------------------------------- #
# Configuration dataclass and helpers
# --------------------------------------------------------------------------- #


class SettingsValidationError(ValueError):
    """Raised when settings configuration cannot be constructed or validated.

    Note: This is distinct from core.exceptions.ConfigurationError which is the
    canonical configuration error for the ViolaError hierarchy. This class is
    used specifically for settings initialization/validation in the config module.
    """


EnvName = Literal["dev", "prod", "test"]
BuildProfile = Literal["personal", "beta", "monetized"]


def _default_cors() -> list[str]:
    """Default CORS allowlist — localhost only.

    To allow LAN devices (phones, tablets), set VIOLA_CORS_ORIGINS to a
    comma-separated list of origins, e.g.:
        VIOLA_CORS_ORIGINS=http://192.168.1.100:8756,http://192.168.1.101:8756
    """
    return [
        DEFAULT_CORS_ORIGIN,
        f"http://{LOCALHOST}",
        f"http://{LOCALHOST}:{DEFAULT_API_PORT}",
        f"http://{LOCALHOST_NAME}",
        f"http://{LOCALHOST_NAME}:{DEFAULT_API_PORT}",
        f"https://{LOCALHOST}:{DEFAULT_API_PORT}",
        f"https://{LOCALHOST_NAME}:{DEFAULT_API_PORT}",
    ]


def _coerce_value(example: Any, raw: str) -> Any:
    """
    Best-effort type cast based on the current default value's type.
    We keep this deliberately forgiving: if coercion fails, fall back to the
    current example value (never the raw string).
    """

    if isinstance(example, bool):
        return _parse_bool(raw)

    if isinstance(example, int):
        try:
            return int(raw)
        except Exception as e:
            logging.debug("Int coercion failed for %r, using default: %s", raw, e)
            return example

    if isinstance(example, float):
        try:
            return float(raw)
        except Exception as e:
            logging.debug("Float coercion failed for %r, using default: %s", raw, e)
            return example

    if isinstance(example, list):
        if example and isinstance(example[0], int):
            # Parse as list[int] for fields expecting integers
            out_int: list[int] = []
            for part in str(raw).split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    out_int.append(int(part))
                except Exception as e:
                    logging.debug(
                        "Failed to parse int from '%s' in comma-separated list (non-critical): %s",
                        part,
                        e,
                    )
                    continue
            return out_int if out_int else example
        # Parse as list[str] for other list types
        out_str: list[str] = [p.strip() for p in str(raw).split(",") if p.strip()]
        return out_str if out_str else example

    if isinstance(example, dict):
        try:
            parsed = json.loads(raw)
        except Exception as e:
            logging.debug("Dict coercion failed for %r, using default: %s", raw, e)
            return example
        return parsed if isinstance(parsed, dict) else example

    return str(raw)


def _apply_environment_overrides(config: object) -> None:
    """Apply ``VIOLA_<FIELD_NAME>`` environment overrides to AppConfig fields."""
    prefix = "VIOLA_"
    for f in fields(config):
        name = f.name
        env_key = prefix + name.upper()
        if env_key in os.environ:
            current_val = getattr(config, name)
            setattr(config, name, _coerce_value(current_val, os.environ[env_key]))


def _apply_inbox_worker_environment_overrides(config: object) -> None:
    """Read the ``VIOLA_INBOX_WORKER_*`` env vars onto the ``inbox_worker_*``
    AppConfig fields (the agentic-inbox worker's Cloudflare Access service token,
    base URL, and mailbox).

    (A throwaway validation instance once used per-instance env aliases; it was
    retired 2026-06-12 and those aliases removed — the canonical
    ``VIOLA_INBOX_WORKER_*`` names are the only form now.)
    """
    fields = (
        "inbox_worker_cf_client_id",
        "inbox_worker_cf_client_secret",
        "inbox_worker_cf_access_aud",
        "inbox_worker_base_url",
        "inbox_worker_mailbox",
    )
    for name in fields:
        env_key = "VIOLA_" + name.upper()
        if env_key in os.environ:
            current_val = getattr(config, name)
            setattr(config, name, _coerce_value(current_val, os.environ[env_key]))


def _apply_sentry_environment_overrides(config: object) -> None:
    """Apply Sentry-specific env aliases without using settings.json."""
    aliases = {
        "sentry_base_url": ("SENTRY_API_BASE_URL",),
        "sentry_org_slug": ("SENTRY_ORGANIZATION_SLUG", "SENTRY_ORG"),
    }
    for f in fields(config):
        name = f.name
        if not name.startswith("sentry_"):
            continue
        sentry_key = "SENTRY_" + name.removeprefix("sentry_").upper()
        env_keys = ("VIOLA_" + name.upper(), sentry_key, *aliases.get(name, ()))
        for env_key in env_keys:
            if env_key in os.environ:
                current_val = getattr(config, name)
                setattr(config, name, _coerce_value(current_val, os.environ[env_key]))
                break


# ===========================================================================
# Domain sub-config view classes
#
# These are *views* over the flat AppConfig fields — they hold a reference
# back to the parent AppConfig so reads and writes are always in sync.
# They are constructed lazily the first time ``settings.wake`` etc. is
# accessed and cached on the AppConfig instance.
#
# Design notes
# ------------
# * No field duplication: sub-configs contain NO data of their own; every
#   attribute access delegates to the parent AppConfig via a stored ref.
# * Read/write symmetry: setting ``settings.wake.wake_enabled = True``
#   immediately updates ``settings.wake_enabled`` because they share the
#   same underlying object.
# * Zero backward-compat breakage: all existing ``settings.<field>``
#   call-sites continue to work without modification.
# ===========================================================================


@dataclass
class AppConfig:
    # --- Core ---
    env: EnvName = "dev"
    dev_mode: bool = False
    app_name: str = "Viola"
    build_profile: BuildProfile = "personal"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    data_dir: str = field(default_factory=lambda: str(_default_data_dir()))
    temp_dir: str = field(default_factory=lambda: str(_default_data_dir() / "tmp"))

    # --- Memory ---
    auto_memory_enabled: bool = True
    auto_memory_directory: str | None = None
    auto_memory_project_scope_enabled: bool = False
    auto_memory_side_query_enabled: bool = True
    # Latency lane B (#465): the memory side-query is fired as early as the
    # user text is available (ai_controller.process_request entry) so its
    # network wait overlaps the pre-context glue instead of blocking the turn
    # at context-build.
    #
    # TTFT cut (#2605/#531): the consume of that early-fire is bounded by
    # ``auto_memory_side_query_max_block_ms`` on the DEFAULT path -- TTFT must
    # not wait on the ~1.3 s side-query LLM round trip. When the selection lands
    # within the budget (fast/cached turns) the model gets the LLM selection at
    # zero added latency; when it does not, this turn's memory is surfaced via a
    # deterministic local relevance ranker (no network, single-digit ms) so the
    # model never loses this turn's relevant topic bodies to buy latency.
    #
    # ``auto_memory_side_query_nonblocking`` (default OFF, founder-gated flip)
    # is the one genuine model-visible trade: when ON, a side-query still in
    # flight past the budget drops to the always-on memory manifest ONLY for
    # this turn -- the model does NOT see this turn's selected topic bodies (no
    # local-ranker backstop). Infrastructure/behavior config (AppConfig), NOT a
    # user preference; never in settings.json.
    auto_memory_side_query_nonblocking: bool = False
    auto_memory_side_query_max_block_ms: int = 150
    auto_memory_extraction_enabled: bool = True
    auto_memory_dream_enabled: bool = True
    auto_memory_dream_min_hours: float = 24.0
    auto_memory_dream_min_sessions: int = 5

    # --- HTTP API / UI ---
    # Default binds loopback only for security. The multiroom hub requires
    # LAN binding — when VIOLA_ENABLE_MULTIROOM=1 the validator in
    # settings_validation.py auto-promotes to BIND_ALL_INTERFACES unless the
    # user has explicitly set VIOLA_API_HOST. Override with
    # VIOLA_API_HOST=0.0.0.0 to expose on LAN, or front with a reverse proxy
    # for controlled exposure.
    api_host: str = LOCALHOST
    api_port: int = DEFAULT_API_PORT
    cors_origins: list[str] = field(default_factory=_default_cors)
    max_ws_connections_per_ip: int = 10

    # --- Audio I/O ---
    input_device: str | None = None
    output_device: str | None = None
    sample_rate: int = SAMPLE_RATE_16K

    # --- TTS ---
    tts_enabled: bool = True
    tts_backend: str = "kokoro"  # kokoro, pyttsx3
    tts_voice: str = "default"
    tts_rate: int = 150  # WPM
    tts_volume: int = 80  # 0-100
    # Case-insensitive whole-word text respellings before phonemization,
    # e.g. {"Jihad": "Jee hahd"}. User overrides win over built-in dictionaries.
    tts_pronunciation_overrides: dict[str, str] = field(default_factory=dict)
    tts_acronym_dict_enabled: bool = True
    tts_brand_dict_enabled: bool = True
    tts_prosody_hints_enabled: bool = True
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    quiet_hours_timezone: str = "auto"
    tts_kokoro_model_path: str = "models/tts/kokoro-v1.0.onnx"
    tts_kokoro_voices_path: str = "models/tts/voices-v1.0.bin"
    tts_kokoro_voice: str = "af_heart"
    tts_opener_cache_enabled: bool = True
    tts_opener_cache_variants: int = 5
    tts_post_fx_enabled: bool = True
    tts_loudness_target_lufs: float = -16.0
    tts_voice_blend: list[list[Any]] | None = field(default_factory=lambda: [["af_river", 0.5], ["af_alloy", 0.5]])
    tts_speed_jitter_pct: float = 0.02
    tts_intersentence_gap_ms: int = 180

    # --- STT ---
    stt_backend: Literal["whisper_local", "whisper_api", "none"] = "whisper_local"
    whisper_model: Literal[
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large",
        "large-v2",
    ] = "tiny.en"
    # Use CPU by default to avoid CUDA DLL issues (cublasLt64_12.dll missing)
    # Users with proper CUDA setup can override with VIOLA_WHISPER_DEVICE=cuda
    whisper_device: Literal["cpu", "cuda", "auto"] = "cpu"
    stt_latency_warn_seconds: float = 4.0
    stt_latency_alert_seconds: float = 8.0
    stt_language_hint_ttl_seconds: int = 1800
    # Use int8 for CPU compatibility - float16 requires CUDA
    stt_compute_type: str = "int8"
    enable_audio_preprocessing: bool = True

    # --- Wake word ---
    wake_enabled: bool = False
    wake_engine: Literal["violawake", "none"] = "none"
    wake_inference_backend: Literal["auto", "onnx", "pytorch"] = "auto"
    wake_keyword_path: str | None = None
    # Default sensitivity from config/defaults.py (0.7 = moderate-high sensitivity, threshold ~0.43)
    # Higher sensitivity makes wake word easier to trigger
    # Users can lower this if they get too many false positives
    wake_sensitivity: float = DEFAULT_WAKE_SENSITIVITY
    wake_words: list[str] = field(default_factory=lambda: ["viola"])
    wake_gain_scheduler_enabled: bool = True
    wake_gain_scheduler_interval_minutes: int = 45
    wake_gain_scheduler_profiles: list[dict[str, Any]] = field(default_factory=list)
    wake_gain_history_retention: int = 180
    # NOTE: wake_noise_* configs removed - they were unused in production code

    # --- Wake word AEC (Acoustic Echo Cancellation) ---
    # Enable AEC to improve wake word detection during music/TTS playback
    # AEC uses loopback audio from speaker to cancel echo in microphone input
    # Falls back to NoOp if pyaec/speex not installed (graceful degradation)
    wake_aec_enabled: bool = True
    wake_aec_backend: Literal["auto", "viola", "pyaec", "speex", "noop"] = "auto"
    wake_aec_filter_length_ms: int = 200  # Echo tail length (50-500ms)
    wake_aec_delay_ms: int = 50  # Speaker-to-mic delay compensation
    wake_aec_denoise_enabled: bool = True  # Also apply noise suppression (Speex only)
    wake_aec_metrics_enabled: bool = False  # Enable per-frame AEC metrics logging

    # --- Wake Decision Policy (layered defenses) ---
    # Echo/Correlation Gating
    wake_loopback_rms_threshold: float = (
        1000.0  # Min loopback RMS to consider gating (increased from 500 for better music filtering)
    )
    wake_correlation_threshold: float = 0.4  # High correlation = playback leakage
    wake_consecutive_frames_required: int = 3  # Frames to engage gating
    wake_playback_threshold_boost_factor: float = 1.5  # Threshold multiplier during playback

    # VAD (Voice Activity Detection) Gating
    wake_enable_vad_gate: bool = True  # Use VAD as first-line filter during playback
    wake_vad_min_confidence_playback: float = (
        0.5  # Min VAD confidence during playback (increased from 0.3 for better music rejection)
    )
    wake_vad_min_confidence_loud_music: float = 0.7  # Higher threshold for loud music (volume > 70%)
    wake_vad_extreme_score_bypass: float = 0.95  # Score that bypasses VAD gate
    wake_enable_baseline_vad: bool = True  # Baseline VAD check even during idle (prevents false wakes on silence)
    wake_vad_min_confidence_idle: float = 0.25  # Lower threshold when idle, but still require some speech

    # Confirmation Window (two-stage confirmation)
    wake_enable_confirmation: bool = True  # Require confirmation during playback
    wake_confirmation_window_ms: int = 500  # Rolling window for confirmation (extended from 300 for stability)
    wake_confirmation_min_detections: int = (
        3  # Min detections in window to confirm (increased from 2 for noise rejection)
    )
    wake_confirmation_required_during_playback: bool = True

    # Feature Flags
    wake_enable_echo_veto: bool = True  # Enable echo/correlation veto layer
    wake_enable_threshold_boost: bool = True  # Enable threshold boosting during playback

    # Audio Presence Detection (RMS-based, not state-based)
    # These thresholds determine when defense layers activate based on ACTUAL audio
    wake_playback_presence_rms_threshold: float = 150.0  # Min RMS to consider "audible playback"
    wake_echo_veto_rms_threshold: float = 500.0  # Higher threshold for echo veto specifically

    # Volume-Scaled Threshold Boost
    wake_volume_scaling_enabled: bool = True  # Enable volume-scaled threshold boosting
    wake_volume_max_boost_factor: float = 1.5  # Max boost at 100% volume

    # SNR-Based Threshold Boost
    wake_snr_scaling_enabled: bool = True  # Enable SNR-based threshold boosting
    wake_low_snr_threshold_db: float = 10.0  # SNR below this is "low"
    wake_low_snr_boost: float = 0.2  # Additional boost factor for low SNR

    # Barge-In Detection (interruption during playback)
    wake_barge_in_enabled: bool = True  # Enable barge-in detection
    wake_barge_in_speech_ratio_threshold: float = 0.7  # Speech ratio threshold for barge-in detection
    wake_barge_in_max_threshold_reduction: float = 0.15  # Maximum threshold reduction during barge-in

    # Model versioning (retraining pipeline)
    wake_model_version: int | None = None  # None = use latest, or specify version number
    # Debug: bypass all policy layers (VIOLA_FORCE_WAKE=1)
    force_wake: bool = False

    # Contributor Mode (dedicated sample collection)
    contributor_mode_enabled: bool = False
    contributor_mode_timeout_minutes: int = 15
    contributor_mode_near_miss_threshold: float = 0.4
    contributor_mode_server_url: str | None = None  # Optional, for upload
    # Training telemetry upload endpoint
    training_upload_endpoint: str | None = None

    # --- Wake word data collection ---
    wake_data_collection_enabled: bool = False
    wake_data_contribute: bool = False
    wake_data_near_miss_low: float = 0.3
    wake_data_near_miss_high: float = 0.7
    wake_data_near_miss_cooldown_sec: float = 30.0
    wake_data_silence_threshold_dbfs: float = -40.0
    wake_data_retention_days: int = 30
    wake_data_near_miss_cap_mb: int = 500
    wake_data_total_cap_mb: int = 1024
    wake_data_upload_endpoint: str = ""
    wake_data_model_update_url: str = ""
    wake_data_auto_update: bool = False
    wake_data_max_daily_uploads: int = 50
    wake_data_max_outbox_size_mb: int = 200
    wake_data_max_model_download_mb: int = 50

    # --- Local music ---
    fuzzy_match_threshold: int = 60

    # --- Playback ---
    player_backend: Literal["vlc", "simple", "qt_media", "null"] = "simple"
    # LEGACY: YouTube scraping removed - these options are no-ops
    # Note: allow_youtube and related scraping knobs were removed.
    # YouTube playback now relies exclusively on linked providers.

    # YouTube Music fallback strategy (embedded, legacy_webview, external_browser)
    # "embedded" uses iframe with React mode support (preferred)
    # "legacy_webview" requires Qt WebEngine controller (fails without Qt)
    ytm_fallback_strategy: str = "embedded"

    # Queue persistence: DISABLED (Product Decision 2026-01-11)
    # Queue resets on app restart to prevent stale context feedback loops.
    # User favorites/ratings still persist (explicit user action).
    # Only volume is restored on startup.
    restore_queue_on_startup: bool = False

    # --- Feature toggles ---

    # --- Browser-native provider ---
    browser_provider_enabled: bool = False  # Feature flag — off by default
    preferred_music_provider: str = "youtube_iframe"  # Default: existing behavior

    # --- Browser-based YouTube search ---
    browser_search_enabled: bool = True  # Use hidden QWebEngineView for search (no API quota)
    browser_search_timeout_seconds: float = 10.0  # Page load + extraction timeout

    # --- Audio capture ---
    # CoreAudio capture on macOS (requires virtual loopback device)
    enable_coreaudio: bool = False

    # --- Playback feature flags ---
    playback_gapless_enabled: bool = True
    playback_artwork_sync_enabled: bool = True
    playback_hot_buffer_enabled: bool = True

    # --- Runtime / playback tuning ---
    default_volume: int = DEFAULT_VOLUME

    # --- Performance tuning ---
    # Performance profile (balanced, conservative, aggressive)
    perf_profile: str = "balanced"
    # Cache type override (memory, disk, hybrid)
    perf_cache_type: str | None = None
    # Max concurrent operations
    perf_max_concurrent: int | None = None
    # Freshness policy (strict, balanced, relaxed)
    perf_freshness_policy: str | None = None

    # --- Diagnostics ---
    diagnostics_disabled: bool = False
    diagnostics_disable_quick_events: bool = False
    coalesce_window_seconds: float = 30.0
    no_color: bool = False
    # Comma-separated list of capabilities to force-miss (testing/debugging)
    force_capability_missing: str = ""

    # Test / headless mode flags
    test_mode: bool = False
    # E2E/embedded-only mode (forces embedded backend for testing/development)
    embedded_only: bool = False
    # Developer mode (enables unsafe/experimental features)
    developer_mode: bool = False
    # Disable multiroom device discovery (set via VIOLA_DISABLE_DEVICE_DISCOVERY)
    disable_device_discovery: bool = False

    # --- Desktop computer-use controls (local desktop only; tier-gated at runtime) ---
    computer_use_app_whitelist: list[str] = field(default_factory=list)
    computer_use_screenshot_format: str = COMPUTER_USE_SCREENSHOT_FORMAT_DEFAULT
    computer_use_screenshot_quality: int = COMPUTER_USE_SCREENSHOT_QUALITY_DEFAULT
    computer_use_max_chars_per_type: int = COMPUTER_USE_MAX_CHARS_PER_TYPE_DEFAULT
    computer_use_log_window_titles_enabled: bool = COMPUTER_USE_LOG_WINDOW_TITLES_ENABLED_DEFAULT

    # Hub-as-spoke: play ChunkStamper audio through local speakers via sounddevice
    # (VIOLA_HUB_LOCAL_PLAYBACK).  Enabled by default.  Feedback loop risk is
    # eliminated: HubLocalPlayback runs in a subprocess whose PID is excluded
    # from ProcTap candidates, and provider muting uses JS/CDP callbacks
    # (not pycaw).  Activates on first spoke connect, deactivates on last
    # spoke disconnect.  Zero latency penalty for solo hub use.
    hub_local_playback: bool = (
        False  # DISABLED: subprocess WASAPI stream crashes parent (segfault in native Qt/Chromium thread)
    )
    # Hub local playback buffer delay in milliseconds (VIOLA_HUB_BUFFER_MS)
    hub_buffer_ms: int = HUB_BUFFER_DEFAULT_MS
    # Hub-local speaker delay in milliseconds (VIOLA_HUB_LOCAL_DELAY_MS).
    # Decoupled from hub_buffer_ms: the hub is on the same machine as the
    # stamper so it needs no WiFi jitter protection. 70ms absorbs OS
    # scheduling jitter while keeping hub/spoke offset small.
    # Remote spokes still use hub_buffer_ms (80ms) for steady-state sync.
    hub_local_delay_ms: int = 70

    # --- Runtime profile & Raspberry Pi / Lightweight mode ---
    runtime_profile: str = "auto"
    runtime_capabilities: dict[str, Any] = field(default_factory=dict)
    lightweight_mode: bool = False

    # --- Command Service Settings ---
    # Use unified LLM pipeline for command processing (default: true)
    use_pipeline: bool = True
    # Enable direct play commands (e.g., "play <file>")
    direct_play_enabled: bool = True
    # Media root directory for direct play
    direct_play_media_root: str | None = None
    # Allowed hosts for direct play (comma-separated)
    direct_play_allowed_hosts: list[str] = field(default_factory=list)

    # --- Security / secrets ---
    openai_api_key: str | None = None

    # --- AI / GPT Configuration ---
    # Low-level AppConfig/env model field. Public settings and HTTP payloads
    # expose llm_model instead; this remains as the internal compatibility
    # layer until the final alias removal.
    gpt_model: str = "gpt-5.4-mini"
    enable_gpt: bool = ENABLE_GPT_DEFAULT
    # LLM backend provider. Supports: openai, anthropic, google, ollama, openai_compatible
    llm_backend: str = "openai"
    ollama_base_url: str | None = None
    llm_fallback_enabled: bool = DEFAULT_LLM_FALLBACK_ENABLED
    llm_fallback_chain: list[str] = field(default_factory=lambda: list(DEFAULT_LLM_FALLBACK_CHAIN))
    local_llm_enabled: bool = DEFAULT_LOCAL_LLM_ENABLED
    local_llm_model: str = DEFAULT_LOCAL_LLM_MODEL
    local_llm_base_url: str = OLLAMA_DEFAULT_BASE_URL

    # Legacy LLM quota-accounting toggles. Daily request/monthly token quotas
    # are not enforced; managed spend caps are the pricing gate.
    llm_rate_limit_enabled: bool = True
    llm_rate_limit_free_daily: int = -1
    llm_rate_limit_paid_daily: int = -1
    llm_rate_limit_free_monthly_tokens: int = -1
    llm_rate_limit_paid_monthly_tokens: int = -1
    llm_max_tokens_cap: int = 150
    # Cost circuit breaker -- hard caps NOT bypassable by dev_mode
    llm_cost_per_minute_cap: int = 30  # max LLM calls per sliding 60s window
    llm_monthly_cost_cap_usd: float = 10.0  # monthly dollar cap (estimate)
    # F-008: Enable LLM usage tracking in audit pipeline (default True)
    llm_usage_tracking_enabled: bool = True

    @property
    def llm_rate_limit_premium_daily(self) -> int:
        """Backward-compatible alias for older premium-tier callers."""
        return self.llm_rate_limit_paid_daily

    @llm_rate_limit_premium_daily.setter
    def llm_rate_limit_premium_daily(self, value: int) -> None:
        self.llm_rate_limit_paid_daily = value

    @property
    def llm_rate_limit_premium_monthly_tokens(self) -> int:
        """Backward-compatible alias for older premium-tier callers."""
        return self.llm_rate_limit_paid_monthly_tokens

    @llm_rate_limit_premium_monthly_tokens.setter
    def llm_rate_limit_premium_monthly_tokens(self, value: int) -> None:
        self.llm_rate_limit_paid_monthly_tokens = value

    # Browser-search throttling for YouTube playback discovery; no Data API quota is used.
    youtube_search_rate_limit_per_user: int = 5
    youtube_search_rate_limit_window_seconds: int = 60

    # Additional LLM provider API keys
    anthropic_api_key: str | None = None
    google_api_key: str | None = None

    # --- Derived / runtime only (not env-overridable) ---
    calendar_timezone: str = "auto"

    # ==========================================================================
    # CLOUD BACKEND SETTINGS
    # ==========================================================================

    # --- Cloud Connection ---
    # Cloud backend URL. Defaults to the Viola SaaS endpoint so fresh
    # installer users get cloud-mode phone calls out of the box (no local
    # Telnyx config, no cloudflared tunnel, no per-user Cloudflare account).
    # Set to None or override via VIOLA_CLOUD_URL to disable cloud features
    # (BYOK / fully-local power users).
    cloud_url: str | None = "https://api.useviola.com"

    # --- Redis (optional shared ephemeral state) ---
    # When set, rate limiters, MFA sessions, and OAuth nonces use Redis
    # instead of in-memory dicts (required for multi-instance SaaS).
    # Format: redis://host:6379/0 or rediss://host:6379/0 (TLS)
    # When None: all components fall back to in-memory (fine for single-instance).
    redis_url: str | None = None
    # Rate limiter backend: memory for desktop/single-process, redis for cloud.
    # Set via VIOLA_RATE_LIMITER_BACKEND=redis in multi-instance deployments.
    rate_limiter_backend: str = "memory"
    # Fail mode when the Redis rate-limit STORE is unreachable (connection/DNS/
    # timeout error) after the in-limiter retries are exhausted.
    #   True  (default, availability-first): degrade gracefully. The IP limiter
    #         falls back to an in-memory per-instance sliding window (still
    #         bounded — NOT unlimited); the auth/phone counter limiters allow the
    #         request and log loudly + emit an abuse signal. A transient Redis/DNS
    #         blip becomes a brief no-shared-limit window instead of a total
    #         auth/phone OUTAGE (login 503, /api/phone/call 502, ws-ticket 429).
    #   False (security-first): fail CLOSED on the cloud surface — block the
    #         request (429/503) rather than serve it unlimited. Restores the
    #         pre-2026-07-01 posture; a Redis blip then amplifies into an outage.
    # Security tradeoff: fail-open RELAXES the shared DoS/brute-force control for
    # the duration of the store outage. It is the default because (a) the outage
    # window is transient, (b) the IP path stays bounded via in-memory fallback,
    # and (c) auth/phone paths retain independent guards (GoTrue's own throttles,
    # Postgres-backed per-number phone caps). Flip to false to prioritize the DoS
    # control over availability. Env: VIOLA_RATE_LIMIT_FAIL_OPEN=false
    rate_limit_fail_open: bool = True

    # --- Authentication ---
    auth_enabled: bool = False
    jwt_secret: str | None = None
    jwt_secret_previous: str = ""
    jwt_secret_previous_expires_at: str = ""
    web_push_vapid_public_key: str | None = None
    web_push_vapid_private_key: str | None = None
    web_push_vapid_subject: str | None = None

    # --- Shell Command Allowlist ---
    shell_additional_allowed: list[str] = field(default_factory=list)

    # --- COPPA Compliance ---
    coppa_minimum_age: int = 13

    # --- Session Security ---
    session_idle_timeout_hours: int = 72  # Revoke session after this many hours of inactivity
    session_enforce_ip_binding: bool = False  # Reject sessions used from a different IP
    session_access_token_minutes: int = 15  # Short-lived signed access token lifetime
    session_refresh_token_days: int = 30  # Rotating refresh token lifetime
    session_accept_legacy_opaque_tokens: bool = False  # Runtime auth rejects old long-lived bearer cookies

    # --- Login Brute-Force Protection ---
    # Per-IP: progressive backoff after failed login attempts
    auth_login_max_attempts_per_ip: int = 5  # Attempts before progressive delay kicks in
    auth_login_window_minutes: int = 15  # Sliding window for counting per-IP failures
    auth_login_block_threshold: int = 10  # Attempts before IP is blocked from auth endpoints
    auth_login_block_duration_minutes: int = 30  # How long to block an IP after threshold
    # Per-account: temporary lockout after repeated failures (regardless of IP)
    auth_account_lock_threshold: int = 10  # Failures against one account before lock
    auth_account_lock_duration_minutes: int = 15  # Lock duration
    auth_brute_force_persistent: bool = True  # SQLite-backed persistence (survives restarts)

    # --- Cloud CORS ---
    cloud_cors_origins: list[str] = field(default_factory=list)  # Empty = reject all cross-origin
    cloud_cors_allow_methods: list[str] = field(default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    cloud_cors_allow_headers: list[str] = field(
        default_factory=lambda: [
            "Content-Type",
            "Authorization",
            "apikey",
            "x-client-info",
            "X-API-Key",
            "X-Request-ID",
            "X-CSRF-Token",
            # Client's IANA zone, so wall-clock times resolve in the user's
            # timezone rather than the container's UTC (#3557).
            "X-Viola-Timezone",
        ]
    )
    # Deployment-level cloud feature gates. Defaults preserve the current
    # production route surface; operators can explicitly disable a surface to
    # skip its routes cleanly instead of relying on import-time failures.
    cloud_phone_routes_enabled: bool = True
    cloud_vision_routes_enabled: bool = True
    cloud_companion_routes_enabled: bool = True
    cloud_voice_stream_routes_enabled: bool = True

    # --- Self-hosted CalDAV calendar backend (cloud surface) ---
    # Viola-hosted Radicale (deploy/calendar) reached over the internal docker
    # network. All three must be set for cloud CalDAV auto-provisioning to
    # activate; unset (the default) leaves desktop behavior untouched. The
    # provision token is a secret: AppConfig/.env only, never settings.json.
    calendar_caldav_url: str | None = None
    calendar_caldav_provision_url: str | None = None
    calendar_caldav_provision_token: str | None = None

    # --- OAuth Callback ---
    # Dedicated HTTP-only port for OAuth callbacks (Google requires http:// for
    # localhost redirect URIs, but the main server runs HTTPS for spoke sync).
    oauth_callback_port: int = 8758

    # --- Google OAuth (Sign-in with Google) ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    google_restricted_features_enabled: bool = False

    # --- Apple OAuth (Sign in with Apple) ---
    apple_client_id: str | None = None
    apple_team_id: str | None = None
    apple_key_id: str | None = None
    apple_private_key_path: str | None = None
    apple_redirect_uri: str | None = None

    # --- Spotify OAuth ---
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    spotify_redirect_uri: str | None = None
    spotify_device_id: str | None = None
    spotify_refresh_token: str | None = None

    # --- Calendar Integration ---
    google_calendar_credentials_path: str | None = None
    icloud_username: str | None = None
    icloud_password: str | None = None

    # --- Weather Service ---
    weather_api_base: str | None = None
    weather_gfs_url: str = "http://weather-gfs:8080"
    weather_air_quality_airnow_enabled: bool = True
    weather_air_quality_airnow_reporting_area_url: str = "https://files.airnowtech.org/airnow/today/reportingarea.dat"
    weather_air_quality_airnow_cache_ttl_seconds: int = 1800
    weather_air_quality_airnow_max_distance_miles: float = 100.0
    searxng_url: str = ""
    web_read_default_chars: int = 12000
    web_read_max_chars: int = 20000
    browser_get_text_default_chars: int = 8000
    browser_get_text_max_chars: int = 20000
    openrouteservice_api_key: str | None = None
    usda_api_key: str | None = None
    # Weather location override (empty = auto-detect from server IP via wttr.in)
    weather_location: str = ""

    # --- Plugin System ---
    plugin_registry_url: str = ""  # Remote registry URL (empty = local only)

    # --- Messaging Channels ---
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_owner_chat_id: str | None = None

    # Slack is internal-pilot only — not in the Settings UI. Remains
    # behind VIOLA_EXPERIMENTAL_CHANNELS=1.
    slack_enabled: bool = False
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    # WhatsApp/Signal removed 2026-04-17 — product decision.

    # --- User Model (learned preferences) ---
    user_model_enabled: bool = True

    # --- Meta-Analysis (weekly self-review) ---
    meta_analysis_enabled: bool = False
    meta_analysis_day: str = "sunday"  # Day of week for scheduled analysis
    meta_analysis_hour: int = 22  # Hour (0-23) for scheduled analysis

    # --- Agent (Agentic Tool-Use Loop) ---
    agent_enabled: bool = True
    agent_timeout_seconds: float = 3600.0  # 1 hour wall-clock backstop.
    agent_tool_timeout_seconds: float = 180.0  # per-tool: browser, web, system calls can be slow

    # --- Orchestration (Multi-Agent Coordination) ---
    orchestration_enabled: bool = False
    orchestration_max_agents: int = 8
    orchestration_spawn_cooldown: int = 30
    orchestration_data_dir: str = field(default_factory=lambda: str(_default_data_dir() / "orchestration"))
    codex_enabled: bool = False
    codex_timeout: int = 300

    # --- Conversational Mode (multi-turn voice) ---
    conversational_mode_enabled: bool = True
    conversation_max_turns: int = 5
    conversation_follow_up_timeout: float = 2.5

    # --- Browser (Playwright for agent web tasks) ---
    # False = visible browser (user can see agent browsing and confirm payments)
    # True = headless (no visible window, faster for automated tasks)
    browser_headless: bool = False
    # Default geolocation for browser context (pre-grants geolocation permission
    # to avoid native Chromium popups the agent cannot interact with).
    browser_default_latitude: float = 43.0147
    browser_default_longitude: float = -87.9956

    # --- Browser Session Mode ---
    # "viola" (default): persistent Viola-owned Chromium profile, user logs in once
    # "ephemeral": temp profile wiped after each session
    browser_session_mode: str = "viola"

    # --- Agentic Browser Display (CDP) ---
    # Remote debugging port for Qt WebEngine CDP access.
    # Disabled by default because Chromium DevTools Protocol has no Viola
    # authentication layer. Set VIOLA_CDP_PORT or pass --remote-debugging-port
    # only for an explicit local debugging/agentic-browser session.
    cdp_port: int = 0

    # --- MCP (Model Context Protocol) Servers ---
    # JSON list of external MCP server configs. Each entry:
    # {"name": "...", "command": "...", "args": [...], "env": {...}}
    # Example: [{"name":"my-server","command":"npx","args":["my-mcp-server"]}]
    mcp_external_servers: str = ""  # JSON string, parsed at hub init time

    # Google Workspace MCP server (gemini-cli-extensions/workspace).
    # Path to workspace-server/dist/index.js. Gated by google_restricted_features_enabled.
    google_workspace_mcp_path: str = ""

    # --- Agent Email (IMAP/SMTP for agent email tools) ---
    email_imap_host: str | None = None
    email_imap_port: int = 993
    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_address: str | None = None
    email_password: str | None = None

    # --- Email Service ---
    resend_api_key: str | None = None
    resend_webhook_secret: str | None = None
    gotrue_webhook_secret: str | None = None
    email_from_address: str = "Viola <noreply@useviola.com>"
    # --- Support Inbox (agentic-inbox worker -> Postgres mirror -> batch reply) ---
    # Inbound customer email is received by the vendored agentic-inbox Cloudflare
    # Worker (infra/agentic-inbox/), whose bodies live in per-mailbox Durable
    # Objects + R2. backend/support_inbox_sync.py mirrors those messages into the
    # support_emails Postgres table (Viola one-source-of-truth + GDPR cascade).
    #
    # The worker HTTP/MCP API sits behind Cloudflare Access; we authenticate with
    # a service token (CF-Access-Client-Id / CF-Access-Client-Secret). Cloudflare
    # WAF blocks the default python-httpx UA, so the client sends a browser UA.
    #
    # Env (see _apply_inbox_worker_environment_overrides): the service-token
    # secret names are VIOLA_INBOX_WORKER_CF_CLIENT_ID / _CF_CLIENT_SECRET / _AUD.
    # Presence of both client-id and client-secret is the feature flag: absent
    # => the support inbox sync/erasure paths are cleanly disabled (fail-closed
    # registration in cloud_app).
    inbox_worker_base_url: str = "https://support-inbox.useviola.com"
    inbox_worker_mailbox: str = "hello@useviola.com"
    inbox_worker_cf_client_id: str | None = None
    inbox_worker_cf_client_secret: str | None = None
    inbox_worker_cf_access_aud: str | None = None
    # From address used when replying to support threads. Replies thread back to
    # the customer via In-Reply-To/References on the original Message-ID.
    support_from_address: str = "Viola Support <hello@useviola.com>"
    website_base_url: str = "https://useviola.com"
    api_base_url: str = "https://api.useviola.com"
    signup_email_delivery_required: bool = True

    # --- Operator Alerts ---
    # Operator destinations are infrastructure secrets. Do not put personal
    # phone numbers in scripts or docs; set VIOLA_OPS_PHONE in the environment.
    ops_phone: str | None = None
    ops_email: str | None = None
    ops_email_from: str = "Viola Ops <alerts@useviola.com>"
    cloudflare_email_account_id: str | None = None
    cloudflare_email_api_token: str | None = None
    cloudflare_email_to: str | None = None
    cloudflare_email_from: str | None = None
    pagerduty_routing_key: str | None = None
    pushover_app_token: str | None = None
    pushover_user_key: str | None = None
    pushover_priority: int = 1
    pushover_retry_seconds: int = 60
    pushover_expire_seconds: int = 1800

    # SMTP fallback
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    # --- Billing / Payments ---

    # BTCPay Server (Bitcoin/Lightning)
    btcpay_url: str | None = None
    btcpay_api_key: str | None = None
    btcpay_store_id: str | None = None
    btcpay_webhook_secret: str | None = None

    # Stripe subscription billing (credit card).
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_publishable_key: str | None = None
    stripe_price_pro_monthly: str | None = None
    stripe_price_pro_annual: str | None = None
    stripe_price_max_monthly: str | None = None
    stripe_price_max_annual: str | None = None
    stripe_price_extra_usage_10: str | None = None

    # Telnyx (AI phone calls)
    telnyx_api_key: str | None = None
    telnyx_phone_number: str | None = None
    telnyx_sip_connection_id: str | None = None
    telnyx_messaging_profile_id: str | None = None
    telnyx_webhook_public_key: str | None = None
    telnyx_public_ws_url: str | None = None
    telnyx_public_webhook_url: str | None = None
    telnyx_stream_shared_secret: str | None = None
    telnyx_media_allowlist: str | None = None
    # SMS sends use the approved toll-free number (+1-833-707-3533), separate
    # from ``telnyx_phone_number`` which is the 10DLC voice/call-control number.
    # Conflating them sends SMS from a long code with no A2P campaign — carriers
    # filter it. When unset, the SMS sender falls back to ``telnyx_phone_number``.
    telnyx_sms_from_number: str | None = None
    # PHONE-16: default to cloud-routed dialing. Fresh installer users hit
    # the SaaS path (api.useviola.com handles dial + Pipecat + Telnyx; the
    # user's machine only opens /ws/call-listen for audio). The local-mode
    # path is opt-in for BYOK power-users who want to dial through their
    # own Telnyx account from their own machine — that path requires
    # cloudflared on PATH for the quick-tunnel fallback OR a configured
    # TELNYX_PUBLIC_WS_URL.
    phone_mode: str = "cloud"  # "cloud" (SaaS server, default) or "local" (BYOK desktop)
    phone_global_max_concurrent: int = 25
    phone_call_hourly_limit: int = 10
    phone_call_daily_limit: int = 50
    phone_daily_burn_alarm_usd: float = 0.0  # 0 disables the phone spend alarm
    phone_sticky_routing_enabled: bool = False
    phone_multi_machine_deploy: bool = False
    # Telnyx Answering-Machine Detection. Default on (real users calling real
    # businesses benefit from voicemail detection). Set VIOLA_PHONE_AMD_ENABLED=false
    # to disable when premium AMD false-positives a human answerer.
    phone_amd_enabled: bool = True
    phone_tts_provider: str = "local"  # "local" (Kokoro cloud default), "piper", "espeak", or "elevenlabs"
    phone_tts_voice: str = ""  # Provider-specific override; empty keeps TelnyxConfig default.
    # Remote GPU voice endpoint (Runpod serverless) for phone STT/TTS.
    # Default OFF: local CPU models stay the production path until the cutover
    # gate passes. Same models/options run server-side; any remote failure or
    # timeout falls back to the local path per turn (telephony/remote_voice.py).
    phone_voice_remote_enabled: bool = False  # VIOLA_PHONE_VOICE_REMOTE_ENABLED
    phone_voice_remote_url: str = ""  # VIOLA_PHONE_VOICE_REMOTE_URL (https://api.runpod.ai/v2/<endpoint_id>)
    phone_voice_remote_api_key: str | None = None  # VIOLA_PHONE_VOICE_REMOTE_API_KEY (secret; .env only)
    phone_voice_remote_timeout_ms: int = 2500  # VIOLA_PHONE_VOICE_REMOTE_TIMEOUT_MS
    phone_voice_remote_cooldown_secs: int = 60  # VIOLA_PHONE_VOICE_REMOTE_COOLDOWN_SECS
    # Test-induction knob (VIOLA_PHONE_DIAL_TIMEOUT_OVERRIDE): overrides ONLY the
    # dial POST timeout in intent/tools/phone_call.py::_cloud_make_call so a live
    # proof run can force the httpx.TimeoutException -> "call_placed_connecting"
    # branch (item-5 grader-taxonomy proof). Harmless in production: unset (None)
    # keeps the default TIMEOUT_VERY_LONG byte-identical. The poll path is never
    # affected. Coerced/validated in settings_validation.py (env overrides land
    # as raw strings on None-default fields).
    phone_dial_timeout_override: float | None = None
    elevenlabs_api_key: str | None = None
    phone_per_number_cap_relief_destinations: list[str] = field(default_factory=list)
    phone_per_number_cap_relief_ack: str = ""
    # Dev/deployment overrides for user-preference fields. Empty string means
    # "no override; use SettingsManager / DEFAULT_SETTINGS". When set, these
    # win over the tracked settings.json so a dev box doesn't pollute the
    # installer-default committed state.
    ai_source_override: str = ""  # VIOLA_AI_SOURCE_OVERRIDE
    record_phone_calls_override: str = ""  # VIOLA_RECORD_PHONE_CALLS_OVERRIDE (true/false/empty)

    # PlanLimiter spend counters. Desktop keeps SQLite; hosted multi-instance
    # deployments must use Postgres so cloud spend caps are shared across machines.
    plan_limiter_backend: str = "sqlite"

    # --- External Production Monitoring ---
    external_monitor_expected: bool = False
    external_monitor_token: str | None = None
    external_monitor_stale_seconds: int = 1800
    external_monitor_badge_url: str | None = None
    incident_log_path: str = "data/incidents.json"

    # --- App Surface ---
    # "desktop" for the installed app, "cloud" for the hosted browser surface.
    app_surface: str = "desktop"

    # --- Admin Dashboard ---
    # Bearer token for remote admin dashboard API access.
    # Required when dashboard is exposed on the internet (cloud deployment).
    # Empty = all admin API requests rejected (fail-closed).
    # Set via VIOLA_ADMIN_TOKEN env var.
    admin_token: str = ""

    # --- Dashboard & Telemetry ---
    sentry_dsn: str | None = None
    sentry_environment: str | None = None
    sentry_traces_sample_rate: float = 0.0
    sentry_base_url: str = "https://sentry.useviola.com"
    sentry_org_slug: str | None = None
    sentry_project_slug: str | None = None
    sentry_project_token: str | None = None

    telemetry_enabled: bool = False  # Opt-in, default OFF
    telemetry_install_id: str = ""  # Random, user-rotatable
    # Explicit telemetry/funnel destination origin. Empty here does NOT mean
    # "disabled": telemetry.reporter.resolve_telemetry_server_url() falls back to
    # api_base_url (the cloud API origin) so an opted-in, consenting desktop
    # install always has a destination. Sending stays gated by opt-in + privacy
    # consent + kill-switch (should_send), so a resolved URL never sends without
    # consent. Set this only to override the destination (a self-hosted operator
    # pointing telemetry at their own ingest origin).
    telemetry_server_url: str = ""
    telemetry_send_interval_hours: int = 4  # Hours between automatic sends
    sentry_enabled: bool = True
    sentry_dsn: str = ""
    sentry_environment: str = ""
    sentry_traces_sample_rate: float = 0.0
    sentry_require_consent: bool = True

    # Anonymized diagnostic minimum (rides crashes + bug reports, opt-out
    # baseline). MASTER arming flag: default OFF so the whole feature ships dark
    # until the founder signs off on the disclosure UI. Env override:
    # VIOLA_DIAGNOSTICS_BASELINE_ENABLED. Read via diagnostics.diagnostic_consent.
    diagnostics_baseline_enabled: bool = False
    # Shared ingest credential the desktop sends as X-Viola-Diagnostics-Key to
    # the cloud relay (/v1/diagnostics/ingest). It authenticates a genuine Viola
    # client WITHOUT carrying user identity, so the anonymized baseline stays
    # anonymous end to end. Empty on the server = ingest endpoint disabled (503);
    # empty on the desktop = no relay. Provisioned only when the feature flips on.
    diagnostics_ingest_key: str = ""

    metrics_db_path: str = "metrics.db"  # SQLite metrics DB location

    # --- Sentry error reporting ---
    # Error reporting remains opt-in through the privacy consent layer. These
    # flags only enable the desktop Qt hand-off once Sentry is configured.
    sentry_dsn: str = ""
    sentry_environment: str = ""
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0
    sentry_qt_bridge_enabled: bool = False
    sentry_user_bug_report_enabled: bool = False

    release_manifest_dir: str = "updates/manifests"  # Stable/beta update manifest JSON files
    # Freeze rollout when the crash-rate increase exceeds the configured threshold
    release_manifest_path: str = ""
    release_freeze_window_hours: int = 1
    release_freeze_min_reports: int = 20
    release_freeze_crash_delta_pct: float = 2.0

    # --- Cloud Sync ---

    # ==========================================================================
    # END CLOUD BACKEND SETTINGS
    # ==========================================================================

    def __post_init__(self) -> None:
        from .settings_validation import apply_post_init_validation

        _apply_sentry_environment_overrides(self)
        _apply_inbox_worker_environment_overrides(self)
        apply_post_init_validation(self)

        # Developer passive collection override: set VIOLA_DEV_COLLECT=1 to
        # enable wake_data_collection_enabled without editing .env.  This lets
        # developers passively collect FP/TP clips during normal use.
        if os.environ.get("VIOLA_DEV_COLLECT", "").strip() == "1":
            object.__setattr__(self, "wake_data_collection_enabled", True)

    # ---------- convenience ----------
    @property
    def pytest_in_progress(self) -> bool:
        """
        Check if pytest is currently running.

        This centralizes the check for PYTEST_CURRENT_TEST environment variable
        to avoid scattered env.get calls throughout the codebase.

        Returns:
            bool: True if running under pytest, False otherwise
        """
        return os.environ.get("PYTEST_CURRENT_TEST") is not None

    @property
    def cache_dir(self) -> str:
        """
        Get cache directory path.

        Respects VIOLA_CACHE_DIR environment variable for test mode override.
        Falls back to platform-appropriate cache directory.

        Returns:
            str: Path to cache directory
        """
        env_cache_dir = os.environ.get("VIOLA_CACHE_DIR")
        if env_cache_dir:
            return env_cache_dir
        return str(_default_cache_dir())

    @property
    def unsafe_allow_youtube_streaming(self) -> bool:
        """
        **DEPRECATED/DEVELOPER-ONLY**: Allow unsafe YouTube Music streaming via VLC.

        **PRD VIOLATION**: This flag violates PRD v5.3 section 7.3.1 which states:
        "Hub MUST NOT use any stream extraction mechanism"

        **CANONICAL RULE**: YouTube Music MUST use embedded_webview mode only.
        This flag is kept for legacy compatibility but is effectively disabled.

        **Safety**: Even if set to True, this flag is only honored when:
        - VIOLA_DEVELOPER_MODE=1 environment variable is set, OR
        - VIOLA_TEST_MODE=1 is set, OR
        - Running in pytest context

        **Production**: This flag is ALWAYS False in production builds.
        Setting it in production has no effect - VLC is blocked regardless.

        **When False (default and production)**:
        - YouTube Music items MUST use embedded_webview mode
        - VLC streaming path is blocked for YouTube Music provider
        - Only official embed player (youtube.com/embed) is allowed

        **When True (dev/test only, with safety checks)**:
        - Allows legacy VLC streaming path for YouTube Music (for debugging)
        - Requires explicit developer mode environment variable
        - Logs warnings when used
        - May violate YouTube Terms of Service
        - Should NEVER be enabled in production builds

        Returns:
            bool: True if unsafe streaming is allowed (dev only), False otherwise (default)
        """
        # Check environment variable for dev override (default False)
        env_value = os.environ.get("VIOLA_UNSAFE_ALLOW_YOUTUBE_STREAMING", "").lower()
        return env_value in ("true", "1", "yes")

    @property
    def ssl_enabled(self) -> bool:
        """Check if SSL cert/key files exist for HTTPS mode."""
        _base = Path(__file__).resolve().parent.parent
        _cert = _base / "data" / "secrets" / "viola_cert.pem"
        _key = _base / "data" / "secrets" / "viola_key.pem"
        return _cert.exists() and _key.exists()

    @property
    def base_url(self) -> str:
        # Wildcard bind addresses can't be used for self-connections;
        # use the loopback address for internal health polling / OAuth.
        host = LOCALHOST if self.api_host in ("0.0.0.0", "::") else self.api_host  # nosec B104
        scheme = "https" if self.ssl_enabled else "http"
        return f"{scheme}://{host}:{self.api_port}"

    @property
    def is_monetized_build(self) -> bool:
        return self.build_profile == "monetized"

    @property
    def alert_webhook_url(self) -> str | None:
        """Webhook URL for external alert notifications.

        When set, triggered alerts are POSTed as JSON to this URL
        (e.g. a Slack incoming webhook).  Returns None if not configured.
        """
        value = os.environ.get("VIOLA_ALERT_WEBHOOK_URL", "").strip()
        return value if value else None

    @property
    def alert_telegram_bot_token(self) -> str | None:
        """Telegram Bot API token for alert delivery.

        Falls back to the general ``telegram_bot_token`` if the
        alert-specific ``VIOLA_ALERT_TELEGRAM_BOT_TOKEN`` is not set,
        allowing reuse of the same bot for both chat and alerts.
        """
        value = os.environ.get("VIOLA_ALERT_TELEGRAM_BOT_TOKEN", "").strip()
        if value:
            return value
        return self.telegram_bot_token

    @property
    def alert_telegram_chat_id(self) -> str | None:
        """Telegram chat ID for alert delivery.

        Falls back to ``telegram_owner_chat_id`` when the alert-specific
        env var is not set (typical single-admin setups).
        """
        value = os.environ.get("VIOLA_ALERT_TELEGRAM_CHAT_ID", "").strip()
        if value:
            return value
        return self.telegram_owner_chat_id

    @property
    def experimental_channels(self) -> bool:
        """Enable experimental messaging channels (Slack).

        These channels are implemented but not yet verified for production use.
        Set VIOLA_EXPERIMENTAL_CHANNELS=1 to enable them.
        """
        env_value = os.environ.get("VIOLA_EXPERIMENTAL_CHANNELS", "").lower()
        return env_value in ("true", "1", "yes")

    @property
    def enable_multiroom(self) -> bool:
        """Multi-room is permanently enabled."""
        return True

    def model_dump_sanitized(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            key = f.name
            try:
                value = getattr(self, key)
            except Exception as e:
                logging.debug("Failed to get attribute %s for sanitization: %s", key, e)
                continue
            if any(key.endswith(suffix) for suffix in ("_key", "_api_key", "_token", "_secret", "_password")):
                if value is not None:
                    value = "***REDACTED***"
            out[key] = value

        out["base_url"] = self.base_url
        out["platform"] = platform.platform()
        return out

    def model_dump_json_sanitized(self) -> str:
        return json.dumps(self.model_dump_sanitized(), ensure_ascii=False)


# Lazy logger initialization to avoid circular import
# (config.settings -> core.logging_config -> diagnostics.observability_logging -> config.settings)
_logger = None


def _get_logger():
    """Get or create the config logger lazily to avoid circular import."""
    global _logger
    if _logger is None:
        _logger = get_logger("viola.config")
        if not _logger.handlers:
            _logger.setLevel(getattr(logging, env.get("VIOLA_LOG_LEVEL", "INFO"), logging.INFO))
            handler = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            handler.setFormatter(formatter)
            _logger.addHandler(handler)
    return _logger


def _is_dev_context(cfg: AppConfig) -> bool:
    return bool(cfg.dev_mode or cfg.env in ("dev", "test") or cfg.test_mode)


def _missing_required_secrets(cfg: AppConfig) -> list[str]:
    missing: list[str] = []
    if cfg.enable_gpt and cfg.llm_backend == "openai" and not cfg.openai_api_key:
        missing.append("OPENAI_API_KEY")
    return missing


def ensure_required_secrets(cfg: AppConfig) -> None:
    """Warn and auto-disable GPT when required secrets are absent outside dev contexts."""
    if _is_dev_context(cfg):
        return
    missing = _missing_required_secrets(cfg)
    if missing:
        _get_logger().warning(
            "Missing secrets (%s) — disabling GPT features. Configure an API key to enable AI features.",
            ", ".join(missing),
        )
        cfg.enable_gpt = False


def get_missing_required_secrets(cfg: AppConfig | None = None) -> list[str]:
    """Return a list of missing secret identifiers for diagnostics."""
    if cfg is None:
        cfg = get_settings()
    return _missing_required_secrets(cfg)


def get_public_config_payload(cfg: AppConfig | None = None) -> dict[str, Any]:
    """
    Produce the strict public configuration snapshot for ``GET /config``.

    This endpoint is intentionally smaller than ``AppConfig``: it exposes only
    stable, non-secret boot metadata and never returns field names or values
    from secret-bearing infrastructure groups.
    """
    cfg = cfg or get_settings()
    return {
        "env": cfg.env,
        "version": VIOLA_VERSION,
        "build_profile": cfg.build_profile,
        "app_surface": str(getattr(cfg, "app_surface", "desktop")),
        "api_port": int(cfg.api_port),
    }


@lru_cache(maxsize=1)
def get_settings() -> AppConfig:
    cfg = AppConfig()
    try:
        safe_payload = {
            "env": cfg.env,
            "build_profile": cfg.build_profile,
            "api_host": cfg.api_host,
            "api_port": cfg.api_port,
            "base_url": cfg.base_url,
        }
        _get_logger().info("Config loaded: %s", json.dumps(safe_payload, ensure_ascii=True))
    except Exception as e:
        # Log serialization failure but don't block config loading
        _get_logger().debug("Could not serialize config for logging: %s", e)
    ensure_required_secrets(cfg)
    return cfg


settings: AppConfig = get_settings()


def get_runtime_base_url(settings_instance: AppConfig | None = None) -> str:
    """
    Get the runtime base URL for the Viola backend.

    This function returns the actual host:port where the backend is listening,
    which may differ from the requested port if port resolution selected a different port.

    The base URL is computed from:
    1. Runtime settings (api_host, api_port) which are updated by backend/ports.py
    2. Environment variables (VIOLA_BASE_URL, VIOLA_HOST, VIOLA_PORT) as fallback
    3. Settings instance passed as parameter (for testing)

    Args:
        settings_instance: Optional AppConfig instance (defaults to global settings)

    Returns:
        Base URL string like "http://127.0.0.1:8756" or "http://localhost:8756"

    Example:
        >>> base_url = get_runtime_base_url()
        >>> redirect_uri = f"{base_url}/v1/consent/callback"
    """
    # Check environment variable first (set by backend/ports.py after port resolution)
    env_base_url = env.get("VIOLA_BASE_URL")
    if env_base_url:
        return env_base_url

    # Use provided settings instance or global settings
    cfg = settings_instance or settings

    # Construct from runtime settings (which are updated by port resolution)
    host = getattr(cfg, "api_host", LOCALHOST)
    port = getattr(cfg, "api_port", DEFAULT_API_PORT)

    # Wildcard bind addresses can't be used for self-connections
    if host in ("0.0.0.0", "::"):  # nosec B104 - comparison, not a bind
        host = LOCALHOST

    scheme = "https" if getattr(cfg, "ssl_enabled", False) else "http"
    return f"{scheme}://{host}:{port}"


# ---------------------------------------------------------------------------
# F3: Hot-reload infrastructure
# ---------------------------------------------------------------------------

_SAFE_RELOAD_FIELDS: frozenset[str] = frozenset(
    {
        "log_level",
        "dev_mode",
        "no_color",
        "diagnostics_disabled",
        "diagnostics_disable_quick_events",
        "coalesce_window_seconds",
        "agent_timeout_seconds",
        "agent_tool_timeout_seconds",
        "stt_latency_warn_seconds",
        "stt_latency_alert_seconds",
        "browser_search_timeout_seconds",
        "wake_enabled",
        "wake_aec_enabled",
        "browser_search_enabled",
        "browser_provider_enabled",
        "playback_gapless_enabled",
        "playback_artwork_sync_enabled",
        "playback_hot_buffer_enabled",
        "tts_enabled",
        "tts_pronunciation_overrides",
        "tts_acronym_dict_enabled",
        "tts_brand_dict_enabled",
        "tts_prosody_hints_enabled",
        "tts_opener_cache_enabled",
        "tts_opener_cache_variants",
        "user_model_enabled",
        "meta_analysis_enabled",
        "conversational_mode_enabled",
        "llm_fallback_enabled",
        "llm_fallback_chain",
        "local_llm_enabled",
        "local_llm_model",
        "local_llm_base_url",
        "llm_rate_limit_enabled",
        "llm_usage_tracking_enabled",
        "telemetry_enabled",
        "default_volume",
        "tts_rate",
        "tts_volume",
        "wake_sensitivity",
        "wake_loopback_rms_threshold",
        "wake_correlation_threshold",
        "wake_vad_min_confidence_playback",
        "wake_vad_min_confidence_idle",
        "perf_profile",
        "llm_max_tokens_cap",
        "llm_cost_per_minute_cap",
        "llm_monthly_cost_cap_usd",
    }
)

_UNSAFE_FIELDS: frozenset[str] = frozenset(
    {
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "openrouteservice_api_key",
        "usda_api_key",
        "stripe_secret_key",
        "stripe_webhook_secret",
        "stripe_publishable_key",
        "stripe_price_pro_monthly",
        "stripe_price_pro_annual",
        "stripe_price_max_monthly",
        "stripe_price_max_annual",
        "stripe_price_extra_usage_10",
        "sentry_dsn",
        "gotrue_webhook_secret",
        "searxng_url",
        "api_port",
        "api_host",
        "data_dir",
        "temp_dir",
        "sample_rate",
        "gpt_model",
        "llm_backend",
        "stt_backend",
        "whisper_model",
        "whisper_device",
        "jwt_secret",
        "jwt_secret_previous",
        "jwt_secret_previous_expires_at",
        "session_access_token_minutes",
        "session_refresh_token_days",
        "session_accept_legacy_opaque_tokens",
        "plan_limiter_backend",
        "web_push_vapid_public_key",
        "web_push_vapid_private_key",
        "player_backend",
    }
)


def _coerce_value(current: object, raw: str) -> object:
    """Coerce a string env value to match the type of *current*."""
    if isinstance(current, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(current, dict):
        try:
            parsed = json.loads(raw)
        except Exception:
            return current
        return parsed if isinstance(parsed, dict) else current
    return raw


def hot_reload_env(cfg: AppConfig | None = None) -> dict[str, tuple[object, object]]:
    """Re-read ``.env`` and apply only safe fields to the live config."""
    cfg = cfg or settings
    changed: dict[str, tuple[object, object]] = {}
    env_path = Path(".env")
    if not env_path.exists():
        return changed

    new_env_vars: dict[str, str] = {}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            new_env_vars[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return changed

    prefix = "VIOLA_"
    restart_needed: list[str] = []
    for f in fields(cfg):
        name = f.name
        raw = new_env_vars.get(prefix + name.upper())
        if raw is None:
            continue
        current_val = getattr(cfg, name)
        new_val = _coerce_value(current_val, raw)
        if new_val == current_val:
            continue
        if name in _SAFE_RELOAD_FIELDS:
            setattr(cfg, name, new_val)
            changed[name] = (current_val, new_val)
        elif name in _UNSAFE_FIELDS:
            restart_needed.append(name)

    if restart_needed:
        _get_logger().warning(
            "hot_reload: %d field(s) require restart: %s",
            len(restart_needed),
            ", ".join(restart_needed),
        )
    return changed


class EnvFileWatcher:
    """Background thread polling ``.env`` for changes and applying safe reloads."""

    def __init__(self, *, cfg: AppConfig | None = None, poll_interval: float = 5.0):
        self._cfg = cfg or settings
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._last_mtime: float = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="env-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._poll_interval)
            if self._stop_event.is_set():
                break
            env_path = Path(".env")
            if not env_path.exists():
                continue
            try:
                current_mtime = env_path.stat().st_mtime
            except Exception:
                continue
            if current_mtime <= self._last_mtime:
                continue
            self._last_mtime = current_mtime
            _get_logger().info("EnvFileWatcher: .env changed, applying safe reload")
            try:
                hot_reload_env(self._cfg)
            except Exception:
                _get_logger().exception("EnvFileWatcher: reload failed")


__all__ = [
    "_SAFE_RELOAD_FIELDS",
    "_UNSAFE_FIELDS",
    "AppConfig",
    "EnvFileWatcher",
    "SettingsValidationError",
    "ensure_required_secrets",
    "get_missing_required_secrets",
    "get_public_config_payload",
    "get_runtime_base_url",
    "get_settings",
    "hot_reload_env",
    "settings",
]
