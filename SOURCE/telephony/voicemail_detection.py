"""Structured voicemail carrier-event bridge for live phone calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.extensions.voicemail.voicemail_detector import VoicemailDetector
    from pipecat.frames.frames import (
        LLMContextFrame,
        LLMMessagesAppendFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False
    LLMMessagesAppendFrame = None  # type: ignore[assignment]
    LLMContextFrame = None  # type: ignore[assignment]
    TranscriptionFrame = None  # type: ignore[assignment]
    FrameDirection = None  # type: ignore[assignment]
    FrameProcessor = object  # type: ignore[assignment,misc]
    VoicemailDetector = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from telephony.call_manager import CallRecord


AMD_MACHINE_RESULTS = {"machine"}
AMD_GREETING_READY_RESULTS = {"beep_detected", "ended", "no_beep_detected", "not_sure"}
AMD_HUMAN_RESULTS = {"human", "human_business", "human_residence"}

# Conservative voicemail-classifier prompt — fail SAFE toward a live human.
#
# The pipecat default prompt, judged by the fast phone classifier (gpt-5.4-mini at
# reasoning effort "none", founder-locked for latency), false-positived real
# receptionist turns as VOICEMAIL: an offer of appointment slots
# ("We can do Wednesday at 9 AM or Thursday at 2 PM. Which one does Jay want?"),
# an escalation ("I need to transfer you"), and an availability question were each
# flipped to VOICEMAIL, after which Viola left a one-shot voicemail message and
# hung up ON A LIVE PERSON. That asymmetry is the whole reason this prompt must be
# conservative: briefly treating a real voicemail as a conversation is recoverable
# (Viola simply keeps talking, the model's own voicemail-handling prompt still
# leaves one coherent message, and the carrier AMD / beep still catches it), but
# hanging up on a human after dumping a voicemail is not. The prompt is a two-step
# decision so the bias does not blind it to a genuine recording: STEP 1 answers
# VOICEMAIL on an explicit voicemail/recording phrase ("forwarded to voicemail",
# "the person you are trying to reach is not available", "leave a message", "at the
# tone", carrier "not in service"/"mailbox is full"); STEP 2 otherwise defaults to
# CONVERSATION, with any question to the caller, any offer of options/appointments,
# any transfer or hold, and any back-and-forth explicitly held as a LIVE PERSON.
# The required CONVERSATION/VOICEMAIL response tokens are preserved so pipecat's
# prompt validation and the verdict parser keep working.
CONSERVATIVE_VOICEMAIL_CLASSIFIER_PROMPT = (
    """You are a voicemail detection classifier for an OUTBOUND calling system. A bot called a number; decide whether a LIVE HUMAN answered or the call reached an automated voicemail/recording, based only on the provided text.

Decide in this order:

STEP 1 — Is there an explicit voicemail/recording phrase? If the text contains ANY of these (or a close paraphrase), answer VOICEMAIL. These are decisive even if the rest sounds conversational:
- "forwarded to voicemail", "you've reached the voicemail", "you have reached", "your call has been forwarded"
- "the person you are trying to reach is not available", "is not available right now", "is unavailable"
- "leave a message", "record your message", "at the tone", "after the beep", "press one for more options" combined with leaving a message
- carrier/system recordings: "the number you have dialed is not in service", "the mailbox is full", "this mailbox has not been set up", "all circuits are busy"

STEP 2 — Otherwise, default to CONVERSATION. A live human wrongly treated as voicemail gets hung up on, which is far worse than briefly mishearing a recording, so when there is NO explicit voicemail phrase from STEP 1, answer CONVERSATION. In particular these are ALWAYS a LIVE PERSON, never voicemail:
- A question directed at the caller: "Who is this?", "How can I help?", "What can I do for you?", "What time is it where you are?"
- An offer of choices, appointments, or availability: "We can do Wednesday at 9 or Thursday at 2 — which works?", "We have an opening Thursday at 2."
- Asking the caller to hold, wait, or be transferred: "One moment", "I'll transfer you", "Let me get someone."
- A receptionist or business greeting that invites a response: "Thanks for calling Maple Books, how can I help?"
- Any back-and-forth, acknowledgment, or spontaneous human reply: "Hello?", "Speaking", "Yep", "Go ahead."

A real business offering a time, asking a question, or transferring the call is a LIVE PERSON. A recording telling you the person is unavailable or to leave a message is VOICEMAIL.

