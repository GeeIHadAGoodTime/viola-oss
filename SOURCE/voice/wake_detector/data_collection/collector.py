"""Audio clip collector for wake word data collection.

Saves trigger and near-miss audio clips to disk and registers them
in the SQLite database. Handles rate-limiting for near-misses and
WAV file writing in background threads.
"""

from __future__ import annotations

import struct
import threading
import time
from pathlib import Path

import numpy as np

from config.settings import settings
from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

from .database import get_data_collection_db

logger = get_logger(__name__)

# 1.5 seconds at 16kHz = 24000 samples
CLIP_SAMPLES = int(1.5 * SAMPLE_RATE_16K)
_WAKE_DATA_UPLOAD_CONTROL = "wake_data_upload"


def wake_data_collection_allowed() -> bool:
    """Return whether passive wake clips may be collected for contribution.

    This is intentionally stricter than the low-level collector methods. It is
    used by the live listener before writing local clips for the contribution
    pipeline.
    """
    if not settings.wake_data_collection_enabled:
        return False

    try:
        from services.operator_controls import require_enabled

        decision = require_enabled(_WAKE_DATA_UPLOAD_CONTROL, action="wake_data_collection")
        if not decision.allowed:
            logger.debug("Wake data collection skipped: %s", decision.reason)
            return False
    except Exception:
        logger.exception("Wake data collection skipped: operator control check failed")
        return False

    try:
        from ui.settings_manager import get_settings_manager

        manager = get_settings_manager()
        opted_in = manager.get("wake_word_training_opt_in", None)
        if opted_in is None:
            opted_in = manager.get("wake_data_contribute", False)
        if not bool(opted_in):
            return False

        if bool(manager.get("use_custom_wake_word", False)):
            logger.debug("Wake data collection skipped: custom wake word is active")
            return False
        active_model = str(manager.get("wake_word_active_model", "") or "").strip()
        if active_model:
            logger.debug("Wake data collection skipped: non-default wake model is active")
            return False
        return True
    except Exception:
        logger.exception("Wake data collection skipped: user settings unavailable")
        return False


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int = SAMPLE_RATE_16K) -> None:
    """Write float32 audio to 16-bit mono WAV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Clip and convert to int16
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    raw = pcm.tobytes()

    num_channels = 1
    sample_width = 2  # 16-bit
    byte_rate = sample_rate * num_channels * sample_width
    block_align = num_channels * sample_width
    data_size = len(raw)

    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))  # chunk size
        f.write(struct.pack("<H", 1))  # PCM format
        f.write(struct.pack("<H", num_channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", sample_width * 8))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(raw)


def _trim_to_clip(audio: np.ndarray) -> np.ndarray:
    """Trim audio to CLIP_SAMPLES centered on peak amplitude."""
    if len(audio) <= CLIP_SAMPLES:
        # Pad with zeros if shorter
        padded = np.zeros(CLIP_SAMPLES, dtype=np.float32)
        padded[: len(audio)] = audio
        return padded

    peak_idx = int(np.argmax(np.abs(audio)))
    half = CLIP_SAMPLES // 2
    start = max(0, peak_idx - half)
    end = start + CLIP_SAMPLES

    if end > len(audio):
        end = len(audio)
        start = max(0, end - CLIP_SAMPLES)

    return audio[start:end].copy()


class ClipCollector:
    """Saves wake word audio clips to disk and database."""

    def __init__(self) -> None:
        self._data_dir = Path(settings.data_dir) / "wake_clips"
        self._last_near_miss_time = 0.0
        self._lock = threading.Lock()

    def save_trigger_clip(
        self,
        audio: np.ndarray,
        score: float,
        threshold: float,
    ) -> int:
        """Save a trigger clip (score >= threshold) to disk and DB.

        Returns the clip ID from the database.
        """
        trimmed = _trim_to_clip(audio)
        duration_ms = int(len(trimmed) / SAMPLE_RATE_16K * 1000)
        ts = time.time()

        rel_dir = "triggers"
        filename = "trigger_%d_%.3f.wav" % (int(ts * 1000), score)
        rel_path = "%s/%s" % (rel_dir, filename)
        full_path = self._data_dir / rel_path

        # Write WAV in background thread
        thread = threading.Thread(
            target=_write_wav,
            args=(full_path, trimmed),
            daemon=True,
        )
        thread.start()

        db = get_data_collection_db()
        clip_id = db.insert_clip(
            timestamp=ts,
            audio_path=rel_path,
            duration_ms=duration_ms,
            confidence_score=score,
            classification="pending",
            classification_method="pending",
        )
        logger.debug("Saved trigger clip %d: score=%.3f path=%s", clip_id, score, rel_path)
        return clip_id

    def save_near_miss_clip(
        self,
        audio: np.ndarray,
        score: float,
    ) -> int | None:
        """Save a near-miss clip if within score range and not rate-limited.

        Returns clip ID or None if rate-limited or out of range.
        """
        low = settings.wake_data_near_miss_low
        high = settings.wake_data_near_miss_high
        cooldown = settings.wake_data_near_miss_cooldown_sec

        if not (low <= score < high):
            return None

        now = time.time()
        with self._lock:
            if now - self._last_near_miss_time < cooldown:
                return None
            self._last_near_miss_time = now

        trimmed = _trim_to_clip(audio)
        duration_ms = int(len(trimmed) / SAMPLE_RATE_16K * 1000)

        rel_dir = "near_misses"
        filename = "nearmiss_%d_%.3f.wav" % (int(now * 1000), score)
        rel_path = "%s/%s" % (rel_dir, filename)
        full_path = self._data_dir / rel_path

        thread = threading.Thread(
            target=_write_wav,
            args=(full_path, trimmed),
            daemon=True,
        )
        thread.start()

        db = get_data_collection_db()
        clip_id = db.insert_clip(
            timestamp=now,
            audio_path=rel_path,
            duration_ms=duration_ms,
            confidence_score=score,
            classification="near_miss",
            classification_method="auto_score_range",
        )
        logger.debug("Saved near-miss clip %d: score=%.3f", clip_id, score)
        return clip_id


_collector: ClipCollector | None = None
_collector_lock = threading.Lock()


def get_clip_collector() -> ClipCollector:
    global _collector
    with _collector_lock:
        if _collector is None:
            _collector = ClipCollector()
        return _collector
