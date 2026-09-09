"""Agent Frame Streamer — captures browser viewport and streams to spokes.

When the agent is working in the visible browser and spokes are connected,
this service captures JPEG frames from ``QWebEngineView.grab()`` and sends
them as binary WebSocket messages so spokes can display the agent's activity
in real-time.

Architecture::

    QTimer (main thread, 8fps default)
      → QWebEngineView.grab() → QPixmap
      → JPEG encode (quality 60, ~30-50KB)
      → broadcast_fn(jpeg_bytes) → EventHub.broadcast_binary_to_rooms()
      → WebSocket binary frames → spoke React <img> with crossfade

Lifecycle::

    Agent enters AGENTIC state + spokes connected
      → start_streaming(webview)

    Agent exits AGENTIC state OR last spoke disconnects
      → stop_streaming()

FPS backpressure::

    spoke_count ≤ 2  → 8 fps (125ms interval)
    spoke_count ≤ 5  → 5 fps (200ms interval)
    spoke_count > 5  → 3 fps (333ms interval)

Thread safety:
    Frame capture (``QWebEngineView.grab()``) MUST run on the Qt main thread.
    This module uses ``QCoreApplication.postEvent()`` with a custom QEvent
    and QTimer for periodic capture — the proven pattern from
    ``services/agent_perception.py``.

Module-level singleton:
    Use ``set_frame_streamer()`` / ``get_frame_streamer()`` to register
    the instance created during bootstrap.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def _resolve_broadcast_user_id() -> str | None:
    """Return the active user for broadcast, using the desktop principal outside requests."""
    try:
        from core.user_context import get_current_or_device_user_id

        return get_current_or_device_user_id()
    except LookupError:
        return None


# ---------------------------------------------------------------------------
# Qt imports (optional — desktop only)
# ---------------------------------------------------------------------------

try:
    from PySide6.QtCore import (
        QBuffer,
        QCoreApplication,
        QEvent,
        QIODevice,
        QObject,
        QTimer,
    )

    class _CaptureEvent(QEvent):
        """Custom QEvent carrying a capture callable for main-thread dispatch."""

        _EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

        def __init__(self, fn: object) -> None:
            super().__init__(self._EVENT_TYPE)
            self.fn = fn

    class _CaptureReceiver(QObject):
        """Receives ``_CaptureEvent`` on the Qt main thread and executes it."""

        def event(self, event: QEvent) -> bool:
            if isinstance(event, _CaptureEvent):
                try:
                    event.fn()  # type: ignore[operator]
                except Exception:
                    logger.exception("FrameStreamer: capture event failed")
                return True
            return super().event(event)

    _HAS_QT = True
except ImportError:
    _HAS_QT = False

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_FPS = 8
_DEFAULT_JPEG_QUALITY = 60
_MAX_FRAME_QUEUE_SIZE = 2


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: AgentFrameStreamer | None = None


def set_frame_streamer(streamer: AgentFrameStreamer) -> None:
    """Register the application-wide frame streamer singleton."""
    global _instance
    _instance = streamer


def get_frame_streamer() -> AgentFrameStreamer | None:
    """Return the frame streamer singleton, or None if not yet wired."""
    return _instance


# ---------------------------------------------------------------------------
# AgentFrameStreamer
# ---------------------------------------------------------------------------


class AgentFrameStreamer:
    """Captures browser viewport frames and streams them to spokes.

    Parameters
    ----------
    jpeg_quality : int
        JPEG encoding quality (0-100). Lower = smaller files, more artifacts.
        Default 60 is a good balance for page content (~30-50KB at 1400x800).
    """

    def __init__(self, jpeg_quality: int = _DEFAULT_JPEG_QUALITY) -> None:
        self._webview: Any | None = None
        self._jpeg_quality = jpeg_quality
        self._timer: Any | None = None  # QTimer, created lazily
        self._receiver: Any | None = None  # _CaptureReceiver on main thread
        self._streaming = False
        self._fps = _DEFAULT_FPS
        self._broadcast_fn: Callable[..., Any] | None = None
        self._broadcast_user_id: str | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._broadcast_queue: asyncio.Queue[bytes] | None = None
        self._broadcast_worker: asyncio.Task[None] | None = None
        self._spoke_count_fn: Callable[..., int] | None = None

        # Metrics (updated during streaming)
        self._frames_sent = 0
        self._frames_dropped = 0
        self._total_capture_ms = 0.0
        self._total_encode_ms = 0.0
        self._total_bytes = 0
        self._start_time = 0.0

        # PoC instrumentation: log detailed timing for first 10 frames
        self._poc_logged = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_webview(self, webview: Any) -> None:
        """Set the QWebEngineView to capture frames from."""
        self._webview = webview

    def set_broadcast_fn(self, fn: Callable[..., Any]) -> None:
        """Set the function called to broadcast JPEG bytes to spokes.

        The function receives raw JPEG bytes and should send them
        as binary WebSocket messages to all connected spokes.
        """
        self._broadcast_fn = fn

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop for scheduling broadcasts."""
        self._event_loop = loop

    def set_spoke_count_fn(self, fn: Callable[..., int]) -> None:
        """Set the function used to count connected spokes for a user."""
        self._spoke_count_fn = fn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_streaming(self, *, user_id: str | None = None) -> bool:
        """Start periodic frame capture and streaming.

        No-op if already streaming or if Qt is not available.
        """
        if self._streaming:
            return True
        resolved_user_id = (user_id or _resolve_broadcast_user_id() or "").strip() or None
        if not resolved_user_id:
            logger.warning("FrameStreamer: refusing to start without a scoped user_id")
            return False
        if self._broadcast_fn is None:
            logger.debug("FrameStreamer: no broadcast function set, cannot start")
            return False
        spoke_count = self._get_spoke_count(resolved_user_id)
        if spoke_count <= 0:
            logger.debug("FrameStreamer: no spokes connected for user_id=%s", resolved_user_id)
            return False
        self._fps = self._determine_fps(spoke_count)
        if not _HAS_QT:
            logger.debug("FrameStreamer: Qt not available, cannot start")
            return False
        if self._webview is None:
            logger.debug("FrameStreamer: no webview set, cannot start")
            return False

        if self._event_loop is None or self._event_loop.is_closed():
            try:
                self._event_loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    self._event_loop = asyncio.get_event_loop()
                except RuntimeError:
                    logger.warning("FrameStreamer: no asyncio loop for bounded broadcasts")
                    return False

        self._broadcast_queue = asyncio.Queue(maxsize=_MAX_FRAME_QUEUE_SIZE)
        self._broadcast_user_id = resolved_user_id
        if self._broadcast_worker is None or self._broadcast_worker.done():
            self._broadcast_worker = self._event_loop.create_task(self._broadcast_loop())
            self._broadcast_worker.add_done_callback(self._consume_worker_result)

        # Create receiver on main thread (if not already done)
        if self._receiver is None:
            app = QCoreApplication.instance()
            if app is None:
                logger.warning("FrameStreamer: no QCoreApplication instance")
                self._stop_broadcast_worker()
                return False
            self._receiver = _CaptureReceiver()
            self._receiver.moveToThread(app.thread())
            logger.debug("FrameStreamer: receiver created on main thread")

        # Reset metrics
        self._frames_sent = 0
        self._frames_dropped = 0
        self._total_capture_ms = 0.0
        self._total_encode_ms = 0.0
        self._total_bytes = 0
        self._start_time = time.monotonic()
        self._poc_logged = False

        # Post timer creation to main thread
        def _create_timer() -> None:
            interval_ms = int(1000 / self._fps)
            self._timer = QTimer()
            self._timer.setInterval(interval_ms)
            self._timer.timeout.connect(self._on_timer_tick)
            self._timer.start()
            logger.info(
                "FrameStreamer: started at %d fps (%d ms interval, spokes=%d, user=%s)",
                self._fps,
                interval_ms,
                spoke_count,
                resolved_user_id,
            )

        try:
            QCoreApplication.postEvent(self._receiver, _CaptureEvent(_create_timer))
            self._streaming = True
            return True
        except Exception:
            logger.exception("FrameStreamer: failed to start timer")
            self._stop_broadcast_worker()
            return False

    def stop_streaming(self) -> None:
        """Stop periodic frame capture and streaming.

        No-op if not streaming.
        """
        if not self._streaming:
            return

        self._streaming = False
        self._broadcast_user_id = None
        self._clear_broadcast_queue()
        self._stop_broadcast_worker()

        # Post timer stop to main thread
        if self._receiver is not None and _HAS_QT:

            def _stop_timer() -> None:
                if self._timer is not None:
                    self._timer.stop()
                    self._timer.deleteLater()
                    self._timer = None

            try:
                QCoreApplication.postEvent(self._receiver, _CaptureEvent(_stop_timer))
            except Exception:
                logger.exception("FrameStreamer: failed to stop timer")

        # Log summary
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        if self._frames_sent > 0 and elapsed > 0:
            avg_size_kb = (self._total_bytes / self._frames_sent) / 1024
            avg_fps = self._frames_sent / elapsed
            logger.info(
                "FrameStreamer: stopped after %d frames, %.1fs, avg %.1f KB/frame, %.1f fps, dropped=%d",
                self._frames_sent,
                elapsed,
                avg_size_kb,
                avg_fps,
                self._frames_dropped,
            )
        else:
            logger.info("FrameStreamer: stopped (no frames sent)")

    @property
    def is_streaming(self) -> bool:
        """Whether the streamer is currently capturing and sending frames."""
        return self._streaming

    # ------------------------------------------------------------------
    # Internal — frame capture (runs on Qt main thread via QTimer)
    # ------------------------------------------------------------------

    def _on_timer_tick(self) -> None:
        """QTimer callback — runs on Qt main thread."""
        if not self._streaming or self._webview is None:
            return

        try:
            self._capture_and_broadcast()
        except Exception:
            logger.exception("FrameStreamer: capture tick failed")

    def _capture_and_broadcast(self) -> None:
        """Capture a frame, JPEG-encode, and schedule broadcast.

        Runs on the Qt main thread.
        """
        # Capture
        t_capture = time.perf_counter()
        pixmap = self._webview.grab()
        capture_ms = (time.perf_counter() - t_capture) * 1000

        if pixmap.isNull():
            return

        # JPEG encode
        t_encode = time.perf_counter()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "JPEG", self._jpeg_quality)
        jpeg_bytes = bytes(buf.data())
        buf.close()
        encode_ms = (time.perf_counter() - t_encode) * 1000

        # Update metrics
        self._frames_sent += 1
        self._total_capture_ms += capture_ms
        self._total_encode_ms += encode_ms
        self._total_bytes += len(jpeg_bytes)

        # PoC instrumentation: log first 10 frames
        if self._frames_sent <= 10 and not self._poc_logged:
            logger.info(
                "FrameStreamer PoC frame %d: %.1f KB, capture=%.1f ms, encode=%.1f ms, res=%dx%d",
                self._frames_sent,
                len(jpeg_bytes) / 1024,
                capture_ms,
                encode_ms,
                pixmap.width(),
                pixmap.height(),
            )
            if self._frames_sent == 10:
                self._poc_logged = True
                avg_size = self._total_bytes / 10 / 1024
                avg_total = (self._total_capture_ms + self._total_encode_ms) / 10
                logger.info(
                    "FrameStreamer PoC summary: avg %.1f KB, avg %.1f ms total — %s",
                    avg_size,
                    avg_total,
                    "PASS" if avg_total < 20 and avg_size < 60 else "REVIEW",
                )

        if self._event_loop is None or self._event_loop.is_closed() or self._broadcast_queue is None:
            return
        try:
            self._event_loop.call_soon_threadsafe(self._enqueue_frame, jpeg_bytes)
        except RuntimeError:
            pass  # Event loop closed

    def _enqueue_frame(self, jpeg_bytes: bytes) -> None:
        """Enqueue a frame, dropping the oldest one when the queue is full."""
        if self._broadcast_queue is None:
            return
        if self._broadcast_queue.full():
            try:
                self._broadcast_queue.get_nowait()
                self._broadcast_queue.task_done()
                self._frames_dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._broadcast_queue.put_nowait(jpeg_bytes)
        except asyncio.QueueFull:
            self._frames_dropped += 1

    async def _broadcast_loop(self) -> None:
        """Drain the bounded frame queue to the spoke broadcast function."""
        while True:
            if self._broadcast_queue is None:
                await asyncio.sleep(0)
                continue
            jpeg_bytes = await self._broadcast_queue.get()
            try:
                if self._broadcast_fn is None or not self._broadcast_user_id:
                    continue
                result = self._broadcast_fn(jpeg_bytes, user_id=self._broadcast_user_id)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FrameStreamer: async broadcast failed")
            finally:
                self._broadcast_queue.task_done()

    def _stop_broadcast_worker(self) -> None:
        worker = self._broadcast_worker
        self._broadcast_worker = None
        if worker is not None and not worker.done():
            loop = self._event_loop
            if loop is not None and loop.is_running() and not loop.is_closed():
                loop.call_soon_threadsafe(worker.cancel)
            else:
                worker.cancel()

    def _clear_broadcast_queue(self) -> None:
        queue = self._broadcast_queue
        self._broadcast_queue = None
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

    @staticmethod
    def _consume_worker_result(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("FrameStreamer: broadcast worker failed")

    def _get_spoke_count(self, user_id: str) -> int:
        if self._spoke_count_fn is None:
            return 0
        try:
            return int(self._spoke_count_fn(user_id=user_id))
        except TypeError:
            return int(self._spoke_count_fn())
        except Exception:
            logger.debug("FrameStreamer: spoke count check failed", exc_info=True)
            return 0

    @staticmethod
    def _determine_fps(spoke_count: int) -> int:
        if spoke_count <= 2:
            return 8
        if spoke_count <= 5:
            return 5
        return 3


__all__ = [
    "AgentFrameStreamer",
    "get_frame_streamer",
    "set_frame_streamer",
]
