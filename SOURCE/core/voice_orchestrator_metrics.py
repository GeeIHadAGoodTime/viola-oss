"""
Voice Orchestrator Metrics Logging.

This module contains metrics collection and logging logic
extracted from the main VoiceOrchestrator class to comply with code constraints.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from diagnostics.runtime_metrics import get_runtime_metrics
except ImportError:
    get_runtime_metrics = None


class VoiceOrchestratorMetricsManager:
    """Handles metrics collection and logging for voice orchestrator."""

    def __init__(self, orchestrator_instance: Any) -> None:
        """
        Initialize metrics manager.

        Args:
            orchestrator_instance: The VoiceOrchestrator instance
        """
        self.orchestrator = orchestrator_instance
        self._wake_metrics_logger: Any = None
        self._idle_monitor_thread: threading.Thread | None = None
        self._wake_metrics_thread: threading.Thread | None = None

    def start_idle_monitor(self) -> None:
        """Start the idle activity monitor thread."""
        if self._idle_monitor_thread and self._idle_monitor_thread.is_alive():
            logger.debug("Idle monitor already running")
            return

        self._idle_monitor_thread = threading.Thread(
            target=self._idle_monitor_loop, name="voice_idle_monitor", daemon=True
        )
        self._idle_monitor_thread.start()
        logger.debug("Started idle activity monitor")

    def _idle_monitor_loop(self) -> None:
        """Monitor idle activity and log metrics."""
        last_activity = time.time()
        check_interval = 30.0  # Check every 30 seconds

        while not self.orchestrator._stop_event.is_set():
            try:
                # Check for recent activity
                current_time = time.time()
                idle_time = current_time - last_activity

                # Log idle time every 5 minutes
                if idle_time > 300:
                    logger.info("🎤 Voice pipeline idle for %.1fs", idle_time)
                    last_activity = current_time  # Reset to avoid spam

                # Update runtime metrics
                try:
                    if get_runtime_metrics is not None:
                        metrics = get_runtime_metrics()
                        if metrics:
                            # Increment voice idle time
                            metrics.record_voice_idle(check_interval)
                except Exception as e:
                    logger.debug("Failed to update idle metrics: %s", e)

            except Exception as e:
                logger.exception("Idle monitor error: %s", e)

            # Wait before next check
            self.orchestrator._stop_event.wait(check_interval)

    def start_wake_metrics_logger(self) -> None:
        """Start the wake metrics logging thread."""
        if self._wake_metrics_thread and self._wake_metrics_thread.is_alive():
            logger.debug("Wake metrics logger already running")
            return

        try:
            from diagnostics.wake_metrics import WakeMetricsLogger

            self._wake_metrics_logger = WakeMetricsLogger()
        except ImportError:
            logger.warning("WakeMetricsLogger not available, skipping wake metrics")
            return

        self._wake_metrics_thread = threading.Thread(
            target=self._wake_metrics_loop, name="wake_metrics_logger", daemon=True
        )
        self._wake_metrics_thread.start()
        logger.debug("Started wake metrics logger")

    def _wake_metrics_loop(self) -> None:
        """Log wake metrics periodically."""
        log_interval = 300.0  # Log every 5 minutes

        while not self.orchestrator._stop_event.is_set():
            try:
                if self._wake_metrics_logger:
                    metrics = self._wake_metrics_logger.get_metrics()
                    if metrics:
                        confidence_str = (
                            f"{metrics.avg_confidence:.2f}" if metrics.avg_confidence is not None else "N/A"
                        )
                        logger.info(
                            "🎤 Wake metrics: %d total, %d false positives, %s avg confidence",
                            metrics.total_wakes,
                            metrics.false_positives,
                            confidence_str,
                        )
            except Exception as e:
                logger.exception("Wake metrics logging error: %s", e)

            # Wait before next log
            self.orchestrator._stop_event.wait(log_interval)

    def stop_metrics(self) -> None:
        """Stop all metrics threads."""
        logger.debug("Stopping metrics threads")

        # Threads are daemon threads, they'll stop when main process exits
        # But we can wait briefly for clean shutdown
        if self._idle_monitor_thread and self._idle_monitor_thread.is_alive():
            logger.debug("Idle monitor thread still running")

        if self._wake_metrics_thread and self._wake_metrics_thread.is_alive():
            logger.debug("Wake metrics thread still running")

        # Log final metrics
        try:
            if self._wake_metrics_logger:
                final_metrics = self._wake_metrics_logger.get_metrics()
                if final_metrics:
                    logger.info(
                        "🎤 Final wake metrics: %d wakes, %d false positives",
                        final_metrics.total_wakes,
                        final_metrics.false_positives,
                    )
        except Exception as e:
            logger.debug("Failed to log final metrics: %s", e)
