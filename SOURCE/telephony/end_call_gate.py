"""End-call lifecycle guard for the hangup drain window.

Sits between STT/transcription_observer and the user aggregator in the phone
pipeline. Once ``call_record._end_call_committed`` flips True (the moment the
end_call tool handler runs), this processor drops inbound recipient-turn frames
so the Pipecat user aggregator does not start a new LLM turn while the carrier
hangup is draining.

This is a narrow transport/lifecycle guard, not the semantic owner-leak fix.
Viola's understanding of who is on the phone line lives in the phone-call
context. The guard only preserves the hangup invariant: the grace window drains
already-queued closing speech, never a new recipient conversation.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import (
        InterimTranscriptionFrame,
        InterruptionFrame,
        TranscriptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without pipecat installed
    PIPECAT_AVAILABLE = False

# Frame types that would start a new recipient turn during the hangup drain.
# Dropping these after end_call commits is lifecycle cleanup, not a substitute
# for model context about the owner/recipient boundary.
_BLOCKED_FRAME_TYPES: tuple[type, ...] = (
    (
        TranscriptionFrame,
        InterimTranscriptionFrame,
        InterruptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    if PIPECAT_AVAILABLE
    else ()
)


class EndCallGateProcessor(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Drops inbound recipient-turn frames during the end_call drain window."""

    def __init__(self, call_record: Any) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__()
        self._record = call_record
        self._logged_block = False

    def _end_call_committed(self) -> bool:
        return bool(getattr(self._record, "_end_call_committed", False))

    async def process_frame(self, frame, direction):
        if PIPECAT_AVAILABLE and _BLOCKED_FRAME_TYPES and isinstance(frame, _BLOCKED_FRAME_TYPES):
            if self._end_call_committed():
                if not self._logged_block:
                    self._logged_block = True
                    logger.info(
                        "end_call lifecycle guard: dropping recipient turn during hangup drain for call=%s",
                        getattr(self._record, "call_id", "unknown"),
                    )
                return

        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
