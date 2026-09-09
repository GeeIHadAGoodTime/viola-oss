"""Pipecat OpenAILLMService subclass that emits trace v2 events.

Wires the phone-call LLM path into the existing trace v2 system
(`intent.task_trace.TaskTraceWriter`) so every chat-completion request emits
trace_llm_attempt_start + trace_llm_provider_payload + trace_llm_stream_chunk
+ trace_llm_attempt_response events with the exact messages list sent to
OpenAI. This is the same trace schema the desktop agent loop uses
(intent/agent_executor.py + services/llm/openai_direct.py).

Defensive: trace failures never break the call. The LLM service is the hot
path; trace emission is best-effort observability.

Used by:
- telephony/loopback_phone_call_session.py (synthetic test bench)
- telephony/call_manager.py (production phone path)

Both paths construct one TaskTraceWriter per phone call and pass it here.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from typing import Any

from openai import APITimeoutError
from pipecat.services.openai.llm import OpenAILLMService

from core.logging_config import get_logger
from intent.log_redaction import redact_card_data
from intent.task_trace import TaskTraceWriter

logger = get_logger(__name__)


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _normalize_setting(value: Any) -> Any:
    from openai import NOT_GIVEN

    if value is NOT_GIVEN:
        return None
    return copy.deepcopy(value)


def _chunk_delta_message(chunk: Any) -> tuple[str, dict[str, Any]]:
    """
    Convert a chunk object into a trace-friendly kind+payload pair.
    """
    if chunk is None:
        return "chunk", {}

    delta = {}
    if hasattr(chunk, "choices") and chunk.choices:
        choice = chunk.choices[0]
        if not choice:
            return "chunk", {}
        choice_delta = getattr(choice, "delta", None)
        if choice_delta is None:
            return "chunk", {}
        content = getattr(choice_delta, "content", None)
        if content is not None:
            delta = {"content": str(content)}
            return "text_delta", delta

        tool_calls = getattr(choice_delta, "tool_calls", None)
        if tool_calls:
            serialized_calls = []
            for tool_call in tool_calls:
                tool_call = getattr(tool_call, "__dict__", tool_call)
                serialized_calls.append(copy.deepcopy(tool_call))
            delta = {"tool_calls": serialized_calls}
            return "tool_call_delta", delta

    if hasattr(chunk, "usage"):
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            return "usage", {"usage": str(usage)}

    return "chunk", {}


def _tool_calls_from_stream(tool_call_state: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_indexes = sorted(tool_call_state)
    calls: list[dict[str, Any]] = []
    for index in ordered_indexes:
        state = tool_call_state[index]
        function_name = str(state.get("name") or "")
        if not function_name:
            continue
        arguments = state.get("arguments") or ""
        calls.append(
            {
                "id": state.get("id"),
                "type": "function",
                "function": {
                    "name": function_name,
                    "arguments": str(arguments),
                },
            }
        )
    return calls


class TracedOpenAILLMService(OpenAILLMService):
    """OpenAILLMService with task-trace v2 hooks on every request/response."""

    def __init__(
        self,
        *args: Any,
        task_trace_writer: TaskTraceWriter | None = None,
        payment_segment_controller: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._task_trace = task_trace_writer
        self._payment_segment_controller = payment_segment_controller
        self._task_trace_llm_attempt_seq = 0

    def _provider_metadata(self) -> dict[str, Any]:
        model = self._settings.model
        return {
            "provider": "OpenAI",
            "provider_class": type(self).__name__,
            "model": model,
            "model_name": model,
            "temperature": _normalize_setting(self._settings.temperature),
        }

    def _next_attempt_id(self) -> tuple[str, bool]:
        task_trace = self._task_trace
        if task_trace is None:
            return "", False
        self._task_trace_llm_attempt_seq += 1
        attempt_id = "%s:llm:%04d" % (task_trace.task_id, self._task_trace_llm_attempt_seq)
        first_turn = self._task_trace_llm_attempt_seq == 1
        return attempt_id, first_turn

    def _trace_payload(self, payload: Any) -> Any:
        controller = getattr(self, "_payment_segment_controller", None)
        if controller is not None and bool(getattr(controller, "active", False)):
            return controller.trace_payload(payload)
        return redact_card_data(copy.deepcopy(payload))

    def _trace_llm_attempt_start(self, *, attempt_id: str, first_turn: bool, request: dict[str, Any]) -> None:
        task_trace = self._task_trace
        if task_trace is None or not attempt_id:
            return
        provider = self._provider_metadata()
        request_payload = self._trace_payload(request)
        request_payload["provider"] = copy.deepcopy(provider)
        task_trace.append_llm_attempt_start(
            ts=_utc_now(),
            attempt_id=attempt_id,
            call_kind="phone_call",
            request_mode="chat_completions",
            provider=provider,
            first_turn=first_turn,
            continuity_before={},
            request=request_payload,
        )
        task_trace.append_llm_provider_payload(
            ts=_utc_now(),
            attempt_id=attempt_id,
            call_kind="phone_call",
            payload_stage="chat_completions.create",
            payload=self._trace_payload(_normalize_settings_payload(request)),
            exact=True,
        )

    def _trace_llm_attempt_response(self, attempt_id: str, response: dict[str, Any]) -> None:
        task_trace = self._task_trace
        if task_trace is None or not attempt_id:
            return
        task_trace.append_llm_attempt_response(
            ts=_utc_now(),
            attempt_id=attempt_id,
            call_kind="phone_call",
            response=self._trace_payload(response),
            continuity_after={},
        )

    def _trace_llm_stream_chunk(
        self,
        *,
        attempt_id: str,
        chunk_index: int,
        chunk_kind: str,
        delta: dict[str, Any],
    ) -> None:
        task_trace = self._task_trace
        if task_trace is None or not attempt_id:
            return
        task_trace.append_llm_stream_chunk(
            ts=_utc_now(),
            attempt_id=attempt_id,
            chunk_index=chunk_index,
            chunk_kind=chunk_kind,
            delta=self._trace_payload(delta),
        )

    def _trace_llm_attempt_failure(self, attempt_id: str, exc: BaseException) -> None:
        task_trace = self._task_trace
        if task_trace is None or not attempt_id:
            return
        task_trace.append_llm_attempt_failure(
            ts=_utc_now(),
            attempt_id=attempt_id,
            call_kind="phone_call",
            error=self._trace_payload({"type": type(exc).__name__, "message": str(exc)}),
            continuity_after={},
        )

    async def _invoke_chat_completion(self, params: dict[str, Any]) -> Any:
        if self._retry_on_timeout:
            try:
                return await asyncio.wait_for(
                    self._client.chat.completions.create(**params),
                    timeout=self._retry_timeout_secs,
                )
            except (TimeoutError, APITimeoutError):
                logger.debug("TracedOpenAILLMService: retrying chat completion after timeout")
                return await self._client.chat.completions.create(**params)
        return await self._client.chat.completions.create(**params)

    async def get_chat_completions(
        self,
        params_from_context,
    ) -> Any:
        params = self.build_chat_completion_params(params_from_context)
        attempt_id, first_turn = self._next_attempt_id()
        self._trace_llm_attempt_start(attempt_id=attempt_id, first_turn=first_turn, request=params)
        try:
            chunks = await self._invoke_chat_completion(params)
        except Exception as exc:
            self._trace_llm_attempt_failure(attempt_id, exc)
            raise

        if not attempt_id:
            return chunks

        async def _wrapped_stream() -> Any:
            response_content: list[str] = []
            tool_calls_by_index: dict[int, dict[str, Any]] = {}
            chunk_index = 0
            final_usage: dict[str, Any] = {}
            try:
                async for chunk in chunks:
                    chunk_index += 1
                    kind, delta = _chunk_delta_message(chunk)
                    if kind != "chunk" or delta:
                        self._trace_llm_stream_chunk(
                            attempt_id=attempt_id,
                            chunk_index=chunk_index,
                            chunk_kind=kind,
                            delta=delta,
                        )

                    if (
                        hasattr(chunk, "choices")
                        and chunk.choices
                        and getattr(chunk.choices[0], "delta", None) is not None
                    ):
                        choice_delta = chunk.choices[0].delta
                        if getattr(choice_delta, "content", None):
                            response_content.append(str(choice_delta.content))
                        tool_calls = getattr(choice_delta, "tool_calls", None)
                        if tool_calls:
                            for tool_call in tool_calls:
                                index = int(getattr(tool_call, "index", 0) or 0)
                                existing = tool_calls_by_index.setdefault(
                                    index,
                                    {"id": "", "name": "", "arguments": ""},
                                )
                                tool_call_id = getattr(tool_call, "id", None)
                                if tool_call_id is not None:
                                    existing["id"] = str(tool_call_id)
                                function = getattr(tool_call, "function", None)
                                if function is not None:
                                    function_name = getattr(function, "name", None)
                                    if function_name:
                                        existing["name"] = str(function_name)
                                    function_arguments = getattr(function, "arguments", None)
                                    if function_arguments is not None:
                                        existing["arguments"] = str(existing.get("arguments") or "") + str(
                                            function_arguments
                                        )

                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        final_usage = copy.deepcopy(
                            {
                                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                                "completion_tokens": getattr(usage, "completion_tokens", None),
                                "total_tokens": getattr(usage, "total_tokens", None),
                                "reasoning_tokens": getattr(
                                    getattr(usage, "completion_tokens_details", None),
                                    "reasoning_tokens",
                                    None,
                                ),
                            }
                        )

                    yield chunk

                tool_calls = _tool_calls_from_stream(tool_calls_by_index)
                self._trace_llm_attempt_response(
                    attempt_id=attempt_id,
                    response={
                        "message": {
                            "role": "assistant",
                            "content": "".join(response_content),
                            "tool_calls": tool_calls,
                        },
                        "usage": final_usage,
                    },
                )
            except Exception as exc:
                self._trace_llm_attempt_failure(attempt_id, exc)
                raise

        return _wrapped_stream()

    async def run_inference(
        self,
        context,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
    ) -> str | None:
        params_from_context = {
            "messages": getattr(context, "messages", []),
            "tools": getattr(context, "tools", None),
            "tool_choice": getattr(context, "tool_choice", None),
        }
        params = self.build_chat_completion_params(params_from_context)
        if system_instruction is not None:
            messages = params.get("messages", [])
            if messages and messages[0].get("role") == "system":
                pass
            else:
                params["messages"] = [{"role": "system", "content": system_instruction}] + list(messages)
        if max_tokens is not None:
            if "max_completion_tokens" in params:
                params["max_completion_tokens"] = max_tokens
            else:
                params["max_tokens"] = max_tokens

        params["stream"] = False
        params.pop("stream_options", None)

        attempt_id, first_turn = self._next_attempt_id()
        self._trace_llm_attempt_start(attempt_id=attempt_id, first_turn=first_turn, request=params)
        try:
            response = await self._invoke_chat_completion(params)
            message = response.choices[0].message if getattr(response, "choices", None) else None
            text = ""
            tool_calls = []
            if message is not None:
                text = str(getattr(message, "content", "") or "")
                message_tool_calls = getattr(message, "tool_calls", None)
                if message_tool_calls:
                    for tool_call in message_tool_calls:
                        function = getattr(tool_call, "function", None)
                        if function is None:
                            continue
                        tool_calls.append(
                            {
                                "id": str(getattr(tool_call, "id", "")),
                                "type": "function",
                                "function": {
                                    "name": str(getattr(function, "name", "")),
                                    "arguments": str(getattr(function, "arguments", "")),
                                },
                            }
                        )
            final_response = {
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": tool_calls,
                }
            }
            self._trace_llm_attempt_response(
                attempt_id=attempt_id,
                response=copy.deepcopy(final_response),
            )
            return text
        except Exception as exc:
            self._trace_llm_attempt_failure(attempt_id, exc)
            raise


def _normalize_settings_payload(params: dict[str, Any]) -> dict[str, Any]:
    normalized_params = copy.deepcopy(params)
    for key in ("frequency_penalty", "presence_penalty", "seed", "temperature", "top_p"):
        if key in normalized_params:
            normalized_params[key] = _normalize_setting(normalized_params[key])
    return normalized_params
