"""Playback metrics recorder encapsulating counters, errors, and durations."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable


class PlaybackMetricsRecorder:
    """Small helper that centralizes playback metrics bookkeeping."""

    def __init__(self, *, max_duration_samples: int = 1000) -> None:
        self._counters: dict[str, int] = {
            "viola_play_requests_total": 0,
            "viola_tracks_completed_total": 0,
        }
        self._errors: dict[str, int] = {}
        self._durations: deque[float] = deque(maxlen=max_duration_samples)
        self._progress: deque[tuple[int, int]] = deque(maxlen=max_duration_samples)

    # ------------------------------------------------------------------ #
    # Counter helpers
    # ------------------------------------------------------------------ #

    def inc_counter(self, key: str, n: int = 1) -> None:
        """Increment a success/throughput counter."""
        self._counters[key] = self._counters.get(key, 0) + n

    def inc_error(self, key: str, n: int = 1) -> None:
        """Increment an error/failure counter."""
        self._errors[key] = self._errors.get(key, 0) + n

    # ------------------------------------------------------------------ #
    # Duration helpers
    # ------------------------------------------------------------------ #

    def record_duration(self, seconds: float) -> None:
        """Record a track completion duration sample."""
        self._durations.append(seconds)

    def record_progress(self, *, position_ms: int, duration_ms: int) -> None:
        """Record a playback progress sample without logging on every tick."""
        self._progress.append((max(0, int(position_ms)), max(0, int(duration_ms))))

    # ------------------------------------------------------------------ #
    # Snapshots (for diagnostics/tests)
    # ------------------------------------------------------------------ #

    @property
    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    @property
    def errors(self) -> dict[str, int]:
        return dict(self._errors)

    @property
    def durations(self) -> tuple[float, ...]:
        return tuple(self._durations)

    @property
    def progress(self) -> tuple[tuple[int, int], ...]:
        return tuple(self._progress)

    def reset(self, *, counters: Iterable[str] | None = None) -> None:
        """
        Reset a subset of counters/errors (used by tests).

        Args:
            counters: Iterable of counter keys to reset. If omitted, nothing is reset.
        """
        if not counters:
            return
        for key in counters:
            if key in self._counters:
                self._counters[key] = 0
            if key in self._errors:
                self._errors[key] = 0
