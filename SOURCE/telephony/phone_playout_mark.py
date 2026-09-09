"""Observe-only ``bot_playout_end`` anchor via the Telnyx media ``mark`` echo.

Why this exists
---------------
The self-echo guard already stamps ``bot_speaking_stop`` into the per-call latency
trace when a ``TTSStoppedFrame`` reaches the output transport -- i.e. the last audio
frame of a bot turn has been *queued to the wire*. That is UPSTREAM of when the audio
actually plays to the caller: it does not account for the Telnyx-side send buffer.

Telnyx media streaming supports a ``mark`` control frame: the app sends
``{"event":"mark","mark":{"name":...}}`` after outbound media, and Telnyx echoes the
SAME name back once the media immediately preceding it has finished playing to the
line. That echo is a *played-to-line* timestamp -- strictly downstream of queued-to-
wire by the Telnyx send buffer. (It still does NOT include the handset's own jitter
buffer / speaker, so it is a floor, not the true acoustic tail -- that is expected;
we are sharpening the anchor, not computing a hard cutoff.)

This processor emits a NEW observe-only event ``bot_playout_end`` into the SAME
``PhoneLatencyTraceRecorder`` the guard instrumentation already uses, timestamped when
Telnyx confirms the last audio of a bot turn finished playing to the line.

Additive-only / non-boxing
--------------------------
A mark is an inert control frame. This processor NEVER gates, delays, or alters audio,
and NEVER touches the self-echo guard's suppress/pass turn-taking decision. It observes
a downstream ``BotStoppedSpeakingFrame`` (which the base output transport pushes only
AFTER its audio queue has drained the turn's last audio to the wire), then TAPS the
existing Telnyx mark round-trip (``wait_for_output_mark`` -> ``send_mark`` ->
``_on_mark_received``) in a fire-and-forget task. The frame itself is always pushed
through unchanged before any mark work is scheduled, so the audio path is never blocked.

It reuses the existing per-turn mark round-trip (the same one
``EndCallHangupAfterOutputProcessor`` uses for the goodbye-drain) with a distinct
per-turn mark name (``playout_end_<call_id>_<n>``) so its waiters never collide with the
end-call mark.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import BotStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - Pipecat is optional in local test environments
    PIPECAT_AVAILABLE = False
    BotStoppedSpeakingFrame = None  # type: ignore[assignment, misc]
    FrameDirection = None  # type: ignore[assignment]
    FrameProcessor = object  # type: ignore[misc, assignment]


# The echo returns within the Telnyx send-buffer duration of the last audio; a couple
# of seconds is generous for a played-to-line confirmation. If it never echoes (early
# hangup, disconnect) we simply do not emit -- this is observe-only, never fail-closed.
PLAYOUT_MARK_TIMEOUT_SECONDS = 5.0


class PlayoutEndMarkProcessor(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Emit ``bot_playout_end`` when Telnyx echoes a per-turn media mark.

    Placed AFTER ``transport.output()`` so the ``BotStoppedSpeakingFrame`` it observes
    has already had the turn's last audio drained to the Telnyx WebSocket.
    """

    def __init__(
        self,
        *,
        transport: Any,
        recorder: Any,
        call_id: str,
        mark_timeout_seconds: float = PLAYOUT_MARK_TIMEOUT_SECONDS,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="phone_playout_end_mark")
        self._transport = transport
        self._recorder = recorder
        self._call_id = str(call_id or "")
        self._mark_timeout_seconds = mark_timeout_seconds
        self._turn_index = 0
        # Keep strong references so fire-and-forget tasks are not GC'd mid-flight.
        self._pending_tasks: set[asyncio.Task[None]] = set()

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not PIPECAT_AVAILABLE:
            return

        await super().process_frame(frame, direction)

        # Push through FIRST and unconditionally: instrumentation must never sit on the
        # frame path. The mark work is scheduled out-of-band below.
        await self.push_frame(frame, direction)

        if (
            direction == FrameDirection.DOWNSTREAM
            and BotStoppedSpeakingFrame is not None
            and isinstance(frame, BotStoppedSpeakingFrame)
        ):
            self._schedule_playout_mark()

    def _schedule_playout_mark(self) -> None:
        """Send a per-turn mark and, out-of-band, emit ``bot_playout_end`` on its echo."""
        wait_for_output_mark = getattr(self._transport, "wait_for_output_mark", None)
        if not callable(wait_for_output_mark):
            # Loopback / non-Telnyx transports have no mark round-trip. Nothing to
            # observe; the played-to-line anchor only exists on a live carrier stream.
            return
        self._turn_index += 1
        turn_index = self._turn_index
        mark_name = "playout_end_%s_%d" % (self._call_id, turn_index)
        try:
            task = asyncio.create_task(self._await_playout(wait_for_output_mark, mark_name, turn_index))
        except RuntimeError:  # pragma: no cover - no running loop (defensive)
            logger.debug("phone playout mark: no running loop to schedule %s", mark_name)
            return
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _await_playout(
        self,
        wait_for_output_mark: Any,
        mark_name: str,
        turn_index: int,
    ) -> None:
        try:
            echoed = await wait_for_output_mark(mark_name, timeout=self._mark_timeout_seconds)
        except (ConnectionError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            # The mark never confirmed (disconnect / early hangup / transport error).
            # Observe-only: do not emit, do not raise into the pipeline.
            logger.debug("phone playout mark %s not confirmed: %s", mark_name, exc)
            return
        if not echoed:
            logger.debug("phone playout mark %s did not echo before timeout", mark_name)
            return
        self._emit_playout_end(mark_name, turn_index)

    def _emit_playout_end(self, mark_name: str, turn_index: int) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        try:
            # Reuse the guard-event channel: inherits the recorder's automatic
            # ts + call_id stamp. ``mark`` ties the played-to-line moment to the
            # specific bot turn that produced it.
            recorder.record_turn_guard_event(
                "bot_playout_end",
                mark=mark_name,
                turn_index=turn_index,
            )
        except (AttributeError, OSError, ValueError, TypeError):  # pragma: no cover
            logger.debug("phone playout mark: failed to emit bot_playout_end", exc_info=True)


if not PIPECAT_AVAILABLE:

    class _PlayoutEndMarkProcessorUnavailable:  # pragma: no cover - non-pipecat guard.
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("PlayoutEndMarkProcessor requires Pipecat")
