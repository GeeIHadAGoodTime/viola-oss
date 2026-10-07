"""Compare explicit local-file addresses without consulting the filesystem.

A selected library row reaches playback as a full path, not a title query.
Only reported file addresses establish identity; similar titles or basenames
must not turn a different file into a match.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit

_WINDOWS_URI_DRIVE = re.compile(r"^/[a-zA-Z]:[\\/]")


def local_file_identity(value: object) -> tuple[str, str] | None:
    """Return a lexical absolute-file identity, or None for opaque identifiers.

    Windows addresses are case-insensitive and accept either separator; POSIX
    addresses retain case. Parent segments are deliberately not resolved:
    resolving them lexically can hide a different file through a symlink.
    Percent escapes are decoded only for file URIs, never plain filenames.
    """
    if not isinstance(value, str):
        return None
    path = value.strip()
    if path.lower().startswith("file:"):
        try:
            uri = urlsplit(path)
            if uri.query or uri.fragment or "@" in uri.netloc or ":" in uri.netloc:
                return None
            path = unquote(uri.path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        if uri.netloc and uri.netloc.lower() != "localhost":
            path = "//" + uri.netloc + path
        elif _WINDOWS_URI_DRIVE.match(path):
            path = path[1:]
    if not path or "\x00" in path:
        return None

    windows = PureWindowsPath(path)
    if windows.is_absolute():
        return "windows", str(windows).lower()
    if path.startswith("/"):
        return "posix", str(PurePosixPath(path))
    return None


def classify_local_file_match(query: str, now_playing: dict) -> str | None:
    """Classify explicit file requests; None leaves ordinary title matching alone."""
    requested = local_file_identity(query)
    if requested is None:
        return None

    for key in ("url", "file_path", "track_uri"):
        reported = now_playing.get(key)
        actual = local_file_identity(reported)
        if actual is not None:
            return "exact" if actual == requested else "fallback_unrelated"
        if isinstance(reported, str):
            try:
                scheme = urlsplit(reported).scheme.lower()
            except ValueError:
                continue
            if scheme in {"http", "https", "spotify", "youtube"}:
                return "fallback_unrelated"

    # A title, even a query echo, cannot prove which local file was loaded.
    return "unknown"
