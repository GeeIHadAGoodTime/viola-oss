"""Static connector manifests for LLM, music, and smart-home sources."""

from __future__ import annotations

from config.defaults import DEFAULT_CODEX_MODEL, DEFAULT_MANAGED_MODEL
from core.constants import OLLAMA_DEFAULT_BASE_URL
from services.connectors.models import ConnectorAction, ConnectorManifest

SCHEMA_VERSION = 1

CONFIGURE_SETTINGS = ConnectorAction("configure", "Configure", "GET", "/v1/settings")
VALIDATE_LLM = ConnectorAction("validate", "Test connection", "POST", "/v1/settings/validate-llm")
FIND_LOCAL_MODELS = ConnectorAction("discover_models", "Find installed models", "GET", "/v1/settings/local-ai")
SELECT_MUSIC = ConnectorAction(
    "select",
    "Use this source",
    "POST",
    "/v1/music/sources/{source_id}/select",
    mutates_selection=True,
)
START_CONSENT = ConnectorAction(
    "connect",
    "Connect account",
    "POST",
    "/v1/consent/session",
    mutates_connection=True,
)
REVOKE_CONSENT = ConnectorAction(
    "disconnect",
    "Disconnect account",
    "POST",
    "/v1/consent/revoke",
    mutates_connection=True,
    destructive=True,
)


def _llm_actions(*, local: bool = False) -> tuple[ConnectorAction, ...]:
    if local:
        return (CONFIGURE_SETTINGS, VALIDATE_LLM, FIND_LOCAL_MODELS)
    return (CONFIGURE_SETTINGS, VALIDATE_LLM)


def _llm_capabilities(
    *,
    native_tools: bool | str,
    model_discovery: bool | str,
    streaming: bool | str = "provider_dependent",
    context_detection: bool | str = "provider_dependent",
    local: bool = False,
) -> dict[str, object]:
    return {
        "chat": True,
        "agent_routing": True,
        "native_tool_contract": native_tools,
        "model_discovery": model_discovery,
        "streaming": streaming,
        "context_detection": context_detection,
        "local_only": local,
    }


def _cloud_llm(
    *,
    connector_id: str,
    display_name: str,
    provider_key: str,
    adapter: str,
    description: str,
    base_url: str | None = None,
    default_models: tuple[str, ...] = (),
    popular_models: tuple[str, ...] = (),
    native_tools: bool | str = "provider_supported_adapter_pending",
    docs_url: str | None = None,
    extra_capabilities: dict[str, object] | None = None,
) -> ConnectorManifest:
    setting_hints: dict[str, object] = {
        "ai_source": "byok",
        "llm_provider": adapter,
    }
    if base_url:
        setting_hints["llm_base_url"] = base_url
    capabilities = _llm_capabilities(native_tools=native_tools, model_discovery="api_if_supported")
    if extra_capabilities:
        capabilities.update(extra_capabilities)
    return ConnectorManifest(
        id=connector_id,
        category="llm",
        display_name=display_name,
        description=description,
        connector_kind="llm_provider",
        provider_key=provider_key,
        adapter=adapter,
        auth_type="api_key",
        privacy_boundary="cloud",
        requires_api_key=True,
        default_base_url=base_url,
        default_models=default_models,
        popular_models=popular_models,
        capabilities=capabilities,
        actions=_llm_actions(),
        tags=("byok", "cloud"),
        docs_url=docs_url,
        setting_hints=setting_hints,
    )


# Named local-server presets for the generic OpenAI-compatible connector. A
# Viola user never installs these; Viola only detects an OpenAI-compatible
# server they already run. LM Studio, vLLM, llama.cpp, and text-generation-webui
# are all the *same* `openai_compatible` adapter differing only by default port,
# so they are presets of one connector, not four separate connectors.
LOCAL_OPENAI_COMPATIBLE_PRESETS: tuple[dict[str, str], ...] = (
    {"name": "LM Studio", "base_url": "http://localhost:1234/v1", "docs_url": "https://lmstudio.ai/docs"},
    {"name": "vLLM", "base_url": "http://localhost:8000/v1", "docs_url": "https://docs.vllm.ai/"},
    {"name": "llama.cpp / LocalAI", "base_url": "http://localhost:8080/v1", "docs_url": ""},
    {"name": "text-generation-webui", "base_url": "http://localhost:5000/v1", "docs_url": ""},
)


