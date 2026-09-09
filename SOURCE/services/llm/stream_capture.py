"""Trace capture helpers for provider streaming chunks."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from config import env as config_env
from core.logging_config import get_logger
from services.llm.stream_bus import publish_text_delta, publish_thinking_delta

logger = get_logger(__name__)

DEFAULT_STREAM_CHUNK_AGG_N = 50
DEFAULT_STREAM_CHUNK_AGG_MS = 250.0
STREAM_CHUNK_AGG_N_ENV = "VIOLA_TRACE_STREAM_CHUNK_AGG_N"
STREAM_CHUNK_AGG_MS_ENV = "VIOLA_TRACE_STREAM_CHUNK_AGG_MS"

_TERMINAL_CHUNK_KINDS = {"stop", "usage", "error"}
_STREAM_CHUNK_KINDS = {
    "text_delta",
    "tool_args_delta",
    "reasoning_delta",
    "stop",
    "usage",
    "raw",
    "error",
}
_OPENAI_NON_TERMINAL_RAW_EVENTS = {
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
}


@dataclass(slots=True)
class _KindBuffer:
    payloads: list[dict[str, Any]] = field(default_factory=list)
    first_index: int = 0
    last_index: int = 0
    started_monotonic: float = 0.0


class StreamChunkAggregator:
    """Buffer streaming chunks per attempt and emit bounded trace events."""

    def __init__(
        self,
        *,
        attempt_id: str,
        task_trace: Any | None,
        agg_n: int | None = None,
        agg_ms: float | None = None,
    ) -> None:
        self.attempt_id = attempt_id
        self.task_trace = task_trace
        self.agg_n = _positive_int(
            agg_n if agg_n is not None else config_env.get_int(STREAM_CHUNK_AGG_N_ENV),
            DEFAULT_STREAM_CHUNK_AGG_N,
        )
        self.agg_ms = _positive_float(
            agg_ms if agg_ms is not None else config_env.get_float(STREAM_CHUNK_AGG_MS_ENV),
            DEFAULT_STREAM_CHUNK_AGG_MS,
        )
        self._buffers: dict[str, _KindBuffer] = {}
        self._seen_by_kind: dict[str, int] = {}
        self._next_raw_index = 0
        self._terminal_emitted: set[str] = set()

    def add(self, chunk_kind: str, delta_payload: dict[str, Any]) -> None:
        """Add one provider stream chunk to the aggregate buffer."""
        kind = _normalize_chunk_kind(chunk_kind)
        if kind == "text_delta":
            publish_text_delta(_first_text(delta_payload, "text", "delta", "content"))
        if kind == "reasoning_delta":
            publish_thinking_delta(_first_text(delta_payload, "text", "delta", "content"))
        if kind in _TERMINAL_CHUNK_KINDS:
            self.emit_terminal(kind, delta_payload)
            return

        now = time.monotonic()
        raw_index = self._next_raw_index
        self._next_raw_index += 1

        seen_count = self._seen_by_kind.get(kind, 0)
        self._seen_by_kind[kind] = seen_count + 1
        payload = dict(delta_payload)

        if seen_count == 0:
            self._emit(kind, [payload], first_index=raw_index, last_index=raw_index)
            return

        buffer = self._buffers.get(kind)
        if buffer is None or not buffer.payloads:
            buffer = _KindBuffer(
                payloads=[],
                first_index=raw_index,
                last_index=raw_index,
                started_monotonic=now,
            )
            self._buffers[kind] = buffer

        buffer.payloads.append(payload)
        buffer.last_index = raw_index

        elapsed_ms = (now - buffer.started_monotonic) * 1000.0
        if len(buffer.payloads) >= self.agg_n or elapsed_ms >= self.agg_ms:
            self._flush_kind(kind)

    def flush(self, *, force: bool = False) -> None:
        """Flush buffered chunks that reached thresholds, or all chunks when forced."""
        if force:
            pending = sorted(
                ((kind, buffer) for kind, buffer in self._buffers.items() if buffer.payloads),
                key=lambda item: item[1].first_index,
            )
            for kind, _buffer in pending:
                self._flush_kind(kind)
            return

        now = time.monotonic()
        for kind, buffer in list(self._buffers.items()):
            if not buffer.payloads:
                continue
            elapsed_ms = (now - buffer.started_monotonic) * 1000.0
            if len(buffer.payloads) >= self.agg_n or elapsed_ms >= self.agg_ms:
                self._flush_kind(kind)

    def emit_terminal(self, chunk_kind: str, payload: dict[str, Any]) -> None:
        """Emit a singleton terminal event immediately."""
        kind = _normalize_chunk_kind(chunk_kind)
        if kind not in _TERMINAL_CHUNK_KINDS:
            self.add(kind, payload)
            return
        if kind in self._terminal_emitted:
            return

        self.flush(force=True)
        raw_index = self._next_raw_index
        self._next_raw_index += 1
        self._emit(kind, [dict(payload)], first_index=raw_index, last_index=raw_index)
        self._terminal_emitted.add(kind)

    def _flush_kind(self, kind: str) -> None:
        buffer = self._buffers.get(kind)
        if buffer is None or not buffer.payloads:
            return
        payloads = buffer.payloads
        first_index = buffer.first_index
        last_index = buffer.last_index
        self._buffers[kind] = _KindBuffer()
        self._emit(kind, payloads, first_index=first_index, last_index=last_index)

    def _emit(
        self,
        chunk_kind: str,
        payloads: list[dict[str, Any]],
        *,
        first_index: int,
        last_index: int,
    ) -> None:
        if not payloads:
            return
        delta = _aggregate_delta(
            chunk_kind,
            payloads,
            first_index=first_index,
            last_index=last_index,
        )
        try:
            if self.task_trace is None:
                return
            self.task_trace.append_llm_stream_chunk(
                ts=_utc_now_iso(),
                attempt_id=self.attempt_id,
                chunk_index=first_index,
                chunk_kind=chunk_kind,
                delta=delta,
            )
        except Exception:
            logger.exception("LLM stream trace chunk write failed")


def capture_openai_responses_stream_event(aggregator: StreamChunkAggregator, event: Any) -> None:
    """Map one OpenAI Responses stream event into a trace stream chunk."""
    event_type = str(_get_attr_or_item(event, "type", "") or "")
    if not event_type:
        aggregator.add("raw", {"event": _json_safe(event)})
        return

    if event_type == "response.output_text.delta":
        text = str(_get_attr_or_item(event, "delta", "") or "")
        if text:
            aggregator.add(
                "text_delta",
                {
                    "event_type": event_type,
                    "text": text,
                    "output_index": _get_attr_or_item(event, "output_index"),
                    "content_index": _get_attr_or_item(event, "content_index"),
                    "item_id": _get_attr_or_item(event, "item_id"),
                },
            )
        return

    if event_type == "response.function_call_arguments.delta":
        arguments = str(_get_attr_or_item(event, "delta", "") or "")
        if arguments:
            aggregator.add(
                "tool_args_delta",
                {
                    "event_type": event_type,
                    "arguments": arguments,
                    "output_index": _get_attr_or_item(event, "output_index"),
                    "item_id": _get_attr_or_item(event, "item_id"),
                },
            )
        return

    if "reasoning" in event_type and event_type.endswith(".delta"):
        text = str(_get_attr_or_item(event, "delta", "") or "")
        if text:
            aggregator.add(
                "reasoning_delta",
                {
                    "event_type": event_type,
                    "text": text,
                    "output_index": _get_attr_or_item(event, "output_index"),
                    "item_id": _get_attr_or_item(event, "item_id"),
                },
            )
        return

    if event_type in _OPENAI_NON_TERMINAL_RAW_EVENTS:
        aggregator.add("raw", _summarize_stream_event(event))


def summarize_openai_response(response: Any) -> dict[str, Any]:
    """Return a compact stop payload for a completed Responses stream."""
    if response is None:
        return {"available": False}
    output = _get_attr_or_item(response, "output")
    output_count = len(output) if isinstance(output, list) else None
    return {
        "available": True,
        "id": _get_attr_or_item(response, "id"),
        "model": _get_attr_or_item(response, "model"),
        "status": _get_attr_or_item(response, "status"),
        "stop_reason": _get_attr_or_item(response, "stop_reason"),
        "finish_reason": _get_attr_or_item(response, "finish_reason"),
        "output_count": output_count,
    }


def summarize_usage(usage: Any) -> dict[str, Any]:
    """Return the terminal usage payload in JSON-safe form."""
    if usage is None:
        return {"available": False}
    payload = _json_safe(usage)
    if isinstance(payload, dict):
        payload["available"] = True
        return payload
    return {"available": True, "usage": payload}


def summarize_stream_error(exc: BaseException) -> dict[str, Any]:
    """Return a terminal error payload for a failed stream."""
    payload: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    for attr in ("status_code", "status", "code", "request_id"):
        value = getattr(exc, attr, None)
        if value is not None:
            payload[attr] = value
    return payload


def _aggregate_delta(
    chunk_kind: str,
    payloads: list[dict[str, Any]],
    *,
    first_index: int,
    last_index: int,
) -> dict[str, Any]:
    metadata = {
        "chunk_count": len(payloads),
        "first_chunk_index": first_index,
        "last_chunk_index": last_index,
        "aggregated": len(payloads) > 1,
    }
    if chunk_kind in _TERMINAL_CHUNK_KINDS:
        delta = dict(payloads[-1])
        delta.update(metadata)
        return delta
    if chunk_kind == "text_delta":
        text = "".join(_first_text(payload, "text", "delta", "content") for payload in payloads)
        return {
            **metadata,
            "text": text,
            "chars": len(text),
            "event_types": _event_type_counts(payloads),
        }
    if chunk_kind == "reasoning_delta":
        text = "".join(_first_text(payload, "text", "delta", "content") for payload in payloads)
        return {
            **metadata,
            "text": text,
            "chars": len(text),
            "event_types": _event_type_counts(payloads),
        }
    if chunk_kind == "tool_args_delta":
        return _aggregate_tool_args(payloads, metadata)
    if chunk_kind == "raw":
        return {
            **metadata,
            "event_types": _event_type_counts(payloads),
            "events": payloads,
        }
    return {
        **metadata,
        "items": payloads,
    }


def _aggregate_tool_args(payloads: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    by_item: dict[str, dict[str, Any]] = {}
    all_arguments: list[str] = []
    for payload in payloads:
        arguments = _first_text(payload, "arguments", "delta")
        all_arguments.append(arguments)
        item_key = str(payload.get("item_id") or payload.get("output_index") or "0")
        stream = by_item.setdefault(
            item_key,
            {
                "item_id": payload.get("item_id"),
                "output_index": payload.get("output_index"),
                "arguments": "",
            },
        )
        stream["arguments"] = str(stream.get("arguments") or "") + arguments
    return {
        **metadata,
        "arguments": "".join(all_arguments),
        "chars": sum(len(part) for part in all_arguments),
        "tool_args": list(by_item.values()),
        "event_types": _event_type_counts(payloads),
    }


def _summarize_stream_event(event: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"event_type": str(_get_attr_or_item(event, "type", "") or "")}
    for key in (
        "sequence_number",
        "response_id",
        "item_id",
        "output_index",
        "content_index",
        "part",
        "item",
    ):
        value = _get_attr_or_item(event, key)
        if value is not None:
            summary[key] = _json_safe(value)
    return summary


def _event_type_counts(payloads: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for payload in payloads:
        event_type = str(payload.get("event_type") or payload.get("type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return ""


def _normalize_chunk_kind(chunk_kind: str) -> str:
    kind = str(chunk_kind or "raw")
    return kind if kind in _STREAM_CHUNK_KINDS else "raw"


def _positive_int(raw: int | None, default: int) -> int:
    if raw is None or raw <= 0:
        return default
    return raw


def _positive_float(raw: float | None, default: float) -> float:
    if raw is None or raw <= 0:
        return default
    return raw


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _get_attr_or_item(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump())
        except Exception:
            logger.debug("Failed to model_dump stream event payload")
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict())
        except Exception:
            logger.debug("Failed to to_dict stream event payload")
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict):
        return {str(key): _json_safe(inner) for key, inner in value_dict.items() if not str(key).startswith("_")}
    return str(value)
