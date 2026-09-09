"""
Text-to-Speech Synthesis Interface for Voice Pipeline

BACKWARD COMPATIBILITY MODULE: This module re-exports TTS components from
the canonical voice/synthesis/ package. New code should import directly from
voice.synthesis.

The canonical TTS implementation lives in voice/synthesis/engine.py with the
factory pattern in voice/synthesis/factory.py.

Migration guide:
    # Old (deprecated)
    from voice.synthesizer import Synthesizer

    # New (preferred)
    from voice.synthesis import TTSEngine, TTSPort, LocalTTS
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from config import AppConfig
from core.logging_config import get_logger

# Import canonical TTS components
from voice.synthesis import LocalTTS, TTSEngine, TTSPort

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Sentence boundary: split after sentence-ending punctuation followed by
# whitespace.  Matches the same regex used by Kokoro's text preprocessor
# so chunking behaviour is consistent between streaming and batch paths.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# Re-export TTSPort as SynthesizerPort for backward compatibility
SynthesizerPort = TTSPort


def split_sentences(text: str) -> tuple[list[str], str]:
    """Split *text* at sentence boundaries, returning complete sentences and remainder.

    A sentence boundary is a period, exclamation mark, or question mark
    followed by whitespace.  The last fragment is treated as incomplete
    unless it ends with sentence-ending punctuation.

    Returns:
        ``(complete_sentences, remainder)`` where *remainder* is the
        trailing text that has not yet reached a sentence boundary.
    """
    parts = _SENTENCE_SPLIT_RE.split(text)
    if not parts:
        return [], text
    # Last part might be an incomplete sentence
    complete = parts[:-1]
    remainder = parts[-1]
    # If remainder ends with sentence-ending punctuation, it is complete
    if remainder and remainder.rstrip()[-1:] in ".!?":
        complete.append(remainder)
        remainder = ""
    return [s for s in complete if s.strip()], remainder


class Synthesizer:
    """
    Unified text-to-speech synthesis abstraction.

    DEPRECATED: This class wraps TTSEngine from voice/synthesis/ package.
    New code should use voice.synthesis.TTSEngine or voice.synthesis.LocalTTS directly.

    This class is kept for backward compatibility with existing voice pipeline code.
    """

    def __init__(
        self,
        config: AppConfig,
        implementation: TTSPort | None = None,
    ):
        """
        Initialize synthesizer.

        Args:
            config: Application configuration
            implementation: Concrete synthesizer implementation (auto-created if None)
        """
        self.config = config
        self._impl = implementation or self._create_implementation()

    def _create_implementation(self) -> TTSPort | None:
        """Create TTS implementation based on config.

        Tries Kokoro (default) then falls back to pyttsx3.
        """
        # Try Kokoro first (default backend). This shares the ONE process-wide
        # engine with BootstrapFactory.create_tts rather than building a second
        # one: two engines meant two 325 MB ONNX loads and two concurrent
        # opener-cache builds during the user's first minutes, and left the
        # startup TTS prewarm warming an engine the /v1/command reply path never
        # spoke through. See voice.synthesis.factory.get_shared_kokoro.
        try:
            from voice.synthesis.factory import get_shared_kokoro

            kokoro = get_shared_kokoro(self.config)
            if kokoro is not None and kokoro.is_available():
                logger.info("Synthesizer using Kokoro TTS backend (shared engine)")
                return kokoro
        except Exception as e:
            logger.debug("Kokoro TTS not available: %s", e)

        # Fallback to pyttsx3
        try:
            tts_engine = TTSEngine(config=self.config)
            logger.info("Synthesizer using pyttsx3 TTS fallback")
            return LocalTTS(tts_engine)
        except Exception as e:
            logger.error("Failed to create synthesizer: %s", e)
            return None

    def prewarm(self) -> None:
        """Prewarm the underlying TTS engine (eager model load) if supported.

        Mirrors Transcriber.prewarm: delegates to the implementation's own
        ``prewarm`` when present so the first spoken reply doesn't pay the model
        load inline (critical_path.md WL-2). Best-effort — never raises.
        """
        if self._impl is not None and hasattr(self._impl, "prewarm"):
            try:
                prewarm_fn = getattr(self._impl, "prewarm", None)
                if callable(prewarm_fn):
                    prewarm_fn()
            except (RuntimeError, OSError, ImportError, ValueError, AttributeError) as exc:
                logger.debug("Synthesizer prewarm skipped: %s", exc)

    async def speak(self, text: str) -> None:
        """
        Speak text.

        Args:
            text: Text to speak
        """
        if self._impl is None:
            logger.warning("Synthesizer not available")
            return

        await self._impl.speak(text)

    async def speak_streaming(self, text_chunks: AsyncIterator[str]) -> str:
        """Speak text as it arrives from an LLM stream, sentence by sentence.

        Each chunk from *text_chunks* may be a partial sentence (typically a
        few tokens).  Text is buffered until a sentence boundary is detected,
        then that sentence is synthesised and played immediately while the LLM
        continues generating subsequent tokens.

        Args:
            text_chunks: Async iterator yielding text fragments (LLM tokens).

        Returns:
            The full concatenated text that was spoken (for logging/history).
        """
        if self._impl is None:
            logger.warning("Synthesizer not available for streaming")
            # Drain the iterator and return the collected text
            collected: list[str] = []
            async for chunk in text_chunks:
                collected.append(chunk)
            return "".join(collected)

        # Delegate to the implementation if it supports streaming natively
        if hasattr(self._impl, "speak_streaming"):
            return await self._impl.speak_streaming(text_chunks)

        # Fallback: buffer into sentences and call speak() per sentence
        buffer = ""
        full_text_parts: list[str] = []
        async for chunk in text_chunks:
            full_text_parts.append(chunk)
            buffer += chunk
            sentences, buffer = split_sentences(buffer)
            for sentence in sentences:
                if sentence.strip():
                    await self._impl.speak(sentence)

        # Speak any remaining buffered text
        if buffer.strip():
            await self._impl.speak(buffer)

        return "".join(full_text_parts)

    def is_available(self) -> bool:
        """Check if synthesizer is available"""
        if self._impl is None:
            return False
        if hasattr(self._impl, "is_available"):
            return self._impl.is_available()
        return True


# Backward compatibility alias
SynthesizerAdapter = LocalTTS

__all__ = [
    "Synthesizer",
    "SynthesizerAdapter",
    "SynthesizerPort",
    "split_sentences",
]
