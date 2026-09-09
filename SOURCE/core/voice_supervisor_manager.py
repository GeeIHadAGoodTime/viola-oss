"""
Voice Supervisor Manager for managing supervisor and intent attachments.

Extracted from voice_orchestrator.py __init__ method to comply with code constraints.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class VoiceSupervisorManager:
    """Manages supervisor initialization and intent attachments."""

    def __init__(self, state: Any, intent: Any):
        self.state = state
        self.intent = intent
        self._supervisor: Any = None

    def initialize_supervisor(self) -> Any:
        """Initialize and return the supervisor."""
        from services.supervisor import ensure_supervisor

        self._supervisor = ensure_supervisor(self.state)
        return self._supervisor

    def attach_supervisor_to_intent(self) -> None:
        """Attach supervisor to intent if possible."""
        if self.intent and self._supervisor:
            # Don't materialize LazyIntentBridge just for supervisor attachment
            from bootstrap.lazy_intent import LazyIntentBridge

            if isinstance(self.intent, LazyIntentBridge) and object.__getattribute__(self.intent, "_real") is None:
                logger.debug("Skipping supervisor attachment - intent not yet materialized")
                return
            try:
                attach = getattr(self.intent, "attach_supervisor", None)
                if callable(attach):
                    attach(self._supervisor)
            except Exception as exc:
                logger.debug("IntentBridge supervisor attachment failed: %s", exc)

    def get_supervisor(self) -> Any:
        """Get the supervisor instance."""
        return self._supervisor
