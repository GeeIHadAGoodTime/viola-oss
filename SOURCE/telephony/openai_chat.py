"""OpenAI Chat Completions helpers for phone-side background calls."""

from __future__ import annotations

import re
from typing import Any

from config.defaults import get_configured_reasoning_effort

_REASONING_MODEL_RE = re.compile(r"^(?:o[134](?:[-\d]|$)|gpt-5)", re.IGNORECASE)


def is_reasoning_chat_model(model: str | None) -> bool:
    """Return True for Chat Completions models that need reasoning token params."""
    if not model:
        return False
    return bool(_REASONING_MODEL_RE.match(model.strip()))


def phone_chat_completion_options(model: str, max_tokens: int) -> dict[str, Any]:
    """Return token/reasoning options for phone Chat Completions requests."""
    if is_reasoning_chat_model(model):
        return {
            "max_completion_tokens": max_tokens,
            "reasoning_effort": get_configured_reasoning_effort("phone", model),
        }
    return {"max_tokens": max_tokens}
