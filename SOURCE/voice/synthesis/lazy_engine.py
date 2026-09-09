"""
Lazy TTS Loading

Defer TTS engine initialization until first use.
This saves ~50-100MB memory at startup if user never uses voice responses.

Moved from utils/lazy_tts.py to consolidate TTS code in voice/synthesis/ package.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

from core.logging_config import get_logger

logger = get_logger(__name__)


class LazyTTSEngine:
    """
    Lazy wrapper for TTS engine - only loads on first use.

    Lazy feature loading reduces startup memory footprint.

    Usage:
        tts = LazyTTSEngine(config=settings)
        # TTS engine not loaded yet - memory saved!

        await tts.speak("Hello")  # Now loads and uses TTS engine
    """

    def __init__(self, config: Any | None = None):
        """
        Initialize lazy TTS wrapper.

        Args:
            config: AppConfig instance (optional)
        """
        self._config = config
        self._engine: Any | None = None
        self._load_error: str | None = None

    def _ensure_loaded(self) -> bool:
        """Ensure TTS engine is loaded, return True if ready."""
        if self._engine is not None:
            return True

        if self._load_error is not None:
            return False

        try:
            logger.info("Loading TTS engine on first use...")
            from voice.synthesis.engine import TTSEngine

            self._engine = TTSEngine(config=self._config) if self._config else TTSEngine()
            logger.info("TTS engine loaded successfully")
            return True
        except Exception as e:
            self._load_error = str(e)
            logger.warning("Failed to load TTS engine: %s", e)
            return False

    async def speak(self, text: str, wait: bool = True) -> bool:
        """
        Speak text (loads engine on first use).

        Args:
            text: Text to speak
            wait: Whether to wait for speech to complete

        Returns:
            True if successful, False otherwise
        """
        if not self._ensure_loaded():
            return False

        try:
            engine = self._engine
            if engine is None:
                logger.error("TTS engine failed to load")
                return False

            speak_callable = getattr(engine, "speak", None)
            if speak_callable is None or not callable(speak_callable):
                logger.error("TTS engine does not expose a callable 'speak'")
                return False

            if inspect.iscoroutinefunction(speak_callable):
                # Bound coroutine functions still need to be awaited
                result = await cast(Any, speak_callable)(text)
            else:
                # Many engines return None on success; treat that as True
                result = cast(Any, speak_callable)(text)

            if isinstance(result, bool):
                return result

            return result is None or bool(result)
        except Exception as e:
            logger.error("TTS speak error: %s", e)
            return False

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to PCM bytes (loads engine on first use).

        The underlying pyttsx3 engine cannot produce audio bytes, so this
        returns empty bytes.  Use KokoroTTSEngine for byte-level synthesis.
        """
        logger.debug("LazyTTSEngine.synthesize — pyttsx3 cannot produce bytes")
        return b""

    def is_available(self) -> bool:
        """Check if TTS engine is available (loaded or can be loaded)."""
        return self._engine is not None or self._ensure_loaded()

    def __getattr__(self, name: str):
        """
        Delegate attribute access to underlying TTS engine.
        Loads engine if needed.
        """
        if not self._ensure_loaded():
            raise AttributeError("TTS engine not available: %s" % self._load_error)

        engine = self._engine
        if engine is None:
            raise AttributeError("TTS engine failed to initialize")

        return getattr(engine, name)
