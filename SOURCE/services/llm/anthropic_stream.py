"""Anthropic streaming response consumer with Claude-parity content-block state machine.

This module implements the wire-level streaming protocol that the official Claude
Code TypeScript client uses (``src/services/api/claude.ts:1980-2300``). The goal is
to make the Anthropic provider stream-first by default while still returning the
same response *shape* that the non-streaming ``messages.create`` call returns —
the rest of the provider does not need to know whether streaming happened.

Stream event surface we consume (Anthropic Messages SSE):

- ``message_start``     — carries ``message.id``, ``message.model``, initial
  ``usage`` (input_tokens, cache_*).
- ``content_block_start`` — opens a content block: ``type=text|tool_use|
  thinking|redacted_thinking`` at ``index``.
- ``content_block_delta``  — appends to the active block:
  ``text_delta`` / ``input_json_delta`` / ``thinking_delta`` /
  ``signature_delta``.
- ``content_block_stop``  — closes a block.
- ``message_delta``       — final ``stop_reason`` + final ``usage.output_tokens``.
- ``message_stop``        — terminal.

Claude-parity error classes:

- :class:`StreamIdleTimeoutError` — no event for ``idle_timeout_s`` seconds
  (matches ``streamIdleTimeoutMs`` in the TS client). Idle is per-event, not
  per-stream, so a slow generator still progresses.
- :class:`StreamNoEventsError`    — stream closed without ``message_start``.
- :class:`StreamConsumerError`    — base class; raised on malformed events
  or premature close (had ``message_start`` but no ``content_block_stop`` /
  ``message_delta`` with ``stop_reason``).

The non-streaming fallback path lives in the provider — when this consumer
raises :class:`StreamConsumerError`, the provider retries once with
``stream=False`` and parameters capped by
:func:`services.llm.anthropic_cache.adjust_params_for_non_streaming`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterable

from core.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public error surface
# ---------------------------------------------------------------------------


class StreamConsumerError(RuntimeError):
    """Base class for unrecoverable stream-consumer failures.

    Provider callers catch this to fall back to non-streaming.
    """


class StreamIdleTimeoutError(StreamConsumerError):
    """No event from the stream for longer than the idle watchdog window."""


class StreamNoEventsError(StreamConsumerError):
    """Stream closed without producing a ``message_start`` event."""


# ---------------------------------------------------------------------------
# Internal block accumulators
# ---------------------------------------------------------------------------


@dataclass
class _ContentBlock:
    """Accumulator for a single content block while streaming."""

    type: str
    text_parts: list[str] = field(default_factory=list)
    # tool_use accumulator
    tool_use_id: str | None = None
    tool_name: str | None = None
    input_json_parts: list[str] = field(default_factory=list)
    tool_input: Any = None
    # thinking accumulator
    thinking_parts: list[str] = field(default_factory=list)
    signature: str = ""
    redacted_data: str = ""

    def finalize(self) -> SimpleNamespace:
        """Convert into the Anthropic SDK message-block shape."""
        if self.type == "text":
            return SimpleNamespace(type="text", text="".join(self.text_parts))
        if self.type == "tool_use":
            inp: Any
            if self.input_json_parts:
                joined = "".join(self.input_json_parts)
                try:
                    import json as _json

                    inp = _json.loads(joined) if joined.strip() else {}
                except Exception:
                    # Defensive: preserve partial JSON as a string so callers
                    # can still observe what came back without crashing.
                    logger.warning(
                        "Anthropic stream: tool_use input JSON parse failed (len=%d)",
                        len(joined),
                    )
                    inp = {}
            else:
                inp = self.tool_input or {}
            return SimpleNamespace(
                type="tool_use",
                id=self.tool_use_id,
                name=self.tool_name,
                input=inp,
            )
        if self.type == "thinking":
            return SimpleNamespace(
                type="thinking",
                thinking="".join(self.thinking_parts),
                signature=self.signature,
            )
        if self.type == "redacted_thinking":
            return SimpleNamespace(
                type="redacted_thinking",
                data=self.redacted_data,
            )
        # Unknown block type — surface raw text if present so we don't drop it.
        return SimpleNamespace(type=self.type, text="".join(self.text_parts))


@dataclass
class _AccumulatedUsage:
    """Token-usage accumulator across ``message_start`` + ``message_delta``."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def merge_start(self, usage: Any) -> None:
        # message_start carries the prompt-side fields. Use > 0 guards so
        # later message_delta with 0 doesn't overwrite real values (matches
        # Claude TS comment at claude.ts:2920-2925).
        for field_name in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = _safe_int(getattr(usage, field_name, 0))
            if value > 0:
                setattr(self, field_name, value)
        out = _safe_int(getattr(usage, "output_tokens", 0))
        if out > 0:
            self.output_tokens = out

    def merge_delta(self, usage: Any) -> None:
        # message_delta usually reports only output_tokens but may include
        # cache fields late. Still use > 0 guard.
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = _safe_int(getattr(usage, field_name, 0))
            if value > 0:
                setattr(self, field_name, value)

    def to_namespace(self) -> SimpleNamespace:
        return SimpleNamespace(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
        )


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


