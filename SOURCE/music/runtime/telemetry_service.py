from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Protocol, cast

from core.json_types import JsonDict, JsonObject, JsonValue, to_json_value
from diagnostics.failure_envelope import emit_failure
from diagnostics.playback_metrics import PlaybackMetricsRecorder
from diagnostics.runtime_metrics import get_runtime_metrics
from models.player import PlayerState, QueueItem
from music.compliance.youtube_tos import ViolationType
from music.errors.envelope import PlaybackErrorEnvelope
from music.exceptions import ResolutionError
from music.providers.errors import (
    MusicProviderUnavailableError,
    MusicTrackNotFoundError,
)
from music.resolution.provider_router import Source

from .compliance_service import YouTubeComplianceService
from .contracts import QueueEngine


class _RuntimeMetrics(Protocol):
    def heartbeat(self, channel: str, **fields: object) -> None: ...

    def record_queue_drift(
        self,
        *,
        expected_queue_len: int,
        actual_queue_len: int,
        now_playing_track_id: str | None,
    ) -> None: ...

    def record_vlc_status(
        self,
        *,
        healthy: bool,
        track: str | None,
        position_seconds: float,
        queue_position: int,
    ) -> None: ...

    def record_music_backend_restart(self, reason: str) -> None: ...

    def record_music_buffer_underrun(self) -> None: ...

    def record_music_heartbeat_miss(self, source: str) -> None: ...

    def record_music_queue_failure(self, stage: str) -> None: ...

    def snapshot(self) -> object: ...


