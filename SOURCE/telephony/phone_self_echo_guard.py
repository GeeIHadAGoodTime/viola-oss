"""Self-echo guard for the inbound cloud phone leg (capstone 49c7106f).

Problem
-------
The inbound cloud phone leg has NO acoustic echo canceller. Viola's own TTS plays
out of the recipient's handset earpiece/speaker, their handset mic picks it up, and
Telnyx returns it to us on the inbound track. At ``PHONE_VAD_CONFIDENCE=0.3`` that
echoed audio trips Silero VAD; Whisper then transcribes the echo of Viola's own
multi-word opening as ``>= PHONE_INTERRUPTION_MIN_WORDS`` "recipient" words, which
satisfies ``MinWordsUserTurnStartStrategy`` and starts a user turn -- cancelling
Viola's in-flight opening. On capstone 49c7106f the founder heard Viola "breaking
up terribly at the beginning, seemed interrupted while I was silent"; the trace
fingerprint was an ~8.2s STT chunk overlapping her TTS while the human was silent.

Why this is NOT a barge-in, and why the fix preserves real barge-in
-------------------------------------------------------------------
The echoed transcript's words ARE Viola's own words, in her own order. A genuine
interruption ("stop, wait, I have a question") shares almost nothing with what she
is saying. So the fix is a transcript-level echo canceller (the software analog of
acoustic echo cancellation): while Viola is speaking, compare the inbound transcript
against the words she is actually speaking right now. If it is substantially her own
words coming back, drop it exactly like a below-threshold backchannel -- reset
aggregation, do NOT start the turn. Anything that is NOT her own words still clears
the guard and interrupts her normally, so genuine barge-in is unchanged.

The match uses ordered word BIGRAMS, not a bag of words, precisely so common
function words ("I", "a", "the") a real interruption happens to share with Viola do
not accumulate into a false echo verdict: echo reproduces her exact word *pairs*,
a fresh utterance almost never does.

Scope / non-boxing
------------------
This layer decides only one thing: "is this inbound audio the machine's own acoustic
echo?" It does not classify the recipient's intent, parse the model's reply to branch
behaviour, or inject prompt hints. It is echo cancellation at the turn layer -- the
same category as an AEC on the audio -- not model boxing (CLAUDE.md "trust the model").
"""

from __future__ import annotations

import itertools
import logging
import re
import time
from collections import deque
from typing import Any, Callable

from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy

logger = logging.getLogger("viola.telephony.self_echo_guard")

_WORD_RE = re.compile(r"[a-z0-9']+")

# Bot words spoken within this many seconds are candidate echo sources. Telephone
# echo returns within ~1s of playout, but STT adds latency and a long opening keeps
# echoing for seconds, so a generous window keeps the opening's words in scope while
# its tail is still coming back.
_ECHO_WINDOW_SECS = 15.0

# Fraction of the inbound transcript's word-bigrams that must be bigrams Viola just
# spoke for it to count as echo. Tolerant of some STT error on the echoed audio, yet
# far above what a genuine fresh utterance shares with her speech.
_ECHO_OVERLAP_THRESHOLD = 0.5

# Require at least this many matching bigrams so a 3-word echo (2 bigrams) can be
# caught while a 1-2 word coincidence cannot trip the guard. This also aligns with
# the interruption floor: an utterance with fewer than 2 bigrams (< 3 words) cannot
# start a turn while Viola speaks anyway (min_words gate), so it is never echo here.
_MIN_OVERLAP_BIGRAMS = 2

# Safety backstop for the generation-window floor (see BotTurnFloorState). Real TTFB
# (LLM first token -> first audio) is a few seconds and Viola almost always speaks a
# commentary opening well within it, which clears the floor via BotStartedSpeaking.
# If a turn somehow produces NO audio and no new user turn for this long, the floor
# releases so a recipient's genuine short reply can still start a turn -- the flag must
# never lock a caller out. 30s is far above any real pre-audio latency yet well short
# of a perceptible lockout. This is only a backstop; the primary clears are bot
# started/stopped speaking and a new user turn.
_GENERATION_FLOOR_MAX_SECS = 30.0


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _bigrams(tokens: list[str]) -> list[tuple[str, str]]:
    return list(itertools.pairwise(tokens))


