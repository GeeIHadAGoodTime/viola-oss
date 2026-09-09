"""
False Positive Audio Collector for ViolaWake Model Retraining.

Captures pre-normalization, post-AEC audio buffers when the model produces
high scores during music playback. Collected audio serves as hard negative
training data for reducing false positives.

Architecture:
    - Marker file (logs/wake_audio/COLLECTING) controls collection state
    - Check is a single os.path.exists() per inference frame (~0 overhead)
    - WAV writes happen in a background thread (never blocks inference)
    - Rate-limited to 1 capture per second to prevent disk fill

Privacy:
    - Collection is disabled by default (wake_audio_logging_enabled=False)
    - Automatic retention cleanup deletes files older than configured hours
    - Cleanup runs on startup and periodically during runtime

Capture thresholds:
    - score >= 0.80: saved to logs/wake_audio/triggers/
    - score >= 0.35: saved to logs/wake_audio/near_misses/
    - score < 0.35: ignored
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

from core.logging_config import get_logger
from core.platform import get_logs_dir
from violawake.config import SAMPLE_RATE

logger = get_logger(__name__)

# Directories
_BASE_DIR = get_logs_dir() / "wake_audio"
_MARKER_FILE = _BASE_DIR / "COLLECTING"
_TRIGGERS_DIR = _BASE_DIR / "triggers"
_NEAR_MISSES_DIR = _BASE_DIR / "near_misses"

# Thresholds (raised to 0.80 for temporal_cnn model — 2026-03-27)
TRIGGER_THRESHOLD = 0.80
NEAR_MISS_THRESHOLD = 0.35

# Rate limit: 1 capture per second
_MIN_CAPTURE_INTERVAL = 1.0


def _get_retention_hours() -> int:
    """Read wake_audio_retention_hours from user settings."""
    try:
        from ui.settings_manager import get_settings_manager

        mgr = get_settings_manager()
        return int(mgr.get("wake_audio_retention_hours", 24))
    except Exception:
        return 24


def _is_logging_enabled() -> bool:
    """Check if wake audio logging is enabled in user settings."""
    try:
        from ui.settings_manager import get_settings_manager

        mgr = get_settings_manager()
        return bool(mgr.get("wake_audio_logging_enabled", False))
    except Exception:
        # Fail closed: default to disabled
        return False


def cleanup_old_audio(retention_hours: int | None = None) -> int:
    """Delete audio files older than the retention period.

    Args:
        retention_hours: Override retention period (None = read from settings).

    Returns:
        Number of files deleted.
    """
    if retention_hours is None:
        retention_hours = _get_retention_hours()

    if retention_hours <= 0:
        return 0

    cutoff = time.time() - (retention_hours * 3600)
    deleted = 0

    for search_dir in (_TRIGGERS_DIR, _NEAR_MISSES_DIR):
        if not search_dir.exists():
            continue
        for wav_file in search_dir.glob("*.wav"):
            try:
                if wav_file.stat().st_mtime < cutoff:
                    wav_file.unlink()
                    deleted += 1
            except OSError as e:
                logger.debug("[FP_COLLECTOR] Could not delete %s: %s", wav_file.name, e)

    if deleted > 0:
        logger.info(
            "[FP_COLLECTOR] Retention cleanup: deleted %d files older than %dh",
            deleted,
            retention_hours,
        )

    return deleted


class FPCollector:
    """
    Collects false positive audio for model retraining.

    Thread-safe. The check_and_save() method is called from the inference
    loop and must be fast. Actual file I/O is deferred to a background thread.
    """

    def __init__(self) -> None:
        self._last_capture_time: float = 0.0
        self._was_collecting: bool = False
        self._trigger_count: int = 0
        self._near_miss_count: int = 0
        self._total_bytes: int = 0
        self._lock = threading.Lock()

        # Periodic cleanup state
        self._last_cleanup_time: float = 0.0
        self._cleanup_interval: float = 3600.0  # 1 hour

        # Run cleanup on initialization
        threading.Thread(target=self._startup_cleanup, daemon=True).start()

    def _startup_cleanup(self) -> None:
        """Run retention cleanup on startup (background thread)."""
        try:
            deleted = cleanup_old_audio()
            if deleted > 0:
                logger.info("[FP_COLLECTOR] Startup cleanup: removed %d expired files", deleted)
            self._last_cleanup_time = time.time()
        except Exception as e:
            logger.debug("[FP_COLLECTOR] Startup cleanup failed: %s", e)

    @property
    def is_collecting(self) -> bool:
        """Check if collection is active (marker file exists AND logging enabled)."""
        if not _is_logging_enabled():
            return False
        return _MARKER_FILE.exists()

    def check_and_save(
        self,
        score: float,
        audio_buffer: np.ndarray,
        ref_buffer: np.ndarray | None = None,
    ) -> None:
        """
        Check if collection is active and save audio if score meets threshold.

        Called once per inference frame. Fast path: single stat() + threshold check.

        Args:
            score: Model inference score (0.0 to 1.0)
            audio_buffer: Pre-normalization, post-AEC float32 audio (CLIP_SAMPLES)
            ref_buffer: Loopback reference float32 audio (same length), or None
        """
        # Periodic cleanup check (cheap: just a time comparison)
        now = time.time()
        if now - self._last_cleanup_time > self._cleanup_interval:
            self._last_cleanup_time = now
            threading.Thread(target=cleanup_old_audio, daemon=True).start()

        if not self.is_collecting:
            if self._was_collecting:
                logger.info("[FP_COLLECTOR] Stopped")
                self._was_collecting = False
            return

        if not self._was_collecting:
            logger.info("[FP_COLLECTOR] Started")
            self._was_collecting = True

        # Below near-miss threshold — nothing to save
        if score < NEAR_MISS_THRESHOLD:
            return

        # Rate limit
        if now - self._last_capture_time < _MIN_CAPTURE_INTERVAL:
            return
        self._last_capture_time = now

        # Determine target directory
        if score >= TRIGGER_THRESHOLD:
            target_dir = _TRIGGERS_DIR
            label = "trigger"
        else:
            target_dir = _NEAR_MISSES_DIR
            label = "near_miss"

        # Copy buffers before handing off to background thread
        audio_copy = audio_buffer.copy()
        ref_copy = ref_buffer.copy() if ref_buffer is not None else None

        # Fire-and-forget background write
        thread = threading.Thread(
            target=self._write_wav,
            args=(target_dir, score, audio_copy, ref_copy, label),
            daemon=True,
        )
        thread.start()

    def _write_wav(
        self,
        target_dir: Path,
        score: float,
        audio: np.ndarray,
        ref: np.ndarray | None,
        label: str,
    ) -> None:
        """Write WAV file(s) in background thread. Never raises."""
        try:
            target_dir.mkdir(parents=True, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            basename = f"{timestamp}_{score:.3f}"

            # Save mic/post-AEC audio
            mic_path = target_dir / f"{basename}.wav"
            self._save_float32_wav(mic_path, audio)

            file_size = mic_path.stat().st_size

            # Save reference if available
            if ref is not None:
                ref_path = target_dir / f"{basename}_ref.wav"
                self._save_float32_wav(ref_path, ref)
                file_size += ref_path.stat().st_size

            # Update counters
            with self._lock:
                if label == "trigger":
                    self._trigger_count += 1
                else:
                    self._near_miss_count += 1
                self._total_bytes += file_size

            logger.debug(
                "[FP_COLLECTOR] Saved %s: %s (score=%.3f, size=%d)",
                label,
                mic_path.name,
                score,
                file_size,
            )

        except Exception as e:
            logger.warning("[FP_COLLECTOR] Write failed: %s", e)

    @staticmethod
    def _save_float32_wav(path: Path, audio: np.ndarray) -> None:
        """Save float32 audio as 16-bit WAV."""
        # Clip to [-1, 1] then convert to int16
        clipped = np.clip(audio, -1.0, 1.0)
        int16_data = (clipped * 32767).astype(np.int16)

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(int16_data.tobytes())

    def get_stats(self) -> dict:
        """Return current collection statistics."""
        with self._lock:
            return {
                "collecting": self.is_collecting,
                "logging_enabled": _is_logging_enabled(),
                "retention_hours": _get_retention_hours(),
                "triggers": self._trigger_count,
                "near_misses": self._near_miss_count,
                "total_bytes": self._total_bytes,
            }


# Module-level singleton
_instance: FPCollector | None = None
_instance_lock = threading.Lock()


def get_fp_collector() -> FPCollector:
    """Get or create the global FP collector singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = FPCollector()
    return _instance
