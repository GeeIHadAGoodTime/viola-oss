"""Measure, while Viola plays, how far the microphone lags the echo reference.

The echo canceller's filter covers 150 ms after the moment the reference is
served. Everything that sits between "served" and "heard" must be taken out by
the delay it is given, and that delay is not a property of the room: the output
buffer alone is about 180 ms on Windows MME, and Viola's own speech is pushed to
the reference when the audio is handed to that buffer. With a delay the filter
cannot reach, the "cancelled" signal is the microphone plus whatever the filter
guessed. Offline, the real ViolaAEC with the reference 230 ms early gives ERLE
-0.2 dB and a real "Viola" scores 0.14; the same audio correctly aligned gives
ERLE 9.4 dB and 0.99. The 1.0.5 build-9 Surface logged ERLE -2.4 dB.

Nothing corrected this at run time. Start-up calibration ran once, needed loud
loopback audio, and fell back to a fixed 50 ms; the periodic passive calibration
passed its result to ``set_buffer_target``, which the reference adapters only
record, and it compared a reference window ending now with a microphone window
ending now, so the true lag was outside what it searched.

This tracker pairs each microphone frame with the reference frame served for it,
exactly as the canceller sees them, and cross-correlates a few seconds of the two
with phase weighting (a sharp peak on music and speech alike). Speech, music and
the chime reach the speaker by different routes, so the answer is kept per route.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from core.logging_config import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16000
WINDOW_S = 3.0  # how much paired audio one measurement uses
INTERVAL_S = 1.5  # time between measurements while audio plays
MAX_LAG_MS = 700.0  # longest microphone lag searched
MIN_LAG_MS = -100.0  # how far the echo may lead the reference (reported, not fixable by a delay)
# A peak this many times the correlation's RMS is a real echo path. Measured on
# speech and vocal music through a laptop path: true peaks 12-60, no echo at all
# (headphones) under 6.
MIN_PEAK_RATIO = 9.0
# Above this one measurement is enough (the first reply after start-up matters).
SURE_PEAK_RATIO = 16.0
AGREE_MS = 12.0  # two measurements this close agree
CHANGE_MS = 15.0
JUMP_MS = 40.0  # a change this big is believed only when two measurements agree  # smaller changes are left alone: the filter already covers them
# The filter is causal: leave it room for the direct path to land inside it.
LEAD_MS = 15.0
MIN_REFERENCE_RMS = 150.0  # int16; quieter playback says nothing about the path
MIN_ACTIVE_SHARE = 0.4  # share of the window that must hold playback


@dataclass(frozen=True)
class LagMeasurement:
    lag_ms: float
    peak_ratio: float


def measure_lag(mic: np.ndarray, reference: np.ndarray, sample_rate: int = SAMPLE_RATE) -> LagMeasurement | None:
    """How many ms ``mic`` lags ``reference`` (equal-length int16/float arrays), or None if no peak.

    Phase-weighted cross-correlation. Positive: the echo arrives after the
    reference frame was served.
    """
    count = min(len(mic), len(reference))
    if count < sample_rate // 2:
        return None
    a = np.asarray(reference[:count], dtype=np.float64)
    b = np.asarray(mic[:count], dtype=np.float64)
    a = a - a.mean()
    b = b - b.mean()
    size = 1
    while size < 2 * count:
        size *= 2
    spectrum = np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size))
    magnitude = np.abs(spectrum)
    floor = float(magnitude.mean()) * 1e-3 + 1e-12
    correlation = np.fft.irfft(spectrum / (magnitude + floor), size)
    max_lag = int(MAX_LAG_MS * sample_rate / 1000)
    min_lag = int(-MIN_LAG_MS * sample_rate / 1000)
    # Lags 0..max at the start of the circular result, negative lags at its end.
    search = np.concatenate([correlation[-min_lag:], correlation[: max_lag + 1]])
    index = int(np.argmax(np.abs(search)))
    rms = float(np.sqrt(np.mean(search**2))) + 1e-12
    ratio = abs(float(search[index])) / rms
    return LagMeasurement((index - min_lag) * 1000.0 / sample_rate, ratio)


class AECDelayTracker:
    """Feed it every (mic, served reference) frame; it says when the delay should change."""

    def __init__(self, window_s: float = WINDOW_S, interval_s: float = INTERVAL_S) -> None:
        self._window = int(window_s * SAMPLE_RATE)
        self._interval = int(interval_s * SAMPLE_RATE)
        self._mic: deque[np.ndarray] = deque()
        self._ref: deque[np.ndarray] = deque()
        self._samples = 0
        self._active = 0
        self._since_measure = self._interval
        self._route: str | None = None
        self._last: LagMeasurement | None = None
        # The delay measured for each route (ms of reference delay to apply).
        self._delays: dict[str, float] = {}
        self.current_ms: float | None = None

    def _clear(self) -> None:
        self._mic.clear()
        self._ref.clear()
        self._samples = 0
        self._active = 0
        self._last = None

    def known_delay(self, route: str) -> float | None:
        return self._delays.get(route)

    def push(self, mic: np.ndarray, reference: np.ndarray, route: str = "default") -> float | None:
        """Add one frame pair. Returns a new delay in ms when it should change, else None.

        ``route``: what is playing ("speech", "music"). Each route keeps its own
        answer, so a change of route can restore a known delay at once.
        """
        result: float | None = None
        if route != self._route:
            self._route = route
            self._clear()
            known = self._delays.get(route)
            if known is not None and (self.current_ms is None or abs(known - self.current_ms) >= CHANGE_MS):
                self.current_ms = known
                result = known
        reference = np.asarray(reference, dtype=np.int16)
        is_active = float(np.sqrt(np.mean(reference.astype(np.float32) ** 2))) >= MIN_REFERENCE_RMS
        self._mic.append(np.asarray(mic, dtype=np.int16).copy())
        self._ref.append(reference.copy())
        self._samples += len(reference)
        self._active += len(reference) if is_active else 0
        while self._samples - len(self._ref[0]) >= self._window:
            old = self._ref.popleft()
            self._mic.popleft()
            self._samples -= len(old)
            if float(np.sqrt(np.mean(old.astype(np.float32) ** 2))) >= MIN_REFERENCE_RMS:
                self._active -= len(old)
        self._since_measure += len(reference)
        if (
            self._samples < self._window
            or self._since_measure < self._interval
            or not is_active
            or self._active < MIN_ACTIVE_SHARE * self._window
        ):
            return result
        self._since_measure = 0
        return self._measure(route) or result

    def _measure(self, route: str) -> float | None:
        measured = measure_lag(np.concatenate(self._mic), np.concatenate(self._ref))
        previous, self._last = self._last, measured
        if measured is None or measured.peak_ratio < MIN_PEAK_RATIO:
            self._last = None
            return None
        agreed = previous is not None and abs(previous.lag_ms - measured.lag_ms) <= AGREE_MS
        if not agreed and measured.peak_ratio < SURE_PEAK_RATIO:
            return None
        delay = max(0.0, measured.lag_ms - LEAD_MS)
        if not agreed and self.current_ms is not None and abs(delay - self.current_ms) >= JUMP_MS:
            # Laptop log 2026-11-12: a delay settled at 12 ms jumped to 472 ms (and
            # 2 -> 60, 8 -> 93, 6 -> 224 ms) on one measurement, then came back on the
            # next, leaving the canceller misaligned for seconds each time. A jump
            # this big now needs two measurements that agree.
            return None
        if measured.lag_ms < 0:
            logger.warning(
                "[AEC_DELAY] echo arrives %.0f ms before the reference (%s); a reference delay cannot fix that",
                -measured.lag_ms,
                route,
            )
        self._delays[route] = delay
        if self.current_ms is not None and abs(delay - self.current_ms) < CHANGE_MS:
            return None
        logger.info(
            "[AEC_DELAY] %s: microphone lags the reference by %.0f ms (peak ratio %.0f); reference delay %s -> %.0f ms",
            route,
            measured.lag_ms,
            measured.peak_ratio,
            "unset" if self.current_ms is None else "%.0f ms" % self.current_ms,
            delay,
        )
        self.current_ms = delay
        return delay


__all__ = ["AECDelayTracker", "LagMeasurement", "measure_lag"]
