"""The wake word while Viola is talking or busy.

1.0.5 build 9 (Jay's Surface, 11:34:29): the wake model scored "Viola, stop" at
0.984 while she spoke and the policy threw it away (`SELF_SPEECH: Wake blocked
while Viola is speaking`). During a voice turn the listening gate threw it away
one line earlier. The only other way in was a transcript of the last 3 s, and
over a loud speaker Whisper mostly hears her, so a shouted "Viola, stop" lost.

The old block existed because her own "Viola" also scores high (first-run
narration woke her). The answer to "was that her?" is available without guessing:
Viola knows exactly what she played a moment ago (audio_core.own_playback), so
the same wake model scores THAT. Measured with Kokoro replies through a laptop
echo path: every self-trigger had a reference score of 0.80 or more at that
moment, and none passed the check in 62 minutes of her speech. The check costs
about one wake-model pass over 3 s of audio and runs only on a candidate.

With no record of her audio (a TTS engine that does not report it) nothing can
tell her voice from a person's, so the wake is refused, as it always was: not
hearing "stop" is better than obeying herself.

This module also carries the two signals between the wake loop and the voice turn:
the barge-in target (a turn that is speaking takes the wake itself) and "a voice
turn is waiting on a long task" (wake stays open, see VoiceCommandHandler).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from core.logging_config import get_logger

logger = get_logger(__name__)


def get_own_playback() -> Any:
    """The record of what she played (imported late: audio_core pulls in the capture stack)."""
    from audio_core.own_playback import get_own_playback as playback

    return playback()


CHUNK_SAMPLES = 1280
# The detector's rule while she talks: (k, n, frame threshold). Every frame of
# "Viola" is masked by her voice, and recordings that peak at 0.92-0.96 over a
# reply clear 6 of 7 at 0.85 only a third of the time. Her own voice cannot pass on
# this rule alone (every candidate is checked below), so it only has to be quick
# and stay quiet on other people's speech: 125 candidates in 30 h of speech.
OVER_SPEECH_RULE: tuple[int, int, float] = (2, 3, 0.85)
# Scored audio: the model needs about 1.4 s to warm up, then the 1.6 s left
# covers the word and the time the detector took to confirm it.
REFERENCE_WINDOW_S = 3.2
# A reference score at or above this means she said something wake-like just now.
# Every time her voice set the detector off (62 min of replies, ones full of
# "Viola" and "viola" and "violet"), her reference scored 0.80 or more; ordinary
# sentences reach 0.59, and that must not hide a person saying "Viola" over them.
OWN_WORD_SCORE = 0.7
# A wake over a reply nobody is waiting on (typed, a reminder) needs this much
# if only the quick rule confirmed it.
STRONG_SCORE = 0.95
# The self-speech flag lasts 2 s past her last word; a record older than that is
# from some earlier speech.
RECORD_FRESH_S = 2.5
# Keeps the scored reference at a level the model handles the same way at any
# output volume (a quiet-hours reply is otherwise near the silence gate).
_REFERENCE_RMS = 0.05


@dataclass(frozen=True)
class OwnSpeechVerdict:
    """Whether a wake candidate heard while Viola talks is someone else."""

    cleared: bool
    reason: str
    reference_score: float | None = None

    def describe(self) -> str:
        ref = "-" if self.reference_score is None else "%.3f" % self.reference_score
        return "%s reference_score=%s" % (self.reason, ref)


def reference_wake_score(engine: Any, audio: np.ndarray) -> float:
    """The best wake score of ``audio`` (float32 at 16 kHz) through a private ``engine`` stream."""
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if len(audio) else 0.0
    if rms < 1e-5:
        return 0.0
    scaled = (audio * (_REFERENCE_RMS / rms)).astype(np.float32)
    engine.reset_stream()
    best = 0.0
    for start in range(0, len(scaled) - CHUNK_SAMPLES + 1, CHUNK_SAMPLES):
        best = max(best, float(engine.process_chunk(scaled[start : start + CHUNK_SAMPLES])))
    return best


def judge_wake_over_speech(reference_score: float | None) -> OwnSpeechVerdict:
    """Decide one wake candidate heard while she talks (or just stopped)."""
    if reference_score is None:
        return OwnSpeechVerdict(False, "no_record_of_her_audio")
    if reference_score >= OWN_WORD_SCORE:
        return OwnSpeechVerdict(False, "her_own_voice", reference_score)
    return OwnSpeechVerdict(True, "not_in_her_speech", reference_score)


def check_wake_over_speech(score: float, reference_engine: Any) -> OwnSpeechVerdict:
    """Score what she just played and judge the candidate. Fails closed: she keeps talking."""
    playback = get_own_playback()
    started = time.perf_counter()
    try:
        audio = playback.recent_audio(REFERENCE_WINDOW_S)
        # She is flagged as speaking, so her audio must be here: a silent record
        # means this speech does not report it, which proves nothing about her voice.
        unrecorded = audio is None or reference_engine is None or playback.seconds_since_audio() > RECORD_FRESH_S
        reference = None if unrecorded else reference_wake_score(reference_engine, audio)
    except Exception as exc:  # noqa: BLE001, RUF100 - a failed check must not cut her off
        logger.warning("[WAKE_OVER_SPEECH] reference check failed: %s", exc)
        return OwnSpeechVerdict(False, "reference_check_failed")
    verdict = judge_wake_over_speech(reference)
    logger.info(
        "[WAKE_OVER_SPEECH] %s score=%.3f %s in %.0f ms",
        "CLEARED" if verdict.cleared else "REFUSED",
        score,
        verdict.describe(),
        (time.perf_counter() - started) * 1000,
    )
    return verdict


# --------------------------------------------------------------------------- #
# Between the wake loop and the voice turn                                     #
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_barge_in_target: Callable[[], None] | None = None
_speech_stopper: Callable[[], None] | None = None
_busy_turn = False


def set_barge_in_target(target: Callable[[], None] | None) -> None:
    """The voice turn now speaking takes wakes heard over it (None: it finished)."""
    global _barge_in_target
    with _lock:
        _barge_in_target = target


def clear_barge_in_target(target: Callable[[], None]) -> None:
    """Remove ``target`` if it is still the current one."""
    global _barge_in_target
    with _lock:
        if _barge_in_target is target:
            _barge_in_target = None


def deliver_barge_in() -> bool:
    """Hand a cleared wake to the speaking turn. False when no turn is speaking."""
    with _lock:
        target = _barge_in_target
    if target is None:
        return False
    get_own_playback().waive_tail()
    target()
    return True


def set_speech_stopper(stopper: Callable[[], None] | None) -> None:
    """How to silence Viola when no voice turn is speaking (a typed reply, a reminder)."""
    global _speech_stopper
    with _lock:
        _speech_stopper = stopper


def stop_speaking() -> None:
    with _lock:
        stopper = _speech_stopper
    get_own_playback().waive_tail()
    if stopper is None:
        return
    try:
        stopper()
    except Exception as exc:  # noqa: BLE001, RUF100 - the wake goes on regardless
        logger.debug("Could not stop speech for a wake: %s", exc)


def set_busy_turn(busy: bool) -> None:
    """A voice turn is waiting on its command and can take a wake alongside it."""
    global _busy_turn
    with _lock:
        _busy_turn = bool(busy)


def busy_turn_active() -> bool:
    with _lock:
        return _busy_turn


__all__ = [
    "OVER_SPEECH_RULE",
    "OWN_WORD_SCORE",
    "REFERENCE_WINDOW_S",
    "STRONG_SCORE",
    "OwnSpeechVerdict",
    "busy_turn_active",
    "check_wake_over_speech",
    "clear_barge_in_target",
    "deliver_barge_in",
    "judge_wake_over_speech",
    "reference_wake_score",
    "set_barge_in_target",
    "set_busy_turn",
    "set_speech_stopper",
    "stop_speaking",
]
