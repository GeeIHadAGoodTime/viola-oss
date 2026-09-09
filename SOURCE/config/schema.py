"""
Unified Settings Schema
Validation and default value management for all settings

This module provides a unified schema for settings with validation,
defaults, and type checking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from config.defaults import (
    DEFAULT_AGENT_REASONING_EFFORT,
    DEFAULT_BACKGROUND_AGENT_REASONING_EFFORT,
    DEFAULT_LLM_FALLBACK_CHAIN,
    DEFAULT_LLM_FALLBACK_ENABLED,
    DEFAULT_LOCAL_LLM_ENABLED,
    DEFAULT_LOCAL_LLM_MODEL,
    PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT,
    REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT,
)
from core.constants import OLLAMA_DEFAULT_BASE_URL
from core.logging_config import get_logger

logger = get_logger(__name__)

# =============================================================================
# Settings Schema Classes
# =============================================================================


@dataclass
class SettingsSchema:
    """
    Unified settings schema with validation and defaults.

    This provides a single source of truth for all settings with:
    - Type validation
    - Default values
    - Range constraints
    - Documentation
    """

    # =====================================================================
    # Voice Settings
    # =====================================================================

    voice_mode: Literal["push_to_talk", "wake_word", "disabled"] = "wake_word"
    """Voice input mode. Keep in step with ``config.defaults.DEFAULT_VOICE_MODE``,
    which carries the rationale; a test asserts the two agree."""

    wake_word_engine: Literal["violawake", "none"] = "violawake"
    """Wake word detection engine (violawake is the only supported engine)"""

    wake_word_sensitivity: float = field(default=0.7, metadata={"min": 0.0, "max": 1.0})
    """Wake word sensitivity (0.0-1.0). Higher = more sensitive (easier to trigger)."""

    wake_gain_scheduler_enabled: bool = True
    """Enable adaptive gain recalibration for wake listeners"""

    wake_gain_scheduler_interval_minutes: int = field(default=45, metadata={"min": 5, "max": 720})
    """Minutes between adaptive recalibration cycles"""

    wake_gain_scheduler_profiles: list[dict[str, Any]] = field(default_factory=list)
    """Optional day/night calibration profile overrides"""

    wake_noise_quiet_rms: float = field(default=350.0, metadata={"min": 10.0, "max": 5000.0})
    """RMS threshold that defines quiet environments"""

    wake_noise_noisy_rms: float = field(default=1400.0, metadata={"min": 50.0, "max": 10000.0})
    """RMS threshold that defines noisy environments"""

    wake_noise_profiles: list[dict[str, Any]] = field(default_factory=list)
    """Noise profile overrides with threshold offsets"""

    ptt_enabled: bool = True
    """Enable push-to-talk"""

    ptt_hotkey: str = "space"
    """Push-to-talk hotkey"""

    # =====================================================================
    # Audio Settings
    # =====================================================================

    input_device: str | None = None
    """Microphone device name (None = default)"""

    output_device: str | None = None
    """Speaker device name (None = default)"""

    microphone_volume: int = field(default=100, metadata={"min": 0, "max": 100})
    """Microphone volume (0-100)"""

    speaker_volume: int = field(default=80, metadata={"min": 0, "max": 100})
    """Speaker volume (0-100)"""

    # =====================================================================
    # STT Settings
    # =====================================================================

    stt_engine: Literal["whisper_local", "whisper_api", "none"] = "whisper_local"
    """Speech-to-text engine"""

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
    """Whisper/faster-whisper model identifier"""

    whisper_device: Literal["auto", "cpu", "cuda"] = "cpu"
    """Whisper device"""

    stt_latency_warn_seconds: float = field(default=4.0, metadata={"min": 0.1, "max": 120.0})
    """Warning threshold for STT latency (seconds)"""

    stt_latency_alert_seconds: float = field(default=8.0, metadata={"min": 0.1, "max": 240.0})
    """Alert threshold for STT latency (seconds)"""

    stt_language_hint_ttl_seconds: int = field(default=1800, metadata={"min": 60, "max": 28800})
    """How long to cache detected language hints (seconds)"""

    whisper_language: str = "auto"
    """Whisper language code"""

    # =====================================================================
    # TTS Settings
    # =====================================================================

    tts_enabled: bool = True
    """Enable text-to-speech"""

    tts_backend: Literal["kokoro", "pyttsx3"] = "kokoro"
    """TTS backend engine (kokoro=neural local, pyttsx3=system SAPI)"""

    tts_voice: str = "default"
    """TTS voice name (engine-specific; for Kokoro see tts_kokoro_voice)"""

    tts_rate: int = field(default=150, metadata={"min": 50, "max": 300})
    """TTS speech rate (words per minute). Kokoro maps this to a speed multiplier."""

    tts_volume: int = field(default=80, metadata={"min": 0, "max": 100})
    """TTS volume (0-100). Applied as amplitude scaling on output PCM."""

    tts_pronunciation_overrides: dict[str, str] = field(default_factory=dict)
    """Case-insensitive whole-word text respellings before phonemization, e.g. {"Jihad": "Jee hahd"}."""

    tts_acronym_dict_enabled: bool = True
    """Enable built-in acronym pronunciations such as AI -> A I and JPEG -> jay peg."""

    tts_brand_dict_enabled: bool = True
    """Enable built-in brand pronunciations such as Spotify and Anthropic."""

    tts_prosody_hints_enabled: bool = True
    """Enable subtle punctuation hints that improve Kokoro timing."""

    quiet_hours_enabled: bool = True
    """Enable time-of-day quiet-hours adjustments for voice and wake behavior."""

    quiet_hours_start: str = "22:00"
    """Quiet-hours local start time (HH:MM)."""

    quiet_hours_end: str = "07:00"
    """Quiet-hours local end time (HH:MM)."""

    quiet_hours_timezone: str = "auto"
    """Quiet-hours timezone; auto uses the local/user timezone."""

    tts_kokoro_model_path: str = "models/tts/kokoro-v1.0.onnx"
    """Path to Kokoro ONNX model file"""

    tts_kokoro_voices_path: str = "models/tts/voices-v1.0.bin"
    """Path to Kokoro voices binary file"""

    tts_kokoro_voice: str = "af_heart"
    """Kokoro voice name (e.g. af_heart, af_sarah, am_adam, bf_emma)"""

    tts_opener_cache_enabled: bool = True
    """Enable pre-rendered cache variants for common short TTS openers."""

    tts_opener_cache_variants: int = field(default=5, metadata={"min": 2, "max": 12})
    """Number of pre-rendered variants per canonical opener."""

    tts_post_fx_enabled: bool = True
    """Enable TTS post-processing EQ/compression/de-ess/limiting."""

    tts_loudness_target_lufs: float = field(default=-16.0, metadata={"min": -30.0, "max": -10.0})
    """Integrated loudness target for processed TTS output."""

    tts_voice_blend: list[list[Any]] | None = field(default_factory=lambda: [["af_river", 0.5], ["af_alloy", 0.5]])
    """Optional Kokoro voice blend as [[voice_id, weight], ...].
    Default: 50/50 af_river + af_alloy (river's natural cadence + alloy's brightness)."""

    tts_speed_jitter_pct: float = field(default=0.02, metadata={"min": 0.0, "max": 0.10})
    """Per-utterance random speed variation percentage."""

    tts_intersentence_gap_ms: int = field(default=180, metadata={"min": 0, "max": 1000})
    """Default gap between independently synthesized TTS sentences."""

    # =====================================================================
    # Music Settings
    # =====================================================================

    player_backend: Literal["vlc", "simple", "qt_media"] = "simple"
    """Music player backend. Note: VLC should only be used for local files or direct stream URLs."""

    autoplay_enabled: bool = True
    """Enable automatic playlist population"""

    ai_autoplay_enabled: bool = True
    """Enable AI-powered autoplay"""

    autoplay_min_queue: int = field(default=7, metadata={"min": 1, "max": 50})
    """Minimum queue size before autoplay triggers"""

    # =====================================================================
    # API Settings
    # =====================================================================

    openai_api_key: str | None = None
    """OpenAI API key"""

    gpt_model: str = "gpt-5.4-mini"
    """GPT model for AI features"""

    enable_gpt: bool = True
    """Enable GPT features"""

    agent_enabled: bool = True
    """Enable the canonical local agent tool-use loop."""

    agent_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = (
        DEFAULT_AGENT_REASONING_EFFORT
    )
    """Reasoning effort for the interactive root agent"""

    background_agent_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = (
        DEFAULT_BACKGROUND_AGENT_REASONING_EFFORT
    )
    """Reasoning effort for delegated background/child agents"""

    llm_fallback_enabled: bool = DEFAULT_LLM_FALLBACK_ENABLED
    """Enable managed-mode provider fallback across configured backup providers"""

    llm_fallback_chain: list[str] = field(default_factory=lambda: list(DEFAULT_LLM_FALLBACK_CHAIN))
    """Provider source order for managed-mode fallback"""

    local_llm_enabled: bool = DEFAULT_LOCAL_LLM_ENABLED
    """Allow local LLM fallback when the local endpoint is reachable"""

    local_llm_model: str = DEFAULT_LOCAL_LLM_MODEL
    """Model name to use for local LLM fallback"""

    local_llm_base_url: str = OLLAMA_DEFAULT_BASE_URL
    """Base URL for local LLM fallback"""

    searxng_url: str = ""
    """SearXNG base URL for web search (empty = degraded DDG fallback only)"""

    weather_air_quality_airnow_enabled: bool = True
    """Enable U.S. AQI enrichment from EPA AirNow public reporting-area data"""

    weather_air_quality_airnow_reporting_area_url: str = "https://files.airnowtech.org/airnow/today/reportingarea.dat"
    """EPA AirNow reportingarea.dat URL used for U.S. AQI enrichment"""

    weather_air_quality_airnow_cache_ttl_seconds: int = field(default=1800, metadata={"min": 300, "max": 7200})
    """Seconds to cache the public AirNow reporting-area file"""

    weather_air_quality_airnow_max_distance_miles: float = field(default=100.0, metadata={"min": 1.0, "max": 250.0})
    """Maximum distance from user coordinates to an AirNow reporting-area centroid"""

    telemetry_install_id: str = ""
    """Local random install id used for opt-in telemetry and update cohorts."""

    sentry_enabled: bool = True
    """Enable Sentry SDK initialization when a DSN is configured."""

    sentry_dsn: str = ""
    """Sentry DSN for process error reporting."""

    sentry_environment: str = ""
    """Optional Sentry environment override."""

    sentry_traces_sample_rate: float = field(default=0.0, metadata={"min": 0.0, "max": 1.0})
    """Sentry performance trace sample rate."""

    sentry_require_consent: bool = True
    """Require explicit error-reporting consent before Sentry events are sent."""

    developer_mode: bool = False
    """Expose developer diagnostics in local UI settings."""

    early_access_updates: bool = False
    """Use the beta update manifest for this desktop install."""

    phone_mode: Literal["cloud", "local"] = "cloud"
    """Outbound phone routing mode. Cloud is the fresh-install default."""

    callback_phone: str = ""
    """User-controlled phone number recipients may call back during outbound calls."""

    phone_ai_identity_enforcement: bool = PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT
    """Opt-in phone watchdog that blocks human claims and answers AI identity questions."""

    home_assistant_url: str = ""
    """Base URL for a user-configured smart-home hub."""

    home_assistant_token: str = ""
    """Encrypted smart-home access token."""

    require_account_for_paid_actions: bool = REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT
    """Require a Viola account before actions that spend Viola funds."""

    openrouteservice_api_key: str | None = None
    """OpenRouteService API key for directions"""

    usda_api_key: str | None = None
    """USDA FoodData Central API key for nutrition lookups"""

    google_restricted_features_enabled: bool = False
    """Enable restricted-scope Gmail and Google Workspace features after verification."""

    # =====================================================================
    # Billing Settings
    # =====================================================================

    stripe_secret_key: str | None = None
    """Stripe secret key for subscription checkout."""

    stripe_webhook_secret: str | None = None
    """Stripe webhook signing secret."""

    stripe_publishable_key: str | None = None
    """Stripe publishable key for frontend-safe configuration."""

    stripe_price_pro_monthly: str | None = None
    """Stripe Price ID for Pro monthly."""

    stripe_price_pro_annual: str | None = None
    """Stripe Price ID for Pro annual."""

    stripe_price_max_monthly: str | None = None
    """Stripe Price ID for Max monthly."""

    stripe_price_max_annual: str | None = None
    """Stripe Price ID for Max annual."""

    # =====================================================================
    # Orchestration Settings
    # =====================================================================

    orchestration_enabled: bool = False
    """Enable the multi-agent orchestration subsystem."""

    orchestration_max_agents: int = field(default=8, metadata={"min": 1, "max": 32})
    """Maximum number of concurrent orchestration worker agents."""

    orchestration_spawn_cooldown: int = field(default=30, metadata={"min": 0, "max": 3600})
    """Minimum seconds between orchestration agent spawns."""

    orchestration_data_dir: str = "data/orchestration"
    """Storage directory for orchestration blackboard and task logs."""

    codex_enabled: bool = False
    """Enable Codex task delegation for orchestration workflows."""

    codex_timeout: int = field(default=300, metadata={"min": 1, "max": 3600})
    """Default Codex task timeout in seconds."""

    # =====================================================================
    # UI Settings
    # =====================================================================

    ui_mode: Literal["native_qt", "web_embedded"] = "native_qt"
    """UI mode"""

    theme: Literal["dark", "light"] = "dark"
    """UI theme"""

    show_notifications: bool = True
    """Show notifications"""

    auto_update_check_enabled: bool = True
    """Deprecated mirror of the SettingsManager user preference (runtime truth is
    ui/settings_manager.py, read via is_update_notification_enabled). Defaults ON
    to match the offered-update check; the user opts out in Settings > Updates."""

    show_api_key_warning: bool = True
    """Show API key warning when not configured"""

    features: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Feature registry overrides"""

    # =====================================================================
    # Performance Settings
    # =====================================================================

    performance_enabled: bool = True
    """Enable performance enhancements"""

    performance_profile: Literal["disabled", "minimal", "balanced", "aggressive"] = "balanced"
    """Performance profile"""

    cache_enabled: bool = True
    """Enable caching"""

    cache_type: Literal["memory", "persistent", "hybrid"] = "hybrid"
    """Cache type"""

    memory_cache_size: int = field(default=50, metadata={"min": 10, "max": 500})
    """Memory cache size (entries)"""

    disk_cache_size: int = field(default=200, metadata={"min": 50, "max": 1000})
    """Disk cache size (entries)"""

    # =====================================================================
    # Validation Methods
    # =====================================================================

    def validate(self, key: str, value: Any) -> tuple[bool, str]:
        """
        Validate a setting value.

        Args:
            key: Setting key
            value: Value to validate

        Returns:
            (is_valid, error_message)
        """
        # Check if key exists
        if not hasattr(self, key):
            return False, f"Unknown setting: {key}"

        # Get expected type
        expected_type = type(getattr(self, key))

        # Check type
        if value is not None and not isinstance(value, expected_type):
            if not (key == "tts_voice_blend" and getattr(self, key) is None and isinstance(value, list)):
                return False, f"Setting {key} must be of type {expected_type.__name__}"

        # Check range constraints
        if hasattr(self, "__dataclass_fields__"):
            field_info = self.__dataclass_fields__.get(key)
            if field_info and field_info.metadata:
                metadata = dict(field_info.metadata)
                if isinstance(value, (int, float)):
                    minimum = metadata.get("min")
                    maximum = metadata.get("max")
                    if minimum is not None and value < minimum:
                        return False, f"Setting {key} must be >= {minimum}"
                    if maximum is not None and value > maximum:
                        return False, f"Setting {key} must be <= {maximum}"
                if isinstance(value, str):
                    choices = metadata.get("choices")
                    if choices and value not in choices:
                        return False, f"Setting {key} must be one of {choices}"

        return True, ""

    def set(self, key: str, value: Any) -> bool:
        """
        Set a setting value with validation.

        Args:
            key: Setting key
            value: Value to set

        Returns:
            True if set successfully, False otherwise
        """
        is_valid, error = self.validate(key, value)
        if not is_valid:
            logger.warning("Settings validation error for %s: %s", key, error)
            return False

        setattr(self, key, value)
        return True

    def to_dict(self, exclude_none: bool = False) -> dict[str, Any]:
        """
        Convert settings to dictionary.

        Args:
            exclude_none: Whether to exclude None values

        Returns:
            Settings dictionary
        """
        result = {}
        for key in self.__dataclass_fields__:
            value = getattr(self, key)
            if not (exclude_none and value is None):
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SettingsSchema:
        """
        Create settings from dictionary.

        Args:
            data: Settings dictionary

        Returns:
            Settings instance
        """
        # Filter to only known fields
        filtered_data = {}
        schema = cls()
        for key in schema.__dataclass_fields__:
            if key in data:
                filtered_data[key] = data[key]

        return cls(**filtered_data)


# =============================================================================
# Factory Functions
# =============================================================================


def create_schema() -> SettingsSchema:
    """
    Create default settings schema.

    Returns:
        Default settings instance
    """
    return SettingsSchema()


def validate_settings(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Validate a settings dictionary.

    Args:
        data: Settings dictionary to validate

    Returns:
        (is_valid, list_of_errors)
    """
    schema = create_schema()
    errors = []

    for key, value in data.items():
        is_valid, error = schema.validate(key, value)
        if not is_valid:
            errors.append(error)

    return len(errors) == 0, errors


# =============================================================================
# Export
# =============================================================================

__all__ = [
    "SettingsSchema",
    "create_schema",
    "validate_settings",
]
