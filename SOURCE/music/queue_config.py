"""
music/queue_config.py
Unified queue configuration and management.

Design Principles:
- Single source of truth for all queue limits
- Configurable through settings
- Clear separation: user queue vs autoplay queue
- Prevents queue overflow and excessive API calls
- Plugin-friendly with validation hooks
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class QueueLimits:
    """
    Queue size limits and configuration.

    Design Philosophy:
    - Users can add unlimited songs to curate their queue (no artificial cap)
    - Autoplay is capped to prevent AI from overwhelming user curation
    - Trigger autoplay when queue drops below min_size_for_autoplay

    Attributes:
        min_size_for_autoplay: Trigger autoplay when queue drops below this
        max_autoplay_size: Maximum songs autoplay can add (prevents API waste)
        buffer_size: Ideal buffer size for smooth playback
    """

    min_size_for_autoplay: int = 7  # Internal heuristic — balances responsiveness vs API calls
    max_autoplay_size: int = 15  # Cap AI suggestions to prevent queue spam
    buffer_size: int = 10  # Target buffer for smooth playback

    def __post_init__(self):
        """Validate configuration."""
        if self.max_autoplay_size < self.min_size_for_autoplay:
            raise ValueError(
                f"max_autoplay_size ({self.max_autoplay_size}) must be >= "
                f"min_size_for_autoplay ({self.min_size_for_autoplay})"
            )

    def can_add_user_song(self, current_size: int) -> bool:
        """
        Check if a user song can be added to queue.

        Users can always add songs - no artificial queue cap.

        Args:
            current_size: Current queue size (unused, kept for API compatibility)

        Returns:
            Always True - users should never be blocked from curating their queue
        """
        return True  # No user queue limit - bad UX to block curation

    def can_add_autoplay_song(self, current_size: int, autoplay_count: int) -> bool:
        """
        Check if autoplay can add a song.

        Autoplay is capped to prevent AI from spamming the queue and wasting API calls.

        Args:
            current_size: Current queue size
            autoplay_count: Number of songs added by autoplay

        Returns:
            True if autoplay can add song
        """
        # Don't exceed autoplay limit (prevents API waste and queue spam)
        if autoplay_count >= self.max_autoplay_size:
            return False

        return True

    def should_trigger_autoplay(self, current_size: int) -> bool:
        """
        Check if autoplay should be triggered.

        Args:
            current_size: Current queue size

        Returns:
            True if autoplay should trigger
        """
        return current_size < self.min_size_for_autoplay

    def calculate_autoplay_need(self, current_size: int) -> int:
        """
        Calculate how many songs autoplay should add.

        Args:
            current_size: Current queue size

        Returns:
            Number of songs to add (0 if none needed)
        """
        if not self.should_trigger_autoplay(current_size):
            return 0

        # Target buffer size
        needed = self.buffer_size - current_size

        # Cap at autoplay limit
        return max(0, min(needed, self.max_autoplay_size))

    def get_info(self) -> dict[str, Any]:
        """Get configuration info as dict."""
        return {
            "min_size_for_autoplay": self.min_size_for_autoplay,
            "max_autoplay_size": self.max_autoplay_size,
            "buffer_size": self.buffer_size,
            "user_queue_unlimited": True,  # Users can curate without limits
        }


class QueueConfig:
    """
    Global queue configuration manager.

    Singleton pattern for consistent configuration across components.
    Can be reconfigured at runtime through settings.
    """

    _instance: QueueConfig | None = None
    _limits: QueueLimits

    def __init__(self, limits: QueueLimits | None = None):
        """
        Initialize queue configuration.

        Args:
            limits: Custom queue limits (uses defaults if None)
        """
        self._limits = limits or QueueLimits()

    @classmethod
    def get_instance(cls) -> QueueConfig:
        """Get or create singleton instance."""
        if cls._instance is None:
            cls._instance = QueueConfig()
        return cls._instance

    @classmethod
    def configure(cls, limits: QueueLimits) -> None:
        """
        Configure queue limits globally.

        Args:
            limits: New queue limits
        """
        instance = cls.get_instance()
        instance._limits = limits

    @classmethod
    def from_settings(cls, settings_manager) -> QueueConfig:
        """
        Create configuration from settings manager.

        Args:
            settings_manager: Settings manager instance

        Returns:
            Configured QueueConfig instance
        """
        try:
            limits = QueueLimits(
                min_size_for_autoplay=settings_manager.get("autoplay_min_queue", 7),
                max_autoplay_size=settings_manager.get("queue_autoplay_max", 15),
                buffer_size=settings_manager.get("queue_buffer_size", 10),
            )
            return QueueConfig(limits)
        except Exception as e:
            # Fallback to defaults if settings unavailable
            from core.logging_config import get_logger

            logger = get_logger(__name__)
            logger.exception("Failed to load queue config from settings, using defaults: %s", e)
            return QueueConfig()

    @property
    def limits(self) -> QueueLimits:
        """Get current limits."""
        return self._limits

    def can_add_user_song(self, current_size: int) -> bool:
        """Check if user can add song."""
        return self._limits.can_add_user_song(current_size)

    def can_add_autoplay_song(self, current_size: int, autoplay_count: int) -> bool:
        """Check if autoplay can add song."""
        return self._limits.can_add_autoplay_song(current_size, autoplay_count)

    def should_trigger_autoplay(self, current_size: int) -> bool:
        """Check if autoplay should trigger."""
        return self._limits.should_trigger_autoplay(current_size)

    def calculate_autoplay_need(self, current_size: int) -> int:
        """Calculate how many songs autoplay should add."""
        return self._limits.calculate_autoplay_need(current_size)

    def get_info(self) -> dict[str, Any]:
        """Get configuration info."""
        return self._limits.get_info()


# Convenience functions for backward compatibility
def get_queue_config() -> QueueConfig:
    """Get global queue configuration instance."""
    return QueueConfig.get_instance()


def create_queue_limits(
    min_size_for_autoplay: int = 7,
    max_autoplay_size: int = 15,
    buffer_size: int = 10,
) -> QueueLimits:
    """
    Factory function for creating queue limits.

    Args:
        min_size_for_autoplay: Trigger autoplay when queue < this (default: 7)
        max_autoplay_size: Maximum autoplay songs (default: 15)
        buffer_size: Ideal buffer size (default: 10)

    Returns:
        Configured QueueLimits
    """
    return QueueLimits(
        min_size_for_autoplay=min_size_for_autoplay,
        max_autoplay_size=max_autoplay_size,
        buffer_size=buffer_size,
    )
