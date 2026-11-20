"""Structured voicemail carrier-event bridge for live phone calls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.extensions.voicemail.voicemail_detector import VoicemailDetector
    from pipecat.frames.frames import (
        InterruptionFrame,
        LLMContextFrame,
        LLMMessagesAppendFrame,
        TranscriptionFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False
    LLMMessagesAppendFrame = None  # type: ignore[assignment]
    LLMContextFrame = None  # type: ignore[assignment]
    TranscriptionFrame = None  # type: ignore[assignment]
    InterruptionFrame = None  # type: ignore[assignment]
    VADUserStartedSpeakingFrame = None  # type: ignore[assignment]
    VADUserStoppedSpeakingFrame = None  # type: ignore[assignment]
    FrameDirection = None  # type: ignore[assignment]
    FrameProcessor = object  # type: ignore[assignment,misc]
    VoicemailDetector = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from telephony.call_manager import CallRecord


# A person may be waiting on this answer, so it is bounded. Warm it takes under a second.
_LATE_GREETING_CLASSIFY_SECS = 4.0

# Leaving the voicemail message (build 12, call 79907d19). With fast transcription the
# voicemail verdict lands two seconds into the greeting, so these timings decide when
# she speaks and what happens when nothing gets out. Every one is a structural signal
# (VAD frames, interruption frames, timers); no wording is looked at.
#
# Her message waits until the far end has been quiet this long, so she never starts
# talking over the greeting that is still playing.
VOICEMAIL_QUIET_BEFORE_MESSAGE_SECS = 1.2
# A line that never goes quiet (music, noise) does not hold her message forever.
VOICEMAIL_QUIET_HOLD_CAP_SECS = 25.0
# An interruption cancelled her in-flight message. Its end frame can be thrown away by
# pipecat's interruption queue reset before the transcript collector sees it, so if no
# end frame with text has arrived by then, the generation is treated as undelivered.
VOICEMAIL_INTERRUPTED_UNDELIVERED_SECS = 1.0
# Nothing delivered this long after the far end went quiet: re-fire the message once.
VOICEMAIL_REFIRE_AFTER_QUIET_SECS = 8.0
# Voicemail detected and still no message this long after: hang up and say so.
VOICEMAIL_NO_MESSAGE_HANGUP_SECS = 40.0
_VOICEMAIL_SUPERVISE_POLL_SECS = 0.1

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
        # Set when a person answered her voicemail message. A later carrier signal
        # about the greeting says nothing about who is on the line now.
        self._live_person_confirmed = False
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

    async def reclassify_late_greeting(self, greeting_text: str) -> str:
        """Classify a greeting that arrived after Viola opened as if nobody spoke.

        A pickup called silent has no greeting to classify, so it is treated as a
        live person. When the first words then turn up, they get the check they
        missed. The gate holds Viola's next ordinary turn until the answer is in,
        and a real recording switches the call to leaving one message.
        """
        greeting = (greeting_text or "").strip()
        if not greeting or self._record.voicemail_detected or self._record.voicemail_prompt_ready:
            return ""
        self._classification_decided = False
        try:
            return await asyncio.wait_for(self.classify_opening(greeting), timeout=_LATE_GREETING_CLASSIFY_SECS)
        finally:
            # The gate holds Viola's turns until a decision lands, so no failure of
            # this check may leave it undecided: that would mute her for the call.
            if not self._classification_decided:
                logger.warning("Call %s: late greeting check gave no answer; staying on the call", self._record.call_id)
                await self._apply_decision("CONVERSATION")

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
        if self._live_person_confirmed:
            return False
        return bool(await self._amd_handler.on_amd_result(result, event_type))

    async def on_live_person_answered(self) -> None:
        """A person answered her message: this is a conversation again."""
        self._live_person_confirmed = True
        self._classification_decided = True
        record = self._record
        record.voicemail_detected = False
        record.voicemail_prompt_ready = False
        record.voicemail_message_delivered = False
        record.voicemail_answered_by_person = True
        record.voicemail_detected_at = None
        record.voicemail_detection_source = ""
        record.outcome = ""
        record._end_call_committed = False
        record._voicemail_auto_hangup_queued = False
        record._pending_end_call = None
        record.voicemail_no_message_left = False
        self._response_gate.reopen_for_conversation()

    def recording_talking_over_message(self) -> bool:
        """True when far-end speech right now is the recording talking over her message.

        Her voicemail message is in progress or already delivered (its generation is
        in flight, it was delivered to speech synthesis, or she is speaking in
        voicemail mode) and the newest speech did not begin after her message did.
        Such speech must not interrupt her: a recording does not take turns. Speech
        that began after her message began is a person answering
        (``VoicemailAnswerWatch``), and that still interrupts and reopens the call.

        "Delivered" is covered on its own, not only "speaking". On the three voicemail
        proof calls (c33057ff, e9b92727, bc5bf732) about 0.9 s passed between the
        message being delivered and her first audio, while speech was synthesized;
        and the phone TTS closes its audio context after 0.5 s without audio, which
        sends BotStoppedSpeaking between two sentences of the same message. In both
        gaps she is neither in flight nor speaking, and a recording transcript there
        used to start a turn and cut her off.
        """
        record = self._record
        if not bool(getattr(record, "voicemail_detected", False)) or bool(
            getattr(record, "voicemail_answered_by_person", False)
        ):
            return False
        answer_watch = getattr(record, "_voicemail_answer_watch", None)
        if answer_watch is not None and answer_watch.latest_speech_answers_message():
            return False
        if bool(getattr(record, "voicemail_message_delivered", False)):
            return True
        bot_speaking = bool(getattr(answer_watch, "_bot_speaking", False))
        return bool(self._response_gate.message_in_flight or bot_speaking)

    async def give_up_without_message(self) -> None:
        """No message got out within the limit: hang up and record that none was left."""
        from telephony.call_tools import hang_up_voicemail_without_message

        llm_service = getattr(self._record, "_llm_service", None)
        push_frame = getattr(llm_service, "push_frame", None)
        if not callable(push_frame):
            push_frame = self._response_gate.push_frame
        await hang_up_voicemail_without_message(self._record, push_frame=push_frame)

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
        await self._response_gate.stop_voicemail_supervision()

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
        answer_watch = getattr(self._record, "_voicemail_answer_watch", None)
        if answer_watch is not None:
            answer_watch.note_voicemail_mode()
        # Drop Viola's buffered normal opening: she leaves a voicemail message instead.
        await self._response_gate.drop_buffered()
        # From here the gate owns the message's timing: it waits for the line to go
        # quiet, re-fires an interrupted message, and gives up after the limit.
        self._response_gate.start_voicemail_supervision()
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
        # context is worth re-firing. It also holds her message while the far end
        # is still talking (VOICEMAIL_QUIET_BEFORE_MESSAGE_SECS).
        self._held_voicemail_frame: tuple[Any, Any] | None = None
        # The newest voicemail context she was given, kept so a message whose
        # generation vanished (end frame lost) can be fired again.
        self._last_voicemail_frame: tuple[Any, Any] | None = None
        # Far-end speech, from the VAD frames the user aggregator sends downstream.
        self._far_end_speaking = False
        self._far_end_active_at: float | None = None
        self._hold_started_at: float | None = None
        self._in_flight_since: float | None = None
        # When an interruption passed this gate while her message was in flight.
        self._interrupted_at: float | None = None
        self._refired = False
        self._voicemail_mode_since: float | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._reopened = False

    @property
    def message_in_flight(self) -> bool:
        return self._voicemail_generation_in_flight

    def reopen_for_conversation(self) -> None:
        """Forget the message accounting: ordinary turns pass from here on."""
        self._reopened = True
        self._voicemail_generation_in_flight = False
        self._held_voicemail_frame = None
        self._last_voicemail_frame = None
        self._interrupted_at = None
        self._buffered_context_frames = []
        supervisor = self._supervisor
        self._supervisor = None
        if supervisor is not None and not supervisor.done() and supervisor is not asyncio.current_task():
            supervisor.cancel()

    async def note_generation_delivered(self) -> None:
        """The in-flight voicemail generation finished with text that reached TTS."""
        self._voicemail_generation_in_flight = False
        self._interrupted_at = None
        if self._held_voicemail_frame is not None:
            self._held_voicemail_frame = None
            logger.info(
                "Call %s: dropped the recording prompt held behind Viola's delivered " "voicemail message (no repeat)",
                self._record.call_id,
            )

    async def note_generation_undelivered(self) -> None:
        """The in-flight voicemail generation was cancelled having spoken nothing."""
        if not self._voicemail_generation_in_flight and self._held_voicemail_frame is None:
            return
        self._mark_undelivered()
        logger.info(
            "Call %s: the interrupted voicemail generation delivered nothing, so Viola's "
            "one message still has to go out",
            self._record.call_id,
        )
        await self._release_held_if_quiet()

    # -- voicemail message timing (build 12) ------------------------------------

    def start_voicemail_supervision(self) -> None:
        """Voicemail was detected: watch the message until it is delivered or given up."""
        self._reopened = False
        if self._voicemail_mode_since is None:
            self._voicemail_mode_since = time.monotonic()
        if self._supervisor is not None and not self._supervisor.done():
            return
        try:
            self._supervisor = asyncio.get_running_loop().create_task(
                self._supervise_voicemail_message(),
                name="voicemail-message-%s" % getattr(self._record, "call_id", ""),
            )
        except RuntimeError:
            self._supervisor = None

    async def stop_voicemail_supervision(self) -> None:
        supervisor = self._supervisor
        self._supervisor = None
        if supervisor is None or supervisor.done() or supervisor is asyncio.current_task():
            return
        supervisor.cancel()
        try:
            await supervisor
        except asyncio.CancelledError:
            pass

    async def cleanup(self) -> None:
        await self.stop_voicemail_supervision()
        if PIPECAT_AVAILABLE:
            await super().cleanup()

    def _far_end_quiet_for(self, seconds: float, now: float | None = None) -> bool:
        """The far end has not been heard for ``seconds``. No VAD frame yet means quiet."""
        if self._far_end_speaking:
            return False
        active_at = self._far_end_active_at
        if active_at is None:
            return True
        return (time.monotonic() if now is None else now) - active_at >= seconds

    def _mark_undelivered(self) -> None:
        """Her in-flight message got nothing out: free the latch, keep the message due."""
        self._voicemail_generation_in_flight = False
        self._interrupted_at = None
        self._in_flight_since = None
        if self._held_voicemail_frame is None and self._last_voicemail_frame is not None:
            self._held_voicemail_frame = self._fresh_copy(self._last_voicemail_frame)
        if self._held_voicemail_frame is not None and self._hold_started_at is None:
            self._hold_started_at = time.monotonic()

    @staticmethod
    def _fresh_copy(held: tuple[Any, Any]) -> tuple[Any, Any]:
        frame, direction = held
        context = getattr(frame, "context", None)
        if LLMContextFrame is not None and context is not None:
            return LLMContextFrame(context=context), direction
        return frame, direction

    async def _admit_voicemail_frame(self, frame: Any, direction: Any) -> None:
        self._voicemail_generation_in_flight = True
        self._in_flight_since = time.monotonic()
        self._interrupted_at = None
        self._hold_started_at = None
        self._last_voicemail_frame = (frame, direction)
        await self.push_frame(frame, direction)

    async def _release_held_if_quiet(self, *, now: float | None = None) -> bool:
        held = self._held_voicemail_frame
        if held is None or self._voicemail_generation_in_flight:
            return False
        if bool(getattr(self._record, "voicemail_message_delivered", False)):
            self._held_voicemail_frame = None
            return False
        now = time.monotonic() if now is None else now
        quiet = self._far_end_quiet_for(VOICEMAIL_QUIET_BEFORE_MESSAGE_SECS, now)
        capped = self._hold_started_at is not None and now - self._hold_started_at >= VOICEMAIL_QUIET_HOLD_CAP_SECS
        if not (quiet or capped):
            return False
        self._held_voicemail_frame = None
        logger.info(
            "Call %s: releasing the held voicemail context: %s",
            self._record.call_id,
            "the line has been quiet for %.1fs" % VOICEMAIL_QUIET_BEFORE_MESSAGE_SECS
            if quiet
            else "the line never went quiet within %.0fs" % VOICEMAIL_QUIET_HOLD_CAP_SECS,
        )
        await self._admit_voicemail_frame(*held)
        return True

    def _voicemail_message_settled(self) -> bool:
        from telephony.call_manager import _is_terminal_call_status

        record = self._record
        return bool(
            self._reopened
            or not bool(getattr(record, "voicemail_detected", False))
            or bool(getattr(record, "voicemail_message_delivered", False))
            or bool(getattr(record, "voicemail_answered_by_person", False))
            or bool(getattr(record, "_end_call_committed", False))
            or bool(getattr(record, "_telnyx_hangup_dispatched", False))
            or _is_terminal_call_status(getattr(record, "status", None))
        )

    async def _supervise_voicemail_message(self) -> None:
        """Make sure her one message gets out, or the call ends saying it did not.

        The latch used to be cleared only by the transcript collector seeing the
        generation's end frame. On call 79907d19 an interruption threw that frame
        away and every later prompt waited behind a generation that no longer
        existed, until the voicemail system hung up at 180 s.
        """
        try:
            while True:
                await asyncio.sleep(_VOICEMAIL_SUPERVISE_POLL_SECS)
                if self._voicemail_message_settled():
                    return
                now = time.monotonic()
                if (
                    self._voicemail_generation_in_flight
                    and self._interrupted_at is not None
                    and now - self._interrupted_at >= VOICEMAIL_INTERRUPTED_UNDELIVERED_SECS
                ):
                    logger.info(
                        "Call %s: voicemail message was interrupted and no end frame with text "
                        "followed within %.1fs; treating it as undelivered",
                        self._record.call_id,
                        VOICEMAIL_INTERRUPTED_UNDELIVERED_SECS,
                    )
                    self._mark_undelivered()
                stale_in_flight = (
                    self._voicemail_generation_in_flight
                    and self._in_flight_since is not None
                    and now - self._in_flight_since >= VOICEMAIL_REFIRE_AFTER_QUIET_SECS
                )
                nothing_due = not self._voicemail_generation_in_flight and self._held_voicemail_frame is None
                if (
                    not self._refired
                    and self._last_voicemail_frame is not None
                    and (stale_in_flight or nothing_due)
                    and self._far_end_quiet_for(VOICEMAIL_REFIRE_AFTER_QUIET_SECS, now)
                ):
                    self._refired = True
                    logger.info(
                        "Call %s: re-firing Viola's voicemail message once: nothing delivered " "%.0fs after the line went quiet",
                        self._record.call_id,
                        VOICEMAIL_REFIRE_AFTER_QUIET_SECS,
                    )
                    self._voicemail_generation_in_flight = False
                    self._held_voicemail_frame = self._fresh_copy(self._last_voicemail_frame)
                    self._hold_started_at = now
                await self._release_held_if_quiet(now=now)
                since = self._voicemail_mode_since
                recent_generation = (
                    self._voicemail_generation_in_flight
                    and self._in_flight_since is not None
                    and now - self._in_flight_since < VOICEMAIL_REFIRE_AFTER_QUIET_SECS
                )
                if since is not None and now - since >= VOICEMAIL_NO_MESSAGE_HANGUP_SECS and not recent_generation:
                    logger.warning(
                        "Call %s: no voicemail message delivered %.0fs after voicemail was detected; "
                        "hanging up, no message was left",
                        self._record.call_id,
                        VOICEMAIL_NO_MESSAGE_HANGUP_SECS,
                    )
                    self._held_voicemail_frame = None
                    await self._owner.give_up_without_message()
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Call %s: voicemail message supervisor failed", getattr(self._record, "call_id", ""))

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
        if VADUserStartedSpeakingFrame is not None and isinstance(frame, VADUserStartedSpeakingFrame):
            self._far_end_speaking = True
            self._far_end_active_at = time.monotonic()
        elif VADUserStoppedSpeakingFrame is not None and isinstance(frame, VADUserStoppedSpeakingFrame):
            self._far_end_speaking = False
            self._far_end_active_at = time.monotonic()
        elif (
            InterruptionFrame is not None
            and isinstance(frame, InterruptionFrame)
            and self._voicemail_generation_in_flight
            and self._interrupted_at is None
            and not bool(getattr(self._record, "voicemail_message_delivered", False))
        ):
            # The interruption cancels her in-flight message. Whether anything of it
            # survived is the transcript collector's call, but its end frame can be
            # dropped on the way; the supervisor resolves it after a short wait.
            self._interrupted_at = time.monotonic()
        # The turn's context can get here before the turn-started event has run, so
        # settle who is on the line first: a person's turn must never be dropped.
        answer_watch = getattr(self._record, "_voicemail_answer_watch", None)
        if (
            answer_watch is not None
            and direction == FrameDirection.DOWNSTREAM
            and LLMContextFrame is not None
            and isinstance(frame, LLMContextFrame)
        ):
            await answer_watch.on_recipient_turn_started()
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
            self.start_voicemail_supervision()
            if self._voicemail_generation_in_flight:
                self._held_voicemail_frame = (frame, direction)
                logger.info(
                    "Call %s: holding recording-prompt LLM context — a voicemail message "
                    "generation is already in flight",
                    self._record.call_id,
                )
                return
            if not self._far_end_quiet_for(VOICEMAIL_QUIET_BEFORE_MESSAGE_SECS):
                # The greeting is still playing. Starting now means talking over it,
                # and its next words would cut her off (call 79907d19).
                self._held_voicemail_frame = (frame, direction)
                if self._hold_started_at is None:
                    self._hold_started_at = time.monotonic()
                logger.info(
                    "Call %s: holding Viola's voicemail message until the line has been quiet for %.1fs",
                    self._record.call_id,
                    VOICEMAIL_QUIET_BEFORE_MESSAGE_SECS,
                )
                return
            self._held_voicemail_frame = None
            logger.info(
                "Call %s: allowing voicemail LLM context (message not yet delivered)",
                self._record.call_id,
            )
            await self._admit_voicemail_frame(frame, direction)
            return
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
