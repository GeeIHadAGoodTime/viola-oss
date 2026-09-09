"""High-level controller wiring playlist, provider, compliance, and metrics."""

from __future__ import annotations

from typing import Any

from diagnostics.playback_metrics import PlaybackMetricsRecorder
from models.player import QueueItem
from music.backends.loader import BackendStrategyLoader
from music.compliance.ytm_policy import ComplianceContext
from music.resolution.provider_router import Source
from music.runtime import ComplianceService, ProviderOrchestrator, QueueEngine


class MusicPlaybackController:
    """
    Thin orchestration layer that coordinates queue mutations, provider resolution,
    and compliance enforcement. Initially used as a façade over existing helpers;
    future work will move more of MusicPlayer's logic into this class.
    """

    def __init__(
        self,
        *,
        logger: Any,
        playlist: QueueEngine,
        provider_router: ProviderOrchestrator,
        compliance_policy: ComplianceService,
        backend_loader: BackendStrategyLoader,
        metrics: PlaybackMetricsRecorder | None,
    ) -> None:
        self._logger = logger
        self._playlist = playlist
        self._provider_router = provider_router
        self._compliance_policy = compliance_policy
        self._backend_loader = backend_loader
        self._metrics = metrics

    # ------------------------------------------------------------------ #
    # Resolution helpers
    # ------------------------------------------------------------------ #

    def resolve_queue_item(
        self,
        query: str,
        source: Source | None,
        metadata: dict[str, Any] | None,
        *,
        engine_manager: Any | None = None,
    ) -> QueueItem:
        """Delegate to provider router to produce a QueueItem."""
        return self._provider_router.resolve_to_queue_item(
            query,
            source,
            metadata,
            engine_manager=engine_manager,
        )

    def validate_metadata_url(self, url: str, *, allow_stream_urls: bool = False) -> str:
        """Expose ProviderRouter's metadata URL policy for legacy callers."""
        return self._provider_router.validate_metadata_url(url, allow_stream_urls=allow_stream_urls)

    def evaluate_compliance(
        self,
        item: QueueItem,
        *,
        allow_test_override: bool = False,
        auto_fix_playback_mode: bool = True,
    ):
        """Run policy checks for an item."""
        context = ComplianceContext(
            allow_test_override=allow_test_override,
            auto_fix_playback_mode=auto_fix_playback_mode,
        )
        return self._compliance_policy.evaluate(item, context=context)

    @property
    def playlist(self) -> QueueEngine:
        return self._playlist

    @property
    def backend_loader(self) -> BackendStrategyLoader:
        return self._backend_loader

    @property
    def metrics(self) -> PlaybackMetricsRecorder | None:
        return self._metrics

    # ------------------------------------------------------------------ #
    # Queue helpers
    # ------------------------------------------------------------------ #

    def append_to_queue(self, item: QueueItem) -> None:
        self._playlist.append(item)

    def insert_next(self, item: QueueItem) -> None:
        self._playlist.insert_next(item)

    def clear_queue(self) -> None:
        self._playlist.clear_upcoming()

    def remove_from_queue(self, item_id: str) -> bool:
        return self._playlist.remove_upcoming(item_id)

    def reorder_queue(self, from_index: int, to_index: int) -> None:
        self._playlist.reorder_upcoming(from_index, to_index)

    def move_to_next(self, item_id: str) -> bool:
        return self._playlist.move_to_next(item_id)

    def reset_playlist(self, item: QueueItem | None) -> None:
        self._playlist.reset(item)
