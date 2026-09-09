"""Pre-upload validation gate for wake word audio clips.

Runs 5 checks on each clip before it can proceed to anonymization:
1. File size < 200KB
2. Valid WAV format (RIFF/WAVE header, PCM)
3. Duration 1.3-1.7s at 16kHz
4. Energy RMS > -50 dBFS
5. Clipping < 5% samples at max amplitude
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

from .database import get_data_collection_db

logger = get_logger(__name__)

MAX_FILE_SIZE_BYTES = 200 * 1024  # 200 KB
MIN_DURATION_SEC = 1.3
MAX_DURATION_SEC = 1.7
MIN_RMS_DBFS = -50.0
MAX_CLIPPING_RATIO = 0.05
CLIPPING_THRESHOLD = 32700  # int16 near-max (out of 32767)
settings = None


def _get_settings():
    if settings is not None:
        return settings
    from config.settings import settings as app_settings

    return app_settings


def _parse_wav_header(path: Path) -> tuple[bool, str, int, int]:
    """Parse WAV header. Returns (valid, reason, sample_rate, num_samples).

    Uses manual RIFF parsing consistent with anonymizer.py.
    """
    try:
        with open(path, "rb") as f:
            riff = f.read(4)
            if riff != b"RIFF":
                return False, "not_riff", 0, 0
            f.read(4)  # file size
            wave = f.read(4)
            if wave != b"WAVE":
                return False, "not_wave", 0, 0

            sample_rate = 0
            audio_format = 0

            while True:
                chunk_id = f.read(4)
                if len(chunk_id) < 4:
                    break
                chunk_size = struct.unpack("<I", f.read(4))[0]
                if chunk_id == b"fmt ":
                    fmt_data = f.read(chunk_size)
                    audio_format = struct.unpack("<H", fmt_data[0:2])[0]
                    sample_rate = struct.unpack("<I", fmt_data[4:8])[0]
                elif chunk_id == b"data":
                    num_samples = chunk_size // 2  # 16-bit mono
                    return True, "", sample_rate, num_samples
                else:
                    f.read(chunk_size)

            if audio_format != 1:
                return False, "not_pcm", 0, 0
            return False, "no_data_chunk", 0, 0
    except Exception:
        return False, "read_error", 0, 0


def _read_pcm_int16(path: Path) -> np.ndarray | None:
    """Read raw int16 PCM from WAV data chunk."""
    try:
        with open(path, "rb") as f:
            f.read(4)  # RIFF
            f.read(4)  # size
            f.read(4)  # WAVE
            while True:
                chunk_id = f.read(4)
                if len(chunk_id) < 4:
                    break
                chunk_size = struct.unpack("<I", f.read(4))[0]
                if chunk_id == b"data":
                    raw = f.read(chunk_size)
                    return np.frombuffer(raw, dtype=np.int16)
                f.read(chunk_size)
        return None
    except Exception:
        return None


def validate_clip(clip_id: int) -> bool:
    """Validate a single clip. Updates DB with pass/fail status.

    Returns True if validation passed.
    """
    db = get_data_collection_db()
    clip = db.get_clip(clip_id)
    if clip is None:
        logger.warning("Clip %d not found for validation", clip_id)
        return False

    data_dir = Path(_get_settings().data_dir) / "wake_clips"
    audio_path = data_dir / clip["audio_path"]

    if not audio_path.exists():
        db.mark_validation_failed(clip_id, "file_not_found")
        return False

    # Check 1: File size
    file_size = audio_path.stat().st_size
    if file_size > MAX_FILE_SIZE_BYTES:
        db.mark_validation_failed(clip_id, "file_too_large")
        logger.debug("Clip %d failed validation: file_too_large (%d bytes)", clip_id, file_size)
        return False

    # Check 2: Valid WAV format
    valid, reason, sample_rate, num_samples = _parse_wav_header(audio_path)
    if not valid:
        db.mark_validation_failed(clip_id, "invalid_wav_%s" % reason)
        logger.debug("Clip %d failed validation: invalid_wav_%s", clip_id, reason)
        return False

    # Check 3: Duration 1.3-1.7s at 16kHz
    if sample_rate != SAMPLE_RATE_16K:
        db.mark_validation_failed(clip_id, "wrong_sample_rate")
        logger.debug("Clip %d failed validation: wrong_sample_rate (%d)", clip_id, sample_rate)
        return False

    duration_sec = num_samples / SAMPLE_RATE_16K
    if not (MIN_DURATION_SEC <= duration_sec <= MAX_DURATION_SEC):
        db.mark_validation_failed(clip_id, "wrong_duration")
        logger.debug("Clip %d failed validation: wrong_duration (%.2fs)", clip_id, duration_sec)
        return False

    # Read PCM for energy/clipping checks
    pcm = _read_pcm_int16(audio_path)
    if pcm is None or len(pcm) == 0:
        db.mark_validation_failed(clip_id, "empty_audio")
        return False

    # Check 4: Energy RMS > -50 dBFS
    rms = np.sqrt(np.mean(pcm.astype(np.float64) ** 2))
    if rms < 1e-10:
        db.mark_validation_failed(clip_id, "silent")
        logger.debug("Clip %d failed validation: silent", clip_id)
        return False
    rms_dbfs = 20 * np.log10(rms / 32767.0)
    if rms_dbfs < MIN_RMS_DBFS:
        db.mark_validation_failed(clip_id, "too_quiet")
        logger.debug("Clip %d failed validation: too_quiet (%.1f dBFS)", clip_id, rms_dbfs)
        return False

    # Check 5: Clipping < 5%
    clipped_count = int(np.sum(np.abs(pcm) >= CLIPPING_THRESHOLD))
    clipping_ratio = clipped_count / len(pcm)
    if clipping_ratio >= MAX_CLIPPING_RATIO:
        db.mark_validation_failed(clip_id, "clipping")
        logger.debug(
            "Clip %d failed validation: clipping (%.1f%%)",
            clip_id,
            clipping_ratio * 100,
        )
        return False

    # All checks passed
    db.mark_validation_passed(clip_id)
    logger.debug("Clip %d passed validation", clip_id)
    return True


def validate_pending(limit: int = 50) -> int:
    """Validate up to `limit` pending clips. Returns count processed."""
    db = get_data_collection_db()
    clips = db.get_clips_for_validation(limit)
    count = 0
    for clip in clips:
        validate_clip(clip["id"])
        count += 1
    return count
