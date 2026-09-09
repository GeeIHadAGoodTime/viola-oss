"""
Artwork extraction for local music files.

Provides two strategies:
1. Embedded art extraction via TinyTag (audio files with cover art tags).
2. Video keyframe thumbnail extraction via FFmpeg (mp4, mkv, etc.).

All artwork is returned as base64 data URIs suitable for direct embedding
in HTML img tags or JSON API responses.
"""

from __future__ import annotations

import base64
import subprocess
import threading
from pathlib import Path

from core.logging_config import get_logger
from core.subprocess_utils import run_silent

logger = get_logger(__name__)

# Lock for TinyTag operations which may not be thread-safe.
_tinytag_lock = threading.Lock()

VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv"})


def _detect_mime(image_data: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if image_data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _to_data_uri(image_data: bytes) -> str:
    """Convert raw image bytes to a base64 data URI."""
    mime = _detect_mime(image_data)
    encoded = base64.b64encode(image_data).decode("ascii")
    return "data:%s;base64,%s" % (mime, encoded)


def extract_embedded_artwork(file_path: str) -> str | None:
    """Extract embedded album art from an audio file via TinyTag.

    Returns a base64 data URI if artwork is embedded, or None.
    Thread-safe via _tinytag_lock.
    """
    try:
        from tinytag import TinyTag

        with _tinytag_lock:
            tag = TinyTag.get(file_path, image=True)
            image_data = tag.get_image()
        if not image_data:
            return None
        return _to_data_uri(image_data)
    except Exception:
        logger.debug("Failed to extract embedded artwork from %s", file_path)
        return None


def extract_video_thumbnail(
    file_path: str,
    seek_seconds: float = 1.0,
) -> str | None:
    """Extract a keyframe from a video file via FFmpeg.

    Grabs one frame at ``seek_seconds`` into the video, encodes as JPEG,
    and returns a base64 data URI. Returns None on any failure.

    FFmpeg is already a project dependency (SimpleBackend uses it for decoding).
    """
    try:
        result = run_silent(
            [
                "ffmpeg",
                "-ss",
                str(seek_seconds),
                "-i",
                file_path,
                "-vframes",
                "1",
                "-an",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "5",
                "pipe:1",
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and len(result.stdout) > 100:
            encoded = base64.b64encode(result.stdout).decode("ascii")
            return "data:image/jpeg;base64,%s" % encoded
    except subprocess.TimeoutExpired:
        logger.debug("FFmpeg thumbnail extraction timed out for %s", file_path)
    except FileNotFoundError:
        logger.warning("FFmpeg not found — video thumbnails unavailable")
    except OSError:
        logger.debug("FFmpeg thumbnail extraction failed for %s", file_path)
    return None


def extract_artwork(file_path: str) -> str | None:
    """Extract artwork from any supported file.

    For audio files: extracts embedded cover art via TinyTag.
    For video files: extracts a keyframe thumbnail via FFmpeg.
    Returns a base64 data URI or None.
    """
    ext = Path(file_path).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return extract_video_thumbnail(file_path)
    return extract_embedded_artwork(file_path)
