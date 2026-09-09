"""
Anthropic LLM Provider

Native Claude API support for Anthropic models.

Mechanism parity with Claude Code's TypeScript reference
(``src/services/api/claude.ts``) at the wire layer:

- Streaming-first request path with content-block state machine
  (see :mod:`services.llm.anthropic_stream`).
- Idle-timeout watchdog; on stream failure or idle timeout, bounded
  non-streaming recovery via
  :func:`services.llm.anthropic_cache.adjust_params_for_non_streaming`.
- System prompt + tool schemas + message cache breakpoints applied through
  :func:`services.llm.anthropic_cache.apply_anthropic_cache_controls`.
- ``thinking`` / ``redacted_thinking`` content blocks are preserved on
  parse so a future extended-thinking session round-trips correctly
  (Opus-G12 parity).

Provider plurality (Anthropic + OpenAI direct + Codex + Google + Ollama)
is Viola-unique; only the Anthropic *mechanism* tracks Claude. Other
providers are untouched.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, cast

from core.logging_config import get_logger
from services.conversation.frame_rendering import render_for_anthropic
from services.llm.anthropic_cache import (
    AnthropicCachePolicy,
    adjust_params_for_non_streaming,
    apply_anthropic_cache_controls,
)
from services.llm.anthropic_stream import (
    StreamConsumerError,
    consume_stream,
)
from services.llm.model_fallback import FALLBACK_MODEL
from services.llm.no_result import build_ai_no_result
from services.llm.prompts import build_provider_prompt_bundle
from services.llm.providers.base import BaseLLMProvider, LLMConfig, LLMTestResult
from services.llm.request_policy import DEFAULT_MAX_RETRIES, ProviderRequestContext, execute_with_policy, new_request_id
from services.llm.token_limits import clamp_max_tokens

logger = get_logger(__name__)

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle


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


def _load_anthropic_module() -> ModuleType | None:
    try:
        return importlib.import_module("anthropic")
    except ImportError:
        return None


_ANTHROPIC_MODULE = _load_anthropic_module()
ANTHROPIC_AVAILABLE = _ANTHROPIC_MODULE is not None


def _web_search_requests_from_usage(usage: Any) -> int:
    if usage is None:
        return 0
    server_tool = getattr(usage, "server_tool_use", None)
    value = (
        server_tool.get("web_search_requests")
        if isinstance(server_tool, dict)
        else getattr(server_tool, "web_search_requests", None)
    )
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# Type-only SDK contracts kept to document the Anthropic client shape.
class _AnthropicUsage(Protocol):
    input_tokens: int | None
    output_tokens: int | None


class _AnthropicMessageResponse(Protocol):
    stop_reason: str | None
    content: list[Any] | None
    usage: _AnthropicUsage | None


class _AnthropicMessagesResource(Protocol):
    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list,
        system: str | None = None,
        temperature: float | None = None,
        tools: list[dict] | None = None,
    ) -> _AnthropicMessageResponse: ...


def _normalize_json_schema(schema: Any) -> dict[str, Any]:
    input_schema = dict(schema) if isinstance(schema, Mapping) else {}
    if "type" not in input_schema:
        input_schema["type"] = "object"
    if "properties" not in input_schema:
        input_schema["properties"] = {}
    if "required" not in input_schema and input_schema["properties"]:
        input_schema["required"] = list(input_schema["properties"].keys())
    return input_schema


def _anthropic_tool_from_any(tool: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a custom Anthropic tool definition from common local formats."""

    function = tool.get("function") if isinstance(tool.get("function"), Mapping) else None
    if function:
        name = function.get("name")
        description = function.get("description", "")
        schema = function.get("parameters") or function.get("input_schema") or function.get("inputSchema")
    else:
        name = tool.get("name")
        description = tool.get("description", "")
        schema = tool.get("input_schema") or tool.get("inputSchema") or tool.get("parameters")

    if not isinstance(name, str) or not name.strip():
        return None

    converted = {
        "name": name.strip(),
        "description": str(description or ""),
        "input_schema": _normalize_json_schema(schema),
    }
    source = function or tool
    if isinstance(source.get("strict"), bool):
        converted["strict"] = bool(source["strict"])
    if isinstance(source.get("defer_loading"), bool):
        converted["defer_loading"] = bool(source["defer_loading"])
    if isinstance(source.get("eager_input_streaming"), bool):
        converted["eager_input_streaming"] = bool(source["eager_input_streaming"])
    cache_control = source.get("cache_control")
    if isinstance(cache_control, Mapping):
        converted["cache_control"] = dict(cache_control)
    return converted


