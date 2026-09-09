"""
music/caching/base.py

Abstract base for resolution caching.
Defines the contract that all cache implementations must follow.

This module implements the core.cache.Cache protocol for music-specific
caching. The ResolutionCache ABC is a specialized Cache[str, CacheEntry].

See Also:
    core.cache - Unified caching protocols for NOVVIOLA
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


@dataclass
class CacheEntry:
    """
    A cached resolution result with metadata.

    Attributes:
        url: Stream URL
        title: Song title
        video_id: YouTube video ID (optional)
        artist: Artist name (optional)
        timestamp: When this was resolved (for expiration)
        provider: Provider identifier (spotify, ytmusic, etc)
        lease_id: Lease token associated with provider auth context
        metadata_hash: Hash for compliance-friendly auditing without raw PII
    """

    url: str
    title: str
    video_id: str | None
    artist: str | None
    timestamp: float
    provider: str | None = None
    lease_id: str | None = None
    metadata_hash: str | None = None
    raw_payload: dict[str, Any] | None = None

    def is_expired(self, max_age_seconds: float = 6 * 3600) -> bool:
        """Check if this entry has expired (default: 6 hours for YouTube URLs)"""
        age = time.time() - self.timestamp
        return age > max_age_seconds

    def is_stale(self, stale_threshold: float = 4 * 3600) -> bool:
        """Check if this entry is getting old and should be refreshed soon"""
        age = time.time() - self.timestamp
        return age > stale_threshold

    def age_hours(self) -> float:
        """Get age of this entry in hours"""
        return (time.time() - self.timestamp) / 3600

    def to_tuple(self) -> tuple[str, str, str | None, str | None]:
        """Convert to legacy tuple format (url, title, video_id, artist)"""
        return (self.url, self.title, self.video_id, self.artist)

    def to_serializable(self) -> dict[str, Any]:
        """Convert to serializable dict including compliance metadata."""
        payload = {
            "url": self.url,
            "title": self.title,
            "video_id": self.video_id,
            "artist": self.artist,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "lease_id": self.lease_id,
            "metadata_hash": self.metadata_hash,
        }
        if self.raw_payload:
            payload["raw_payload"] = dict(self.raw_payload)
        return payload

    @classmethod
    def from_tuple(
        cls,
        data: tuple[str, str, str | None, str | None],
        timestamp: float | None = None,
        provider: str | None = None,
        lease_id: str | None = None,
        metadata_hash: str | None = None,
    ) -> CacheEntry:
        """Create from legacy tuple format"""
        return cls(
            url=data[0],
            title=data[1],
            video_id=data[2] if len(data) > 2 else None,
            artist=data[3] if len(data) > 3 else None,
            timestamp=timestamp or time.time(),
            provider=provider,
            lease_id=lease_id,
            metadata_hash=metadata_hash,
        )

    @classmethod
    def from_serializable(cls, payload: dict) -> CacheEntry:
        """Create from serialized dict (used by encrypted disk cache)."""
        return cls(
            url=payload["url"],
            title=payload["title"],
            video_id=payload.get("video_id"),
            artist=payload.get("artist"),
            timestamp=payload.get("timestamp", time.time()),
            provider=payload.get("provider"),
            lease_id=payload.get("lease_id"),
            metadata_hash=payload.get("metadata_hash"),
            raw_payload=payload.get("raw_payload"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a dictionary view of the entry (legacy compatibility)."""
        return self.to_serializable()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Dict-like accessor for backward compatibility.

        Tests and older helpers historically treated cache entries as plain
        dictionaries, so we expose a minimal mapping-style surface.
        """
        if self.raw_payload and key in self.raw_payload:
            return self.raw_payload.get(key, default)
        return self.to_serializable().get(key, default)

    def __getitem__(self, key: str) -> Any:
        if self.raw_payload and key in self.raw_payload:
            return self.raw_payload[key]
        return self.to_serializable()[key]


class ResolutionCache(ABC):
    """
    Abstract base class for resolution caching strategies.

    This class implements the core.cache.Cache[str, CacheEntry] protocol,
    providing a consistent interface for all music resolution caches.

    All cache implementations must support:
    - get(key) -> Optional[CacheEntry]
    - put(key, entry)
    - clear()
    - size()

    Note:
        This ABC conforms to the Cache protocol from core.cache,
        allowing music caches to be used polymorphically with any
        code expecting a generic Cache interface.
    """

    @abstractmethod
    def get(self, key: str) -> CacheEntry | None:
        """
        Retrieve a cached entry.

        Args:
            key: Cache key (typically "source:query:format")

        Returns:
            CacheEntry if found and not expired, None otherwise
        """
        pass

    @abstractmethod
    def put(self, key: str, entry: CacheEntry) -> None:
        """
        Store an entry in the cache.

        Args:
            key: Cache key
            entry: Entry to store
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all entries from the cache"""
        pass

    @abstractmethod
    def size(self) -> int:
        """Get the number of entries in the cache"""
        pass

    def invalidate(self, key: str) -> bool:
        """
        Invalidate a specific cache entry.

        Args:
            key: Cache key to invalidate

        Returns:
            True if entry was found and removed, False otherwise
        """
        # Default implementation - subclasses can override
        return False

    def stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dict with cache metrics (hits, misses, size, etc.)
        """
        return {"size": self.size(), "type": self.__class__.__name__}

    def flush(self) -> None:
        """
        Flush the cache to persistent storage (if applicable).

        Default implementation is a no-op. Subclasses with disk or
        network-backed storage should override.
        """
        pass
