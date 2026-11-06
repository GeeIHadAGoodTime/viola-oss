"""What Viola herself has just played, kept for a few seconds in real time.

The wake path needs to answer one question while she talks: "was that wake word
hers?" The echo canceller's reference cannot answer it. That reference is a
queue the canceller drains, so nothing is left to look back at, and on a laptop
speaker the canceller may remove nothing at all (1.0.5 build 9: ERLE -2.4 dB).

This module keeps a copy of what she played, on a clock: a gap in her speech is
silence here too, so the last N seconds of this buffer are what the speaker was
playing during the last N seconds. :mod:`voice.wake_detector.own_speech` scores
that audio with the wake model. It also keeps the sentences she was given to
say, so the follow-up recorder can recognise her own words.

Everything is best effort and never raises into playback.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)

# Long enough to score a wake window plus the model's warm-up.
RING_SECONDS = 6.0
# Sentences older than this cannot still be coming back through the microphone.
TEXT_KEEP_SECONDS = 45.0
# A pause shorter than this is the playback worker's own 10 ms pacing, not silence.
_SILENCE_GAP_S = 0.05
UTTERANCE_GAP_S = 1.5


@dataclass(frozen=True)
class SpokenText:
    """One piece of text handed to the speaker, and when."""

    text: str
    at: float


class OwnPlayback:
    """Thread-safe record of Viola's recent output audio and text."""

    def __init__(self, sample_rate: int = SAMPLE_RATE_16K, ring_seconds: float = RING_SECONDS) -> None:
        self._rate = int(sample_rate)
        self._size = int(ring_seconds * self._rate)
        self._ring = np.zeros(self._size, dtype=np.float32)
        self._write = 0
        self._lock = threading.Lock()
        self._last_audio_at: float | None = None
        # How far the ring has been written, on the clock (silence included).
        self._ring_at: float | None = None
        self._text: deque[SpokenText] = deque(maxlen=64)
        # When the current or last utterance began (a pause of UTTERANCE_GAP_S
        # or more starts a new one), for the turn timing log.
        self._utterance_started_at: float | None = None
        # Seconds between her last audio and the speech onset the recorder saw last.
        self.last_onset_gap_s: float | None = None
        # She was cut off: the user is talking now, so no quiet tail applies.
        self._tail_waived = False

    # -- writers ------------------------------------------------------------

    def _append_locked(self, samples: np.ndarray) -> None:
        count = len(samples)
        if count >= self._size:
            self._ring[:] = samples[-self._size :]
            self._write = 0
            return
        end = self._write + count
        if end <= self._size:
            self._ring[self._write : end] = samples
        else:
            first = self._size - self._write
            self._ring[self._write :] = samples[:first]
            self._ring[: count - first] = samples[first:]
        self._write = end % self._size

    def _advance_silence_locked(self, now: float) -> None:
        """Write the silence between the last audio and ``now`` so the ring stays on the clock."""
        last = self._ring_at
        if last is None:
            return
        gap = now - last
        if gap > _SILENCE_GAP_S:
            self._append_locked(np.zeros(min(self._size, int(gap * self._rate)), dtype=np.float32))
            self._ring_at = now

    def note_audio(self, samples: np.ndarray, sample_rate: int) -> None:
        """Record PCM that has just gone to the speaker (mono, float [-1, 1] or int16)."""
        data = np.asarray(samples)
        if data.size == 0 or sample_rate <= 0:
            return
        if data.ndim > 1:
            data = data.mean(axis=1)
        floats = (
            data.astype(np.float32, copy=False)
            if np.issubdtype(data.dtype, np.floating)
            else data.astype(np.float32) / 32768.0
        )
        if sample_rate != self._rate:
            count = max(1, round(len(floats) * self._rate / sample_rate))
            # Linear is enough here: the wake model's features are mel bands, and
            # this runs on the playback worker every 10 ms.
            floats = np.interp(
                np.linspace(0.0, len(floats) - 1.0, count), np.arange(len(floats), dtype=np.float64), floats
            ).astype(np.float32)
        now = time.monotonic()
        with self._lock:
            if self._last_audio_at is None or now - self._last_audio_at >= UTTERANCE_GAP_S:
                self._utterance_started_at = now
            self._advance_silence_locked(now)
            self._append_locked(floats)
            self._last_audio_at = now
            self._ring_at = now
            self._tail_waived = False

    def note_text(self, text: str) -> None:
        """Record text she is about to say (a sentence or a whole reply)."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        with self._lock:
            self._text.append(SpokenText(cleaned, time.monotonic()))

    # -- readers ------------------------------------------------------------

    def recent_audio(self, seconds: float) -> np.ndarray | None:
        """The last ``seconds`` she played, oldest first; None if she never played anything."""
        wanted = min(self._size, max(0, int(seconds * self._rate)))
        with self._lock:
            if self._last_audio_at is None:
                return None
            self._advance_silence_locked(time.monotonic())
            if wanted == 0:
                return np.zeros(0, dtype=np.float32)
            start = (self._write - wanted) % self._size
            if start < self._write:
                return self._ring[start : self._write].copy()
            return np.concatenate([self._ring[start:], self._ring[: self._write]])

    def seconds_since_audio(self) -> float:
        """Seconds since she last sent audio to the speaker (inf if never)."""
        with self._lock:
            last = self._last_audio_at
        return float("inf") if last is None else time.monotonic() - last

    def voice_tail_active(self, seconds: float) -> bool:
        """True within ``seconds`` of her last audio, unless she was cut off by the user."""
        with self._lock:
            if self._tail_waived or self._last_audio_at is None:
                return False
            return time.monotonic() - self._last_audio_at < seconds

    def waive_tail(self) -> None:
        """The user interrupted her: what the microphone hears next is the user."""
        with self._lock:
            self._tail_waived = True

    def utterance_started_at(self) -> float | None:
        """time.monotonic() when her current or last utterance began."""
        with self._lock:
            return self._utterance_started_at

    def note_speech_onset(self) -> None:
        """The command recorder heard speech start; remember how soon after her it was."""
        self.last_onset_gap_s = self.seconds_since_audio()

    def recent_text(self, seconds: float) -> str:
        """Everything she was given to say in the last ``seconds``, in order."""
        cutoff = time.monotonic() - seconds
        with self._lock:
            return " ".join(item.text for item in self._text if item.at >= cutoff)

    def reset(self) -> None:
        with self._lock:
            self._ring[:] = 0.0
            self._write = 0
            self._last_audio_at = None
            self._ring_at = None
            self._text.clear()
            self._utterance_started_at = None
            self.last_onset_gap_s = None


_own_playback = OwnPlayback()


def get_own_playback() -> OwnPlayback:
    return _own_playback


__all__ = ["OwnPlayback", "SpokenText", "get_own_playback"]
