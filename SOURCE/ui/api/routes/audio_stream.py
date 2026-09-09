"""
Binary WebSocket endpoint for PCM audio streaming to browser spokes.

Endpoint: /ws/audio-stream
Protocol:
  - Binary frames: 3856 bytes each (16B header + 3840B PCM)
  - Text frames: JSON clock sync messages

On connect:
  1. Hub accepts WebSocket; enters receive loop immediately (no burst yet)
  2. Spoke runs NTP clock sync via text messages
  3. Spoke sends {"type": "ready_for_audio"} after sync completes
  4. Hub sends ring buffer burst (up to 2.5s), then adds spoke to live broadcast

Clock sync (NTP 4-timestamp model, text messages on the same connection):
  All timestamps are in **seconds** (float64).
  Spoke uses ``audioContext.currentTime`` (audio hardware clock), hub uses ``time.perf_counter()``.

  Request:  {"type": "clock_sync", "t1": <spoke_send_time_sec>}
  Response: {"type": "clock_sync_reply", "t1": ..., "t2": ..., "t3": ...}

  The spoke records t4 = performance.now()/1000 on receipt, then computes:
    RTT    = (t4 - t1) - (t3 - t2)
    offset = ((t2 - t1) + (t3 - t4)) / 2

  offset represents (hub_clock - spoke_clock).  To convert a hub play_at
  timestamp to spoke-local time:  spoke_time = play_at - offset
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import statistics
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger
from fastapi import WebSocket, WebSocketDisconnect
from ui.api.routes.websocket_auth import (
    get_websocket_session_context,
    get_websocket_spoke_credential,
    websocket_is_authorized,
)
from ui.core.security import check_websocket_origin, reject_websocket
from voice.synthesis.tts_wire import TTS_PREFIX, encode_tts_frame

if TYPE_CHECKING:
    from audio_core.streaming.chunk_stamper import ChunkStamper
    from fastapi import FastAPI

logger = get_logger(__name__)

# Per-spoke send queue capacity (~1 second of audio at 50 fps)
_SPOKE_QUEUE_MAX = 50

# Slow-spoke eviction: if queue stays >80% full for this many seconds, evict
_EVICTION_QUEUE_THRESHOLD = int(_SPOKE_QUEUE_MAX * 0.8)  # 40
_EVICTION_TIME_SEC = 10.0

# WebSocket close code for slow spoke eviction
_WS_CLOSE_SLOW = 4001

# Number of frames to send as a resume burst after background tab returns
_RESUME_BURST_FRAMES = 10

# ── Per-spoke send-timeline telemetry ──────────────────────────────────
# Passive per-second ring of {enqueued, sent, dropped, queue_depth} per
# spoke, retained ~15 min so a field starvation episode (e.g. an iPhone
# spoke's jitter buffer bouncing 0-180ms) can be compared after the fact
# against the phone-side diagnostics timeline: if the hub's ``sent``
# column held ~50/s through the episode, the loss is in transit or on the
# client; if ``sent`` stalled while ``enqueued`` kept flowing, it's the
# hub's socket write path.  Counting is O(1) per frame and everything
# runs on the asyncio event loop (broadcast + drain), so no locks.
_SEND_TIMELINE_SECONDS = 900  # ring length: 900 one-second buckets (~15 min)
_SEND_RATE_NOMINAL_FPS = 50  # ChunkStamper cadence (20 ms frames)
# A closed one-second bucket counts as "stamper active" when at least this
# many frames were enqueued for the spoke that second.
_SEND_RATE_WARN_MIN_ENQUEUED = 40
# While active and unpaused, fewer socket writes than this in one second is
# a material deviation from the ~50/s cadence -> throttled WARNING so the
# episode lands timestamped in the log even if nobody is watching.
_SEND_RATE_WARN_SENT_FLOOR = 35
_SEND_RATE_WARN_THROTTLE_SEC = 30.0


def _epoch_now() -> int:
    """Current wall-clock second (epoch).  Wall clock, not monotonic, so the
    hub timeline can be paired against phone-side logs after an incident."""
    return int(time.time())


class _SpokeSendTimeline:
    """Per-second ring of send accounting for one spoke.

    Buckets are stored as ``(epoch_sec, enqueued, sent, dropped, queue_depth)``
    tuples in a bounded deque; the in-progress bucket lives in plain int
    fields.  All mutation happens on the event loop, so no locking.
    ``sent`` counts frames actually written to the WebSocket by the drain
    task — not what the broadcast enqueued; the difference IS the signal.
    """

    __slots__ = (
        "bucket_epoch",
        "dropped",
        "enqueued",
        "last_warn_monotonic",
        "queue_depth",
        "ring",
        "sent",
    )

    def __init__(self) -> None:
        self.ring: deque[tuple[int, int, int, int, int]] = deque(maxlen=_SEND_TIMELINE_SECONDS)
        self.bucket_epoch = 0
        self.enqueued = 0
        self.sent = 0
        self.dropped = 0
        self.queue_depth = 0
        self.last_warn_monotonic = 0.0

    def note(
        self,
        epoch_sec: int,
        enqueued: int,
        sent: int,
        dropped: int,
        queue_depth: int,
    ) -> tuple[int, int, int, int, int] | None:
        """Record counts into the current bucket (O(1)).

        Returns the just-closed bucket when ``epoch_sec`` rolls past the
        current bucket, else None.  Seconds with no events produce no
        bucket (absent == nothing flowed).
        """
        closed: tuple[int, int, int, int, int] | None = None
        if epoch_sec != self.bucket_epoch:
            if self.bucket_epoch:
                closed = (
                    self.bucket_epoch,
                    self.enqueued,
                    self.sent,
                    self.dropped,
                    self.queue_depth,
                )
                self.ring.append(closed)
            self.bucket_epoch = epoch_sec
            self.enqueued = 0
            self.sent = 0
            self.dropped = 0
        self.enqueued += enqueued
        self.sent += sent
        self.dropped += dropped
        self.queue_depth = queue_depth
        return closed

    def snapshot(self) -> list[dict[str, int | bool]]:
        """Return the ring as JSON-friendly dicts, oldest first, plus the
        in-progress bucket marked ``partial``."""
        buckets: list[dict[str, int | bool]] = [
            {"t": t, "enqueued": e, "sent": s, "dropped": d, "queue_depth": q} for t, e, s, d, q in self.ring
        ]
        if self.bucket_epoch:
            buckets.append(
                {
                    "t": self.bucket_epoch,
                    "enqueued": self.enqueued,
                    "sent": self.sent,
                    "dropped": self.dropped,
                    "queue_depth": self.queue_depth,
                    "partial": True,
                }
            )
        return buckets


# Canonical browser-spoke sync config. Hub-local playback was removed, so
# this target describes the remote spoke jitter buffer, not a hub speaker.
SPOKE_SYNC_ANCHOR_MS = 60.0
SPOKE_BUFFER_TARGET_MS = 280.0

# Maximum ring buffer frames to send at initial spoke connection.
# Must match spoke buffer target (14 chunks = 280ms) to prevent burst
# overshoot that triggers perturbation freeze in the drift corrector.
# Spoke trims to bufferTargetChunks+1 anyway, but sending exactly target
# avoids the trim entirely and keeps the initial buffer depth clean.
_INITIAL_BURST_MAX_FRAMES = 14
_BROWSER_SPOKE_DEVICE_PREFIX = "browser-spoke:"
_TTS_PREFIX = TTS_PREFIX
_active_audio_stream_manager: AudioStreamManager | None = None


def _clean_spoke_room_name(value: object) -> str | None:
    """Return a display-safe spoke room name from URL/register metadata."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("'\"")
    if not cleaned:
        return None
    return cleaned[:100]


def _normalize_spoke_room_id(value: str) -> str:
    """Return a stable registry id for a browser-spoke room."""
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-._")
    if not normalized:
        normalized = "speaker"
    if normalized == "local":
        normalized = "spoke-local"
    return normalized


def _room_id_to_display_name(room_id: str) -> str:
    """Convert a QR room slug into a human-friendly room name."""
    return " ".join(part for part in room_id.replace("-", " ").split()).title() or "Speaker"


# Stash key for the authenticated user_id on WebSocket.state.  The /ws/audio-stream
# handler resolves it once at the route entry (after websocket_is_authorized passes)
# and downstream methods (spoke registration, mark-offline) consume it from there
# so per-user room registry scoping is applied uniformly.
_WS_STATE_USER_ID_ATTR = "viola_audio_stream_user_id"