def _anthropic_tool_choice_from_any(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        normalized = choice.strip().lower()
        if normalized in {"auto", "any", "none"}:
            return {"type": normalized}
        if choice.strip():
            return {"type": "tool", "name": choice.strip()}
        return None
    if not isinstance(choice, Mapping):
        return None
    choice_type = str(choice.get("type") or "").strip().lower()
    if choice_type in {"auto", "any", "none"}:
        return {"type": choice_type}
    if choice_type == "tool":
        name = str(choice.get("name") or "").strip()
        return {"type": "tool", "name": name} if name else None
    if choice_type == "function":
        function = choice.get("function")
        name = str(function.get("name") if isinstance(function, Mapping) else "").strip()
        return {"type": "tool", "name": name} if name else None
    name = str(choice.get("name") or "").strip()
    return {"type": "tool", "name": name} if name else None


def _openai_assistant_to_anthropic_content(content: Mapping[str, Any]) -> str | list[dict[str, Any]]:
    """Convert a canonical ``_openai_assistant`` turn to Anthropic content.

    The agent loop reconstructs every provider's assistant tool-call turn into
    one canonical ``{"_openai_assistant": True, "tool_calls": [...]}`` dict
    (``intent/agent_loop.py`` ``_openai_assistant`` synthesis) before appending
    it to the running message list. The Anthropic Messages API only accepts a
    string or a list of content blocks, so this dict must be translated to
    ``text`` + ``tool_use`` blocks — exactly as the Google and Ollama adapters
    already do for the same input. Passing the dict through unconverted sends
    invalid content to ``messages.create`` and breaks every multi-turn
    Anthropic tool call at iteration 2.

    A valid, already-native message (a plain string or a list of blocks) never
    reaches this helper — the caller only routes ``_openai_assistant`` dicts
    here — so the conversion can only repair invalid input, never alter valid
    input.
    """
    blocks: list[dict[str, Any]] = []
    text = content.get("content")
    if isinstance(text, str) and text.strip():
        blocks.append({"type": "text", "text": text})
    for tool_call in content.get("tool_calls") or []:
        if not isinstance(tool_call, Mapping):
            continue
        function = tool_call.get("function")
        if not isinstance(function, Mapping):
            continue
        raw_args = function.get("arguments")
        if isinstance(raw_args, Mapping):
            args: Any = dict(raw_args)
        else:
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
        block: dict[str, Any] = {
            "type": "tool_use",
            "id": str(tool_call.get("id") or ""),
            "name": str(function.get("name") or ""),
            "input": args if isinstance(args, dict) else {},
        }
        blocks.append(block)
    if blocks:
        return blocks
    # No tool calls and no meaningful text: fall back to a text block so the
    # turn stays a structurally valid assistant message rather than an empty
    # dict the API would reject.
    return text if isinstance(text, str) else ""


def _normalize_openai_assistant_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate canonical ``_openai_assistant`` assistant turns to Anthropic.

    Only assistant messages whose ``content`` is an ``_openai_assistant`` dict
    are rewritten; every other message (native string / block-list content,
    tool_result user turns) is passed through by reference. Returns a new list
    only when a rewrite happened, otherwise the original list.
    """
    if not isinstance(messages, list):
        return messages
    rewritten: list[dict[str, Any]] | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, Mapping) and content.get("_openai_assistant"):
            if rewritten is None:
                rewritten = list(messages)
            new_message = dict(message)
            new_message["content"] = _openai_assistant_to_anthropic_content(content)
            rewritten[index] = new_message
    return rewritten if rewritten is not None else messages


def _tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize MCP, OpenAI function, or Anthropic-like tools for Claude."""

    result: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, Mapping):
            converted = _anthropic_tool_from_any(tool)
            if converted is not None:
                result.append(converted)
    return result


def _mcp_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert MCP tool schemas to Anthropic native tool format.

    MCP tools have ``inputSchema``; Anthropic expects ``input_schema``.
    Ensures every schema has ``type: object`` at the top level and
    that ``required`` is set for properties without defaults.

    Args:
        tools: List of tool dicts from ``MCPClientHub.list_tools()``.

    Returns:
        List of Anthropic-format tool definitions.
    """
    return _tools_to_anthropic(tools)


def _anthropic_cache_policy_from_kwargs(kwargs: Mapping[str, Any] | None = None) -> AnthropicCachePolicy:
    """Build request-local Anthropic cache policy from provider kwargs."""

    def _tuple_or_single_mapping(value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if isinstance(value, Mapping):
            return (value,)
        return tuple(value)

    if not kwargs:
        return AnthropicCachePolicy()

    raw_policy = kwargs.get("anthropic_cache_policy")
    if isinstance(raw_policy, AnthropicCachePolicy):
        return raw_policy
    if raw_policy is False:
        return AnthropicCachePolicy(enabled=False)
    if isinstance(raw_policy, Mapping):
        allowed = {
            "enabled",
            "static_prompt",
            "compact_summary",
            "long_lived_meta",
            "tool_schemas",
            "ttl",
            "scope",
            "cache_edits",
            "pinned_cache_edits",
            "skip_cache_write",
        }
        policy_kwargs = {key: value for key, value in raw_policy.items() if key in allowed}
        return AnthropicCachePolicy(**policy_kwargs)

    cache_edits = kwargs.get("anthropic_cache_edits", kwargs.get("cache_edits"))
    pinned_edits = kwargs.get("anthropic_pinned_cache_edits", kwargs.get("pinned_cache_edits"))
    skip_cache_write = bool(kwargs.get("anthropic_skip_cache_write", False))
    if cache_edits or pinned_edits or skip_cache_write:
        return AnthropicCachePolicy(
            cache_edits=_tuple_or_single_mapping(cache_edits),
            pinned_cache_edits=_tuple_or_single_mapping(pinned_edits),
            skip_cache_write=skip_cache_write,
        )

    return AnthropicCachePolicy()


class _AnthropicAsyncClient(Protocol):
    messages: _AnthropicMessagesResource


def _create_async_client(api_key: str) -> _AnthropicAsyncClient:
    if _ANTHROPIC_MODULE is None:
        raise RuntimeError("Anthropic package not installed")

    client_cls_obj: object = getattr(_ANTHROPIC_MODULE, "AsyncAnthropic", None)
    if client_cls_obj is None or not callable(client_cls_obj):
        raise RuntimeError("Anthropic AsyncAnthropic client not available")

    client_factory = cast(Callable[..., object], client_cls_obj)
    client = client_factory(api_key=api_key)
    return cast(_AnthropicAsyncClient, client)


def _is_anthropic_exception(exc: BaseException, class_name: str) -> bool:
    if _ANTHROPIC_MODULE is None:
        return False

    exc_type_obj: object = getattr(_ANTHROPIC_MODULE, class_name, None)
    if not isinstance(exc_type_obj, type):
        return False

    if not issubclass(exc_type_obj, BaseException):
        return False

    return isinstance(exc, exc_type_obj)


def _strip_llm_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM response text.

    Handles ``\\`\\`\\`json ... \\`\\`\\`` wrapping as well as partial/trailing
    fences.  Returns cleaned text ready for JSON parsing.
    """
    import re as _re

    content = text.strip()
    # Full fence: ```json\n...\n```  or  ```\n...\n```
    # Use a regex so we handle newlines and whitespace flexibly
    m = _re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", content, _re.DOTALL)
    if m:
        return m.group(1).strip()
    # Leading fence only (no closing ```)
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    # Trailing fence only
    if content.endswith("```"):
        content = content[:-3]
    # Trailing incomplete fence fragment (e.g. ``` json at end)
    content = _re.sub(r"\s*```(?:json)?\s*$", "", content)
    return content.strip()


