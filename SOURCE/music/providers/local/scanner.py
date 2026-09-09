"""
Local music folder scanner with metadata extraction.

Recursively scans a directory for audio files, extracts metadata using
tinytag, and returns track dicts ready for LocalLibraryRepo.upsert_track().

Designed to run in a background thread — no Qt imports, no UI calls.

Usage:
    >>> from music.providers.local.scanner import scan_and_index
    >>> from music.providers.local.db import get_local_library_repo
    >>> repo = get_local_library_repo()
    >>> repo.initialize()
    >>> stats = scan_and_index("/home/user/Music", repo)
    >>> print(stats)
    {"scanned": 142, "upserted": 140, "removed": 3, "errors": 2}
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)

# Serialize scan_and_index calls to prevent concurrent scans from racing
# on the remove_tracks_not_in step.
_scan_lock = threading.Lock()

AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".m4a", ".aac", ".wav", ".wma", ".flac", ".ogg", ".opus"})

VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"})

SUPPORTED_EXTENSIONS: frozenset[str] = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def _canonical_path(path: str | Path) -> Path:
    try:
        return Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(path).absolute()


def _is_under_root(path: str | Path, root: Path) -> bool:
    try:
        _canonical_path(path).relative_to(root)
        return True
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink()
    except (OSError, RuntimeError):
        return False


def classify_media_type(ext: str) -> str:
    """Classify a file extension as 'audio' or 'video'."""
    if ext.lower() in VIDEO_EXTENSIONS:
        return "video"
    return "audio"


def _title_from_filename(file_name: str) -> str:
    """Derive a human-readable title from a filename.

    Strips the extension, replaces underscores and hyphens with spaces,
    collapses whitespace, and applies title-case.
    """
    stem = Path(file_name).stem
    cleaned = re.sub(r"[_\-]+", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() if cleaned else file_name


def _artist_from_filename(file_name: str) -> str | None:
    """Try to extract an artist name from a filename.

    Recognises the common "Artist - Title" convention. Returns None
    if the filename doesn't match.
    """
    stem = Path(file_name).stem
    # Match "Artist - Title" or "Artist_-_Title" patterns
    match = re.match(r"^(.+?)\s*[-–—]\s+(.+)$", stem)
    if match:
        artist = re.sub(r"[_]+", " ", match.group(1)).strip()
        if artist:
            return artist.title()
    return None


def scan_folder(folder_path: str) -> list[dict]:
    """Recursively scan a folder for audio files and extract metadata.

    Args:
        folder_path: Root folder to scan.

    Returns:
        List of track dicts suitable for LocalLibraryRepo.upsert_track().
        Each dict has keys: file_path, file_name, title, artist, album,
        duration_seconds, format, file_size, file_mtime, file_hash,
        album_art_embedded.
    """
    try:
        from tinytag import TinyTag
    except ImportError:
        logger.error("tinytag is not installed. Install it with: pip install tinytag")
        return []

    root = Path(folder_path)
    is_dir = root.is_dir()
    logger.info("scan_folder: path=%s, exists=%s, is_dir=%s", folder_path, root.exists(), is_dir)
    if not is_dir:
        logger.warning("Scan target is not a directory: %s", folder_path)
        return []

    canonical_root = _canonical_path(root)
    tracks: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for dirname in list(dirnames):
            child_dir = Path(dirpath) / dirname
            if _is_link(child_dir) or not _is_under_root(child_dir, canonical_root):
                logger.warning("Skipping linked or outside-root directory during local scan: %s", child_dir)
                dirnames.remove(dirname)

        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            if _is_link(fpath):
                logger.warning("Skipping linked media file during local scan: %s", fpath)
                continue
            canonical_file = _canonical_path(fpath)
            if not _is_under_root(canonical_file, canonical_root):
                logger.warning("Skipping outside-root media file during local scan: %s", fpath)
                continue

            # Skip files with excessively long filenames (OS limit)
            if len(fname) > 255:
                logger.warning("Skipping file with filename exceeding 255 chars: %s", fname[:80])
                continue

            # Skip files with paths exceeding OS limits (260 on Windows)
            full_path_str = str(canonical_file)
            if len(full_path_str) > 260 and os.name == "nt":
                logger.warning(
                    "Skipping file with path exceeding 260 chars: %s",
                    full_path_str[:80],
                )
                continue

            try:
                stat = canonical_file.stat()
            except OSError as exc:
                logger.warning("Cannot stat file %s: %s", canonical_file, exc)
                continue

            title = None
            artist = None
            album = None
            duration_seconds = None
            album_art_embedded = False
            artwork_data = None

            try:
                tag = TinyTag.get(str(canonical_file), image=True)
                title = tag.title if tag.title else None
                artist = tag.artist if tag.artist else None
                album = tag.album if tag.album else None
                duration_seconds = tag.duration
                image_bytes = tag.get_image()
                if image_bytes:
                    album_art_embedded = True
                    from music.providers.local.artwork import _to_data_uri

                    artwork_data = _to_data_uri(image_bytes)
            except Exception as exc:
                logger.warning("Failed to read metadata from %s: %s", canonical_file, exc)

            if not title:
                title = _title_from_filename(fname)
            if not artist:
                artist = _artist_from_filename(fname)

            tracks.append(
                {
                    "file_path": str(canonical_file),
                    "file_name": fname,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "duration_seconds": duration_seconds,
                    "format": ext.lstrip("."),
                    "file_size": stat.st_size,
                    "file_mtime": stat.st_mtime,
                    "file_hash": None,
                    "album_art_embedded": album_art_embedded,
                    "artwork_data": artwork_data,
                    "media_type": classify_media_type(ext),
                }
            )

    logger.info("Scanned %d audio files in %s", len(tracks), folder_path)
    return tracks


def scan_and_index(
    folder_path: str,
    repo: object,
) -> dict[str, int]:
    """Scan a folder, upsert all tracks, and remove deleted files.

    This is the main entry point for a full library rescan. It:
    1. Scans the folder for audio files with metadata.
    2. Upserts each track into the repository.
    3. Removes tracks outside the configured root and tracks that no longer exist on disk.

    Safety: if the scan finds 0 files but the library already has tracks,
    the removal step is skipped to prevent accidental purges caused by
    transient filesystem errors (e.g. drive temporarily unavailable).

    Args:
        folder_path: Root folder to scan.
        repo: A LocalLibraryRepo instance (typed as object to avoid
            circular imports when called from external modules).

    Returns:
        Dict with keys: scanned, upserted, removed, errors.
    """
    from music.providers.local.db import LocalLibraryRepo

    if not isinstance(repo, LocalLibraryRepo):
        raise TypeError("repo must be a LocalLibraryRepo instance, got %s" % type(repo).__name__)

    with _scan_lock:
        return _scan_and_index_locked(folder_path, repo)


def _scan_and_index_locked(
    folder_path: str,
    repo: object,
) -> dict[str, int]:
    """Inner implementation of scan_and_index, called under _scan_lock."""
    root_path = Path(folder_path)
    root_is_dir = root_path.is_dir()
    root = _canonical_path(root_path)
    tracks = scan_folder(folder_path)
    upserted = 0
    errors = 0
    removed = 0
    file_paths: set[str] = set()

    for track in tracks:
        file_paths.add(track["file_path"])
        try:
            repo.upsert_track(track)
            upserted += 1
        except Exception as exc:
            logger.warning("Failed to upsert track %s: %s", track["file_path"], exc)
            errors += 1

    if root_is_dir:
        outside_root_paths = {
            str(track["file_path"])
            for track in repo.get_all_tracks()
            if track.get("file_path") and not _is_under_root(str(track["file_path"]), root)
        }
        if outside_root_paths:
            removed_outside_root = repo.remove_tracks_by_paths(outside_root_paths)
            removed += removed_outside_root
            logger.info(
                "Pruned %d local library rows outside configured root %s",
                removed_outside_root,
                root,
            )

    # Safety guard: if the scan returned 0 files but the library already has
    # indexed tracks, a transient error (drive unavailable, permission issue,
    # etc.) is the most likely cause.  Refuse to purge to avoid data loss.
    if not file_paths:
        existing = repo.get_all_tracks()
        if existing:
            logger.warning(
                "Scan returned 0 files but library has %d indexed tracks — "
                "skipping purge to prevent data loss (folder=%s, is_dir=%s)",
                len(existing),
                folder_path,
                Path(folder_path).is_dir(),
            )
        else:
            # Library is already empty, nothing to remove.
            pass
    else:
        removed += repo.remove_tracks_not_in(file_paths)

    logger.info(
        "Index complete: scanned=%d, upserted=%d, removed=%d, errors=%d",
        len(tracks),
        upserted,
        removed,
        errors,
    )

    # Phase 2: extract video thumbnails in background for tracks missing artwork
    _start_background_artwork_extraction(repo)

    return {
        "scanned": len(tracks),
        "upserted": upserted,
        "removed": removed,
        "errors": errors,
    }


def _start_background_artwork_extraction(repo: object) -> None:
    """Start a background thread to extract video thumbnails for tracks missing artwork."""
    thread = threading.Thread(
        target=_extract_missing_artwork,
        args=(repo,),
        name="artwork-extraction",
        daemon=True,
    )
    thread.start()


def _extract_missing_artwork(repo: object) -> None:
    """Extract artwork for tracks that have no cached artwork_data.

    For video files: extracts a keyframe thumbnail via FFmpeg.
    For audio files without embedded art: skips (no external source).
    """
    from music.providers.local.artwork import VIDEO_EXTENSIONS, extract_video_thumbnail

    try:
        tracks = repo.get_tracks_missing_artwork()
        video_tracks = [t for t in tracks if Path(t["file_path"]).suffix.lower() in VIDEO_EXTENSIONS]
        if not video_tracks:
            return

        logger.info("Phase 2: extracting thumbnails for %d video files", len(video_tracks))
        extracted = 0
        for track in video_tracks:
            artwork = extract_video_thumbnail(track["file_path"])
            if artwork:
                repo.update_artwork(track["file_path"], artwork)
                extracted += 1
        logger.info(
            "Phase 2 complete: extracted %d/%d video thumbnails",
            extracted,
            len(video_tracks),
        )
    except Exception:
        logger.exception("Background artwork extraction failed")
