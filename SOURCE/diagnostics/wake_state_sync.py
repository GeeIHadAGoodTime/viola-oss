"""
Wake Word State Synchronization Diagnostics
============================================

Tracks state synchronization between different components that affect
wake word detection decisions.

Monitors:
- Playback State Sync: Agreement between callback and RMS-based detection
- TTS State Integration: Track TTS speech for false positive classification
- Volume State: Track volume changes and ducking
- State Transition Latency: How quickly state changes propagate

Usage:
    from diagnostics.wake_state_sync import get_state_sync_monitor

    monitor = get_state_sync_monitor()

    # Update playback state from callback
    monitor.update_playback_callback(is_playing=True, volume=80)

    # Update RMS-detected playback state
    monitor.update_playback_rms(loopback_rms=3500)

    # Update TTS state
    monitor.update_tts_state(is_speaking=True, utterance_id="abc123")

    # Check synchronization health
    health = monitor.get_sync_health()
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Data Structures                                                              #
# --------------------------------------------------------------------------- #


class SyncStatus(Enum):
    """State synchronization status."""

    IN_SYNC = "in_sync"  # All signals agree
    MINOR_DISAGREEMENT = "minor_disagreement"  # Brief disagreement (<500ms)
    MAJOR_DISAGREEMENT = "major_disagreement"  # Extended disagreement
    STALE = "stale"  # State hasn't been updated recently


@dataclass
class PlaybackStateSync:
    """Playback state synchronization metrics."""

    # Callback-reported state (from player)
    callback_playback_active: bool = False
    callback_volume: int = 0
    callback_timestamp: float = 0.0

    # RMS-detected state (ground truth)
    rms_playback_active: bool = False
    rms_value: float = 0.0
    rms_timestamp: float = 0.0

    # Agreement tracking
    in_sync: bool = True
    disagreement_start_time: float | None = None
    disagreement_duration_ms: float = 0.0
    total_disagreement_count: int = 0

    # Latency measurement
    last_state_change_timestamp: float = 0.0
    notification_latency_ms: float = 0.0

    # RMS threshold for playback detection
    RMS_THRESHOLD: float = 2000.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "callback_state": {
                "playback_active": self.callback_playback_active,
                "volume": self.callback_volume,
                "timestamp": self.callback_timestamp,
                "age_ms": ((time.time() - self.callback_timestamp) * 1000 if self.callback_timestamp > 0 else 0),
            },
            "rms_state": {
                "playback_active": self.rms_playback_active,
                "rms_value": round(self.rms_value, 1),
                "timestamp": self.rms_timestamp,
                "age_ms": ((time.time() - self.rms_timestamp) * 1000 if self.rms_timestamp > 0 else 0),
            },
            "sync_status": {
                "in_sync": self.in_sync,
                "disagreement_duration_ms": round(self.disagreement_duration_ms, 1),
                "total_disagreement_count": self.total_disagreement_count,
                "notification_latency_ms": round(self.notification_latency_ms, 1),
            },
        }


@dataclass
class TTSState:
    """TTS (Text-to-Speech) state for false positive classification."""

    is_speaking: bool = False
    current_utterance_id: str | None = None
    speech_start_timestamp: float | None = None
    estimated_duration_ms: float | None = None
    estimated_remaining_ms: float | None = None

    # History for classification
    recent_utterance_count: int = 0
    last_utterance_end_timestamp: float | None = None

    def is_recently_speaking(self, within_ms: float = 1000.0) -> bool:
        """Check if TTS was speaking recently."""
        if self.is_speaking:
            return True
        if self.last_utterance_end_timestamp is None:
            return False
        return (time.time() - self.last_utterance_end_timestamp) * 1000 < within_ms

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "is_speaking": self.is_speaking,
            "current_utterance_id": self.current_utterance_id,
            "speech_start_timestamp": self.speech_start_timestamp,
            "estimated_duration_ms": self.estimated_duration_ms,
            "estimated_remaining_ms": self.estimated_remaining_ms,
            "recent_utterance_count": self.recent_utterance_count,
            "last_utterance_end_timestamp": self.last_utterance_end_timestamp,
            "is_recently_speaking": self.is_recently_speaking(),
        }


@dataclass
class VolumeState:
    """Volume state for threshold adjustment context."""

    current_volume_percent: int = 80
    volume_category: str = "normal"  # "silent", "quiet", "normal", "loud"
    ducking_active: bool = False
    ducking_level: float = 1.0  # 1.0 = no ducking, 0.5 = 50% ducking

    # History
    last_volume_change_timestamp: float = 0.0
    volume_change_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "current_volume_percent": self.current_volume_percent,
            "volume_category": self.volume_category,
            "ducking_active": self.ducking_active,
            "ducking_level": round(self.ducking_level, 2),
            "last_volume_change_timestamp": self.last_volume_change_timestamp,
            "volume_change_count": self.volume_change_count,
        }


@dataclass
class StateTransition:
    """Record of a state transition for latency analysis."""

    timestamp: float
    state_type: str  # "playback", "tts", "volume"
    old_value: Any
    new_value: Any
    latency_ms: float | None = None  # Time from trigger to notification

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "state_type": self.state_type,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "latency_ms": (round(self.latency_ms, 1) if self.latency_ms is not None else None),
        }


@dataclass
class SyncHealthSnapshot:
    """Complete state synchronization health snapshot."""

    timestamp: float
    playback_sync: PlaybackStateSync
    tts_state: TTSState
    volume_state: VolumeState

    # Overall status
    overall_status: SyncStatus = SyncStatus.IN_SYNC
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status.value,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "playback_sync": self.playback_sync.to_dict(),
            "tts_state": self.tts_state.to_dict(),
            "volume_state": self.volume_state.to_dict(),
        }


# --------------------------------------------------------------------------- #
# State Synchronization Monitor                                                 #
# --------------------------------------------------------------------------- #


class StateSyncMonitor:
    """
    Monitors state synchronization between wake word detection components.

    Tracks disagreements between callback-reported and RMS-detected playback
    state, TTS activity for false positive classification, and volume changes
    for threshold context.

    Thread-safe for concurrent access.
    """

    # Configuration
    SYNC_CHECK_INTERVAL_MS = 100.0  # Check sync every 100ms
    STALE_THRESHOLD_MS = 1000.0  # State is stale after 1s without update
    MINOR_DISAGREEMENT_THRESHOLD_MS = 500.0  # Minor if <500ms
    TRANSITION_HISTORY_SIZE = 100

    def __init__(self) -> None:
        """Initialize state sync monitor."""
        self._lock = threading.RLock()

        # State tracking
        self._playback = PlaybackStateSync()
        self._tts = TTSState()
        self._volume = VolumeState()

        # Transition history
        self._transitions: deque[StateTransition] = deque(maxlen=self.TRANSITION_HISTORY_SIZE)

        # Disagreement tracking
        self._disagreement_history: deque[tuple[float, float]] = deque(maxlen=100)  # (start, duration)

        logger.info("StateSyncMonitor initialized")

    def update_playback_callback(
        self,
        is_playing: bool,
        volume: int = 80,
        timestamp: float | None = None,
    ) -> None:
        """
        Update playback state from player callback.

        Args:
            is_playing: Whether player reports playback active
            volume: Current playback volume (0-100)
            timestamp: Update timestamp
        """
        ts = timestamp or time.time()

        with self._lock:
            old_playing = self._playback.callback_playback_active
            old_volume = self._playback.callback_volume

            self._playback.callback_playback_active = is_playing
            self._playback.callback_volume = volume
            self._playback.callback_timestamp = ts

            # Record transition if state changed
            if is_playing != old_playing:
                self._record_transition("playback_callback", old_playing, is_playing, ts)

            if volume != old_volume:
                self._record_transition("volume_callback", old_volume, volume, ts)

            # Check sync
            self._check_sync(ts)

    def update_playback_rms(
        self,
        loopback_rms: float,
        timestamp: float | None = None,
    ) -> None:
        """
        Update RMS-based playback detection.

        Args:
            loopback_rms: Current loopback RMS value
            timestamp: Update timestamp
        """
        ts = timestamp or time.time()

        with self._lock:
            old_active = self._playback.rms_playback_active
            is_active = loopback_rms > self._playback.RMS_THRESHOLD

            self._playback.rms_playback_active = is_active
            self._playback.rms_value = loopback_rms
            self._playback.rms_timestamp = ts

            # Record transition if state changed
            if is_active != old_active:
                self._record_transition("playback_rms", old_active, is_active, ts)
                self._playback.last_state_change_timestamp = ts

            # Check sync
            self._check_sync(ts)

    def _check_sync(self, timestamp: float) -> None:
        """Check synchronization between callback and RMS state."""
        callback_active = self._playback.callback_playback_active
        rms_active = self._playback.rms_playback_active

        previously_in_sync = self._playback.in_sync

        if callback_active == rms_active:
            # States agree
            if not previously_in_sync and self._playback.disagreement_start_time is not None:
                # Was disagreeing, now agree - record disagreement duration
                duration = (timestamp - self._playback.disagreement_start_time) * 1000
                self._disagreement_history.append((self._playback.disagreement_start_time, duration))

            self._playback.in_sync = True
            self._playback.disagreement_start_time = None
            self._playback.disagreement_duration_ms = 0.0
        else:
            # States disagree
            if previously_in_sync:
                # Just started disagreeing
                self._playback.disagreement_start_time = timestamp
                self._playback.total_disagreement_count += 1

                logger.debug(
                    "Playback state disagreement: callback=%s, rms_detected=%s (rms=%.0f)",
                    callback_active,
                    rms_active,
                    self._playback.rms_value,
                )

            self._playback.in_sync = False
            if self._playback.disagreement_start_time is not None:
                self._playback.disagreement_duration_ms = (timestamp - self._playback.disagreement_start_time) * 1000

    def update_tts_state(
        self,
        is_speaking: bool,
        utterance_id: str | None = None,
        estimated_duration_ms: float | None = None,
        timestamp: float | None = None,
    ) -> None:
        """
        Update TTS state.

        Args:
            is_speaking: Whether TTS is currently speaking
            utterance_id: ID of current utterance
            estimated_duration_ms: Estimated duration of utterance
            timestamp: Update timestamp
        """
        ts = timestamp or time.time()

        with self._lock:
            old_speaking = self._tts.is_speaking

            if is_speaking and not old_speaking:
                # Started speaking
                self._tts.speech_start_timestamp = ts
                self._tts.current_utterance_id = utterance_id
                self._tts.estimated_duration_ms = estimated_duration_ms
                self._tts.recent_utterance_count += 1
                self._record_transition("tts", False, True, ts)
            elif not is_speaking and old_speaking:
                # Stopped speaking
                self._tts.last_utterance_end_timestamp = ts
                self._tts.current_utterance_id = None
                self._tts.estimated_duration_ms = None
                self._tts.estimated_remaining_ms = None
                self._record_transition("tts", True, False, ts)
            elif is_speaking and self._tts.speech_start_timestamp is not None:
                # Still speaking - update remaining time
                elapsed = (ts - self._tts.speech_start_timestamp) * 1000
                if estimated_duration_ms is not None:
                    self._tts.estimated_remaining_ms = max(0, estimated_duration_ms - elapsed)

            self._tts.is_speaking = is_speaking

    def update_volume(
        self,
        volume_percent: int,
        ducking_active: bool = False,
        ducking_level: float = 1.0,
        timestamp: float | None = None,
    ) -> None:
        """
        Update volume state.

        Args:
            volume_percent: Current volume (0-100)
            ducking_active: Whether audio ducking is active
            ducking_level: Ducking multiplier (1.0 = no ducking)
            timestamp: Update timestamp
        """
        ts = timestamp or time.time()

        with self._lock:
            old_volume = self._volume.current_volume_percent

            self._volume.current_volume_percent = volume_percent
            self._volume.ducking_active = ducking_active
            self._volume.ducking_level = ducking_level

            # Categorize volume
            if volume_percent == 0:
                self._volume.volume_category = "silent"
            elif volume_percent < 30:
                self._volume.volume_category = "quiet"
            elif volume_percent < 70:
                self._volume.volume_category = "normal"
            else:
                self._volume.volume_category = "loud"

            if volume_percent != old_volume:
                self._volume.last_volume_change_timestamp = ts
                self._volume.volume_change_count += 1
                self._record_transition("volume", old_volume, volume_percent, ts)

    def _record_transition(
        self,
        state_type: str,
        old_value: Any,
        new_value: Any,
        timestamp: float,
    ) -> None:
        """Record a state transition."""
        transition = StateTransition(
            timestamp=timestamp,
            state_type=state_type,
            old_value=old_value,
            new_value=new_value,
        )
        self._transitions.append(transition)

    def get_sync_health(self) -> SyncHealthSnapshot:
        """
        Get current state synchronization health.

        Returns:
            Complete health snapshot with recommendations.
        """
        now = time.time()

        with self._lock:
            snapshot = SyncHealthSnapshot(
                timestamp=now,
                playback_sync=PlaybackStateSync(
                    callback_playback_active=self._playback.callback_playback_active,
                    callback_volume=self._playback.callback_volume,
                    callback_timestamp=self._playback.callback_timestamp,
                    rms_playback_active=self._playback.rms_playback_active,
                    rms_value=self._playback.rms_value,
                    rms_timestamp=self._playback.rms_timestamp,
                    in_sync=self._playback.in_sync,
                    disagreement_duration_ms=self._playback.disagreement_duration_ms,
                    total_disagreement_count=self._playback.total_disagreement_count,
                    notification_latency_ms=self._playback.notification_latency_ms,
                ),
                tts_state=TTSState(
                    is_speaking=self._tts.is_speaking,
                    current_utterance_id=self._tts.current_utterance_id,
                    speech_start_timestamp=self._tts.speech_start_timestamp,
                    estimated_duration_ms=self._tts.estimated_duration_ms,
                    estimated_remaining_ms=self._tts.estimated_remaining_ms,
                    recent_utterance_count=self._tts.recent_utterance_count,
                    last_utterance_end_timestamp=self._tts.last_utterance_end_timestamp,
                ),
                volume_state=VolumeState(
                    current_volume_percent=self._volume.current_volume_percent,
                    volume_category=self._volume.volume_category,
                    ducking_active=self._volume.ducking_active,
                    ducking_level=self._volume.ducking_level,
                    last_volume_change_timestamp=self._volume.last_volume_change_timestamp,
                    volume_change_count=self._volume.volume_change_count,
                ),
            )

            # Assess health
            self._assess_health(snapshot, now)

            return snapshot

    def _assess_health(self, snapshot: SyncHealthSnapshot, now: float) -> None:
        """Assess state synchronization health."""
        issues = []
        recommendations = []
        status = SyncStatus.IN_SYNC

        # Check playback sync
        if not snapshot.playback_sync.in_sync:
            duration = snapshot.playback_sync.disagreement_duration_ms

            if duration > self.MINOR_DISAGREEMENT_THRESHOLD_MS:
                issues.append(f"Major playback state disagreement ({duration:.0f}ms)")
                recommendations.append("Check audio pipeline wiring and state callbacks")
                status = SyncStatus.MAJOR_DISAGREEMENT
            else:
                issues.append(f"Minor playback state disagreement ({duration:.0f}ms)")
                status = SyncStatus.MINOR_DISAGREEMENT

        # Check for stale callback state
        if snapshot.playback_sync.callback_timestamp > 0:
            callback_age_ms = (now - snapshot.playback_sync.callback_timestamp) * 1000
            if callback_age_ms > self.STALE_THRESHOLD_MS:
                issues.append(f"Playback callback state is stale ({callback_age_ms:.0f}ms old)")
                recommendations.append("Ensure player state callbacks are being fired")
                if status == SyncStatus.IN_SYNC:
                    status = SyncStatus.STALE

        # Check for stale RMS state
        if snapshot.playback_sync.rms_timestamp > 0:
            rms_age_ms = (now - snapshot.playback_sync.rms_timestamp) * 1000
            if rms_age_ms > self.STALE_THRESHOLD_MS:
                issues.append(f"RMS playback state is stale ({rms_age_ms:.0f}ms old)")
                recommendations.append("Check AEC reference buffer updates")
                if status == SyncStatus.IN_SYNC:
                    status = SyncStatus.STALE

        # Check disagreement frequency
        if snapshot.playback_sync.total_disagreement_count > 10:
            issues.append(f"High disagreement frequency ({snapshot.playback_sync.total_disagreement_count} total)")
            recommendations.append("Review playback detection thresholds")

        # TTS-related warnings
        if snapshot.tts_state.is_speaking:
            issues.append("TTS currently speaking - wake detections may be echoes")

        snapshot.issues = issues
        snapshot.recommendations = recommendations
        snapshot.overall_status = status

    def get_recent_transitions(self, n: int = 20) -> list[dict[str, Any]]:
        """Get recent state transitions."""
        with self._lock:
            return [t.to_dict() for t in list(self._transitions)[-n:]]

    def get_disagreement_stats(self) -> dict[str, Any]:
        """Get disagreement statistics."""
        with self._lock:
            if not self._disagreement_history:
                return {
                    "total_disagreements": 0,
                    "avg_duration_ms": 0,
                    "max_duration_ms": 0,
                    "recent_5min_count": 0,
                }

            durations = [d for _, d in self._disagreement_history]
            now = time.time()
            recent_cutoff = now - 300  # 5 minutes
            recent_count = sum(1 for t, _ in self._disagreement_history if t > recent_cutoff)

            return {
                "total_disagreements": len(durations),
                "avg_duration_ms": round(sum(durations) / len(durations), 1),
                "max_duration_ms": round(max(durations), 1),
                "min_duration_ms": round(min(durations), 1),
                "recent_5min_count": recent_count,
            }

    def is_tts_likely_cause(self, timestamp: float | None = None) -> bool:
        """
        Check if TTS could be the cause of a detection.

        Args:
            timestamp: Detection timestamp (uses current time if None)

        Returns:
            True if TTS was recently active
        """
        ts = timestamp or time.time()

        with self._lock:
            # Check if currently speaking
            if self._tts.is_speaking:
                return True

            # Check if recently stopped speaking (within 1 second)
            if self._tts.last_utterance_end_timestamp is not None:
                elapsed = (ts - self._tts.last_utterance_end_timestamp) * 1000
                if elapsed < 1000:
                    return True

            return False

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostics summary for API exposure."""
        health = self.get_sync_health()

        return {
            "sync_health": health.to_dict(),
            "disagreement_stats": self.get_disagreement_stats(),
            "recent_transitions": self.get_recent_transitions(10),
        }

    def reset(self) -> None:
        """Reset all state."""
        with self._lock:
            self._playback = PlaybackStateSync()
            self._tts = TTSState()
            self._volume = VolumeState()
            self._transitions.clear()
            self._disagreement_history.clear()

        logger.info("StateSyncMonitor reset")


# --------------------------------------------------------------------------- #
# Singleton Instance                                                           #
# --------------------------------------------------------------------------- #

_monitor: StateSyncMonitor | None = None
_monitor_lock = threading.Lock()


def get_state_sync_monitor() -> StateSyncMonitor:
    """
    Get the global state sync monitor instance.

    Returns:
        Global StateSyncMonitor instance
    """
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = StateSyncMonitor()
        return _monitor


def reset_state_sync_monitor() -> None:
    """Reset the global state sync monitor (for testing)."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.reset()


__all__ = [
    "PlaybackStateSync",
    "StateSyncMonitor",
    "StateTransition",
    "SyncHealthSnapshot",
    "SyncStatus",
    "TTSState",
    "VolumeState",
    "get_state_sync_monitor",
    "reset_state_sync_monitor",
]
