"""Agent tool handler for liked-songs queries.

Exposes Viola's cross-provider liked-songs store to the MCP layer so the
AI can reason about the user's music taste and create playlists from it.

Wired into ``mcp_servers/core_tools/server.py``.
"""

from __future__ import annotations

from intent.tool_types import ToolResult


async def get_liked_songs_handler(
    since: str = "",
    provider: str = "",
    limit: int = 50,
    user_id: str = "",
) -> ToolResult:
    """Return liked songs from Viola's local tracker, optionally filtered.

    Args:
        since: ISO-8601 date or datetime string.  Only songs liked *after*
               this value are returned.  Examples: ``"2026-03-10"``,
               ``"2026-03-10T14:30:00+00:00"``.  Empty string = no filter.
        provider: Filter to a single provider: ``spotify``, ``youtube_music``,
                  ``youtube``, or ``local``.  Empty string = all providers.
        limit: Maximum number of results (default 50, max 500).

    Returns:
        ToolResult with a list of dicts, each containing:
        ``title``, ``artist``, ``provider``, ``provider_track_id``, ``url``,
        ``liked_at``.  Ordered newest-first.
    """
    from music.playlists.likes_store import LikesStore

    limit = max(1, min(limit, 500))
    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")

    try:
        store = LikesStore()
        # Async-native read: the cloud `get_liked_songs` tool runs ON the FastAPI
        # serving loop, so a sync `get_liked` (Postgres `_run` bridge) would raise
        # SyncBridgeLoopError. Await the async-native variant instead (CL-20260711-afd7).
        records = await store.get_liked_async(
            user_id=resolved_user_id,
            since=since.strip() if since.strip() else None,
            provider=provider.strip() if provider.strip() else None,
            limit=limit,
        )

        songs = [
            {
                "title": r.title,
                "artist": r.artist,
                "provider": r.provider,
                "provider_track_id": r.provider_track_id,
                "url": r.url,
                "liked_at": r.liked_at,
            }
            for r in records
        ]

        if not songs:
            filter_parts = []
            if provider.strip():
                filter_parts.append("provider=%r" % provider.strip())
            if since.strip():
                filter_parts.append("since=%r" % since.strip())
            filter_desc = " (" + ", ".join(filter_parts) + ")" if filter_parts else ""
            return ToolResult(
                ok=True,
                data={"songs": [], "count": 0, "message": "No liked songs found%s." % filter_desc},
            )

        return ToolResult(
            ok=True,
            data={"songs": songs, "count": len(songs)},
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="get_liked_songs failed: %s" % exc)
