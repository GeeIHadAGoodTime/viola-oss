"""Observes transcription frames for hold detection and language switching.

Sits between STT and user_aggregator in the pipeline. Intercepts
TranscriptionFrame, routes text to hold_handler and language_handler,
then ALWAYS passes the frame through (observer, not filter).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.events.bus import get_event_bus
from core.events.types import CallTranscriptDelta
from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import (
        InterruptionFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TextFrame,
        TranscriptionFrame,
        TTSSpeakFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False

if TYPE_CHECKING:
    from telephony.call_manager import TranscriptCollector
    from telephony.hold_handler import HoldModeHandler


class TranscriptFrameCollector(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc] # PHONE-02: Pipecat fallback base.
    """Taps Pipecat text frames and appends live turns to a transcript."""

    def __init__(
        self,
        transcript_collector: TranscriptCollector,
        *,
        call_id: str | None = None,
        capture_user: bool = True,
        capture_assistant: bool = True,
        on_assistant_complete: Any | None = None,
    ):
        if PIPECAT_AVAILABLE:
            super().__init__()
        self._transcript = transcript_collector
        record = getattr(transcript_collector, "_record", None)
        self._call_id = call_id or getattr(record, "call_id", None)
        self._capture_user = capture_user
        self._capture_assistant = capture_assistant
        self._assistant_chunks: list[str] = []
        self._on_assistant_complete = on_assistant_complete
        self._partial_assistant_buffer: str = ""
        # True when an interruption cancelled the generation currently being
        # accumulated. Cleared at each generation start and consumed at its end.
        self._generation_interrupted: bool = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if PIPECAT_AVAILABLE:
            await self._capture_frame(frame)
            self._publish_transcript_delta(frame)

        await self.push_frame(frame, direction)

    async def _capture_frame(self, frame) -> None:
        if getattr(frame, "viola_skip_transcript", False) or self._payment_segment_active():
            return

        if self._capture_user and isinstance(frame, TranscriptionFrame):
            if self._is_final_transcription(frame):
                self._transcript.add_user(frame.text, getattr(frame, "timestamp", None))
                # PHONE-15 path-drop recovery: the recipient spoke again, so the
                # conversation is continuing — a text-empty end_call that was
                # latched on a prior turn is now stale. Cancel it so the next
                # spoken assistant turn does not auto-hang-up mid-conversation.
                record = getattr(self._transcript, "_record", None)
                if record is not None and getattr(record, "_pending_end_call", None) is not None:
                    from telephony.call_tools import cancel_latched_end_call

                    cancel_latched_end_call(record, reason="recipient_spoke")
            return

        if not self._capture_assistant:
            return

        # An interruption cancels the in-flight assistant generation: its audio is
        # dropped downstream at the TTS service. This collector sits UPSTREAM of TTS,
        # so without honoring the interruption it keeps the cancelled generation's
        # accumulated text and concatenates the replacement generation onto it,
        # recording the turn doubled ("Thanks, 5 p.m. today.Thanks, 5 p.m. today.")
        # while the call audio spoke it once (rung-3 159f7b1e). Drop the partial so
        # the transcript matches what was actually said. Match the base
        # InterruptionFrame -- pipecat 0.0.107 emits InterruptionFrame at runtime;
        # StartInterruptionFrame is a deprecated subclass, so matching the base
        # catches both (matching only the subclass would miss the live frame).
        if isinstance(frame, InterruptionFrame):
            self._assistant_chunks = []
            # Remember that THIS generation was cancelled. The voicemail response
            # gate needs the difference between "ended having spoken nothing
            # because it was cut off" (release what it is holding, or Viola's one
            # message never gets out — 857c52b2) and "ended with no text because
            # the model only called a tool" (keep holding; the same turn continues
            # after the tool result). The interruption alone cannot be the signal:
            # it is a SystemFrame and can overtake the queued end frame, so the
            # verdict is taken at the end frame, below.
            self._generation_interrupted = True
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._assistant_chunks = []
            self._generation_interrupted = False
        elif isinstance(frame, LLMTextFrame):
            self._assistant_chunks.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = "".join(self._assistant_chunks).strip()
            self._assistant_chunks = []
            if text:
                self._transcript.add_assistant(text)
                # PHONE-15: mark first-turn-complete when a non-empty assistant
                # turn finishes. The end_call tool's safety guard reads this to
                # distinguish hallucinated early-end (no turn yet) from
                # legitimate quick-end (turn delivered, task complete).
                record = getattr(self._transcript, "_record", None)
                if record is not None and not getattr(record, "first_assistant_turn_complete", False):
                    record.first_assistant_turn_complete = True
                # Voicemail delivery signal: the first non-empty assistant turn
                # that COMPLETES after voicemail detection IS Viola's one
                # voicemail message reaching TTS. An interrupted generation
                # never reaches this branch (InterruptionFrame clears the
                # chunks above), so this flips True only on a real delivery --
                # the signal _VoicemailResponseGate uses to stop allowing the
                # voicemail context to re-fire (no repeat) while still letting
                # an interrupt-then-refire deliver the message once.
                if (
                    record is not None
                    and getattr(record, "voicemail_detected", False)
                    and not getattr(record, "voicemail_message_delivered", False)
                ):
                    record.voicemail_message_delivered = True
                    await self._notify_voicemail_gate(record, "note_voicemail_message_delivered")
            elif self._generation_interrupted:
                # Cut off before a word survived. Tell the gate so a context frame
                # it is holding behind this generation is released instead of
                # silently absorbed — the difference between one message and dead
                # air. A text-less end frame WITHOUT an interruption is a tool-call
                # segment of a turn that is still going, so it says nothing here.
                self._generation_interrupted = False
                record = getattr(self._transcript, "_record", None)
                if (
                    record is not None
                    and getattr(record, "voicemail_detected", False)
                    and not getattr(record, "voicemail_message_delivered", False)
                ):
                    await self._notify_voicemail_gate(record, "note_voicemail_generation_undelivered")
            if self._on_assistant_complete and text:
                try:
                    await self._on_assistant_complete(text)
                except Exception:
                    logger.exception("on_assistant_complete callback raised")
        elif isinstance(frame, (TextFrame, TTSSpeakFrame)):
            self._transcript.add_assistant(frame.text)
            # PHONE-15: TTSSpeakFrame represents an assistant turn that
            # bypasses the LLM (e.g. greeting injection). Mark turn-complete
            # the same way so quick-greeting + end_call doesn't false-refuse.
            record = getattr(self._transcript, "_record", None)
            if record is not None and not getattr(record, "first_assistant_turn_complete", False):
                record.first_assistant_turn_complete = True

    async def _notify_voicemail_gate(self, record, method_name: str) -> None:
        """Tell the voicemail response gate how the in-flight generation resolved.

        This collector is the only processor that watches a generation from its
        start frame to its end frame, so it is the only place that can say whether
        anything was actually spoken. The gate sits UPSTREAM of the LLM and has to
        decide about the recording's next prompt before that is knowable, which is
        exactly why it holds rather than guesses (#4796).
        """
        handler = getattr(record, "_voicemail_handler", None)
        notify = getattr(handler, method_name, None)
        if notify is None:
            return
        try:
            await notify()
        except Exception:
            logger.exception("voicemail gate notification %s raised", method_name)

    def _publish_transcript_delta(self, frame) -> None:
        if getattr(frame, "viola_skip_transcript", False) or self._payment_segment_active():
            return

        if not self._call_id:
            return

        if self._capture_user and isinstance(frame, TranscriptionFrame):
            if self._is_final_transcription(frame):
                self._publish_delta(role="them", text=frame.text, partial=False)
            return

        if not self._capture_assistant:
            return

        if isinstance(frame, (InterruptionFrame, LLMFullResponseStartFrame)):
            self._partial_assistant_buffer = ""
        elif isinstance(frame, LLMTextFrame):
            self._partial_assistant_buffer += frame.text
            self._publish_delta(role="viola", text=self._partial_assistant_buffer, partial=True)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._partial_assistant_buffer = ""
        elif isinstance(frame, (TextFrame, TTSSpeakFrame)):
            self._partial_assistant_buffer = ""
            self._publish_delta(role="viola", text=frame.text, partial=False)

    def _publish_delta(self, *, role: str, text: str, partial: bool) -> None:
        try:
            bus = get_event_bus()
            if bus:
                # PHONE_TRANSCRIPT_L1_PUBLISH: link 1 of the live-transcript chain.
                # Finals at INFO; partials at DEBUG (word-stream). If this fires but
                # PHONE_TRANSCRIPT_L2_BRIDGE never does, the bus subscriber isn't
                # receiving (bus-instance / subscription break upstream of link 2).
                if partial:
                    logger.debug("PHONE_TRANSCRIPT_L1_PUBLISH call=%s role=%s partial=True", self._call_id or "", role)
                else:
                    logger.info("PHONE_TRANSCRIPT_L1_PUBLISH call=%s role=%s partial=False", self._call_id or "", role)
                bus.publish(
                    CallTranscriptDelta(
                        call_id=self._call_id or "",
                        role=role,
                        text=text,
                        partial=partial,
                        ts=datetime.now(tz=UTC).isoformat(),
                    )
                )
        except Exception as exc:
            logger.debug("Failed to publish call transcript delta: %s", exc)

    @staticmethod
    def _is_final_transcription(frame) -> bool:
        return getattr(frame, "finalized", True) is not False

    def _payment_segment_active(self) -> bool:
        record = getattr(self._transcript, "_record", None)
        controller = getattr(record, "_payment_sensitive_segment", None)
        return controller is not None and bool(getattr(controller, "active", False))


class TranscriptionObserver(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc] # PHONE-02: Pipecat fallback base.
    """Taps transcription frames and routes to hold + language handlers."""

    def __init__(
        self,
        hold_handler: HoldModeHandler | None = None,
        language_handler=None,
        on_recipient_transcript: Callable[[str], None] | None = None,
    ):
        if PIPECAT_AVAILABLE:
            super().__init__()
        self._hold = hold_handler
        self._lang = language_handler
        self._on_recipient_transcript = on_recipient_transcript

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if PIPECAT_AVAILABLE and isinstance(frame, TranscriptionFrame):
            if self._on_recipient_transcript and self._is_final_transcription(frame) and frame.text:
                try:
                    self._on_recipient_transcript(frame.text)
                except Exception:
                    logger.exception("on_recipient_transcript callback raised")

            # Route to hold handler
            if self._hold and self._hold.is_on_hold and frame.text:
                await self._hold.on_transcription(frame.text)

            # Route to language handler (language info available on frame)
            if self._lang and frame.text and hasattr(frame, "language") and frame.language:
                lang_code = str(frame.language.value) if hasattr(frame.language, "value") else str(frame.language)
                confidence = getattr(frame, "language_probability", 0.9)
                await self._lang.on_transcription_with_language(
                    frame.text,
                    lang_code,
                    confidence,
                )

        await self.push_frame(frame, direction)

    @staticmethod
    def _is_final_transcription(frame) -> bool:
        return getattr(frame, "finalized", True) is not False
