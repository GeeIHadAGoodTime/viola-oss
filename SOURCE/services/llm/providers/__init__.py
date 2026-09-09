"""
LLM Provider Implementations

Provider-agnostic LLM support for multiple backends:
- OpenAI Compatible (OpenAI, Groq, Together.ai, etc.)
- Anthropic (Claude)
- Google (Gemini)
- Ollama (Local)
"""

from __future__ import annotations

from services.llm.providers.base import (
    PROVIDER_INFO,
    BaseLLMProvider,
    LLMConfig,
    LLMProviderInfo,
    LLMProviderType,
    LLMTestResult,
    get_all_providers,
    get_provider_info,
)

# Lazy load providers to avoid import errors if dependencies missing
__all__ = [
    "PROVIDER_INFO",
    "AnthropicProvider",
    # Base classes and types
    "BaseLLMProvider",
    "GoogleProvider",
    "LLMConfig",
    "LLMProviderInfo",
    "LLMProviderType",
    "LLMTestResult",
    "OllamaNativeProvider",
    # Provider classes (lazy loaded)
    "OpenAICompatibleProvider",
    "get_all_providers",
    # Helper functions
    "get_provider_info",
]


def __getattr__(name: str):
    """Lazy load provider classes to handle missing dependencies gracefully."""
    if name == "OpenAICompatibleProvider":
        from services.llm.providers.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider
    elif name == "AnthropicProvider":
        from services.llm.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider
    elif name == "GoogleProvider":
        from services.llm.providers.google_provider import GoogleProvider

        return GoogleProvider
    elif name == "OllamaNativeProvider":
        from services.llm.providers.ollama_native_provider import OllamaNativeProvider

        return OllamaNativeProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
