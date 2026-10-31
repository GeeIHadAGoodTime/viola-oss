"""Stage 2 wake verification: a second listen with Whisper before the chime.

The wake detector (a small acoustic model scoring 1.5 s windows) fires on
ordinary speech about once an hour of conversation ("you know", "we all",
"real", "yellow"). This module is the second look that sits between "the
detector fired and the policy allowed it" and "chime plus command". It runs
the bundled faster-whisper ``tiny.en`` (the same model the app already loads
for commands, shared, never loaded on the wake path) over the wake window plus
up to about one second of the audio that follows, with the hint
``initial_prompt="Viola, "``, and asks one question: does the transcript
contain something that sounds like "Viola"?

It is a *soft* gate for a confident detector score. It rejects such a wake
only when Whisper clearly heard speech and none of it sounds like "Viola".
Everything uncertain passes: the model not loaded yet, an error or timeout, an
empty transcript, audio too quiet to judge (whispers, far field), a
low-confidence or repetitive (hallucinated) decode. A missed real "Viola" is as
bad as a false wake, so doubt lets a confident wake through.

Two cases are stricter (1.0.5 build-7 laptop report):

- A *low-score* wake: the detector's agreement rule allowed it (k of n frames at
  the lower per-frame threshold) but the confirming frame scored below the
  sensitivity level. Then any words Whisper decodes must include a Viola-like
  token, unsure decode or not. Build 7 let "Yo!" from a rap track through at
  0.885 (level 0.900) as ``unsure_low_confidence``. Wake-opt data: speech false
  wakes 11 -> 8 of 22 candidates, music 68 -> 35 of 92, for 0 to 2 points of
  recall on real recordings (2 only over loud music from another device).
- A *music-path* wake (``require_token``): while Viola's own music plays, the
  detector may also wake on a looser rule (``violawake.config``
  ``MUSIC_WAKE_RULE``) because vocal music masks the word; such a wake passes
  only on a Viola-like token Whisper is confident about. Doubt rejects it.

The check also runs while Viola's own music plays. It used to be skipped then,
which let room talk over the music wake Viola with nothing checked (build 7,
11:34:53, "There we go..."). With the decode early start it costs no extra
wait; over music it keeps about 90% of detected real wakes in the recorded sets.

A Viola-like token is not trusted when Whisper itself says there was no speech
and the decode is unsure (``no_speech_prob >= 0.6`` and ``avg_logprob <=
-1.0``): that is the prompt "Viola, " echoed back over music. All 6 music
false-wake candidates that decoded a token looked like that, and none of 1,491
token decodes of real "Viola" recordings did.

Early start: the streaming detector confirms a wake only after k agreeing
80 ms frames (6 of 7 at Balanced while this check is on, see
``violawake.config.STREAM_OPERATING_POINTS_CHECKED``), a few hundred ms after
its first agreeing frame. Once the run of agreeing frames reaches
``VerifierConfig.early_start_frames`` (2), the listener hands the window
ending on that frame to Whisper on the verifier's worker thread; the wake that
run confirms decides on that result, which is usually ready by then, so the
chime waits for nothing. That window also holds less of a command said straight
after "Viola", which Whisper otherwise transcribes instead of the wake word.
Without a usable early decode (model just warmed, a gap in the audio) the
check runs serially: the window plus a short tail, then the decode.

The audio read while verifying (the tail and anything that arrives while
Whisper runs) is handed back to the caller as raw frames so the command
recorder can start with it: "Viola, what time is it" said in one breath loses
nothing to the check.

Every verification writes one ``[WAKE_VERIFY]`` line to the app log (verdict,
reason, detector score, transcript, timings) so real-world false and missed
wakes are visible in ``viola-qt.log``. The advanced setting
``wake_verification_enabled`` (default on) turns the check off; the
``VIOLA_WAKE_VERIFICATION`` environment variable (``0``/``1``) overrides it.

The defaults were measured on held-out conversational speech and on real
recordings of the wake word (release 1.0.5 Stage 2 measurement note,
``docs/wake/STAGE2_WHISPER_VERIFICATION.md`` in the app repository).
"""

