"""Execute an ``agent`` (verifier) hook from settings/session config.

Parity reference: ``src/utils/hooks/execAgentHook.ts``. The hook is treated
as a verifier — a focused agentic prompt that returns either an
``additionalContext`` message or a permission-style ``deny`` to block the
tool call. Verification is best-effort: a missing managed-agent provider
results in a no-op rather than a hard failure.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from core.logging_config import get_logger
from intent.hooks.settings_runner import HookCommand, HookExecutionContext

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_CHARS = 8000

ExecAgentHook = Callable[[HookCommand, Mapping[str, Any], HookExecutionContext], Awaitable[Any]]


async def default_exec_agent(
    hook: HookCommand,
    envelope: Mapping[str, Any],
    context: HookExecutionContext,
) -> Any:
    """Run a verifier agent hook.

    Verifiers may BLOCK by returning a JSON object with
    ``"permissionBehavior": "deny"``. Anything else is folded into
    ``additionalContext``.
    """

    if not hook.prompt:
        return None

    prompt = _interpolate_arguments(hook.prompt, envelope)
    timeout = hook.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS

    try:
        response = await asyncio.wait_for(
            _invoke_verifier(prompt, model=hook.model),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("Agent hook timeout after %.0fs: %s", timeout, hook.describe())
        return None
    except Exception as exc:
        logger.exception("Agent hook failed: %s", hook.describe())
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
    if "$ARGUMENTS" not in prompt:
        return prompt
    return prompt.replace("$ARGUMENTS", json.dumps(envelope, sort_keys=True, default=str))


async def _invoke_verifier(prompt: str, *, model: str | None) -> str | None:
    """Dispatch to the verifier-style agent.

    The hook agent runs through the same provider stack the main loop uses —
    we delegate to :mod:`services.llm.hook_llm` which decides which provider
    + model the verifier should hit. If no verifier wiring is present, the
    hook is a no-op.
    """

    try:
        from services.llm.hook_llm import invoke_verifier_agent
    except ImportError:
        return None
    result = invoke_verifier_agent(prompt, model=model)
    if asyncio.iscoroutine(result):
        return await result
    return result


__all__ = ["ExecAgentHook", "default_exec_agent"]
