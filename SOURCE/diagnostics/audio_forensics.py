"""
Audio Forensics Logger
======================

Captures and stores audio samples around wake events for debugging
false positives and missed detections.

Usage:
    from diagnostics.audio_forensics import get_audio_forensics_logger

    logger = get_audio_forensics_logger()

    # Feed audio continuously
    logger.feed_audio(audio_samples)

    # Capture on events
    path = logger.capture_trigger(correlation_id, wake_score)

    # Mark false positives
    path = logger.mark_false_positive(correlation_id)

    # Mark missed detections
    path = logger.mark_missed_detection()
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from config.constants import AUDIO_SAMPLE_RATE
from core.constants import AUDIO_INT16_SCALE
from core.logging_config import get_logger
from core.platform import get_logs_dir

logger = get_logger(__name__)

# Optional soundfile import
try:
    import soundfile as sf

    SOUNDFILE_AVAILABLE = True
except ImportError:
    SOUNDFILE_AVAILABLE = False
    sf = None


@dataclass
class AudioCapture:
    """Captured audio segment with metadata."""

    timestamp: float
    audio: np.ndarray
    sample_rate: int
    event_type: str  # "trigger", "false_positive", "missed"
    correlation_id: str | None
    wake_score: float | None
    metadata: dict[str, Any]

    def get_duration_seconds(self) -> float:
        """Get audio duration in seconds."""
        return len(self.audio) / self.sample_rate


class AudioForensicsLogger:
    """
    Captures audio around wake events for post-hoc analysis.

    Maintains a rolling buffer of recent audio and saves segments
    when interesting events occur (triggers, false positives, missed detections).

    Features:
    - Rolling audio buffer for pre-event capture
    - Post-event capture with configurable duration
    - Rate limiting to prevent storage explosion
    - Metadata sidecar files for analysis
    """

    # Buffer 10 seconds of audio at 16kHz
    DEFAULT_BUFFER_SECONDS = 10.0
    SAMPLE_RATE = AUDIO_SAMPLE_RATE
    DEFAULT_OUTPUT_DIR = get_logs_dir() / "audio_forensics"

    def __init__(
        self,
        output_dir: Path | None = None,
        pre_event_seconds: float = 2.0,
        post_event_seconds: float = 1.0,
        buffer_seconds: float = DEFAULT_BUFFER_SECONDS,
        max_captures_per_hour: int = 50,
    ):
        """
        Initialize audio forensics logger.

        Args:
            output_dir: Directory for captured audio files
            pre_event_seconds: Audio to capture before event
            post_event_seconds: Audio to capture after event
            buffer_seconds: Rolling buffer size in seconds
            max_captures_per_hour: Rate limit for captures
        """
        self._output_dir = Path(output_dir or self.DEFAULT_OUTPUT_DIR)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._pre_samples = int(pre_event_seconds * self.SAMPLE_RATE)
        self._post_samples = int(post_event_seconds * self.SAMPLE_RATE)
        buffer_samples = int(buffer_seconds * self.SAMPLE_RATE)

        self._lock = threading.Lock()
        self._buffer: deque[np.int16 | np.float32] = deque(maxlen=buffer_samples)

        # Rate limiting
        self._max_per_hour = max_captures_per_hour
        self._captures_this_hour: deque[float] = deque()

        # Post-event capture state
        self._pending_capture: dict | None = None
        self._pending_samples_remaining = 0
        self._post_buffer: list[np.ndarray] = []

        # Stats
        self._total_captures = 0
        self._captures_by_type: dict[str, int] = {}

        if not SOUNDFILE_AVAILABLE:
            logger.warning(
                "soundfile not available - audio forensics will not save WAV files. "
                "Install with: pip install soundfile"
            )

        logger.info("Audio forensics logger initialized: output_dir=%s", self._output_dir)

    def feed_audio(self, samples: np.ndarray) -> None:
        """
        Feed audio samples into the rolling buffer.

        Should be called continuously with incoming audio chunks.

        Args:
            samples: Audio samples (int16 or float32)
        """
        with self._lock:
            # Flatten and extend buffer
            flat_samples = samples.flatten()
            self._buffer.extend(flat_samples)

            # Handle pending post-event capture
            if self._pending_capture is not None:
                self._post_buffer.append(flat_samples.copy())
                self._pending_samples_remaining -= len(flat_samples)

                if self._pending_samples_remaining <= 0:
                    self._finalize_capture()

    def capture_trigger(
        self,
        correlation_id: str | None = None,
        wake_score: float | None = None,
        metadata: dict | None = None,
    ) -> Path | None:
        """
        Capture audio around a wake trigger event.

        Args:
            correlation_id: Correlation ID for this detection
            wake_score: Wake word score
            metadata: Additional metadata

        Returns:
            Path to saved audio file, or None if rate limited
        """
        return self._capture_event(
            event_type="trigger",
            correlation_id=correlation_id,
            wake_score=wake_score,
            metadata=metadata,
        )

    def mark_false_positive(
        self,
        correlation_id: str | None = None,
        metadata: dict | None = None,
    ) -> Path | None:
        """
        Mark a recent trigger as false positive and capture audio.

        Args:
            correlation_id: Correlation ID of the false positive
            metadata: Additional metadata

        Returns:
            Path to saved audio file, or None if rate limited
        """
        return self._capture_event(
            event_type="false_positive",
            correlation_id=correlation_id,
            metadata=metadata,
        )

    def mark_missed_detection(
        self,
        metadata: dict | None = None,
    ) -> Path | None:
        """
        Mark a missed detection (user said wake word but not detected).

        Args:
            metadata: Additional metadata

        Returns:
            Path to saved audio file, or None if rate limited
        """
        return self._capture_event(
            event_type="missed",
            metadata=metadata,
        )

    def capture_custom(
        self,
        event_type: str,
        correlation_id: str | None = None,
        wake_score: float | None = None,
        metadata: dict | None = None,
    ) -> Path | None:
        """
        Capture audio for a custom event type.

        Args:
            event_type: Custom event type name
            correlation_id: Optional correlation ID
            wake_score: Optional wake score
            metadata: Additional metadata

        Returns:
            Path to saved audio file, or None if rate limited
        """
        return self._capture_event(
            event_type=event_type,
            correlation_id=correlation_id,
            wake_score=wake_score,
            metadata=metadata,
        )

    def _capture_event(
        self,
        event_type: str,
        correlation_id: str | None = None,
        wake_score: float | None = None,
        metadata: dict | None = None,
    ) -> Path | None:
        """
        Internal method to capture audio around an event.

        Captures pre_event_seconds before event and waits for
        post_event_seconds after before saving.
        """
        if not SOUNDFILE_AVAILABLE:
            return None

        with self._lock:
            # Rate limiting
            now = time.time()
            hour_ago = now - 3600
            self._captures_this_hour = deque([t for t in self._captures_this_hour if t > hour_ago])

            if len(self._captures_this_hour) >= self._max_per_hour:
                logger.debug(
                    "Audio capture rate limited: %d/%d per hour",
                    len(self._captures_this_hour),
                    self._max_per_hour,
                )
                return None

            self._captures_this_hour.append(now)

            # Get pre-event audio from buffer
            pre_audio = np.array(list(self._buffer)[-self._pre_samples :], dtype=np.float32)

            # If buffer doesn't have enough samples, pad with zeros
            if len(pre_audio) < self._pre_samples:
                padding: np.ndarray = np.zeros(self._pre_samples - len(pre_audio), dtype=np.float32)
                pre_audio = np.concatenate([padding, pre_audio])

            # Set up pending capture for post-event audio
            self._pending_capture = {
                "timestamp": now,
                "event_type": event_type,
                "correlation_id": correlation_id,
                "wake_score": wake_score,
                "metadata": metadata or {},
                "pre_audio": pre_audio,
            }
            self._pending_samples_remaining = self._post_samples
            self._post_buffer = []

            # If no post-capture needed, finalize immediately
            if self._post_samples == 0:
                return self._finalize_capture()

            return None  # Will be finalized later

    def _finalize_capture(self) -> Path:
        """Finalize and save a pending capture."""
        if self._pending_capture is None:
            raise RuntimeError("No pending capture to finalize")

        capture = self._pending_capture
        self._pending_capture = None

        # Combine pre and post audio
        if self._post_buffer:
            post_audio = np.concatenate(self._post_buffer)[: self._post_samples]
        else:
            post_audio = np.zeros(self._post_samples, dtype=np.float32)

        full_audio = np.concatenate([capture["pre_audio"], post_audio])
        self._post_buffer = []

        # Normalize to float32 range [-1, 1]
        if full_audio.dtype == np.int16:
            full_audio = full_audio.astype(np.float32) / AUDIO_INT16_SCALE
        elif full_audio.dtype != np.float32:
            full_audio = full_audio.astype(np.float32)

        # Clip to valid range
        full_audio = np.clip(full_audio, -1.0, 1.0)

        # Generate filename
        ts = int(capture["timestamp"] * 1000)
        event_type = capture["event_type"]
        score = capture.get("wake_score", 0) or 0
        corr_id = capture.get("correlation_id", "none") or "none"
        filename = f"{event_type}_{ts}_{corr_id}_{score:.2f}.wav"
        path = self._output_dir / filename

        # Save audio
        try:
            sf.write(str(path), full_audio, self.SAMPLE_RATE)
        except Exception as e:
            logger.error("Failed to save audio capture: %s", e)
            return path

        # Save metadata sidecar
        meta_path = path.with_suffix(".json")
        try:
            with open(meta_path, "w") as f:
                json.dump(
                    {
                        "timestamp": capture["timestamp"],
                        "event_type": event_type,
                        "correlation_id": capture.get("correlation_id"),
                        "wake_score": capture.get("wake_score"),
                        "audio_duration_seconds": len(full_audio) / self.SAMPLE_RATE,
                        "pre_event_seconds": self._pre_samples / self.SAMPLE_RATE,
                        "post_event_seconds": self._post_samples / self.SAMPLE_RATE,
                        "sample_rate": self.SAMPLE_RATE,
                        "metadata": capture.get("metadata", {}),
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            logger.debug("Failed to save audio metadata: %s", e)

        # Update stats
        self._total_captures += 1
        self._captures_by_type[event_type] = self._captures_by_type.get(event_type, 0) + 1

        logger.info("Audio forensics captured: %s (%s)", path, event_type)
        return path

    def get_stats(self) -> dict[str, Any]:
        """Get capture statistics."""
        with self._lock:
            # maxlen is always set during __init__, but typed as int | None
            maxlen = self._buffer.maxlen
            buffer_fill_pct = (len(self._buffer) / maxlen * 100) if maxlen else 0.0
            return {
                "total_captures": self._total_captures,
                "captures_by_type": dict(self._captures_by_type),
                "captures_last_hour": len(self._captures_this_hour),
                "max_captures_per_hour": self._max_per_hour,
                "buffer_fill_pct": buffer_fill_pct,
                "output_dir": str(self._output_dir),
            }

    def cleanup_old_files(self, max_age_hours: float = 24.0) -> int:
        """
        Clean up old capture files.

        Args:
            max_age_hours: Maximum age of files to keep

        Returns:
            Number of files deleted
        """
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = 0

        for wav_file in self._output_dir.glob("*.wav"):
            if wav_file.stat().st_mtime < cutoff:
                try:
                    wav_file.unlink()
                    # Also delete sidecar
                    meta_file = wav_file.with_suffix(".json")
                    if meta_file.exists():
                        meta_file.unlink()
                    deleted += 1
                except OSError as e:
                    logger.debug("Failed to delete %s: %s", wav_file, e)

        if deleted > 0:
            logger.info("Audio forensics cleanup: deleted %d old files", deleted)

        return deleted


# Singleton
_logger: AudioForensicsLogger | None = None
_logger_lock = threading.Lock()


def get_audio_forensics_logger(output_dir: Path | None = None) -> AudioForensicsLogger:
    """Get global audio forensics logger."""
    global _logger
    with _logger_lock:
        if _logger is None:
            _logger = AudioForensicsLogger(output_dir=output_dir)
        return _logger


def reset_audio_forensics_logger() -> None:
    """Reset global audio forensics logger (for testing)."""
    global _logger
    with _logger_lock:
        _logger = None


__all__ = [
    "AudioCapture",
    "AudioForensicsLogger",
    "get_audio_forensics_logger",
    "reset_audio_forensics_logger",
]
