"""TTS Factory for Local vs Cloud Selection.

Provides factory pattern for creating TTS components with fallback support.
Default backend is Kokoro-82M (neural, local, Apache-2.0 licensed).
Fallback chain: configured primary → pyttsx3 (local).
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING, Protocol

from config import AppConfig
from config.policy import PolicyConfig
from core.exceptions import SynthesisError
from core.logging_config import get_logger
from voice.synthesis.engine import TTSEngine

if TYPE_CHECKING:
    from voice.synthesis.kokoro_engine import KokoroTTSEngine

logger = get_logger(__name__)

_MAX_TTS_DEDUP = 3


class TTSPort(Protocol):
    """Protocol for TTS (local or cloud)."""

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to raw 16 kHz / 16-bit / mono PCM bytes."""
        ...

    async def speak(self, text: str) -> None:
        """Speak text directly (synchronous playback)."""
        ...

    def is_available(self) -> bool:
        """Check if TTS is available (online/offline)."""
        ...


class LocalTTS:
    """Local TTS wrapper for pyttsx3 TTSEngine (emergency fallback)."""

    def __init__(self, tts_engine: TTSEngine):
        self.tts_engine = tts_engine

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """pyttsx3 cannot produce audio bytes — returns empty.

        Use KokoroTTSEngine for byte-level synthesis.
        """
        logger.debug(
            "LocalTTS (pyttsx3) synthesize — no byte output (text length: %s)",
            len(text),
        )
        return b""

    async def speak(self, text: str) -> None:
        """Speak text directly using pyttsx3 TTSEngine."""
        await self.tts_engine.speak(text)

    def is_available(self) -> bool:
        """pyttsx3 local TTS is always available (offline)."""
        return True


def create_kokoro(config: AppConfig | None = None) -> KokoroTTSEngine | None:
    """Create a Kokoro TTS engine instance.

    Returns None if the model files are not present on disk.
    """
    try:
        from voice.synthesis.kokoro_engine import KokoroTTSEngine

        engine = KokoroTTSEngine(config=config)
        if engine.is_available():
            logger.info("Kokoro TTS engine created (model files present)")
            return engine
        logger.warning("Kokoro model files not found — skipping Kokoro backend")
        return None
    except Exception as exc:
        logger.warning("Failed to create Kokoro TTS engine: %s", exc)
        return None


_shared_kokoro_lock = threading.Lock()
_shared_kokoro: KokoroTTSEngine | None = None
_shared_kokoro_attempted = False


def _forward_generic_voice(config: AppConfig | None) -> None:
    """Copy a user-set generic ``tts_voice`` onto ``tts_kokoro_voice``.

    Lives here rather than in ``TTSFactory.create_primary`` so that every
    caller of :func:`get_shared_kokoro` gets the same voice, whichever one
    happens to construct the shared engine first. Two engines built from
    different voice values would also hash to two different opener-cache files
    (``OpenerCache.cache_hash``), so this is what keeps one cache one cache.
    """
    if config is None:
        return
    generic_voice = getattr(config, "tts_voice", "default")
    kokoro_voice = getattr(config, "tts_kokoro_voice", "af_heart")
    if generic_voice != "default" and kokoro_voice == "af_heart":
        try:
            config.tts_kokoro_voice = generic_voice
        except AttributeError:
            pass


def get_shared_kokoro(config: AppConfig | None = None) -> KokoroTTSEngine | None:
    """Return the ONE process-wide Kokoro engine, building it on first call.

    The Kokoro ONNX model is ~325 MB and each engine instance also kicks off its
    own 50-render opener-cache build (10 canonical openers x 5 speed variants).
    The desktop used to construct two independent engines -- one via
    ``BootstrapFactory.create_tts`` (what ``/v1/command`` speaks its reply
    through) and one via ``voice.synthesizer.Synthesizer`` (what the voice
    pipeline speaks wake acknowledgements and voice-turn replies through) -- so a
    cold start paid the model load twice and ran two opener-cache builds
    concurrently on the same CPU the user was trying to talk to, racing on the
    same ``openers.<hash>.npz.tmp`` path. Worse, the startup prewarm
    (``VoicePipelineInitializer._prewarm_synthesizer``) warmed only the pipeline's
    engine, so the ``/v1/command`` reply path still paid the full load inline on
    the first spoken reply -- the exact stall that prewarm exists to remove.

    One shared engine makes the prewarm warm the engine that actually speaks.
    Concurrent synthesis stays safe: ``KokoroTTSEngine`` already serialises ONNX
    inference on its own ``threading.Lock``, which is why the cloud app has
    always run a single shared instance (``backend/cloud_app.py`` ``app.state.tts``).
    """
    global _shared_kokoro, _shared_kokoro_attempted

    with _shared_kokoro_lock:
        if _shared_kokoro is not None or _shared_kokoro_attempted:
            return _shared_kokoro
        _shared_kokoro_attempted = True
        _forward_generic_voice(config)
        _shared_kokoro = create_kokoro(config)
        return _shared_kokoro


