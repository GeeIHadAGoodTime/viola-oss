"""Fast local Piper neural TTS service for cloud phone calls."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Iterator

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from core.logging_config import get_logger
from telephony.config import DEFAULT_PIPER_PHONE_VOICE

logger = get_logger(__name__)

_DEFAULT_OUTPUT_SAMPLE_RATE = 16000
_SYNTHESIS_DONE = object()
_PHONE_TTS_PRONUNCIATIONS = ((re.compile(r"\bViola\b", re.IGNORECASE), "vee oh luh"),)


@dataclass
class PiperTTSSettings(TTSSettings):
    """Runtime settings for the Piper phone TTS service."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def phone_piper_model_paths(
    voice: str = DEFAULT_PIPER_PHONE_VOICE,
) -> tuple[Path, Path]:
    """Return the baked Piper model/config paths for a supported phone voice."""
    voice_id = (voice or DEFAULT_PIPER_PHONE_VOICE).strip()
    if voice_id != DEFAULT_PIPER_PHONE_VOICE:
        raise ValueError("Unsupported Piper phone voice: %s" % voice)
    model_path = _project_root() / "models" / "tts" / "piper" / ("%s.onnx" % voice_id)
    return model_path, model_path.with_suffix(".onnx.json")


def _apply_phone_tts_pronunciations(text: str) -> str:
    spoken = str(text or "")
    for pattern, replacement in _PHONE_TTS_PRONUNCIATIONS:
        spoken = pattern.sub(replacement, spoken)
    return spoken


class PiperPhoneTTSService(TTSService):
    """Local neural TTS service using Piper's published ONNX runtime."""

    Settings = PiperTTSSettings

    def __init__(
        self,
        *,
        model_path: str | Path,
        config_path: str | Path | None = None,
        voice: str = DEFAULT_PIPER_PHONE_VOICE,
        settings: PiperTTSSettings | None = None,
        **kwargs: Any,
    ) -> None:
        default_settings = self.Settings(
            model="piper",
            voice=voice,
            language="en-US",
        )
        if settings is not None:
            default_settings.apply_update(settings)
        kwargs.setdefault("sample_rate", _DEFAULT_OUTPUT_SAMPLE_RATE)
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=default_settings,
            **kwargs,
        )
        self._model_path = Path(model_path)
        self._config_path = Path(config_path) if config_path is not None else self._model_path.with_suffix(".onnx.json")
        if not self._model_path.is_file():
            raise RuntimeError("Piper phone TTS model missing: %s" % self._model_path)
        if not self._config_path.is_file():
            raise RuntimeError("Piper phone TTS config missing: %s" % self._config_path)
        self._voice = self._load_voice()
        if self._sample_rate <= 0:
            self._sample_rate = _DEFAULT_OUTPUT_SAMPLE_RATE
        self._resampler = create_stream_resampler()

    def can_generate_metrics(self) -> bool:
        return True

    def _processor_started(self) -> bool:
        return bool(getattr(self, "_FrameProcessor__started", False))

    async def _stop_ttfb_metrics_if_started(self) -> None:
        if self._processor_started():
            await self.stop_ttfb_metrics()

    def _load_voice(self) -> Any:
        try:
            from piper.voice import PiperVoice
        except (ImportError, OSError) as exc:
            raise RuntimeError("piper-tts is required for Piper phone TTS") from exc
        return PiperVoice.load(self._model_path, config_path=self._config_path)

    def _iter_synthesis_chunks(self, text: str) -> Iterator[tuple[bytes, int]]:
        text = _apply_phone_tts_pronunciations(text)
        sample_rate = int(getattr(self._voice.config, "sample_rate", 0) or 0)
        emitted_audio = False
        for chunk in self._voice.synthesize(text):
            audio = bytes(chunk.audio_int16_bytes or b"")
            sample_rate = int(getattr(chunk, "sample_rate", 0) or sample_rate)
            if audio:
                if sample_rate <= 0:
                    raise RuntimeError("Piper phone TTS returned invalid sample rate")
                emitted_audio = True
                yield audio, sample_rate
        if not emitted_audio:
            raise RuntimeError("Piper phone TTS produced no audio")
        if sample_rate <= 0:
            raise RuntimeError("Piper phone TTS returned invalid sample rate")

    def _synthesize_pcm(self, text: str) -> tuple[bytes, int]:
        chunks = list(self._iter_synthesis_chunks(text))
        pcm = b"".join(audio for audio, _sample_rate in chunks)
        sample_rate = chunks[-1][1] if chunks else 0
        if not pcm:
            raise RuntimeError("Piper phone TTS produced no audio")
        if sample_rate <= 0:
            raise RuntimeError("Piper phone TTS returned invalid sample rate")
        return pcm, sample_rate

    async def _stream_synthesis_chunks(self, text: str) -> AsyncGenerator[tuple[bytes, int], None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()

        def _produce() -> None:
            try:
                for item in self._iter_synthesis_chunks(text):
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _SYNTHESIS_DONE)

        producer = asyncio.create_task(asyncio.to_thread(_produce))
        try:
            while True:
                item = await queue.get()
                if item is _SYNTHESIS_DONE:
                    break
                if isinstance(item, (OSError, RuntimeError, ValueError, TimeoutError)):
                    raise item
                audio, sample_rate = item
                yield audio, sample_rate
        finally:
            if producer.done():
                await producer
            else:
                producer.cancel()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug("%s: Generating TTS [%s]", self, text)
        try:
            await self.start_tts_usage_metrics(text)
            emitted_audio = False
            async for pcm, sample_rate in self._stream_synthesis_chunks(text):
                audio_data = await self._resampler.resample(pcm, sample_rate, self.sample_rate)
                if not audio_data:
                    continue
                emitted_audio = True
                await self._stop_ttfb_metrics_if_started()
                yield TTSAudioRawFrame(
                    audio=audio_data,
                    sample_rate=self.sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
            if not emitted_audio:
                raise RuntimeError("Piper phone TTS produced no audio")
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            logger.warning("Piper phone TTS failed: %s", exc)
            yield ErrorFrame(error="Piper phone TTS failed: %s" % exc)
        finally:
            await self._stop_ttfb_metrics_if_started()
