"""
music/caching/__init__.py

Pluggable caching abstractions for music resolution.
Supports multiple implementations (memory, disk, hybrid) with clean interfaces.
"""

from __future__ import annotations

from .base import CacheEntry, ResolutionCache
from .hybrid import HybridCache
from .memory import MemoryCache
from .persistent import PersistentCache

__all__ = [
    "CacheEntry",
    "HybridCache",
    "MemoryCache",
    "PersistentCache",
    "ResolutionCache",
]
