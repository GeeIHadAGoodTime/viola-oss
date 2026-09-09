"""
ASR Factory for Local vs Cloud Selection

Provides factory pattern for creating ASR components with fallback support.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol

from config import AppConfig
from config.policy import PolicyConfig
from core.logging_config import get_logger
from voice.transcription.whisper import WhisperTranscriber

logger = get_logger(__name__)


class ASRPort(Protocol):
    """Protocol for ASR (local or cloud)"""

    async def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe audio to text"""
        ...

    def transcribe_sync(self, audio_path: str | Path) -> str:
        """Transcribe audio to text (synchronous)"""
        ...

    def is_available(self) -> bool:
        """Check if ASR is available (online/offline)"""
        ...


class LocalASR:
    """Local ASR wrapper for WhisperTranscriber"""

    def __init__(self, transcriber: WhisperTranscriber):
        self.transcriber = transcriber

    async def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe audio to text (async wrapper)"""
        # WhisperTranscriber is synchronous, so we just call it directly
        # In a real async implementation, this would use asyncio.to_thread
        import asyncio

        return await asyncio.to_thread(self.transcriber.transcribe, audio_path)

    def transcribe_sync(self, audio_path: str | Path) -> str:
        """Transcribe audio to text (synchronous)"""
        return self.transcriber.transcribe(audio_path)

    def is_available(self) -> bool:
        """Local ASR is always available (offline)"""
        return True


class ASRFactory:
    """Factory for creating ASR components with fallback support"""

    def __init__(self, config: AppConfig, policy: PolicyConfig | None = None):
        self.config = config
        self.policy = policy or self._create_default_policy()
        self._local_asr: LocalASR | None = None

    def _create_default_policy(self):
        """Create default policy from config"""
        from config.policy import create_default_policy

        return create_default_policy()

    def create_primary(self) -> ASRPort:
        """
        Create primary ASR component based on policy and consent.

        Returns:
            Primary ASR component (local or cloud)
        """
        # If offline_only, always use local
        if self.policy.offline_only:
            logger.info("Offline-only mode: Using local ASR")
            return self.create_local()

        # Privacy consent gate: cloud STT requires explicit user opt-in
        from core.privacy_consent import is_cloud_stt_consented

        if not is_cloud_stt_consented():
            logger.info(
                "Cloud STT consent not given — using local ASR only. " "Say 'enable cloud transcription' to turn it on."
            )
            return self.create_local()

        # Default to local
        logger.info("Using local ASR")
        return self.create_local()

    def create_local(self) -> LocalASR:
        """Create local ASR component"""
        if self._local_asr is None:
            transcriber = WhisperTranscriber(self.config)
            self._local_asr = LocalASR(transcriber)
        return self._local_asr

    def create_with_fallback(self) -> ASRPort:
        """
        Create ASR component with automatic fallback

        Returns:
            ASR component with fallback chain (primary → local)
        """
        return ASRFallbackChain(
            primary=self.create_primary(),
            fallback=self.create_local(),
            policy=self.policy,
        )


_TRANSCRIBER_SINGLETON: ASRPort | None = None
_TRANSCRIBER_LOCK = threading.Lock()


def get_transcriber() -> ASRPort | None:
    """
    Get the global transcriber instance (singleton).

    Returns:
        The transcriber instance, or None if not initialized.
    """
    global _TRANSCRIBER_SINGLETON
    return _TRANSCRIBER_SINGLETON


def set_transcriber(transcriber: ASRPort | None) -> None:
    """
    Set the global transcriber instance.

    Args:
        transcriber: The transcriber to use globally.
    """
    global _TRANSCRIBER_SINGLETON
    with _TRANSCRIBER_LOCK:
        _TRANSCRIBER_SINGLETON = transcriber


def create_default_transcriber() -> ASRPort:
    """
    Create a default transcriber using app config.

    Returns:
        A transcriber configured from application settings.
    """
    from config import settings

    factory = ASRFactory(settings)
    return factory.create_with_fallback()


class ASRFallbackChain:
    """ASR component with automatic fallback support"""

    def __init__(self, primary: ASRPort, fallback: ASRPort, policy: PolicyConfig):
        self.primary = primary
        self.fallback = fallback
        self.policy = policy

    async def transcribe(self, audio_path: str | Path) -> str:
        """
        Transcribe audio with automatic fallback

        Args:
            audio_path: Path to audio file

        Returns:
            Transcribed text
        """
        # Try primary first
        try:
            if hasattr(self.primary, "transcribe"):
                result = await self.primary.transcribe(audio_path)
                if result:
                    return result
        except Exception as e:
            logger.warning("Primary ASR failed: %s, falling back to local", e)

        # Fallback to local if graceful degradation is enabled
        if self.policy.degrade_gracefully:
            try:
                if hasattr(self.fallback, "transcribe"):
                    result = await self.fallback.transcribe(audio_path)
                    return result
            except Exception as e:
                logger.error("Fallback ASR also failed: %s", e)
                raise

        raise RuntimeError("Both primary and fallback ASR failed")

    def transcribe_sync(self, audio_path: str | Path) -> str:
        """Transcribe audio synchronously with fallback"""
        # Try primary first
        try:
            if hasattr(self.primary, "transcribe_sync"):
                result = self.primary.transcribe_sync(audio_path)
                if result:
                    return result
        except Exception as e:
            logger.warning("Primary ASR failed: %s, falling back to local", e)

        # Fallback to local
        if self.policy.degrade_gracefully:
            if hasattr(self.fallback, "transcribe_sync"):
                return self.fallback.transcribe_sync(audio_path)

        raise RuntimeError("Both primary and fallback ASR failed")

    def is_available(self) -> bool:
        """Check if ASR is available (at least fallback must be available)"""
        return self.fallback.is_available() if hasattr(self.fallback, "is_available") else True
