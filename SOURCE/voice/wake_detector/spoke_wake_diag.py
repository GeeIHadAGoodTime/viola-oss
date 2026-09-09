"""
Wake Word Diagnostics — instrumentation for both hub and spoke inference.

Captures near-misses, false positives, and audio characteristics from hub
microphone and spoke voice streams during music playback. Read-only
observation — does NOT alter detection behavior.

Activation:
    Set environment variable  VIOLA_SPOKE_WAKE_DIAG=1  before starting Viola.
    When disabled (default), the public API methods return immediately with
    zero overhead (a single bool check per call).

JSONL log:
    - Every inference (hub and spoke) logged to logs/spoke_wake_diag/scores.jsonl
    - Fields: ts, source (hub/spoke), room, score, rms, triggered
    - Capped at 50 MB (oldest half truncated). Buffered writes.

Audio saves:
    - score >= 0.30: WAV saved to logs/spoke_wake_diag/
    - Hub filename: score{SCORE}_spokehub_{TIMESTAMP}.wav
    - Spoke filename: score{SCORE}_spoke{ROOM}_{TIMESTAMP}.wav
    - Capped at MAX_FILES (500). Oldest deleted when full.
    - Writes happen in a background thread (never blocks inference).

Follows the same architectural pattern as fp_collector.py.
"""

from __future__ import annotations

import json
import os
import threading
import time
import wave
from datetime import UTC, datetime

import numpy as np

from core.logging_config import get_logger
from core.platform import get_logs_dir
from violawake.config import SAMPLE_RATE, SILENCE_GATE_RMS

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DIAG_DIR = get_logs_dir() / "spoke_wake_diag"
_MAX_FILES = 500

# Score thresholds (observation only — detection threshold stays at 0.80)
_LOG_SCORE_THRESHOLD = 0.20  # Log every inference above this
_SAVE_SCORE_THRESHOLD = 0.30  # Save WAV above this
_TRIGGER_THRESHOLD = 0.80  # Actual trigger threshold (read-only reference)

# Running stats interval
_STATS_INTERVAL_S = 30.0

# Rate limit WAV saves: at most 1 per second per spoke
_MIN_SAVE_INTERVAL_S = 1.0


def _is_enabled() -> bool:
    """Check VIOLA_SPOKE_WAKE_DIAG env var (checked once, cached)."""
    return os.environ.get("VIOLA_SPOKE_WAKE_DIAG", "0") == "1"


# Module-level flag — evaluated once at import time for zero-cost fast path
DIAG_ENABLED: bool = _is_enabled()

# JSONL score log limits
_JSONL_PATH = _DIAG_DIR / "scores.jsonl"
_JSONL_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_JSONL_FLUSH_LINES = 10
_JSONL_FLUSH_INTERVAL_S = 5.0


# ---------------------------------------------------------------------------
# Buffered JSONL score log (shared across all spoke sessions)
# ---------------------------------------------------------------------------


