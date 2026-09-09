"""
music/backends/qt_media.py

Qt-native media backend using PyQt6 QMediaPlayer + QAudioOutput.
No third-party dependencies beyond PyQt6.QtMultimedia.

Thread-safety: QMediaPlayer has thread affinity — it must be created and
operated on the Qt main thread. This module uses a signal/slot bridge
(_MainThreadInvoker) to marshal all QMediaPlayer calls to the main thread,
making it safe to call QtMediaBackend methods from any thread.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from music.backends.base import BackendCapabilities, BackendProgress, BaseBackend

if TYPE_CHECKING:
    import logging

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy Qt imports — fail gracefully when QtMultimedia is absent
# ---------------------------------------------------------------------------

_QT_AVAILABLE = False

try:
    from PySide6.QtCore import (
        QCoreApplication,
        QObject,
        Qt,
        QThread,
        QUrl,
        Signal,
        Slot,
    )
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    _QT_AVAILABLE = True
except ImportError:
    pass


# ===================================================================
# Main-thread invoker (signal/slot bridge)
# ===================================================================

if _QT_AVAILABLE:

    class _VideoSignalBridge(QObject):
        """Emits video_available_changed from the main thread.

        Separate QObject because QtMediaBackend inherits BaseBackend (not QObject).
        """

        video_available_changed = Signal(bool)

    class _MainThreadInvoker(QObject):
        """Dispatch arbitrary callables to the Qt main thread.

        Emit ``_invoke_signal`` with a callable; the connected slot
        ``_run`` executes it on the thread that owns this QObject
        (the main thread, since it is created there).
        """

        _invoke_signal = Signal(object)

        def __init__(self, parent: QObject | None = None) -> None:
            super().__init__(parent)
            # AutoConnection → direct if same thread, queued if cross-thread
            self._invoke_signal.connect(self._run, Qt.ConnectionType.AutoConnection)

        @Slot(object)
        def _run(self, func: object) -> None:  # pragma: no cover – Qt slot
            if callable(func):
                func()

        def invoke(self, func: Any, *, blocking: bool = True) -> Any:
            """Run *func* on the Qt main thread.

            * If the caller is already on the main thread the function is
              invoked directly (no signal overhead).
            * If *blocking* is True (default) the caller blocks until the
              function returns.  A 5-second safety timeout prevents hangs
              if the event loop is stalled.
            """
            app = QCoreApplication.instance()
            if app is None or QThread.currentThread() == app.thread():
                return func()

            if not blocking:
                self._invoke_signal.emit(func)
                return None

            done = threading.Event()
            result_box: list[Any] = [None]
            error_box: list[BaseException | None] = [None]

            def _wrapper() -> None:
                try:
                    result_box[0] = func()
                except BaseException as exc:
                    error_box[0] = exc
                finally:
                    done.set()

            self._invoke_signal.emit(_wrapper)
            if not done.wait(timeout=5.0):
                logger.warning("_MainThreadInvoker timed out waiting for main thread")
                return None

            if error_box[0] is not None:
                raise error_box[0]
            return result_box[0]


# ===================================================================
# QtMediaBackend
# ===================================================================


class QtMediaBackend(BaseBackend):
    """Qt-native playback backend backed by QMediaPlayer + QAudioOutput.

    All QMediaPlayer interactions are marshaled to the Qt main thread
    via ``_MainThreadInvoker``.  Position and duration are cached from
    QMediaPlayer signals so reads are lock-free from any thread.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        super().__init__()
        self._logger = logger or get_logger("viola.backend.qt_media")

        # Thread-safe cached state (written from main-thread signal
        # handlers, read from any thread under _state_lock).
        self._state_lock = threading.Lock()
        self._position_ms: int = 0
        self._duration_ms: int = 0
        self._is_playing_flag: bool = False
        self._volume_level: int = 50
        self._last_error: str | None = None

        # Qt objects – created on the main thread by _init_qt_objects()
        self._invoker: Any | None = None  # _MainThreadInvoker
        self._player: Any | None = None  # QMediaPlayer
        self._audio_output: Any | None = None  # QAudioOutput

        # Video support
        self._video_output: Any | None = None  # QVideoWidget (injected)
        self._has_video: bool = False
        self._video_signal_bridge: Any | None = None  # _VideoSignalBridge

        if not _QT_AVAILABLE:
            raise RuntimeError("PyQt6.QtMultimedia not available — QtMediaBackend cannot function")

        self._init_qt_objects()

    # ------------------------------------------------------------------
    # Qt object bootstrap
    # ------------------------------------------------------------------

    def _init_qt_objects(self) -> None:
        """Create QMediaPlayer, QAudioOutput, and the invoker on the
        Qt main thread."""
        app = QCoreApplication.instance()
        if app is None:
            raise RuntimeError("No QCoreApplication — QtMediaBackend requires a running Qt application")

        if QThread.currentThread() == app.thread():
            self._create_qt_objects()
        else:
            # Rare path: backend created from a worker thread.
            # Dispatch creation to the main thread and block.
            done = threading.Event()
            error_box: list[BaseException | None] = [None]

            def _create() -> None:
                try:
                    self._create_qt_objects()
                except BaseException as exc:
                    error_box[0] = exc
                finally:
                    done.set()

            # Use a temporary invoker to bootstrap
            tmp_invoker = _MainThreadInvoker()
            tmp_invoker._invoke_signal.emit(_create)
            if not done.wait(timeout=5.0):
                self._logger.error("Timed out creating Qt media objects on the main thread")
                return
            if error_box[0] is not None:
                self._logger.error("Failed to create Qt media objects: %s", error_box[0])

    def _create_qt_objects(self) -> None:
        """Must run on the Qt main thread."""
        self._invoker = _MainThreadInvoker()
        self._player = QMediaPlayer()
        self._audio_output = QAudioOutput()
        self._player.setAudioOutput(self._audio_output)

        # Apply initial volume
        self._audio_output.setVolume(self._volume_level / 100.0)

        # Wire QMediaPlayer signals → cached state + progress listener
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.errorOccurred.connect(self._on_error_occurred)

        # Video signal bridge for UI notification
        self._video_signal_bridge = _VideoSignalBridge()

        # Connect hasVideoChanged signal (Qt 6.2+)
        if hasattr(self._player, "hasVideoChanged"):
            self._player.hasVideoChanged.connect(self._on_has_video_changed)

        self._logger.info("QtMediaBackend initialized (QMediaPlayer + QAudioOutput)")

    # ------------------------------------------------------------------
    # Internal helper: dispatch to the main thread
    # ------------------------------------------------------------------

    def _on_main_thread(self, func: Any, *, blocking: bool = True) -> Any:
        if self._invoker is None:
            self._logger.warning("QtMediaBackend not initialised — ignoring operation")
            return None
        return self._invoker.invoke(func, blocking=blocking)

    # ==================================================================
    # BaseBackend API
    # ==================================================================

    def play(self, source: str) -> None:
        self._last_error = None

        def _do_play() -> None:
            if self._player is None:
                return
            if source.startswith(("http://", "https://")):
                url = QUrl(source)
            else:
                url = QUrl.fromLocalFile(source)
            self._player.setSource(url)
            self._player.play()

        self._on_main_thread(_do_play)
        with self._state_lock:
            self._is_playing_flag = True
        self._logger.debug("QtMediaBackend playing: %s", source[:100])

    def pause(self) -> None:
        def _do_pause() -> None:
            if self._player is not None:
                self._player.pause()

        self._on_main_thread(_do_pause)
        with self._state_lock:
            self._is_playing_flag = False

    def resume(self) -> None:
        def _do_resume() -> None:
            if self._player is not None:
                self._player.play()

        self._on_main_thread(_do_resume)
        with self._state_lock:
            self._is_playing_flag = True

    def stop(self) -> None:
        def _do_stop() -> None:
            if self._player is not None:
                self._player.stop()

        self._on_main_thread(_do_stop)
        with self._state_lock:
            self._is_playing_flag = False
            self._position_ms = 0

    def is_playing(self) -> bool:
        with self._state_lock:
            return self._is_playing_flag

    def set_volume(self, level: int) -> int:
        clamped = max(0, min(100, int(level)))

        def _do_volume() -> None:
            if self._audio_output is not None:
                self._audio_output.setVolume(clamped / 100.0)

        self._on_main_thread(_do_volume)
        with self._state_lock:
            self._volume_level = clamped
        return clamped

    def seek(self, position_seconds: float) -> None:
        ms = int(position_seconds * 1000)

        def _do_seek() -> None:
            if self._player is not None:
                self._player.setPosition(ms)

        self._on_main_thread(_do_seek)
        self._logger.debug("QtMediaBackend seek to %.1fs (%dms)", position_seconds, ms)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=False,
            pause=True,
            resume=True,
            seek=True,
            volume=True,
            position=True,
            duration=True,
        )

    def current_position_ms(self) -> int | None:
        with self._state_lock:
            if not self._is_playing_flag:
                return None
            return self._position_ms

    def current_duration_ms(self) -> int | None:
        with self._state_lock:
            return self._duration_ms if self._duration_ms > 0 else None

    def cleanup(self) -> None:
        self._logger.info("Cleaning up QtMediaBackend...")
        self.stop()

        def _do_cleanup() -> None:
            if self._player is not None:
                self._player.setVideoOutput(None)  # type: ignore[arg-type]
                self._player.setSource(QUrl())
            self._player = None
            self._audio_output = None
            self._video_signal_bridge = None

        if self._invoker is not None:
            self._on_main_thread(_do_cleanup)
        self._invoker = None
        self._video_output = None
        self._logger.info("QtMediaBackend cleaned up")

    # ==================================================================
    # QMediaPlayer signal handlers (always run on the main thread)
    # ==================================================================

    def _on_position_changed(self, position: int) -> None:
        with self._state_lock:
            self._position_ms = position
            duration = self._duration_ms

        self._emit_progress(
            BackendProgress(
                position_ms=position,
                duration_ms=duration if duration > 0 else None,
            )
        )

    def _on_duration_changed(self, duration: int) -> None:
        with self._state_lock:
            self._duration_ms = duration
        self._logger.debug("QtMediaBackend duration: %dms", duration)

    def _on_state_changed(self, state: Any) -> None:
        with self._state_lock:
            if state == QMediaPlayer.PlaybackState.PlayingState:
                self._is_playing_flag = True
            elif state in (
                QMediaPlayer.PlaybackState.PausedState,
                QMediaPlayer.PlaybackState.StoppedState,
            ):
                self._is_playing_flag = False
                if state == QMediaPlayer.PlaybackState.StoppedState:
                    self._has_video = False
                    if self._video_signal_bridge is not None:
                        self._video_signal_bridge.video_available_changed.emit(False)
        self._logger.debug("QtMediaBackend state: %s", state)

    def _on_error_occurred(self, error: Any, message: str) -> None:
        with self._state_lock:
            self._last_error = message
            self._is_playing_flag = False
        self._logger.error("QtMediaBackend error (%s): %s", error, message)

    def _on_has_video_changed(self, has_video: bool) -> None:
        """Called when QMediaPlayer detects or loses a video track."""
        with self._state_lock:
            self._has_video = has_video
        self._logger.info("QMediaPlayer video track detected: %s", has_video)
        if self._video_signal_bridge is not None:
            self._video_signal_bridge.video_available_changed.emit(has_video)

    # ==================================================================
    # Video output (dependency injection from UI layer)
    # ==================================================================

    def set_video_output(self, widget: Any) -> None:
        """Set the QVideoWidget for video rendering.

        Called by the UI layer to inject the video surface. The backend
        does not import or create UI widgets — it only receives them.
        Must be called from any thread; dispatched to Qt main thread.
        """
        self._video_output = widget

        def _do_set_video_output() -> None:
            if self._player is not None and widget is not None:
                self._player.setVideoOutput(widget)
                self._logger.info("QtMediaBackend: video output set")
            elif self._player is not None:
                self._player.setVideoOutput(None)  # type: ignore[arg-type]
                self._logger.info("QtMediaBackend: video output cleared")

        self._on_main_thread(_do_set_video_output)

    @property
    def has_active_video(self) -> bool:
        """Whether the current media contains a video track."""
        with self._state_lock:
            return self._has_video

    @property
    def video_signal_bridge(self) -> Any:
        """Access the video signal bridge for connecting UI signals.

        Returns a QObject with ``video_available_changed(bool)`` signal,
        or None if Qt is unavailable.
        """
        return self._video_signal_bridge
