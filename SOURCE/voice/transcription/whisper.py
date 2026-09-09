# viola/stt/whisper_transcriber.py

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol, cast

from config import AppConfig
from core.logging_config import get_logger
from voice.transcription.whisper_model_cache import (
    model_available_offline,
    resolve_model_load,
)

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from faster_whisper import WhisperModel
except ImportError as exc:  # pragma: no cover - optional dependency
    WhisperModel = None
    _WHISPER_IMPORT_ERROR = exc
else:
    _WHISPER_IMPORT_ERROR = None


def _settings_value(key: str, default: object, *, user_id: str | None = None) -> object:
    try:
        from ui.settings_manager import get_settings_manager

        settings_mgr = get_settings_manager()
        try:
            value = settings_mgr.get(key, default, user_id=user_id)
        except TypeError:
            value = settings_mgr.get(key, default)
    except Exception:
        return default
    return default if value in (None, "") else value


class WhisperTranscriber:
    """Handles speech-to-text using faster-whisper."""

    def __init__(self, config: AppConfig, *, user_id: str | None = None):
        self.config = config
        self.user_id = user_id
        self.model = None
        self._language_hint: tuple[str, float, float] | None = None
        self._language_hint_ttl = max(60.0, float(getattr(self.config, "stt_language_hint_ttl_seconds", 1800)))
        self._is_prewarmed = False
        self._last_no_speech_prob: float = 0.0
        self._load_model()

    def _load_model(self):
        """
        Loads the whisper model into memory with graceful fallback.

        Fallback chain:
        1. Try primary model + compute_type from config
        2. Try safer compute_types (int8, default)
        3. Try smaller models (base -> tiny)
        4. Try CPU if GPU fails
        5. Raise error only if all fallbacks fail
        """
        if WhisperModel is None:
            raise RuntimeError(
                "faster-whisper is not installed. Install 'faster-whisper' to enable local STT "
                "or set stt_engine='none' / VIOLA_TEST_TRANSCRIBER=1 to disable speech input."
            ) from _WHISPER_IMPORT_ERROR

        # Use the correct attribute names from config
        model_name = str(
            _settings_value("whisper_model", getattr(self.config, "whisper_model", "base"), user_id=self.user_id)
        )
        device = str(
            _settings_value("whisper_device", getattr(self.config, "whisper_device", "cpu"), user_id=self.user_id)
        )
        compute_type = getattr(self.config, "stt_compute_type", "default")

        # Raspberry Pi optimization: Use optimal model based on available resources
        if getattr(self.config, "lightweight_mode", False):
            try:
                from performance.pi_optimizations import get_optimal_whisper_model

                model_name = get_optimal_whisper_model(self.config)
                logger.info(
                    "Lightweight mode: Using optimal Whisper model '%s' for Pi",
                    model_name,
                )
            except ImportError:
                # Pi optimizations optional
                pass

        # Fallback models (smaller is better for fallback)
        fallback_models = ["base", "small", "tiny"]
        if model_name not in fallback_models:
            fallback_models.insert(0, model_name)

        # Compute types to try: user's preference, then safer fallbacks
        # float16 requires GPU support; int8/default are safer for CPU
        compute_fallbacks = [compute_type]
        if compute_type not in ("int8", "default"):
            compute_fallbacks.append("int8")
        if compute_type != "default":
            compute_fallbacks.append("default")

        # Try combinations of model + compute_type.
        #
        # A candidate that is neither baked into the build nor already cached has
        # to reach HuggingFace. On a machine with no usable network (offline, or
        # a TLS-inspecting corporate proxy) every such candidate pays its own
        # full connection timeout, so the chain below turns one unreachable
        # network into N stacked timeouts before it reports failure -- and
        # because construction raises, nothing is cached and the whole wait is
        # repaid on the user's next push-to-talk. Once the first network-needing
        # candidate has failed we therefore stop attempting further
        # network-needing candidates and keep only the ones that can load from
        # disk. Locally-available candidates (including every compute_type
        # fallback for an already-present model) are unaffected.
        last_error = None
        network_download_failed = False
        for attempt_model in fallback_models:
            available_offline = model_available_offline(attempt_model)
            if not available_offline and network_download_failed:
                logger.warning(
                    "Skipping whisper model '%s': it is not bundled or cached and a previous "
                    "download attempt already failed, so retrying would only stack another "
                    "network timeout.",
                    attempt_model,
                )
                continue
            for attempt_compute in compute_fallbacks:
                try:
                    logger.info(
                        "Loading whisper model '%s' on device '%s' with compute_type '%s'...",
                        attempt_model,
                        device,
                        attempt_compute,
                    )
                    self.model = WhisperModel(
                        attempt_model,
                        device=device,
                        compute_type=attempt_compute,
                        **resolve_model_load(attempt_model),
                    )
                    logger.info(
                        "Whisper model '%s' loaded successfully (compute_type=%s).",
                        attempt_model,
                        attempt_compute,
                    )
                    return
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "Failed to load model '%s' with compute_type='%s': %s",
                        attempt_model,
                        attempt_compute,
                        e,
                    )
                    if not available_offline:
                        # Re-read availability: a failure AFTER the weights
                        # landed (an unsupported compute_type on this CPU) leaves
                        # the snapshot cached, and the remaining compute_types
                        # are then worth trying because they cost no network. A
                        # snapshot that is still absent means the fetch itself
                        # failed, and every remaining compute_type would repeat
                        # the same download against the same dead network.
                        if model_available_offline(attempt_model):
                            available_offline = True
                        else:
                            network_download_failed = True
                            break
                    continue

            # All compute types failed for this model, try CPU fallback if not already on CPU
            if device != "cpu" and (available_offline or not network_download_failed):
                try:
                    logger.info("Trying CPU fallback for model '%s'...", attempt_model)
                    self.model = WhisperModel(
                        attempt_model,
                        device="cpu",
                        compute_type="int8",
                        **resolve_model_load(attempt_model),
                    )
                    logger.info("Whisper model '%s' loaded on CPU (fallback).", attempt_model)
                    return
                except Exception as e2:
                    logger.debug("CPU fallback also failed: %s", e2)

        # All fallbacks failed
        logger.critical(
            "Failed to load whisper model: All fallback attempts failed. Last error: %s",
            last_error,
        )
        logger.critical("Please ensure the model name is correct and dependencies are installed.")
        raise RuntimeError(f"Failed to load Whisper model after all fallbacks: {last_error}")

    def prewarm(self) -> None:
        """
        Prewarm the Whisper model to reduce latency of the first transcription.
        """
        if self.model is None or self._is_prewarmed:
            return
        try:

            class _HasDecoder(Protocol):
                def _get_decoder(self) -> object: ...

            if hasattr(self.model, "_get_decoder"):
                cast(_HasDecoder, self.model)._get_decoder()
            self._is_prewarmed = True
            logger.debug("Whisper decoder prewarmed.")
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.debug("Whisper prewarm skipped: %s", exc)
            self._is_prewarmed = True

    def transcribe(
        self,
        audio_source: str | Path | np.ndarray,
        preprocess: bool = False,
    ) -> str:
        """
        Transcribes audio to text from a file path or numpy array.

        Args:
            audio_source: Path to audio file, or int16 numpy array of audio samples.
                          When passing a numpy array, it must be int16 mono at 16kHz.
            preprocess: Whether to apply audio preprocessing (noise reduction, etc.)
                       Default False since it requires optional dependencies.
                       Only applicable to file-based sources.

        Returns:
            str: The transcribed text, or an empty string on failure.
        """
        import numpy as np

        if not self.model:
            logger.error("Transcription model is not loaded.")
            return ""

        # Determine if source is a numpy buffer or file path
        is_buffer = isinstance(audio_source, np.ndarray)

        if is_buffer:
            # Convert int16 buffer to float32 normalized [-1.0, 1.0] for faster-whisper
            audio_float = audio_source.astype(np.float32) / 32768.0
            transcribe_input = audio_float
            logger.debug(
                "Transcribing from buffer: %d samples (%.2fs at 16kHz)",
                len(audio_source),
                len(audio_source) / 16000.0,
            )
        else:
            audio_path = Path(audio_source)

            # Optional audio preprocessing for better accuracy (disabled by default)
            if preprocess and getattr(self.config, "enable_audio_preprocessing", False):
                try:
                    from utils.audio_preprocessing import preprocess_audio_for_stt

                    logger.debug("Applying audio preprocessing for better STT accuracy...")
                    audio_path = preprocess_audio_for_stt(
                        audio_path,
                        enable_noise_reduction=True,
                        enable_normalization=True,
                        noise_strength=0.5,
                    )
                except Exception as e:
                    logger.debug("Audio preprocessing failed (continuing anyway): %s", e)

            transcribe_input = str(audio_path)

        # Try transcription with graceful fallback
        import time as _time_mod

        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                # Greedy decode (beam=1) on the happy path: measured content-
                # identical to beam=5 on the STT fixture oracle + longer/noisy
                # utterances, at 13-18% lower decode latency on every voice turn
                # (LEVERAGE_RANKING.md §1 #8 / critical_path.md WL-5, 2026-07-02).
                # The retry after an empty transcript widens to beam=5 so the
                # fallback gains decode diversity instead of losing it.
                beam_size = 1 if attempt == 0 else 5
                configured_language = _settings_value(
                    "whisper_language",
                    getattr(self.config, "whisper_language", None),
                    user_id=self.user_id,
                )
                language_hint = configured_language or None
                if language_hint == "auto":
                    language_hint = self._get_cached_language_code()
                _stt_t0 = _time_mod.perf_counter()
                segments, info = self.model.transcribe(
                    transcribe_input,
                    beam_size=beam_size,
                    language=language_hint,
                    vad_filter=True,
                )
                logger.info(
                    "Detected language '%s' with probability %s",
                    info.language,
                    info.language_probability,
                )

                # Collect segments to extract both text and no_speech_prob
                segment_list = list(segments)
                transcript = "".join(seg.text for seg in segment_list).strip()

                # Track no_speech_prob across segments (use max for conservative filtering)
                if segment_list:
                    self._last_no_speech_prob = max(getattr(seg, "no_speech_prob", 0.0) for seg in segment_list)
                else:
                    self._last_no_speech_prob = 1.0  # No segments = no speech

                self._update_language_hint(
                    getattr(info, "language", None),
                    getattr(info, "language_probability", None),
                )
                if transcript:
                    # Telemetry: record STT latency
                    try:
                        from admin.instrumentation import record_stt_latency

                        record_stt_latency((_time_mod.perf_counter() - _stt_t0) * 1000)
                    except Exception:
                        logger.debug("STT latency telemetry unavailable")
                    return transcript
                elif attempt == 0:
                    logger.warning("Empty transcript, retrying with lower beam size...")
                    continue
            except Exception as e:
                if attempt < max_attempts - 1:
                    logger.warning(
                        "Transcription attempt %s failed: %s, retrying...",
                        attempt + 1,
                        e,
                    )
                    continue
                else:
                    logger.error(
                        "Error during transcription after %s attempts: %s",
                        max_attempts,
                        e,
                    )
                    return ""

        return ""

    def get_language_hint(self, min_confidence: float = 0.85) -> str | None:
        """
        Return the cached language hint if confidence/TTL criteria are met.
        """
        record = self._get_cached_language_record()
        if record is None:
            return None
        language, confidence, _ = record
        return language if confidence >= min_confidence else None

    def _get_cached_language_code(self) -> str | None:
        record = self._get_cached_language_record()
        if record is None:
            return None
        language, _confidence, _timestamp = record
        return language

    def _get_cached_language_record(self) -> tuple[str, float, float] | None:
        if self._language_hint is None:
            return None
        _language, confidence, timestamp = self._language_hint
        if confidence <= 0.0:
            return None
        if time.time() - timestamp > self._language_hint_ttl:
            return None
        return self._language_hint

    def _update_language_hint(self, language: str | None, confidence: float | None) -> None:
        if not language or confidence is None:
            return
        confidence = float(confidence)
        if confidence <= 0.0:
            return
        self._language_hint = (language, confidence, time.time())

    def is_available(self) -> bool:
        """Return True when the underlying Whisper model is ready."""
        return self.model is not None

    @property
    def last_no_speech_prob(self) -> float:
        """Return no_speech_prob from the most recent transcription."""
        return self._last_no_speech_prob
