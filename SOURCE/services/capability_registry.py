"""
Capability Registry — Phase 0: Passive Observer.

Read-only registry that boots up, reads existing config state, and tracks
which capability domains are connected, available, or unknown.  Exposes
query methods for use by future phases (prompt generation, adaptive UI).

This phase changes nothing about how Viola currently works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from config.settings import AppConfig
    from ui.settings_manager import SettingsManager

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DomainState(str, Enum):
    """Connection state of a capability domain."""

    CONNECTED = "connected"
    AVAILABLE = "available"
    DISCOVERED = "discovered"
    UNKNOWN = "unknown"


class ProviderType(str, Enum):
    """How the capability is provided to Viola."""

    MCP = "mcp"
    NATIVE = "native"
    PLUGIN = "plugin"
    AGENT_TOOL = "agent_tool"


class HealthStatus(str, Enum):
    """Health of a capability domain."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"
    UNCHECKED = "unchecked"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SetupGuide:
    """Guidance for connecting an available-but-not-connected domain."""

    orthodox_path: str
    auto_setup_possible: bool = False
    requirements: list[str] = field(default_factory=list)
    estimated_effort: str = ""


@dataclass
class CapabilityDomain:
    """A single capability domain tracked by the registry."""

    domain_id: str
    display_name: str
    description: str
    state: DomainState
    provider: str | None = None
    provider_type: ProviderType = ProviderType.NATIVE
    tools: list[str] = field(default_factory=list)
    health: HealthStatus = HealthStatus.UNCHECKED
    last_health_check: datetime | None = None
    setup_guide: SetupGuide | None = None
    config_keys: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class CapabilityRegistry:
    """Singleton registry of all Viola capability domains."""

    _instance: CapabilityRegistry | None = None

    def __init__(self) -> None:
        self._domains: dict[str, CapabilityDomain] = {}

    # -- Query methods --

    def get_domain(self, domain_id: str) -> CapabilityDomain | None:
        return self._domains.get(domain_id)

    def get_active_domains(self) -> list[CapabilityDomain]:
        return [d for d in self._domains.values() if d.state == DomainState.CONNECTED]

    def get_available_domains(self) -> list[CapabilityDomain]:
        return [d for d in self._domains.values() if d.state == DomainState.AVAILABLE]

    def is_domain_connected(self, domain_id: str) -> bool:
        domain = self._domains.get(domain_id)
        return domain is not None and domain.state == DomainState.CONNECTED

    # -- Registration --

    def register_domain(self, domain: CapabilityDomain) -> None:
        self._domains[domain.domain_id] = domain

    def update_domain_state(self, domain_id: str, state: DomainState) -> None:
        domain = self._domains.get(domain_id)
        if domain is not None:
            domain.state = state

    # -- Health --

    def check_health(self, domain_id: str) -> HealthStatus:
        domain = self._domains.get(domain_id)
        if domain is None:
            return HealthStatus.UNCHECKED
        return domain.health

    def check_all_health(self) -> dict[str, HealthStatus]:
        return {d.domain_id: d.health for d in self._domains.values()}

    # -- Prompt generation --

    def get_active_capabilities_prompt(self) -> str:
        active = self.get_active_domains()
        if not active:
            return "No capabilities are currently connected."
        lines = ["Currently connected capabilities:"]
        for d in active:
            provider_info = " (via %s)" % d.provider if d.provider else ""
            lines.append("- %s%s: %s" % (d.display_name, provider_info, d.description))
        return "\n".join(lines)

    def get_llm_capability_context(self, domain_id: str | None = None) -> str:
        """Build dynamic capability context for the LLM system prompt.

        Args:
            domain_id: If provided, return context specific to this domain.
                       If None, return a general summary.

        Returns:
            Capability context string to inject into the system prompt.
        """
        lines: list[str] = []

        if domain_id:
            domain = self._domains.get(domain_id)
            if domain is None:
                return ""
            if domain.state == DomainState.CONNECTED:
                lines.append("DOMAIN: %s" % domain.display_name)
                if domain.provider:
                    lines.append("PROVIDER: %s" % domain.provider)
                if domain.tools:
                    lines.append("AVAILABLE TOOLS: %s" % ", ".join(domain.tools))
                lines.append("Use the available tools for this request. " "Do not improvise alternative approaches.")
            else:
                lines.append("DOMAIN: %s (NOT CONFIGURED)" % domain.display_name)
                lines.append(
                    "This capability is not set up. Do NOT attempt to solve this via "
                    "shell commands, browser automation, or other creative workarounds. "
                    "Instead, inform the user that this capability needs to be configured "
                    "and offer to help with setup."
                )
                if domain.setup_guide:
                    lines.append("RECOMMENDED SETUP: %s" % domain.setup_guide.orthodox_path)
        else:
            # General summary
            connected = self.get_active_domains()
            available = self.get_available_domains()
            if connected:
                lines.append("CONNECTED CAPABILITIES:")
                for d in connected:
                    provider_info = " (via %s)" % d.provider if d.provider else ""
                    tools_info = ""
                    if d.tools:
                        tools_info = " [tools: %s]" % ", ".join(d.tools)
                    lines.append("- %s%s: %s%s" % (d.display_name, provider_info, d.description, tools_info))
            if available:
                lines.append("\nNOT CONFIGURED (do not improvise for these domains):")
                for d in available:
                    setup_hint = ""
                    if d.setup_guide:
                        setup_hint = " — Setup: %s" % d.setup_guide.orthodox_path
                    lines.append("- %s: %s%s" % (d.display_name, d.description, setup_hint))

        return "\n".join(lines)

    # -- Bootstrap --

    @classmethod
    def bootstrap_from_config(
        cls,
        app_config: AppConfig,
        settings_manager: SettingsManager,
        user_id: str | None = None,
    ) -> CapabilityRegistry:
        registry = cls()
        cls._instance = registry

        _register_all_domains(
            registry,
            app_config,
            settings_manager,
            user_id=user_id,
        )

        connected = len([d for d in registry._domains.values() if d.state == DomainState.CONNECTED])
        available = len([d for d in registry._domains.values() if d.state == DomainState.AVAILABLE])
        logger.info(
            "CapabilityRegistry booted: %d domains (%d connected, %d available)",
            len(registry._domains),
            connected,
            available,
        )
        return registry

    @classmethod
    def get_instance(cls) -> CapabilityRegistry | None:
        return cls._instance