from __future__ import annotations

import os
import re
import threading
import time
import zlib
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Protocol

import numpy as np

from core.logging_config import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280  # 80 ms, the wake listener's read size
SETTING_KEY = "wake_verification_enabled"
ENV_OVERRIDE = "VIOLA_WAKE_VERIFICATION"
VERIFIER_MODEL = "tiny.en"


# ---------------------------------------------------------------------------
# Viola-like tokens
# ---------------------------------------------------------------------------

# Spellings tiny.en produced for a spoken "Viola" (two speakers, normal,
# whispered, over music) in the wake review prototype and in this stage's
# development half. Matched after lower-casing and stripping punctuation, on
# single words and on two adjacent words joined ("the ola" -> "theola").
_VIOLA_EXACT = frozenset(
    {
        "viola",
        "violas",
        "violah",
        "vyola",
        "viyola",
        "vieola",
        "veola",
        "voila",
        "vola",
        "viala",
        "vioa",
        "fiola",
        "biola",
        "beola",
        "theola",
        "riola",
        "reola",
        "diola",
        "diala",
        "piola",
        "wiola",
        "yola",
        "iola",
        "ola",
        "viol",
        "violet",
        "violin",
    }
)

_TARGET = "viola"
_WORD_RE = re.compile(r"[a-z]+")


