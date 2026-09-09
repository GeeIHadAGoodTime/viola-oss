"""
Audio Source Plugin Protocol and Registry.

Defines a lightweight, Protocol-based contract that any audio source
(YouTube, Spotify, local files, internet radio, etc.) can satisfy
without inheriting from a shared base class.

This is intentionally separate from :class:`MusicProvider` (which is
an ABC-based contract for the full provider SDK with auth, playlists,
artwork, etc.).  ``AudioSourceProtocol`` captures only the minimal
surface needed by the playback pipeline:

    search -> get_stream_url -> get_track_info

Providers are NOT refactored to implement this yet.  This module
defines the target contract and a registry for future migration.

Design notes
------------
* Uses :class:`typing.Protocol` (structural subtyping) so providers
  conform implicitly — no base class required.
* ``TrackResult`` / ``TrackInfo`` are plain dataclasses to avoid
  Pydantic overhead in the hot audio path.
* ``SourceRegistry`` is a simple dict-backed singleton that
  mirrors the pattern in ``music.providers.registry`` but is
  decoupled from ``ProviderName`` so third-party plugins can
  register arbitrary source names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Data models                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TrackResult:
    """Lightweight search result returned by an audio source.

    Attributes:
        track_id: Opaque identifier meaningful to the source that produced it.
        title: Display title (song name, episode name, etc.).
        artist: Artist / creator name.  Empty string if not applicable.
        duration_ms: Duration in milliseconds, or ``None`` if unknown.
        artwork_url: Optional URL for cover art / thumbnail.
        extras: Arbitrary key-value metadata the source wants to propagate.
    """

    track_id: str
    title: str
    artist: str = ""
    duration_ms: int | None = None
    artwork_url: str | None = None
    extras: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TrackInfo:
    """Detailed metadata for a single track, returned by ``get_track_info``.

    Attributes:
        track_id: Same opaque identifier used in ``TrackResult``.
        title: Display title.
        artist: Artist / creator name.
        album: Album or collection name.  Empty string if not applicable.
        duration_ms: Duration in milliseconds, or ``None`` if unknown.
        artwork_url: Optional URL for cover art / thumbnail.
        genre: Genre string, if available.
        release_year: Year of release, if available.
        is_explicit: Whether the track has explicit content.
        extras: Arbitrary key-value metadata.
    """

    track_id: str
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int | None = None
    artwork_url: str | None = None
    genre: str | None = None
    release_year: int | None = None
    is_explicit: bool = False
    extras: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Audio Source Protocol                                                         #
# --------------------------------------------------------------------------- #


@runtime_checkable
class AudioSourceProtocol(Protocol):
    """Minimal contract for a pluggable audio source.

    Any class that structurally satisfies this protocol can be registered
    with the :class:`SourceRegistry` and used by the playback pipeline.

    The protocol is intentionally small — it captures only what the
    queue engine needs to discover, resolve, and play tracks.

    Capability strings (returned by ``get_capabilities``) are freeform
    but the following are conventional:

    * ``"search"``   — source supports text search
    * ``"playlist"`` — source can enumerate playlists
    * ``"radio"``    — source can generate continuous radio streams
    * ``"offline"``  — source supports offline / cached playback
    * ``"lyrics"``   — source can provide lyrics
    """

    @property
    def name(self) -> str:
        """Stable, unique identifier for this source (e.g. ``"youtube"``)."""
        ...

    def search(self, query: str, limit: int = 10) -> list[TrackResult]:
        """Search for tracks matching *query*.

        Args:
            query: Free-text search string.
            limit: Maximum number of results to return.

        Returns:
            List of matching :class:`TrackResult` items, possibly empty.
        """
        ...

    def get_stream_url(self, track_id: str) -> str:
        """Resolve *track_id* to a playable stream URL.

        Args:
            track_id: Opaque identifier previously returned in a
                :class:`TrackResult`.

        Returns:
            A URL string suitable for the playback backend.

        Raises:
            LookupError: If the track cannot be resolved.
        """
        ...

    def get_track_info(self, track_id: str) -> TrackInfo:
        """Fetch detailed metadata for *track_id*.

        Args:
            track_id: Opaque identifier previously returned in a
                :class:`TrackResult`.

        Returns:
            A :class:`TrackInfo` with full metadata.

        Raises:
            LookupError: If the track is not found.
        """
        ...

    async def is_available(self) -> bool:
        """Return ``True`` if this source is currently usable.

        Implementations should perform a lightweight connectivity or
        health check (e.g. HEAD request to API, check local DB exists).
        """
        ...

    def get_capabilities(self) -> set[str]:
        """Return the set of capability strings this source supports.

        Returns:
            A set of freeform capability strings.  See class docstring
            for conventional values.
        """
        ...


# --------------------------------------------------------------------------- #
# Source Registry                                                              #
# --------------------------------------------------------------------------- #


class SourceRegistry:
    """Registry for audio source plugins.

    Thread-safe, singleton-style registry where audio sources register
    by name.  The playback pipeline can then discover and iterate over
    registered sources.

    Usage::

        registry = SourceRegistry()
        registry.register(my_youtube_source)
        registry.register(my_local_source)

        for source in registry.iter_sources():
            if "search" in source.get_capabilities():
                results = source.search("lofi beats")
    """

    def __init__(self) -> None:
        self._sources: dict[str, AudioSourceProtocol] = {}

    def register(
        self,
        source: AudioSourceProtocol,
        *,
        override: bool = False,
    ) -> None:
        """Register an audio source instance.

        Args:
            source: An object satisfying :class:`AudioSourceProtocol`.
            override: If ``True``, replace an existing source with the
                same name.  Otherwise raise ``ValueError``.

        Raises:
            ValueError: If a source with the same name is already
                registered and *override* is ``False``.
            TypeError: If *source* does not satisfy the protocol.
        """
        if not isinstance(source, AudioSourceProtocol):
            raise TypeError("source must satisfy AudioSourceProtocol, got %s" % type(source).__name__)

        name = source.name
        if not override and name in self._sources:
            raise ValueError("Source '%s' already registered. Pass override=True to replace." % name)

        self._sources[name] = source
        logger.info("Audio source registered: %s", name)

    def unregister(self, name: str) -> bool:
        """Remove a source by name.

        Args:
            name: The source name to remove.

        Returns:
            ``True`` if the source was removed, ``False`` if not found.
        """
        removed = self._sources.pop(name, None)
        if removed is not None:
            logger.info("Audio source unregistered: %s", name)
            return True
        return False

    def get(self, name: str) -> AudioSourceProtocol | None:
        """Look up a source by name.

        Args:
            name: The source identifier.

        Returns:
            The registered source, or ``None`` if not found.
        """
        return self._sources.get(name)

    def iter_sources(self) -> list[AudioSourceProtocol]:
        """Return all registered sources as a list.

        Returns:
            A snapshot list of currently registered sources.
        """
        return list(self._sources.values())

    def iter_names(self) -> list[str]:
        """Return all registered source names.

        Returns:
            A snapshot list of registered source name strings.
        """
        return list(self._sources.keys())

    def has(self, name: str) -> bool:
        """Check if a source with *name* is registered."""
        return name in self._sources

    def clear(self) -> None:
        """Remove all registered sources."""
        self._sources.clear()
        logger.debug("Source registry cleared")


# Module-level singleton for convenience.
_default_registry: SourceRegistry | None = None


def get_source_registry() -> SourceRegistry:
    """Return the module-level default :class:`SourceRegistry`.

    Creates the singleton on first call.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = SourceRegistry()
    return _default_registry


__all__ = [
    "AudioSourceProtocol",
    "SourceRegistry",
    "TrackInfo",
    "TrackResult",
    "get_source_registry",
]