def _policy_fallback_model(model_name: str) -> str | None:
    """Return the model-level fallback unless the request is already on it."""

    effective_model = str(model_name or "").strip()
    return FALLBACK_MODEL if effective_model and effective_model != FALLBACK_MODEL else None


class AnthropicProvider(BaseLLMProvider):
    """
    LLM provider for Anthropic's Claude models.

    Supports Claude 4.5/4.6 (Haiku, Sonnet, Opus) and Claude 3.5/3 families.
    Uses native tool_use API for structured tool calling in agent mode.
    """

    SUPPORTS_NATIVE_TOOLS = True
    NATIVE_TOOL_FORMAT = "anthropic"

    # Available Claude models
    AVAILABLE_MODELS = [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-6",
        "claude-sonnet-4-20250514",
        "claude-3-5-sonnet-20241022",
        "claude-3-haiku-20240307",
    ]

    def __init__(self, config: LLMConfig):
        """
        Initialize Anthropic provider.

        Args:
            config: LLM configuration with api_key and model
        """
        super().__init__(config)
        self._client: _AnthropicAsyncClient | None = None
        # B3/COST-1: Pending settle info — set by callers so the provider can
        # call settle() after each API response with actual token counts.
        self._settle_user_id: str | None = None
        self._settle_estimated_tokens: int = 0

        if not ANTHROPIC_AVAILABLE:
            self.last_error = "Anthropic package not installed. Run: pip install anthropic"
            return

        # Validate API key
        if not config.api_key:
            self.last_error = "Anthropic API key is required"
            return

        # Create async client
        try:
            self._client = _create_async_client(api_key=config.api_key)
            logger.info("Anthropic provider initialized: model=%s", config.model)
        except Exception as e:
            self.last_error = f"Failed to initialize Anthropic client: {e}"
            self._client = None
            logger.error(self.last_error)

    def is_available(self) -> bool:
        """Check if provider is available."""
        return ANTHROPIC_AVAILABLE and self._client is not None and bool(self.config.api_key)

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
                "Anthropic rate-limiter settle failed (reserved tokens leaked) user_id=%s est_tokens=%d actual_tokens=%d",
                uid,
                est,
                actual_tokens,
            )

    def get_available_models(self) -> list[str]:
        """Get list of available Claude models."""
        return self.AVAILABLE_MODELS.copy()

    # ------------------------------------------------------------------
    # Claude-parity wire layer: stream-first request path + helpers
    # ------------------------------------------------------------------

    def _prompt_caching_enabled(self, model: str | None = None) -> bool:
        """Mirror Claude's ``getPromptCachingEnabled``.

        Disabled via ``VIOLA_ANTHROPIC_DISABLE_PROMPT_CACHING`` env var.
        Default: enabled.
        """
        _ = model  # reserved for per-model overrides
        raw = os.environ.get("VIOLA_ANTHROPIC_DISABLE_PROMPT_CACHING", "").strip().lower()
        return raw not in {"1", "true", "yes", "on"}

    def _streaming_enabled(self) -> bool:
        """Streaming is on by default; off via ``VIOLA_ANTHROPIC_DISABLE_STREAMING``."""
        raw = os.environ.get("VIOLA_ANTHROPIC_DISABLE_STREAMING", "").strip().lower()
        return raw not in {"1", "true", "yes", "on"}

    def _nonstreaming_fallback_disabled(self) -> bool:
        """Mirror ``CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK``.

        When set, a streaming failure raises instead of double-billing a
        non-streaming retry — useful for cost-sensitive environments.
        """
        raw = os.environ.get("VIOLA_ANTHROPIC_DISABLE_NONSTREAMING_FALLBACK", "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _stream_idle_timeout_s(self) -> float | None:
        """Idle watchdog (per-event) for the stream consumer.

        Default 90s, settable via ``VIOLA_ANTHROPIC_STREAM_IDLE_TIMEOUT_S``.
        Non-positive values disable the watchdog.
        """
        raw = os.environ.get("VIOLA_ANTHROPIC_STREAM_IDLE_TIMEOUT_S", "").strip()
        if not raw:
            return 90.0
        try:
            val = float(raw)
        except ValueError:
            return 90.0
        return val if val > 0 else None

    def _build_request_body(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        system_blocks: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None = None,
        cache_policy: AnthropicCachePolicy | None = None,
    ) -> dict[str, Any]:
        """Assemble a Claude-parity ``messages.create`` body.

        Mirrors :ts:func:`paramsFromContext` in
        ``src/services/api/claude.ts:1538-1729``. Key behaviors:

        - ``temperature`` is included ONLY when ``thinking`` is disabled.
          The Anthropic API rejects ``temperature != 1`` when thinking is
          enabled, so we follow Claude's omit-on-thinking rule.
        - Tools are routed through ``_tools_to_anthropic`` to strip MCP
          ``type=custom`` and convert ``inputSchema`` → ``input_schema``.
        - Cache controls (system + tools + message breakpoint) are applied
          last via :func:`apply_anthropic_cache_controls`, which enforces
          the Claude exactly-one-message-marker rule.
        """
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # The agent loop hands every provider the canonical
            # ``_openai_assistant`` reconstruction of the assistant tool-call
            # turn; translate those to Anthropic ``tool_use`` blocks so
            # multi-turn tool calls (and cross-provider Haiku fallback) send
            # valid content to ``messages.create``.
            "messages": _normalize_openai_assistant_messages(messages),
        }

        if system_blocks:
            body["system"] = system_blocks
        elif system_prompt:
            body["system"] = [{"type": "text", "text": system_prompt}]

        if tools:
            anthropic_tools = _tools_to_anthropic(tools)
            if not anthropic_tools:
                raise ValueError("Anthropic native tool payload contained no valid tool schemas.")
            body["tools"] = anthropic_tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice

        if thinking and thinking.get("type") != "disabled":
            body["thinking"] = thinking
        elif temperature is not None:
            body["temperature"] = float(temperature)

        if self._prompt_caching_enabled(model):
            effective_policy = cache_policy or AnthropicCachePolicy()
            body = apply_anthropic_cache_controls(body, effective_policy)

        return body

    async def _send_request(
        self,
        body: dict[str, Any],
        *,
        client: Any = None,
        prefer_stream: bool | None = None,
        idle_timeout_s: float | None = None,
    ) -> Any:
        """Stream-first wire send with bounded non-streaming recovery.

        Behavior (S3-P0-01):

        1. Streaming attempt: ``messages.create(stream=True)`` and feed
           the iterator to
           :func:`services.llm.anthropic_stream.consume_stream`. Idle
           watchdog (default 90s) aborts on hung streams.
        2. On :class:`StreamConsumerError` (idle, no events, malformed
           stream), discard partial output and retry once with
           ``stream=False`` and parameters capped via
           :func:`adjust_params_for_non_streaming`.
        3. On any other exception, propagate — the request_policy retry
           layer classifies and handles it.

        ``VIOLA_ANTHROPIC_DISABLE_NONSTREAMING_FALLBACK`` matches Claude's
        ``CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK`` semantic: when set,
        a stream failure raises rather than double-billing a fallback
        request.
        """
        client = client or self._client
        if client is None:
            raise RuntimeError("Anthropic client not initialized")

        if prefer_stream is None:
            prefer_stream = self._streaming_enabled()
        if idle_timeout_s is None:
            idle_timeout_s = self._stream_idle_timeout_s()

        if not prefer_stream:
            return await client.messages.create(**body)

        stream_body = {**body, "stream": True}
        stream: Any | None = None
        try:
            stream = await client.messages.create(**stream_body)
        except StreamConsumerError:
            # SDK call itself raised a consumer error before we owned
            # the iterator — fall through to non-streaming retry.
            stream = None

        if stream is not None:
            try:
                return await consume_stream(stream, idle_timeout_s=idle_timeout_s)
            except StreamConsumerError as exc:
                if self._nonstreaming_fallback_disabled():
                    logger.warning(
                        "Anthropic stream failed (%s) and non-streaming fallback is disabled",
                        type(exc).__name__,
                    )
                    raise
                logger.warning(
                    "Anthropic stream %s — retrying non-streaming",
                    type(exc).__name__,
                )
                try:
                    self._emit_diagnostics(
                        "stream.fallback_to_nonstreaming",
                        severity="WARNING",
                        error_type=type(exc).__name__,
                    )
                except Exception:
                    logger.debug(
                        "diagnostic emit failed (stream.fallback_to_nonstreaming)",
                        exc_info=True,
                    )

        recovered_body = adjust_params_for_non_streaming(body)
        return await client.messages.create(**recovered_body)

    async def test_connection(self) -> LLMTestResult:
        """Test the connection to Anthropic API."""
        if not self.is_available():
            return LLMTestResult(
                success=False,
                message=self.last_error or "Provider not available",
                error_code="not_available",
            )

        try:
            start_time = time.time()

            # Make a simple API call to test connection
            response = await self._client.messages.create(
                model=self.config.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}],
            )

            latency_ms = round((time.time() - start_time) * 1000)

            return LLMTestResult(
                success=True,
                message="Connected to Anthropic API successfully",
                latency_ms=latency_ms,
                model_info={
                    "model": self.config.model,
                    "stop_reason": (response.stop_reason if hasattr(response, "stop_reason") else None),
                },
            )
        except Exception as e:
            if _is_anthropic_exception(e, "AuthenticationError"):
                return LLMTestResult(
                    success=False,
                    message="Invalid API key. Please check your Anthropic API key.",
                    error_code="auth_error",
                )

            if _is_anthropic_exception(e, "RateLimitError"):
                return LLMTestResult(
                    success=False,
                    message="Rate limit exceeded. Please wait and try again.",
                    error_code="rate_limit",
                )

            if _is_anthropic_exception(e, "APIConnectionError"):
                return LLMTestResult(
                    success=False,
                    message="Could not connect to Anthropic API. Check your internet connection.",
                    error_code="connection_error",
                )

            if _is_anthropic_exception(e, "NotFoundError"):
                return LLMTestResult(
                    success=False,
                    message=f"Model '{self.config.model}' not found. Please select a valid Claude model.",
                    error_code="model_not_found",
                )

            logger.warning("Anthropic connection test failed: %s", e)
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
        Ask a question and get a response from Claude.

        Args:
            question: User's question
            system_prompt: Optional system prompt
            include_history: Deprecated compatibility flag. Prompt context is
                supplied by PromptFrameBundle.
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature

        Returns:
            Dict with 'content', 'tokens_used', 'model', 'error' keys
        """
        if not self.is_available():
            return {
                "content": "Anthropic provider not available. Please configure your API key.",
                "error": self.last_error or "not_available",
                "tokens_used": 0,
                "model": self.config.model,
            }

        # F-009: Enforce max_tokens cap to prevent uncapped cost exposure.
        max_tokens = clamp_max_tokens(max_tokens)

        rendered_prompt = render_for_anthropic(build_provider_prompt_bundle(user_text=question))
        messages = rendered_prompt["messages"]
        effective_system_prompt = system_prompt
        if not effective_system_prompt:
            system_blocks = rendered_prompt.get("system") or []
            effective_system_prompt = "\n\n".join(
                str(block.get("text") or "").strip()
                for block in system_blocks
                if isinstance(block, dict) and str(block.get("text") or "").strip()
            )

        try:
            start_time = time.time()

            api_kwargs = self._build_request_body(
                model=self.config.model,
                max_tokens=max_tokens,
                messages=messages,
                system_prompt=effective_system_prompt if effective_system_prompt else None,
                temperature=temperature,
            )
            response = await self._send_request(api_kwargs)

            self.last_latency_ms = round((time.time() - start_time) * 1000)

            # Extract content from response
            content = ""
            if response.content:
                for block in response.content:
                    if hasattr(block, "text"):
                        content += block.text

            # Calculate tokens used
            tokens_used = 0
            if hasattr(response, "usage"):
                tokens_used = (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)

            # COST-1: settle reservation with actual tokens
            await self._do_settle(tokens_used)

            # Provider-level history removed — AIController owns per-user history.

            self._emit_diagnostics(
                "ask.success",
                latency_ms=self.last_latency_ms,
                tokens_used=tokens_used,
            )

            # Metrics instrumentation
            try:
                from admin.instrumentation import record_llm_call

                _in = getattr(response.usage, "input_tokens", 0) or 0
                _out = getattr(response.usage, "output_tokens", 0) or 0
                _cw = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                _cr = getattr(response.usage, "cache_read_input_tokens", 0) or 0
                _web = _web_search_requests_from_usage(response.usage)
                record_llm_call(
                    input_tokens=_in,
                    output_tokens=_out,
                    cache_read_tokens=_cr,
                    cache_write_tokens=_cw,
                    web_search_requests=_web,
                    model=self.config.model,
                    latency_ms=self.last_latency_ms,
                    request_type="ask",
                )
                from admin.instrumentation import record_cache_hit, record_cache_miss

                if _cr > 0:
                    record_cache_hit()
                else:
                    record_cache_miss()
            except Exception:
                logger.debug("Metrics instrumentation skipped in ask()")

            return {
                "content": content.strip(),
                "tokens_used": tokens_used,
                "model": self.config.model,
                "error": None,
            }

        except Exception as e:
            error_msg = f"Anthropic API error: {e!s}"
            logger.exception(error_msg)
            self.last_error = error_msg

            self._emit_diagnostics(
                "ask.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )
            raise

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """
        Route user input to a tool call or answer using Claude.

        Args:
            text: User's request text
            history: Deprecated compatibility input ignored by this provider.
            context_bundle: Optional prompt-frame bundle
            max_tokens: Maximum tokens in response
            model_override: Optional model name override

        Returns:
            Dict with 'type' ('tool_call', 'answer', 'ignore'),
            plus type-specific fields.
        """
        self._warn_ignored_history_arg(history, "route_command")
        self._reject_route_command_agent_state()
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "Anthropic provider not available. Please configure your API key.",
            }

        native_tools: list[dict[str, Any]] | None = None

        # F-009: Enforce max_tokens cap — but NOT for native tool mode.
        # The 150-token default cap truncates tool_use blocks mid-generation,
        # causing the LLM to fall back to text answers instead of calling tools.
        # Agent mode has managed spend caps and a wall-clock timeout.
        # NOTE: We check native_tools AFTER the route-tools block below may set
        # it, so move the cap enforcement to after system prompt selection.

        _use_route_tools = False
        _route_tools_anthropic: list[dict[str, Any]] | None = None
        try:
            from services.llm.route_tool_schemas import (
                get_anthropic_route_tools,
            )

            _route_tools_anthropic = get_anthropic_route_tools()
            _use_route_tools = True
        except ImportError:
            pass

        if _use_route_tools and _route_tools_anthropic:
            native_tools = _route_tools_anthropic
            route_response_contract = ""
        else:
            route_response_contract = _structured_route_response_contract()

        prompt_bundle = build_provider_prompt_bundle(
            context_bundle=context_bundle,
            user_text=text,
            native_tools=bool(native_tools),
            response_contract=route_response_contract,
        )
        rendered_prompt = render_for_anthropic(prompt_bundle)
        system_blocks = rendered_prompt["system"]
        messages = rendered_prompt["messages"]

        # Now enforce max_tokens cap based on whether we have native tools
        if not native_tools:
            max_tokens = clamp_max_tokens(max_tokens, default=300)
            system_blocks.append(
                {
                    "type": "text",
                    "text": "IMPORTANT: Respond with valid JSON only. No additional text or explanation.",
                }
            )
        else:
            max_tokens = max(max_tokens, 1024)

        try:
            start_time = time.time()
            effective_model = model_override or self.config.model

            api_kwargs = self._build_request_body(
                model=effective_model,
                max_tokens=max_tokens,
                messages=messages,
                system_blocks=system_blocks if system_blocks else None,
                temperature=0.3,
                tools=list(native_tools) if native_tools else None,
                tool_choice={"type": "any"} if native_tools else None,
            )

            from services.llm.key_pool import get_key_pool

            _pool = get_key_pool("anthropic")
            _current_key = self.config.api_key

            async def _do_anthropic_call() -> Any:
                nonlocal _current_key
                if _pool and _current_key:
                    rotated_client = _create_async_client(api_key=_current_key)
                    return await self._send_request(api_kwargs, client=rotated_client)
                return await self._send_request(api_kwargs)

            def _on_anthropic_retry(_attempt_context: Any, _exc: BaseException, decision: Any) -> None:
                nonlocal _current_key
                if _pool and _current_key and decision.reason == "rate_limit":
                    _pool.report_failure(_current_key, is_billing=False)
                    next_key = _pool.get_key()
                    if next_key:
                        _current_key = next_key

            _policy_result = await execute_with_policy(
                _do_anthropic_call,
                ProviderRequestContext(
                    provider="Anthropic",
                    model=effective_model,
                    session_id=None,
                    request_id=new_request_id("anthropic_route_command"),
                    stream=False,
                    timeout_s=60.0,
                    source="route_command",
                    max_retries=2,
                    base_delay_s=1.0,
                    max_delay_s=4.0,
                    on_retry=_on_anthropic_retry,
                    fallback_model=_policy_fallback_model(effective_model),
                    raise_fallback_triggered=True,
                ),
            )
            response = _policy_result.value_or_raise()
            if _pool and _current_key:
                _pool.report_success(_current_key)
            self.last_latency_ms = round((time.time() - start_time) * 1000)

            # COST-1: settle reservation with actual tokens
            _route_tokens = 0
            _u_settle = getattr(response, "usage", None)
            if _u_settle:
                _route_tokens = (getattr(_u_settle, "input_tokens", 0) or 0) + (
                    getattr(_u_settle, "output_tokens", 0) or 0
                )
            await self._do_settle(_route_tokens)

            # Metrics instrumentation
            try:
                from admin.instrumentation import record_llm_call

                _u = getattr(response, "usage", None)
                if _u:
                    _cr2 = getattr(_u, "cache_read_input_tokens", 0) or 0
                    record_llm_call(
                        input_tokens=getattr(_u, "input_tokens", 0) or 0,
                        output_tokens=getattr(_u, "output_tokens", 0) or 0,
                        cache_read_tokens=_cr2,
                        cache_write_tokens=getattr(_u, "cache_creation_input_tokens", 0) or 0,
                        web_search_requests=_web_search_requests_from_usage(_u),
                        model=effective_model,
                        latency_ms=self.last_latency_ms,
                        request_type=("route_native" if native_tools else "simple_command"),
                    )
                    from admin.instrumentation import (
                        record_cache_hit,
                        record_cache_miss,
                    )

                    if _cr2 > 0:
                        record_cache_hit()
                    else:
                        record_cache_miss()
            except Exception:
                logger.debug("Metrics instrumentation skipped in route_command()")

            # Native tool path: parse structured response
            if native_tools:
                result = self._parse_native_response(response)
                # Route-mode tool calls normalize to the tool_call/answer/ignore format.
                if result.get("type") == "tool_call":
                    try:
                        from services.llm.route_tool_schemas import (
                            convert_route_tool_response,
                        )

                        converted = convert_route_tool_response(
                            result["tool"],
                            result.get("args", {}),
                        )
                        # Preserve usage stats
                        if "_usage" in result:
                            converted["_usage"] = result["_usage"]
                        return converted
                    except Exception:
                        logger.warning("Route tool response conversion failed, falling back to native parse")
                try:
                    with open("logs/_anthropic_diag.log", "a") as _df:
                        _df.write("NATIVE result type=%s tool=%s\n" % (result.get("type"), result.get("tool")))
                except Exception:
                    logger.debug("Anthropic native diag log write skipped")
                return result

            # JSON-in-prompt path: extract text and parse JSON
            result = self._parse_json_response(response)
            try:
                with open("logs/_anthropic_diag.log", "a") as _df:
                    _df.write(
                        "JSON result type=%s cmd=%s\n"
                        % (
                            result.get("type"),
                            result.get("command", result.get("tool", "")),
                        )
                    )
            except Exception:
                logger.debug("Anthropic JSON diag log write skipped")
            return result

        except Exception as e:
            logger.exception("Claude command routing failed: %s", e)
            self.last_error = str(e)
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )
            raise

    async def route_command_native(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None = 1024,
        model_override: str | None = None,
        native_tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        prompt_context_bundle: PromptFrameBundle | None = None,
        first_turn: bool = False,
        tool_choice_override: Any | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute one turn of the agent loop with native Anthropic tools.

        Unlike ``route_command()``, this accepts full Anthropic-format messages
        (with structured content blocks for tool_use / tool_result) and always
        uses native tool calling.

        Args:
            messages: Anthropic-format message history (may contain content
                block lists, not just strings).
            max_tokens: Maximum tokens in response.
            model_override: Optional model name override.

        Returns:
            Standardised dict (same shape as ``route_command()``).
        """
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "Anthropic provider not available.",
            }

        # NOTE: Do NOT apply llm_max_tokens_cap here.  This method is only
        # called from the agent loop which has managed spend caps and a
        # wall-clock timeout. The 150-token default cap is
        # sized for simple JSON command routing and truncates tool_use
        # blocks mid-generation, causing empty input ({}) on every call.
        effective_model = model_override or self.config.model
        effective_max_tokens = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else 1024
        tools = list(native_tools) if native_tools is not None else list(self._native_tools or [])
        tool_choice = _anthropic_tool_choice_from_any(tool_choice_override)
        if tool_choice is None and tools:
            tool_choice = {"type": "any"} if first_turn else {"type": "auto"}
        cache_policy = _anthropic_cache_policy_from_kwargs(kwargs)
        system_blocks: list[dict[str, Any]] = []
        if prompt_context_bundle is not None:
            rendered_prompt = render_for_anthropic(build_provider_prompt_bundle(context_bundle=prompt_context_bundle))
            system_blocks = list(rendered_prompt.get("system") or [])
        elif system_prompt is not None:
            system_blocks = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        elif self._agent_system_prompt:
            system_blocks = [
                {
                    "type": "text",
                    "text": str(self._agent_system_prompt),
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        try:
            start_time = time.time()
            abort_signal = kwargs.get("abort_signal") or kwargs.get("cancel_event")

            api_kwargs = self._build_request_body(
                model=effective_model,
                max_tokens=effective_max_tokens,
                messages=messages,
                system_blocks=system_blocks if system_blocks else None,
                temperature=0.3,
                tools=list(tools) if tools else None,
                tool_choice=tool_choice,
                cache_policy=cache_policy,
            )

            async def _do_native_call() -> Any:
                return await self._send_request(api_kwargs)

            _native_result = await execute_with_policy(
                _do_native_call,
                ProviderRequestContext(
                    provider="Anthropic",
                    model=effective_model,
                    session_id=None,
                    request_id=new_request_id("anthropic_native_agent_turn"),
                    stream=False,
                    timeout_s=60.0,
                    source="agent_loop",
                    max_retries=DEFAULT_MAX_RETRIES,
                    base_delay_s=1.0,
                    max_delay_s=4.0,
                    abort_signal=abort_signal,
                    fallback_model=_policy_fallback_model(effective_model),
                    raise_fallback_triggered=True,
                ),
            )
            response = _native_result.value_or_raise()
            self.last_latency_ms = round((time.time() - start_time) * 1000)

            # COST-1: settle reservation with actual tokens
            _native_tokens = 0
            _u_settle3 = getattr(response, "usage", None)
            if _u_settle3:
                _native_tokens = (getattr(_u_settle3, "input_tokens", 0) or 0) + (
                    getattr(_u_settle3, "output_tokens", 0) or 0
                )
            await self._do_settle(_native_tokens)

            # Metrics instrumentation
            try:
                from admin.instrumentation import record_llm_call

                _u = getattr(response, "usage", None)
                if _u:
                    _cr3 = getattr(_u, "cache_read_input_tokens", 0) or 0
                    record_llm_call(
                        input_tokens=getattr(_u, "input_tokens", 0) or 0,
                        output_tokens=getattr(_u, "output_tokens", 0) or 0,
                        cache_read_tokens=_cr3,
                        cache_write_tokens=getattr(_u, "cache_creation_input_tokens", 0) or 0,
                        web_search_requests=_web_search_requests_from_usage(_u),
                        model=effective_model,
                        latency_ms=self.last_latency_ms,
                        request_type="agent_task",
                    )
                    from admin.instrumentation import (
                        record_cache_hit,
                        record_cache_miss,
                    )

                    if _cr3 > 0:
                        record_cache_hit()
                    else:
                        record_cache_miss()
            except Exception:
                logger.debug("Metrics instrumentation skipped in route_command_native()")

            parsed = self._parse_native_response(response)
            parsed["_model_name"] = effective_model
            return parsed

        except Exception as e:
            logger.exception("Claude native agent turn failed: %s", e)
            self.last_error = str(e)
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )
            # Re-raise so the agent executor's _get_next_response can
            # distinguish a real LLM failure from a legitimate final answer.
            # The executor already has an exception handler that produces a
            # user-friendly message and tracks the error properly.
            raise

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    def _parse_native_response(self, response: Any) -> dict[str, Any]:
        """Parse an Anthropic response that may contain ``tool_use`` blocks.

        Returns a dict matching the contract expected by ``AIController``:
        - ``{"type": "tool_call", "tool": ..., "args": ..., ...}``
        - ``{"type": "answer", "answer": ...}``
        - ``{"type": "ignore", "reason": ...}``
        """
        stop_reason = getattr(response, "stop_reason", None)
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        # Opus-G12 parity: preserve ``thinking`` / ``redacted_thinking``
        # blocks so a future agent loop can replay them. Extended-thinking
        # signatures are credential-bound and the API rejects resumed
        # turns when these blocks are dropped on parse.
        thinking_blocks: list[dict[str, Any]] = []
        for block in response.content or []:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use":
                tool_calls.append(
                    {
                        "tool_use_id": getattr(block, "id", None),
                        "tool": getattr(block, "name", None),
                        "args": getattr(block, "input", {}) or {},
                    }
                )
            elif block_type == "thinking":
                thinking_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", "") or "",
                        "signature": getattr(block, "signature", "") or "",
                    }
                )
            elif block_type == "redacted_thinking":
                thinking_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": getattr(block, "data", "") or "",
                    }
                )
            elif hasattr(block, "text"):
                text_parts.append(block.text)

        # Token tracking
        tokens_used = 0
        if hasattr(response, "usage") and response.usage:
            input_tokens = getattr(response.usage, "input_tokens", 0) or 0
            output_tokens = getattr(response.usage, "output_tokens", 0) or 0
            tokens_used = input_tokens + output_tokens
            cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            logger.info(
                "LLM usage: input=%d output=%d cache_create=%d cache_read=%d",
                input_tokens,
                output_tokens,
                cache_creation,
                cache_read,
            )

        logger.debug(
            "Anthropic native response: stop_reason=%s, text_blocks=%d, tool_blocks=%d, tokens=%d",
            stop_reason,
            len(text_parts),
            len(tool_calls),
            tokens_used,
        )

        # Build usage dict for agent step logging
        usage_dict: dict[str, int] = {}
        if hasattr(response, "usage") and response.usage:
            usage_dict = {
                "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
                "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_write_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                "web_search_requests": _web_search_requests_from_usage(response.usage),
            }

        if stop_reason in {"max_tokens", "model_context_window_exceeded"} and not tool_calls:
            result = build_ai_no_result(
                "max_output_tokens",
                retryable=True,
                message="Anthropic stopped because the response hit its output limit.",
            )
            result["_recoverable_provider_error"] = "max_output_tokens"
            result["_usage"] = usage_dict
            if thinking_blocks:
                result["_thinking_blocks"] = thinking_blocks
            return result

        # If tool_use blocks present, return tool_call
        if tool_calls:
            first = tool_calls[0]
            self._emit_diagnostics(
                "route.native_tool_call",
                latency_ms=self.last_latency_ms,
                tool_name=first["tool"],
                tool_count=len(tool_calls),
                tokens_used=tokens_used,
            )
            tool_result: dict[str, Any] = {
                "type": "tool_call",
                "tool": first["tool"],
                "args": first["args"],
                "tool_use_id": first["tool_use_id"],
                "_raw_content": response.content,
                "_all_tool_calls": tool_calls,
                "_usage": usage_dict,
            }
            if thinking_blocks:
                tool_result["_thinking_blocks"] = thinking_blocks
            return tool_result

        # Text-only response: attempt JSON parse for answer/tool_call/ignore.
        result = self._parse_text_as_json(text_parts, tokens_used)
        result["_usage"] = usage_dict
        if thinking_blocks:
            result["_thinking_blocks"] = thinking_blocks
        return result

    def _parse_json_response(self, response: Any) -> dict[str, Any]:
        """Parse a JSON-in-prompt text response (non-native path)."""
        content = ""
        if response.content:
            for block in response.content:
                if hasattr(block, "text"):
                    content += block.text

        if not content:
            return build_ai_no_result("empty_assistant_content")

        # Token tracking
        tokens_used = 0
        usage_dict: dict[str, int] = {}
        if hasattr(response, "usage") and response.usage:
            input_tokens = getattr(response.usage, "input_tokens", 0) or 0
            output_tokens = getattr(response.usage, "output_tokens", 0) or 0
            tokens_used = input_tokens + output_tokens
            cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            usage_dict = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
                "cache_write_tokens": cache_creation,
                "web_search_requests": _web_search_requests_from_usage(response.usage),
            }
            logger.info(
                "LLM usage: input=%d output=%d cache_create=%d cache_read=%d",
                input_tokens,
                output_tokens,
                cache_creation,
                cache_read,
            )

        # Clean markdown code blocks and parse
        content = _strip_llm_code_fences(content)

        try:
            parsed = json.loads(content)

            if parsed.get("type") == "command":
                parsed.setdefault("command", "")
                if not isinstance(parsed.get("params"), dict):
                    parsed["params"] = {}
            elif parsed.get("type") == "answer":
                parsed.setdefault("answer", "")
            elif parsed.get("type") == "ignore":
                pass
            elif parsed.get("type") == "tool_call":
                # Model returned a tool_call as JSON text — pass through for agent loop
                pass
            else:
                result = {
                    "type": "answer",
                    "answer": parsed.get("answer", content),
                    "_usage": usage_dict,
                }
                return result

            self._emit_diagnostics(
                "route.success",
                latency_ms=self.last_latency_ms,
                response_type=parsed.get("type"),
                tokens_used=tokens_used,
            )
            parsed["_usage"] = usage_dict
            return parsed

        except json.JSONDecodeError:
            return {"type": "answer", "answer": content.strip(), "_usage": usage_dict}

    def _parse_text_as_json(
        self,
        text_parts: list[str],
        tokens_used: int,
    ) -> dict[str, Any]:
        """Try to parse collected text blocks as a JSON answer/tool_call/ignore.

        Falls back to treating the raw text as a plain-text answer.
        """
        content = "".join(text_parts).strip()
        if not content:
            return build_ai_no_result("empty_assistant_content")

        # Strip markdown code fences
        content = _strip_llm_code_fences(content)

        try:
            parsed = json.loads(content)
            resp_type = parsed.get("type", "")

            if resp_type == "command":
                parsed.setdefault("command", "")
                if not isinstance(parsed.get("params"), dict):
                    parsed["params"] = {}
            elif resp_type == "answer":
                parsed.setdefault("answer", "")
            elif resp_type == "ignore":
                pass
            elif resp_type == "tool_call":
                # Model returned a tool_call as JSON text instead of native block
                pass
            else:
                return {
                    "type": "answer",
                    "answer": parsed.get("answer", content),
                }

            self._emit_diagnostics(
                "route.success",
                latency_ms=self.last_latency_ms,
                response_type=resp_type,
                tokens_used=tokens_used,
            )
            return parsed

        except json.JSONDecodeError:
            # Plain text — treat as direct answer
            self._emit_diagnostics(
                "route.success",
                latency_ms=self.last_latency_ms,
                response_type="answer",
                tokens_used=tokens_used,
            )
            return {"type": "answer", "answer": content}