def _local_llm(
    *,
    connector_id: str,
    display_name: str,
    provider_key: str,
    adapter: str,
    description: str,
    base_url: str,
    native_tools: bool | str = "adapter_pending",
    docs_url: str | None = None,
) -> ConnectorManifest:
    return ConnectorManifest(
        id=connector_id,
        category="llm",
        display_name=display_name,
        description=description,
        connector_kind="local_llm_server",
        provider_key=provider_key,
        adapter=adapter,
        auth_type="local_server",
        privacy_boundary="local",
        requires_api_key=False,
        default_base_url=base_url,
        capabilities=_llm_capabilities(
            native_tools=native_tools,
            model_discovery=True,
            streaming="provider_dependent",
            context_detection="probe_required",
            local=True,
        ),
        actions=_llm_actions(local=True),
        tags=("local",),
        docs_url=docs_url,
        setting_hints={
            "ai_source": "local",
            "llm_provider": adapter,
            "llm_base_url": base_url,
        },
    )


CONNECTOR_MANIFESTS: tuple[ConnectorManifest, ...] = (
    ConnectorManifest(
        id="llm.managed",
        category="llm",
        display_name="Viola Managed AI",
        description="Viola-managed OpenAI-backed model routing for the default product path.",
        connector_kind="managed_llm",
        provider_key="managed",
        adapter="openai",
        auth_type="managed",
        privacy_boundary="managed_cloud",
        default_models=(DEFAULT_MANAGED_MODEL,),
        popular_models=(DEFAULT_MANAGED_MODEL,),
        capabilities=_llm_capabilities(native_tools=True, model_discovery=False, context_detection=True),
        actions=(CONFIGURE_SETTINGS,),
        tags=("managed", "cloud"),
        setting_hints={"ai_source": "managed"},
    ),
    ConnectorManifest(
        id="llm.codex",
        category="llm",
        display_name="Codex Subscription",
        description="Local Codex auth bridge for ChatGPT/Codex subscription-backed OpenAI Responses API calls.",
        connector_kind="subscription_llm",
        provider_key="codex",
        adapter="openai",
        auth_type="subscription",
        privacy_boundary="cloud",
        default_models=(DEFAULT_CODEX_MODEL,),
        popular_models=(DEFAULT_CODEX_MODEL, "gpt-5.3-codex-spark", "gpt-5.3-codex"),
        capabilities=_llm_capabilities(native_tools=True, model_discovery=False, context_detection=True),
        actions=(CONFIGURE_SETTINGS,),
        tags=("subscription", "cloud"),
        setting_hints={"ai_source": "codex", "llm_provider": "openai"},
    ),
    _cloud_llm(
        connector_id="llm.openai",
        display_name="OpenAI",
        provider_key="openai",
        adapter="openai",
        description="Native OpenAI API provider.",
        default_models=("gpt-5.4-mini", "gpt-5.4", "gpt-4o", "gpt-4o-mini"),
        popular_models=("gpt-5.4-mini", "gpt-5.4"),
        native_tools=True,
        docs_url="https://platform.openai.com/docs",
    ),
    _cloud_llm(
        connector_id="llm.anthropic",
        display_name="Anthropic",
        provider_key="anthropic",
        adapter="anthropic",
        description="Native Anthropic Claude API provider.",
        default_models=("claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929", "claude-opus-4-6"),
        popular_models=("claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"),
        native_tools=True,
        docs_url="https://docs.anthropic.com/",
    ),
    _cloud_llm(
        connector_id="llm.google",
        display_name="Google Gemini",
        provider_key="google",
        adapter="google",
        description="Native Google Gemini API provider.",
        default_models=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-pro-latest"),
        popular_models=("gemini-2.5-flash", "gemini-2.5-pro"),
        native_tools=True,
        docs_url="https://ai.google.dev/gemini-api/docs",
    ),
    _cloud_llm(
        connector_id="llm.openrouter",
        display_name="OpenRouter",
        provider_key="openrouter",
        adapter="openai_compatible",
        description="OpenAI-compatible multi-provider routing through OpenRouter.",
        base_url="https://openrouter.ai/api/v1",
        default_models=(
            "~google/gemini-flash-latest",
            "google/gemini-3.1-flash-lite",
            "~openai/gpt-mini-latest",
        ),
        popular_models=(
            "~google/gemini-flash-latest",
            "google/gemini-3.1-flash-lite",
        ),
        native_tools="provider_dependent",
        docs_url="https://openrouter.ai/docs",
    ),
    _cloud_llm(
        connector_id="llm.groq",
        display_name="Groq",
        provider_key="groq",
        adapter="openai_compatible",
        description="OpenAI-compatible Groq API provider.",
        base_url="https://api.groq.com/openai/v1",
        default_models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"),
        popular_models=("llama-3.3-70b-versatile", "mixtral-8x7b-32768"),
        native_tools="provider_dependent",
    ),
    _cloud_llm(
        connector_id="llm.together",
        display_name="Together AI",
        provider_key="together",
        adapter="openai_compatible",
        description="OpenAI-compatible Together AI provider.",
        base_url="https://api.together.xyz/v1",
        default_models=(
            "Qwen/Qwen2.5-7B-Instruct-Turbo",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "openai/gpt-oss-20b",
        ),
        popular_models=("Qwen/Qwen2.5-7B-Instruct-Turbo", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        native_tools="provider_dependent",
    ),
    _cloud_llm(
        connector_id="llm.fireworks",
        display_name="Fireworks AI",
        provider_key="fireworks",
        adapter="openai_compatible",
        description="OpenAI-compatible Fireworks AI provider.",
        base_url="https://api.fireworks.ai/inference/v1",
        default_models=(
            "accounts/fireworks/models/gpt-oss-120b",
            "accounts/fireworks/models/kimi-k2p5",
            "accounts/fireworks/models/deepseek-v4-pro",
        ),
        popular_models=("accounts/fireworks/models/gpt-oss-120b", "accounts/fireworks/models/kimi-k2p5"),
        native_tools="provider_dependent",
    ),
    _cloud_llm(
        connector_id="llm.deepseek",
        display_name="DeepSeek",
        provider_key="deepseek",
        adapter="openai_compatible",
        description="OpenAI-compatible DeepSeek provider.",
        base_url="https://api.deepseek.com/v1",
        default_models=("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"),
        popular_models=("deepseek-v4-flash", "deepseek-v4-pro"),
        native_tools="provider_dependent",
    ),
    _cloud_llm(
        connector_id="llm.mistral",
        display_name="Mistral",
        provider_key="mistral",
        adapter="openai_compatible",
        description="OpenAI-compatible Mistral provider.",
        base_url="https://api.mistral.ai/v1",
        default_models=("mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"),
        popular_models=("mistral-large-latest", "mistral-small-latest"),
        native_tools="provider_dependent",
    ),
    _cloud_llm(
        connector_id="llm.perplexity",
        display_name="Perplexity",
        provider_key="perplexity",
        adapter="openai_compatible",
        description="OpenAI-compatible Perplexity provider.",
        base_url="https://api.perplexity.ai",
        native_tools="provider_dependent",
    ),
    _cloud_llm(
        connector_id="llm.xai",
        display_name="xAI",
        provider_key="xai",
        adapter="openai_compatible",
        description="OpenAI-compatible xAI provider.",
        base_url="https://api.x.ai/v1",
        default_models=("grok-4.3", "grok-4.3-fast", "grok-3-mini"),
        popular_models=("grok-4.3", "grok-3-mini"),
        native_tools="provider_dependent",
        docs_url="https://docs.x.ai/",
    ),
    _cloud_llm(
        connector_id="llm.cohere",
        display_name="Cohere",
        provider_key="cohere",
        adapter="openai_compatible",
        description="OpenAI-compatible Cohere compatibility provider.",
        base_url="https://api.cohere.ai/compatibility/v1",
        default_models=("command-a-03-2025", "command-r-08-2024", "command-r-plus-08-2024"),
        popular_models=("command-a-03-2025", "command-r-08-2024"),
        native_tools="provider_dependent",
        docs_url="https://docs.cohere.com/",
    ),
    _cloud_llm(
        connector_id="llm.openai_compatible",
        display_name="OpenAI-Compatible API",
        provider_key="openai_compatible",
        adapter="openai_compatible",
        description=(
            "Any OpenAI-compatible endpoint with a user-supplied base URL — a "
            "cloud API or a local server. Local servers (LM Studio, vLLM, "
            "llama.cpp, text-generation-webui) are detected automatically and "
            "offered as base-URL presets."
        ),
        native_tools="provider_dependent",
        extra_capabilities={
            "supports_local_server": True,
            "local_presets": [dict(preset) for preset in LOCAL_OPENAI_COMPATIBLE_PRESETS],
        },
    ),
    _local_llm(
        connector_id="llm.ollama",
        display_name="Ollama",
        provider_key="ollama",
        adapter="ollama",
        description="Local Ollama server or installed Ollama models.",
        base_url=OLLAMA_DEFAULT_BASE_URL,
        native_tools="parsed_tool_calls_adapter_partial",
        docs_url="https://github.com/ollama/ollama/blob/main/docs/api.md",
    ),
    ConnectorManifest(
        id="music.spotify",
        category="music",
        display_name="Spotify",
        description="Spotify music account and browser/CDP playback session.",
        connector_kind="music_source",
        provider_key="spotify",
        adapter="spotify_cdp",
        auth_type="oauth_or_browser_session",
        privacy_boundary="cloud",
        capabilities={"playback": True, "search": True, "browser_session": True},
        actions=(
            SELECT_MUSIC,
            ConnectorAction("connect", "Sign in", "POST", "/v1/browser/auth/login/spotify", mutates_connection=True),
            ConnectorAction(
                "disconnect",
                "Sign out",
                "POST",
                "/v1/spotify/cdp/disconnect",
                mutates_connection=True,
                destructive=True,
            ),
        ),
        tags=("music", "oauth", "browser"),
        setting_hints={"active_music_provider_id": "spotify"},
    ),
    ConnectorManifest(
        id="music.youtube_music",
        category="music",
        display_name="YouTube Music",
        description="YouTube Music account and browser playback session.",
        connector_kind="music_source",
        provider_key="youtube_music",
        adapter="youtube_iframe",
        auth_type="oauth_or_browser_session",
        privacy_boundary="cloud",
        capabilities={"playback": True, "search": True, "browser_session": True, "account_optional": True},
        actions=(
            SELECT_MUSIC,
            ConnectorAction(
                "connect",
                "Sign in",
                "POST",
                "/v1/browser/auth/login/youtube_music",
                mutates_connection=True,
            ),
            REVOKE_CONSENT,
        ),
        tags=("music", "oauth", "browser"),
        setting_hints={"active_music_provider_id": "youtube_music"},
    ),
    ConnectorManifest(
        id="music.local",
        category="music",
        display_name="Local Files",
        description="Local music library on this device.",
        connector_kind="music_source",
        provider_key="local",
        adapter="local",
        auth_type="local_path",
        privacy_boundary="local",
        capabilities={"playback": True, "search": True, "browser_session": False},
        actions=(SELECT_MUSIC, CONFIGURE_SETTINGS),
        tags=("music", "local"),
        setting_hints={"active_music_provider_id": "local", "local_music_folder": ""},
    ),
    ConnectorManifest(
        id="smart_home.network_discovery",
        category="smart_home",
        display_name="Network Discovery",
        description="Opt-in LAN discovery scan for smart-home hubs and devices.",
        connector_kind="device_discovery",
        provider_key="network_discovery",
        adapter="network_discovery",
        auth_type="local_network_scan",
        privacy_boundary="local",
        capabilities={"scan": True, "mdns": True, "known_port_scan": True},
        actions=(
            CONFIGURE_SETTINGS,
            ConnectorAction("discover", "Find devices", "POST", "/v1/smarthome/discover"),
        ),
        tags=("smart_home", "local"),
        setting_hints={"network_discovery_enabled": True},
    ),
    ConnectorManifest(
        id="smart_home.home_assistant",
        category="smart_home",
        display_name="Home Assistant",
        description="Home Assistant URL and long-lived access token connection.",
        connector_kind="smart_home_hub",
        provider_key="home_assistant",
        adapter="home_assistant",
        auth_type="url_token",
        privacy_boundary="local_or_lan",
        capabilities={"control": True, "entity_discovery": True},
        actions=(
            CONFIGURE_SETTINGS,
            ConnectorAction("validate", "Test connection", "POST", "/v1/smarthome/test-connection"),
        ),
        tags=("smart_home", "home_assistant"),
        setting_hints={"home_assistant_url": "", "home_assistant_token": ""},
    ),
)


def list_connector_manifests(category: str | None = None) -> list[ConnectorManifest]:
    normalized = (category or "").strip().lower()
    manifests = list(CONNECTOR_MANIFESTS)
    if not normalized:
        return manifests
    return [manifest for manifest in manifests if manifest.category == normalized]


def get_connector_manifest(connector_id: str) -> ConnectorManifest | None:
    normalized = (connector_id or "").strip().lower()
    for manifest in CONNECTOR_MANIFESTS:
        if manifest.id == normalized:
            return manifest
    return None


def build_connector_manifest_payload(category: str | None = None) -> dict[str, object]:
    manifests = list_connector_manifests(category)
    categories = sorted({manifest.category for manifest in manifests})
    return {
        "schema_version": SCHEMA_VERSION,
        "categories": categories,
        "connectors": [manifest.to_dict() for manifest in manifests],
        "count": len(manifests),
    }
