"""Pre-TTS PAYMENT_GATE text filter for phone calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TextFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - Pipecat is optional in some test envs
    PIPECAT_AVAILABLE = False
    FrameProcessor = object  # type: ignore[misc, assignment]
    LLMFullResponseEndFrame = None  # type: ignore[assignment]
    LLMFullResponseStartFrame = None  # type: ignore[assignment]
    LLMTextFrame = None  # type: ignore[assignment]
    TextFrame = None  # type: ignore[assignment]


_PHONE_PAYMENT_GATE_REPLACEMENT_TEXT = "I need the account holder to review this securely before I continue."


def _payment_gate_prefix() -> str:
    from intent.agent_executor import _PAYMENT_GATE_PREFIX

    return _PAYMENT_GATE_PREFIX


class PhonePaymentGateTextFilter(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Swallow phone PAYMENT_GATE text before it can reach TTS."""

    def __init__(
        self,
        *,
        on_payment_gate: Callable[[str], Awaitable[None]],
        replacement_text: str = _PHONE_PAYMENT_GATE_REPLACEMENT_TEXT,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="phone_payment_gate_text_filter")
        self._on_payment_gate = on_payment_gate
        self._replacement_text = replacement_text
        self._prefix = _payment_gate_prefix()
        self._buffered_frames: list[tuple[Any, Any]] = []
        self._buffered_text_parts: list[str] = []

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not PIPECAT_AVAILABLE:
            return

        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._start_buffering(frame, direction)
            return

        if self._is_buffering:
            await self._process_buffered_response_frame(frame, direction)
            return

        if isinstance(frame, TextFrame) and self._is_payment_gate_text(frame.text):
            await self._replace_payment_gate_text(frame.text, direction)
            return

        await self.push_frame(frame, direction)

    @property
    def _is_buffering(self) -> bool:
        return bool(self._buffered_frames)

    def _start_buffering(self, frame: Any, direction: Any) -> None:
        self._buffered_frames = [(frame, direction)]
        self._buffered_text_parts = []

    async def _process_buffered_response_frame(self, frame: Any, direction: Any) -> None:
        self._buffered_frames.append((frame, direction))

        if isinstance(frame, TextFrame):
            self._buffered_text_parts.append(frame.text)
            if not self._is_possible_payment_gate_prefix(self._buffered_text):
                await self._flush_buffered_frames()
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            text = self._buffered_text
            if self._is_payment_gate_text(text):
                await self._replace_buffered_payment_gate_response(text)
            else:
                await self._flush_buffered_frames()

    @property
    def _buffered_text(self) -> str:
        return "".join(self._buffered_text_parts)

    def _is_possible_payment_gate_prefix(self, text: str) -> bool:
        stripped = text.lstrip()
        if not stripped:
            return True
        return self._prefix.startswith(stripped) or stripped.startswith(self._prefix)

    def _is_payment_gate_text(self, text: str) -> bool:
        return text.strip().startswith(self._prefix)

    async def _flush_buffered_frames(self) -> None:
        buffered_frames = self._buffered_frames
        self._clear_buffer()
        for frame, direction in buffered_frames:
            await self.push_frame(frame, direction)

    async def _replace_buffered_payment_gate_response(self, text: str) -> None:
        buffered_frames = self._buffered_frames
        self._clear_buffer()

        await self._start_payment_gate(text)
        if not self._replacement_text:
            return

        start_item = next(
            (
                (frame, direction)
                for frame, direction in buffered_frames
                if isinstance(frame, LLMFullResponseStartFrame)
            ),
            None,
        )
        end_item = next(
            ((frame, direction) for frame, direction in buffered_frames if isinstance(frame, LLMFullResponseEndFrame)),
            None,
        )
        direction = start_item[1] if start_item is not None else None
        if start_item is not None:
            await self.push_frame(start_item[0], start_item[1])
        await self.push_frame(LLMTextFrame(self._replacement_text), direction)
        if end_item is not None:
            await self.push_frame(end_item[0], end_item[1])

    async def _replace_payment_gate_text(self, text: str, direction: Any) -> None:
        await self._start_payment_gate(text)
        if self._replacement_text:
            await self.push_frame(LLMTextFrame(self._replacement_text), direction)

    async def _start_payment_gate(self, text: str) -> None:
        await self._on_payment_gate(text.strip())
        logger.info("Phone PAYMENT_GATE text filter swallowed assistant text before TTS")

    def _clear_buffer(self) -> None:
        self._buffered_frames = []
        self._buffered_text_parts = []
