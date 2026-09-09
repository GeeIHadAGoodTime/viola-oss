"""
playback/engines/base.py

Common abstractions shared by all provider playback engines.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.logging_config import get_logger
from models.player import QueueItem

ArtworkCallback = Callable[[QueueItem, str | None], None]


@dataclass(slots=True)
class PlaybackCapabilities:
    """
    Describes provider-specific playback capabilities.

    Attributes:
        gapless: Whether the provider/player supports gapless hand-off.
        hot_buffer: Whether pre-buffering successive tracks is supported.
        artwork_sync: Whether artwork updates can be pulled directly from
            provider events (otherwise we fall back to cached thumbnails).
        max_bitrate_kbps: Maximum advertised bitrate (None when unknown).
        supports_offline: Provider allows offline downloads/playback.
        supports_lyrics: Provider exposes time-synchronised lyrics.
    """

    gapless: bool = False
    hot_buffer: bool = False
    artwork_sync: bool = True
    max_bitrate_kbps: int | None = None
    supports_offline: bool = False
    supports_lyrics: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "gapless": self.gapless,
            "hot_buffer": self.hot_buffer,
            "artwork_sync": self.artwork_sync,
            "max_bitrate_kbps": self.max_bitrate_kbps,
            "supports_offline": self.supports_offline,
            "supports_lyrics": self.supports_lyrics,
        }


@dataclass(slots=True)
class QueueContext:
    """
    Snapshot of the queue state passed to providers for prefetching decisions.
    """

    upcoming_items: list[QueueItem] = field(default_factory=list)
    feature_flags: dict[str, Any] = field(default_factory=dict)


class EnginePlaybackError(RuntimeError):
    """Raised when provider playback fails irrecoverably.

    Note: This is a low-level engine exception. For the canonical playback error,
    use core.exceptions.PlaybackError instead. This exception is used internally
    by playback engine implementations.
    """


# Backward compatibility alias - deprecated, use EnginePlaybackError
PlaybackError = EnginePlaybackError


class PlaybackHandle:
    """
    Runtime handle that tracks the lifecycle of a provider playback session.

    The handle is intentionally generic to avoid leaking provider-specific
    constructs back into the queue engine.
    """

    def __init__(
        self,
        item: QueueItem,
        provider_id: str,
        *,
        on_finished: Callable[[PlaybackHandle], None] | None = None,
    ) -> None:
        self.item = item
        self.provider_id = provider_id
        self._on_finished = on_finished
        self._started = threading.Event()
        self._completed = threading.Event()
        self._paused = threading.Event()
        self._stop_requested = threading.Event()
        self._error: Exception | None = None
        self._artwork_url: str | None = getattr(item, "artwork_url", None)

    def mark_started(self) -> None:
        self._started.set()

    def mark_artwork(self, url: str | None) -> None:
        if url:
            self._artwork_url = url
            self.item.with_artwork(url)

    def mark_finished(self, error: Exception | None = None) -> None:
        self._error = error
        self._completed.set()
        if self._on_finished:
            try:
                self._on_finished(self)
            except Exception as exc:
                # EXEMPT(hollow-check): Callback error at playback boundary
                # We log the error but don't re-raise to protect the playback thread
                import logging

                get_logger("viola.playback.handle").warning(
                    "Playback finished callback failed for item %s: %r",
                    self.item.id if hasattr(self.item, "id") else "unknown",
                    exc,
                )

    def wait_until_started(self, timeout: float | None = 10.0) -> bool:
        return self._started.wait(timeout)

    def wait_until_finished(self, timeout: float | None = None) -> bool:
        return self._completed.wait(timeout)

    def is_active(self) -> bool:
        return self._started.is_set() and not self._completed.is_set()

    def error(self) -> Exception | None:
        return self._error

    def artwork_url(self) -> str | None:
        return self._artwork_url

    def mark_paused(self) -> None:
        self._paused.set()

    def mark_resumed(self) -> None:
        self._paused.clear()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def set_on_finished(self, callback: Callable[[PlaybackHandle], None] | None) -> None:
        self._on_finished = callback


class ProviderPlaybackEngine:
    """
    Base class for provider playback engines.
    """

    provider_id: str
    display_name: str
    capabilities: PlaybackCapabilities

    def __init__(
        self,
        provider_id: str,
        display_name: str,
        capabilities: PlaybackCapabilities | None = None,
        *,
        delegates_to_legacy_backend: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.display_name = display_name
        self.capabilities = capabilities or PlaybackCapabilities()
        self._available = True
        self._availability_error: str | None = None
        self._delegates_to_legacy_backend = delegates_to_legacy_backend

    # ------------------------------------------------------------------#
    # Availability lifecycle
    # ------------------------------------------------------------------#
    def is_available(self) -> bool:
        return self._available

    def availability_error(self) -> str | None:
        return self._availability_error

    def delegates_to_legacy_backend(self) -> bool:
        """
        Indicates whether the engine relies on the legacy VLC/simple backend for
        actual playback.
        """
        return self._delegates_to_legacy_backend

    def _mark_unavailable(self, message: str) -> None:
        self._available = False
        self._availability_error = message

    # ------------------------------------------------------------------#
    # Resolution helpers
    # ------------------------------------------------------------------#
    def resolve_track(self, query: str) -> QueueItem:
        """
        Resolve provider-specific track identifiers, using the official SDK.

        Subclasses MUST override this method for provider-aware resolution.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------#
    # Playback primitives
    # ------------------------------------------------------------------#
    def prefetch(self, upcoming: Iterable[QueueItem], *, queue_context: QueueContext) -> None:
        """
        Preload future tracks when the provider supports hot buffering.
        """
        if not upcoming:
            return
        # Default implementation is a no-op. Subclasses override as needed.

    def play(
        self,
        item: QueueItem,
        *,
        queue_context: QueueContext,
        on_artwork: ArtworkCallback | None = None,
    ) -> PlaybackHandle:
        """
        Start playback for ``item``.

        Subclasses MUST implement this using the official SDK/player. The base
        class handles handle creation and gapless negotiation.
        """
        raise NotImplementedError

    def pause(self, handle: PlaybackHandle) -> None:
        raise NotImplementedError

    def resume(self, handle: PlaybackHandle) -> None:
        raise NotImplementedError

    def stop(self, handle: PlaybackHandle) -> None:
        raise NotImplementedError

    def set_volume(self, handle: PlaybackHandle, level: int) -> None:
        """
        Set the playback volume for the active session.
        """
        raise NotImplementedError

    def current_position(self, handle: PlaybackHandle) -> float:
        """
        Return the current playback position in seconds.
        """
        return 0.0

    def duration(self, handle: PlaybackHandle) -> float:
        """
        Return the total duration in seconds if available.
        """
        return 0.0
