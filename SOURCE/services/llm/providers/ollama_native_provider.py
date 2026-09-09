"""
Ollama Native LLM Provider

Native Ollama API support for local models.
Provides privacy-first operation by running LLMs locally.
"""

from __future__ import annotations

import json
import re
import time
from types import ModuleType
from typing import TYPE_CHECKING, Any

from core.constants import (
    OLLAMA_DEFAULT_BASE_URL,
    TIMEOUT_EXTENDED,
    TIMEOUT_HOUR,
    TIMEOUT_LLM,
    TIMEOUT_LONG,
    TIMEOUT_VERY_LONG,
)
from core.logging_config import get_logger
from intent.token_budget import context_window_for_model
from services.conversation.frame_rendering import render_for_openai_responses
from services.llm.no_result import build_ai_no_result
from services.llm.prompts import build_provider_prompt_bundle
from services.llm.providers.base import BaseLLMProvider, LLMConfig, LLMTestResult
from services.llm.request_policy import ProviderRequestContext, execute_with_policy, is_abort_signal_set, new_request_id

logger = get_logger(__name__)

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle

# Check for httpx package
_httpx: ModuleType | None = None
try:
    import httpx as _httpx_imported

    _httpx = _httpx_imported
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


def _strip_llm_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM response text."""
    import re as _re

    content = text.strip()
    m = _re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", content, _re.DOTALL)
    if m:
        return m.group(1).strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = _re.sub(r"\s*```(?:json)?\s*$", "", content)
    return content.strip()


def _coerce_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_tool_envelope(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize common local-model tool-call shapes into Viola's envelope."""
    if not isinstance(parsed, dict):
        return parsed
    if parsed.get("type") == "tool_call":
        parsed["args"] = _coerce_args(parsed.get("args", {}))
        return parsed
    if "name" in parsed and ("arguments" in parsed or "args" in parsed):
        return {
            "type": "tool_call",
            "tool": str(parsed.get("name") or ""),
            "args": _coerce_args(parsed.get("arguments", parsed.get("args", {}))),
        }
    if "tool" in parsed and "type" not in parsed:
        return {
            "type": "tool_call",
            "tool": str(parsed.get("tool") or ""),
            "args": _coerce_args(parsed.get("args", {})),
        }
    function = parsed.get("function")
    if isinstance(function, dict) and "name" in function:
        return {
            "type": "tool_call",
            "tool": str(function.get("name") or ""),
            "args": _coerce_args(function.get("arguments", {})),
        }
    return parsed


def _tool_schema_to_ollama(tool: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Viola/MCP tool schema to Ollama's OpenAI-style tool shape."""
    if not isinstance(tool, dict):
        return None

    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        function = tool["function"]
        name = str(function.get("name") or "").strip()
        if not name:
            return None
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": parameters,
            },
        }

    name = str(tool.get("name") or "").strip()
    if not name:
        return None
    parameters = tool.get("inputSchema") or tool.get("input_schema") or tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    if "type" not in parameters:
        parameters = {"type": "object", **parameters}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": str(tool.get("description") or ""),
            "parameters": parameters,
        },
    }


