"""
DeterministicTranscriber
========================

Test-friendly speech-to-text implementation that decodes commands from
synthetic PCM waveforms.  This allows end-to-end voice command testing
without requiring heavyweight Whisper models or GPU acceleration.

Encoding scheme
---------------
- Audio is expected to be mono, 16-bit little-endian PCM.
- Each character is encoded as a single sample with value:
      sample_value = BASE_OFFSET + ord(character)
- BASE_OFFSET is fixed at 1000 to keep the waveform within audible range
  while avoiding clipping (max 32767).
- Optional terminator ``\0`` is supported to truncate trailing data.

When the deterministic transcriber is enabled (via the
``VIOLA_TEST_TRANSCRIBER=1`` environment variable), the FastAPI backend
will instantiate this implementation instead of Whisper.  Tests can then
generate synthetic waveforms that map directly to textual commands.
"""

from __future__ import annotations

import wave
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)

BASE_OFFSET = 1000
MIN_CHAR = 32
MAX_CHAR = 126


class DeterministicTranscriber:
    """Simple, deterministic STT adapter for automated tests."""

    def __init__(self, _config: object | None = None) -> None:
        self._config = _config
        logger.info("🧪 Using DeterministicTranscriber for voice commands")

    def is_available(self) -> bool:
        """Always available – no external dependencies."""
        return True

    # Compatibility with voice.transcriber.TranscriberAdapter
    def transcribe(self, audio_path: str | Path, preprocess: bool = False) -> str:
        """Decode deterministic waveform into text."""
        try:
            path = Path(audio_path)
            with wave.open(str(path), "rb") as wf:
                if wf.getsampwidth() != 2:
                    logger.warning(
                        "DeterministicTranscriber expected 16-bit audio, got %s bytes",
                        wf.getsampwidth(),
                    )
                if wf.getnchannels() != 1:
                    logger.warning(
                        "DeterministicTranscriber expected mono audio, got %s channels",
                        wf.getnchannels(),
                    )

                frames = wf.readframes(wf.getnframes())

            if len(frames) < 2:
                logger.warning("DeterministicTranscriber received empty audio payload")
                return ""

            chars: list[str] = []
            prev_code: int | None = None
            # Iterate over 16-bit little-endian samples
            for i in range(0, len(frames) - 1, 2):
                sample = frames[i] | (frames[i + 1] << 8)
                if sample >= 0x8000:
                    sample -= 0x10000  # convert to signed

                code = sample - BASE_OFFSET
                if code == 0:
                    # Explicit terminator encountered
                    break
                if MIN_CHAR <= code <= MAX_CHAR:
                    if code != prev_code:
                        chars.append(chr(code))
                        prev_code = code
                else:
                    prev_code = None

            transcript = "".join(chars).strip()
            logger.info("Deterministic transcription length=%d", len(transcript))
            return transcript
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("DeterministicTranscriber failed: %s", exc)
            return ""
