from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger
from music.exceptions import ConfigurationError, InvalidOperation
from music.providers.checker import (
    get_youtube_unavailable_reason,
    is_provider_linked,
    is_youtube_track_requiring_provider,
)
from ui.core.player_state import to_player_state as _to_player_state

log = get_logger(__name__)


class _Hub(Protocol):
    async def broadcast(
        self,
        event: str,
        payload: JsonDict,
        *,
        user_id: str | None = None,
        force: bool = False,
    ) -> None: ...


class _SupportsClearQueue(Protocol):
    def clear_queue(self) -> None: ...


class _SupportsReorderQueue(Protocol):
    def reorder_queue(self, from_index: int, to_index: int) -> None: ...


class _SupportsPlayItemNow(Protocol):
    def play_item_now(self, item_id: str) -> None: ...


class _SupportsRemoveFromQueue(Protocol):
    def remove_from_queue(self, item_id: str) -> None: ...


class QueueServiceError(Exception):
    """Base class for queue service related errors."""

    status_code: int = 400
    error_code: str = "queue_error"
    message: str = "Queue operation failed"

    def __init__(
        self,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        if status_code is not None:
            self.status_code = status_code
        if error_code is not None:
            self.error_code = error_code
        if message is not None:
            self.message = message
        super().__init__(self.message)

    def to_payload(self) -> JsonDict:
        return {"ok": False, "error": self.error_code, "message": self.message}


class QueueCapabilityError(QueueServiceError):
    status_code = 501
    error_code = "not_supported"
    message = "Queue operation not supported by current music backend"


class QueueItemNotFound(QueueServiceError):
    status_code = 404
    error_code = "item_not_found"
    message = "Requested queue item was not found"


class QueueIndexError(QueueServiceError):
    status_code = 400
    error_code = "invalid_index"
    message = "Invalid queue index"


class QueueProtectedItem(QueueServiceError):
    status_code = 400
    error_code = "cannot_remove_current_item"
    message = "Cannot remove currently playing item; use skip instead"


@dataclass
class QueueService:
    """Service layer wrapper around music queue operations."""

    music: object
    state: object
    hub: _Hub
    app: object | None = None

    async def clear(self) -> JsonDict:
        if not hasattr(self.music, "clear_queue"):
            raise QueueCapabilityError()
        cast(_SupportsClearQueue, self.music).clear_queue()
        await self._broadcast_state()
        return {"ok": True, "error": None}

    async def reorder(self, from_index: int | None, to_index: int | None) -> JsonDict:
        if from_index is None or to_index is None:
            raise QueueIndexError(message="missing_indices")

        if not hasattr(self.music, "reorder_queue"):
            raise QueueCapabilityError(error_code="reorder_not_supported")

        log.info("Reordering queue: moving %s -> %s", from_index, to_index)
        try:
            cast(_SupportsReorderQueue, self.music).reorder_queue(from_index, to_index)
        except ValueError as exc:
            raise QueueIndexError(message=str(exc)) from exc

        await self._broadcast_state()
        return {"ok": True, "error": None}

    async def play_item(self, item_id: str | None) -> JsonDict:
        if not item_id:
            raise QueueServiceError(
                status_code=400,
                error_code="missing_item_id",
                message="Queue item id missing",
            )

        if not hasattr(self.music, "play_item_now"):
            raise QueueCapabilityError(error_code="play_not_supported")

        log.info("Playing queue item: %s", item_id)
        try:
            cast(_SupportsPlayItemNow, self.music).play_item_now(item_id)
        except ConfigurationError as exc:
            # YouTube provider not linked or disabled
            raise QueueServiceError(
                status_code=403,
                error_code="provider_not_linked",
                message=str(exc),
            ) from exc
        except ValueError as exc:
            raise QueueItemNotFound(message=str(exc)) from exc

        await self._broadcast_state()
        return {"ok": True, "error": None}

    async def remove_item(self, item_id: str) -> JsonDict:
        if not hasattr(self.music, "remove_from_queue"):
            raise QueueCapabilityError()

        try:
            cast(_SupportsRemoveFromQueue, self.music).remove_from_queue(item_id)
        except InvalidOperation as exc:
            raise QueueProtectedItem(message=str(exc)) from exc
        except ValueError as exc:
            raise QueueItemNotFound(message=str(exc)) from exc

        await self._broadcast_state()
        return {"ok": True, "error": None}

    async def snapshot(self) -> JsonDict:
        hub_state = getattr(self.app, "state", None) if self.app else None
        hub_authority = getattr(hub_state, "hub_state_authority", None) if hub_state is not None else None
        player_state = _to_player_state(self.music, self.state, hub_authority=hub_authority)

        now_playing: JsonDict | None = None
        if player_state.now_playing is not None:
            now_value = to_json_value(player_state.now_playing.model_dump())
            now_playing = now_value if isinstance(now_value, dict) else None

        # Mark unavailable YouTube tracks
        queue_items = []
        for item in player_state.queue:
            item_value = to_json_value(item.model_dump())
            item_dict: JsonDict = item_value if isinstance(item_value, dict) else {}
            # Check if this is a YouTube track that requires provider
            if is_youtube_track_requiring_provider(item):
                if not is_provider_linked("youtube_music"):
                    item_dict["unavailable"] = True
                    item_dict["unavailable_reason"] = get_youtube_unavailable_reason()
            queue_items.append(item_dict)

        return {
            "ok": True,
            "is_playing": player_state.is_playing,
            "now_playing": now_playing,
            "current": now_playing,
            "queue": to_json_value(queue_items),
            "queue_size": len(queue_items),
            "error": None,
        }

    async def _broadcast_state(self) -> None:
        try:
            user_id: str | None
            try:
                from core.user_context import get_current_user_id

                user_id = get_current_user_id()
            except LookupError:
                user_id = None

            hub_state = getattr(self.app, "state", None) if self.app else None
            hub_authority = getattr(hub_state, "hub_state_authority", None) if hub_state is not None else None
            payload_value = to_json_value(
                _to_player_state(self.music, self.state, hub_authority=hub_authority).model_dump()
            )
            payload: JsonDict = payload_value if isinstance(payload_value, dict) else {}
            # CRITICAL: Use force=True to bypass 250ms throttle for queue operations
            # Without this, rapid queue ops (remove, clear, reorder) get silently dropped
            await self.hub.broadcast("state", payload, user_id=user_id, force=True)
        except Exception as exc:
            log.debug("Queue broadcast failure: %s", exc)