def _tool_call_field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _jsonish_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _ollama_tool_arguments(value: Any) -> dict[str, Any]:
    """Native Ollama history requires argument objects, including on tool continuations."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Ollama tool arguments must be a JSON object")
    return dict(value)


def _ollama_history_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for call in calls:
        function = call["function"]
        converted.append(
            {
                **call,
                "function": {**function, "arguments": _ollama_tool_arguments(function.get("arguments", {}))},
            }
        )
    return converted


def _context_window_from_ollama_show(data: dict[str, Any]) -> int | None:
    model_info = data.get("model_info")
    if isinstance(model_info, dict):
        for key, value in model_info.items():
            if str(key).endswith("context_length") and isinstance(value, int) and value > 0:
                return value
    parameters = data.get("parameters")
    if isinstance(parameters, str):
        match = re.search(r"\bnum_ctx\s+(\d+)\b", parameters)
        if match:
            return int(match.group(1))
    if isinstance(parameters, dict):
        raw = parameters.get("num_ctx")
        if isinstance(raw, int) and raw > 0:
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    return None


class OllamaNativeProvider(BaseLLMProvider):
    """
    LLM provider for Ollama (local LLM server).

    Supports running LLaMA, Mistral, and other models locally via Ollama.
    See: https://ollama.ai/
    """

    # Ollama's HTTP API accepts OpenAI-style function tools. Individual local
    # models may still ignore tools or lack tuned tool-calling behavior.
    SUPPORTS_NATIVE_TOOLS = True

    # Common Ollama models
    COMMON_MODELS = [
        "llama2",
        "llama2:7b",
        "llama2:13b",
        "llama3",
        "llama3:8b",
        "llama3:70b",
        "mistral",
        "mistral:7b",
        "mixtral",
        "codellama",
        "phi",
        "phi3",
        "gemma",
        "gemma2",
        "qwen",
        "qwen2",
        "deepseek-coder",
        "neural-chat",
        "starling-lm",
    ]

    def __init__(self, config: LLMConfig):
        """
        Initialize Ollama provider.

        Args:
            config: LLM configuration with model and optional base_url
        """
        super().__init__(config)
        self.context_window = context_window_for_model(config.model)
        self._context_window_model = config.model
        self._context_window_detected = False

        if not HTTPX_AVAILABLE:
            self.last_error = "httpx package not installed. Run: pip install httpx"
            self._client = None
            return

        # Determine base URL
        self._base_url = config.base_url or OLLAMA_DEFAULT_BASE_URL
        self._base_url = self._base_url.rstrip("/")

        # Create HTTP client
        try:
            assert _httpx is not None
            self._client = _httpx.AsyncClient(
                base_url=self._base_url,
                timeout=TIMEOUT_LLM,  # Ollama can be slow, especially on CPU
            )
            logger.info(
                "Ollama provider initialized: model=%s, url=%s",
                config.model,
                self._base_url,
            )
        except Exception as e:
            self.last_error = f"Failed to initialize Ollama client: {e}"
            self._client = None
            logger.error(self.last_error)

    async def _request_with_policy(
        self,
        method: str,
        path: str,
        *,
        source: str,
        request_id_prefix: str,
        model_name: str | None = None,
        json_payload: dict[str, Any] | None = None,
        payload_factory: Any | None = None,
        timeout: float = TIMEOUT_LLM,
        abort_signal: Any | None = None,
        max_retries: int = 1,
    ) -> Any:
        """Execute one Ollama HTTP request through the shared provider policy."""

        if self._client is None:
            raise RuntimeError("Ollama client is not initialized")

        effective_model = model_name or self.config.model

        async def _do_ollama_call(_attempt_context: Any, *, retry_context: Any) -> Any:
            max_tokens_override = getattr(retry_context, "max_tokens_override", None)
            effective_payload = payload_factory(max_tokens_override) if payload_factory is not None else json_payload
            if method == "get":
                response = await self._client.get(path, timeout=timeout)
            elif method == "post":
                response = await self._client.post(path, json=effective_payload, timeout=timeout)
            else:
                raise ValueError("Unsupported Ollama policy method: %s" % method)
            response.raise_for_status()
            return response

        result = await execute_with_policy(
            _do_ollama_call,
            ProviderRequestContext(
                provider="ollama",
                model=effective_model,
                session_id=None,
                request_id=new_request_id(request_id_prefix),
                stream=False,
                timeout_s=float(timeout),
                source=source,
                max_retries=max_retries,
                base_delay_s=1.0,
                max_delay_s=4.0,
                abort_signal=abort_signal,
                pass_retry_context=True,
                fallback_model=None,
                raise_fallback_triggered=True,
            ),
        )
        return result.value_or_raise()

    async def _refresh_context_window(self, model: str) -> int | None:
        if not self._client:
            return self.context_window
        if self._context_window_model != model:
            self.context_window = context_window_for_model(model)
            self._context_window_model = model
            self._context_window_detected = False
        if self._context_window_model == model and self._context_window_detected and self.context_window:
            return self.context_window
        try:
            response = await self._request_with_policy(
                "post",
                "/api/show",
                source="context_window_probe",
                request_id_prefix="ollama_context_window_probe",
                model_name=model,
                json_payload={"model": model},
                timeout=TIMEOUT_EXTENDED,
                max_retries=1,
            )
            if response.status_code == 200:
                detected = _context_window_from_ollama_show(response.json())
                if detected:
                    self.context_window = detected
                    self._context_window_detected = True
        except Exception as exc:
            logger.debug("Ollama context-window detection failed: %s", exc)
        return self.context_window

    def is_available(self) -> bool:
        """Check if provider is available (quick check)."""
        return HTTPX_AVAILABLE and self._client is not None and bool(self._base_url)

    def get_available_models(self) -> list[str]:
        """Get list of common Ollama models."""
        return self.COMMON_MODELS.copy()

    async def _check_server_available(self) -> bool:
        """Check if Ollama server is running."""
        if not self._client:
            return False

        try:
            response = await self._request_with_policy(
                "get",
                "/api/tags",
                source="server_check",
                request_id_prefix="ollama_server_check",
                timeout=TIMEOUT_LONG,
                max_retries=1,
            )
            return response.status_code == 200
        except Exception as e:
            logger.debug("Ollama server check failed: %s", e)
            return False

    async def _list_installed_models(self) -> list[str]:
        """Get list of models installed on Ollama server."""
        if not self._client:
            return []

        try:
            response = await self._request_with_policy(
                "get",
                "/api/tags",
                source="model_discovery",
                request_id_prefix="ollama_model_discovery",
                timeout=TIMEOUT_EXTENDED,
                max_retries=1,
            )
            if response.status_code == 200:
                data = response.json()
                models = data.get("models", [])
                return [m.get("name", "") for m in models if m.get("name")]
            return []
        except Exception as e:
            logger.debug("Failed to list Ollama models: %s", e)
            return []

    async def test_connection(self) -> LLMTestResult:
        """Test the connection to Ollama server."""
        if not self.is_available():
            return LLMTestResult(
                success=False,
                message=self.last_error or "Ollama provider not configured",
                error_code="not_available",
            )

        # Check if server is running
        if not await self._check_server_available():
            return LLMTestResult(
                success=False,
                message=f"Cannot connect to Ollama server at {self._base_url}. Make sure Ollama is running.",
                error_code="server_unavailable",
            )

        # Check if model is available
        installed_models = await self._list_installed_models()
        model_name = self.config.model.split(":")[0]  # Handle version tags

        model_found = any(m.startswith(model_name) or model_name in m for m in installed_models)

        if not model_found and installed_models:
            return LLMTestResult(
                success=False,
                message=f"Model '{self.config.model}' not found. Available models: {', '.join(installed_models[:5])}",
                error_code="model_not_found",
            )

        try:
            start_time = time.time()

            # Make a simple API call to test the model
            payload = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_predict": 5},
                "stream": False,
            }

            response = await self._request_with_policy(
                "post",
                "/api/chat",
                source="test_connection",
                request_id_prefix="ollama_test_connection",
                model_name=self.config.model,
                json_payload=payload,
                timeout=TIMEOUT_VERY_LONG,
                max_retries=1,
            )

            latency_ms = round((time.time() - start_time) * 1000)
            await self._refresh_context_window(self.config.model)

            return LLMTestResult(
                success=True,
                message=f"Connected to Ollama successfully. Model: {self.config.model}",
                latency_ms=latency_ms,
                model_info={
                    "model": self.config.model,
                    "base_url": self._base_url,
                    "installed_models": installed_models[:10],
                    "context_window": self.context_window,
                },
            )

        except Exception as e:
            # Handle HTTPStatusError if httpx is available
            if _httpx is not None and isinstance(e, _httpx.HTTPStatusError):
                if e.response.status_code == 404:
                    return LLMTestResult(
                        success=False,
                        message=f"Model '{self.config.model}' not found. Run: ollama pull {self.config.model}",
                        error_code="model_not_found",
                    )
                return LLMTestResult(
                    success=False,
                    message="Could not connect to Ollama. Please try again.",
                    error_code="http_error",
                )
            logger.warning("Ollama connection test failed: %s", e)
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
        Ask a question and get a response from Ollama.

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
                "content": "Ollama provider not available. Make sure Ollama is installed and running.",
                "error": self.last_error or "not_available",
                "tokens_used": 0,
                "model": self.config.model,
            }

        # Check server availability
        if not await self._check_server_available():
            return {
                "content": f"Cannot connect to Ollama at {self._base_url}. Please start Ollama with: ollama serve",
                "error": "server_unavailable",
                "tokens_used": 0,
                "model": self.config.model,
            }

        # Build messages
        messages = []

        # Add system prompt
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Add current question
        messages.append({"role": "user", "content": question})

        try:
            start_time = time.time()

            payload = {
                "model": self.config.model,
                "messages": messages,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
                "stream": False,
            }

            response = await self._request_with_policy(
                "post",
                "/api/chat",
                source="ask",
                request_id_prefix="ollama_ask",
                model_name=self.config.model,
                payload_factory=lambda max_tokens_override: {
                    **payload,
                    "options": {
                        **payload["options"],
                        "num_predict": int(max_tokens_override or max_tokens),
                    },
                },
                timeout=TIMEOUT_HOUR,
                max_retries=1,
            )

            result = response.json()

            self.last_latency_ms = round((time.time() - start_time) * 1000)

            # Extract content
            content = result.get("message", {}).get("content", "")
            if not content:
                return {
                    "content": "Ollama returned an empty response. Please try again.",
                    "error": "empty_response",
                    "tokens_used": 0,
                    "model": self.config.model,
                }

            # Calculate tokens used
            tokens_used = result.get("eval_count", 0) + result.get("prompt_eval_count", 0)

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
            # Handle HTTPStatusError if httpx is available
            if _httpx is not None and isinstance(e, _httpx.HTTPStatusError):
                error_msg = f"Ollama API error: {e.response.status_code}"
                logger.exception(error_msg)
                self.last_error = error_msg

                self._emit_diagnostics(
                    "ask.error",
                    severity="ERROR",
                    error_type="HTTPStatusError",
                    status_code=e.response.status_code,
                )

                return {
                    "content": f"Error from Ollama: {e.response.status_code}. Make sure the model is pulled.",
                    "error": error_msg,
                    "tokens_used": 0,
                    "model": self.config.model,
                }

            error_msg = f"Ollama request failed: {e!s}"
            logger.exception(error_msg)
            self.last_error = error_msg

            self._emit_diagnostics(
                "ask.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )

            return {
                "content": "Ollama connection failed (%s). Check that the Ollama server is running." % type(e).__name__,
                "error": error_msg,
                "tokens_used": 0,
                "model": self.config.model,
            }

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """
        Route user input to a tool_call or answer using Ollama.

        Args:
            text: User's request text
            history: Optional conversation history
            context_bundle: Optional prompt-frame bundle
            max_tokens: Maximum tokens in response

        Returns:
            Dict with 'type' ('tool_call' or 'answer'), 'tool', 'args', 'answer'
        """
        self._warn_ignored_history_arg(history, "route_command")
        self._reject_route_command_agent_state()
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "Ollama provider not available. Please start Ollama.",
            }

        if not await self._check_server_available():
            return {
                "type": "answer",
                "answer": f"Cannot connect to Ollama at {self._base_url}. Please start Ollama.",
            }

        abilities = """
