"""Voice channel adapter — wraps the existing voice pipeline as a MessageChannel.

This adapter bridges the legacy TTS/STT voice pipeline into the new
channel-agnostic messaging architecture.  It preserves all existing
voice approval behaviour unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger
from voice.synthesis.text_normalizer import normalize_for_speech

logger = get_logger(__name__)

_LISTEN_TIMEOUT = 15.0  # seconds to wait for voice response


class VoiceChannel:
    """MessageChannel implementation backed by the voice pipeline (TTS + STT)."""

    channel_type = "voice"

    def __init__(
        self,
        tts_speaker: Any | None = None,
        voice_pipeline: Any | None = None,
        user_key: str = "owner",
    ) -> None:
        """
        Args:
            tts_speaker: Object with ``speak(text)`` async method (TTSSpeaker).
            voice_pipeline: Object with ``listen_and_record`` / ``transcribe`` methods.
            user_key: User identity for per-user history isolation.
        """
        self._tts = tts_speaker
        self._voice = voice_pipeline
        self._user_key = user_key

    @property
    def user_key(self) -> str:
        """Return the user identity associated with this voice channel."""
        return self._user_key

    # -- MessageChannel protocol -------------------------------------------------

    async def send(self, text: str) -> None:
        """Speak *text* via TTS."""
        if self._tts is None:
            return
        try:
            spoken_text = normalize_for_speech(text)
            if not spoken_text:
                return

            speak = getattr(self._tts, "speak", None)
            if callable(speak):
                await speak(spoken_text)
            else:
                say = getattr(self._tts, "say", None)
                if callable(say):
                    say(spoken_text)
        except Exception as exc:
            logger.debug("VoiceChannel.send TTS failed: %s", exc)

    async def send_image(self, path: str, caption: str = "") -> None:
        """Voice channel cannot send images — speak the caption instead."""
        if caption:
            await self.send(caption)

    async def ask(self, prompt: str, timeout: float = _LISTEN_TIMEOUT) -> str | None:
        """Speak *prompt* via TTS, then listen for a voice response.

        Returns transcribed text or ``None`` on timeout / failure.
        """
        await self.send(prompt)

        if self._voice is None:
            return None

        listen = getattr(self._voice, "listen_and_record", None)
        transcribe = getattr(self._voice, "transcribe", None)
        if not callable(listen) or not callable(transcribe):
            return None

        try:
            audio_data = await asyncio.wait_for(
                asyncio.to_thread(listen, timeout),
                timeout=timeout + 2.0,
            )
            if audio_data is None:
                return None

            result = await asyncio.to_thread(transcribe, audio_data)
            if isinstance(result, dict):
                return result.get("text")
            if isinstance(result, str):
                return result
            return None
        except (TimeoutError, Exception) as exc:
            logger.debug("VoiceChannel.ask listen failed: %s", exc)
            return None

    async def send_typing(self) -> None:
        """Voice has no typing indicator — no-op."""

    # -- Helpers -----------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if both TTS and STT are available."""
        if self._voice is None:
            return False
        return callable(getattr(self._voice, "listen_and_record", None)) and callable(
            getattr(self._voice, "transcribe", None)
        )
