"""
Contributor Mode Manager.

Manages contributor mode lifecycle:
- Activation with timeout
- Wake detection interception
- Sample collection coordination

When contributor mode is active:
- Wake word detection runs but is DISCONNECTED (triggers don't activate commands)
- All detections are saved as false positive training samples
- Near-miss detections (score 0.4-0.7) are also captured
- Mode auto-disables after timeout (default 15 minutes)
"""

from __future__ import annotations

import contextvars
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from config import AppConfig
from core.logging_config import get_logger
from voice.wake_detector.sample_collector import SampleCollector

logger = get_logger(__name__)

# Default settings
DEFAULT_TIMEOUT_MINUTES = 15
DEFAULT_NEAR_MISS_THRESHOLD = 0.4


def default_samples_dir() -> Path:
    """Writable contributor-samples dir under the user data dir (PATH-1).

    Was the cwd-relative ``violawake_data/contributor_samples`` — on an
    installed build that either dies with PermissionError at detector
    construction (protected launch cwd; requal-M3 ``[WinError 5]``) or
    pollutes the install dir.
    """
    from violawake.config import wake_runtime_data_dir

    return wake_runtime_data_dir() / "contributor_samples"


# Per-session consent override. None = use global setting. False = block writes
# even if global consent is enabled. True = require global consent too.
session_wake_contributor: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "session_wake_contributor",
    default=None,
)


def set_session_wake_contributor(
    enabled: bool | None,
) -> contextvars.Token[bool | None]:
    """Set a session-level override for wake-data contribution.

    ``False`` blocks writes regardless of global consent. ``True`` keeps
    global behavior. ``None`` clears the override.
    """
    return session_wake_contributor.set(enabled)


