"""
Voice Startup Validator for voice orchestrator initialization checks.

Extracted from voice_orchestrator.py start() method to comply with code constraints.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - optional in non-Qt contexts
    from ui.qt_native.debug_events import emit_debug_event
except Exception:
    emit_debug_event = None


class VoiceStartupValidator:
    """Validates and handles voice orchestrator startup conditions."""

    def __init__(self, capabilities: dict, metrics: Any, voice_pipeline: Any):
        self.capabilities = capabilities
        self._metrics = metrics
        self.voice_pipeline = voice_pipeline

    def check_voice_enabled(self) -> bool:
        """Check if voice is enabled by runtime profile."""
        if not self.capabilities.get("enable_voice", True):
            logger.info(
                "Voice orchestrator disabled by runtime profile '%s'",
                self.capabilities.get("runtime_profile", "unknown"),
            )
            self._metrics.heartbeat(
                "voice.pipeline",
                status="disabled",
                reason="profile_disabled_voice",
            )
            if emit_debug_event is not None:
                emit_debug_event(
                    "wake_ready",
                    {"enabled": False, "reason": "profile_disabled_voice"},
                    source="backend",
                )
            return False
        return True

    def check_stt_available(self) -> bool:
        """Check if STT is available.

        Returns False if STT is unavailable. Callers should treat this as
        non-fatal: wake detection and AEC can still run without STT.
        """
        if not self.voice_pipeline.transcriber.is_available():
            logger.warning("STT is unavailable — voice commands will be disabled.")
            self._metrics.heartbeat(
                "voice.pipeline",
                status="degraded",
                reason="stt_unavailable",
            )
            if emit_debug_event is not None:
                emit_debug_event(
                    "stt_status",
                    {"available": False, "reason": "stt_unavailable"},
                    source="backend",
                )
            return False
        return True

    def check_wake_detector(self, wake_enabled: bool) -> bool:
        """Check if wake detector is available when wake is enabled."""
        if wake_enabled:
            detector = self.voice_pipeline.wake_detector
            if detector is None or not detector.is_available():
                logger.warning("--wake requested but wake detector is unavailable; voice disabled.")
                self._metrics.heartbeat(
                    "voice.pipeline",
                    status="error",
                    reason="wake_detector_unavailable",
                )
                if emit_debug_event is not None:
                    emit_debug_event(
                        "wake_ready",
                        {"enabled": False, "reason": "wake_detector_unavailable"},
                        source="backend",
                    )
                return False
        return True

    def check_wake_start(self, wake_enabled: bool) -> bool:
        """Check if wake word detection can be started."""
        if wake_enabled:
            if not self.voice_pipeline.start(wake_enabled=True):
                logger.warning("Failed to start wake word detection")
                self._metrics.heartbeat(
                    "voice.pipeline",
                    status="error",
                    reason="wake_start_failed",
                )
                if emit_debug_event is not None:
                    emit_debug_event(
                        "wake_ready",
                        {"enabled": False, "reason": "wake_start_failed"},
                        source="backend",
                    )
                return False
            logger.info("Wake detector started via VoicePipeline")
            self._metrics.heartbeat(
                "voice.pipeline",
                status="ready",
                wake_enabled=True,
            )
            return True
        else:
            logger.warning("--voice enabled without --wake; open-mic is not implemented in this orchestrator.")
            self.voice_pipeline.start(wake_enabled=False)
            self._metrics.heartbeat(
                "voice.pipeline",
                status="ready",
                wake_enabled=False,
            )
            return True
