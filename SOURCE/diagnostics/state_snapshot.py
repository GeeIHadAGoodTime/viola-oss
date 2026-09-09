"""
diagnostics/state_snapshot.py
================================

Persistent runtime snapshotting utilities used to capture rotating JSON
artifacts for post-mortem analysis. Snapshots combine application state,
music/voice pipeline summaries, runtime metrics, and thread inventories.

State Access:
    App state is read from the canonical StateHub via selectors.
    Music/voice state is provided via Protocol interfaces for components
    not yet integrated with StateHub.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeAlias, runtime_checkable

from core.constants import TIMEOUT_DEFAULT
from core.json_types import JsonDict, JsonValue, to_json_value
from core.logging_config import get_logger
from core.platform import get_logs_dir
from diagnostics.runtime_metrics import get_runtime_metrics

logger = get_logger(__name__)


class _ModelDumpable(Protocol):
    def model_dump(self) -> JsonDict: ...


_MusicSnapshot = JsonValue | _ModelDumpable


# NOTE: SnapshotState Protocol removed - now reading from canonical StateHub


@runtime_checkable
class SnapshotMusicStateful(Protocol):
    def state(self) -> _MusicSnapshot: ...


@runtime_checkable
class SnapshotMusicStatusful(Protocol):
    def status(self) -> _MusicSnapshot: ...


SnapshotMusic: TypeAlias = SnapshotMusicStateful | SnapshotMusicStatusful


class _VoicePipelineConfig(Protocol):
    @property
    def wake_words(self) -> Sequence[str]: ...

    @property
    def wake_sensitivity(self) -> float | None: ...

    @property
    def wake_engine(self) -> str | None: ...


class _VoicePipeline(Protocol):
    @property
    def config(self) -> _VoicePipelineConfig: ...


class SnapshotVoice(Protocol):
    @property
    def wake_enabled(self) -> bool: ...

    @property
    def capabilities(self) -> dict[str, JsonValue]: ...

    @property
    def voice_pipeline(self) -> _VoicePipeline: ...


class _SnapshotStore(Protocol):
    def record_snapshot(self, snapshot_type: str, payload: JsonDict, *, max_files: int = 96) -> Path: ...


# Optional state store import - module may not be available in minimal installs
_get_state_store_fn: Callable[[], _SnapshotStore] | None = None

try:
    from services.persistence.state_store import (
        get_state_store as _imported_get_state_store,
    )

    _get_state_store_fn = _imported_get_state_store
except Exception:  # pragma: no cover - optional dependency
    pass

SNAPSHOT_VERSION = "1.1.0"  # Version bump: reads from StateHub
DEFAULT_SNAPSHOT_DIR = get_logs_dir() / "state_snapshots"


@dataclass(slots=True)
class SnapshotContext:
    """Context for snapshot capture - music and voice components."""

    music: SnapshotMusic
    voice: SnapshotVoice | None = None


class PersistentSnapshotter:
    """
    Periodically persists runtime state snapshots with rotation and versioning.

    Args:
        output_dir: Directory where snapshots are written.
        interval: Desired cadence between snapshots in seconds.
        max_snapshots: Maximum number of on-disk snapshots to retain.
        min_interval: Lower bound guard for snapshot cadence (allows fast-test overrides).
    """

    def __init__(
        self,
        *,
        output_dir: Path = DEFAULT_SNAPSHOT_DIR,
        interval: float = 30.0,
        max_snapshots: int = 20,
        min_interval: float = 5.0,
    ) -> None:
        self._output_dir = output_dir
        if interval <= 0:
            raise ValueError("interval must be greater than 0")
        if min_interval <= 0:
            raise ValueError("min_interval must be greater than 0")
        safe_min_interval = max(0.01, float(min_interval))
        self._interval = max(safe_min_interval, float(interval))
        self._max_snapshots = max(1, int(max_snapshots))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._context: SnapshotContext | None = None
        self._runtime_metrics = get_runtime_metrics()
        self._io_lock = threading.RLock()

    # ------------------------------------------------------------------ public
    def start(
        self,
        *,
        music: SnapshotMusic,
        voice: SnapshotVoice | None = None,
        state: object | None = None,  # DEPRECATED: ignored, reads from StateHub
    ) -> None:
        """
        Start snapshotting with the provided runtime components.

        App state is read from the canonical StateHub.
        Music and voice state are provided via their respective protocols.

        Args:
            music: Music controller implementing SnapshotMusic protocol
            voice: Optional voice orchestrator implementing SnapshotVoice protocol
            state: DEPRECATED - ignored, app state is read from StateHub
        """
        # In test mode, do not start background snapshot thread to avoid hanging tests
        from config.settings import settings

        if state is not None:
            logger.debug("PersistentSnapshotter: 'state' parameter deprecated, reading from StateHub")

        if settings.test_mode or settings.pytest_in_progress:
            logger.debug("PersistentSnapshotter disabled in test mode")
            self._context = SnapshotContext(music=music, voice=voice)
            return
        self._context = SnapshotContext(music=music, voice=voice)
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="state-snapshotter",
            daemon=True,
        )
        self._thread.start()
        logger.debug(
            "State snapshotter started (dir=%s, interval=%ss)",
            self._output_dir,
            self._interval,
        )

    def stop(self) -> None:
        """Stop the snapshot thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=TIMEOUT_DEFAULT)
        self._thread = None

    def capture_once(self) -> Path:
        """Capture a snapshot immediately (synchronous)."""
        if self._context is None:
            raise RuntimeError("Snapshot context not initialized")
        snapshot = self._build_snapshot(self._context)
        with self._io_lock:
            return self._write_snapshot(snapshot)

    # ---------------------------------------------------------------- internal
    def _run(self) -> None:
        if self._context is None:
            logger.debug("Snapshot context missing; aborting snapshotter loop.")
            return
        while not self._stop_event.wait(self._interval):
            try:
                snapshot = self._build_snapshot(self._context)
                with self._io_lock:
                    self._write_snapshot(snapshot)
            except Exception as e:
                logger.debug("State snapshot capture failed (non-critical): %s", e, exc_info=True)

    def _build_snapshot(self, context: SnapshotContext) -> JsonDict:
        timestamp = datetime.utcnow().isoformat() + "Z"
        metrics_value = to_json_value(self._runtime_metrics.snapshot())
        metrics_snapshot: JsonDict
        if isinstance(metrics_value, dict):
            metrics_snapshot = metrics_value
        else:
            metrics_snapshot = {"value": metrics_value}

        process_value = metrics_snapshot.get("process")
        process_snapshot: JsonDict = process_value if isinstance(process_value, dict) else {}

        snapshot: JsonDict = {
            "version": SNAPSHOT_VERSION,
            "generated_at": timestamp,
            "process": process_snapshot,
            "metrics": metrics_snapshot,
            "app_state": self._extract_app_state_from_hub(),
            "music": self._extract_music_state(context.music),
            "voice": self._extract_voice_state(context.voice),
            "threads": self._collect_threads(),
        }
        return snapshot

    def _write_snapshot(self, snapshot: JsonDict) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S.%f")
        final_path = self._output_dir / f"snapshot-{timestamp}.json"
        tmp_path = final_path.with_suffix(".json.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(final_path)
            self._rotate_snapshots()
            if _get_state_store_fn is not None:
                try:
                    store = _get_state_store_fn()
                    store.record_snapshot("runtime", snapshot)
                except Exception as e:
                    logger.debug(
                        "Failed to mirror snapshot to persistent store (non-critical): %s",
                        e,
                        exc_info=True,
                    )
            return final_path
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()

    def _rotate_snapshots(self) -> None:
        snapshots = sorted(
            self._output_dir.glob("snapshot-*.json"),
            key=lambda path: path.stat().st_mtime,
        )
        excess = len(snapshots) - self._max_snapshots
        for path in snapshots[: max(0, excess)]:
            with contextlib.suppress(OSError):
                path.unlink()

    # ---------------------------------------------------------------- helpers
    def _extract_app_state_from_hub(self) -> JsonDict:
        """Extract app state from the canonical StateHub via selectors."""
        try:
            from core.state_selectors import (
                select_is_listening,
                select_is_playing,
                select_last_transcript,
                select_session_id,
                select_voice_summary,
            )

            voice_summary = select_voice_summary()
            return {
                "is_listening": select_is_listening(),
                "is_playing": select_is_playing(),
                "last_transcript": select_last_transcript(),
                "session_id": select_session_id(),
                "voice_mode": voice_summary.get("mode"),
                "wake_enabled": voice_summary.get("wake_enabled"),
                "wake_active": voice_summary.get("wake_active"),
                "is_ducked": voice_summary.get("is_ducked"),
            }
        except Exception as e:
            logger.debug("Failed to extract state from hub: %s", e)
            return {"error": "hub_unavailable"}

    def _extract_music_state(self, music: SnapshotMusic) -> JsonDict:
        state: _MusicSnapshot
        try:
            # Diagnostics snapshots run outside any HTTP request; bind the
            # desktop active principal so the player state read resolves to
            # the install's real owner instead of raising LookupError.
            from core.user_context import desktop_active_principal_scope

            with desktop_active_principal_scope():
                if isinstance(music, SnapshotMusicStateful):
                    state = music.state()
                elif isinstance(music, SnapshotMusicStatusful):
                    state = music.status()
                else:
                    return {}
        except Exception as e:
            logger.debug("Failed to capture music state (non-critical): %s", e, exc_info=True)
            return {"error": "capture_failed"}

        try:
            if isinstance(state, dict):
                return state
            if hasattr(state, "model_dump"):
                dumped = state.model_dump()
                if isinstance(dumped, dict):
                    return dumped

            value = to_json_value(state)
            return value if isinstance(value, dict) else {"value": value}
        except Exception as e:
            logger.debug("Failed to capture music state (non-critical): %s", e, exc_info=True)
            return {"error": "capture_failed"}

    def _extract_voice_state(self, voice: SnapshotVoice | None) -> JsonDict:
        if voice is None:
            return {}
        payload: JsonDict = {
            "wake_enabled": voice.wake_enabled,
            "capabilities": dict(voice.capabilities),
        }
        with contextlib.suppress(Exception):
            config = voice.voice_pipeline.config
            payload["wake_config"] = {
                "wake_words": list(config.wake_words),
                "wake_sensitivity": config.wake_sensitivity,
                "wake_engine": config.wake_engine,
            }
        return payload

    def _collect_threads(self) -> JsonDict:
        threads_info: list[JsonValue] = []
        for thread in threading.enumerate():
            threads_info.append(
                {
                    "name": thread.name,
                    "daemon": thread.daemon,
                    "alive": thread.is_alive(),
                    "ident": thread.ident,
                }
            )
        return {"count": len(threads_info), "threads": threads_info}


__all__ = ["SNAPSHOT_VERSION", "PersistentSnapshotter"]