def _edit_distance(a: str, b: str, limit: int = 2) -> int:
    """Levenshtein distance, stopping early once it exceeds ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(value)
            best = min(best, value)
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _is_viola_like_word(word: str) -> bool:
    if word in _VIOLA_EXACT:
        return True
    # "...iola" / "...yola" endings: midiola, riola, sheyola.
    if len(word) <= 8 and (word.endswith("iola") or word.endswith("yola")):
        return True
    # Short "...ola" words: theola, beola, re-ola. Long ones (granola,
    # gondola, motorola) are real words that are nothing like "Viola".
    if 3 <= len(word) <= 6 and word.endswith("ola"):
        return True
    # A real "Viola" heard as "Gala" or "Caula": short "...ala" / "...ula" words.
    if 4 <= len(word) <= 5 and (word.endswith("ala") or word.endswith("ula")):
        return True
    # One letter away from "viola": voila, vioia, viola typos.
    return 4 <= len(word) <= 6 and _edit_distance(word, _TARGET, limit=1) <= 1


def find_viola_token(text: str) -> str | None:
    """Return the first Viola-like word (or joined word pair) in ``text``."""
    words = _WORD_RE.findall(text.lower().replace("'", ""))
    for word in words:
        if _is_viola_like_word(word):
            return word
    for first, second in pairwise(words):
        joined = first + second
        if len(joined) <= 8 and _is_viola_like_word(joined):
            return f"{first} {second}"
    return None


# ---------------------------------------------------------------------------
# Soft-gate rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifierConfig:
    """Tunables. Defaults are the measured operating point (see module doc)."""

    prompt: str = "Viola, "
    # Tail after the wake window: at least min_tail_s, at most max_tail_s, and
    # stop early once quiet_run_s of quiet has followed the word (the speaker
    # is pausing for the chime; more silence tells Whisper nothing).
    min_tail_s: float = 0.16
    max_tail_s: float = 1.0
    quiet_run_s: float = 0.24
    # A chunk is "quiet" below this fraction of the loudest chunk in the window.
    quiet_ratio: float = 0.2
    noise_floor_factor: float = 2.0
    # Window too quiet to judge (whispers, far field): float RMS of the
    # loudest 80 ms chunk. 0.004 is about -48 dBFS.
    quiet_audio_rms: float = 0.004
    # Whisper must answer within this budget or the wake passes.
    whisper_timeout_s: float = 1.5
    # Unsure decode -> pass. -0.75 (not Whisper's usual -1.0): with the early
    # start (below) the dev half of the real recordings lost "Viola" said over
    # another device's music at -0.8 (Jay 89.8%, detector 93.2%); -0.75 keeps
    # 90.7% for no more of 22 speech false wakes and 3 more of 92 over music
    # let through. Build-5 measurement note, "Faster and steadier" section.
    no_speech_unsure: float = 0.6
    logprob_unsure: float = -0.75
    compression_unsure: float = 2.4
    min_words: int = 1
    # Early start: begin the Whisper decode while the detector's agreement
    # run is still building, once this many consecutive frames agree (0 = off).
    # The decode hears the window that ends on that frame (no tail) and is
    # usually finished by the time the detector confirms, so the chime waits
    # for nothing. A wake that confirms without a usable early decode falls
    # back to the serial check (window + tail, then decode). 2: at Balanced
    # (6 of 7 with the check on) the decode gets about 320 ms of lead, enough
    # for tiny.en on this class of PC; the window ends about where the word does.
    early_start_frames: int = 2
    # A Viola-like token decoded with no_speech_prob at or above this AND
    # avg_logprob at or below hallucination_logprob is the prompt echoed back,
    # not a heard word (see module doc).
    hallucination_no_speech: float = 0.6
    hallucination_logprob: float = -1.0


@dataclass(frozen=True)
class TranscriptInfo:
    text: str
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0
    compression_ratio: float = 1.0


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reason: str
    token: str | None = None


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text.lower()))


def _hallucinated_token(info: TranscriptInfo, cfg: VerifierConfig) -> bool:
    return info.no_speech_prob >= cfg.hallucination_no_speech and info.avg_logprob <= cfg.hallucination_logprob


def decide(
    info: TranscriptInfo | None,
    config: VerifierConfig | None = None,
    *,
    low_score: bool = False,
    require_token: bool = False,
) -> GateDecision:
    """The gate.

    Default (a confident detector score): reject only clear speech that holds
    no Viola-like token. ``low_score``: the confirming frame scored below the
    sensitivity level, so decoded words without a Viola-like token reject even
    when the decode is unsure. ``require_token``: only a trusted Viola-like
    token passes (the looser music-path rule).
    """
    cfg = config or VerifierConfig()
    if info is None:
        return GateDecision(not require_token, "no_transcript")
    text = info.text.strip()
    if not text:
        return GateDecision(not require_token, "empty")
    token = find_viola_token(text)
    if token and not _hallucinated_token(info, cfg):
        return GateDecision(True, "token", token)
    if require_token:
        return GateDecision(False, "music_rule_without_viola")
    if low_score:
        return GateDecision(False, "low_score_without_viola")
    if info.no_speech_prob >= cfg.no_speech_unsure:
        return GateDecision(True, "unsure_no_speech")
    if info.avg_logprob <= cfg.logprob_unsure:
        return GateDecision(True, "unsure_low_confidence")
    if info.compression_ratio >= cfg.compression_unsure:
        return GateDecision(True, "unsure_repetitive")
    if _word_count(text) < cfg.min_words:
        return GateDecision(True, "unsure_no_words")
    return GateDecision(False, "speech_without_viola")


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def _chunk_rms(audio: np.ndarray) -> np.ndarray:
    n = len(audio) // CHUNK_SAMPLES
    if n == 0:
        return np.array([float(np.sqrt(np.mean(audio**2)))] if len(audio) else [0.0])
    frames = audio[: n * CHUNK_SAMPLES].reshape(n, CHUNK_SAMPLES).astype(np.float64)
    return np.sqrt(np.mean(frames**2, axis=1))


def _trailing_quiet_chunks(rms: Sequence[float], threshold: float) -> int:
    count = 0
    for value in reversed(rms):
        if value >= threshold:
            break
        count += 1
    return count


def _compression_ratio(text: str) -> float:
    raw = text.encode("utf-8")
    if not raw:
        return 1.0
    return len(raw) / max(1, len(zlib.compress(raw)))


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeChunk:
    """One 80 ms read: the raw mic bytes (for the command recorder) and the
    float32 audio in [-1, 1] the verifier listens to (post echo cancellation)."""

    raw: bytes
    audio: np.ndarray


class ChunkReader(Protocol):
    def __call__(self, block: bool) -> WakeChunk | None:
        """Return the next chunk. ``block=False`` returns None when no full
        chunk is buffered yet; ``block=True`` returns None only when the stream
        cannot be read (stopped, device error)."""


@dataclass
class VerificationResult:
    passed: bool
    reason: str
    score: float
    transcript: str = ""
    token: str | None = None
    tail_ms: float = 0.0
    whisper_ms: float = 0.0
    total_ms: float = 0.0
    no_speech_prob: float | None = None
    avg_logprob: float | None = None
    held_frames: list[bytes] = field(default_factory=list)
    # "early": the decode started before the detector confirmed (lead_ms
    # earlier); "serial": window + tail, decoded after the detector confirmed.
    mode: str = "serial"
    lead_ms: float = 0.0
    # The sensitivity level the score was compared with (None: not known),
    # whether the stricter low-score / music-path rule applied, and whether
    # Viola's own music was playing.
    level: float | None = None
    low_score: bool = False
    require_token: bool = False
    music: bool = False

    def log_line(self) -> str:
        transcript = self.transcript.replace("\n", " ")
        if len(transcript) > 120:
            transcript = transcript[:117] + "..."
        nsp = "-" if self.no_speech_prob is None else f"{self.no_speech_prob:.2f}"
        alp = "-" if self.avg_logprob is None else f"{self.avg_logprob:.2f}"
        level = "-" if self.level is None else f"{self.level:.3f}"
        rule = "music_path" if self.require_token else ("low_score" if self.low_score else "normal")
        return (
            f"[WAKE_VERIFY] {'PASS' if self.passed else 'REJECT'} reason={self.reason} "
            f"score={self.score:.3f} level={level} rule={rule} music={'yes' if self.music else 'no'} "
            f"token={self.token or '-'} transcript={transcript!r} "
            f"mode={self.mode} lead_ms={self.lead_ms:.0f} "
            f"tail_ms={self.tail_ms:.0f} whisper_ms={self.whisper_ms:.0f} total_ms={self.total_ms:.0f} "
            f"no_speech={nsp} logprob={alp}"
        )


class EarlyStartTracker:
    """When to start the Whisper decode early (pure bookkeeping, no audio).

    Fed every streaming score with the frame threshold and the agreement rule
    (k of the last n frames) the engine applied to it. ``update`` returns True
    on the frame where the run of consecutive agreeing frames first reaches
    ``launch_frames``; the caller then starts a decode of the window ending on
    that frame. Only rules that need more agreeing frames than that (k >
    launch_frames) start early; otherwise the detector would confirm on the
    same frame and nothing is gained.
    """

    def __init__(self, launch_frames: int) -> None:
        self.launch_frames = max(0, int(launch_frames))
        self.run = 0

    def reset(self) -> None:
        self.run = 0

    def update(self, score: float, threshold: float, k: int) -> bool:
        self.run = self.run + 1 if score >= threshold else 0
        return 0 < self.launch_frames < int(k) and self.run == self.launch_frames

    @staticmethod
    def usable(launch_frame: int, trigger_frame: int, n: int) -> bool:
        """An early decode belongs to a wake when its window ended inside the
        agreement window the detector confirmed on (the last n frames)."""
        return trigger_frame - max(1, int(n)) < launch_frame <= trigger_frame


@dataclass
class EarlyDecode:
    """A decode started before the detector confirmed (see EarlyStartTracker)."""

    frame: int
    started: float
    future: Future[TranscriptInfo] | None = None
    # The early window was too quiet to judge: the wake will pass without Whisper.
    quiet: bool = False
    finished: float | None = None


def _default_model_loader() -> Any:
    """The process-wide tiny.en the command transcriber also uses (shared)."""
    from voice.transcription.whisper import _shared_whisper_model

    return _shared_whisper_model(VERIFIER_MODEL, device="cpu", compute_type="int8")


class WakeVerifier:
    """Loads Whisper once, in the background, and verifies wake candidates."""

    LOAD_RETRY_S = 60.0

    def __init__(
        self,
        config: VerifierConfig | None = None,
        model_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config or VerifierConfig()
        self._model_loader = model_loader or _default_model_loader
        self._model: Any = None
        self._lock = threading.Lock()
        self._warm_thread: threading.Thread | None = None
        # A failed load is retried, at most once per LOAD_RETRY_S, from the
        # next warm-up request (a wake candidate), never on the wake path.
        self._load_failed_at: float | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wake-verify")
        # The newest early decode (start_early cancels it if it has not started).
        self._early_pending: Future[TranscriptInfo] | None = None

    # -- model lifecycle ----------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def warm_async(self) -> None:
        """Load and warm the model on a background thread (idempotent)."""
        with self._lock:
            if self._model is not None:
                return
            failed_at = self._load_failed_at
            if failed_at is not None and time.monotonic() - failed_at < self.LOAD_RETRY_S:
                return
            if self._warm_thread is not None and self._warm_thread.is_alive():
                return
            self._warm_thread = threading.Thread(target=self._warm, name="wake-verify-warm", daemon=True)
            self._warm_thread.start()

    def _warm(self) -> None:
        started = time.perf_counter()
        try:
            model = self._model_loader()
            # One throwaway decode so the first real wake pays no first-call cost.
            self._run_whisper(model, np.zeros(SAMPLE_RATE, dtype=np.float32))
        except Exception as exc:  # noqa: BLE001, RUF100 - a missing verifier must never block wakes
            self._load_failed_at = time.monotonic()
            logger.warning(
                "[WAKE_VERIFY] Whisper verifier unavailable; wakes pass unchecked (retry in %.0f s): %s",
                self.LOAD_RETRY_S,
                exc,
            )
            return
        self._load_failed_at = None
        self._model = model
        logger.info(
            "[WAKE_VERIFY] Whisper %s verifier warm in %.0f ms",
            VERIFIER_MODEL,
            (time.perf_counter() - started) * 1000,
        )

    def wait_until_ready(self, timeout: float) -> bool:
        thread = self._warm_thread
        if thread is not None:
            thread.join(timeout)
        return self.is_ready

    # -- transcription ------------------------------------------------------

    def _run_whisper(self, model: Any, audio: np.ndarray) -> TranscriptInfo:
        segments, _info = model.transcribe(
            audio.astype(np.float32, copy=False),
            language="en",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=self.config.prompt,
            vad_filter=False,
            no_speech_threshold=None,
        )
        segments = list(segments)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if not segments:
            return TranscriptInfo(text="")
        weights = [max(1, len(seg.text)) for seg in segments]
        avg_logprob = float(np.average([seg.avg_logprob for seg in segments], weights=weights))
        return TranscriptInfo(
            text=text,
            no_speech_prob=float(max(seg.no_speech_prob for seg in segments)),
            avg_logprob=avg_logprob,
            compression_ratio=_compression_ratio(text),
        )

    def transcribe(self, audio: np.ndarray) -> TranscriptInfo:
        if self._model is None:
            raise RuntimeError("wake verifier model is not loaded")
        return self._run_whisper(self._model, audio)

    # -- verification -------------------------------------------------------

    def start_early(self, window: np.ndarray, frame: int) -> EarlyDecode | None:
        """Start decoding a wake candidate before the detector confirms.

        Called from the wake loop when ``EarlyStartTracker.update`` says so.
        Never blocks and never loads the model (returns None until it is warm;
        the wake, if it comes, is then checked serially). At most one early
        decode waits behind a running one: a newer launch replaces an older
        one that has not started yet (a broken run's audio is stale anyway).
        """
        if self._model is None:
            self.warm_async()
            return None
        now = time.perf_counter()
        audio = np.array(window, dtype=np.float32)
        rms = _chunk_rms(audio)
        if (float(rms.max()) if len(rms) else 0.0) < self.config.quiet_audio_rms:
            return EarlyDecode(frame=frame, started=now, quiet=True)
        with self._lock:
            previous = self._early_pending
            if previous is not None:
                previous.cancel()  # only succeeds while it has not started
            try:
                future: Future[TranscriptInfo] = self._executor.submit(self.transcribe, audio)
            except RuntimeError:
                return None
            self._early_pending = future
        early = EarlyDecode(frame=frame, started=now, future=future)

        def _done(_f: Future[TranscriptInfo]) -> None:
            early.finished = time.perf_counter()

        future.add_done_callback(_done)
        return early

    def verify(
        self,
        window: np.ndarray,
        read_chunk: ChunkReader,
        score: float,
        *,
        playback_active: bool = False,
        early: EarlyDecode | None = None,
        level: float | None = None,
        require_token: bool = False,
    ) -> VerificationResult:
        """Decide one wake candidate. Never raises; doubt passes (not on the music path).

        ``window`` is the 1.5 s float32 audio the detector scored (oldest
        sample first). ``read_chunk`` pulls the audio that follows, 80 ms at a
        time, from the same stream the command recorder will read next.
        ``playback_active``: Viola is playing music (logged; the check runs
        either way, see the module doc).
        ``level``: the sensitivity level; a ``score`` below it is a low-score
        wake (stricter rule). ``require_token``: the wake came from the looser
        music-path rule; only a trusted Viola-like token passes and every
        doubt (model not ready, quiet, timeout, error) rejects.
        ``early``: a decode this wake's agreement run started before the
        detector confirmed (the caller checked it belongs to this wake). Its
        result decides; no tail is read, and the microphone is drained only
        while it is still running.
        """
        cfg = self.config
        started = time.perf_counter()
        held: list[bytes] = []
        mode = "serial"
        lead_ms = 0.0

        low_score = level is not None and float(score) < float(level)

        def _result(passed: bool, reason: str, **kw: Any) -> VerificationResult:
            res = VerificationResult(
                passed=passed,
                reason=reason,
                score=score,
                held_frames=held,
                total_ms=(time.perf_counter() - started) * 1000,
                mode=mode,
                lead_ms=lead_ms,
                level=level,
                low_score=low_score,
                require_token=require_token,
                music=bool(playback_active),
                **kw,
            )
            return res

        def _doubt(reason: str, **kw: Any) -> VerificationResult:
            # Doubt lets a normal wake through; a music-path wake needs a heard "Viola".
            return _result(not require_token, reason, **kw)

        if self._model is None:
            # Never load on the wake path: kick the background load and pass.
            self.warm_async()
            return _doubt("not_ready")

        tail_ms = 0.0
        if early is not None:
            mode = "early"
            lead_ms = max(0.0, (started - early.started) * 1000)
            if early.quiet or early.future is None:
                return _doubt("quiet_audio")
            future: Future[TranscriptInfo] = early.future
            whisper_started = early.started
        else:
            window = np.asarray(window, dtype=np.float32)
            rms = _chunk_rms(window)
            peak = float(rms.max()) if len(rms) else 0.0
            if peak < cfg.quiet_audio_rms:
                return _doubt("quiet_audio")

            # "Quiet" is relative to the speech peak, but never below the room's
            # own noise floor (else a soft voice in a noisy room never pauses and
            # every wake waits the full tail): twice the window's quietest 10%,
            # capped at half the peak so speech itself is never called quiet.
            floor = float(np.percentile(rms, 10)) if len(rms) else 0.0
            quiet_level = max(min(max(peak * cfg.quiet_ratio, floor * cfg.noise_floor_factor), peak * 0.5), 1e-5)
            quiet_run = _trailing_quiet_chunks(rms.tolist(), quiet_level)
            need_quiet = max(1, round(cfg.quiet_run_s * SAMPLE_RATE / CHUNK_SAMPLES))
            min_tail = round(cfg.min_tail_s * SAMPLE_RATE)
            max_tail = round(cfg.max_tail_s * SAMPLE_RATE)

            tail: list[np.ndarray] = []
            tail_samples = 0
            try:
                while tail_samples < max_tail:
                    if tail_samples >= min_tail and quiet_run >= need_quiet:
                        break
                    chunk = read_chunk(True)
                    if chunk is None:
                        break
                    held.append(chunk.raw)
                    audio = np.asarray(chunk.audio, dtype=np.float32)
                    tail.append(audio)
                    tail_samples += len(audio)
                    chunk_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if len(audio) else 0.0
                    quiet_run = quiet_run + 1 if chunk_rms < quiet_level else 0
            except Exception as exc:  # noqa: BLE001, RUF100 - a read error must not drop the wake
                logger.debug("[WAKE_VERIFY] tail read failed: %s", exc)
            tail_ms = tail_samples * 1000.0 / SAMPLE_RATE

            audio = np.concatenate([window, *tail]) if tail else window
            whisper_started = time.perf_counter()
            try:
                future = self._executor.submit(self.transcribe, audio)
            except RuntimeError as exc:
                return _doubt("error", tail_ms=tail_ms, transcript=str(exc)[:60])
        # The time budget runs from now (the detector confirmed), whichever path.
        deadline = time.perf_counter() + cfg.whisper_timeout_s
        # Keep draining the microphone while Whisper runs so nothing the user
        # says meanwhile is lost (and the input buffer never overflows).
        while not future.done():
            if time.perf_counter() >= deadline:
                return _doubt(
                    "timeout",
                    tail_ms=tail_ms,
                    whisper_ms=(time.perf_counter() - whisper_started) * 1000,
                )
            try:
                chunk = read_chunk(False)
            except Exception:  # noqa: BLE001, RUF100 - keep waiting for Whisper
                chunk = None
            if chunk is None:
                time.sleep(0.005)
                continue
            held.append(chunk.raw)
        finished = early.finished if early is not None and early.finished is not None else time.perf_counter()
        whisper_ms = (finished - whisper_started) * 1000
        try:
            info = future.result()
        except Exception as exc:  # noqa: BLE001, RUF100 - doubt passes
            logger.debug("[WAKE_VERIFY] Whisper failed: %s", exc)
            return _doubt("error", tail_ms=tail_ms, whisper_ms=whisper_ms)
        verdict = decide(info, cfg, low_score=low_score, require_token=require_token)
        return _result(
            verdict.passed,
            verdict.reason,
            transcript=info.text,
            token=verdict.token,
            tail_ms=tail_ms,
            whisper_ms=whisper_ms,
            no_speech_prob=info.no_speech_prob,
            avg_logprob=info.avg_logprob,
        )


# ---------------------------------------------------------------------------
# Process-wide access and the kill switch
# ---------------------------------------------------------------------------

_verifier: WakeVerifier | None = None
_verifier_lock = threading.Lock()


def get_wake_verifier() -> WakeVerifier:
    global _verifier
    with _verifier_lock:
        if _verifier is None:
            _verifier = WakeVerifier()
        return _verifier


def wake_verification_enabled() -> bool:
    """The switch: env override first, then the device setting (default on)."""
    override = os.environ.get(ENV_OVERRIDE, "").strip().lower()
    if override in {"0", "false", "off", "no"}:
        return False
    if override in {"1", "true", "on", "yes"}:
        return True
    try:
        from ui.settings_manager import get_settings_manager

        value = get_settings_manager().get(SETTING_KEY, True)
    except Exception as exc:  # noqa: BLE001, RUF100 - unreadable settings keep the default
        logger.debug("[WAKE_VERIFY] could not read %s: %s", SETTING_KEY, exc)
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no"}
    return bool(value) if value is not None else True


def warm_wake_verifier() -> None:
    """Start loading the verifier model in the background when the check is on."""
    if wake_verification_enabled():
        get_wake_verifier().warm_async()


__all__ = [
    "ChunkReader",
    "EarlyDecode",
    "EarlyStartTracker",
    "GateDecision",
    "TranscriptInfo",
    "VerificationResult",
    "VerifierConfig",
    "WakeChunk",
    "WakeVerifier",
    "decide",
    "find_viola_token",
    "get_wake_verifier",
    "wake_verification_enabled",
    "warm_wake_verifier",
]
