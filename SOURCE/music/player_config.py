"""
music/player_config.py
Unified configuration constants for MusicPlayer.

Design Principles:
- Single source of truth for all constants
- Easy to tune for different hardware (Pi vs Desktop)
- Plugin-friendly with override support
- Clear documentation for each setting
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.constants import TIMEOUT_DEFAULT, TIMEOUT_SHUTDOWN


@dataclass
class PlaybackConfig:
    """
    Playback-related configuration constants.
    Tuned for different hardware profiles.
    """

    # Position caching
    POSITION_CACHE_TTL_SEC: float = 0.1  # 100ms cache (10 updates/sec, imperceptible)

    # Track filtering
    MIN_TRACK_LENGTH_SEC: int = 90  # Skip videos shorter than 90s (avoid Shorts)
    MIN_PLAYBACK_DURATION_SEC: int = 10  # Track must play 10s+ to count as "played"

    # Retry logic
    MAX_CONSECUTIVE_FAILURES: int = 3  # Pause playback after N failures
    FAILURE_BACKOFF_SEC: int = 5  # Wait N seconds before retrying after max failures

    @classmethod
    def for_raspberry_pi(cls):
        """Optimized settings for Raspberry Pi (lower CPU usage)."""
        return cls(
            POSITION_CACHE_TTL_SEC=0.2,  # Longer cache on Pi (5 updates/sec)
            MAX_CONSECUTIVE_FAILURES=2,  # Fewer retries on Pi
        )

    @classmethod
    def for_desktop(cls):
        """Optimized settings for Desktop (higher performance)."""
        return cls(
            POSITION_CACHE_TTL_SEC=0.05,  # Shorter cache on desktop (20 updates/sec)
        )


@dataclass
class ResolutionConfig:
    """
    YouTube resolution configuration constants.
    """

    # Timeouts
    SOCKET_TIMEOUT_SEC: int = 10
    SUBPROCESS_TIMEOUT_SEC: int = 20

    # Cache settings
    CACHE_MAX_SIZE: int = 50
    CACHE_TTL_SEC: int = 18000  # 5 hours (YouTube URLs expire ~6h)

    # Retry settings
    MAX_RETRIES: int = 1  # Only one retry to prevent UI freezing


@dataclass
class MetricsConfig:
    """
    Metrics collection configuration.
    """

    # History tracking
    HISTORY_MAX_SIZE: int = 10

    # Duration tracking
    DURATION_HISTORY_MAX: int = 100  # Keep last 100 play durations

    # Size limits
    MAX_ERROR_LOG_SIZE: int = 100


@dataclass
class BackendStreamingConfig:
    """
    Configuration for streaming backends (ffmpeg-based pipeline).
    """

    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    read_chunk_size: int = 64 * 1024  # bytes
    startup_timeout_sec: float = TIMEOUT_DEFAULT
    progress_interval_sec: float = 0.25
    prebuffer_seconds: float = 3.0
    shutdown_timeout_sec: float = TIMEOUT_SHUTDOWN
    simulate_track_duration_sec: float = 45.0


class PlayerConfigManager:
    """
    Centralized configuration manager for MusicPlayer.
    Provides hardware-specific presets and plugin override support.
    """

    def __init__(
        self,
        hardware_profile: str = "auto",
        playback_config: PlaybackConfig | None = None,
        resolution_config: ResolutionConfig | None = None,
        backend_config: BackendStreamingConfig | None = None,
    ):
        """
        Initialize configuration manager.

        Args:
            hardware_profile: "auto", "raspberry_pi", or "desktop"
            playback_config: Custom playback config (uses hardware preset if None)
            resolution_config: Custom resolution config (uses defaults if None)
        """
        self.hardware_profile = hardware_profile

        # Determine playback config
        if playback_config is not None:
            self.playback = playback_config
        elif hardware_profile == "raspberry_pi":
            self.playback = PlaybackConfig.for_raspberry_pi()
        elif hardware_profile == "desktop":
            self.playback = PlaybackConfig.for_desktop()
        else:  # auto or unknown
            self.playback = PlaybackConfig()

        # Resolution config
        self.resolution = resolution_config or ResolutionConfig()

        # Metrics config
        self.metrics = MetricsConfig()

        # Backend config
        if backend_config is not None:
            self.backend = backend_config
        else:
            self.backend = BackendStreamingConfig()

    def get_info(self) -> dict[str, Any]:
        """Get all configuration as dictionary."""
        return {
            "hardware_profile": self.hardware_profile,
            "playback": {
                "position_cache_ttl_sec": self.playback.POSITION_CACHE_TTL_SEC,
                "min_track_length_sec": self.playback.MIN_TRACK_LENGTH_SEC,
                "min_playback_duration_sec": self.playback.MIN_PLAYBACK_DURATION_SEC,
                "max_consecutive_failures": self.playback.MAX_CONSECUTIVE_FAILURES,
                "failure_backoff_sec": self.playback.FAILURE_BACKOFF_SEC,
            },
            "resolution": {
                "socket_timeout_sec": self.resolution.SOCKET_TIMEOUT_SEC,
                "subprocess_timeout_sec": self.resolution.SUBPROCESS_TIMEOUT_SEC,
                "cache_max_size": self.resolution.CACHE_MAX_SIZE,
                "cache_ttl_sec": self.resolution.CACHE_TTL_SEC,
                "max_retries": self.resolution.MAX_RETRIES,
            },
            "metrics": {
                "history_max_size": self.metrics.HISTORY_MAX_SIZE,
                "duration_history_max": self.metrics.DURATION_HISTORY_MAX,
                "max_error_log_size": self.metrics.MAX_ERROR_LOG_SIZE,
            },
            "backend": {
                "ffmpeg_path": self.backend.ffmpeg_path,
                "ffprobe_path": self.backend.ffprobe_path,
                "read_chunk_size": self.backend.read_chunk_size,
                "startup_timeout_sec": self.backend.startup_timeout_sec,
                "progress_interval_sec": self.backend.progress_interval_sec,
                "prebuffer_seconds": self.backend.prebuffer_seconds,
                "shutdown_timeout_sec": self.backend.shutdown_timeout_sec,
            },
        }


# Global defaults (can be overridden by plugins)
_default_config = PlayerConfigManager()


def get_player_config() -> PlayerConfigManager:
    """Get global player configuration instance."""
    return _default_config


def set_player_config(config: PlayerConfigManager):
    """Set global player configuration (for plugins or testing)."""
    global _default_config
    _default_config = config
