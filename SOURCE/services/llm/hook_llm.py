"""Hook-prompt LLM dispatcher.

This module is the integration boundary between the settings/session hook
runner (:mod:`intent.hooks.settings_runner`) and the rest of Viola's LLM
stack. Prompts run through the small/fast managed model unless the hook
explicitly opts into a model override.

Imports are kept lazy so the runner module can be imported (and unit-tested)
before the LLM stack is wired up.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


async def invoke_hook_prompt(prompt: str, *, model: str | None = None) -> str | None:
    """Run a hook prompt against the managed LLM.

    The verifier path is decoupled from the agent loop — hook prompts must
    NOT register tool calls or trigger downstream MCP routing. Returns the
    plain-text reply, or ``None`` when no provider is available.
    """

    provider = _resolve_hook_provider()
    if provider is None:
        logger.debug("No hook-LLM provider configured; prompt hook is a no-op")
        return None
    try:
        response = await _ask_provider(provider, prompt, model=model)
    except Exception as exc:
        logger.warning("Hook LLM call failed: %s", exc)
        return None
    return response


async def invoke_verifier_agent(prompt: str, *, model: str | None = None) -> str | None:
    """Run a verifier-style agent hook.

    Verifier prompts are not allowed to invoke tools, so the implementation
    is identical to :func:`invoke_hook_prompt` for now. Should we ever wire
    a richer verifier-only provider, this is the place to swap it in.
    """

    return await invoke_hook_prompt(prompt, model=model)


def _resolve_hook_provider() -> Any:
    """Resolve the LLM provider used for hook prompts.

    Kept lazy to avoid importing the LLM stack from a settings-only context.
    The resolver intentionally returns ``None`` when no provider is set up so
    hook prompts degrade to no-ops instead of breaking the request.
    """

    try:
        from services.llm import factory
    except ImportError:
        return None
    get_provider = getattr(factory, "get_default_provider", None)
    if get_provider is None:
        return None
    try:
        return get_provider()
    except Exception as exc:
        logger.debug("Hook LLM provider resolution failed: %s", exc)
        return None


async def _ask_provider(provider: Any, prompt: str, *, model: str | None) -> str | None:
    """Send a single-turn prompt to ``provider`` and return the assistant text."""

    if hasattr(provider, "ask_async"):
        result = provider.ask_async(prompt, model=model)
    elif hasattr(provider, "ask"):
        result = provider.ask(prompt, model=model)
    else:
        return None
    if asyncio.iscoroutine(result):
        result = await result
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        text = result.get("content") or result.get("text") or result.get("message")
        if isinstance(text, str):
            return text
    return None


__all__ = ["invoke_hook_prompt", "invoke_verifier_agent"]
