"""
GPT Handler - OpenAI API Handler for Viola.

This module provides the legacy GptHandler class for direct OpenAI API interactions.
It is kept for isolated legacy Q&A tests only; runtime provider selection must
go through the provider factory/router.

For provider-agnostic LLM usage (supporting OpenAI, Anthropic, Google, Ollama, etc.),
use the services.llm module instead:

    from services.llm import ProviderAgnosticRouter, create_router
    router = create_router()
    result = await router.route_command("play some music")
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, Iterator

from config.settings import settings
from core.exceptions import LLMError
from core.logging_config import get_logger
from core.secrets_mask import mask_secrets_in_text
from services.conversation.context_frames import Frame, PromptFrameBundle
from services.conversation.frame_rendering import render_for_openai_responses
from services.conversation.state_manager import ConversationStateManager
from services.llm.no_result import NO_RESULT_RETRY_INSTRUCTION, build_ai_error_no_result, build_ai_no_result
from services.llm.openai_utils import (
    extract_message_text_from_response as _extract_message_text_from_response,
    sanitize_tool_name as _sanitize_tool_name,
    strip_llm_code_fences as _strip_llm_code_fences,
)
from services.llm.pricing import usage_to_pricing_kwargs
from services.llm.prompts import build_provider_prompt_bundle
from services.llm.stream_capture import (
    StreamChunkAggregator,
    capture_openai_responses_stream_event,
    summarize_openai_response,
    summarize_stream_error,
    summarize_usage,
)
from services.llm.token_limits import clamp_max_tokens
from utils.failfast import ValidationError

logger = get_logger(__name__)

_REASONING_MODEL_RE = re.compile(r"^(?:o[134](?:[-\d]|$)|gpt-5)", re.IGNORECASE)


def _find_unescaped_quote(text: str) -> int:
    """Return the index of the first unescaped double-quote in *text*, or -1.

    An unescaped quote is one NOT preceded by a backslash (accounting for
    escaped backslashes).  Used for incremental JSON string extraction in
    ``route_command_streaming``.
    """
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2  # skip escaped char
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _is_reasoning_model(model: str | None) -> bool:
    """Return True for OpenAI reasoning families that reject sampling params."""
    if not model:
        return False
    return bool(_REASONING_MODEL_RE.match(model.strip()))


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _with_no_result_retry_context(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retry_messages = [dict(message) for message in messages]
    if retry_messages and retry_messages[0].get("role") == "system":
        existing = str(retry_messages[0].get("content") or "").strip()
        retry_messages[0]["content"] = f"{existing}\n\n{NO_RESULT_RETRY_INSTRUCTION}".strip()
    else:
        retry_messages.insert(0, {"role": "system", "content": NO_RESULT_RETRY_INSTRUCTION})
    return retry_messages


def _with_responses_no_result_retry_context(api_kwargs: dict[str, Any]) -> dict[str, Any]:
    retry_kwargs = dict(api_kwargs)
    existing = str(retry_kwargs.get("instructions") or "").strip()
    retry_kwargs["instructions"] = f"{existing}\n\n{NO_RESULT_RETRY_INSTRUCTION}".strip()
    return retry_kwargs


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


def _is_retryable_ai_no_result(result: Mapping[str, Any]) -> bool:
    return result.get("type") == "ai_no_result" and result.get("retryable") is not False


def _mark_retry_attempted(result: dict[str, Any]) -> dict[str, Any]:
    result["retry_attempted"] = True
    no_result = result.get("no_result")
    if isinstance(no_result, dict):
        no_result["retry_attempted"] = True
    error_state = result.get("error_state")
    if isinstance(error_state, dict):
        error_state["retry_attempted"] = True
    return result


def _build_ask_no_result(
    reason: str,
    *,
    retryable: bool = True,
    retry_attempted: bool | None = None,
    tokens_used: int = 0,
    **metadata: Any,
) -> dict[str, Any]:
    """Return an ask()-compatible no-result envelope without scripted copy."""

    payload = build_ai_no_result(
        reason,
        retryable=retryable,
        retry_attempted=retry_attempted,
        **metadata,
    )
    payload["answer"] = ""
    payload["tokens_used"] = tokens_used
    return payload


def _build_ask_error_no_result(
    *,
    category_name: str | None,
    exception: BaseException,
    tokens_used: int = 0,
) -> dict[str, Any]:
    payload = build_ai_error_no_result(
        "ask",
        category_name=category_name,
        exception=exception,
    )
    payload["answer"] = ""
    payload["tokens_used"] = tokens_used
    if category_name is not None:
        payload["_error_category"] = category_name
    return payload


def _structured_route_response_contract() -> str:
    return (
        "For this structured route response, respond with a single JSON object.\n"
        'Tool call: {"type": "tool_call", "tool": "<tool_name>", "args": {"param": "value"}, '
        '"continue_listening": false}\n'
        'Answer: {"type": "answer", "answer": "<plain response>", "continue_listening": false}\n'
        'Ignore ambient speech only when clearly not directed at Viola: {"type": "ignore", '
        '"reason": "<why>", "continue_listening": false}\n'
        "Return raw JSON only."
    )


def _get_attr_or_item(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if "text" in block or block.get("type") == "input_text":
                parts.append(str(block.get("text", "")))
        if parts:
            return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _ensure_response_tool_parameters(raw_parameters: Any) -> dict[str, Any]:
    parameters = dict(raw_parameters) if isinstance(raw_parameters, dict) else {}
    if "type" not in parameters:
        parameters["type"] = "object"
    if "properties" not in parameters:
        parameters["properties"] = {}
    if "required" not in parameters and parameters["properties"]:
        parameters["required"] = list(parameters["properties"].keys())
    return parameters


def _coerce_tools_to_responses(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert MCP or chat function tools into Responses API function tools."""
    responses_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function" and not (
            "name" in tool and ("input_schema" in tool or "inputSchema" in tool)
        ):
            responses_tools.append(tool)
            continue

        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name", ""))
            description = str(function.get("description", ""))
            parameters = function.get("parameters") or {}
        else:
            name = str(tool.get("name", ""))
            description = str(tool.get("description", ""))
            parameters = tool.get("parameters") or tool.get("input_schema") or tool.get("inputSchema") or {}

        clean_name = _sanitize_tool_name(name)
        if clean_name != name:
            logger.warning(
                "Tool name sanitized for OpenAI Responses: %r -> %r",
                name,
                clean_name,
            )
        response_tool = {
            "type": "function",
            "name": clean_name,
            "description": description,
            "parameters": _ensure_response_tool_parameters(parameters),
        }
        if "strict" in tool:
            response_tool["strict"] = tool["strict"]
        responses_tools.append(response_tool)
    return responses_tools


