"""Settings tier allowlists for the D7 settings split."""

from __future__ import annotations

from typing import Final

INFRASTRUCTURE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "_settings_last_saved",
        "_settings_version",
        "api_host",
        "api_port",
        "app_surface",
        # Device tier (local-only, never cloud-synced): optional first-run
        # "How did you hear about Viola?" self-report. Non-PII closed enum;
        # only the opt-in first_run funnel ping ever carries it off-device.
        "attribution_self_report",
        "auth_enabled",
        "auto_update_check_enabled",
        "base_url",
        "browser_provider_enabled",
        "companion_device_name",
        "companion_enabled",
        "computer_use_app_whitelist",
        "computer_use_log_window_titles_enabled",
        "computer_use_max_chars_per_type",
        "computer_use_screenshot_format",
        "computer_use_screenshot_quality",
        "custom_wake_word_name",
        "dangerous_mode_acknowledged",
        "debug_routes_enabled",
        "deployment_mode",
        "dev_mode",
        "developer_mode",
        "disable_device_discovery",
        "early_access_updates",
        "features",
        "founder_phone_number",
        "google_workspace_mcp_path",
        "input_device",
        "local_music_folder",
        "log_level",
        "microphone_volume",
        "mic_muted",
        "minimize_to_tray",
        "mute_hotkey",
        "mute_hotkey_display",
        "network_discovery_enabled",
        "output_device",
        "phone_mode",
        "ptt_enabled",
        "ptt_hotkey",
        "ptt_hotkey_display",
        "ptt_hotkey_type",
        "remote_control_at_startup",
        "require_account_for_paid_actions",
        "security_auth_enabled",
        "sentry_dsn",
        "sentry_enabled",
        "sentry_environment",
        "sentry_require_consent",
        "sentry_traces_sample_rate",
        "speaker_volume",
        "start_on_boot",
        "stt_engine",
        "telemetry_install_id",
        "use_custom_wake_word",
        "voice_mode",
        "wake_enabled",
        "wake_engine",
        "wake_sensitivity",
        "wake_word_active_model",
        "wake_word_engine",
        "wake_word_model",
        "whisper_device",
        "whisper_model",
    }
)

USER_SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "accent_color",
        "active_music_provider_id",
        "agent_autonomy",
        "agent_enabled",
        "agent_model",
        "agent_reasoning_effort",
        "announce_ai_on_calls",
        "ai_autoplay_enabled",
        "ai_enabled",
        "ai_source",
        "allow_explicit",
        "auto_propose_shortcuts",
        "autoplay_enabled",
        "autoplay_min_queue",
        "browser_session_mode",
        "calendar_reminder_lead_minutes",
        "calendar_reminders_enabled",
        "call_history_retention_days",
        "callback_phone",
        "codex_reasoning_effort",
        "custom_instructions",
        "default_music_volume",
        "delivery_address",
        "email_link_delivery_default",
        "email_recipient_allowlist",
        "enable_gpt",
        "home_assistant_url",
        "intent_market_enabled",
        # The iPhone's own onboarding completion. Separate from
        # "onboarding_completed" on purpose: the iOS flow walks a different
        # set of steps (notification/microphone permission prompts, the
        # privacy-consent screen) that a desktop user has never seen, so
        # completing desktop onboarding must not silently mark the phone
        # done, nor the reverse. It is Tier-2 user-scoped rather than
        # device-local so a reinstall or a second device does not re-run the
        # walkthrough. Until this key existed, every write 400'd with
        # unknown_setting_key and every read 400'd too -- which the client
        # read as "not onboarded", so onboarding re-ran on every cold start.
        "ios_onboarding_completed",
        "keep_phone_transcript",
        "knowledge_storage_mode",
        "llm_base_url",
        "llm_fallback_strategy",
        "llm_model",
        "llm_prefer_local",
        "llm_provider",
        "locale",
        "onboarding_completed",
        "openai_key_source",
        "phone_ai_identity_enforcement",
        "phone_call_ai_disclosure",
        "phone_reasoning_effort",
        "quiet_hours_enabled",
        "quiet_hours_end",
        "quiet_hours_start",
        "quiet_hours_timezone",
        "reasoning_effort",
        "record_phone_calls",
        "routing_reasoning_effort",
        "session_purchase_ceiling_cents",
        "show_api_key_warning",
        "show_notifications",
        "slack_enabled",
        "speak_all_replies",
        "telegram_enabled",
        "telegram_owner_chat_id",
        "theme",
        "time_display_format",
        "tts_acronym_dict_enabled",
        "tts_brand_dict_enabled",
        "tts_enabled",
        "tts_intersentence_gap_ms",
        "tts_loudness_target_lufs",
        "tts_opener_cache_enabled",
        "tts_opener_cache_variants",
        "tts_post_fx_enabled",
        "tts_pronunciation_overrides",
        "tts_prosody_hints_enabled",
        "tts_rate",
        "tts_speed_jitter_pct",
        "tts_voice",
        "tts_voice_blend",
        "tts_volume",
        "user_name",
        "user_phone_number",
        "voice_muted",
        "wake_audio_logging_enabled",
        "wake_audio_retention_hours",
        "weather_location",
        "weekly_review_enabled",
        "whisper_language",
    }
)