# ---------------------------------------------------------------------------
# Domain registration helpers
# ---------------------------------------------------------------------------


def _register_all_domains(
    registry: CapabilityRegistry,
    cfg: AppConfig,
    sm: SettingsManager,
    user_id: str | None = None,
) -> None:
    """Populate the registry from existing config values."""

    # -- Music --
    active_provider = sm.get_user_setting(user_id, "active_music_provider_id", None) if user_id else None
    music_connected = bool(active_provider)
    registry.register_domain(
        CapabilityDomain(
            domain_id="music",
            display_name="Music",
            description="Play, queue, search tracks, manage playlists, and control music from linked providers",
            state=DomainState.CONNECTED if music_connected else DomainState.AVAILABLE,
            provider=str(active_provider) if music_connected else None,
            provider_type=ProviderType.NATIVE,
            config_keys=["active_music_provider_id"],
            setup_guide=(
                None
                if music_connected
                else SetupGuide(
                    orthodox_path="Connect a music provider (YouTube Music, Spotify, or local files)",
                    auto_setup_possible=True,
                    requirements=["A music streaming account or local music library"],
                    estimated_effort="2 minutes",
                )
            ),
        )
    )

    # -- Calendar --
    cal_connected = True
    cal_provider: str | None = "local"
    if user_id:
        # Legacy credentials_path is unused — calendar uses OAuth tokens.
        # Check the consent vault (same pattern as email).
        try:
            from core.asyncio_safe import run_async_synchronously
            from services.calendar import get_calendar_manager

            async def _configured_calendar_providers() -> list[str]:
                manager = get_calendar_manager()
                providers = await manager.list_providers(user_id)
                return [
                    str(item.get("provider"))
                    for item in providers
                    if bool(item.get("configured")) and item.get("provider")
                ]

            configured_providers = run_async_synchronously(_configured_calendar_providers())

            if configured_providers:
                cal_provider = configured_providers[0] if len(configured_providers) == 1 else "multiple"
        except Exception:
            logger.debug("Calendar capability bootstrap check failed", exc_info=True)
    registry.register_domain(
        CapabilityDomain(
            domain_id="calendar",
            display_name="Calendar",
            description="View, create, and manage local-primary calendar events; separate from timers and recurring automations",
            state=DomainState.CONNECTED if cal_connected else DomainState.AVAILABLE,
            provider=cal_provider,
            provider_type=ProviderType.NATIVE,
            config_keys=[
                "google_client_id",
                "google_client_secret",
                "microsoft_client_id",
                "microsoft_client_secret",
            ],
            setup_guide=(
                None
                if cal_connected
                else SetupGuide(
                    orthodox_path="Use the local calendar immediately; connect Google, Microsoft 365/Outlook, or CalDAV only for sync",
                    auto_setup_possible=True,
                    requirements=["A user-scoped local calendar store; optional remote credentials for sync"],
                    estimated_effort="5 minutes",
                )
            ),
        )
    )

    # -- Weather (built-in, always connected) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="weather",
            display_name="Weather",
            description="Structured current weather conditions and forecasts for locations; not historical weather or weather news",
            state=DomainState.CONNECTED,
            provider="wttr.in",
            provider_type=ProviderType.NATIVE,
            config_keys=[],
        )
    )

    # -- Timer (built-in) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="timer",
            display_name="Timer",
            description="Set and check simple countdown timers; separate from calendars and recurring schedules",
            state=DomainState.CONNECTED,
            provider="built_in",
            provider_type=ProviderType.NATIVE,
            config_keys=[],
        )
    )

    # -- Scheduler (built-in) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="scheduler",
            display_name="Scheduler",
            description="Schedule reminders, recurring tasks, and future Viola actions; separate from calendar events",
            state=DomainState.CONNECTED,
            provider="built_in",
            provider_type=ProviderType.NATIVE,
            config_keys=[],
        )
    )

    # -- Messaging channels --
    _register_messaging_domain(
        registry, cfg, "telegram", "Telegram", "Send and receive Telegram messages", "telegram_enabled", sm=sm
    )
    # Slack is internal-pilot only (not surfaced in the Settings UI).
    # WhatsApp/Signal removed 2026-04-17 — product decision.
    _register_messaging_domain(
        registry, cfg, "slack", "Slack", "Send and receive Slack messages", "slack_enabled", sm=sm
    )

    # -- Email --
    try:
        from services.oauth.google import is_google_restricted_features_enabled

        google_email_available = is_google_restricted_features_enabled(cfg)
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        logger.debug("Google restricted feature gate lookup failed closed for email capability: %s", exc)
        google_email_available = False

    email_description = (
        "Read, compose, and manage email"
        if google_email_available
        else "Send email through configured Resend or SMTP backends"
    )
    email_setup_guide = (
        SetupGuide(
            orthodox_path="Connect an email account (Gmail, Outlook, or IMAP/SMTP)",
            auto_setup_possible=True,
            requirements=["An email account with API or IMAP access"],
            estimated_effort="5 minutes",
        )
        if google_email_available
        else SetupGuide(
            orthodox_path="Configure Resend or SMTP for outbound email",
            auto_setup_possible=False,
            requirements=["A Resend API key or SMTP credentials"],
            estimated_effort="5 minutes",
        )
    )
    registry.register_domain(
        CapabilityDomain(
            domain_id="email",
            display_name="Email",
            description=email_description,
            state=DomainState.AVAILABLE,
            provider_type=ProviderType.NATIVE,
            setup_guide=email_setup_guide,
        )
    )

    # -- AI Chat / LLM --
    llm_connected = bool(cfg.llm_backend)
    registry.register_domain(
        CapabilityDomain(
            domain_id="ai_chat",
            display_name="AI Chat",
            description="Natural language conversation and question answering",
            state=DomainState.CONNECTED if llm_connected else DomainState.AVAILABLE,
            provider=cfg.llm_backend if llm_connected else None,
            provider_type=ProviderType.NATIVE,
            config_keys=["llm_backend"],
            setup_guide=(
                None
                if llm_connected
                else SetupGuide(
                    orthodox_path="Configure an LLM provider (OpenAI, Anthropic, Google, or Ollama)",
                    auto_setup_possible=True,
                    requirements=["An API key or local Ollama installation"],
                    estimated_effort="2 minutes",
                )
            ),
        )
    )

    # -- Agent --
    agent_connected = bool(cfg.agent_enabled)
    registry.register_domain(
        CapabilityDomain(
            domain_id="agent",
            display_name="Agent",
            description="Autonomous tool-use loop for complex multi-step tasks",
            state=DomainState.CONNECTED if agent_connected else DomainState.AVAILABLE,
            provider="built_in" if agent_connected else None,
            provider_type=ProviderType.NATIVE,
            config_keys=["agent_enabled"],
            setup_guide=(
                None
                if agent_connected
                else SetupGuide(
                    orthodox_path="Enable agent mode through Viola's setup flow",
                    auto_setup_possible=True,
                    requirements=["A configured LLM provider"],
                    estimated_effort="1 minute",
                )
            ),
        )
    )

    # -- Web Search (agent tool, always connected) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="web_search",
            display_name="Web Search",
            description="Search public web snippets for current information when no structured or connected tool covers the request",
            state=DomainState.CONNECTED,
            provider="built_in",
            provider_type=ProviderType.AGENT_TOOL,
            config_keys=[],
        )
    )

    # -- Browser (agent tool, always connected) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="browser",
            display_name="Browser",
            description="Open specific pages for full-page reading, account or commerce flows, and site interaction",
            state=DomainState.CONNECTED,
            provider="playwright",
            provider_type=ProviderType.AGENT_TOOL,
            config_keys=[],
        )
    )

    # -- Wake Word --
    wake_connected = bool(cfg.wake_enabled)
    registry.register_domain(
        CapabilityDomain(
            domain_id="wake_word",
            display_name="Wake Word",
            description="Hands-free voice activation via wake word detection",
            state=DomainState.CONNECTED if wake_connected else DomainState.AVAILABLE,
            provider="violawake" if wake_connected else None,
            provider_type=ProviderType.NATIVE,
            config_keys=["wake_enabled"],
            setup_guide=(
                None
                if wake_connected
                else SetupGuide(
                    orthodox_path="Enable wake-word listening through Viola's setup flow",
                    auto_setup_possible=True,
                    requirements=["A microphone"],
                    estimated_effort="1 minute",
                )
            ),
        )
    )

    # -- STT --
    stt_connected = bool(cfg.stt_backend) and cfg.stt_backend != "none"
    registry.register_domain(
        CapabilityDomain(
            domain_id="stt",
            display_name="Speech-to-Text",
            description="Transcribe voice input to text",
            state=DomainState.CONNECTED if stt_connected else DomainState.AVAILABLE,
            provider=cfg.stt_backend if stt_connected else None,
            provider_type=ProviderType.NATIVE,
            config_keys=["stt_backend"],
            setup_guide=(
                None
                if stt_connected
                else SetupGuide(
                    orthodox_path="Configure speech recognition with a microphone and STT backend",
                    auto_setup_possible=True,
                    requirements=["A microphone and Whisper model or cloud STT API key"],
                    estimated_effort="2 minutes",
                )
            ),
        )
    )

    # -- TTS --
    tts_connected = bool(cfg.tts_enabled)
    registry.register_domain(
        CapabilityDomain(
            domain_id="tts",
            display_name="Text-to-Speech",
            description="Speak responses aloud with natural-sounding voice",
            state=DomainState.CONNECTED if tts_connected else DomainState.AVAILABLE,
            provider=cfg.tts_backend if tts_connected else None,
            provider_type=ProviderType.NATIVE,
            config_keys=["tts_enabled"],
            setup_guide=(
                None
                if tts_connected
                else SetupGuide(
                    orthodox_path="Enable voice output with an available speaker or audio device",
                    auto_setup_possible=True,
                    requirements=["A speaker or audio output device"],
                    estimated_effort="1 minute",
                )
            ),
        )
    )

    # -- Multiroom --
    registry.register_domain(
        CapabilityDomain(
            domain_id="multiroom",
            display_name="Multi-Room Audio",
            description="Synchronized audio playback across multiple devices",
            state=DomainState.CONNECTED,
            provider="hub_spoke",
            provider_type=ProviderType.NATIVE,
            config_keys=[],
            setup_guide=None,
        )
    )

    # -- Desktop Control (agent tool, always connected) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="desktop_control",
            display_name="Desktop Control",
            description="Control desktop applications, manage windows, and automate tasks",
            state=DomainState.CONNECTED,
            provider="built_in",
            provider_type=ProviderType.AGENT_TOOL,
            config_keys=[],
        )
    )

    # -- Smart Home (not implemented yet) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="smart_home",
            display_name="Smart Home",
            description="Control lights, thermostats, locks, scenes, and other smart devices; separate from multiroom music playback",
            state=DomainState.AVAILABLE,
            provider=None,
            provider_type=ProviderType.MCP,
            config_keys=[],
            setup_guide=SetupGuide(
                orthodox_path="Connect to a smart home system on your network or cloud account",
                auto_setup_possible=True,
                requirements=["A smart home system (Home Assistant, Hue, SmartThings, etc.)"],
                estimated_effort="5 minutes",
            ),
        )
    )

    # -- Commerce (Stripe-based payment links) --
    registry.register_domain(
        CapabilityDomain(
            domain_id="commerce",
            display_name="Commerce",
            description="Order products and services, process payments via Stripe checkout links",
            state=DomainState.CONNECTED,
            provider="stripe",
            provider_type=ProviderType.AGENT_TOOL,
            config_keys=[],
        )
    )


def _register_messaging_domain(
    registry: CapabilityRegistry,
    cfg: AppConfig,
    domain_id: str,
    display_name: str,
    description: str,
    config_key: str,
    sm: Any | None = None,
) -> None:
    """Helper to register a messaging channel domain."""
    # Check BOTH AppConfig (.env) and SettingsManager (settings.json).
    # Users configure Telegram/WhatsApp/etc. via the UI (SettingsManager),
    # not by editing .env, so we must check both sources.
    connected = bool(getattr(cfg, config_key, False))
    if not connected and sm is not None:
        connected = bool(sm.get(config_key, False))
    registry.register_domain(
        CapabilityDomain(
            domain_id=domain_id,
            display_name=display_name,
            description=description,
            state=DomainState.CONNECTED if connected else DomainState.AVAILABLE,
            provider=domain_id if connected else None,
            provider_type=ProviderType.NATIVE,
            config_keys=[config_key],
            setup_guide=(
                None
                if connected
                else SetupGuide(
                    orthodox_path=f"Connect {display_name} through the relevant account or bot-token setup flow",
                    auto_setup_possible=False,
                    requirements=[f"A {display_name} bot token or account"],
                    estimated_effort="5 minutes",
                )
            ),
        )
    )
