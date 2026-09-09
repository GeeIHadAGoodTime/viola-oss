"""
Voice Transcriber Facade

Provides a consistent transcription interface for the voice pipeline.
Delegates to the canonical STT implementation in voice/transcription/.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from core.logging_config import get_logger

if TYPE_CHECKING:
    from config import AppConfig
    from voice.transcription.whisper import (
        WhisperTranscriber as _WhisperTranscriberType,
    )

logger = get_logger(__name__)


def _configured_stt_engine(config: AppConfig) -> str:
    try:
        from ui.settings_manager import get_settings_manager

        value = get_settings_manager().get("stt_engine", getattr(config, "stt_backend", "whisper_local"))
    except Exception:
        value = getattr(config, "stt_backend", "whisper_local")
    return str(value or "whisper_local")


# Re-export the canonical protocol from voice/transcription/
# This avoids duplicating the protocol definition
try:
    from voice.transcription.factory import ASRPort as TranscriberPort
except ImportError:  # pragma: no cover - fallback if factory unavailable
    from typing import Protocol

    class TranscriberPort(Protocol):
        """Protocol for transcription implementations (fallback)."""

        def transcribe(self, audio_path: str | Path | np.ndarray, preprocess: bool = False) -> str:
            """Transcribe audio to text (synchronous). Accepts path or numpy buffer."""
            ...

        def is_available(self) -> bool:
            """Check if transcriber is available."""
            ...


class Transcriber:
    """
    Voice pipeline transcription facade.

    Wraps the canonical voice.transcription.whisper.WhisperTranscriber behind
    a consistent interface with lazy loading and error recovery.

    This is NOT a reimplementation - it delegates to voice/transcription/ for actual transcription.
    """

    def __init__(
        self,
        config: AppConfig,
        implementation: TranscriberPort | _WhisperTranscriberType | None = None,
    ):
        """
        Initialize transcriber.

        Args:
            config: Application configuration
            implementation: Concrete transcriber implementation (auto-created if None)
        """
        self.config = config
        self._impl: TranscriberPort | _WhisperTranscriberType | None = (
            implementation if implementation is not None else self._create_implementation()
        )

    def _create_implementation(
        self,
    ) -> TranscriberPort | _WhisperTranscriberType | None:
        """Create transcription implementation using canonical stt/ module."""
        engine = _configured_stt_engine(self.config)
        self.config.stt_backend = engine
        if engine == "none":
            logger.info("STT transcriber disabled by stt_engine setting")
            return None
        try:
            from voice.transcription.whisper import WhisperTranscriber

            if engine == "whisper_api":
                logger.info("Cloud STT selected; using consent-gated Whisper API transcriber")
                return CloudWhisperTranscriber(self.config, WhisperTranscriber(self.config))
            return WhisperTranscriber(self.config)
        except Exception as e:
            logger.warning("Failed to create transcriber: %s", e)
            return None

    def transcribe(
        self,
        audio_source: str | Path | np.ndarray,
        preprocess: bool = False,
    ) -> str:
        """
        Transcribe audio to text from file path or numpy buffer.

        Args:
            audio_source: Path to audio file, or int16 numpy array (mono, 16kHz)
            preprocess: Whether to apply audio preprocessing (file-based only)

        Returns:
            Transcribed text, or empty string if failed
        """
        if self._impl is None:
            logger.error("Transcriber not available")
            return ""

        return self._impl.transcribe(audio_source, preprocess)

    def prewarm(self) -> None:
        """Prewarm the underlying transcription engine if supported."""
        if self._impl is not None and hasattr(self._impl, "prewarm"):
            try:
                prewarm_fn = getattr(self._impl, "prewarm", None)
                if callable(prewarm_fn):
                    prewarm_fn()
            except Exception as exc:
                logger.debug("Transcriber prewarm skipped: %s", exc)

    def get_language_hint(self, min_confidence: float = 0.85) -> str | None:
        """Return cached language hint from implementation if available."""
        if self._impl is not None and hasattr(self._impl, "get_language_hint"):
            try:
                get_hint_fn = getattr(self._impl, "get_language_hint", None)
                if callable(get_hint_fn):
                    result = get_hint_fn(min_confidence)
                    return str(result) if result is not None else None
            except Exception as exc:
                logger.debug("Transcriber language hint not available: %s", exc)
        return None

    def is_available(self) -> bool:
        """Check if transcriber is available."""
        if self._impl is None:
            return False
        if hasattr(self._impl, "is_available"):
            return self._impl.is_available()
        return True


class CloudWhisperTranscriber:
    """Consent-gated OpenAI Whisper API wrapper with local Whisper fallback."""

    def __init__(
        self,
        config: AppConfig,
        local_transcriber: TranscriberPort | _WhisperTranscriberType,
        cloud_adapter: object | None = None,
    ) -> None:
        self.config = config
        self._local = local_transcriber
        self._cloud_adapter = cloud_adapter

    def transcribe(
        self,
        audio_source: str | Path | np.ndarray,
        preprocess: bool = False,
    ) -> str:
        from core.privacy_consent import is_cloud_stt_consented

        if not is_cloud_stt_consented():
            logger.info("Cloud STT selected but consent is missing; using local Whisper")
            return self._local.transcribe(audio_source, preprocess=preprocess)

        adapter = self._get_cloud_adapter()
        if adapter is None or not adapter.is_available():
            logger.warning("Cloud STT selected but Whisper API is unavailable; using local Whisper")
            return self._local.transcribe(audio_source, preprocess=preprocess)

        audio_path, cleanup_path = self._cloud_audio_path(audio_source)
        try:
            result = self._run_cloud_transcription(adapter, str(audio_path))
            transcript = str(result.get("text", "") if isinstance(result, dict) else "").strip()
            if transcript:
                return transcript
            logger.warning("Cloud STT returned an empty transcript; using local Whisper")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Cloud STT failed; using local Whisper: %s", exc)
        finally:
            if cleanup_path is not None:
                self._cleanup_temp_wav(cleanup_path)

        return self._local.transcribe(audio_source, preprocess=preprocess)

    def is_available(self) -> bool:
        return True

    def prewarm(self) -> None:
        prewarm = getattr(self._local, "prewarm", None)
        if callable(prewarm):
            prewarm()

    def _get_cloud_adapter(self) -> object | None:
        if self._cloud_adapter is None:
            from voice.transcription.cloud_adapter import CloudASRAdapter

            self._cloud_adapter = CloudASRAdapter()
        return self._cloud_adapter

    def _cloud_audio_path(self, audio_source: str | Path | np.ndarray) -> tuple[Path, Path | None]:
        if isinstance(audio_source, np.ndarray):
            temp_path = _write_temp_wav(audio_source)
            return temp_path, temp_path
        return Path(audio_source), None

    def _run_cloud_transcription(self, adapter: object, audio_path: str) -> dict[str, object]:
        import asyncio

        transcribe = adapter.transcribe
        return asyncio.run(
            transcribe(
                audio_path,
                language=_cloud_language(self.config),
                user_id=_cloud_stt_user_id(),
            )
        )

    def _cleanup_temp_wav(self, temp_path: Path) -> None:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to delete temporary cloud STT WAV %s: %s", temp_path, exc)


def _write_temp_wav(audio_buffer: np.ndarray) -> Path:
    import tempfile
    import wave

    path = Path(tempfile.NamedTemporaryFile(prefix="viola_cloud_stt_", suffix=".wav", delete=False).name)
    audio_int16 = audio_buffer.astype(np.int16, copy=False)
    try:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(audio_int16.tobytes())
    except (OSError, wave.Error, ValueError, TypeError):
        path.unlink(missing_ok=True)
        raise
    return path


def _cloud_language(config: AppConfig) -> str:
    language = str(getattr(config, "whisper_language", "en") or "en").strip()
    return "en" if language == "auto" else language


def _cloud_stt_user_id() -> str:
    from core.user_context import get_current_user_id

    user_id = str(get_current_user_id() or "").strip()
    if not user_id or user_id == "default":
        raise ValueError("Cloud STT requires an authenticated user id")
    return user_id
