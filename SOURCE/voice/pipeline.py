"""
Unified Voice Pipeline Orchestration

Coordinates wake word detection, transcription, and synthesis into
a single unified voice processing pipeline.
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from config import AppConfig
from core.exceptions import (
    AudioDeviceError,
    TranscriptionError,
    WakeDetectorUnavailableError,
)
from core.logging_config import get_logger
from services.liveness import Health, WorkSignal
from services.supervisor import HeartbeatSupervisor
from utils.audio_ducking import AudioDucker
from utils.timeout_manager import TimeoutManager
from voice.audio_manager import AudioDeviceManager

# Extracted modules
from voice.pipeline_initialization import VoicePipelineInitializer
from voice.pipeline_transcription import VoiceTranscriptionManager
from voice.pipeline_wake_callback import WakeCallbackManager
from voice.synthesizer import Synthesizer
from voice.transcriber import Transcriber
from voice.wake_detector import WakeDetector

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
except ImportError:  # pragma: no cover - UI not available in some environments
    emit_debug_event = None


_py_logger = get_logger("voice.pipeline")


class VoicePipeline:
    """
    Unified voice input/output pipeline.

    Coordinates:
    - Wake word detection
    - Speech-to-text transcription
    - Text-to-speech synthesis
    - Audio device management
    - Audio ducking coordination
    """

    handles_wake_ducking = True

    # Type annotations for dynamically set attributes (initialized by VoicePipelineInitializer)
    _supervisor: HeartbeatSupervisor | None = None
    _timeout_manager: TimeoutManager | None = None
    _stop_event: threading.Event
    _active_command_task: asyncio.Task[object] | None
    _cancellation_event: threading.Event
    _active_stt_task: asyncio.Task[object] | None
    _stt_cancellation_event: threading.Event
    _stt_cancelled: bool = False
    _audio_ducker: AudioDucker | None = None
    _wake_callback_immutable: Callable[[], None] | None = None
    audio_manager: AudioDeviceManager
    transcriber: Transcriber | None
    synthesizer: Synthesizer
    _stt_latency_samples: deque[float]
    _stt_jitter_samples: deque[float]
    _active_commands: int
    _max_backlog: int
    wake_detector: WakeDetector | None = None
    _wake_callback_manager: WakeCallbackManager
    _transcription_manager: VoiceTranscriptionManager
    _continuous_capture: object | None = None

    def __init__(
        self,
        config: AppConfig,
        on_wake_word_detected: Callable[[], None] | None = None,
        wake_detector: WakeDetector | None = None,
        transcriber: Transcriber | None = None,
        synthesizer: Synthesizer | None = None,
        audio_manager: AudioDeviceManager | None = None,
        audio_ducker: AudioDucker | None = None,
        aec_reference_source: object | None = None,
        *,
        supervisor: HeartbeatSupervisor | None = None,
        timeout_manager: TimeoutManager | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
    ):
        """
        Initialize voice pipeline.

        Args:
            config: Application configuration
            on_wake_word_detected: Callback when wake word is detected (if not using wake_detector)
            wake_detector: Pre-initialized wake detector (auto-created if None)
            transcriber: Pre-initialized transcriber (auto-created if None)
            synthesizer: Pre-initialized synthesizer (auto-created if None)
            audio_manager: Pre-initialized audio manager (auto-created if None)
            audio_ducker: Audio ducker instance (auto-retrieved from global if None)
            aec_reference_source: AEC reference source for echo cancellation
        """
        self.config = config
        object.__setattr__(self, "_wake_callback_violation", False)

        # CRITICAL: Validate callback at initialization - crash loud if invalid
        if on_wake_word_detected is not None and not callable(on_wake_word_detected):
            import traceback

            stack = "".join(traceback.format_stack())
            error_msg = (
                f"FATAL: on_wake_word_detected must be callable or None!\n"
                f"Got type: {type(on_wake_word_detected).__name__}\n"
                f"Got value: {on_wake_word_detected!r}\n"
                f"This is a BUG - the caller passed a bool instead of a callback.\n"
                f"Stack trace:\n{stack}"
            )
            logger.critical(error_msg)
            raise TypeError(error_msg)
        # Store callback - validation passed
        object.__setattr__(self, "_wake_callback_immutable", on_wake_word_detected)

        # Initialize attributes that will be set by the initializer
        # (These are declared at class level but need to be initialized for type checker)
        self._supervisor = supervisor
        self._timeout_manager = timeout_manager
        self._stop_event = threading.Event()
        self._active_command_task = None
        self._cancellation_event = threading.Event()
        self._active_stt_task = None
        self._stt_cancellation_event = threading.Event()
        self._stt_cancelled = False
        self._audio_ducker = audio_ducker
        self.audio_manager = audio_manager or AudioDeviceManager(config)
        self.transcriber = transcriber
        # Advanced only by real transcription (see _record_stt_heartbeat). Used
        # to report recent STT work; its silence is never treated as a fault,
        # because an idle transcriber is a healthy transcriber.
        self._stt_signal = WorkSignal("stt_transcriber", stall_after=180.0)
        self.synthesizer = synthesizer or Synthesizer(config)
        self.aec_reference_source = aec_reference_source
        self._stt_latency_samples = deque()
        self._stt_jitter_samples = deque()
        self._active_commands = 0
        self._max_backlog = 0
        self.wake_detector = wake_detector
        # Store event loop for thread-safe async operations (wake callback runs on detector thread)
        self._event_loop = event_loop

        # Initialize managers BEFORE component initialization
        # (VoicePipelineInitializer.initialize_components calls _build_wake_callback
        # which requires _wake_callback_manager to be available)
        self._wake_callback_manager = WakeCallbackManager(self)
        self._transcription_manager = VoiceTranscriptionManager(self)
        if event_loop is not None:
            self._wake_callback_manager.set_event_loop(event_loop)

        # Initialize components using extracted initializer
        initializer = VoicePipelineInitializer(self, config)
        initializer.initialize_components(
            wake_detector=wake_detector,
            transcriber=transcriber,
            synthesizer=self.synthesizer,
            audio_manager=self.audio_manager,
            audio_ducker=audio_ducker,
            supervisor=supervisor,
            timeout_manager=timeout_manager,
            aec_reference_source=aec_reference_source,
        )

        # State tracking
        self._wake_thread: threading.Thread | None = None
        self._last_wake_time = 0.0
        self._min_wake_interval = 2.0  # Minimum seconds between wake detections

        if self._supervisor:
            self._register_supervisor_sources()

    def __setattr__(self, name: str, value: object) -> None:
        """
        Validate _wake_callback_immutable assignments.

        Reject non-callable values other than None at assignment time, before
        a wake event can invoke an invalid callback. Log the assignment's call
        stack to identify its source.
        """
        if name == "_wake_callback_immutable":
            import traceback

            # Include the assignment's callers in the diagnostic log.
            stack_summary = traceback.extract_stack()[-5:-1]  # Last 4 frames before this
            caller_info = " <- ".join(
                f"{frame.filename.split('/')[-1]}:{frame.lineno}:{frame.name}" for frame in reversed(stack_summary)
            )
            logger.debug(
                "CALLBACK_ATTRIBUTION: %s = %s (type=%s) | from: %s",
                name,
                "<callable>" if callable(value) else repr(value),
                type(value).__name__,
                caller_info,
            )

            # Validate: must be None or callable
            if value is not None and not callable(value):
                stack = "".join(traceback.format_stack())
                error_msg = (
                    f"FATAL: _wake_callback_immutable must be callable or None!\n"
                    f"Got type: {type(value).__name__}\n"
                    f"Got value: {value!r}\n"
                    f"This is a BUG - find and fix the code that passed this value.\n"
                    f"Stack trace:\n{stack}"
                )
                logger.critical(error_msg)
                # CRASH LOUD - don't silently continue with broken state
                raise TypeError(error_msg)

        # All other attributes (or valid callback): allow assignment
        object.__setattr__(self, name, value)

    def _register_supervisor_sources(self) -> None:
        if not self._supervisor:
            return

        # Wake detection is CONTINUOUS work: its probe reads the signal the
        # detection loop marks from inside its own body, so a loop that exited
        # -- or that is parked forever in a blocking device read -- reports
        # stalled and gets restarted. It previously reported healthy either way,
        # because the beat came from a timer thread alongside the loop rather
        # than from the loop.
        self._supervisor.register_source(
            "wake_detector",
            description="Wake word listener thread",
            grace_period=10.0,
            restart=self._restart_wake_detector,
            probe=self._probe_wake_detector,
        )
        # Transcription is ON-DEMAND work: it runs only when someone speaks, so
        # silence is its healthy resting state and must never be read as death.
        # Under the old 180s grace period an idle install rebuilt the whisper
        # model every ~6 minutes forever -- the only heartbeat was emitted by
        # the restart path itself, so each rebuild produced the single beat that
        # scheduled the next one.
        self._supervisor.register_source(
            "stt_transcriber",
            description="Speech-to-text pipeline",
            grace_period=180.0,
            restart=self._restart_transcriber,
            probe=self._probe_transcriber,
        )

    def _probe_wake_detector(self) -> Health:
        """Health of the wake detection loop, measured at the loop itself."""
        detector = self.wake_detector
        if detector is None:
            return Health.UNAVAILABLE
        return detector.health()

    def _probe_transcriber(self) -> Health:
        """Readiness of the on-demand transcriber. Idle is healthy."""
        transcriber = self.transcriber
        if transcriber is None:
            return Health.UNAVAILABLE
        try:
            ready = bool(transcriber.is_available())
        except Exception:
            logger.exception("Transcriber readiness check raised; reporting unavailable")
            return Health.UNAVAILABLE
        if not ready:
            return Health.UNAVAILABLE
        # worked_recently(), not is_fresh(): a transcriber that has never run is
        # idle, not working. Either way this is healthy -- idleness is never a
        # fault for on-demand work -- so it only affects which healthy state is
        # reported.
        return Health.WORKING if self._stt_signal.worked_recently() else Health.IDLE_OK

    def _emit_wake_heartbeat(self) -> None:
        if self._supervisor:
            self._supervisor.record_heartbeat("wake_detector")

    def _record_stt_heartbeat(self) -> None:
        """Record that transcription actually ran.

        Called from the real transcription path (``transcribe_audio``), which is
        the only thing that can honestly claim STT work happened.
        """
        self._stt_signal.mark()
        if self._supervisor:
            self._supervisor.record_heartbeat("stt_transcriber")

    def set_wake_callback(self, callback: Callable[[], None] | None) -> None:
        """
        Safely set the wake word callback with validation.

        Args:
            callback: Callback function or None. Must be callable if not None.

        Raises:
            TypeError: If callback is not None and not callable.
        """
        if callback is not None and not callable(callback):
            error_msg = (
                f"Cannot set wake callback to non-callable value: "
                f"type={type(callback).__name__}, value={callback}. "
                f"This is likely a configuration error - "
                f"wake callbacks must be functions, not booleans or other values."
            )
            logger.error(error_msg)
            raise TypeError(error_msg)
        # Update immutable callback field - this is the only place it should be modified
        # Use object.__setattr__ to bypass our custom __setattr__ since we've already validated
        object.__setattr__(self, "_wake_callback_immutable", callback)
        # If wake detector is already created, rebuild it with new callback
        if hasattr(self, "wake_detector") and self.wake_detector:
            try:
                # Rebuild wake detector with new callback
                old_stop_event = getattr(self.wake_detector, "_stop_event", None)
                was_running = old_stop_event is not None and not old_stop_event.is_set()
                self.wake_detector.stop()
                self.wake_detector = WakeDetector(
                    self.config,
                    self._build_wake_callback(),
                    heartbeat_callback=(self._emit_wake_heartbeat if self._supervisor else None),
                )
                if was_running and old_stop_event:
                    self.wake_detector.start(old_stop_event)
            except Exception as exc:
                logger.exception("Failed to update wake detector with new callback: %s", exc)

    def _build_wake_callback(self) -> Callable[[], None]:
        """
        Build wake word callback, validating that it's actually callable.

        CRITICAL: Check callable() FIRST before truthiness to avoid treating
        booleans or other truthy non-callables as valid callbacks.

        Uses immutable callback field to prevent mutation after closure creation.
        """
        # Read from immutable callback field - should never be mutated except via set_wake_callback()
        # Use object.__getattribute__ to bypass any potential __getattribute__ overrides
        user_callback = object.__getattribute__(self, "_wake_callback_immutable")

        # DEFENSIVE: Validate callback hasn't been mutated to a non-callable
        # This catches mutations that bypass __setattr__ (e.g., using object.__setattr__ directly)
        if user_callback is not None:
            if not callable(user_callback):
                # Callback was mutated to non-callable - log error and use default
                logger.error(
                    "Wake callback was mutated to non-callable (type: %s, value: %s). Using default callback. This indicates a bug - something bypassed __setattr__ protection. Use set_wake_callback() to safely update the callback.",
                    type(user_callback).__name__,
                    user_callback,
                )
                return self._default_wake_callback

            # For bound methods, also validate the underlying function is callable
            if hasattr(user_callback, "__func__"):
                underlying_func = getattr(user_callback, "__func__", None)
                if underlying_func is not None and not callable(underlying_func):
                    logger.error(
                        "Wake callback's underlying function is not callable (type: %s, value: %s). Using default callback.",
                        type(underlying_func).__name__,
                        underlying_func,
                    )
                    return self._default_wake_callback

        # Validate that callback is actually callable (not a boolean or other non-callable)
        if user_callback is not None and callable(user_callback):
            return self._wake_callback_manager.create_wake_callback_with_ducking(user_callback)
        return self._default_wake_callback

    def _restart_wake_detector(self) -> bool:
        if self._stop_event.is_set():
            logger.debug("Restart request ignored: VoicePipeline stop event set")
            return False

        try:
            if self.wake_detector is not None:
                self.wake_detector.stop()
        except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive cleanup
            logger.debug("Wake detector stop during restart failed: %s", exc)

        try:
            self.wake_detector = WakeDetector(
                self.config,
                self._build_wake_callback(),
                heartbeat_callback=(self._emit_wake_heartbeat if self._supervisor else None),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            error = WakeDetectorUnavailableError(str(exc))
            logger.error(
                "Failed to rebuild wake detector: %s (user message: %s)",
                exc,
                error.user_friendly_message(),
            )
            return False

        assert self.wake_detector is not None, "Wake detector should be initialized"

        # Update facade with new instance for health checks
        from voice.wake_detector.facade import WakeDetectorFacade

        WakeDetectorFacade.set_instance(self.wake_detector)

        started = self.wake_detector.start(self._stop_event)
        if started:
            # Deliberately does NOT emit a wake heartbeat. Starting a thread is
            # not evidence that detection works -- the new loop proves itself by
            # marking its own signal once real audio frames arrive, which is
            # what _probe_wake_detector reads.
            # Re-wire AEC reference to the new wake detector instance
            self._rewire_aec_after_restart()
            logger.info("Wake detector restarted successfully")
        return started

    def _rewire_aec_after_restart(self) -> None:
        """Re-wire AEC reference source to the newly created wake detector.

        After a supervisor restart, the old wake detector (and its AEC wiring)
        is destroyed. The pipeline's ``aec_reference_source`` survives because
        it lives on the orchestrator/pipeline, not the detector. We reconnect
        it here so the new detector receives echo-cancelled audio.
        """
        if self.aec_reference_source is None:
            logger.debug("[AEC_REWIRE] No AEC reference source stored — skipping")
            return

        if self.wake_detector is None:
            return

        try:
            # The facade wraps the raw listener; wire through the facade
            from voice.wake_detector.facade import WakeDetectorFacade
            from voice.wake_detector.wake_factory import wire_aec_reference

            facade = WakeDetectorFacade.get_instance()
            if facade is not None:
                success = facade.wire_aec_reference(self.aec_reference_source)
            else:
                # Fallback: wire directly to the raw listener if facade unavailable
                raw_listener = getattr(self.wake_detector, "_listener", None)
                success = wire_aec_reference(raw_listener, self.aec_reference_source)

            if success:
                logger.info("[AEC_REWIRE] AEC reference re-wired after supervisor restart")
            else:
                logger.warning("[AEC_REWIRE] AEC re-wiring returned False after restart")
        except Exception as e:
            logger.warning("[AEC_REWIRE] Failed to re-wire AEC after restart: %s", e)

    def _restart_transcriber(self) -> bool:
        try:
            self.transcriber = Transcriber(self.config)
        except (OSError, RuntimeError, ValueError) as exc:
            error = TranscriptionError(str(exc))
            logger.error(
                "Failed to rebuild STT transcriber: %s (user message: %s)",
                exc,
                error.user_friendly_message(),
            )
            return False
        # Deliberately does NOT record an STT heartbeat. Rebuilding a component
        # is not evidence that it works -- and when the restart path was the
        # only beat, each rebuild emitted the single heartbeat that scheduled
        # the next rebuild, which is what turned an idle install into an endless
        # whisper-model reload every ~6 minutes. Health is answered by
        # _probe_transcriber; real work is recorded by transcribe_audio.
        logger.info("STT transcriber rebuilt successfully")
        return True

    def _default_wake_callback(self) -> None:
        """Default wake word callback - ducks audio and logs"""
        logger.debug("Wake word detected (no callback configured)")
        # Duck audio when wake word detected
        if self._audio_ducker:
            try:
                self._audio_ducker.duck()
                logger.debug("Audio ducked on wake word detection")
            except (OSError, RuntimeError) as exc:
                # Audio ducking failure is recoverable - log at warning level
                error = AudioDeviceError("output", str(exc))
                logger.warning(
                    "Failed to duck audio on wake word: %s (user message: %s)",
                    exc,
                    error.user_friendly_message(),
                )

    def _cancel_active_command(self) -> None:
        """
        Cancel any active voice command processing.

        This implements the cancellation protocol - when wake word fires,
        we cancel any in-flight voice command to ensure clean state.
        """
        if self._active_command_task and not self._active_command_task.done():
            logger.debug("Cancelling ongoing voice command due to wake word")
            self._active_command_task.cancel()
            self._cancellation_event.set()
            try:
                # Give cancellation a moment to propagate
                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Schedule cleanup
                    _task = asyncio.create_task(self._cleanup_cancelled_command())
                    _task.add_done_callback(_log_task_exception)
            except RuntimeError as exc:
                # Event loop issues during cancellation - log and continue
                logger.debug("Error during command cancellation (event loop): %s", exc)
        else:
            # Reset cancellation event if no active command
            self._cancellation_event.clear()

    def cancel_inflight_stt(self) -> None:
        """
        Cancel any in-flight STT transcription work.

        Per PRD cancellation protocol: STOP-class intents must cancel
        in-flight STT work for that session. This method is safe to call
        even if no STT work is active (no-op).

        This cancels:
        - Active STT transcription tasks
        - All buffered audio for the current utterance
        - Prevents partial STT results from being used

        Note: The cancellation event persists until the next transcription
        session starts, ensuring no partial results leak through.
        """
        # Set cancellation flag
        self._stt_cancellation_event.set()
        self._stt_cancelled = True

        # Cancel active STT task if it exists
        if self._active_stt_task is not None:
            if not self._active_stt_task.done():
                logger.debug("Cancelling in-flight STT work per cancellation protocol")
                self._active_stt_task.cancel()
            else:
                logger.debug("STT task already done, no need to cancel")
            self._active_stt_task = None
        else:
            logger.debug("No active STT task to cancel")

    async def _cleanup_cancelled_command(self) -> None:
        """Cleanup after cancelling a voice command."""
        try:
            await asyncio.sleep(0.1)  # Brief wait for cancellation to propagate
            if self._active_command_task:
                try:
                    await self._active_command_task
                except asyncio.CancelledError:  # Silent OK: expected when cancelling async task
                    pass
            self._active_command_task = None
            self._cancellation_event.clear()
        except (RuntimeError, asyncio.InvalidStateError) as exc:
            # Async cleanup issues - log and continue
            logger.debug("Error during cancelled command cleanup: %s", exc)

    def start(self, wake_enabled: bool = True) -> bool:
        """
        Start voice pipeline.

        Args:
            wake_enabled: Whether to enable wake word detection

        Returns:
            True if started successfully, False otherwise
        """
        if wake_enabled:
            assert self.wake_detector is not None, "Wake detector should be initialized when wake_enabled is True"
            if not self.wake_detector.is_available():
                logger.warning("Wake word detection requested but not available")
                return False

            # Start wake word detection
            if self.wake_detector.start(self._stop_event):
                self._try_start_continuous_capture()
                logger.info("✅ Voice pipeline started (wake word enabled)")
                return True
            else:
                logger.warning("Failed to start wake word detection")
                return False
        else:
            logger.info("✅ Voice pipeline started (wake word disabled)")
            return True

    def stop(self) -> None:
        """Stop voice pipeline and cleanup resources"""
        self._stop_event.set()
        if self._continuous_capture is not None:
            try:
                self._continuous_capture.stop()
            except Exception as exc:
                logger.debug("Continuous capture stop error: %s", exc)
            self._continuous_capture = None
        if self.wake_detector:
            self.wake_detector.stop()
        logger.info("Voice pipeline stopped")

    def _try_start_continuous_capture(self) -> None:
        """Start the continuous mic capture for interruptible TTS and zero-gap turns.

        The capture is only started when conversational mode is enabled.  It runs
        alongside the existing wake detector (paused mode initially) and switches
        into active modes during conversational sessions.
        """
        try:
            from config.settings import settings

            if not settings.conversational_mode_enabled:
                logger.debug("Continuous capture skipped: conversational mode disabled")
                return

            from voice.continuous_capture import ContinuousMicCapture

            # Get AEC reference source if wired
            aec_ref = None
            if self.aec_reference_source is not None:
                aec_ref_fn = getattr(self.aec_reference_source, "get_aec_reference_frame", None)
                if callable(aec_ref_fn):
                    aec_ref = aec_ref_fn

            # Get AEC processor from the raw wake listener if available
            aec_proc = None
            raw_listener = getattr(self.wake_detector, "_raw_listener", None)
            if raw_listener is not None:
                aec_proc = getattr(raw_listener, "_aec_processor", None)

            # Get device index from the raw wake listener
            device_index = None
            if raw_listener is not None:
                device_index = getattr(raw_listener, "_device_index", None)

            capture = ContinuousMicCapture(
                aec_processor=aec_proc,
                aec_reference_source=aec_ref,
                device_index=device_index,
            )
            if capture.start():
                # Start in paused mode — activated during conversational sessions
                capture.set_mode("paused")
                self._continuous_capture = capture

                # Wire frame tap: wake detector feeds audio to continuous capture
                # so we don't open a second PyAudio stream (segfaults on Windows).
                if raw_listener is not None and hasattr(raw_listener, "set_frame_tap"):
                    raw_listener.set_frame_tap(capture.feed_audio)
                    logger.info("Continuous mic capture started (fed via wake detector frame tap)")
                else:
                    logger.info("Continuous mic capture started (paused, ready for conversations)")
            else:
                logger.warning("Continuous mic capture failed to start")
        except Exception as exc:
            logger.warning("Continuous capture initialization failed (non-critical): %s", exc)

    def transcribe_audio(
        self,
        audio_source: str | Path | np.ndarray,
        preprocess: bool = False,
        duck_audio: bool = True,
    ) -> str:
        """
        Transcribe audio to text from file path or numpy buffer.

        This method is designed to be called from asyncio.to_thread() context.
        It uses the synchronous transcriber directly to avoid event loop issues.

        Args:
            audio_source: Path to audio file, or int16 numpy array (mono, 16kHz).
                         Buffer mode skips disk I/O for lower latency.
            preprocess: Whether to apply audio preprocessing (file-based only)
            duck_audio: Whether to duck audio during transcription (default: True)

        Returns:
            Transcribed text, or empty string if failed
        """
        import time as time_module

        import numpy as np

        start_time = time_module.perf_counter()
        is_buffer = isinstance(audio_source, np.ndarray)

        if is_buffer:
            logger.info(
                "[PIPELINE] transcribe_audio() called with buffer (%d samples)",
                len(audio_source),
            )
        else:
            logger.info("[PIPELINE] transcribe_audio() called for %s", audio_source)

        # Direct sync transcription - we're already in a thread from asyncio.to_thread()
        # Don't try to create async context here, just use the sync transcriber
        if self.transcriber is None:
            logger.error("[PIPELINE] Transcriber is None - cannot transcribe")
            self._record_stt_operation(False, None, start_time, error="transcriber_unavailable")
            return ""

        try:
            # Use the sync transcribe method directly
            # VoiceCommandHandler calls us via asyncio.to_thread(), so we're in a worker thread
            # Pass numpy array or file path — transcriber handles both
            if is_buffer:
                result = self.transcriber.transcribe(audio_source, preprocess=False)
            else:
                result = self.transcriber.transcribe(str(audio_source), preprocess=preprocess)
            if result:
                logger.info(
                    "[PIPELINE] Transcription result: '%s'",
                    result[:50] if len(result) > 50 else result,
                )
                # The one place entitled to claim STT work happened: a real
                # transcription just produced a real transcript.
                self._record_stt_heartbeat()
                self._record_stt_operation(True, result, start_time)
                return result
            else:
                logger.warning("[PIPELINE] Transcription returned empty result")
                self._record_stt_operation(False, result, start_time, error="empty_result")
                return ""
        except (OSError, RuntimeError, ValueError) as exc:
            # Specific transcription failures
            source_desc = "buffer" if is_buffer else str(audio_source)
            error = TranscriptionError(str(exc), source_desc)
            logger.error(
                "[PIPELINE] STT transcription failed: %s (user message: %s)",
                exc,
                error.user_friendly_message(),
            )
            self._record_stt_operation(False, None, start_time, error=str(exc))
            return ""

    def _record_stt_operation(
        self,
        success: bool,
        transcript: str | None,
        start_time: float,
        error: str | None = None,
    ) -> None:
        """Record STT operation for AI debugging diagnostics."""
        import time as time_module

        try:
            from diagnostics.operation_trace import OperationType, record_operation

            duration_ms = (time_module.perf_counter() - start_time) * 1000
            record_operation(
                OperationType.VOICE,
                "stt_complete",
                success=success,
                details={
                    "transcript_length": len(transcript) if transcript else 0,
                    "provider": "whisper",
                    "has_result": bool(transcript),
                },
                duration_ms=round(duration_ms, 2),
                error=error,
            )
        except ImportError:
            pass  # Operation tracing not available
        except Exception as e:
            # Don't let tracing break transcription
            logger.debug("Failed to record STT operation trace: %s", e)

    async def speak_text(self, text: str, duck_audio: bool = True) -> None:
        """
        Speak text using TTS.

        Args:
            text: Text to speak
            duck_audio: Whether to duck audio during TTS (default: True)
        """
        if not self.synthesizer.is_available():
            logger.warning("Synthesizer not available")
            return

        # Duck audio during TTS if enabled (TTS engine may handle its own ducking)
        # But we provide the option here for consistency
        if duck_audio and self._audio_ducker:
            try:
                from utils.audio_ducking import duck_context

                with duck_context():
                    await self.synthesizer.speak(text)
            except (OSError, RuntimeError) as exc:
                # Audio ducking failed - continue without ducking
                error = AudioDeviceError("output", str(exc))
                logger.warning(
                    "Audio ducking failed during TTS: %s (user message: %s)",
                    exc,
                    error.user_friendly_message(),
                )
                await self.synthesizer.speak(text)
        else:
            await self.synthesizer.speak(text)

    def listen_and_record(
        self,
        silence_threshold: int = 500,
        silence_duration: float = 1.0,
        timeout: float = 7.0,
        onset_timeout: float | None = None,
    ) -> Path | None:
        """
        Listen for and record voice command.

        Args:
            silence_threshold: Audio level threshold for silence detection
            silence_duration: Duration of silence to stop recording
            timeout: Maximum time to wait for command
            onset_timeout: If set, wait this long for speech to start before
                          applying silence endpoint. None = immediate recording.

        Returns:
            Path to recorded audio file, or None if failed
        """
        logger.debug("[PIPELINE] listen_and_record() called")
        logger.debug(
            "[PIPELINE] params: silence_threshold=%d, silence_duration=%.1f, timeout=%.1f, onset_timeout=%s",
            silence_threshold,
            silence_duration,
            timeout,
            onset_timeout,
        )

        if self.wake_detector is None:
            logger.warning("[PIPELINE] Wake detector is None - cannot record command")
            return None

        if not self.wake_detector.is_available():
            logger.warning("[PIPELINE] Wake detector not available - cannot record command")
            return None

        logger.debug("[PIPELINE] Calling wake_detector.listen_and_record_command()...")
        result = self.wake_detector.listen_and_record_command(
            silence_threshold,
            silence_duration,
            timeout,
            onset_timeout=onset_timeout,
        )
        logger.debug("[PIPELINE] listen_and_record_command() returned: %s", result)
        return result

    def listen_and_record_buffer(
        self,
        silence_threshold: int = 500,
        silence_duration: float = 1.0,
        timeout: float = 7.0,
        onset_timeout: float | None = None,
    ) -> tuple[np.ndarray | None, int]:
        """
        Listen for and record voice command, returning numpy buffer (no disk I/O).

        Eliminates WAV file write/read overhead for lower latency transcription.
        Audio never touches disk, which is strictly better for privacy.

        Args:
            silence_threshold: Audio level threshold for silence detection
            silence_duration: Duration of silence to stop recording
            timeout: Maximum time to wait for command
            onset_timeout: If set, wait this long for speech to start before
                          applying silence endpoint. None = immediate recording.

        Returns:
            (int16_numpy_array, sample_rate) or (None, 0) if no speech detected.
        """
        import numpy as np

        logger.debug("[PIPELINE] listen_and_record_buffer() called")

        if self.wake_detector is None:
            logger.warning("[PIPELINE] Wake detector is None - cannot record (buffer)")
            return None, 0

        if not self.wake_detector.is_available():
            logger.warning("[PIPELINE] Wake detector not available - cannot record (buffer)")
            return None, 0

        # Check if the wake detector supports buffer recording
        recorder = getattr(self.wake_detector, "_listener", None)
        if recorder is None:
            recorder = self.wake_detector

        buffer_fn = getattr(recorder, "listen_and_record_command_buffer", None)
        if buffer_fn is None:
            # Fallback: use file-based recording and read the result
            logger.debug("[PIPELINE] Buffer recording not available, falling back to file-based")
            path = self.listen_and_record(silence_threshold, silence_duration, timeout, onset_timeout)
            if path is None:
                return None, 0
            try:
                import wave

                with wave.open(str(path), "rb") as wf:
                    sample_rate = wf.getframerate()
                    raw = wf.readframes(wf.getnframes())
                    audio = np.frombuffer(raw, dtype=np.int16)
                return audio, sample_rate
            except Exception as exc:
                logger.warning("[PIPELINE] Failed to read WAV for buffer fallback: %s", exc)
                return None, 0

        logger.debug("[PIPELINE] Calling listen_and_record_command_buffer()...")
        audio, sample_rate = buffer_fn(
            silence_threshold,
            silence_duration,
            timeout,
            onset_timeout=onset_timeout,
        )
        if audio is not None:
            logger.debug("[PIPELINE] Buffer recording complete: %d samples", len(audio))
        else:
            logger.debug("[PIPELINE] Buffer recording returned None")
        return audio, sample_rate

    async def handle_voice_command(
        self,
        text: str,
    ) -> bool:
        """
        Handle a complete voice command: transcribe -> process -> speak.

        This is a convenience method that orchestrates the full voice
        command pipeline. For more control, use the individual methods.

        Supports cancellation protocol - if wake word fires during processing,
        the command will be cancelled.

        Args:
            audio_path: Path to recorded audio file
            on_complete: Optional callback with (transcript, result) when complete
            intent_interpreter: Optional intent interpreter for command processing
        """
        return await self._transcription_manager.handle_voice_command(text)

    def is_available(self) -> bool:
        """Check if voice pipeline is available (at least one component ready)"""
        transcriber_ok = self.transcriber.is_available() if self.transcriber else False
        synthesizer_ok = self.synthesizer.is_available() if self.synthesizer else False
        return transcriber_ok or synthesizer_ok

    def get_status(self) -> dict[str, object]:
        """Get status of all pipeline components"""
        return {
            "wake_detector": (self.wake_detector.is_available() if self.wake_detector else False),
            "transcriber": (self.transcriber.is_available() if self.transcriber else False),
            "synthesizer": (self.synthesizer.is_available() if self.synthesizer else False),
            "audio_manager": True,  # Always available
        }

    def get_stt_metrics(self) -> dict[str, object]:
        """Expose aggregated STT metrics for diagnostics."""
        avg_latency = (
            sum(self._stt_latency_samples) / len(self._stt_latency_samples) if self._stt_latency_samples else 0.0
        )
        avg_jitter = sum(self._stt_jitter_samples) / len(self._stt_jitter_samples) if self._stt_jitter_samples else 0.0
        return {
            "avg_latency": avg_latency,
            "p95_latency": self._percentile(self._stt_latency_samples, 95.0),
            "avg_jitter": avg_jitter,
            "samples": len(self._stt_latency_samples),
            "histogram": self._build_latency_histogram(),
        }

    def get_queue_metrics(self) -> dict[str, int]:
        """Expose command backlog metrics."""
        return {
            "current_backlog": self._active_commands,
            "max_backlog": self._max_backlog,
        }

    def _percentile(self, samples: deque[float], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        k = (len(ordered) - 1) * (percentile / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return ordered[int(k)]
        return ordered[f] * (c - k) + ordered[c] * (k - f)

    def _build_latency_histogram(self) -> dict[str, int]:
        bins = [
            ("<=250ms", 0.25),
            ("<=500ms", 0.5),
            ("<=1000ms", 1.0),
            (">1000ms", float("inf")),
        ]
        histogram = {label: 0 for label, _ in bins}
        for seconds in self._stt_latency_samples:
            for label, upper_bound in bins:
                if seconds <= upper_bound or math.isinf(upper_bound):
                    histogram[label] += 1
                    break
        return histogram
