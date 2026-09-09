"""
Unified Wake Word Detection Abstraction

Provides a consistent interface for wake word detection using the
ViolaWake engine exclusively.

Uses WakeDecisionPolicy as central authority for threshold and gating decisions.
Includes circuit breaker for repeated detection failures.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from config import AppConfig
from core.constants import TIMEOUT_MEDIUM
from core.exceptions import WakeDetectionError
from core.logging_config import get_logger
from services.liveness import Health, WorkSignal
from utils.circuit_breaker import CircuitBreaker

logger = get_logger(__name__)


class WakeDecisionPolicyProtocol(Protocol):
    def set_playback_active(self, is_playing: bool, volume: int) -> None: ...

    def get_diagnostics(self) -> object: ...

    def set_listening_active(self, active: bool) -> None: ...


WakePolicyFactory = Callable[[AppConfig], WakeDecisionPolicyProtocol]

try:
    from voice.wake_detector.wake_decision_policy import (
        get_wake_policy as _get_wake_policy,
    )
except ImportError:  # pragma: no cover - optional dependency
    _get_wake_policy = None

_GET_WAKE_POLICY: WakePolicyFactory | None = (
    cast(WakePolicyFactory, _get_wake_policy) if _get_wake_policy is not None else None
)


class AdaptiveGainSchedulerProtocol(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


AdaptiveGainSchedulerFactory = Callable[..., AdaptiveGainSchedulerProtocol]

_AdaptiveGainSchedulerImpl: AdaptiveGainSchedulerFactory | None

try:
    from voice.wake_detector.adaptive_gain_scheduler import (
        AdaptiveGainScheduler as _AdaptiveGainSchedulerImpl,
    )
except ImportError:  # pragma: no cover - optional dependency
    _AdaptiveGainSchedulerImpl = None

_ADAPTIVE_GAIN_SCHEDULER: AdaptiveGainSchedulerFactory | None = _AdaptiveGainSchedulerImpl


class SupportsAdaptiveGain(Protocol):
    def supports_adaptive_gain(self) -> bool: ...


def create_wake_listener(config: AppConfig, on_detected: Callable[[], None]) -> WakeDetectorPort | None:
    """
    Thin wrapper exposing listener factory for tests/patching while keeping
    runtime import lazy to avoid heavy dependencies during module import.
    """
    from voice.wake_detector.wake_factory import (
        create_wake_listener as _create_wake_listener,
    )

    return cast(WakeDetectorPort | None, _create_wake_listener(config, on_detected))


class WakeDetectorPort(Protocol):
    """Protocol for wake word detection implementations"""

    def run(self, stop_event: threading.Event) -> None:
        """Run wake word detection loop in a thread"""
        ...

    def listen_and_record_command(
        self,
        silence_threshold: int = 500,
        silence_duration: float = 1.0,
        timeout: float = 7.0,
        onset_timeout: float | None = None,
    ) -> Path | None:
        """Listen for and record a voice command after wake word detection"""
        ...

    def cleanup(self) -> None:
        """Clean up resources"""
        ...

    def reload_model(self, model_path: str | Path) -> dict[str, object]:
        """Swap the active model without restarting the detector."""
        ...


class WakeDetector:
    """
    Unified wake word detection abstraction.

    Uses ViolaWake as the exclusive wake word detection engine.
    """

    # Circuit breaker configuration for detection failures
    _DETECTION_FAILURE_THRESHOLD = 10  # Consecutive failures before circuit opens
    _DETECTION_RECOVERY_TIMEOUT = 30.0  # Seconds before trying again

    def __init__(
        self,
        config: AppConfig,
        on_wake_word_detected: Callable[[], None],
        implementation: WakeDetectorPort | None = None,
        *,
        heartbeat_callback: Callable[[], None] | None = None,
    ):
        """
        Initialize wake word detector.

        Args:
            config: Application configuration
            on_wake_word_detected: Callback when wake word is detected
            implementation: Concrete wake detector implementation (auto-created if None)
        """
        self.config = config
        # Define user callback with proper type
        self._user_callback: Callable[[], None] | None = None
        # CRITICAL: Validate callback is actually callable (not a bool or other non-callable)
        if on_wake_word_detected is not None and not callable(on_wake_word_detected):
            logger.error(
                "Wake word callback is not callable (type: %s, value: %s). Replacing with no-op callback.",
                type(on_wake_word_detected).__name__,
                on_wake_word_detected,
            )
            # Replace with a safe no-op callback to prevent 'bool' object is not callable errors
            self._user_callback = lambda: None
        else:
            self._user_callback = on_wake_word_detected

        # Wrap the callback to check pause state before firing
        self.on_wake_word_detected = self._create_pausable_callback()
        self._raw_listener: object | None = None
        if implementation is None:
            self._impl = self._create_implementation()
        else:
            self._impl = implementation
            self._raw_listener = getattr(implementation, "_listener", implementation)
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._gain_scheduler = self._create_gain_scheduler()
        # Heartbeats for wake detection are no longer PUSHED from here. The
        # supervisor pulls health() instead, which reads the signal the
        # detection loop marks itself. The old push was a timer thread that
        # beat for as long as the stop event was clear -- true whether or not
        # the loop it stood for was still running.
        self._heartbeat_callback = heartbeat_callback
        self._intentionally_stopped = False

        # Circuit breaker for repeated detection failures
        self._detection_circuit = CircuitBreaker(
            failure_threshold=self._DETECTION_FAILURE_THRESHOLD,
            recovery_timeout=self._DETECTION_RECOVERY_TIMEOUT,
        )

        # Pause flag - when True, wake callbacks are suppressed
        self._paused = False
        self._pause_lock = threading.Lock()

        # Get policy reference for centralized decisions
        self._policy: WakeDecisionPolicyProtocol | None = None
        if _GET_WAKE_POLICY is not None:
            try:
                self._policy = _GET_WAKE_POLICY(config)
                logger.debug("WakeDetector connected to WakeDecisionPolicy")
            except (ImportError, RuntimeError) as exc:
                logger.debug("Could not connect to WakeDecisionPolicy: %s", exc)

        # Publish the live probe so /health voice_status reflects reality, not
        # bootstrap intent. Without this, a detector whose implementation failed
        # to construct (e.g. broken AEC native lib in the 1.0.1 frozen build)
        # left health reporting wake_enabled=true while _impl was None — a
        # false-green (lane-4 finding L4-5).
        #
        # That fix used is_available(), which only asks whether _impl was
        # constructed — still true for a detector whose loop has since died, so
        # the probe answered the wrong question and the false-green survived in
        # a narrower form. is_detecting() reads the signal the detection loop
        # marks itself, so health drops the moment audio stops flowing.
        try:
            from diagnostics.voice_status import register_live_wake_probe

            register_live_wake_probe(self.is_detecting)
        except (ImportError, RuntimeError) as exc:
            logger.warning("Could not register live wake probe for voice status: %s", exc)

    def _create_pausable_callback(self) -> Callable[[], None]:
        """
        Create a wrapper callback that checks pause state before firing.

        This prevents wake triggers while the system is actively listening.
        """

        def _pausable_callback() -> None:
            # Check pause state first
            with self._pause_lock:
                if self._paused:
                    logger.debug("Wake callback suppressed: detector is paused")
                    return

            # Fire the actual callback
            if self._user_callback:
                self._user_callback()

        return _pausable_callback

    def _create_implementation(self) -> WakeDetectorPort | None:
        """Create wake word detection implementation based on config"""
        # Read from config (AppConfig) - bootstrap wrote wake_enabled/wake_engine there
        engine = getattr(self.config, "wake_engine", "none")
        if isinstance(engine, str):
            engine = engine.lower()
        wake_enabled = getattr(self.config, "wake_enabled", False)

        if engine == "none" or not wake_enabled:
            logger.info(
                "Wake word detection disabled (engine=%s, enabled=%s)",
                engine,
                wake_enabled,
            )
            return None

        # Use existing factory for creation
        try:
            listener = create_wake_listener(self.config, self.on_wake_word_detected)
            if listener is None:
                return None

            # Wrap in adapter if needed
            self._raw_listener = listener
            return WakeDetectorAdapter(listener)
        except Exception as e:
            logger.error("Failed to create wake word detector: %s", e)
            return None

    def _create_gain_scheduler(self) -> AdaptiveGainSchedulerProtocol | None:
        """Initialize adaptive gain scheduler if supported and enabled."""
        if not getattr(self.config, "wake_gain_scheduler_enabled", False):
            return None
        listener = self._raw_listener
        if listener is None:
            return None
        try:
            if not hasattr(listener, "supports_adaptive_gain"):
                return None
            if not cast(SupportsAdaptiveGain, listener).supports_adaptive_gain():
                return None
        except Exception as exc:
            logger.debug("Failed to check adaptive gain support: %s", exc)
            return None

        try:
            if _ADAPTIVE_GAIN_SCHEDULER is None:
                return None
            scheduler = _ADAPTIVE_GAIN_SCHEDULER(listener=listener, config=self.config)
            return scheduler
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to initialize adaptive gain scheduler: %s", exc)
            return None

    def start(self, stop_event: threading.Event) -> bool:
        """
        Start wake word detection in a background thread.

        Args:
            stop_event: Threading event to signal shutdown

        Returns:
            True if started successfully, False otherwise
        """
        if self._impl is None:
            logger.warning("Wake word detector not available")
            return False

        self._stop_event = stop_event
        try:
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(stop_event,),
                daemon=True,
                name="wake-detector-thread",
            )
            self._thread.start()
            if self._gain_scheduler:
                self._gain_scheduler.start()
            self._intentionally_stopped = False
            logger.info("✅ Wake word detection started")
            return True
        except Exception as e:
            logger.error("Failed to start wake word detector: %s", e)
            return False

    def _run_loop(self, stop_event: threading.Event) -> None:
        """Internal run loop that calls implementation with circuit breaker protection."""
        if self._impl:
            # Check circuit breaker before starting
            if self._detection_circuit.is_open:
                logger.warning(
                    "Wake detection circuit breaker open - detection paused. " "Will retry in %.0fs",
                    self._DETECTION_RECOVERY_TIMEOUT,
                )
                return

            try:
                self._impl.run(stop_event)
                # Successful run - record success
                self._detection_circuit.record_success()
            except (OSError, RuntimeError) as exc:
                # Detection infrastructure error
                self._detection_circuit.record_failure()
                error = WakeDetectionError(str(exc))
                logger.error(
                    "Wake word detection error: %s (user message: %s)",
                    exc,
                    error.user_friendly_message(),
                )
            except Exception as exc:
                # Unexpected error - record failure but don't crash
                self._detection_circuit.record_failure()
                logger.exception("Wake word detection unexpected error: %s", exc)

    def listen_and_record_command(
        self,
        silence_threshold: int = 500,
        silence_duration: float = 1.0,
        timeout: float = 7.0,
        onset_timeout: float | None = None,
    ) -> Path | None:
        """
        Listen for and record a voice command.

        Args:
            silence_threshold: Audio level threshold for silence detection
            silence_duration: Duration of silence to stop recording
            timeout: Maximum time to wait for command
            onset_timeout: If set, wait this long for speech to start before
                          applying silence endpoint. None = immediate recording.

        Returns:
            Path to recorded audio file, or None if failed
        """
        logger.info("[DETECTOR] listen_and_record_command() called")

        if self._impl is None:
            logger.warning("[DETECTOR] _impl is None - no wake detector implementation")
            return None

        if not hasattr(self._impl, "listen_and_record_command"):
            logger.warning("[DETECTOR] _impl has no listen_and_record_command method")
            return None

        logger.info("[DETECTOR] Calling _impl.listen_and_record_command()...")
        result = self._impl.listen_and_record_command(
            silence_threshold,
            silence_duration,
            timeout,
            onset_timeout=onset_timeout,
        )
        logger.info("[DETECTOR] _impl.listen_and_record_command() returned: %s", result)
        return result

    def stop(self) -> None:
        """Stop wake word detection and cleanup"""
        # Record that this is a DELIBERATE stop before tearing anything down.
        # Muting the mic or leaving wake-word mode must not look like a death,
        # or the supervisor "recovers" the detector and silently undoes the
        # user's choice a few seconds later.
        self._intentionally_stopped = True
        if self._gain_scheduler:
            self._gain_scheduler.stop()
        if self._impl and hasattr(self._impl, "cleanup"):
            try:
                self._impl.cleanup()
            except Exception as e:
                logger.debug("Error during wake detector cleanup: %s", e)
        # Clear thread reference immediately so is_running() returns False right away.
        # The daemon thread will exit on its own when stop_event fires.
        self._thread = None

    @property
    def detection_signal(self) -> WorkSignal | None:
        """The signal the detection loop marks from inside its own body."""
        return getattr(self._raw_listener, "detection_signal", None)

    def capture_open_failed(self) -> bool:
        """True when the listener knows its capture device did not open.

        A definite fact, unlike the detection signal's "no work lately", which is
        ambiguous while a loop is still starting. Without it the runtime reported
        "listening" for the whole cold-start window on a microphone that never
        opened at all.
        """
        failed = getattr(self._raw_listener, "_capture_open_failed", None)
        return bool(failed.is_set()) if failed is not None else False

    def is_available(self) -> bool:
        """Whether a wake detector implementation was successfully constructed.

        This is a CONSTRUCTION check, not a health check: it stays true for a
        detector whose loop has since died. Use :meth:`is_detecting` for "is
        wake detection actually working", and never publish this as a status a
        user reads.
        """
        return self._impl is not None

    def is_running(self) -> bool:
        """Whether a detection thread exists that must not be started again.

        This is the CONTROL-PLANE predicate, used to avoid starting a second
        detector thread on top of a live one. It answers a question about a
        thread object, so it is true for a thread parked forever in a blocking
        device read -- do not use it to decide whether detection works. That
        question is :meth:`is_detecting`.
        """
        return self._thread is not None and self._thread.is_alive()

    def is_detecting(self) -> bool:
        """Whether wake detection is REALLY happening right now.

        True only while the detection loop keeps marking its own work signal, so
        a loop that exited, never initialised, or is parked in a blocking read
        reports False -- the cases every previous status check reported as
        healthy.
        """
        if self._impl is None or self._intentionally_stopped:
            return False
        if self.capture_open_failed():
            return False
        if not self.is_running():
            return False
        signal = self.detection_signal
        if signal is None:
            return False
        return signal.is_fresh()

    def health(self) -> Health:
        """Supervisor-facing health of the wake detection loop."""
        if self._impl is None:
            return Health.UNAVAILABLE
        if self._intentionally_stopped:
            # Off on purpose. Nothing to recover.
            return Health.STOPPED
        if self.capture_open_failed():
            # The device demonstrably did not open. Known now, not after a grace
            # period, so the supervisor can rebuild instead of the app sitting
            # deaf while every status surface says listening.
            return Health.STALLED
        signal = self.detection_signal
        if signal is None:
            # No way to measure the loop. Refuse to claim it is working rather
            # than fall back to a thread-liveness check that would report a dead
            # loop as healthy; report STOPPED so a detector we cannot vouch for
            # is never advertised as listening and never restart-thrashed.
            logger.warning(
                "Wake listener %s exposes no detection signal; wake health cannot be measured",
                type(self._raw_listener).__name__,
            )
            return Health.STOPPED
        if not self.is_running():
            return Health.STALLED
        return signal.health(present=True)

    def record_missed_command(self) -> None:
        """Record a missed command for adaptive thresholding."""
        impl = self._impl
        if impl is not None:
            missed_cmd_fn = getattr(impl, "record_missed_command", None)
            if callable(missed_cmd_fn):
                try:
                    missed_cmd_fn()
                except Exception as e:
                    logger.debug("Wake detector missed-command hook failed: %s", e)

    def wire_aec_reference(self, audio_sink: object) -> bool:
        """
        Wire AEC reference from audio sink to wake word listener.

        This connects the playback audio buffer to the wake word detector
        for Acoustic Echo Cancellation during music playback.

        Args:
            audio_sink: Audio sink with get_aec_reference_frame method

        Returns:
            True if wiring was successful, False otherwise
        """
        from voice.wake_detector.wake_factory import wire_aec_reference

        return wire_aec_reference(self._raw_listener, audio_sink)

    def set_playback_context(self, is_playing: bool, volume: int = 80) -> None:
        """
        Update playback context for threshold adjustment.

        Updates both the central policy and the listener.

        Args:
            is_playing: Whether media is currently playing
            volume: Current playback volume (0-100)
        """
        # Update central policy first (authoritative)
        if self._policy is not None:
            try:
                self._policy.set_playback_active(is_playing, volume)
            except Exception as e:
                logger.debug("Failed to set policy playback context: %s", e)

        # Also update listener for backward compatibility
        listener = self._raw_listener
        if listener is not None and hasattr(listener, "set_playback_context"):
            try:
                listener.set_playback_context(is_playing, volume)
            except Exception as e:
                logger.debug("Failed to set listener playback context: %s", e)

    def mark_detection_as_false_positive(self) -> None:
        """Mark the last detection as a false positive for adaptive thresholding."""
        impl = self._impl
        if impl is not None:
            fp_fn = getattr(impl, "mark_last_detection_as_false_positive", None)
            if callable(fp_fn):
                try:
                    fp_fn()
                except Exception as e:
                    logger.debug("Wake detector false-positive hook failed: %s", e)

    def get_metrics(self) -> dict[str, object]:
        """Expose listener metrics when available."""
        metrics: dict[str, object] = {}
        listener = getattr(self, "_raw_listener", None)
        if listener and hasattr(listener, "get_detection_metrics"):
            try:
                metrics["detection"] = listener.get_detection_metrics()
            except (AttributeError, RuntimeError) as exc:
                logger.debug("Wake detector metrics unavailable: %s", exc)

        # Include policy diagnostics
        if self._policy is not None:
            try:
                metrics["policy"] = self._policy.get_diagnostics()
            except (AttributeError, RuntimeError) as exc:
                logger.debug("Wake policy diagnostics unavailable: %s", exc)

        # Include circuit breaker state
        metrics["circuit_breaker"] = {
            "state": self._detection_circuit.state,
            "failures": self._detection_circuit.failures,
            "is_open": self._detection_circuit.is_open,
        }

        return metrics

    def reload_model(self, model_path: str | Path) -> dict[str, object]:
        """Hot-reload the active wake model if the concrete listener supports it."""

        impl = self._impl
        if impl is None:
            return {"reloaded": False, "reason": "detector_not_running"}
        reload_fn = getattr(impl, "reload_model", None)
        if not callable(reload_fn):
            return {"reloaded": False, "reason": "reload_not_supported"}
        try:
            return dict(reload_fn(model_path))
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Wake detector model reload failed for %s: %s", model_path, exc)
            return {
                "reloaded": False,
                "reason": "reload_failed",
                "model_path": str(model_path),
            }

    def get_policy(self) -> WakeDecisionPolicyProtocol | None:
        """Get the wake decision policy (for external access)."""
        return self._policy

    def reset_circuit_breaker(self) -> None:
        """Reset the detection circuit breaker to closed state."""
        self._detection_circuit.reset()
        logger.info("Wake detection circuit breaker reset")

    def is_circuit_open(self) -> bool:
        """Check if the detection circuit breaker is open."""
        return self._detection_circuit.is_open

    def pause(self) -> None:
        """
        Pause wake detection callbacks.

        When paused, wake word detections are suppressed and callbacks
        are not fired. Use this when the system is actively listening
        for voice commands to prevent false triggers.

        CRITICAL: This MUST be called before any voice processing begins
        to prevent race conditions where a wake word fires during recording.
        """
        import threading

        with self._pause_lock:
            if not self._paused:
                self._paused = True
                logger.info(
                    "🔇 Wake detection PAUSED (listening active) thread=%s",
                    threading.current_thread().name,
                )

                # Also notify the policy if available
                if self._policy is not None:
                    try:
                        # Let the policy know we're in listening mode for extra safety
                        if hasattr(self._policy, "set_listening_active"):
                            self._policy.set_listening_active(True)
                    except Exception as e:
                        logger.debug("Could not notify policy of pause: %s", e)

    def resume(self) -> None:
        """
        Resume wake detection callbacks.

        Call this after voice command processing is complete to
        re-enable wake word detection.

        CRITICAL: This should ONLY be called after voice processing is
        completely finished, including any TTS responses.
        """
        import threading

        with self._pause_lock:
            if self._paused:
                self._paused = False
                logger.info(
                    "🔊 Wake detection RESUMED thread=%s",
                    threading.current_thread().name,
                )

                # Also notify the policy if available
                if self._policy is not None:
                    try:
                        if hasattr(self._policy, "set_listening_active"):
                            self._policy.set_listening_active(False)
                    except Exception as e:
                        logger.debug("Could not notify policy of resume: %s", e)

    def is_paused(self) -> bool:
        """Check if wake detection is currently paused."""
        with self._pause_lock:
            return self._paused


class WakeDetectorFacade:
    """
    Singleton facade for wake word detection.

    Provides a global access point for the wake detector instance,
    used by health checks and other components that need to query
    wake detector status.
    """

    _instance: WakeDetector | None = None
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> WakeDetector | None:
        """Get the singleton wake detector instance."""
        with cls._lock:
            return cls._instance

    @classmethod
    def set_instance(cls, detector: WakeDetector | None) -> None:
        """Set the singleton wake detector instance."""
        with cls._lock:
            cls._instance = detector

    @classmethod
    def is_available(cls) -> bool:
        """Check if wake detection is available."""
        instance = cls.get_instance()
        return instance is not None and instance.is_available()

    @classmethod
    def is_circuit_open(cls) -> bool:
        """Check if the detection circuit breaker is open."""
        instance = cls.get_instance()
        return instance is not None and instance.is_circuit_open()

    @classmethod
    def is_running(cls) -> bool:
        """Whether a detection thread exists (control plane, NOT health).

        See :meth:`WakeDetector.is_running`. Health questions -- "/health",
        anything a user reads -- must use :meth:`is_detecting`.
        """
        instance = cls.get_instance()
        return instance is not None and instance.is_running()

    @classmethod
    def is_detecting(cls) -> bool:
        """Whether wake detection is really happening (loop-coupled truth)."""
        instance = cls.get_instance()
        return instance is not None and instance.is_detecting()

    @classmethod
    def health(cls) -> Health:
        """Supervisor-facing health of the singleton detector."""
        instance = cls.get_instance()
        if instance is None:
            return Health.UNAVAILABLE
        return instance.health()

    @classmethod
    def detection_snapshot(cls) -> dict[str, object] | None:
        """Diagnostic view of the detection loop's work signal, if any."""
        instance = cls.get_instance()
        if instance is None:
            return None
        signal = instance.detection_signal
        return signal.snapshot() if signal is not None else None

    @classmethod
    def reload_model(cls, model_path: str | Path) -> dict[str, object]:
        """Hot-reload the singleton detector's active model."""

        instance = cls.get_instance()
        if instance is None:
            return {"reloaded": False, "reason": "detector_not_running"}
        return instance.reload_model(model_path)

    @classmethod
    def sync_to_state(cls, voice_mode: str, mic_muted: bool) -> bool:
        """Start/stop the singleton detector so it runs iff voice_mode is
        'wake_word' AND the mic is not muted.

        This generalizes the voice_mode-only start/stop toggle that used to
        be inlined in ``ui/settings_api.py`` (Bug 34.9 / 34.10 — see
        ``tests/unit/test_wake_word_runtime_toggle.py``) so an explicit mic
        mute hard-gates the wake/STT pipeline regardless of voice_mode: a
        muted mic must never leave the wake detector listening.

        Callers (the mute-hotkey settings mutation in ui/settings_api.py and
        the tray "Mute Microphone" toggle in ui/qt_native/webview_window.py)
        pass the *effective* voice_mode/mic_muted pair after their own write
        so this stays the single place that decides whether the detector
        should be running.

        Returns the detector's ``is_running()`` state after the sync (False
        if no detector instance exists).
        """
        instance = cls.get_instance()
        if instance is None:
            logger.debug("No wake detector instance to sync to voice_mode/mic_muted state")
            return False

        should_run = voice_mode == "wake_word" and not mic_muted
        try:
            if should_run:
                if not instance.is_running():
                    started = instance.start(threading.Event())
                    logger.info(
                        "Wake detector started (voice_mode=%s mic_muted=%s): %s",
                        voice_mode,
                        mic_muted,
                        started,
                    )
            elif instance.is_running():
                instance.stop()
                logger.info(
                    "Wake detector stopped (voice_mode=%s mic_muted=%s)",
                    voice_mode,
                    mic_muted,
                )
        except Exception:
            logger.exception(
                "Failed to sync wake detector to voice_mode=%s mic_muted=%s",
                voice_mode,
                mic_muted,
            )
        return instance.is_running()

    @classmethod
    def reset_for_tests(cls) -> None:
        """Reset the singleton for testing purposes."""
        with cls._lock:
            cls._instance = None