# Stash key for the paired device id behind a spoke credential.  Recorded once
# at route entry (the credential is already being verified there) so the room a
# spoke serves can be written next to its device in the paired-device registry
# — that is what lets a user revoke ONE speaker by name (#4434).
_WS_STATE_SPOKE_DEVICE_ATTR = "viola_spoke_device_id"


async def _resolve_authenticated_ws_user_id(ws: WebSocket) -> str | None:
    """Return the authenticated user_id for a WS that already passed websocket_is_authorized.

    Tries the spoke-token credential first (sync) — paired browser spokes carry the
    owning desktop principal on VerifiedSpokeCredential.hub_user_id.  Falls back to the
    cookie/bearer session context.  Returns None only if neither auth path established
    a user; that indicates a deployment misconfiguration (the WS should not have been
    authorized in the first place) and the caller must refuse to register the spoke
    into the room registry to avoid cross-user pollution.
    """
    try:
        # Worker-thread hop: credential verification reads the spoke secret
        # file and can harden the secret dir (icacls subprocess) on first use
        # — never on the event loop (2026-07-01 starvation conviction).
        cred = await asyncio.to_thread(get_websocket_spoke_credential, ws)
        if cred is not None and getattr(cred, "device_id", None):
            try:
                setattr(ws.state, _WS_STATE_SPOKE_DEVICE_ATTR, str(cred.device_id))
            except AttributeError:
                logger.debug("Could not stash spoke device_id on ws.state", exc_info=True)
        if cred is not None and getattr(cred, "hub_user_id", None):
            return str(cred.hub_user_id)
    except (AttributeError, TypeError, RuntimeError, ValueError, ImportError):
        logger.debug(
            "Spoke credential resolution failed during /ws/audio-stream user_id lookup",
            exc_info=True,
        )

    try:
        ctx = await get_websocket_session_context(ws)
        if ctx is not None and getattr(ctx, "user_id", None):
            return str(ctx.user_id)
    except (
        AttributeError,
        TypeError,
        RuntimeError,
        ValueError,
        ImportError,
        ConnectionError,
    ):
        logger.debug(
            "Session context resolution failed during /ws/audio-stream user_id lookup",
            exc_info=True,
        )

    return None


@dataclass
class _SpokeState:
    """Per-spoke state for non-blocking send queues and lifecycle tracking."""

    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=_SPOKE_QUEUE_MAX))
    drain_task: asyncio.Task | None = None
    drops: int = 0
    consecutive_full_secs: float = 0.0
    last_check_time: float = field(default_factory=time.monotonic)
    paused: bool = False
    timeline: _SpokeSendTimeline = field(default_factory=_SpokeSendTimeline)


