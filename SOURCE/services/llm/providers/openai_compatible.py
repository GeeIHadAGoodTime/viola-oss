"""
OpenAI-Compatible LLM Provider

Works with any OpenAI-compatible API:
- OpenAI
- Groq
- Together.ai
- Mistral
- vLLM
- LM Studio
- Ollama (OpenAI mode)
- Any other OpenAI-compatible endpoint
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from config.defaults import get_configured_reasoning_effort
from core.logging_config import get_logger
from core.subprocess_utils import run_silent
from services.conversation.frame_rendering import (
    render_for_openai_responses,
    render_openai_responses_instructions,
)
from services.llm.model_fallback import FALLBACK_MODEL
from services.llm.no_result import (
    NO_RESULT_RETRY_INSTRUCTION,
    build_ai_error_no_result,
    build_ai_no_result,
)
from services.llm.openai_consent import enforce_storage_consent
from services.llm.openai_utils import (
    OPENAI_DEFAULT_BASE_URL,
    RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID,
    RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS,
    extract_message_text_from_response as _extract_message_text_from_response,
    extract_reasoning_text_from_response as _extract_reasoning_text_from_response,
    extract_response_continuity_metadata as _extract_response_continuity_metadata,
    is_stale_verifier_prompt as _is_stale_verifier_prompt,
    normalize_response_continuity as _normalize_response_continuity,
    redact_responses_payload as _redact_responses_payload,
    response_output_item_to_input_item as _response_output_item_to_input_item,
)
from services.llm.operator_diagnostics import classify_llm_operator_error
from services.llm.prompts import build_provider_prompt_bundle, runtime_context_bundle
from services.llm.providers.base import BaseLLMProvider, LLMConfig, LLMTestResult
from services.llm.stream_bus import get_current_command_stream_id
from services.llm.stream_capture import (
    StreamChunkAggregator,
    capture_openai_responses_stream_event,
    summarize_openai_response,
    summarize_stream_error,
    summarize_usage,
)
from services.llm.token_limits import clamp_max_tokens
from services.llm.tool_arg_retry import (
    append_chat_retry_message,
    append_responses_retry_input,
    build_empty_tool_arguments_retry,
    coerce_tool_call_arguments,
)

logger = get_logger(__name__)

_CODEX_SUBSCRIPTION_API_KEY = "codex-subscription"  # pragma: allowlist secret

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle


def _uses_codex_subscription_transport(config: LLMConfig) -> bool:
    """Return True for the Codex subscription provider's injected transport."""
    return config.api_key == _CODEX_SUBSCRIPTION_API_KEY


class _ResponsesStreamFallbackError(RuntimeError):
    """Recoverable Responses stream terminal state that should retry without streaming."""

    def __init__(self, retry_error_type: str) -> None:
        super().__init__("OpenAI Responses stream requires non-streaming fallback: %s" % retry_error_type)
        self.retry_error_type = retry_error_type


class OpenAIResponsesToolSchemaError(ValueError):
    """Raised before sending a Responses request with a non-strict function schema."""

    def __init__(self, tool_name: str, issues: list[str]) -> None:
        self.tool_name = tool_name or "<unknown>"
        self.issues = list(issues)
        super().__init__(
            "OpenAI Responses tool schema for %r is not strict-compatible: %s"
            % (self.tool_name, "; ".join(self.issues))
        )


def _policy_fallback_model(model_name: str) -> str | None:
    """Return the model-level fallback unless the request is already on it."""

    effective_model = str(model_name or "").strip()
    return FALLBACK_MODEL if effective_model and effective_model != FALLBACK_MODEL else None


# Check for openai package
_openai: ModuleType | None = None
try:
    import openai as _openai_imported

    _openai = _openai_imported
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


# Well-known OpenAI-compatible endpoints
KNOWN_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "mistral": "https://api.mistral.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
    "cohere": "https://api.cohere.ai/compatibility/v1",
}

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMMIT_SHA_CACHE: str | None = None
_RUNTIME_SHA_CACHE: str | None = None
_COMMIT_SHA_ENV_KEYS = ("VIOLA_COMMIT_SHA", "GIT_COMMIT", "GITHUB_SHA")


