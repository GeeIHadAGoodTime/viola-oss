"""Pre-TTS disclosure watchdog for phone calls."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from core.logging_config import get_logger
from telephony.disclosure_text import expected_disclosure_sentence

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        TextFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - Pipecat is optional in some test envs
    PIPECAT_AVAILABLE = False
    FrameProcessor = object  # type: ignore[misc, assignment] # PHONE-01: Pipecat is optional in local test environments.
    LLMFullResponseEndFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional in local test environments.
    LLMFullResponseStartFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional in local test environments.
    TextFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional in local test environments.


DisclosureCallback = Callable[[], None | Awaitable[None]]


class DisclosureWatchdog(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc] # PHONE-01: Pipecat fallback base.
    """Keep recording/transcript disclosure fail-closed before persisted recording starts."""

    def __init__(
        self,
        *,
        caller_name: str,
        recording_enabled: bool,
        transcript_retention_enabled: bool,
        on_disclosure_spoken: DisclosureCallback,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="disclosure_watchdog")
        self._caller_name = caller_name
        self._recording_enabled = recording_enabled
        self._transcript_retention_enabled = transcript_retention_enabled
        self._on_disclosure_spoken = on_disclosure_spoken
        self._expected_disclosure = expected_disclosure_sentence(
            recording_enabled,
            transcript_retention_enabled,
            caller_name,
        )
        self._disclosure_confirmed = self._expected_disclosure is None
        self._buffered_frames: list[tuple[Any, Any]] = []
        self._buffered_text_parts: list[str] = []
        self._logged_unconfirmed_live_turn = False

    @property
    def disclosure_confirmed(self) -> bool:
        return self._disclosure_confirmed

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not PIPECAT_AVAILABLE:
            return

        await super().process_frame(frame, direction)

        if self._expected_disclosure is None:
            await self.push_frame(frame, direction)
            return

        if self._disclosure_confirmed:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._start_buffering(frame, direction)
            return

        if self._is_buffering:
            await self._process_buffered_response_frame(frame, direction)
            return

        if isinstance(frame, TextFrame):
            await self._process_single_text_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    @property
    def _is_buffering(self) -> bool:
        return bool(self._buffered_frames)

    @property
    def _buffered_text(self) -> str:
        return "".join(self._buffered_text_parts)

    def _start_buffering(self, frame: Any, direction: Any) -> None:
        self._buffered_frames = [(frame, direction)]
        self._buffered_text_parts = []

    async def _process_buffered_response_frame(self, frame: Any, direction: Any) -> None:
        self._buffered_frames.append((frame, direction))
        if isinstance(frame, TextFrame):
            self._buffered_text_parts.append(frame.text)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            await self._flush_first_turn()

    async def _process_single_text_frame(self, frame: Any, direction: Any) -> None:
        text = str(getattr(frame, "text", "") or "")
        if not self._has_expected_disclosure(text):
            text = self._append_disclosure_to_text(text)
            frame.text = text

        await self.push_frame(frame, direction)

        if self._has_expected_disclosure(text):
            await self._mark_disclosure_spoken()
            return

        self._log_unconfirmed_live_turn()

    async def _flush_first_turn(self) -> None:
        buffered_frames = self._buffered_frames
        text = self._buffered_text
        if not self._has_expected_disclosure(text):
            text = self._append_disclosure_to_last_text_frame(buffered_frames)
        self._clear_buffer()

        for frame, direction in buffered_frames:
            await self.push_frame(frame, direction)

        if self._has_expected_disclosure(text):
            await self._mark_disclosure_spoken()
            return

        self._log_unconfirmed_live_turn()

    async def _mark_disclosure_spoken(self) -> None:
        if self._disclosure_confirmed:
            return
        self._disclosure_confirmed = True
        try:
            result = self._on_disclosure_spoken()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Recording disclosure callback failed")

    def _has_expected_disclosure(self, text: str) -> bool:
        expected = self._expected_disclosure
        return expected is None or expected in str(text or "")

    def _append_disclosure_to_text(self, text: str) -> str:
        expected = self._expected_disclosure
        if not expected:
            return text
        text = str(text or "").strip()
        return "%s %s" % (text, expected) if text else expected

    def _append_disclosure_to_last_text_frame(self, buffered_frames: list[tuple[Any, Any]]) -> str:
        for frame, _direction in reversed(buffered_frames):
            if isinstance(frame, TextFrame):
                text = self._append_disclosure_to_text(getattr(frame, "text", ""))
                frame.text = text
                if self._buffered_text_parts:
                    self._buffered_text_parts[-1] = text
                else:
                    self._buffered_text_parts = [text]
                return self._buffered_text
        return self._buffered_text

    def _log_unconfirmed_live_turn(self) -> None:
        if self._logged_unconfirmed_live_turn:
            return
        self._logged_unconfirmed_live_turn = True
        logger.warning(
            "Phone disclosure watchdog passed through an assistant turn before deterministic disclosure confirmation"
        )

    def _clear_buffer(self) -> None:
        self._buffered_frames = []
        self._buffered_text_parts = []
