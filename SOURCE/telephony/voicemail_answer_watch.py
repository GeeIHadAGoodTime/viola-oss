"""Notice a real person answering Viola's voicemail message.

Some phones play a voicemail greeting and then put the person on the line. On call
3745134a Jay said "hello" after one. Classifying the greeting is right, but leaving
one message and hanging up would hang up on him.

A voicemail system does not talk back. Once it starts recording it is silent, so
speech that BEGINS after her message began cannot be the recording. That timing, plus
the normal turn strategy accepting the speech as a real turn, is the whole test: no
wording is looked at. Her own echo never qualifies, because the turn strategy drops it,
and the tail of a greeting never qualifies, because it began before her message.

The watch also holds the hangup. After the message has played it listens for a short
window, and she only hangs up if nobody answered.

Sits right after STT, beside the answer-settle observer: it needs transcripts going
downstream and the VAD and bot-speaking frames coming back upstream.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        TranscriptionFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without pipecat installed
    PIPECAT_AVAILABLE = False
    FrameProcessor = object  # type: ignore[assignment,misc]

# How long the line stays open after her message has played. Long enough for someone
# who just picked up to say hello; a recording only gains a few seconds of silence.
VOICEMAIL_ANSWER_WINDOW_SECS = 4.0
# Speech heard in the window may still be with STT when it closes. Wait for it, up to
# here, so a slow transcript cannot lose the person.
VOICEMAIL_ANSWER_MAX_WAIT_SECS = 9.0
_POLL_SECS = 0.1


class VoicemailAnswerWatch(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Decides whether anyone answered, and holds the hangup until that is known."""

    def __init__(
        self,
        call_record: Any,
        *,
        on_live_answer: Callable[[], Awaitable[None]],
        window_secs: float = VOICEMAIL_ANSWER_WINDOW_SECS,
        max_wait_secs: float = VOICEMAIL_ANSWER_MAX_WAIT_SECS,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__()
        self._record = call_record
        self._on_live_answer = on_live_answer
        self._window_secs = window_secs
        self._max_wait_secs = max_wait_secs
        self._bot_speaking = False
        self._talking = False
        self._message_started_at: float | None = None
        # When each stretch of speech began, oldest first. STT answers them in order.
        self._untranscribed_speech: deque[float] = deque()
        self._latest_transcript_speech_at: float | None = None
        self._answered = asyncio.Event()

    @property
    def answered(self) -> bool:
        return self._answered.is_set()

    async def process_frame(self, frame: Any, direction: Any) -> None:
        await super().process_frame(frame, direction)
        if PIPECAT_AVAILABLE:
            if isinstance(frame, BotStartedSpeakingFrame):
                self._bot_speaking = True
                self.note_voicemail_mode()
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_speaking = False
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                self._talking = True
                self._untranscribed_speech.append(time.monotonic())
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._talking = False
            elif isinstance(frame, TranscriptionFrame) and getattr(frame, "finalized", True):
                self._latest_transcript_speech_at = (
                    self._untranscribed_speech.popleft() if self._untranscribed_speech else None
                )
        await self.push_frame(frame, direction)

    def note_voicemail_mode(self) -> None:
        """Her voicemail message is playing from now, if she is speaking."""
        if self._message_started_at is None and self._bot_speaking and self._record.voicemail_detected:
            self._message_started_at = time.monotonic()

    def latest_speech_answers_message(self) -> bool:
        """True when the newest transcript is speech that began after her message did."""
        started = self._message_started_at
        heard = self._latest_transcript_speech_at
        return (
            not self.answered
            and bool(self._record.voicemail_detected)
            and started is not None
            and heard is not None
            and heard >= started
        )

    async def on_recipient_turn_started(self) -> bool:
        """The turn strategy accepted a recipient turn. True if a person answered."""
        if not self.latest_speech_answers_message():
            return False
        self._answered.set()
        logger.info(
            "Call %s: a person answered during the voicemail message; carrying on the call",
            getattr(self._record, "call_id", ""),
        )
        await self._on_live_answer()
        return True

    def _speech_still_being_heard(self) -> bool:
        started = self._message_started_at
        if self._talking:
            return True
        return started is not None and any(heard >= started for heard in self._untranscribed_speech)

    async def nobody_answered(self) -> bool:
        """Wait out the window after the message. True when she should hang up."""
        opened = time.monotonic()
        while not self.answered:
            waited = time.monotonic() - opened
            if waited >= self._max_wait_secs:
                return True
            if waited >= self._window_secs and not self._speech_still_being_heard():
                return True
            try:
                await asyncio.wait_for(self._answered.wait(), timeout=_POLL_SECS)
            except TimeoutError:
                continue
        return False
