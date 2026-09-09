"""
LLM Provider Factory

Creates and manages LLM providers based on configuration.
Single entry point for provider instantiation.
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from core.exceptions import ErrorContext, ProviderUnavailableError, ViolaError
from core.logging_config import get_logger
from services.llm.local_models import (
    best_local_model,
    looks_like_cloud_model,
    ollama_model_matches,
)
from services.llm.providers.base import (
    BaseLLMProvider,
    LLMConfig,
    LLMProviderType,
    LLMTestResult,
    get_all_providers,
    get_provider_info,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle


class ManagedLLMAuthRequired(ViolaError):
    """Raised when a Viola-managed LLM request has no authenticated account."""

    def __init__(self) -> None:
        from core.account_gate import (
            LOGIN_REQUIRED_FOR_PAID_ACTION,
            paid_action_login_required_data,
        )

        self.error_code = LOGIN_REQUIRED_FOR_PAID_ACTION
        self.data = paid_action_login_required_data(
            action="managed_llm",
            message="Sign in to use Viola-managed AI.",
        )
        super().__init__(
            self.data["message"],
            context=ErrorContext(
                component="services.llm.factory",
                operation="create_managed_provider",
                params={"error_code": self.error_code},
                user_message=self.data["message"],
                recovery_hint="Sign in or switch AI source to BYOK, Codex, or local.",
            ),
        )


class _UnavailableLLMProvider(BaseLLMProvider):
    """Provider stub used to keep a blocked primary visible in fallback status."""

    def __init__(
        self,
        *,
        provider_type: str,
        provider_name: str,
        model: str,
        reason: str,
        error_code: str | None = None,
    ) -> None:
        super().__init__(LLMConfig(provider=provider_type, model=model))
        self._provider_name = provider_name
        self._unavailable_reason = reason
        self._error_code = error_code
        self.last_error = reason

    async def ask(
        self,
        question: str,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        raise ProviderUnavailableError(self.get_provider_name(), self._unavailable_reason)

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
    ) -> dict[str, Any]:
        raise ProviderUnavailableError(self.get_provider_name(), self._unavailable_reason)

    async def route_command_native(
        self,
        messages: list[dict[str, Any]],
        *,
        native_tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        prompt_context_bundle: PromptFrameBundle | None = None,
        max_tokens: int | None = 1024,
        model_override: str | None = None,
        first_turn: bool = False,
        tool_choice_override: Any | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Concrete implementation required so this placeholder can be
        # instantiated as the "unavailable primary" in a fallback chain. It is
        # never routed to (the chain skips unavailable providers); if it ever
        # is, fail loud with the same unavailable signal as the other paths.
        raise ProviderUnavailableError(self.get_provider_name(), self._unavailable_reason)

    def is_available(self) -> bool:
        return False

    async def test_connection(self) -> LLMTestResult:
        return LLMTestResult(
            success=False,
            message=self._unavailable_reason,
            error_code=self._error_code,
        )

    def get_available_models(self) -> list[str]:
        return [self.config.model] if self.config.model else []

    def get_provider_name(self) -> str:
        return self._provider_name

    def get_unavailable_reason(self) -> str | None:
        return self._unavailable_reason


class LLMProviderFactory:
    """
    Factory for creating LLM providers.

    Centralizes provider creation and configuration loading.
    """

    # Provider class mapping (lazy loaded to avoid circular imports)
    _provider_classes: dict[str, type[BaseLLMProvider]] | None = None
    _agents_sdk_fallback_warning_logged = False
    _stale_byok_warning_logged = False
    _NO_PROFILE_PROVIDER = object()

    @classmethod
    def _get_provider_classes(cls) -> dict[str, type[BaseLLMProvider]]:
        """Get provider class mapping (lazy loaded)."""
        if cls._provider_classes is None:
            from services.llm.providers.anthropic_provider import AnthropicProvider
            from services.llm.providers.google_provider import GoogleProvider
            from services.llm.providers.ollama_native_provider import (
                OllamaNativeProvider,
            )
            from services.llm.providers.openai_compatible import (
                OpenAICompatibleProvider,
            )

            cls._provider_classes = {
                LLMProviderType.OPENAI.value: OpenAICompatibleProvider,
                LLMProviderType.ANTHROPIC.value: AnthropicProvider,
                LLMProviderType.GOOGLE.value: GoogleProvider,
                LLMProviderType.OLLAMA.value: OllamaNativeProvider,
                LLMProviderType.OPENAI_COMPATIBLE.value: OpenAICompatibleProvider,
            }
        return cls._provider_classes

    @classmethod
    def create_provider(cls, config: LLMConfig) -> BaseLLMProvider:
        """
        Create appropriate provider based on config.

        Args:
            config: LLM configuration

        Returns:
            Instantiated provider

        Raises:
            ValueError: If provider type is unknown
        """
        if config.provider == LLMProviderType.OPENAI_COMPATIBLE.value and not (config.model or "").strip():
            raise ValueError("OpenAI-compatible providers require an explicit llm_model.")

        # Check if OpenAI Agents SDK provider is enabled for OpenAI-type providers
        if config.provider in (
            LLMProviderType.OPENAI.value,
            LLMProviderType.OPENAI_COMPATIBLE.value,
        ):
            from services.llm.providers.openai_agents_provider import (
                should_use_openai_agents_sdk,
            )

            if should_use_openai_agents_sdk(config):
                from services.llm.providers.openai_agents_provider import (
                    OpenAIAgentsProvider,
                )

                logger.info("Creating OpenAI Agents SDK provider with model %s", config.model)
                provider = OpenAIAgentsProvider(config)
                if provider.is_available():
                    return provider
                log_fn = logger.debug if cls._agents_sdk_fallback_warning_logged else logger.warning
                log_fn(
                    "OpenAI Agents SDK provider unavailable (%s); falling back to OpenAI-compatible provider",
                    provider.last_error or "unknown error",
                )
                cls._agents_sdk_fallback_warning_logged = True

        provider_classes = cls._get_provider_classes()

        provider_class = provider_classes.get(config.provider)
        if not provider_class:
            raise ValueError(
                f"Unknown provider: {config.provider}. " f"Supported providers: {list(provider_classes.keys())}"
            )

        logger.info("Creating %s provider with model %s", config.provider, config.model)
        return provider_class(config)

    # Provider types that run locally and never send data to the cloud
    _LOCAL_PROVIDERS = frozenset({LLMProviderType.OLLAMA.value})
    _LOCAL_BASE_URL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # nosec B104

    @classmethod
    def _is_local_base_url(cls, base_url: str | None) -> bool:
        """Return True when *base_url* points to a loopback/local endpoint."""
        if not base_url:
            return False
        candidate = base_url.strip()
        if not candidate:
            return False
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        parsed = urlparse(candidate)
        hostname = (parsed.hostname or "").lower()
        return hostname in cls._LOCAL_BASE_URL_HOSTS

    @classmethod
    def _is_ollama_native_base_url(cls, base_url: str | None) -> bool:
        """Return True when *base_url* targets Ollama's native HTTP API."""
        if not cls._is_local_base_url(base_url):
            return False
        candidate = base_url.strip() if isinstance(base_url, str) else ""
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        parsed = urlparse(candidate)
        if parsed.port != 11434:
            return False
        path = (parsed.path or "").rstrip("/")
        return not path.startswith("/v1")

    @classmethod
    def _is_cloud_provider(cls, provider_type: str, base_url: str | None) -> bool:
        """Determine if the provider sends data to a cloud endpoint.

        Ollama is always local. For openai_compatible, if the base_url
        points to localhost/127.0.0.1 it is treated as local; otherwise
        it is treated as cloud.
        """
        if provider_type in cls._LOCAL_PROVIDERS:
            return False

        # openai_compatible with a localhost base_url is local
        if provider_type == LLMProviderType.OPENAI_COMPATIBLE.value and cls._is_local_base_url(base_url):
            return False

        return True

    @classmethod
    def create_from_settings(cls) -> BaseLLMProvider | None:
        """
        Create provider from user settings.

        Reads configuration from SettingsManager and creates
        the appropriate provider. Cloud providers require explicit
        user consent via the privacy consent gate.

        Returns:
            Provider instance or None if AI is disabled or consent not given
        """
        logger.debug("LLMProviderFactory.create_from_settings: starting")
        try:
            from ui.settings_manager import get_settings_manager

            settings = get_settings_manager()
            logger.debug("create_from_settings: settings_manager loaded")
        except ImportError as e:
            logger.warning("Could not import SettingsManager: %s", e)
            return None

        # Check if AI is enabled
        ai_enabled = settings.get("ai_enabled", True)  # Default to True for backward compat
        if not ai_enabled:
            logger.info("AI features disabled in settings")
            return None

        # ---- Codex subscription short-circuit ----
        from config.defaults import DEFAULT_AI_SOURCE

        # Env override path lets devs/deployments select an ai_source without
        # polluting the tracked settings.json (codex follow-up audit
        # 2026-05-11). Empty string means "no override; use the user prefs."
        try:
            from config.settings import settings as _app_settings

            override = (getattr(_app_settings, "ai_source_override", "") or "").strip()
        except Exception:
            override = ""
        if override:
            ai_source = override
        else:
            profiled_provider = cls._create_selected_connection_profile_provider(settings)
            if profiled_provider is not cls._NO_PROFILE_PROVIDER:
                return profiled_provider
            ai_source_raw = settings.get("ai_source", DEFAULT_AI_SOURCE)
            ai_source = ai_source_raw if isinstance(ai_source_raw, str) else DEFAULT_AI_SOURCE

        if ai_source == "codex":
            return cls._create_codex_provider(settings)
        if ai_source in {"managed", "subscription"}:
            return cls._create_managed_provider_gated(settings)
        if ai_source == "byok":
            return cls._create_byok_provider(settings)
        if ai_source == "local":
            return cls._create_local_provider(settings)
        logger.warning("Unknown ai_source '%s' - falling back to managed provider", ai_source)
        return cls._create_managed_provider_gated(settings)

    @classmethod
    def _current_user_id_for_profiles(cls) -> str | None:
        """Resolve the principal whose selected LLM connection profile applies.

        Uses the same desktop-aware resolver as the entitlement gate so a
        logged-in account's user-scoped profile is honoured even when the
        provider is built outside the request contextvar (router rebuild on a
        fresh task, startup). A bare ``device-*`` identity has no user profiles,
        so anonymous desktop and cloud-no-context both resolve to no profile,
        unchanged from before.
        """
        return cls._entitlement_user_id()

    @classmethod
    def _create_selected_connection_profile_provider(cls, settings: Any) -> BaseLLMProvider | None | object:
        """Create the selected user-scoped LLM profile, if one exists.

        This path intentionally precedes the legacy settings tuple.  A selected
        profile is authoritative; if it is misconfigured we return ``None``
        rather than falling through to a different provider and hiding the
        bad selection.
        """

        user_id = cls._current_user_id_for_profiles()
        if not user_id:
            return cls._NO_PROFILE_PROVIDER
        try:
            from services.connectors.profiles import get_connection_profile_store

            store = get_connection_profile_store()
            selected_profile_id = store.get_selected_profile_id(user_id, "llm")
            if not selected_profile_id:
                return cls._NO_PROFILE_PROVIDER
            profile = store.get_profile(user_id, selected_profile_id)
        except Exception:
            logger.exception("Failed to resolve selected LLM connection profile")
            return cls._NO_PROFILE_PROVIDER
        if profile is None:
            logger.warning(
                "Selected LLM connection profile is missing: profile=%s",
                selected_profile_id,
            )
            return None
        if not profile.enabled:
            logger.warning(
                "Selected LLM connection profile is disabled: connector=%s profile=%s",
                profile.connector_id,
                profile.profile_id,
            )
            return None

        logger.info(
            "Creating LLM provider from selected connection profile: connector=%s profile=%s",
            profile.connector_id,
            profile.profile_id,
        )
        if profile.connector_id == "llm.managed":
            return cls._create_managed_provider_gated(settings)
        if profile.connector_id == "llm.codex":
            return cls._create_codex_provider(settings, model=profile.model or None)

        api_key = store.get_profile_secret(profile, "api_key") or ""
        if (
            profile.adapter == LLMProviderType.OPENAI_COMPATIBLE.value
            and not api_key
            and cls._is_local_base_url(profile.base_url)
        ):
            api_key = "local-ai"  # pragma: allowlist secret -- local OpenAI-compatible servers often accept any key

        if profile.adapter != LLMProviderType.OLLAMA.value and not api_key:
            logger.warning(
                "Selected LLM connection profile is missing credentials: connector=%s profile=%s",
                profile.connector_id,
                profile.profile_id,
            )
            return None

        from services.connectors.profiles import profile_native_tools_verified

        native_tools_verified = profile_native_tools_verified(
            profile, store.get_profile_secret(profile, "api_key") or ""
        )
        return cls._create_api_key_provider(
            provider_type=profile.adapter,
            api_key=api_key,
            model=profile.model,
            base_url=profile.base_url,
            native_tools_verified=native_tools_verified,
        )

    @classmethod
    def _entitlement_user_id(cls) -> str | None:
        """Resolve the principal whose entitlement gates the managed provider.

        The factory may build providers outside the originating HTTP request's
        contextvar scope (router rebuilds on a fresh task, startup singleton,
        background callbacks). In those contexts the bare ``current_user_id``
        contextvar is unset or holds the desktop *device* principal, so reading
        it directly mis-resolves a logged-in account as "no account" and the
        paid funnel never activates after sign-in (M-BILL-1).

        ``get_current_or_desktop_active_user_id`` is the canonical desktop-aware
        resolver: it returns the request principal when one is bound, otherwise
        the desktop active principal — which prefers the install's logged-in
        GoTrue account and only falls back to the ``device-*`` identity when no
        account is signed in. Using the bare device-fallback resolver here was
        the #337 defect: provider construction runs outside the request (health
        probe, router rebuild on a fresh task/thread, startup singleton), so the
        device fallback mis-resolved a signed-in desktop user as "no account"
        and keyless managed AI stayed dead after a real GUI sign-in. On cloud
        surfaces with no request context it raises ``LookupError``, preserving
        fail-loud cloud behaviour. The resolved id is threaded explicitly into
        the account-gate checks so they never silently read a stale or device
        contextvar during provider construction.

        Returns ``None`` only when no principal can be resolved at all (cloud
        with no request context), which the account gate treats as fail-closed.
        """
        from core.user_context import get_current_or_desktop_active_user_id

        try:
            user_id = get_current_or_desktop_active_user_id()
        except LookupError:
            return None
        return user_id.strip() if isinstance(user_id, str) and user_id.strip() else None

    @classmethod
    def _user_has_account(cls) -> bool:
        """Return True when the resolved principal has a real Viola account.

        Delegates to the canonical ``core.account_gate.user_has_viola_account``
        so the factory gate and the ``/v1/command`` preflight never drift, and
        passes the explicitly-resolved entitlement principal so the check works
        even when provider construction runs outside the request contextvar.
        """
        from core.account_gate import user_has_viola_account

        return user_has_viola_account(cls._entitlement_user_id())

    @classmethod
    def _create_managed_provider_gated(cls, settings: Any) -> BaseLLMProvider | None:
        """Managed provider with the account gate enforced."""
        from core.account_gate import paid_action_login_required

        if paid_action_login_required(cls._entitlement_user_id()):
            logger.info(
                "Managed LLM blocked: current user has no Viola account "
                "(free tier requires signup; BYOK/Codex remain available)",
            )
            fallback_chain = cls._create_managed_account_gate_fallback_chain(settings)
            if fallback_chain is not None and fallback_chain.is_available():
                logger.info("Managed LLM account gate using configured fallback chain")
                return fallback_chain
            raise ManagedLLMAuthRequired()
        return cls._create_managed_provider(settings)

    @classmethod
    def _create_managed_account_gate_fallback_chain(cls, settings: Any) -> BaseLLMProvider | None:
        """Build an explicit fallback chain when managed mode is blocked by account login.

        This preserves the selected managed primary as unavailable evidence while
        allowing configured no-Viola-spend backups such as Codex, Anthropic BYOK,
        or local Ollama to handle the request.
        """

        try:
            from config.defaults import DEFAULT_AI_SOURCE, resolve_effective_model
            from config.settings import settings as app_settings
            from core.account_gate import LOGIN_REQUIRED_FOR_PAID_ACTION
            from services.llm.provider_fallback import LLMProviderFallbackChain

            if not getattr(app_settings, "llm_fallback_enabled", True):
                logger.info("Managed LLM account-gate fallback skipped: fallback chain disabled")
                return None

            fallback_providers, unavailable_fallbacks = cls._create_configured_fallback_provider_plan(settings)
            fallback_providers = [provider for provider in fallback_providers if provider is not None]
            if not fallback_providers:
                skipped = "; ".join("%s: %s" % (entry["name"], entry["reason"]) for entry in unavailable_fallbacks)
                logger.info(
                    "Managed LLM account-gate fallback skipped: no configured backup provider is available%s",
                    " (skipped: %s)" % skipped if skipped else "",
                )
                return None

            ai_source_raw = settings.get("ai_source", DEFAULT_AI_SOURCE)
            ai_source = ai_source_raw if isinstance(ai_source_raw, str) else DEFAULT_AI_SOURCE
            model = resolve_effective_model(
                ai_source=ai_source,
                provider=LLMProviderType.OPENAI.value,
                agent=False,
                candidates=(
                    settings.get("llm_model", ""),
                    getattr(app_settings, "gpt_model", "") or "",
                ),
            )
            primary = _UnavailableLLMProvider(
                provider_type=LLMProviderType.OPENAI.value,
                provider_name="Managed OpenAI",
                model=model,
                reason="Sign in to use Viola-managed AI.",
                error_code=LOGIN_REQUIRED_FOR_PAID_ACTION,
            )
            return LLMProviderFallbackChain(
                primary,
                fallback_providers,
                unavailable_fallbacks=unavailable_fallbacks,
            )
        except Exception:
            logger.exception("Failed to create managed account-gate fallback chain")
            return None

    @classmethod
    def _create_managed_provider(cls, settings: Any) -> BaseLLMProvider | None:
        """Create an OpenAI provider using Viola's managed business API key."""
        try:
            from config.defaults import DEFAULT_AI_SOURCE, resolve_effective_model
            from config.settings import settings as app_settings

            ai_source_raw = settings.get("ai_source", DEFAULT_AI_SOURCE)
            ai_source = ai_source_raw if isinstance(ai_source_raw, str) else DEFAULT_AI_SOURCE
            # Candidate precedence: per-user llm_model from SettingsManager
            # first, then the AppConfig.gpt_model (VIOLA_GPT_MODEL env var
            # override). This lets cloud deployments pick a model the
            # OPENAI_API_KEY's project actually has access to without
            # touching every user's settings.
            model = resolve_effective_model(
                ai_source=ai_source,
                provider=LLMProviderType.OPENAI.value,
                agent=False,
                candidates=(
                    settings.get("llm_model", ""),
                    getattr(app_settings, "gpt_model", "") or "",
                ),
            )
            # Desktop-to-cloud managed forward: a REAL customer install ships no
            # OpenAI key (the installer is byte-scanned clean), so the direct
            # ``openai_api_key`` path below has managed AI dead after sign-in.
            # When there is no local key AND this is a desktop surface, route the
            # managed turn through Viola's cloud, authenticated by the user's
            # GoTrue token; the key stays in server env (Tier-3). A local key
            # present (our dev ``.env``) keeps the direct path — the dev escape
            # hatch. The cloud surface itself holds the real server key and calls
            # OpenAI directly, so it also keeps the direct path.
            local_key = (getattr(app_settings, "openai_api_key", "") or "").strip()
            app_surface = str(getattr(app_settings, "app_surface", "desktop") or "desktop").strip().lower()
            if not local_key and app_surface != "cloud":
                cloud_primary = cls._create_cloud_managed_provider(model)
                if cloud_primary is not None:
                    return cls.with_fallback_chain(cloud_primary, settings)
                # else fall through to the direct path (no-ops without a key),
                # preserving the historical "managed disabled" behaviour.
            primary = cls._create_api_key_provider(
                provider_type=LLMProviderType.OPENAI.value,
                api_key=app_settings.openai_api_key or "",
                model=model,
                base_url="",
            )
            return cls.with_fallback_chain(primary, settings)
        except Exception:
            logger.exception("Failed to create managed LLM provider")
            return None

    @classmethod
    def _create_cloud_managed_provider(cls, model: str) -> BaseLLMProvider | None:
        """Build the desktop->cloud managed forward provider, or ``None``.

        Returns ``None`` (so the caller falls back to the direct path) when the
        cloud URL is unset or the user has not granted cloud-LLM consent. The
        cloud-consent gate mirrors ``_create_api_key_provider``'s gate for the
        direct managed OpenAI path: managed AI sends the prompt to a cloud LLM
        either way, so the same privacy consent must hold. NEVER writes any key
        to desktop storage — it only forwards a GoTrue bearer that already
        exists in the desktop session store.
        """
        try:
            from config.settings import settings as app_settings
            from core.privacy_consent import is_cloud_llm_consented

            if not is_cloud_llm_consented():
                logger.warning("Cloud LLM consent not granted; managed cloud forward disabled.")
                return None

            cloud_url = (
                getattr(app_settings, "cloud_url", "") or getattr(app_settings, "api_base_url", "") or ""
            ).strip()
            if not cloud_url:
                logger.warning("No cloud_url configured; managed cloud forward disabled.")
                return None

            from services.llm.providers.base import LLMConfig
            from services.llm.providers.cloud_managed_provider import (
                CloudManagedProvider,
            )

            config = LLMConfig(
                provider=LLMProviderType.OPENAI.value,
                api_key=None,
                model=model,
                base_url=None,
            )
            logger.info("Creating managed cloud-forward LLM provider (server holds key)")
            return CloudManagedProvider(config, cloud_url=cloud_url)
        except Exception:
            logger.exception("Failed to create managed cloud-forward provider")
            return None

    @classmethod
    def with_fallback_chain(
        cls,
        primary: BaseLLMProvider | None,
        settings: Any | None = None,
        *,
        fallbacks: list[BaseLLMProvider] | None = None,
        enabled: bool | None = None,
    ) -> BaseLLMProvider | None:
        """Wrap a primary managed provider in the configured provider fallback chain."""
        if primary is None:
            return None

        try:
            from config.settings import settings as app_settings
            from services.llm.provider_fallback import LLMProviderFallbackChain

            # Fail closed: if the flag is absent, do NOT silently build a
            # provider fallback chain — ai_source must stay authoritative.
            fallback_enabled = getattr(app_settings, "llm_fallback_enabled", False)
            if enabled is not None:
                fallback_enabled = enabled
            if not fallback_enabled:
                return primary

            unavailable_fallbacks: list[dict[str, str]] = []
            backup_providers = fallbacks
            if backup_providers is None:
                backup_providers, unavailable_fallbacks = cls._create_configured_fallback_provider_plan(settings)
            backup_providers = [provider for provider in backup_providers if provider is not None]
            if not backup_providers and not unavailable_fallbacks:
                return primary

            chain = LLMProviderFallbackChain(
                primary,
                backup_providers,
                unavailable_fallbacks=unavailable_fallbacks,
            )
            status = chain.get_fallback_status()
            if backup_providers:
                logger.info(
                    "LLM provider fallback chain enabled: %s",
                    " -> ".join(entry["name"] for entry in status["chain"]),
                )
            else:
                logger.info(
                    "LLM provider fallback chain enabled with no available backup providers; skipped=%s",
                    "; ".join("%s: %s" % (entry["name"], entry["reason"]) for entry in unavailable_fallbacks),
                )
            return chain
        except Exception:
            logger.exception("Failed to attach LLM provider fallback chain; using primary provider only")
            return primary

    @classmethod
    def _configured_fallback_sources(cls) -> list[str]:
        """Return normalized provider source names for managed-mode fallback."""
        from config.defaults import DEFAULT_LLM_FALLBACK_CHAIN
        from config.settings import settings as app_settings

        raw = getattr(app_settings, "llm_fallback_chain", list(DEFAULT_LLM_FALLBACK_CHAIN))
        if isinstance(raw, str):
            candidates = raw.split(",")
        elif isinstance(raw, (list, tuple)):
            candidates = [str(item) for item in raw]
        else:
            candidates = list(DEFAULT_LLM_FALLBACK_CHAIN)

        normalized: list[str] = []
        aliases = {
            "openai": "managed",
            "primary": "managed",
            "ollama": "local",
        }
        allowed = {"managed", "codex", "anthropic", "local"}
        for candidate in candidates:
            source = aliases.get(candidate.strip().lower(), candidate.strip().lower())
            if source in allowed and source not in normalized:
                normalized.append(source)

        if not normalized:
            normalized = list(DEFAULT_LLM_FALLBACK_CHAIN)
        if normalized[0] != "managed":
            normalized.insert(0, "managed")
        return normalized

    @classmethod
    def _fallback_skip_entry(cls, source: str, reason: str | None) -> dict[str, str]:
        names = {
            "codex": "Codex",
            "anthropic": "Anthropic",
            "local": "Local LLM",
        }
        return {
            "source": source,
            "name": names.get(source, source),
            "reason": reason or "unavailable",
        }

    @classmethod
    def _create_configured_fallback_provider_plan(
        cls,
        settings: Any | None,
    ) -> tuple[list[BaseLLMProvider], list[dict[str, str]]]:
        providers: list[BaseLLMProvider] = []
        unavailable: list[dict[str, str]] = []
        for source in cls._configured_fallback_sources()[1:]:
            provider, reason = cls._create_fallback_provider_for_source(source, settings)
            if provider is not None:
                providers.append(provider)
            else:
                unavailable.append(cls._fallback_skip_entry(source, reason))
        return providers, unavailable

    @classmethod
    def _create_configured_fallback_providers(cls, settings: Any | None) -> list[BaseLLMProvider]:
        providers, _unavailable = cls._create_configured_fallback_provider_plan(settings)
        return providers

    @classmethod
    def _create_fallback_provider_for_source(
        cls,
        source: str,
        settings: Any | None,
    ) -> tuple[BaseLLMProvider | None, str | None]:
        if source == "codex":
            reason = "Codex is only available when AI source is set to codex"
            logger.info("Codex fallback skipped: %s", reason)
            return None, reason
        if source == "anthropic":
            return cls._create_anthropic_fallback_provider()
        if source == "local":
            return cls._create_local_fallback_provider()
        return None, "unsupported fallback source"

    @classmethod
    def _create_anthropic_fallback_provider(
        cls,
    ) -> tuple[BaseLLMProvider | None, str | None]:
        """Create Anthropic as a managed-mode backup when an API key is configured."""
        try:
            from config.defaults import get_provider_default_agent_model
            from config.settings import settings as app_settings

            api_key = app_settings.anthropic_api_key or ""
            if not api_key:
                reason = "ANTHROPIC_API_KEY is not configured"
                logger.info("Anthropic fallback skipped: %s", reason)
                return None, reason
            model = get_provider_default_agent_model(LLMProviderType.ANTHROPIC.value)
            provider = cls._create_api_key_provider(
                provider_type=LLMProviderType.ANTHROPIC.value,
                api_key=api_key,
                model=model,
                base_url="",
            )
            if provider is None:
                return None, "Anthropic provider could not be created"
            return provider, None
        except Exception:
            logger.exception("Failed to create Anthropic fallback provider")
            return None, "Anthropic fallback provider creation failed"

    @classmethod
    def _create_local_fallback_provider(
        cls,
    ) -> tuple[BaseLLMProvider | None, str | None]:
        """Create a local Ollama fallback when explicitly enabled and reachable."""
        try:
            from config.defaults import DEFAULT_LOCAL_LLM_MODEL
            from config.settings import settings as app_settings
            from core.constants import OLLAMA_DEFAULT_BASE_URL

            if not getattr(app_settings, "local_llm_enabled", False):
                reason = "VIOLA_LOCAL_LLM_ENABLED is false"
                logger.info("Local LLM fallback skipped: %s", reason)
                return None, reason
            base_url = getattr(app_settings, "local_llm_base_url", "") or app_settings.ollama_base_url
            base_url = base_url or OLLAMA_DEFAULT_BASE_URL
            if not cls._is_local_base_url(base_url):
                logger.warning(
                    "Local LLM fallback requires a loopback base URL; got '%s'",
                    base_url,
                )
                return None, "base URL is not loopback"
            if not cls._is_ollama_reachable(base_url):
                logger.info(
                    "Local LLM fallback skipped: Ollama is not reachable at %s",
                    base_url,
                )
                return None, "Ollama is not reachable at %s" % base_url
            model = getattr(app_settings, "local_llm_model", "") or DEFAULT_LOCAL_LLM_MODEL
            provider = cls._create_api_key_provider(
                provider_type=LLMProviderType.OLLAMA.value,
                api_key="",
                model=model,
                base_url=base_url,
            )
            if provider is None:
                return None, "local LLM provider could not be created"
            return provider, None
        except Exception:
            logger.exception("Failed to create local LLM fallback provider")
            return None, "local LLM fallback provider creation failed"

    @classmethod
    def _is_ollama_reachable(cls, base_url: str) -> bool:
        candidate = base_url.strip()
        if not candidate:
            return False
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        candidate = candidate.rstrip("/")
        try:
            import httpx

            response = httpx.get(f"{candidate}/api/tags", timeout=1.5)
            return response.status_code == 200
        except Exception as exc:
            logger.debug("Ollama fallback reachability check failed for %s: %s", candidate, exc)
            return False

    @classmethod
    def _list_ollama_models_sync(cls, base_url: str) -> list[str]:
        candidate = base_url.strip()
        if not candidate:
            return []
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        candidate = candidate.rstrip("/")
        try:
            import httpx

            response = httpx.get(f"{candidate}/api/tags", timeout=1.5)
            if response.status_code != 200:
                return []
            data = response.json()
            models = data.get("models", [])
            if not isinstance(models, list):
                return []
            return [
                name
                for item in models
                if isinstance(item, dict)
                for name in (item.get("name") or item.get("model"),)
                if isinstance(name, str) and name.strip()
            ]
        except Exception as exc:
            logger.debug("Ollama model discovery failed for %s: %s", candidate, exc)
            return []

    @classmethod
    def _resolve_ollama_local_model(cls, settings_model: str, provider_type: str, base_url: str) -> str:
        """Resolve local Ollama model without leaking cloud model pins."""
        from config.defaults import DEFAULT_LOCAL_LLM_MODEL, resolve_effective_model

        candidates: list[str] = []
        if settings_model and not looks_like_cloud_model(settings_model):
            candidates.append(settings_model)

        try:
            from config.settings import settings as app_settings

            configured_local = getattr(app_settings, "local_llm_model", "") or ""
            if configured_local and not looks_like_cloud_model(configured_local):
                candidates.append(configured_local)
        except Exception:
            pass

        installed = cls._list_ollama_models_sync(base_url)
        if installed:
            for candidate in candidates:
                for installed_name in installed:
                    if ollama_model_matches(candidate, installed_name):
                        return installed_name
            best = best_local_model(installed)
            if settings_model and looks_like_cloud_model(settings_model):
                logger.info(
                    "Local ai_source ignored cloud model '%s' and selected installed Ollama model '%s'",
                    settings_model,
                    best,
                )
            return best

        if candidates:
            return candidates[0]
        return resolve_effective_model(
            ai_source="local",
            provider=LLMProviderType.OLLAMA.value,
            agent=False,
            fallback=DEFAULT_LOCAL_LLM_MODEL,
        )

    @classmethod
    def _create_local_provider(cls, settings: Any) -> BaseLLMProvider | None:
        """Create a provider that runs on the user's own machine."""
        from core.constants import OLLAMA_DEFAULT_BASE_URL

        provider_type_raw = settings.get("llm_provider", "")
        provider_type = provider_type_raw.strip().lower() if isinstance(provider_type_raw, str) else ""
        api_key_raw = settings.get("llm_api_key", "")
        api_key = api_key_raw if isinstance(api_key_raw, str) else ""
        model_raw = settings.get("llm_model", "") or settings.get("local_llm_model", "")
        model = model_raw if isinstance(model_raw, str) else ""
        base_url_raw = settings.get("llm_base_url", "")
        base_url = base_url_raw.strip() if isinstance(base_url_raw, str) else ""

        if base_url:
            if not cls._is_local_base_url(base_url):
                logger.warning(
                    "Local ai_source requires a localhost/loopback llm_base_url; got '%s'",
                    base_url,
                )
                return None

            if cls._is_ollama_native_base_url(base_url):
                return cls._create_api_key_provider(
                    provider_type=LLMProviderType.OLLAMA.value,
                    api_key="",
                    model=cls._resolve_ollama_local_model(model, provider_type, base_url),
                    base_url=base_url,
                )

            if provider_type and provider_type != LLMProviderType.OPENAI_COMPATIBLE.value:
                logger.info(
                    "Local ai_source is routing via loopback OpenAI-compatible endpoint '%s' despite provider='%s'",
                    base_url,
                    provider_type,
                )

            compat_api_key = api_key if api_key and api_key != "***ENCRYPTED***" else "local-ai"
            return cls._create_api_key_provider(
                provider_type=LLMProviderType.OPENAI_COMPATIBLE.value,
                api_key=compat_api_key,
                model=model,
                base_url=base_url,
            )

        if provider_type == LLMProviderType.OPENAI_COMPATIBLE.value:
            logger.warning(
                "Local ai_source with openai_compatible but no llm_base_url is incomplete; defaulting to Ollama"
            )
        elif provider_type not in {"", LLMProviderType.OLLAMA.value}:
            logger.info(
                "Local ai_source ignores non-local provider '%s'; defaulting to Ollama",
                provider_type,
            )

        return cls._create_api_key_provider(
            provider_type=LLMProviderType.OLLAMA.value,
            api_key="",
            model=cls._resolve_ollama_local_model(model, provider_type, OLLAMA_DEFAULT_BASE_URL),
            base_url=OLLAMA_DEFAULT_BASE_URL,
        )

    @classmethod
    def _create_byok_provider(cls, settings: Any) -> BaseLLMProvider | None:
        """Create a provider using only user-scoped BYOK settings."""
        provider_type_raw = settings.get("llm_provider", LLMProviderType.OPENAI.value)
        provider_type = provider_type_raw if isinstance(provider_type_raw, str) else LLMProviderType.OPENAI.value
        api_key_raw = settings.get("llm_api_key", "")
        api_key = api_key_raw if isinstance(api_key_raw, str) else ""
        legacy_api_key_raw = settings.get("openai_api_key", "")
        legacy_api_key = legacy_api_key_raw if isinstance(legacy_api_key_raw, str) else ""
        model_raw = settings.get("llm_model", "")
        model = model_raw if isinstance(model_raw, str) else ""
        base_url_raw = settings.get("llm_base_url", "")
        base_url = base_url_raw if isinstance(base_url_raw, str) else ""

        logger.debug(
            "create_from_settings(byok): provider=%s, api_key_present=%s, api_key_encrypted=%s, model=%s",
            provider_type,
            bool(api_key),
            api_key == "***ENCRYPTED***" if api_key else False,
            model or "(default)",
        )

        managed_key = ""
        if provider_type == LLMProviderType.OPENAI.value:
            try:
                from config.settings import settings as app_settings

                managed_key = app_settings.openai_api_key or ""
            except Exception:
                managed_key = ""

            if managed_key and api_key == managed_key:
                logger.warning("Ignoring OpenAI BYOK llm_api_key because it matches the shared managed key")
                api_key = ""
            if managed_key and legacy_api_key == managed_key:
                if legacy_api_key and legacy_api_key != "***ENCRYPTED***":
                    logger.debug("Ignoring legacy openai_api_key for BYOK because it matches the shared managed key")
                legacy_api_key = ""

        if not api_key and legacy_api_key and legacy_api_key != "***ENCRYPTED***":
            api_key = legacy_api_key
            logger.info("Migrating legacy openai_api_key to llm_api_key")

        # BYOK is the user-paid path. Never fall back to the shared managed
        # OpenAI key here; users without their own key should switch to the
        # managed source and sign in.
        if not api_key:
            logger.debug(
                "BYOK api_key missing for provider=%s; BYOK path is inactive.",
                provider_type,
            )
            return None

        return cls._create_api_key_provider(
            provider_type=provider_type,
            api_key=api_key,
            model=model,
            base_url=base_url,
        )

    @classmethod
    def _create_api_key_provider(
        cls,
        *,
        provider_type: str,
        api_key: str,
        model: str,
        base_url: str,
        native_tools_verified: bool = False,
    ) -> BaseLLMProvider | None:
        """Create a provider from explicit provider/api-key settings."""

        # Authoritative consent gate — checked here only, not duplicated in router or controller.
        # Cloud LLM providers require explicit opt-in before creation.
        if cls._is_cloud_provider(provider_type, base_url or None):
            from core.privacy_consent import is_cloud_llm_consented

            if not is_cloud_llm_consented():
                logger.warning(
                    "Cloud LLM consent not granted; %s provider disabled.",
                    provider_type,
                )
                return None

        # Validate model: reject obviously invalid model names that don't
        # belong to the selected provider (e.g. a stale settings.json value
        # like "normal-model" or an OpenAI model name for Anthropic).
        if model:
            model = cls._validate_model_for_provider(provider_type, model)

        # Set defaults based on provider
        if not model:
            model = cls._get_default_model(provider_type)

        if provider_type == LLMProviderType.OPENAI_COMPATIBLE.value and not (model or "").strip():
            logger.warning("OpenAI-compatible provider requires explicit model; blank llm_model rejected")
            return None

        # Create config
        config = LLMConfig(
            provider=provider_type,
            api_key=api_key if api_key and api_key != "***ENCRYPTED***" else None,
            model=model,
            base_url=base_url if base_url else None,
            native_tools_verified=native_tools_verified,
        )

        # For providers that don't require API keys (like Ollama)
        provider_info = get_provider_info(provider_type)
        if provider_info and not provider_info.requires_api_key:
            return cls.create_provider(config)

        # For providers requiring API keys, verify we have one
        if not config.api_key:
            logger.warning("No API key configured for %s", provider_type)
            return None

        return cls.create_provider(config)

    @classmethod
    def _create_codex_provider(cls, settings: Any | None, *, model: str | None = None) -> BaseLLMProvider | None:
        """Create an LLM provider backed by the user's Codex/ChatGPT subscription.

        Uses the OAuth token from ``~/.codex/auth.json`` to call
        ``chatgpt.com/backend-api/codex/responses`` at zero API cost.
        """
        try:
            # Authoritative consent gate — checked here only, not duplicated in router or controller.
            from core.privacy_consent import is_cloud_llm_consented

            if not is_cloud_llm_consented():
                logger.warning("Cloud LLM consent not given — Codex subscription provider disabled.")
                return None

            from services.llm.codex_auth import (
                create_codex_openai_client,
                is_codex_available,
            )

            if not is_codex_available():
                logger.warning(
                    "Codex subscription selected but ~/.codex/auth.json not found "
                    "or has no valid token.  Run `codex login` in a terminal."
                )
                return None

            from config.defaults import resolve_effective_model

            settings_ai_source = ""
            settings_model = ""
            if settings is not None:
                raw_source = settings.get("ai_source", "")
                settings_ai_source = raw_source.strip().lower() if isinstance(raw_source, str) else ""
                if settings_ai_source == "codex":
                    settings_model = settings.get("llm_model", "")
            env_model = os.environ.get("VIOLA_CODEX_MODEL", "").strip()
            resolved_model = resolve_effective_model(
                ai_source="codex",
                provider=LLMProviderType.OPENAI.value,
                agent=False,
                candidates=(model, env_model, settings_model),
            )
            model = resolved_model
            openai_client = create_codex_openai_client()

            config = LLMConfig(
                provider=LLMProviderType.OPENAI.value,
                api_key="codex-subscription",  # pragma: allowlist secret  # placeholder — auth is in the transport
                model=model,
            )

            from services.llm.providers.openai_agents_provider import (
                OpenAIAgentsProvider,
            )

            provider = OpenAIAgentsProvider(config, openai_client=openai_client)
            if provider.is_available():
                logger.info("Codex subscription provider created: model=%s", resolved_model)
                return provider

            logger.warning("Codex provider unavailable: %s", provider.last_error)
            return None
        except Exception:
            logger.exception("Failed to create Codex subscription provider")
            return None

    @classmethod
    def _get_default_model(cls, provider_type: str) -> str:
        """Get default model for provider type."""
        from config.defaults import get_provider_default_model

        return get_provider_default_model(provider_type)

    # Known model name prefixes per provider.  Used to reject obviously
    # invalid model names that were left over in settings.json after a
    # provider switch (e.g. an OpenAI model for Anthropic, or a junk
    # placeholder like "normal-model").
    _MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
        LLMProviderType.ANTHROPIC.value: ("claude",),
        LLMProviderType.OPENAI.value: ("gpt-", "o1-", "o3-", "chatgpt-", "ft:"),
        LLMProviderType.GOOGLE.value: ("gemini",),
        # Ollama and openai_compatible accept any model name
    }

    @classmethod
    def _validate_model_for_provider(cls, provider_type: str, model: str) -> str:
        """Validate a model name against the selected provider.

        Returns the model unchanged if it looks plausible, or an empty
        string (which triggers default-model selection) when the name is
        obviously wrong for the chosen provider.
        """
        prefixes = cls._MODEL_PREFIXES.get(provider_type)
        if prefixes is None:
            # Provider accepts arbitrary model names (Ollama, openai_compatible)
            return model
        if any(model.lower().startswith(p) for p in prefixes):
            return model
        # Model name does not match provider — likely stale settings
        default = cls._get_default_model(provider_type)
        logger.warning(
            "Model '%s' does not look valid for %s provider; " "falling back to default '%s'",
            model,
            provider_type,
            default,
        )
        return ""

    @classmethod
    def get_available_providers(cls) -> list[dict[str, Any]]:
        """
        Get list of available provider types with metadata.

        Returns:
            List of provider info dicts
        """
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "requires_api_key": p.requires_api_key,
                "supports_custom_base_url": p.supports_custom_base_url,
                "default_base_url": p.default_base_url,
                "default_models": p.default_models,
                "popular_models": p.popular_models,
            }
            for p in get_all_providers()
        ]


