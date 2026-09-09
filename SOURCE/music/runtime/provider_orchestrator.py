from __future__ import annotations

from typing import Any, cast

from models.player import QueueItem
from music.resolution.error_handler import ResolutionErrorHandler
from music.resolution.provider_router import (
    ProviderEngineManager,
    ProviderRouter,
    Source,
)
from music.runtime.contracts import (
    ProviderOrchestrator,
    ResolutionDecision,
    ResolutionRequest,
)


class ProviderOrchestratorService(ProviderOrchestrator):
    """
    Wrapper around `ProviderRouter` that exposes the runtime contract.

    The implementation keeps the legacy router intact while giving the refactor
    a stable seam to depend on.
    """

    def __init__(
        self,
        *,
        logger: Any,
        resolver: Any | None,
        background_resolver: Any | None,
        background_available: bool,
        error_handler: ResolutionErrorHandler,
        record_resolution_failure,
        background_worker: Any | None = None,
    ) -> None:
        self._router = ProviderRouter(
            logger=logger,
            resolver=resolver,
            background_resolver=background_resolver,
            background_available=background_available,
            error_handler=error_handler,
            record_resolution_failure=record_resolution_failure,
            background_worker=background_worker,
        )

    # Legacy-facing helpers ------------------------------------------------- #

    def resolve_to_queue_item(
        self,
        query: str,
        source: Source | None,
        metadata: dict[str, Any] | None = None,
        *,
        engine_manager: Any | None = None,
    ) -> QueueItem:
        return self._router.resolve_to_queue_item(
            query,
            source,
            metadata,
            engine_manager=engine_manager,
        )

    def validate_metadata_url(self, url: str, *, allow_stream_urls: bool = False) -> str:
        return self._router.validate_metadata_url(
            url,
            allow_stream_urls=allow_stream_urls,
        )

    # Contract implementation ----------------------------------------------- #

    def resolve(self, request: ResolutionRequest) -> ResolutionDecision:
        # Cast engine_manager from metadata to expected type
        engine_mgr = cast(
            ProviderEngineManager | None,
            request.metadata.get("engine_manager") if request.metadata else None,
        )
        queue_item = self._router.resolve_to_queue_item(
            request.query,
            request.source,
            request.metadata,
            engine_manager=engine_mgr,
        )
        return ResolutionDecision(
            queue_item=queue_item,
            provider=getattr(queue_item, "provider", None),
            playback_mode=getattr(queue_item, "playback_mode", None),
            resolver_info=getattr(queue_item, "resolver_info", None),
        )

    def refresh(self, queue_item: QueueItem, *, reason: str) -> ResolutionDecision:
        # Legacy router does not support refresh semantics yet; return pass-through.
        return ResolutionDecision(
            queue_item=queue_item,
            provider=getattr(queue_item, "provider", None),
            playback_mode=getattr(queue_item, "playback_mode", None),
            resolver_info=getattr(queue_item, "resolver_info", None),
        )
