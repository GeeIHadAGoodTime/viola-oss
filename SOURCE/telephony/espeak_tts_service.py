"""Low-latency local phone TTS backed by the ``espeak-ng`` binary."""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import wave
from dataclasses import dataclass

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
    from pipecat.services.tts_service import TTSService

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - phone runtime guard catches this before use.
    ErrorFrame = TTSAudioRawFrame = TTSService = object  # type: ignore[assignment,misc]
    PIPECAT_AVAILABLE = False


DEFAULT_ESPEAK_VOICE = "en-us"
DEFAULT_ESPEAK_WORDS_PER_MINUTE = 185


@dataclass(frozen=True)
class EspeakAudio:
    pcm: bytes
    sample_rate: int


def _wav_to_pcm_mono(wav_bytes: bytes) -> EspeakAudio:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        pcm = wav_file.readframes(wav_file.getnframes())
    if channels != 1:
        raise RuntimeError("espeak-ng produced %d channels; expected mono" % channels)
    if sample_width != 2:
        raise RuntimeError("espeak-ng produced %d-byte samples; expected 16-bit PCM" % sample_width)
    if not pcm:
        raise RuntimeError("espeak-ng produced empty audio")
    return EspeakAudio(pcm=pcm, sample_rate=sample_rate)


def synthesize_espeak_ng(
    text: str,
    *,
    binary: str | None = None,
    voice: str = DEFAULT_ESPEAK_VOICE,
    words_per_minute: int = DEFAULT_ESPEAK_WORDS_PER_MINUTE,
) -> EspeakAudio:
    executable = binary or shutil.which("espeak-ng")
    if not executable:
        raise RuntimeError("espeak-ng binary is not installed")
    spoken_text = (text or "").strip()
    if not spoken_text:
        raise RuntimeError("espeak-ng received empty text")
    result = subprocess.run(
        [
            executable,
            "--stdout",
            "-v",
            voice,
            "-s",
            str(int(words_per_minute)),
            spoken_text,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5.0,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("espeak-ng failed with code %d: %s" % (result.returncode, stderr))
    return _wav_to_pcm_mono(result.stdout)


if PIPECAT_AVAILABLE:

    class EspeakNgTTSService(TTSService):
        """Pipecat TTS service that emits local espeak-ng audio without paid APIs."""

        def __init__(
            self,
            *,
            voice: str = DEFAULT_ESPEAK_VOICE,
            words_per_minute: int = DEFAULT_ESPEAK_WORDS_PER_MINUTE,
            binary: str | None = None,
            **kwargs,
        ) -> None:
            super().__init__(
                push_start_frame=True,
                push_stop_frames=True,
                **kwargs,
            )
            self._voice = voice
            self._words_per_minute = int(words_per_minute)
            self._binary = binary

        def can_generate_metrics(self) -> bool:
            return True

        async def run_tts(self, text: str, context_id: str):
            logger.debug("%s: Generating espeak-ng TTS [%s]", self, text)
            try:
                await self.start_tts_usage_metrics(text)
                audio = await asyncio.to_thread(
                    synthesize_espeak_ng,
                    text,
                    binary=self._binary,
                    voice=self._voice,
                    words_per_minute=self._words_per_minute,
                )
                await self.stop_ttfb_metrics()
                yield TTSAudioRawFrame(
                    audio=audio.pcm,
                    sample_rate=audio.sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, wave.Error) as exc:
                yield ErrorFrame(error="espeak-ng TTS failed: %s" % exc)
            finally:
                await self.stop_ttfb_metrics()
