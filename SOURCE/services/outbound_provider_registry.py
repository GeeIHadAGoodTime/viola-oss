"""Central declarations for outbound provider integrations.

This module is intentionally passive metadata. Runtime providers keep their
current behavior, while launch/readiness checks can enforce that external-call
surfaces have an owner-reviewed governance posture.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from core.constants import TIMEOUT_5_MINUTES, TIMEOUT_10_MINUTES, TIMEOUT_HOUR, TIMEOUT_MINUTE

_FIVE_MINUTES_SECONDS = int(TIMEOUT_5_MINUTES)
_TEN_MINUTES_SECONDS = int(TIMEOUT_10_MINUTES)
_FIFTEEN_MINUTES_SECONDS = _TEN_MINUTES_SECONDS + _FIVE_MINUTES_SECONDS
_ONE_HOUR_SECONDS = int(TIMEOUT_HOUR)
_ONE_DAY_SECONDS = _ONE_HOUR_SECONDS * 24
_ONE_WEEK_SECONDS = _ONE_DAY_SECONDS * 7
_THIRTY_DAYS_SECONDS = _ONE_DAY_SECONDS * 30
_NINETY_DAYS_SECONDS = _ONE_DAY_SECONDS * 90


class ProviderDomain(str, Enum):
    """Product domain that owns or primarily uses the outbound provider."""

    AUTH = "auth"
    BILLING = "billing"
    BROWSER = "browser"
    EMAIL = "email"
    INTEGRATION = "integration"
    LLM = "llm"
    MESSAGING = "messaging"
    MUSIC = "music"
    SEARCH = "search"
    SMART_HOME = "smart_home"
    TELEPHONY = "telephony"
    WEATHER = "weather"


class CacheScope(str, Enum):
    """How provider responses, tokens, or metadata may be cached."""

    NONE = "none"
    LOCAL_RUNTIME = "local_runtime"
    PUBLIC_SHARED = "public_shared"
    TOKEN_SCOPED = "token_scoped"
    USER_SCOPED = "user_scoped"


class PrivacyClass(str, Enum):
    """Highest sensitivity class normally sent to the provider."""

    LOCAL_ONLY = "local_only"
    PUBLIC_DATA = "public_data"
    USER_QUERY = "user_query"
    USER_CONTENT = "user_content"
    CONTACT_OR_MESSAGE = "contact_or_message"
    OAUTH_TOKEN = "oauth_token"
    OPERATIONAL_METADATA = "operational_metadata"
    PAYMENT_DATA = "payment_data"
    VOICE_AUDIO = "voice_audio"


class RateLimitPosture(str, Enum):
    """Where rate limiting is expected to be enforced."""

    NONE = "none"
    PROVIDER_QUOTA = "provider_quota"
    APP_LIMITED = "app_limited"
    PROVIDER_AND_APP_LIMITED = "provider_and_app_limited"


class SpendCapPosture(str, Enum):
    """How direct provider spend is controlled."""

    LOCAL_RESOURCE = "local_resource"
    MANAGED_CAP_REQUIRED = "managed_cap_required"
    NO_DIRECT_SPEND = "no_direct_spend"
    OWNER_BILLING_ACCOUNT = "owner_billing_account"
    PROVIDER_FREE = "provider_free"
    PROVIDER_QUOTA_ONLY = "provider_quota_only"
    USER_BYOK = "user_byok"


class FallbackBehavior(str, Enum):
    """What callers should do when the provider is unavailable or denied."""

    NONE = "none"
    CASCADE_TO_PROVIDER = "cascade_to_provider"
    DEGRADE_GRACEFULLY = "degrade_gracefully"
    FAIL_CLOSED = "fail_closed"
    LOCAL_FALLBACK = "local_fallback"
    REQUIRE_USER_ACTION = "require_user_action"


class OwnerAlertBehavior(str, Enum):
    """When operator/owner alerting should fire beyond routine logs."""

    NONE = "none"
    LOG_ONLY = "log_only"
    METRICS = "metrics"
    OWNER_ALERT_ON_FAILURE = "owner_alert_on_failure"
    OWNER_ALERT_ON_QUOTA_OR_SPEND = "owner_alert_on_quota_or_spend"
    SECURITY_OR_COMPLIANCE_ALERT = "security_or_compliance_alert"


@dataclass(frozen=True, slots=True)
class OutboundProviderDeclaration:
    """Governance posture for one outbound provider integration."""

    provider_id: str
    display_name: str
    domain: ProviderDomain
    integration_points: tuple[str, ...]
    cache_scope: CacheScope
    cache_ttl_seconds: int | None
    privacy_class: PrivacyClass
    rate_limit_posture: RateLimitPosture
    spend_cap_posture: SpendCapPosture
    fallback_behavior: FallbackBehavior
    owner_alert_behavior: OwnerAlertBehavior
    notes: str = ""


def _declaration(
    provider_id: str,
    display_name: str,
    domain: ProviderDomain,
    integration_points: Iterable[str],
    cache_scope: CacheScope,
    cache_ttl_seconds: int | None,
    privacy_class: PrivacyClass,
    rate_limit_posture: RateLimitPosture,
    spend_cap_posture: SpendCapPosture,
    fallback_behavior: FallbackBehavior,
    owner_alert_behavior: OwnerAlertBehavior,
    notes: str = "",
) -> OutboundProviderDeclaration:
    return OutboundProviderDeclaration(
        provider_id=provider_id,
        display_name=display_name,
        domain=domain,
        integration_points=tuple(integration_points),
        cache_scope=cache_scope,
        cache_ttl_seconds=cache_ttl_seconds,
        privacy_class=privacy_class,
        rate_limit_posture=rate_limit_posture,
        spend_cap_posture=spend_cap_posture,
        fallback_behavior=fallback_behavior,
        owner_alert_behavior=owner_alert_behavior,
        notes=notes,
    )


_DECLARATIONS = {
    # LLM providers.
    "openai": _declaration(
        "openai",
        "OpenAI API",
        ProviderDomain.LLM,
        (
            "chat/service.py",
            "services/llm/providers/openai_compatible.py",
            "services/llm/providers/openai_agents_provider.py",
            "services/llm/openai_direct.py",
            "services/openai_background.py",
            "intent/tools/vision_tools.py",
            "telephony/call_manager.py",
            "telephony/post_call_actions.py",
            "telephony/phone_simulator.py",
            "telephony/traced_openai_llm_service.py",
            "telephony/traced_openai_responses_llm_service.py",
            "ui/api/routes/voice_session.py",
            "utils/enhancements/connection_pool.py",
            "voice/transcription/cloud_adapter.py",
        ),
        CacheScope.NONE,
        None,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.MANAGED_CAP_REQUIRED,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.OWNER_ALERT_ON_QUOTA_OR_SPEND,
        "Managed OpenAI usage must stay behind quota, spend-cap, and account gates.",
    ),
    "openai_compatible": _declaration(
        "openai_compatible",
        "OpenAI-compatible API",
        ProviderDomain.LLM,
        (
            "services/llm/providers/openai_compatible.py",
            "services/llm/factory.py",
            "services/llm/local_models.py",
            "ui/settings_api.py",
        ),
        CacheScope.NONE,
        None,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.USER_BYOK,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.LOG_ONLY,
        "Remote base URLs are cloud providers; loopback base URLs are local-provider variants.",
    ),
    "anthropic": _declaration(
        "anthropic",
        "Anthropic API",
        ProviderDomain.LLM,
        ("services/llm/providers/anthropic_provider.py", "services/llm/factory.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.USER_BYOK,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.OWNER_ALERT_ON_QUOTA_OR_SPEND,
    ),
    "google_gemini": _declaration(
        "google_gemini",
        "Google Gemini API",
        ProviderDomain.LLM,
        ("services/llm/providers/google_provider.py", "services/llm/factory.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.USER_BYOK,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.OWNER_ALERT_ON_QUOTA_OR_SPEND,
    ),
    "ollama_local": _declaration(
        "ollama_local",
        "Ollama local API",
        ProviderDomain.LLM,
        (
            "services/llm/providers/ollama_native_provider.py",
            "services/llm/factory.py",
            "services/llm/local_models.py",
            "ui/settings_api.py",
        ),
        CacheScope.LOCAL_RUNTIME,
        _FIVE_MINUTES_SECONDS,
        PrivacyClass.LOCAL_ONLY,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.LOCAL_RESOURCE,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "codex_subscription": _declaration(
        "codex_subscription",
        "OpenAI Codex subscription endpoint",
        ProviderDomain.LLM,
        ("services/llm/codex_auth.py", "services/llm/factory.py"),
        CacheScope.TOKEN_SCOPED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.OWNER_ALERT_ON_FAILURE,
    ),
    # Music and media providers.
    "youtube_music_browser": _declaration(
        "youtube_music_browser",
        "YouTube Music browser provider",
        ProviderDomain.MUSIC,
        (
            "music/providers/browser/auth_manager.py",
            "music/providers/browser/recipes/youtube_music.py",
            "music/providers/browser/music_provider.py",
            "music/providers/browser_search.py",
            "music/providers/youtube_iframe.py",
            "intent/tools/music_connect.py",
        ),
        CacheScope.LOCAL_RUNTIME,
        _ONE_DAY_SECONDS,
        PrivacyClass.USER_QUERY,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
        "No Google OAuth, YouTube API key, or app-side token exchange is used.",
    ),
    "spotify_oauth": _declaration(
        "spotify_oauth",
        "Spotify OAuth",
        ProviderDomain.MUSIC,
        ("music/consent/adapters/spotify.py", "music/consent/provider_config.py"),
        CacheScope.TOKEN_SCOPED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.OAUTH_TOKEN,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "spotify_web": _declaration(
        "spotify_web",
        "Spotify Web Player/API",
        ProviderDomain.MUSIC,
        (
            "music/spotify/cdp_controller.py",
            "music/providers/spotify_cdp.py",
            "music/providers/browser/recipes/spotify.py",
            "intent/instant_commands/music.py",
            "intent/tools/music_connect.py",
        ),
        CacheScope.TOKEN_SCOPED,
        _ONE_DAY_SECONDS,
        PrivacyClass.USER_QUERY,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    # Weather and public-data providers.
    "noaa_gfs_weather": _declaration(
        "noaa_gfs_weather",
        "NOAA GFS weather service",
        ProviderDomain.WEATHER,
        (
            "backend/weather_fetch.py",
            "services/weather-gfs/weather_gfs/app.py",
            "services/weather-gfs/weather_gfs/ingestion.py",
        ),
        CacheScope.PUBLIC_SHARED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.PUBLIC_DATA,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.METRICS,
    ),
    "nws_weather": _declaration(
        "nws_weather",
        "National Weather Service API",
        ProviderDomain.WEATHER,
        ("backend/weather_fetch.py",),
        CacheScope.PUBLIC_SHARED,
        _FIFTEEN_MINUTES_SECONDS,
        PrivacyClass.PUBLIC_DATA,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "wttr_weather": _declaration(
        "wttr_weather",
        "wttr.in weather API",
        ProviderDomain.WEATHER,
        ("backend/weather_fetch.py",),
        CacheScope.PUBLIC_SHARED,
        _TEN_MINUTES_SECONDS,
        PrivacyClass.PUBLIC_DATA,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "openstreetmap_nominatim": _declaration(
        "openstreetmap_nominatim",
        "OpenStreetMap Nominatim",
        ProviderDomain.WEATHER,
        ("backend/weather_fetch.py",),
        CacheScope.PUBLIC_SHARED,
        _ONE_WEEK_SECONDS,
        PrivacyClass.USER_QUERY,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "airnow": _declaration(
        "airnow",
        "AirNow reporting-area data",
        ProviderDomain.WEATHER,
        ("backend/weather_fetch.py",),
        CacheScope.PUBLIC_SHARED,
        _ONE_DAY_SECONDS,
        PrivacyClass.PUBLIC_DATA,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "ip_api_geolocation": _declaration(
        "ip_api_geolocation",
        "ip-api.com location lookup",
        ProviderDomain.WEATHER,
        ("utils/weather_location.py",),
        CacheScope.PUBLIC_SHARED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    # Search and browsing.
    "searxng_search": _declaration(
        "searxng_search",
        "Configured SearXNG search",
        ProviderDomain.SEARCH,
        (
            "intent/tools/web_search.py",
            "admin/cloud_health.py",
            "services/cloud_music/public_youtube_resolver.py",
        ),
        CacheScope.PUBLIC_SHARED,
        _TEN_MINUTES_SECONDS,
        PrivacyClass.USER_QUERY,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.METRICS,
    ),
    "duckduckgo_search": _declaration(
        "duckduckgo_search",
        "DuckDuckGo search fallback",
        ProviderDomain.SEARCH,
        ("intent/tools/web_search.py",),
        CacheScope.PUBLIC_SHARED,
        _TEN_MINUTES_SECONDS,
        PrivacyClass.USER_QUERY,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "browser_navigation": _declaration(
        "browser_navigation",
        "Browser navigation to user-selected sites",
        ProviderDomain.BROWSER,
        (
            "mcp_servers/browser/server.py",
            "mcp_servers/browser_cdp/server.py",
            "mcp_servers/browser/browser_manager.py",
            "services/browser/agent_page_resolver.py",
            "services/browser/cloud_session_pool.py",
            "services/playwright_cdp_client.py",
            "services/cloud_browser/agent_browser.py",
        ),
        CacheScope.NONE,
        None,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    # Email, calendar, and OAuth.
    "google_oauth": _declaration(
        "google_oauth",
        "Google OAuth",
        ProviderDomain.AUTH,
        (
            "services/oauth/google.py",
            "services/oauth/workspace_bridge.py",
            "backend/fastapi_app.py",
            "music/consent/adapters/google_calendar.py",
        ),
        CacheScope.TOKEN_SCOPED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.OAUTH_TOKEN,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "microsoft_oauth": _declaration(
        "microsoft_oauth",
        "Microsoft OAuth",
        ProviderDomain.AUTH,
        ("music/consent/adapters/microsoft_calendar.py", "services/calendar/graph_auth.py"),
        CacheScope.TOKEN_SCOPED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.OAUTH_TOKEN,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "apple_identity_oauth": _declaration(
        "apple_identity_oauth",
        "Apple identity OAuth",
        ProviderDomain.AUTH,
        ("auth/gotrue_facade.py", "services/oauth/apple.py"),
        CacheScope.TOKEN_SCOPED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.OAUTH_TOKEN,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "google_calendar": _declaration(
        "google_calendar",
        "Google Calendar API",
        ProviderDomain.EMAIL,
        ("music/consent/adapters/google_calendar.py", "intent/tools/calendar_tools.py"),
        CacheScope.USER_SCOPED,
        _FIVE_MINUTES_SECONDS,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.LOCAL_FALLBACK,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "microsoft_graph_calendar": _declaration(
        "microsoft_graph_calendar",
        "Microsoft Graph Calendar API",
        ProviderDomain.EMAIL,
        ("services/calendar/providers/graph.py", "music/consent/adapters/microsoft_calendar.py"),
        CacheScope.USER_SCOPED,
        _FIVE_MINUTES_SECONDS,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.LOCAL_FALLBACK,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "gmail_oauth": _declaration(
        "gmail_oauth",
        "Gmail OAuth API",
        ProviderDomain.EMAIL,
        ("intent/tools/email.py", "services/oauth/google.py"),
        CacheScope.TOKEN_SCOPED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "imap_email": _declaration(
        "imap_email",
        "IMAP email access",
        ProviderDomain.EMAIL,
        ("intent/tools/email.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "smtp_email": _declaration(
        "smtp_email",
        "SMTP email delivery",
        ProviderDomain.EMAIL,
        ("intent/tools/email.py", "viola_email/providers/smtp.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "resend_email": _declaration(
        "resend_email",
        "Resend email delivery",
        ProviderDomain.EMAIL,
        ("viola_email/providers/resend.py", "backend/resend_webhook.py", "admin/alerts.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.CASCADE_TO_PROVIDER,
        OwnerAlertBehavior.OWNER_ALERT_ON_QUOTA_OR_SPEND,
    ),
    # Telephony and messaging.
    "telnyx_sms": _declaration(
        "telnyx_sms",
        "Telnyx SMS",
        ProviderDomain.TELEPHONY,
        (
            "backend/telnyx_sms_webhook.py",
            "mcp_servers/core_tools/server.py",
            "admin/alerts.py",
            "services/telnyx_sender.py",
        ),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "telnyx_call_control": _declaration(
        "telnyx_call_control",
        "Telnyx Call Control",
        ProviderDomain.TELEPHONY,
        (
            "telephony/call_manager.py",
            "backend/telnyx_webhook.py",
            "telephony/cloud_routes.py",
            "telephony/routes.py",
            "telephony/cloud_readiness.py",
        ),
        CacheScope.NONE,
        None,
        PrivacyClass.VOICE_AUDIO,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "viola_cloud_phone": _declaration(
        "viola_cloud_phone",
        "Viola cloud phone API",
        ProviderDomain.TELEPHONY,
        (
            "intent/tools/phone_call.py",
            "telephony/cloud_routes.py",
            "telephony/config.py",
            "telephony/desktop_cloud_proxy.py",
            "telephony/phone_cloud_event_relay.py",
        ),
        CacheScope.NONE,
        None,
        PrivacyClass.VOICE_AUDIO,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "cloudflare_tunnel": _declaration(
        "cloudflare_tunnel",
        "Cloudflare quick tunnel",
        ProviderDomain.TELEPHONY,
        ("telephony/tunnel.py",),
        CacheScope.LOCAL_RUNTIME,
        _ONE_HOUR_SECONDS,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.OWNER_ALERT_ON_FAILURE,
    ),
    "telegram_bot": _declaration(
        "telegram_bot",
        "Telegram Bot API",
        ProviderDomain.MESSAGING,
        ("backend/telegram_webhook.py", "intent/tools/telegram_tools.py", "ui/settings_api.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "telegram_owner_alerts": _declaration(
        "telegram_owner_alerts",
        "Telegram owner safety alerts",
        ProviderDomain.MESSAGING,
        ("admin/alerts.py", "scripts/send_test_alert.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.OWNER_ALERT_ON_FAILURE,
    ),
    "pushover_owner_alerts": _declaration(
        "pushover_owner_alerts",
        "Pushover owner safety alerts",
        ProviderDomain.MESSAGING,
        ("admin/alerts.py", "scripts/send_test_alert.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.OWNER_ALERT_ON_FAILURE,
    ),
    "pagerduty_owner_alerts": _declaration(
        "pagerduty_owner_alerts",
        "PagerDuty owner safety alerts",
        ProviderDomain.MESSAGING,
        ("admin/alerts.py", "scripts/send_test_alert.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.OWNER_ALERT_ON_FAILURE,
    ),
    "cloudflare_email": _declaration(
        "cloudflare_email",
        "Cloudflare Email Service",
        ProviderDomain.EMAIL,
        ("admin/alerts.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.OWNER_ALERT_ON_QUOTA_OR_SPEND,
    ),
    "generic_alert_webhook": _declaration(
        "generic_alert_webhook",
        "Generic owner alert webhook",
        ProviderDomain.MESSAGING,
        ("admin/alerts.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.OWNER_ALERT_ON_FAILURE,
    ),
    "slack_bot": _declaration(
        "slack_bot",
        "Slack messaging integration",
        ProviderDomain.MESSAGING,
        ("services/capability_registry.py", "ui/settings_api.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    # Billing and commerce providers.
    "stripe_billing": _declaration(
        "stripe_billing",
        "Stripe billing",
        ProviderDomain.BILLING,
        ("backend/cloud_app.py", "billing/providers/stripe.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.PAYMENT_DATA,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "btcpay_billing": _declaration(
        "btcpay_billing",
        "BTCPay billing",
        ProviderDomain.BILLING,
        ("backend/cloud_app.py", "billing/providers/btcpay.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.PAYMENT_DATA,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    # Other integration providers.
    "home_assistant": _declaration(
        "home_assistant",
        "Home Assistant",
        ProviderDomain.SMART_HOME,
        (
            "services/smart_home/home_assistant.py",
            "services/capability_providers/home_assistant.py",
            "plugins/builtin/smart-home/__init__.py",
            "ui/api/routes/smarthome.py",
        ),
        CacheScope.LOCAL_RUNTIME,
        int(TIMEOUT_MINUTE),
        PrivacyClass.LOCAL_ONLY,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.LOCAL_RESOURCE,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "viola_cloud_api": _declaration(
        "viola_cloud_api",
        "Viola cloud API",
        ProviderDomain.INTEGRATION,
        (
            "auth/desktop_gotrue_proxy.py",
            "auth/desktop_session.py",
            "auth/cloud_gdpr.py",
            "auth/gotrue_proxy.py",
            "billing/routes.py",
            "intent/tools/phone_call.py",
            "services/companion_client/client.py",
            "services/llm/providers/cloud_managed_provider.py",
            "services/payments/confirmation.py",
            "services/sync/http_transport.py",
        ),
        CacheScope.USER_SCOPED,
        _FIVE_MINUTES_SECONDS,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "web_read_http": _declaration(
        "web_read_http",
        "User-requested web fetch/read",
        ProviderDomain.BROWSER,
        ("intent/tools/web_read.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.USER_QUERY,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "youtube_oembed": _declaration(
        "youtube_oembed",
        "YouTube oEmbed availability check",
        ProviderDomain.MUSIC,
        (
            "music/providers/youtube_availability.py",
            "playback/engines/youtube.py",
            "services/cloud_music/public_youtube_resolver.py",
        ),
        CacheScope.PUBLIC_SHARED,
        _ONE_DAY_SECONDS,
        PrivacyClass.PUBLIC_DATA,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
        "Uses YouTube's public oEmbed endpoint only; this is not the YouTube Data API.",
    ),
    "api_vault_user_tools": _declaration(
        "api_vault_user_tools",
        "User-configured API Vault tools",
        ProviderDomain.INTEGRATION,
        ("services/api_vault/tool_factory.py",),
        CacheScope.USER_SCOPED,
        _FIVE_MINUTES_SECONDS,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.USER_BYOK,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "external_provider_health_probe": _declaration(
        "external_provider_health_probe",
        "External provider reachability probes",
        ProviderDomain.INTEGRATION,
        ("services/health/heartbeat.py",),
        CacheScope.PUBLIC_SHARED,
        _FIVE_MINUTES_SECONDS,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.METRICS,
    ),
    "cloud_storage_s3": _declaration(
        "cloud_storage_s3",
        "S3-compatible cloud storage",
        ProviderDomain.INTEGRATION,
        ("services/cloud_storage/service.py", "services/storage/s3_backend.py", "telephony/recording_storage.py"),
        CacheScope.NONE,
        None,
        PrivacyClass.USER_CONTENT,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "wake_data_upload": _declaration(
        "wake_data_upload",
        "Wake data upload and model update service",
        ProviderDomain.INTEGRATION,
        (
            "services/wake_word/training_telemetry.py",
            "services/violawake_client.py",
            "voice/wake_detector/sample_uploader.py",
            "voice/wake_detector/data_collection/model_updater.py",
            "voice/wake_detector/data_collection/uploader.py",
        ),
        CacheScope.USER_SCOPED,
        _ONE_HOUR_SECONDS,
        PrivacyClass.VOICE_AUDIO,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
    ),
    "multiroom_peer_http": _declaration(
        "multiroom_peer_http",
        "Multiroom peer HTTP control",
        ProviderDomain.INTEGRATION,
        ("services/multiroom/remote_client.py",),
        CacheScope.LOCAL_RUNTIME,
        int(TIMEOUT_MINUTE),
        PrivacyClass.LOCAL_ONLY,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.LOCAL_RESOURCE,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
    ),
    "deepgram_stt": _declaration(
        "deepgram_stt",
        "Deepgram streaming speech-to-text",
        ProviderDomain.INTEGRATION,
        ("voice/dictation/streaming_stt.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.VOICE_AUDIO,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.LOCAL_FALLBACK,
        OwnerAlertBehavior.OWNER_ALERT_ON_QUOTA_OR_SPEND,
    ),
    "telemetry_ingest": _declaration(
        "telemetry_ingest",
        "Viola telemetry ingest endpoint",
        ProviderDomain.INTEGRATION,
        ("telemetry/reporter.py", "telemetry/first_run.py"),
        CacheScope.LOCAL_RUNTIME,
        _ONE_HOUR_SECONDS,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.METRICS,
    ),
    "agentic_inbox_worker": _declaration(
        "agentic_inbox_worker",
        "Agentic inbox Cloudflare Worker",
        ProviderDomain.EMAIL,
        ("backend/inbox_worker_client.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
        "Founder ops agentic-inbox Cloudflare Worker for hello@useviola.com; "
        "Viola's user-facing agent never calls it.",
    ),
    "dictionary_api": _declaration(
        "dictionary_api",
        "dictionaryapi.dev definition lookup",
        ProviderDomain.SEARCH,
        ("intent/instant_commands/info.py",),
        CacheScope.PUBLIC_SHARED,
        _ONE_DAY_SECONDS,
        PrivacyClass.USER_QUERY,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
        "Public dictionaryapi.dev word-definition endpoint; free, no key or login.",
    ),
    "cloud_caldav_provisioner": _declaration(
        "cloud_caldav_provisioner",
        "Cloud CalDAV account provisioner",
        ProviderDomain.EMAIL,
        ("services/calendar/cloud_caldav.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.OPERATIONAL_METADATA,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.FAIL_CLOSED,
        OwnerAlertBehavior.SECURITY_OR_COMPLIANCE_ALERT,
        "Cloud-only Radicale CalDAV provisioner; creates per-user CalDAV accounts "
        "keyed to user_id via a dedicated provision token.",
    ),
    "runpod_voice_gpu": _declaration(
        "runpod_voice_gpu",
        "RunPod serverless GPU phone voice endpoint",
        ProviderDomain.TELEPHONY,
        ("telephony/remote_voice.py",),
        CacheScope.NONE,
        None,
        PrivacyClass.VOICE_AUDIO,
        RateLimitPosture.PROVIDER_AND_APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.LOCAL_FALLBACK,
        OwnerAlertBehavior.OWNER_ALERT_ON_QUOTA_OR_SPEND,
        "Optional RunPod serverless GPU (faster-whisper + Kokoro) for phone STT/TTS; "
        "default-OFF, falls back to the local CPU path on any failure.",
    ),
    "carddav_contacts": _declaration(
        "carddav_contacts",
        "CardDAV contacts server",
        ProviderDomain.EMAIL,
        ("services/contacts/providers/carddav.py",),
        CacheScope.USER_SCOPED,
        _FIVE_MINUTES_SECONDS,
        PrivacyClass.CONTACT_OR_MESSAGE,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.NO_DIRECT_SPEND,
        FallbackBehavior.REQUIRE_USER_ACTION,
        OwnerAlertBehavior.LOG_ONLY,
        "Read-only RFC 6352 CardDAV client (iCloud or any CardDAV host). The host is "
        "the user's own, discovered per-account from the well-known URI; credentials "
        "are the user's app-specific password from the desktop credential store.",
    ),
    "desktop_update_feed": _declaration(
        "desktop_update_feed",
        "Desktop update manifest and artifact feed",
        ProviderDomain.INTEGRATION,
        ("utils/update_checker.py", "utils/macos_updater.py"),
        CacheScope.PUBLIC_SHARED,
        _FIFTEEN_MINUTES_SECONDS,
        PrivacyClass.PUBLIC_DATA,
        RateLimitPosture.APP_LIMITED,
        SpendCapPosture.OWNER_BILLING_ACCOUNT,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
        "Public release manifest plus the installer/.app artifacts it points at. The "
        "manifest is TLS-trusted but unsigned, so the published SHA-256 is the "
        "integrity anchor on both apply legs; no user data is sent.",
    ),
    "sherpa_onnx_model_download": _declaration(
        "sherpa_onnx_model_download",
        "sherpa-onnx STT model artifact download",
        ProviderDomain.INTEGRATION,
        ("telephony/streaming_stt_model.py",),
        CacheScope.PUBLIC_SHARED,
        _NINETY_DAYS_SECONDS,
        PrivacyClass.PUBLIC_DATA,
        RateLimitPosture.PROVIDER_QUOTA,
        SpendCapPosture.PROVIDER_FREE,
        FallbackBehavior.DEGRADE_GRACEFULLY,
        OwnerAlertBehavior.LOG_ONLY,
        "One-time pinned sherpa-onnx streaming STT model tarball fetched from GitHub "
        "releases; sha256-verified; the streaming-STT feature ships default-OFF.",
    ),
}


OUTBOUND_PROVIDER_REGISTRY: Mapping[str, OutboundProviderDeclaration] = MappingProxyType(_DECLARATIONS)

LLM_PROVIDER_DECLARATION_IDS: Mapping[str, str] = MappingProxyType(
    {
        "openai": "openai",
        "openai_compatible": "openai_compatible",
        "anthropic": "anthropic",
        "google": "google_gemini",
        "ollama": "ollama_local",
    }
)

MUSIC_PROVIDER_DECLARATION_IDS: Mapping[str, str] = MappingProxyType(
    {
        "youtube_music": "youtube_music_browser",
        "youtube_iframe": "youtube_music_browser",
        "youtube": "youtube_music_browser",
        "spotify": "spotify_web",
        "spotify_cdp": "spotify_web",
    }
)

OUTBOUND_CALLSITE_PROVIDER_IDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "admin/alerts.py": (
            "cloudflare_email",
            "generic_alert_webhook",
            "pagerduty_owner_alerts",
            "pushover_owner_alerts",
            "resend_email",
            "telegram_owner_alerts",
            "telnyx_sms",
        ),
        "admin/cloud_health.py": ("searxng_search",),
        "services/oauth/workspace_bridge.py": ("google_oauth",),
        "auth/desktop_gotrue_proxy.py": ("viola_cloud_api",),
        "auth/desktop_session.py": ("viola_cloud_api",),
        "auth/cloud_gdpr.py": ("viola_cloud_api",),
        "auth/gotrue_proxy.py": ("viola_cloud_api",),
        "backend/fastapi_app.py": ("google_oauth",),
        "backend/inbox_worker_client.py": ("agentic_inbox_worker",),
        "backend/telegram_webhook.py": ("telegram_bot",),
        "backend/weather_fetch.py": (
            "airnow",
            "noaa_gfs_weather",
            "nws_weather",
            "openstreetmap_nominatim",
            "wttr_weather",
        ),
        "billing/providers/btcpay.py": ("btcpay_billing",),
        "billing/providers/stripe.py": ("stripe_billing",),
        "billing/routes.py": ("viola_cloud_api",),
        "chat/service.py": ("openai",),
        "intent/instant_commands/info.py": ("dictionary_api",),
        "intent/instant_commands/music.py": ("spotify_web",),
        "intent/tools/email.py": ("gmail_oauth", "imap_email", "smtp_email"),
        "intent/tools/music_connect.py": (
            "spotify_web",
            "youtube_music_browser",
        ),
        "intent/tools/phone_call.py": ("viola_cloud_api",),
        "intent/tools/telegram_tools.py": ("telegram_bot",),
        "intent/tools/vision_tools.py": ("openai",),
        "intent/tools/web_read.py": ("web_read_http",),
        "intent/tools/web_search.py": ("duckduckgo_search", "searxng_search"),
        "mcp_servers/browser/browser_manager.py": ("browser_navigation",),
        "mcp_servers/core_tools/server.py": ("telnyx_sms",),
        "music/consent/adapters/google_calendar.py": ("google_calendar", "google_oauth"),
        "music/consent/adapters/microsoft_calendar.py": ("microsoft_graph_calendar", "microsoft_oauth"),
        "music/consent/adapters/spotify.py": ("spotify_oauth",),
        "music/providers/browser_search.py": ("youtube_music_browser",),
        "music/providers/youtube_availability.py": ("youtube_oembed",),
        "music/spotify/cdp_controller.py": ("spotify_web",),
        "playback/engines/youtube.py": ("youtube_oembed",),
        "services/api_vault/tool_factory.py": ("api_vault_user_tools",),
        "services/browser/cloud_session_pool.py": ("browser_navigation",),
        "services/calendar/cloud_caldav.py": ("cloud_caldav_provisioner",),
        "services/calendar/providers/graph.py": ("microsoft_graph_calendar",),
        "services/capability_providers/home_assistant.py": ("home_assistant",),
        "services/contacts/providers/carddav.py": ("carddav_contacts",),
        "services/sync/http_transport.py": ("viola_cloud_api",),
        "services/playwright_cdp_client.py": ("browser_navigation",),
        "services/cloud_music/public_youtube_resolver.py": ("searxng_search", "youtube_oembed"),
        "services/cloud_storage/service.py": ("cloud_storage_s3",),
        "services/health/heartbeat.py": ("external_provider_health_probe",),
        "services/companion_client/client.py": ("viola_cloud_api",),
        "services/llm/codex_auth.py": ("codex_subscription",),
        "services/llm/factory.py": ("ollama_local",),
        "services/llm/local_models.py": ("ollama_local", "openai_compatible"),
        "services/llm/openai_direct.py": ("openai",),
        "services/llm/providers/cloud_managed_provider.py": ("viola_cloud_api",),
        "services/llm/providers/google_provider.py": ("google_gemini",),
        "services/llm/providers/openai_compatible.py": ("openai", "openai_compatible"),
        "services/multiroom/remote_client.py": ("multiroom_peer_http",),
        "services/openai_background.py": ("openai",),
        "services/payments/confirmation.py": ("viola_cloud_api",),
        "services/smart_home/home_assistant.py": ("home_assistant",),
        "services/storage/s3_backend.py": ("cloud_storage_s3",),
        "services/violawake_client.py": ("wake_data_upload",),
        "services/wake_word/training_telemetry.py": ("wake_data_upload",),
        "services/weather-gfs/weather_gfs/ingestion.py": ("noaa_gfs_weather",),
        "telemetry/first_run.py": ("telemetry_ingest",),
        "telemetry/reporter.py": ("telemetry_ingest",),
        "telephony/call_manager.py": ("openai", "telnyx_call_control"),
        "telephony/cloud_readiness.py": ("telnyx_call_control",),
        "telephony/cloud_routes.py": ("telnyx_call_control",),
        "telephony/desktop_cloud_proxy.py": ("viola_cloud_phone",),
        "telephony/phone_cloud_event_relay.py": ("viola_cloud_phone",),
        "telephony/remote_voice.py": ("runpod_voice_gpu",),
        "telephony/routes.py": ("telnyx_call_control",),
        "telephony/streaming_stt_model.py": ("sherpa_onnx_model_download",),
        "services/telnyx_sender.py": ("telnyx_sms",),
        "telephony/phone_simulator.py": ("openai",),
        "telephony/recording_storage.py": ("cloud_storage_s3",),
        "telephony/traced_openai_llm_service.py": ("openai",),
        "telephony/traced_openai_responses_llm_service.py": ("openai",),
        "ui/settings_api.py": (
            "ollama_local",
            "openai_compatible",
            "slack_bot",
            "telegram_bot",
        ),
        "ui/api/routes/smarthome.py": ("home_assistant",),
        "utils/enhancements/connection_pool.py": ("openai",),
        "utils/macos_updater.py": ("desktop_update_feed",),
        "utils/update_checker.py": ("desktop_update_feed",),
        "utils/weather_location.py": ("ip_api_geolocation",),
        "viola_email/providers/resend.py": ("resend_email",),
        "viola_email/providers/smtp.py": ("smtp_email",),
        "voice/transcription/cloud_adapter.py": ("openai",),
        "voice/dictation/streaming_stt.py": ("deepgram_stt",),
        "voice/wake_detector/data_collection/model_updater.py": ("wake_data_upload",),
        "voice/wake_detector/data_collection/uploader.py": ("wake_data_upload",),
        "voice/wake_detector/sample_uploader.py": ("wake_data_upload",),
    }
)

OUTBOUND_CALLSITE_EXEMPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "intent/capability_resolver.py": "Loopback desktop OAuth/login helper calls only.",
        "intent/agent_executor.py": "LLM provider calls are delegated to provider modules; direct HTTP detection here is a static false positive.",
        "intent/hooks/exec_http.py": "User-configured HTTP hooks; the hook owner supplies the destination and the SSRF guard runs before send.",
        "intent/instant_commands/smart_home.py": "Loopback desktop API calls only (base = http://localhost:{api_port} to /auth/oauth/start and /v1/browser/auth/login/spotify).",
        "intent/instant_commands/system.py": "Loopback desktop health probe only (http://127.0.0.1:8756/health/details).",
        "intent/tools/music_tools.py": "Loopback desktop playback API calls only.",
        "intent/tools/playlist_tools.py": "Loopback desktop playlist/playback API calls only.",
        "intent/tools/weather_tool.py": "Loopback desktop weather API call only.",
        "services/browser/qt_cdp_bridge.py": (
            "Loopback CDP relay only; the single httpx call reads "
            "http://127.0.0.1:<port>/json/version from the local Qt WebEngine DevTools endpoint."
        ),
        "ui/api/routes/lifecycle.py": "Local Qt/WebEngine lifecycle probe only.",
        "ui/api/routes/voice_stream.py": "Local streaming bridge probe only.",
        "ui/qt_native/health_monitor.py": "Loopback app health polling only.",
        "ui/qt_native/api_base.py": "Loopback app API session only.",
        "ui/qt_native/webview_window.py": "Loopback React bundle availability check only.",
        "utils/network/quality_detector.py": "Network quality utility; callers own provider declarations.",
        "utils/network/resilient_client.py": "Reusable HTTP client utility; callers own provider declarations.",
        "voice/oracle/live_voice_oracle.py": "Local voice oracle harness; every httpx call targets the running desktop instance at loopback DEFAULT_BASE_URL http://127.0.0.1:8756.",
    }
)


def get_provider_declaration(provider_id: str) -> OutboundProviderDeclaration | None:
    """Return a declaration by canonical provider id."""

    return OUTBOUND_PROVIDER_REGISTRY.get(provider_id.strip().lower())


def require_provider_declaration(provider_id: str) -> OutboundProviderDeclaration:
    """Return a declaration or raise ``KeyError`` with a clear missing-provider message."""

    declaration = get_provider_declaration(provider_id)
    if declaration is None:
        raise KeyError("Outbound provider declaration is missing for '%s'" % provider_id)
    return declaration


def list_provider_declarations(
    *,
    domain: ProviderDomain | None = None,
    privacy_class: PrivacyClass | None = None,
) -> tuple[OutboundProviderDeclaration, ...]:
    """List declarations, optionally filtered by domain or privacy class."""

    declarations = OUTBOUND_PROVIDER_REGISTRY.values()
    if domain is not None:
        declarations = [declaration for declaration in declarations if declaration.domain is domain]
    if privacy_class is not None:
        declarations = [declaration for declaration in declarations if declaration.privacy_class is privacy_class]
    return tuple(sorted(declarations, key=lambda declaration: declaration.provider_id))


def validate_registry(
    registry: Mapping[str, OutboundProviderDeclaration] = OUTBOUND_PROVIDER_REGISTRY,
) -> tuple[str, ...]:
    """Return deterministic validation errors for malformed declarations."""

    errors: list[str] = []
    for provider_id in sorted(registry):
        declaration = registry[provider_id]
        if provider_id != declaration.provider_id:
            errors.append("%s: key does not match provider_id %s" % (provider_id, declaration.provider_id))
        if provider_id != provider_id.lower() or " " in provider_id:
            errors.append("%s: provider_id must be lowercase snake_case without spaces" % provider_id)
        if not declaration.display_name.strip():
            errors.append("%s: display_name is required" % provider_id)
        if not declaration.integration_points:
            errors.append("%s: at least one integration_point is required" % provider_id)
        if declaration.cache_scope is CacheScope.NONE and declaration.cache_ttl_seconds not in (None, 0):
            errors.append("%s: cache_ttl_seconds must be empty when cache_scope is none" % provider_id)
        if declaration.cache_ttl_seconds is not None and declaration.cache_ttl_seconds < 0:
            errors.append("%s: cache_ttl_seconds must not be negative" % provider_id)

    for path, provider_ids in sorted(OUTBOUND_CALLSITE_PROVIDER_IDS.items()):
        if path in OUTBOUND_CALLSITE_EXEMPTIONS:
            errors.append("%s: callsite cannot be both registered and exempt" % path)
        if not provider_ids:
            errors.append("%s: callsite must list at least one provider id" % path)
        if "\\" in path or path.startswith("/") or path.endswith("/"):
            errors.append("%s: callsite path must be repo-relative with forward slashes" % path)
        for provider_id in provider_ids:
            if provider_id not in registry:
                errors.append("%s: unknown outbound provider id %s" % (path, provider_id))

    for path, reason in sorted(OUTBOUND_CALLSITE_EXEMPTIONS.items()):
        if "\\" in path or path.startswith("/") or path.endswith("/"):
            errors.append("%s: exemption path must be repo-relative with forward slashes" % path)
        if not reason.strip():
            errors.append("%s: exemption reason is required" % path)
    return tuple(errors)


__all__ = [
    "LLM_PROVIDER_DECLARATION_IDS",
    "MUSIC_PROVIDER_DECLARATION_IDS",
    "OUTBOUND_CALLSITE_EXEMPTIONS",
    "OUTBOUND_CALLSITE_PROVIDER_IDS",
    "OUTBOUND_PROVIDER_REGISTRY",
    "CacheScope",
    "FallbackBehavior",
    "OutboundProviderDeclaration",
    "OwnerAlertBehavior",
    "PrivacyClass",
    "ProviderDomain",
    "RateLimitPosture",
    "SpendCapPosture",
    "get_provider_declaration",
    "list_provider_declarations",
    "require_provider_declaration",
    "validate_registry",
]
