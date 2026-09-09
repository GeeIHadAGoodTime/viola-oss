from __future__ import annotations

import hashlib
from typing import Any

from core.logging_config import get_logger
from models.player import PlayerState, QueueItem

log = get_logger(__name__)


def to_player_state(music: Any, state: Any, hub_authority: Any | None = None) -> PlayerState:
    """
    Get player state from the music player and normalise the payload.

    The music adapter may return either a `PlayerState` instance or a raw dict.
    We make sure the structure is valid and sanitised for downstream consumers.

    If hub_authority is provided, provider state is routed through it before
    being returned, ensuring canonical state supremacy.
    """
    # Handle None music player gracefully
    if music is None:
        log.warning("Music player is None, returning empty state")
        return sanitize_player_state(PlayerState())

    try:
        provider_state = music.state()

        # Extract playback_mode from provider before hub reconciliation
        # (reconciliation may create a new PlayerState that drops it).
        _provider_pm = (
            provider_state.get("playback_mode")
            if isinstance(provider_state, dict)
            else getattr(provider_state, "playback_mode", None)
        )

        # Route through Hub State Authority if available
        if hub_authority is not None:
            try:
                from core.hub_state_authority import StateMergeStrategy

                reconciliation = hub_authority.reconcile_provider_state(
                    provider_state=provider_state,
                    provider_id="music_player",
                    merge_strategy=StateMergeStrategy.MERGE,
                )

                if reconciliation.success and reconciliation.canonical_state:
                    # Use canonical state from hub authority
                    player_state = reconciliation.canonical_state
                else:
                    # Reconciliation failed - use provider state as fallback
                    log.warning("State reconciliation failed: %s", reconciliation.error_message)
                    player_state = provider_state
            except Exception as exc:
                log.exception(
                    "Hub state authority reconciliation failed: %s",
                    exc,
                )
                # Fall through to use provider state directly
                player_state = provider_state
        else:
            player_state = provider_state

        # Restore playback_mode if hub reconciliation dropped it
        if _provider_pm and isinstance(player_state, PlayerState):
            if not player_state.playback_mode:
                player_state = player_state.model_copy(update={"playback_mode": _provider_pm})

        if isinstance(player_state, PlayerState):
            return sanitize_player_state(player_state)

        if isinstance(player_state, dict):
            payload = normalize_state_payload(player_state)
            return sanitize_player_state(PlayerState(**payload))

    except Exception as exc:
        log.warning("Failed to get state from music.state(): %s", exc)

    return sanitize_player_state(PlayerState())


def sanitize_player_state(player_state: PlayerState) -> PlayerState:
    """
    Ensure queue entries have stable IDs and are valid `QueueItem` instances.
    """
    normalized_queue: list[QueueItem] = []
    normalized_now_playing: QueueItem | None = None

    for index, raw_item in enumerate(player_state.queue):
        try:
            normalized_queue.append(normalize_queue_item(raw_item, index))
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Dropping invalid queue entry at index %s: %s", index, exc)

    if player_state.now_playing is not None:
        try:
            normalized_now_playing = normalize_queue_item(player_state.now_playing, -1)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Dropping invalid now_playing entry: %s", exc)

    if not normalized_queue and not player_state.queue:
        if normalized_now_playing is None:
            return player_state

    update_payload: dict[str, Any] = {}
    if normalized_queue or player_state.queue:
        update_payload["queue"] = normalized_queue
    if normalized_now_playing is not None:
        update_payload["now_playing"] = normalized_now_playing

    if not update_payload:
        return player_state

    return player_state.model_copy(update=update_payload, deep=True)


def normalize_queue_item(raw_item: Any, index: int) -> QueueItem:
    """Return a `QueueItem`, synthesising an ID when required."""
    if isinstance(raw_item, QueueItem):
        if raw_item.id:
            return raw_item
        data = raw_item.model_dump()
    elif hasattr(raw_item, "model_dump"):
        data = raw_item.model_dump()
    elif isinstance(raw_item, dict):
        data = {**raw_item}
    elif hasattr(raw_item, "__dict__"):
        data = {**vars(raw_item)}
    else:
        data = {"title": str(raw_item)}

    if not data.get("title") and data.get("id"):
        data["title"] = str(data["id"])

    if not data.get("id"):
        data["id"] = synth_queue_item_id(data, index)

    return QueueItem.model_validate(data)


def synth_queue_item_id(data: dict[str, Any], index: int) -> str:
    """Produce a deterministic ID for queue entries lacking one."""
    candidate_fields = [
        str(data.get("video_id") or ""),
        str(data.get("url") or ""),
        str(data.get("title") or ""),
        str(data.get("artist") or ""),
        str(index),
    ]
    digest = hashlib.sha1(  # nosec B324 — not used for security
        "::".join(candidate_fields).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"auto-{digest[:12]}"


def normalize_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise raw dict payloads before model validation."""
    normalized: dict[str, Any] = dict(payload)

    queue_value = normalized.get("queue")
    if isinstance(queue_value, list):
        normalized_queue: list[dict[str, Any]] = []
        for idx, raw in enumerate(queue_value):
            try:
                normalized_queue.append(normalize_queue_item(raw, idx).model_dump())
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("Skipping invalid queue entry while normalizing payload: %s", exc)
        normalized["queue"] = normalized_queue

    now_playing_value = normalized.get("now_playing")
    if now_playing_value not in (None, {}):
        try:
            normalized["now_playing"] = normalize_queue_item(now_playing_value, -1).model_dump()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Removing invalid now_playing while normalizing payload: %s", exc)
            normalized["now_playing"] = None

    return normalized


def redact(text: str | None, limit: int = 256) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


STATE_CHANGING_COMMANDS = {
    "play",
    "pause",
    "resume",
    "stop",
    "skip",
    "volume.set",
    "volume.up",
    "volume.down",
}


def map_error(msg: str | None) -> str:
    if not msg:
        return "dispatch_failed"
    lowered = msg.lower()
    if "empty" in lowered and "input" in lowered:
        return "empty_input"
    if "no match" in lowered or "unrecognized" in lowered or "unknown" in lowered:
        return "no_match"
    return "dispatch_failed"


__all__ = [
    "STATE_CHANGING_COMMANDS",
    "map_error",
    "normalize_queue_item",
    "normalize_state_payload",
    "redact",
    "sanitize_player_state",
    "synth_queue_item_id",
    "to_player_state",
]
