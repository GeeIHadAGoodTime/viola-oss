"""
Google LLM Provider

Gemini API support via direct REST calls so credentials stay scoped to each
provider instance instead of process-global SDK state.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from core.logging_config import get_logger
from services.conversation.frame_rendering import render_for_openai_responses
from services.llm.model_fallback import FALLBACK_MODEL
from services.llm.no_result import build_ai_no_result
from services.llm.prompts import build_provider_prompt_bundle
from services.llm.providers.base import BaseLLMProvider, LLMConfig, LLMTestResult
from services.llm.request_policy import ProviderRequestContext, execute_with_policy, is_abort_signal_set, new_request_id

logger = get_logger(__name__)

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle

_GOOGLE_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_PRIORITY_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-flash-latest",
    "gemini-pro-latest",
)
_EXCLUDED_MODEL_TOKENS = (
    "computer-use",
    "deep-research",
    "image",
    "lyria",
    "preview",
    "robotics",
    "tts",
)
GOOGLE_AVAILABLE = True


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


def _normalize_model_name(model_name: str) -> str:
    normalized = (model_name or "").strip()
    if normalized.startswith("models/"):
        normalized = normalized[len("models/") :]
    return normalized


def _preferred_model_sort_key(model_name: str) -> tuple[int, str]:
    try:
        return (_PRIORITY_MODELS.index(model_name), model_name)
    except ValueError:
        return (len(_PRIORITY_MODELS), model_name)


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _policy_fallback_model(model_name: str) -> str | None:
    """Return the model-level fallback unless the request is already on it."""

    effective_model = str(model_name or "").strip()
    return FALLBACK_MODEL if effective_model and effective_model != FALLBACK_MODEL else None


def _model_is_chat_capable(model_name: str, supported_methods: list[str]) -> bool:
    if "generateContent" not in supported_methods:
        return False
    if not model_name.startswith("gemini"):
        return False
    lower = model_name.lower()
    return not any(token in lower for token in _EXCLUDED_MODEL_TOKENS)


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return ""


def _classify_error(*, status_code: int | None, message: str, model_name: str) -> str:
    lower = message.lower()
    if status_code in {401, 403} or "api key" in lower or "permission" in lower or "authentication" in lower:
        return "auth_error"
    if status_code == 404 or ("not found" in lower and _normalize_model_name(model_name).lower() in lower):
        return "model_not_found"
    if status_code == 429 or "quota" in lower or "rate limit" in lower or "resource exhausted" in lower:
        return "rate_limit"
    if status_code is not None and status_code >= 500:
        return "server_error"
    return "unknown_error"


def _sanitize_error_message(message: str) -> str:
    sanitized = re.sub(r"([?&]key=)[^&\\s]+", r"\\1***REDACTED***", message)
    return sanitized


def _error_message_from_exception(exc: httpx.HTTPStatusError, *, model_name: str) -> tuple[str, str]:
    message = ""
    try:
        message = _extract_error_message(exc.response.json())
    except ValueError:
        message = exc.response.text.strip()
    if not message:
        message = str(exc)
    error_code = _classify_error(
        status_code=exc.response.status_code,
        message=message,
        model_name=model_name,
    )
    return _sanitize_error_message(message), error_code


def _extract_text_from_response(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return ""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        texts = [part.get("text", "") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
        if texts:
            return "".join(texts).strip()
    return ""


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


def _normalize_json_schema(schema: Any) -> dict[str, Any]:
    input_schema = dict(schema) if isinstance(schema, Mapping) else {}
    if "type" not in input_schema:
        input_schema["type"] = "object"
    if "properties" not in input_schema:
        input_schema["properties"] = {}
    if "required" not in input_schema and input_schema["properties"]:
        input_schema["required"] = list(input_schema["properties"].keys())
    return input_schema


def _clean_google_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"$schema", "$defs", "additionalProperties", "default", "examples", "patternProperties"}:
                continue
            if key == "type" and isinstance(item, list):
                non_null = [entry for entry in item if entry != "null"]
                cleaned[key] = non_null[0] if non_null else "string"
                continue
            cleaned[str(key)] = _clean_google_schema(item)
        return cleaned
    if isinstance(value, list):
        return [_clean_google_schema(item) for item in value]
    return value


def _google_tool_from_any(tool: Mapping[str, Any]) -> dict[str, Any] | None:
    function = tool.get("function") if isinstance(tool.get("function"), Mapping) else None
    if function:
        name = function.get("name")
        description = function.get("description", "")
        schema = function.get("parameters") or function.get("input_schema") or function.get("inputSchema")
    else:
        name = tool.get("name")
        description = tool.get("description", "")
        schema = tool.get("parameters") or tool.get("input_schema") or tool.get("inputSchema")

    if not isinstance(name, str) or not name.strip():
        return None

    return {
        "name": name.strip(),
        "description": str(description or ""),
        "parameters": _clean_google_schema(_normalize_json_schema(schema)),
    }


def _tools_to_google(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, Mapping):
            converted = _google_tool_from_any(tool)
            if converted is not None:
                result.append(converted)
    return result


def _extract_tool_choice_name(tool_choice_override: Any) -> str | None:
    if not isinstance(tool_choice_override, Mapping):
        return None
    function = tool_choice_override.get("function")
    if isinstance(function, Mapping) and isinstance(function.get("name"), str):
        return function["name"]
    if isinstance(tool_choice_override.get("name"), str):
        return tool_choice_override["name"]
    return None


def _function_response_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, Mapping):
        return dict(content)
    if isinstance(content, list):
        text_parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text" and part.get("text")
        ]
        return {"content": "\n".join(text_parts)}
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return {"content": content}
    return {"content": str(content)}


def _extract_google_usage(payload: Mapping[str, Any]) -> dict[str, int]:
    usage = payload.get("usageMetadata")
    if not isinstance(usage, Mapping):
        return {}
    input_tokens = int(usage.get("promptTokenCount") or 0)
    output_tokens = int(usage.get("candidatesTokenCount") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": int(usage.get("cachedContentTokenCount") or 0),
        "cache_creation_tokens": 0,
    }


def _record_google_usage(
    usage: Mapping[str, int],
    *,
    model: str,
    latency_ms: int,
    request_type: str,
) -> None:
    """F-019: bridge Google completion usage into SessionCostTracker + metrics.

    Mirrors the call site Anthropic / OpenAI / OpenAI-compatible providers
    use; the Google provider previously extracted usage but never emitted.
    Errors are swallowed (instrumentation is best-effort).
    """
    if not usage:
        return
    try:
        from admin.instrumentation import record_llm_call

        record_llm_call(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_tokens", 0)),
            cache_write_tokens=int(usage.get("cache_creation_tokens", 0)),
            model=model,
            latency_ms=latency_ms,
            request_type=request_type,
        )
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Google usage instrumentation skipped", exc_info=True)


class GoogleProvider(BaseLLMProvider):
    """
    LLM provider for Google's Gemini models.

    Uses direct HTTP requests so per-user API keys never flow through
    module-global SDK configuration.
    """

    SUPPORTS_NATIVE_TOOLS = True

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._discovered_models: list[str] | None = None

        if not GOOGLE_AVAILABLE:
            self.last_error = "Google AI provider runtime not available"
            return

        if not config.api_key:
            self.last_error = "Google AI API key is required"
            return

        if not _normalize_model_name(config.model):
            self.last_error = "Google AI model is required"
            return

        self.config.model = _normalize_model_name(config.model)
        logger.info("Google provider initialized: model=%s", self.config.model)

    def is_available(self) -> bool:
        return GOOGLE_AVAILABLE and bool(self.config.api_key) and bool(self.config.model)

    def _list_models_url(self) -> str:
        return "%s/models" % _GOOGLE_API_BASE

    def _generate_content_url(self, model_name: str | None = None) -> str:
        return "%s/models/%s:generateContent" % (
            _GOOGLE_API_BASE,
            _normalize_model_name(model_name or self.config.model),
        )

    def _build_generation_payload(self, prompt: str, *, max_output_tokens: int, temperature: float) -> dict[str, Any]:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "temperature": temperature,
            },
        }

    async def _generate_content_with_policy(
        self,
        *,
        model_name: str,
        payload_factory: Callable[[int | None], dict[str, Any]],
        source: str,
        request_id_prefix: str,
        abort_signal: Any | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
    ) -> httpx.Response:
        """POST Gemini generateContent through the shared provider policy."""

        from services.llm.key_pool import get_key_pool

        key_pool = get_key_pool("google")
        current_key = self.config.api_key

        async def _do_google_call(_attempt_context: Any, *, retry_context: Any) -> httpx.Response:
            nonlocal current_key
            max_tokens_override = getattr(retry_context, "max_tokens_override", None)
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.post(
                    self._generate_content_url(model_name),
                    params={"key": current_key},
                    json=payload_factory(max_tokens_override),
                )
                response.raise_for_status()
                return response

        def _on_google_retry(_attempt_context: Any, _exc: BaseException, decision: Any) -> None:
            nonlocal current_key
            if key_pool and current_key and decision.reason == "rate_limit":
                key_pool.report_failure(current_key, is_billing=False)
                next_key = key_pool.get_key()
                if next_key:
                    current_key = next_key

        async def _refresh_google_key(prev_error: BaseException | None) -> None:
            nonlocal current_key
            del prev_error

            if key_pool is None or not current_key:
                return
            key_pool.report_failure(current_key, is_billing=False)
            next_key = key_pool.get_key()
            if next_key:
                current_key = next_key

        result = await execute_with_policy(
            _do_google_call,
            ProviderRequestContext(
                provider="google",
                model=model_name,
                session_id=None,
                request_id=new_request_id(request_id_prefix),
                stream=False,
                timeout_s=timeout_s,
                source=source,
                max_retries=max_retries,
                base_delay_s=1.0,
                max_delay_s=4.0,
                abort_signal=abort_signal,
                on_retry=_on_google_retry,
                refresh_client=_refresh_google_key,
                pass_retry_context=True,
                fallback_model=_policy_fallback_model(model_name),
                raise_fallback_triggered=True,
            ),
        )
        response = result.value_or_raise()
        if key_pool and current_key:
            key_pool.report_success(current_key)
        return response

    def _discover_models(self) -> list[str]:
        if not self.config.api_key:
            return []

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                response = client.get(
                    self._list_models_url(),
                    params={"key": self.config.api_key, "pageSize": 1000},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message, error_code = _error_message_from_exception(exc, model_name=self.config.model)
            logger.warning("Google model discovery failed: %s (%s)", message, error_code)
            return []
        except httpx.HTTPError as exc:
            logger.warning("Google model discovery failed: %s", _sanitize_error_message(str(exc)))
            return []

        try:
            payload = response.json()
        except ValueError:
            logger.warning("Google model discovery returned non-JSON response")
            return []

        models: list[str] = []
        for item in payload.get("models", []):
            if not isinstance(item, dict):
                continue
            normalized = _normalize_model_name(str(item.get("name", "")))
            methods_raw = item.get("supportedGenerationMethods") or []
            methods = [method for method in methods_raw if isinstance(method, str)]
            if normalized and _model_is_chat_capable(normalized, methods):
                models.append(normalized)

        ordered = sorted(_dedupe_preserve_order(models), key=_preferred_model_sort_key)
        self._discovered_models = ordered
        return ordered

    async def _generate_text(self, prompt: str, *, max_output_tokens: int, temperature: float) -> str:
        if not self.config.api_key:
            raise RuntimeError("Google AI API key is required")

        try:
            response = await self._generate_content_with_policy(
                model_name=self.config.model,
                payload_factory=lambda max_tokens_override: (
                    self._build_generation_payload(
                        prompt,
                        max_output_tokens=int(max_tokens_override or max_output_tokens),
                        temperature=temperature,
                    )
                ),
                source="ask",
                request_id_prefix="google_generate_text",
                max_retries=2,
            )
        except httpx.HTTPStatusError as exc:
            message, error_code = _error_message_from_exception(exc, model_name=self.config.model)
            raise RuntimeError("%s||%s" % (error_code, message)) from None
        except httpx.HTTPError as exc:
            raise RuntimeError("transport_error||%s" % _sanitize_error_message(str(exc))) from None

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("unknown_error||Google AI returned invalid JSON") from exc

        text = _extract_text_from_response(data)
        if not text:
            raise RuntimeError("unknown_error||Google AI response missing text content")
        return text

    def _google_parts_from_content(
        self,
        content: Any,
        *,
        role: str,
        tool_id_to_name: dict[str, str],
        tool_id_to_provider_id: dict[str, str],
    ) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"text": content}]

        if isinstance(content, Mapping):
            if content.get("_openai_assistant"):
                parts: list[dict[str, Any]] = []
                text = content.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append({"text": text})
                for tool_call in content.get("tool_calls") or []:
                    if not isinstance(tool_call, Mapping):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    raw_args = function.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    name = str(function.get("name") or "")
                    call_id = str(tool_call.get("id") or "")
                    if call_id:
                        tool_id_to_name[call_id] = name
                        tool_id_to_provider_id[call_id] = call_id
                    function_call = {"name": name, "args": args if isinstance(args, dict) else {}}
                    if call_id:
                        function_call["id"] = call_id
                    parts.append({"functionCall": function_call})
                return parts

            if content.get("type") == "tool_result":
                content = [content]
            elif "text" in content:
                return [{"text": str(content.get("text") or "")}]

        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                if block_type == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or block.get("call_id") or "")
                    name = str(block.get("name") or tool_id_to_name.get(tool_use_id) or "")
                    if not name:
                        continue
                    function_response = {
                        "name": name,
                        "response": _function_response_payload(block.get("content", "")),
                    }
                    provider_id = tool_id_to_provider_id.get(tool_use_id)
                    if provider_id:
                        function_response["id"] = provider_id
                    parts.append({"functionResponse": function_response})
                elif block_type in {"tool_use", "function_call"}:
                    name = str(block.get("name") or block.get("tool") or "")
                    args = block.get("input", block.get("args", block.get("arguments", {})))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    tool_use_id = str(block.get("id") or block.get("tool_use_id") or block.get("call_id") or "")
                    provider_id = str(block.get("provider_tool_call_id") or block.get("google_function_call_id") or "")
                    if tool_use_id:
                        tool_id_to_name[tool_use_id] = name
                        if provider_id:
                            tool_id_to_provider_id[tool_use_id] = provider_id
                    function_call = {"name": name, "args": args}
                    if provider_id:
                        function_call["id"] = provider_id
                    part = {"functionCall": function_call}
                    thought_signature = block.get("thoughtSignature") or block.get("thought_signature")
                    if thought_signature:
                        part["thoughtSignature"] = thought_signature
                    parts.append(part)
                elif block_type in {"text", "input_text"}:
                    text = block.get("text")
                    if text:
                        parts.append({"text": str(text)})
                elif role == "user" and block_type in {"image", "input_image"}:
                    source = block.get("source")
                    if isinstance(source, Mapping) and source.get("type") == "base64":
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": str(source.get("media_type") or "image/png"),
                                    "data": str(source.get("data") or ""),
                                }
                            }
                        )
            return parts

        return [{"text": str(content)}]

    def _convert_messages_to_google_contents(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []
        tool_id_to_name: dict[str, str] = {}
        tool_id_to_provider_id: dict[str, str] = {}

        for message in messages:
            role = str(message.get("role") or "")
            content = message.get("content")
            if role == "system":
                if isinstance(content, str) and content.strip():
                    system_parts.append(content.strip())
                continue

            google_role = "model" if role == "assistant" else "user"
            parts = self._google_parts_from_content(
                content,
                role=google_role,
                tool_id_to_name=tool_id_to_name,
                tool_id_to_provider_id=tool_id_to_provider_id,
            )
            if parts:
                contents.append({"role": google_role, "parts": parts})

        return contents, "\n\n".join(system_parts)

    def _build_native_generation_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        native_tools: list[dict[str, Any]],
        system_prompt: str,
        max_output_tokens: int | None,
        tool_choice_override: Any | None,
        first_turn: bool,
    ) -> dict[str, Any]:
        contents, embedded_system_prompt = self._convert_messages_to_google_contents(messages)
        payload: dict[str, Any] = {"contents": contents}
        full_system_prompt = "\n\n".join(
            part for part in (system_prompt.strip(), embedded_system_prompt.strip()) if part
        )
        if full_system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": full_system_prompt}]}

        generation_config: dict[str, Any] = {"temperature": 0.3}
        if max_output_tokens is not None:
            generation_config["maxOutputTokens"] = max_output_tokens
        payload["generationConfig"] = generation_config

        if native_tools:
            function_declarations = _tools_to_google(native_tools)
            if not function_declarations:
                raise ValueError("Google native tool payload contained no valid function declarations.")
            payload["tools"] = [{"functionDeclarations": function_declarations}]
            function_calling_config = self._resolve_google_tool_choice(
                tool_choice_override,
                first_turn=first_turn,
                has_tools=True,
            )
            if function_calling_config is not None:
                payload["toolConfig"] = {"functionCallingConfig": function_calling_config}

        return payload

    def _resolve_google_tool_choice(
        self,
        tool_choice_override: Any | None,
        *,
        first_turn: bool,
        has_tools: bool,
    ) -> dict[str, Any] | None:
        if not has_tools:
            return None

        forced_name = _extract_tool_choice_name(tool_choice_override)
        if forced_name:
            return {"mode": "ANY", "allowedFunctionNames": [forced_name]}

        if isinstance(tool_choice_override, str) and tool_choice_override:
            normalized = tool_choice_override.strip().lower()
        elif first_turn and self._preferred_first_tool:
            return {"mode": "ANY", "allowedFunctionNames": [self._preferred_first_tool]}
        else:
            normalized = str(getattr(self, "_agent_tool_choice", "auto") or "auto").lower()

        mode_map = {
            "auto": "AUTO",
            "required": "ANY",
            "any": "ANY",
            "none": "NONE",
        }
        return {"mode": mode_map.get(normalized, "AUTO")}

    def _parse_native_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        usage_dict = _extract_google_usage(payload)
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            result = build_ai_no_result("empty_assistant_content")
            result["_usage"] = usage_dict
            return result

        first_candidate = candidates[0] if isinstance(candidates[0], Mapping) else {}
        content = first_candidate.get("content") if isinstance(first_candidate, Mapping) else {}
        parts = content.get("parts") if isinstance(content, Mapping) else []
        if not isinstance(parts, list):
            parts = []

        text_parts: list[str] = []
        raw_content: list[dict[str, Any]] = []
        all_tool_calls: list[dict[str, Any]] = []

        for index, part in enumerate(parts):
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
                raw_content.append({"type": "text", "text": text})
            function_call = part.get("functionCall") or part.get("function_call")
            if isinstance(function_call, Mapping):
                tool_name = str(function_call.get("name") or "")
                args = function_call.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                provider_id = str(function_call.get("id") or function_call.get("call_id") or "")
                tool_use_id = provider_id or "google_%d_%s" % (index, tool_name or "tool")
                thought_signature = (
                    part.get("thoughtSignature")
                    or part.get("thought_signature")
                    or function_call.get("thoughtSignature")
                    or function_call.get("thought_signature")
                )
                raw_block: dict[str, Any] = {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": args,
                }
                if provider_id:
                    raw_block["provider_tool_call_id"] = provider_id
                if thought_signature:
                    raw_block["thoughtSignature"] = thought_signature
                raw_content.append(raw_block)
                all_tool_calls.append(
                    {
                        "tool": tool_name,
                        "args": args,
                        "tool_use_id": tool_use_id,
                    }
                )

        tokens_used = usage_dict.get("input_tokens", 0) + usage_dict.get("output_tokens", 0)
        if all_tool_calls:
            first = all_tool_calls[0]
            self._emit_diagnostics(
                "route.native_tool_call",
                latency_ms=self.last_latency_ms,
                tool_name=first["tool"],
                tool_count=len(all_tool_calls),
                tokens_used=tokens_used,
            )
            return {
                "type": "tool_call",
                "tool": first["tool"],
                "args": first["args"],
                "tool_use_id": first["tool_use_id"],
                "_raw_content": raw_content,
                "_all_tool_calls": all_tool_calls,
                "_usage": usage_dict,
            }

        answer = "".join(text_parts).strip()
        if not answer:
            result = build_ai_no_result("empty_assistant_content")
            result["_usage"] = usage_dict
            return result

        stripped = _strip_llm_code_fences(answer)
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and parsed.get("type") in {"answer", "command", "tool_call", "ignore"}:
                if parsed.get("type") == "answer" and "continue_listening" in parsed:
                    parsed["continue_listening"] = bool(parsed["continue_listening"])
                parsed["_usage"] = usage_dict
                return parsed
        except json.JSONDecodeError:
            pass

        return {
            "type": "answer",
            "answer": answer,
            "_usage": usage_dict,
        }

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
        """Execute one agent-loop turn with Gemini native function calling."""
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "Google AI provider not available.",
            }
        if is_abort_signal_set(kwargs.get("abort_signal") or kwargs.get("cancel_event")):
            return build_ai_no_result(
                "provider_aborted",
                retryable=False,
                retry_attempted=False,
                interrupted=True,
                provider="google",
            )

        effective_model = _normalize_model_name(model_override or self.config.model)
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

            def _payload_factory(max_tokens_override: int | None) -> dict[str, Any]:
                return self._build_native_generation_payload(
                    messages,
                    native_tools=tools,
                    system_prompt=effective_system_prompt,
                    max_output_tokens=int(max_tokens_override) if max_tokens_override is not None else max_tokens,
                    tool_choice_override=tool_choice_override,
                    first_turn=first_turn,
                )

            response = await self._generate_content_with_policy(
                model_name=effective_model,
                payload_factory=_payload_factory,
                source="agent_loop",
                request_id_prefix="google_native_agent_turn",
                abort_signal=kwargs.get("abort_signal") or kwargs.get("cancel_event"),
                timeout_s=60.0,
                max_retries=2,
            )

            self.last_latency_ms = round((time.time() - start_time) * 1000)
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("unknown_error||Google AI returned invalid JSON") from exc

            parsed = self._parse_native_response(data)
            parsed["_model_name"] = effective_model
            # F-019: emit usage to SessionCostTracker / metrics writers so
            # Google API spend is visible alongside Anthropic / OpenAI.
            _record_google_usage(
                parsed.get("_usage") or {},
                model=effective_model,
                latency_ms=self.last_latency_ms,
                request_type="agent_turn",
            )
            return parsed

        except httpx.HTTPStatusError as exc:
            message, error_code = _error_message_from_exception(exc, model_name=effective_model)
            error = RuntimeError("%s||%s" % (error_code, message))
            self.last_error = message
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=error_code,
            )
            raise error from None
        except httpx.HTTPError as exc:
            message = _sanitize_error_message(str(exc))
            error = RuntimeError("transport_error||%s" % message)
            self.last_error = message
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type="transport_error",
            )
            raise error from None
        except Exception as exc:
            error_code, message = self._split_runtime_error(exc)
            self.last_error = message
            logger.exception("Google native agent turn failed: %s", message)
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=error_code if error_code != "unknown_error" else type(exc).__name__,
            )
            raise

    @staticmethod
    def _split_runtime_error(exc: Exception) -> tuple[str, str]:
        text = _sanitize_error_message(str(exc))
        if "||" in text:
            error_code, message = text.split("||", 1)
            return error_code, message
        return "unknown_error", text

    def get_available_models(self) -> list[str]:
        if self._discovered_models is not None:
            return self._discovered_models.copy()
        return self._discover_models()

    async def test_connection(self) -> LLMTestResult:
        if not self.is_available():
            return LLMTestResult(
                success=False,
                message=self.last_error or "Provider not available",
                error_code="not_available",
            )

        available_models = self.get_available_models()
        if available_models and _normalize_model_name(self.config.model) not in available_models:
            return LLMTestResult(
                success=False,
                message="Model '%s' not found. Please select a valid Gemini model." % self.config.model,
                error_code="model_not_found",
            )

        try:
            start_time = time.time()
            try:
                await self._generate_text("Reply with OK only.", max_output_tokens=16, temperature=0.0)
            except Exception as exc:
                error_code, message = self._split_runtime_error(exc)
                if error_code != "unknown_error" or "missing text content" not in message.lower():
                    raise
                await self._generate_text("Return exactly OK and no other text.", max_output_tokens=32, temperature=0.0)
            latency_ms = round((time.time() - start_time) * 1000)
            return LLMTestResult(
                success=True,
                message="Connected to Google AI API successfully",
                latency_ms=latency_ms,
                model_info={"model": self.config.model},
            )
        except Exception as exc:
            error_code, message = self._split_runtime_error(exc)
            if error_code == "auth_error":
                return LLMTestResult(
                    success=False,
                    message="Invalid API key. Please check your Google AI API key.",
                    error_code=error_code,
                )
            if error_code == "rate_limit":
                return LLMTestResult(
                    success=False,
                    message="Rate limit or quota exceeded. Please wait and try again.",
                    error_code=error_code,
                )
            if error_code == "model_not_found":
                return LLMTestResult(
                    success=False,
                    message="Model '%s' not found. Please select a valid Gemini model." % self.config.model,
                    error_code=error_code,
                )
            return LLMTestResult(
                success=False,
                message="Connection test failed: %s" % message,
                error_code=error_code,
            )

    async def ask(
        self,
        question: str,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        if not self.is_available():
            return {
                "content": "Google AI provider not available. Please configure your API key.",
                "error": self.last_error or "not_available",
                "tokens_used": 0,
                "model": self.config.model,
            }

        prompt_parts: list[str] = []
        if system_prompt:
            prompt_parts.append(system_prompt)
        prompt_parts.append("User: %s" % question)
        full_prompt = "\n\n".join(prompt_parts)

        try:
            start_time = time.time()
            content = await self._generate_text(
                full_prompt,
                max_output_tokens=max_tokens,
                temperature=temperature,
            )
            self.last_latency_ms = round((time.time() - start_time) * 1000)
            tokens_used = len(content.split()) + len(question.split())

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
        except Exception as exc:
            error_code, message = self._split_runtime_error(exc)
            self.last_error = message
            logger.error("Google AI API error: %s", message)

            self._emit_diagnostics(
                "ask.error",
                severity="ERROR",
                error_type=error_code,
            )
            raise

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
    ) -> dict[str, Any]:
        self._warn_ignored_history_arg(history, "route_command")
        self._reject_route_command_agent_state()
        if not self.is_available():
            return {
                "type": "answer",
                "answer": "Google AI provider not available. Please configure your API key.",
            }

        rendered_prompt = render_for_openai_responses(
            build_provider_prompt_bundle(
                context_bundle=context_bundle,
                user_text=text,
                native_tools=False,
                response_contract=_structured_route_response_contract(),
            )
        )
        route_messages: list[dict[str, Any]] = []
        instructions = str(rendered_prompt.get("instructions") or "").strip()
        if instructions:
            route_messages.append({"role": "system", "content": instructions})
        for item in rendered_prompt.get("input") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                route_messages.append({"role": str(item.get("role") or "user"), "content": content})
        contents, embedded_system_prompt = self._convert_messages_to_google_contents(route_messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.3,
            },
        }
        if embedded_system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": embedded_system_prompt}]}

        try:
            start_time = time.time()
            response = await self._generate_content_with_policy(
                model_name=_normalize_model_name(self.config.model),
                payload_factory=lambda max_tokens_override: {
                    **payload,
                    "generationConfig": {
                        **payload["generationConfig"],
                        "maxOutputTokens": int(max_tokens_override or max_tokens),
                    },
                },
                source="route_command",
                request_id_prefix="google_route_command",
                timeout_s=60.0,
                max_retries=2,
            )
            data = response.json()
            content = _extract_text_from_response(data)
            self.last_latency_ms = round((time.time() - start_time) * 1000)
            # F-019: route_command (simple_command) emits usage too.
            _record_google_usage(
                _extract_google_usage(data),
                model=_normalize_model_name(self.config.model),
                latency_ms=self.last_latency_ms,
                request_type="simple_command",
            )
            if not content:
                return build_ai_no_result("empty_assistant_content")

            try:
                parsed = json.loads(_strip_llm_code_fences(content))
                if parsed.get("type") == "command":
                    return build_ai_no_result(
                        "legacy_command_envelope_rejected",
                        response_preview=content[:200],
                        retryable=True,
                    )
                if parsed.get("type") in {"tool_call", "tool_use"}:
                    parsed["type"] = "tool_call"
                    if "tool" not in parsed and "name" in parsed:
                        parsed["tool"] = parsed.get("name")
                    if "tool" not in parsed and "command" in parsed:
                        parsed["tool"] = parsed.get("command")
                    if "args" not in parsed and "params" in parsed:
                        parsed["args"] = parsed.get("params")
                    if "args" not in parsed or not isinstance(parsed["args"], dict):
                        parsed["args"] = {}
                elif parsed.get("type") == "answer":
                    if "answer" not in parsed:
                        parsed["answer"] = ""
                else:
                    return {
                        "type": "answer",
                        "answer": parsed.get("answer", content),
                    }

                self._emit_diagnostics(
                    "route.success",
                    latency_ms=self.last_latency_ms,
                    response_type=parsed.get("type"),
                )
                return parsed
            except json.JSONDecodeError:
                return {"type": "answer", "answer": content.strip()}
        except httpx.HTTPStatusError as exc:
            message, error_code = _error_message_from_exception(
                exc, model_name=_normalize_model_name(self.config.model)
            )
            self.last_error = message
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=error_code,
            )
            raise RuntimeError("%s||%s" % (error_code, message)) from None
        except httpx.HTTPError as exc:
            message = _sanitize_error_message(str(exc))
            self.last_error = message
            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type="transport_error",
            )
            raise RuntimeError("transport_error||%s" % message) from None
        except Exception as exc:
            error_code, message = self._split_runtime_error(exc)
            self.last_error = message
            logger.error("Gemini command routing failed: %s", message)

            self._emit_diagnostics(
                "route.error",
                severity="ERROR",
                error_type=error_code,
            )
            raise

    def clear_history(self) -> None:
        super().clear_history()
