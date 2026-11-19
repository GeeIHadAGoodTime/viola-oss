"""Event-driven Telnyx hangup after final phone TTS media drains."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import ControlFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - Pipecat is optional in local test environments
    PIPECAT_AVAILABLE = False
    ControlFrame = object  # type: ignore[misc, assignment]
    FrameDirection = None  # type: ignore[assignment]
    FrameProcessor = object  # type: ignore[misc, assignment]


END_CALL_MEDIA_MARK_TIMEOUT_SECONDS = 2.0
# The carrier plays in real time audio it was handed all at once (a 10 s voicemail
# message was sent within 1.7 s on call c33057ff), and echoes the mark only when its
# playback reaches it. So the wait covers the estimated rest of playback plus this.
END_CALL_PLAYOUT_MARGIN_SECONDS = 1.5
END_CALL_MEDIA_MARK_MAX_WAIT_SECONDS = 60.0
# The hang-up frame can get here before the turn's last sentence has been
# synthesized (call e9b92727): the TTS closes a turn's audio context after 0.5 s
# without audio and lets frames queued behind it through. Wait this long at most
# for the rest of the turn's speech. Kokoro made that 9 s sentence in 0.54 s; this
# covers a slow CPU fallback over several sentences, and a TTS that never finishes
# still ends the call. It counts towards END_CALL_MEDIA_MARK_MAX_WAIT_SECONDS.
END_CALL_SPEECH_PRODUCTION_MAX_WAIT_SECONDS = 20.0
# Audio still to play below this counts as played (estimate rounding).
_PLAYOUT_SETTLED_SECONDS = 0.1
_SPEECH_POLL_SECONDS = 0.05
# A hangup already sent to the carrier is let finish when the pipeline is torn
# down (the carrier's own hangup tears it down), so the record gets its status.
END_CALL_HANGUP_TEARDOWN_WAIT_SECONDS = 5.0


@dataclass
class EndCallHangupFrame(ControlFrame):  # type: ignore[misc]
    """Queued behind final TTS audio so hangup can wait for carrier playback."""

    call_id: str
    reason: str
    # After a voicemail message: give a person who picked up the chance to answer.
    listen_first: bool = False


async def dispatch_telnyx_end_call_hangup(
    *,
    telnyx_client: Any,
    call_control_id: str,
    call_record: Any,
    reason_text: str,
) -> None:
    """Issue the Telnyx hangup and mark the local call record terminal.

    The record is only marked COMPLETED/VOICEMAIL when the hangup is
    confirmed -- either it just succeeded, or ``_telnyx_hangup_dispatched``
    was already True from an earlier successful call. If the Telnyx hangup
    API call itself fails, ``call_record.status`` is left untouched (not
    forced to a terminal value) so the retry/reconciliation net --
    ``_record_needs_telnyx_hangup`` in call_manager.py, which only retries
    non-terminal statuses -- still sees this record as needing a hangup and
    tries again. Marking it COMPLETED regardless of hangup success would
    suppress that retry while the still-live Telnyx leg keeps billing to
    its time_limit.
    """
    from telephony.call_manager import CallStatus

    hangup_confirmed = bool(getattr(call_record, "_telnyx_hangup_dispatched", False))
    try:
        active_call_control_id = str(call_control_id or getattr(call_record, "telnyx_call_control_id", "")).strip()
        if not active_call_control_id:
            raise ValueError("live Telnyx call_control_id unavailable")
        if not hangup_confirmed:
            await telnyx_client.calls.actions.hangup(call_control_id=active_call_control_id)
            logger.info("end_call: Telnyx hangup dispatched for %s", call_record.call_id)
            hangup_confirmed = True
        with suppress(AttributeError, RuntimeError, TypeError, ValueError):
            call_record._telnyx_hangup_dispatched = True
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        hangup_confirmed = False
        logger.warning("end_call: Telnyx hangup failed for %s: %s", call_record.call_id, exc)

    if not hangup_confirmed:
        logger.warning(
            "end_call: hangup unconfirmed for %s; leaving call record non-terminal for retry/reconciliation",
            getattr(call_record, "call_id", ""),
        )
        return

    # The carrier hangup is confirmed dispatched, so THIS is when the billable
    # leg ends on the agent-side stop path (#2589). Without it, billing falls
    # back to the pipeline teardown stamp, which can be up to
    # PHONE_PIPELINE_IDLE_TIMEOUT_S (150s) later. Earliest-wins, and never
    # overwrites an anchor another path already set.
    with suppress(AttributeError, RuntimeError, TypeError, ValueError):
        if getattr(call_record, "local_end_at", None) is None:
            call_record.local_end_at = datetime.now(tz=UTC)

    try:
        voicemail_detected = bool(getattr(call_record, "voicemail_detected", False))
        human_takeover_detected = bool(getattr(call_record, "human_takeover_detected", False))
        no_message_left = bool(getattr(call_record, "voicemail_no_message_left", False)) and not bool(
            getattr(call_record, "voicemail_message_delivered", False)
        )
        if voicemail_detected and not human_takeover_detected and no_message_left:
            # Reached a recording but nothing got out: not a delivered voicemail.
            call_record.status = CallStatus.FAILED
            call_record.outcome = call_record.outcome or "Reached voicemail, but no message was left."
        elif voicemail_detected and not human_takeover_detected:
            call_record.status = CallStatus.VOICEMAIL
            call_record.outcome = call_record.outcome or "Voicemail handled by Viola: %s" % reason_text
        else:
            call_record.status = CallStatus.COMPLETED
            call_record.outcome = call_record.outcome or "Ended by Viola: %s" % reason_text
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("end_call: failed to mark CallRecord completed: %s", exc)


class EndCallHangupAfterOutputProcessor(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Hang up after Telnyx confirms all prior WebSocket media has played."""

    def __init__(
        self,
        *,
        telnyx_client: Any,
        call_control_id_getter: Callable[[], str],
        call_record: Any,
        transport: Any,
        mark_timeout_seconds: float = END_CALL_MEDIA_MARK_TIMEOUT_SECONDS,
        playout_margin_seconds: float = END_CALL_PLAYOUT_MARGIN_SECONDS,
        max_mark_wait_seconds: float = END_CALL_MEDIA_MARK_MAX_WAIT_SECONDS,
        speech_production_max_wait_seconds: float = END_CALL_SPEECH_PRODUCTION_MAX_WAIT_SECONDS,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="end_call_hangup_after_output")
        self._telnyx_client = telnyx_client
        self._call_control_id_getter = call_control_id_getter
        self._call_record = call_record
        self._transport = transport
        self._mark_timeout_seconds = mark_timeout_seconds
        self._playout_margin_seconds = playout_margin_seconds
        self._max_mark_wait_seconds = max_mark_wait_seconds
        self._speech_production_max_wait_seconds = speech_production_max_wait_seconds
        self._dispatched = False
        self._listen_task: asyncio.Task[None] | None = None
        self._hangup_task: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not PIPECAT_AVAILABLE:
            return

        await super().process_frame(frame, direction)

        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, EndCallHangupFrame)
            and frame.call_id == getattr(self._call_record, "call_id", "")
        ):
            await self._hangup_after_output_mark(frame)
            return

        await self.push_frame(frame, direction)

    async def _hangup_after_output_mark(self, frame: EndCallHangupFrame) -> None:
        if self._dispatched:
            return
        self._dispatched = True

        answer_watch = getattr(self._call_record, "_voicemail_answer_watch", None)
        if frame.listen_first and answer_watch is not None:
            # Not inline: frames keep flowing here while her message plays and the
            # line is listened to, and an interruption cannot cancel the wait.
            self._listen_task = asyncio.create_task(
                self._hang_up_unless_answered(frame, answer_watch),
                name="voicemail-answer-window-%s" % frame.call_id,
            )
            return

        await self._wait_for_output_playout(frame)
        await self._dispatch_hangup(frame)

    def _output_playout_remaining(self) -> float:
        playout_remaining = getattr(self._transport, "output_playout_remaining", None)
        if not callable(playout_remaining):
            return 0.0
        try:
            return max(0.0, float(playout_remaining()))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return 0.0

    def _output_speech_in_progress(self) -> bool:
        speech_in_progress = getattr(self._transport, "output_speech_in_progress", None)
        if not callable(speech_in_progress):
            return False
        try:
            return bool(speech_in_progress())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    @staticmethod
    async def _unless_answered(work: Awaitable[Any], answer_watch: Any) -> tuple[bool, Any]:
        """Await ``work`` unless a person answers first. Returns (answered, result)."""
        wait_answered = getattr(answer_watch, "wait_answered", None)
        if not callable(wait_answered):
            return False, await work
        work_task = asyncio.ensure_future(work)
        answered_task = asyncio.ensure_future(wait_answered())
        try:
            done, _pending = await asyncio.wait({work_task, answered_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (work_task, answered_task):
                if not task.done():
                    task.cancel()
        if work_task in done:
            return False, work_task.result()
        return True, None

    async def _wait_for_turn_speech(self, frame: EndCallHangupFrame, answer_watch: Any, deadline: float) -> bool:
        """Wait while the turn's speech is still being produced. False if a person answered.

        Read from the transport, not from frames: on a live call this processor's
        own frame handling is blocked while it waits.
        """
        if not self._output_speech_in_progress():
            return True
        started = time.monotonic()
        logger.info(
            "end_call: speech for this turn is still being produced on %s; waiting for it (up to %.0fs)",
            frame.call_id,
            max(0.0, deadline - started),
        )
        while self._output_speech_in_progress():
            if answer_watch is not None and bool(getattr(answer_watch, "answered", False)):
                return False
            if time.monotonic() >= deadline:
                logger.warning(
                    "end_call: speech for this turn on %s was still being produced after %.1fs; going on",
                    frame.call_id,
                    time.monotonic() - started,
                )
                return True
            await asyncio.sleep(_SPEECH_POLL_SECONDS)
        logger.info(
            "end_call: the turn's speech on %s finished being produced after %.1fs",
            frame.call_id,
            time.monotonic() - started,
        )
        return True

    async def _wait_for_output_playout(self, frame: EndCallHangupFrame, answer_watch: Any = None) -> None:
        """Return once the carrier has played all of the turn's speech.

        First the turn's speech must exist (it can still be in the TTS when this
        frame arrives), then it must have played: the mark echo is the proof, the
        playback estimate the floor. Audio handed over during a wait gets a new
        mark. Returns early only if a person answers her message.
        """
        started = time.monotonic()
        deadline = started + max(self._mark_timeout_seconds, self._max_mark_wait_seconds)
        production_deadline = min(deadline, started + self._speech_production_max_wait_seconds)
        wait_for_output_mark = getattr(self._transport, "wait_for_output_mark", None)
        if not callable(wait_for_output_mark):
            logger.warning("end_call: Telnyx transport has no output mark support; hanging up fail-closed")
        first_round = True
        while True:
            if not await self._wait_for_turn_speech(frame, answer_watch, production_deadline):
                return
            playout = self._output_playout_remaining()
            if not first_round and playout <= _PLAYOUT_SETTLED_SECONDS:
                return
            budget = deadline - time.monotonic()
            if budget <= 0:
                logger.warning(
                    "end_call: gave up waiting for playback on %s after %.0fs (estimated %.1fs still to play)",
                    frame.call_id,
                    time.monotonic() - started,
                    playout,
                )
                return
            timeout = min(
                max(self._mark_timeout_seconds if first_round else 0.0, playout + self._playout_margin_seconds),
                budget,
            )
            if not first_round:
                logger.info(
                    "end_call: more audio was handed over on %s while waiting; waiting for its %.1fs too",
                    frame.call_id,
                    playout,
                )
            elif playout > self._mark_timeout_seconds:
                logger.info(
                    "end_call: waiting up to %.1fs for the last %.1fs of audio to play on %s",
                    timeout,
                    playout,
                    frame.call_id,
                )
            first_round = False
            if not callable(wait_for_output_mark):
                rest = self._output_playout_remaining()
                if rest <= 0:
                    return
                answered, _ = await self._unless_answered(asyncio.sleep(min(rest, budget)), answer_watch)
                if answered:
                    return
                continue

            mark_started = time.monotonic()
            mark_name = "end_call_%s_%s_%d" % (frame.call_id, getattr(frame, "id", "mark"), int(mark_started * 1000))
            drained = False
            try:
                answered, drained = await self._unless_answered(
                    wait_for_output_mark(mark_name, timeout=timeout), answer_watch
                )
                if answered:
                    return
                if drained:
                    logger.info("end_call: Telnyx media mark drained for %s", frame.call_id)
                else:
                    logger.warning(
                        "end_call: Telnyx media mark did not confirm drain for %s after %.1fs "
                        "(estimated playback %.1fs); hanging up fail-closed",
                        frame.call_id,
                        time.monotonic() - mark_started,
                        playout,
                    )
            except (ConnectionError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
                logger.warning(
                    "end_call: Telnyx media mark wait failed for %s: %s; hanging up fail-closed",
                    frame.call_id,
                    exc,
                )
            if not drained:
                # Whatever ended the wait early, never cut off audio still playing.
                rest = self._output_playout_remaining()
                budget = deadline - time.monotonic()
                if rest > 0 and budget > 0:
                    answered, _ = await self._unless_answered(asyncio.sleep(min(rest, budget)), answer_watch)
                    if answered:
                        return

    async def _hang_up_unless_answered(self, frame: EndCallHangupFrame, answer_watch: Any) -> None:
        await self._wait_for_output_playout(frame, answer_watch)
        if not await answer_watch.nobody_answered():
            self._dispatched = False
            return
        logger.info("end_call: nobody answered the voicemail message on %s; hanging up", frame.call_id)
        with suppress(AttributeError, TypeError):
            self._call_record._end_call_committed = True
        await self._dispatch_hangup(frame)

    async def cleanup(self) -> None:
        # The carrier tears the pipeline down when it carries out our own hangup,
        # often before the hangup request has returned (call e9b92727). Let that
        # request finish so the record gets its terminal status here.
        hangup_task = self._hangup_task
        if hangup_task is not None and not hangup_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(hangup_task), timeout=END_CALL_HANGUP_TEARDOWN_WAIT_SECONDS)
            except TimeoutError:
                logger.warning(
                    "end_call: Telnyx hangup request still open at teardown for %s",
                    getattr(self._call_record, "call_id", ""),
                )
            except Exception as exc:  # noqa: BLE001 - logged; teardown goes on
                logger.debug("end_call: hangup request ended with %s at teardown", exc)
        if self._listen_task is not None and not self._listen_task.done():
            self._listen_task.cancel()
        await super().cleanup()

    async def _dispatch_hangup(self, frame: EndCallHangupFrame) -> None:
        # Its own task, shielded: cancelling the caller (teardown) must not cut the
        # request off between the carrier acting on it and the record being marked.
        self._hangup_task = asyncio.ensure_future(
            dispatch_telnyx_end_call_hangup(
                telnyx_client=self._telnyx_client,
                call_control_id=str(self._call_control_id_getter() or ""),
                call_record=self._call_record,
                reason_text=frame.reason,
            )
        )
        await asyncio.shield(self._hangup_task)
