"""
Shared contracts for the modular MusicPlayer runtime.

The refactor replaces the monolithic `MusicPlayer` with narrowly scoped
services. These Protocols define the seams between those services while tying
them to the canonical dataclasses in `models.player` and `models.state_manager`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from models.player import PlayerState, QueueItem
from models.state_manager import ConsolidatedState

try:  # Python <3.11 compatibility for typing.Literal
    from typing import Literal
except ImportError:  # pragma: no cover
    from typing import Literal


@dataclass(frozen=True)
class ResolutionRequest:
    """
    Canonical data bundle describing a resolution attempt.

    Attributes:
        query: Raw search string, URL, or provider-prefixed identifier.
        source: Optional pre-declared source hint (e.g., "ytsearch1", "url").
        metadata: Inline metadata provided by upstream intents or UI.
        preferred_provider: Provider slug to prioritize when resolving.
        background_ok: Whether resolution can be deferred to background workers.
        policy_context: Optional compliance context (e.g., prod vs. test mode).
    """

    query: str
    source: Source | None = None
    metadata: dict[str, object] | None = None
    preferred_provider: str | None = None
    background_ok: bool = True
    policy_context: ComplianceContext | None = None


@dataclass(frozen=True)
class ResolutionDecision:
    """
    Normalized result returned by the provider orchestrator.

    Attributes:
        queue_item: Fully hydrated queue item ready for playback.
        provider: Provider slug that produced the item.
        playback_mode: Rendering hint (embedded_webview, vlc_stream, etc.).
        resolver_info: Auxiliary metadata emitted by resolvers.
        route: Optional playback route chosen by the router.
    """

    queue_item: QueueItem
    provider: str | None = None
    playback_mode: str | None = None
    resolver_info: dict[str, object] | None = None
    route: PlaybackRoute | None = None


@dataclass(frozen=True)
class QueueMutationEvent:
    """
    Event emitted whenever the queue mutates.

    Attributes:
        type: Semantic label (enqueue, dequeue, start, complete, failure, etc.).
        item: Queue item involved in the mutation, if any.
        timestamp: Seconds since epoch for worker ordering/metrics.
        reason: Optional reason string (user_skip, autoplay, recovery, ...).
        metadata: Arbitrary key/value payload for workers or metrics.
    """

    type: Literal[
        "enqueue",
        "dequeue",
        "start",
        "complete",
        "failure",
        "reorder",
        "backpressure",
    ]
    item: QueueItem | None
    timestamp: float
    reason: str | None = None
    metadata: dict[str, object] | None = None


@runtime_checkable
class StateService(Protocol):
    """Owns PlayerState/ConsolidatedState mutation and persistence."""

    @property
    def consolidated(self) -> ConsolidatedState:
        """Return the shared ConsolidatedState instance."""
        ...

    def snapshot(self) -> PlayerState:
        """Return a deep copy of the current PlayerState."""
        ...

    def apply_queue_state(
        self,
        *,
        queue: Sequence[QueueItem],
        now_playing: QueueItem | None,
        is_playing: bool,
        backend_name: str | None = None,
        backend_capabilities: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PlayerState:
        """Update the state snapshot after queue mutations."""
        ...

    def set_volume(self, volume: int) -> PlayerState:
        """Update volume and emit the resulting state."""
        ...

    def persist(self, snapshot: PlayerState | None = None) -> None:
        """Persist the provided snapshot (or current state) to disk."""
        ...


@runtime_checkable
class QueueEngine(Protocol):
    """Encapsulates queue/history bookkeeping and emits mutation events."""

    def current(self) -> QueueItem | None: ...

    def current_id(self) -> str | None: ...

    def schedule_current(self) -> bool: ...

    def mark_playing(self) -> QueueItem | None: ...

    def upcoming(self) -> Sequence[QueueItem]: ...

    @property
    def upcoming_ref(self) -> Sequence[QueueItem]: ...

    @property
    def history_ref(self) -> Iterable[QueueItem]: ...

    def queue_size(self) -> int: ...

    def append(self, item: QueueItem) -> None: ...

    def insert_next(self, item: QueueItem) -> None: ...

    def clear_upcoming(self) -> None: ...

    def remove_upcoming(self, item_id: str) -> bool: ...

    def reorder_upcoming(self, from_index: int, to_index: int) -> None: ...

    def move_to_next(self, item_id: str) -> bool: ...

    def rewind(self) -> QueueItem | None: ...

    def complete_current(self, success: bool) -> QueueItem | None: ...

    def snapshot(self) -> tuple[int, str | None]: ...

    def matches(self, token: tuple[int, str | None], item_id: str) -> bool: ...

    def reset(self, item: QueueItem | None) -> None: ...

    def advance_embedded_cursor(self) -> QueueItem | None: ...

    def loop_from_history(self) -> QueueItem | None: ...

    def shuffle_upcoming(self) -> int: ...

    def unshuffle_upcoming(self) -> int: ...

    def set_backpressure(self, origin: str, reason: str) -> dict[str, object]: ...

    def clear_backpressure(self) -> None: ...

    def backpressure_notice(self) -> dict[str, object] | None: ...

    def reset_autoplay_counter(self) -> None: ...

    def increment_autoplay_counter(self) -> int: ...

    def autoplay_additions(self) -> int: ...

    def record_failure(self, item: QueueItem, info: dict[str, object]) -> None: ...

    def has_recent_failure(self, item: QueueItem, ttl_seconds: float) -> bool: ...

    def expire_failures(self, ttl_seconds: float) -> None: ...

    def force_current(self, item: QueueItem | None, *, pending: bool | None = None) -> None: ...

    def replace_history(self, items: Sequence[QueueItem]) -> None: ...

    def replace_upcoming(self, items: Sequence[QueueItem]) -> None: ...


@runtime_checkable
class ProviderOrchestrator(Protocol):
    """
    Resolves user intent into playable queue items and routes providers/backends.
    """

    def resolve(self, request: ResolutionRequest) -> ResolutionDecision:
        """Resolve a query into a queue item."""
        ...

    def refresh(self, queue_item: QueueItem, *, reason: str) -> ResolutionDecision:
        """Re-resolve/refresh metadata for an existing queue item."""
        ...

    def resolve_to_queue_item(
        self,
        query: str,
        source: Source | None,
        metadata: dict[str, object] | None = None,
        *,
        engine_manager: object | None = None,
    ) -> QueueItem: ...

    def validate_metadata_url(self, url: str, *, allow_stream_urls: bool = False) -> str: ...


@runtime_checkable
class ComplianceService(Protocol):
    """Wraps YouTube compliance policy evaluation and telemetry."""

    def evaluate(
        self,
        item: QueueItem,
        context: ComplianceContext,
    ) -> ComplianceVerdict:
        """Return a compliance verdict for the provided queue item."""
        ...

    def record_violation(
        self,
        item: QueueItem,
        violation_type: str,
        metadata: dict[str, object],
    ) -> None:
        """Forward violations to metrics/diagnostics sinks."""


@runtime_checkable
class WorkerManager(Protocol):
    """Owns playback/integrity/background workers and telemetry fan-out."""

    def start(self) -> None:
        """Start all managed workers."""

    def stop(self) -> None:
        """Stop workers and release resources."""

    def emit_event(self, event: QueueMutationEvent) -> None:
        """Deliver queue mutation events to workers."""

    def heartbeat(self) -> None:
        """Record a liveness heartbeat for observability."""


@runtime_checkable
class PlayerControlSurface(Protocol):
    """
    Minimal method surface required by `MusicControllerAdapter`.

    Every runtime implementation (legacy MusicPlayer, new controller, tests)
    must implement these methods so that backend services/UI can remain
    agnostic to the actual player internals.
    """

    on_state_change: Callable[[PlayerState], None] | None

    def play(
        self,
        query: str,
        source: Source | None = ...,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> QueueItem: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self) -> None: ...

    def skip(self) -> dict[str, object] | None: ...

    def previous(self) -> None: ...

    def set_volume(self, value: int) -> int: ...

    def state(self) -> PlayerState | dict[str, object]: ...

    def status(self) -> dict[str, object]: ...

    def emit_state_change(self) -> None: ...

    def clear_queue(self) -> None: ...

    def remove_from_queue(self, item_id: str) -> None: ...

    def reorder_queue(self, from_index: int, to_index: int) -> None: ...

    def play_item_now(self, item_id: str) -> None: ...


if TYPE_CHECKING:  # pragma: no cover - aid static analyzers without runtime cost
    from music.compliance.ytm_policy import ComplianceContext, ComplianceVerdict
    from music.playback.routing import PlaybackRoute
    from music.resolution.provider_router import Source
