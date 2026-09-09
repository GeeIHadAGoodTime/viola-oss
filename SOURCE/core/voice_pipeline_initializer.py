"""
Voice Pipeline Initializer for setting up voice pipeline and wake callbacks.

Extracted from voice_orchestrator.py __init__ method to comply with code constraints.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class VoicePipelineInitializer:
    """Handles voice pipeline initialization and wake callback setup."""

    def __init__(self, orchestrator: Any, settings: Any):
        self.orchestrator = orchestrator
        self.settings = settings
        self.voice_pipeline: Any = None
        self._pipeline_handles_ducking = True

    def initialize_pipeline(self) -> Any:
        """Initialize and return the voice pipeline."""
        from voice.pipeline import VoicePipeline

        # Get event loop from orchestrator for thread-safe async operations
        event_loop = getattr(self.orchestrator, "_event_loop", None)

        self.voice_pipeline = VoicePipeline(
            config=self.settings,
            on_wake_word_detected=self.orchestrator._wake_callback_immutable,
            supervisor=self.orchestrator._supervisor,
            event_loop=event_loop,
            aec_reference_source=getattr(self.orchestrator, "aec_reference_source", None),
        )

        # Validate callback remains callable after pipeline initialization
        # This is a defensive check to catch any mutations during pipeline creation
        if self.orchestrator._wake_callback_immutable is None or not callable(
            self.orchestrator._wake_callback_immutable
        ):
            logger.error(
                "Wake callback became non-callable after pipeline init (type: %s, value: %r). "
                "This is a bug - callback must remain callable.",
                type(self.orchestrator._wake_callback_immutable).__name__,
                self.orchestrator._wake_callback_immutable,
            )
            raise RuntimeError("Wake word callback must remain callable")

        self._pipeline_handles_ducking = getattr(self.voice_pipeline, "handles_wake_ducking", True)

        return self.voice_pipeline

    def setup_wake_callback(self) -> None:
        """Setup the wake callback."""
        self.orchestrator._wake_manager.setup_wake_callback()

    def get_pipeline_handles_ducking(self) -> bool:
        """Get whether the pipeline handles ducking."""
        return self._pipeline_handles_ducking