def create_llm_provider(config: LLMConfig) -> BaseLLMProvider:
    """
    Convenience function to create a provider.

    Args:
        config: LLM configuration

    Returns:
        Provider instance
    """
    return LLMProviderFactory.create_provider(config)


def create_provider_from_settings() -> BaseLLMProvider | None:
    """
    Convenience function to create provider from settings.

    Returns:
        Provider instance or None
    """
    return LLMProviderFactory.create_from_settings()


def get_available_providers() -> list[dict[str, Any]]:
    """
    Get list of available provider types.

    Returns:
        List of provider info dicts
    """
    return LLMProviderFactory.get_available_providers()


# Process-wide memo for global LLM handler access, keyed on the settings the
# provider selection actually reads. Two bugs live at this seam and the key is
# what fixes both:
#
#   * Assigning only on success meant a raise -- ManagedLLMAuthRequired, the
#     default state of a brand-new install with no account -- cached nothing,
#     so every caller re-ran the whole selection chain forever. /health/details
#     is polled every 30s by the Qt client, so that was a permanent repeat.
#   * Caching unconditionally forever meant that once a handler WAS built, it
#     outlived the settings that produced it: sign in, or switch AI source, and
#     the health surface kept reporting the provider from before the change.
#
# Keying the memo on the selection inputs gives a fast answer that is still the
# truth: unchanged inputs reuse the result, changed inputs rebuild immediately.
_LLM_HANDLER_SINGLETON: BaseLLMProvider | None = None
_LLM_HANDLER_KEY: tuple[Any, ...] | None = None
_LLM_HANDLER_ERROR: ManagedLLMAuthRequired | None = None
_LLM_HANDLER_LOCK = threading.Lock()


