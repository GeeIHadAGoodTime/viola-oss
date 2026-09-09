"""Anonymization pipeline for wake word audio clips.

Strips metadata, trims to 1.5s, normalizes audio level, computes
content hash, and writes to anonymized directory.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

from .database import get_data_collection_db

logger = get_logger(__name__)

# 1.5 seconds at 16kHz
ANON_CLIP_SAMPLES = int(1.5 * SAMPLE_RATE_16K)
# Peak normalize to -3 dBFS → ~0.708 linear
TARGET_PEAK = 10 ** (-3.0 / 20.0)  # 0.7079


def _read_wav_float32(path: Path) -> np.ndarray | None:
    """Read a 16-bit mono WAV file into float32 array."""
    try:
        with open(path, "rb") as f:
            # Skip RIFF header
            riff = f.read(4)
            if riff != b"RIFF":
                logger.warning("Not a RIFF file: %s", path)
                return None
            f.read(4)  # file size
            wave = f.read(4)
            if wave != b"WAVE":
                logger.warning("Not a WAVE file: %s", path)
                return None

            # Find data chunk
            while True:
                chunk_id = f.read(4)
                if len(chunk_id) < 4:
                    break
                chunk_size = struct.unpack("<I", f.read(4))[0]
                if chunk_id == b"data":
                    raw = f.read(chunk_size)
                    pcm = np.frombuffer(raw, dtype=np.int16)
                    return pcm.astype(np.float32) / 32767.0
                f.read(chunk_size)

        logger.warning("No data chunk found in %s", path)
        return None
    except Exception:
        logger.exception("Failed to read WAV: %s", path)
        return None


def _write_wav_float32(path: Path, audio: np.ndarray) -> None:
    """Write float32 audio to 16-bit mono WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    raw = pcm.tobytes()

    num_channels = 1
    sample_width = 2
    byte_rate = SAMPLE_RATE_16K * num_channels * sample_width
    block_align = num_channels * sample_width
    data_size = len(raw)

    with open(path, "wb") as f:
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


def _trim_and_normalize(audio: np.ndarray) -> np.ndarray:
    """Trim to exactly 1.5s centered on peak, then normalize to -3 dBFS."""
    # Trim to clip length
    if len(audio) <= ANON_CLIP_SAMPLES:
        padded = np.zeros(ANON_CLIP_SAMPLES, dtype=np.float32)
        padded[: len(audio)] = audio
        audio = padded
    else:
        peak_idx = int(np.argmax(np.abs(audio)))
        half = ANON_CLIP_SAMPLES // 2
        start = max(0, peak_idx - half)
        end = start + ANON_CLIP_SAMPLES
        if end > len(audio):
            end = len(audio)
            start = max(0, end - ANON_CLIP_SAMPLES)
        audio = audio[start:end].copy()

    # Peak normalize to -3 dBFS
    peak = np.max(np.abs(audio))
    if peak > 1e-6:
        audio = audio * (TARGET_PEAK / peak)

    return audio


def anonymize_clip(clip_id: int) -> bool:
    """Anonymize a clip: trim, normalize, hash, save to anonymized dir.

    Returns True on success, False on failure.
    """
    db = get_data_collection_db()
    clip = db.get_clip(clip_id)
    if clip is None:
        logger.warning("Clip %d not found for anonymization", clip_id)
        return False

    from config.settings import settings as app_settings

    data_dir = Path(app_settings.data_dir) / "wake_clips"
    original_path = data_dir / clip["audio_path"]

    if not original_path.exists():
        logger.warning("Original audio not found: %s", original_path)
        return False

    audio = _read_wav_float32(original_path)
    if audio is None:
        return False

    # Process
    processed = _trim_and_normalize(audio)

    # Compute hash of processed audio bytes
    pcm_bytes = (np.clip(processed, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    content_hash = hashlib.sha256(pcm_bytes).hexdigest()

    # Write anonymized file
    anon_path = data_dir / "anonymized" / ("%d.wav" % clip_id)
    _write_wav_float32(anon_path, processed)

    # Update DB
    db.mark_anonymized(clip_id, content_hash)

    # Delete original file
    try:
        original_path.unlink()
    except OSError:
        logger.warning("Could not delete original file: %s", original_path)

    logger.debug("Anonymized clip %d → %s (hash=%s…)", clip_id, anon_path, content_hash[:12])
    return True


def anonymize_pending(limit: int = 50) -> int:
    """Anonymize up to `limit` pending clips. Returns count processed."""
    db = get_data_collection_db()
    clips = db.get_clips_for_anonymization(limit)
    count = 0
    for clip in clips:
        if anonymize_clip(clip["id"]):
            count += 1
    return count