def reset_shared_kokoro_for_tests() -> None:
    """Drop the process-wide Kokoro engine so a test can build a fresh one."""
    global _shared_kokoro, _shared_kokoro_attempted

    with _shared_kokoro_lock:
        _shared_kokoro = None
        _shared_kokoro_attempted = False


class TTSFactory:
    """Factory for creating TTS components with fallback support."""

    def __init__(self, config: AppConfig, policy: PolicyConfig | None = None):
        self.config = config
        self.policy = policy or self._create_default_policy()
        self._kokoro_tts: KokoroTTSEngine | None = None
        self._local_tts: LocalTTS | None = None

    def _create_default_policy(self):
        """Create default policy from config."""
        from config.policy import create_default_policy

        return create_default_policy()

    def create_primary(self) -> TTSPort:
        """Create primary TTS component based on configured backend.

        Wires the generic ``tts_voice`` setting through to engine-specific
        voice configuration when no engine-specific voice has been set.

        Returns:
            Primary TTS component.
        """
        backend = getattr(self.config, "tts_backend", "kokoro")

        if backend == "kokoro":
            # If the user set a generic tts_voice and the Kokoro-specific
            # voice is still the default, forward the generic voice.
            _forward_generic_voice(self.config)

            kokoro = self.create_kokoro()
            if kokoro is not None:
                logger.info("Using Kokoro TTS (neural, local)")
                return kokoro
            logger.warning("Kokoro unavailable, falling back to pyttsx3")
            return self.create_local()

        # backend == "pyttsx3" or unknown
        logger.info("Using pyttsx3 TTS (system)")
        return self.create_local()

    def create_kokoro(self) -> KokoroTTSEngine | None:
        """Return the process-wide Kokoro TTS engine.

        Shared rather than per-factory: see :func:`get_shared_kokoro` for why a
        second instance costs a second 325 MB model load and a duplicate
        opener-cache build on the user's first minutes.
        """
        if self._kokoro_tts is None:
            self._kokoro_tts = get_shared_kokoro(self.config)
        return self._kokoro_tts

    def create_local(self) -> LocalTTS:
        """Create pyttsx3 local TTS component."""
        if self._local_tts is None:
            tts_engine = TTSEngine(config=self.config)
            self._local_tts = LocalTTS(tts_engine)
        return self._local_tts

    def create_with_fallback(self) -> TTSPort:
        """Create TTS component with automatic fallback.

        Fallback chain: configured primary → pyttsx3 local.
        """
        primary = self.create_primary()
        fallback = self.create_local()
        return TTSFallbackChain(
            primary=primary,
            fallback=fallback,
            policy=self.policy,
        )


class TTSFallbackChain:
    """TTS component with automatic fallback support."""

    def __init__(self, primary: TTSPort, fallback: TTSPort, policy: PolicyConfig):
        self.primary = primary
        self.fallback = fallback
        self.policy = policy
        self._recent_tts_hashes: list[str] = []

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text with automatic fallback."""
        from config.settings import settings as _settings

        if not getattr(_settings, "tts_enabled", True):
            return b""

        try:
            result = await self.primary.synthesize(text, voice)
            if result:
                return result
        except Exception as e:
            logger.warning("Primary TTS synthesize failed: %s, falling back", e)

        if self.policy.degrade_gracefully:
            try:
                return await self.fallback.synthesize(text, voice)
            except Exception as e:
                logger.error("Fallback TTS also failed: %s", e)
                raise

        raise SynthesisError("Both primary and fallback TTS failed", text=text)

    async def speak(self, text: str) -> None:
        """Speak text with automatic fallback."""
        from config.settings import settings as _settings

        if not getattr(_settings, "tts_enabled", True):
            return

        # C4: Voice response dedup — suppress identical consecutive TTS
        _h = hashlib.md5(text.strip().lower().encode(), usedforsecurity=False).hexdigest()
        if _h in self._recent_tts_hashes:
            logger.info("Suppressing duplicate TTS response length=%d", len(text))
            return
        self._recent_tts_hashes.append(_h)
        if len(self._recent_tts_hashes) > _MAX_TTS_DEDUP:
            self._recent_tts_hashes.pop(0)

        try:
            await self.primary.speak(text)
            return
        except Exception as e:
            logger.warning("Primary TTS speak failed: %s, falling back", e)

        if self.policy.degrade_gracefully:
            await self.fallback.speak(text)
            return

        raise SynthesisError("Both primary and fallback TTS failed", text=text)

    def is_available(self) -> bool:
        """Check if at least one TTS engine is available."""
        if hasattr(self.primary, "is_available") and self.primary.is_available():
            return True
        return self.fallback.is_available() if hasattr(self.fallback, "is_available") else True