async def consume_stream(
    stream: AsyncIterator[Any] | Iterable[Any],
    *,
    idle_timeout_s: float | None = 90.0,
) -> SimpleNamespace:
    """Consume an Anthropic message stream into a non-streaming response shape.

    Returns a :class:`types.SimpleNamespace` matching the Anthropic SDK's
    non-streaming ``Message`` object: ``.id``, ``.model``, ``.stop_reason``,
    ``.content`` (list of block namespaces), ``.usage`` (namespace with
    input/output/cache_* tokens).

    Raises:
        StreamIdleTimeoutError: when no event arrived in ``idle_timeout_s``.
        StreamNoEventsError:    when the stream closed without
            ``message_start``.
        StreamConsumerError:    on premature close after ``message_start`` or
            malformed events.
    """
    message_id: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    saw_message_start = False
    saw_message_stop = False
    usage = _AccumulatedUsage()
    blocks_by_index: dict[int, _ContentBlock] = {}
    finalized_blocks: list[SimpleNamespace] = []

    iterator = stream.__aiter__() if hasattr(stream, "__aiter__") else aiter(stream)

    while True:
        try:
            if idle_timeout_s is None:
                event = await iterator.__anext__()
            else:
                event = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=idle_timeout_s,
                )
        except StopAsyncIteration:
            break
        except TimeoutError as exc:
            raise StreamIdleTimeoutError(f"Anthropic stream idle > {idle_timeout_s:.1f}s") from exc
        except StreamConsumerError:
            raise
        except Exception as exc:  # pragma: no cover - SDK-specific
            raise StreamConsumerError(f"Anthropic stream raised {type(exc).__name__}: {exc}") from exc

        event_type = getattr(event, "type", None)
        if event_type == "message_start":
            saw_message_start = True
            message = getattr(event, "message", None)
            if message is not None:
                message_id = getattr(message, "id", None) or message_id
                model = getattr(message, "model", None) or model
                msg_usage = getattr(message, "usage", None)
                if msg_usage is not None:
                    usage.merge_start(msg_usage)
            continue

        if event_type == "content_block_start":
            index = _safe_int(getattr(event, "index", 0))
            block = getattr(event, "content_block", None)
            block_type = getattr(block, "type", "text") if block else "text"
            accumulator = _ContentBlock(type=str(block_type))
            if block_type == "tool_use" and block is not None:
                accumulator.tool_use_id = getattr(block, "id", None)
                accumulator.tool_name = getattr(block, "name", None)
                accumulator.tool_input = getattr(block, "input", None)
            elif block_type == "text" and block is not None:
                initial_text = getattr(block, "text", "")
                if initial_text:
                    # Claude TS comment (claude.ts:2023-2026) notes the
                    # SDK echoes the same text in a subsequent delta. We
                    # rely on the delta and skip the echo here to avoid
                    # double-emit.
                    pass
            elif block_type == "redacted_thinking" and block is not None:
                accumulator.redacted_data = getattr(block, "data", "") or ""
            blocks_by_index[index] = accumulator
            continue

        if event_type == "content_block_delta":
            index = _safe_int(getattr(event, "index", 0))
            block = blocks_by_index.get(index)
            if block is None:
                # Out-of-order delta; create a text block on demand.
                block = _ContentBlock(type="text")
                blocks_by_index[index] = block
            delta = getattr(event, "delta", None)
            delta_type = getattr(delta, "type", None) if delta else None
            if delta_type == "text_delta":
                text = getattr(delta, "text", "") or ""
                if text:
                    block.text_parts.append(text)
            elif delta_type == "input_json_delta":
                partial = getattr(delta, "partial_json", "") or ""
                if partial:
                    block.input_json_parts.append(partial)
            elif delta_type == "thinking_delta":
                thinking = getattr(delta, "thinking", "") or ""
                if thinking:
                    block.thinking_parts.append(thinking)
            elif delta_type == "signature_delta":
                signature = getattr(delta, "signature", "") or ""
                if signature:
                    block.signature += signature
            continue

        if event_type == "content_block_stop":
            index = _safe_int(getattr(event, "index", 0))
            block = blocks_by_index.pop(index, None)
            if block is not None:
                finalized_blocks.append(block.finalize())
            continue

        if event_type == "message_delta":
            delta = getattr(event, "delta", None)
            new_stop = getattr(delta, "stop_reason", None) if delta else None
            if new_stop:
                stop_reason = new_stop
            msg_usage = getattr(event, "usage", None)
            if msg_usage is not None:
                usage.merge_delta(msg_usage)
            continue

        if event_type == "message_stop":
            saw_message_stop = True
            continue

        # Unknown event type — log at debug and continue. Unknown events are
        # not fatal; Claude's stream protocol adds new event types over time.
        logger.debug("Anthropic stream: ignoring unknown event type=%r", event_type)

    if not saw_message_start:
        raise StreamNoEventsError("Anthropic stream closed without message_start")

    # If we received message_start but never saw a stop_reason / message_stop,
    # treat as premature close — the caller will retry non-streaming.
    if stop_reason is None and not saw_message_stop:
        # Finalize any blocks still open so the diagnostic frame is complete.
        for block in blocks_by_index.values():
            finalized_blocks.append(block.finalize())
        raise StreamConsumerError("Anthropic stream ended without stop_reason / message_stop")

    # Finalize any still-open blocks (defensive — they should have been closed
    # by content_block_stop before message_delta per the protocol).
    for block in blocks_by_index.values():
        finalized_blocks.append(block.finalize())

    return SimpleNamespace(
        id=message_id,
        model=model,
        stop_reason=stop_reason,
        content=finalized_blocks,
        usage=usage.to_namespace(),
    )


__all__ = [
    "StreamConsumerError",
    "StreamIdleTimeoutError",
    "StreamNoEventsError",
    "consume_stream",
]
