"""
Unified Player State Models - CANONICAL source of truth.

AI Instructions
===============
This is the CANONICAL module for player state. Always import from here.

Usage:
    >>> from models.player import PlayerState, QueueItem
    >>> item = QueueItem(id="track-123", title="Song", url="https://...")
    >>> state = PlayerState(is_playing=True, now_playing=item, queue=[item])

Related Modules:
    - models/state_manager.py: ConsolidatedState (thread-safe wrapper)
    - backend/music_adapter.py: MusicControllerAdapter (control interface)
    - contracts/player_state.py: JSON Schema validation

Deprecated Alternatives (DO NOT USE):
    - music/queue_state.py (REMOVED - Ruff TID251 blocks import)
    - music/queue_manager.py (REMOVED - Ruff TID251 blocks import)
    - Dict-based queue items (use QueueItem dataclass instead)

See Also:
    - docs/architecture/canonical_surfaces.md
    - docs/legacy_surfaces.md
    - CHANGELOG_RECENT.md

Uses Pydantic (v2) for API validation and dataclass compatibility.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field, computed_field

from core.json_types import JsonDict, JsonValue, to_json_value


class QueueItem(BaseModel):
    """
    Represents a single item in the playback queue.

    Attributes:
        id: Unique identifier for this queue item
        title: Song/video title
        url: Direct playback URL (resolved stream URL)
        source: Source type (ytsearch1, url, local)
        video_id: YouTube video ID (for thumbnails, metadata)
        artist: Artist/channel name
        resolved_at: Timestamp when URL was resolved (for freshness tracking)
    """

    id: str
    title: str | None = None
    url: str | None = None
    source: str | None = None
    video_id: str | None = None
    artist: str | None = None
    provider: str | None = Field(
        default=None,
        description="Normalized provider identifier (spotify, youtube_music, youtube_iframe, local, browser).",
    )
    artwork_url: str | None = Field(
        default=None,
        description="Resolved artwork URL suitable for UI display",
    )
    stream_token: str | None = Field(
        default=None,
        description="Provider-issued token or track identifier required for official SDK playback",
    )
    capabilities: JsonDict = Field(
        default_factory=dict,
        description="Provider capability overrides for this item (gapless, lyrics, bitrate, offline flags)",
    )
    resolved_at: float | None = Field(default=None, description="Unix timestamp when URL was resolved")
    duration: int | None = Field(default=None, description="Track duration in seconds")
    unavailable: bool = Field(
        default=False,
        description="Whether this track is unavailable (e.g., provider not linked, disabled in build)",
    )
    unavailable_reason: str | None = Field(
        default=None,
        description="Human-readable reason why track is unavailable",
    )
    playback_mode: str | None = Field(
        default=None,
        description=(
            "Playback mode: 'qt_media' (local files), 'vlc_stream' (direct streams), "
            "'external_browser', 'embedded_webview'. Determines which "
            "playback mechanism to use."
        ),
    )
    media_type: str | None = Field(
        default=None,
        description="Media type: 'audio' or 'video'. Determined by file extension for local files.",
    )

    model_config = ConfigDict(frozen=False, extra="allow")

    def is_url_expired(self, max_age_hours: float = 6.0) -> bool:
        """Check if URL is likely expired (YouTube URLs expire after ~6 hours)."""
        if self.resolved_at is None:
            return False  # Legacy item, assume not expired
        import time

        age_hours = (time.time() - self.resolved_at) / 3600
        return age_hours > max_age_hours

    def is_url_stale(self, threshold_hours: float = 4.0) -> bool:
        """Check if URL is getting old and should be refreshed soon."""
        if self.resolved_at is None:
            return False
        import time

        age_hours = (time.time() - self.resolved_at) / 3600
        return age_hours > threshold_hours

    def age_hours(self) -> float:
        """Get age of URL in hours."""
        if self.resolved_at is None:
            return 0.0
        import time

        return (time.time() - self.resolved_at) / 3600

    def set_capability(self, key: str, value: JsonValue) -> None:
        """Attach a provider-specific capability override to this queue item."""
        self.capabilities[key] = value

    def get_capability(self, key: str, default: JsonValue | None = None) -> JsonValue | None:
        """Retrieve a provider capability override."""
        return self.capabilities.get(key, default)

    @property
    @computed_field(return_type=str | None)
    def thumbnail_url(self) -> str | None:
        """
        Get thumbnail URL from capabilities for backward compatibility.

        This property allows tests and legacy code to access thumbnail_url
        directly on QueueItem, even though it's stored in capabilities.
        """
        # Check capabilities first (canonical location)
        thumbnail = self.capabilities.get("thumbnail_url")
        if isinstance(thumbnail, str):
            return thumbnail
        # Fallback to artwork_url if available
        if self.artwork_url:
            return self.artwork_url
        return None

    @thumbnail_url.setter
    def thumbnail_url(self, value: str | None) -> None:
        """
        Set thumbnail URL in capabilities for backward compatibility.
        """
        if value:
            self.capabilities["thumbnail_url"] = value
        elif "thumbnail_url" in self.capabilities:
            del self.capabilities["thumbnail_url"]

    def with_artwork(self, artwork_url: str | None) -> QueueItem:
        """
        Return a shallow copy of this queue item with updated artwork metadata.

        The existing instance is mutated as well for backward compatibility with
        code paths that rely on in-place updates.
        """
        if artwork_url:
            self.artwork_url = artwork_url
            # Maintain thumbnail alias expected by legacy UI components.
            if "thumbnail_url" not in self.capabilities:
                self.capabilities["thumbnail_url"] = artwork_url
        return self


class PlayerState(BaseModel):
    """
    Complete player state representation.

    This is the single source of truth for all player state.
    Used by:
    - MusicPlayer (internal state)
    - WebSocket broadcasts
    - UI updates
    - API responses

    Attributes:
        is_playing: Whether audio is currently playing
        now_playing: Currently playing track (None if idle)
        queue: Upcoming tracks in order
        volume: Current volume (0-100)
        position: Current playback position in seconds
        duration: Total track duration in seconds
        position_percentage: Position as percentage (0.0 to 1.0)
    """

    is_playing: bool = False
    now_playing: QueueItem | None = None
    queue: list[QueueItem] = Field(default_factory=list)
    volume: Annotated[int, Field(ge=0, le=100)] = 80
    position: int = 0
    position_ms: int = Field(
        default=0,
        description="Playback position in milliseconds (sub-second precision).",
    )
    duration: int = 0
    position_percentage: float = 0.0
    backend: str | None = Field(
        default=None,
        description="Identifier for the active playback backend (e.g. 'vlc', 'simple', 'embedded', 'youtube_web').",
    )
    backend_display_name: str | None = Field(
        default=None,
        description="Human friendly backend name suitable for UI display.",
    )
    backend_capabilities: JsonDict = Field(
        default_factory=dict,
        description="Backend-level capability flags (pause, seek, volume, etc.).",
    )
    playback_capabilities: JsonDict = Field(
        default_factory=dict,
        description="Normalized capability flags after combining backend + track overrides.",
    )
    playback_mode: str | None = Field(
        default=None,
        description=(
            "Current playback mode: 'vlc_stream' (for local files/direct "
            "streams), 'external_browser', 'embedded_webview'."
        ),
    )
    resolver_info: JsonDict = Field(
        default_factory=dict,
        description="Resolver metadata describing how the current track was resolved.",
    )
    playback_errors: list[JsonDict] = Field(
        default_factory=list,
        description="Recent playback warnings/errors surfaced to the UI.",
    )
    metadata: JsonDict = Field(
        default_factory=dict,
        description="Additional state metadata for UI hints (playlist summary, context).",
    )
    state_version: int = Field(
        default=0,
        description="Monotonically increasing version number for state changes. Incremented on each state update per PRD §4.2.",
    )
    repeat_mode: str = Field(
        default="off",
        description="Repeat mode: 'off', 'all', or 'one'.",
    )
    shuffle: bool = Field(
        default=False,
        description="Whether shuffle is enabled.",
    )

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    def to_dict(self) -> JsonDict:
        """Convert to dictionary for JSON serialization."""
        value = to_json_value(self.model_dump())
        if isinstance(value, dict):
            return value
        raise TypeError("PlayerState.model_dump returned non-object payload")


_RESPONSE_METADATA_KEYS: frozenset[str] = frozenset({"ok", "error"})


def _strip_response_metadata(payload: Mapping[str, JsonValue]) -> JsonDict:
    return {key: value for key, value in payload.items() if key not in _RESPONSE_METADATA_KEYS}


_PLAYER_STATE_JSON_SCHEMA_VALUE = to_json_value(PlayerState.model_json_schema())
PLAYER_STATE_JSON_SCHEMA: JsonDict = (
    _PLAYER_STATE_JSON_SCHEMA_VALUE if isinstance(_PLAYER_STATE_JSON_SCHEMA_VALUE, dict) else {}
)


def get_player_state_schema() -> JsonDict:
    """Return a deep copy of the PlayerState JSON schema for consumers."""
    return copy.deepcopy(PLAYER_STATE_JSON_SCHEMA)


def validate_player_state_payload(payload: object) -> PlayerState:
    """
    Validate a raw `/v1/state` payload and return a strongly-typed PlayerState.

    Raises:
        ValidationError: If payload is missing required fields or has wrong types.
    """

    if isinstance(payload, PlayerState):
        return payload

    if not isinstance(payload, Mapping):
        return PlayerState.model_validate(payload)

    payload_value = to_json_value(payload)
    if not isinstance(payload_value, dict):
        return PlayerState.model_validate(payload)

    stripped_payload = _strip_response_metadata(payload_value)
    data_section = stripped_payload.get("data")
    if isinstance(data_section, Mapping):
        candidate: JsonDict = {
            str(key): to_json_value(value)
            for key, value in data_section.items()
            if isinstance(key, str) and key not in {"schema", "fallback"}
        }
        return PlayerState.model_validate(candidate)

    return PlayerState.model_validate(stripped_payload)
