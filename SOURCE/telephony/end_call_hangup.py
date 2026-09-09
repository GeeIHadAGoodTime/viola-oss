"""Event-driven Telnyx hangup after final phone TTS media drains."""

from __future__ import annotations

from collections.abc import Callable
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


@dataclass
class EndCallHangupFrame(ControlFrame):  # type: ignore[misc]
    """Queued behind final TTS audio so hangup can wait for carrier playback."""

    call_id: str
    reason: str


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
        if voicemail_detected and not human_takeover_detected:
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
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="end_call_hangup_after_output")
        self._telnyx_client = telnyx_client
        self._call_control_id_getter = call_control_id_getter
        self._call_record = call_record
        self._transport = transport
        self._mark_timeout_seconds = mark_timeout_seconds
        self._dispatched = False

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

        mark_name = "end_call_%s_%s" % (frame.call_id, getattr(frame, "id", "mark"))
        wait_for_output_mark = getattr(self._transport, "wait_for_output_mark", None)
        if callable(wait_for_output_mark):
            try:
                drained = await wait_for_output_mark(mark_name, timeout=self._mark_timeout_seconds)
                if drained:
                    logger.info("end_call: Telnyx media mark drained for %s", frame.call_id)
                else:
                    logger.warning(
                        "end_call: Telnyx media mark did not confirm drain for %s; hanging up fail-closed",
                        frame.call_id,
                    )
            except (ConnectionError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
                logger.warning(
                    "end_call: Telnyx media mark wait failed for %s: %s; hanging up fail-closed",
                    frame.call_id,
                    exc,
                )
        else:
            logger.warning("end_call: Telnyx transport has no output mark support; hanging up fail-closed")

        await dispatch_telnyx_end_call_hangup(
            telnyx_client=self._telnyx_client,
            call_control_id=str(self._call_control_id_getter() or ""),
            call_record=self._call_record,
            reason_text=frame.reason,
        )