class WakeDetectorAdapter:
    """
    Adapter to wrap existing wake word listeners to match WakeDetectorPort protocol.

    Existing listeners (OpenWakeWordListener, WakeWordListener) already match
    the protocol, but this adapter provides explicit compatibility.
    """

    def __init__(self, listener: WakeDetectorPort):
        self._listener = listener

    def run(self, stop_event: threading.Event) -> None:
        """Run wake word detection loop"""
        self._listener.run(stop_event)

    def listen_and_record_command(
        self,
        silence_threshold: int = 500,
        silence_duration: float = 1.0,
        timeout: float = 7.0,
        onset_timeout: float | None = None,
    ) -> Path | None:
        """Listen for and record a voice command"""
        return self._listener.listen_and_record_command(
            silence_threshold,
            silence_duration,
            timeout,
            onset_timeout=onset_timeout,
        )

    def cleanup(self) -> None:
        """Clean up resources"""
        if hasattr(self._listener, "cleanup"):
            self._listener.cleanup()

    def reload_model(self, model_path: str | Path) -> dict[str, object]:
        """Reload the wrapped listener model when supported."""

        reload_fn = getattr(self._listener, "reload_model", None)
        if not callable(reload_fn):
            return {"reloaded": False, "reason": "reload_not_supported"}
        return dict(reload_fn(model_path))

    def record_missed_command(self) -> None:
        """Propagate missed command signal if listener supports it."""
        missed_cmd_fn = getattr(self._listener, "record_missed_command", None)
        if callable(missed_cmd_fn):
            missed_cmd_fn()

    def mark_last_detection_as_false_positive(self) -> None:
        """Propagate false positive signal if listener supports it."""
        fp_fn = getattr(self._listener, "mark_last_detection_as_false_positive", None)
        if callable(fp_fn):
            fp_fn()

    def set_aec_reference_source(self, callback) -> None:
        """Set AEC reference source callback for echo cancellation."""
        if hasattr(self._listener, "set_aec_reference_source"):
            self._listener.set_aec_reference_source(callback)