class RuntimeTelemetryService:
    """
    Aggregates runtime metrics, diagnostics envelopes, and debug emission.

    This service centralizes the legacy `_emit`, `_record_queue_failure`, and
    `_record_resolution_failure` helpers so the `MusicPlayer` can shrink into a
    thin coordinator.
    """

    def __init__(
        self,
        *,
        state_service,
        queue_engine: QueueEngine,
        lock: Lock,
        logger: logging.Logger,
        emit_debug_event: Callable[..., None] | None = None,
        runtime_metrics: _RuntimeMetrics | None = None,
        playback_metrics: PlaybackMetricsRecorder | None = None,
    ) -> None:
        self._state_service = state_service
        self._queue_engine = queue_engine
        self._lock = lock
        self._logger = logger.getChild("telemetry")
        self._emit_debug_event = emit_debug_event
        self._metrics: _RuntimeMetrics = runtime_metrics or cast(_RuntimeMetrics, get_runtime_metrics())
        self._playback_metrics = playback_metrics or PlaybackMetricsRecorder()
        self._recent_resolution_errors: deque[JsonDict] = deque(maxlen=10)
        self._last_resolution_error: JsonDict | None = None
        self._last_queue_error_info: JsonDict | None = None
        self._last_emitted_snapshot: PlayerState | None = None
        self._last_emitted_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Public accessors                                                   #
    # ------------------------------------------------------------------ #

    @property
    def playback_metrics(self) -> PlaybackMetricsRecorder:
        return self._playback_metrics

    @property
    def last_emit_timestamp(self) -> float:
        return self._last_emitted_at

    def last_resolution_error(self) -> JsonDict | None:
        with self._lock:
            if self._last_resolution_error is None:
                return None
            return dict(self._last_resolution_error)

    def resolution_failure_history(self) -> list[JsonDict]:
        with self._lock:
            return [dict(entry) for entry in self._recent_resolution_errors]

    def last_queue_error(self) -> JsonDict | None:
        with self._lock:
            if self._last_queue_error_info is None:
                return None
            return dict(self._last_queue_error_info)

    def runtime_snapshot(self) -> JsonDict:
        snapshot_method = getattr(self._metrics, "snapshot", None)
        if callable(snapshot_method):
            result = snapshot_method()
            snapshot_value = to_json_value(result)
            if isinstance(snapshot_value, dict):
                return snapshot_value
            return {}
        return {}

    # ------------------------------------------------------------------ #
    # Metric helpers                                                     #
    # ------------------------------------------------------------------ #

    def heartbeat(self, channel: str, **fields: JsonValue) -> None:
        self._metrics.heartbeat(channel, **fields)

    def record_queue_drift(
        self,
        *,
        expected_queue_len: int,
        actual_queue_len: int,
        now_playing_track_id: str | None,
    ) -> None:
        self._metrics.record_queue_drift(
            expected_queue_len=expected_queue_len,
            actual_queue_len=actual_queue_len,
            now_playing_track_id=now_playing_track_id,
        )

    def record_vlc_status(
        self,
        *,
        healthy: bool,
        track: str | None,
        position_seconds: float,
        queue_position: int,
    ) -> None:
        self._metrics.record_vlc_status(
            healthy=healthy,
            track=track,
            position_seconds=position_seconds,
            queue_position=queue_position,
        )

    def record_backend_restart(self, *, reason: str, count: int | None = None) -> None:
        self._metrics.record_music_backend_restart(reason=reason)
        self._metrics.heartbeat(
            "music.backend",
            status="restarting",
            reason=reason,
            count=count,
        )

    def record_buffer_underrun(self) -> None:
        self._metrics.record_music_buffer_underrun()

    def record_heartbeat_miss(self, *, source: str) -> None:
        self._metrics.record_music_heartbeat_miss(source=source)

    def record_play_request(self) -> None:
        self._playback_metrics.inc_counter("viola_play_requests_total")

    def record_track_completion(self, duration: float | None) -> None:
        self._playback_metrics.inc_counter("viola_tracks_completed_total")
        if duration is not None:
            self._playback_metrics.record_duration(duration)

    def emit_debug_event(self, event: str, payload: JsonObject, *, source: str = "music_player") -> None:
        if self._emit_debug_event is None:
            return
        try:
            self._emit_debug_event(event, payload, source)
        except Exception:  # pragma: no cover - defensive
            self._logger.exception("emit_debug_event failed")

    # ------------------------------------------------------------------ #
    # Player snapshot + failure helpers                                  #
    # ------------------------------------------------------------------ #

    def emit_player_state(
        self,
        *,
        snapshot: PlayerState,
        playlist_current_id: str | None,
        upcoming_count: int,
        backend: object | None,
        backend_name: str | None,
        on_state_change: Callable[[PlayerState], None] | None,
    ) -> None:
        self._state_service.persist(snapshot)
        now_ts = time.time()
        actual_queue_len = upcoming_count + (1 if playlist_current_id else 0)
        expected_queue_len = len(snapshot.queue) + (1 if snapshot.now_playing is not None else 0)
        now_playing_id = self._extract_track_id(snapshot.now_playing)
        target_track_id = playlist_current_id or now_playing_id

        self.record_queue_drift(
            expected_queue_len=expected_queue_len,
            actual_queue_len=actual_queue_len,
            now_playing_track_id=target_track_id,
        )

        backend_position = float(snapshot.position or 0)
        backend_healthy = backend is not None
        if backend is not None and hasattr(backend, "get_position"):
            try:
                backend_position = float(backend.get_position() or 0.0)
            except Exception as e:
                self._logger.debug("get_position failed during health check (non-critical): %s", e)
                backend_healthy = False

        self.record_vlc_status(
            healthy=backend_healthy,
            track=target_track_id,
            position_seconds=backend_position,
            queue_position=actual_queue_len,
        )
        self.heartbeat(
            "music.player",
            status="playing" if snapshot.is_playing else "idle",
            queue_length=actual_queue_len,
            track_id=target_track_id,
            backend=backend_name,
        )

        with self._lock:
            self._last_emitted_snapshot = snapshot
            self._last_emitted_at = now_ts

        self.emit_debug_event(
            "music_engine_heartbeat",
            {
                "status": "playing" if snapshot.is_playing else "idle",
                "queue_length": actual_queue_len,
                "track_id": target_track_id,
                "backend": backend_name,
            },
            source="music_player",
        )

        if on_state_change:
            try:
                on_state_change(snapshot)
            except Exception:  # pragma: no cover - user callback
                self._logger.exception("on_state_change callback failed")

    def record_queue_failure(
        self,
        item: QueueItem,
        *,
        stage: str,
        metadata: JsonDict | None = None,
    ) -> None:
        metadata_payload: JsonDict = metadata or {}
        info: JsonDict = {
            "item_id": item.id,
            "title": item.title,
            "stage": stage,
            "metadata": metadata_payload,
            "timestamp": time.time(),
        }
        envelope = PlaybackErrorEnvelope(
            code=f"QUEUE_{stage.upper()}",
            message=f"Playback stalled during {stage.replace('_', ' ')}.",
            severity="warning",
            retryable=True,
            origin="queue",
            context=info,
        )
        self._push_playback_error(envelope)
        self._playback_metrics.inc_error("viola_queue_failures_total")
        self._playback_metrics.inc_error(f"viola_queue_failure_{stage}")
        self._metrics.record_music_queue_failure(stage)
        emit_failure(
            envelope.code,
            "music.player.queue",
            message=envelope.message,
            item_id=item.id,
            title=item.title,
            stage=stage,
            metadata=metadata_payload,
            severity=envelope.severity.upper(),
            retryable=envelope.retryable,
        )
        self._queue_engine.record_failure(item, {k: v for k, v in info.items()})
        with self._lock:
            self._last_queue_error_info = info

    def record_resolution_failure(
        self,
        *,
        query: str,
        source: Source,
        exc: Exception,
    ) -> None:
        context_payload: JsonDict = {
            "query": query,
            "source": str(source),
            "error": repr(exc),
            "exception_type": exc.__class__.__name__,
            "timestamp": time.time(),
            "provider": "youtube_music",
        }
        if isinstance(exc, ResolutionError):
            context_value = to_json_value(exc.context)
            if isinstance(context_value, dict):
                context_payload.update(context_value)

        if isinstance(exc, (MusicProviderUnavailableError, MusicTrackNotFoundError)) and hasattr(
            exc, "technical_details"
        ):
            technical_value = to_json_value(exc.technical_details)
            context_payload["technical_details"] = technical_value
            root_cause = (
                technical_value.get("root_cause", "unknown") if isinstance(technical_value, dict) else "unknown"
            )
            log_level = self._logger.info if isinstance(exc, MusicTrackNotFoundError) else self._logger.warning
            log_level(
                "Resolution failed for %s (%s): root_cause=%s error=%s technical_details=%s",
                query[:50],
                source,
                root_cause,
                str(exc),
                exc.technical_details,
            )

        envelope = PlaybackErrorEnvelope.from_exception(
            exc,
            code=getattr(exc, "code", None),
            origin="resolver",
            context=context_payload,
        )
        self._push_playback_error(envelope)

        record = dict(context_payload)
        record["envelope"] = envelope.to_dict()

        with self._lock:
            self._last_resolution_error = record
            self._recent_resolution_errors.append(record)

        self._playback_metrics.inc_error("viola_resolution_failures_total")
        self._logger.warning(
            "Resolution failed for %s (%s): %s",
            query,
            source,
            context_payload["exception_type"],
        )
        emit_failure(
            envelope.code,
            "music.player.resolver",
            message=envelope.message,
            exc=exc,
            severity=envelope.severity.upper(),
            retryable=envelope.retryable,
            details=context_payload,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _push_playback_error(self, envelope: PlaybackErrorEnvelope) -> None:
        payload = envelope.to_dict()
        with self._lock:
            state = self._state_service.state
            existing = [dict(item) for item in getattr(state, "playback_errors", []) if isinstance(item, dict)]
            existing.append(payload)
            state.playback_errors = existing[-5:]
            metadata = dict(state.metadata or {})
            metadata["last_error"] = payload
            state.metadata = metadata

    def _extract_track_id(self, item: QueueItem | None) -> str | None:
        if item is None:
            return None
        try:
            return item.id
        except Exception as e:
            self._logger.debug("Failed to extract track id (non-critical): %s", e)
            getter = getattr(item, "get", None)
            if callable(getter):
                result = getter("id", None)
                return str(result) if result is not None else None
        return getattr(item, "id", None)


class ComplianceEventBridge:
    """
    Wires YouTube compliance callbacks into the telemetry surface.
    """

    def __init__(
        self,
        *,
        telemetry: RuntimeTelemetryService,
        logger: logging.Logger,
    ) -> None:
        self._telemetry = telemetry
        self._logger = logger.getChild("compliance_bridge")
        self.service = YouTubeComplianceService(
            logger=logger,
            violation_callback=self._handle_violation,
        )

    def _handle_violation(
        self,
        item: QueueItem,
        violation_type: ViolationType,
        details: Mapping[str, object],
    ) -> None:
        stage_map = {
            ViolationType.MISSING_VIDEO_ID: "missing_video_id_tos_violation",
            ViolationType.INVALID_PLAYBACK_MODE: "invalid_playback_mode_tos_violation",
            ViolationType.VLC_BACKEND_ATTEMPTED: "vlc_backend_blocked_tos",
            ViolationType.EXTERNAL_BROWSER_ATTEMPTED: "external_browser_blocked_tos",
        }
        stage = stage_map.get(violation_type, "tos_violation")
        details_value = to_json_value(details)
        details_payload: JsonDict = details_value if isinstance(details_value, dict) else {}
        metadata: JsonDict = {
            "error": violation_type.value,
            "reason": details_payload.get("reason", "TOS compliance violation"),
            "tos_compliance": "violated",
            **details_payload,
        }
        self._telemetry.record_queue_failure(item, stage=stage, metadata=metadata)
        self._logger.warning(
            "TOS violation recorded stage=%s item_id=%s reason=%s",
            stage,
            getattr(item, "id", None),
            metadata.get("reason"),
        )


__all__ = ["ComplianceEventBridge", "RuntimeTelemetryService"]