AVAILABLE COMMANDS:
1. play_music - Play a song, artist, or album. Params: query
2. pause_music - Pause playback
3. resume_music - Resume playback
4. skip_track - Skip to next track
5. previous_track - Go to previous track
6. volume_set - Set volume. Params: level (0-100)
7. volume_up - Increase volume
8. volume_down - Decrease volume

RESPONSE FORMAT (JSON only):
For actions: {"type": "tool_call", "tool": "<tool_name>", "args": {"param_name": "value"}}
For questions: {"type": "answer", "answer": "<your response>"}
"""
        rendered_prompt = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                user_text=text,
                native_tools=False,
                response_contract=abilities,
            )
        )
        messages = []
        instructions = str(rendered_prompt.get("instructions") or "").strip()
        if instructions:
            messages.append({"role": "system", "content": instructions})
        for item in rendered_prompt.get("input") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                messages.append({"role": str(item.get("role") or "user"), "content": content})

        try:
            start_time = time.time()
            effective_model = model_override or self.config.model
            await self._refresh_context_window(effective_model)

            payload = {
                "model": effective_model,
                "messages": messages,
                "options": {
                    "temperature": 0.3,  # Lower for more consistent routing
                    "num_predict": max_tokens,
                    "num_ctx": self.context_window,
                },
                "format": "json",  # Request JSON format
                "stream": False,
            }

            response = await self._request_with_policy(
                "post",
                "/api/chat",
                source="route_command",
                request_id_prefix="ollama_route_command",
                model_name=effective_model,
                payload_factory=lambda max_tokens_override: {
                    **payload,
                    "options": {
                        **payload["options"],
                        "num_predict": int(max_tokens_override or max_tokens),
                    },
                },
                timeout=TIMEOUT_HOUR,
                max_retries=1,
            )

            result = response.json()
            message = result.get("message", {})
            if isinstance(message, dict):
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    first = tool_calls[0]
                    if isinstance(first, dict):
                        normalized = _normalize_tool_envelope(first)
                        normalized["_model_name"] = effective_model
                        normalized["_context_window"] = self.context_window
                        return normalized
                content = message.get("content", "")
            else:
                content = ""

            self.last_latency_ms = round((time.time() - start_time) * 1000)

            if not content:
                return build_ai_no_result("empty_assistant_content")

            # Strip markdown code fences before JSON parsing
            content = _strip_llm_code_fences(content)

            # Parse JSON response
            try:
                parsed = _normalize_tool_envelope(json.loads(content))

                # Validate response structure
                resp_type = str(parsed.get("type", "")).strip()
                if resp_type == "command":
                    return build_ai_no_result(
                        "legacy_command_envelope_rejected",
                        response_preview=content[:200],
                        retryable=True,
                    )
                if resp_type == "answer":
                    if "answer" not in parsed:
                        parsed["answer"] = ""
                elif resp_type == "tool_call":
                    if "tool" not in parsed and "command" in parsed:
                        parsed["tool"] = parsed.get("command", "")
                    if "args" not in parsed and "params" in parsed:
                        parsed["args"] = parsed.get("params", {})
                    if "tool" not in parsed:
                        parsed["tool"] = ""
                    parsed["args"] = _coerce_args(parsed.get("args", {}))
                elif resp_type == "ignore":
                    if "reason" not in parsed:
                        parsed["reason"] = ""
                else:
                    return {
                        "type": "answer",
                        "answer": parsed.get("answer", "I'm not sure I understood that."),
                        "_model_name": effective_model,
                        "_context_window": self.context_window,
                    }

                self._emit_diagnostics(
                    "route.success",
                    latency_ms=self.last_latency_ms,
                    response_type=parsed.get("type"),
                )

                parsed["_model_name"] = effective_model
                parsed["_context_window"] = self.context_window
                return parsed

            except json.JSONDecodeError:
                # Fallback: treat raw response as answer
                return {"type": "answer", "answer": content.strip()}

        except Exception as e:
            logger.exception("Ollama command routing failed: %s", e)
            self.last_error = str(e)

            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )

            # Re-raise to allow fallback to other providers
            raise

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
        """Execute one Ollama chat turn with native tool schemas when available."""
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "Ollama provider not available. Please start Ollama.",
            }
        if is_abort_signal_set(kwargs.get("abort_signal") or kwargs.get("cancel_event")):
            return build_ai_no_result(
                "provider_aborted",
                retryable=False,
                retry_attempted=False,
                interrupted=True,
                provider="ollama",
            )

        if not await self._check_server_available():
            return {
                "type": "answer",
                "answer": f"Cannot connect to Ollama at {self._base_url}. Please start Ollama.",
            }

        effective_model = model_override or self.config.model
        tools = list(native_tools) if native_tools is not None else list(self._native_tools or [])
        if prompt_context_bundle is not None:
            rendered_prompt = render_for_openai_responses(
                build_provider_prompt_bundle(context_bundle=prompt_context_bundle)
            )
            effective_system_prompt = str(rendered_prompt.get("instructions") or "")
        else:
            effective_system_prompt = system_prompt if system_prompt is not None else self._agent_system_prompt or ""

        try:
            start_time = time.time()
            await self._refresh_context_window(effective_model)

            payload: dict[str, Any] = {
                "model": effective_model,
                "messages": self._convert_messages_to_ollama(messages, effective_system_prompt),
                "options": {
                    "temperature": 0.3,
                    "num_ctx": self.context_window,
                },
                "stream": False,
            }
            if max_tokens is not None:
                payload["options"]["num_predict"] = max_tokens

            ollama_tools = [converted for tool in tools if (converted := _tool_schema_to_ollama(tool)) is not None]
            if tools and not ollama_tools:
                raise ValueError("Ollama native tool payload contained no valid tool schemas.")
            if ollama_tools:
                payload["tools"] = ollama_tools

            if tool_choice_override is not None:
                logger.debug("Ollama route_command_native ignoring unsupported tool_choice_override")

            def _payload_factory(max_tokens_override: int | None) -> dict[str, Any]:
                if max_tokens_override is None:
                    return payload
                adjusted_payload = {
                    **payload,
                    "options": {
                        **payload["options"],
                        "num_predict": int(max_tokens_override),
                    },
                }
                return adjusted_payload

            response = await self._request_with_policy(
                "post",
                "/api/chat",
                source="agent_loop",
                request_id_prefix="ollama_native_agent_turn",
                model_name=effective_model,
                payload_factory=_payload_factory,
                timeout=TIMEOUT_HOUR,
                abort_signal=kwargs.get("abort_signal") or kwargs.get("cancel_event"),
                max_retries=1,
            )

            result = response.json()
            self.last_latency_ms = round((time.time() - start_time) * 1000)

            parsed = self._parse_native_response(result)
            parsed["_model_name"] = effective_model
            parsed["_context_window"] = self.context_window
            return parsed

        except Exception as e:
            logger.exception("Ollama native agent turn failed: %s", e)
            self.last_error = str(e)

            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=type(e).__name__,
            )

            # Re-raise so the agent executor/fallback chain can tell a real
            # provider failure apart from a legitimate model answer.
            raise

    def _convert_messages_to_ollama(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        if system_prompt:
            converted.append({"role": "system", "content": system_prompt})

        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content", "")

            if isinstance(content, dict) and content.get("_openai_assistant"):
                converted.append(
                    {
                        "role": "assistant",
                        "content": _jsonish_text(content.get("content") or ""),
                        "tool_calls": _ollama_history_tool_calls(content.get("tool_calls") or []),
                    }
                )
                continue

            if isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        text_parts.append(str(block))
                        continue
                    block_type = str(block.get("type") or "")
                    if block_type == "text":
                        text = block.get("text") or block.get("content")
                        if text:
                            text_parts.append(str(text))
                        continue
                    if block_type == "tool_result":
                        if text_parts:
                            converted.append({"role": role, "content": "\n".join(text_parts)})
                            text_parts = []
                        tool_message: dict[str, Any] = {
                            "role": "tool",
                            "content": _jsonish_text(block.get("content", "")),
                        }
                        tool_use_id = str(block.get("tool_use_id") or "").strip()
                        if tool_use_id:
                            tool_message["tool_call_id"] = tool_use_id
                        converted.append(tool_message)
                        continue
                    if block_type == "tool_use":
                        if text_parts:
                            converted.append({"role": role, "content": "\n".join(text_parts)})
                            text_parts = []
                        tool_name = str(block.get("name") or "").strip()
                        tool_use_id = str(block.get("id") or block.get("tool_use_id") or "").strip()
                        if tool_name:
                            converted.append(
                                {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": tool_use_id,
                                            "type": "function",
                                            "function": {
                                                "name": tool_name,
                                                "arguments": _ollama_tool_arguments(block.get("input", {})),
                                            },
                                        }
                                    ],
                                }
                            )
                        continue
                    text = block.get("content") or block.get("text")
                    if text:
                        text_parts.append(str(text))
                if text_parts:
                    converted.append({"role": role, "content": "\n".join(text_parts)})
                continue

            converted.append({"role": role, "content": _jsonish_text(content)})

        return converted

    def _parse_native_response(self, result: dict[str, Any]) -> dict[str, Any]:
        usage_dict = {
            "input_tokens": int(result.get("prompt_eval_count", 0) or 0),
            "output_tokens": int(result.get("eval_count", 0) or 0),
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        message = result.get("message", {})
        if not isinstance(message, dict):
            response = build_ai_no_result("empty_assistant_content")
            response["_usage"] = usage_dict
            return response

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            raw_content: dict[str, Any] = {
                "_openai_assistant": True,
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": [],
            }
            all_tool_calls: list[dict[str, Any]] = []
            for index, tool_call in enumerate(tool_calls):
                function = _tool_call_field(tool_call, "function", {})
                tool_name = str(
                    _tool_call_field(function, "name", "") or _tool_call_field(tool_call, "name", "") or ""
                ).strip()
                if not tool_name:
                    continue
                raw_arguments = _tool_call_field(function, "arguments", None) if function is not None else None
                if raw_arguments is None:
                    raw_arguments = _tool_call_field(tool_call, "arguments", _tool_call_field(tool_call, "args", {}))
                tool_use_id = str(
                    _tool_call_field(tool_call, "id", "")
                    or _tool_call_field(tool_call, "tool_use_id", "")
                    or f"ollama_call_{index}"
                )
                raw_content["tool_calls"].append(
                    {
                        "id": tool_use_id,
                        "type": str(_tool_call_field(tool_call, "type", "function") or "function"),
                        "function": {
                            "name": tool_name,
                            "arguments": _jsonish_text(raw_arguments),
                        },
                    }
                )
                all_tool_calls.append(
                    {
                        "tool": tool_name,
                        "args": _coerce_args(raw_arguments),
                        "tool_use_id": tool_use_id,
                    }
                )

            if all_tool_calls:
                first = all_tool_calls[0]
                return {
                    "type": "tool_call",
                    "tool": first["tool"],
                    "args": first["args"],
                    "tool_use_id": first["tool_use_id"],
                    "_raw_content": raw_content,
                    "_all_tool_calls": all_tool_calls,
                    "_usage": usage_dict,
                }

        content = str(message.get("content") or "").strip()
        if not content:
            response = build_ai_no_result("empty_assistant_content")
            response["_usage"] = usage_dict
            return response

        stripped = _strip_llm_code_fences(content)
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                normalized = _normalize_tool_envelope(parsed)
                response_type = str(normalized.get("type") or "").strip()
                if response_type == "tool_call":
                    normalized["args"] = _coerce_args(normalized.get("args", {}))
                if response_type in {"answer", "command", "tool_call", "ignore"}:
                    normalized["_usage"] = usage_dict
                    return normalized
        except json.JSONDecodeError:
            pass

        return {
            "type": "answer",
            "answer": content,
            "_usage": usage_dict,
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
