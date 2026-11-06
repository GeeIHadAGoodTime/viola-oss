"""Show Viola's side of the live transcript at the speed she says it.

The model writes a reply in about a second, and synthesis and playout take several
more. Publishing each token as it was generated sent 19-42 transcript updates in 1-2 s
and put the words on screen before she spoke them (build 9 laptop run). The text is
known early, but the only honest clock for it is the audio. So this processor sits
after ``transport.output()``, where the bot-speaking frames mark audio really going
out, and reveals the reply at a speaking rate while she is speaking and not otherwise.

It also tells the saved transcript the truth about interruptions. A reply the
recipient talked over is recorded when the model finishes writing it, though much of
it was never said; call 3745134a showed three "openings" of which the recipient heard
about one. An interruption trims that entry to what had been spoken.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        CancelFrame,
        EndFrame,
        InterruptionFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without pipecat installed
    PIPECAT_AVAILABLE = False
    FrameProcessor = object  # type: ignore[assignment,misc]

# Kokoro at speed 1.0 measured on the recordings of the 2026-11-05 call: "Sorry to
# interrupt, that was just a silly fridge joke." is 52 characters in 3.5 s.
_SPEECH_CHARS_PER_SECOND = 15.0
_REVEAL_TICK_SECS = 0.25
# Audio stops between sentences while the next one is synthesized. Only a stop that
# lasts this long means the reply is over.
_REPLY_FINISHED_SILENCE_SECS = 1.5


def _word_boundary(text: str, limit: int) -> int:
    """The longest prefix of *text* up to *limit* that does not cut a word."""
    if limit >= len(text):
        return len(text)
    cut = text.rfind(" ", 0, limit + 1)
    return cut if cut > 0 else 0


class SpokenTranscriptPacer(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Publishes one reply at a time, in step with the audio going out."""

    def __init__(
        self,
        publish: Callable[..., None],
        *,
        trim_spoken: Callable[[str, str], None] | None = None,
        chars_per_second: float = _SPEECH_CHARS_PER_SECOND,
        tick_secs: float = _REVEAL_TICK_SECS,
        finished_silence_secs: float = _REPLY_FINISHED_SILENCE_SECS,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__()
        self._publish = publish
        self._trim_spoken = trim_spoken
        self._chars_per_second = chars_per_second
        self._tick_secs = tick_secs
        self._finished_silence_secs = finished_silence_secs
        self._target = ""
        self._revealed = 0
        self._spoken_secs = 0.0
        self._writing = False
        self._speaking = False
        self._reveal_task: asyncio.Task[None] | None = None
        self._finish_task: asyncio.Task[None] | None = None

    # -- fed by the transcript collector, upstream of TTS ----------------------
    def note_reply_started(self) -> None:
        self._finish_reply()
        self._writing = True

    def add_text(self, text: str) -> None:
        self._target += text

    def note_reply_written(self) -> None:
        self._writing = False
        if not self._speaking:
            self._schedule_finish()

    # -- fed by the audio side -------------------------------------------------
    async def process_frame(self, frame: Any, direction: Any) -> None:
        await super().process_frame(frame, direction)
        if PIPECAT_AVAILABLE:
            if isinstance(frame, BotStartedSpeakingFrame):
                self._on_audio_started()
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._on_audio_stopped()
            elif isinstance(frame, InterruptionFrame):
                self._on_interrupted()
            elif isinstance(frame, (EndFrame, CancelFrame)):
                self._finish_reply()
        await self.push_frame(frame, direction)

    def _on_audio_started(self) -> None:
        self._speaking = True
        self._cancel(self._finish_task)
        self._finish_task = None
        if self._reveal_task is None or self._reveal_task.done():
            self._reveal_task = asyncio.create_task(self._reveal_while_speaking(), name="phone-transcript-pacer")

    def _on_audio_stopped(self) -> None:
        self._speaking = False
        self._schedule_finish()

    def _on_interrupted(self) -> None:
        self._speaking = False
        self._writing = False
        self._cancel(self._finish_task)
        self._finish_task = None
        self._cancel(self._reveal_task)
        self._reveal_task = None
        target = self._target.strip()
        heard = self._target[: self._revealed].strip()
        if target and self._trim_spoken is not None:
            self._trim_spoken(target, heard)
        if heard:
            self._publish(role="viola", text=heard, partial=False)
        self._reset()

    async def _reveal_while_speaking(self) -> None:
        while self._speaking:
            await asyncio.sleep(self._tick_secs)
            self._spoken_secs += self._tick_secs
            limit = int(self._spoken_secs * self._chars_per_second)
            revealed = _word_boundary(self._target, limit)
            if revealed > self._revealed:
                self._revealed = revealed
                self._publish(role="viola", text=self._target[:revealed].strip(), partial=True)

    def _schedule_finish(self) -> None:
        self._cancel(self._finish_task)
        self._finish_task = asyncio.create_task(self._finish_after_silence(), name="phone-transcript-finish")

    async def _finish_after_silence(self) -> None:
        await asyncio.sleep(self._finished_silence_secs)
        self._finish_task = None
        if not self._speaking and not self._writing:
            self._finish_reply()

    def _finish_reply(self) -> None:
        """Publish whatever is left of the current reply as its finished line."""
        self._cancel(self._reveal_task)
        self._reveal_task = None
        self._cancel(self._finish_task)
        self._finish_task = None
        text = self._target.strip()
        if text:
            self._publish(role="viola", text=text, partial=False)
        self._reset()

    def _reset(self) -> None:
        self._target = ""
        self._revealed = 0
        self._spoken_secs = 0.0
        self._writing = False

    @staticmethod
    def _cancel(task: asyncio.Task[None] | None) -> None:
        if task is None or task.done() or task is _current_task():
            return
        task.cancel()


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