class ContributorModeManager:
    """
    Manages Contributor Mode state.

    Thread-safe manager for contributor mode that:
    - Activates/deactivates mode with timeout
    - Intercepts wake detections during contributor mode
    - Collects both triggered and near-miss samples
    """

    def __init__(self, config: AppConfig | None = None):
        """
        Initialize contributor mode manager.

        Args:
            config: Application configuration
        """
        if config is None:
            from config import settings

            config = settings

        self._config = config
        configured_dir = getattr(config, "contributor_mode_save_dir", None)
        self._collector = SampleCollector(Path(configured_dir) if configured_dir else default_samples_dir())

        self._active = False
        self._started_at: datetime | None = None
        self._timeout_timer: threading.Timer | None = None
        self._session_detections = 0
        self._session_near_misses = 0
        self._lock = threading.Lock()

        # Near-miss threshold from config
        self._near_miss_threshold = getattr(config, "contributor_mode_near_miss_threshold", DEFAULT_NEAR_MISS_THRESHOLD)

        # Cooldown to prevent duplicate near-miss saves
        self._last_near_miss_time = 0.0
        self._near_miss_cooldown_seconds = 2.0

        logger.info(
            "ContributorModeManager initialized (near_miss_threshold=%s)",
            self._near_miss_threshold,
        )

    @property
    def is_active(self) -> bool:
        """Check if contributor mode is active."""
        with self._lock:
            return self._active

    @property
    def time_remaining_seconds(self) -> int | None:
        """
        Get seconds until auto-disable.

        Returns:
            Seconds remaining, or None if not active
        """
        with self._lock:
            if not self._active or self._started_at is None:
                return None

            timeout = getattr(
                self._config,
                "contributor_mode_timeout_minutes",
                DEFAULT_TIMEOUT_MINUTES,
            )
            elapsed = (datetime.now() - self._started_at).total_seconds()
            remaining = (timeout * 60) - elapsed
            return max(0, int(remaining))

    @property
    def near_miss_threshold(self) -> float:
        """Threshold for near-miss collection."""
        return self._near_miss_threshold

    @staticmethod
    def _check_consent() -> bool:
        """Check if user has consented to wake data contribution.

        Honors the session-level override: ``False`` in the contextvar
        always blocks, even if the global flag is True. This lets an
        individual session opt out (e.g. during sensitive conversations).
        """
        if session_wake_contributor.get() is False:
            return False
        try:
            from config.settings import settings

            return bool(getattr(settings, "wake_data_contribute", False))
        except Exception:
            return False

    def activate(self, timeout_minutes: int | None = None) -> dict[str, Any]:
        """
        Activate contributor mode.

        Requires wake_data_contribute consent to be enabled in settings.

        Args:
            timeout_minutes: Minutes until auto-disable (default 15)

        Returns:
            Status dict with success/error
        """
        with self._lock:
            if not self._check_consent():
                return {
                    "success": False,
                    "error": "consent_required",
                    "message": (
                        "Wake word data contribution requires your opt-in. "
                        "Say 'enable contributor mode' to turn it on."
                    ),
                }

            if self._active:
                return {
                    "success": False,
                    "error": "already_active",
                    "message": "Contributor mode is already active",
                    "time_remaining_seconds": self.time_remaining_seconds,
                }

            timeout = timeout_minutes or getattr(
                self._config,
                "contributor_mode_timeout_minutes",
                DEFAULT_TIMEOUT_MINUTES,
            )

            self._active = True
            self._started_at = datetime.now()
            self._session_detections = 0
            self._session_near_misses = 0

            # Set up auto-disable timer
            if self._timeout_timer is not None:
                self._timeout_timer.cancel()

            self._timeout_timer = threading.Timer(
                timeout * 60,
                self._auto_deactivate,
            )
            self._timeout_timer.daemon = True
            self._timeout_timer.start()

            logger.info(
                "Contributor mode ACTIVATED (timeout=%dmin). Wake detection is now DISCONNECTED - triggers will be saved as samples.",
                timeout,
            )

            return {
                "success": True,
                "message": (
                    f"Contributor mode activated. Wake word detection is now DISCONNECTED. "
                    f"All triggers will be saved as training samples. "
                    f"Auto-disables in {timeout} minutes."
                ),
                "timeout_minutes": timeout,
                "started_at": self._started_at.isoformat(),
            }

    def deactivate(self) -> dict[str, Any]:
        """
        Deactivate contributor mode.

        Returns:
            Session stats dict
        """
        with self._lock:
            if not self._active:
                return {
                    "success": False,
                    "error": "not_active",
                    "message": "Contributor mode is not active",
                }

            # Cancel timer
            if self._timeout_timer is not None:
                self._timeout_timer.cancel()
                self._timeout_timer = None

            # Calculate session duration
            duration_seconds: int = 0
            if self._started_at is not None:
                duration_seconds = int((datetime.now() - self._started_at).total_seconds())

            # Collect session stats
            stats: dict[str, Any] = {
                "success": True,
                "message": "Contributor mode deactivated. Wake detection is now connected.",
                "session_stats": {
                    "duration_seconds": duration_seconds,
                    "detections_saved": self._session_detections,
                    "near_misses_saved": self._session_near_misses,
                    "total_samples": self._session_detections + self._session_near_misses,
                },
                "pending_upload_count": self._collector.get_pending_count(),
            }

            self._active = False
            self._started_at = None
            self._session_detections = 0
            self._session_near_misses = 0

            logger.info(
                "Contributor mode DEACTIVATED. Session: %d samples saved (%d detections, %d near-misses)",
                stats["session_stats"]["total_samples"],
                stats["session_stats"]["detections_saved"],
                stats["session_stats"]["near_misses_saved"],
            )

            return stats

    def _auto_deactivate(self) -> None:
        """Auto-deactivate after timeout."""
        logger.info("Contributor mode timeout reached, auto-deactivating")
        self.deactivate()

    def on_wake_detection(
        self,
        audio: np.ndarray,
        score: float,
        threshold: float,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """
        Handle wake detection during contributor mode.

        Called when the wake word model triggers a detection.
        If contributor mode is active, saves the audio as a sample
        and returns True to indicate the caller should NOT trigger
        the normal wake callback.

        Args:
            audio: Audio data as numpy array
            score: Detection score
            threshold: Detection threshold used
            context: Additional context (playback state, etc.)

        Returns:
            True if sample was saved (mode is active) - caller should NOT trigger wake callback
            False if mode is not active - caller should proceed with normal wake flow
        """
        with self._lock:
            if not self._active:
                return False

            # Honor session-level consent override even when contributor mode
            # was activated by a previous session. Return False so the wake
            # callback still runs — silence the save, not the detection.
            if session_wake_contributor.get() is False:
                return False

            # Save as detection sample
            path = self._collector.save_detection(
                audio=audio,
                score=score,
                threshold=threshold,
                metadata=context,
            )

            if path:
                self._session_detections += 1
                logger.info(
                    "[Contributor] Saved detection (score=%.3f, threshold=%.3f). Session total: %d",
                    score,
                    threshold,
                    self._session_detections,
                )

            return True

    def on_score_update(
        self,
        audio: np.ndarray,
        score: float,
        threshold: float,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """
        Called on every score update to capture near-misses.

        Saves if: near_miss_threshold <= score < threshold

        Args:
            audio: Audio data as numpy array
            score: Current detection score
            threshold: Detection threshold
            context: Additional context

        Returns:
            True if a near-miss was saved
        """
        with self._lock:
            if not self._active:
                return False

            # Session-level consent revocation blocks near-miss writes too.
            if session_wake_contributor.get() is False:
                return False

            # Check if it's a near-miss (between our threshold and detection threshold)
            if not (self._near_miss_threshold <= score < threshold):
                return False

            # Cooldown to prevent saving many similar samples
            now = time.time()
            if now - self._last_near_miss_time < self._near_miss_cooldown_seconds:
                return False
            self._last_near_miss_time = now

            # Save as near-miss sample
            path = self._collector.save_near_miss(
                audio=audio,
                score=score,
                threshold=threshold,
                metadata=context,
            )

            if path:
                self._session_near_misses += 1
                logger.debug(
                    "[Contributor] Saved near-miss (score=%.3f, threshold=%.3f). Session near-misses: %d",
                    score,
                    threshold,
                    self._session_near_misses,
                )
                return True

            return False

    def get_status(self) -> dict[str, Any]:
        """
        Get current mode status.

        Returns:
            Dict with current status
        """
        with self._lock:
            collector_stats = self._collector.get_stats()

            return {
                "is_active": self._active,
                "started_at": (self._started_at.isoformat() if self._started_at else None),
                "time_remaining_seconds": self.time_remaining_seconds,
                "session_detections": self._session_detections,
                "session_near_misses": self._session_near_misses,
                "near_miss_threshold": self._near_miss_threshold,
                "pending_upload_count": collector_stats.get("pending_count", 0),
                "total_samples_collected": collector_stats.get("total_count", 0),
            }

    def get_pending_upload_count(self) -> int:
        """Get count of samples pending upload."""
        return self._collector.get_pending_count()

    def get_collector(self) -> SampleCollector:
        """Get the sample collector instance."""
        return self._collector


# Global instance
_manager: ContributorModeManager | None = None
_manager_lock = threading.Lock()


def get_contributor_manager(config: AppConfig | None = None) -> ContributorModeManager:
    """
    Get or create global contributor mode manager.

    Args:
        config: Application configuration (only used on first call)

    Returns:
        Global ContributorModeManager instance
    """
    global _manager

    with _manager_lock:
        if _manager is None:
            _manager = ContributorModeManager(config)
        return _manager


def reset_contributor_manager() -> None:
    """
    Reset the global manager (for testing).
    """
    global _manager

    with _manager_lock:
        if _manager is not None:
            # Deactivate if active
            if _manager.is_active:
                _manager.deactivate()
            _manager = None
