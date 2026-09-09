"""
Voice Orchestrator Wake Word Management.

This module contains wake word detection and callback management
logic extracted from the main VoiceOrchestrator class to comply with code constraints.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

from core.constants import TIMEOUT_DEFAULT
from core.logging_config import get_logger
from core.task_tracker import TaskTracker

if TYPE_CHECKING:
    from core.logging_config import StructuredLogger

logger = get_logger(__name__)


def _start_policy_playback_poller(music: Any, policy: Any, log: StructuredLogger) -> None:
    """
    Start a background poller that pushes playback state into the wake policy.

    This is a robustness fallback in case music.on_state_change doesn't fire
    reliably (e.g., in embedded YouTube mode). Polls every 1 second and only
    updates the policy when state actually changes.
    """
    if getattr(music, "_wake_policy_poller_started", False):
        log.debug("Wake policy poller already started, skipping")
        return

    music._wake_policy_poller_started = True

    def _loop() -> None:
        last: tuple[bool | None, int | None] = (None, None)
        poll_count = 0
        source_used = "unknown"
        while True:
            try:
                # Try multiple sources for is_playing state
                # The music object might be MusicControllerAdapter wrapping a player
                is_playing = False
                volume = 80

                # Source 1: music.player._state.is_playing (adapter -> player -> state)
                player = getattr(music, "player", None)
                if player is not None:
                    state_obj = getattr(player, "_state", None)
                    if state_obj is not None:
                        is_playing = bool(getattr(state_obj, "is_playing", False))
                        volume = int(getattr(state_obj, "volume", 80) or 80)
                        source_used = "player._state"
                    else:
                        # Source 2: music.player.is_playing
                        is_playing = bool(getattr(player, "is_playing", False))
                        volume = int(getattr(player, "volume", 80) or 80)
                        source_used = "player.is_playing"
                elif hasattr(music, "status") and callable(music.status):
                    # Source 3: music.status() method (MusicControllerAdapter)
                    try:
                        status = music.status()
                        is_playing = bool(status.get("is_playing", False))
                        volume = int(status.get("volume", 80) or 80)
                        source_used = "music.status()"
                    except Exception:
                        source_used = "status() failed"
                else:
                    # Source 4: Direct attributes (fallback)
                    is_playing = bool(getattr(music, "is_playing", False))
                    volume = int(getattr(music, "volume", 80) or 80)
                    source_used = "direct attrs"

                cur = (is_playing, volume)

                # First poll: log diagnostic info
                poll_count += 1
                if poll_count == 1:
                    log.info(
                        "Wake policy poller FIRST READ: source=%s, is_playing=%s, volume=%s",
                        source_used,
                        is_playing,
                        volume,
                    )

                # Log every 10 polls for diagnostics
                if poll_count % 10 == 0:
                    log.debug(
                        "Wake policy poller check #%d: music.is_playing=%s, volume=%s, policy.is_playback_active=%s",
                        poll_count,
                        is_playing,
                        volume,
                        policy.is_playback_active,
                    )

                if cur != last:
                    policy.set_playback_active(is_playing, volume)
                    log.info(
                        "Wake policy playback state updated (poll): playing=%s, volume=%s",
                        is_playing,
                        volume,
                    )
                    last = cur
            except Exception as e:
                log.debug("Wake policy poller error: %s", e)
            time.sleep(TIMEOUT_DEFAULT)  # Poll interval for playback state

    t = threading.Thread(target=_loop, name="wake-policy-playback-poller", daemon=True)
    t.start()
    log.info(
        "✅ Wake policy playback poller started (fallback) - music type: %s",
        type(music).__name__,
    )


class VoiceOrchestratorWakeManager:
    """Handles wake word detection and callback management."""

    def __init__(self, orchestrator_instance: Any) -> None:
        """
        Initialize wake manager.

        Args:
            orchestrator_instance: The VoiceOrchestrator instance
        """
        self.orchestrator = orchestrator_instance
        self._background_tasks = TaskTracker()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop for thread-safe coroutine scheduling."""
        self._event_loop = loop

    def _schedule_coroutine_threadsafe(self, coro: Any) -> None:
        """
        Schedule a coroutine from any thread onto the main event loop.

        This is needed because wake word callbacks run on a separate detector thread,
        but we need to run async operations on the main event loop.
        """
        if self._event_loop is None:
            # If called from async context, schedule via background task tracker.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                in_async_context = False
            else:
                in_async_context = True

            if in_async_context:
                self._background_tasks.create_task(coro)
                return

            # Try to get the event loop from the orchestrator
            if hasattr(self.orchestrator, "_event_loop") and self.orchestrator._event_loop:
                self._event_loop = self.orchestrator._event_loop

        if self._event_loop is None or self._event_loop.is_closed():
            # Expected when running from wake detector thread without async context
            logger.debug("No event loop available for scheduling coroutine (wake thread)")
            # Close the coroutine to avoid "never awaited" warning
            coro.close()
            return

        # Schedule the coroutine on the main event loop from this thread
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._event_loop)
            # Add callback to log any exceptions
            future.add_done_callback(self._log_future_exception)
        except Exception as e:
            logger.error("Failed to schedule coroutine: %s", e)
            coro.close()

    def _log_future_exception(self, future: Any) -> None:
        """Log any exceptions from futures scheduled via run_coroutine_threadsafe."""
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Scheduled coroutine failed: %s", e)

    def setup_wake_callback(self) -> None:
        """Setup the wake callback with defensive validation."""
        # CRITICAL: Store callback reference in immutable field to prevent overwriting
        # The callback must remain callable - never assign a boolean or other non-callable value
        # This field should NEVER be reassigned after initialization
        # Use object.__setattr__ to bypass our custom __setattr__ during initialization
        object.__setattr__(self.orchestrator, "_wake_callback_immutable", self.orchestrator._on_wake)

        # Defensive validation: ensure callback is callable before passing to pipeline
        if self.orchestrator._wake_callback_immutable is None or not callable(
            self.orchestrator._wake_callback_immutable
        ):
            error_msg = (
                f"Wake callback is not callable at orchestrator init "
                f"(type: {type(self.orchestrator._wake_callback_immutable).__name__}, "
                f"value: {self.orchestrator._wake_callback_immutable}). This is a critical bug."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

    def validate_wake_callback_immutable(self) -> None:
        """Validate that the wake callback hasn't been corrupted."""
        # Runtime validation: ensure callback remains callable
        if self.orchestrator._wake_callback_immutable is None or not callable(
            self.orchestrator._wake_callback_immutable
        ):
            error_msg = (
                f"CRITICAL BUG: Wake callback corrupted at runtime! "
                f"Expected callable, got {type(self.orchestrator._wake_callback_immutable).__name__} "
                f"with value {self.orchestrator._wake_callback_immutable}. "
                f"This indicates a mutation bug in the orchestrator."
            )
            logger.error(error_msg)
            # Don't raise here - log and continue with degraded functionality
            # The pipeline may still work if it has other wake detection mechanisms

            # Note: _wake_callback_violation flag is set by VoiceOrchestrator.__setattr__
            # on mutation attempts to track when invalid assignments are rejected.

    def on_wake_detected(self) -> None:
        """
        Handle initial wake word detection setup.

        This method handles the early-stage wake word response:
        - Validates callback integrity
        - Records metrics
        - Cancels any in-flight work

        NOTE: This method does NOT start voice processing. That is handled by
        VoiceWakeProcessor.schedule_voice_command() which is called separately
        in VoiceOrchestrator._on_wake() after rate limiting checks pass.

        IMPORTANT: This is called from the wake detector thread, NOT the main event loop.
        """
        try:
            logger.info("🎤 Wake word detected - preparing voice pipeline")

            # Validate callback integrity before use
            self.validate_wake_callback_immutable()

            # Record wake event for metrics
            if hasattr(self.orchestrator, "_wake_metrics_logger"):
                self.orchestrator._wake_metrics_logger.record_wake_event()

            # Cancel any in-flight work
            if hasattr(self.orchestrator.intent, "cancel_inflight_work"):
                try:
                    self.orchestrator.intent.cancel_inflight_work()
                except Exception as e:
                    logger.debug("Intent cancellation failed: %s", e)

            # NOTE: Voice processing is started by VoiceWakeProcessor.schedule_voice_command()
            # which calls VoiceCommandHandler.handle_once() after rate limiting checks.
            # We do NOT start voice processing here to avoid duplicate handling.

        except Exception:
            logger.exception("Wake word detection setup failed")

    def cancel_background_tasks(self) -> None:
        """Best-effort cancel of wake-related background tasks."""
        if hasattr(self, "_background_tasks"):
            self._background_tasks.cancel_all_nowait()

    def wire_playback_state(self) -> bool:
        """
        Wire music player playback state to wake decision policy.

        This connects the music player's on_state_change callback to update
        the wake decision policy's playback context. This is CRITICAL for
        preventing false positive wake detections during music playback.

        IMPORTANT: This wires directly to the policy singleton, NOT to wake_detector.
        If music exists and policy exists, we wire. Period.
        The wake_detector is just one consumer of the policy.

        Returns:
            True if wiring was successful, False otherwise
        """
        orchestrator = self.orchestrator
        music = getattr(orchestrator, "music", None)

        # DIAGNOSTIC: Log exactly what we have
        logger.info(
            "wire_playback_state() called: music=%s, type=%s",
            "exists" if music else "None",
            type(music).__name__ if music else "N/A",
        )

        if music is None:
            logger.warning("⚠️ No music controller - playback state wiring SKIPPED (false positives likely)")
            return False

        # DIAGNOSTIC: Check the path to _state
        player = getattr(music, "player", None)
        state_obj = getattr(player, "_state", None) if player else None
        logger.info(
            "wire_playback_state() path check: music.player=%s, player._state=%s",
            "exists" if player else "None",
            "exists" if state_obj else "None",
        )

        # Get the policy singleton - this is the target for playback state
        try:
            from voice.wake_detector.wake_decision_policy import get_wake_policy

            policy = get_wake_policy()
        except Exception as e:
            logger.warning("Could not get wake policy: %s", e)
            return False

        # Try to get defense health monitor for tracking
        health_monitor = None
        try:
            from voice.wake_detector.defense_health_monitor import DefenseHealthMonitor

            health_monitor = DefenseHealthMonitor(policy)
            health_monitor.set_music_controller(music)
        except ImportError:
            logger.debug("DefenseHealthMonitor unavailable; continuing without it")

        # Get existing on_state_change callback to chain
        # Wire to music.player (MusicPlayer), NOT music (MusicControllerAdapter).
        # The adapter doesn't fire on_state_change; the player does (core.py:570).
        # (player already resolved at line 300 diagnostic block above)
        if player is None:
            logger.warning("⚠️ music.player is None - callback wiring SKIPPED")
            _start_policy_playback_poller(music, policy, logger)
            return True
        previous_callback = getattr(player, "on_state_change", None)

        def on_playback_state_change(state: Any) -> None:
            """Update wake policy when playback state changes."""
            try:
                is_playing = getattr(state, "is_playing", False)
                volume = getattr(state, "volume", 80)

                # Update policy directly (singleton)
                policy.set_playback_active(is_playing, volume)
                logger.debug(
                    "Wake policy playback state updated: playing=%s, volume=%d",
                    is_playing,
                    volume,
                )

                # Track for health monitoring
                if health_monitor is not None:
                    health_monitor.record_playback_update()

            except Exception as e:
                logger.debug("Failed to update wake policy playback state: %s", e)

            # Chain to previous callback if it exists
            if previous_callback is not None:
                try:
                    previous_callback(state)
                except Exception as e:
                    logger.debug("Previous on_state_change callback failed: %s", e)

        # Wire the callback
        try:
            player.on_state_change = on_playback_state_change
            logger.info("✅ Playback state wired to wake decision policy (via music.player)")

            # Set initial state immediately - don't wait for a state change
            # Music might already be playing when the app starts
            # Read from player._state (same source the poller uses)
            try:
                state_obj = getattr(player, "_state", None)
                if state_obj is not None:
                    current_is_playing = bool(getattr(state_obj, "is_playing", False))
                    current_volume = int(getattr(state_obj, "volume", 80) or 80)
                else:
                    current_is_playing = bool(getattr(player, "is_playing", False))
                    current_volume = int(getattr(player, "volume", 80) or 80)
                policy.set_playback_active(current_is_playing, current_volume)
                logger.info(
                    "Initial playback state set: playing=%s, volume=%d",
                    current_is_playing,
                    current_volume,
                )
            except Exception as init_err:
                logger.debug("Could not set initial playback state: %s", init_err)

            # Start poll-based fallback for robustness
            # This ensures policy convergence even if on_state_change doesn't fire
            _start_policy_playback_poller(music, policy, logger)

            # Start defense health monitoring
            if health_monitor is not None:
                health_monitor.start_monitoring()
                logger.info("Defense health monitor started")

            # Update health metrics
            try:
                from diagnostics.defense_metrics import get_defense_metrics

                get_defense_metrics().update_health(playback_synced=True)
            except ImportError:
                logger.debug("Defense metrics unavailable; skipping playback_synced metric update")

            return True
        except Exception as e:
            logger.warning("Failed to wire playback state: %s", e)
            return False
