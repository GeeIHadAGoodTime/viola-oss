"""Typed runtime configuration surface for MusicPlayer."""

from __future__ import annotations

from dataclasses import dataclass

from music.player_config import PlayerConfigManager, get_player_config
from music.queue_config import QueueConfig, get_queue_config


@dataclass(frozen=True)
class RuntimeToggles:
    """Feature toggles that depend on runtime conditions (env, test mode)."""

    autoplay_enabled: bool
    background_resolver_enabled: bool
    integrity_monitor_enabled: bool


@dataclass(frozen=True)
class PlayerRuntimeConfig:
    """Aggregate container for runtime config and toggles."""

    player_config: PlayerConfigManager
    queue_config: QueueConfig
    toggles: RuntimeToggles


def _resolve_toggle(
    explicit: bool | None,
    *,
    default_when_enabled: bool,
) -> bool:
    """
    Resolve toggle boolean, preferring explicit values.

    Args:
        explicit: Optional explicit value passed by caller.
        default_when_enabled: Default when not running under test mode.
    """
    if explicit is not None:
        return explicit
    return default_when_enabled


def build_runtime_config(
    *,
    test_mode: bool,
    autoplay_enabled: bool | None = None,
    background_resolver_enabled: bool | None = None,
    integrity_monitor_enabled: bool | None = None,
) -> PlayerRuntimeConfig:
    """
    Build a PlayerRuntimeConfig object ready for dependency injection.

    Args:
        test_mode: Whether MusicPlayer is running under tests/CI.
        autoplay_enabled: Optional override for autoplay toggle.
        background_resolver_enabled: Optional override for background resolver.
        integrity_monitor_enabled: Optional override for integrity monitor.
    """

    player_cfg = get_player_config()
    queue_cfg = get_queue_config()

    toggles = RuntimeToggles(
        autoplay_enabled=_resolve_toggle(
            autoplay_enabled,
            default_when_enabled=not test_mode,
        ),
        background_resolver_enabled=_resolve_toggle(
            background_resolver_enabled,
            default_when_enabled=not test_mode,
        ),
        integrity_monitor_enabled=_resolve_toggle(
            integrity_monitor_enabled,
            default_when_enabled=not test_mode,
        ),
    )

    return PlayerRuntimeConfig(
        player_config=player_cfg,
        queue_config=queue_cfg,
        toggles=toggles,
    )
