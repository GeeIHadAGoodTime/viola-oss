"""Execute a ``prompt`` hook from settings/session config.

Parity reference: ``src/utils/hooks/execPromptHook.ts``. The hook sends the
hook envelope JSON to a small/fast LLM and treats its plain-text answer as
``additional_context`` unless the answer is itself a hook JSON object.

Viola treats prompts as Tier 3 / local-only — the prompt runs through the
configured managed model unless explicitly overridden.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from core.logging_config import get_logger
from intent.hooks.settings_runner import HookCommand, HookExecutionContext

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_CHARS = 8000

ExecPromptHook = Callable[[HookCommand, Mapping[str, Any], HookExecutionContext], Awaitable[Any]]


async def default_exec_prompt(
    hook: HookCommand,
    envelope: Mapping[str, Any],
    context: HookExecutionContext,
) -> Any:
    """Execute a ``prompt`` hook against the small/fast LLM.

    The runner is deliberately small — providers expose a unified
    ``invoke_hook_prompt`` if available; otherwise the hook returns a
    ``additional_context`` blob explaining the missing provider.
    """

    if not hook.prompt:
        return None

    prompt = _interpolate_arguments(hook.prompt, envelope)
    timeout = hook.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS

    try:
        response = await asyncio.wait_for(
            _invoke_hook_llm(prompt, model=hook.model),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("Prompt hook timeout after %.0fs: %s", timeout, hook.describe())
        return None
    except Exception as exc:
        logger.exception("Prompt hook failed: %s", hook.describe())
        return None

    if not response:
        return None
    response = response.strip()[:_MAX_RESPONSE_CHARS]
    if response.startswith("{") or response.startswith("["):
        try:
            return _coerce_json_response(json.loads(response))
        except json.JSONDecodeError:
            pass
    return {"additionalContext": response}


def _coerce_json_response(value: Any) -> Any:
    if isinstance(value, dict) and value.get("ok") is False:
        return {
            "permissionBehavior": "deny",
            "reason": str(value.get("reason") or value.get("error") or "Hook verifier returned ok=false."),
        }
    return value


def _interpolate_arguments(prompt: str, envelope: Mapping[str, Any]) -> str:
    """Replace ``$ARGUMENTS`` with the JSON-encoded envelope, per Claude."""

    if "$ARGUMENTS" not in prompt:
        return prompt
    args_json = json.dumps(envelope, sort_keys=True, default=str)
    return prompt.replace("$ARGUMENTS", args_json)


async def _invoke_hook_llm(prompt: str, *, model: str | None) -> str | None:
    """Dispatch to the small/fast managed LLM.

    Implementation kept lazy: pipeline wires the actual provider call when
    booting the runner. The default returns ``None`` so the prompt hook is
    a no-op when no provider is configured (e.g. during unit tests).
    """

    try:
        from services.llm.hook_llm import invoke_hook_prompt
    except ImportError:
        return None
    result = invoke_hook_prompt(prompt, model=model)
    if asyncio.iscoroutine(result):
        return await result
    return result


__all__ = ["ExecPromptHook", "default_exec_prompt"]
