"""
diagnostics/playback_stack.py
=============================

Comprehensive playback diagnostics that expose the ACTUAL runtime state of every
layer involved in YouTube playback.

Layers tracked:
- Python backend (player state, position, is_playing)
- Frontend (last received WebSocket state from React/iframe)
- YouTube IFrame API state (forwarded from frontend)

This module stores the last received frontend diagnostics and provides
functions to collect and compare state across all layers.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from core.logging_config import get_logger

logger = get_logger(__name__)


class YouTubePlayerState(int, Enum):
    """YouTube IFrame API player states."""

    UNSTARTED = -1
    ENDED = 0
    PLAYING = 1
    PAUSED = 2
    BUFFERING = 3
    CUED = 5

    @classmethod
    def from_int(cls, value: int) -> YouTubePlayerState:
        """Convert integer to YouTubePlayerState, defaulting to UNSTARTED."""
        try:
            return cls(value)
        except ValueError:
            return cls.UNSTARTED

    @classmethod
    def from_string(cls, value: str) -> YouTubePlayerState:
        """Convert string state name to YouTubePlayerState."""
        mapping = {
            "UNSTARTED": cls.UNSTARTED,
            "ENDED": cls.ENDED,
            "PLAYING": cls.PLAYING,
            "PAUSED": cls.PAUSED,
            "BUFFERING": cls.BUFFERING,
            "CUED": cls.CUED,
        }
        return mapping.get(value.upper(), cls.UNSTARTED)


@dataclass
class FrontendDiagnostics:
    """Diagnostics received from the frontend (React app / YouTube iframe)."""

    # YouTube IFrame API state
    yt_api_loaded: bool = False
    player_exists: bool = False
    player_state: int = -1  # YouTubePlayerState value
    player_state_name: str = "UNKNOWN"
    player_error: int | None = None
    player_error_message: str | None = None

    # Iframe visibility
    iframe_in_dom: bool = False
    iframe_visible: bool = False
    iframe_src: str | None = None

    # Position/duration from iframe
    current_time: float = 0.0
    duration: float = 0.0

    # Video info
    video_id: str | None = None
    video_url: str | None = None

    # Debug tracking fields (for investigating playback issues)
    onReady_called: bool = False
    initial_state_at_ready: int | None = None
    initial_duration_at_ready: float | None = None
    state_history: list = field(default_factory=list)
    wants_to_play: bool = False
    retry_count: int = 0
    tried_muted_start: bool = False

    # Autoplay debug fields (critical for diagnosing why wants_to_play may be false)
    debug_line527_reached: float | None = None
    autoplay_param: int | None = None
    has_playVideo_func: bool | None = None
    autoplay_check_log: str | None = None
    last_error: str | None = None
    last_error_time: float | None = None

    # Enhanced diagnostics - video element and event log
    video_element: dict | None = None  # Real video element state from browser
    event_log: list = field(default_factory=list)  # Chronological event sequence

    # Timestamps
    collected_at: float = 0.0
    received_at: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "yt_api_loaded": self.yt_api_loaded,
            "player_exists": self.player_exists,
            "player_state": self.player_state,
            "player_state_name": self.player_state_name,
            "player_error": self.player_error,
            "player_error_message": self.player_error_message,
            "iframe_in_dom": self.iframe_in_dom,
            "iframe_visible": self.iframe_visible,
            "iframe_src": self.iframe_src,
            "current_time": self.current_time,
            "duration": self.duration,
            "video_id": self.video_id,
            "video_url": self.video_url,
            "onReady_called": self.onReady_called,
            "initial_state_at_ready": self.initial_state_at_ready,
            "initial_duration_at_ready": self.initial_duration_at_ready,
            "state_history": self.state_history,
            "wants_to_play": self.wants_to_play,
            "retry_count": self.retry_count,
            "tried_muted_start": self.tried_muted_start,
            "debug_line527_reached": self.debug_line527_reached,
            "autoplay_param": self.autoplay_param,
            "has_playVideo_func": self.has_playVideo_func,
            "autoplay_check_log": self.autoplay_check_log,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
            "video_element": self.video_element,
            "event_log": self.event_log,
            "collected_at": self.collected_at,
            "received_at": self.received_at,
            "age_seconds": (time.time() - self.received_at if self.received_at > 0 else None),
        }


@dataclass
class BackendDiagnostics:
    """Diagnostics from the Python backend."""

    # Player state
    is_playing: bool = False
    is_paused: bool = False
    position_ms: int = 0
    duration_ms: int = 0
    volume: int = 80

    # Current track
    now_playing_id: str | None = None
    now_playing_title: str | None = None
    now_playing_video_id: str | None = None

    # Queue info
    queue_size: int = 0

    # Backend type
    backend_type: str = "unknown"
    backend_available: bool = False

    # Internal flags
    internal_is_playing: bool | None = None
    internal_paused: bool | None = None

    # Recent errors
    recent_errors: list[str] = field(default_factory=list)

    # Timestamps
    collected_at: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_playing": self.is_playing,
            "is_paused": self.is_paused,
            "position_ms": self.position_ms,
            "duration_ms": self.duration_ms,
            "position_seconds": self.position_ms / 1000.0 if self.position_ms else 0.0,
            "duration_seconds": self.duration_ms / 1000.0 if self.duration_ms else 0.0,
            "volume": self.volume,
            "now_playing_id": self.now_playing_id,
            "now_playing_title": self.now_playing_title,
            "now_playing_video_id": self.now_playing_video_id,
            "queue_size": self.queue_size,
            "backend_type": self.backend_type,
            "backend_available": self.backend_available,
            "internal_is_playing": self.internal_is_playing,
            "internal_paused": self.internal_paused,
            "recent_errors": self.recent_errors,
            "collected_at": self.collected_at,
        }


@dataclass
class ConsistencyReport:
    """Report on consistency between frontend and backend state."""

    all_layers_agree: bool = True
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "all_layers_agree": self.all_layers_agree,
            "issues": self.issues,
            "warnings": self.warnings,
        }


@dataclass
class FullPlaybackState:
    """Combined playback state from all layers."""

    backend: BackendDiagnostics
    frontend: FrontendDiagnostics | None
    consistency: ConsistencyReport
    collected_at: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "backend": self.backend.to_dict(),
            "frontend": self.frontend.to_dict() if self.frontend else None,
            "consistency": self.consistency.to_dict(),
            "collected_at": self.collected_at,
        }


# Global storage for last received frontend diagnostics
_last_frontend_diagnostics: FrontendDiagnostics | None = None
_frontend_diagnostics_lock = threading.Lock()

# Pending diagnostic request (for async waiting)
_diagnostic_request_event: asyncio.Event | None = None
_diagnostic_request_lock = threading.Lock()


def store_frontend_diagnostics(data: dict) -> None:
    """
    Store frontend diagnostics received via WebSocket.

    Called when the frontend sends a diagnostic payload in response
    to a diagnostic request.
    """
    global _last_frontend_diagnostics, _diagnostic_request_event

    diag = FrontendDiagnostics(
        yt_api_loaded=data.get("yt_api_loaded", False),
        player_exists=data.get("player_exists", False),
        player_state=data.get("player_state", -1),
        player_state_name=data.get("player_state_name", "UNKNOWN"),
        player_error=data.get("player_error"),
        player_error_message=data.get("player_error_message"),
        iframe_in_dom=data.get("iframe_in_dom", False),
        iframe_visible=data.get("iframe_visible", False),
        iframe_src=data.get("iframe_src"),
        current_time=data.get("current_time", 0.0),
        duration=data.get("duration", 0.0),
        video_id=data.get("video_id"),
        video_url=data.get("video_url"),
        # Debug tracking fields
        onReady_called=data.get("onReady_called", False),
        initial_state_at_ready=data.get("initial_state_at_ready"),
        initial_duration_at_ready=data.get("initial_duration_at_ready"),
        state_history=data.get("state_history", []),
        wants_to_play=data.get("wants_to_play", False),
        retry_count=data.get("retry_count", 0),
        tried_muted_start=data.get("tried_muted_start", False),
        # Autoplay debug fields
        debug_line527_reached=data.get("debug_line527_reached"),
        autoplay_param=data.get("autoplay_param"),
        has_playVideo_func=data.get("has_playVideo_func"),
        autoplay_check_log=data.get("autoplay_check_log"),
        last_error=data.get("last_error"),
        last_error_time=data.get("last_error_time"),
        video_element=data.get("video_element"),
        event_log=data.get("event_log", []),
        collected_at=data.get("collected_at", 0.0),
        received_at=time.time(),
    )

    with _frontend_diagnostics_lock:
        _last_frontend_diagnostics = diag

    # Signal any waiting requests
    with _diagnostic_request_lock:
        if _diagnostic_request_event is not None:
            try:
                _diagnostic_request_event.set()
            except Exception as _e:
                logger.debug("Diagnostic event signal failed (may be in different loop): %s", _e)

    logger.info("Stored frontend diagnostics: state=%s", diag.player_state_name)


def get_last_frontend_diagnostics() -> FrontendDiagnostics | None:
    """Get the last received frontend diagnostics."""
    with _frontend_diagnostics_lock:
        return _last_frontend_diagnostics


def collect_backend_diagnostics(music: object, state: object = None) -> BackendDiagnostics:
    """
    Collect diagnostics from the Python backend.

    Args:
        music: Music player instance
        state: Optional app state object
    """
    diag = BackendDiagnostics(collected_at=time.time())

    if music is None:
        diag.recent_errors.append("Music player is None")
        return diag

    try:
        # Get backend type
        player = getattr(music, "player", music)
        backend = getattr(player, "_backend", None)
        diag.backend_type = type(backend).__name__ if backend is not None else "unknown"
        diag.backend_available = backend is not None

        # Get internal flags (direct access for diagnostics)
        diag.internal_is_playing = getattr(player, "_is_playing", None)
        diag.internal_paused = getattr(player, "_paused", None)
        diag.position_ms = getattr(player, "_position_ms", 0) or 0
        diag.duration_ms = getattr(player, "_duration_ms", 0) or 0

        # Get player state via state() method
        if hasattr(music, "state"):
            player_state = music.state()
            if hasattr(player_state, "model_dump"):
                player_state = player_state.model_dump()
            elif not isinstance(player_state, dict):
                player_state = {}

            diag.is_playing = player_state.get("is_playing", False)
            diag.is_paused = player_state.get("is_paused", False)
            diag.volume = player_state.get("volume", 80)

            # Get now_playing info
            now_playing = player_state.get("now_playing")
            if isinstance(now_playing, dict):
                diag.now_playing_id = now_playing.get("id")
                diag.now_playing_title = now_playing.get("title")
                diag.now_playing_video_id = now_playing.get("video_id")

            # Queue size
            queue = player_state.get("queue", [])
            diag.queue_size = len(queue) if isinstance(queue, list) else 0

    except Exception as e:
        logger.exception("Failed to collect backend diagnostics")
        diag.recent_errors.append(f"Collection error: {e}")

    return diag


def check_consistency(
    backend: BackendDiagnostics,
    frontend: FrontendDiagnostics | None,
) -> ConsistencyReport:
    """
    Check consistency between backend and frontend state.

    Returns a report identifying any mismatches or issues.
    """
    report = ConsistencyReport()

    if frontend is None:
        report.warnings.append("No frontend diagnostics available (frontend not responding)")
        return report

    # Check if frontend data is stale (older than 5 seconds)
    age = time.time() - frontend.received_at
    if age > 5.0:
        report.warnings.append(f"Frontend diagnostics are stale ({age:.1f}s old)")

    # Check YouTube API loaded
    if not frontend.yt_api_loaded:
        report.all_layers_agree = False
        report.issues.append("YouTube IFrame API not loaded in frontend")

    # Check player exists
    if not frontend.player_exists:
        report.all_layers_agree = False
        report.issues.append("YouTube player instance does not exist in frontend")

    # Check player error
    if frontend.player_error is not None:
        report.all_layers_agree = False
        error_msg = frontend.player_error_message or f"Error code {frontend.player_error}"
        report.issues.append(f"YouTube player error: {error_msg}")

    # Check iframe visibility
    if not frontend.iframe_in_dom:
        report.all_layers_agree = False
        report.issues.append("YouTube iframe not in DOM")
    elif not frontend.iframe_visible:
        report.warnings.append("YouTube iframe is in DOM but not visible")

    # Compare playing state
    backend_playing = backend.is_playing
    frontend_playing = frontend.player_state == YouTubePlayerState.PLAYING.value

    if backend_playing != frontend_playing:
        report.all_layers_agree = False
        backend_state = "PLAYING" if backend_playing else "NOT_PLAYING"
        frontend_state = frontend.player_state_name
        report.issues.append(f"Playing state mismatch: backend={backend_state}, frontend={frontend_state}")

    # Compare video IDs (if both have one)
    if backend.now_playing_video_id and frontend.video_id:
        if backend.now_playing_video_id != frontend.video_id:
            report.all_layers_agree = False
            report.issues.append(
                f"Video ID mismatch: backend={backend.now_playing_video_id}, " f"frontend={frontend.video_id}"
            )

    # Compare position (allow 3 second tolerance)
    if frontend.duration > 0 and backend.duration_ms > 0:
        frontend_pos = frontend.current_time
        backend_pos = backend.position_ms / 1000.0
        diff = abs(frontend_pos - backend_pos)
        if diff > 3.0:
            report.warnings.append(
                f"Position difference: backend={backend_pos:.1f}s, " f"frontend={frontend_pos:.1f}s (diff={diff:.1f}s)"
            )

    return report


def collect_full_playback_state(music: object, state: object = None) -> FullPlaybackState:
    """
    Collect full playback state from all layers.

    This provides a complete snapshot of:
    - Backend state (from Python)
    - Frontend state (last received from WebSocket)
    - Consistency check between layers
    """
    backend = collect_backend_diagnostics(music, state)
    frontend = get_last_frontend_diagnostics()
    consistency = check_consistency(backend, frontend)

    return FullPlaybackState(
        backend=backend,
        frontend=frontend,
        consistency=consistency,
        collected_at=time.time(),
    )


__all__ = [
    "BackendDiagnostics",
    "ConsistencyReport",
    "FrontendDiagnostics",
    "FullPlaybackState",
    "YouTubePlayerState",
    "check_consistency",
    "collect_backend_diagnostics",
    "collect_full_playback_state",
    "get_last_frontend_diagnostics",
    "store_frontend_diagnostics",
]
