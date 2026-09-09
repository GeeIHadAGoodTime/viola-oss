from __future__ import annotations

import re

from core.constants import DEFAULT_API_PORT, LOCALHOST_NAME

# NOTE: DEFAULT_EMBED_ORIGIN stays http for YouTube origin parameter (external)
DEFAULT_EMBED_ORIGIN = f"http://{LOCALHOST_NAME}"
_VIDEO_ID_RE = re.compile(r"(?:v=|\/)([0-9A-Za-z_-]{11})(?:[&?].*)?$")


def build_embed_url(
    video_id: str,
    origin: str | None = None,
    *,
    use_iframe_html: bool = True,
    port: int | None = None,
) -> str:
    """
    Build the YouTube embed URL.

    By default, returns URL to the local iframe HTML template which properly
    sets up the YouTube IFrame API with matching origin. This prevents Error 153.

    Args:
        video_id: The 11-character YouTube identifier.
        origin: Optional override for the origin parameter (defaults to localhost).
        use_iframe_html: If True (default), return URL to local HTML template.
                        If False, return direct YouTube embed URL (may cause Error 153).
        port: API server port (defaults to 8756).

    Returns:
        URL to load in WebEngine for YouTube playback.
    """
    if use_iframe_html:
        # Use local HTML template that properly sets up IFrame API with matching origin
        from config.settings import settings

        api_port = port or DEFAULT_API_PORT
        scheme = "https" if settings.ssl_enabled else "http"
        return f"{scheme}://{LOCALHOST_NAME}:{api_port}/static/webviews/youtube_iframe.html?video={video_id}&autoplay=1"
    else:
        # Legacy: Direct YouTube embed (prone to Error 153 due to origin mismatch)
        safe_origin = (origin or DEFAULT_EMBED_ORIGIN).rstrip("/")
        return f"https://www.youtube.com/embed/{video_id}?enablejsapi=1&origin={safe_origin}"


def extract_video_id(value: str | None) -> str | None:
    """
    Extract a YouTube video_id from watch/embed URLs or shortlinks.

    PRD 7.3.2-7.3.3: Returns only the 11-character ID or None.
    Validates format to ensure canonical behavior.

    Args:
        value: Candidate video URL or identifier.

    Returns:
        11-character video_id if it can be derived and valid, otherwise None.
    """
    if not value:
        return None
    candidate = value.strip()

    # Direct 11-character ID check (must be alphanumeric + _-)
    if len(candidate) == 11 and all(c.isalnum() or c in ("_", "-") for c in candidate):
        return candidate

    # Extract from URL patterns
    match = _VIDEO_ID_RE.search(candidate)
    if match:
        extracted = match.group(1)
        # Validate extracted ID is exactly 11 characters
        if len(extracted) == 11:
            return extracted

    # Handle youtu.be shortlinks
    if "youtu.be/" in candidate:
        potential_id = candidate.rstrip("/").split("/")[-1].split("?")[0].split("&")[0]
        if len(potential_id) == 11 and all(c.isalnum() or c in ("_", "-") for c in potential_id):
            return potential_id

    return None
