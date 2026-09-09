"""
music/performance/factory.py

Factory functions for creating performance-enhanced music players.
Provides simple one-line initialization with sensible defaults.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from music.freshness import FreshnessPolicy

from .integration import PerformanceConfig, PerformanceEnhancedPlayer

logger = get_logger(__name__)


def create_performance_config(profile: str = "balanced", **overrides) -> PerformanceConfig:
    """
    Create performance configuration from a preset profile.

    Profiles:
        - "disabled": All performance features off
        - "minimal": Memory cache only (fast, no persistence)
        - "balanced": Hybrid cache + parallel (default)
        - "aggressive": All features enabled with aggressive settings

    Args:
        profile: Profile name
        **overrides: Override specific settings

    Returns:
        PerformanceConfig instance
    """
    if profile == "disabled":
        config = PerformanceConfig(
            enabled=False,
            enable_cache=False,
            enable_parallel=False,
            enable_freshness=False,
        )

    elif profile == "minimal":
        config = PerformanceConfig(
            enabled=True,
            enable_cache=True,
            cache_type="memory",
            memory_cache_size=50,
            enable_parallel=False,
            enable_freshness=False,
        )

    elif profile == "balanced":
        config = PerformanceConfig(
            enabled=True,
            enable_cache=True,
            cache_type="hybrid",
            memory_cache_size=50,
            disk_cache_size=200,
            enable_parallel=True,
            max_concurrent=5,
            enable_freshness=True,
            freshness_policy=FreshnessPolicy.BALANCED,
            enable_background_monitor=False,
        )

    elif profile == "aggressive":
        config = PerformanceConfig(
            enabled=True,
            enable_cache=True,
            cache_type="hybrid",
            memory_cache_size=100,
            disk_cache_size=500,
            enable_parallel=True,
            max_concurrent=8,
            enable_freshness=True,
            freshness_policy=FreshnessPolicy.STRICT,
            enable_background_monitor=True,
        )

    else:
        raise ValueError(f"Unknown profile: {profile}")

    # Apply overrides
    for key, value in overrides.items():
        if hasattr(config, key):
            setattr(config, key, value)
        else:
            logger.warning("Unknown config key: %s", key)

    return config


def create_performance_config_from_env() -> PerformanceConfig:
    """
    Create performance configuration from settings.

    Settings:
        perf_profile: Profile name (disabled, minimal, balanced, aggressive)
        perf_cache_type: Cache type (memory, persistent, hybrid)
        perf_max_concurrent: Max concurrent resolutions
        perf_freshness_policy: Freshness policy (strict, balanced, relaxed)

    Returns:
        PerformanceConfig instance
    """
    from config.settings import settings

    profile = settings.perf_profile
    config = create_performance_config(profile)

    # Override from settings
    if settings.perf_cache_type:
        config.cache_type = settings.perf_cache_type

    if settings.perf_max_concurrent:
        config.max_concurrent = settings.perf_max_concurrent

    if settings.perf_freshness_policy:
        config.freshness_policy = FreshnessPolicy(settings.perf_freshness_policy)

    return config


def wrap_player_with_performance(
    player: Any, config: PerformanceConfig | None = None, auto_config: bool = True
) -> PerformanceEnhancedPlayer:
    """
    Wrap an existing MusicPlayer with performance enhancements.

    Args:
        player: MusicPlayer instance to wrap
        config: Optional configuration (uses defaults if None)
        auto_config: If True, try to load config from environment

    Returns:
        PerformanceEnhancedPlayer wrapping the base player

    Example:
        # Simple usage with defaults
        player = MusicPlayer()
        enhanced = wrap_player_with_performance(player)

        # With custom config
        config = create_performance_config("aggressive")
        enhanced = wrap_player_with_performance(player, config)

        # From environment
        enhanced = wrap_player_with_performance(player, auto_config=True)
    """
    if config is None:
        if auto_config:
            try:
                from config.settings import settings

                config = create_performance_config_from_env()
                logger.info(
                    "📊 Performance config loaded from settings: %s",
                    settings.perf_profile,
                )
            except Exception as e:
                logger.warning("Failed to load settings config, using balanced: %s", e)
                config = create_performance_config("balanced")
        else:
            config = create_performance_config("balanced")

    return PerformanceEnhancedPlayer(player, config)


# Convenience shorthand
def enhance(player: Any, profile: str = "balanced", **overrides) -> PerformanceEnhancedPlayer:
    """
    One-liner to enhance a player with a specific profile.

    Args:
        player: MusicPlayer to enhance
        profile: Performance profile
        **overrides: Override specific settings

    Returns:
        Enhanced player

    Example:
        enhanced = enhance(player, "aggressive", max_concurrent=10)
    """
    config = create_performance_config(profile, **overrides)
    return PerformanceEnhancedPlayer(player, config)
