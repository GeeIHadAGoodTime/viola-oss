"""
Base LLM Provider Interface

Abstract base class and data structures for all LLM providers.
Providers implement this interface to enable provider-agnostic LLM operations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from config.defaults import get_provider_default_model
from core.constants import OLLAMA_DEFAULT_BASE_URL
from core.logging_config import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle


class LLMProviderType(str, Enum):
    """Supported LLM provider types."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


@dataclass
class LLMConfig:
    """Configuration for any LLM provider."""

    provider: str  # LLMProviderType value
    api_key: str | None = None
    model: str = ""
    base_url: str | None = None

    # Provider-specific options
    temperature: float = 0.7
    max_tokens: int = 300
    # Set only after an explicit successful native-tool contract probe for
    # this exact compatible endpoint/model/credential configuration.
    native_tools_verified: bool = False

    def __post_init__(self):
        """Validate configuration after initialization."""
        if not self.provider:
            raise ValueError("Provider type is required")

        # Set default models if not specified
        if not self.model:
            self.model = self._get_default_model()

    def _get_default_model(self) -> str:
        """Get default model for provider type."""
        return get_provider_default_model(self.provider)


@dataclass
class LLMTestResult:
    """Result of connection test."""

    success: bool
    message: str
    latency_ms: int | None = None
    model_info: dict | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "success": self.success,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "model_info": self.model_info,
            "error_code": self.error_code,
        }


@dataclass
class LLMProviderInfo:
    """Information about an LLM provider."""

    id: str  # Provider type ID
    name: str  # Display name
    description: str
    requires_api_key: bool = True
    supports_custom_base_url: bool = False
    default_base_url: str | None = None
    default_models: list[str] = field(default_factory=list)
    popular_models: list[str] = field(default_factory=list)


# Provider registry with metadata
PROVIDER_INFO: dict[str, LLMProviderInfo] = {
    LLMProviderType.OPENAI.value: LLMProviderInfo(
        id=LLMProviderType.OPENAI.value,
        name="OpenAI",
        description="GPT-4, GPT-3.5 and other OpenAI models",
        requires_api_key=True,
        supports_custom_base_url=False,
        default_models=["gpt-5.4-mini", "gpt-5.4", "gpt-4o", "gpt-4o-mini"],
        popular_models=["gpt-5.4-mini", "gpt-5.4"],
    ),
    LLMProviderType.ANTHROPIC.value: LLMProviderInfo(
        id=LLMProviderType.ANTHROPIC.value,
        name="Anthropic",
        description="Claude models from Anthropic",
        requires_api_key=True,
        supports_custom_base_url=False,
        default_models=[
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-6",
            "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022",
            "claude-3-haiku-20240307",
        ],
        popular_models=["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"],
    ),
    LLMProviderType.GOOGLE.value: LLMProviderInfo(
        id=LLMProviderType.GOOGLE.value,
        name="Google",
        description="Gemini models from Google",
        requires_api_key=True,
        supports_custom_base_url=False,
        default_models=["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-pro-latest"],
        popular_models=["gemini-2.5-flash", "gemini-2.5-pro"],
    ),
    LLMProviderType.OLLAMA.value: LLMProviderInfo(
        id=LLMProviderType.OLLAMA.value,
        name="Ollama (Local)",
        description="Run models locally with Ollama",
        requires_api_key=False,
        supports_custom_base_url=True,
        default_base_url=OLLAMA_DEFAULT_BASE_URL,
        default_models=["llama2", "mistral", "codellama", "phi"],
        popular_models=["llama2", "mistral"],
    ),
    LLMProviderType.OPENAI_COMPATIBLE.value: LLMProviderInfo(
        id=LLMProviderType.OPENAI_COMPATIBLE.value,
        name="OpenAI Compatible",
        description="Any OpenAI-compatible API (Groq, Together.ai, vLLM, etc.)",
        requires_api_key=True,
        supports_custom_base_url=True,
        default_models=[],
        popular_models=[],
    ),
}


def get_provider_info(provider_type: str) -> LLMProviderInfo | None:
    """Get information about a provider type."""
    return PROVIDER_INFO.get(provider_type)


def get_all_providers() -> list[LLMProviderInfo]:
    """Get list of all supported providers."""
    return list(PROVIDER_INFO.values())


