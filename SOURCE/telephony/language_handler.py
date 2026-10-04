"""Automatic language detection and switching for phone calls.

When a non-English speaker answers, Viola detects the language and switches
both comprehension (LLM) and speech (TTS) to match. Supported TTS languages:
English, Spanish, French, Italian, Portuguese, Japanese, Chinese, Hindi.
For unsupported TTS languages, Viola exits gracefully in English.
"""

from __future__ import annotations

import asyncio

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
        self._context_frame_target = None

    def set_context_frame_target(self, target) -> None:
        """Bind the user aggregator, before context can queue behind speech."""
        self._context_frame_target = target

    def _context_frame_applied(self, frame) -> bool:
        messages = getattr(self._context_frame_target, "messages", ())
        return all(any(message is applied for applied in messages) for message in frame.messages)

    async def _push_context_frame(self, frame) -> None:
        from pipecat.processors.frame_processor import FrameDirection

        if self._context_frame_target is None:
            raise RuntimeError("Phone language context target is not bound")
        # Match voicemail's direct-context path. A push from the LLM would queue
        # behind speech and could be discarded by ordinary barge-in.
        await self._context_frame_target.process_frame(frame, FrameDirection.DOWNSTREAM)
        if not self._context_frame_applied(frame):
            raise RuntimeError("Phone language context was not applied")

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
            # The pinned Kokoro service uses settings frames, not set_language.
            # Other phone providers do not necessarily consume that setting.
            from pipecat.frames.frames import TTSUpdateSettingsFrame
            from pipecat.processors.frame_processor import FrameDirection
            from pipecat.transcriptions.language import Language

            try:
                from pipecat.services.kokoro.tts import KokoroTTSService
            except Exception as exc:
                logger.warning("Kokoro language switching is unavailable: %s", exc)
                return

            if not isinstance(self._tts, KokoroTTSService):
                logger.warning("Phone TTS cannot switch language to %s", lang_code)
                return

            language = Language(lang_code)
            previous_language = self._tts._settings.language
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

            frame = LLMMessagesAppendFrame(messages=[{"role": "system", "content": context}], run_llm=False)
            try:
                await self._tts.process_frame(
                    TTSUpdateSettingsFrame(delta=KokoroTTSService.Settings(language=language), service=self._tts),
                    FrameDirection.DOWNSTREAM,
                )
                if self._tts._settings.language != self._tts.language_to_service_language(language):
                    raise RuntimeError("Kokoro did not apply the requested language")
                await self._push_context_frame(frame)
            except (Exception, asyncio.CancelledError) as exc:
                # A context target can fail/cancel after appending. Preserve a
                # completed switch rather than roll back speech alone or retry
                # an already-published context. Before append, restore Kokoro.
                if self._context_frame_applied(frame):
                    self._current_lang = lang_code
                    self._switched = True
                else:
                    self._tts._settings.language = previous_language
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if self._switched:
                    logger.warning("Language switched to %s; context handler failed after append: %s", lang_code, exc)
                else:
                    logger.warning("TTS language switch failed: %s", exc)
                return

            self._current_lang = lang_code
            self._switched = True
            logger.info("Language switched to %s (%s)", lang_name, lang_code)

        else:
            context = "\n".join(
                [
                    "phone_event: language_detected",
                    "source: asr_language_metadata",
                    "recipient_language_code: %s" % lang_code,
                    "recipient_language_name: %s" % lang_name,
                    "tts_language_supported: false",
                ]
            )
            frame = LLMMessagesAppendFrame(messages=[{"role": "system", "content": context}], run_llm=False)
            try:
                await self._push_context_frame(frame)
            finally:
                self._switched = self._context_frame_applied(frame)
            logger.info("Unsupported language %s — exiting gracefully", lang_name)

    @property
    def current_language(self) -> str:
        return self._current_lang

    @property
    def has_switched(self) -> bool:
        return self._switched
