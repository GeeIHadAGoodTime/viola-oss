"""
YouTube URL Validation and Conversion Utilities

CANONICAL RULE (PRD v5.3 section 7.3.3): All YouTube playback URLs MUST use
the minimal embed format: https://www.youtube.com/embed/{video_id}

This module provides validation and conversion utilities to ensure compliance
with the PRD architecture.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from core.logging_config import get_logger
from music.youtube_embed import (
    build_embed_url,
    extract_video_id as canonical_extract_video_id,
)

logger = get_logger(__name__)

# YouTube video ID pattern (11 characters: alphanumeric, dash, underscore)
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# URL patterns that should be converted to embed format
WATCH_URL_PATTERNS = [
    re.compile(r"youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})"),
    re.compile(r"music\.youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtu\.be/([a-zA-Z0-9_-]{11})"),
]

# Embed URL pattern (canonical format)
EMBED_URL_PATTERN = re.compile(r"youtube\.com/embed/([a-zA-Z0-9_-]{11})")

# Local iframe template URL pattern (also valid for playback)
LOCAL_IFRAME_PATTERN = re.compile(r"localhost:\d+/static/webviews/youtube_iframe\.html\?video=([a-zA-Z0-9_-]{11})")


def extract_video_id(url: str) -> str | None:
    """
    Extract YouTube video ID from any YouTube URL format.

    Supports:
    - youtube.com/watch?v=VIDEO_ID
    - music.youtube.com/watch?v=VIDEO_ID
    - youtu.be/VIDEO_ID
    - youtube.com/embed/VIDEO_ID
    - Bare video ID (11 characters)

    Args:
        url: YouTube URL or bare video ID

    Returns:
        Video ID if found, None otherwise
    """
    canonical = canonical_extract_video_id(url)
    if canonical:
        return canonical

    if not url or not isinstance(url, str):
        return None

    url = url.strip()

    # Legacy fallback: keep previous heuristics in case canonical helper misses an edge case.
    embed_match = EMBED_URL_PATTERN.search(url)
    if embed_match:
        return embed_match.group(1)

    for pattern in WATCH_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            video_id = match.group(1)
            if YOUTUBE_VIDEO_ID_PATTERN.match(video_id):
                return video_id

    try:
        parsed = urlparse(url)
        if "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc:
            if parsed.path.startswith("/") and len(parsed.path) > 1:
                path_id = parsed.path.lstrip("/").split("/")[0]
                if YOUTUBE_VIDEO_ID_PATTERN.match(path_id):
                    return path_id

            query_params = parse_qs(parsed.query)
            if "v" in query_params:
                video_id = query_params["v"][0]
                if YOUTUBE_VIDEO_ID_PATTERN.match(video_id):
                    return video_id
    except Exception as e:
        logger.exception("URL parsing failed: %s", e)
        pass  # Silent OK: URL parsing fallback

    return None


def is_embed_url(url: str) -> bool:
    """
    Check if URL is already in canonical embed format.

    Recognizes two valid formats:
    1. Direct YouTube embed: youtube.com/embed/{video_id}
    2. Local iframe template: localhost:{port}/static/webviews/youtube_iframe.html?video={video_id}

    Args:
        url: URL to check

    Returns:
        True if URL is in embed format, False otherwise
    """
    if not url or not isinstance(url, str):
        return False

    # Check for direct YouTube embed format
    if EMBED_URL_PATTERN.search(url.lower()):
        return True

    # Check for local iframe template format (also valid for playback)
    if LOCAL_IFRAME_PATTERN.search(url):
        return True

    return False


def to_embed_url(video_id: str, autoplay: bool = True) -> str:
    """
    Build canonical YouTube embed URL from video ID.

    Args:
        video_id: YouTube video ID (11 characters)
        autoplay: Whether to enable autoplay

    Returns:
        Canonical embed URL

    Raises:
        ValueError: If video_id is invalid
    """
    if not video_id or not isinstance(video_id, str):
        raise ValueError("video_id must be a non-empty string")

    if not YOUTUBE_VIDEO_ID_PATTERN.match(video_id):
        raise ValueError(f"Invalid YouTube video ID format: {video_id}")

    # PRD requires that the canonical helper be the single source of truth.
    # The `autoplay` argument is preserved for backward compatibility only.
    return build_embed_url(video_id)


def validate_and_convert_url(url: str) -> tuple[str, bool]:
    """
    Validate YouTube URL and convert to embed format if needed.

    CANONICAL RULE: All YouTube playback URLs MUST use embed format.

    Args:
        url: YouTube URL to validate/convert

    Returns:
        Tuple of (converted_url, is_valid)
        - converted_url: Embed format URL or original if invalid
        - is_valid: True if URL is valid for playback, False otherwise

    Raises:
        ValueError: If URL is invalid and cannot be converted
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")

    url = url.strip()

    # Check if already in embed format (canonical)
    if is_embed_url(url):
        return url, True

    # Extract video ID and convert to embed format
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(
            f"Could not extract video ID from YouTube URL: {url[:100]}. "
            "URL must be in format: youtube.com/watch?v=VIDEO_ID, "
            "youtu.be/VIDEO_ID, or youtube.com/embed/VIDEO_ID"
        )

    # Convert to canonical embed format
    embed_url = to_embed_url(video_id, autoplay=True)
    logger.debug("Converted YouTube URL to embed format: %s -> %s", url[:80], embed_url[:80])

    return embed_url, True


def assert_embed_url(url: str, context: str = "") -> None:
    """
    Assert that URL is in embed format (runtime validation).

    Use this for defensive programming to catch violations early.

    Args:
        url: URL to validate
        context: Optional context string for error messages

    Raises:
        ValueError: If URL is not in embed format
    """
    if not url:
        return  # Empty URLs are handled elsewhere

    # Must be embed format
    if not is_embed_url(url):
        error_msg = (
            f"YouTube URL is not in canonical embed format: {url[:100]}. "
            f"PRD v5.3 section 7.3.3 requires: youtube.com/embed/{{video_id}}. "
            f"{context}"
        )
        logger.error("PRD_VIOLATION: %s", error_msg)
        raise ValueError(error_msg)
