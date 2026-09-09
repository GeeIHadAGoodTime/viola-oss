"""
Wake Word Listener Factory

Simplified factory that creates ViolaWake listener for wake word detection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from config import AppConfig
from core.logging_config import get_logger
from core.quiet_hours import wake_threshold_for_now

logger = get_logger(__name__)


def create_wake_listener(config: AppConfig, on_wake_word_detected: Callable[[], None]) -> Any | None:
    """
    Factory function to create the ViolaWake wake word listener.

    Args:
        config: Application configuration
        on_wake_word_detected: Callback when wake word is detected

    Returns:
        ViolaWakeListener instance or None if disabled
    """
    # Validate callback
    if on_wake_word_detected is not None and not callable(on_wake_word_detected):
        logger.error(
            "Wake word callback is not callable (type: %s). Replacing with no-op callback.",
            type(on_wake_word_detected).__name__,
        )
        on_wake_word_detected = lambda: None

    engine = getattr(config, "wake_engine", "none")
    if isinstance(engine, str):
        engine = engine.lower()
    else:
        engine = "none"

    if engine == "none":
        logger.info("Wake word detection disabled")
        return None

    # ViolaWake is the only supported engine
    if engine not in ("violawake", "none"):
        logger.info("Unsupported wake engine '%s', defaulting to 'violawake'", engine)
        engine = "violawake"

    if engine == "violawake":
        logger.info("🎤 Using ViolaWake wake word engine")
        try:
            from voice.wake_detector.violawake_listener import ViolaWakeListener

            threshold = wake_threshold_for_now(getattr(config, "wake_sensitivity", 0.80))
            return ViolaWakeListener(
                config=config,
                on_wake_word_detected=on_wake_word_detected,
                threshold=threshold,
            )
        except ImportError as e:
            logger.error("ViolaWake listener not available: %s", e)
            raise
    else:
        logger.warning("Unknown wake engine '%s', disabling wake word", engine)
        return None


def wire_aec_reference(
    wake_listener: Any,
    reference_source: Any,
) -> bool:
    """
    Wire an AEC reference source to a wake listener.

    Args:
        wake_listener: Wake word listener (must support set_aec_reference_source)
        reference_source: Audio output implementing AECReferenceSource protocol

    Returns:
        True if wiring was successful, False otherwise
    """
    if wake_listener is None or reference_source is None:
        logger.debug("AEC wiring skipped: listener or source is None")
        return False

    if not hasattr(reference_source, "get_aec_reference_frame"):
        logger.debug("AEC wiring skipped: source doesn't implement AECReferenceSource")
        return False

    if not hasattr(wake_listener, "set_aec_reference_source"):
        logger.debug("AEC wiring skipped: listener doesn't support AEC")
        return False

    try:
        get_ref = reference_source.get_aec_reference_frame
        wake_listener.set_aec_reference_source(get_ref)

        # Wire adapter for passive delay calibration if supported
        set_adapter = getattr(wake_listener, "set_aec_adapter", None)
        if callable(set_adapter):
            set_adapter(reference_source)

        logger.info("✅ AEC wiring complete")
        return True
    except Exception as e:
        logger.warning("AEC wiring failed: %s", e)
        return False
