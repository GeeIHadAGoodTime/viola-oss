"""
Base API client infrastructure and shared utilities.
Contains Qt thread pool, session management, and common HTTP helpers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

import requests

from config import env, settings
from contracts.api_response import (
    ResponseContractError,
    ResponseEnvelope,
    ResponseError,
    ensure_envelope,
)
from core.logging_config import get_logger

# Import security config for API key
try:
    from ui.security.bootstrap import load_bootstrap_api_key
    from ui.security.config import get_security_config

    _SECURITY_AVAILABLE = True
except ImportError:
    _SECURITY_AVAILABLE = False
    get_security_config = None  # Fallback when security module unavailable
    load_bootstrap_api_key = None  # Fallback when security module unavailable

if TYPE_CHECKING:
    from PySide6.QtCore import (
        QObject,
        QRunnable,
        QThreadPool,
        Signal,
        SignalInstance,
    )
else:
    from PySide6.QtCore import (
        QObject,
        QRunnable,
        QThreadPool,
        Signal,
        SignalInstance,
    )

QT_AVAILABLE = True


# TypedDict definitions for API responses
class NowPlayingTrack(TypedDict, total=False):
    """Structure for now playing track data."""

    video_id: str
    title: str
    artist: str
    thumbnail: str
    duration: int


class QueueItem(TypedDict, total=False):
    """Structure for queue item data."""

    id: str
    video_id: str
    title: str
    artist: str
    thumbnail: str
    duration: int


class PlayerStateData(TypedDict, total=False):
    """Structure for player state API response data."""

    is_playing: bool
    now_playing: NowPlayingTrack | None
    queue: list[QueueItem]
    volume: int
    position: int
    duration: int
    position_percentage: float


class QueueResponse(TypedDict, total=False):
    """Structure for queue API response data."""

    queue: list[QueueItem]
    current_index: int


class DebugEventPayload(TypedDict, total=False):
    """Structure for debug event payloads."""

    client: str
    signal_name: str
    source: str


class EmitDebugEvent(Protocol):
    def __call__(self, name: str, payload: dict[str, object] | None = None, *, source: str = "qt") -> None: ...


if TYPE_CHECKING:
    from ui.qt_native.debug_events import DebugEventBus

    GetDebugBus = Callable[[], DebugEventBus]
else:
    GetDebugBus = Callable[[], object]

emit_debug_event: EmitDebugEvent | None
get_debug_bus: GetDebugBus | None

try:
    from ui.qt_native.debug_events import (
        emit_debug_event as _emit_debug_event,
        get_debug_bus as _get_debug_bus,
    )
except Exception as e:  # pragma: no cover - allows CLI tools without Qt available
    get_logger(__name__).exception("Debug events not available: %s", e)
    emit_debug_event = None
    get_debug_bus = None
else:
    emit_debug_event = _emit_debug_event
    get_debug_bus = _get_debug_bus

logger = get_logger(__name__)

if TYPE_CHECKING:
    from models.player import (
        PlayerState,
        get_player_state_schema,
        validate_player_state_payload,
    )

    _HAS_PLAYER_SCHEMA = True
else:
    try:
        from models.player import (
            PlayerState,
            get_player_state_schema,
            validate_player_state_payload,
        )

        _HAS_PLAYER_SCHEMA = True
    except ImportError:  # pragma: no cover - fallback if shared models unavailable

        @dataclass
        class PlayerState:
            is_playing: bool = False
            now_playing: NowPlayingTrack | None = None
            queue: list[QueueItem] = field(default_factory=list)
            volume: int = 80
            position: int = 0
            duration: int = 0
            position_percentage: float = 0.0

        def get_player_state_schema() -> dict[str, object] | None:
            return None

        def _coerce_now_playing(value: object) -> NowPlayingTrack | None:
            if value is None:
                return None
            if not isinstance(value, Mapping):
                return None
            track: NowPlayingTrack = {}
            video_id = value.get("video_id")
            if isinstance(video_id, str):
                track["video_id"] = video_id
            title = value.get("title")
            if isinstance(title, str):
                track["title"] = title
            artist = value.get("artist")
            if isinstance(artist, str):
                track["artist"] = artist
            thumbnail = value.get("thumbnail")
            if isinstance(thumbnail, str):
                track["thumbnail"] = thumbnail
            duration = value.get("duration")
            if isinstance(duration, int):
                track["duration"] = duration
            return track or None

        def _coerce_queue(value: object) -> list[QueueItem]:
            if not isinstance(value, list):
                return []
            coerced: list[QueueItem] = []
            for entry in value:
                if not isinstance(entry, Mapping):
                    continue
                item: QueueItem = {}
                item_id = entry.get("id")
                if isinstance(item_id, str):
                    item["id"] = item_id
                video_id = entry.get("video_id")
                if isinstance(video_id, str):
                    item["video_id"] = video_id
                title = entry.get("title")
                if isinstance(title, str):
                    item["title"] = title
                artist = entry.get("artist")
                if isinstance(artist, str):
                    item["artist"] = artist
                thumbnail = entry.get("thumbnail")
                if isinstance(thumbnail, str):
                    item["thumbnail"] = thumbnail
                duration = entry.get("duration")
                if isinstance(duration, int):
                    item["duration"] = duration
                coerced.append(item)
            return coerced

        def validate_player_state_payload(data: dict[str, object]) -> PlayerState:
            return PlayerState(
                is_playing=bool(data.get("is_playing", False)),
                now_playing=_coerce_now_playing(data.get("now_playing")),
                queue=_coerce_queue(data.get("queue", [])),
                volume=int(data.get("volume", 80)),
                position=int(data.get("position", 0)),
                duration=int(data.get("duration", 0)),
                position_percentage=float(data.get("position_percentage", 0.0)),
            )

        _HAS_PLAYER_SCHEMA = False


@dataclass
class StatePollResult:
    """Structured response returned by async state polling."""

    state: PlayerState | None
    warnings: tuple[str, ...] = ()
    error: BaseException | None = None


class _APICallSignalsBase:
    """Base protocol for signal containers used by API workers."""

    class _SignalLike(Protocol):
        def connect(self, slot: Callable[..., object] | SignalInstance) -> object: ...

        def emit(self, *args: object) -> None: ...

    result: _SignalLike
    error: _SignalLike
    completed: _SignalLike


class _APICallRunnableBase:
    """Base protocol for runnable wrappers."""

    signals: _APICallSignalsBase

    def __init__(
        self,
        fn: Callable[..., object] | None = None,
        *,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> None:
        # Base class init - subclasses override but MRO may call this
        pass


_APICallSignalsCls: type[_APICallSignalsBase]
_APICallRunnableCls: type[_APICallRunnableBase]

if QT_AVAILABLE:

    class _QtAPICallSignals(QObject, _APICallSignalsBase):
        """Qt signal helper for worker-based API requests."""

        result = Signal(object)
        error = Signal(object)
        completed = Signal()

        def __init__(self) -> None:
            super().__init__()

    class _QtAPICallRunnable(QRunnable, _APICallRunnableBase):
        """QRunnable wrapper that executes API calls off the UI thread."""

        def __init__(
            self,
            fn: Callable[..., object],
            *,
            args: tuple[object, ...] | None = None,
            kwargs: dict[str, object] | None = None,
        ) -> None:
            QRunnable.__init__(self)
            self._fn = fn
            self._args = args or ()
            self._kwargs = kwargs or {}
            self.signals = _QtAPICallSignals()

        def run(self) -> None:
            try:
                result = self._fn(*self._args, **self._kwargs)
            except Exception as exc:  # pragma: no cover - surfaced via signal
                self.signals.error.emit(exc)
            else:
                self.signals.result.emit(result)
            finally:
                self.signals.completed.emit()

    _APICallSignalsCls = _QtAPICallSignals
    _APICallRunnableCls = _QtAPICallRunnable

else:  # pragma: no cover - CLI fallback with no Qt runtime

    class _SignalProxy:
        def connect(self, *_args: object, **_kwargs: object) -> None:
            pass

        def emit(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _FallbackAPICallSignals(_APICallSignalsBase):
        """Fallback signals that no-op when Qt is unavailable."""

        def __init__(self) -> None:
            self.result = _SignalProxy()
            self.error = _SignalProxy()
            self.completed = _SignalProxy()

    class _FallbackAPICallRunnable(_APICallRunnableBase):
        """Fallback runnable that should never be instantiated without Qt."""

        def __init__(
            self,
            fn: Callable[..., object],
            *,
            args: tuple[object, ...] | None = None,
            kwargs: dict[str, object] | None = None,
        ) -> None:
            raise RuntimeError("Qt runtime not available; background worker unsupported")

    _APICallSignalsCls = _FallbackAPICallSignals
    _APICallRunnableCls = _FallbackAPICallRunnable


class BaseAPIClient(ABC):
    """Base API client with session management and infrastructure."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or settings.base_url
        self.session = requests.Session()
        # SEC-R5a: defense-in-depth against CVE-2024-47081 (.netrc leak).
        # trust_env=False prevents the requests Session from auto-reading
        # ~/.netrc and $HTTP_PROXY; Viola's Qt client talks to localhost
        # only, so losing env-proxy support is fine and the .netrc
        # leak vector is completely closed off.
        self.session.trust_env = False
        self.session.headers.update({"Content-Type": "application/json", "User-Agent": "Viola-Native-Qt/1.0"})

        # Trust the self-signed cert when HTTPS mode is active
        if settings.ssl_enabled:
            from pathlib import Path

            _base = Path(__file__).resolve().parent.parent.parent
            _cert = _base / "data" / "secrets" / "viola_cert.pem"
            if _cert.exists():
                self.session.verify = str(_cert)
            else:
                self.session.verify = True

        # Load and set API key for authentication
        api_key = self._load_api_key()
        if api_key:
            self.session.headers.update({"X-API-Key": api_key})

        if QT_AVAILABLE:
            thread_pool: QThreadPool | None = QThreadPool.globalInstance()
        else:
            thread_pool = None
        self._thread_pool = thread_pool
        self._active_workers: set[_APICallRunnableBase] = set()

        # Event log for tracking commands and events
        self.event_log: list[tuple[str, str]] = []

    def _load_api_key(self) -> str | None:
        """Load the Viola API key from env, security config, or bootstrap."""

        # Try environment variable first
        env_key = env.get("VIOLA_SECURITY_API_KEY")
        if env_key:
            return env_key.strip()

        # Try security config
        if _SECURITY_AVAILABLE and get_security_config:
            try:
                config = get_security_config()
                if config and config.auth_api_key:
                    return config.auth_api_key
            except Exception as e:
                logger.exception("Failed to get API key from security config: %s", e)
                pass

        # Try bootstrap API key file
        if _SECURITY_AVAILABLE and load_bootstrap_api_key:
            try:
                bootstrap_key = load_bootstrap_api_key()
                if bootstrap_key:
                    return bootstrap_key
            except Exception as e:
                logger.exception("Failed to load bootstrap API key: %s", e)
                pass

        return None

    def _normalise_envelope(self, payload: object) -> ResponseEnvelope | None:
        """Normalize API response envelope."""
        try:
            envelope = ensure_envelope(payload)
        except ResponseContractError as exc:
            logger.error("Invalid API response envelope: %s", exc)
            return None

        error_obj = envelope.get("error")
        if error_obj is not None and isinstance(error_obj, Mapping):
            normalised_error: ResponseError = {
                "code": str(error_obj.get("code", "error")),
                "message": str(error_obj.get("message", "Request failed.")),
            }
            details = error_obj.get("details")
            if isinstance(details, Mapping):
                from core.json_types import to_json_value

                normalised_error["details"] = {str(k): to_json_value(v) for k, v in details.items()}
            envelope["error"] = normalised_error
        return envelope

    @staticmethod
    def _build_error_envelope(code: str, message: str, *, data: dict[str, object] | None = None) -> ResponseEnvelope:
        """Build standardized error envelope."""
        from core.json_types import to_json_value

        return {
            "ok": False,
            "error": {"code": code, "message": message},
            "data": to_json_value(data or {}),
        }

    def _emit_api_event(self, signal_name: str, payload: dict[str, object]) -> None:
        """Emit debug event for API operations."""
        merged_payload: dict[str, object] = {"client": "qt", **payload}

        bus_factory = get_debug_bus if get_debug_bus is not None else None
        if bus_factory is not None:
            try:
                bus = bus_factory()
                signal = getattr(bus, signal_name, None)
                emit = getattr(signal, "emit", None)
                if callable(emit):
                    emit(merged_payload)
                    return
            except Exception as e:
                logger.exception("Failed to emit via debug bus: %s", e)
                pass

        if callable(emit_debug_event):
            try:
                emit_debug_event(signal_name, merged_payload, source="qt")
            except Exception as e:
                logger.exception("Failed to emit debug event: %s", e)
                pass

    def _submit_worker(
        self,
        fn: Callable[..., object],
        *,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        on_result: Callable[[object], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> _APICallRunnableBase | None:
        """Execute function on background worker."""
        call_args = args or ()
        call_kwargs = kwargs or {}

        if not QT_AVAILABLE or self._thread_pool is None:
            try:
                result = fn(*call_args, **call_kwargs)
            except Exception as exc:
                if on_error:
                    on_error(exc)
                    return None
                raise
            else:
                if on_result:
                    on_result(result)
                return None

        worker = _APICallRunnableCls(fn, args=call_args, kwargs=call_kwargs)
        if on_result is not None:
            worker.signals.result.connect(on_result)
        if on_error is not None:
            worker.signals.error.connect(on_error)
        else:
            worker.signals.error.connect(lambda exc: logger.debug("Async API call raised: %s", exc))

        def on_completed() -> None:
            self._active_workers.discard(worker)

        worker.signals.completed.connect(on_completed)
        self._active_workers.add(worker)
        if self._thread_pool is not None:
            self._thread_pool.start(cast(QRunnable, worker))
        return worker

    # Abstract methods that concrete implementations must provide
    @abstractmethod
    def health_check(self) -> bool:
        """Check if the backend is healthy."""
        ...

    @abstractmethod
    def get_state(self, *, notify: bool = True) -> PlayerState | None:
        """Get current player state."""
        ...

    @abstractmethod
    def get_queue(self) -> QueueResponse | None:
        """Get the current playback queue."""
        ...

    @abstractmethod
    def clear_queue(self) -> bool:
        """Clear the playback queue."""
        ...

    @abstractmethod
    def set_volume(self, level: int) -> bool:
        """Set the playback volume."""
        ...

    @abstractmethod
    def play_pause(self) -> bool:
        """Toggle play/pause."""
        ...

    @abstractmethod
    def next_track(self) -> bool:
        """Skip to next track."""
        ...

    @abstractmethod
    def previous_track(self) -> bool:
        """Skip to previous track."""
        ...

    @abstractmethod
    def seek(self, position: int) -> bool:
        """Seek to position in seconds."""
        ...

    @abstractmethod
    def reorder_queue(self, from_index: int, to_index: int) -> bool:
        """Reorder queue items."""
        ...

    @abstractmethod
    def remove_from_queue(self, item_id: str) -> bool:
        """Remove item from queue."""
        ...

    @abstractmethod
    def thumbs_up(self, video_id: str, title: str, artist: str | None = None) -> bool:
        """Rate track with thumbs up."""
        ...

    @abstractmethod
    def thumbs_down(self, video_id: str, title: str, artist: str | None = None) -> bool:
        """Rate track with thumbs down."""
        ...

    @abstractmethod
    def get_rating_status(self, video_id: str) -> str | None:
        """Get rating status for track."""
        ...


__all__ = [
    "BaseAPIClient",
    "DebugEventPayload",
    "NowPlayingTrack",
    "PlayerState",
    "PlayerStateData",
    "QueueItem",
    "QueueResponse",
    "StatePollResult",
    "get_player_state_schema",
    "validate_player_state_payload",
]
