"""Schedule streamed tool calls with ordered barriers and explicit cancellation.

Safe calls may overlap. A serial call forms a barrier for later submissions.
Outputs retain their invocation identity; failed streaming attempts discard
their outputs and cancel unfinished work before the caller retries.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from core.logging_config import get_logger
from services.conversation.message_invariants import synthetic_tool_result_content, synthetic_tool_use_result

logger = get_logger(__name__)
ToolStatus = Literal["queued", "executing", "completed", "yielded"]
InterruptBehavior = Literal["cancel", "block"]
CancelReason = Literal["sibling_error", "user_interrupted", "streaming_fallback"]


@dataclass
class ToolDefinition:
    name: str
    is_concurrency_safe: Callable[[dict[str, Any]], bool] = field(default=lambda args: False)
    interrupt_behavior: Callable[[], InterruptBehavior] = field(default=lambda: "block")


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AssistantMessage:
    uuid: str
    content: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ProgressUpdate:
    tool_use_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultMessage:
    tool_use_id: str
    content: Any
    is_error: bool = False
    tool_use_result: str | None = None
    source_tool_assistant_uuid: str | None = None

    def to_native_block(self) -> dict[str, Any]:
        result = dict(type="tool_result", tool_use_id=self.tool_use_id, content=self.content)
        if self.is_error:
            result["is_error"] = True
        return result


ToolRunner = Callable[
    [ToolUseBlock, AssistantMessage, asyncio.Event], AsyncIterator[ProgressUpdate | ToolResultMessage]
]


@dataclass
class _Invocation:
    block: ToolUseBlock
    assistant: AssistantMessage
    parallel: bool
    interruptible: bool
    status: ToolStatus = "queued"
    progress: deque[ProgressUpdate] = field(default_factory=deque)
    results: list[ToolResultMessage] = field(default_factory=list)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


def _make_synthetic_error(
    tool_use_id: str, reason: CancelReason, *, assistant_uuid: str | None, errored_tool_description: str = ""
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_use_id=tool_use_id,
        content=synthetic_tool_result_content(reason, errored_tool_description=errored_tool_description),
        is_error=True,
        tool_use_result=synthetic_tool_use_result(reason, errored_tool_description=errored_tool_description),
        source_tool_assistant_uuid=assistant_uuid,
    )


class StreamingToolExecutor:
    def __init__(
        self,
        tool_definitions: dict[str, ToolDefinition],
        tool_runner: ToolRunner,
        *,
        parent_cancel_event: asyncio.Event | None = None,
        user_interrupt_event: asyncio.Event | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._definitions = tool_definitions
        self._runner = tool_runner
        self._loop = loop or asyncio.get_event_loop()
        self._entries: list[_Invocation] = []
        self._changed = asyncio.Event()
        self._discarded = False
        self._discard = asyncio.Event()
        self._failure = asyncio.Event()
        self._failure_description = ""
        self._external_stops = tuple(
            event for event in (user_interrupt_event, parent_cancel_event) if event is not None
        )

    @property
    def is_discarded(self) -> bool:
        return self._discarded

    def discard(self) -> None:
        if self._discarded:
            return
        self._discarded = True
        self._discard.set()
        for entry in self._entries:
            entry.stop.set()
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
        self._changed.set()

    def add_tool(self, block: ToolUseBlock, assistant_message: AssistantMessage) -> None:
        if self._discarded:
            return
        definition = self._definitions.get(block.name)
        parallel, interruptible = False, False
        if definition is not None:
            try:
                parallel = bool(definition.is_concurrency_safe(block.input))
            except Exception:
                logger.exception("Tool concurrency policy failed for %s; serializing", block.name)
            try:
                interruptible = definition.interrupt_behavior() == "cancel"
            except Exception:
                logger.exception("Tool interruption policy failed for %s; preserving completion", block.name)
        entry = _Invocation(block, assistant_message, parallel, interruptible)
        if definition is None:
            entry.parallel = True
            entry.status = "completed"
            message = "Error: No such tool available: " + block.name
            entry.results.append(
                ToolResultMessage(
                    block.id, "<tool_use_error>" + message + "</tool_use_error>", True, message, assistant_message.uuid
                )
            )
        self._entries.append(entry)
        self._schedule()
        self._changed.set()

    def _schedule(self) -> None:
        if self._discarded:
            return
        active = [entry for entry in self._entries if entry.status == "executing"]
        for entry in self._entries:
            if entry.status != "queued":
                continue
            if active and (not entry.parallel or any(not running.parallel for running in active)):
                break
            entry.status = "executing"
            entry.task = self._loop.create_task(self._run(entry))
            active.append(entry)

    def _reason(self, entry: _Invocation) -> CancelReason | None:
        if self._discarded:
            return "streaming_fallback"
        if entry.interruptible:
            if self._failure.is_set():
                return "sibling_error"
            if any(event.is_set() for event in self._external_stops):
                return "user_interrupted"
        return None

    def _cancel_result(self, entry: _Invocation, reason: CancelReason) -> ToolResultMessage:
        return _make_synthetic_error(
            entry.block.id,
            reason,
            assistant_uuid=entry.assistant.uuid,
            errored_tool_description=self._failure_description,
        )

    async def _relay_stop(self, entry: _Invocation) -> None:
        sources = (self._discard, self._failure, *self._external_stops) if entry.interruptible else (self._discard,)
        listeners = [self._loop.create_task(event.wait()) for event in sources]
        try:
            await asyncio.wait(listeners, return_when=asyncio.FIRST_COMPLETED)
            entry.stop.set()
        finally:
            for listener in listeners:
                listener.cancel()
            await asyncio.gather(*listeners, return_exceptions=True)

    async def _run(self, entry: _Invocation) -> None:
        bridge = None
        try:
            reason = self._reason(entry)
            if reason is not None:
                entry.results.append(self._cancel_result(entry, reason))
                return
            bridge = self._loop.create_task(self._relay_stop(entry))
            own_error = False
            async for output in self._runner(entry.block, entry.assistant, entry.stop):
                reason = self._reason(entry)
                if reason is not None and not own_error:
                    entry.results.append(self._cancel_result(entry, reason))
                    break
                if isinstance(output, ProgressUpdate):
                    entry.progress.append(output)
                    self._changed.set()
                elif isinstance(output, ToolResultMessage):
                    entry.results.append(output)
                    if output.is_error:
                        own_error = True
                        if entry.interruptible:
                            self._failure_description = _describe_invocation(entry)
                            self._failure.set()
                else:
                    logger.warning("Ignoring invalid output from tool %s", entry.block.name)
            if not entry.results:
                reason = self._reason(entry)
                if reason is not None:
                    entry.results.append(self._cancel_result(entry, reason))
        except asyncio.CancelledError:
            if not entry.results:
                entry.results.append(self._cancel_result(entry, self._reason(entry) or "user_interrupted"))
            raise
        except Exception as exc:
            logger.exception("Tool execution failed for %s", entry.block.name)
            if not entry.results:
                entry.results.append(
                    ToolResultMessage(
                        entry.block.id,
                        "<tool_use_error>%s</tool_use_error>" % exc,
                        True,
                        str(exc),
                        entry.assistant.uuid,
                    )
                )
        finally:
            if bridge is not None:
                bridge.cancel()
                await asyncio.gather(bridge, return_exceptions=True)
            entry.status = "completed"
            self._schedule()
            self._changed.set()

    def get_completed_results(self) -> list[ProgressUpdate | ToolResultMessage]:
        if self._discarded:
            return []
        batch: list[ProgressUpdate | ToolResultMessage] = []
        for entry in self._entries:
            batch.extend(entry.progress)
            entry.progress.clear()
            if entry.status == "completed":
                batch.extend(entry.results)
                entry.status = "yielded"
            elif entry.status == "executing" and not entry.parallel:
                break
        return batch

    async def get_remaining_results(self) -> AsyncIterator[ProgressUpdate | ToolResultMessage]:
        while not self._discarded:
            self._changed.clear()
            self._schedule()
            for output in self.get_completed_results():
                if self._discarded:
                    return
                yield output
            if all(entry.status == "yielded" for entry in self._entries):
                return
            # Clearing before inspecting prevents a completed task's notification
            # from being lost between the readiness check and this wait.
            await self._changed.wait()

    def pending_tool_use_ids(self) -> list[str]:
        return [entry.block.id for entry in self._entries if entry.status != "yielded"]

    async def wait_all(self) -> None:
        while True:
            self._schedule()
            active = [entry.task for entry in self._entries if entry.task is not None and not entry.task.done()]
            if not active:
                return
            await asyncio.gather(*active, return_exceptions=True)


def _describe_invocation(entry: _Invocation) -> str:
    arguments = entry.block.input
    if isinstance(arguments, dict):
        for key in ("command", "file_path", "pattern", "url"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return "%s(%s)" % (entry.block.name, value[:40] + "…" if len(value) > 40 else value)
    return entry.block.name


def tombstone_partial_assistant_messages(
    native_messages: list[dict[str, Any]], pending_tool_use_ids: set[str]
) -> tuple[list[dict[str, Any]], int]:
    """Drop failed-attempt assistant turns so retries cannot create orphan calls."""
    if not pending_tool_use_ids:
        return native_messages, 0

    def keep(message: dict[str, Any]) -> bool:
        blocks = message.get("content")
        if message.get("role") != "assistant" or not isinstance(blocks, list):
            return True
        return not any(
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and str(block.get("id") or "") in pending_tool_use_ids
            for block in blocks
        )

    retained = [message for message in native_messages if keep(message)]
    return retained, len(native_messages) - len(retained)
