"""Speak a reply while the model is still writing it.

A :class:`StreamedReply` listens to one command stream (``services.llm.stream_bus``)
for the provider's answer events, lets :class:`services.llm.answer_stream.FinalAnswerSpeechGate`
decide which sentences are final-answer text that no guard can still change,
and feeds those sentences to the TTS engine's ``speak_streaming`` as they
complete. When the command returns, :meth:`StreamedReply.finish` hands over the
final answer (after every guard ran) and only its unspoken part is queued.

Nothing here decides what a reply says; the command path does. If live speech
never started (no streaming provider, an older cloud, a held first sentence),
the caller speaks the final answer exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from config import env as config_env
from core.logging_config import get_logger
from services.llm.answer_stream import FinalAnswerSpeechGate
from services.llm.stream_bus import (
    add_stream_listener,
    bind_stream_producer,
    command_stream_context,
    finalize_stream,
    remove_stream,
    remove_stream_listener,
)

logger = get_logger(__name__)

# Kill switch: VIOLA_STREAMED_SPEECH=0 speaks every reply only after it is complete.
STREAMED_SPEECH_ENV = "VIOLA_STREAMED_SPEECH"

# Hooks that can make the agent loop replace a finished answer with a new one.
_ANSWER_REPLACING_HOOKS = ("Stop", "TaskCompleted", "TeammateIdle")


def streamed_speech_enabled() -> bool:
    return config_env.get_bool(STREAMED_SPEECH_ENV, True)


def answer_replacing_hooks_registered(user_id: str | None) -> bool:
    """True when a user hook could swap the model's answer after it streamed."""
    try:
        from intent.hooks.dispatcher import has_registered_hooks

        return any(has_registered_hooks(name, user_id=user_id) for name in _ANSWER_REPLACING_HOOKS)
    except Exception:  # noqa: BLE001, RUF100 - unknown hook state means do not stream speech
        return True


def can_stream_speech(tts: Any) -> bool:
    return tts is not None and callable(getattr(tts, "speak_streaming", None))


class StreamedReply:
    """One reply's live speech: stream listener, speech gate and sentence queue."""

    def __init__(
        self,
        tts: Any,
        *,
        user_id: str,
        stream_id: str | None = None,
        start_speaking: Callable[[StreamedReply], asyncio.Future[Any]] | None = None,
        live: bool = True,
        reason: str | None = None,
    ) -> None:
        self._tts = tts
        self.user_id = user_id
        self._own_stream = stream_id is None
        self.stream_id = stream_id or "speech-%s" % uuid.uuid4().hex
        self.gate = FinalAnswerSpeechGate(live=live, reason=reason)
        self._start_speaking = start_speaking
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._closed = False
        self._attached = False
        self.speaking_task: asyncio.Future[Any] | None = None
        self.created_at = time.monotonic()
        self.first_sentence_at: float | None = None

    # -- wiring -----------------------------------------------------------
    @property
    def started(self) -> bool:
        return self.speaking_task is not None

    @property
    def heard_text(self) -> str:
        """The final-answer text queued for speech so far (grows while speaking)."""
        return self.gate.heard_text

    @contextlib.contextmanager
    def attached(self) -> Iterator[StreamedReply]:
        """Listen on the stream and scope every provider call under it to this stream."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        try:
            if self._own_stream:
                bind_stream_producer(self.stream_id, owner_id=self.user_id)
            self._attached = add_stream_listener(self.stream_id, self._on_event)
        except Exception:  # noqa: BLE001, RUF100 - no live speech is the safe fallback
            logger.debug("Streamed speech could not attach; the reply is spoken when complete", exc_info=True)
            self._attached = False
        if not self._attached:
            self.gate.stop_live("not_attached")
        # A caller-provided stream id is already scoped by the caller (the
        # typed-command route); an own stream is scoped here.
        context: contextlib.AbstractContextManager[Any] = (
            command_stream_context(self.stream_id) if self._own_stream and self._attached else contextlib.nullcontext()
        )
        try:
            with context:
                yield self
        finally:
            remove_stream_listener(self.stream_id, self._on_event)

    def _on_event(self, event: dict[str, Any]) -> None:
        """Stream-bus listener; may run on any thread."""
        loop = self._loop
        if loop is None or self._closed:
            return
        if threading.get_ident() == self._loop_thread:
            self._handle(event)
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._handle, event)

    def _handle(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        for sentence in self.gate.feed(event):
            self._push(sentence)

    def _push(self, text: str) -> None:
        if self._closed or not text.strip():
            return
        self._queue.put_nowait(text.strip() + " ")
        if self.speaking_task is None:
            self.first_sentence_at = time.monotonic()
            logger.info(
                "Streamed speech: first sentence ready %.0f ms after the command started",
                (self.first_sentence_at - self.created_at) * 1000.0,
            )
            starter = self._start_speaking or _default_start
            self.speaking_task = starter(self)

    async def sentences(self) -> AsyncIterator[str]:
        """Sentences to speak, in order, until the reply is finished or cancelled."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def speak(self) -> None:
        """Speak every queued and future sentence through the TTS engine."""
        await self._tts.speak_streaming(self.sentences())

    # -- end of turn ------------------------------------------------------
    def finish(self, final_message: str | None, *, already_spoken: bool = False) -> str:
        """Hand over the final answer; queue only its unspoken part.

        Returns the text the caller still has to speak itself: the whole final
        answer when live speech never started, otherwise "" (the remainder went
        to the running speech).
        """
        remainder = self.gate.finish(final_message, already_spoken=already_spoken)
        self._close_stream(final_message)
        if self.speaking_task is None:
            self._closed = True
            return "" if already_spoken else remainder
        if remainder.strip() and not self._closed:
            self._queue.put_nowait(remainder.strip())
        self._end_queue()
        return ""

    def cancel(self) -> None:
        """Stop queueing speech (barge-in, failed command)."""
        self.gate.finish(None, already_spoken=True)
        self._end_queue()
        self._close_stream(None)

    def _end_queue(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    def _close_stream(self, final_message: str | None) -> None:
        if not self._own_stream:
            return
        try:
            finalize_stream(self.stream_id, content=final_message or None)
            remove_stream(self.stream_id)
        except Exception:  # noqa: BLE001, RUF100 - stream cleanup is best effort
            logger.debug("Streamed speech stream cleanup failed", exc_info=True)


def _default_start(reply: StreamedReply) -> asyncio.Future[Any]:
    return asyncio.ensure_future(reply.speak())


def new_streamed_reply(
    tts: Any,
    *,
    user_id: str,
    stream_id: str | None = None,
    start_speaking: Callable[[StreamedReply], asyncio.Future[Any]] | None = None,
) -> StreamedReply | None:
    """A StreamedReply when this reply may be spoken live, else None (speak when complete)."""
    if not streamed_speech_enabled() or not can_stream_speech(tts):
        return None
    if answer_replacing_hooks_registered(user_id):
        logger.info("Streamed speech off for this turn: a hook can replace the finished answer")
        return None
    return StreamedReply(tts, user_id=user_id, stream_id=stream_id, start_speaking=start_speaking)


__all__ = [
    "STREAMED_SPEECH_ENV",
    "StreamedReply",
    "answer_replacing_hooks_registered",
    "can_stream_speech",
    "new_streamed_reply",
    "streamed_speech_enabled",
]
