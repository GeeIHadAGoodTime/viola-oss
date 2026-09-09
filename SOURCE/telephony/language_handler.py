"""Automatic language detection and switching for phone calls.

When a non-English speaker answers, Viola detects the language and switches
both comprehension (LLM) and speech (TTS) to match. Supported TTS languages:
English, Spanish, French, Italian, Portuguese, Japanese, Chinese, Hindi.
For unsupported TTS languages, Viola exits gracefully in English.
"""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)

KOKORO_SUPPORTED = {"en", "es", "fr", "it", "pt", "ja", "zh", "hi"}
LANGUAGE_NAMES = {
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "zh": "Chinese",
    "hi": "Hindi",
    "de": "German",
    "ko": "Korean",
    "ar": "Arabic",
    "vi": "Vietnamese",
    "ru": "Russian",
}


class LanguageHandler:
    """Detects non-English speech and switches pipeline language."""

    def __init__(self, llm, tts=None, switch_threshold: int = 2):
        self._llm = llm
        self._tts = tts
        self._consecutive_non_english = 0
        self._current_lang = "en"
        self._switched = False
        self._switch_threshold = switch_threshold

    async def on_transcription_with_language(self, text: str, detected_lang: str, confidence: float):
        """Called with each transcription + detected language."""
        if self._switched:
            return  # Already switched, stay in new language

        # Normalize language code — Pipecat Language enum values may be like "Language.ES" or "es"
        lang = detected_lang.lower().split(".")[-1].split("-")[0][:2]

        if lang == "en" or confidence < 0.7:
            self._consecutive_non_english = 0
            return

        self._consecutive_non_english += 1

        if self._consecutive_non_english >= self._switch_threshold:
            await self._switch_language(lang)

    async def _switch_language(self, lang_code: str):
        from pipecat.frames.frames import LLMMessagesAppendFrame

        lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)

        if lang_code in KOKORO_SUPPORTED:
            self._switched = True
            self._current_lang = lang_code
            context = "\n".join(
                [
                    "phone_event: language_detected",
                    "source: asr_language_metadata",
                    "recipient_language_code: %s" % lang_code,
                    "recipient_language_name: %s" % lang_name,
                    "tts_language_supported: true",
                    "tts_language_active: %s" % lang_code,
                ]
            )

            await self._llm.push_frame(LLMMessagesAppendFrame(messages=[{"role": "system", "content": context}]))

            # If TTS needs explicit language parameter, set it
            if self._tts and hasattr(self._tts, "set_language"):
                try:
                    await self._tts.set_language(lang_code)
                except Exception as exc:
                    logger.warning("TTS language switch failed: %s", exc)

            logger.info("Language switched to %s (%s)", lang_name, lang_code)

        else:
            self._switched = True
            context = "\n".join(
                [
                    "phone_event: language_detected",
                    "source: asr_language_metadata",
                    "recipient_language_code: %s" % lang_code,
                    "recipient_language_name: %s" % lang_name,
                    "tts_language_supported: false",
                ]
            )
            await self._llm.push_frame(LLMMessagesAppendFrame(messages=[{"role": "system", "content": context}]))
            logger.info("Unsupported language %s — exiting gracefully", lang_name)

    @property
    def current_language(self) -> str:
        return self._current_lang

    @property
    def has_switched(self) -> bool:
        return self._switched
