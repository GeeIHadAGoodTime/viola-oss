"""
Spoke Voice Handler

Integrates wake-word pipeline into Spokes with proper cancellation protocol.
When wake word fires, cancels any ongoing operations and handles voice input.

This integrates with the existing VoicePipeline - no new magic, just proper orchestration.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from config import AppConfig
from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from voice.pipeline import VoicePipeline

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


class _DebugEventEmitter(Protocol):
    """Protocol for debug event emission function."""

    def __call__(self, name: str, payload: dict[str, object] | None = None, *, source: str = "qt") -> None: ...


_emit_debug_event: _DebugEventEmitter | None = None
try:  # pragma: no cover - optional in non-Qt contexts
    from ui.qt_native.debug_events import emit_debug_event as _emit_debug_event
except Exception:  # pragma: no cover - UI not available in some environments
    logger.debug("Qt debug event emitter unavailable, debug events disabled")

emit_debug_event = _emit_debug_event


class SpokeVoiceHandler:
    """
    Voice handler for Spoke nodes.

    Integrates wake-word detection from VoicePipeline with proper cancellation
    protocol. When wake word fires, cancels ongoing operations and processes
    voice input.
    """

    def __init__(
        self,
        config: AppConfig,
        voice_pipeline: VoicePipeline,
        on_voice_command: Callable[[Path], None] | None = None,
    ):
        """
        Initialize Spoke voice handler.

        Args:
            config: Application configuration
            voice_pipeline: Existing VoicePipeline instance
            on_voice_command: Callback when voice command audio is captured
                             (for sending to Hub or local processing)
        """
        self.config = config
        self.voice_pipeline = voice_pipeline
        self.on_voice_command = on_voice_command

        # Cancellation protocol state
        self._active_operation: asyncio.Task | None = None
        self._cancellation_event = threading.Event()
        self._operation_lock = threading.Lock()
        self._last_wake_time = 0.0
        self._min_wake_interval = 2.0  # Minimum seconds between wake detections

        # Wrap wake word callback with cancellation
        self._wrapped_wake_callback = self._build_wake_callback()

        # Replace pipeline's wake callback with our wrapped version
        if hasattr(voice_pipeline, "wake_detector") and voice_pipeline.wake_detector:
            # CRITICAL: Validate callback is actually callable before assignment
            # This prevents 'bool' object is not callable errors
            if self._wrapped_wake_callback is not None and not callable(self._wrapped_wake_callback):
                logger.error(
                    "Wake word callback is not callable (type: %s, value: %s). Skipping callback assignment. This is a bug - wake callbacks must be callable functions, not booleans or other values.",
                    type(self._wrapped_wake_callback).__name__,
                    self._wrapped_wake_callback,
                )
                # Replace with safe no-op to prevent crashes
                self._wrapped_wake_callback = lambda: None
            # Update the callback on the wake detector
            voice_pipeline.wake_detector.on_wake_word_detected = self._wrapped_wake_callback
            # Also update the listener's callback if it exists
            if hasattr(voice_pipeline.wake_detector, "_raw_listener"):
                listener = voice_pipeline.wake_detector._raw_listener
                if listener is not None and hasattr(listener, "on_wake_word_detected"):
                    # Validate listener callback assignment too
                    if callable(self._wrapped_wake_callback):
                        listener.on_wake_word_detected = self._wrapped_wake_callback
                    else:
                        logger.error(
                            "Cannot set listener callback to non-callable: type=%s, value=%s",
                            type(self._wrapped_wake_callback).__name__,
                            self._wrapped_wake_callback,
                        )

    def _build_wake_callback(self) -> Callable[[], None]:
        """
        Build wake word callback with cancellation protocol.

        Returns:
            Callback function that handles wake word detection with cancellation
        """

        def wake_callback() -> None:
            """Handle wake word detection with cancellation protocol."""
            # Rate limiting
            now = time.time()
            since_last = now - self._last_wake_time
            if since_last < self._min_wake_interval:
                logger.debug("Rate limiting wake word (cooldown: %ss)", self._min_wake_interval)
                if emit_debug_event is not None:
                    try:
                        emit_debug_event(
                            "wake_edge_transition",
                            {
                                "state": "rate_limited",
                                "cooldown_s": self._min_wake_interval,
                                "since_last": since_last,
                            },
                            source="spoke_voice_handler",
                        )
                    except Exception as e:  # Silent OK: debug telemetry failure should not interrupt wake handling
                        logger.debug("Operation failed: %s", e, exc_info=True)
                        pass
                return

            self._last_wake_time = now
            logger.info("Wake word detected on Spoke; initiating cancellation protocol")

            if emit_debug_event is not None:
                try:
                    emit_debug_event(
                        "wake_edge_transition",
                        {"state": "handled", "since_last": since_last},
                        source="spoke_voice_handler",
                    )
                except Exception as e:  # Silent OK: debug telemetry failure should not interrupt wake handling
                    logger.debug("Operation failed: %s", e, exc_info=True)
                    pass

            # CANCELLATION PROTOCOL: Cancel any ongoing operations
            self._cancel_ongoing_operations()

            # Process wake word (record audio, send to Hub, etc.)
            self._handle_wake_word()

        return wake_callback

    def _cancel_ongoing_operations(self) -> None:
        """
        Cancel any ongoing voice operations.

        This implements the cancellation protocol - when wake word fires,
        we cancel any in-flight operations to ensure clean state.
        """
        with self._operation_lock:
            # Signal cancellation
            self._cancellation_event.set()

            # Cancel active async operation if exists
            if self._active_operation and not self._active_operation.done():
                logger.info("Cancelling ongoing voice operation due to wake word")
                self._active_operation.cancel()
                # Give cancellation a moment to propagate
                try:
                    self._active_operation.result()
                except asyncio.CancelledError:  # Silent OK: expected when cancelling async task
                    pass
                except Exception as e:
                    logger.debug("Error during operation cancellation: %s", e)

            # Reset cancellation event for next operation
            self._cancellation_event.clear()

    def _handle_wake_word(self) -> None:
        """
        Handle wake word detection.

        Records audio and either processes locally or sends to Hub.
        """
        # Schedule async handling
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                _task = asyncio.create_task(self._handle_wake_word_async())
                _task.add_done_callback(_log_task_exception)
            else:
                loop.run_until_complete(self._handle_wake_word_async())
        except RuntimeError:
            # No event loop, create new one
            asyncio.run(self._handle_wake_word_async())

    async def _handle_wake_word_async(self) -> None:
        """Async handler for wake word - records audio and processes."""
        # Check if cancelled before starting
        if self._cancellation_event.is_set():
            logger.debug("Wake word handling cancelled before start")
            return

        with self._operation_lock:
            # Create new operation task
            self._active_operation = asyncio.create_task(self._record_and_process_command())

        try:
            await self._active_operation
        except asyncio.CancelledError:
            logger.debug("Voice command recording cancelled")
        except Exception as e:
            logger.error("Error handling wake word: %s", e)
        finally:
            with self._operation_lock:
                self._active_operation = None

    async def _record_and_process_command(self) -> None:
        """
        Record voice command after wake word detection.

        Uses existing VoicePipeline methods - no new magic.
        """
        # Check cancellation before recording
        if self._cancellation_event.is_set():
            return

        logger.info("🎤 Recording voice command after wake word...")

        # Use existing pipeline method to record
        try:
            audio_file = await asyncio.to_thread(
                self.voice_pipeline.listen_and_record,
                silence_threshold=500,
                silence_duration=1.0,
                timeout=TIMEOUT_LONG,
            )
        except Exception as e:
            logger.error("Failed to record voice command: %s", e)
            return

        # Check cancellation after recording
        if self._cancellation_event.is_set():
            logger.debug("Voice command cancelled after recording")
            if audio_file:
                try:
                    audio_file.unlink(missing_ok=True)
                except Exception as e:  # Silent OK: file cleanup during cancellation
                    logger.debug("Operation failed: %s", e, exc_info=True)
                    pass
            return

        if not audio_file:
            logger.warning("No audio recorded after wake word")
            return

        logger.info("✅ Voice command recorded: %s", audio_file)

        # Call callback to send to Hub or process locally
        if self.on_voice_command:
            try:
                self.on_voice_command(audio_file)
            except Exception as e:
                logger.error("Error in voice command callback: %s", e)
        else:
            logger.debug("No voice command callback configured")

    def start(self, wake_enabled: bool = True) -> bool:
        """
        Start voice handler.

        Args:
            wake_enabled: Whether to enable wake word detection

        Returns:
            True if started successfully
        """
        if wake_enabled:
            return self.voice_pipeline.start(wake_enabled=True)
        return True

    def stop(self) -> None:
        """Stop voice handler and cancel any ongoing operations."""
        # Cancel ongoing operations
        self._cancel_ongoing_operations()

        # Stop pipeline
        self.voice_pipeline.stop()

        logger.info("Spoke voice handler stopped")

    def is_available(self) -> bool:
        """Check if voice handler is available."""
        return self.voice_pipeline.is_available()
