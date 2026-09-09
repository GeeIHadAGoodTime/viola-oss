"""
Sample Collector for Contributor Mode.

Collects wake word detection samples during contributor mode.
All samples are anonymized before saving (trimmed, normalized, clean WAV).
Samples are saved locally and uploaded on developer request.
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)

# Default sample rate for ViolaWake
DEFAULT_SAMPLE_RATE = SAMPLE_RATE_16K

# Anonymization constants (match data_collection/anonymizer.py)
_ANON_CLIP_SAMPLES = int(1.5 * SAMPLE_RATE_16K)
_TARGET_PEAK = 10 ** (-3.0 / 20.0)  # -3 dBFS → 0.7079

# Metadata keys that could contain PII — stripped before saving
_PII_METADATA_KEYS = frozenset(
    {
        "device_name",
        "device_id",
        "user_id",
        "user_name",
        "ip_address",
        "hostname",
        "mac_address",
        "serial_number",
        "username",
    }
)


class SampleCollector:
    """
    Collects and stores training samples.

    Saves both triggered detections and near-miss detections
    to a local directory for later upload.
    """

    def __init__(self, save_dir: Path | str):
        """
        Initialize sample collector.

        Args:
            save_dir: Directory to save samples to
        """
        self._save_dir = Path(save_dir)
        self._save_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_file = self._save_dir / "metadata.jsonl"
        self._uploaded_file = self._save_dir / "uploaded.txt"

        logger.info("SampleCollector initialized: %s", self._save_dir)

    def save_detection(
        self,
        audio: np.ndarray,
        score: float,
        threshold: float,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        metadata: dict[str, Any] | None = None,
    ) -> Path | None:
        """
        Save a triggered detection sample.

        Args:
            audio: Audio data as float32 numpy array
            score: Detection score
            threshold: Detection threshold
            sample_rate: Audio sample rate
            metadata: Additional metadata

        Returns:
            Path to saved file, or None if failed
        """
        return self._save_sample(
            audio=audio,
            sample_type="detection",
            score=score,
            threshold=threshold,
            sample_rate=sample_rate,
            metadata=metadata,
        )

    def save_near_miss(
        self,
        audio: np.ndarray,
        score: float,
        threshold: float,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        metadata: dict[str, Any] | None = None,
    ) -> Path | None:
        """
        Save a near-miss detection sample.

        Args:
            audio: Audio data as float32 numpy array
            score: Detection score
            threshold: Detection threshold
            sample_rate: Audio sample rate
            metadata: Additional metadata

        Returns:
            Path to saved file, or None if failed
        """
        return self._save_sample(
            audio=audio,
            sample_type="near_miss",
            score=score,
            threshold=threshold,
            sample_rate=sample_rate,
            metadata=metadata,
        )

    @staticmethod
    def _anonymize_audio(audio: np.ndarray) -> np.ndarray:
        """Trim to 1.5s centered on peak and normalize to -3 dBFS.

        Matches the anonymization pipeline in data_collection/anonymizer.py.
        """
        # Ensure float32
        if audio.dtype != np.float32:
            if audio.dtype == np.int16:
                audio = audio.astype(np.float32) / 32767.0
            else:
                audio = audio.astype(np.float32)

        # Trim to clip length centered on peak
        if len(audio) <= _ANON_CLIP_SAMPLES:
            padded = np.zeros(_ANON_CLIP_SAMPLES, dtype=np.float32)
            padded[: len(audio)] = audio
            audio = padded
        else:
            peak_idx = int(np.argmax(np.abs(audio)))
            half = _ANON_CLIP_SAMPLES // 2
            start = max(0, peak_idx - half)
            end = start + _ANON_CLIP_SAMPLES
            if end > len(audio):
                end = len(audio)
                start = max(0, end - _ANON_CLIP_SAMPLES)
            audio = audio[start:end].copy()

        # Peak normalize to -3 dBFS
        peak = np.max(np.abs(audio))
        if peak > 1e-6:
            audio = audio * (_TARGET_PEAK / peak)

        return audio

    @staticmethod
    def _strip_pii(metadata: dict[str, Any] | None) -> dict[str, Any]:
        """Remove PII keys from metadata dict."""
        if not metadata:
            return {}
        return {k: v for k, v in metadata.items() if k not in _PII_METADATA_KEYS}

    @staticmethod
    def _write_clean_wav(filepath: Path, audio: np.ndarray) -> None:
        """Write minimal RIFF WAV with no extra metadata."""
        clipped = np.clip(audio, -1.0, 1.0)
        pcm = (clipped * 32767).astype(np.int16)
        raw = pcm.tobytes()

        num_channels = 1
        sample_width = 2
        byte_rate = SAMPLE_RATE_16K * num_channels * sample_width
        block_align = num_channels * sample_width
        data_size = len(raw)

        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + data_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", 16))
            f.write(struct.pack("<H", 1))
            f.write(struct.pack("<H", num_channels))
            f.write(struct.pack("<I", SAMPLE_RATE_16K))
            f.write(struct.pack("<I", byte_rate))
            f.write(struct.pack("<H", block_align))
            f.write(struct.pack("<H", sample_width * 8))
            f.write(b"data")
            f.write(struct.pack("<I", data_size))
            f.write(raw)

    def _save_sample(
        self,
        audio: np.ndarray,
        sample_type: str,
        score: float,
        threshold: float,
        sample_rate: int,
        metadata: dict[str, Any] | None,
    ) -> Path | None:
        """
        Save an anonymized sample to disk.

        Audio is trimmed, normalized, and written as a clean WAV.
        Metadata is stripped of PII keys.

        Args:
            audio: Audio data as numpy array (float32 or int16)
            sample_type: Type of sample ("detection" or "near_miss")
            score: Detection score
            threshold: Detection threshold
            sample_rate: Audio sample rate
            metadata: Additional metadata (PII keys are stripped)

        Returns:
            Path to saved file, or None if failed
        """
        try:
            # Anonymize: trim to 1.5s, normalize to -3 dBFS
            processed = self._anonymize_audio(audio)

            # Compute content hash for deduplication
            pcm_bytes = (np.clip(processed, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            content_hash = hashlib.sha256(pcm_bytes).hexdigest()

            timestamp = int(time.time() * 1000)  # Milliseconds
            filename = f"{sample_type}_{timestamp}.wav"
            filepath = self._save_dir / filename

            # Write clean WAV (minimal headers, no metadata)
            self._write_clean_wav(filepath, processed)

            # Build metadata record (PII stripped)
            clean_meta = self._strip_pii(metadata)
            record = {
                "filename": filename,
                "timestamp": timestamp,
                "sample_type": sample_type,
                "score": round(score, 4),
                "threshold": round(threshold, 4),
                "sample_rate": sample_rate,
                "duration_seconds": round(len(processed) / SAMPLE_RATE_16K, 3),
                "content_hash": content_hash[:16],
                **clean_meta,
            }

            # Append to metadata file
            with open(self._metadata_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            logger.debug(
                "Saved anonymized %s sample: %s (score=%.3f)",
                sample_type,
                filename,
                score,
            )
            return filepath

        except Exception as e:
            logger.error("Failed to save sample: %s", e)
            return None

    def get_pending_samples(self) -> list[Path]:
        """
        Get samples not yet uploaded.

        Returns:
            List of paths to pending samples
        """
        uploaded = self._get_uploaded_set()
        pending = []

        for wav_file in self._save_dir.glob("*.wav"):
            if wav_file.name not in uploaded:
                pending.append(wav_file)

        # Sort by modification time (oldest first)
        pending.sort(key=lambda p: p.stat().st_mtime)
        return pending

    def get_pending_count(self) -> int:
        """
        Count samples pending upload.

        Returns:
            Number of pending samples
        """
        return len(self.get_pending_samples())

    def mark_as_uploaded(self, paths: list[Path]) -> None:
        """
        Mark samples as uploaded.

        Args:
            paths: List of paths to mark as uploaded
        """
        try:
            with open(self._uploaded_file, "a", encoding="utf-8") as f:
                for path in paths:
                    f.write(path.name + "\n")
            logger.debug("Marked %d samples as uploaded", len(paths))
        except Exception as e:
            logger.error("Failed to mark samples as uploaded: %s", e)

    def _get_uploaded_set(self) -> set[str]:
        """
        Get set of uploaded filenames.

        Returns:
            Set of uploaded filenames
        """
        if not self._uploaded_file.exists():
            return set()

        try:
            with open(self._uploaded_file, encoding="utf-8") as f:
                return {line.strip() for line in f if line.strip()}
        except Exception as e:
            logger.error("Failed to read uploaded file: %s", e)
            return set()

    def get_stats(self) -> dict[str, Any]:
        """
        Get collection statistics.

        Returns:
            Dict with stats about collected samples
        """
        try:
            pending = self.get_pending_samples()
            uploaded = self._get_uploaded_set()

            # Count by type
            detection_count = 0
            near_miss_count = 0
            total_duration = 0.0

            for wav_file in self._save_dir.glob("*.wav"):
                if wav_file.name.startswith("detection_"):
                    detection_count += 1
                elif wav_file.name.startswith("near_miss_"):
                    near_miss_count += 1

                # Estimate duration from file size (16-bit mono @ 16kHz = 32KB/sec)
                size_bytes = wav_file.stat().st_size
                total_duration += size_bytes / 32000

            return {
                "pending_count": len(pending),
                "uploaded_count": len(uploaded),
                "total_count": detection_count + near_miss_count,
                "detection_count": detection_count,
                "near_miss_count": near_miss_count,
                "total_duration_seconds": total_duration,
                "save_dir": str(self._save_dir),
            }

        except Exception as e:
            logger.error("Failed to get stats: %s", e)
            return {
                "pending_count": 0,
                "uploaded_count": 0,
                "total_count": 0,
                "error": str(e),
            }

    def clear_all(self) -> int:
        """
        Clear all collected samples.

        Returns:
            Number of files removed
        """
        count = 0
        try:
            for wav_file in self._save_dir.glob("*.wav"):
                wav_file.unlink()
                count += 1

            if self._metadata_file.exists():
                self._metadata_file.unlink()

            if self._uploaded_file.exists():
                self._uploaded_file.unlink()

            logger.info("Cleared %d samples from %s", count, self._save_dir)
        except Exception as e:
            logger.error("Failed to clear samples: %s", e)

        return count
