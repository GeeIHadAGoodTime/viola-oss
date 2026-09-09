from __future__ import annotations

from models.player import QueueItem
from music.resolution.provider_router import Source


class QueueItemResolver:
    """Normalizes queue-item resolution + metadata fixing."""

    def __init__(self, controller, logger) -> None:
        self._controller = controller
        self._logger = logger.getChild("queue_resolver")

    def resolve(
        self,
        query: str,
        source: Source | str | None,
        metadata: dict[str, Any] | None,
        *,
        engine_manager,
    ) -> QueueItem:
        item = self._controller.resolve_queue_item(
            query,
            source,
            metadata,
            engine_manager=engine_manager,
        )
        self._apply_metadata(item, metadata)
        return item

    def _apply_metadata(
        self,
        item: QueueItem,
        metadata: dict[str, Any] | None,
    ) -> None:
        if not metadata:
            return
        # Read item fields via __dict__ to avoid Pydantic descriptor proxy issues
        item_data = item.__dict__
        extra_data = item.__pydantic_extra__ or {}
        for key, value in metadata.items():
            if key == "url" and value:
                if item_data.get("playback_mode") == "vlc_stream":
                    continue
                validated_url = self._controller.validate_metadata_url(
                    value,
                    allow_stream_urls=True,
                )
                self._safe_set(item, key, validated_url)
            elif key == "title" and value:
                existing = item_data.get(key) or extra_data.get(key)
                if not existing or self._is_url_title(existing):
                    self._safe_set(item, key, value)
            else:
                existing = item_data.get(key) or extra_data.get(key)
                if not existing:
                    self._safe_set(item, key, value)

    @staticmethod
    def _safe_set(item: QueueItem, key: str, value: object) -> None:
        """Set attribute on a QueueItem, bypassing Pydantic descriptor issues."""
        if key in item.model_fields:
            item.__dict__[key] = value
        else:
            if item.__pydantic_extra__ is None:
                object.__setattr__(item, "__pydantic_extra__", {})
            item.__pydantic_extra__[key] = value

    def _is_url_title(self, title: str) -> bool:
        """Check if a title looks like a URL (not a proper track title)."""
        if not title:
            return False
        return title.startswith(("http://", "https://", "www."))


from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    pass
