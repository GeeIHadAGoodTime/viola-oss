from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from models.player import QueueItem
from music.exceptions import ResolutionError
from music.youtube_embed import extract_video_id


@dataclass(slots=True)
class ResolutionMetadata:
    """
    Normalised metadata returned by resolver implementations.

    Attributes
    ----------
    url:
        Direct streamable URL. Required.
    title:
        Human-readable title. Required.
    source:
        Source hint (ytsearch1, url, local, etc.). Required.
    video_id:
        YouTube video identifier (11 chars) if known.
    artist:
        Channel/uploader/artist hint.
    duration:
        Track duration in seconds.
    thumbnail_url:
        URL of representative artwork/thumbnail.
    artwork_url:
        Optional override for artwork (defaults to thumbnail_url when provided).
    provider:
        Provider identifier (youtube_music, local, etc.).
    resolver_path:
        Resolver strategy used (developer, background, fallback, cache, etc.).
    capabilities:
        Additional per-track metadata fed into QueueItem.capabilities.
    extras:
        Arbitrary structured metadata preserved for debugging/instrumentation.
    resolved_at:
        Epoch timestamp when the metadata was produced.
    """

    url: str
    title: str
    source: str
    video_id: str | None = None
    artist: str | None = None
    duration: int | None = None
    thumbnail_url: str | None = None
    artwork_url: str | None = None
    provider: str | None = "youtube_music"
    resolver_path: str = "unknown"
    capabilities: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    resolved_at: float | None = None

    def ensure_valid(self) -> ResolutionMetadata:
        """Validate that critical fields exist."""
        if not isinstance(self.url, str) or len(self.url.strip()) < 5:
            raise ResolutionError("resolver_produced_invalid_url")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ResolutionError("resolver_produced_invalid_title")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ResolutionError("resolver_produced_invalid_source")
        return self


def queue_item_from_resolution(
    metadata: ResolutionMetadata,
    *,
    force_source: str | None = None,
    logger: Any | None = None,
) -> QueueItem:
    """
    Convert normalised resolution metadata into a QueueItem.

    Parameters
    ----------
    metadata:
        ResolutionMetadata instance (validated prior to conversion).
    force_source:
        Optional override for QueueItem.source (e.g. to enforce url/test-mode).
    logger:
        Optional logger for instrumentation.
    """

    resolved = metadata.ensure_valid()

    item_id = resolved.extras.get("queue_item_id")
    if not isinstance(item_id, str) or not item_id:
        item_id = str(uuid.uuid4())

    resolved_at = resolved.resolved_at if isinstance(resolved.resolved_at, (int, float)) else time.time()

    item_capabilities: dict[str, Any] = dict(resolved.capabilities or {})
    if resolved.thumbnail_url and "thumbnail_url" not in item_capabilities:
        item_capabilities["thumbnail_url"] = resolved.thumbnail_url
    if resolved.duration is not None and "duration_seconds" not in item_capabilities:
        item_capabilities["duration_seconds"] = resolved.duration
    if resolved.resolver_path:
        item_capabilities.setdefault("resolver_path", resolved.resolver_path)

    artwork = resolved.artwork_url or resolved.thumbnail_url

    # Extract playback_mode from extras if present
    # CRITICAL: This preserves playback_mode from provider (e.g., "embedded_webview" for YouTube Music)
    # The provider sets playback_mode in StreamInfo.metadata, which flows through ResolutionMetadata.extras
    playback_mode = resolved.extras.get("playback_mode") if resolved.extras else None

    provider_id = (resolved.provider or "").lower()
    if provider_id in {"youtube_music", "youtube", "youtube_iframe"} and not resolved.video_id:
        derived_video_id = extract_video_id(resolved.url)
        if derived_video_id:
            resolved.video_id = derived_video_id
            if logger:
                logger.info(
                    "YTM_QUEUE_ITEM video_id_missing_in_metadata action=derived_from_url id=%s",
                    item_id,
                )
        elif logger:
            logger.error(
                "YTM_QUEUE_ITEM video_id_missing_in_metadata provider=%s url=%s id=%s",
                provider_id,
                resolved.url[:80] if resolved.url else "none",
                item_id,
            )

    queue_item = QueueItem(
        id=item_id,
        title=resolved.title.strip(),
        url=resolved.url.strip(),
        source=(force_source or resolved.source or "url").strip(),
        video_id=resolved.video_id,
        artist=resolved.artist,
        provider=resolved.provider,
        artwork_url=artwork,
        capabilities=item_capabilities,
        resolved_at=resolved_at,
        playback_mode=playback_mode,  # Propagate playback_mode from resolution metadata (supports "embedded_webview", "external_browser", "vlc_stream")
    )
    if resolved.thumbnail_url:
        queue_item.thumbnail_url = resolved.thumbnail_url

    # Preserve extras for downstream consumers (UI/debug bus) without polluting model fields.
    if resolved.extras:
        queue_item.capabilities.setdefault("resolver_extras", resolved.extras)

    if logger:
        # Log with distinctive marker for YouTube Music pipeline tracing
        if resolved.provider == "youtube_music":
            logger.info(
                "YTM_QUEUE_ITEM provider=youtube_music video_id=%s url=%s title=%s id=%s",
                resolved.video_id or "none",
                resolved.url[:80] if resolved.url else "none",
                resolved.title[:50] if resolved.title else "none",
                queue_item.id,
            )
        else:
            logger.debug(
                "event=queue_item_from_resolution status=ok resolver_path=%s video_id=%s id=%s",
                resolved.resolver_path,
                resolved.video_id,
                queue_item.id,
            )

    return queue_item
