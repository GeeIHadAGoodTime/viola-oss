"""
Minimal Integration Hooks for Sync Engine

Provides helper functions to integrate the sync engine with audio playback systems.
This module provides minimal hooks without modifying music providers.
"""

from __future__ import annotations

from collections.abc import Callable

from core.events.bus import EventBus, LocalEventBus
from core.logging_config import get_logger

from .sync_engine import SyncEngine


def create_sync_engine(
    hub_clock_url: str | None = None,
    event_bus: EventBus | None = None,
    is_hub: bool = False,
) -> SyncEngine:
    """
    Create and configure sync engine instance.

    Args:
        hub_clock_url: URL for Hub clock endpoint (None if Hub mode)
        event_bus: Event bus for emitting events
        is_hub: True if this node is the Hub (authoritative clock source)

    Returns:
        Configured SyncEngine instance
    """
    logger = get_logger("audio_core.sync_integration")

    # If Hub mode, use local clock as authority (no URL needed)
    if is_hub:
        logger.info("Creating sync engine in Hub mode (local clock authority)")
        hub_clock_url = None
    else:
        logger.info("Creating sync engine in Spoke mode (Hub URL: %s)", hub_clock_url)

    engine = SyncEngine(
        hub_clock_url=hub_clock_url,
        event_bus=event_bus or LocalEventBus(),
        logger=logger,
    )

    return engine


def attach_sync_engine_to_playback(
    sync_engine: SyncEngine,
    pause_callback: Callable[[], None] | None = None,
    rebuffer_callback: Callable[[], None] | None = None,
    align_callback: Callable[[float], None] | None = None,
    resume_callback: Callable[[], None] | None = None,
) -> None:
    """
    Attach sync engine to playback system with correction callbacks.

    Args:
        sync_engine: SyncEngine instance
        pause_callback: Callback to pause playback (for drift correction)
        rebuffer_callback: Callback to rebuffer audio (for drift correction)
        align_callback: Callback to align playback position (takes drift_ms)
        resume_callback: Callback to resume playback (for drift correction)
    """
    sync_engine.set_correction_callbacks(
        pause=pause_callback,
        rebuffer=rebuffer_callback,
        align=align_callback,
        resume=resume_callback,
    )


def start_sync_engine(sync_engine: SyncEngine) -> None:
    """
    Start sync engine (starts background sync pulse task).

    Args:
        sync_engine: SyncEngine instance
    """
    sync_engine.start()


def stop_sync_engine(sync_engine: SyncEngine) -> None:
    """
    Stop sync engine (stops background sync pulse task).

    Args:
        sync_engine: SyncEngine instance
    """
    sync_engine.stop()


__all__ = [
    "attach_sync_engine_to_playback",
    "create_sync_engine",
    "start_sync_engine",
    "stop_sync_engine",
]