class BotSpeechEchoReference:
    """Rolling record of the words Viola has spoken recently, for echo detection.

    Populated from the outbound TTS text stream (see ``create_bot_speech_echo_tap``)
    and consulted by the guarded turn-start strategy. Shared object, single event
    loop -- no locking needed.
    """

    def __init__(
        self,
        *,
        window_secs: float = _ECHO_WINDOW_SECS,
        overlap_threshold: float = _ECHO_OVERLAP_THRESHOLD,
        min_overlap_bigrams: int = _MIN_OVERLAP_BIGRAMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_secs = window_secs
        self._overlap_threshold = overlap_threshold
        self._min_overlap_bigrams = min_overlap_bigrams
        self._clock = clock
        # (timestamp, word) in spoken order, pruned to the window.
        self._recent: deque[tuple[float, str]] = deque()

    def note_bot_text(self, text: str) -> None:
        """Record a chunk of text Viola is speaking (a TTS text frame)."""
        ts = self._clock()
        for word in _tokens(text):
            self._recent.append((ts, word))
        self._prune(ts)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_secs
        recent = self._recent
        while recent and recent[0][0] < cutoff:
            recent.popleft()

    def is_echo(self, text: str) -> bool:
        """True if ``text`` is substantially Viola's own recently-spoken words.

        Ordered-bigram overlap against the recent bot word stream, above threshold
        and above a minimum matched-bigram count. Returns False for anything that is
        not clearly her own speech coming back -- which is what preserves barge-in.
        """
        now = self._clock()
        self._prune(now)
        inbound_bigrams = _bigrams(_tokens(text))
        if len(inbound_bigrams) < self._min_overlap_bigrams:
            return False
        bot_stream = [word for _, word in self._recent]
        bot_bigrams = set(_bigrams(bot_stream))
        if not bot_bigrams:
            return False
        matched = sum(1 for bg in inbound_bigrams if bg in bot_bigrams)
        if matched < self._min_overlap_bigrams:
            return False
        return matched / len(inbound_bigrams) >= self._overlap_threshold


class BotTurnFloorState:
    """Tracks whether Viola holds the conversational floor during her *generation*
    window -- after the recipient's turn is transcribed (STT done) but before her
    audio starts playing (first audio).

    Why this exists
    ---------------
    Pipecat's ``MinWordsUserTurnStartStrategy`` raises the interruption word-count bar
    (``min_words``) ONLY while the bot is *actively speaking* -- i.e. between
    ``BotStartedSpeakingFrame`` and ``BotStoppedSpeakingFrame`` (its ``_bot_speaking``
    flag; min_words_user_turn_start_strategy.py:105 ``min_words = self._min_words if
    self._bot_speaking else 1``). The *generation* window BEFORE first audio is left at
    ``min_words = 1``, so a 2-word backchannel ("can you?") clears the bar and flushes
    Viola's pending turn (call 6a632a0b: two ``turn_latency_incomplete
    reason=new_user_turn`` events while she was mid-generation).

    This object extends "the bot has the floor" to cover the generation window, so the
    SAME word-count gate applies there too. It is set by the user aggregator's
    ``on_user_turn_stopped`` event (the moment the recipient yields and the LLM run
    begins) and cleared when the bot starts/stops speaking or a new user turn starts.

    Non-boxing: this is a floor-ownership flag driven by Pipecat's own turn lifecycle,
    not a query classifier. The model still receives the raw context and decides; a
    genuine multi-word interruption still clears ``min_words`` and cuts Viola off.
    """

    def __init__(
        self,
        *,
        max_window_secs: float = _GENERATION_FLOOR_MAX_SECS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_window_secs = max_window_secs
        self._clock = clock
        self._generating_since: float | None = None

    def note_generation_started(self) -> None:
        """Recipient yielded the floor; the LLM run/generation has begun."""
        self._generating_since = self._clock()

    def note_generation_ended(self) -> None:
        """The bot started/stopped speaking, or a new user turn started."""
        self._generating_since = None

    @property
    def is_generating(self) -> bool:
        """True while Viola owns the floor generating a pending turn (pre-first-audio).

        Self-expires after ``max_window_secs`` as a safety backstop so a turn that
        never produces audio can never permanently lock the recipient out of starting
        a turn.
        """
        since = self._generating_since
        if since is None:
            return False
        if self._clock() - since > self._max_window_secs:
            self._generating_since = None
            return False
        return True


class SelfEchoGuardedMinWordsUserTurnStartStrategy(MinWordsUserTurnStartStrategy):
    """``MinWordsUserTurnStartStrategy`` that ignores Viola's own acoustic echo AND
    applies the word-count interruption gate across her whole turn -- both while she is
    speaking and during the generation window before her first audio.

    Two extensions over the base strategy, each preserving genuine barge-in:

    1. **Self-echo (2026-07-02).** While Viola is speaking, an inbound transcript the
       echo reference recognises as her own words coming back is dropped like a
       below-threshold backchannel (reset aggregation, no turn start) regardless of
       word count. Genuine interruptions share almost no ordered bigrams with her
       speech, so they still cut her off.

    2. **Generation-window floor (call 6a632a0b).** The base strategy's ``_bot_speaking``
       flag is False during generation (STT-done -> first-audio), so it falls back to
       ``min_words = 1`` and a 2-word backchannel flushes her pending turn. Here the
       effective "bot has the floor" state also covers generation (via the shared
       ``BotTurnFloorState``), so the same ``min_words`` gate rejects sub-threshold
       backchannels in that window. A >= min_words interruption still clears the gate.

    Ordinary single-word replies while Viola is genuinely idle (not speaking AND not
    generating) still start a turn at ``min_words = 1``, so normal back-and-forth keeps
    its low latency -- the gate governs ONLY interruptions.
    """

    def __init__(
        self,
        *,
        min_words: int,
        echo_reference: BotSpeechEchoReference,
        floor_state: BotTurnFloorState | None = None,
        latency_trace: Any = None,
        use_interim: bool = True,
        **kwargs: Any,
    ) -> None:
        # Set before super().__init__: the base sets ``self._bot_speaking = False``,
        # which routes through the property setter below and must find these attrs.
        self._bot_actually_speaking = False
        self._floor_state = floor_state
        super().__init__(min_words=min_words, use_interim=use_interim, **kwargs)
        self._echo_reference = echo_reference
        # Observe-only recorder (PhoneLatencyTraceRecorder | None). Emitting into it
        # inherits the shared per-call ts+call_id stamp. NEVER influences the guard's
        # suppress/pass disposition -- this is instrumentation, not model boxing.
        self._latency_trace = latency_trace

    def _emit_guard_event(self, event: str, **payload: Any) -> None:
        """Best-effort emit to the shared latency recorder; never breaks the turn path."""
        recorder = self._latency_trace
        if recorder is None:
            return
        # The recorder's append_event already self-disables on write errors; this
        # narrow catch is defense-in-depth so a malformed handle can never break the
        # live turn path. Instrumentation must never take down the audio pipeline.
        try:
            recorder.record_turn_guard_event(event, **payload)
        except (AttributeError, OSError, ValueError, TypeError):  # pragma: no cover
            logger.debug("phone self-echo guard: failed to emit %s event", event, exc_info=True)

    @property
    def _bot_speaking(self) -> bool:
        """Effective "bot holds the floor" = actively speaking OR mid-generation.

        The base ``_handle_transcription`` reads ``self._bot_speaking`` to pick
        ``min_words`` vs 1. Overriding it as a property lets the min_words gate cover
        the generation window WITHOUT duplicating the base decision logic, so future
        Pipecat changes to that logic keep working.
        """
        if self._bot_actually_speaking:
            return True
        floor = self._floor_state
        return bool(floor is not None and floor.is_generating)

    @_bot_speaking.setter
    def _bot_speaking(self, value: bool) -> None:
        # The base tracks only *actively speaking* here (BotStarted/Stopped frames);
        # the generation half of the floor comes from ``_floor_state``.
        self._bot_actually_speaking = bool(value)

    async def _handle_bot_started_speaking(self, frame: Any) -> None:
        await super()._handle_bot_started_speaking(frame)
        # Viola's audio is now playing: the generation window is over. ``_bot_speaking``
        # stays True via ``_bot_actually_speaking``, so the gate is unaffected.
        if self._floor_state is not None:
            self._floor_state.note_generation_ended()
        self._emit_guard_event("bot_speaking_start", frame_type=type(frame).__name__)

    async def _handle_bot_stopped_speaking(self, frame: Any) -> None:
        await super()._handle_bot_stopped_speaking(frame)
        # Viola's turn is fully done: release the floor so the recipient's next reply
        # starts a turn at min_words=1 (snappy) rather than being gated as an interrupt.
        if self._floor_state is not None:
            self._floor_state.note_generation_ended()
        # Key missing signal: the discrete moment Viola's turn ends. This is where the
        # echo-tail window starts in a later correlation phase. See module discovery note
        # on BotStoppedSpeakingFrame semantics (queue-of-last-frame vs actual playout).
        self._emit_guard_event("bot_speaking_stop", frame_type=type(frame).__name__)

    async def _handle_transcription(self, frame: Any) -> ProcessFrameResult:
        if self._bot_speaking and self._echo_reference is not None:
            text = getattr(frame, "text", "") or ""
            if self._echo_reference.is_echo(text):
                logger.debug(
                    "phone self-echo suppressed inbound transcript (%d chars) during bot speech",
                    len(text),
                )
                # Observe-only: record the candidate + the guard's disposition BEFORE
                # acting. Emission never alters the suppress/pass decision below.
                self._emit_guard_event(
                    "bargein_candidate",
                    disposition="suppressed_as_echo",
                    text=text,
                    text_chars=len(text),
                    bot_speaking=True,
                )
                # Same disposition as a sub-threshold backchannel: drop it, keep Viola.
                await self.trigger_reset_aggregation()
                return ProcessFrameResult.CONTINUE
        # Pass-through: the guard did NOT suppress this as echo -> it goes to the base
        # turn-start strategy (which independently applies the min_words gate). Record
        # the candidate with the guard's own disposition; do not pre-judge the base's
        # outcome here. ``bot_speaking`` distinguishes an interruption attempt (True)
        # from an idle-state reply (False) for later correlation.
        self._emit_guard_event(
            "bargein_candidate",
            disposition="passed",
            text=getattr(frame, "text", "") or "",
            text_chars=len(getattr(frame, "text", "") or ""),
            bot_speaking=bool(self._bot_speaking),
        )
        return await super()._handle_transcription(frame)


def build_phone_user_turn_strategies(
    *,
    min_words: int,
    stop_strategies: list[Any],
    latency_trace: Any = None,
    use_interim: bool = True,
) -> tuple[Any, BotSpeechEchoReference, BotTurnFloorState]:
    """Construct the phone pipeline's user-turn strategies + their guard state.

    This is the ONE place the less-yielding, self-echo-guarded, generation-window-floor
    turn-start strategy is built, so the production cloud pipeline (``call_manager.py``)
    and the in-process loopback rig (``loopback_phone_call_session.py``) cannot silently
    diverge on the exact barge-in / self-echo behaviour they claim to share. Returns the
    ``UserTurnStrategies`` plus the freshly-created ``BotSpeechEchoReference`` and
    ``BotTurnFloorState`` the caller must (a) stash on the call record, (b) feed via
    ``create_bot_speech_echo_tap`` after TTS, and (c) drive via
    ``attach_generation_floor_handlers`` on the user aggregator -- exactly as production does.

    ``stop_strategies`` is passed through unchanged (turn-STOP is Smart-Turn/VAD's job and
    is orthogonal to the start-strategy guard). ``latency_trace`` is the observe-only
    per-call recorder; it never affects the guard's suppress/pass disposition.
    """
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    echo_reference = BotSpeechEchoReference()
    floor_state = BotTurnFloorState()
    turn_strategies = UserTurnStrategies(
        start=[
            SelfEchoGuardedMinWordsUserTurnStartStrategy(
                min_words=min_words,
                echo_reference=echo_reference,
                floor_state=floor_state,
                latency_trace=latency_trace,
                use_interim=use_interim,
            ),
        ],
        stop=stop_strategies,
    )
    return turn_strategies, echo_reference, floor_state


def attach_generation_floor_handlers(user_aggregator: Any, floor_state: BotTurnFloorState) -> None:
    """Drive the generation-window floor from Pipecat's own user-turn lifecycle.

    The recipient yielding (``on_user_turn_stopped``) opens Viola's generation window;
    a new user turn starting (``on_user_turn_started`` -- an ordinary reply or a genuine
    multi-word interruption that cleared the gate) closes it. Bot start/stop speaking is
    handled inside the strategy from the bot-speaking frames it already sees. Identical
    wiring in production and the loopback rig.
    """

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def _on_user_turn_stopped_floor(_agg: Any, _strategy: Any, _message: Any) -> None:
        floor_state.note_generation_started()

    @user_aggregator.event_handler("on_user_turn_started")
    async def _on_user_turn_started_floor(_agg: Any, _strategy: Any) -> None:
        floor_state.note_generation_ended()


def create_bot_speech_echo_tap(reference: BotSpeechEchoReference) -> Any:
    """A pass-through FrameProcessor that feeds outbound TTS text into ``reference``.

    Placed just after the TTS service so it sees the ``TTSTextFrame`` word stream that
    is actually being synthesised (post payment/identity/disclosure filtering) -- i.e.
    exactly the audio that gets echoed back off the recipient's handset.
    """
    from pipecat.frames.frames import TTSTextFrame
    from pipecat.processors.frame_processor import FrameProcessor

    ref = reference

    class _BotSpeechEchoTap(FrameProcessor):
        async def process_frame(self, frame: Any, direction: Any) -> None:
            # Must call super() so the base processor handles StartFrame and marks
            # itself started; otherwise it rejects every subsequent frame.
            await super().process_frame(frame, direction)
            if isinstance(frame, TTSTextFrame):
                ref.note_bot_text(getattr(frame, "text", "") or "")
            await self.push_frame(frame, direction)

    return _BotSpeechEchoTap()
