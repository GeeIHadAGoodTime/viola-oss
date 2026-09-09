"""
Voice Async Initializer for setting up async components.

Extracted from voice_orchestrator.py __init__ method to comply with code constraints.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class VoiceAsyncInitializer:
    """Handles initialization of async components like AsyncBridge."""

    def __init__(self, event_loop: Any | None = None) -> None:
        self.event_loop = event_loop
        self._async_bridge: Any | None = None  # AsyncCallbackBridge when initialized

    def initialize_async_bridge(self) -> Any:
        """Initialize and return the AsyncBridge."""
        # Initialize AsyncBridge for reliable async callback handling (Bug #3 fix)
        # This replaces the 3-layer fallback mess with a single, reliable path
        try:
            from utils.async_helper import AsyncCallbackBridge

            self._async_bridge = AsyncCallbackBridge(loop=self.event_loop)
            logger.info("✅ Voice: AsyncBridge initialized for wake word callbacks")
            return self._async_bridge
        except Exception as e:
            logger.error("❌ Failed to initialize AsyncBridge: %s", e)
            raise RuntimeError(
                "AsyncBridge is required for voice commands. Ensure utils/async_helper.py is available."
            ) from e

    def get_async_bridge(self) -> Any | None:
        """Get the AsyncBridge instance."""
        return self._async_bridge
