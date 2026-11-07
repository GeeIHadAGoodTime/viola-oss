"""Show the final answer on the stage while the model writes it.

The stage used to get a reply in one piece, when the command returned. A typed
turn did receive tokens over SSE, but every token of the model's output, tool
preambles included, and the stage kept its "processing" spinner over them until
the turn ended (1.0.5 build-9 T9 FAIL). Voice turns had no live text at all.

:class:`StageReplyRelay` listens to one command stream, keeps only what
:class:`services.llm.answer_stream.FinalAnswerText` says is the answer, and
broadcasts it to the user's clients as ``reply_partial`` events. The client paces
what it shows (with the voice when the reply is being spoken). The finished
reply still arrives as ``chat_response`` and the command result, and always wins.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from core.logging_config import get_logger
from services.llm.answer_stream import FinalAnswerText
from services.llm.stream_bus import (
    add_stream_listener,
    bind_stream_producer,
    command_stream_context,
    finalize_stream,
    remove_stream,
    remove_stream_listener,
)

logger = get_logger(__name__)

REPLY_PARTIAL_EVENT = "reply_partial"
# At most this often: a burst of tokens is one update, not one websocket frame each.
_MIN_BROADCAST_SECONDS = 0.05


def _hub_broadcast(user_id: str, payload: dict[str, Any]) -> None:
    from ui.websocket.event_hub import get_event_hub

    hub = get_event_hub()
    if hub is None:
        return
    asyncio.ensure_future(hub.broadcast(REPLY_PARTIAL_EVENT, payload, user_id=user_id, force=True))


class StageReplyRelay:
    """One turn's live answer text, broadcast to that user's clients only."""

    def __init__(
        self,
        *,
        user_id: str,
        spoken: bool,
        broadcast: Callable[[str, dict[str, Any]], None] = _hub_broadcast,
    ) -> None:
        self.user_id = user_id
        self.spoken = spoken
        self.turn = uuid.uuid4().hex[:12]
        self._text = FinalAnswerText()
        self._broadcast = broadcast
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._last_sent_at = 0.0
        self._sent = ""
        self._flush_pending = False
        self._closed = False

    def bind_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()

    def on_event(self, event: dict[str, Any]) -> None:
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
        if self._closed or not self._text.feed(event):
            return
        now = time.monotonic()
        wait = _MIN_BROADCAST_SECONDS - (now - self._last_sent_at)
        if wait <= 0:
            self._send()
        elif not self._flush_pending and self._loop is not None:
            self._flush_pending = True
            self._loop.call_later(wait, self._flush)

    def _flush(self) -> None:
        self._flush_pending = False
        self._send()

    def _send(self) -> None:
        if self._closed or self._text.text == self._sent:
            return
        self._sent = self._text.text
        self._last_sent_at = time.monotonic()
        try:
            self._broadcast(self.user_id, {"turn": self.turn, "text": self._sent, "spoken": self.spoken})
        except Exception:  # noqa: BLE001, RUF100 - a live preview must never affect the turn
            logger.debug("Live reply broadcast failed", exc_info=True)

    def close(self) -> None:
        """The turn is over: send what is pending and stop."""
        if not self._closed:
            self._send()
            self._closed = True


@contextlib.contextmanager
def stage_reply(stream_id: str | None, *, user_id: str) -> Iterator[StageReplyRelay | None]:
    """Live stage text for a turn that is not being spoken.

    Uses the caller's stream when there is one, otherwise a private stream for
    this turn. A spoken turn gets the same relay from its StreamedReply.
    """
    own = stream_id is None
    sid = stream_id or "stage-%s" % uuid.uuid4().hex
    relay = StageReplyRelay(user_id=user_id, spoken=False)
    attached = False
    try:
        relay.bind_loop()
        if own:
            bind_stream_producer(sid, owner_id=user_id)
        attached = add_stream_listener(sid, relay.on_event)
    except Exception:  # noqa: BLE001, RUF100 - no live text is the safe fallback
        logger.debug("Live stage text could not attach", exc_info=True)
    context: contextlib.AbstractContextManager[Any] = (
        command_stream_context(sid) if own and attached else contextlib.nullcontext()
    )
    try:
        with context:
            yield relay if attached else None
    finally:
        relay.close()
        remove_stream_listener(sid, relay.on_event)
        if own:
            try:
                finalize_stream(sid, content=None)
                remove_stream(sid)
            except Exception:  # noqa: BLE001, RUF100 - stream cleanup is best effort
                logger.debug("Live stage stream cleanup failed", exc_info=True)


__all__ = ["REPLY_PARTIAL_EVENT", "StageReplyRelay", "stage_reply"]
