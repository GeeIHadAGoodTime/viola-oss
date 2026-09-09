"""Multilingual Whisper STT — enables language auto-detection for phone calls.

Subclasses Pipecat's WhisperSTTService to:
1. Pass language=None to faster-whisper (enables multilingual mode)
2. Capture detected language and confidence from TranscriptionInfo
3. Populate TranscriptionFrame.language with the detected language

The standard WhisperSTTService forces language=Language.EN, which:
- Produces garbled English text when the speaker uses Spanish/French/etc
- Never populates the language field, so LanguageHandler never fires
"""

from __future__ import annotations

import asyncio

from core.logging_config import get_logger
from telephony.phone_stt_options import (
    phone_pcm_to_whisper_float,
    phone_whisper_transcribe_options,
)

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import ErrorFrame, TranscriptionFrame
    from pipecat.services.whisper.stt import WhisperSTTService
    from pipecat.transcriptions.language import Language
    from pipecat.utils.time import time_now_iso8601

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False


# Map Whisper language codes (ISO 639-1) to Pipecat Language enum values
_WHISPER_TO_PIPECAT: dict[str, str] = {
    "en": "EN",
    "es": "ES",
    "fr": "FR",
    "de": "DE",
    "it": "IT",
    "pt": "PT",
    "ja": "JA",
    "zh": "ZH",
    "ko": "KO",
    "hi": "HI",
    "ar": "AR",
    "ru": "RU",
    "vi": "VI",
    "nl": "NL",
    "pl": "PL",
    "tr": "TR",
    "sv": "SV",
    "da": "DA",
    "no": "NO",
    "fi": "FI",
}


if PIPECAT_AVAILABLE:

    class MultilingualWhisperSTTService(WhisperSTTService):
        """WhisperSTTService with multilingual auto-detection enabled.

        Overrides run_stt() to pass language=None to faster-whisper's transcribe(),
        enabling automatic language detection. Captures the detected language and
        confidence from TranscriptionInfo and attaches them to TranscriptionFrame.
        """

        def __init__(self, *args, beam_size: int = 5, hotwords: str = "", initial_prompt: str = "", **kwargs):
            self._beam_size = beam_size
            self._hotwords = hotwords.strip()
            self._initial_prompt = initial_prompt.strip()
            super().__init__(*args, **kwargs)

        async def run_stt(self, audio: bytes):
            """Transcribe audio with language auto-detection.

            Yields TranscriptionFrame with detected language populated,
            or ErrorFrame if the model is unavailable.
            """
            if not self._model:
                yield ErrorFrame("Whisper model not available")
                return

            await self.start_processing_metrics()

            # Normalize signed 16-bit PCM to float32 and resample up to whisper's
            # 16 kHz (faster-whisper does NOT resample an ndarray, so raw 8 kHz
            # phone audio would transcribe pitch-halved and time-stretched).
            audio_float = phone_pcm_to_whisper_float(audio, self.sample_rate)

            # Pass language=None for auto-detection (key difference from base class)
            kwargs = phone_whisper_transcribe_options(
                language=None,
                beam_size=self._beam_size,
                hotwords=self._hotwords,
                initial_prompt=self._initial_prompt,
            )
            segments, info = await asyncio.to_thread(self._model.transcribe, audio_float, **kwargs)

            text: str = ""
            for segment in segments:
                text += "%s " % segment.text

            await self.stop_processing_metrics()

            # Resolve detected language to Pipecat Language enum
            detected_lang = None
            lang_probability = 0.0
            if info:
                whisper_lang = getattr(info, "language", None) or "en"
                lang_probability = getattr(info, "language_probability", 0.0)
                pipecat_code = _WHISPER_TO_PIPECAT.get(whisper_lang, "EN")
                try:
                    detected_lang = Language(pipecat_code)
                except (ValueError, KeyError):
                    detected_lang = Language.EN

                if whisper_lang != "en":
                    logger.debug(
                        "Whisper detected language: %s (confidence: %.2f)",
                        whisper_lang,
                        lang_probability,
                    )

            if text:
                await self._handle_transcription(text, True, detected_lang or self._settings.language)
                logger.debug("Transcription: [%s]", text.strip())

                frame = TranscriptionFrame(
                    text,
                    self._user_id,
                    time_now_iso8601(),
                    detected_lang or self._settings.language,
                    finalized=True,
                )
                # Attach language probability as extra metadata for
                # TranscriptionObserver to read via getattr()
                frame.language_probability = lang_probability
                yield frame
