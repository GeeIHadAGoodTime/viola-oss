"""
music.providers.local
---------------------

Local file music provider: SQLite library index, folder scanner, and
MusicProvider ABC implementation with fuzzy search.

- LocalMusicProvider: Full MusicProvider implementation for local files.
- LocalLibraryRepo: Thread-safe SQLite repository for the music library.
- scan_and_index: Scan a folder, extract metadata, and populate the library.
"""

from __future__ import annotations

from .artwork import extract_artwork, extract_embedded_artwork, extract_video_thumbnail
from .db import LocalLibraryRepo, get_local_library_repo
from .provider import LocalMusicProvider
from .scanner import scan_and_index

__all__ = [
    "LocalLibraryRepo",
    "LocalMusicProvider",
    "extract_artwork",
    "extract_embedded_artwork",
    "extract_video_thumbnail",
    "get_local_library_repo",
    "scan_and_index",
]