def _coerce_usage_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_field(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        return value.get(field_name)
    return getattr(value, field_name, None)


def _cached_tokens_from_usage(usage: Any) -> int:
    """Extract OpenAI cached-token metadata from Responses or Chat usage."""
    if usage is None:
        return 0
    for details_name in ("input_tokens_details", "prompt_tokens_details"):
        details = _usage_field(usage, details_name)
        cached = _usage_field(details, "cached_tokens")
        if cached is not None:
            return _coerce_usage_int(cached)
    return 0


def _cache_write_tokens_from_usage(usage: Any) -> int:
    """Extract cache-creation/write token metadata from provider usage."""
    if usage is None:
        return 0
    for field_name in ("cache_write_tokens", "cache_creation_input_tokens"):
        value = _usage_field(usage, field_name)
        if value is not None:
            return _coerce_usage_int(value)
    for details_name in ("input_tokens_details", "prompt_tokens_details"):
        details = _usage_field(usage, details_name)
        for field_name in ("cache_write_tokens", "cache_creation_tokens"):
            value = _usage_field(details, field_name)
            if value is not None:
                return _coerce_usage_int(value)
    return 0


def _web_search_requests_from_usage(usage: Any) -> int:
    """Extract server-tool web-search request count from provider usage."""
    if usage is None:
        return 0
    direct = _usage_field(usage, "web_search_requests")
    if direct is not None:
        return _coerce_usage_int(direct)
    server_tool = _usage_field(usage, "server_tool_use")
    return _coerce_usage_int(_usage_field(server_tool, "web_search_requests"))


_RUNTIME_SHA_ENV_KEYS = (
    "VIOLA_RUNTIME_SHA",
    "RENDER_GIT_COMMIT",
    "RAILWAY_GIT_COMMIT_SHA",
    "VERCEL_GIT_COMMIT_SHA",
    "GITHUB_SHA",
)
_LOOKAROUND_PATTERN_TOKENS = ("(?=", "(?!", "(?<=", "(?<!")

# OpenAI prompt-cache routing namespace. ``prompt_cache_key`` is combined with
# the request's prefix hash to pin requests that share a long, common prefix to
# the same cache partition (https://developers.openai.com/api/docs/guides/prompt-caching
# "Cache Routing"). Without it the API load-balances each request by prefix hash
# alone, so late turns of a long agent run increasingly route to machines that
# don't hold the cached prefix and ``cached_tokens`` collapses to 0 — exactly
# the LLC trace fa6c24ee7d31 regression (step 29/30 input_tokens~15k,
# cache_read=0). The key must be STABLE across the turns of one conversation
# (so the shared instructions+tools+early-history prefix keeps hitting) and
# DISTINCT per conversation/user (so we never funnel many users' traffic onto a
# single prefix-key pair — OpenAI's guidance is to keep a key under ~15 req/min,
# and cross-user funneling would also be a multi-tenant leak of routing scope).
_PROMPT_CACHE_KEY_NAMESPACE = "viola-conv"
# OpenAI caps prompt_cache_key length; keep well under it after namespacing.
_PROMPT_CACHE_KEY_MAX_LEN = 128


def _normalize_prompt_cache_key(raw: Any) -> str | None:
    """Return a sanitized, namespaced prompt_cache_key, or None when unusable.

    The key is opaque to OpenAI (it only routes by it); we namespace it so it
    never collides with a caller-supplied raw id and stays a stable, bounded
    string. Empty / non-string values yield None so the param is simply omitted
    (automatic prefix caching still applies; it just isn't pinned).
    """
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("%s:" % _PROMPT_CACHE_KEY_NAMESPACE):
        namespaced = value
    else:
        namespaced = "%s:%s" % (_PROMPT_CACHE_KEY_NAMESPACE, value)
    return namespaced[:_PROMPT_CACHE_KEY_MAX_LEN]


def _stream_event_field(event: Any, key: str, default: Any = None) -> Any:
    if event is None:
        return default
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


# Responses-API "incomplete" reasons we treat as equivalent to max_output_tokens
# for unified recovery. Claude's ``src/services/api/claude.ts:2279-2291`` maps
# ``model_context_window_exceeded`` -> ``max_output_tokens`` because, from the
# model's perspective, both mean "response was cut off, continue from where you
# left off." We normalize here so the agent loop's single recovery path
# (``intent/agent_loop.py:_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT``) catches both
# without needing a separate context-window branch.
_RESPONSES_CONTEXT_WINDOW_ALIASES = frozenset(
    {
        "model_context_window_exceeded",
        "context_length_exceeded",
        "context_window_exceeded",
    }
)


def _responses_incomplete_reason(response: Any) -> str | None:
    """Return the normalized ``incomplete`` reason for a Responses-API response.

    Returns ``None`` when the response is not in the ``incomplete`` status.
    Maps known context-window-exceeded aliases to ``max_output_tokens`` so the
    agent loop's existing max-output recovery path drives both.
    """

    if response is None or getattr(response, "status", None) != "incomplete":
        return None
    details = getattr(response, "incomplete_details", None)
    reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
    normalized = str(reason or "incomplete")
    if normalized in _RESPONSES_CONTEXT_WINDOW_ALIASES:
        return "max_output_tokens"
    return normalized


_RESPONSES_TOOL_COUNT_WARN_THRESHOLD = 80
_RESPONSES_TOOL_JSON_BYTES_WARN_THRESHOLD = 70_000
_RESPONSES_INSTRUCTIONS_BYTES_WARN_THRESHOLD = 20_000
_RESPONSES_TOOL_DESCRIPTION_MAX_CHARS = 300
_RESPONSES_SCHEMA_DESCRIPTION_MAX_CHARS = 160
_RESPONSES_SCHEMA_METADATA_DROP_KEYS = frozenset({"title", "default"})


def _with_no_result_retry_context(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    retry_messages = [dict(message) for message in messages]
    if retry_messages and retry_messages[0].get("role") == "system":
        existing = str(retry_messages[0].get("content") or "").strip()
        retry_messages[0]["content"] = "%s\n\n%s" % (
            existing,
            NO_RESULT_RETRY_INSTRUCTION,
        )
        retry_messages[0]["content"] = retry_messages[0]["content"].strip()
    else:
        retry_messages.insert(0, {"role": "system", "content": NO_RESULT_RETRY_INSTRUCTION})
    return retry_messages


def _with_responses_no_result_retry_context(
    api_kwargs: dict[str, Any],
) -> dict[str, Any]:
    retry_kwargs = copy.deepcopy(api_kwargs)
    existing = str(retry_kwargs.get("instructions") or "").strip()
    retry_kwargs["instructions"] = "%s\n\n%s" % (existing, NO_RESULT_RETRY_INSTRUCTION)
    retry_kwargs["instructions"] = retry_kwargs["instructions"].strip()
    return retry_kwargs


def _native_empty_response_retryable(parsed: dict[str, Any]) -> bool:
    if parsed.get("type") != "ai_no_result":
        return False
    no_result = parsed.get("no_result")
    if not isinstance(no_result, dict):
        no_result = {}
    reason = str(no_result.get("reason") or parsed.get("reason") or "")
    return reason == "empty_assistant_content" and bool(parsed.get("retryable", no_result.get("retryable", True)))


def _mark_native_empty_response_retry_attempted(parsed: dict[str, Any]) -> None:
    if parsed.get("type") != "ai_no_result":
        return
    parsed["retry_attempted"] = True
    no_result = parsed.get("no_result")
    if isinstance(no_result, dict):
        no_result["retry_attempted"] = True
    error_state = parsed.get("error_state")
    if isinstance(error_state, dict):
        error_state["retry_attempted"] = True


def _filter_orphan_responses_tool_pairs(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop invalid ``function_call_output`` items from Responses input.

    OpenAI Responses API rejects requests where a function_call_output's
    call_id does not match any function_call earlier in the input - error 400
    "No tool call found for function call output with call_id ...". This can
    happen after context compaction drops the assistant turn that contained
    the original function_call while keeping the user turn that contained its
    function_call_output. Compaction's frame-level tool-pair expansion handles
    most cases, but legacy paths and cross-turn drops can still produce
    orphans here.

    Mirrors Claude Code TS's local pair repair (utils/messages.ts): walk in
    request order, accept outputs only after their call has appeared, and keep
    at most one output for each call id. This avoids sending pre-call outputs
    or duplicate outputs that the API rejects even when the call id appears
    somewhere else in the assembled list.
    """

    seen_call_ids: set[str] = set()
    seen_output_ids: set[str] = set()
    filtered: list[dict[str, Any]] = []
    dropped_orphan = 0
    dropped_duplicate = 0
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            call_id = str(item.get("call_id") or "")
            if call_id:
                seen_call_ids.add(call_id)
            filtered.append(item)
            continue

        if isinstance(item, dict) and item.get("type") == "function_call_output":
            call_id = str(item.get("call_id") or "")
            if not call_id or call_id not in seen_call_ids:
                dropped_orphan += 1
                continue
            if call_id in seen_output_ids:
                dropped_duplicate += 1
                continue
            seen_output_ids.add(call_id)
        filtered.append(item)

    dropped = dropped_orphan + dropped_duplicate
    if dropped:
        logger.warning(
            "Dropped %d invalid function_call_output item(s) from Responses input "
            "(%d orphan/pre-call, %d duplicate). This recovers from post-compaction "
            "or corrupted transcript state before the provider sees the request.",
            dropped,
            dropped_orphan,
            dropped_duplicate,
        )
    return filtered


def _rendered_openai_to_chat_messages(rendered: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    instructions = str(rendered.get("instructions") or "").strip()
    if instructions:
        messages.append({"role": "system", "content": instructions})
    for item in rendered.get("input") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    return messages


def _structured_route_response_contract() -> str:
    return (
        "For this legacy structured-response fallback, respond with a single JSON object.\n"
        'Tool call: {"type": "tool_call", "tool": "<tool_name>", "args": {"param": "value"}, '
        '"continue_listening": false}\n'
        'Answer: {"type": "answer", "answer": "<plain response>", "continue_listening": false}\n'
        'Ignore ambient speech only when clearly not directed at Viola: {"type": "ignore", '
        '"reason": "<why>", "continue_listening": false}\n'
        "Return raw JSON only."
    )


def _normalize_structured_route_payload(parsed: dict[str, Any], raw_content: str) -> dict[str, Any]:
    resp_type = str(parsed.get("type", ""))
    if resp_type == "text" and isinstance(parsed.get("text"), str):
        return {"type": "answer", "answer": parsed["text"]}
    if resp_type == "tool_use":
        parsed["type"] = "tool_call"

    if parsed.get("type") == "command":
        return build_ai_no_result(
            "legacy_command_envelope_rejected",
            response_preview=raw_content[:200],
            retryable=True,
        )
    if parsed.get("type") == "tool_call":
        if "tool" not in parsed and "command" in parsed:
            parsed["tool"] = parsed.get("command", "")
        if "args" not in parsed and "params" in parsed:
            parsed["args"] = parsed.get("params", {})
        if "tool" not in parsed:
            parsed["tool"] = ""
        if "args" not in parsed or not isinstance(parsed["args"], dict):
            parsed["args"] = {}
    elif parsed.get("type") == "answer":
        if "answer" not in parsed:
            parsed["answer"] = ""
    elif parsed.get("type") == "ignore":
        if "reason" not in parsed:
            parsed["reason"] = ""
    else:
        parsed = {
            "type": "answer",
            "answer": parsed.get("answer", raw_content),
        }
    return parsed


def _is_openai_assistant_delta_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    if str(message.get("role") or "").strip().lower() != "assistant":
        return False
    content = message.get("content")
    return isinstance(content, dict) and bool(content.get("_openai_assistant"))


def _clean_sha(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _read_git_head_sha() -> str | None:
    try:
        result = run_silent(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )  # proc-tree-ok: single git binary, no shell, no grandchildren
    except Exception:
        logger.debug("Unable to resolve git HEAD for responses fingerprint", exc_info=True)
        return None
    if result.returncode != 0:
        return None
    return _clean_sha(result.stdout)


def _get_commit_sha() -> str:
    global _COMMIT_SHA_CACHE
    if _COMMIT_SHA_CACHE is None:
        for env_key in _COMMIT_SHA_ENV_KEYS:
            sha = _clean_sha(os.environ.get(env_key))
            if sha:
                _COMMIT_SHA_CACHE = sha
                break
        if _COMMIT_SHA_CACHE is None:
            _COMMIT_SHA_CACHE = _read_git_head_sha() or "unknown"
    return _COMMIT_SHA_CACHE


def _get_runtime_sha() -> str:
    global _RUNTIME_SHA_CACHE
    if _RUNTIME_SHA_CACHE is None:
        for env_key in _RUNTIME_SHA_ENV_KEYS:
            sha = _clean_sha(os.environ.get(env_key))
            if sha:
                _RUNTIME_SHA_CACHE = sha
                break
        if _RUNTIME_SHA_CACHE is None:
            _RUNTIME_SHA_CACHE = _get_commit_sha()
    return _RUNTIME_SHA_CACHE


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8",
        "ignore",
    )


def _string_or_json_bytes(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8", "ignore"))
    return len(_canonical_json_bytes(value))


def _coerce_status_code(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _responses_error_status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "code"):
        status = _coerce_status_code(getattr(exc, attr, None))
        if status is not None:
            return status

    response = getattr(exc, "response", None)
    if response is not None:
        status = _coerce_status_code(getattr(response, "status_code", None))
        if status is not None:
            return status

    exc_str = str(exc).lower()
    for status in (529, 500, 502, 503, 504):
        if str(status) in exc_str:
            return status
    return None


def _responses_error_should_surface(exc: BaseException) -> bool:
    """Return True for Responses failures that must not enter model fallback."""
    status = _responses_error_status_code(exc)
    if status == 400:
        return True
    return status is not None and 500 <= status < 600 and status != 529


def _supports_responses_api(config: LLMConfig) -> bool:
    """Return True when the endpoint should use the OpenAI Responses API."""
    provider = (config.provider or "").strip().lower()
    if provider == "openai":
        return True
    if provider != "openai_compatible":
        return False
    base_url = (config.base_url or OPENAI_DEFAULT_BASE_URL).strip()
    parsed = urlparse(base_url)
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    return netloc == "api.openai.com" and path in ("", "/v1")


def _is_localish_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("192.168.") or host.startswith("10.")


def _enforce_storage_consent_for_config(config: LLMConfig, api_kwargs: dict[str, Any]) -> dict[str, Any]:
    if _supports_responses_api(config):
        return enforce_storage_consent(api_kwargs)
    api_kwargs.pop("store", None)
    return api_kwargs


async def _responses_create_with_storage_consent(client: Any, config: LLMConfig, api_kwargs: dict[str, Any]) -> Any:
    _enforce_storage_consent_for_config(config, api_kwargs)
    from diagnostics import latency_spans

    with latency_spans.span(
        "SDK_RESPONSES_CREATE",
        model=str(api_kwargs.get("model") or ""),
        stream=bool(api_kwargs.get("stream")),
    ):
        return await client.responses.create(**api_kwargs)


async def _chat_create_resilient(client: Any, config: LLMConfig, api_kwargs: dict[str, Any]) -> Any:
    """``chat.completions.create`` that survives endpoints which reject our
    ``response_format``.

    Local and third-party OpenAI-compatible servers (LM Studio, llama.cpp,
    older builds) do not all support the legacy ``{"type": "json_object"}``
    response format — newer LM Studio only accepts ``json_schema`` or ``text``
    and answers a bare 400. The adapter already parses JSON out of raw content
    (code-fence stripping, brace repair), so dropping ``response_format`` is a
    safe degradation rather than a failure. On fallback the key is removed from
    ``api_kwargs`` in place so any later reuse of the same dict stays clean.
    """
    _enforce_storage_consent_for_config(config, api_kwargs)
    try:
        return await client.chat.completions.create(**api_kwargs)
    except Exception as exc:
        message = str(getattr(exc, "message", "") or exc)
        if (
            "response_format" in api_kwargs
            and "response_format" in message
            and ("400" in message or type(exc).__name__ == "BadRequestError")
        ):
            api_kwargs.pop("response_format", None)
            logger.warning("Endpoint rejected response_format; retrying without it (server lacks json_object support)")
            _enforce_storage_consent_for_config(config, api_kwargs)
            return await client.chat.completions.create(**api_kwargs)
        raise


# Viola's agent system prompt + tool schemas run ~2600 tokens; with output and
# a little conversation history a local model needs noticeably more than that.
# Below this floor, local commands fail with an opaque context-overflow 400, so
# connection validation warns the user to load the model with a larger context.
_AGENT_MIN_CONTEXT_TOKENS = 4096


def _strip_llm_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM response text.

    Handles ``\\`\\`\\`json ... \\`\\`\\`` wrapping as well as partial/trailing
    fences.  Returns cleaned text ready for JSON parsing.
    """
    import re as _re

    content = text.strip()
    # Full fence: ```json\n...\n```  or  ```\n...\n```
    m = _re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", content, _re.DOTALL)
    if m:
        return m.group(1).strip()
    # Leading fence only
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    # Trailing fence only
    if content.endswith("```"):
        content = content[:-3]
    # Trailing incomplete fence fragment
    content = _re.sub(r"\s*```(?:json)?\s*$", "", content)
    return content.strip()


# OpenAI requires tool names to match ^[a-zA-Z0-9_-]{1,64}$.
_OPENAI_TOOL_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")
_OPENAI_TOOL_NAME_MAX_LEN = 64


# Reasoning-model family matcher.  These families reject the legacy
# ``max_tokens`` parameter (must use ``max_completion_tokens``) and do not
# accept ``temperature != 1`` or ``top_p != 1``.  Pattern matches any model
# name that starts with ``o1``/``o3``/``o4`` (optionally followed by ``-``
# or digits) OR starts with ``gpt-5`` (covers gpt-5, gpt-5-mini, gpt-5.4,
# gpt-5.4-mini, gpt-5-codex, etc.).  Use a family regex, not a hardcoded
# list — OpenAI ships new variants frequently.
_REASONING_MODEL_RE = re.compile(r"^(?:o[134](?:[-\d]|$)|gpt-5)", re.IGNORECASE)


def _is_reasoning_model(model: str | None) -> bool:
    """Return True if *model* is an OpenAI reasoning model.

    Reasoning models (o1/o3/o4/gpt-5 families) require
    ``max_completion_tokens`` instead of ``max_tokens`` and reject
    non-default ``temperature`` / ``top_p`` values.
    """
    if not model:
        return False
    return bool(_REASONING_MODEL_RE.match(model.strip()))


def _apply_token_budget(
    api_kwargs: dict[str, Any],
    model: str,
    max_tokens: int,
    *,
    temperature: float | None = None,
    min_reasoning_budget: int = 0,
    is_agent: bool = False,
) -> None:
    """Populate token / sampling / reasoning params on *api_kwargs*.

    For reasoning models (o1/o3/o4/gpt-5.x):
      - ``max_completion_tokens`` (possibly floored at ``min_reasoning_budget``
        to leave room for internal chain-of-thought).
      - ``temperature`` omitted (API rejects non-default values).
      - ``reasoning_effort`` set per tier: agent (is_agent=True) uses the
        agent constant; ASK/ROUTE (is_agent=False) uses the routing constant.
        Per-model-family API value handled by ``resolve_reasoning_effort``.
    For legacy chat models: use ``max_tokens`` + supplied ``temperature``.
    """
    if _is_reasoning_model(model):
        budget = max(max_tokens, min_reasoning_budget) if min_reasoning_budget else max_tokens
        api_kwargs["max_completion_tokens"] = budget
        from config.defaults import (
            DEFAULT_AGENT_REASONING_EFFORT,
            DEFAULT_ROUTING_REASONING_EFFORT,
            get_configured_reasoning_effort,
        )

        _setting_key = "agent_reasoning_effort" if is_agent else "routing_reasoning_effort"
        _default_effort = DEFAULT_AGENT_REASONING_EFFORT if is_agent else DEFAULT_ROUTING_REASONING_EFFORT
        api_kwargs["reasoning_effort"] = get_configured_reasoning_effort(_setting_key, _default_effort, model)
    else:
        api_kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            api_kwargs["temperature"] = temperature


def _drop_unsupported_chat_tool_reasoning(
    api_kwargs: dict[str, Any],
    model: str,
    *,
    has_tools: bool,
) -> None:
    """Strip chat-only params for third-party-compatible tool calls.

    Official OpenAI paths use Responses. This helper only supports the explicit
    non-Responses branch used by third-party OpenAI-compatible endpoints.
    """
    if not has_tools or not _is_reasoning_model(model):
        return
    if "reasoning_effort" in api_kwargs:
        api_kwargs.pop("reasoning_effort", None)
        logger.debug(
            "Dropping reasoning_effort for reasoning-model chat.completions " "tool call: model=%s",
            model,
        )


def _sanitize_tool_name(name: str) -> str:
    """Return a tool name that passes OpenAI's ``^[a-zA-Z0-9_-]{1,64}$`` check.

    Any character outside ``[a-zA-Z0-9_-]`` is replaced with ``_`` and the
    result is truncated to 64 characters.  The root cause of invalid names is
    the ``server_name.tool_name`` dot-separator used for namespaced external
    MCP servers; that has been fixed in ``mcp_hub/client_hub.py`` (now uses
    ``__``), but this function provides a defensive fallback for any future
    external tool names that slip through.
    """
    sanitized = _OPENAI_TOOL_NAME_INVALID.sub("_", name)
    return sanitized[:_OPENAI_TOOL_NAME_MAX_LEN]


def _register_openai_tool_name(
    seen_names: dict[str, str],
    *,
    raw_name: str,
    clean_name: str,
) -> None:
    """Fail before provider calls when OpenAI name sanitization collapses tools."""
    if not clean_name:
        raise ValueError("OpenAI tool name is empty after sanitization for %r" % raw_name)

    existing_raw = seen_names.get(clean_name)
    if existing_raw is not None:
        raise ValueError(
            "OpenAI tool name collision after sanitization: %r and %r both map to %r"
            % (existing_raw, raw_name, clean_name)
        )
    seen_names[clean_name] = raw_name


def _schema_allows_null(schema: Any) -> bool:
    """Return True when a JSON Schema explicitly allows ``null``."""
    if not isinstance(schema, dict):
        return False

    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True

    for composite_key in ("anyOf", "oneOf"):
        composite_value = schema.get(composite_key)
        if isinstance(composite_value, list) and any(_schema_allows_null(item) for item in composite_value):
            return True

    return False


def _strict_required_fields(properties: dict[str, Any], _existing_required: Any) -> list[str]:
    """Return the strict Responses ``required`` list in property order.

    OpenAI's current strict function-tool validation requires every key in
    ``properties`` to appear in ``required``. Optionality is expressed by
    allowing ``null`` in the property's schema, not by omitting the field from
    ``required``.
    """
    return [name for name in properties if isinstance(name, str)]


def _pattern_uses_lookaround(pattern: Any) -> bool:
    return isinstance(pattern, str) and any(token in pattern for token in _LOOKAROUND_PATTERN_TOKENS)


def _compact_description(value: Any, max_chars: int) -> Any:
    """Keep useful natural-language guidance while removing payload-only bloat."""
    if not isinstance(value, str):
        return value

    compacted = " ".join(value.split())
    if len(compacted) <= max_chars:
        return compacted

    cutoff = max_chars
    for marker in (". ", "; ", ", "):
        marker_index = compacted.rfind(marker, 0, max_chars)
        if marker_index >= max_chars // 2:
            cutoff = marker_index + 1
            break

    body = compacted[: max(0, min(cutoff, max_chars - 1))].rstrip(" ,;:.")
    return "%s." % body if body else ""


def _compact_responses_schema(schema: Any, *, _in_properties_dict: bool = False) -> Any:
    """Drop generated JSON-schema metadata and cap verbose field descriptions.

    The DROP_KEYS filter ({"title", "default"}) targets schema-level metadata
    only. When recursing inside a JSON Schema ``properties`` dict, the keys
    are user-defined property names (e.g. pydantic models with a ``title``
    field) and must NOT be filtered — otherwise the property is removed from
    properties while remaining in ``required``, producing OpenAI strict-mode
    400 'Extra required key supplied' errors.
    """
    if isinstance(schema, dict):
        compacted: dict[str, Any] = {}
        for key, value in schema.items():
            if not _in_properties_dict and key in _RESPONSES_SCHEMA_METADATA_DROP_KEYS:
                continue
            if not _in_properties_dict and key == "description":
                compacted[key] = _compact_description(value, _RESPONSES_SCHEMA_DESCRIPTION_MAX_CHARS)
                continue
            compacted[key] = _compact_responses_schema(
                value,
                _in_properties_dict=(key == "properties" and not _in_properties_dict),
            )
        return compacted
    if isinstance(schema, list):
        return [_compact_responses_schema(item, _in_properties_dict=False) for item in schema]
    return schema


def _normalize_openai_function_schema(
    schema: Any,
    *,
    strict: bool,
    tool_name: str | None = None,
    schema_path: str = "$",
    log_strict_warnings: bool = True,
) -> Any:
    """Recursively normalize JSON Schema for OpenAI function tools."""
    if isinstance(schema, dict):
        normalized = dict(schema)
        if strict:
            normalized.pop("format", None)
            if _pattern_uses_lookaround(normalized.get("pattern")):
                normalized.pop("pattern", None)
                if log_strict_warnings:
                    logger.warning(
                        "OpenAI strict tool schema stripped unsupported lookaround pattern for tool %r at %s",
                        tool_name or "<unknown>",
                        schema_path,
                    )
        for defs_key in ("$defs", "definitions"):
            defs_value = normalized.get(defs_key)
            if isinstance(defs_value, dict):
                normalized[defs_key] = {
                    key: _normalize_openai_function_schema(
                        value,
                        strict=strict,
                        tool_name=tool_name,
                        schema_path="%s.%s.%s" % (schema_path, defs_key, key),
                        log_strict_warnings=log_strict_warnings,
                    )
                    for key, value in defs_value.items()
                }

        properties = normalized.get("properties")
        if isinstance(properties, dict):
            normalized["properties"] = {
                key: _normalize_openai_function_schema(
                    value,
                    strict=strict,
                    tool_name=tool_name,
                    schema_path="%s.properties.%s" % (schema_path, key),
                    log_strict_warnings=log_strict_warnings,
                )
                for key, value in properties.items()
            }

        if "items" in normalized:
            normalized["items"] = _normalize_openai_function_schema(
                normalized["items"],
                strict=strict,
                tool_name=tool_name,
                schema_path="%s.items" % schema_path,
                log_strict_warnings=log_strict_warnings,
            )

        for composite_key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            composite_value = normalized.get(composite_key)
            if isinstance(composite_value, list):
                normalized[composite_key] = [
                    _normalize_openai_function_schema(
                        item,
                        strict=strict,
                        tool_name=tool_name,
                        schema_path="%s.%s[%d]" % (schema_path, composite_key, index),
                        log_strict_warnings=log_strict_warnings,
                    )
                    for index, item in enumerate(composite_value)
                ]

        is_object_schema = normalized.get("type") == "object" or isinstance(properties, dict)
        if is_object_schema:
            normalized.setdefault("type", "object")
            normalized.setdefault("properties", {})
            if strict:
                normalized["additionalProperties"] = False
                normalized["required"] = _strict_required_fields(
                    normalized["properties"],
                    normalized.get("required"),
                )

        return normalized

    if isinstance(schema, list):
        return [
            _normalize_openai_function_schema(
                item,
                strict=strict,
                tool_name=tool_name,
                schema_path="%s[%d]" % (schema_path, index),
                log_strict_warnings=log_strict_warnings,
            )
            for index, item in enumerate(schema)
        ]

    return schema


def _ensure_openai_tool_parameters(
    raw_parameters: Any,
    *,
    strict: bool,
    tool_name: str | None = None,
    log_strict_warnings: bool = True,
) -> dict[str, Any]:
    parameters = _normalize_openai_function_schema(
        dict(raw_parameters) if isinstance(raw_parameters, dict) else {},
        strict=strict,
        tool_name=tool_name,
        log_strict_warnings=log_strict_warnings,
    )
    if "type" not in parameters:
        parameters["type"] = "object"
    if "properties" not in parameters:
        parameters["properties"] = {}
    if strict and parameters.get("type") == "object":
        parameters["additionalProperties"] = False
        if isinstance(parameters.get("properties"), dict):
            parameters["required"] = _strict_required_fields(
                parameters["properties"],
                parameters.get("required"),
            )
    if not strict and isinstance(parameters.get("required"), list) and not parameters["required"]:
        parameters.pop("required", None)
    if "required" not in parameters and isinstance(parameters.get("properties"), dict) and parameters["properties"]:
        parameters["required"] = list(parameters["properties"].keys())
    # OpenAI strict-mode validation rejects schemas where `required` is a proper
    # subset of `properties`. Per-tool schemas (e.g. calendar) declare narrower
    # `required` for semantic clarity; promote to all properties when strict so
    # the LLM call is accepted. Non-mandatory fields can be passed empty/null.
    # Also strip required entries that aren't in properties — pydantic models
    # with reserved-name aliases (e.g. `title` → JSON Schema metadata) can
    # leak the alias name into required while excluding it from properties.
    if (
        strict
        and isinstance(parameters.get("properties"), dict)
        and parameters["properties"]
        and isinstance(parameters.get("required"), list)
    ):
        prop_keys = list(parameters["properties"].keys())
        prop_set = set(prop_keys)
        cur_required = parameters["required"]
        has_extras = any(k not in prop_set for k in cur_required)
        has_missing = any(k not in cur_required for k in prop_keys)
        if has_extras or has_missing:
            parameters["required"] = prop_keys
    return parameters


def _iter_schema_nodes(schema: Any) -> Any:
    if isinstance(schema, dict):
        yield schema

        properties = schema.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                yield from _iter_schema_nodes(child)

        for defs_key in (
            "$defs",
            "definitions",
            "dependentSchemas",
            "patternProperties",
        ):
            defs_value = schema.get(defs_key)
            if isinstance(defs_value, dict):
                for child in defs_value.values():
                    yield from _iter_schema_nodes(child)

        for child_key in ("items", "additionalProperties", "propertyNames"):
            child_value = schema.get(child_key)
            if isinstance(child_value, (dict, list)):
                yield from _iter_schema_nodes(child_value)

        for composite_key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            composite_value = schema.get(composite_key)
            if isinstance(composite_value, list):
                for child in composite_value:
                    yield from _iter_schema_nodes(child)

    elif isinstance(schema, list):
        for item in schema:
            yield from _iter_schema_nodes(item)


def _schema_is_object_like(schema: Any) -> bool:
    return isinstance(schema, dict) and (schema.get("type") == "object" or isinstance(schema.get("properties"), dict))


def _schema_is_bare_object(schema: Any) -> bool:
    if not _schema_is_object_like(schema):
        return False

    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        return False

    if schema.get("additionalProperties") is False:
        return False

    if schema.get("additionalProperties") not in (None, False):
        return False

    if schema.get("$ref"):
        return False

    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        value = schema.get(key)
        if isinstance(value, list) and value:
            return False

    for key in ("items", "patternProperties", "dependentSchemas", "propertyNames"):
        value = schema.get(key)
        if isinstance(value, dict) and value:
            return False
        if isinstance(value, list) and value:
            return False
        if key == "items" and value is not None:
            return False

    if "enum" in schema or "const" in schema:
        return False

    return True


def _schema_is_semantically_empty(schema: Any) -> bool:
    return _schema_is_bare_object(schema)


def _schema_has_freeform_object(schema: Any) -> bool:
    """Return True when strict mode would turn a constructable map into {}."""
    return bool(_collect_freeform_object_paths(schema))


def _collect_freeform_object_paths(schema: Any, *, schema_path: str = "$") -> list[str]:
    """Return JSON-schema paths where object maps cannot be strict Responses tools."""

    paths: list[str] = []
    if isinstance(schema, dict):
        if _schema_is_object_like(schema):
            additional_properties = schema.get("additionalProperties")
            if additional_properties not in (None, False):
                paths.append(schema_path)

        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, child in properties.items():
                paths.extend(_collect_freeform_object_paths(child, schema_path="%s.properties.%s" % (schema_path, key)))

        for defs_key in (
            "$defs",
            "definitions",
            "dependentSchemas",
            "patternProperties",
        ):
            defs_value = schema.get(defs_key)
            if isinstance(defs_value, dict):
                for key, child in defs_value.items():
                    paths.extend(
                        _collect_freeform_object_paths(child, schema_path="%s.%s.%s" % (schema_path, defs_key, key))
                    )

        for child_key in ("items", "additionalProperties", "propertyNames"):
            child_value = schema.get(child_key)
            if isinstance(child_value, dict):
                paths.extend(
                    _collect_freeform_object_paths(child_value, schema_path="%s.%s" % (schema_path, child_key))
                )
            elif isinstance(child_value, list):
                for index, child in enumerate(child_value):
                    paths.extend(
                        _collect_freeform_object_paths(
                            child,
                            schema_path="%s.%s[%d]" % (schema_path, child_key, index),
                        )
                    )

        for composite_key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            composite_value = schema.get(composite_key)
            if isinstance(composite_value, list):
                for index, child in enumerate(composite_value):
                    paths.extend(
                        _collect_freeform_object_paths(
                            child,
                            schema_path="%s.%s[%d]" % (schema_path, composite_key, index),
                        )
                    )

    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            paths.extend(_collect_freeform_object_paths(item, schema_path="%s[%d]" % (schema_path, index)))

    return paths


def _strict_responses_schema_issues(schema: Any, *, schema_path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(schema, dict):
        if _schema_is_object_like(schema):
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                issues.append("%s object schema must declare properties" % schema_path)
            if schema.get("additionalProperties") is not False:
                issues.append("%s object schema must set additionalProperties=false" % schema_path)
            if isinstance(properties, dict):
                required = schema.get("required")
                prop_keys = [name for name in properties if isinstance(name, str)]
                if not isinstance(required, list):
                    issues.append("%s object schema must declare required" % schema_path)
                else:
                    missing = [name for name in prop_keys if name not in required]
                    extra = [name for name in required if name not in prop_keys]
                    if missing:
                        issues.append("%s required missing properties: %s" % (schema_path, ", ".join(missing)))
                    if extra:
                        issues.append("%s required has unknown properties: %s" % (schema_path, ", ".join(extra)))

        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, child in properties.items():
                issues.extend(
                    _strict_responses_schema_issues(child, schema_path="%s.properties.%s" % (schema_path, key))
                )

        for defs_key in (
            "$defs",
            "definitions",
            "dependentSchemas",
            "patternProperties",
        ):
            defs_value = schema.get(defs_key)
            if isinstance(defs_value, dict):
                for key, child in defs_value.items():
                    issues.extend(
                        _strict_responses_schema_issues(child, schema_path="%s.%s.%s" % (schema_path, defs_key, key))
                    )

        for child_key in ("items", "additionalProperties", "propertyNames"):
            child_value = schema.get(child_key)
            if isinstance(child_value, dict):
                issues.extend(
                    _strict_responses_schema_issues(child_value, schema_path="%s.%s" % (schema_path, child_key))
                )
            elif isinstance(child_value, list):
                for index, child in enumerate(child_value):
                    issues.extend(
                        _strict_responses_schema_issues(
                            child,
                            schema_path="%s.%s[%d]" % (schema_path, child_key, index),
                        )
                    )

        for composite_key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            composite_value = schema.get(composite_key)
            if isinstance(composite_value, list):
                for index, child in enumerate(composite_value):
                    issues.extend(
                        _strict_responses_schema_issues(
                            child,
                            schema_path="%s.%s[%d]" % (schema_path, composite_key, index),
                        )
                    )

    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            issues.extend(_strict_responses_schema_issues(item, schema_path="%s[%d]" % (schema_path, index)))

    return issues


def _validate_responses_function_tool(tool: dict[str, Any]) -> None:
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return

    name = str(tool.get("name") or "<unknown>")
    issues: list[str] = []
    if tool.get("strict") is not True:
        issues.append("strict must be true")
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        issues.append("parameters must be an object schema")
    else:
        issues.extend(_strict_responses_schema_issues(parameters))
        if _schema_is_bare_object(parameters):
            issues.append("$ must not be a bare object schema")
    if issues:
        raise OpenAIResponsesToolSchemaError(name, issues)


def _validate_responses_function_tools(tools: list[dict[str, Any]]) -> None:
    for tool in tools:
        _validate_responses_function_tool(tool)


def _responses_tool_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    function = tool.get("function")
    if isinstance(function, dict):
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            return parameters
    parameters = tool.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _collect_responses_tool_flags(tools: list[dict[str, Any]]) -> dict[str, Any]:
    has_freeform_object = False
    has_bare_object = False
    max_required_count = 0

    for tool in tools:
        parameters = _responses_tool_parameters(tool)
        required = parameters.get("required")
        if isinstance(required, list):
            max_required_count = max(
                max_required_count,
                len([name for name in required if isinstance(name, str)]),
            )

        for schema in _iter_schema_nodes(parameters):
            if not _schema_is_object_like(schema):
                continue
            if schema.get("additionalProperties") is not False:
                has_freeform_object = True
            if _schema_is_bare_object(schema):
                has_bare_object = True

    return {
        "has_freeform_object": has_freeform_object,
        "has_bare_object": has_bare_object,
        "max_required_count_tool": max_required_count,
    }


def _prepare_responses_tool_parameters(
    raw_parameters: Any,
    *,
    strict_requested: bool,
    tool_name: str | None = None,
) -> tuple[dict[str, Any], bool]:
    freeform_paths = _collect_freeform_object_paths(raw_parameters)
    if freeform_paths:
        raise OpenAIResponsesToolSchemaError(
            tool_name or "<unknown>",
            ["%s uses freeform additionalProperties" % path for path in freeform_paths],
        )
    strict_parameters = _ensure_openai_tool_parameters(
        raw_parameters,
        strict=True,
        tool_name=tool_name,
        log_strict_warnings=strict_requested,
    )
    compacted = _compact_responses_schema(strict_parameters)
    return compacted, True


def _mcp_tools_to_openai(
    tools: list[dict[str, Any]],
    *,
    for_responses: bool = False,
    responses_api: bool = False,
) -> list[dict[str, Any]]:
    """Convert tool schemas to OpenAI tool format.

    Accepts both MCP format (``inputSchema``) and Anthropic format
    (``input_schema``).  Ensures every schema has ``type: object`` and
    conservative ``required`` defaults.

    Tool names are sanitized via :func:`_sanitize_tool_name` to satisfy
    OpenAI's ``^[a-zA-Z0-9_-]{1,64}$`` validation requirement.

    Returns:
        List of OpenAI-format tool definitions. Chat Completions expects the
        nested ``{"type": "function", "function": {...}}`` shape, while the
        Responses API expects top-level ``name``/``parameters`` fields.
    """
    responses_mode = for_responses or responses_api
    result: list[dict[str, Any]] = []
    seen_names: dict[str, str] = {}
    for tool in tools:
        raw_name = str(tool["name"])
        clean_name = _sanitize_tool_name(raw_name)
        _register_openai_tool_name(seen_names, raw_name=raw_name, clean_name=clean_name)
        if clean_name != raw_name:
            logger.warning(
                "Tool name sanitized for OpenAI: %r -> %r",
                raw_name,
                clean_name,
            )
        raw_parameters = tool.get("input_schema") or tool.get("inputSchema") or {}
        if responses_mode:
            parameters, strict = _prepare_responses_tool_parameters(
                raw_parameters,
                strict_requested=True,
                tool_name=clean_name,
            )
            result.append(
                {
                    "type": "function",
                    "name": clean_name,
                    "description": _compact_description(
                        tool.get("description", ""),
                        _RESPONSES_TOOL_DESCRIPTION_MAX_CHARS,
                    ),
                    "parameters": parameters,
                    "strict": strict,
                }
            )
        else:
            parameters = _ensure_openai_tool_parameters(raw_parameters, strict=False)
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": clean_name,
                        "description": tool.get("description", ""),
                        "parameters": parameters,
                    },
                }
            )
    if responses_mode:
        _validate_responses_function_tools(result)
    return result


def _merge_responses_include(api_kwargs: dict[str, Any], include_value: str) -> None:
    include = api_kwargs.get("include")
    if isinstance(include, list):
        if include_value not in include:
            include.append(include_value)
        return
    api_kwargs["include"] = [include_value]


def _coerce_tools_to_responses(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Lift OpenAI chat-style tools into Responses function-tool shape."""
    result: list[dict[str, Any]] = []
    seen_names: dict[str, str] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            result.append(tool)
            continue

        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name", "")
            description = function.get("description", "")
            raw_parameters = function.get("parameters") or {}
            strict_requested = bool(tool.get("strict", True))
        else:
            name = tool.get("name", "")
            description = tool.get("description", "")
            raw_parameters = tool.get("parameters") or {}
            strict_requested = bool(tool.get("strict", True))

        raw_name = str(name)
        clean_name = _sanitize_tool_name(raw_name)
        _register_openai_tool_name(seen_names, raw_name=raw_name, clean_name=clean_name)
        if clean_name != raw_name:
            logger.warning(
                "Tool name sanitized for OpenAI Responses: %r -> %r",
                raw_name,
                clean_name,
            )

        normalized_parameters, strict = _prepare_responses_tool_parameters(
            raw_parameters,
            strict_requested=strict_requested,
            tool_name=clean_name,
        )

        result.append(
            {
                "type": "function",
                "name": clean_name,
                "description": _compact_description(description, _RESPONSES_TOOL_DESCRIPTION_MAX_CHARS),
                "parameters": normalized_parameters,
                "strict": strict,
            }
        )
    _validate_responses_function_tools(result)
    return result


class OpenAICompatibleProvider(BaseLLMProvider):
    """
    LLM provider for OpenAI and OpenAI-compatible APIs.

    Official OpenAI endpoints use the Responses API. Third-party compatible
    endpoints that do not support Responses use their Chat Completions-compatible
    route directly; that branch is not a fallback after an OpenAI Responses error.
    """

    SUPPORTS_NATIVE_TOOLS = True
    NATIVE_TOOL_FORMAT = "mcp"

    def __init__(self, config: LLMConfig):
        """
        Initialize OpenAI-compatible provider.

        Args:
            config: LLM configuration with api_key, model, and optional base_url
        """
        super().__init__(config)
        # Native-agent state is set lazily by AIController._maybe_use_agent_prompt(),
        # but the compat provider may also be used directly via ProviderAgnosticRouter.
        # Keep the attributes present so native turns do not explode on first read.
        self._agent_system_prompt: str | None = None
        self._native_tools: list[dict[str, Any]] = []
        self._ask_tier_native: bool = False
        self._preferred_first_tool: str | None = None
        self._agent_tool_choice: Any = "auto"

        if not OPENAI_AVAILABLE:
            self.last_error = "OpenAI package not installed. Run: pip install openai"
            self._client = None
            return

        # Determine base URL
        base_url = config.base_url
        if not base_url:
            # Use default OpenAI endpoint
            base_url = OPENAI_DEFAULT_BASE_URL

        # Validate API key
        if not config.api_key:
            self.last_error = "API key is required"
            self._client = None
            return

        # Validate API key format for OpenAI
        if base_url == OPENAI_DEFAULT_BASE_URL and not config.api_key.startswith("sk-"):
            logger.warning("OpenAI API key format looks invalid (should start with 'sk-')")

        # Create async client
        try:
            assert _openai is not None
            from diagnostics import latency_spans

            _instrumented_http_client = latency_spans.build_instrumented_openai_http_client()
            _client_kwargs: dict[str, Any] = {
                "api_key": config.api_key,
                "base_url": base_url,
            }
            if _instrumented_http_client is not None:
                _client_kwargs["http_client"] = _instrumented_http_client
            self._client = _openai.AsyncOpenAI(**_client_kwargs)
            self._base_url = base_url
            # OpenAI-compatible only means API shape compatibility. Generic,
            # third-party, and local endpoints stay on the text JSON contract
            # unless a provider-specific adapter proves native support.
            self.SUPPORTS_NATIVE_TOOLS = _supports_responses_api(config) or config.native_tools_verified is True
            logger.info(
                "OpenAI-compatible provider initialized: model=%s, endpoint=%s",
                config.model,
                self._get_endpoint_name(),
            )
        except Exception as e:
            self.last_error = f"Failed to initialize OpenAI client: {e}"
            self._client = None
            logger.error(self.last_error)

    def _find_stored_responses_anchor(self, messages: list[dict[str, Any]]) -> tuple[str | None, int | None]:
        """Return the latest stored assistant response that can anchor continuity."""
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, dict) or not content.get("_openai_assistant"):
                continue
            response_id = content.get("response_id")
            if isinstance(response_id, str) and response_id and content.get("store") is True:
                return response_id, index
        return None, None

    def _build_responses_request_fingerprint(
        self,
        *,
        context: str,
        payload: dict[str, Any],
        first_turn: bool | None,
    ) -> dict[str, Any]:
        tools = payload.get("tools")
        tools_list = tools if isinstance(tools, list) else []
        tool_flags = _collect_responses_tool_flags(tools_list)
        request_hash = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
        return {
            "context": context,
            "commit_sha": _get_commit_sha(),
            "runtime_sha": _get_runtime_sha(),
            "request_hash": request_hash,
            "model": payload.get("model"),
            "first_turn": first_turn,
            "tool_count": len(tools_list),
            "tools_json_bytes": _string_or_json_bytes(tools_list),
            "instructions_bytes": _string_or_json_bytes(payload.get("instructions")),
            "input_bytes": _string_or_json_bytes(payload.get("input")),
            "max_required_count_tool": tool_flags["max_required_count_tool"],
            "has_freeform_object": tool_flags["has_freeform_object"],
            "has_bare_object": tool_flags["has_bare_object"],
            "store": payload.get("store"),
            "has_previous_response_id": bool(payload.get("previous_response_id")),
        }

    def _log_responses_preflight(
        self,
        *,
        context: str,
        payload: dict[str, Any],
        first_turn: bool | None,
    ) -> dict[str, Any]:
        fingerprint = self._build_responses_request_fingerprint(
            context=context,
            payload=payload,
            first_turn=first_turn,
        )
        logger.info(
            "OpenAI Responses request fingerprint: %s",
            json.dumps(fingerprint, sort_keys=True, default=str),
        )
        oversized_fields: list[str] = []
        if fingerprint["tool_count"] > _RESPONSES_TOOL_COUNT_WARN_THRESHOLD:
            oversized_fields.append("tool_count=%s" % fingerprint["tool_count"])
        if fingerprint["tools_json_bytes"] > _RESPONSES_TOOL_JSON_BYTES_WARN_THRESHOLD:
            oversized_fields.append("tools_json_bytes=%s" % fingerprint["tools_json_bytes"])
        if fingerprint["instructions_bytes"] > _RESPONSES_INSTRUCTIONS_BYTES_WARN_THRESHOLD:
            oversized_fields.append("instructions_bytes=%s" % fingerprint["instructions_bytes"])
        if oversized_fields:
            logger.warning(
                "OpenAI Responses request fingerprint exceeds payload budget: %s",
                ", ".join(oversized_fields),
            )
        return fingerprint

    def _prepare_responses_native_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        first_turn: bool,
        store_enabled: bool,
    ) -> dict[str, Any]:
        """Build Responses input items for stored and stateless native turns."""
        previous_response_id: str | None = None
        turn_messages = messages
        replay_response_items = not store_enabled

        if store_enabled and not first_turn:
            previous_response_id, anchor_index = self._find_stored_responses_anchor(messages)
            if previous_response_id and anchor_index is not None:
                turn_messages = messages[anchor_index + 1 :]
                replay_response_items = False

        return {
            "input": self._convert_messages_to_responses_input(
                turn_messages,
                replay_response_items=replay_response_items,
            ),
            "previous_response_id": previous_response_id,
            "replay_response_items": replay_response_items,
        }

    async def compact_responses_continuity(
        self,
        *,
        continuity: dict[str, Any] | None,
        model_override: str | None = None,
    ) -> dict[str, Any] | None:
        """Compact a stateless response-items prefix with ``responses.compact``."""
        state = _normalize_response_continuity(continuity)
        if state.get("mode") != RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS:
            return None

        response_items = state.get("response_items")
        if not isinstance(response_items, list) or not response_items:
            return None
        normalized_items = [
            replay_item
            for replay_item in (_response_output_item_to_input_item(item) for item in response_items)
            if replay_item is not None
        ]
        if not normalized_items:
            return None

        if not _supports_responses_api(self.config):
            return None

        responses_api = getattr(self._client, "responses", None)
        compact_fn = getattr(responses_api, "compact", None)
        if not callable(compact_fn):
            return None

        effective_model = model_override or getattr(self, "effective_model", None) or self.config.model
        compacted = await compact_fn(
            model=effective_model,
            input=normalized_items,
        )
        compacted_state = _normalize_response_continuity(
            _extract_response_continuity_metadata(
                compacted,
                store=False,
                previous_response_id=None,
                preserve_compacted_window=True,
            )
        )
        if not compacted_state:
            return None
        compacted_state["mode"] = RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS
        compacted_state["continuity_mode"] = RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS
        compacted_state["compacted_window"] = True
        return compacted_state

    def _log_responses_failure(
        self,
        *,
        context: str,
        payload: dict[str, Any],
        exc: BaseException,
        fingerprint: dict[str, Any] | None = None,
    ) -> None:
        """Emit a shape-preserving, text-redacted payload log for failed Responses calls."""
        failure = self._responses_failure_metadata(exc, fingerprint=fingerprint)
        logger.warning(
            "OpenAI Responses request failed: context=%s status=%s request_id=%s error_type=%s fingerprint=%s payload=%s error_body=%s",
            context,
            failure.get("status_code"),
            failure.get("request_id"),
            failure.get("error_type"),
            json.dumps(fingerprint or {}, sort_keys=True, default=str),
            json.dumps(_redact_responses_payload(payload), sort_keys=True, default=str),
            json.dumps(failure.get("error_body"), sort_keys=True, default=str),
        )
        # Stamp the operator-overload signal so cloud_intent.dispatch's
        # right-most-mile guard can re-route a "Something went wrong" response
        # through the provider_overloaded UX path. The ASK path (where this
        # method is called from) doesn't otherwise route through
        # classify_llm_operator_error — only the route_command catch at
        # line ~2141 does, and that's a different agent path. Calling classify
        # here is idempotent: it mutates the request-scoped provider-overload
        # signal object that dispatch.py reads later in the same request.
        try:
            classify_llm_operator_error(exc)
        except Exception:
            # Diagnostic side-channel must never break the actual error path.
            pass

    def _responses_failure_metadata(
        self,
        exc: BaseException,
        *,
        fingerprint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return trace-safe details for a failed Responses provider call."""
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        request_id = getattr(exc, "request_id", None)
        if not request_id and headers is not None:
            request_id = headers.get("x-request-id") or headers.get("request-id")
        error_body = getattr(exc, "body", None)
        return {
            "error_type": type(exc).__name__,
            "status_code": _responses_error_status_code(exc),
            "request_id": request_id,
            "message": str(exc)[:1000],
            "fingerprint": copy.deepcopy(fingerprint or {}),
            "error_body": (_redact_responses_payload(error_body) if error_body is not None else None),
        }

    def _get_endpoint_name(self) -> str:
        """Get human-readable endpoint name."""
        if not hasattr(self, "_base_url"):
            return "unknown"

        # Check known endpoints
        for name, url in KNOWN_ENDPOINTS.items():
            if self._base_url.startswith(url.rstrip("/")):
                return name

        # Return domain name for custom endpoints
        try:
            from urllib.parse import urlparse

            parsed = urlparse(self._base_url)
            return parsed.netloc or "custom"
        except Exception:
            logger.debug("Failed to parse base URL", exc_info=True)
            return "custom"

    def is_available(self) -> bool:
        """Check if provider is available."""
        return OPENAI_AVAILABLE and self._client is not None and bool(self.config.api_key)

    def get_available_models(self) -> list[str]:
        """Get list of available models."""
        # Return common models based on endpoint
        endpoint = self._get_endpoint_name()

        models_by_endpoint = {
            "openai": [
                "gpt-4o",
                "gpt-4o-mini",
                "gpt-4-turbo",
                "gpt-3.5-turbo",
                "o1",
                "o1-mini",
            ],
            "groq": [
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "mixtral-8x7b-32768",
            ],
            "together": [
                "Qwen/Qwen2.5-7B-Instruct-Turbo",
                "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                "openai/gpt-oss-20b",
            ],
            "fireworks": [
                "accounts/fireworks/models/gpt-oss-120b",
                "accounts/fireworks/models/kimi-k2p5",
                "accounts/fireworks/models/deepseek-v4-pro",
            ],
            "mistral": [
                "mistral-large-latest",
                "mistral-medium-latest",
                "mistral-small-latest",
            ],
            "perplexity": [
                "llama-3.1-sonar-large-128k-online",
                "llama-3.1-sonar-small-128k-online",
            ],
            "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"],
            "openrouter": [
                "~google/gemini-flash-latest",
                "google/gemini-3.1-flash-lite",
                "~openai/gpt-mini-latest",
            ],
            "xai": ["grok-4.3", "grok-4.3-fast", "grok-3-mini"],
            "cohere": [
                "command-a-03-2025",
                "command-r-08-2024",
                "command-r-plus-08-2024",
            ],
        }

        return models_by_endpoint.get(endpoint, [self.config.model])

    async def _probe_context_window(self) -> int | None:
        """Best-effort detection of the served model's context window.

        OpenAI-compatible endpoints are heterogeneous: vLLM reports
        ``max_model_len`` on each ``/v1/models`` entry, while llama.cpp (and
        text-generation-webui's llama.cpp loader) expose ``n_ctx`` at the
        non-versioned ``/props`` endpoint. Returns ``None`` when the window
        cannot be determined — callers must treat that as "unknown", never
        as "insufficient".
        """
        base = str(getattr(self.config, "base_url", "") or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            return None
        root = base[:-3].rstrip("/") if base.endswith("/v1") else base
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                with suppress(Exception):
                    resp = await client.get("%s/models" % base)
                    if resp.status_code == 200:
                        for entry in resp.json().get("data") or []:
                            mml = entry.get("max_model_len") if isinstance(entry, dict) else None
                            if isinstance(mml, int) and mml > 0:
                                return mml
                with suppress(Exception):
                    resp = await client.get("%s/props" % root)
                    if resp.status_code == 200:
                        props = resp.json()
                        dgs = props.get("default_generation_settings")
                        if isinstance(dgs, dict) and isinstance(dgs.get("n_ctx"), int) and dgs["n_ctx"] > 0:
                            return dgs["n_ctx"]
                        if isinstance(props.get("n_ctx"), int) and props["n_ctx"] > 0:
                            return props["n_ctx"]
                with suppress(Exception):
                    # LM Studio native REST API: loaded_context_length is the
                    # context the model is actually loaded with (max_context_length
                    # is only the model's ceiling, not what is in effect).
                    resp = await client.get("%s/api/v0/models" % root)
                    if resp.status_code == 200:
                        for entry in resp.json().get("data") or []:
                            if not isinstance(entry, dict):
                                continue
                            loaded = entry.get("loaded_context_length")
                            if isinstance(loaded, int) and loaded > 0:
                                return loaded
        except Exception:
            return None
        return None

    async def test_connection(self) -> LLMTestResult:
        """Test the connection to the API."""
        if not self.is_available():
            return LLMTestResult(
                success=False,
                message=self.last_error or "Provider not available",
                error_code="not_available",
            )

        try:
            start_time = time.time()

            if _supports_responses_api(self.config):
                test_kwargs: dict[str, Any] = {
                    "model": self.config.model,
                    "input": [{"role": "user", "content": "Hi"}],
                    # Official OpenAI Responses API rejects values below 16.
                    "max_output_tokens": 16,
                }
                response = await _responses_create_with_storage_consent(self._client, self.config, test_kwargs)
            else:
                # Non-OpenAI compatible endpoints may only implement the chat
                # route. This is a direct provider path, not an OpenAI fallback.
                test_kwargs = {
                    "model": self.config.model,
                    "messages": [{"role": "user", "content": "Hi"}],
                }
                # OpenRouter can proxy OpenAI/Azure-compatible backends that
                # reject tiny output budgets; keep the probe cheap but at the
                # same minimum accepted by official OpenAI Responses.
                _apply_token_budget(test_kwargs, self.config.model, 16)
                response = await _chat_create_resilient(self._client, self.config, test_kwargs)

            latency_ms = round((time.time() - start_time) * 1000)

            model_info: dict[str, Any] = {
                "model": self.config.model,
                "endpoint": self._get_endpoint_name(),
                "response_id": response.id if hasattr(response, "id") else None,
            }
            message = f"Connected to {self._get_endpoint_name()} successfully"

            # Capability check: a local model loaded with too small a context
            # window fails Viola's agent commands with an opaque 400. Surface it
            # here, at validation time, where the user can act on it.
            context_window = await self._probe_context_window()
            if context_window:
                model_info["context_window"] = context_window
                if context_window < _AGENT_MIN_CONTEXT_TOKENS:
                    model_info["context_window_sufficient"] = False
                    message += (
                        f" — warning: this model's context window ({context_window} tokens) is below the "
                        f"~{_AGENT_MIN_CONTEXT_TOKENS} tokens Viola's assistant needs. Reload the model with a "
                        f"larger context (8192+ recommended) or commands may fail."
                    )
                else:
                    model_info["context_window_sufficient"] = True

            return LLMTestResult(
                success=True,
                message=message,
                latency_ms=latency_ms,
                model_info=model_info,
            )

        except Exception as e:
            # Handle OpenAI-specific exceptions if available
            if _openai is not None:
                if isinstance(e, _openai.AuthenticationError):
                    return LLMTestResult(
                        success=False,
                        message="Invalid API key. Please check your API key and try again.",
                        error_code="auth_error",
                    )
                if isinstance(e, _openai.RateLimitError):
                    return LLMTestResult(
                        success=False,
                        message="Rate limit exceeded. Please wait and try again.",
                        error_code="rate_limit",
                    )
                if isinstance(e, _openai.APIConnectionError):
                    return LLMTestResult(
                        success=False,
                        message="Could not connect to API. Check your internet connection and endpoint URL.",
                        error_code="connection_error",
                    )
                if isinstance(e, _openai.NotFoundError):
                    return LLMTestResult(
                        success=False,
                        message=f"Model '{self.config.model}' not found. Please select a valid model.",
                        error_code="model_not_found",
                    )
            logger.warning("OpenAI-compatible connection test failed: %s", e)
            return LLMTestResult(
                success=False,
                message=f"Connection test failed: {e!s}",
                error_code="unknown_error",
            )

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
            include_history: Whether to include conversation history
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature

        Returns:
            Dict with 'content', 'tokens_used', 'model', 'error' keys
        """
        if not self.is_available():
            return {
                "content": "AI provider not available. Please configure your API key.",
                "error": self.last_error or "not_available",
                "tokens_used": 0,
                "model": self.config.model,
            }

        # F-009: Enforce max_tokens cap to prevent uncapped cost exposure.
        # Coerce settings values defensively; test mocks must never crash live routing.
        max_tokens = clamp_max_tokens(max_tokens)

        # Build messages
        messages = []

        # Add system prompt
        effective_system_prompt = system_prompt
        if not effective_system_prompt:
            from services.llm.prompts import build_unified_system_prompt

            effective_system_prompt = build_unified_system_prompt()

        # Legacy o1 quirk: o1 originally rejected the ``system`` role, so we
        # fold the system prompt into the first user message.  Newer reasoning
        # models (o3/o4/gpt-5) accept the system role normally — only o1 needs
        # this workaround.  Token-budget swap (max_completion_tokens vs
        # max_tokens) is handled separately via ``_is_reasoning_model``.
        is_o1_legacy = self.config.model.lower().startswith("o1")

        if effective_system_prompt and not is_o1_legacy:
            messages.append({"role": "system", "content": effective_system_prompt})

        # Add current question
        if is_o1_legacy and effective_system_prompt:
            # For legacy o1 models, prepend system prompt to user message
            question = f"{effective_system_prompt}\n\n{question}"

        messages.append({"role": "user", "content": question})

        try:
            if _supports_responses_api(self.config):
                responses_kwargs: dict[str, Any] = {
                    "model": self.config.model,
                    "input": self._convert_messages_to_responses_input(messages),
                    "max_output_tokens": max_tokens,
                }
                if effective_system_prompt:
                    responses_kwargs["instructions"] = effective_system_prompt
                if _is_reasoning_model(self.config.model):
                    responses_kwargs["reasoning"] = {
                        "effort": get_configured_reasoning_effort("routing", self.config.model),
                        "summary": "auto",
                    }
                responses_kwargs.setdefault("store", True)
                _enforce_storage_consent_for_config(self.config, responses_kwargs)
                if not responses_kwargs.get("store") and _is_reasoning_model(self.config.model):
                    # Encrypted-content replay is a reasoning-model-only feature.
                    # Non-reasoning models (gpt-4o-mini, etc.) reject the
                    # `include: reasoning.encrypted_content` param with a 400.
                    _merge_responses_include(responses_kwargs, "reasoning.encrypted_content")
                # Pin consecutive ASK turns of one conversation to the same
                # cache partition so the shared instructions prefix keeps hitting.
                ask_prompt_cache_key = self._resolve_prompt_cache_key()
                if ask_prompt_cache_key:
                    responses_kwargs["prompt_cache_key"] = ask_prompt_cache_key
                fingerprint = self._log_responses_preflight(
                    context="ask",
                    payload=responses_kwargs,
                    first_turn=True,
                )

                start_time = time.time()
                try:
                    response = await _responses_create_with_storage_consent(
                        self._client,
                        self.config,
                        responses_kwargs,
                    )
                except Exception as exc:
                    self._log_responses_failure(
                        context="ask",
                        payload=responses_kwargs,
                        exc=exc,
                        fingerprint=fingerprint,
                    )
                    raise
                else:
                    self.last_latency_ms = round((time.time() - start_time) * 1000)
                    usage = getattr(response, "usage", None)
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    output_tokens = getattr(usage, "output_tokens", 0) or 0
                    tokens_used = getattr(usage, "total_tokens", 0) or (input_tokens + output_tokens)
                    content = _extract_message_text_from_response(response).strip()
                    no_result_payload: dict[str, Any] | None = None
                    if not content:
                        no_result_payload = build_ai_no_result("empty_assistant_content")
                    continuity = _extract_response_continuity_metadata(
                        response,
                        store=bool(responses_kwargs.get("store")),
                    )

                    self._emit_diagnostics(
                        "ask.success",
                        latency_ms=self.last_latency_ms,
                        tokens_used=tokens_used,
                    )

                    result_payload = {
                        "content": content,
                        "tokens_used": tokens_used,
                        "model": self.config.model,
                        "error": None,
                        "response_id": continuity["response_id"],
                        "_continuity": continuity,
                    }
                    if no_result_payload is not None:
                        result_payload["no_result"] = no_result_payload["no_result"]
                        result_payload["error_state"] = no_result_payload["error_state"]
                    return result_payload

            start_time = time.time()

            # Build API parameters
            api_kwargs: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
            }

            # Reasoning models reject ``max_tokens`` and non-default
            # ``temperature``; legacy chat models accept both.
            _apply_token_budget(
                api_kwargs,
                self.config.model,
                max_tokens,
                temperature=temperature,
                is_agent=False,
            )

            response = await _chat_create_resilient(self._client, self.config, api_kwargs)

            self.last_latency_ms = round((time.time() - start_time) * 1000)

            content = response.choices[0].message.content or ""
            tokens_used = response.usage.total_tokens if response.usage else 0

            # Provider-level history removed — AIController owns per-user history.

            self._emit_diagnostics(
                "ask.success",
                latency_ms=self.last_latency_ms,
                tokens_used=tokens_used,
            )

            return {
                "content": content.strip(),
                "tokens_used": tokens_used,
                "model": self.config.model,
                "error": None,
            }

        except Exception as e:
            if _supports_responses_api(self.config) and _responses_error_should_surface(e):
                raise
            error_msg = "API error: %s" % e
            logger.exception("LLM ask failed")
            self.last_error = error_msg

            self._emit_diagnostics(
                "ask.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )

            # Use centralized error messages instead of hardcoded refusals
            from core.error_messages import get_error_for_exception

            _code, user_msg = get_error_for_exception(e)
            from diagnostics.error_classification import categorize_exception

            category = categorize_exception(e)
            if category.name.startswith("EXPECTED_"):
                user_msg = "Temporary issue reaching the AI service. Please try again in a moment."
            else:
                user_msg = "I ran into an issue with the AI service (%s). %s" % (
                    type(e).__name__,
                    user_msg,
                )

            return {
                "content": user_msg,
                "error": error_msg,
                "error_category": category.name,
                "tokens_used": 0,
                "model": self.config.model,
            }

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        system_context: str | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """
        Route user input to a tool call or answer.

        Args:
            text: User's request text
            history: Deprecated compatibility input ignored by this provider.
            system_context: Deprecated compatibility context text.
            context_bundle: Optional prompt-frame bundle.
            max_tokens: Maximum tokens in response
            model_override: Optional model name to use instead of config.model

        Returns:
            Dict with 'type' ('tool_call' or 'answer'), 'tool', 'args', 'answer'
        """
        self._warn_ignored_history_arg(history, "route_command")
        # R6F kept route_command for non-agent tool_call/answer routing only.
        # Native agent turns must fail closed here and use route_command_native().
        self._reject_route_command_agent_state()
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "AI provider not available. Please configure your API key.",
            }

        # F-009: Enforce max_tokens cap for command routing.
        # The 150-token default cap truncates tool_call JSON mid-generation,
        # causing the LLM to fall back to text answers instead of calling tools.
        native_tools: list[dict[str, Any]] | None = None
        max_tokens = clamp_max_tokens(max_tokens, default=300)
        if context_bundle is None and system_context:
            context_bundle = runtime_context_bundle(system_context, origin="legacy_system_context")
        # Non-agent route mode: use native route tools only for endpoints
        # that have explicitly proven compatible with OpenAI tool_choice.
        _use_route_tools = False
        _route_tools_openai: list[dict[str, Any]] | None = None
        if bool(getattr(self, "SUPPORTS_NATIVE_TOOLS", False)):
            try:
                from services.llm.route_tool_schemas import (
                    get_openai_route_tools,
                )

                _route_tools_openai = get_openai_route_tools()
                _use_route_tools = True
            except ImportError:
                pass

        if _use_route_tools and _route_tools_openai:
            native_tools = _route_tools_openai
            route_response_contract = ""
        else:
            route_response_contract = _structured_route_response_contract()

        rendered_prompt = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                user_text=text,
                native_tools=bool(native_tools),
                response_contract=route_response_contract,
            )
        )
        system_prompt = str(rendered_prompt.get("instructions") or "")

        messages = _rendered_openai_to_chat_messages(rendered_prompt)

        try:
            start_time = time.time()

            # Use model_override if provided, else fall back to config.model
            effective_model = model_override or self.config.model

            if _supports_responses_api(self.config):
                responses_kwargs: dict[str, Any] = {
                    "model": effective_model,
                    "input": self._convert_messages_to_responses_input(messages),
                    "max_output_tokens": max_tokens,
                }
                if system_prompt:
                    responses_kwargs["instructions"] = system_prompt
                if native_tools:
                    if isinstance(native_tools[0], dict) and native_tools[0].get("type") == "function":
                        responses_kwargs["tools"] = _coerce_tools_to_responses(native_tools)
                    else:
                        responses_kwargs["tools"] = _mcp_tools_to_openai(native_tools, responses_api=True)
                    responses_kwargs["tool_choice"] = "required"
                if _is_reasoning_model(effective_model):
                    responses_kwargs["reasoning"] = {
                        "effort": get_configured_reasoning_effort(
                            "routing",
                            effective_model,
                        ),
                        "summary": "auto",
                    }
                responses_kwargs.setdefault("store", True)
                _enforce_storage_consent_for_config(self.config, responses_kwargs)
                if not responses_kwargs.get("store") and _is_reasoning_model(effective_model):
                    # Encrypted-content replay is a reasoning-model-only feature.
                    # Non-reasoning models reject this include param with a 400.
                    _merge_responses_include(responses_kwargs, "reasoning.encrypted_content")
                # Pin the turns of one conversation to the same cache partition
                # so the shared instructions+tools prefix keeps hitting the cache.
                route_prompt_cache_key = self._resolve_prompt_cache_key()
                if route_prompt_cache_key:
                    responses_kwargs["prompt_cache_key"] = route_prompt_cache_key
                fingerprint = self._log_responses_preflight(
                    context="route_command",
                    payload=responses_kwargs,
                    first_turn=not bool(history),
                )

                try:
                    response = await _responses_create_with_storage_consent(
                        self._client,
                        self.config,
                        responses_kwargs,
                    )
                except Exception as exc:
                    self._log_responses_failure(
                        context="route_command",
                        payload=responses_kwargs,
                        exc=exc,
                        fingerprint=fingerprint,
                    )
                    raise
                else:
                    self.last_latency_ms = round((time.time() - start_time) * 1000)
                    continuity = _extract_response_continuity_metadata(
                        response,
                        store=bool(responses_kwargs.get("store")),
                    )

                    # Extract token usage so downstream settle_spend in
                    # ``intent/ai_controller.py:process_request`` can record
                    # cost against the user's plan ledger. Pre-2026-05-02 the
                    # Responses-API route_command branch attached only
                    # ``response_id``/``_continuity`` to its return dict and
                    # silently DROPPED ``_usage`` — so the cloud's Pro
                    # $6/month managed-LLM cap never decremented because
                    # ``response.get("_usage")`` was always None at the
                    # ``settle_spend`` gate. The OpenAI-compatible chat path
                    # already attached ``_usage`` (line ~2332) and the agents
                    # provider's text-response builder did too (line ~894);
                    # only this Responses-API branch missed it.
                    _route_usage = getattr(response, "usage", None)
                    _route_input = int(getattr(_route_usage, "input_tokens", 0) or 0)
                    _route_output = int(getattr(_route_usage, "output_tokens", 0) or 0)
                    _route_cached = _cached_tokens_from_usage(_route_usage)
                    _route_cache_write = _cache_write_tokens_from_usage(_route_usage)
                    _route_web_search = _web_search_requests_from_usage(_route_usage)
                    _usage_payload: dict[str, int] = {
                        "input_tokens": _route_input,
                        "output_tokens": _route_output,
                        "cache_read_tokens": _route_cached,
                        "cache_creation_tokens": _route_cache_write,
                        "cache_write_tokens": _route_cache_write,
                        "web_search_requests": _route_web_search,
                    }
                    _model_name_for_settle = getattr(self.config, "model", "") or ""

                    from services.llm.model_fallback import get_fallback_tracker

                    get_fallback_tracker().record_success()

                    if native_tools:
                        parsed_native = self._parse_responses_native_response(
                            response,
                            request_metadata=continuity,
                        )
                        if parsed_native.get("type") == "tool_call":
                            try:
                                from services.llm.route_tool_schemas import (
                                    convert_route_tool_response,
                                )

                                routed = convert_route_tool_response(
                                    parsed_native["tool"],
                                    parsed_native.get("args", {}),
                                )
                                routed["response_id"] = continuity["response_id"]
                                routed["_continuity"] = continuity
                                routed["_usage"] = _usage_payload
                                routed["_model_name"] = _model_name_for_settle
                                return routed
                            except Exception:
                                logger.warning("Route tool response conversion failed, falling back to native parse")
                        parsed_native["response_id"] = continuity["response_id"]
                        parsed_native["_continuity"] = continuity
                        parsed_native["_usage"] = _usage_payload
                        parsed_native["_model_name"] = _model_name_for_settle
                        return parsed_native

                    content = _extract_message_text_from_response(response).strip()
                    if not content:
                        retry_kwargs = _with_responses_no_result_retry_context(responses_kwargs)
                        retry_response = await _responses_create_with_storage_consent(
                            self._client,
                            self.config,
                            retry_kwargs,
                        )
                        retry_continuity = _extract_response_continuity_metadata(
                            retry_response,
                            store=bool(retry_kwargs.get("store")),
                        )
                        retry_content = _extract_message_text_from_response(retry_response).strip()
                        if retry_content:
                            response = retry_response
                            continuity = retry_continuity
                            content = retry_content
                        else:
                            result = build_ai_no_result("empty_assistant_content", retry_attempted=True)
                            result["response_id"] = retry_continuity["response_id"]
                            result["_continuity"] = retry_continuity
                            return result

                    content = _strip_llm_code_fences(content)
                    try:
                        try:
                            parsed = json.loads(content)
                        except json.JSONDecodeError:
                            content = content.replace("{{", "{").replace("}}", "}")
                            parsed = json.loads(content)

                        if not isinstance(parsed, dict):
                            parsed = {
                                "type": "answer",
                                "answer": content,
                            }

                        parsed = _normalize_structured_route_payload(parsed, content)

                        self._emit_diagnostics(
                            "route.success",
                            latency_ms=self.last_latency_ms,
                            response_type=parsed.get("type"),
                        )
                        parsed["response_id"] = continuity["response_id"]
                        parsed["_continuity"] = continuity
                        parsed["_usage"] = _usage_payload
                        parsed["_model_name"] = _model_name_for_settle
                        return parsed
                    except json.JSONDecodeError:
                        return {
                            "type": "answer",
                            "answer": content.strip(),
                            "response_id": continuity["response_id"],
                            "_continuity": continuity,
                            "_usage": _usage_payload,
                            "_model_name": _model_name_for_settle,
                        }

            api_kwargs: dict[str, Any] = {
                "model": effective_model,
                "messages": messages,
            }

            # Reasoning models (o1/o3/o4/gpt-5 families) use
            # max_completion_tokens (not max_tokens), reject temperature, and
            # need a higher budget because internal chain-of-thought consumes
            # tokens before visible output.  Legacy chat models get lowered
            # temperature for more consistent routing.
            _apply_token_budget(
                api_kwargs,
                effective_model,
                max_tokens,
                temperature=0.3,
                min_reasoning_budget=4096,
                is_agent=False,
            )

            if not _is_reasoning_model(effective_model):
                # json_object mode conflicts with function calling.
                # Route tools use function calling, so JSON mode only applies
                # to the structured text fallback.
                if not native_tools:
                    api_kwargs["response_format"] = {"type": "json_object"}

            if native_tools:
                # Route tools are already in OpenAI format (list of
                # {"type":"function","function":{...}}) when set by the
                # route-mode code path above.  Other formats need conversion.
                if native_tools and isinstance(native_tools[0], dict) and native_tools[0].get("type") == "function":
                    api_kwargs["tools"] = native_tools
                else:
                    api_kwargs["tools"] = _mcp_tools_to_openai(native_tools)
                # Force at least one tool call for the initial routing request.
                # Route mode must return tool_call/answer/ignore through the
                # route tool schema instead of falling back to free text.
                api_kwargs["tool_choice"] = "required"
                _drop_unsupported_chat_tool_reasoning(
                    api_kwargs,
                    effective_model,
                    has_tools=True,
                )

            response = await _chat_create_resilient(self._client, self.config, api_kwargs)

            self.last_latency_ms = round((time.time() - start_time) * 1000)

            # Record success for model fallback tracker
            from services.llm.model_fallback import get_fallback_tracker

            get_fallback_tracker().record_success()

            # Native tool path: parse structured response
            if native_tools:
                parsed_native = self._parse_native_response(response)
                # Route-mode tool calls are normalized into the same
                # tool_call/answer/ignore shape consumed by AIController.
                if parsed_native.get("type") == "tool_call":
                    try:
                        from services.llm.route_tool_schemas import (
                            convert_route_tool_response,
                        )

                        return convert_route_tool_response(
                            parsed_native["tool"],
                            parsed_native.get("args", {}),
                        )
                    except Exception:
                        logger.warning("Route tool response conversion failed, falling back to native parse")
                return parsed_native

            content = response.choices[0].message.content or ""

            if not content:
                retry_kwargs = dict(api_kwargs)
                retry_kwargs["messages"] = _with_no_result_retry_context(messages)
                retry_response = await _chat_create_resilient(self._client, self.config, retry_kwargs)
                retry_content = retry_response.choices[0].message.content or ""
                if retry_content:
                    response = retry_response
                    content = retry_content
                else:
                    return build_ai_no_result("empty_assistant_content", retry_attempted=True)

            # Strip markdown code fences before JSON parsing
            content = _strip_llm_code_fences(content)

            # Parse JSON response
            try:
                # Try as-is first; fallback sanitizes double braces (nano artifact)
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    content = content.replace("{{", "{").replace("}}", "}")
                    parsed = json.loads(content)

                parsed = _normalize_structured_route_payload(parsed, content)

                self._emit_diagnostics(
                    "route.success",
                    latency_ms=self.last_latency_ms,
                    response_type=parsed.get("type"),
                )

                return parsed

            except json.JSONDecodeError:
                # Fallback: treat raw response as answer
                return {"type": "answer", "answer": content.strip()}

        except Exception as e:
            if _supports_responses_api(self.config) and _responses_error_should_surface(e):
                raise
            logger.exception("Command routing failed")
            # Record failure for model fallback tracker
            from services.llm.model_fallback import get_fallback_tracker

            _tracker = get_fallback_tracker()
            _tracker.record_failure(e)
            self.last_error = str(e)

            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )

            # If Haiku fallback is active, re-raise so provider_router
            # can route through the Anthropic fallback provider
            if _tracker.is_fallback_active:
                raise

            # Use error classification for context-aware messages
            from diagnostics.error_classification import categorize_exception

            category = categorize_exception(e)
            operator_diagnostic = classify_llm_operator_error(e)
            return build_ai_error_no_result(
                "route_command",
                category_name=category.name,
                exception=e,
                operator_diagnostic=operator_diagnostic,
            )

    # ------------------------------------------------------------------
    # Native tool calling (OpenAI function calling API)
    # ------------------------------------------------------------------

    def _build_chat_native_kwargs(
        self,
        *,
        messages: list[dict[str, Any]],
        system_prompt: str,
        model: str,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
    ) -> dict[str, Any]:
        """Build the Chat Completions payload for one native agent turn.

        The first call and every in-turn retry go through here so a retry can
        never drift from the shape of the call it is retrying (token budget,
        tool schemas, tool_choice, reasoning-param stripping).
        """

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._convert_messages_to_openai(messages, system_prompt),
        }
        if max_tokens is not None:
            _apply_token_budget(
                api_kwargs,
                model,
                max_tokens,
                temperature=0.3,
            )
        if tools:
            api_kwargs["tools"] = _mcp_tools_to_openai(tools)
            if tool_choice is not None:
                api_kwargs["tool_choice"] = tool_choice
            _drop_unsupported_chat_tool_reasoning(
                api_kwargs,
                model,
                has_tools=True,
            )
        return api_kwargs

    async def route_command_native(
        self,
        messages: list[dict[str, Any]],
        first_turn: bool = False,
        tool_choice_override: Any | None = None,
        native_tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = 1024,
        model_override: str | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute one turn of the agent loop with native tool calling.

        Unlike ``route_command()``, this accepts full message histories
        (with structured tool_call / tool_result content) and always uses
        native tool calling.

        The *messages* may be in the Anthropic-ish format produced by the
        agent executor (``tool_result`` content blocks inside user messages).
        This method converts them to either:
        - Responses API input items for official OpenAI endpoints
        - Chat Completions messages for third-party compatible endpoints

        Args:
            messages: Message history from the agent executor.
            first_turn: Whether this is the first loop turn.
            tool_choice_override: Optional forced tool choice.
            native_tools: Optional tool schema override from the executor.
            max_tokens: Maximum tokens in response. None omits the output cap.
            model_override: Optional model name override.

        Returns:
            Standardised dict (same shape as ``route_command()``).
        """
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "OpenAI provider not available.",
            }

        effective_model = model_override or self.config.model

        tools = list(native_tools) if native_tools is not None else list(self._native_tools or [])
        prompt_context_bundle = kwargs.get("prompt_context_bundle")
        if prompt_context_bundle is not None:
            effective_system_prompt = render_openai_responses_instructions(prompt_context_bundle)
        else:
            effective_system_prompt = system_prompt if system_prompt is not None else self._agent_system_prompt or ""
        forwarded_continuity = kwargs.get("responses_continuity") or kwargs.get("continuity") or {}
        messages_are_delta = bool(kwargs.get("messages_are_delta"))

        try:
            start_time = time.time()

            # F-029 (R3-A): native OpenAI-compatible turns now share the
            # Claude-parity ``execute_with_policy`` state machine
            # (``services/llm/request_policy.py``). That matches Anthropic's
            # native turn (see ``anthropic_provider.py:1072-1086``) and
            # buys foreground/background 529 semantics, context-overflow
            # token recalibration, key-rotation hook plumbing, abort-signal
            # checks before every attempt, and persistent-retry handling.
            from services.llm.key_pool import get_key_pool
            from services.llm.request_policy import (
                ProviderAttemptContext,
                ProviderRequestContext,
                ProviderRetryDecision,
                execute_with_policy,
                new_request_id,
            )

            uses_codex_subscription = _uses_codex_subscription_transport(self.config)
            _oai_pool = None if uses_codex_subscription else get_key_pool(self.config.provider)
            use_responses_api = _supports_responses_api(self.config)
            tool_choice = self._resolve_native_tool_choice(tool_choice_override, first_turn, bool(tools))
            responses_request_metadata: dict[str, Any] | None = None
            responses_request_fingerprint: dict[str, Any] | None = None
            responses_retry_events: list[dict[str, Any]] = []
            force_non_streaming_responses = False

            async def _do_third_party_chat_native_call() -> Any:
                api_kwargs = self._build_chat_native_kwargs(
                    messages=messages,
                    system_prompt=effective_system_prompt,
                    model=effective_model,
                    max_tokens=max_tokens,
                    tools=tools,
                    tool_choice=tool_choice,
                )
                return await _chat_create_resilient(self._client, self.config, api_kwargs)

            async def _do_oai_compat_call() -> Any:
                nonlocal force_non_streaming_responses, responses_request_fingerprint, responses_request_metadata
                if use_responses_api:
                    api_kwargs: dict[str, Any] = {
                        "model": effective_model,
                    }
                    if max_tokens is not None:
                        api_kwargs["max_output_tokens"] = max_tokens
                    if effective_system_prompt:
                        api_kwargs["instructions"] = effective_system_prompt
                    if tools:
                        if isinstance(tools[0], dict) and tools[0].get("type") == "function":
                            api_kwargs["tools"] = _coerce_tools_to_responses(tools)
                        else:
                            api_kwargs["tools"] = _mcp_tools_to_openai(tools, responses_api=True)
                        if tool_choice is not None:
                            api_kwargs["tool_choice"] = tool_choice
                    if _is_reasoning_model(effective_model):
                        # R14-06: agent_loop.py:1969-1974 + agent_executor.py:8230-8238/11496-11501
                        # forward reasoning_tier="background_agent" for child/nested agents,
                        # and config/defaults.py:276-290 maps that tier to background_agent_reasoning_effort
                        # (default "high"). Previously this provider hardcoded "agent" tier, so
                        # background agents got root-agent effort. Consume the forwarded tier instead.
                        reasoning_tier = str(kwargs.get("reasoning_tier") or "agent")
                        api_kwargs["reasoning"] = {
                            "effort": get_configured_reasoning_effort(reasoning_tier, effective_model),
                            "summary": "auto",
                        }
                    api_kwargs.setdefault("store", True)
                    _enforce_storage_consent_for_config(self.config, api_kwargs)
                    store_enabled = bool(api_kwargs.get("store"))
                    continuity_mode = str((forwarded_continuity or {}).get("mode") or "").strip().lower()
                    if messages_are_delta:
                        delta_messages = messages
                        if continuity_mode in {
                            RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID,
                            RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS,
                        }:
                            delta_messages = [
                                message for message in messages if not _is_openai_assistant_delta_message(message)
                            ]
                        delta_input: list[dict[str, Any]] = []
                        if continuity_mode == RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS:
                            forwarded_items = (forwarded_continuity or {}).get("response_items")
                            if isinstance(forwarded_items, list):
                                for item in forwarded_items:
                                    replay_item = _response_output_item_to_input_item(item)
                                    if replay_item is not None:
                                        delta_input.append(replay_item)
                        delta_input.extend(
                            self._convert_messages_to_responses_input(
                                delta_messages,
                                replay_response_items=False,
                            )
                        )
                        prepared_turn = {
                            "input": delta_input,
                            "previous_response_id": (
                                (forwarded_continuity or {}).get("previous_response_id")
                                if continuity_mode == RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID
                                else None
                            ),
                            "replay_response_items": continuity_mode == RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS,
                        }
                    else:
                        prepared_turn = self._prepare_responses_native_turn(
                            messages,
                            first_turn=first_turn,
                            store_enabled=store_enabled,
                        )
                    # D.1/R13-WIRE: in previous_response_id mode the paired
                    # function_call lives in the server-stored prior response.
                    # In response_items delta mode it can live in the caller's
                    # forwarded continuity window. Filtering those deltas strips
                    # valid function_call_output items and silently mutates state.
                    _filter_input = prepared_turn["input"]
                    if not (
                        prepared_turn.get("previous_response_id")
                        or (messages_are_delta and continuity_mode == RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS)
                    ):
                        _filter_input = _filter_orphan_responses_tool_pairs(_filter_input)
                    api_kwargs["input"] = _filter_input
                    previous_response_id = prepared_turn.get("previous_response_id")
                    if previous_response_id:
                        api_kwargs["previous_response_id"] = previous_response_id
                    if not store_enabled and _is_reasoning_model(api_kwargs.get("model", self.config.model)):
                        # Encrypted-content replay is reasoning-model only;
                        # gpt-4o-mini etc. 400 on this include param.
                        _merge_responses_include(api_kwargs, "reasoning.encrypted_content")
                    # Pin every turn of this conversation to one cache partition
                    # so the stable instructions+tools+early-history prefix keeps
                    # hitting the prompt cache across the run. Without this the
                    # stateless (store=False) replay path re-pays the full prefix
                    # on late turns once routing scatters them off the cached
                    # machine (trace fa6c24ee7d31: cache_read 0 at step 29/30).
                    prompt_cache_key = self._resolve_prompt_cache_key(kwargs)
                    if prompt_cache_key:
                        api_kwargs["prompt_cache_key"] = prompt_cache_key
                    responses_request_metadata = {
                        "store": store_enabled,
                        "previous_response_id": previous_response_id,
                        "continuity_mode": continuity_mode or None,
                        "messages_are_delta": messages_are_delta,
                        "input_items": copy.deepcopy(api_kwargs["input"]),
                    }
                    responses_request_fingerprint = self._log_responses_preflight(
                        context="route_command_native",
                        payload=api_kwargs,
                        first_turn=first_turn,
                    )
                    _enforce_storage_consent_for_config(self.config, api_kwargs)
                    responses_provider_payload = copy.deepcopy(api_kwargs)
                    responses_request_metadata["provider_payload"] = responses_provider_payload
                    command_stream_id = get_current_command_stream_id()
                    stream_capture = (
                        StreamChunkAggregator(
                            attempt_id="command_stream:%s:%d" % (command_stream_id, int(time.time() * 1000)),
                            task_trace=getattr(self, "_task_trace", None),
                        )
                        if command_stream_id and not force_non_streaming_responses
                        else None
                    )

                    async def _retry_non_streaming_after_stream_error(
                        exc: _ResponsesStreamFallbackError,
                    ) -> Any:
                        nonlocal force_non_streaming_responses
                        force_non_streaming_responses = True
                        responses_retry_events.append(
                            {
                                "attempt": 1,
                                "error_type": exc.retry_error_type,
                                "status_code": _responses_error_status_code(exc),
                                "action": "fallback_non_streaming",
                                "delay_seconds": 0.0,
                                "request_id": getattr(exc, "request_id", None),
                            }
                        )
                        fallback_kwargs = copy.deepcopy(api_kwargs)
                        fallback_kwargs.pop("stream", None)
                        responses_request_metadata["provider_payload"] = copy.deepcopy(fallback_kwargs)
                        responses_request_metadata["stream_fallback_from"] = exc.retry_error_type
                        return await _responses_create_with_storage_consent(
                            self._client,
                            self.config,
                            fallback_kwargs,
                        )

                    try:
                        if stream_capture is None:
                            return await _responses_create_with_storage_consent(
                                self._client,
                                self.config,
                                api_kwargs,
                            )

                        completed_response: Any | None = None
                        # The codex/chatgpt.com streaming path delivers the
                        # final response as a shell on `response.completed`
                        # — its `output` array is empty even though all the
                        # text was sent via separate `output_item.done`
                        # events earlier in the stream. We capture each
                        # output_item.done item ourselves and attach them
                        # to completed_response.output below if empty so
                        # the agent loop's text extractor (which reads
                        # message.output_text content blocks) can find
                        # the assistant text.
                        captured_output_items: list[Any] = []
                        stream_kwargs = copy.deepcopy(api_kwargs)
                        stream_kwargs["stream"] = True
                        stream = await _responses_create_with_storage_consent(
                            self._client,
                            self.config,
                            stream_kwargs,
                        )
                        async for event in stream:
                            capture_openai_responses_stream_event(stream_capture, event)
                            event_type = str(_stream_event_field(event, "type", "") or "")
                            if event_type == "response.output_item.done":
                                _item = _stream_event_field(event, "item")
                                if _item is not None:
                                    captured_output_items.append(_item)
                                continue
                            if event_type == "response.completed":
                                completed_response = _stream_event_field(event, "response")
                                continue
                            if event_type == "response.incomplete":
                                # S1-002: ``incomplete`` is recoverable - the
                                # post-stream path classifies the reason via
                                # ``_responses_incomplete_reason`` and the agent
                                # loop's max_output_tokens recovery resumes the
                                # turn.  Treat the event as terminal but do not
                                # raise: keep the response so its
                                # ``incomplete_details.reason`` is observable.
                                completed_response = _stream_event_field(event, "response")
                                continue
                            if event_type in {"response.failed", "error"}:
                                raise _ResponsesStreamFallbackError(event_type)
                        usage = getattr(completed_response, "usage", None) if completed_response is not None else None
                        stream_capture.emit_terminal("usage", summarize_usage(usage))
                        stream_capture.emit_terminal("stop", summarize_openai_response(completed_response))
                        if completed_response is None:
                            raise _ResponsesStreamFallbackError("stream_completed_without_final_response")
                        if captured_output_items and not (getattr(completed_response, "output", None) or []):
                            try:
                                completed_response.output = captured_output_items
                            except Exception as _attach_exc:
                                logger.warning(
                                    "Could not attach captured output items to completed_response: %s",
                                    _attach_exc,
                                )
                        return completed_response
                    except _ResponsesStreamFallbackError as exc:
                        if stream_capture is not None:
                            stream_capture.emit_terminal("error", summarize_stream_error(exc))
                        return await _retry_non_streaming_after_stream_error(exc)
                    except Exception as exc:
                        if stream_capture is not None:
                            stream_capture.emit_terminal("error", summarize_stream_error(exc))
                        failure = self._responses_failure_metadata(
                            exc,
                            fingerprint=responses_request_fingerprint,
                        )
                        exc.viola_provider_payload = responses_provider_payload
                        exc.viola_provider_failure = failure
                        self._log_responses_failure(
                            context="route_command_native",
                            payload=api_kwargs,
                            exc=exc,
                            fingerprint=responses_request_fingerprint,
                        )
                        raise

                return await _do_third_party_chat_native_call()

            def _on_oai_policy_retry(
                attempt_ctx: ProviderAttemptContext,
                exc: BaseException,
                decision: ProviderRetryDecision,
            ) -> None:
                responses_retry_events.append(
                    {
                        "attempt": attempt_ctx.attempt,
                        "error_type": type(exc).__name__,
                        "status_code": _responses_error_status_code(exc),
                        "action": decision.action,
                        "delay_seconds": decision.delay_s,
                        "request_id": getattr(exc, "request_id", None),
                    }
                )

            async def _refresh_oai_client_on_auth(
                prev_error: BaseException | None,
            ) -> None:
                del prev_error

                # F-029: rotate API key on auth/stale-connection retries
                # (Claude parity via ``refresh_client`` hook,
                # ``withRetry.ts:170-251``) off the shared policy.
                if uses_codex_subscription or _oai_pool is None or not self.config.api_key:
                    return
                _oai_pool.report_failure(self.config.api_key, is_billing=False)
                next_key = _oai_pool.get_key()
                if not next_key:
                    return
                from diagnostics import latency_spans as _latency_spans

                _rotated_http_client = _latency_spans.build_instrumented_openai_http_client()
                _rotated_kwargs: dict[str, Any] = {
                    "api_key": next_key,
                    "base_url": getattr(self, "_base_url", None) or self.config.base_url or OPENAI_DEFAULT_BASE_URL,
                }
                if _rotated_http_client is not None:
                    _rotated_kwargs["http_client"] = _rotated_http_client
                self._client = _openai.AsyncOpenAI(**_rotated_kwargs)
                self.config.api_key = next_key

            _abort_signal = kwargs.get("abort_signal") or kwargs.get("cancel_event")
            # F-029: We keep the legacy attempt budget (max_retries=2) for
            # OpenAI-compatible because the third-party endpoints we target
            # have lower retry tolerance than Anthropic; the parity win we
            # actually need is on the *policy* (abort signal, foreground
            # 529 semantics, refresh_client, context-overflow recovery),
            # not the attempt count. Callers who want Claude's 10-attempt
            # foreground budget can override via the policy context.
            _oai_result_env = await execute_with_policy(
                _do_oai_compat_call,
                ProviderRequestContext(
                    provider=self.config.provider or "openai_compatible",
                    model=effective_model,
                    session_id=None,
                    request_id=new_request_id("openai_compat_native_agent_turn"),
                    stream=False,
                    timeout_s=60.0,
                    source="agent_loop",
                    max_retries=2,
                    base_delay_s=1.0,
                    max_delay_s=4.0,
                    abort_signal=_abort_signal,
                    on_retry=_on_oai_policy_retry,
                    refresh_client=None if uses_codex_subscription else _refresh_oai_client_on_auth,
                    fallback_model=None if uses_codex_subscription else _policy_fallback_model(effective_model),
                    raise_fallback_triggered=True,
                ),
            )
            # ``value_or_raise`` raises the underlying provider error
            # (with viola_provider_* annotations attached) on failure, or
            # returns the value on success.
            try:
                response = _oai_result_env.value_or_raise()
            except BaseException as _policy_exc:
                _policy_exc.viola_provider_attempts = {
                    "total": _oai_result_env.metadata.get("attempts", 0),
                    "retry_events": copy.deepcopy(responses_retry_events),
                    "policy_status": _oai_result_env.status,
                }
                raise
            if _oai_pool and self.config.api_key:
                _oai_pool.report_success(self.config.api_key)

            self.last_latency_ms = round((time.time() - start_time) * 1000)

            # Metrics instrumentation
            try:
                from admin.instrumentation import record_llm_call

                _u = getattr(response, "usage", None)
                if _u:
                    input_tokens = getattr(_u, "prompt_tokens", None) or getattr(_u, "input_tokens", 0) or 0
                    output_tokens = getattr(_u, "completion_tokens", None) or getattr(_u, "output_tokens", 0) or 0
                    record_llm_call(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=_cached_tokens_from_usage(_u),
                        cache_write_tokens=_cache_write_tokens_from_usage(_u),
                        web_search_requests=_web_search_requests_from_usage(_u),
                        model=effective_model,
                        latency_ms=self.last_latency_ms,
                        request_type="agent_task",
                    )
            except Exception:
                logger.debug("Telemetry record_llm_call failed, continuing")

            # Record success for model fallback tracker
            from services.llm.model_fallback import get_fallback_tracker

            get_fallback_tracker().record_success()

            parsed = (
                self._parse_responses_native_response(
                    response,
                    request_metadata=responses_request_metadata,
                )
                if use_responses_api
                else self._parse_native_response(response)
            )
            if _native_empty_response_retryable(parsed):
                if use_responses_api:
                    provider_payload = (
                        responses_request_metadata.get("provider_payload")
                        if isinstance(responses_request_metadata, dict)
                        else None
                    )
                    if isinstance(provider_payload, dict):
                        retry_kwargs = _with_responses_no_result_retry_context(provider_payload)
                        retry_response = await _responses_create_with_storage_consent(
                            self._client,
                            self.config,
                            retry_kwargs,
                        )
                        parsed = self._parse_responses_native_response(
                            retry_response,
                            request_metadata={
                                "store": bool(retry_kwargs.get("store")),
                                "previous_response_id": retry_kwargs.get("previous_response_id"),
                                "continuity_mode": (
                                    responses_request_metadata.get("continuity_mode")
                                    if isinstance(responses_request_metadata, dict)
                                    else None
                                ),
                                "messages_are_delta": (
                                    responses_request_metadata.get("messages_are_delta")
                                    if isinstance(responses_request_metadata, dict)
                                    else False
                                ),
                                "input_items": copy.deepcopy(retry_kwargs.get("input") or []),
                                "provider_payload": copy.deepcopy(retry_kwargs),
                            },
                        )
                    _mark_native_empty_response_retry_attempted(parsed)
                else:
                    retry_api_kwargs = self._build_chat_native_kwargs(
                        messages=_with_no_result_retry_context(messages),
                        system_prompt=effective_system_prompt,
                        model=effective_model,
                        max_tokens=max_tokens,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                    retry_response = await _chat_create_resilient(
                        self._client,
                        self.config,
                        retry_api_kwargs,
                    )
                    parsed = self._parse_native_response(retry_response)
                    _mark_native_empty_response_retry_attempted(parsed)
            empty_args_retry = build_empty_tool_arguments_retry(parsed, tools)
            if empty_args_retry:
                # A tool call whose arguments were empty or unusable gets one
                # re-prompt on WHICHEVER API surface produced it — Responses
                # for official OpenAI, Chat Completions for the third-party
                # endpoints BYOK users point at. Detection without recovery on
                # one of the two surfaces just means those users watch the
                # failure instead of the fix.
                empty_args_retry_parsed: dict[str, Any] | None = None
                unretryable_reason = ""
                if use_responses_api:
                    provider_payload = (
                        responses_request_metadata.get("provider_payload")
                        if isinstance(responses_request_metadata, dict)
                        else None
                    )
                    if isinstance(provider_payload, dict):
                        logger.warning(
                            "Retrying bad tool arguments on responses for %s with required args %s",
                            empty_args_retry.tool_name,
                            ",".join(empty_args_retry.required_args),
                        )
                        retry_kwargs = append_responses_retry_input(provider_payload, empty_args_retry.message)
                        # Bad-tool-args is a single extra call, not a retry-policy lane.
                        empty_args_retry_response = await _responses_create_with_storage_consent(
                            self._client,
                            self.config,
                            retry_kwargs,
                        )
                        empty_args_retry_parsed = self._parse_responses_native_response(
                            empty_args_retry_response,
                            request_metadata={
                                "store": bool(retry_kwargs.get("store")),
                                "previous_response_id": retry_kwargs.get("previous_response_id"),
                                "continuity_mode": (
                                    responses_request_metadata.get("continuity_mode")
                                    if isinstance(responses_request_metadata, dict)
                                    else None
                                ),
                                "messages_are_delta": (
                                    responses_request_metadata.get("messages_are_delta")
                                    if isinstance(responses_request_metadata, dict)
                                    else False
                                ),
                                "input_items": copy.deepcopy(retry_kwargs.get("input") or []),
                                "provider_payload": copy.deepcopy(retry_kwargs),
                            },
                        )
                    else:
                        unretryable_reason = "missing_responses_provider_payload"
                else:
                    logger.warning(
                        "Retrying bad tool arguments on chat completions for %s with required args %s",
                        empty_args_retry.tool_name,
                        ",".join(empty_args_retry.required_args),
                    )
                    empty_args_retry_kwargs = self._build_chat_native_kwargs(
                        messages=append_chat_retry_message(messages, empty_args_retry.message),
                        system_prompt=effective_system_prompt,
                        model=effective_model,
                        max_tokens=max_tokens,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                    empty_args_retry_response = await _chat_create_resilient(
                        self._client,
                        self.config,
                        empty_args_retry_kwargs,
                    )
                    empty_args_retry_parsed = self._parse_native_response(empty_args_retry_response)
                if empty_args_retry_parsed is None:
                    # Nothing to replay. Say so loudly and record it on the
                    # turn so the trace shows a detected-but-unrecovered call
                    # rather than a tool that mysteriously ran with no args.
                    logger.warning(
                        "Bad tool arguments for %s were not retried (%s)",
                        empty_args_retry.tool_name,
                        unretryable_reason or "no_retry_path",
                    )
                    parsed["_empty_arguments_retry"] = {
                        "attempted": False,
                        "tool": empty_args_retry.tool_name,
                        "required_args": list(empty_args_retry.required_args),
                        "reason": unretryable_reason or "no_retry_path",
                    }
                else:
                    parsed = empty_args_retry_parsed
                    parsed["_empty_arguments_retry"] = {
                        "attempted": True,
                        "tool": empty_args_retry.tool_name,
                        "required_args": list(empty_args_retry.required_args),
                    }
            if use_responses_api:
                parsed["_provider_attempts"] = {
                    "total": _oai_result_env.metadata.get("attempts", 0),
                    "retry_events": copy.deepcopy(responses_retry_events),
                    "policy_status": _oai_result_env.status,
                }
            parsed["_model_name"] = effective_model
            return parsed

        except Exception as e:
            if use_responses_api and _responses_error_should_surface(e):
                raise
            logger.exception("OpenAI native agent turn failed: %s", e)
            # Record failure for model fallback tracker
            from services.llm.model_fallback import get_fallback_tracker

            get_fallback_tracker().record_failure(e)
            self.last_error = str(e)
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )
            # Re-raise so agent executor can distinguish LLM failure
            # from a legitimate final answer.
            raise

    def _parse_native_response(self, response: Any) -> dict[str, Any]:
        """Parse an OpenAI response that may contain ``tool_calls``.

        Returns a dict matching the contract expected by the agent executor:
        - ``{"type": "tool_call", "tool": ..., "args": ..., ...}``
        - ``{"type": "answer", "answer": ...}``
        """
        choice = response.choices[0]
        message = _usage_field(choice, "message")
        finish_reason = str(_usage_field(choice, "finish_reason") or "").strip().lower()
        message_content = _usage_field(message, "content") or ""
        message_tool_calls = _usage_field(message, "tool_calls") or []

        # Build usage dict
        usage_dict: dict[str, int] = {}
        tokens_used = 0
        if response.usage:
            prompt_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(response.usage, "completion_tokens", 0) or 0
            tokens_used = prompt_tokens + completion_tokens
            usage_dict = {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "cache_read_tokens": _cached_tokens_from_usage(response.usage),
                "cache_creation_tokens": _cache_write_tokens_from_usage(response.usage),
                "cache_write_tokens": _cache_write_tokens_from_usage(response.usage),
                "web_search_requests": _web_search_requests_from_usage(response.usage),
            }
            logger.info(
                "LLM usage: input=%d output=%d total=%d",
                prompt_tokens,
                completion_tokens,
                tokens_used,
            )

        if finish_reason == "length" and not message_tool_calls:
            result = build_ai_no_result("max_output_tokens", retryable=True)
            result.update(
                {
                    "_usage": usage_dict,
                    "_raw_content": {
                        "_openai_assistant": True,
                        "role": "assistant",
                        "content": message_content,
                        "finish_reason": finish_reason,
                    },
                    "_tokens_used": tokens_used,
                    "_recoverable_provider_error": "max_output_tokens",
                }
            )
            return result

        # If the model returned tool calls, parse them
        if message_tool_calls:
            # Build raw_content as an OpenAI message dict that the agent
            # executor can store and we can later reconstruct.
            raw_content: dict[str, Any] = {
                "_openai_assistant": True,
                "role": "assistant",
                "content": message_content,
                "tool_calls": [],
            }

            all_tool_calls: list[dict[str, Any]] = []
            invalid_tool_call_reasons: list[str] = []
            iterated_tool_call_count = 0

            def _tool_call_field(value: Any, key: str, default: Any = None) -> Any:
                if isinstance(value, dict):
                    return value.get(key, default)
                return getattr(value, key, default)

            try:
                tool_call_iterable = iter(message_tool_calls)
            except TypeError:
                tool_call_iterable = iter(())
                invalid_tool_call_reasons.append("tool_calls_not_iterable")

            for index, tc in enumerate(tool_call_iterable):
                iterated_tool_call_count += 1
                function = _tool_call_field(tc, "function")
                tool_name = str(_tool_call_field(function, "name", "") or "").strip()
                tool_use_id = str(_tool_call_field(tc, "id", "") or "").strip()
                raw_arguments = _tool_call_field(function, "arguments", "") if function is not None else ""
                coerced_arguments = coerce_tool_call_arguments(raw_arguments)
                # Replay the model's own JSON text when it sent text; when it
                # sent an already-parsed object (third-party servers do),
                # re-serialize what we actually dispatch so the assistant turn
                # we hand back to the provider carries the same arguments.
                raw_arguments_text = (
                    raw_arguments
                    if isinstance(raw_arguments, str)
                    else json.dumps(coerced_arguments.args, separators=(",", ":"))
                )

                raw_content["tool_calls"].append(
                    {
                        "id": tool_use_id,
                        "type": str(_tool_call_field(tc, "type", "function") or "function"),
                        "function": {
                            "name": tool_name,
                            "arguments": raw_arguments_text,
                        },
                    }
                )

                call_reasons: list[str] = []
                if function is None:
                    call_reasons.append("missing_function")
                if not tool_name:
                    call_reasons.append("missing_function_name")
                if not tool_use_id:
                    call_reasons.append("missing_tool_call_id")
                if call_reasons:
                    invalid_tool_call_reasons.extend("call_%d_%s" % (index, reason) for reason in call_reasons)
                    continue

                parsed_call = {
                    "tool": tool_name,
                    "args": coerced_arguments.args,
                    "tool_use_id": tool_use_id,
                }
                # An argument payload that is empty or unusable must never be
                # dispatched as a silent no-arg call: flagging it is what lets
                # ``build_empty_tool_arguments_retry`` re-prompt the model, and
                # what makes the defect visible in the trace either way.
                if coerced_arguments.was_empty:
                    parsed_call["_arguments_were_empty"] = True
                elif coerced_arguments.was_malformed:
                    parsed_call["_arguments_were_malformed"] = True
                all_tool_calls.append(parsed_call)

            if not all_tool_calls:
                content = str(message_content or "").strip()
                if not invalid_tool_call_reasons and iterated_tool_call_count == 0:
                    invalid_tool_call_reasons.append("truthy_empty_tool_calls_iterable")
                logger.warning(
                    "OpenAI native response declared tool_calls but none were usable; "
                    "recovering_as=%s content_len=%d iterated_tool_calls=%d reasons=%s",
                    "answer" if content else "ai_no_result",
                    len(content),
                    iterated_tool_call_count,
                    ",".join(invalid_tool_call_reasons),
                )
                if content:
                    result = self._build_answer_response(content, usage_dict)
                    result.update(
                        {
                            "_raw_content": raw_content,
                            "_tokens_used": tokens_used,
                            "_recovered_from_invalid_tool_calls": True,
                            "_invalid_tool_call_reasons": list(invalid_tool_call_reasons),
                        }
                    )
                    return result
                result = build_ai_no_result("invalid_tool_calls_empty_content")
                result.update(
                    {
                        "_usage": usage_dict,
                        "_raw_content": raw_content,
                        "_tokens_used": tokens_used,
                        "_recovered_from_invalid_tool_calls": True,
                        "_invalid_tool_call_reasons": list(invalid_tool_call_reasons),
                    }
                )
                return result

            first = all_tool_calls[0]
            logger.debug(
                "OpenAI native response: %d tool_calls, first=%s, tokens=%d",
                len(all_tool_calls),
                first["tool"],
                tokens_used,
            )

            try:
                self._emit_diagnostics(
                    "route.native_tool_call",
                    latency_ms=getattr(self, "last_latency_ms", 0),
                    tool_name=first["tool"],
                    tool_count=len(all_tool_calls),
                    tokens_used=tokens_used,
                )
            except Exception:
                logger.debug("Diagnostics emission failed for native tool call", exc_info=True)

            return {
                "type": "tool_call",
                "tool": first["tool"],
                "args": first["args"],
                "tool_use_id": first["tool_use_id"],
                "_raw_content": raw_content,
                "_all_tool_calls": all_tool_calls,
                "_usage": usage_dict,
            }

        # Text-only response: try JSON parse, fallback to plain answer
        content = str(message_content or "").strip()
        logger.debug(
            "OpenAI native response: text-only (len=%d), tokens=%d",
            len(content),
            tokens_used,
        )

        if not content:
            result = build_ai_no_result("empty_assistant_content")
            result["_usage"] = usage_dict
            return result

        # Try to parse as JSON (model may return structured answer/tool_call)
        stripped = _strip_llm_code_fences(content)
        try:
            # Try as-is first; fallback sanitizes double braces (nano artifact)
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                stripped = stripped.replace("{{", "{").replace("}}", "}")
                parsed = json.loads(stripped)
            # Guard: gpt-5-family reasoning models sometimes return a bare
            # JSON scalar (e.g. the literal 15 for "6+9") instead of the
            # full ``{"type":"answer","answer":"15"}`` envelope. ``.get``
            # crashes on non-dicts. Fall through to the plain-text path.
            if isinstance(parsed, dict):
                resp_type = parsed.get("type", "")
                if resp_type in ("answer", "command", "tool_call", "ignore"):
                    if resp_type == "answer" and "continue_listening" in parsed:
                        parsed["continue_listening"] = bool(parsed["continue_listening"])
                    parsed["_usage"] = usage_dict
                    return parsed
        except json.JSONDecodeError:
            pass

        # Native-tools mode: LLM returned plain text instead of JSON.
        # Plain prose cannot mutate conversation-control state.
        return self._build_answer_response(content, usage_dict)

    def _parse_responses_native_response(
        self,
        response: Any,
        *,
        request_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Parse a Responses API payload into the native agent contract."""
        usage_dict: dict[str, int] = {}
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            usage_dict = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": _cached_tokens_from_usage(usage),
                "cache_creation_tokens": _cache_write_tokens_from_usage(usage),
                "cache_write_tokens": _cache_write_tokens_from_usage(usage),
                "web_search_requests": _web_search_requests_from_usage(usage),
            }
            logger.info(
                "LLM usage: input=%d output=%d total=%d",
                input_tokens,
                output_tokens,
                getattr(usage, "total_tokens", 0) or (input_tokens + output_tokens),
            )

        response_items = getattr(response, "output", []) or []
        reasoning_text = _extract_reasoning_text_from_response(response)
        assistant_text = _extract_message_text_from_response(response)
        continuity = _extract_response_continuity_metadata(
            response,
            store=bool((request_metadata or {}).get("store")),
            previous_response_id=(request_metadata or {}).get("previous_response_id"),
            input_items=(request_metadata or {}).get("input_items"),
        )
        converted_payload = (request_metadata or {}).get("provider_payload")
        function_calls = [item for item in response_items if getattr(item, "type", None) == "function_call"]

        # S1-002: Route Responses-API "incomplete" status through the agent
        # loop's unified max_output_tokens recovery.  ``model_context_window_exceeded``
        # and other context-window-exceeded aliases normalize to
        # ``max_output_tokens`` inside ``_responses_incomplete_reason`` so a
        # single loop branch (``intent/agent_loop.py:_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT``)
        # recovers both.  Only fires when no function_calls were emitted - if
        # the model produced a tool call before the incomplete event we let
        # the tool-call path take over.
        incomplete_reason = _responses_incomplete_reason(response)
        if incomplete_reason and not function_calls:
            result = build_ai_no_result(incomplete_reason, retryable=True)
            result.update(
                {
                    "_usage": usage_dict,
                    "_reasoning": reasoning_text,
                    "response_id": continuity.get("response_id"),
                    "_continuity": continuity,
                    "_recoverable_provider_error": incomplete_reason,
                }
            )
            if isinstance(converted_payload, dict):
                result["_converted_responses_payload"] = copy.deepcopy(converted_payload)
            return result

        if function_calls:
            replayable_response_items = [
                replay_item
                for replay_item in (_response_output_item_to_input_item(item) for item in response_items)
                if replay_item is not None
            ]
            raw_content: dict[str, Any] = {
                "_openai_assistant": True,
                "role": "assistant",
                "content": assistant_text,
                "response_id": continuity.get("response_id"),
                "previous_response_id": continuity.get("previous_response_id"),
                "store": continuity.get("store"),
                "continuity_mode": continuity.get("continuity_mode"),
                "has_encrypted_reasoning": continuity.get("has_encrypted_reasoning", False),
                "response_items": replayable_response_items,
                "tool_calls": [],
            }

            all_tool_calls: list[dict[str, Any]] = []
            for call in function_calls:
                raw_arguments = getattr(call, "arguments", "")
                coerced_arguments = coerce_tool_call_arguments(raw_arguments)
                args = coerced_arguments.args
                tool_name = getattr(call, "name", "") or ""
                tool_use_id = getattr(call, "call_id", "") or ""
                raw_content["tool_calls"].append(
                    {
                        "id": tool_use_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, separators=(",", ":")),
                        },
                    }
                )
                parsed_call = {
                    "tool": tool_name,
                    "args": args,
                    "tool_use_id": tool_use_id,
                }
                # Same contract as _parse_native_response: empty and malformed
                # payloads are flagged, never dispatched as a silent no-arg
                # call.
                if coerced_arguments.was_empty:
                    parsed_call["_arguments_were_empty"] = True
                elif coerced_arguments.was_malformed:
                    parsed_call["_arguments_were_malformed"] = True
                all_tool_calls.append(parsed_call)

            first = all_tool_calls[0]
            try:
                self._emit_diagnostics(
                    "route.native_tool_call",
                    latency_ms=getattr(self, "last_latency_ms", 0),
                    tool_name=first["tool"],
                    tool_count=len(all_tool_calls),
                    tokens_used=usage_dict.get("input_tokens", 0) + usage_dict.get("output_tokens", 0),
                )
            except Exception:
                logger.debug(
                    "Diagnostics emission failed for responses native tool call",
                    exc_info=True,
                )
            result = {
                "type": "tool_call",
                "tool": first["tool"],
                "args": first["args"],
                "tool_use_id": first["tool_use_id"],
                "_raw_content": raw_content,
                "_all_tool_calls": all_tool_calls,
                "_usage": usage_dict,
                "_reasoning": reasoning_text,
                "response_id": continuity["response_id"],
                "_continuity": continuity,
            }
            if isinstance(converted_payload, dict):
                result["_converted_responses_payload"] = copy.deepcopy(converted_payload)
            return result

        content = assistant_text.strip()
        if not content:
            result = build_ai_no_result("empty_assistant_content")
            result.update(
                {
                    "_usage": usage_dict,
                    "_reasoning": reasoning_text,
                    "response_id": continuity["response_id"],
                    "_continuity": continuity,
                }
            )
            if isinstance(converted_payload, dict):
                result["_converted_responses_payload"] = copy.deepcopy(converted_payload)
            return result

        stripped = _strip_llm_code_fences(content)
        try:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = json.loads(stripped.replace("{{", "{").replace("}}", "}"))
            if isinstance(parsed, dict) and parsed.get("type") in {
                "answer",
                "command",
                "tool_call",
                "ignore",
            }:
                if parsed.get("type") == "answer" and "continue_listening" in parsed:
                    parsed["continue_listening"] = bool(parsed["continue_listening"])
                parsed["_usage"] = usage_dict
                parsed["_reasoning"] = reasoning_text
                parsed["response_id"] = continuity["response_id"]
                parsed["_continuity"] = continuity
                if isinstance(converted_payload, dict):
                    parsed["_converted_responses_payload"] = copy.deepcopy(converted_payload)
                return parsed
        except json.JSONDecodeError:
            pass

        result = self._build_answer_response(
            content,
            usage_dict,
            reasoning_text=reasoning_text,
            continuity=continuity,
        )
        if isinstance(converted_payload, dict):
            result["_converted_responses_payload"] = copy.deepcopy(converted_payload)
        return result

    def _build_answer_response(
        self,
        answer: str,
        usage_dict: dict[str, int],
        *,
        reasoning_text: str | None = None,
        continuity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a normalized answer payload with explicit follow-up state."""
        result: dict[str, Any] = {
            "type": "answer",
            "answer": answer,
            "_usage": usage_dict,
        }
        if reasoning_text is not None:
            result["_reasoning"] = reasoning_text
        result["continue_listening"] = False
        if continuity:
            result["response_id"] = continuity.get("response_id")
            result["_continuity"] = continuity
        return result

    def _resolve_native_tool_choice(
        self,
        tool_choice_override: Any | None,
        first_turn: bool,
        has_tools: bool,
    ) -> str | dict[str, str] | None:
        """Resolve tool choice for native agent turns."""
        if not has_tools:
            return None
        if isinstance(tool_choice_override, str) and tool_choice_override:
            return tool_choice_override
        if isinstance(tool_choice_override, dict):
            function = tool_choice_override.get("function", {})
            if isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str) and name:
                    return {"type": "function", "name": _sanitize_tool_name(name)}
        if first_turn and self._preferred_first_tool:
            return {
                "type": "function",
                "name": _sanitize_tool_name(self._preferred_first_tool),
            }
        tool_choice = getattr(self, "_agent_tool_choice", "auto")
        if isinstance(tool_choice, str) and tool_choice:
            return tool_choice
        return "auto"

    def _resolve_prompt_cache_key(self, kwargs: dict[str, Any] | None = None) -> str | None:
        """Resolve a stable-per-conversation OpenAI ``prompt_cache_key``.

        Identity priority, most-to-least authoritative, all stable across the
        turns of a single conversation and distinct per conversation:

        1. An explicit ``prompt_cache_key`` forwarded by the caller.
        2. The task-trace id, which the agent loop holds constant for every
           turn of one agent run (the LLC run's ``fa6c24ee7d31``).
        3. The current command stream id, constant across a streamed command's
           turns via the ``command_stream_context`` contextvar.

        Returns ``None`` (param omitted) when no stable identity exists, e.g. a
        one-off ASK turn with no trace and no stream — there is no multi-turn
        prefix to pin, so automatic prefix caching alone is correct there.
        """
        forwarded = (kwargs or {}).get("prompt_cache_key")
        resolved = _normalize_prompt_cache_key(forwarded)
        if resolved:
            return resolved

        task_trace = getattr(self, "_task_trace", None)
        task_id = getattr(task_trace, "task_id", None)
        resolved = _normalize_prompt_cache_key(task_id)
        if resolved:
            return resolved

        return _normalize_prompt_cache_key(get_current_command_stream_id())

    @staticmethod
    def _convert_image_block_to_responses_input(
        block: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Convert chat/Anthropic-style image blocks to OpenAI Responses input_image."""
        block_type = block.get("type")
        detail = block.get("detail")

        if block_type == "image":
            source = block.get("source", {})
            if isinstance(source, dict) and source.get("type") == "base64" and source.get("data"):
                item: dict[str, Any] = {
                    "type": "input_image",
                    "image_url": "data:%s;base64,%s"
                    % (
                        source.get("media_type", "image/png"),
                        source["data"],
                    ),
                }
                if isinstance(detail, str) and detail:
                    item["detail"] = detail
                return item
            return None

        image_url_value = block.get("image_url")
        if isinstance(image_url_value, dict):
            image_url = image_url_value.get("url")
            detail = detail or image_url_value.get("detail")
        elif isinstance(image_url_value, str):
            image_url = image_url_value
        else:
            return None

        if not isinstance(image_url, str) or not image_url:
            return None

        item = {"type": "input_image", "image_url": image_url}
        if isinstance(detail, str) and detail:
            item["detail"] = detail
        return item

    def _convert_tool_result_to_responses_output(self, content: Any) -> str | list[dict[str, Any]]:
        """Convert stored tool_result content into Responses API input shape."""
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            converted: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in {"text", "input_text"}:
                    converted.append({"type": "input_text", "text": str(block.get("text", ""))})
                elif block_type in {"image", "input_image", "image_url"}:
                    image_item = self._convert_image_block_to_responses_input(block)
                    if image_item is not None:
                        converted.append(image_item)
            if converted:
                return converted

        return str(content)

    def _convert_messages_to_responses_input(
        self,
        messages: list[dict[str, Any]],
        *,
        replay_response_items: bool = False,
    ) -> list[dict[str, Any]]:
        """Convert agent-executor messages into Responses API input items."""
        result: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")

            if role == "user":
                if isinstance(content, str):
                    if _is_stale_verifier_prompt(content):
                        continue
                    result.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    content_parts: list[dict[str, Any]] = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "tool_result":
                            if content_parts:
                                result.append({"role": "user", "content": content_parts})
                                content_parts = []
                            result.append(
                                {
                                    "type": "function_call_output",
                                    "call_id": block.get("tool_use_id", ""),
                                    "output": self._convert_tool_result_to_responses_output(block.get("content", "")),
                                }
                            )
                        elif block_type in {"text", "input_text"}:
                            text = str(block.get("text", ""))
                            if not _is_stale_verifier_prompt(text):
                                content_parts.append({"type": "input_text", "text": text})
                        elif block_type in {"image", "input_image", "image_url"}:
                            image_item = self._convert_image_block_to_responses_input(block)
                            if image_item is not None:
                                content_parts.append(image_item)
                    if content_parts:
                        result.append({"role": "user", "content": content_parts})

            elif role == "assistant":
                if isinstance(content, dict) and content.get("_openai_assistant"):
                    if replay_response_items:
                        response_items = content.get("response_items")
                        if isinstance(response_items, list) and response_items:
                            for item in response_items:
                                replay_item = _response_output_item_to_input_item(item)
                                if replay_item is not None:
                                    result.append(replay_item)
                            continue

                    if isinstance(content.get("content"), str) and content["content"].strip():
                        result.append(
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": content["content"],
                                    }
                                ],
                            }
                        )

                    tool_calls = content.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for tool_call in tool_calls:
                            if not isinstance(tool_call, dict):
                                continue
                            function = tool_call.get("function", {})
                            if not isinstance(function, dict):
                                continue
                            result.append(
                                {
                                    "type": "function_call",
                                    "call_id": tool_call.get("id", ""),
                                    "name": function.get("name", ""),
                                    "arguments": function.get("arguments", "{}"),
                                }
                            )
                elif isinstance(content, str) and content.strip():
                    result.append(
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": content,
                                }
                            ],
                        }
                    )
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "refusal":
                            refusal = str(block.get("refusal") or block.get("text") or "").strip()
                            if refusal:
                                parts.append({"type": "refusal", "refusal": refusal})
                        elif block_type in {"text", "input_text", "output_text"} and "text" in block:
                            text = str(block["text"])
                            if text.strip():
                                parts.append({"type": "output_text", "text": text})
                    if parts:
                        result.append({"role": "assistant", "content": parts})

        # Orphan filtering happens at the final api_kwargs["input"] assembly
        # so it can see the full chain (response_items + delta). Filtering here
        # would drop valid function_call_outputs whose paired function_call lives
        # in the prior continuity items (2026-05-26 LLC trace regression).
        return result

    def _convert_messages_to_openai(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        """Convert agent executor's native messages to OpenAI chat format.

        The agent executor builds messages in an Anthropic-ish format:

        - User messages with string content (plain text)
        - User messages with list content (``tool_result`` blocks +
          optional ``text`` blocks for spin-detection interventions)
        - Assistant messages whose ``content`` is a dict with
          ``_openai_assistant`` flag (our stored raw_content)
        - Assistant messages whose ``content`` is a plain string
          (e.g. from nudge continuations)

        This method converts them to the OpenAI chat format:

        - ``{"role": "system", ...}`` for the system prompt
        - ``{"role": "user", ...}`` with string content
        - ``{"role": "assistant", ..., "tool_calls": [...]}``
        - ``{"role": "tool", "tool_call_id": ..., "content": ...}``
        """
        result: list[dict[str, Any]] = []

        if system_prompt:
            result.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")

            if role == "user":
                if isinstance(content, str):
                    if _is_stale_verifier_prompt(content):
                        continue
                    result.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    # List of tool_result blocks + optional text blocks
                    text_parts: list[str] = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            tool_content = block.get("content", "")
                            # Handle content that's a list
                            # (e.g., image + text blocks)
                            if isinstance(tool_content, list):
                                parts = [
                                    sub.get("text", "")
                                    for sub in tool_content
                                    if isinstance(sub, dict) and sub.get("type") == "text"
                                ]
                                tool_content = "\n".join(parts)
                            result.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": block.get("tool_use_id", ""),
                                    "content": str(tool_content),
                                }
                            )
                        elif block.get("type") == "text":
                            text = str(block.get("text", ""))
                            if not _is_stale_verifier_prompt(text):
                                text_parts.append(text)
                    # Append any text blocks (spin interventions) as
                    # a user message after the tool results
                    if text_parts:
                        result.append(
                            {
                                "role": "user",
                                "content": "\n".join(text_parts),
                            }
                        )

            elif role == "assistant":
                if isinstance(content, dict) and content.get("_openai_assistant"):
                    # Our stored raw_content from _parse_native_response
                    openai_msg: dict[str, Any] = {"role": "assistant"}
                    if content.get("content"):
                        openai_msg["content"] = content["content"]
                    if content.get("tool_calls"):
                        openai_msg["tool_calls"] = content["tool_calls"]
                    result.append(openai_msg)
                elif isinstance(content, str):
                    # Plain text (nudge continuation, final answer text)
                    result.append({"role": "assistant", "content": content})
                elif isinstance(content, list):
                    # Unlikely for OpenAI but handle gracefully:
                    # extract text from content blocks
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            parts.append(block["text"])
                    if parts:
                        result.append(
                            {
                                "role": "assistant",
                                "content": "".join(parts),
                            }
                        )

        return result
