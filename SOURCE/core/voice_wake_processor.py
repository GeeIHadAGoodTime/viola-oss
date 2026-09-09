"""
Voice Wake Processor for handling wake word detection and processing.

Extracted from voice_orchestrator.py _on_wake method to comply with code constraints.

Uses WakeDecisionPolicy for centralized decision making when policy is available.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    from voice.wake_detector.wake_decision_policy import (
        WakeDecisionPolicy as WakeDecisionPolicyType,
    )

logger = get_logger(__name__)

try:  # pragma: no cover - optional in non-Qt contexts
    from ui.qt_native.debug_events import emit_debug_event
except Exception as e:
    logger.debug("Qt debug events not available (non-critical): %s", e, exc_info=True)
    emit_debug_event = None

# Import policy for centralized wake decisions
# Define typed module-level variables with proper Optional types
_get_wake_policy: Callable[..., WakeDecisionPolicyType] | None = None
POLICY_AVAILABLE = False

try:
    from voice.wake_detector.wake_decision_policy import (
        get_wake_policy as _imported_get_wake_policy,
    )

    _get_wake_policy = _imported_get_wake_policy
    POLICY_AVAILABLE = True
except ImportError:
    pass  # Already initialized to None/False above


class VoiceWakeProcessor:
    """
    Handles wake word detection processing and rate limiting.

    Uses WakeDecisionPolicy as central authority when available.
    """

    def __init__(self, orchestrator: Any):
        self.orchestrator = orchestrator
        self._last_wake_time = 0.0
        self._min_wake_interval = 2.0  # Minimum 2 seconds between wake word detections

        # Get policy reference if available
        self._policy: WakeDecisionPolicyType | None = None
        if POLICY_AVAILABLE and _get_wake_policy is not None:
            try:
                self._policy = _get_wake_policy()
                logger.debug("VoiceWakeProcessor connected to WakeDecisionPolicy")
            except Exception as e:
                logger.debug("Could not connect to WakeDecisionPolicy: %s", e)

    def check_rate_limit(self) -> bool:
        """Check if wake word detection is within rate limits."""
        now = time.time()
        since_last = now - self._last_wake_time
        if since_last < self._min_wake_interval:
            cooldown_remaining = self._min_wake_interval - since_last
            logger.debug(
                "Wake rejected (cooldown: %ss remaining, last wake was %ss ago)",
                format(cooldown_remaining, ".2f"),
                format(since_last, ".2f"),
            )
            # Record rejected wake (cooldown)
            from diagnostics.wake_metrics import get_wake_metrics

            get_wake_metrics().record_rejected_wake_cooldown()

            detector = getattr(self.orchestrator.voice_pipeline, "wake_detector", None)
            if detector:
                detector.mark_detection_as_false_positive()
            if emit_debug_event is not None:
                emit_debug_event(
                    "wake_edge_transition",
                    {
                        "state": "rate_limited",
                        "cooldown_s": self._min_wake_interval,
                        "since_last": since_last,
                        "cooldown_remaining": cooldown_remaining,
                    },
                    source="voice_orchestrator",
                )
            # Unduck audio since we're not processing this wake
            self._unduck_audio()
            return False
        return True

    def check_already_listening(self) -> bool:
        """Check if already listening for commands."""
        is_listening = bool(getattr(self.orchestrator.state, "is_listening", False))
        is_user_listening = getattr(self.orchestrator.state, "is_user_listening", None)
        if callable(is_user_listening):
            try:
                from core.user_context import get_current_user_id

                is_listening = bool(is_user_listening(get_current_user_id()))
            except (ImportError, LookupError, ValueError):
                is_listening = False
        if is_listening:
            logger.debug("Wake rejected (already listening)")
            from diagnostics.wake_metrics import get_wake_metrics

            get_wake_metrics().record_rejected_wake_already_listening()
            # Unduck audio since we're not processing this wake
            self._unduck_audio()
            return False
        return True

    def handle_wake_accepted(self) -> None:
        """Handle accepted wake word detection."""
        now = time.time()
        since_last = now - self._last_wake_time
        self._last_wake_time = now

        logger.info("Wake accepted; entering LISTENING")

        # Record accepted wake
        from diagnostics.wake_metrics import get_wake_metrics

        get_wake_metrics().record_accepted_wake()
        self.orchestrator._metrics.heartbeat(
            "voice.wake_listener",
            status="handled",
            since_last_wake=since_last,
        )

        # Include policy diagnostics in event
        policy_info = {}
        if self._policy is not None:
            try:
                policy_info = self._policy.get_diagnostics()
            except Exception as e:
                logger.warning("Failed to get wake policy diagnostics: %s", e)

        if emit_debug_event is not None:
            emit_debug_event(
                "wake_edge_transition",
                {
                    "state": "accepted",
                    "since_last": since_last,
                    "policy": policy_info,
                },
                source="voice_orchestrator",
            )

    def get_policy_diagnostics(self) -> dict[str, Any]:
        """Get diagnostics from the wake decision policy."""
        if self._policy is not None:
            try:
                return self._policy.get_diagnostics()
            except Exception as e:
                logger.debug("Could not get policy diagnostics: %s", e)
        return {}

    def handle_audio_ducking(self) -> None:
        """Handle audio ducking for wake word detection."""
        if not self.orchestrator._pipeline_handles_ducking:
            try:
                from utils.audio_ducking import get_global_ducker

                ducker = get_global_ducker()
                if ducker:
                    ducker.duck()
                    logger.debug("Audio ducked on wake word detection")
            except Exception as e:
                logger.debug("Could not duck audio on wake word: %s", e)

    def schedule_voice_command(self) -> None:
        """Schedule voice command processing."""
        # Single path: Use AsyncBridge (initialized in __init__)
        logger.info("[WAKE→CMD] Scheduling voice command handler...")
        try:
            bridge = self.orchestrator._async_bridge
            handler = self.orchestrator._command_handler

            if bridge is None:
                logger.error("[WAKE→CMD] AsyncBridge is None - cannot schedule command")
                self._unduck_audio()
                self._emit_voice_error("AsyncBridge not initialized")
                return

            if handler is None:
                logger.error("[WAKE→CMD] VoiceCommandHandler is None - cannot schedule command")
                self._unduck_audio()
                self._emit_voice_error("Command handler not initialized")
                return

            # Record wake timestamp for STT start guardrail
            handler.record_wake_timestamp()

            # Log bridge state for debugging
            loop = getattr(bridge, "_loop", None)
            loop_running = loop.is_running() if loop else False
            loop_thread = getattr(bridge, "_loop_thread", None)
            logger.info(
                "[WAKE→CMD] AsyncBridge state: loop=%s, running=%s, thread=%s",
                "exists" if loop else "None",
                loop_running,
                "alive" if loop_thread and loop_thread.is_alive() else "dead/none",
            )

            # Schedule the coroutine
            coro = handler.handle_once()
            bridge.schedule(coro)
            logger.info("[WAKE→CMD] Voice command scheduled successfully")

        except Exception as e:
            from core.validation import sanitize_error_message

            error_msg = sanitize_error_message(e)
            logger.error("[WAKE→CMD] Failed to schedule voice command: %s", error_msg)
            logger.error("This should never happen - AsyncBridge was initialized at startup.")
            logger.error("Please report this issue with your startup logs.")
            import traceback

            logger.error("Stack trace: %s", traceback.format_exc())
            # Unduck audio since command won't be processed
            self._unduck_audio()
            self._emit_voice_error(f"Failed to schedule command: {error_msg}")

    def _emit_voice_error(self, message: str) -> None:
        """Emit a voice error event for the UI."""
        if emit_debug_event is not None:
            emit_debug_event(
                "voice_error",
                {"message": message, "source": "wake_processor"},
                source="voice_orchestrator",
            )

    def _unduck_audio(self) -> None:
        """Unduck audio when wake processing is rejected or fails."""
        try:
            from utils.audio_ducking import get_global_ducker

            ducker = get_global_ducker()
            if ducker and ducker.is_ducked():
                ducker.unduck()
                logger.debug("Audio unducked after wake rejection")
        except Exception as e:
            logger.debug("Failed to unduck audio (non-critical): %s", e, exc_info=True)
