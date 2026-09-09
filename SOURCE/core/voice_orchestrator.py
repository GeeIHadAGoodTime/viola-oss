"""
Voice orchestrator for coordinating voice input processing.

Coordinate application voice services.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Callable

from core.constants import TIMEOUT_DEFAULT
from core.logging_config import get_logger

if TYPE_CHECKING:
    from audio_core.aec_reference_base import RingBufferAECReferenceAdapter
    from backend.intent_bridge.bridge import IntentBridge
    from models.state_manager import ConsolidatedState
    from music.player.core import MusicPlayer
    from voice.synthesis.engine import TTSEngine

logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


try:  # pragma: no cover - optional in non-Qt contexts
    from ui.qt_native.debug_events import emit_debug_event
except Exception:
    emit_debug_event = None

# Import validation utilities
try:
    from core.validation import sanitize_error_message, validate_command_text
except ImportError:
    # Fallback if validation module not available
    def validate_command_text(text: str) -> tuple[bool, str]:
        return True, ""

    def sanitize_error_message(error: Exception) -> str:
        return str(error)


try:
    from config import settings
except ImportError:
    settings = None

from diagnostics.runtime_metrics import get_runtime_metrics

from .callback_validator import validate_wake_callback_assignment
from .voice_async_initializer import VoiceAsyncInitializer
from .voice_command_handler import VoiceCommandHandler
from .voice_orchestrator_metrics import VoiceOrchestratorMetricsManager
from .voice_orchestrator_wake import VoiceOrchestratorWakeManager
from .voice_pipeline_initializer import VoicePipelineInitializer
from .voice_startup_validator import VoiceStartupValidator
from .voice_supervisor_manager import VoiceSupervisorManager
from .voice_wake_processor import VoiceWakeProcessor


def maybe_await(coro: Coroutine[None, None, None]) -> asyncio.Task[None] | None:
    """Helper to await coroutines in both sync and async contexts."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context, create task
            task = asyncio.create_task(coro)
            task.add_done_callback(_log_task_exception)
            return task
        else:
            # Not running, run until complete
            asyncio.run(coro)
            return None
    except RuntimeError:
        # No event loop, create new one
        asyncio.run(coro)
        return None


def _build_aec_adapter() -> tuple[RingBufferAECReferenceAdapter | None, str]:
    """Construct the AEC reference adapter this platform can actually use.

    Device-level capture is the point: AEC must cancel whatever the microphone
    hears from the speakers, no matter which process (YouTube iframe, local
    file, Spotify) produced it. Each platform reaches that differently, so this
    is a real dispatch rather than one hardcoded implementation:

    * Windows -> WASAPI loopback.
    * macOS   -> Core Audio process tap, via the system-audio capture provider.
    * Linux   -> PulseAudio monitor source, same route as macOS.

    Windows was the only branch that ever existed, which is why macOS ran wake
    detection with AEC in passthrough (#333).

    ChunkStamperAECAdapter is deliberately NOT used here — it only captures one
    process's audio and is reserved for multi-room sync stamping.

    Returns:
        ``(adapter, description)``; ``adapter`` is None when this platform has
        no usable reference source.
    """
    if sys.platform == "win32":
        from audio_core.wasapi.aec_reference_adapter import (
            WasapiAECReferenceAdapter,
        )

        return WasapiAECReferenceAdapter(), "WASAPI loopback"

    from audio_core.capture.system_audio_aec_reference import (
        SystemAudioAECReferenceAdapter,
        select_system_audio_capture_provider,
    )

    provider = select_system_audio_capture_provider()
    if provider is None:
        # The selector has already logged why, with the platform-specific
        # remedy (e.g. the macOS audio-capture permission).
        return None, "none available"

    return (
        SystemAudioAECReferenceAdapter(provider),
        "system-audio capture via %s" % type(provider).__name__,
    )


def create_and_start_aec_adapter() -> RingBufferAECReferenceAdapter | None:
    """Create and start an AEC reference adapter (standalone, no VoiceOrchestrator needed).

    Returns:
        A started AEC adapter implementing the AECReferenceSource protocol,
        or None if no adapter could be created/started.
    """
    try:
        adapter, description = _build_aec_adapter()
        if adapter is None:
            logger.warning("AEC_NOT_WIRED: no AEC reference source on this platform")
            return None
        logger.info("[AEC] Using %s reference (device-level capture)", description)
    except ImportError as e:
        logger.debug("AEC reference adapter not available: %s", e)
        return None
    except Exception as e:
        logger.warning("Failed to create AEC adapter: %s", e)
        return None

    # Start the adapter
    try:
        success = adapter.start()
        if success:
            logger.info("AEC_WIRED: %s AEC adapter started successfully", description)
            return adapter
        logger.warning("AEC_NOT_WIRED: %s AEC adapter start returned False", description)
    except Exception as e:
        logger.warning("AEC_NOT_WIRED: Failed to start %s AEC adapter: %s", description, e)

    return None


