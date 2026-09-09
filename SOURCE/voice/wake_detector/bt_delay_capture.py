"""
Bluetooth Delay Capture — Raw Audio WAV Recorder
=================================================

Captures frame-aligned raw mic and loopback audio to WAV files
during music playback. Activated by VIOLA_LIVE_TEST=1.

Writes to: logs/live_test/raw_mic.wav, logs/live_test/raw_loopback.wav

Usage: Call `record_frame(mic_frame, ref_frame)` from the AEC loop.
After `capture_seconds` of audio, automatically closes and logs the paths.

TEMPORARY DIAGNOSTIC — remove after Bluetooth delay analysis.
"""

from __future__ import annotations

import os
import threading
import time
import wave
from pathlib import Path

import numpy as np
import numpy.typing as npt

from core.logging_config import get_logger
from core.platform import get_logs_dir

logger = get_logger(__name__)

# Only active when VIOLA_LIVE_TEST=1
_ENABLED = os.environ.get("VIOLA_LIVE_TEST", "0") == "1"

# Capture config
CAPTURE_SECONDS = 10
SAMPLE_RATE = 16000
WARMUP_SECONDS = 15  # Wait this many seconds before starting capture
TOTAL_SAMPLES = CAPTURE_SECONDS * SAMPLE_RATE


class BTDelayCapture:
    """
    Records raw mic + loopback frames to WAV files for delay analysis.

    Thread-safe. Buffers frames in memory, writes once when capture completes.
    """

    def __init__(
        self,
        capture_seconds: int = CAPTURE_SECONDS,
        warmup_seconds: int = WARMUP_SECONDS,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._capture_seconds = capture_seconds
        self._warmup_seconds = warmup_seconds
        self._sample_rate = sample_rate
        self._total_samples = capture_seconds * sample_rate

        self._lock = threading.Lock()
        self._mic_frames: list[npt.NDArray[np.int16]] = []
        self._ref_frames: list[npt.NDArray[np.int16]] = []
        self._samples_captured = 0
        self._start_time = time.monotonic()
        self._warmup_done = False
        self._capture_done = False
        self._output_dir = get_logs_dir() / "live_test"
        self._first_nonzero_ref_seen = False

        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "BTDelayCapture initialized: warmup=%ds, capture=%ds, output=%s",
            warmup_seconds,
            capture_seconds,
            self._output_dir,
        )

    @property
    def capture_done(self) -> bool:
        return self._capture_done

    def record_frame(
        self,
        mic_frame: npt.NDArray[np.int16],
        ref_frame: npt.NDArray[np.int16],
    ) -> None:
        """
        Record a frame pair. Called from the AEC loop for every 160-sample frame.

        Skips until warmup period is over AND loopback has nonzero data
        (confirming music is actually playing).
        """
        if self._capture_done:
            return

        with self._lock:
            # Phase 1: Wait for warmup + actual music signal
            if not self._warmup_done:
                elapsed = time.monotonic() - self._start_time

                # Check if loopback has signal (music playing)
                ref_rms = float(np.sqrt(np.mean(ref_frame.astype(np.float32) ** 2)))
                if ref_rms > 100 and not self._first_nonzero_ref_seen:
                    self._first_nonzero_ref_seen = True
                    logger.info(
                        "BTDelayCapture: loopback signal detected (rms=%.0f) at %.1fs",
                        ref_rms,
                        elapsed,
                    )

                if elapsed < self._warmup_seconds:
                    return
                if not self._first_nonzero_ref_seen:
                    # Still no loopback signal — keep waiting
                    if int(elapsed) % 5 == 0 and int(elapsed * 100) % 100 == 0:
                        logger.warning(
                            "BTDelayCapture: waiting for loopback signal (%.0fs elapsed)",
                            elapsed,
                        )
                    return

                self._warmup_done = True
                logger.info(
                    "BTDelayCapture: warmup complete at %.1fs, starting %ds capture",
                    elapsed,
                    self._capture_seconds,
                )

            # Phase 2: Capture frames
            frame_len = len(mic_frame)
            remaining = self._total_samples - self._samples_captured

            if remaining <= 0:
                self._finalize()
                return

            # Clip to remaining if we'd overshoot
            take = min(frame_len, remaining)
            self._mic_frames.append(mic_frame[:take].copy())
            self._ref_frames.append(ref_frame[:take].copy())
            self._samples_captured += take

            # Progress logging every 2 seconds
            if self._samples_captured % (self._sample_rate * 2) < frame_len:
                logger.info(
                    "BTDelayCapture: %.1f / %ds captured",
                    self._samples_captured / self._sample_rate,
                    self._capture_seconds,
                )

            if self._samples_captured >= self._total_samples:
                self._finalize()

    def _finalize(self) -> None:
        """Concatenate frames and write WAV files."""
        if self._capture_done:
            return
        self._capture_done = True

        mic_path = self._output_dir / "raw_mic.wav"
        ref_path = self._output_dir / "raw_loopback.wav"

        mic_all = np.concatenate(self._mic_frames) if self._mic_frames else np.array([], dtype=np.int16)
        ref_all = np.concatenate(self._ref_frames) if self._ref_frames else np.array([], dtype=np.int16)

        # Ensure exact same length
        min_len = min(len(mic_all), len(ref_all))
        mic_all = mic_all[:min_len]
        ref_all = ref_all[:min_len]

        self._write_wav(mic_path, mic_all)
        self._write_wav(ref_path, ref_all)

        # Free memory
        self._mic_frames.clear()
        self._ref_frames.clear()

        duration = min_len / self._sample_rate
        logger.info(
            "BTDelayCapture DONE: %d samples (%.1fs) written to %s and %s",
            min_len,
            duration,
            mic_path,
            ref_path,
        )

    def _write_wav(self, path: Path, data: npt.NDArray[np.int16]) -> None:
        """Write int16 mono WAV at self._sample_rate."""
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16
            wf.setframerate(self._sample_rate)
            wf.writeframes(data.tobytes())


# --------------------------------------------------------------------------- #
# Global singleton — only created when VIOLA_LIVE_TEST=1                       #
# --------------------------------------------------------------------------- #

_instance: BTDelayCapture | None = None
_instance_lock = threading.Lock()


def get_bt_delay_capture() -> BTDelayCapture | None:
    """Get the global capture instance (None if not enabled)."""
    global _instance
    if not _ENABLED:
        return None
    with _instance_lock:
        if _instance is None:
            _instance = BTDelayCapture()
        return _instance


__all__ = [
    "BTDelayCapture",
    "get_bt_delay_capture",
]