class _ScoreLog:
    """Append-only JSONL logger with buffered writes and size cap.

    Thread-safe.  Flushes every ``_JSONL_FLUSH_LINES`` lines **or**
    ``_JSONL_FLUSH_INTERVAL_S`` seconds, whichever comes first.
    When the file exceeds ``_JSONL_MAX_BYTES`` the oldest half is
    truncated.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        _DIAG_DIR.mkdir(parents=True, exist_ok=True)

    def append(self, source: str, room: str, score: float, rms: float, triggered: bool) -> None:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        line = json.dumps(
            {
                "ts": ts,
                "source": source,
                "room": room,
                "score": round(score, 6),
                "rms": round(rms, 6),
                "triggered": triggered,
            },
            separators=(",", ":"),
        )
        with self._lock:
            self._buf.append(line)
            now = time.monotonic()
            if len(self._buf) >= _JSONL_FLUSH_LINES or (now - self._last_flush) >= _JSONL_FLUSH_INTERVAL_S:
                self._flush_locked()
                self._last_flush = now

    # Must be called with self._lock held.
    def _flush_locked(self) -> None:
        if not self._buf:
            return
        data = "\n".join(self._buf) + "\n"
        self._buf.clear()
        try:
            with open(_JSONL_PATH, "a", encoding="utf-8") as f:
                f.write(data)
            # Check size cap
            if _JSONL_PATH.stat().st_size > _JSONL_MAX_BYTES:
                self._truncate_locked()
        except Exception as e:
            logger.debug("[SPOKE_DIAG] JSONL write failed: %s", e)

    def _truncate_locked(self) -> None:
        """Drop the oldest half of the file to stay under the cap."""
        try:
            raw = _JSONL_PATH.read_bytes()
            mid = len(raw) // 2
            # Find the next newline after midpoint so we don't split a line
            nl = raw.index(b"\n", mid)
            _JSONL_PATH.write_bytes(raw[nl + 1 :])
            logger.info(
                "[SPOKE_DIAG] Truncated scores.jsonl: %d -> %d bytes",
                len(raw),
                len(raw) - nl - 1,
            )
        except Exception as e:
            logger.debug("[SPOKE_DIAG] JSONL truncate failed: %s", e)

    def flush(self) -> None:
        """Force-flush the buffer (call on session disconnect)."""
        with self._lock:
            self._flush_locked()


# Module-level singleton — created only when diag is enabled.
_score_log: _ScoreLog | None = _ScoreLog() if DIAG_ENABLED else None


# ---------------------------------------------------------------------------
# SpokeDiagnostics
# ---------------------------------------------------------------------------


class SpokeDiagnostics:
    """Per-spoke diagnostic state. One instance per _SpokeVoiceSession."""

    def __init__(self, room: str) -> None:
        self.room = room

        # Running stats (reset every _STATS_INTERVAL_S)
        self._stats_start = time.monotonic()
        self._frame_count = 0
        self._infer_count = 0
        self._max_score = 0.0
        self._above_030_count = 0
        self._trigger_count = 0

        # Cumulative stats
        self._total_triggers = 0
        self._total_near_misses = 0
        self._total_inferences = 0

        # RMS tracking
        self._rms_sum = 0.0
        self._rms_count = 0
        self._rms_max = 0.0
        self._rms_above_silence = 0  # frames with RMS > SILENCE_GATE_RMS
        self._latest_rms = 0.0  # most recent frame RMS, used by JSONL log

        # Per-spoke save rate limit
        self._last_save_time = 0.0

        # Ensure output directory exists
        _DIAG_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(
            "[SPOKE_DIAG] Diagnostics active for room=%s, saving to %s",
            room,
            _DIAG_DIR,
        )

    def record_frame_rms(self, rms: float) -> None:
        """Record RMS of an incoming spoke audio frame.

        Called for every binary frame, before inference gating.
        """
        self._frame_count += 1
        self._latest_rms = rms
        self._rms_sum += rms
        self._rms_count += 1
        if rms > self._rms_max:
            self._rms_max = rms

        if rms > SILENCE_GATE_RMS:
            self._rms_above_silence += 1

    def record_inference(
        self,
        score: float,
        audio_buffer: np.ndarray,
        triggered: bool,
    ) -> None:
        """Record an inference result.

        Called after every engine.process_audio() call.

        Args:
            score: Wake word probability [0.0, 1.0]
            audio_buffer: The contiguous float32 buffer that was inferred on
            triggered: Whether this score actually triggered a wake event
        """
        self._infer_count += 1
        self._total_inferences += 1

        # Log every inference to JSONL (full noise floor visibility)
        if _score_log is not None:
            _score_log.append("spoke", self.room, score, self._latest_rms, triggered)

        if score > self._max_score:
            self._max_score = score

        if score >= 0.30:
            self._above_030_count += 1

        if triggered:
            self._trigger_count += 1
            self._total_triggers += 1

        # Log scores above 0.20
        if score >= _LOG_SCORE_THRESHOLD:
            logger.info(
                "[SPOKE_DIAG] room=%s score=%.4f triggered=%s infer#=%d",
                self.room,
                score,
                triggered,
                self._total_inferences,
            )

        # Save WAV for scores above 0.30
        if score >= _SAVE_SCORE_THRESHOLD:
            self._total_near_misses += 1
            now = time.monotonic()
            if now - self._last_save_time >= _MIN_SAVE_INTERVAL_S:
                self._last_save_time = now
                # Copy buffer before handing to background thread
                buf_copy = audio_buffer.copy()
                label = "TRIGGER" if triggered else "near_miss"
                thread = threading.Thread(
                    target=_save_wav,
                    args=(self.room, score, buf_copy, label),
                    daemon=True,
                )
                thread.start()

        # Periodic stats summary
        elapsed = time.monotonic() - self._stats_start
        if elapsed >= _STATS_INTERVAL_S:
            self._emit_stats(elapsed)

    def _emit_stats(self, elapsed: float) -> None:
        """Log a 30-second stats summary and reset window counters."""
        avg_rms = self._rms_sum / self._rms_count if self._rms_count > 0 else 0.0
        pct_above_silence = 100.0 * self._rms_above_silence / self._rms_count if self._rms_count > 0 else 0.0
        logger.info(
            "[SPOKE_DIAG] room=%s STATS %.0fs: "
            "frames=%d inferences=%d max_score=%.4f "
            "above_030=%d triggers=%d "
            "avg_rms=%.5f max_rms=%.5f pct_above_silence=%.1f%%",
            self.room,
            elapsed,
            self._frame_count,
            self._infer_count,
            self._max_score,
            self._above_030_count,
            self._trigger_count,
            avg_rms,
            self._rms_max,
            pct_above_silence,
        )

        # Reset window counters
        self._stats_start = time.monotonic()
        self._frame_count = 0
        self._infer_count = 0
        self._max_score = 0.0
        self._above_030_count = 0
        self._trigger_count = 0
        self._rms_sum = 0.0
        self._rms_count = 0
        self._rms_max = 0.0
        self._rms_above_silence = 0

    def get_cumulative_stats(self) -> dict:
        """Return cumulative stats for this spoke session."""
        return {
            "room": self.room,
            "total_inferences": self._total_inferences,
            "total_triggers": self._total_triggers,
            "total_near_misses": self._total_near_misses,
        }


# ---------------------------------------------------------------------------
# WAV saving (runs in background thread)
# ---------------------------------------------------------------------------


def _save_wav(room: str, score: float, audio: np.ndarray, label: str) -> None:
    """Save a float32 audio buffer as a 16-bit WAV. Never raises."""
    try:
        # Enforce file cap
        _enforce_file_cap()

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        # Include milliseconds for uniqueness
        ms = int((time.time() % 1) * 1000)
        filename = f"score{score:.3f}_spoke{room}_{timestamp}_{ms:03d}.wav"
        path = _DIAG_DIR / filename

        # Convert float32 [-1,1] → int16
        clipped = np.clip(audio, -1.0, 1.0)
        int16_data = (clipped * 32767).astype(np.int16)

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(int16_data.tobytes())

        logger.debug(
            "[SPOKE_DIAG] Saved %s: %s (score=%.4f, %d bytes)",
            label,
            filename,
            score,
            path.stat().st_size,
        )
    except Exception as e:
        logger.warning("[SPOKE_DIAG] WAV write failed: %s", e)


_hub_last_save_time: float = 0.0


def log_hub_inference(
    score: float,
    rms: float,
    triggered: bool,
    audio_buffer: np.ndarray | None = None,
) -> None:
    """Log a hub-side inference result to the shared JSONL + optional WAV save.

    Called from ViolaWakeListener after every process_audio() call.
    Zero-cost when DIAG_ENABLED is False (caller should gate on the flag).

    If *audio_buffer* is provided and score >= 0.30, a WAV file is saved
    in a background thread (same policy as spoke saves).
    """
    global _hub_last_save_time

    if _score_log is not None:
        _score_log.append("hub", "hub", score, rms, triggered)

    # Save WAV for hub scores above the save threshold
    if audio_buffer is not None and score >= _SAVE_SCORE_THRESHOLD:
        now = time.monotonic()
        if now - _hub_last_save_time >= _MIN_SAVE_INTERVAL_S:
            _hub_last_save_time = now
            buf_copy = audio_buffer.copy()
            label = "TRIGGER" if triggered else "near_miss"
            thread = threading.Thread(
                target=_save_wav,
                args=("hub", score, buf_copy, label),
                daemon=True,
            )
            thread.start()


def flush_score_log() -> None:
    """Force-flush the JSONL buffer. Call on session disconnect."""
    if _score_log is not None:
        _score_log.flush()


def _enforce_file_cap() -> None:
    """Delete oldest files if directory exceeds _MAX_FILES."""
    try:
        wav_files = sorted(_DIAG_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        excess = len(wav_files) - _MAX_FILES
        if excess > 0:
            for old_file in wav_files[:excess]:
                old_file.unlink(missing_ok=True)
            logger.debug("[SPOKE_DIAG] Pruned %d oldest files (cap=%d)", excess, _MAX_FILES)
    except Exception as e:
        logger.debug("[SPOKE_DIAG] File cap enforcement failed: %s", e)