class BaseLLMProvider(ABC):
    """
    Abstract base for all LLM providers.

    All provider implementations must inherit from this class
    and implement the required abstract methods.
    """

    # Override to True in providers that support native tool calling
    # (e.g. Anthropic tool_use blocks instead of JSON-in-prompt)
    SUPPORTS_NATIVE_TOOLS: bool = False

    # Override to False in providers that never need a locally-configured API
    # key (the key lives server-side, or the backend is keyless). The agent
    # preflight reads this off the real provider instance to decide whether a
    # missing api_key blocks the loop (services/agent/preflight.py).
    REQUIRES_LOCAL_API_KEY: bool = True

    def __init__(self, config: LLMConfig):
        """
        Initialize provider with configuration.

        Args:
            config: LLM configuration
        """
        self.config = config
        self.last_error: str | None = None
        self.last_latency_ms: int = 0

        # Agent system prompt (set dynamically by AIController for tool-use mode)
        self._agent_system_prompt: str | None = None

        # Native tool schemas (set dynamically for providers with SUPPORTS_NATIVE_TOOLS)
        self._native_tools: list[dict] | None = None
        self._agent_tool_choice: str = "auto"
        self._preferred_first_tool: str | None = None

        # Diagnostics bus (lazy loaded)
        self._diagnostics_bus = None

    @property
    def effective_model(self) -> str:
        """Model name in use (for step logging + agent loop telemetry).

        Subclasses may override (e.g. OpenAIAgentsProvider returns the routed
        model after the codex compat layer's auto-switch). Default reads from
        config so every BaseLLMProvider subclass exposes the same attribute,
        keeping agent_loop / agent_executor consumers agnostic.
        """
        return self.config.model

    def _get_diagnostics_bus(self):
        """Get diagnostics bus lazily."""
        if self._diagnostics_bus is None:
            try:
                from diagnostics.bus import get_diagnostics_bus

                self._diagnostics_bus = get_diagnostics_bus()
            except ImportError:
                pass
        return self._diagnostics_bus

    def _reject_route_command_agent_state(self) -> None:
        """Fail closed if old provider globals try to drive an agent turn."""
        if getattr(self, "_agent_system_prompt", None) or getattr(self, "_native_tools", None):
            raise RuntimeError(
                "%s.route_command() cannot run agent/native-tool state; use route_command_native()"
                % self.__class__.__name__
            )

    @abstractmethod
    async def ask(
        self,
        question: str,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        """
        Ask a question and get a response.

        Args:
            question: User's question
            system_prompt: Optional system prompt
            include_history: Deprecated compatibility flag. Prompt context is
                supplied by the caller's PromptFrameBundle.
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature

        Returns:
            Dict with 'content', 'tokens_used', 'model', 'error' keys
        """
        pass

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
        *,
        system_context: str | None = None,
        model_override: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Route a single non-agent turn to a tool call or answer.

        This is the legacy simple-answer/voice lane, distinct from the agent
        loop (which uses ``route_command_native``). It is a **concrete default**
        expressed entirely through the one universal contract: it renders a
        single user turn and delegates to ``route_command_native``. A provider
        that has no bespoke non-agent path therefore needs to implement only
        ``route_command_native`` — there is no second command entry point for it
        to add (CLAUDE.md "One agent loop": extend the native contract, never a
        parallel loop). Providers with a specialised non-agent routing path
        (OpenAI/Anthropic/Google/Ollama/OpenAI-compatible) override this method.

        Args:
            text: User's request text.
            history: Deprecated compatibility input. Implementations must
                ignore it and use ``context_bundle`` frames instead.
            context_bundle: Optional prompt-frame bundle for per-turn context.
            max_tokens: Maximum tokens in response.
            system_context: Deprecated legacy string context, accepted only so
                the historical ``provider_router``/``provider_fallback`` call
                shape keeps working; rendered into a bundle when no bundle was
                supplied.
            model_override: Optional per-turn model name override.

        Returns:
            Dict with 'type' ('tool_call' or 'answer'), 'tool', 'args', 'answer'.
        """
        self._warn_ignored_history_arg(history, "route_command")
        bundle = context_bundle
        if bundle is None and system_context:
            from services.llm.prompts import runtime_context_bundle

            bundle = runtime_context_bundle(system_context, origin="legacy_system_context")
        return await self.route_command_native(
            [{"role": "user", "content": text}],
            native_tools=self._native_tools,
            prompt_context_bundle=bundle,
            max_tokens=max_tokens,
            model_override=model_override,
        )

    @abstractmethod
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
        """Execute one turn of the agent loop with native tool-calling.

        This is the universal contract the agent loop uses to talk to any
        provider. Every concrete provider must implement it by translating
        these arguments into its own native API. Implementations may accept
        additional keyword arguments for provider-specific features, but
        callers should only depend on the standard signature.

        Args:
            messages: Full multi-turn message list, including the system
                message (if not passed separately) and the current user
                request. Each entry has ``role`` (``user``/``assistant``/
                ``system``/``tool``) and ``content``. ``content`` may be a
                string or a structured list for providers that support
                tool_use / tool_result blocks.
            native_tools: Tool schemas the provider should expose to the
                model. Format may vary per provider; the loop builds these.
            system_prompt: Deprecated compatibility input. Agent-loop callers
                pass ``prompt_context_bundle`` so providers render system
                instructions at the adapter boundary.
            prompt_context_bundle: Prompt-frame bundle for provider-rendered
                system/static/dynamic context.
            max_tokens: Output budget for this turn. ``None`` means use the
                provider/model default.
            model_override: Optional model name override for this turn.
            first_turn: True if this is the first turn of the task (some
                providers use this to gate first-turn-only logic).
            tool_choice_override: Optional override for the provider's
                tool_choice behavior (``"auto"``, ``"required"``, etc.).

        Returns:
            A dict describing the model's response. Standard fields:
              - ``type``: ``"tool_call"`` or ``"answer"``
              - ``tool`` / ``args`` / ``tool_use_id`` (if tool_call)
              - ``answer`` (if answer)
              - ``reasoning``: optional readable reasoning payload for trace-v2
                observability. Use provider summaries when raw chain-of-thought
                is not exposed.
              - ``_continuity``: provider-internal continuity payload, if any
              - ``_reasoning``: transitional provider-private readable
                reasoning text; the agent loop normalizes this into
                ``reasoning`` before trace persistence.
              - ``_usage``: token usage stats, if available
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if provider is configured and ready.

        Returns:
            True if provider can be used
        """
        pass

    @abstractmethod
    async def test_connection(self) -> LLMTestResult:
        """
        Test the connection and return detailed result.

        Returns:
            LLMTestResult with success status and details
        """
        pass

    @abstractmethod
    def get_available_models(self) -> list[str]:
        """
        Get list of available models for this provider.

        Returns:
            List of model identifiers
        """
        pass

    def get_provider_type(self) -> str:
        """Get the provider type identifier."""
        return self.config.provider

    def get_provider_name(self) -> str:
        """Get human-readable provider name."""
        info = get_provider_info(self.config.provider)
        return info.name if info else self.config.provider

    def get_unavailable_reason(self) -> str | None:
        """
        Get reason why provider is unavailable.

        Returns:
            Human-readable reason or None if available
        """
        if self.is_available():
            return None
        return self.last_error or "Provider not configured"

    def clear_history(self) -> None:
        """Deprecated no-op retained for older callers."""

        self._warn_legacy_history_call("clear_history")

    def get_history(self) -> list[dict[str, str]]:
        """Deprecated compatibility reader. The provider owns no prompt state."""

        self._warn_legacy_history_call("get_history")
        return []

    def add_to_history(self, role: str, content: str) -> None:
        """Deprecated no-op retained for older callers.

        Args:
            role: 'user' or 'assistant'
            content: Message content
        """

        _ = (role, content)
        self._warn_legacy_history_call("add_to_history")

    def _warn_legacy_history_call(self, method_name: str) -> None:
        # REMOVE AFTER USERS: role/content provider history was retired in favor
        # of ConversationStateManager + PromptFrameBundle.
        logger.warning(
            "%s.%s is deprecated and ignored; pass canonical prompt frames instead",
            type(self).__name__,
            method_name,
        )

    def _warn_ignored_history_arg(self, value: object, method_name: str) -> None:
        if value:
            # REMOVE AFTER USERS: callers may still pass legacy role/content
            # lists during migration, but providers must not use them.
            logger.warning(
                "%s.%s ignored legacy role/content prompt input; use PromptFrameBundle",
                type(self).__name__,
                method_name,
            )

    def _emit_diagnostics(
        self,
        event: str,
        severity: str = "INFO",
        **context: Any,
    ) -> None:
        """Emit diagnostics event if bus available."""
        bus = self._get_diagnostics_bus()
        if bus:
            try:
                bus.emit(
                    f"llm.{self.config.provider}.{event}",
                    severity=severity,
                    provider=self.config.provider,
                    model=self.config.model,
                    **context,
                )
            except Exception as e:
                logger.debug("Operation failed: %s", e, exc_info=True)
                pass  # Don't fail on diagnostics errors
