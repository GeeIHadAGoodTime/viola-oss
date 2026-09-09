"""
Voice pipeline initialization utilities.

Extracted from voice/pipeline.py to reduce method complexity.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from config import AppConfig
from core.logging_config import get_logger
from services.supervisor import HeartbeatSupervisor
from utils.timeout_manager import TimeoutManager, get_timeout_manager
from voice.audio_manager import AudioDeviceManager
from voice.synthesizer import Synthesizer
from voice.transcriber import Transcriber
from voice.wake_detector import WakeDetector
from voice.wake_detector.facade import WakeDetectorFacade

logger = get_logger(__name__)


def _coerce_float(value: Any, default: float) -> float:
    """Convert to float, falling back to default for mocks or invalid values."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class VoicePipelineInitializer:
    """
    Handles initialization of VoicePipeline components.

    Extracted from VoicePipeline.__init__ to reduce method complexity.
    """

    def __init__(self, pipeline_instance: Any, config: AppConfig):
        self.pipeline = pipeline_instance
        self.config = config

    def initialize_components(
        self,
        wake_detector: WakeDetector | None,
        transcriber: Transcriber | None,
        synthesizer: Synthesizer | None,
        audio_manager: AudioDeviceManager | None,
        audio_ducker: Any | None,
        supervisor: HeartbeatSupervisor | None,
        timeout_manager: TimeoutManager | None,
        aec_reference_source: Any = None,
    ) -> None:
        """
        Initialize all voice pipeline components.

        This method handles the complex initialization logic that was previously
        in VoicePipeline.__init__.
        """
        # Store AEC source in pipeline
        self.pipeline.aec_reference_source = aec_reference_source

        # Basic setup
        self.pipeline._stop_event = threading.Event()
        self.pipeline._supervisor = supervisor
        self.pipeline._timeout_manager = timeout_manager or get_timeout_manager()

        # Component initialization
        self.pipeline.audio_manager = audio_manager or AudioDeviceManager(self.config)

        # Audio ducking setup
        self._setup_audio_ducking(audio_ducker)

        # Wake detector setup
        self._setup_wake_detector(wake_detector, supervisor)

        # Transcriber and synthesizer
        self.pipeline.transcriber = transcriber or Transcriber(self.config)
        self.pipeline.synthesizer = synthesizer or Synthesizer(self.config)

        # Prewarm STT backend
        self._prewarm_transcriber()

        # Prewarm TTS backend (background — the Kokoro model loads lazily)
        self._prewarm_synthesizer()

        # Performance monitoring setup
        self._setup_performance_monitoring()

        # Cancellation and task tracking setup
        self._setup_cancellation_support()

    def _setup_audio_ducking(self, audio_ducker: Any | None) -> None:
        """Setup audio ducking support."""
        if audio_ducker is None:
            try:
                from utils.audio_ducking import get_global_ducker

                self.pipeline._audio_ducker = get_global_ducker()
            except Exception as e:
                logger.debug("Audio ducker initialization failed (non-critical): %s", e)
                self.pipeline._audio_ducker = None
        else:
            self.pipeline._audio_ducker = audio_ducker

    def _setup_wake_detector(self, wake_detector: WakeDetector | None, supervisor: HeartbeatSupervisor | None) -> None:
        """Setup wake detector with proper callback wrapping."""
        wrapped_callback = self.pipeline._build_wake_callback()

        self.pipeline.wake_detector = wake_detector or WakeDetector(
            self.config,
            wrapped_callback,
            heartbeat_callback=(self.pipeline._emit_wake_heartbeat if supervisor else None),
        )

        # Register with global facade for health checks and diagnostics
        WakeDetectorFacade.set_instance(self.pipeline.wake_detector)
        logger.info("Wake detector registered with global facade")

        # Wire AEC reference source if available
        if hasattr(self.pipeline, "aec_reference_source") and self.pipeline.aec_reference_source:
            try:
                success = self.pipeline.wake_detector.wire_aec_reference(self.pipeline.aec_reference_source)
                if success:
                    logger.info("✅ AEC reference source wired to wake detector")
                else:
                    logger.warning("AEC reference source wiring failed")
            except Exception as e:
                logger.warning("Failed to wire AEC reference source: %s", e)

    def _prewarm_transcriber(self) -> None:
        """Prewarm STT backend to reduce first-call latency."""
        try:
            self.pipeline.transcriber.prewarm()
            logger.debug("VoicePipeline transcription backend prewarmed.")
        except Exception as exc:
            logger.debug("VoicePipeline prewarm skipped: %s", exc)

    def _prewarm_synthesizer(self) -> None:
        """Prewarm the TTS backend so the first spoken reply doesn't pay the
        ~310 MB Kokoro model load inline (critical_path.md WL-2).

        Mirrors _prewarm_transcriber, but runs the warm-up on a daemon thread:
        unlike STT (whose model loads at Transcriber construction), the Kokoro
        model loads lazily on first speak, so a synchronous prewarm here would
        move that heavy load onto the startup path. The daemon thread keeps
        startup non-blocking while still warming TTS before the first turn.
        """
        synthesizer = getattr(self.pipeline, "synthesizer", None)
        if synthesizer is None or not hasattr(synthesizer, "prewarm"):
            return

        def _warm() -> None:
            try:
                synthesizer.prewarm()
                logger.debug("VoicePipeline TTS backend prewarmed.")
            except (RuntimeError, OSError, ImportError, ValueError, AttributeError) as exc:
                logger.debug("VoicePipeline TTS prewarm skipped: %s", exc)

        thread = threading.Thread(target=_warm, name="tts-prewarm", daemon=True)
        thread.start()

    def _setup_performance_monitoring(self) -> None:
        """Setup performance monitoring structures."""
        self.pipeline._stt_latency_samples = deque(maxlen=512)
        self.pipeline._stt_jitter_samples = deque(maxlen=512)
        self.pipeline._last_transcription_latency = None

        # Latency thresholds
        warn_value = getattr(self.config, "stt_latency_warn_seconds", 4.0)
        alert_value = getattr(self.config, "stt_latency_alert_seconds", 8.0)
        self.pipeline._stt_latency_warn = _coerce_float(warn_value, 4.0)
        self.pipeline._stt_latency_alert = _coerce_float(alert_value, 8.0)

        # Command tracking
        self.pipeline._active_commands = 0
        self.pipeline._max_backlog = 0

    def _setup_cancellation_support(self) -> None:
        """Setup cancellation support for wake word protocol."""

        # Command cancellation
        self.pipeline._active_command_task = None
        self.pipeline._cancellation_event = threading.Event()

        # STT cancellation
        self.pipeline._active_stt_task = None
        self.pipeline._stt_cancellation_event = threading.Event()
        self.pipeline._stt_cancelled = False
