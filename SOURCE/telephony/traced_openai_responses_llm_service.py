"""Pipecat OpenAIResponsesLLMService subclass that emits trace v2 events.

Mirrors `telephony.traced_openai_llm_service.TracedOpenAILLMService` but for the
**Responses API** path. The key reason this exists: OpenAI's Chat Completions
rejects `reasoning_effort` when function tools are present on gpt-5.4-mini, so
the phone path running on Pipecat's `OpenAILLMService` was running with
`reasoning_tokens=0` (verified in trace v2 usage chunks). The Responses API
accepts `reasoning={"effort": ...}` alongside tools — so this service recovers
the model's reasoning capacity in the phone path.

Used by:
- telephony/loopback_phone_call_session.py (synthetic test bench)
- telephony/call_manager.py (production phone path)

Trace v2 emission is best-effort; failures never raise into the call path.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any, Callable

from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseReasoningItem,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseReasoningTextDoneEvent,
    ResponseTextDeltaEvent,
)
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.processors.aggregators.llm_context import LLMSpecificMessage
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

from core.logging_config import get_logger
from intent.log_redaction import redact_card_data
from intent.task_trace import TaskTraceWriter

logger = get_logger(__name__)

_PHONE_VOLATILE_CONTEXT_PREFIX = "call_state: volatile_phone_context"
_PHONE_PROMPT_CACHE_KEY_NAMESPACE = "viola-phone"
_PHONE_PROMPT_CACHE_KEY_MAX_LEN = 128
_PHONE_PROMPT_CACHE_SESSION_ID_RE = re.compile(r"(?m)^session_id: phone:[^\r\n]+$")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _safe_usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _ensure_reasoning_summary_requested(params: dict[str, Any]) -> None:
    reasoning = params.get("reasoning")
    if not isinstance(reasoning, dict):
        return
    if str(reasoning.get("summary") or "").strip():
        return
    params["reasoning"] = {**reasoning, "summary": "auto"}


def _reasoning_trace_payload(
    reasoning_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    text_parts: list[str] = []
    trace_items: list[dict[str, Any]] = []
    for item in reasoning_items:
        trace_item: dict[str, Any] = {
            "id": item.get("id"),
            "type": "reasoning",
        }
        summary = item.get("summary")
        if isinstance(summary, list):
            clean_summary = [
                {"type": part.get("type"), "text": str(part.get("text") or "")}
                for part in summary
                if isinstance(part, dict) and str(part.get("text") or "").strip()
            ]
            if clean_summary:
                trace_item["summary"] = clean_summary
                text_parts.extend(part["text"].strip() for part in clean_summary)
        content = item.get("content")
        if isinstance(content, list):
            clean_content = [
                {"type": part.get("type"), "text": str(part.get("text") or "")}
                for part in content
                if isinstance(part, dict) and str(part.get("text") or "").strip()
            ]
            if clean_content:
                trace_item["content"] = clean_content
                text_parts.extend(part["text"].strip() for part in clean_content)
        if "summary" in trace_item or "content" in trace_item:
            trace_items.append(trace_item)
    if not text_parts and not trace_items:
        return None
    return {
        "kind": "provider_reasoning",
        "format": "text",
        "source": "openai_responses_reasoning_item",
        "text": "\n".join(text_parts).strip(),
        "items": trace_items,
    }


def _openai_exception_payload(exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc)[:1000],
    }
    for attr in ("code", "status_code", "request_id"):
        value = getattr(exc, attr, None)
        if value is not None:
            payload[attr] = str(value)[:500]
    body = getattr(exc, "body", None)
    response = getattr(exc, "response", None)
    if body is None and response is not None:
        body = getattr(response, "text", None)
    if body is not None:
        payload["body"] = str(body)[:2000]
    return payload


def _reasoning_item_for(items: dict[str, dict[str, Any]], item_id: str | None) -> dict[str, Any]:
    key = item_id or "reasoning"
    item = items.setdefault(key, {"id": key, "type": "reasoning"})
    return item


def _set_indexed_text(
    items: dict[str, dict[str, Any]],
    *,
    item_id: str | None,
    bucket: str,
    index: int | None,
    text: str | None,
) -> None:
    clean_text = str(text or "").strip()
    if not clean_text:
        return
    item = _reasoning_item_for(items, item_id)
    indexed = item.setdefault(bucket, {})
    if isinstance(indexed, dict):
        indexed[int(index or 0)] = clean_text


def _append_indexed_text_delta(
    items: dict[str, dict[str, Any]],
    *,
    item_id: str | None,
    bucket: str,
    index: int | None,
    delta: str | None,
) -> None:
    clean_delta = str(delta or "")
    if not clean_delta:
        return
    item = _reasoning_item_for(items, item_id)
    indexed = item.setdefault(bucket, {})
    if isinstance(indexed, dict):
        key = int(index or 0)
        indexed[key] = str(indexed.get(key) or "") + clean_delta


def _merge_reasoning_item(
    items: dict[str, dict[str, Any]],
    *,
    item: ResponseReasoningItem,
) -> None:
    trace_item = _reasoning_item_for(items, item.id)
    if item.summary:
        for idx, part in enumerate(item.summary):
            _set_indexed_text(
                items,
                item_id=item.id,
                bucket="_summary_by_index",
                index=idx,
                text=getattr(part, "text", None),
            )
    if item.content:
        for idx, part in enumerate(item.content):
            _set_indexed_text(
                items,
                item_id=item.id,
                bucket="_content_by_index",
                index=idx,
                text=getattr(part, "text", None),
            )
    if item.encrypted_content is not None:
        trace_item["encrypted_content"] = item.encrypted_content


def _finalize_reasoning_items(items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    for item in items.values():
        clean_item = {k: v for k, v in item.items() if not k.startswith("_")}
        summary_by_index = item.get("_summary_by_index")
        if isinstance(summary_by_index, dict):
            summary_parts = [
                {"type": "summary_text", "text": text}
                for _, text in sorted(summary_by_index.items())
                if str(text or "").strip()
            ]
            if summary_parts:
                clean_item["summary"] = summary_parts
        content_by_index = item.get("_content_by_index")
        if isinstance(content_by_index, dict):
            content_parts = [
                {"type": "reasoning_text", "text": text}
                for _, text in sorted(content_by_index.items())
                if str(text or "").strip()
            ]
            if content_parts:
                clean_item["content"] = content_parts
        if "encrypted_content" in clean_item and "summary" not in clean_item:
            clean_item["summary"] = []
        finalized.append(clean_item)
    return finalized


def _normalize_phone_cache_instructions(instructions: Any) -> Any:
    if not isinstance(instructions, str):
        return instructions
    return _PHONE_PROMPT_CACHE_SESSION_ID_RE.sub("session_id: phone:<stable>", instructions)


def _phone_prompt_cache_key(params: dict[str, Any]) -> str:
    """Return the stable cache-routing key for a phone Responses prompt prefix."""

    cache_prefix = {
        "model": params.get("model"),
        "instructions": _normalize_phone_cache_instructions(params.get("instructions")),
        "tools": params.get("tools"),
        "reasoning": params.get("reasoning"),
    }
    digest = hashlib.sha256(
        json.dumps(
            cache_prefix,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="ignore")
    ).hexdigest()[:24]
    return ("%s:%s" % (_PHONE_PROMPT_CACHE_KEY_NAMESPACE, digest))[:_PHONE_PROMPT_CACHE_KEY_MAX_LEN]


def _decode_function_call_arguments(function_call: dict[str, str]) -> dict[str, Any]:
    raw_arguments = function_call.get("arguments") or ""
    if not raw_arguments:
        return {}
    try:
        decoded = json.loads(raw_arguments)
    except json.JSONDecodeError:
        logger.warning(
            "Failed to parse function call arguments for %s: %s",
            function_call.get("name") or "<unknown>",
            raw_arguments,
        )
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _responses_stream_event_replay_key(event: Any) -> tuple[Any, ...] | None:
    """Return a Responses stream event identity when OpenAI provides one.

    This is intentionally event-level, not text-level. A model can still choose
    to repeat a sentence in distinct stream events; only transport replays of
    the same numbered Responses event are dropped before they become speech.
    """

    sequence_number = getattr(event, "sequence_number", None)
    if sequence_number is None:
        return None
    return (
        getattr(event, "type", type(event).__name__),
        sequence_number,
        getattr(event, "output_index", None),
        getattr(event, "content_index", None),
        getattr(event, "item_id", None),
    )


class TracedOpenAIResponsesLLMService(OpenAIResponsesLLMService):
    """OpenAIResponsesLLMService with task-trace v2 hooks.

    Captures the exact request payload once at attempt start, compact provider
    request metadata, per-event stream chunks, and the final response. Mirrors the v1 chat
    completions tracer's emission shape so downstream tooling
    (`task_trace_reader.TraceReader`) reads both identically.
    """

    def __init__(
        self,
        *args: Any,
        task_trace_writer: TaskTraceWriter | None = None,
        direct_context_commit: bool = False,
        payment_segment_controller: Any | None = None,
        phone_latency_recorder: Any | None = None,
        openai_client: Any = None,
        phone_volatile_context_builder: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Allow the caller to inject a pre-built AsyncOpenAI client (e.g. one
        # wired through codex-auth's AsyncCodexTransport so the phone pipeline
        # uses the user's ChatGPT-Plus subscription instead of a depleted
        # direct OpenAI API key). Without this seam the parent's _create_client
        # always builds a vanilla AsyncOpenAI keyed off api_key, which means
        # the phone pipeline's only auth path is the api_key=.env entry — and
        # 429 insufficient_quota stops the call dead the moment the LLM speaks.
        if openai_client is not None:
            self._client = openai_client
        self._task_trace = task_trace_writer
        self._payment_segment_controller = payment_segment_controller
        self._phone_latency_recorder = phone_latency_recorder
        self._phone_volatile_context_builder = phone_volatile_context_builder
        self._direct_context_commit = direct_context_commit
        self._task_trace_llm_attempt_seq = 0
        self._current_attempt_id: str | None = None
        self._current_attempt_chunk_idx = 0
        self._current_response_text: list[str] = []
        self._current_response_tool_calls: list[dict[str, Any]] = []
        self._trace_emit_tail: asyncio.Task[None] | None = None

    def _trace_payload(self, payload: Any) -> Any:
        controller = self._payment_segment_controller
        if controller is not None and bool(getattr(controller, "active", False)):
            return controller.trace_payload(payload)
        return redact_card_data(copy.deepcopy(payload))

    def _request_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        tools = params.get("tools")
        input_items = params.get("input")
        instructions = params.get("instructions")
        prompt_cache_key = params.get("prompt_cache_key")
        try:
            request_bytes = len(
                json.dumps(
                    {
                        "model": params.get("model"),
                        "stream": params.get("stream"),
                        "input": input_items,
                        "instructions": instructions,
                        "tools": tools,
                        "reasoning": params.get("reasoning"),
                        "temperature": params.get("temperature"),
                        "max_output_tokens": params.get("max_output_tokens"),
                        "service_tier": params.get("service_tier"),
                        "prompt_cache_key": prompt_cache_key,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8", errors="ignore")
            )
        except (TypeError, ValueError):
            request_bytes = 0
        return {
            "tool_count": len(tools) if isinstance(tools, list) else 0,
            "request_bytes": request_bytes,
            "input_items": len(input_items) if isinstance(input_items, list) else 0,
            "instruction_chars": (len(instructions) if isinstance(instructions, str) else 0),
            "prompt_cache_key": (prompt_cache_key if isinstance(prompt_cache_key, str) else ""),
            "prompt_cache_key_set": bool(isinstance(prompt_cache_key, str) and prompt_cache_key.strip()),
        }

    def _record_phone_llm_request_stage(
        self,
        stage: str,
        duration_ms: float,
        *,
        params: dict[str, Any] | None = None,
    ) -> None:
        recorder = self._phone_latency_recorder
        if recorder is None:
            return
        try:
            stats = self._request_stats(params or {})
            recorder.record_llm_request_stage(
                stage=stage,
                duration_ms=duration_ms,
                tool_count=stats["tool_count"],
                request_bytes=stats["request_bytes"],
                input_items=stats["input_items"],
                instruction_chars=stats["instruction_chars"],
                prompt_cache_key=stats["prompt_cache_key"],
                prompt_cache_key_set=stats["prompt_cache_key_set"],
            )
        except Exception:  # noqa: BLE001, RUF100 - latency telemetry must never break a live phone turn
            logger.debug("Phone LLM latency stage emit failed for %s", stage, exc_info=True)

    def _record_phone_llm_usage(self, usage: Any) -> None:
        recorder = self._phone_latency_recorder
        if recorder is None:
            return
        try:
            input_details = getattr(usage, "input_tokens_details", None)
            output_details = getattr(usage, "output_tokens_details", None)
            recorder.record_llm_usage(
                input_tokens=_safe_usage_int(getattr(usage, "input_tokens", 0)),
                output_tokens=_safe_usage_int(getattr(usage, "output_tokens", 0)),
                total_tokens=_safe_usage_int(getattr(usage, "total_tokens", 0)),
                cached_input_tokens=_safe_usage_int(getattr(input_details, "cached_tokens", 0)),
                reasoning_tokens=_safe_usage_int(getattr(output_details, "reasoning_tokens", 0)),
            )
        except Exception:  # noqa: BLE001, RUF100 - latency telemetry must never break a live phone turn
            logger.debug("Phone LLM usage emit failed", exc_info=True)

    async def enqueue_operator_note(self, text: str) -> None:
        """Attach an operator note to the next phone-agent LLM turn."""
        note_text = " ".join(str(text or "").split()).strip()
        if not note_text:
            return

        notice = "\n".join(
            [
                "phone_event: operator_note",
                "source: human_operator",
                "note: %s" % note_text,
            ]
        )

        await self.push_frame(
            LLMMessagesAppendFrame(
                messages=[
                    {
                        "role": "system",
                        "content": notice,
                    }
                ],
                run_llm=False,
            )
        )

    def _provider_metadata(self) -> dict[str, Any]:
        model = self._settings.model
        return {
            "provider": "OpenAI",
            "provider_class": type(self).__name__,
            "api": "responses",
            "model": model,
            "model_name": model,
        }

    def _upsert_phone_volatile_context(self, context: Any) -> None:
        builder = self._phone_volatile_context_builder
        if builder is None:
            return
        try:
            text = str(builder() or "").strip()
            if not text:
                return
            message = {"role": "system", "content": text}
            messages = list(context.get_messages()) if hasattr(context, "get_messages") else list(context.messages)
            retained_messages: list[Any] = []
            for existing in messages:
                if not isinstance(existing, dict):
                    retained_messages.append(existing)
                    continue
                if existing.get("role") != "system" or not str(existing.get("content") or "").startswith(
                    _PHONE_VOLATILE_CONTEXT_PREFIX
                ):
                    retained_messages.append(existing)
                    continue
            if hasattr(context, "set_messages"):
                context.set_messages([*retained_messages, message])
            elif hasattr(context, "messages"):
                context.messages[:] = [*retained_messages, message]
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.debug("Phone volatile context refresh failed", exc_info=True)

    def _next_attempt_id(self) -> tuple[str, bool]:
        if self._task_trace is None:
            return "", False
        self._task_trace_llm_attempt_seq += 1
        attempt_id = "%s:llm:%04d" % (
            self._task_trace.task_id,
            self._task_trace_llm_attempt_seq,
        )
        first_turn = self._task_trace_llm_attempt_seq == 1
        return attempt_id, first_turn

    def _safe(self, fn, *a, **kw) -> None:
        try:
            fn(*a, **kw)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("TracedOpenAIResponsesLLMService trace emit failed: %s", exc)

    async def _run_queued_trace_emit(
        self,
        previous: asyncio.Task[None] | None,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        if previous is not None:
            results = await asyncio.gather(previous, return_exceptions=True)
            if results and isinstance(results[0], BaseException):
                logger.debug(
                    "TracedOpenAIResponsesLLMService prior trace emit failed: %s",
                    results[0],
                )
        await asyncio.to_thread(self._safe, fn, *args, **kwargs)

    def _schedule_trace_emit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task[None] | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._safe(fn, *args, **kwargs)
            return None

        previous = self._trace_emit_tail
        task = loop.create_task(
            self._run_queued_trace_emit(previous, fn, args, kwargs),
            name="phone-llm-trace-emit",
        )
        self._trace_emit_tail = task
        return task

    async def _drain_trace_emits(self) -> None:
        tail = self._trace_emit_tail
        if tail is None:
            return
        results = await asyncio.gather(tail, return_exceptions=True)
        if results and isinstance(results[0], BaseException):
            logger.debug(
                "TracedOpenAIResponsesLLMService trace emit drain failed: %s",
                results[0],
            )

    def _build_response_params(self, invocation_params: Any) -> dict:
        """Override to enforce phone Responses request policy before sending."""
        params = super()._build_response_params(invocation_params)
        # SEC-08: enforce per-user storage consent on the phone Responses call
        # before the SDK is invoked. Phone calls run on OpenAI's Responses API
        # which defaults ``store=True``, so without this gate every caller's
        # spoken prompts and the called party's transcript are persisted on
        # OpenAI's servers for 30 days regardless of the user's consent flag.
        # The codex-auth transport already forces ``store=False`` for Codex-
        # backed clients, but the direct-OpenAI fallback path (raw api_key) is
        # the gap this guard closes. The privacy policy claims call data is
        # used "only to complete the task," so retention outside the call is
        # a Phase 3 legal alignment break for the called-party context too.
        from services.llm.openai_consent import enforce_storage_consent

        enforce_storage_consent(params)
        # PHONE-LATENCY-01: do NOT set reasoning.summary="auto" on phone calls.
        # The reasoning summary is an extra model output stream (generated in
        # addition to the text reply) used only for trace logging. Requesting it
        # forces the model to produce two outputs per turn — the actual answer
        # AND a summary of its reasoning — adding measurable TTFT on every turn.
        # On cloud call 2c74f645, actual model TTFT averaged ~2344ms; removing
        # this saves the summary-generation overhead without affecting answer
        # quality or call behaviour. The ratchet phone-no-reasoning-summary-phone
        # enforces this going forward.
        if not str(params.get("prompt_cache_key") or "").strip():
            params["prompt_cache_key"] = _phone_prompt_cache_key(params)
        return params

    def _begin_traced_attempt(self, params: dict[str, Any]) -> str | None:
        if self._task_trace is None:
            return None
        attempt_id, first_turn = self._next_attempt_id()
        self._current_attempt_id = attempt_id
        self._current_attempt_chunk_idx = 0
        self._current_response_text = []
        self._current_response_tool_calls = []

        provider = self._provider_metadata()
        request_payload = {
            "model": params.get("model"),
            "stream": params.get("stream"),
            "input": params.get("input"),
            "instructions": params.get("instructions"),
            "tools": params.get("tools"),
            "reasoning": params.get("reasoning"),
            "include": params.get("include"),
            "temperature": params.get("temperature"),
            "max_output_tokens": params.get("max_output_tokens"),
            "service_tier": params.get("service_tier"),
            "prompt_cache_key": params.get("prompt_cache_key"),
        }

        ts = _utc_now()
        traced_request_payload = self._trace_payload(request_payload)
        self._schedule_trace_emit(
            self._task_trace.append_llm_attempt_start,
            ts=ts,
            attempt_id=attempt_id,
            call_kind="phone_call",
            request_mode="responses",
            provider=provider,
            first_turn=first_turn,
            continuity_before={},
            request=traced_request_payload,
        )
        stats = self._request_stats(params)
        self._schedule_trace_emit(
            self._task_trace.append_llm_provider_payload,
            ts=ts,
            attempt_id=attempt_id,
            call_kind="phone_call",
            payload_stage="responses.create",
            payload={
                "model": params.get("model"),
                "stream": params.get("stream"),
                "reasoning": params.get("reasoning"),
                "include": params.get("include"),
                "service_tier": params.get("service_tier"),
                "prompt_cache_key": params.get("prompt_cache_key"),
                "tool_count": stats["tool_count"],
                "input_items": stats["input_items"],
                "instruction_chars": stats["instruction_chars"],
                "request_bytes": stats["request_bytes"],
                "exact_request_event": "trace_llm_attempt_start",
                "exact_request_attempt_id": attempt_id,
            },
            exact=False,
        )
        return attempt_id

    async def _push_llm_text(self, text: str) -> None:
        """Capture text deltas for trace v2 + delegate to parent's frame push."""
        if self._task_trace is not None and self._current_attempt_id and text:
            self._current_response_text.append(text)
            self._current_attempt_chunk_idx += 1
            self._schedule_trace_emit(
                self._task_trace.append_llm_stream_chunk,
                ts=_utc_now(),
                attempt_id=self._current_attempt_id,
                chunk_index=self._current_attempt_chunk_idx,
                chunk_kind="text_delta",
                delta=self._trace_payload({"content": text}),
            )
        await super()._push_llm_text(text)

    async def _process_context(self, context: Any) -> None:
        """Wrap parent's stream consumption to capture the full final response.

        The parent emits `_push_llm_text` per text delta (which we already trace
        via the override above) but does NOT expose any hook for function-call
        events or the terminal `ResponseCompletedEvent`. Without those, the
        trace v2 stream has no record of what tool calls the model emitted, no
        record of the final response, and no `append_llm_attempt_response`
        event ever fires — every attempt's `ts_end` stays None.

        We replicate the parent's stream-iteration loop, add hooks at each
        event type, then delegate the function-call dispatch back to the
        parent's helpers via super._dispatch_function_calls/run_function_calls.
        On any exception we emit `append_llm_attempt_failure` so cancellations
        and errors are observable.
        """
        # PHONE-LATENCY: mark the instant the LLM service begins handling the
        # user turn's context. Closes the far end of the previously-invisible
        # STT-done -> LLM-request window (post_transcript_to_llm_dispatch_ms).
        # Placed first so it captures the true dispatch moment regardless of the
        # trace-writer / super() early-return below.
        recorder = self._phone_latency_recorder
        if recorder is not None:
            try:
                recorder.mark_llm_process_context_start()
            except Exception:  # noqa: BLE001, RUF100 - latency telemetry must never break a live phone turn
                logger.debug("Phone LLM process-context start mark failed", exc_info=True)

        self._upsert_phone_volatile_context(context)
        if self._task_trace is None:
            await super()._process_context(context)
            return

        from contextlib import asynccontextmanager

        from openai import AsyncStream
        from openai.types.responses import (
            ResponseCompletedEvent,
            ResponseFunctionToolCall,
            ResponseStreamEvent,
        )
        from pipecat.adapters.services.open_ai_responses_adapter import (
            OpenAIResponsesLLMAdapter,
        )
        from pipecat.metrics.metrics import LLMTokenUsage
        from pipecat.services.llm_service import FunctionCallFromLLM

        adapter: OpenAIResponsesLLMAdapter = self.get_llm_adapter()
        # Pipecat's OpenAIResponsesAdapter.get_messages_for_logging trips on
        # LLMSpecificMessage instances ("argument of type 'LLMSpecificMessage'
        # is not iterable" at adapter.py:138). Since we now store reasoning
        # items as LLMSpecificMessage in the context, we have to log safely.
        try:
            logged = adapter.get_messages_for_logging(context)
        except Exception:
            logged = "<context-with-LLMSpecificMessage-items>"
        logger.debug("%s: Generating response from universal context %s", self, logged)

        request_prep_start = time.perf_counter()
        stage_start = request_prep_start
        invocation_params = adapter.get_llm_invocation_params(
            context, system_instruction=self._settings.system_instruction
        )
        after_adapter = time.perf_counter()
        self._record_phone_llm_request_stage(
            "adapter_params",
            (after_adapter - stage_start) * 1000.0,
        )
        stage_start = after_adapter
        params = self._build_response_params(invocation_params)
        # NOTE: _ensure_reasoning_summary_requested is NOT called here (see
        # _build_response_params comment — phone calls skip reasoning summary
        # to avoid the per-turn TTFT overhead of generating an extra output stream).
        after_params = time.perf_counter()
        self._record_phone_llm_request_stage(
            "response_params",
            (after_params - stage_start) * 1000.0,
            params=params,
        )
        # Reasoning continuity: ask the API to return the encrypted reasoning
        # content so we can re-feed it on the next turn. Without this, every
        # turn re-reasons from scratch and the codex-spark backend will
        # stochastically return reasoning_tokens=0, which truncates multi-step
        # outputs (e.g., a multi-digit member-ID entry → only the first digit).
        # OpenAI's docs on ResponseReasoningItem say: "Be sure to include
        # these items in your input to the Responses API for subsequent turns
        # of a conversation if you are manually managing context."
        existing_include = params.get("include") or []
        if "reasoning.encrypted_content" not in existing_include:
            params["include"] = list(existing_include) + ["reasoning.encrypted_content"]
        trace_start = time.perf_counter()
        attempt_id = self._begin_traced_attempt(params)
        trace_done = time.perf_counter()
        self._record_phone_llm_request_stage(
            "trace_enqueue",
            (trace_done - trace_start) * 1000.0,
            params=params,
        )
        self._record_phone_llm_request_stage(
            "request_prep",
            (trace_done - request_prep_start) * 1000.0,
            params=params,
        )

        await self.start_ttfb_metrics()

        captured_response: Any = None
        captured_text: list[str] = []
        function_calls: dict[str, dict[str, str]] = {}
        current_arguments: dict[str, str] = {}
        captured_reasoning_items: dict[str, dict[str, Any]] = {}
        seen_stream_event_keys: set[tuple[Any, ...]] = set()
        # PHASE FILTER (gpt-5.4/5.5 reasoning models): an assistant turn can arrive
        # as TWO message items — a `commentary` preamble AND the `final_answer`. Both
        # stream text; speaking both is the phone "double-fire" (on short turns the
        # preamble ~ the final, so it sounds byte-identical). OpenAI documents phase
        # as a field a consumer must handle: speak/commit only the final answer and
        # treat commentary as a preamble. BUT a turn can be commentary-only (the
        # goodbye line that accompanies end_call) OR carry an EMPTY final_answer with
        # the real text in commentary (inverted shape) — those MUST still be spoken.
        # So: buffer commentary per item; if a final_answer item produces SPOKEN text,
        # drop the buffered commentary (it was the duplicate preamble); otherwise flush
        # the commentary at end (it was the real reply). phase==None (non-reasoning
        # models / no phase) streams live, unchanged.
        message_item_phase: dict[str, str | None] = {}
        commentary_buffer: dict[str, list[str]] = {}
        spoken_text: list[str] = []
        final_answer_spoke = False

        try:
            stream: AsyncStream[ResponseStreamEvent] = await self._client.responses.create(**params)

            @asynccontextmanager
            async def _closing(stream: Any):
                chunk_iter = stream.__aiter__()
                try:
                    yield chunk_iter
                finally:
                    if hasattr(chunk_iter, "aclose"):
                        await chunk_iter.aclose()
                    if hasattr(stream, "close"):
                        await stream.close()
                    elif hasattr(stream, "aclose"):
                        await stream.aclose()

            async with _closing(stream) as event_iter:
                async for event in event_iter:
                    replay_key = _responses_stream_event_replay_key(event)
                    if replay_key is not None:
                        if replay_key in seen_stream_event_keys:
                            logger.debug(
                                "Dropped replayed Responses stream event: %s",
                                replay_key,
                            )
                            continue
                        seen_stream_event_keys.add(replay_key)

                    if isinstance(event, ResponseTextDeltaEvent):
                        await self.stop_ttfb_metrics()
                        captured_text.append(event.delta or "")
                        # Route by the emitting item's phase. commentary -> hold
                        # (pending the final-answer decision); final_answer/unknown ->
                        # speak live so streaming latency is preserved.
                        delta_phase = message_item_phase.get(getattr(event, "item_id", "") or "")
                        if delta_phase == "commentary":
                            commentary_buffer.setdefault(getattr(event, "item_id", "") or "", []).append(
                                event.delta or ""
                            )
                        else:
                            if delta_phase == "final_answer" and (event.delta or "").strip():
                                final_answer_spoke = True
                            spoken_text.append(event.delta or "")
                            await self._push_llm_text(event.delta)

                    elif isinstance(event, ResponseOutputItemAddedEvent):
                        await self.stop_ttfb_metrics()
                        item = event.item
                        if isinstance(item, ResponseFunctionToolCall):
                            item_id = item.id or ""
                            function_calls[item_id] = {
                                "name": item.name,
                                "call_id": item.call_id,
                                "arguments": "",
                            }
                            current_arguments[item_id] = ""
                        elif getattr(item, "type", None) == "message":
                            # Record the message item's phase so the text-delta handler
                            # above can route this item's deltas. phase is populated at
                            # item-add time, so filtering stays live (no buffering of
                            # the final answer).
                            message_item_phase[getattr(item, "id", "") or ""] = getattr(item, "phase", None)

                    elif isinstance(event, ResponseFunctionCallArgumentsDeltaEvent):
                        item_id = event.item_id
                        if item_id in current_arguments:
                            current_arguments[item_id] += event.delta

                    elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
                        item_id = event.item_id
                        if item_id in function_calls:
                            function_calls[item_id]["arguments"] = event.arguments
                            self._current_attempt_chunk_idx += 1
                            self._schedule_trace_emit(
                                self._task_trace.append_llm_stream_chunk,
                                ts=_utc_now(),
                                attempt_id=attempt_id,
                                chunk_index=self._current_attempt_chunk_idx,
                                chunk_kind="function_call",
                                delta=self._trace_payload(
                                    {
                                        "name": function_calls[item_id].get("name") or "",
                                        "call_id": function_calls[item_id].get("call_id") or "",
                                        "arguments": event.arguments,
                                    }
                                ),
                            )

                    elif isinstance(event, ResponseOutputItemDoneEvent):
                        item = event.item
                        if isinstance(item, ResponseFunctionToolCall):
                            item_id = item.id or ""
                            if item_id in function_calls:
                                function_calls[item_id]["name"] = item.name
                                function_calls[item_id]["call_id"] = item.call_id
                                function_calls[item_id]["arguments"] = item.arguments
                        elif isinstance(item, ResponseReasoningItem):
                            _merge_reasoning_item(captured_reasoning_items, item=item)

                    elif isinstance(event, ResponseReasoningSummaryTextDeltaEvent):
                        _append_indexed_text_delta(
                            captured_reasoning_items,
                            item_id=event.item_id,
                            bucket="_summary_by_index",
                            index=event.summary_index,
                            delta=event.delta,
                        )

                    elif isinstance(event, ResponseReasoningSummaryTextDoneEvent):
                        _set_indexed_text(
                            captured_reasoning_items,
                            item_id=event.item_id,
                            bucket="_summary_by_index",
                            index=event.summary_index,
                            text=event.text,
                        )
                        self._current_attempt_chunk_idx += 1
                        self._schedule_trace_emit(
                            self._task_trace.append_llm_stream_chunk,
                            ts=_utc_now(),
                            attempt_id=attempt_id,
                            chunk_index=self._current_attempt_chunk_idx,
                            chunk_kind="reasoning_summary",
                            delta={"text": event.text},
                        )

                    elif isinstance(event, ResponseReasoningSummaryPartDoneEvent):
                        _set_indexed_text(
                            captured_reasoning_items,
                            item_id=event.item_id,
                            bucket="_summary_by_index",
                            index=event.summary_index,
                            text=getattr(event.part, "text", None),
                        )

                    elif isinstance(event, ResponseReasoningTextDeltaEvent):
                        _append_indexed_text_delta(
                            captured_reasoning_items,
                            item_id=event.item_id,
                            bucket="_content_by_index",
                            index=event.content_index,
                            delta=event.delta,
                        )

                    elif isinstance(event, ResponseReasoningTextDoneEvent):
                        _set_indexed_text(
                            captured_reasoning_items,
                            item_id=event.item_id,
                            bucket="_content_by_index",
                            index=event.content_index,
                            text=event.text,
                        )

                    elif isinstance(event, ResponseCompletedEvent):
                        captured_response = event.response
                        if captured_response and captured_response.usage:
                            usage = captured_response.usage
                            tokens = LLMTokenUsage(
                                prompt_tokens=usage.input_tokens,
                                completion_tokens=usage.output_tokens,
                                total_tokens=usage.total_tokens,
                                cache_read_input_tokens=usage.input_tokens_details.cached_tokens,
                                reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
                            )
                            await self.start_llm_usage_metrics(tokens)
                            self._record_phone_llm_usage(usage)
                        if captured_response is not None:
                            self._full_model_name = captured_response.model
        except Exception as exc:
            self._schedule_trace_emit(
                self._task_trace.append_llm_attempt_failure,
                ts=_utc_now(),
                attempt_id=attempt_id or "",
                call_kind="phone_call",
                error=self._trace_payload(_openai_exception_payload(exc)),
                continuity_after={},
            )
            await self._drain_trace_emits()
            raise

        # PHASE FILTER tail: if no final_answer produced spoken text, the buffered
        # commentary WAS the real reply (a commentary-only goodbye paired with
        # end_call, or an inverted shape where final_answer was empty) — speak it now,
        # before the function-call dispatch below, so the turn is not silent and
        # end_call is not a bare tool-only hangup. If a final_answer DID speak, the
        # buffered commentary was the duplicate preamble and is dropped.
        if not final_answer_spoke and commentary_buffer:
            for _cid, deltas in commentary_buffer.items():
                for delta_text in deltas:
                    spoken_text.append(delta_text)
                    await self._push_llm_text(delta_text)

        # Emit terminal response event.
        response_payload: dict[str, Any] = {
            "model": (getattr(captured_response, "model", None) if captured_response else None),
            "id": getattr(captured_response, "id", None) if captured_response else None,
            "status": (getattr(captured_response, "status", None) if captured_response else None),
            "text": "".join(captured_text),
            "tool_calls": [
                {
                    "name": fc.get("name") or "",
                    "call_id": fc.get("call_id") or "",
                    "arguments": fc.get("arguments") or "",
                }
                for fc in function_calls.values()
            ],
        }
        if captured_response is not None and getattr(captured_response, "usage", None) is not None:
            usage = captured_response.usage
            response_payload["usage"] = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
                "cached_input_tokens": (
                    getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0)
                    if getattr(usage, "input_tokens_details", None)
                    else 0
                ),
                "reasoning_tokens": (
                    getattr(
                        getattr(usage, "output_tokens_details", None),
                        "reasoning_tokens",
                        0,
                    )
                    if getattr(usage, "output_tokens_details", None)
                    else 0
                ),
            }

        finalized_reasoning_items = _finalize_reasoning_items(captured_reasoning_items)
        reasoning_payload = _reasoning_trace_payload(finalized_reasoning_items)
        if reasoning_payload is not None:
            response_payload["reasoning"] = reasoning_payload

        self._schedule_trace_emit(
            self._task_trace.append_llm_attempt_response,
            ts=_utc_now(),
            attempt_id=attempt_id or "",
            call_kind="phone_call",
            response=self._trace_payload(response_payload),
            continuity_after={},
        )

        # Reasoning continuity: stash the encrypted reasoning items into the
        # LLMContext so the responses-adapter passes them back as input on the
        # next turn. They go in BEFORE any text/tool-call assistant items so
        # the API sees the reasoning preamble in the same logical position it
        # was emitted.
        # This re-feed exists to fix multi-step truncation (e.g. a multi-digit
        # member-ID entry collapsing to the first digit) when the backend would
        # otherwise return reasoning_tokens=0. It is NOT a double-fire lever: the
        # phone "double-fire" was the provider returning a commentary + a
        # final_answer message item and our code speaking both, fixed by the
        # phase filter in _process_context (speak only final_answer). An earlier
        # default-off "re-feed amplifier" A/B gate here was removed once the
        # phase root cause was confirmed.
        for ritem in finalized_reasoning_items:
            if "encrypted_content" not in ritem:
                continue
            try:
                context.add_message(LLMSpecificMessage(llm="openai_responses", message=ritem))
            except Exception as exc:
                logger.warning("%s: failed to add reasoning item to context: %s", self, exc)

        # Direct-commit path: when enabled by the caller, commit the assistant's
        # full text turn to the LLM context HERE — once, synchronously. Required
        # when the surrounding pipeline disables TTS-derived context aggregation
        # (push_text_frames=False) to prevent TTS-timing-coupled message splits.
        # Disabled by default: production topology keeps the Pipecat universal
        # aggregator's TTSTextFrame → push_aggregation path so this would
        # double-add. Loopback session enables it because it also disables the
        # TTS-side text-frame emission via push_text_frames=False.
        if self._direct_context_commit:
            # Commit only what was SPOKEN (final_answer, or commentary-as-sole-reply),
            # never the dropped commentary preamble — else the duplicate re-enters
            # context. Prod's TTS-derived aggregation already gets only spoken text.
            full_text = "".join(spoken_text).strip()
            if full_text:
                try:
                    context.add_message({"role": "assistant", "content": full_text})
                except Exception as exc:
                    logger.warning("%s: failed to add assistant message to context: %s", self, exc)

        # Dispatch function calls (mirror parent's tail). A live human call must
        # not hang up on an empty assistant turn; the registered end_call
        # handler owns that refusal inside Pipecat's normal function-call
        # lifecycle so the result is attached to a running tool call.
        if function_calls:
            full_response_had_spoken_text = bool("".join(spoken_text).strip())
            fc_list: list[FunctionCallFromLLM] = []
            for _item_id, fc in function_calls.items():
                arguments = _decode_function_call_arguments(fc)
                if str(fc.get("name") or "").strip() == "end_call":
                    arguments = {
                        **arguments,
                        "_viola_response_had_spoken_text": full_response_had_spoken_text,
                    }
                fc_list.append(
                    FunctionCallFromLLM(
                        context=context,
                        tool_call_id=fc.get("call_id") or "",
                        function_name=fc.get("name") or "",
                        arguments=arguments,
                    )
                )
            await self.run_function_calls(fc_list)

        await self._drain_trace_emits()