"""
    + 'Respond with ONLY "CONVERSATION" if a person answered, or "VOICEMAIL" if it\'s an automated voicemail/recording.'
)


class VoicemailDetectionHandler(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Publish raw Telnyx AMD context without classifying recipient transcript text."""

    def __init__(
        self,
        *,
        llm: Any,
        call_record: CallRecord,
        fallback_delay_seconds: float | None = 8.0,
        on_voicemail_ready: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__()
        self._llm = llm
        self._record = call_record
        self._fallback_delay_seconds = fallback_delay_seconds
        self._on_voicemail_ready = on_voicemail_ready
        self._notice_sent = False
        self._fallback_task: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Any, direction: Any) -> None:
        await super().process_frame(frame, direction)

        if PIPECAT_AVAILABLE and isinstance(frame, TranscriptionFrame):
            await self.on_transcription(frame.text, getattr(frame, "timestamp", None))

        await self.push_frame(frame, direction)

    async def on_transcription(self, text: str, timestamp: Any = None) -> bool:
        """Observe STT text without suppressing it or deriving call state from it."""

        del text, timestamp
        return False

    async def on_amd_result(self, result: str, event_type: str = "") -> bool:
        """Handle structured Telnyx AMD webhook results for this active call."""

        normalized = (result or "").strip().lower()
        if not normalized or normalized in AMD_HUMAN_RESULTS:
            return False

        if "greeting" in event_type and normalized in AMD_GREETING_READY_RESULTS:
            self._mark_detected("amd", normalized)
            await self._publish_amd_context("amd_greeting", normalized, event_type)
            return True

        if normalized == "not_sure":
            return False

        if normalized in AMD_MACHINE_RESULTS:
            self._mark_detected("amd", normalized)
            if "greeting" in event_type:
                await self._publish_amd_context("amd_greeting", normalized, event_type)
            else:
                self._schedule_fallback("amd_machine", normalized, event_type)
            return True

        if normalized in AMD_GREETING_READY_RESULTS:
            self._mark_detected("amd", normalized)
            await self._publish_amd_context("amd_greeting", normalized, event_type)
            return True

        return False

    def _mark_detected(self, source: str, evidence: str) -> None:
        if not self._record.voicemail_detected:
            self._record.voicemail_detected_at = datetime.now(tz=UTC)
            self._record.voicemail_detection_source = source
            self._record.outcome = self._record.outcome or "Carrier reported voicemail via %s" % source
            logger.info(
                "Call %s: voicemail carrier event via %s (%s)",
                self._record.call_id,
                source,
                evidence[:80],
            )
        self._record.voicemail_detected = True

    def _schedule_fallback(self, source: str, result: str, event_type: str) -> None:
        if self._notice_sent or self._fallback_delay_seconds is None:
            return
        if self._fallback_task is not None and not self._fallback_task.done():
            return
        self._fallback_task = asyncio.create_task(self._fallback_after_delay(source, result, event_type))

    async def _fallback_after_delay(self, source: str, result: str, event_type: str) -> None:
        assert self._fallback_delay_seconds is not None
        try:
            await asyncio.sleep(self._fallback_delay_seconds)
            await self._publish_amd_context("%s_timeout" % source, result, event_type)
        except asyncio.CancelledError:
            return

    async def _publish_amd_context(self, source: str, result: str, event_type: str) -> None:
        if self._notice_sent:
            return
        self._notice_sent = True
        self._record.voicemail_prompt_ready = True
        self._record.voicemail_detection_source = self._record.voicemail_detection_source or "amd"
        current_task = asyncio.current_task()
        if (
            self._fallback_task is not None
            and self._fallback_task is not current_task
            and not self._fallback_task.done()
        ):
            self._fallback_task.cancel()

        if not PIPECAT_AVAILABLE:
            return

        if self._on_voicemail_ready is not None:
            await self._on_voicemail_ready(source, result, event_type)
            return

        opening_or_disclosure_already_spoken = bool(
            getattr(self._record, "disclosure_spoken", False)
            or any(item.get("role") == "viola" for item in getattr(self._record, "transcript", []) or [])
        )
        notice_lines = [
            "phone_event: answering_machine_detection",
            "source: telnyx_amd",
            "event_type: %s" % str(event_type or ""),
            "result: %s" % str(result or ""),
            "call_state: carrier_reported_voicemail",
            "viola_opening_or_disclosure_already_spoken: %s" % str(opening_or_disclosure_already_spoken).lower(),
        ]
        notice = "\n".join(notice_lines)
        await self._llm.push_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "system", "content": notice}],
                run_llm=True,
            )
        )
        logger.info(
            "Call %s: raw AMD context published via %s (%s)",
            self._record.call_id,
            source,
            result[:80],
        )

    async def cleanup(self) -> None:
        if self._fallback_task is not None and not self._fallback_task.done():
            self._fallback_task.cancel()
            try:
                await self._fallback_task
            except asyncio.CancelledError:
                pass


