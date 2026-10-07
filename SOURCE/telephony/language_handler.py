"""Automatic language detection and switching for phone calls.

When a non-English speaker answers, Viola detects the language and switches
both comprehension (LLM) and speech (TTS) to match. The retained asset inventory covers:
English, Spanish, French, Italian, Portuguese, Japanese, Mandarin Chinese, Hindi.
Customer capability comes only from the explicitly bound pronunciation component
and a matching voice in its loaded asset, not from that inventory list.
For unsupported TTS languages, Viola exits gracefully in English.
"""

from __future__ import annotations

import asyncio
import os

from core.logging_config import get_logger

logger = get_logger(__name__)

KOKORO_SUPPORTED = {"en", "es", "fr", "it", "pt", "ja", "zh", "hi"}
LANGUAGE_NAMES = {
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "zh": "Mandarin Chinese",
    "cmn": "Mandarin Chinese",
    "yue": "Cantonese",
    "fil": "Filipino",
    "tl": "Tagalog",
    "hi": "Hindi",
    "de": "German",
    "ko": "Korean",
    "ar": "Arabic",
    "vi": "Vietnamese",
    "ru": "Russian",
}


def _parse_phone_language(value: object):
    """Accept complete codes from the pinned language registry, never prefixes."""
    from pipecat.transcriptions.language import Language

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.lower().startswith("language."):
        raw = raw[len("language.") :]
        member = Language.__members__.get(raw.upper())
        if member is not None:
            return member
    normalized = raw.replace("_", "-").lower()
    return next((language for language in Language if language.value.lower() == normalized), None)


def _selected_backend_supports_locale(tts, locale: str) -> bool:
    """Check the bound pronunciation component, separately from voice/assets.

    The remote-first wrapper delegates tokenizer access to its local fallback.
    Never advertise a customer locale based only on the product-wide language
    list or a mutable environment selector. Legacy forwarding stays unchanged.
    """
    try:
        runtime = getattr(tts, "_kokoro", None)
        tokenizer = getattr(runtime, "tokenizer", None)
        customer = getattr(tokenizer, "_customer", None)
        if customer is not None:
            supports = getattr(customer, "supports_locale", None)
            return callable(supports) and supports(locale) is True
        # A selected customer profile with an absent/unknown component is not
        # evidence of readiness. Only the existing default backend may use the
        # legacy product-wide forwarding contract without this declaration.
        return os.getenv("VIOLA_KOKORO_PHONEMIZER", "espeak") == "espeak"
    except Exception:
        return False


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

        language = _parse_phone_language(detected_lang)
        if (
            language is None
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.7 <= confidence <= 1.0
        ):
            self._consecutive_non_english = 0
            return
        lang = language.value.split("-", 1)[0]
        if lang == "en":
            self._consecutive_non_english = 0
            return

        self._consecutive_non_english += 1

        if self._consecutive_non_english >= self._switch_threshold:
            await self._switch_language(language)

    async def _switch_language(self, lang_code: str):
        from pipecat.frames.frames import LLMMessagesAppendFrame

        language = _parse_phone_language(lang_code)
        lang_code = language.value.split("-", 1)[0] if language is not None else "unknown"
        if lang_code == "cmn":
            lang_code = "zh"
        lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)

        if lang_code in KOKORO_SUPPORTED:
            # The pinned Kokoro service uses settings frames, not set_language.
            # Other phone providers do not necessarily consume that setting.
            from pipecat.frames.frames import TTSUpdateSettingsFrame
            from pipecat.processors.frame_processor import FrameDirection

            try:
                from pipecat.services.kokoro.tts import KokoroTTSService
            except Exception as exc:
                logger.warning("Kokoro language switching is unavailable: %s", exc)
                return

            if not isinstance(self._tts, KokoroTTSService):
                logger.warning("Phone TTS cannot switch language to %s", lang_code)
                return

            runtime = getattr(self._tts, "_kokoro", None)
            customer = getattr(getattr(runtime, "tokenizer", None), "_customer", None)
            if customer is not None:
                from voice.customer_voice_routing import customer_phone_locale_is_explicit

                if not customer_phone_locale_is_explicit(language):
                    await self._report_unsupported_language(
                        language.value, "Chinese (regional variety)", selected_backend=True
                    )
                    return
            locale = self._tts.language_to_service_language(language)
            if not _selected_backend_supports_locale(self._tts, locale):
                await self._report_unsupported_language(lang_code, lang_name, selected_backend=True)
                return
            previous_language = self._tts._settings.language
            previous_voice = self._tts._settings.voice
            selected_voice = previous_voice
            if customer is not None:
                from voice.customer_voice_routing import select_customer_voice

                try:
                    selected_voice = select_customer_voice(locale, previous_voice, runtime.get_voices())
                except Exception:
                    await self._report_unsupported_language(
                        lang_code, lang_name, selected_backend=True, voice_unavailable=True
                    )
                    return
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
                delta = {"language": language}
                if customer is not None:
                    delta["voice"] = selected_voice
                await self._tts.process_frame(
                    TTSUpdateSettingsFrame(delta=KokoroTTSService.Settings(**delta), service=self._tts),
                    FrameDirection.DOWNSTREAM,
                )
                if self._tts._settings.language != self._tts.language_to_service_language(language):
                    raise RuntimeError("Kokoro did not apply the requested language")
                if self._tts._settings.voice != selected_voice:
                    raise RuntimeError("Kokoro did not apply the matching voice")
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
                    self._tts._settings.voice = previous_voice
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
            await self._report_unsupported_language(lang_code, lang_name)

    async def _report_unsupported_language(
        self, lang_code: str, lang_name: str, *, selected_backend: bool = False, voice_unavailable: bool = False
    ):
        from pipecat.frames.frames import LLMMessagesAppendFrame

        context = "\n".join(
            [
                "phone_event: language_detected",
                "source: asr_language_metadata",
                "recipient_language_code: %s" % lang_code,
                "recipient_language_name: %s" % lang_name,
                "tts_language_supported: false",
            ]
        )
        if selected_backend:
            context += "\ntts_language_scope: selected_pronunciation_backend"
        if voice_unavailable:
            context += "\ntts_voice_available: false"
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