def _llm_handler_selection_key() -> tuple[Any, ...]:
    """Snapshot the inputs ``create_from_settings`` branches on.

    Cheap by construction: settings reads plus the already-resolved entitlement
    principal. Anything unreadable degrades to a sentinel that differs from a
    real value, so an unreadable input re-derives rather than serving a stale
    handler.
    """
    values: list[Any] = []
    try:
        from ui.settings_manager import get_settings_manager

        settings = get_settings_manager()
        for key, default in (
            ("ai_enabled", True),
            ("ai_source", ""),
            ("llm_provider", ""),
            ("llm_model", ""),
        ):
            values.append(settings.get(key, default))
    except Exception:  # noqa: BLE001, RUF100 -- an unreadable key must re-derive, never serve stale
        logger.debug("LLM handler key: settings unreadable", exc_info=True)
        values.append(object())
    try:
        from config.settings import settings as _app_settings

        values.append((getattr(_app_settings, "ai_source_override", "") or "").strip())
    except Exception:  # noqa: BLE001, RUF100 -- see above
        logger.debug("LLM handler key: app settings unreadable", exc_info=True)
        values.append(object())
    try:
        user_id = LLMProviderFactory._entitlement_user_id()
    except Exception:  # noqa: BLE001, RUF100 -- see above
        logger.debug("LLM handler key: entitlement principal unresolved", exc_info=True)
        user_id = object()
    values.append(user_id)
    values.append(_selected_llm_profile_id(user_id))
    return tuple(values)