class PipecatVoicemailDetectionHandler:
    """Compose Pipecat voicemail classification with Telnyx AMD webhooks."""

    def __init__(
        self,
        *,
        llm: Any,
        classifier_llm: Any,
        call_record: CallRecord,
        amd_fallback_delay_seconds: float | None = 8.0,
    ) -> None:
        if not PIPECAT_AVAILABLE or VoicemailDetector is None or LLMMessagesAppendFrame is None:
            raise RuntimeError("Pipecat voicemail detection requires pipecat.extensions.voicemail")

        self._llm = llm
        self._classifier_llm = classifier_llm
        self._record = call_record
        self._notice_sent = False
        self._context_frame_target: Any | None = None
        self._classification_decided = False
        self._response_gate = _VoicemailResponseGate(call_record, self)
        # One-shot voicemail classification (2026-06-27): the opening greeting is
        # classified ONCE, synchronously, when the recipient's first turn completes
        # (telephony/answer_settle, Smart-Turn end-of-turn) -- not by a persistent
        # parallel pipeline. The persistent pipecat VoicemailDetector's second user
        # aggregator held EVERY turn ~5.6s (the latency root cause,
        # _diag/2026-06-26/phone_latency_root_cause.md). The accuracy lives in the
        # conservative prompt + fail-safe-to-human, not in the pipeline's persistence,
        # so collapsing to one-shot keeps accuracy and removes the per-turn drag.
        # Telnyx AMD stays as the separate late-machine catch.
        self._amd_handler = VoicemailDetectionHandler(
            llm=llm,
            call_record=call_record,
            fallback_delay_seconds=amd_fallback_delay_seconds,
            on_voicemail_ready=self._on_amd_voicemail_ready,
        )

    def set_context_frame_target(self, target: Any) -> None:
        self._context_frame_target = target

    def response_gate(self) -> Any:
        return self._response_gate

    async def classify_opening(self, greeting_text: str) -> str:
        """Classify the recipient's opening greeting once, then apply the decision.

        Called when answer_settle reports the recipient's first turn complete, so the
        full greeting is in hand. Fails SAFE toward a live human: empty greeting or any
        classifier error -> CONVERSATION (talk to the person; never risk leaving a
        message to a human). Idempotent -- only the first decision counts.
        """
        if self._classification_decided:
            return "CONVERSATION" if not self._record.voicemail_detected else "VOICEMAIL"

        greeting = (greeting_text or "").strip()
        classification = "CONVERSATION"
        if greeting:
            try:
                from pipecat.processors.aggregators.llm_context import LLMContext

                raw = await self._classifier_llm.run_inference(
                    LLMContext(messages=[{"role": "user", "content": greeting}]),
                    system_instruction=CONSERVATIVE_VOICEMAIL_CLASSIFIER_PROMPT,
                    max_tokens=16,
                )
                if "VOICEMAIL" in str(raw or "").upper():
                    classification = "VOICEMAIL"
            except (RuntimeError, ValueError, TypeError, OSError) as exc:
                logger.warning(
                    "Call %s: one-shot voicemail classify failed; failing safe to human: %s",
                    self._record.call_id,
                    exc,
                )
        await self._apply_decision(classification)
        return classification

    async def _apply_decision(self, classification: str) -> None:
        """Apply a terminal human/voicemail decision exactly once."""
        if self._classification_decided:
            return
        self._classification_decided = True
        if "VOICEMAIL" in str(classification or "").upper():
            await self._on_pipecat_voicemail_detected()
        else:
            await self._on_pipecat_conversation_detected()

    async def on_transcription(self, text: str, timestamp: Any = None) -> bool:
        return await self._amd_handler.on_transcription(text, timestamp)

    async def on_amd_result(self, result: str, event_type: str = "") -> bool:
        return bool(await self._amd_handler.on_amd_result(result, event_type))

    async def on_outbound_opening_queued(self, reason: str) -> None:
        """Release Pipecat's TTS gate for silent-answer openings."""

        if reason != "silent_answer":
            return
        if self._record.voicemail_detected or self._record.voicemail_prompt_ready:
            return
        # A silent pickup means no greeting to classify -> treat as a live human and
        # release the buffered opening (fail-safe-to-human).
        await self._apply_decision("CONVERSATION")

    async def cleanup(self) -> None:
        await self._amd_handler.cleanup()

    async def note_voicemail_message_delivered(self) -> None:
        """Viola's one voicemail message completed and reached TTS.

        Called by the assistant transcript collector, which is the only place a
        generation is seen to finish with surviving text. Resolves whatever the gate
        is holding behind the in-flight message: nothing more may be generated.
        """
        await self._response_gate.note_generation_delivered()

    async def note_voicemail_generation_undelivered(self) -> None:
        """The in-flight voicemail generation ended having spoken nothing.

        Called by the assistant transcript collector when an interruption cancelled
        the generation before any text survived (the 857c52b2 shape). Releases what
        the gate is holding so Viola's one message still gets out.
        """
        await self._response_gate.note_generation_undelivered()

    async def _on_amd_voicemail_ready(self, source: str, result: str, event_type: str) -> None:
        # Telnyx carrier AMD is the separate LATE machine-catch: it can flip to
        # voicemail at any point in the call, including after the one-shot opening
        # decision said human. _apply_decision is idempotent on the first call, so a
        # genuine late AMD machine signal still marks voicemail here.
        del source, result, event_type
        if self._classification_decided and not self._record.voicemail_detected:
            # Opening already classified human; a later carrier machine signal is a
            # real late-catch -> mark voicemail directly (bypass the first-decision latch).
            await self._on_pipecat_voicemail_detected()
            return
        await self._apply_decision("VOICEMAIL")

    async def _on_pipecat_conversation_detected(self, *_args: Any) -> None:
        self._classification_decided = True
        await self._response_gate.release_buffered()
        logger.info(
            "Call %s: Pipecat voicemail detector classified live conversation",
            self._record.call_id,
        )

    async def _on_pipecat_voicemail_detected(self, *_args: Any) -> None:
        self._mark_detected("pipecat_llm", "voicemail")
        # Drop Viola's buffered normal opening: she leaves a voicemail message instead.
        await self._response_gate.drop_buffered()
        await self._publish_voicemail_context()

    def _mark_detected(self, source: str, evidence: str) -> None:
        if not self._record.voicemail_detected:
            self._record.voicemail_detected_at = datetime.now(tz=UTC)
            self._record.voicemail_detection_source = source
            self._record.outcome = self._record.outcome or "Voicemail detected via %s" % source
            logger.info(
                "Call %s: voicemail detected via %s (%s)",
                self._record.call_id,
                source,
                evidence[:80],
            )
        self._record.voicemail_detected = True
        self._record.voicemail_prompt_ready = True

    async def _publish_voicemail_context(self) -> None:
        if self._notice_sent:
            return
        self._notice_sent = True
        opening_or_disclosure_already_spoken = bool(
            getattr(self._record, "disclosure_spoken", False)
            or any(item.get("role") == "viola" for item in getattr(self._record, "transcript", []) or [])
        )
        notice_lines = [
            "phone_event: voicemail_detection",
            "source: %s" % (self._record.voicemail_detection_source or "pipecat_llm"),
            "call_state: voicemail_recording",
            "voicemail_prompt_ready: true",
            "viola_opening_or_disclosure_already_spoken: %s" % str(opening_or_disclosure_already_spoken).lower(),
            "Leave exactly one coherent voicemail message now, then finish the call in the same assistant response.",
            "Later carrier prompts, beeps, and trailing noise are part of this same recording; do not start another spoken response for them.",
        ]
        await self._push_context_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "system", "content": "\n".join(notice_lines)}],
                run_llm=True,
            )
        )
        logger.info(
            "Call %s: Pipecat voicemail context published (%s)",
            self._record.call_id,
            self._record.voicemail_detection_source or "pipecat_llm",
        )

    async def _push_context_frame(self, frame: Any) -> None:
        if self._context_frame_target is not None and FrameDirection is not None:
            await self._context_frame_target.process_frame(frame, FrameDirection.DOWNSTREAM)
            return
        await self._llm.push_frame(frame)


