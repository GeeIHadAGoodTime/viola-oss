"""Ring a fired alarm until the user stops it (or a safety limit is reached).

Why this exists (1.0.5 laptop report, X1.3): an alarm used to fire as a single
TTS line. At 09:10:07 the scheduler ran "set an alarm for 9:10 am", Viola said
"Alarm alarm" for about three seconds, and that was all: no ringing tone, no
card, nothing to stop. Anyone not already standing next to the PC missed it.

An alarm now behaves like an alarm clock:

* a generated beep pattern (no audio asset) repeats on the speaker until the
  user says "stop", taps Stop on the alarm card, or cancels the alarm;
* it gives up on its own after ``MAX_RING_SECONDS`` so a PC left alone does not
  beep forever;
* the desktop UI is told over the EventHub (``alarm_ringing`` when it starts,
  ``alarm_stopped`` when it ends, with the reason) so it can show the card and
  take it down again, and ``active_rings`` lets a webview that connects late
  show a ring that is already going.

The ringer is per user: every ring belongs to the user whose alarm fired, every
stop names a user, and a stop for one user never touches another user's ring.
It is desktop-only; the cloud surface has no speaker and never starts one.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from core.logging_config import get_logger

logger = get_logger(__name__)

ALARM_RINGING_EVENT = "alarm_ringing"
ALARM_STOPPED_EVENT = "alarm_stopped"

# Ten minutes, the usual alarm-clock and phone default before an alarm silences
# itself. Long enough to wake someone, short enough that a PC left alone does
# not beep for the rest of the day.
MAX_RING_SECONDS = 10 * 60

RING_SAMPLE_RATE = 24000
# Peak amplitude of the beeps (about -9 dBFS). Louder than the wake chime and
# close to Viola's speech level: this is meant to get attention.
RING_PEAK = 0.35
_BEEP_HZ = 880.0
_BEEP_SECONDS = 0.11
_BEEP_GAP_SECONDS = 0.07
_BEEPS_PER_BURST = 4
# Silence between bursts. The stop flag is checked during it, so a stop lands
# within one burst (well under a second).
_BURST_PAUSE_SECONDS = 0.65

StopReason = str  # "stopped" | "timeout" | "cancelled" | "replaced" | "playback_failed"


def generate_ring_burst(sample_rate: int = RING_SAMPLE_RATE) -> np.ndarray:
    """Return one burst of the alarm pattern (four short beeps) as mono float32."""
    beep_len = int(sample_rate * _BEEP_SECONDS)
    t = np.arange(beep_len, dtype=np.float32) / float(sample_rate)
    edge = max(1, int(sample_rate * 0.005))
    envelope = np.ones(beep_len, dtype=np.float32)
    envelope[:edge] = np.linspace(0.0, 1.0, edge, dtype=np.float32)
    envelope[-edge:] = np.linspace(1.0, 0.0, edge, dtype=np.float32)
    beep = (np.sin(2.0 * np.pi * _BEEP_HZ * t).astype(np.float32) * envelope).astype(np.float32)
    gap = np.zeros(int(sample_rate * _BEEP_GAP_SECONDS), dtype=np.float32)
    parts: list[np.ndarray] = []
    for index in range(_BEEPS_PER_BURST):
        parts.append(beep)
        if index < _BEEPS_PER_BURST - 1:
            parts.append(gap)
    burst = np.concatenate(parts)
    peak = float(np.max(np.abs(burst))) or 1.0
    return (burst / peak * RING_PEAK).astype(np.float32)


def _play_on_speaker(samples: np.ndarray, sample_rate: int) -> bool:
    """Play ``samples`` on the hub speaker and wait for them. Returns whether they played."""
    try:
        from audio_core.device_validation import resolve_output_device
        from audio_core.portaudio_guard import sounddevice_guard, sounddevice_playback_guard

        with sounddevice_guard():
            import sounddevice as sd

        with sounddevice_playback_guard():
            try:
                from audio_core.aec_reference_base import push_local_playback_reference

                # The wake word must still hear "Viola, stop" over the alarm.
                push_local_playback_reference(samples, sample_rate)
            except Exception:  # noqa: BLE001, RUF100 - the reference tap must never stop the alarm
                logger.debug("Alarm AEC reference push failed", exc_info=True)
            with sd.OutputStream(
                samplerate=sample_rate,
                device=resolve_output_device(sd),
                channels=1,
                dtype="float32",
            ) as stream:
                stream.write(samples.reshape(-1, 1))
        return True
    except ImportError:
        logger.warning("sounddevice is not available; the alarm cannot ring")
        return False
    except Exception:  # noqa: BLE001, RUF100 - one failed burst must not crash the ringer thread
        logger.warning("Alarm burst playback failed", exc_info=True)
        return False


def _default_emit(user_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Broadcast an alarm event to this user's webview clients. Never raises."""
    try:
        from services.notifications.timer_notifier import _default_hub, _default_loop

        hub = _default_hub()
        loop = _default_loop()
        if hub is None or loop is None or not loop.is_running():
            logger.debug("Alarm %s broadcast skipped; EventHub/loop unavailable", event_type)
            return
        asyncio.run_coroutine_threadsafe(hub.broadcast_to_user(user_id, event_type, payload), loop)
    except Exception:  # noqa: BLE001, RUF100 - a UI broadcast failure must not stop the ring
        logger.warning("Alarm %s broadcast failed for user=%s", event_type, user_id, exc_info=True)


