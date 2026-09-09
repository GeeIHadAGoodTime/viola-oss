"""Utilities for queueing playlist tracks on the active music interface."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from core.json_types import JsonDict, JsonValue, to_json_value
from core.logging_config import get_logger

default_logger = get_logger(__name__)


class _Logger(Protocol):
    def warning(self, msg: str, *args: object) -> None: ...

    def debug(self, msg: str, *args: object) -> None: ...


async def _invoke_method(
    method: Callable[..., object],
    query: str,
    source: str | None,
    metadata: JsonDict | None,
) -> None:
    """
    Call the given music method and await the result if necessary.

        Some music interfaces accept a ``source`` keyword argument; others do not.
        We optimistically provide it and gracefully retry without it when unsupported.
    """
    attempts: list[dict[str, JsonValue]] = []
    base_kwargs: dict[str, JsonValue] = {}
    if source:
        base_kwargs["source"] = source
    if metadata is not None:
        attempts.append({**base_kwargs, "metadata": metadata})
    attempts.append(base_kwargs)
    attempts.append({})

    last_error: TypeError | None = None
    for kwargs in attempts:
        try:
            result = method(query, **kwargs)
            break
        except TypeError as error:
            last_error = error
    else:
        if last_error is not None:
            raise last_error
        result = method(query)

    if inspect.isawaitable(result):
        await result


async def queue_playlist_tracks(
    music_iface: object,
    videos: Iterable[Mapping[str, object]],
    *,
    log: _Logger | None = None,
    max_logged_failures: int = 5,
) -> tuple[int, int]:
    """
    Queue playlist videos onto the provided music interface.

    Args:
        music_iface: Active music player interface.
        videos: Iterable of video dictionaries containing at least a ``url`` key.
        log: Optional logger (defaults to loguru's ``logger``).
        max_logged_failures: Maximum number of warning-level failures to log before
            switching to debug-level to avoid log spam.

    Returns:
        Tuple of (successfully_queued, failed_count).
    """

    log = log or default_logger
    video_list: list[JsonDict] = []
    for video in videos:
        video_value = to_json_value(video)
        if isinstance(video_value, dict):
            video_list.append(video_value)

    if not video_list:
        return 0, 0

    if not music_iface:
        log.warning("No music interface available to queue playlist tracks.")
        return 0, len(video_list)

    successes = 0
    failures = 0
    first_track_pending = True
    fallback_notice_emitted = False

    for video_dict in video_list:
        url_value = video_dict.get("url")
        title_value = video_dict.get("title")
        video_id_value = video_dict.get("video_id")

        url: str | None = url_value if isinstance(url_value, str) else None
        title: str | None = title_value if isinstance(title_value, str) else None
        video_id: str | None = video_id_value if isinstance(video_id_value, str) else None

        normalized_url = url.strip() if isinstance(url, str) else None
        normalized_video_id = video_id.strip() if isinstance(video_id, str) else None

        query_value: str | None
        if normalized_video_id:
            # Use full YouTube URL instead of bare video ID so the player
            # correctly detects YouTube content and routes to the right backend
            query_value = f"https://www.youtube.com/watch?v={normalized_video_id}"
        elif normalized_url:
            query_value = normalized_url
        else:
            failures += 1
            message = f"Playlist entry missing identifier; skipping track (title={title or '<unknown>'})"
            if failures <= max_logged_failures:
                log.warning(message)
            else:
                log.debug(message)
            continue

        source_hint = (
            "url"
            if (normalized_video_id or (normalized_url and normalized_url.startswith(("http://", "https://"))))
            else None
        )
        metadata_hints: JsonDict = {}
        if normalized_video_id:
            metadata_hints["video_id"] = normalized_video_id
            metadata_hints["source"] = "url"
            metadata_hints["thumbnail_url"] = f"https://img.youtube.com/vi/{normalized_video_id}/hqdefault.jpg"
            metadata_hints["provider"] = "youtube_music"
        if title:
            metadata_hints["title"] = title
        uploader_value = video_dict.get("uploader") or video_dict.get("artist")
        if isinstance(uploader_value, str) and uploader_value:
            metadata_hints["artist"] = uploader_value

        duration_value = video_dict.get("duration") or video_dict.get("length")
        if duration_value is not None:
            metadata_hints["duration"] = to_json_value(duration_value)
        if normalized_url and "source" not in metadata_hints:
            metadata_hints["source"] = "url"
        if normalized_url and "original_url" not in metadata_hints:
            metadata_hints["original_url"] = normalized_url
        # Include URL in metadata so player can use it directly without re-resolution
        if normalized_url and "url" not in metadata_hints:
            metadata_hints["url"] = normalized_url
        # Always ensure URL is in metadata for direct player use
        if "url" not in metadata_hints:
            metadata_hints["url"] = query_value

        display_identifier = title or normalized_url or normalized_video_id or query_value

        try:
            if first_track_pending:
                play_async = getattr(music_iface, "play_async", None)
                if callable(play_async):
                    await _invoke_method(play_async, query_value, source_hint, metadata_hints)
                else:
                    play = getattr(music_iface, "play", None)
                    if not callable(play):
                        failures += 1
                        log.warning(
                            "Music interface cannot start playlist playback (track=%s).",
                            display_identifier,
                        )
                        continue
                    await _invoke_method(play, query_value, source_hint, metadata_hints)

                successes += 1
                first_track_pending = False
                continue

            enqueue = getattr(music_iface, "enqueue", None)
            if callable(enqueue):
                await _invoke_method(enqueue, query_value, source_hint, metadata_hints)
                successes += 1
                continue

            enqueue_async = getattr(music_iface, "enqueue_async", None)
            if callable(enqueue_async):
                await _invoke_method(enqueue_async, query_value, source_hint, metadata_hints)
                successes += 1
                continue

            fallback_method = None
            fallback_label = None
            play_async = getattr(music_iface, "play_async", None)
            if callable(play_async):
                fallback_method = play_async
                fallback_label = "play_async"
            else:
                play = getattr(music_iface, "play", None)
                if callable(play):
                    fallback_method = play
                    fallback_label = "play"

            if fallback_method:
                if not fallback_notice_emitted:
                    log.warning(
                        "Music interface lacks enqueue support; falling back to %s for subsequent playlist tracks. Playback will restart per track.",
                        fallback_label,
                    )
                    fallback_notice_emitted = True

                await _invoke_method(fallback_method, query_value, source_hint, metadata_hints)
                successes += 1
                continue

            failures += 1
            log.warning(
                "Unable to queue playlist track; no suitable playback methods (track=%s).",
                display_identifier,
            )

        except Exception as exc:  # pragma: no cover - defensive logging
            failures += 1
            if failures <= max_logged_failures:
                log.warning(
                    "Failed to queue playlist track (track=%s): %s",
                    display_identifier,
                    exc,
                )
            else:
                log.debug(
                    "Failed to queue playlist track (track=%s): %s",
                    display_identifier,
                    exc,
                )

    return successes, failures