class _VoicemailResponseGate(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Drop ordinary main-LLM turns after voicemail classification."""

    def __init__(self, call_record: CallRecord, owner: PipecatVoicemailDetectionHandler) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="voicemail_response_gate")
        self._record = call_record
        self._owner = owner
        self._buffered_context_frames: list[tuple[Any, Any]] = []
        # ONE-MESSAGE-AT-A-TIME, decided at admit time (#2587/#4796). True from the
        # moment a voicemail context is handed to the LLM until that generation
        # resolves -- delivered (note_generation_delivered) or cancelled having
        # spoken nothing (note_generation_undelivered). `voicemail_message_delivered`
        # alone cannot carry this: it only flips once the generation has finished
        # AND its text has travelled to the transcript collector, and a recording's
        # next prompt reaches this gate before that. Admitting on "not delivered
        # yet" therefore admits a SECOND generation of the same message.
        self._voicemail_generation_in_flight = False
        # A voicemail context that arrived while a generation was in flight. Held,
        # not dropped, because whether it is a repeat or the re-fire that rescues an
        # interrupted message is not knowable until the in-flight one resolves.
        # Single slot: a recording emits several prompts and only the most recent
        # context is worth re-firing.
        self._held_voicemail_frame: tuple[Any, Any] | None = None

    async def note_generation_delivered(self) -> None:
        """The in-flight voicemail generation finished with text that reached TTS."""
        self._voicemail_generation_in_flight = False
        if self._held_voicemail_frame is not None:
            self._held_voicemail_frame = None
            logger.info(
                "Call %s: dropped the recording prompt held behind Viola's delivered " "voicemail message (no repeat)",
                self._record.call_id,
            )

    async def note_generation_undelivered(self) -> None:
        """The in-flight voicemail generation was cancelled having spoken nothing."""
        self._voicemail_generation_in_flight = False
        held = self._held_voicemail_frame
        self._held_voicemail_frame = None
        if held is None:
            return
        logger.info(
            "Call %s: releasing the held voicemail context -- the interrupted "
            "generation delivered nothing, so Viola's one message still has to go out",
            self._record.call_id,
        )
        self._voicemail_generation_in_flight = True
        await self.push_frame(*held)

    async def release_buffered(self) -> None:
        buffered = self._buffered_context_frames
        self._buffered_context_frames = []
        for frame, direction in buffered:
            await self.push_frame(frame, direction)

    async def drop_buffered(self) -> None:
        if self._buffered_context_frames:
            logger.info(
                "Call %s: dropped %d buffered ordinary LLM context frame(s) after voicemail classification",
                self._record.call_id,
                len(self._buffered_context_frames),
            )
        self._buffered_context_frames = []

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not PIPECAT_AVAILABLE:
            return
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and LLMContextFrame is not None
            and isinstance(frame, LLMContextFrame)
            and bool(getattr(self._record, "voicemail_detected", False))
        ):
            # Delivery-aware accounting (replaces the brittle one-frame
            # _voicemail_context_frames_to_allow count, which dropped the
            # natural interrupt-then-refire and left dead air -- closing call
            # 857c52b2, 2026-06-27). A voicemail recording emits SEVERAL prompts
            # (greeting + "leave a message" + beep + trailing noise); each can
            # fire a fresh LLMContextFrame here. The rule:
            #   - AFTER delivery (record.voicemail_message_delivered True), DROP
            #     every further context frame so later recording prompts never
            #     trigger a repeat message.
            #   - While a message generation is IN FLIGHT, HOLD the context rather
            #     than deciding now: whether it is a repeat or the re-fire that
            #     rescues an interrupted message is not knowable yet, and the
            #     delivery flag cannot answer it — it only flips once the
            #     generation has finished AND its text has travelled to the
            #     transcript collector, which is later than the recording's next
            #     prompt arrives here. Admitting on "not delivered yet" is what
            #     let ONE recipient utterance drive TWO generations of the same
            #     message (#4796). The held frame is resolved by
            #     note_generation_delivered (drop it: no repeat) or
            #     note_generation_undelivered (release it: the interrupted
            #     message still has to go out — the 857c52b2 requirement).
            #   - Otherwise ADMIT and latch: exactly one voicemail message
            #     generation is in flight at a time.
            if bool(getattr(self._record, "voicemail_message_delivered", False)):
                logger.info(
                    "Call %s: dropped LLM context after voicemail message already delivered (no repeat)",
                    self._record.call_id,
                )
                return
            if self._voicemail_generation_in_flight:
                self._held_voicemail_frame = (frame, direction)
                logger.info(
                    "Call %s: holding recording-prompt LLM context — a voicemail message "
                    "generation is already in flight",
                    self._record.call_id,
                )
                return
            self._voicemail_generation_in_flight = True
            logger.info(
                "Call %s: allowing voicemail LLM context (message not yet delivered)",
                self._record.call_id,
            )
        elif (
            direction == FrameDirection.DOWNSTREAM
            and LLMContextFrame is not None
            and isinstance(frame, LLMContextFrame)
            and not bool(getattr(self._owner, "_classification_decided", False))
        ):
            self._buffered_context_frames = [(frame, direction)]
            logger.debug(
                "Call %s: held ordinary LLM context until voicemail classifier decides",
                self._record.call_id,
            )
            return
        await self.push_frame(frame, direction)