@dataclass
class _Ring:
    user_id: str
    alarm_id: str
    label: str
    started_at: datetime
    started_monotonic: float
    max_seconds: float
    stop_event: threading.Event = field(default_factory=threading.Event)
    stop_reason: StopReason = "timeout"
    thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "alarm_id": self.alarm_id,
            "label": self.label,
            "message": alarm_message(self.label),
            "started_at": self.started_at.isoformat(),
            "max_seconds": int(self.max_seconds),
        }


def alarm_message(label: str) -> str:
    """The words for a ringing alarm: "Your alarm is going off." / "Your Wake up alarm is going off."."""
    clean = " ".join((label or "").split())
    if not clean or clean.lower() == "alarm":
        return "Your alarm is going off."
    if clean.lower().endswith("alarm"):
        return "Your %s is going off." % clean
    return "Your %s alarm is going off." % clean


class AlarmRinger:
    """Process-wide owner of the alarms that are ringing right now."""

    def __init__(
        self,
        *,
        play: Callable[[np.ndarray, int], bool] | None = None,
        emit: Callable[[str, str, dict[str, Any]], None] | None = None,
        max_seconds: float = MAX_RING_SECONDS,
        burst_pause_seconds: float = _BURST_PAUSE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._play = play or _play_on_speaker
        self._emit = emit or _default_emit
        self._max_seconds = float(max_seconds)
        self._burst_pause_seconds = float(burst_pause_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._rings: dict[str, _Ring] = {}  # keyed by user_id: one ring per user at a time
        self._burst: np.ndarray | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self, user_id: str, alarm_id: str | int, label: str = "Alarm") -> bool:
        """Start ringing ``label`` for ``user_id``. Returns False when it cannot start."""
        uid = str(user_id or "").strip()
        if not uid:
            logger.warning("Alarm ring refused: no user_id")
            return False
        ring = _Ring(
            user_id=uid,
            alarm_id=str(alarm_id),
            label=" ".join(str(label or "Alarm").split()) or "Alarm",
            started_at=datetime.now(UTC),
            started_monotonic=self._clock(),
            max_seconds=self._max_seconds,
        )
        with self._lock:
            previous = self._rings.pop(uid, None)
            self._rings[uid] = ring
        if previous is not None:
            # A second alarm firing while one rings takes over the speaker and
            # the card; the first one ends as "replaced".
            self._finish(previous, "replaced")
        thread = threading.Thread(target=self._run, args=(ring,), daemon=True, name="alarm-ringer")
        ring.thread = thread
        logger.info(
            "Alarm ringing: id=%s label=%r user=%s (auto-stop after %ds)",
            ring.alarm_id,
            ring.label,
            uid,
            int(self._max_seconds),
        )
        self._emit(uid, ALARM_RINGING_EVENT, ring.snapshot())
        thread.start()
        return True

    def stop(self, user_id: str, alarm_id: str | int | None = None, *, reason: StopReason = "stopped") -> int:
        """Stop this user's ringing alarm (only the named one when ``alarm_id`` is given).

        Returns how many rings were stopped (0 or 1). Never touches another user's ring.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        wanted = None if alarm_id in (None, "") else str(alarm_id)
        with self._lock:
            ring = self._rings.get(uid)
            if ring is None or (wanted is not None and ring.alarm_id != wanted):
                return 0
            self._rings.pop(uid, None)
        self._finish(ring, reason)
        return 1

    def is_ringing(self, user_id: str) -> bool:
        with self._lock:
            return str(user_id or "").strip() in self._rings

    def active_rings(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            ring = self._rings.get(str(user_id or "").strip())
            return [ring.snapshot()] if ring is not None else []

    def stop_all(self, *, reason: StopReason = "stopped") -> int:
        """Stop every ring (process shutdown only)."""
        with self._lock:
            rings = list(self._rings.values())
            self._rings.clear()
        for ring in rings:
            self._finish(ring, reason)
        return len(rings)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _burst_samples(self) -> np.ndarray:
        if self._burst is None:
            self._burst = generate_ring_burst(RING_SAMPLE_RATE)
        return self._burst

    def _finish(self, ring: _Ring, reason: StopReason) -> None:
        with self._lock:
            if ring.stop_event.is_set():
                return
            ring.stop_reason = reason
            ring.stop_event.set()
        elapsed = max(0.0, self._clock() - ring.started_monotonic)
        logger.info("Alarm stopped: id=%s reason=%s after %.0fs", ring.alarm_id, reason, elapsed)
        self._emit(
            ring.user_id,
            ALARM_STOPPED_EVENT,
            {"alarm_id": ring.alarm_id, "label": ring.label, "reason": reason, "rang_seconds": int(elapsed)},
        )

    def _run(self, ring: _Ring) -> None:
        failures = 0
        while not ring.stop_event.is_set():
            if self._clock() - ring.started_monotonic >= ring.max_seconds:
                self._end_from_thread(ring, "timeout")
                return
            if self._play(self._burst_samples(), RING_SAMPLE_RATE):
                failures = 0
            else:
                failures += 1
                if failures >= 3:
                    # No speaker at all: stop pretending to ring, but the card
                    # and the spoken line already told the user.
                    self._end_from_thread(ring, "playback_failed")
                    return
            if ring.stop_event.wait(self._burst_pause_seconds):
                return

    def _end_from_thread(self, ring: _Ring, reason: StopReason) -> None:
        with self._lock:
            if self._rings.get(ring.user_id) is ring:
                self._rings.pop(ring.user_id, None)
        self._finish(ring, reason)


_ringer: AlarmRinger | None = None
_ringer_lock = threading.Lock()


def get_alarm_ringer() -> AlarmRinger:
    global _ringer
    with _ringer_lock:
        if _ringer is None:
            _ringer = AlarmRinger()
        return _ringer


def stop_ringing_alarm(user_id: str, alarm_id: str | int | None = None, *, reason: StopReason = "stopped") -> int:
    """Stop ``user_id``'s ringing alarm if there is one. Never raises; returns how many stopped."""
    try:
        with _ringer_lock:
            ringer = _ringer
        if ringer is None:
            return 0
        return ringer.stop(user_id, alarm_id, reason=reason)
    except Exception:  # noqa: BLE001, RUF100 - a stop path must never break the command that called it
        logger.warning("Stopping the ringing alarm failed for user=%s", user_id, exc_info=True)
        return 0


__all__ = [
    "ALARM_RINGING_EVENT",
    "ALARM_STOPPED_EVENT",
    "MAX_RING_SECONDS",
    "AlarmRinger",
    "alarm_message",
    "generate_ring_burst",
    "get_alarm_ringer",
    "stop_ringing_alarm",
]