class AudioStreamManager:
    """Manages binary WebSocket connections for PCM audio streaming.

    Bridges the synchronous ChunkStamper tick thread to async WebSocket
    sends via ``call_soon_threadsafe``.  Each connected spoke receives
    the same continuous stream of binary frames.
    """

    def __init__(self, stamper: ChunkStamper, app: FastAPI | None = None) -> None:
        self._stamper = stamper
        self._app = app
        self._spokes: dict[WebSocket, _SpokeState] = {}
        self._spoke_info: dict[WebSocket, dict] = {}
        self._spoke_id_counter: int = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._frames_broadcast: int = 0
        # FPS tracking: snapshot-based for recent rate
        self._fps_snapshot_time: float = 0.0
        self._fps_snapshot_count: int = 0
        self._current_fps: float = 0.0
        # Config: last broadcast target_total_ms for change detection
        self._last_target_total_ms: float = 0.0
        # Spoke count change callback (called in thread pool)
        self._on_spoke_count_change: object = None
        # Ring buffer snapshot for resume bursts (kept by _broadcast)
        self._recent_frames: list[bytes] = []
        # Cache last successful diagnostic data per spoke room (for slow WiFi spokes)
        self._last_diag_cache: dict[str, dict] = {}

        # ── Cross-spoke sync calibration ──────────────────────────────
        # Tracks per-spoke effective playout delay (EMA-smoothed) to compute
        # padding corrections that normalize rendered timing across devices.
        # Effective delay includes sequence backlog, scheduler headroom, and
        # output latency; no platform detection is involved.
        self._spoke_headroom_ema: dict[str, float] = {}  # room → EMA headroom ms
        self._spoke_effective_delay_ema: dict[str, float] = {}  # room → EMA effective delay ms
        self._spoke_intrinsic_delay_ema: dict[str, float] = {}  # room → EMA delay before applied padding
        self._spoke_calibration_components: dict[str, dict[str, float | int | None]] = {}
        self._spoke_padding: dict[str, int] = {}  # room → computed padding ms
        self._spoke_sync_reports: dict[str, int] = {}  # room → count of reports received
        self._spoke_reanchor_counts: dict[str, int] = {}
        self._headroom_ema_alpha = 0.2  # stable startup/skew smoothing
        self._late_join_ema_alpha = 0.5  # faster repaired-spoke convergence after a mature peer exists
        self._paired_startup_ema_alpha = 0.5
        self._paired_startup_report_window = 20
        self._late_join_peer_report_floor = 20
        self._late_join_report_window = 12
        self._calibration_min_reports = 3  # wait for a short stable window before padding
        self._startup_padding_delay_sec = 12.0
        self._max_padding_ms = 200  # sanity cap

    # ------------------------------------------------------------------ #
    # ChunkStamper output callback (called from stamper tick thread)      #
    # ------------------------------------------------------------------ #

    def on_stamper_frame(self, frame: bytes) -> None:
        """Output callback registered on the ChunkStamper.

        Called from the stamper's daemon thread every ~20ms.  Schedules
        an async broadcast on the event loop via ``call_soon_threadsafe``.
        """
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._schedule_broadcast, frame)
        except RuntimeError:
            logger.debug("Audio stream loop closed before broadcast could be scheduled")

    def _schedule_broadcast(self, frame: bytes) -> None:
        """Create a broadcast task on the event loop thread."""
        fut = asyncio.ensure_future(self._broadcast(frame))
        fut.add_done_callback(self._broadcast_error_handler)

    def broadcast_tts_pcm(self, pcm_mono_int16: bytes, sample_rate: int = SAMPLE_RATE_16K) -> None:
        """Broadcast a TTS PCM payload to every connected browser spoke.

        The payload is raw mono / int16 PCM.  The wire frame carries explicit
        sample-rate metadata after the shared ``b"TTS\\x00"`` prefix so browser
        spokes can play Kokoro's native-rate output without assuming 16 kHz.
        """
        if not pcm_mono_int16 or not self._spokes or self._loop is None:
            return

        frame = encode_tts_frame(pcm_mono_int16, sample_rate)
        coro = self._broadcast_tts_frame(frame)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            coro.close()
            return
        fut.add_done_callback(self._broadcast_error_handler)

    @staticmethod
    def _broadcast_error_handler(fut: asyncio.Future) -> None:
        """Log unhandled broadcast exceptions instead of swallowing them."""
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            import logging

            logging.getLogger(__name__).error("Broadcast error: %s", exc, exc_info=exc)

    async def _broadcast_tts_frame(self, frame: bytes) -> None:
        """Enqueue a TTS frame for connected spokes without adding it to the resume ring."""
        if not self._spokes:
            return

        epoch = _epoch_now()
        for ws, state in list(self._spokes.items()):
            if state.paused:
                continue

            try:
                state.queue.put_nowait(frame)
                self._note_send_event(ws, state, epoch, enqueued=1)
            except asyncio.QueueFull:
                dropped = 0
                enqueued = 0
                try:
                    state.queue.get_nowait()
                    dropped = 1
                except asyncio.QueueEmpty:
                    logger.debug("TTS spoke queue emptied before oldest frame could be dropped")
                try:
                    state.queue.put_nowait(frame)
                    enqueued = 1
                except asyncio.QueueFull:
                    state.drops += 1
                    dropped += 1
                    logger.debug("Dropped TTS frame for full spoke queue")
                self._note_send_event(ws, state, epoch, enqueued=enqueued, dropped=dropped)
            except Exception:
                logger.debug("Failed to queue TTS frame for spoke", exc_info=True)

    async def _broadcast(self, frame: bytes) -> None:
        """Enqueue a binary frame for every connected spoke.

        Uses per-spoke queues (Item 1) so one slow spoke never blocks
        others.  If a spoke's queue is full, the oldest frame is dropped
        and the spoke is evaluated for eviction (Item 2).
        """
        if not self._spokes:
            return

        # Keep a small ring of recent frames for resume bursts (Item 4)
        self._recent_frames.append(frame)
        if len(self._recent_frames) > _RESUME_BURST_FRAMES:
            self._recent_frames.pop(0)

        now = time.monotonic()
        epoch = _epoch_now()
        evict: list[WebSocket] = []

        for ws, state in list(self._spokes.items()):
            # Skip paused spokes (background tab — Item 4)
            if state.paused:
                continue

            try:
                state.queue.put_nowait(frame)
                self._note_send_event(ws, state, epoch, enqueued=1)
            except asyncio.QueueFull:
                # Drop oldest, enqueue new (only this spoke loses a frame)
                dropped = 0
                enqueued = 0
                try:
                    state.queue.get_nowait()
                    dropped = 1
                except asyncio.QueueEmpty:
                    logger.debug("Spoke queue emptied before oldest frame could be dropped")
                try:
                    state.queue.put_nowait(frame)
                    enqueued = 1
                except asyncio.QueueFull:
                    dropped += 1
                    logger.debug("Spoke queue remained full after dropping oldest frame")
                state.drops += 1
                self._note_send_event(ws, state, epoch, enqueued=enqueued, dropped=dropped)

            # --- Slow-spoke eviction check (Item 2) ---
            q_size = state.queue.qsize()
            elapsed = now - state.last_check_time
            if elapsed >= 1.0:
                if q_size > _EVICTION_QUEUE_THRESHOLD:
                    state.consecutive_full_secs += elapsed
                else:
                    state.consecutive_full_secs = 0.0
                state.last_check_time = now

            if state.consecutive_full_secs >= _EVICTION_TIME_SEC:
                info = self._spoke_info.get(ws, {})
                room = info.get("room_name", "unknown")
                logger.warning(
                    "Evicting slow spoke %s (queue full for %.1fs, drops=%d)",
                    room,
                    state.consecutive_full_secs,
                    state.drops,
                )
                evict.append(ws)

        for ws in evict:
            await self._evict_spoke(ws)

        self._frames_broadcast += 1
        # Update FPS every 2 seconds
        if self._fps_snapshot_time == 0.0:
            self._fps_snapshot_time = now
            self._fps_snapshot_count = self._frames_broadcast
        else:
            elapsed = now - self._fps_snapshot_time
            if elapsed >= 2.0:
                delta_frames = self._frames_broadcast - self._fps_snapshot_count
                self._current_fps = delta_frames / elapsed
                self._fps_snapshot_time = now
                self._fps_snapshot_count = self._frames_broadcast

    async def _drain_spoke(self, ws: WebSocket, state: _SpokeState) -> None:
        """Drain loop: pull frames from the spoke's queue and send via WS."""
        try:
            while True:
                frame = await state.queue.get()
                await ws.send_bytes(frame)
                # Count only after the write completes — the timeline's
                # ``sent`` column must reflect what actually left on the
                # socket, not what was dequeued.
                self._note_send_event(ws, state, _epoch_now(), sent=1)
        except asyncio.CancelledError:
            logger.debug("Spoke drain loop cancelled")
        except Exception:
            # Mark spoke as dead — the connection handler's finally block
            # will clean up via _remove_spoke.
            logger.debug(
                "Drain loop error for spoke; connection handler will clean up",
                exc_info=True,
            )

    def _note_send_event(
        self,
        ws: WebSocket,
        state: _SpokeState,
        epoch_sec: int,
        *,
        enqueued: int = 0,
        sent: int = 0,
        dropped: int = 0,
    ) -> None:
        """O(1) timeline update; emits the throttled deviation WARNING when a
        closed bucket shows the socket writes falling off the ~50/s cadence."""
        closed = state.timeline.note(epoch_sec, enqueued, sent, dropped, state.queue.qsize())
        if closed is not None:
            self._maybe_warn_send_rate(ws, state, closed)

    def _maybe_warn_send_rate(
        self,
        ws: WebSocket,
        state: _SpokeState,
        closed: tuple[int, int, int, int, int],
    ) -> None:
        """Log a throttled WARNING when an active, unpaused spoke's sent-rate
        deviated materially from the stamper cadence in the closed bucket.

        Over-send (resume-burst catch-up) is expected recovery behavior and
        is not warned on; the starvation signal is under-send while frames
        kept being enqueued.
        """
        bucket_epoch, enqueued, sent, dropped, queue_depth = closed
        if state.paused:
            return
        if enqueued < _SEND_RATE_WARN_MIN_ENQUEUED:
            return  # stamper wasn't at cadence for this spoke; not "active"
        if sent >= _SEND_RATE_WARN_SENT_FLOOR:
            return
        now = time.monotonic()
        if now - state.timeline.last_warn_monotonic < _SEND_RATE_WARN_THROTTLE_SEC:
            return
        state.timeline.last_warn_monotonic = now
        room = self._spoke_info.get(ws, {}).get("room_name", "unknown")
        logger.warning(
            "spoke-send-rate-deviation room=%s sec=%d enqueued=%d sent=%d dropped=%d queue_depth=%d expected~%d/s",
            room,
            bucket_epoch,
            enqueued,
            sent,
            dropped,
            queue_depth,
            _SEND_RATE_NOMINAL_FPS,
        )

    def get_send_timelines(self) -> dict[str, list[dict[str, int | bool]]]:
        """Per-spoke send timeline: room -> ring of per-second buckets
        ``{t, enqueued, sent, dropped, queue_depth[, partial]}`` (~15 min).

        Exposed through the sync-diag and streaming-metrics endpoints so a
        field starvation episode can be paired against the spoke's own
        diagnostics timeline after the fact.
        """
        timelines: dict[str, list[dict[str, int | bool]]] = {}
        for ws, state in self._spokes.items():
            room = self._spoke_info.get(ws, {}).get("room_name", "unknown")
            timelines[room] = state.timeline.snapshot()
        return timelines

    async def _evict_spoke(self, ws: WebSocket) -> None:
        """Close a slow spoke's WebSocket and clean up its state."""
        state = self._spokes.pop(ws, None)
        if state and state.drain_task:
            state.drain_task.cancel()
        info = self._spoke_info.pop(ws, None)
        self._mark_spoke_room_offline(info)
        room = info.get("room_name", "") if isinstance(info, dict) else ""
        if room:
            self._cleanup_spoke_calibration(room)
        try:
            # ws-established-close: evicts an already-connected, live spoke (accepted long ago in handle_connection()).
            await ws.close(code=_WS_CLOSE_SLOW, reason="network_too_slow")
        except Exception:
            logger.debug("Failed to close evicted spoke WebSocket cleanly", exc_info=True)
        await self._reset_unpaired_spoke_calibration()
        self._notify_spoke_count()

    def _remove_spoke(self, ws: WebSocket) -> bool:
        """Remove a spoke and cancel its drain task.

        Returns True if the spoke was in the active set.
        """
        info = self._spoke_info.get(ws, {})
        room = info.get("room_name", "")
        state = self._spokes.pop(ws, None)
        info = self._spoke_info.pop(ws, None)
        self._mark_spoke_room_offline(info)
        if state is not None:
            if state.drain_task:
                state.drain_task.cancel()
            if room:
                self._cleanup_spoke_calibration(room)
            return True
        return False

    async def _send_resume_burst(self, ws: WebSocket) -> None:
        """Send recent frames to a spoke that just resumed from background."""
        state = self._spokes.get(ws)
        if state is None:
            return
        epoch = _epoch_now()
        for frame in self._recent_frames:
            try:
                state.queue.put_nowait(frame)
                self._note_send_event(ws, state, epoch, enqueued=1)
            except asyncio.QueueFull:
                break

    # ------------------------------------------------------------------ #
    # Spoke count change callback                                         #
    # ------------------------------------------------------------------ #

    def set_spoke_change_callback(self, callback: object) -> None:
        """Set callback invoked (in thread pool) when spoke count changes.

        The callback receives a single int argument: the new spoke count.
        It is run via ``loop.run_in_executor`` to avoid blocking the
        event loop (the callback may perform COM/pycaw operations).
        """
        self._on_spoke_count_change = callback

    def _notify_spoke_count(self) -> None:
        """Fire the spoke count change callback in a dedicated daemon thread.

        IMPORTANT: Uses threading.Thread instead of run_in_executor.
        The callback drives the demand-driven source pipeline sync
        (_on_device_count_changed -> _sync_source_pipeline_demand), which starts
        or stops ProcTap capture + the ChunkStamper to match consumer demand.
        Starting the ProcTap capture subprocess can block for seconds on Windows.
        Placing that on the asyncio thread pool via run_in_executor would saturate
        the pool and make ALL FastAPI sync route handlers time out.  A plain daemon
        thread is not managed by the asyncio executor, so it never blocks API
        request serving.
        """
        cb = self._on_spoke_count_change
        if cb is None or not callable(cb):
            return
        count = len(self._spokes)
        t = threading.Thread(
            target=cb,
            args=(count,),
            daemon=True,
            name=f"spoke-count-cb-{count}",
        )
        t.start()

    # ------------------------------------------------------------------ #
    # WebSocket connection handler                                        #
    # ------------------------------------------------------------------ #

    async def handle_connection(self, ws: WebSocket) -> None:
        """Handle a single spoke WebSocket connection lifecycle.

        Flow:
          1. Accept WebSocket and enter receive loop immediately
          2. Spoke runs NTP clock sync probes (text messages)
          3. Spoke sends ``ready_for_audio`` after sync completes
          4. Hub sends ring buffer burst, then adds spoke to live broadcast
        """
        origin = ws.headers.get("origin", "unknown")
        logger.info("Audio stream: connection attempt (origin=%s)", origin)

        if not await websocket_is_authorized(ws, allow_spoke_token=True):
            logger.info("Audio stream: unauthorized WebSocket rejected (origin=%s)", origin)
            await reject_websocket(ws, code=4003, reason="Unauthorized")
            return

        await ws.accept()
        logger.info("Audio stream: WebSocket accepted (origin=%s)", origin)
        logger.info("Audio stream: spoke connected (origin=%s)", origin)

        # Capture event loop reference on first connection
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        # Spoke must complete clock sync before receiving audio.
        # It sends {"type": "ready_for_audio"} when ready.
        spoke_activated = False
        first_sync_logged = False

        try:
            while True:
                msg = await ws.receive()
                msg_type = msg.get("type", "")
                if msg_type == "websocket.disconnect":
                    break

                if "text" in msg:
                    # Record t2 IMMEDIATELY — before JSON parsing —
                    # to minimize clock sync measurement error.
                    # Must use perf_counter to match ChunkStamper's play_at clock domain.
                    t2_receive = time.perf_counter()
                    raw = msg["text"]

                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    kind = data.get("type")
                    if kind == "clock_sync":
                        if not first_sync_logged:
                            first_sync_logged = True
                            logger.info("Audio stream: first clock sync probe from spoke")
                        await self._handle_clock_sync(ws, data, t2_receive)
                    elif kind == "ready_for_audio" and not spoke_activated:
                        spoke_activated = True
                        await self._send_burst_and_activate(ws)
                    elif kind == "register":
                        await self._register_spoke(ws, data)
                    elif kind == "diagnostics_report":
                        self.handle_diagnostics_report(ws, data)
                    elif kind == "sync_report":
                        await self._handle_sync_report(ws, data)
                    elif kind == "pause_audio":
                        state = self._spokes.get(ws)
                        if state:
                            state.paused = True
                            logger.debug("Spoke paused audio (background tab)")
                    elif kind == "resume_audio":
                        state = self._spokes.get(ws)
                        if state:
                            state.paused = False
                            logger.debug("Spoke resumed audio (foreground tab)")
                            asyncio.ensure_future(self._send_resume_burst(ws))

                # Binary from spoke is ignored (spoke is receive-only for PCM)
        except WebSocketDisconnect:
            logger.debug("Audio stream spoke disconnected")
        except Exception:
            logger.exception("Audio stream WebSocket error")
        finally:
            room_name = self._spoke_info.get(ws, {}).get("room_name", "unknown")
            was_active = self._remove_spoke(ws)
            logger.info(
                "Audio stream: spoke disconnected (room=%s, total=%d)",
                room_name,
                len(self._spokes),
            )
            if was_active:
                await self._reset_unpaired_spoke_calibration()
                self._notify_spoke_count()

    # ------------------------------------------------------------------ #
    # Clock sync (NTP-style)                                              #
    # ------------------------------------------------------------------ #

    async def _handle_clock_sync(
        self,
        ws: WebSocket,
        data: dict,
        t2_receive: float,
    ) -> None:
        """Handle a clock_sync probe from the spoke.

        NTP 4-timestamp model (all values in seconds):
          t1 = spoke send time  (performance.now()/1000)  — from spoke
          t2 = hub receive time (time.perf_counter())      — captured by caller
          t3 = hub send time    (time.perf_counter())      — captured below
          t4 = spoke receive time (performance.now()/1000) — spoke records

        Spoke computes:
          RTT    = (t4 - t1) - (t3 - t2)
          offset = ((t2 - t1) + (t3 - t4)) / 2   (hub_clock - spoke_clock)
        """
        t1 = data.get("t1", 0)
        t3 = time.perf_counter()

        reply = json.dumps(
            {
                "type": "clock_sync_reply",
                "t1": t1,
                "t2": t2_receive,
                "t3": t3,
            }
        )
        try:
            await ws.send_text(reply)
        except Exception:
            logger.debug("Failed to send clock sync reply to spoke", exc_info=True)

    # ------------------------------------------------------------------ #
    # Burst + activation                                                  #
    # ------------------------------------------------------------------ #

    async def _send_burst_and_activate(self, ws: WebSocket) -> None:
        """Send config + ring buffer burst and add spoke to the live broadcast set.

        Called when the spoke signals ``ready_for_audio`` after completing
        clock sync.  Sends the config text frame FIRST so the spoke knows
        its buffer target before any binary audio arrives.
        """
        # --- Send config BEFORE burst (Break 1 + Break 4) ---
        config = self._build_config_payload()
        info = self._spoke_info.get(ws, {})
        room = info.get("room_name", "unknown")
        config["padding_ms"] = 0
        try:
            await ws.send_text(json.dumps(config))
            logger.info(
                "Audio stream: sent config to spoke (target_total_ms=%.1f, padding_ms=%d, room=%s)",
                config["target_total_ms"],
                config["padding_ms"],
                room,
            )
        except Exception:
            logger.debug("Failed to send config to spoke")
            return

        # --- Send ring buffer burst ---
        ring_snapshot = self._stamper.get_ring_snapshot()
        # Trim to most recent _INITIAL_BURST_MAX_FRAMES to prevent burst
        # accumulation at the spoke. Spoke buffer target is ~7-12 frames;
        # sending 2.5s (125 frames) causes 2-second delay before live sync.
        if len(ring_snapshot) > _INITIAL_BURST_MAX_FRAMES:
            ring_snapshot = ring_snapshot[-_INITIAL_BURST_MAX_FRAMES:]
        logger.info(
            "Audio stream: spoke ready, sending burst (%d frames)",
            len(ring_snapshot),
        )

        for i, frame in enumerate(ring_snapshot):
            try:
                # Log sample values of first and last burst frame.
                if i == 0 or i == len(ring_snapshot) - 1:
                    # Parse header: play_at(f64) + seq(u32) + flags(u16) + reserved(u16) = 16 bytes
                    _flags = struct.unpack_from("<H", frame, 12)[0]
                    _is_silence = bool(_flags & 0x0001)
                    pcm_payload = frame[16:]
                    if len(pcm_payload) >= 4 and not _is_silence:
                        # Read a few Int16 samples to check range.
                        _samples = [
                            struct.unpack_from("<h", pcm_payload, j * 2)[0]
                            for j in range(min(20, len(pcm_payload) // 2))
                        ]
                        _max_s = max(_samples)
                        _min_s = min(_samples)
                        logger.info(
                            "Audio stream: burst frame %d/%d: isSilence=%s, sample_range=[%d, %d], pcm_len=%d",
                            i,
                            len(ring_snapshot) - 1,
                            _is_silence,
                            _min_s,
                            _max_s,
                            len(pcm_payload),
                        )
                    else:
                        logger.info(
                            "Audio stream: burst frame %d: silence or too short, pcm_len=%d",
                            i,
                            len(pcm_payload),
                        )
                await ws.send_bytes(frame)
                if i == 0:
                    logger.info("Audio stream: first binary frame sent to spoke")
            except Exception:
                logger.debug("Failed to send ring burst frame to spoke")
                return

        state = _SpokeState()
        state.drain_task = asyncio.create_task(self._drain_spoke(ws, state))
        self._spokes[ws] = state
        logger.info(
            "Audio stream: spoke activated (total=%d, burst=%d frames)",
            len(self._spokes),
            len(ring_snapshot),
        )
        self._notify_spoke_count()

        # Push stored volume/mute state on reconnect
        info = self._spoke_info.get(ws)
        if info is not None:
            volume = info.get("volume", 80)
            muted = info.get("muted", False)
            try:
                await ws.send_text(json.dumps({"type": "set_volume", "volume": volume / 100.0}))
                if muted:
                    await ws.send_text(json.dumps({"type": "set_mute", "muted": True}))
            except Exception:
                logger.debug("Failed to push volume/mute to spoke on activate")

    # ------------------------------------------------------------------ #
    # Track change notification                                            #
    # ------------------------------------------------------------------ #

    async def _broadcast_track_change(self) -> None:
        """Notify all spokes that a track transition occurred."""
        msg = json.dumps({"type": "track_change"})
        for ws in list(self._spokes):
            try:
                await ws.send_text(msg)
            except (RuntimeError, WebSocketDisconnect, OSError):
                logger.debug("Failed to notify spoke of track change", exc_info=True)
        logger.info("Broadcast track_change to %d spokes", len(self._spokes))

    def notify_track_change(self) -> None:
        """Thread-safe track change notification (called from playback thread)."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._broadcast_track_change()))

    # ------------------------------------------------------------------ #
    # Spoke registration                                                  #
    # ------------------------------------------------------------------ #

    def _get_actual_buffer_ms(self) -> int:
        """Return the browser-spoke jitter-buffer target in ms.

        With hub-local playback removed, this is the remote-spoke target.
        Used by the config payload (``hub_buffer_ms``) and by the multiroom
        diagnostics endpoint.
        """
        return round(SPOKE_BUFFER_TARGET_MS)

    def _build_config_payload(self) -> dict:
        """Build the config payload to send to spokes.

        Hub-local playback has been removed.  The desktop is an unsync'd
        source; spokes sync with *each other* via identical timing params.

        sync_anchor_ms — playout timing offset.  Every spoke gets the
            same fixed value so they all schedule audio at the same
            wall-clock instant.  60ms is a good latency-vs-stability
            tradeoff: low enough for tight sync, high enough to absorb
            typical LAN jitter (~5-15ms).

        min_buffer_ms — jitter buffer floor.  Minimum ms of audio the
            spoke must buffer before starting playback and the target
            depth for the drift corrector.  280ms (14 chunks) matches
            the spoke BUFFER_TARGET_CHUNKS default.

        target_total_ms — legacy field for backward compatibility with
            older spoke builds that don't read sync_anchor_ms.

        The per-spoke padding calibration system (_spoke_padding,
        _spoke_effective_delay_ema) runs on top of these base values to
        normalize playout timing across devices with different audio
        pipeline depths and late-join backlog.
        """
        # Fixed playout offset — all spokes get the same value for
        # inter-spoke synchronization.  No hub-local pipeline to match.
        sync_anchor_ms = SPOKE_SYNC_ANCHOR_MS

        # Jitter buffer floor.  280ms (14 chunks) matches WiFi equilibrium
        # center (~260-340ms).  DriftCorrector correction_term stays near 0
        # because buffer naturally sits at target.  Empirically proven in
        # golden config soak tests (iter29b: correction_term=0, 2-4ms sync).
        min_buffer_ms = SPOKE_BUFFER_TARGET_MS

        # Legacy field: old spokes use this as their only timing target.
        target_total_ms = max(75.0, sync_anchor_ms)

        logger.info(
            "Sync config: sync_anchor=%.0fms, min_buffer=%.0fms",
            sync_anchor_ms,
            min_buffer_ms,
        )
        return {
            "type": "config",
            "target_total_ms": round(target_total_ms, 1),
            "sync_anchor_ms": round(sync_anchor_ms, 1),
            "min_buffer_ms": round(min_buffer_ms, 1),
            "hub_buffer_ms": self._get_actual_buffer_ms(),
            "padding_ms": 0,  # Overridden per-spoke after calibration
        }

    async def check_and_broadcast_buffer_change(self) -> bool:
        """Check if target_total_ms changed; broadcast update if so.

        Called from sync-diag endpoint. Returns True if a broadcast was sent.
        """
        config = self._build_config_payload()
        target_ms = round(config["target_total_ms"])
        if target_ms == round(self._last_target_total_ms):
            return False

        self._last_target_total_ms = config["target_total_ms"]
        logger.info(
            "Config changed, broadcasting target_total_ms=%.1f to %d spokes",
            config["target_total_ms"],
            len(self._spokes),
        )

        dead: list[WebSocket] = []
        startup_padding_guard_active = self._startup_padding_guard_active()
        for ws in list(self._spokes):
            info = self._spoke_info.get(ws, {})
            room = info.get("room_name", "unknown")
            padding = 0 if startup_padding_guard_active else self._spoke_padding.get(room, 0)
            spoke_config = {**config, "padding_ms": padding}
            try:
                await ws.send_text(json.dumps(spoke_config))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._remove_spoke(ws)

        return True

    async def _register_spoke(self, ws: WebSocket, data: dict) -> None:
        """Store spoke metadata."""
        room_id, room_name = self._resolve_spoke_room_identity(ws, data)

        # Remove stale entry for the same room (reconnection before cleanup).
        # Only evict if the old WebSocket is no longer actively streaming —
        # a healthy spoke that shares a room name must NOT be disconnected.
        # This prevents iPhone spokes from being evicted when Playwright
        # test spokes connect with the same default room_name="Speaker".
        stale = [
            old_ws
            for old_ws, old_info in self._spoke_info.items()
            if (old_info.get("registry_room_id") == room_id or old_info.get("room_name") == room_name)
            and old_ws is not ws
            and old_ws not in self._spokes  # not actively streaming
        ]
        for old_ws in stale:
            self._spoke_info.pop(old_ws, None)
            logger.info(
                "Removed stale metadata for room %s (replaced by reconnection)",
                room_name,
            )

        self._spoke_id_counter += 1

        config = self._build_config_payload()
        self._last_target_total_ms = config["target_total_ms"]
        self._register_spoke_room(room_id, room_name, ws)
        await self._label_spoke_device_room(room_id, ws)

        info = {
            "id": self._spoke_id_counter,
            "registry_room_id": room_id,
            "room_name": room_name,
            "user_id": getattr(getattr(ws, "state", None), _WS_STATE_USER_ID_ATTR, None),
            "buffer_ms": config["hub_buffer_ms"],
            "target_total_ms": config["target_total_ms"],
            "volume": 80,
            "muted": False,
            "connected_at": time.monotonic(),
            "is_local": False,
        }
        self._spoke_info[ws] = info

        logger.info(
            "Spoke registered: id=%d, registry_room_id=%s, room_name=%s, target_total_ms=%.1f",
            info["id"],
            info["registry_room_id"],
            info["room_name"],
            config["target_total_ms"],
        )

    def _resolve_spoke_room_identity(self, ws: WebSocket, data: dict) -> tuple[str, str]:
        """Resolve the durable room id/name for a browser spoke registration."""
        query_params = getattr(ws, "query_params", {})
        requested_room = _clean_spoke_room_name(query_params.get("room"))
        registered_name = _clean_spoke_room_name(data.get("room_name"))
        source_name = requested_room or registered_name or "Speaker"
        room_id = _normalize_spoke_room_id(source_name)
        room_name = _room_id_to_display_name(room_id) if requested_room else source_name
        return room_id, room_name

    def _register_spoke_room(self, room_id: str, room_name: str, ws: WebSocket) -> None:
        """Register a connected browser spoke in the durable room registries.

        Per-user scoping invariant: every registry mutation MUST be addressed by
        the authenticated owner's user_id so user A's spoke cannot land in user B's
        registry instance.  The route handler stashes the resolved user_id on
        ``ws.state.viola_audio_stream_user_id`` after auth succeeds; absence here
        means an auth bypass or misrouting and we refuse to register.
        """
        client = getattr(ws, "client", None)
        client_host = client.host if client else None
        device_id = "%s%s" % (_BROWSER_SPOKE_DEVICE_PREFIX, room_id)

        user_id = getattr(getattr(ws, "state", None), _WS_STATE_USER_ID_ATTR, None)
        if not isinstance(user_id, str) or not user_id:
            logger.error(
                "Refusing to register spoke room %s: authenticated user_id missing on ws.state "
                "(/ws/audio-stream auth bypass or misroute)",
                room_id,
            )
            return

        try:
            from services.multiroom.room_registry import RoomInfo, get_room_registry

            registry = get_room_registry(user_id=user_id)
            registry.register_room(
                RoomInfo(
                    id=room_id,
                    name=room_name,
                    device_id=device_id,
                    ip_address=client_host,
                    is_local=False,
                    status="online",
                    last_seen=time.time(),
                )
            )
        except Exception:
            logger.exception("Failed to register browser spoke room %s", room_id)

        try:
            from core.room_registry import get_room_registry as get_core_room_registry

            get_core_room_registry(user_id=user_id).register_remote_room(room_id, room_name)
        except Exception:
            logger.debug(
                "Failed to mirror browser spoke room into StateHub registry",
                exc_info=True,
            )

    async def _label_spoke_device_room(self, room_id: str, ws: WebSocket) -> None:
        """Record which room this paired device serves (#4434).

        That label is what lets the user revoke "Kitchen" instead of an opaque
        device id. Worker-thread hop: the registry resolves the secret
        directory, which can harden it (icacls subprocess) on first use, and
        this runs while spoke audio is streaming.
        """
        spoke_device_id = getattr(getattr(ws, "state", None), _WS_STATE_SPOKE_DEVICE_ATTR, None)
        if not isinstance(spoke_device_id, str) or not spoke_device_id:
            return
        try:
            from ui.security.spoke_device_registry import set_device_room

            await asyncio.to_thread(set_device_room, spoke_device_id, room_id)
        except (OSError, ValueError, RuntimeError, ImportError):
            logger.debug("Could not label spoke device with its room", exc_info=True)

    def _mark_spoke_room_offline(self, info: dict | None) -> None:
        """Mark a browser-spoke room offline when its WS disconnects."""
        if not isinstance(info, dict):
            return
        room_id = info.get("registry_room_id")
        if not isinstance(room_id, str) or not room_id:
            return
        user_id = info.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            # No user_id captured at registration -> nothing was written to a
            # user-scoped registry, so there is nothing to mark offline.
            return
        if any(
            isinstance(other_info, dict)
            and other_info.get("registry_room_id") == room_id
            and other_info.get("user_id") == user_id
            for other_info in self._spoke_info.values()
        ):
            return
        try:
            from services.multiroom.room_registry import get_room_registry

            get_room_registry(user_id=user_id).mark_offline(room_id)
        except Exception:
            logger.debug("Failed to mark browser spoke room offline: %s", room_id, exc_info=True)

    def get_rooms(self) -> list[dict]:
        """Return list of connected spoke info dicts for the rooms API."""
        rooms: list[dict] = []
        for ws, info in self._spoke_info.items():
            if ws in self._spokes:
                state = self._spokes[ws]
                rooms.append(
                    {
                        "id": info["id"],
                        "room_name": info["room_name"],
                        "buffer_ms": info.get("buffer_ms", round(SPOKE_BUFFER_TARGET_MS)),
                        "volume": info["volume"],
                        "muted": info.get("muted", False),
                        "connected_at": info["connected_at"],
                        "is_local": info["is_local"],
                        "is_hub": False,
                        "queue_depth": state.queue.qsize(),
                        "drops": state.drops,
                        "paused": state.paused,
                    }
                )
        return rooms

    async def set_spoke_volume(self, spoke_id: int, volume: int) -> bool:
        """Send a volume command to a specific spoke by ID.

        Returns True if the spoke was found and the message was sent.
        """
        for ws, info in list(self._spoke_info.items()):
            if info["id"] == spoke_id and ws in self._spokes:
                clamped = max(0, min(100, volume))
                info["volume"] = clamped
                try:
                    msg = json.dumps(
                        {
                            "type": "set_volume",
                            "volume": clamped / 100.0,
                        }
                    )
                    await ws.send_text(msg)
                    return True
                except Exception:
                    logger.debug("Failed to send volume to spoke %d", spoke_id)
                    return False
        return False

    async def set_spoke_mute(self, spoke_id: int, muted: bool) -> bool:
        """Send a mute command to a specific spoke by ID.

        Returns True if the spoke was found and the message was sent.
        """
        for ws, info in list(self._spoke_info.items()):
            if info["id"] == spoke_id and ws in self._spokes:
                info["muted"] = muted
                try:
                    msg = json.dumps(
                        {
                            "type": "set_mute",
                            "muted": muted,
                        }
                    )
                    await ws.send_text(msg)
                    return True
                except Exception:
                    logger.debug("Failed to send mute to spoke %d", spoke_id)
                    return False
        return False

    # ------------------------------------------------------------------ #
    # Diagnostics request/response                                        #
    # ------------------------------------------------------------------ #

    async def request_spoke_diagnostics(self, request_id: str, timeout_sec: float = 2.0) -> dict:
        """Send a diagnostics request to all connected spokes and collect responses.

        Args:
            request_id: Unique request identifier for correlating responses.
            timeout_sec: Maximum time to wait for spoke responses.

        Returns:
            Dict mapping spoke room names to their diagnostics data.
            Includes ``_unresponsive`` list for spokes that didn't reply in time.
        """
        if not self._spokes:
            return {"_unresponsive": []}

        # Prepare the future collector
        pending_spokes: dict[WebSocket, str] = {}
        for ws in self._spokes:
            info = self._spoke_info.get(ws, {})
            pending_spokes[ws] = info.get("room_name", "unknown")

        results: dict[str, Any] = {}
        response_event = asyncio.Event()

        # Store collector state so _handle_diagnostics_report can fill it
        self._diag_request_id = request_id
        self._diag_results = results
        self._diag_expected = len(pending_spokes)
        self._diag_event = response_event

        # Send request to all spokes
        msg = json.dumps({"type": "request_diagnostics", "request_id": request_id})
        dead: list[WebSocket] = []
        for ws in pending_spokes:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self._remove_spoke(ws)
            pending_spokes.pop(ws, None)

        # Wait for responses (or timeout)
        if pending_spokes:
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout_sec)
            except TimeoutError:
                logger.debug("Timed out waiting for spoke diagnostics responses")

        # Clear collector state
        self._diag_request_id = None
        self._diag_event = None

        # Cache successful responses; backfill unresponsive with stale cache
        responded_rooms = set(results.keys())
        for room in responded_rooms:
            self._last_diag_cache[room] = results[room]

        unresponsive: list[str] = []
        for room in pending_spokes.values():
            if room not in responded_rooms:
                cached = self._last_diag_cache.get(room)
                if cached:
                    results[room] = {**cached, "_stale": True}
                else:
                    unresponsive.append(room)
        results["_unresponsive"] = unresponsive

        # Compute cross-spoke sync stats (Item 7)
        results["_sync_stats"] = self.compute_sync_stats(results)

        return results

    def handle_diagnostics_report(self, ws: WebSocket, data: dict) -> None:
        """Handle an incoming diagnostics_report from a spoke.

        Called from the WebSocket receive loop when a text message with
        ``type: diagnostics_report`` arrives. The room is resolved from the
        server-bound registration (``_spoke_info``), never the client-supplied
        ``room_name``, so a registered spoke cannot poison another room's
        diagnostics bucket (round_6 S8 room-spoof).
        """
        req_id = data.get("request_id")
        if req_id != getattr(self, "_diag_request_id", None):
            return

        room = self._spoke_info.get(ws, {}).get("room_name", "unknown")
        report_data = data.get("data", {})

        results = getattr(self, "_diag_results", None)
        if results is not None:
            results[room] = report_data

        event = getattr(self, "_diag_event", None)
        expected = getattr(self, "_diag_expected", 0)
        if event is not None and results is not None and len(results) >= expected:
            event.set()

    # ------------------------------------------------------------------ #
    # Cross-spoke sync calibration                                        #
    # ------------------------------------------------------------------ #

    def _sync_report_ema_alpha(self, room: str) -> tuple[float, bool, bool]:
        """Return smoothing alpha plus startup/late-join fast-path flags."""
        reports = self._spoke_sync_reports.get(room, 0)
        mature_peer_reports = [
            count
            for peer, count in self._spoke_sync_reports.items()
            if peer != room and count >= self._late_join_peer_report_floor
        ]
        is_late_join = bool(mature_peer_reports) and reports <= self._late_join_report_window
        if is_late_join:
            return self._late_join_ema_alpha, False, True
        paired_startup = (
            self._active_spoke_room_count() >= 2
            and bool(self._spoke_sync_reports)
            and all(count <= self._paired_startup_report_window for count in self._spoke_sync_reports.values())
        )
        if paired_startup:
            return self._paired_startup_ema_alpha, True, False
        return self._headroom_ema_alpha, False, False

    def _active_spoke_room_count(self) -> int:
        """Return the number of distinct active rooms currently streaming."""
        return len({self._spoke_info.get(ws, {}).get("room_name", "unknown") for ws in self._spokes})

    def _active_spokes_min_age_sec(self) -> float:
        """Return the youngest active spoke connection age."""
        if not self._spokes:
            return 0.0
        now = time.monotonic()
        ages: list[float] = []
        for ws in self._spokes:
            connected_at = self._spoke_info.get(ws, {}).get("connected_at")
            if isinstance(connected_at, bool) or not isinstance(connected_at, (int, float)):
                return 0.0
            connected = float(connected_at)
            if not math.isfinite(connected):
                return 0.0
            ages.append(max(0.0, now - connected))
        return min(ages) if ages else 0.0

    def _startup_padding_guard_active(self) -> bool:
        """Return True while a newly formed pair must stay on zero padding."""
        return (
            self._active_spoke_room_count() >= 2 and self._active_spokes_min_age_sec() < self._startup_padding_delay_sec
        )

    def _has_spoke_calibration(self) -> bool:
        """Return True when cross-spoke calibration state could affect playback."""
        return any(
            (
                self._spoke_headroom_ema,
                self._spoke_effective_delay_ema,
                self._spoke_intrinsic_delay_ema,
                self._spoke_calibration_components,
                self._spoke_padding,
                self._spoke_sync_reports,
                self._spoke_reanchor_counts,
            )
        )

    def _clear_all_spoke_calibration(self) -> None:
        """Clear all cross-spoke calibration state."""
        self._spoke_headroom_ema.clear()
        self._spoke_effective_delay_ema.clear()
        self._spoke_intrinsic_delay_ema.clear()
        self._spoke_calibration_components.clear()
        self._spoke_padding.clear()
        self._spoke_sync_reports.clear()
        self._spoke_reanchor_counts.clear()

    async def _reset_unpaired_spoke_calibration(self) -> None:
        """Remove stale cross-spoke padding once fewer than two rooms remain."""
        if self._active_spoke_room_count() >= 2 or not self._has_spoke_calibration():
            return

        self._clear_all_spoke_calibration()
        config = self._build_config_payload()
        config["padding_ms"] = 0
        payload = json.dumps(config)
        for ws_conn in list(self._spokes):
            try:
                await ws_conn.send_text(payload)
            except (RuntimeError, WebSocketDisconnect):
                logger.debug(
                    "Failed to send zero-padding config to unpaired spoke",
                    exc_info=True,
                )

    @staticmethod
    def _finite_report_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    async def _handle_sync_report(self, ws: WebSocket, data: dict) -> None:
        """Process a periodic sync_report from a spoke.

        Updates EMA-smoothed effective playout delay for the spoke and
        recomputes per-spoke padding corrections when 2+ spokes are calibrated.
        If corrections change, broadcasts updated config to affected spokes.
        """
        # Server-bound room only; ignore client room_name post-registration so a
        # registered spoke cannot poison another room's sync calibration (round_6 S8 room-spoof).
        room = self._spoke_info.get(ws, {}).get("room_name", "unknown")
        if self._active_spoke_room_count() < 2:
            await self._reset_unpaired_spoke_calibration()
            return
        headroom = data.get("headroom_ms", 0)
        if not isinstance(headroom, (int, float)) or headroom <= 0:
            return
        output_latency = data.get("output_latency_ms", 0)
        if not isinstance(output_latency, (int, float)) or output_latency < 0:
            output_latency = 0
        applied_padding = data.get("applied_padding_ms", 0)
        if not isinstance(applied_padding, (int, float)) or applied_padding < 0:
            applied_padding = 0

        pipeline_ms: float | None = None
        playing_seq = data.get("currently_playing_seq")
        try:
            hub_metrics = self._stamper.get_metrics()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            hub_metrics = {}
        hub_seq = hub_metrics.get("sequence") if isinstance(hub_metrics, dict) else None
        if isinstance(hub_seq, (int, float)) and isinstance(playing_seq, (int, float)) and playing_seq >= 0:
            pipeline_ms = max(0.0, (float(hub_seq) - float(playing_seq)) * 20.0)

        render_normalized_ms = self._finite_report_number(data.get("sequence_normalized_render_ms"))
        render_hub_time_ms = self._finite_report_number(data.get("render_hub_time_ms"))
        re_anchor_count_raw = self._finite_report_number(data.get("re_anchor_count"))
        re_anchor_count = (
            int(re_anchor_count_raw) if re_anchor_count_raw is not None and re_anchor_count_raw >= 0 else None
        )
        previous_re_anchor_count = self._spoke_reanchor_counts.get(room)
        reanchor_fast_path = (
            re_anchor_count is not None
            and previous_re_anchor_count is not None
            and re_anchor_count > previous_re_anchor_count
        )
        if re_anchor_count is not None:
            self._spoke_reanchor_counts[room] = re_anchor_count
        if reanchor_fast_path:
            self._spoke_headroom_ema.pop(room, None)
            self._spoke_effective_delay_ema.pop(room, None)
            self._spoke_intrinsic_delay_ema.pop(room, None)
            self._spoke_padding.pop(room, None)
            self._spoke_sync_reports[room] = 0
            config = self._build_config_payload()
            config["padding_ms"] = 0
            try:
                await ws.send_text(json.dumps(config))
            except (RuntimeError, WebSocketDisconnect):
                logger.debug(
                    "Failed to send zero-padding config after spoke reanchor",
                    exc_info=True,
                )

        if pipeline_ms is not None:
            calibration_source = "audible_pipeline"
            effective_delay = pipeline_ms + float(headroom) + float(output_latency)
        elif render_normalized_ms is not None:
            calibration_source = "render_tap_normalized"
            effective_delay = render_normalized_ms
        else:
            calibration_source = "headroom_pipeline"
            effective_delay = float(headroom) + float(output_latency)
        intrinsic_delay = max(0.0, effective_delay - float(applied_padding))
        alpha, paired_startup_fast_path, late_join_fast_path = self._sync_report_ema_alpha(room)

        # EMA-smooth the raw headroom for diagnostics and the effective delay
        # for actual padding decisions.
        prev = self._spoke_headroom_ema.get(room)
        if prev is None:
            self._spoke_headroom_ema[room] = float(headroom)
        else:
            self._spoke_headroom_ema[room] = alpha * headroom + (1 - alpha) * prev

        prev_effective = self._spoke_effective_delay_ema.get(room)
        if prev_effective is None:
            self._spoke_effective_delay_ema[room] = effective_delay
        else:
            self._spoke_effective_delay_ema[room] = alpha * effective_delay + (1 - alpha) * prev_effective
        prev_intrinsic = self._spoke_intrinsic_delay_ema.get(room)
        if prev_intrinsic is None:
            self._spoke_intrinsic_delay_ema[room] = intrinsic_delay
        else:
            self._spoke_intrinsic_delay_ema[room] = alpha * intrinsic_delay + (1 - alpha) * prev_intrinsic
        self._spoke_calibration_components[room] = {
            "headroom_ms": float(headroom),
            "pipeline_ms": pipeline_ms,
            "output_latency_ms": float(output_latency),
            "applied_padding_ms": float(applied_padding),
            "effective_delay_ms": effective_delay,
            "intrinsic_delay_ms": intrinsic_delay,
            "calibration_source": calibration_source,
            "calibration_alpha": alpha,
            "paired_startup_fast_path": paired_startup_fast_path,
            "late_join_fast_path": late_join_fast_path,
            "reanchor_fast_path": reanchor_fast_path,
            "re_anchor_count": re_anchor_count,
            "render_hub_time_ms": render_hub_time_ms,
            "sequence_normalized_render_ms": render_normalized_ms,
            "hub_sequence": int(hub_seq) if isinstance(hub_seq, (int, float)) else None,
            "currently_playing_seq": (int(playing_seq) if isinstance(playing_seq, (int, float)) else None),
        }

        self._spoke_sync_reports[room] = self._spoke_sync_reports.get(room, 0) + 1

        # Need 2+ calibrated spokes to compute corrections
        calibrated = {
            r: h
            for r, h in self._spoke_intrinsic_delay_ema.items()
            if self._spoke_sync_reports.get(r, 0) >= self._calibration_min_reports
        }
        if len(calibrated) < 2:
            return

        startup_padding_guard_active = (
            self._startup_padding_guard_active() and not reanchor_fast_path and not late_join_fast_path
        )
        if startup_padding_guard_active:
            zero_padding = {r: 0 for r in calibrated}
            changed = any(self._spoke_padding.get(r, 0) != 0 for r in calibrated)
            self._spoke_padding = zero_padding
            for r in calibrated:
                self._spoke_calibration_components.setdefault(r, {})["startup_padding_guard_active"] = True
            if changed:
                for ws_conn in list(self._spokes):
                    info = self._spoke_info.get(ws_conn, {})
                    spoke_room = info.get("room_name", "unknown")
                    config = self._build_config_payload()
                    config["padding_ms"] = zero_padding.get(spoke_room, 0)
                    try:
                        await ws_conn.send_text(json.dumps(config))
                    except (RuntimeError, WebSocketDisconnect, OSError):
                        logger.debug(
                            "Failed to send startup zero-padding config to spoke",
                            exc_info=True,
                        )
            return

        # Compute padding from intrinsic delay: the spoke with highest
        # unpadded delay gets 0 padding. Faster spokes get positive padding.
        max_delay = max(calibrated.values())
        new_padding: dict[str, int] = {}
        for r, h in calibrated.items():
            raw = round(max_delay - h)
            new_padding[r] = min(raw, self._max_padding_ms)

        # Only broadcast if any correction changed by >5ms
        changed = False
        for r, p in new_padding.items():
            if abs(p - self._spoke_padding.get(r, 0)) > 5:
                changed = True
                break

        if not changed:
            return

        self._spoke_padding = new_padding
        logger.info(
            "Sync calibration: intrinsic_delay=%s, padding=%s",
            {r: round(h) for r, h in calibrated.items()},
            new_padding,
        )

        # Broadcast per-spoke config with individual padding
        for ws_conn in list(self._spokes):
            info = self._spoke_info.get(ws_conn, {})
            spoke_room = info.get("room_name", "unknown")
            padding = new_padding.get(spoke_room, 0)
            config = self._build_config_payload()
            config["padding_ms"] = padding
            try:
                await ws_conn.send_text(json.dumps(config))
            except (RuntimeError, WebSocketDisconnect, OSError):
                logger.debug("Failed to send sync calibration config to spoke", exc_info=True)

    def _cleanup_spoke_calibration(self, room: str) -> None:
        """Remove calibration data for a disconnected spoke."""
        self._spoke_headroom_ema.pop(room, None)
        self._spoke_effective_delay_ema.pop(room, None)
        self._spoke_intrinsic_delay_ema.pop(room, None)
        self._spoke_calibration_components.pop(room, None)
        self._spoke_padding.pop(room, None)
        self._spoke_sync_reports.pop(room, None)
        self._spoke_reanchor_counts.pop(room, None)

    # ------------------------------------------------------------------ #
    # Metrics                                                             #
    # ------------------------------------------------------------------ #

    def get_metrics(self) -> dict[str, int | float]:
        """Return stream manager metrics for monitoring."""
        total_drops = sum(s.drops for s in self._spokes.values())
        return {
            "connected_spokes": len(self._spokes),
            "frames_broadcast": self._frames_broadcast,
            "frames_per_sec": round(self._current_fps, 1),
            "total_drops": total_drops,
        }

    def get_spoke_info_snapshot(self) -> list[dict]:
        """Return connection info for all active spokes (no diagnostics request needed)."""
        snapshot = []
        for ws, info in self._spoke_info.items():
            if ws in self._spokes:
                entry = dict(info)
                state = self._spokes[ws]
                entry["queue_depth"] = state.queue.qsize()
                entry["drops"] = state.drops
                entry["paused"] = state.paused
                snapshot.append(entry)
        return snapshot

    # ------------------------------------------------------------------ #
    # Sync measurement (Item 7)                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def compute_sync_stats(diagnostics: dict) -> dict:
        """Compute cross-spoke sync statistics from diagnostics data.

        For each pair of spokes, computes the difference in
        ``currentPlayTimeHub`` — a proxy for playout offset.

        Args:
            diagnostics: Dict mapping room names to spoke diagnostics
                         (as returned by ``request_spoke_diagnostics``).

        Returns:
            Dict with mean/p50/p95/max offset in ms and per-pair breakdown.
        """
        # Collect rooms that have hub play time data
        rooms: list[tuple[str, float]] = []
        for room, data in diagnostics.items():
            if room.startswith("_"):
                continue
            play_time = 0.0
            if isinstance(data, dict):
                play_time = data.get("currentPlayTimeHub", 0.0)
            if play_time > 0:
                rooms.append((room, play_time))

        if len(rooms) < 2:
            return {
                "mean_offset_ms": 0.0,
                "p50_offset_ms": 0.0,
                "p95_offset_ms": 0.0,
                "max_offset_ms": 0.0,
                "spoke_pairs": [],
            }

        # Compute pairwise offsets
        offsets: list[float] = []
        pairs: list[dict] = []
        for i in range(len(rooms)):
            for j in range(i + 1, len(rooms)):
                name_a, time_a = rooms[i]
                name_b, time_b = rooms[j]
                offset_ms = abs(time_a - time_b) * 1000
                offsets.append(offset_ms)
                pairs.append(
                    {
                        "spoke_a": name_a,
                        "spoke_b": name_b,
                        "offset_ms": round(offset_ms, 2),
                    }
                )

        offsets.sort()
        n = len(offsets)
        p50_idx = n // 2
        p95_idx = min(int(n * 0.95), n - 1)

        return {
            "mean_offset_ms": round(statistics.mean(offsets), 2),
            "p50_offset_ms": round(offsets[p50_idx], 2),
            "p95_offset_ms": round(offsets[p95_idx], 2),
            "max_offset_ms": round(offsets[-1], 2),
            "spoke_pairs": pairs,
        }


def register_audio_stream_ws(
    app: FastAPI,
    stamper: ChunkStamper,
) -> AudioStreamManager:
    """
    Register the ``/ws/audio-stream`` binary WebSocket endpoint.

    Creates an :class:`AudioStreamManager`, wires it as a ChunkStamper
    output, and registers the FastAPI WebSocket route.

    Args:
        app: FastAPI application instance.
        stamper: ChunkStamper providing continuous binary frames.

    Returns:
        The AudioStreamManager (stored on ``app.state`` by caller for teardown).
    """
    global _active_audio_stream_manager

    manager = AudioStreamManager(stamper, app=app)
    _active_audio_stream_manager = manager

    @app.websocket("/ws/audio-stream")
    async def ws_audio_stream(ws: WebSocket) -> None:
        client_host = ws.client.host if ws.client else None
        if not check_websocket_origin(ws, client_host=client_host):
            await reject_websocket(ws, code=1008, reason="Origin not allowed")
            return
        # Auth rules:
        # - If VIOLA_SPOKE_TOKEN is unset, preserve single-device/dev behavior.
        # - If it is set, allow either a verified session cookie or a matching
        #   X-Spoke-Token so registered spokes can still receive hub audio.
        # - Rejected before handle_connection() via reject_websocket (accepts
        #   then closes so the real code/reason still reaches the client).
        if not await websocket_is_authorized(ws, allow_spoke_token=True):
            await reject_websocket(ws, code=4003, reason="Unauthorized")
            return
        # Resolve the authenticated user_id ONCE here so every downstream room
        # registry mutation (spoke registration, mark-offline) is correctly
        # user-scoped.  Refusing the connection if neither auth path exposed a
        # user_id prevents the spoke from being written into the userless/global
        # registry (which would let user A's spoke pollute user B's room namespace).
        ws_user_id = await _resolve_authenticated_ws_user_id(ws)
        if not ws_user_id:
            logger.warning(
                "/ws/audio-stream: rejecting connection - auth passed but no user_id "
                "could be resolved from spoke credential or session context"
            )
            await reject_websocket(ws, code=4003, reason="Unauthorized")
            return
        try:
            setattr(ws.state, _WS_STATE_USER_ID_ATTR, ws_user_id)
        except AttributeError:
            logger.debug("Could not stash user_id on ws.state", exc_info=True)
        await manager.handle_connection(ws)

    # Wire stamper → manager so every frame is broadcast to spokes
    stamper.add_output(manager.on_stamper_frame)

    logger.info("Binary audio stream endpoint registered at /ws/audio-stream")
    return manager


def get_active_audio_stream_manager() -> AudioStreamManager | None:
    """Return the currently registered audio-stream manager, if any."""
    return _active_audio_stream_manager


__all__ = [
    "AudioStreamManager",
    "get_active_audio_stream_manager",
    "register_audio_stream_ws",
]
