"""Phone turn-STOP strategy: end the recipient's turn when Smart-Turn says it ended.

WHY THIS EXISTS (2026-08-08). On every steady-state turn of a live call, Viola sat
silent for a fixed 800 ms after the recipient's transcript was already in hand, with
Smart-Turn's semantic end-of-turn verdict computed, discarded, and replaced by a timer.

Measured on graded call `6f271e68`: the stretch from the transcript reaching the
aggregator's turn-START strategy to the aggregated context leaving toward the LLM was
823, 823, 831, 819 and 823 ms across four turns in completely different call states --
one of them a barge-in over Viola's own audio. A 12 ms spread is a fixed wait, not
model inference and not scheduler jitter.

THE INTERACTION, which lives across three pipecat files and no single one of them
looks wrong on its own:

1. Viola deliberately runs ``MinWordsUserTurnStartStrategy`` as its SOLE turn-START
   strategy (the less-yielding turn-taking decision, `call_manager.py`), so a
   recipient turn STARTS on their finalized transcript rather than on VAD.
2. ``UserTurnController.process_frame`` runs every START strategy before every STOP
   strategy, on the same frame
   (`pipecat/turns/user_turn_controller.py:174-181`).
3. Starting a turn resets every STOP strategy
   (`pipecat/turns/user_turn_controller.py:268-275`). That reset clears
   ``_turn_complete``, ``_vad_stopped_time`` and ``_transcript_finalized`` -- but not
   ``_stop_secs`` and not ``_stt_timeout``
   (`.../turn_analyzer_user_turn_stop_strategy.py:64-74`).
4. So when the STOP strategy finally sees that same transcript, the VAD stop it
   already processed, and the Smart-Turn verdict it already computed, are gone. Its
   first ``_maybe_trigger_user_turn_stopped()`` returns immediately because
   ``_turn_complete`` is False. It then takes its "no VAD stop was received" fallback,
   assumes the turn is complete, arms a timer -- and never calls
   ``_maybe_trigger_user_turn_stopped()`` again
   (`.../turn_analyzer_user_turn_stop_strategy.py:170-192`).
5. The timer is ``max(0, stt_timeout - stop_secs)``. Neither pipecat's Whisper service
   nor Viola's `_SharedPhoneWhisperSTTService` passes ``ttfs_p99_latency``, so it falls
   back to ``DEFAULT_TTFS_P99 = 1.0`` (`pipecat/services/stt_latency.py:28`), giving
   ``1.0 - 0.2 = 0.8`` exactly. When it fires it re-checks three flags that were all
   already satisfied 800 ms earlier.

WHAT THIS CLASS DOES. It preserves the Smart-Turn verdict across that reset and, when
the verdict says the utterance is semantically COMPLETE and the transcript is
finalized, ends the turn immediately instead of waiting out the timer.

WHY NOT THE OBVIOUS ALTERNATIVES:

- *Set ``ttfs_p99_latency`` to a measured Whisper value.* It only shrinks the sleep
  (~0.5 s on our measured 410-759 ms speech-end-to-transcript), leaves the defect in
  place, and couples how fast Viola takes her turn to an STT latency constant. That is
  tuning a number whose job is not turn-taking.
- *Trigger unconditionally on any finalized transcript.* This removes the same 800 ms
  but also removes real protection. That window incidentally acts as a resume grace: a
  fresh ``VADUserStartedSpeakingFrame`` cancels the timer
  (`.../turn_analyzer_user_turn_stop_strategy.py:138-149`), so a recipient who pauses
  mid-thought for longer than the 192 ms VAD hangover is not talked over. Firing on
  every finalized transcript would trade a latency bug for an interruption bug, which
  is the same shape of mistake as leaving it slow.
- *Patch pipecat in the venv.* Vendored library; the edit dies at the next install.

So the gate stays semantic and Smart-Turn keeps owning it -- which is the whole reason
`PHONE_VAD_SILENCE_SECS` is at Smart-Turn's recommended 0.2 s design point. When
Smart-Turn says the utterance is finished, Viola answers now. When it says the
recipient is mid-thought, the existing timer still runs and behaviour is byte-for-byte
what it is today. The fast path is strictly narrower than "always fast".
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Only the base strategy is imported. The frame and end-of-turn-state types this
# module reasons about (``VADUserStartedSpeakingFrame``, ``TranscriptionFrame``,
# ``EndOfTurnState``, ...) are never referenced in code: the base class dispatches to
# the ``_handle_*`` overrides below by name, so nothing here isinstance-checks a frame,
# and the analyzer's verdict is read through the base class's own ``_turn_complete``
# boolean rather than compared against an ``EndOfTurnState`` member. Importing them
# anyway left five names bound to ``None`` on the non-pipecat path that no code could
# ever read -- an import list that documented intent instead of expressing it.
try:
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard for non-pipecat unit environments.
    PIPECAT_AVAILABLE = False
    TurnAnalyzerUserTurnStopStrategy = object  # type: ignore[assignment,misc]


class SemanticEndOfTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):  # type: ignore[misc,valid-type]
    """Turn-STOP strategy that survives the turn-START reset with its verdict intact.

    The base class computes Smart-Turn's end-of-turn verdict, then loses it when the
    turn-START strategy resets it on the very same frame (see module docstring). This
    subclass keeps the last verdict in a field ``reset()`` does not touch, so the
    decision the analyzer already made is still available when the transcript arrives.
    """

    def __init__(self, *, latency_trace: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # The analyzer's most recent verdict for the utterance currently being
        # transcribed. Deliberately NOT cleared by ``reset()`` -- surviving the reset
        # is the entire point. It is cleared when a genuinely new utterance begins
        # (``_handle_vad_user_started_speaking``), so a stale COMPLETE from the
        # previous utterance can never fast-path the next one.
        self._semantic_turn_complete = False
        # Observe-only. Smart-Turn's verdict reaches NO trace today: the analyzer's
        # MetricsFrame is pushed downstream from inside the aggregator, while the only
        # tap that classifies VAD/turn metrics sits upstream of it, so `vad_ms` appears
        # in zero rows of any archived call. That matters more after this fix than
        # before, because the verdict now decides whether Viola answers immediately or
        # waits out the timer -- so how often the fast path fires is the difference
        # between "800 ms saved on every turn" and "800 ms saved sometimes", and
        # nothing in production could tell them apart. This emits the verdict at the
        # moment it is acted on. It never influences the decision.
        self._latency_trace = latency_trace

    async def _handle_vad_user_started_speaking(self, frame: Any) -> None:
        # A new utterance invalidates the previous verdict. This runs BEFORE the base
        # class clears its own state, but order does not matter here: both land on
        # "no verdict for the utterance now starting".
        self._semantic_turn_complete = False
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_vad_user_stopped_speaking(self, frame: Any) -> None:
        # The base class runs the Smart-Turn analyzer here and records the answer in
        # `_turn_complete`. Capture it before a turn-START reset can wipe it.
        await super()._handle_vad_user_stopped_speaking(frame)
        self._semantic_turn_complete = bool(self._turn_complete)

    async def _handle_input_audio(self, frame: Any) -> None:
        # A batch analyzer can also reach COMPLETE here, on its own silence timeout,
        # after returning INCOMPLETE at the VAD stop. That is a real verdict too.
        await super()._handle_input_audio(frame)
        if self._turn_complete:
            self._semantic_turn_complete = True

    async def _handle_transcription(self, frame: Any) -> None:
        await super()._handle_transcription(frame)

        # Past this point the base class has, for a finalized transcript arriving after
        # a turn-START reset, set `_turn_complete = True` via its fallback and armed the
        # timer. Every condition `_maybe_trigger_user_turn_stopped` tests is satisfied;
        # the base class simply never asks again. Ask -- but only when the analyzer
        # actually said the utterance was over, so a mid-thought pause still waits.
        finalized = bool(getattr(frame, "finalized", False))
        fast_path = bool(self._semantic_turn_complete and finalized and self._text and self._turn_complete)
        self._record_verdict(finalized=finalized, fast_path=fast_path)
        if not fast_path:
            return
        # Cancels the pending timeout and fires `on_user_turn_stopped`.
        await self._maybe_trigger_user_turn_stopped()

    def _record_verdict(self, *, finalized: bool, fast_path: bool) -> None:
        """Emit what Smart-Turn decided and whether it shortened this turn."""
        if self._latency_trace is None:
            return
        try:
            self._latency_trace.record_turn_guard_event(
                "turn_stop_verdict",
                semantic_turn_complete=self._semantic_turn_complete,
                transcript_finalized=finalized,
                # True  -> turn ended now; the ~800 ms fallback timer was cancelled.
                # False -> the timer still governs, exactly as it did before this fix.
                fast_path_taken=fast_path,
            )
        except (AttributeError, TypeError, ValueError):
            # Instrumentation must never break turn-taking on a live call.
            logger.debug("turn_stop_verdict trace emit failed", exc_info=True)


def build_phone_user_turn_stop_strategies(*, turn_analyzer: Any, latency_trace: Any = None) -> list[Any]:
    """The ONE place both phone pipelines build their turn-STOP strategies.

    Production (`telephony/call_manager.py`) and the in-process loopback rig
    (`telephony/loopback_phone_call_session.py`) must construct turn-taking the same
    way or the rig stops being an oracle for turn latency -- the same reasoning that
    already keeps the turn-START strategy in one shared builder
    (`phone_self_echo_guard.build_phone_user_turn_strategies`) and the answer-settle
    observer on the same side of STT in both. `scripts/check_phone_call_quality_guards.py`
    pins that both call this.
    """
    if not PIPECAT_AVAILABLE:  # pragma: no cover - guarded at both call sites already.
        raise RuntimeError("phone turn-stop strategies require pipecat")
    return [SemanticEndOfTurnStopStrategy(turn_analyzer=turn_analyzer, latency_trace=latency_trace)]