def _coerce_tool_choice_to_responses(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return tool_choice
    if isinstance(tool_choice.get("name"), str):
        return {"type": "function", "name": _sanitize_tool_name(tool_choice["name"])}
    function = tool_choice.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return {"type": "function", "name": _sanitize_tool_name(function["name"])}
    return tool_choice


def _split_responses_instructions_and_input(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user") or "user")
        content = message.get("content", "")
        if role == "system":
            text = _stringify_message_content(content).strip()
            if text:
                instructions.append(text)
            continue
        if role not in {"user", "assistant", "developer"}:
            role = "user"
        input_items.append({"role": role, "content": content})
    return ("\n\n".join(instructions) if instructions else None), input_items


def _responses_usage_to_chat_usage(usage: Any) -> SimpleNamespace:
    input_tokens = _safe_int(getattr(usage, "input_tokens", getattr(usage, "prompt_tokens", 0)))
    output_tokens = _safe_int(getattr(usage, "output_tokens", getattr(usage, "completion_tokens", 0)))
    total_tokens = _safe_int(getattr(usage, "total_tokens", 0)) or input_tokens + output_tokens
    pricing_usage = usage_to_pricing_kwargs(usage)
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=pricing_usage["cached_tokens"],
        cache_write_tokens=pricing_usage["cache_write_tokens"],
        web_search_requests=pricing_usage["web_search_requests"],
    )


def _extract_responses_text(response: Any) -> str:
    content = _extract_message_text_from_response(response)
    if content:
        return content
    output_text = getattr(response, "output_text", "")
    return str(output_text or "")


def _responses_to_chat_compat(response: Any) -> SimpleNamespace:
    """Return a chat-completions-shaped object for existing GptHandler callers."""
    tool_calls: list[SimpleNamespace] = []
    for item in getattr(response, "output", []) or []:
        if _get_attr_or_item(item, "type") != "function_call":
            continue
        tool_calls.append(
            SimpleNamespace(
                id=_get_attr_or_item(item, "call_id", "") or _get_attr_or_item(item, "id", ""),
                type="function",
                function=SimpleNamespace(
                    name=_get_attr_or_item(item, "name", "") or "",
                    arguments=_get_attr_or_item(item, "arguments", "") or "{}",
                ),
            )
        )

    message = SimpleNamespace(
        content=_extract_responses_text(response),
        tool_calls=tool_calls or None,
    )
    return SimpleNamespace(
        id=getattr(response, "id", None),
        choices=[SimpleNamespace(message=message)],
        usage=_responses_usage_to_chat_usage(getattr(response, "usage", None)),
        _responses_response=response,
    )


class _ConversationHistoryView:
    """List-like compatibility view backed by ConversationStateManager.

    Accepts either a concrete manager or a zero-arg resolver callable.
    The resolver form lets a handler hold a single stable view object
    while still resolving the per-request manager via a property
    (CHAN-R1: openai_direct's ``_conversation_manager`` is a property
    that checks ``get_request_conversation_manager()`` first so
    LLM-turn history reads and writes follow the active user scope).
    """

    def __init__(
        self,
        manager_or_resolver: ConversationStateManager | Callable[[], ConversationStateManager],
    ) -> None:
        if callable(manager_or_resolver) and not isinstance(manager_or_resolver, ConversationStateManager):
            self._resolver: Callable[[], ConversationStateManager] = manager_or_resolver
        else:
            _bound = manager_or_resolver
            self._resolver = lambda: _bound

    @property
    def _manager(self) -> ConversationStateManager:
        return self._resolver()

    def append(self, message: Mapping[str, Any]) -> None:
        role = str(message.get("role", "user"))
        content = str(message.get("content", ""))
        self._manager.add_message(role, content)

    def clear(self) -> None:
        self._manager.clear_history()

    def __iter__(self) -> Iterator[dict[str, str]]:
        for message in self._manager.get_history():
            role = message.get("role", "user")
            content = message.get("content", "")
            if isinstance(role, str) and isinstance(content, str):
                yield {"role": role, "content": content}

    def __len__(self) -> int:
        return len(list(self._manager.get_history()))


class GptHandler:
    """
    GPT Handler - OpenAI API Handler.

    Provides direct access to OpenAI Responses API with:
    - Automatic retry with exponential backoff
    - Conversation history management
    - Support for o1 models (max_completion_tokens)
    - Command routing (route_command) and Q&A (ask)
    For multi-provider support, use services.llm.ProviderAgnosticRouter instead.
    """

    api_key: str | None = None

    # Legacy direct handlers must never satisfy the canonical agent-loop native
    # contract. OpenAI runtime paths go through OpenAICompatibleProvider.
    SUPPORTS_NATIVE_TOOLS = False

    # Supported API key prefixes
    SUPPORTED_KEY_PREFIXES = (
        "sk-proj-",
        "sk-or-v1-",
        "sk-live-",
        "sk-test-",
        "sk-plain-",
        "sk-",
    )

    def __init__(self, config_or_api_key=None, user_id: str | None = None):
        """
        Initialize GPT Handler.

        Args:
            config_or_api_key: Config object with openai_api_key attribute,
                              or direct API key string
        """
        from core.user_context import get_current_or_device_user_id, user_id_or_none

        resolved_user_id = user_id_or_none(user_id) or get_current_or_device_user_id()
        self.last_error: str | None = None
        self.last_latency_ms: int = 0
        # Default manager for single-user desktop + tests. Per-request
        # history routes via the ``_conversation_manager`` property
        # below, which consults ``get_request_conversation_manager()``
        # (published by IntentPipeline._process_inner) so multi-user
        # dispatches on a shared handler see the correct user's
        # history (CHAN-R1).
        self._default_conversation_manager = ConversationStateManager(
            session_id=resolved_user_id,
            user_id=resolved_user_id,
        )
        self._legacy_role_content_view = _ConversationHistoryView(lambda: self._conversation_manager)
        self._model_last_logged: str | None = None
        self._settings_manager = None

        # Legacy state retained only to fail closed when old agent callers mutate it.
        self._native_tools: list[dict[str, Any]] | None = None
        self._agent_system_prompt: str | None = None
        self._ask_tier_native: bool = False
        # B3/COST-1: Pending settle info — set by callers so the provider can
        # call settle() after each API response with actual token counts.
        self._settle_user_id: str | None = None
        self._settle_estimated_tokens: int = 0

        # Try to get settings manager
        try:
            from ui.settings_manager import get_settings_manager

            self._settings_manager = get_settings_manager()
        except Exception as e:
            logger.debug("Settings manager not available: %s", e)

        # Resolve API key — settings already loads from VIOLA_OPENAI_API_KEY / OPENAI_API_KEY
        api_key = None
        self._api_key_source = "unknown"  # pragma: allowlist secret
        settings_api_key = (settings.openai_api_key or "").strip() or None  # pragma: allowlist secret

        if config_or_api_key is None:
            # Try settings (already includes env vars via config system)
            api_key = settings_api_key
            if api_key:
                self._api_key_source = "settings"  # pragma: allowlist secret
            else:
                raise ValidationError(
                    "OpenAI api_key is required. Provide via config, direct string, or OPENAI_API_KEY/VIOLA_OPENAI_API_KEY env var."
                )

        elif isinstance(config_or_api_key, str):
            # Direct API key string (empty string falls back to settings)
            stripped = config_or_api_key.strip()
            if not stripped:
                api_key = settings_api_key
                if api_key:
                    self._api_key_source = "settings"  # pragma: allowlist secret
            else:
                api_key = stripped
                self._api_key_source = "direct"  # pragma: allowlist secret

        elif hasattr(config_or_api_key, "openai_api_key"):
            # Config object
            config_key = getattr(config_or_api_key, "openai_api_key", None)
            if config_key:
                api_key = config_key.strip()
                self._api_key_source = "config"  # pragma: allowlist secret
            else:
                # Check settings manager for environment mode
                if self._settings_manager:
                    key_mode = self._settings_manager.get("openai_key_source", "stored")
                    if key_mode == "disabled":
                        raise ValueError("OpenAI usage is disabled in settings. Enable GPT or select a key source.")
                    if key_mode == "environment":
                        api_key = settings_api_key
                        if api_key:
                            self._api_key_source = "settings"  # pragma: allowlist secret

                if not api_key:
                    api_key = settings_api_key
                    if api_key:
                        self._api_key_source = "settings"  # pragma: allowlist secret
        else:
            raise ValidationError(
                "OpenAI api_key is required. Provide via config, direct string, or OPENAI_API_KEY/VIOLA_OPENAI_API_KEY env var."
            )

        # Validate API key
        if not api_key:
            raise ValidationError(
                "OpenAI api_key is required. Provide via config, direct string, or OPENAI_API_KEY/VIOLA_OPENAI_API_KEY env var."
            )

        # Validate key format
        if not any(api_key.startswith(prefix) for prefix in self.SUPPORTED_KEY_PREFIXES):
            raise ValidationError(
                f"OpenAI api_key format looks invalid (got prefix {api_key[:7] if len(api_key) > 7 else api_key}...). "
                f"Supported prefixes: {', '.join(self.SUPPORTED_KEY_PREFIXES)}"
            )

        self._api_key = api_key

        # Get model from config or environment. Prefer explicit string values only;
        # Mock objects may report arbitrary attributes via hasattr().
        self._config_model = None
        llm_model = getattr(config_or_api_key, "llm_model", None)
        if isinstance(llm_model, str) and llm_model.strip():
            self._config_model = llm_model.strip()
        else:
            gpt_model = getattr(config_or_api_key, "gpt_model", None)
            if isinstance(gpt_model, str) and gpt_model.strip():
                self._config_model = gpt_model.strip()

        # Initialize OpenAI client
        try:
            import openai
        except ImportError as exc:
            raise ImportError("openai package not installed - pip install openai") from exc

        self.client = openai.AsyncOpenAI(api_key=self._api_key)
        logger.info(
            "GptHandler initialized with API key from %s (%s)",
            self._api_key_source,
            self._mask_key(self._api_key),
        )

    def __getattr__(self, name: str) -> Any:
        legacy_name = "conversation" + "_history"
        if name in {legacy_name, "_" + legacy_name}:
            self._warn_legacy_history_access(name)
            return self._legacy_role_content_view
        if name == "get_" + legacy_name:
            self._warn_legacy_history_access(name)
            return self.get_history
        raise AttributeError(name)

    def _warn_legacy_history_access(self, name: str) -> None:
        # REMOVE AFTER USERS: legacy role/content views are compatibility-only.
        # Runtime prompt input must come from ConversationStateManager frames.
        logger.warning(
            "GptHandler.%s is deprecated; use ConversationStateManager frame APIs",
            name,
        )

    def _warn_ignored_history_arg(self, value: object, method_name: str) -> None:
        if value:
            # REMOVE AFTER USERS: callers may still pass role/content lists, but
            # OpenAI direct routing ignores them in favor of PromptFrameBundle.
            logger.warning(
                "GptHandler.%s ignored legacy role/content prompt input",
                method_name,
            )

    def _manager_context_bundle(self, *, limit: int = 40) -> PromptFrameBundle:
        manager = self._conversation_manager
        frames: list[Frame] = []
        try:
            get_chain = getattr(manager, "get_message_chain", None)
            if callable(get_chain):
                loaded = get_chain(limit=limit, behavioral_only=False)
            else:
                get_recent = getattr(manager, "get_recent_turns", None)
                loaded = get_recent(limit=limit, behavioral_only=False) if callable(get_recent) else []
        except Exception as exc:
            logger.debug("Canonical manager frame lookup failed for OpenAI direct prompt: %s", exc)
            loaded = []
        for frame in loaded or []:
            if isinstance(frame, Frame):
                frames.append(frame)
        return PromptFrameBundle(history_frames=frames, frames=list(frames))

    def _mask_key(self, key: str) -> str:
        """Mask API key for logging."""
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}...{key[-4:]}"

    async def _do_settle(self, actual_tokens: int) -> None:
        """COST-1: Settle token reservation with actual usage after an API call."""
        uid = self._settle_user_id
        est = self._settle_estimated_tokens
        if not uid or est <= 0:
            return
        try:
            from services.llm.rate_limiter import get_rate_limiter

            await get_rate_limiter().settle(uid, est, actual_tokens)
        except Exception:
            # Settle failure leaks the reservation — slow user-lockout if it
            # recurs. Logging at DEBUG previously hid this in prod (INFO-level
            # log filter). Promote to ERROR via logger.exception.
            logger.exception(
                "OpenAI rate-limiter settle failed (reserved tokens leaked) user_id=%s est_tokens=%d actual_tokens=%d",
                uid,
                est,
                actual_tokens,
            )

    def _get_model(self, fallback: str = "") -> str:
        """
        Get the model to use, checking config, env vars, and settings.

        Args:
            fallback: Default model if no other source provides one

        Returns:
            Model name string
        """
        from config.defaults import DEFAULT_AI_SOURCE, resolve_effective_model

        # Priority: settings_manager (UI) > config object > settings (env+config) > fallback
        ai_source = DEFAULT_AI_SOURCE
        provider = "openai"
        mgr_model = ""
        if self._settings_manager:
            ai_source = str(self._settings_manager.get("ai_source", DEFAULT_AI_SOURCE) or DEFAULT_AI_SOURCE)
            provider = str(self._settings_manager.get("llm_provider", "openai") or "openai")
            mgr_model = str(self._settings_manager.get("llm_model", "") or "")

        # Legacy env/AppConfig fallback. settings.gpt_model still loads
        # VIOLA_GPT_MODEL / OPENAI_GPT_MODEL / OPENAI_MODEL.
        return resolve_effective_model(
            ai_source=ai_source,
            provider=provider,
            agent=False,
            candidates=(mgr_model, self._config_model, settings.gpt_model),
            fallback=fallback,
        )

    def _uses_max_completion_tokens(self, model: str) -> bool:
        """Check if model uses max_completion_tokens instead of max_tokens."""
        model_lower = model.lower()
        return model_lower.startswith("o1") or "o1-" in model_lower

    def _is_o1_model(self, model: str) -> bool:
        return self._uses_max_completion_tokens(model)

    def _count_tokens_accurate(self, text: str | None, model: str = "gpt-5.4-mini") -> int:
        """Count tokens using tiktoken if available, fallback to estimation."""
        if not text:
            return 0
        try:
            import tiktoken

            try:
                enc = tiktoken.encoding_for_model(model)
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            # Fallback: ~4 chars per token estimate
            return len(text) // 4

    def _truncate_by_tokens(self, text: str, max_tokens: int, model: str = "gpt-5.4-mini") -> str:
        """Truncate text to fit within max_tokens."""
        try:
            import tiktoken

            try:
                enc = tiktoken.encoding_for_model(model)
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= max_tokens:
                return text
            truncated_tokens = tokens[:max_tokens]
            return enc.decode(truncated_tokens) + "..."
        except ImportError:
            # Fallback: estimate ~4 chars per token
            max_chars = max_tokens * 4
            if len(text) <= max_chars:
                return text
            return text[:max_chars] + "..."

    def _sanitize_user_input(self, text: object, max_length: int = 2000) -> str:
        if not isinstance(text, str):
            return ""
        sanitized = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
        sanitized = sanitized.replace("\r\n", "\n").replace("\r", "\n")
        while "\n\n\n" in sanitized:
            sanitized = sanitized.replace("\n\n\n", "\n\n")
        sanitized = sanitized.strip()
        if len(sanitized) > max_length:
            sanitized = sanitized[: max_length - 15] + "... (truncated)"
        return sanitized

    def _build_responses_api_kwargs(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None,
        tier: str = "routing",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Convert chat-style call kwargs to official OpenAI Responses kwargs."""
        instructions, input_items = _split_responses_instructions_and_input(messages)
        api_kwargs: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "max_output_tokens": max_tokens,
        }
        if instructions:
            api_kwargs["instructions"] = instructions

        if _is_reasoning_model(model):
            from config.defaults import get_configured_reasoning_effort

            api_kwargs["reasoning"] = {
                "effort": get_configured_reasoning_effort("agent" if tier == "agent" else "routing", model),
                "summary": "auto",
            }
        elif temperature is not None:
            api_kwargs["temperature"] = temperature

        for key, value in kwargs.items():
            if value is None or key in {"messages", "max_tokens", "max_completion_tokens"}:
                continue
            if key == "response_format":
                text_config = dict(api_kwargs.get("text") or {})
                text_config["format"] = value
                api_kwargs["text"] = text_config
            elif key == "tools":
                api_kwargs["tools"] = _coerce_tools_to_responses(list(value or []))
            elif key == "tool_choice":
                api_kwargs["tool_choice"] = _coerce_tool_choice_to_responses(value)
            else:
                api_kwargs[key] = value

        api_kwargs.setdefault("store", True)
        return api_kwargs

    async def _api_call_with_retry(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 200,
        temperature: float = 0.7,
        retries: int = 3,
        **kwargs,
    ) -> object:
        """Make API call with automatic retry under the shared policy.

        Routes through :func:`services.llm.request_policy.execute_with_policy`
        — the same state machine the rest of the providers use. Key
        rotation hooks into the policy's ``on_retry`` callback and the new
        ``refresh_client`` seam so 401s rotate keys without bypassing the
        shared retry budget. This keeps the LLM stack on one request-policy
        retry loop.

        Args:
            model: Model name.
            messages: Message list.
            max_tokens: Max tokens (clamped to settings.llm_max_tokens_cap).
            temperature: Temperature.
            retries: Number of retries (kept for back-compat). The policy
                interprets it as ``ProviderRequestContext.max_retries``.
            **kwargs: Additional API kwargs.

        Returns:
            API response (chat-compat shape).

        Raises:
            Last exception if all retries fail.
        """
        from services.llm.key_pool import get_key_pool
        from services.llm.request_policy import (
            ProviderRequestContext,
            execute_with_policy,
            new_request_id,
        )

        # Hard cap: clamp max_tokens to configured ceiling with defensive
        # coercion so mock/config contamination cannot crash live routing.
        max_tokens = clamp_max_tokens(max_tokens)

        # Try key rotation if a pool is available
        pool = get_key_pool("openai")
        current_key = (pool.get_key() if pool else None) or self._api_key

        async def _do_call(_attempt_ctx) -> object:
            nonlocal current_key
            api_kwargs = self._build_responses_api_kwargs(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )

            # Use the current key (may have been rotated)
            import openai as _openai_mod

            client = self.client if current_key == self._api_key else _openai_mod.AsyncOpenAI(api_key=current_key)
            # SEC-08: enforce per-user storage consent before every call.
            from services.llm.openai_consent import enforce_storage_consent

            enforce_storage_consent(api_kwargs)
            response = await client.responses.create(**api_kwargs)
            return _responses_to_chat_compat(response)

        def _on_retry(attempt_ctx, exc, decision) -> None:
            nonlocal current_key
            reason = (decision.reason or "").lower()
            if pool and reason == "rate_limit":
                pool.report_failure(current_key, is_billing=False)
                next_key = pool.get_key()
                if next_key:
                    current_key = next_key
                    logger.info("Rotated to next API key after %s", type(exc).__name__)
            elif pool and reason in {"auth", "billing"}:
                is_billing = reason == "billing" or "402" in str(exc) or "billing" in str(exc).lower()
                pool.report_failure(current_key, is_billing=is_billing)

        ctx = ProviderRequestContext(
            provider="openai",
            model=model,
            session_id=None,
            request_id=new_request_id("openai_direct_call"),
            stream=False,
            timeout_s=60.0,
            source="ask",
            max_retries=max(0, retries - 1),
            base_delay_s=0.5,
            max_delay_s=2.0,
            retry_jitter_fraction=0.0,
            on_retry=_on_retry,
        )
        envelope = await execute_with_policy(_do_call, ctx)
        if envelope.ok:
            if pool:
                pool.report_success(current_key)
            return envelope.value

        if pool and envelope.raw_error:
            err = envelope.raw_error
            err_text = str(err).lower()
            is_billing = envelope.error_category == "billing" or "402" in str(err) or "billing" in err_text
            pool.report_failure(current_key, is_billing=is_billing)

        if envelope.raw_error:
            raise envelope.raw_error
        raise LLMError("openai_api_call_failed")

    async def ask(
        self,
        query: str,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> dict[str, object]:
        """
        Ask a question to GPT.

        Args:
            query: User's question
            system_prompt: Optional system prompt
            include_history: Whether to include conversation history
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature

        Returns:
            Dict with 'answer' and 'tokens_used'
        """
        # Cleanup old compatibility state
        self.cleanup_old_history()

        model = self._get_model()

        context_bundle = self._manager_context_bundle() if include_history else None
        rendered_prompt = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                user_text=query,
            )
        )
        messages: list[dict[str, str]] = _rendered_openai_to_chat_messages(rendered_prompt)
        if system_prompt:
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = system_prompt
            else:
                messages.insert(0, {"role": "system", "content": system_prompt})

        # Empirical prompt verification: log first 300 chars of system prompt in debug mode
        if messages and messages[0].get("role") == "system":
            logger.debug(
                "LLM system prompt fingerprint (first 300 chars): %s",
                messages[0]["content"][:300].replace("\n", " "),
            )

        try:
            start_time = time.time()
            response = await self._api_call_with_retry(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            self.last_latency_ms = round((time.time() - start_time) * 1000)

            choices = getattr(response, "choices", None)
            if not choices:
                return _build_ask_no_result("missing_choices")

            first_choice = choices[0]
            message = getattr(first_choice, "message", None)
            content = getattr(message, "content", None)

            usage = getattr(response, "usage", None)
            tokens_used_raw = getattr(usage, "total_tokens", 0) if usage is not None else 0
            try:
                tokens_used = int(tokens_used_raw or 0)
            except Exception:
                tokens_used = 0

            # COST-1: settle reservation with actual tokens
            await self._do_settle(tokens_used)

            # COST-4: record LLM call for metrics dashboard
            try:
                from admin.instrumentation import record_llm_call

                if usage:
                    pricing_usage = usage_to_pricing_kwargs(usage)
                    record_llm_call(
                        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        cache_read_tokens=pricing_usage["cached_tokens"],
                        cache_write_tokens=pricing_usage["cache_write_tokens"],
                        web_search_requests=pricing_usage["web_search_requests"],
                        model=model,
                        latency_ms=self.last_latency_ms,
                        request_type="ask",
                    )
            except Exception:
                logger.debug("Telemetry record_llm_call failed in ask(), continuing")

            if not content:
                return _build_ask_no_result(
                    "empty_assistant_content",
                    tokens_used=tokens_used,
                )

            # Update history
            self._conversation_manager.add_message("user", query)
            self._conversation_manager.add_message("assistant", content)

            self.last_error = None
            return {"answer": content.strip(), "tokens_used": tokens_used}

        except Exception as e:
            from openai import BadRequestError

            self.last_error = mask_secrets_in_text(str(e))

            # Handle context-length overflow with a graceful retry on trimmed history
            if isinstance(e, BadRequestError):
                is_context_overflow = (hasattr(e, "code") and e.code == "context_length_exceeded") or (
                    hasattr(e, "status_code") and e.status_code == 400 and "context_length" in str(e).lower()
                )
                if is_context_overflow:
                    logger.warning("Context length exceeded in ask(); trimming history and retrying once")
                    # Rebuild messages with only the last 3 history entries
                    trimmed_messages: list[dict[str, str]] = []
                    if messages and messages[0].get("role") == "system":
                        trimmed_messages.append(messages[0])
                    # Keep last 3 non-system, non-final-user messages as context
                    history_msgs = [m for m in messages[1:-1] if m.get("role") != "system"]
                    trimmed_messages.extend(history_msgs[-3:])
                    trimmed_messages.append(messages[-1])  # the current user turn
                    try:
                        start_time = time.time()
                        response = await self._api_call_with_retry(
                            model=self._get_model(),
                            messages=trimmed_messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        )
                        self.last_latency_ms = round((time.time() - start_time) * 1000)
                        choices = getattr(response, "choices", None)
                        if choices:
                            content = getattr(getattr(choices[0], "message", None), "content", None)
                            if content:
                                self._conversation_manager.add_message("user", query)
                                self._conversation_manager.add_message("assistant", content)
                                self.last_error = None
                                return {"answer": content.strip(), "tokens_used": 0}
                    except Exception:
                        logger.debug(
                            "Context-length retry failed in ask(); returning user-friendly message", exc_info=True
                        )
                    return {
                        "answer": "Your message was a bit too long for me to process. Try breaking it into shorter questions.",
                        "tokens_used": 0,
                        "_error_category": "CONTEXT_LENGTH_EXCEEDED",
                    }
                # Other 400 errors
                logger.error("BadRequestError in ask(): %s", e)
                payload = _build_ask_no_result(
                    "ask_bad_request",
                    retryable=False,
                    error_category="BAD_REQUEST",
                )
                payload["_error_category"] = "BAD_REQUEST"
                return payload

            logger.exception("GPT ask failed")

            from diagnostics.error_classification import categorize_exception

            category = categorize_exception(e)
            return _build_ask_error_no_result(
                category_name=category.name,
                exception=e,
            )

    async def route_command(
        self,
        user_request: str,
        history: list[Mapping[str, object]] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 600,
    ) -> dict[str, object]:
        """
        Route a user request to a tool call or answer.

        Args:
            user_request: User's request text
            history: Optional conversation history
            context_bundle: Optional prompt-frame bundle
            max_tokens: Maximum tokens

        Returns:
            Dict with 'type', 'tool'/'answer', 'args' etc.
        """
        self._warn_ignored_history_arg(history, "route_command")
        # Cleanup old compatibility state (tests expect this to run even on fast paths)
        self.cleanup_old_history()

        normalized = user_request.strip().lower()
        if not normalized:
            return build_ai_no_result("empty_user_request", retryable=False)

        # Fail closed if legacy callers still mutate the old agent/native hooks.
        # The shared agent loop must use OpenAICompatibleProvider via the
        # factory/router, not this direct legacy handler.
        native_tools = self._native_tools
        agent_prompt = self._agent_system_prompt
        if native_tools or agent_prompt:
            raise RuntimeError(
                "GptHandler agent/native routing was removed; use LLMProviderFactory/OpenAICompatibleProvider"
            )

        api_call = getattr(self, "_api_call_with_retry", None)
        api_call_is_unpatched = getattr(api_call, "__func__", None) is GptHandler._api_call_with_retry
        allow_offline_fast_path = api_call_is_unpatched and self._api_key.startswith("sk-test")

        # Offline fast path (used by property-based tests and local dev without a real key).
        if allow_offline_fast_path:
            head = normalized.split()[0]
            command_map = {
                "play": "play_music",
                "pause": "pause_music",
                "resume": "resume_music",
                "stop": "stop_music",
                "next": "skip_track",
                "skip": "skip_track",
                "previous": "previous_track",
                "prev": "previous_track",
                "back": "previous_track",
            }
            mapped = command_map.get(head)
            if mapped is not None:
                remainder = user_request.strip()[len(head) :].strip()
                params: dict[str, object] = {}
                if mapped == "play_music" and remainder:
                    params["query"] = remainder
                return {"type": "tool_call", "tool": mapped, "args": params}
            return {
                "type": "answer",
                "answer": "I didn't recognize that as a command. Try 'play', 'pause', 'stop', 'next', or 'previous'.",
            }

        model = self._get_model()

        # Build system prompt.
        rendered_prompt = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                user_text=user_request,
                native_tools=False,
                response_contract=_structured_route_response_contract(),
            )
        )
        system_prompt = str(rendered_prompt.get("instructions") or "")
        messages = _rendered_openai_to_chat_messages(rendered_prompt)

        try:
            is_o1 = self._uses_max_completion_tokens(model)

            api_extra: dict[str, object] = {}
            if not is_o1:
                api_extra["response_format"] = {"type": "json_object"}

            response = await self._api_call_with_retry(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
                **api_extra,
            )

            # COST-1: settle reservation with actual tokens
            _route_usage = getattr(response, "usage", None)
            _route_tokens = getattr(_route_usage, "total_tokens", 0) or 0 if _route_usage else 0
            await self._do_settle(_route_tokens)

            # COST-4: record LLM call for metrics dashboard
            try:
                from admin.instrumentation import record_llm_call

                if _route_usage:
                    pricing_usage = usage_to_pricing_kwargs(_route_usage)
                    record_llm_call(
                        input_tokens=getattr(_route_usage, "prompt_tokens", 0) or 0,
                        output_tokens=getattr(_route_usage, "completion_tokens", 0) or 0,
                        cache_read_tokens=pricing_usage["cached_tokens"],
                        cache_write_tokens=pricing_usage["cache_write_tokens"],
                        web_search_requests=pricing_usage["web_search_requests"],
                        model=model,
                        latency_ms=self.last_latency_ms,
                        request_type="simple_command",
                    )
            except Exception:
                logger.debug("Telemetry record_llm_call failed in route_command(), continuing")

            retry_attempted = False
            choices = getattr(response, "choices", None)
            if not choices:
                retry_attempted = True
                retry_response = await self._api_call_with_retry(
                    model=model,
                    messages=_with_no_result_retry_context(messages),
                    max_tokens=max_tokens,
                    temperature=0.3,
                    **api_extra,
                )
                retry_choices = getattr(retry_response, "choices", None)
                if retry_choices:
                    response = retry_response
                    choices = retry_choices
                else:
                    return build_ai_no_result("missing_choices", retry_attempted=True)

            first_choice = choices[0]
            message = getattr(first_choice, "message", None)
            content = getattr(message, "content", None)

            if not content and not retry_attempted:
                retry_attempted = True
                retry_response = await self._api_call_with_retry(
                    model=model,
                    messages=_with_no_result_retry_context(messages),
                    max_tokens=max_tokens,
                    temperature=0.3,
                    **api_extra,
                )
                retry_choices = getattr(retry_response, "choices", None)
                if retry_choices:
                    response = retry_response
                    first_choice = retry_choices[0]
                    message = getattr(first_choice, "message", None)
                    content = getattr(message, "content", None)

            if not content:
                return build_ai_no_result("empty_assistant_content", retry_attempted=retry_attempted)

            try:
                # Try parsing as-is first; fallback sanitizes double braces (nano artifact)
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    content = content.replace("{{", "{").replace("}}", "}")
                    parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    return build_ai_no_result("malformed_route_response")
                parsed_obj: dict[str, object] = {str(key): value for key, value in parsed.items()}

                # Reject stale command envelopes; native tool_call is the only action contract.
                resp_type = str(parsed_obj.get("type", ""))
                if parsed_obj.get("type") == "command":
                    return build_ai_no_result(
                        "legacy_command_envelope_rejected",
                        response_preview=content[:200],
                        retryable=True,
                    )
                if parsed_obj.get("type") == "tool_use":
                    parsed_obj["type"] = "tool_call"
                if parsed_obj.get("type") == "tool_call":
                    if "tool" not in parsed_obj and "command" in parsed_obj:
                        parsed_obj["tool"] = parsed_obj.get("command", "")
                    if "args" not in parsed_obj and "params" in parsed_obj:
                        parsed_obj["args"] = parsed_obj.get("params", {})
                    if not parsed_obj.get("tool"):
                        return build_ai_no_result("missing_tool_name")
                    if "args" not in parsed_obj or not isinstance(parsed_obj["args"], dict):
                        parsed_obj["args"] = {}
                    return parsed_obj

                # Validate answer type response
                if parsed_obj.get("type") == "answer":
                    if not parsed_obj.get("answer"):
                        return build_ai_no_result("empty_answer_content")
                    return parsed_obj

                # Unknown type, treat as answer
                return build_ai_no_result(
                    "unrecognized_route_response_type",
                    response_type=resp_type or None,
                )

            except json.JSONDecodeError:
                return build_ai_no_result("malformed_route_response")

        except Exception as e:
            from openai import BadRequestError

            # Handle context-length overflow with a graceful retry on trimmed history
            if isinstance(e, BadRequestError):
                is_context_overflow = (hasattr(e, "code") and e.code == "context_length_exceeded") or (
                    hasattr(e, "status_code") and e.status_code == 400 and "context_length" in str(e).lower()
                )
                if is_context_overflow:
                    logger.warning("Context length exceeded in route_command(); trimming history and retrying once")
                    # Keep system prompt + last 3 history messages + current user turn
                    trimmed_messages = [messages[0]] if messages and messages[0].get("role") == "system" else []
                    history_msgs = [m for m in messages[1:-1] if m.get("role") != "system"]
                    trimmed_messages.extend(history_msgs[-3:])
                    trimmed_messages.append(messages[-1])
                    try:
                        is_o1 = self._uses_max_completion_tokens(model)
                        retry_kwargs: dict[str, object] = {
                            "model": model,
                            "messages": trimmed_messages,
                            "max_tokens": max_tokens,
                            "temperature": 0.3,
                        }
                        if not is_o1:
                            retry_kwargs["response_format"] = {"type": "json_object"}
                        response = await self._api_call_with_retry(**retry_kwargs)
                        choices = getattr(response, "choices", None)
                        if choices:
                            content = getattr(getattr(choices[0], "message", None), "content", None)
                            if content:
                                try:
                                    try:
                                        parsed = json.loads(content)
                                    except json.JSONDecodeError:
                                        content = content.replace("{{", "{").replace("}}", "}")
                                        parsed = json.loads(content)
                                    if isinstance(parsed, dict):
                                        return {str(k): v for k, v in parsed.items()}
                                except json.JSONDecodeError:
                                    pass
                    except Exception:
                        logger.debug(
                            "Context-length retry failed in route_command(); returning user-friendly message",
                            exc_info=True,
                        )
                    return {
                        "type": "answer",
                        "answer": "Your message was a bit too long for me to process. Try breaking it into shorter questions.",
                        "_error_category": "CONTEXT_LENGTH_EXCEEDED",
                    }
                # Other 400 errors (unsupported model param, invalid request, etc.)
                logger.error("BadRequestError in route_command(): %s", e)
                return build_ai_no_result(
                    "route_command_bad_request",
                    retryable=False,
                    error_category="BAD_REQUEST",
                )

            logger.exception("GPT route_command failed")

            from diagnostics.error_classification import categorize_exception

            category = categorize_exception(e)
            return build_ai_error_no_result(
                "route_command",
                category_name=category.name,
                exception=e,
            )

    # ------------------------------------------------------------------
    # Streaming route_command for voice TTS overlap
    # ------------------------------------------------------------------

    def _build_stream_chunk_aggregator(
        self,
        *,
        task_trace: Any | None = None,
        attempt_id: str | None = None,
    ) -> StreamChunkAggregator | None:
        trace_writer = task_trace or getattr(self, "_task_trace", None)
        if trace_writer is None:
            return None

        resolved_attempt_id = attempt_id
        if not resolved_attempt_id:
            stream_seq = getattr(self, "_task_trace_stream_attempt_seq", 0) + 1
            self._task_trace_stream_attempt_seq = stream_seq
            resolved_attempt_id = "%s:llm_stream:%04d" % (
                getattr(trace_writer, "task_id", "unknown"),
                stream_seq,
            )

        return StreamChunkAggregator(attempt_id=str(resolved_attempt_id), task_trace=trace_writer)

    async def route_command_streaming(
        self,
        user_request: str,
        history: list[Mapping[str, object]] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 600,
        *,
        task_trace: Any | None = None,
        attempt_id: str | None = None,
    ):
        """Stream a route_command response, yielding answer text tokens as they arrive.

        For answer-type responses from the JSON-in-prompt path, tokens inside
        the ``"answer"`` field are yielded as ``{"token": "..."}`` dicts while
        the LLM is still generating.  The final ``{"done": True, ...}`` dict
        contains the full parsed response.

        For command/tool_call/native-tool responses the full result is
        collected and yielded as a single ``{"done": True, ...}`` dict
        (streaming is not beneficial for these).

        Yields:
            Dicts: ``{"token": str}`` for incremental answer text, then a
            final ``{"done": True, "type": ..., ...}`` with the full result.
        """
        self._warn_ignored_history_arg(history, "route_command_streaming")
        self.cleanup_old_history()

        normalized = user_request.strip().lower()
        if not normalized:
            yield {"done": True, **build_ai_no_result("empty_user_request", retryable=False)}
            return

        # Native tools and agent mode were removed from this legacy path.
        native_tools = self._native_tools
        agent_prompt = self._agent_system_prompt
        if native_tools or agent_prompt:
            raise RuntimeError(
                "GptHandler agent/native routing was removed; use LLMProviderFactory/OpenAICompatibleProvider"
            )

        # Offline fast path (tests with sk-test keys)
        api_call = getattr(self, "_api_call_with_retry", None)
        api_call_is_unpatched = getattr(api_call, "__func__", None) is GptHandler._api_call_with_retry
        if api_call_is_unpatched and self._api_key.startswith("sk-test"):
            result = await self.route_command(
                user_request,
                context_bundle=context_bundle,
                max_tokens=max_tokens,
            )
            result["done"] = True
            yield result
            return

        model = self._get_model()

        rendered_prompt = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                user_text=user_request,
                native_tools=False,
                response_contract=_structured_route_response_contract(),
            )
        )
        messages = _rendered_openai_to_chat_messages(rendered_prompt)

        stream_capture: StreamChunkAggregator | None = None
        try:
            max_tokens = clamp_max_tokens(max_tokens, default=600)

            api_kwargs = self._build_responses_api_kwargs(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
                stream=True,
                response_format={"type": "json_object"},
            )

            import openai as _openai_mod

            client = (
                self.client
                if getattr(self, "client", None) is not None
                else _openai_mod.AsyncOpenAI(api_key=self._api_key)
            )
            start_time = time.time()
            collected: list[str] = []
            completed_response: Any | None = None
            stream_capture = self._build_stream_chunk_aggregator(
                task_trace=task_trace,
                attempt_id=attempt_id,
            )

            # --- Incremental JSON answer extraction ---
            # The LLM response is JSON like:
            #   {"type": "answer", "answer": "The capital...", ...}
            # We detect when we're inside the "answer" value and yield
            # those tokens immediately for TTS streaming.
            in_answer_value = False
            answer_tokens: list[str] = []
            # Simple state machine: track accumulated JSON to detect the
            # "answer": " prefix, then yield everything until the closing quote.
            raw_so_far = ""
            _ANSWER_KEY_MARKER = '"answer":'
            _answer_value_started = False
            _answer_string_depth = 0  # track escaped chars
            _answer_complete = False

            # SEC-08: enforce per-user storage consent.
            from services.llm.openai_consent import enforce_storage_consent

            enforce_storage_consent(api_kwargs)
            stream = await client.responses.create(**api_kwargs)
            async for event in stream:
                if stream_capture is not None:
                    capture_openai_responses_stream_event(stream_capture, event)
                event_type = _get_attr_or_item(event, "type", "")
                if event_type == "response.completed":
                    completed_response = _get_attr_or_item(event, "response")
                    continue
                if event_type in {"response.failed", "response.incomplete", "error"}:
                    raise RuntimeError("OpenAI Responses stream failed: %s" % event_type)
                if event_type != "response.output_text.delta":
                    continue

                token = str(_get_attr_or_item(event, "delta", "") or "")
                if not token:
                    continue
                collected.append(token)
                raw_so_far += token

                # --- Answer value extraction state machine ---
                if _answer_complete:
                    # Already finished extracting answer, just collect
                    continue

                if not in_answer_value:
                    # Check if we've reached the answer value
                    # Look for "answer": followed by a quote
                    if _ANSWER_KEY_MARKER in raw_so_far:
                        # Find position after the marker
                        marker_end = raw_so_far.index(_ANSWER_KEY_MARKER) + len(_ANSWER_KEY_MARKER)
                        after_marker = raw_so_far[marker_end:].lstrip()
                        if after_marker.startswith('"'):
                            in_answer_value = True
                            # Extract any text after the opening quote
                            text_start = raw_so_far.index('"', marker_end) + 1
                            initial_text = raw_so_far[text_start:]
                            # Check if the answer value is already complete
                            # (single token contained the entire answer)
                            unescaped_end = _find_unescaped_quote(initial_text)
                            if unescaped_end >= 0:
                                # Complete answer in buffer
                                answer_text = initial_text[:unescaped_end]
                                answer_text = answer_text.replace('\\"', '"').replace("\\n", "\n")
                                if answer_text:
                                    answer_tokens.append(answer_text)
                                    yield {"token": answer_text}
                                _answer_complete = True
                            elif initial_text:
                                decoded = initial_text.replace('\\"', '"').replace("\\n", "\n")
                                if decoded:
                                    answer_tokens.append(decoded)
                                    yield {"token": decoded}
                            _answer_value_started = True
                    continue

                # We're inside the answer string value
                # Check if this token contains the closing quote
                unescaped_end = _find_unescaped_quote(token)
                if unescaped_end >= 0:
                    # Token contains closing quote — extract text before it
                    final_part = token[:unescaped_end]
                    if final_part:
                        decoded = final_part.replace('\\"', '"').replace("\\n", "\n")
                        if decoded:
                            answer_tokens.append(decoded)
                            yield {"token": decoded}
                    _answer_complete = True
                    in_answer_value = False
                else:
                    # Pure answer text token
                    decoded = token.replace('\\"', '"').replace("\\n", "\n")
                    if decoded:
                        answer_tokens.append(decoded)
                        yield {"token": decoded}

            # --- Parse the complete JSON response ---
            full_content = "".join(collected)
            if not full_content and completed_response is not None:
                full_content = _extract_responses_text(completed_response)
            latency_ms = round((time.time() - start_time) * 1000)
            self.last_latency_ms = latency_ms
            if stream_capture is not None:
                usage = getattr(completed_response, "usage", None) if completed_response is not None else None
                stream_capture.emit_terminal("usage", summarize_usage(usage))
                stream_capture.emit_terminal("stop", summarize_openai_response(completed_response))

            if not full_content:
                result = await self.route_command(
                    user_request,
                    context_bundle=context_bundle,
                    max_tokens=max_tokens,
                )
                result["done"] = True
                yield result
                return

            try:
                try:
                    parsed = json.loads(full_content)
                except json.JSONDecodeError:
                    full_content = full_content.replace("{{", "{").replace("}}", "}")
                    parsed = json.loads(full_content)
            except json.JSONDecodeError:
                if answer_tokens:
                    yield {
                        "done": True,
                        "type": "answer",
                        "answer": "".join(answer_tokens),
                    }
                    return
                yield {
                    "done": True,
                    **build_ai_no_result("malformed_streaming_route_response"),
                }
                return

            if not isinstance(parsed, dict):
                yield {
                    "done": True,
                    **build_ai_no_result("malformed_streaming_route_response"),
                }
                return

            parsed_obj: dict[str, object] = {str(k): v for k, v in parsed.items()}

            # Reject stale command envelopes; native tool_call is the only action contract.
            resp_type = str(parsed_obj.get("type", ""))
            if resp_type == "command":
                yield {
                    "done": True,
                    **build_ai_no_result(
                        "legacy_command_envelope_rejected",
                        response_preview=full_content[:200],
                        retryable=True,
                    ),
                }
                return
            if resp_type == "tool_use":
                parsed_obj["type"] = "tool_call"
            if parsed_obj.get("type") == "tool_call":
                if "tool" not in parsed_obj and "command" in parsed_obj:
                    parsed_obj["tool"] = parsed_obj.get("command", "")
                if "args" not in parsed_obj and "params" in parsed_obj:
                    parsed_obj["args"] = parsed_obj.get("params", {})
                if "args" not in parsed_obj or not isinstance(parsed_obj["args"], dict):
                    parsed_obj["args"] = {}

            # Record conversation for answer-type
            if str(parsed_obj.get("type", "")) == "answer":
                answer_text = str(parsed_obj.get("answer", ""))
                if answer_text:
                    self._conversation_manager.add_message("user", user_request)
                    self._conversation_manager.add_message("assistant", answer_text)

            parsed_obj["done"] = True
            yield parsed_obj

        except Exception as e:
            logger.exception("Streaming route_command failed")
            self.last_error = mask_secrets_in_text(str(e))
            if stream_capture is not None:
                stream_capture.emit_terminal("error", summarize_stream_error(e))
            try:
                result = await self.route_command(
                    user_request,
                    context_bundle=context_bundle,
                    max_tokens=max_tokens,
                )
                result["done"] = True
                yield result
            except Exception as retry_exc:
                logger.debug("Streaming route_command non-stream retry failed", exc_info=True)
                yield {
                    "done": True,
                    **build_ai_no_result(
                        "streaming_route_command_failed",
                        retry_attempted=True,
                        exception_type=type(retry_exc).__name__,
                    ),
                }

    # ------------------------------------------------------------------
    # Native tool calling removed
    # ------------------------------------------------------------------

    @property
    def route_command_native(self) -> Any:
        """Hide the removed native agent route from hasattr/getattr probes."""
        raise AttributeError("GptHandler native routing was removed; use LLMProviderFactory/OpenAICompatibleProvider")

    async def gpt_autoplay_suggest(
        self,
        history_tail: list[dict[str, str]],
        max_suggestions: int = 5,
    ) -> list[str]:
        """
        Get GPT suggestions for autoplay.

        Args:
            history_tail: Recent play history with 'id', 'title', 'artist'
            max_suggestions: Maximum number of suggestions

        Returns:
            List of video IDs
        """
        if not history_tail:
            return []

        model = self._get_model()

        # Build prompt
        history_str = "\n".join(
            f"- {item.get('title', 'Unknown')} by {item.get('artist', 'Unknown')}" for item in history_tail[-5:]
        )

        prompt = f"""Based on the user's recent listening history:
{history_str}

Suggest {max_suggestions} similar songs that would be good to play next.
Return as JSON: {{"video_ids": ["id1", "id2", ...]}}
Only return valid YouTube video IDs (11 characters).
"""

        messages: list[dict[str, str]] = [
            {"role": "system", "content": "You are a music recommendation assistant."},
            {"role": "user", "content": prompt},
        ]

        try:
            is_o1 = self._uses_max_completion_tokens(model)
            if is_o1:
                response = await self._api_call_with_retry(
                    model=model,
                    messages=messages,
                    max_tokens=200,
                    temperature=0.7,
                )
            else:
                response = await self._api_call_with_retry(
                    model=model,
                    messages=messages,
                    max_tokens=200,
                    temperature=0.7,
                    response_format={"type": "json_object"},
                )

            choices = getattr(response, "choices", None)
            if not choices:
                return []

            first_choice = choices[0]
            message = getattr(first_choice, "message", None)
            content = getattr(message, "content", None)
            if not content:
                return []

            try:
                parsed = json.loads(content)
                video_ids = parsed.get("video_ids", [])
                # Filter out empty/invalid IDs
                return [vid for vid in video_ids if vid and vid.strip()]
            except json.JSONDecodeError:
                return []

        except Exception as e:
            logger.exception("GPT autoplay suggest failed: %s", mask_secrets_in_text(str(e)))
            return []

    def clear_history(self):
        """Clear stored frame context for this handler's manager."""
        self._warn_legacy_history_access("clear_history")
        self._conversation_manager.clear_history()

    def get_history(self) -> list[dict[str, str]]:
        """Return a role/content compatibility view backed by stored frames."""
        self._warn_legacy_history_access("get_history")
        messages: list[dict[str, str]] = []
        for message in self._conversation_manager.get_history():
            role = message.get("role")
            content = message.get("content")
            if isinstance(role, str) and isinstance(content, str):
                messages.append({"role": role, "content": content})
        return messages

    def _add_to_history(self, role: str, content: str) -> None:
        """Compatibility shim for tests that append directly to role/content state."""
        self._warn_legacy_history_access("_add_to_history")
        self._conversation_manager.add_message(role, content)

    @property
    def _conversation_manager(self) -> ConversationStateManager:
        """Return the per-request manager or the default.

        Mirrors the pipeline + ai_controller resolution so every
        internal access to ``self._conversation_manager.X(...)``
        (history reads, add_message, clear_history) targets the
        correct user's store when called inside a ``use_request_manager``
        scope — even when this handler instance is shared across
        tenants via a shared pipeline (CHAN-R1).
        """
        from services.conversation.state_manager import get_request_conversation_manager

        requested = get_request_conversation_manager()
        if requested is not None:
            return requested
        return self._default_conversation_manager

    @_conversation_manager.setter
    def _conversation_manager(self, manager: ConversationStateManager) -> None:
        """Back-compat setter — writes the default slot.

        Legacy callers that do ``handler._conversation_manager = mgr``
        are treated as updating the default (as if they had called
        ``set_conversation_manager``), so the per-request override still
        wins when a request is active.
        """
        self._default_conversation_manager = manager

    def set_conversation_manager(self, manager: ConversationStateManager) -> None:
        """Inject the default conversation state manager.

        The per-request manager published via ``use_request_manager``
        still overrides this at runtime (CHAN-R1). The instance-level
        the warned role/content compatibility view resolves the current
        manager on every access, so re-binding it here would be redundant.
        """
        self._default_conversation_manager = manager

    def cleanup_old_history(self) -> int:
        """
        Clean up old history entries.

        Returns:
            Number of entries removed (always 0 in current impl)
        """
        # Current implementation doesn't use timestamps,
        # but deque already limits size
        return 0

    def get_api_key_info(self) -> dict[str, object]:
        """
        Get information about the current API key.

        Returns:
            Dict with 'source', 'masked_key', 'active'
        """
        return {
            "source": self._api_key_source,
            "masked_key": self._mask_key(self._api_key),
            "active": bool(self._api_key),
        }

    # ------------------------------------------------------------------
    # B6: Streaming LLM Responses
    # ------------------------------------------------------------------

    async def ask_streaming(
        self,
        query: str,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
        *,
        task_trace: Any | None = None,
        attempt_id: str | None = None,
    ):
        """Stream a response from OpenAI token-by-token.

        Yields dicts with either ``{"token": "..."}`` for content chunks,
        ``{"tool_call_chunk": {...}}`` for streaming tool call fragments,
        or ``{"done": True, "tokens_used": N}`` when the stream completes.

        For non-streaming contexts (voice TTS), use ``ask()`` instead.

        Args:
            query: User's question.
            system_prompt: Optional system prompt override.
            include_history: Whether to include conversation history.
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Yields:
            Dicts representing stream events.
        """
        model = self._get_model()
        max_tokens = clamp_max_tokens(max_tokens)

        context_bundle = self._manager_context_bundle() if include_history else None
        rendered_prompt = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                user_text=query,
            )
        )
        messages: list[dict[str, str]] = _rendered_openai_to_chat_messages(rendered_prompt)
        if system_prompt:
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = system_prompt
            else:
                messages.insert(0, {"role": "system", "content": system_prompt})

        api_kwargs = self._build_responses_api_kwargs(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )

        start_time = time.time()
        collected_content: list[str] = []
        # Accumulate tool call fragments for streaming tool calls
        tool_call_accum: dict[int, dict[str, Any]] = {}
        stream_capture = self._build_stream_chunk_aggregator(
            task_trace=task_trace,
            attempt_id=attempt_id,
        )

        try:
            # SEC-08: enforce per-user storage consent.
            from services.llm.openai_consent import enforce_storage_consent

            enforce_storage_consent(api_kwargs)
            stream = await self.client.responses.create(**api_kwargs)
            completed_response: Any | None = None
            async for event in stream:
                if stream_capture is not None:
                    capture_openai_responses_stream_event(stream_capture, event)
                event_type = _get_attr_or_item(event, "type", "")
                if event_type == "response.completed":
                    completed_response = _get_attr_or_item(event, "response")
                    continue
                if event_type in {"response.failed", "response.incomplete", "error"}:
                    raise RuntimeError("OpenAI Responses stream failed: %s" % event_type)

                # Content token
                if event_type == "response.output_text.delta":
                    delta = str(_get_attr_or_item(event, "delta", "") or "")
                    if delta:
                        collected_content.append(delta)
                        yield {"token": delta}
                    continue

                # Streaming tool call fragments
                if event_type == "response.function_call_arguments.delta":
                    idx = _safe_int(_get_attr_or_item(event, "output_index", 0))
                    item_id = str(_get_attr_or_item(event, "item_id", "") or "")
                    if idx not in tool_call_accum:
                        tool_call_accum[idx] = {"id": item_id, "name": "", "arguments": ""}
                    if item_id:
                        tool_call_accum[idx]["id"] = item_id
                    tool_call_accum[idx]["arguments"] += str(_get_attr_or_item(event, "delta", "") or "")
                    yield {"tool_call_chunk": tool_call_accum[idx]}
                    continue

                if event_type in {"response.output_item.added", "response.output_item.done"}:
                    item = _get_attr_or_item(event, "item")
                    if _get_attr_or_item(item, "type") != "function_call":
                        continue
                    idx = _safe_int(_get_attr_or_item(event, "output_index", 0))
                    call_id = str(_get_attr_or_item(item, "call_id", "") or _get_attr_or_item(item, "id", "") or "")
                    current = tool_call_accum.setdefault(idx, {"id": call_id, "name": "", "arguments": ""})
                    if call_id:
                        current["id"] = call_id
                    current["name"] = str(_get_attr_or_item(item, "name", "") or current["name"])
                    arguments = str(_get_attr_or_item(item, "arguments", "") or "")
                    if arguments:
                        current["arguments"] = arguments
                    if event_type == "response.output_item.done":
                        yield {"tool_call_chunk": current}

            latency_ms = round((time.time() - start_time) * 1000)
            self.last_latency_ms = latency_ms
            if stream_capture is not None:
                usage = getattr(completed_response, "usage", None) if completed_response is not None else None
                stream_capture.emit_terminal("usage", summarize_usage(usage))
                stream_capture.emit_terminal("stop", summarize_openai_response(completed_response))

            full_content = "".join(collected_content)
            if not full_content and completed_response is not None:
                full_content = _extract_responses_text(completed_response)
            if full_content:
                self._conversation_manager.add_message("user", query)
                self._conversation_manager.add_message("assistant", full_content)

            usage = getattr(completed_response, "usage", None) if completed_response is not None else None
            token_count = (
                _safe_int(getattr(usage, "total_tokens", 0)) if usage is not None else len(full_content.split())
            )
            yield {
                "done": True,
                "content": full_content,
                "tokens_used": token_count,
                "latency_ms": latency_ms,
                "tool_calls": list(tool_call_accum.values()) if tool_call_accum else None,
            }

        except Exception as e:
            logger.exception("Streaming ask failed")
            self.last_error = mask_secrets_in_text(str(e))
            if stream_capture is not None:
                stream_capture.emit_terminal("error", summarize_stream_error(e))
            yield {
                "error": True,
                "message": "Streaming failed: %s" % type(e).__name__,
            }