class VoiceOrchestrator:
    """Orchestrates voice input processing (wake word + STT + command execution)"""

    # Immutable callback attribute - set via object.__setattr__ for immutability
    _wake_callback_immutable: Callable[[], None] | None

    def __init__(
        self,
        state: ConsolidatedState,
        intent: IntentBridge,
        music: MusicPlayer | None,
        tts: TTSEngine | None,
        wake_enabled: bool,
        event_loop: asyncio.AbstractEventLoop | None = None,
        aec_reference_source: RingBufferAECReferenceAdapter | None = None,
    ) -> None:
        self.state = state
        self.intent = intent
        self.music = music
        self.tts = tts

        # Store AEC reference source for wake word echo cancellation
        # This is wired to the wake detector in pipeline initialization
        self.aec_reference_source = aec_reference_source
        self._owns_aec_adapter = False  # Track if we created the adapter ourselves

        # Read wake config from settings (bootstrap wrote it there)
        # The wake_enabled parameter is ignored - we use settings as source of truth
        self.wake_enabled = getattr(settings, "wake_enabled", False) if settings else wake_enabled
        self._resolved_wake_engine = getattr(settings, "wake_engine", "none") if settings else "none"

        logger.info(
            "VoiceOrchestrator using settings: wake_enabled=%s, wake_engine=%s, aec_source=%s",
            self.wake_enabled,
            self._resolved_wake_engine,
            type(aec_reference_source).__name__ if aec_reference_source else "None",
        )

        self.capabilities = getattr(state, "runtime_capabilities", {}) or {}
        self._stop_event = threading.Event()
        object.__setattr__(self, "_wake_callback_violation", False)
        if settings is None:
            raise RuntimeError("App settings unavailable; cannot initialize voice pipeline.")

        # Initialize supervisor manager
        self._supervisor_manager = VoiceSupervisorManager(state, intent)
        self._supervisor = self._supervisor_manager.initialize_supervisor()
        self._supervisor_manager.attach_supervisor_to_intent()

        # Initialize helper managers first
        self._wake_manager = VoiceOrchestratorWakeManager(self)
        self._metrics_manager = VoiceOrchestratorMetricsManager(self)

        # Initialize metrics
        self._metrics = get_runtime_metrics()

        # Initialize async components
        self._async_initializer = VoiceAsyncInitializer(event_loop)
        self._async_bridge = self._async_initializer.initialize_async_bridge()

        # Initialize pipeline components
        self._pipeline_initializer = VoicePipelineInitializer(self, settings)
        self._pipeline_initializer.setup_wake_callback()
        self.voice_pipeline = self._pipeline_initializer.initialize_pipeline()
        self._pipeline_handles_ducking = self._pipeline_initializer.get_pipeline_handles_ducking()
        self._command_handler = VoiceCommandHandler(
            orchestrator_state=self.state,
            intent=self.intent,
            music=self.music,
            tts=self.tts,
            voice_pipeline=self.voice_pipeline,
            metrics=self._metrics,
            async_bridge=self._async_bridge,
            stop_event=self._stop_event,
            capabilities=self.capabilities,
        )
        self._startup_validator = VoiceStartupValidator(
            capabilities=self.capabilities,
            metrics=self._metrics,
            voice_pipeline=self.voice_pipeline,
        )
        self._wake_processor = VoiceWakeProcessor(self)

        self._idle_monitor_thread: threading.Thread | None = None
        self._wake_metrics_logger_thread: threading.Thread | None = None
        self._callback_watchdog_thread: threading.Thread | None = None
        self._callback_watchdog_interval = 1.0  # Check every second

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Validate _wake_callback_immutable assignments.

        Uses shared validation from core.callback_validator to ensure
        wake callbacks are callable (or None), raising TypeError otherwise.
        """
        # Validate wake callback (logs attribution and raises TypeError if invalid)
        validate_wake_callback_assignment(name, value, logger)
        # Proceed with assignment (validation passed or not a wake callback)
        object.__setattr__(self, name, value)

    def _start_idle_monitor(self) -> None:
        """Start the idle activity monitor."""
        self._metrics_manager.start_idle_monitor()

    def _start_wake_metrics_logger(self) -> None:
        """Start the wake metrics logging."""
        self._metrics_manager.start_wake_metrics_logger()

    def _start_callback_watchdog(self) -> None:
        """
        Start a watchdog thread that periodically verifies callback integrity.
        """
        if self._callback_watchdog_thread and self._callback_watchdog_thread.is_alive():
            return

        self._callback_watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="callback-watchdog",
        )
        self._callback_watchdog_thread.start()
        logger.info("Callback watchdog started")

    def _watchdog_loop(self) -> None:
        """Main watchdog loop that checks callback integrity."""
        while not self._stop_event.is_set():
            self._perform_watchdog_check()
            self._watchdog_interruptible_sleep()
        logger.debug("Callback watchdog stopped")

    def _perform_watchdog_check(self) -> None:
        """Perform a single watchdog integrity check."""
        try:
            self._check_pipeline_callback()
            self._check_orchestrator_callback()
        except Exception as e:
            logger.debug("Watchdog check error: %s", e)

    def _check_pipeline_callback(self) -> None:
        """Check pipeline wake callback for corruption."""
        pipeline = getattr(self, "voice_pipeline", None)
        if pipeline is None:
            return

        callback = object.__getattribute__(pipeline, "_wake_callback_immutable")
        if callback is not None and not callable(callback):
            self._handle_corrupted_pipeline_callback(pipeline, callback)

    def _handle_corrupted_pipeline_callback(self, pipeline: Any, callback: Any) -> None:
        """Handle corrupted pipeline callback."""
        import sys
        import traceback

        logger.critical(
            "WATCHDOG_ALERT: Pipeline wake callback corrupted! "
            "type=%s, value=%r. "
            "This indicates a serious bug - callback was modified "
            "after validation.",
            type(callback).__name__,
            callback,
        )

        # Log all thread stacks for debugging
        for thread_id, frame in sys._current_frames().items():
            logger.critical(
                "Thread %s stack:\n%s",
                thread_id,
                "".join(traceback.format_stack(frame)),
            )

        self._recover_corrupted_callback(pipeline)

    def _recover_corrupted_callback(self, pipeline: Any) -> None:
        """Try to recover by setting a safe no-op callback."""
        try:
            object.__setattr__(pipeline, "_wake_callback_immutable", lambda: None)
            logger.warning("WATCHDOG_RECOVERY: Set no-op callback")
        except Exception as e:
            logger.error("WATCHDOG_RECOVERY_FAILED: %s", e)

    def _check_orchestrator_callback(self) -> None:
        """Check orchestrator wake callback for corruption."""
        orchestrator_callback = getattr(self, "_wake_callback_immutable", None)
        if orchestrator_callback is not None and not callable(orchestrator_callback):
            logger.critical(
                "WATCHDOG_ALERT: Orchestrator wake callback corrupted! type=%s, value=%r",
                type(orchestrator_callback).__name__,
                orchestrator_callback,
            )

    def _watchdog_interruptible_sleep(self) -> None:
        """Sleep in small increments to allow clean shutdown."""
        for _ in range(int(self._callback_watchdog_interval * 10)):
            if self._stop_event.is_set():
                break
            self._stop_event.wait(0.1)

    def _start_aec_reference_adapter(self) -> None:
        """Start the WASAPI loopback AEC reference adapter if available.

        Uses WASAPI loopback to capture device-level audio output,
        ensuring AEC works regardless of which process produces audio.

        Delegates to the standalone create_and_start_aec_adapter() so the
        same logic is available outside VoiceOrchestrator (e.g. when
        voice=False but wake detection is still needed).
        """
        if self.aec_reference_source is None and self.wake_enabled:
            adapter = create_and_start_aec_adapter()
            if adapter is not None:
                self.aec_reference_source = adapter
                self._owns_aec_adapter = True
                self._wire_aec_to_wake_detector()
            else:
                logger.warning("AEC_NOT_WIRED: No AEC adapter could be created/started")

    def _wire_aec_to_wake_detector(self) -> None:
        """Wire AEC reference to wake detector after adapter is started."""
        if self.aec_reference_source is None:
            return
        if not hasattr(self, "voice_pipeline") or self.voice_pipeline is None:
            return
        wake_detector = getattr(self.voice_pipeline, "wake_detector", None)
        if wake_detector is None:
            return

        try:
            success = wake_detector.wire_aec_reference(self.aec_reference_source)
            if success:
                logger.info("AEC reference wired to wake detector at startup")
            else:
                logger.debug("AEC wiring skipped (already wired or not supported)")
        except Exception as e:
            logger.warning("Failed to wire AEC reference to wake detector: %s", e)

    def _stop_aec_reference_adapter(self) -> None:
        """Stop the AEC reference adapter if we own it."""
        if self.aec_reference_source is not None and self._owns_aec_adapter:
            stop_fn = getattr(self.aec_reference_source, "stop", None)
            if callable(stop_fn):
                try:
                    stop_fn()
                    logger.debug("AEC reference adapter stopped")
                except Exception as e:
                    logger.debug("Error stopping AEC adapter: %s", e)

    def start(self) -> None:
        """Start voice orchestration"""
        # DEFENSIVE: Validate callback is still callable before starting
        # This catches any mutations that might have occurred between init and start
        if not callable(self._wake_callback_immutable):
            logger.error(
                "Wake callback became non-callable before start (type: %s, value: %r). "
                "This is a critical bug - callback must remain callable.",
                type(self._wake_callback_immutable).__name__,
                self._wake_callback_immutable,
            )
            raise RuntimeError("Wake word callback must remain callable")

        # ── Phase 1: Hard blocks (voice disabled by profile) ──
        if not self._startup_validator.check_voice_enabled():
            return

        self._stop_event.clear()

        # ── Phase 2: Wake detection infrastructure (independent of STT) ──
        if not self._startup_validator.check_wake_detector(self.wake_enabled):
            return

        if not self._startup_validator.check_wake_start(self.wake_enabled):
            return

        # Start AEC reference adapter for echo cancellation during music playback
        # MUST run regardless of STT availability — AEC protects wake detection
        self._start_aec_reference_adapter()

        # Verify AEC reference is actually wired
        if self.aec_reference_source is not None:
            is_active = getattr(self.aec_reference_source, "_running", None)
            if is_active:
                logger.info("[AEC_STARTUP] aec_source=wired, reference_active=True")
            else:
                logger.warning("[AEC_STARTUP] aec_source=created but not confirmed active")
        else:
            logger.warning("[AEC_STARTUP] aec_source=NO_REF — AEC running as passthrough!")

        # Wire playback state to wake decision policy (critical for false positive prevention)
        # MUST run regardless of STT availability — playback state protects wake detection
        wired = self._wake_manager.wire_playback_state()
        logger.info(
            "Playback state wiring result: %s (wake_enabled=%s, engine=%s)",
            "SUCCESS" if wired else "SKIPPED",
            self.wake_enabled,
            getattr(self, "_resolved_wake_engine", "unknown"),
        )

        # Start monitoring based on wake configuration
        if self.wake_enabled:
            self._start_idle_monitor()
            self._start_wake_metrics_logger()
            self._start_callback_watchdog()  # Monitor callback integrity
            if emit_debug_event is not None:
                emit_debug_event(
                    "wake_ready",
                    {"enabled": True, "status": "starting"},
                    source="backend",
                )
        else:
            self._start_idle_monitor()
            if emit_debug_event is not None:
                emit_debug_event(
                    "wake_ready",
                    {"enabled": False, "reason": "wake_disabled"},
                    source="backend",
                )

        # ── Phase 3: STT / voice command setup (non-fatal) ──
        # Wake detection + AEC are already running above. STT unavailability
        # only disables voice command processing, not wake word detection.
        if not self._startup_validator.check_stt_available():
            logger.warning(
                "STT unavailable — wake detection active, voice commands disabled. "
                "Install faster-whisper or configure an STT backend to enable voice commands."
            )

    def stop(self) -> None:
        """Stop voice orchestration"""
        try:
            # Signal the stop event first
            self._stop_event.set()
            self.voice_pipeline.stop()
        except Exception as e:
            # Cleanup failure during shutdown (non-critical)
            logger.debug("Error during voice orchestration cleanup: %s", e)

        # Stop AEC reference adapter
        self._stop_aec_reference_adapter()

        if self._idle_monitor_thread and self._idle_monitor_thread.is_alive():
            self._idle_monitor_thread.join(timeout=TIMEOUT_DEFAULT)
        self._idle_monitor_thread = None
        if self._wake_metrics_logger_thread and self._wake_metrics_logger_thread.is_alive():
            self._wake_metrics_logger_thread.join(timeout=TIMEOUT_DEFAULT)
        self._wake_metrics_logger_thread = None
        if self._callback_watchdog_thread and self._callback_watchdog_thread.is_alive():
            self._callback_watchdog_thread.join(timeout=TIMEOUT_DEFAULT)
        self._callback_watchdog_thread = None
        self._metrics.heartbeat("voice.pipeline", status="stopped")

    def _on_wake(self) -> None:
        """
        Handle wake word detection.

        This method coordinates the wake word response across all subsystems.
        """
        self._wake_manager.on_wake_detected()

        # Use wake processor for rate limiting and wake handling
        if not self._wake_processor.check_rate_limit():
            return

        if not self._wake_processor.check_already_listening():
            return

        self._wake_processor.handle_wake_accepted()
        self._wake_processor.handle_audio_ducking()
        self._wake_processor.schedule_voice_command()
