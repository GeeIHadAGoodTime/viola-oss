"""Answer-settle observer — lets Viola listen before her outbound opening.

Sits right after STT in the outbound phone pipeline, in ``CallManager`` and in the
loopback rig alike (both restored to that side on 2026-08-06, #4796, closing the
#2587 defect; the note beside ``CallManager``'s pipeline assembly records what the
wrong side cost and why the move needed the voicemail one-message fix with it).
That position is load-bearing, not cosmetic: pipecat STT services push
``TranscriptionFrame`` downstream only, so from immediately BEFORE ``stt`` this
observer could never see the recipient's *text*, and everything below that depends
on it was inert. When the recipient answers and speaks their own greeting ("Thanks
for calling Tony's Pizza"), this observer notices the recipient is talking and
signals when they have *semantically* finished, so the static outbound greeting is
queued AFTER their opening instead of on top of it.

Why this exists (2026-06-18 capstone call 27c3f98a): the static greeting was
queued the instant the media stream connected, so Viola spoke directly over the
recipient's "thanks for calling Tony's pizza." A considerate caller waits for
the person who answered to finish their hello first.

How it knows the recipient finished — native end-of-turn, NOT a fixed timer.
The pipeline's user aggregator runs Pipecat's Smart-Turn analyzer
(``LocalSmartTurnAnalyzerV3`` via ``TurnAnalyzerUserTurnStopStrategy`` in
call_manager.py). Smart-Turn is a small model that decides whether the speaker
has *semantically* finished their utterance, so a mid-sentence pause ("thanks
for calling Tony's pizza … <breath> … how can I help you?") does NOT count as
the end of their turn. When Smart-Turn (plus VAD silence) concludes the turn is
over, the aggregator broadcasts a ``UserStoppedSpeakingFrame`` both downstream
and upstream; the upstream copy reaches this observer. We simply *await that
frame* rather than guessing with a clock — this is the highest-UX signal the
installed Pipecat (0.0.107) exposes.

Pipecat 0.0.107 has no ``respond_immediately`` flag (that is a newer-version
transport API), so "wait for the remote party first" is implemented here: hold
the greeting until the recipient's first turn completes. The only timer is a
single fallback for a *genuinely silent* pickup — if the recipient never starts
speaking at all, we fall through after ``silent_answer_fallback_seconds`` so the
call is not stalled. Crucially, once they HAVE started, there is no window that
can cut them off mid-greeting: we follow Smart-Turn to the real end of their
turn.
"""

from __future__ import annotations

import asyncio

from core.logging_config import get_logger

logger = get_logger(__name__)

# After the silent-answer timer elapses, confirm silence over this short extra
# window before Viola commits to leading. It closes the TOCTOU race where the
# recipient's UserStartedSpeaking lands a beat after the timeout resolves (a human
# who answered right at the boundary): without it, the fallback opening and the
# recipient's completing-turn opening both fire → choppy/restarted speech
# (capstone f6875e9b). Kept small so a genuinely silent pickup is barely delayed.
_SILENT_CONFIRM_GRACE_SECS = 1.5

# Bounded wait, after the recipient's turn ends, for the turn's finalized
# transcription to be observed when it trails the end-of-turn signal (the
# loopback rig's frame order; at the observer's DESIGNED position, after STT,
# the transcription precedes it because the user aggregator only concludes the
# turn after aggregating it).
#
# This is the LAST thing standing between the recipient finishing their greeting
# and Viola's first spoken word, so its ceiling is dead air the caller hears
# (#2587). It used to be spent in full on EVERY call, because the observer was
# wired before `stt` and no TranscriptionFrame could reach it from up there; that
# alone was why 2.0s of guaranteed silence bought nothing. The observer now sits
# after STT (#4796), so the transcription is normally already in hand when the turn
# ends and this costs 0.000s.
#
# A long ceiling would still be wrong from the designed position. It now only
# elapses in full when no transcription is coming at all -- a greeting
# faster-whisper drops entirely under its own vad_filter / no_speech_threshold /
# log_prob guards, which is exactly what happened on the 2026-07-25 cloud calls
# f7a3b9cb and fb6755ce (an STT pass ran, no TranscriptionFrame followed).
# Waiting cannot conjure text that was never produced, so a long ceiling costs
# the caller silence on the one turn they judge the product on and returns
# nothing. Sized instead for what it actually covers: a transcription trailing
# the turn-end signal by a processing hop, which lands in tens of milliseconds.
# A missed capture is still not a blind spot for the voicemail classifier --
# call_manager's classify_opening call site falls back to scraping
# record.transcript, which a downstream observer fills independently.
_OPENING_TRANSCRIPTION_GRACE_SECS = 0.4