def _selected_llm_profile_id(user_id: Any) -> Any:
    """Return the user's selected LLM connection profile id, if any.

    A selected profile overrides the plain settings tuple, so it belongs in the
    memo key. Anonymous/desktop-local principals have no profiles, which is the
    default new-install path, so this costs nothing there.
    """
    if not isinstance(user_id, str) or not user_id:
        return None
    try:
        from services.connectors.profiles import get_connection_profile_store

        return get_connection_profile_store().get_selected_profile_id(user_id, "llm")
    except Exception:  # noqa: BLE001, RUF100 -- unreadable profile store must re-derive
        logger.debug("LLM handler key: connection profile store unreadable", exc_info=True)
        return object()


def reset_llm_handler() -> None:
    """Drop the memoised handler so the next call rebuilds from settings."""
    global _LLM_HANDLER_SINGLETON, _LLM_HANDLER_KEY, _LLM_HANDLER_ERROR
    with _LLM_HANDLER_LOCK:
        _LLM_HANDLER_SINGLETON = None
        _LLM_HANDLER_KEY = None
        _LLM_HANDLER_ERROR = None


def get_llm_handler() -> BaseLLMProvider | None:
    """
    Get the global LLM handler instance (memoised on its selection inputs).

    Returns the cached handler while the settings that produced it are
    unchanged, otherwise creates one from settings.

    Raises:
        ManagedLLMAuthRequired: when managed AI is selected and the current
            principal has no Viola account. The raise is cached alongside the
            success path, so repeat callers get the same answer without
            re-running provider selection.

    Returns:
        The LLM handler instance, or None if not configured.
    """
    global _LLM_HANDLER_SINGLETON, _LLM_HANDLER_KEY, _LLM_HANDLER_ERROR

    key = _llm_handler_selection_key()
    with _LLM_HANDLER_LOCK:
        if key == _LLM_HANDLER_KEY:
            if _LLM_HANDLER_ERROR is not None:
                raise _LLM_HANDLER_ERROR
            return _LLM_HANDLER_SINGLETON

        start = time.perf_counter()
        try:
            handler = create_provider_from_settings()
        except ManagedLLMAuthRequired as exc:
            _LLM_HANDLER_SINGLETON = None
            _LLM_HANDLER_ERROR = exc
            _LLM_HANDLER_KEY = key
            logger.info(
                "LLM handler factory call completed in %.3fs (handler=None, account required)",
                time.perf_counter() - start,
            )
            raise
        _LLM_HANDLER_SINGLETON = handler
        _LLM_HANDLER_ERROR = None
        _LLM_HANDLER_KEY = key
        logger.info(
            "LLM handler factory call completed in %.3fs (handler=%s)",
            time.perf_counter() - start,
            (type(handler).__name__ if handler is not None else "None"),
        )
        return handler
