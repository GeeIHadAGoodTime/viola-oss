"""Origin for the independently hosted spoke video helper."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_YOUTUBE_EMBED_URL = "https://useviola.com/embed"


def youtube_embed_origin(value: str | None = None) -> str:
    """Validate the public endpoint before adding its exact origin to CSP."""
    value = value or DEFAULT_YOUTUBE_EMBED_URL
    if re.search(r"[\s'\";<>\\]", value):
        raise ValueError("VITE_YOUTUBE_EMBED_URL contains invalid URL characters")
    url = urlsplit(value)
    local_http = url.scheme == "http" and url.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if (url.scheme != "https" and not local_http) or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("VITE_YOUTUBE_EMBED_URL requires HTTPS (HTTP loopback is allowed for development)")
    hostname = url.hostname.encode("idna").decode("ascii")
    host = "[%s]" % hostname if ":" in hostname else hostname
    port = url.port
    port_suffix = ":%d" % port if port is not None and port != (443 if url.scheme == "https" else 80) else ""
    return "%s://%s%s" % (url.scheme, host, port_suffix)


def configured_youtube_embed_origin(value: str | None = None, *, assets_dir: Path | None = None) -> str:
    """Use the endpoint compiled into the UI, including in frozen installers.

    An explicit runtime override must agree with that artifact's origin; changing
    it requires rebuilding the UI rather than allowing messages to a stale host.
    """
    assets_dir = assets_dir or Path(__file__).resolve().parents[1] / "ui" / "static" / "react"
    manifest = assets_dir / "youtube-embed.json"
    if not manifest.exists():
        # Unbuilt source checkout or an older bundle retains the existing default.
        return youtube_embed_origin(value)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("The built YouTube helper manifest is invalid; rebuild the frontend") from exc
    endpoint = data.get("url") if isinstance(data, dict) else None
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("The built YouTube helper manifest is invalid; rebuild the frontend")
    origin = youtube_embed_origin(endpoint)
    if value and youtube_embed_origin(value) != origin:
        raise ValueError("VITE_YOUTUBE_EMBED_URL differs from the built frontend origin; rebuild the frontend")
    return origin