CLOUD_COMPAT_USER_SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "language",
        "timezone",
        "use_dark_mode",
    }
)

CLOUD_CONSENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "consent_cloud_stt",
        "consent_cloud_sync",
        "consent_error_reporting",
        "consent_session_replay",
        "consent_vision_clipboard",
        "contributor_mode_enabled",
        "telemetry_opt_in",
        "wake_data_contribute",
        "wake_word_training_opt_in",
        # Anonymized diagnostic minimum (crash + bug report). All three stay
        # device-local (consent tier is never synced to cloud): the disclosure
        # acknowledgement, the opt-out for the anonymized baseline, and the
        # separate opt-in for the identifiable extra. They are ALSO in
        # _GLOBAL_SETTINGS_KEYS so they resolve against the device/global store,
        # never the per-user store: the crash handler runs on a background thread
        # with no user context, so a per-user scoping would let it miss a
        # signed-in user's opt-out and send after opt-out (the telemetry-consent
        # dead-letter class). Device-global is correct here (Viola is
        # one-user-per-install) and safe by construction.
        "diagnostics_disclosure_shown",
        "diagnostics_baseline_opted_out",
        "consent_diagnostics_identifiable",
    }
)

CREDENTIAL_RELOCATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "companion_cloud_token",
        "home_assistant_token",
        "icloud_password",
        "llm_api_key",
        "openai_api_key",
        "slack_app_token",
        "slack_bot_token",
        "spotify_refresh_token",
        "telegram_bot_token",
    }
)

STALE_DELETE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "signal_account",
        "signal_daemon_url",
        "signal_enabled",
        "volume",
        "whatsapp_bridge_url",
        "whatsapp_enabled",
    }
)

BIPA_DELETE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "bipa_consent_granted",
        "bipa_consent_timestamp",
        "bipa_consent_version",
    }
)

CLOUD_NON_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "display_name",
        "push_to_talk_enabled",
        "sms_consent",
    }
)

SETTINGS_CLOUD_USER_SCOPED: Final[frozenset[str]] = USER_SETTING_KEYS | CLOUD_COMPAT_USER_SETTING_KEYS

SETTINGS_CLOUD_TIER3_ONLY: Final[frozenset[str]] = (
    INFRASTRUCTURE_KEYS | CREDENTIAL_RELOCATE_KEYS | STALE_DELETE_KEYS | BIPA_DELETE_KEYS | CLOUD_NON_SETTINGS_KEYS
)

SETTING_KEY_ALIASES: Final[dict[str, str]] = {
    "capability_tier": "agent_autonomy",
    "telemetry_enabled": "telemetry_opt_in",
    "wake_word_sensitivity": "wake_sensitivity",
}

__all__ = [
    "BIPA_DELETE_KEYS",
    "CLOUD_COMPAT_USER_SETTING_KEYS",
    "CLOUD_CONSENT_KEYS",
    "CLOUD_NON_SETTINGS_KEYS",
    "CREDENTIAL_RELOCATE_KEYS",
    "INFRASTRUCTURE_KEYS",
    "SETTINGS_CLOUD_TIER3_ONLY",
    "SETTINGS_CLOUD_USER_SCOPED",
    "SETTING_KEY_ALIASES",
    "STALE_DELETE_KEYS",
    "USER_SETTING_KEYS",
]
