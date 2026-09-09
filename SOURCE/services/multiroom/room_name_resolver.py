"""
Room Name Resolver for Voice Room Routing.

Resolves user-friendly room names (e.g., "kitchen", "bedroom speaker") to
room IDs by fuzzy-matching against registered room names in the RoomRegistry
and group names in the RoomGroupManager.

Usage:
    >>> from services.multiroom.room_name_resolver import resolve_room_name
    >>> result = resolve_room_name("kitchen")
    >>> if result is not None:
    ...     room_id, match_type = result
    ...     # match_type is "room" or "group"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from core.logging_config import get_logger
from core.platform import get_data_dir

if TYPE_CHECKING:
    from services.multiroom.room_groups.manager import RoomGroupManager
    from services.multiroom.room_registry import RoomRegistry

logger = get_logger(__name__)

_ALL_ROOMS_TARGET_ID = "__ALL_ROOMS__"
_ALL_ROOMS_PATTERNS = frozenset(
    {
        "all rooms",
        "every room",
        "everywhere",
        "all speakers",
        "every speaker",
        "all devices",
    }
)


@dataclass(frozen=True, slots=True)
class RoomMatch:
    """
    Result of a room name resolution.

    Attributes:
        target_id: The room_id (for room matches) or group_id (for group matches)
        match_type: Whether the match is a "room" or "group"
        display_name: The display name of the matched room/group
        is_local: Whether the matched room is the local device (only for room matches)
    """

    target_id: str
    match_type: Literal["room", "group", "ha_media_player"]
    display_name: str
    is_local: bool = False


def _normalize(name: str) -> str:
    """
    Normalize a room name for comparison.

    Strips whitespace, lowercases, and removes common filler words like
    "the", "my", "speaker", "room".
    """
    name = name.lower().strip()
    # Remove possessives
    name = re.sub(r"'s\b", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    return name


def _tokenize(name: str) -> set[str]:
    """Split a normalized name into a set of meaningful tokens."""
    # Remove noise words that don't help distinguish rooms
    noise = {"the", "a", "an", "my", "our", "in", "on", "at"}
    tokens = set(_normalize(name).split())
    return tokens - noise


def _score_match(query: str, candidate: str) -> float:
    """
    Score how well a query matches a candidate room name.

    Returns a score between 0.0 (no match) and 1.0 (exact match).
    Higher scores indicate better matches.
    """
    q_norm = _normalize(query)
    c_norm = _normalize(candidate)

    # Exact match after normalization
    if q_norm == c_norm:
        return 1.0

    # One is a substring of the other
    if q_norm in c_norm or c_norm in q_norm:
        # Score based on length ratio (closer lengths = better match)
        ratio = min(len(q_norm), len(c_norm)) / max(len(q_norm), len(c_norm))
        return 0.7 + (0.2 * ratio)

    # Token-based matching
    q_tokens = _tokenize(query)
    c_tokens = _tokenize(candidate)

    if not q_tokens or not c_tokens:
        return 0.0

    # Jaccard similarity on tokens
    intersection = q_tokens & c_tokens
    union = q_tokens | c_tokens

    if not union:
        return 0.0

    jaccard = len(intersection) / len(union)

    # Bonus if all query tokens appear in candidate
    if q_tokens <= c_tokens:
        jaccard = max(jaccard, 0.7)

    return jaccard


# Minimum score threshold for a match to be considered valid
_MATCH_THRESHOLD = 0.5


def _is_ha_media_player_fallback_configured() -> bool:
    """Return True only when the user has configured Home Assistant."""
    try:
        from ui.settings_manager import get_settings_manager

        settings_manager = get_settings_manager()
        url = settings_manager.get("home_assistant_url", "")
        token = settings_manager.get("home_assistant_token", "")
        return bool(url and token)
    except Exception as exc:
        logger.debug("HA media_player fallback configuration unavailable: %s", exc)
        return False


def _resolve_ha_media_player(room_name: str) -> RoomMatch | None:
    """Try to find a configured HA media_player entity matching the room name.

    Checks the cached HA device map (data/smart_home_devices.json) for
    media_player entities whose room matches the query, but only after the
    user has configured Home Assistant. Stale HA cache files must not override
    Viola's in-house multi-room pairing flow.

    Returns a RoomMatch with match_type="ha_media_player" and target_id
    set to the HA entity_id (e.g., "media_player.kitchen_sonos").
    """
    import json as _json

    if not _is_ha_media_player_fallback_configured():
        logger.debug(
            "Skipping HA media_player fallback for '%s'; provider is not configured",
            room_name,
        )
        return None

    # Try cached device map first (no network call)
    device_map_path = get_data_dir() / "smart_home_devices.json"
    if device_map_path.exists():
        try:
            data = _json.loads(device_map_path.read_text(encoding="utf-8"))
            for room_group in data:
                room_key = room_group.get("room", "")
                devices = room_group.get("devices", [])
                for device in devices:
                    if device.get("type") != "media_player":
                        continue
                    # Score room match
                    score = _score_match(room_name, room_key)
                    if score >= _MATCH_THRESHOLD:
                        entity_id = device.get("provider_entity_id", "")
                        display = device.get("name", room_key)
                        if entity_id:
                            logger.info(
                                "HA media_player fallback: '%s' -> %s (%s, score=%.2f)",
                                room_name,
                                entity_id,
                                display,
                                score,
                            )
                            return RoomMatch(
                                target_id=entity_id,
                                match_type="ha_media_player",
                                display_name=display,
                                is_local=False,
                            )
        except Exception as exc:
            logger.debug("HA device map read failed: %s", exc)

    return None


def resolve_room_name(
    room_name: str,
    *,
    user_id: str | None = None,
    registry: RoomRegistry | None = None,
    group_manager: RoomGroupManager | None = None,
) -> RoomMatch | None:
    """
    Resolve a user-friendly room name to a room_id or group_id.

    Searches both individual rooms (from RoomRegistry) and group names
    (from RoomGroupManager). Returns the best match above the threshold,
    or None if no good match is found.

    Individual rooms are checked first, then groups.

    Args:
        room_name: The user-spoken room name (e.g., "kitchen", "bedroom speaker")
        user_id: Tenant id for user-owned room and group state.
        registry: Optional RoomRegistry override (uses singleton if None)
        group_manager: Optional RoomGroupManager override (uses singleton if None)

    Returns:
        RoomMatch if a match is found, None otherwise
    """
    if not room_name or not room_name.strip():
        return None

    normalized_room_name = _normalize(room_name)
    if normalized_room_name in _ALL_ROOMS_PATTERNS:
        logger.info("Resolved room name '%s' to synthetic All Rooms group", room_name)
        return RoomMatch(
            target_id=_ALL_ROOMS_TARGET_ID,
            match_type="group",
            display_name="All Rooms",
            is_local=False,
        )

    # Resolve registry
    if registry is None:
        try:
            from services.multiroom.room_registry import get_room_registry

            registry = get_room_registry(user_id=user_id) if user_id else get_room_registry()
        except Exception as exc:
            logger.debug("RoomRegistry not available: %s", exc)
            registry = None

    # Resolve group manager
    if group_manager is None:
        try:
            if user_id:
                from ui.api.routes.room_groups import get_room_group_manager

                group_manager = get_room_group_manager()
        except Exception as exc:
            logger.debug("RoomGroupManager not available: %s", exc)
            group_manager = None

    best_match: RoomMatch | None = None
    best_score = _MATCH_THRESHOLD

    # Search individual rooms
    if registry is not None:
        try:
            for room in registry.list_rooms():
                score = _score_match(room_name, room.name)
                if score > best_score:
                    best_score = score
                    best_match = RoomMatch(
                        target_id=room.id,
                        match_type="room",
                        display_name=room.name,
                        is_local=room.is_local,
                    )
        except Exception as exc:
            logger.warning("Error searching rooms: %s", exc)

    # Search groups
    if group_manager is not None:
        try:
            group_manager_any = cast(Any, group_manager)
            if user_id:
                groups = group_manager_any.list_groups(user_id=user_id)
            else:
                groups = group_manager_any.list_groups()
            for group in groups:
                score = _score_match(room_name, group.group_name)
                if score > best_score:
                    best_score = score
                    best_match = RoomMatch(
                        target_id=group.group_id,
                        match_type="group",
                        display_name=group.group_name,
                        is_local=False,
                    )
        except Exception as exc:
            logger.warning("Error searching room groups: %s", exc)

    # Fallback: check configured Home Assistant media_player entities by
    # room/area. This is intentionally after the in-house room registry and
    # group manager, and it is skipped unless HA credentials exist.
    if best_match is None:
        try:
            ha_match = _resolve_ha_media_player(room_name)
            if ha_match is not None:
                best_match = ha_match
                best_score = 0.7  # synthetic score for logging
        except Exception as exc:
            logger.debug("HA media_player fallback unavailable: %s", exc)

    if best_match is not None:
        logger.info(
            "Resolved room name '%s' to %s '%s' (id=%s, score=%.2f)",
            room_name,
            best_match.match_type,
            best_match.display_name,
            best_match.target_id[:8],
            best_score,
        )
    else:
        logger.debug(
            "No room match for '%s' (best score below threshold %.2f)",
            room_name,
            _MATCH_THRESHOLD,
        )

    return best_match


__all__ = [
    "_ALL_ROOMS_TARGET_ID",
    "RoomMatch",
    "resolve_room_name",
]
