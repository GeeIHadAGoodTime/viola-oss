"""
LLM Provider Abstractions

Provides provider-agnostic abstractions for multiple LLM providers:
- OpenAI (GPT-4, GPT-3.5, etc.)
- Anthropic (Claude)
- Google (Gemini)
- Ollama (Local models)
- OpenAI-compatible APIs (Groq, Together.ai, vLLM, etc.)

Usage:
    from services.llm import ProviderAgnosticRouter, create_router

    # Create router from user settings
    router = create_router()

    # Route commands
    result = await router.route_command("play some music")

    # Ask questions
    answer = await router.ask("What's the weather like?")

Direct OpenAI access is intentionally not exported from this package. Use the
provider factory/router so OpenAI requests share the canonical provider path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.llm.factory import (
        LLMProviderFactory,
        create_llm_provider,
        create_provider_from_settings,
        get_available_providers,
    )
    from services.llm.key_pool import KeyPool
    from services.llm.provider_router import ProviderAgnosticRouter, create_router
    from services.llm.providers import (
        BaseLLMProvider,
        LLMConfig,
        LLMProviderInfo,
        LLMProviderType,
        LLMTestResult,
    )

__all__ = [
    "BaseLLMProvider",
    "KeyPool",
    "LLMConfig",
    "LLMProviderFactory",
    "LLMProviderInfo",
    "LLMProviderType",
    "LLMTestResult",
    "ProviderAgnosticRouter",
    "create_llm_provider",
    "create_provider_from_settings",
    "create_router",
    "get_available_providers",
]


def __getattr__(name: str) -> Any:
    """Resolve public LLM exports without importing every provider up front."""

    if name in {
        "LLMProviderFactory",
        "create_llm_provider",
        "create_provider_from_settings",
        "get_available_providers",
    }:
        from services.llm.factory import (
            LLMProviderFactory,
            create_llm_provider,
            create_provider_from_settings,
            get_available_providers,
        )

        exports = {
            "LLMProviderFactory": LLMProviderFactory,
            "create_llm_provider": create_llm_provider,
            "create_provider_from_settings": create_provider_from_settings,
            "get_available_providers": get_available_providers,
        }
        globals().update(exports)
        return exports[name]
    if name in {"ProviderAgnosticRouter", "create_router"}:
        from services.llm.provider_router import ProviderAgnosticRouter, create_router

        exports = {
            "ProviderAgnosticRouter": ProviderAgnosticRouter,
            "create_router": create_router,
        }
        globals().update(exports)
        return exports[name]
    if name in {
        "BaseLLMProvider",
        "LLMConfig",
        "LLMProviderInfo",
        "LLMProviderType",
        "LLMTestResult",
    }:
        from services.llm.providers import (
            BaseLLMProvider,
            LLMConfig,
            LLMProviderInfo,
            LLMProviderType,
            LLMTestResult,
        )

        exports = {
            "BaseLLMProvider": BaseLLMProvider,
            "LLMConfig": LLMConfig,
            "LLMProviderInfo": LLMProviderInfo,
            "LLMProviderType": LLMProviderType,
            "LLMTestResult": LLMTestResult,
        }
        globals().update(exports)
        return exports[name]
    if name == "KeyPool":
        from services.llm.key_pool import KeyPool as _KP

        globals()[name] = _KP
        return _KP
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