# Absolute cap on Phase 2 (see wait_for_recipient_opening below): once the
# recipient has started speaking, we normally follow their turn to Smart-Turn's
# semantic end with no timer at all. But if that end-of-turn signal never
# arrives — continuous hold music or background noise keeping VAD "active"
# forever, or a Telnyx server-side teardown that tears the media stream down
# without ever emitting UserStoppedSpeaking — the uncapped await wedges
# _run_call at this line permanently: its `finally` never runs, so the call
# task, acquired phone number, billing heartbeat, and disconnect watchdog all
# leak (issue #2797). This ceiling is deliberately generous — far longer than
# any plausible business greeting — so it never cuts a real, if long-winded,
# recipient off; it only bounds the pathological never-ending case.
_RECIPIENT_TURN_MAX_WAIT_SECS = 45.0

try:
    from pipecat.frames.frames import (
        TranscriptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without pipecat installed
    PIPECAT_AVAILABLE = False
    TranscriptionFrame = None  # type: ignore[assignment]


class AnswerSettleObserver(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Tracks the recipient's opening speech so the greeting waits its turn."""

    def __init__(self) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__()
        self._recipient_started = False
        # Set when the recipient has begun speaking (first UserStartedSpeaking).
        # Latches True for the life of the call — it answers "did anyone ever
        # speak?", which is what the silent-answer fallback turns on.
        self._recipient_started_event = asyncio.Event()
        # Set on each UserStoppedSpeaking (semantic end-of-turn from Smart-Turn)
        # and cleared on each UserStartedSpeaking, so awaiting it tracks the
        # *current* turn rather than a stale earlier one.
        self._recipient_finished = asyncio.Event()
        # Accumulated finalized recipient transcription for the opening turn. The
        # observer sits right after STT (#4796), so it sees the recipient's
        # TranscriptionFrame and the turn-end signal at one point. Capturing the
        # greeting here (rather than reading record.transcript, which a
        # downstream TranscriptionObserver populates asynchronously) lets the
        # one-shot voicemail classifier read the greeting deterministically.
        self._recipient_opening_chunks: list[str] = []
        # Set when a finalized TranscriptionFrame for the current turn has been
        # observed; cleared on each UserStartedSpeaking. In production the
        # transcription reaches the observer (flowing downstream) BEFORE the
        # turn-end UserStoppedSpeaking (broadcast upstream from the aggregator,
        # which only concludes the turn after aggregating the transcription), so
        # this is already set when the turn finishes and adds no delay. It exists
        # so wait_for_recipient_opening can briefly wait for a transcription that
        # trails the end-of-turn signal, keeping recipient_opening_text populated
        # for the classifier without depending on frame-emission order.
        self._recipient_transcription_event = asyncio.Event()
        # Set when Phase 2's wait for the recipient's semantic end-of-turn hits
        # the generous _RECIPIENT_TURN_MAX_WAIT_SECS cap instead of observing a
        # real UserStoppedSpeaking. Lets the caller (queue_outbound_opening_after_
        # answer_settle) tell "genuinely about to finish organically" apart from
        # "capped — this turn will never finish, proceed anyway" (issue #2797).
        self._recipient_turn_capped = False

    @property
    def recipient_started_speaking(self) -> bool:
        return self._recipient_started

    @property
    def recipient_turn_capped(self) -> bool:
        """True if Phase 2 hit its absolute cap without a semantic end-of-turn."""
        return self._recipient_turn_capped

    @property
    def recipient_opening_text(self) -> str:
        """The recipient's accumulated opening-turn transcription, race-free."""
        return " ".join(chunk for chunk in self._recipient_opening_chunks if chunk).strip()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if PIPECAT_AVAILABLE:
            if isinstance(frame, UserStartedSpeakingFrame):
                self._recipient_started = True
                self._recipient_started_event.set()
                self._recipient_finished.clear()
                self._recipient_transcription_event.clear()
            elif TranscriptionFrame is not None and isinstance(frame, TranscriptionFrame):
                text = str(getattr(frame, "text", "") or "").strip()
                if text:
                    self._recipient_opening_chunks.append(text)
                    self._recipient_transcription_event.set()
            elif isinstance(frame, UserStoppedSpeakingFrame):
                self._recipient_finished.set()

        await self.push_frame(frame, direction)

    async def wait_for_recipient_opening(
        self,
        *,
        silent_answer_fallback_seconds: float,
        silent_confirm_grace_seconds: float = _SILENT_CONFIRM_GRACE_SECS,
        recipient_turn_max_wait_seconds: float = _RECIPIENT_TURN_MAX_WAIT_SECS,
    ) -> bool:
        """Wait for the recipient's opening to complete, then return.

        Behaviour:
          * If the recipient starts speaking, wait for their turn to *finish*
            as decided by the pipeline's Smart-Turn / VAD end-of-turn detector
            (the upstream ``UserStoppedSpeakingFrame``). There is effectively no
            time cap on this phase for a real, if long-winded, greeting — only
            the generous ``recipient_turn_max_wait_seconds`` ceiling below, so a
            longer business greeting or a mid-sentence pause is never cut off —
            the considerate-caller behaviour the recipient actually experiences
            as natural.
          * If the recipient never starts speaking (a silent pickup), fall
            through after ``silent_answer_fallback_seconds`` (plus a short confirm
            grace, below) so a silent answer never stalls the call.
          * If the recipient starts speaking but their turn never semantically
            ends (continuous hold music/noise, or a Telnyx teardown that drops
            the media stream without emitting UserStoppedSpeaking), fall through
            once ``recipient_turn_max_wait_seconds`` elapses instead of waiting
            forever — see ``recipient_turn_capped`` (issue #2797).

        Returns True if we waited out a real recipient opening, False if the
        silent-answer fallback elapsed with no speech OR the Phase-2 cap fired
        without the turn completing (check ``recipient_turn_capped`` to tell
        the two apart).

        A turn that completed before this is awaited (a fast answerer who
        finished their hello during pipeline setup) is handled correctly:
        ``_recipient_started_event`` is already set, so we skip straight to the
        finished check below.

        Double-fire avoidance (capstone f6875e9b): the fallback fires off a clock,
        but the recipient's ``UserStartedSpeaking`` can land a beat AFTER the timer
        resolves — a human who answered right at the fallback boundary. If we
        returned False the instant the timer elapsed, the silent-answer fallback
        would queue an opening AND the recipient's now-completing turn would drive
        the user aggregator's own opening: two overlapping generations the recipient
        hears as choppy, restarted speech. So "genuinely silent" is confirmed over a
        short additional grace window after the timer: if speech starts during it,
        we yield to the real turn (Phase 2) instead of opening over it. A truly
        silent pickup sees no start in that grace and still falls through to lead.
        """
        if not PIPECAT_AVAILABLE:
            return False

        # Phase 1 — wait for the recipient to BEGIN their greeting. A live
        # answerer starts within ~1-2s; a silent pickup never starts, so this
        # is the only place a timer applies, and only to avoid stalling on
        # silence. We do NOT cut the recipient off here — the timer just ends
        # the *waiting-for-them-to-start* state.
        if not self._recipient_started_event.is_set():
            try:
                await asyncio.wait_for(
                    self._recipient_started_event.wait(),
                    timeout=max(0.0, silent_answer_fallback_seconds),
                )
            except TimeoutError:
                # Confirm silence over a short grace before committing to lead, so
                # a recipient whose start raced the timeout is followed, not talked
                # over (the double-fire root cause). No speech in the grace = a
                # genuinely silent pickup, and we fall through to lead.
                grace = max(0.0, silent_confirm_grace_seconds)
                if grace and not self._recipient_started_event.is_set():
                    try:
                        await asyncio.wait_for(self._recipient_started_event.wait(), timeout=grace)
                    except TimeoutError:
                        return False
                if not self._recipient_started_event.is_set():
                    return False

        # Phase 2 — the recipient is (or was) speaking. Follow their turn to the
        # semantic end-of-turn — no *practical* cap, so we never talk over a
        # pause or a longer greeting; Smart-Turn decides when the turn is truly
        # over. But an absolute ceiling still bounds the wait: if the end-of-turn
        # signal never arrives (continuous hold music/noise, or a Telnyx
        # teardown that drops the stream without emitting UserStoppedSpeaking),
        # an unbounded `await` here wedges the caller (_run_call) forever and
        # leaks the call task/number/heartbeats (issue #2797).
        if not self._recipient_finished.is_set():
            try:
                await asyncio.wait_for(
                    self._recipient_finished.wait(),
                    timeout=max(0.0, recipient_turn_max_wait_seconds),
                )
            except TimeoutError:
                self._recipient_turn_capped = True
                logger.warning(
                    "Answer-settle Phase 2 exceeded %.1fs cap without a semantic "
                    "end-of-turn signal; proceeding instead of waiting indefinitely",
                    recipient_turn_max_wait_seconds,
                )
                # Return False (not True) here: the turn never semantically
                # finished, so there is no finalized greeting to wait on below —
                # a capped turn is the opposite of "we waited out a real
                # recipient opening". This also lets the caller
                # (queue_outbound_opening_after_answer_settle) tell it apart
                # from a completed turn via recipient_turn_capped and queue the
                # opening itself instead of deferring to a turn that is never
                # going to finish.
                return False

        # The one-shot voicemail classifier reads the recipient's opening
        # transcription the instant this returns. From the observer's position
        # AFTER STT, the finalized TranscriptionFrame reaches it before the
        # end-of-turn signal, recipient_transcription_event is already set, and
        # this returns immediately; it then only waits when the transcription
        # trails the turn-end signal (the loopback rig emits them in that order)
        # — a bounded wait so the classifier sees the greeting instead of an
        # empty string (which would fail safe to CONVERSATION and miss a real
        # voicemail). A genuinely no-text turn falls through after the same
        # small cap.
        if not self._recipient_transcription_event.is_set():
            try:
                await asyncio.wait_for(
                    self._recipient_transcription_event.wait(),
                    timeout=_OPENING_TRANSCRIPTION_GRACE_SECS,
                )
            except TimeoutError:
                pass
        return True
