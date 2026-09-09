"""Call-scoped suppression state for spoken phone payment frames.

The controller also owns the only in-process evidence that a payment segment
actually reached the line. ``begin_payment_segment`` is called when the card
text is *queued*; ``end_payment_segment`` is called by the outbound audio tee
when the closing :class:`PaymentTransmitEndFrame` arrives there, which is
strictly downstream of the TTS service in the call pipeline. So the outcome
recorded here distinguishes "we queued it" from "the card was spoken", and
``wait_for_outcome`` lets the transmit tool await that distinction instead of
assuming it.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_ACTIVE_PAYMENT_SEGMENTS: dict[str, str] = {}
_ACTIVE_PAYMENT_SEGMENTS_LOCK = RLock()
_DEFAULT_MAX_DURATION_SECONDS = 45.0

# Segment outcomes. Only TRANSMITTED means audio for the card actually flowed
# past the outbound tee; everything else means it did not, or is unknown.
OUTCOME_TRANSMITTED = "transmitted"
OUTCOME_ABORTED = "aborted"
OUTCOME_EXPIRED = "expired"
OUTCOME_UNCONFIRMED = "unconfirmed"


@dataclass
class PaymentSegmentState:
    segment_id: str
    call_id: str
    last4: str
    reason: str
    marker: str
    started_monotonic: float
    outcome: str | None = None
    settled: asyncio.Event = field(default_factory=asyncio.Event)


class PaymentSensitiveSegmentController:
    """Owns the active payment-sensitive segment for one call."""

    def __init__(self, *, call_id: str, max_duration_seconds: float = _DEFAULT_MAX_DURATION_SECONDS) -> None:
        self.call_id = call_id
        self._max_duration_seconds = max_duration_seconds
        self._active: PaymentSegmentState | None = None
        self._transcript_collector: Any | None = None
        # Settled segments stay reachable by id so a waiter that arrives after
        # settlement still reads the real outcome instead of timing out.
        self._settled: dict[str, PaymentSegmentState] = {}

    @property
    def max_duration_seconds(self) -> float:
        return self._max_duration_seconds

    def _settle(self, state: PaymentSegmentState, outcome: str) -> None:
        """Record a terminal outcome for ``state`` exactly once and wake waiters."""
        if state.outcome is None:
            state.outcome = outcome
        self._settled[state.segment_id] = state
        if len(self._settled) > 8:
            for stale_id in list(self._settled)[:-8]:
                self._settled.pop(stale_id, None)
        try:
            state.settled.set()
        except RuntimeError:
            # No running loop bound to the event (segment settled from a
            # non-async teardown path). The outcome is still recorded, which is
            # what a waiter reads first.
            logger.debug("Payment segment %s settled without a live event loop", state.segment_id)

    def _state_for(self, segment_id: str) -> PaymentSegmentState | None:
        state = self._active
        if state is not None and state.segment_id == segment_id:
            return state
        return self._settled.get(segment_id)

    def outcome_for(self, segment_id: str) -> str | None:
        """Return the terminal outcome for ``segment_id``, or None while in flight."""
        state = self._state_for(segment_id)
        return None if state is None else state.outcome

    async def wait_for_outcome(self, segment_id: str, *, timeout: float | None = None) -> str:
        """Await the real fate of a payment segment.

        Returns :data:`OUTCOME_TRANSMITTED` only when the closing frame reached
        the outbound audio tee, i.e. the card audio was synthesized and pushed
        downstream. Returns :data:`OUTCOME_UNCONFIRMED` when the wait elapses
        without a terminal signal — the caller must treat that as "not known to
        have happened", never as success.
        """
        state = self._state_for(segment_id)
        if state is None:
            return OUTCOME_ABORTED
        if state.outcome is not None:
            return state.outcome
        budget = self._max_duration_seconds if timeout is None else timeout
        try:
            await asyncio.wait_for(state.settled.wait(), timeout=budget)
        except TimeoutError:
            return state.outcome or OUTCOME_UNCONFIRMED
        return state.outcome or OUTCOME_UNCONFIRMED

    def attach_transcript_collector(self, transcript_collector: Any) -> None:
        self._transcript_collector = transcript_collector

    @property
    def active(self) -> bool:
        self._expire_stale_segment()
        return self._active is not None

    @property
    def active_segment_id(self) -> str:
        state = self._active
        return "" if state is None else state.segment_id

    @property
    def marker(self) -> str:
        return self._active.marker if self._active is not None else "[paid with card ending unknown]"

    def begin_payment_segment(self, *, last4: str, reason: str = "phone_payment") -> str:
        segment_id = uuid.uuid4().hex
        safe_last4 = "".join(ch for ch in str(last4) if ch.isdigit())[-4:] or "unknown"
        self._active = PaymentSegmentState(
            segment_id=segment_id,
            call_id=self.call_id,
            last4=safe_last4,
            reason=reason,
            marker="[paid with card ending %s]" % safe_last4,
            started_monotonic=monotonic(),
        )
        with _ACTIVE_PAYMENT_SEGMENTS_LOCK:
            _ACTIVE_PAYMENT_SEGMENTS[segment_id] = self._active.marker
        return segment_id

    def end_payment_segment(self, segment_id: str) -> str | None:
        """Close a segment because its audio reached the outbound tee.

        This is the ONLY path that records a transmitted outcome — it runs from
        the tee processor downstream of TTS, so reaching it means the card was
        actually spoken onto the call rather than merely queued.
        """
        state = self._active
        if state is None or state.segment_id != segment_id:
            return None
        marker = state.marker
        self._active = None
        with _ACTIVE_PAYMENT_SEGMENTS_LOCK:
            _ACTIVE_PAYMENT_SEGMENTS.pop(segment_id, None)
        self._settle(state, OUTCOME_TRANSMITTED)
        if self._transcript_collector is not None:
            self._transcript_collector.add_assistant(marker)
        return marker

    def abort_payment_segment(self, segment_id: str, *, outcome: str = OUTCOME_ABORTED) -> None:
        state = self._active
        if state is not None and state.segment_id == segment_id:
            self._active = None
            self._settle(state, outcome)
        with _ACTIVE_PAYMENT_SEGMENTS_LOCK:
            _ACTIVE_PAYMENT_SEGMENTS.pop(segment_id, None)

    def trace_payload(self, payload: Any) -> Any:
        if not self.active:
            return payload
        return {"payment_sensitive_segment": "active", "marker": self.marker}

    def _expire_stale_segment(self) -> None:
        state = self._active
        if state is None:
            return
        if monotonic() - state.started_monotonic <= self._max_duration_seconds:
            return
        self.abort_payment_segment(state.segment_id, outcome=OUTCOME_EXPIRED)


def payment_logging_suppressed() -> bool:
    with _ACTIVE_PAYMENT_SEGMENTS_LOCK:
        return bool(_ACTIVE_PAYMENT_SEGMENTS)
