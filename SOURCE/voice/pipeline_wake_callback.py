"""
Voice pipeline wake callback utilities.

Extracted from voice/pipeline.py to reduce method complexity.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from core.exceptions import AudioDeviceError, WakeCallbackError
from core.logging_config import get_logger
from diagnostics.operation_trace import OperationType, record_operation

logger = get_logger(__name__)


class WakeCallbackManager:
    """
    Manages wake word callback creation and wrapping.

    Handles audio ducking, heartbeat emission, and callback validation.
    """

    def __init__(self, pipeline_instance: Any):
        self.pipeline = pipeline_instance
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop for thread-safe async operations."""
        self._event_loop = loop

    def create_wake_callback_with_ducking(self, on_wake_word_detected: Callable[[], None] | None) -> Callable[[], None]:
        """
        Create wake callback with audio ducking support.

        This implements the complex wake callback wrapping logic that was
        previously in VoicePipeline._create_wake_callback_with_ducking.

        CRITICAL: Input validation to prevent 'bool' object is not callable errors.
        The root cause of this bug is passing a boolean (e.g., wake_enabled=True)
        instead of a callback function.

        Args:
            on_wake_word_detected: Callback function or None. Must be callable if not None.

        Returns:
            A wrapped callback function that handles audio ducking.

        Raises:
            TypeError: If on_wake_word_detected is not None and not callable.
        """
        # CRITICAL: Validate input - crash loud to find root cause
        if on_wake_word_detected is not None and not callable(on_wake_word_detected):
            import traceback

            stack = "".join(traceback.format_stack())
            error_msg = (
                f"FATAL: on_wake_word_detected must be callable or None!\n"
                f"Got type: {type(on_wake_word_detected).__name__}\n"
                f"Got value: {on_wake_word_detected!r}\n"
                f"This usually means a boolean (wake_enabled) was passed instead of a callback.\n"
                f"Stack trace:\n{stack}"
            )
            logger.critical(error_msg)
            raise TypeError(error_msg)

        def wrapped_callback() -> None:
            """
            Wake word detected callback with audio ducking and validation.

            This callback runs when wake word is detected and coordinates
            audio ducking, heartbeat emission, and wake word validation.

            IMPORTANT: This runs on the wake detector thread, NOT the main event loop.
            """
            # Emit heartbeat if supervisor is available
            if self.pipeline._supervisor:
                try:
                    self.pipeline._emit_wake_heartbeat()
                except (AttributeError, RuntimeError) as exc:
                    logger.debug("Wake heartbeat emission failed: %s", exc)

            # Validate callback is still callable (not corrupted by runtime mutation)
            callback = object.__getattribute__(self.pipeline, "_wake_callback_immutable")
            if callback is None:
                logger.warning(
                    "Wake word callback is None - wake word detection disabled. "
                    "This may indicate a configuration issue or runtime corruption."
                )
                return

            if not callable(callback):
                logger.error(
                    "Wake word callback became non-callable at runtime (type: %s, value: %s). This indicates a serious bug in callback storage. Wake word detection is now disabled.",
                    type(callback).__name__,
                    callback,
                )
                # Mark as corrupted to prevent further attempts
                object.__setattr__(self.pipeline, "_wake_callback_violation", True)
                return

            # Audio ducking for wake feedback
            if self.pipeline._audio_ducker:
                try:
                    self.pipeline._audio_ducker.duck()
                except (OSError, RuntimeError) as exc:
                    error = AudioDeviceError("output", str(exc))
                    logger.warning(
                        "Audio ducking failed: %s (user message: %s)",
                        exc,
                        error.user_friendly_message(),
                    )

            # Wake feedback is the chime alone (played by the wake processor in
            # the callback below, right after this duck), never a spoken "Yes?".

            # NOTE: Audio stays ducked until voice interaction completes.
            # This matches Alexa/Google Home behavior - ducking persists during:
            # - User speaking their command
            # - STT transcription
            # - Command processing
            # - TTS response
            # The unduck happens in VoiceCommandHandler when the interaction finishes.

            # Call the user's wake callback
            try:
                callback()
                # Record successful wake detection
                record_operation(
                    OperationType.VOICE,
                    "wake_detected",
                    success=True,
                    details={"active_commands": self.pipeline._active_commands},
                )
            except TypeError as exc:
                # Callback type error - likely a configuration bug (e.g., bool instead of callable)
                callback_error = WakeCallbackError(type(callback).__name__, str(exc))
                logger.error(
                    "Wake word callback type error: %s (user message: %s)\nCallback type: %s, value: %r",
                    exc,
                    callback_error.user_friendly_message(),
                    type(callback).__name__,
                    callback,
                )
                record_operation(
                    OperationType.VOICE,
                    "wake_detected",
                    success=False,
                    error=f"Callback type error: {exc}",
                )
            except (AttributeError, RuntimeError) as exc:
                # Callback execution error
                import traceback

                callback_error = WakeCallbackError(type(callback).__name__, str(exc))
                logger.error(
                    "Wake word callback failed: %s (user message: %s)\nCallback type: %s, value: %r\nTraceback:\n%s",
                    exc,
                    callback_error.user_friendly_message(),
                    type(callback).__name__,
                    callback,
                    "".join(traceback.format_exc()),
                )
                record_operation(
                    OperationType.VOICE,
                    "wake_detected",
                    success=False,
                    error=f"Callback failed: {exc}",
                )

        # Final validation: ensure we're returning a callable
        assert callable(wrapped_callback), "wrapped_callback must be callable"
        return wrapped_callback

    def build_wake_callback(self) -> Callable[[], None]:
        """
        Build the wake callback for the pipeline.

        Retrieves the stored immutable callback and wraps it with ducking support.
        """
        # Get the immutable callback (should never be None/callable after validation)
        callback = object.__getattribute__(self.pipeline, "_wake_callback_immutable")
        return self.create_wake_callback_with_ducking(callback)

    def emit_wake_heartbeat(self) -> None:
        """
        Emit heartbeat for wake word detection.

        Called when wake word is detected to indicate pipeline health.
        """
        if not self.pipeline._supervisor:
            return

        try:
            self.pipeline._supervisor.heartbeat(
                component="wake_detector",
                status="wake_word_detected",
                metadata={
                    "timestamp": time.time(),
                    "active_commands": self.pipeline._active_commands,
                },
            )
        except (AttributeError, RuntimeError) as exc:
            logger.debug("Wake heartbeat failed: %s", exc)
