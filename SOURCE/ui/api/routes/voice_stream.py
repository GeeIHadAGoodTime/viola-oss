"""
WebSocket endpoint for browser-based voice streaming with wake word detection.

Spoke browsers stream 16kHz mono int16 PCM audio to the hub.
The hub runs ViolaWake inference on the stream and sends back
``wake_detected`` events when the wake word is recognised.

After a wake detection the hub also sends the spoke's subsequent
audio to the transcription pipeline (``/v1/transcribe``) and returns
the command result so the spoke can display feedback.

Protocol — messages from client:
  Text: {"type": "start_listening"} -> activates wake-word streaming
  Text: {"type": "stop_listening"}  -> deactivates wake-word streaming
  Text: {"type": "ptt_start"}       -> begin push-to-talk command capture
                                       (bypasses wake detection; subsequent
                                       PCM frames are captured as a command)
  Text: {"type": "ptt_stop"}        -> end PTT capture and run the command
  Binary: Raw PCM audio (16kHz, mono, int16)

Protocol — messages to client:
  {"type": "listening_status", "active": true/false}
  {"type": "ptt_status", "active": true/false}
  {"type": "wake_detected", "timestamp": <unix_ms>}
  {"type": "capture_ended"} -> server-side silence/timeout endpointing ended
                                a command capture the client never explicitly
                                stopped (e.g. a tap-mode PTT turn); no ptt_stop
                                was sent, so this is the client's only signal
                                to stop streaming and arm a dropped-turn
                                backstop (see useVoiceWs.js).
  {"type": "command_result", "transcript": "...", "response": "...",
   "cap_state": {...}}  -> cap_state is present ONLY when the turn was denied
                           for spending the managed-AI plan allowance, so the
                           client can offer an upgrade route beside a reply it
                           can otherwise only speak aloud (candidate C-077).
  Binary: TTS PCM audio prefixed with b"TTS\\x00" and sample-rate metadata
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from backend.launch_kill_switches import get_store
from core.constants import AUDIO_INT16_SCALE, SAMPLE_RATE_16K
from core.logging_config import get_logger
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from services.llm.managed_budget import cap_state_from_response
from ui.api.routes.transcription import TRANSCRIBE_FILE_FIELD
from ui.api.routes.websocket_auth import (
    get_websocket_spoke_credential,
    websocket_is_authorized,
)
from ui.core.security import check_websocket_origin, reject_websocket
from voice.synthesis.tts_wire import TTS_PREFIX, encode_tts_frame
from voice.wake_detector.wake.context import WakeContext, WakeDecisionResult
from voice.wake_detector.wake_decision_policy import WakeDecisionPolicy, get_wake_policy

if TYPE_CHECKING:
    from voice.wake_detector.spoke_wake_diag import SpokeDiagnostics

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# CONFIRM-6 warm-keeping: bounded server-side counterpart.
#
# The browser client (ui/react-app/src/hooks/useVoiceWs.js) keeps ONE
# /ws/voice-stream connection open across multiple push-to-talk turns instead
# of reconnecting every turn — that skips the single-use ws-auth-token mint
# (POST /v1/ws/auth) plus the WS handshake round trip on every command, paying
# them once per warm session instead of once per turn. Two bounded safeguards
# keep an idle-but-open connection from becoming a server-side resource leak
# from "a million warm tabs" (CLAUDE.md R7 / CONFIRM-6 guardrail):
#
#   1. Idle receive-timeout: a connection that receives NOTHING (no PCM, no
#      ptt_start/stop, no client keepalive ping) for VOICE_STREAM_IDLE_TIMEOUT_S
#      is closed server-side. The client pings roughly every 20s while
#      idle-but-open (well under this timeout), so a live tab never trips it;
#      an abandoned/dead socket is reaped instead of held open forever. Today's
#      code has NO idle protection at all, so this is a strict improvement, not
#      a behavior regression for any existing client.
#   2. Per-user concurrent-connection cap: bounds how many idle-kept-open
#      voice-stream sockets one user_id can hold at once (covers a handful of
#      tabs/devices) so a scripted or compromised client cannot grow unbounded
#      server-side state — each open connection holds one numpy ring buffer
#      (~96 KB) and, when a wake engine is loaded, one isolated engine fork.
# ---------------------------------------------------------------------------

VOICE_STREAM_IDLE_TIMEOUT_S = 90.0
MAX_CONCURRENT_VOICE_STREAM_PER_USER = 4


class _VoiceStreamConnectionLimiter:
    """Bounded per-user concurrent ``/ws/voice-stream`` connection counter.

    Single-process, in-memory — matches the existing ``_engine`` shared
    template pattern in this module. The cloud app runs one process per
    container and every call here happens on the asyncio event loop with no
    ``await`` between check-and-increment, so a plain dict needs no lock.
    """

    def __init__(self, max_per_user: int) -> None:
        self._max_per_user = max_per_user
        self._counts: dict[str, int] = {}

    def try_acquire(self, user_id: str) -> bool:
        """Reserve a slot for ``user_id``. Returns False when at the cap."""
        current = self._counts.get(user_id, 0)
        if current >= self._max_per_user:
            return False
        self._counts[user_id] = current + 1
        return True

    def release(self, user_id: str) -> None:
        """Free a previously-acquired slot. Safe to call even if none was held."""
        current = self._counts.get(user_id, 0)
        if current <= 1:
            self._counts.pop(user_id, None)
        else:
            self._counts[user_id] = current - 1

    def current_count(self, user_id: str) -> int:
        """Test/diagnostic hook: slots currently held by ``user_id``."""
        return self._counts.get(user_id, 0)


_connection_limiter = _VoiceStreamConnectionLimiter(MAX_CONCURRENT_VOICE_STREAM_PER_USER)


async def _receive_or_idle_timeout(ws: WebSocket, timeout_s: float) -> dict:
    """Await one WS message, or a disconnect-shaped sentinel on idle timeout.

    Wraps ``ws.receive()`` in ``asyncio.wait_for`` so a connection that goes
    completely silent (no PCM, no client message) for ``timeout_s`` is treated
    exactly like a real disconnect by the caller's existing
    ``msg_type == "websocket.disconnect"`` branch — no separate code path
    needed in the main loop.
    """
    try:
        return await asyncio.wait_for(ws.receive(), timeout=timeout_s)
    except TimeoutError:
        return {"type": "websocket.disconnect", "code": 1000}


async def _handle_ping(session: _SpokeVoiceSession) -> None:
    """Reply to a client keepalive ping with a small JSON pong frame."""
    try:
        await session.ws.send_text(json.dumps({"type": "pong"}))
    except (RuntimeError, OSError, WebSocketDisconnect):
        # A dead/closing socket mid-ping is normal churn (the disconnect will
        # surface on the next receive); never let it kill the receive loop.
        logger.debug("Voice stream: failed to send pong (room=%s)", session.room)


# ---------------------------------------------------------------------------
# ViolaWake engine template — loaded once to share read-only model resources.
# Each connection gets an isolated runtime fork because wake detection
# history/debounce/user binding are mutable per audio stream.
# ---------------------------------------------------------------------------

_engine = None
_engine_lock = asyncio.Lock()
_CLIP_SAMPLES: int = 24000  # default, overwritten on first load
_TTS_PREFIX = TTS_PREFIX


def _fork_wake_engine_runtime(engine):
    fork_runtime_state = getattr(engine, "fork_runtime_state", None)
    if not callable(fork_runtime_state):
        logger.error("ViolaWake engine does not support isolated spoke runtime state")
        return None
    try:
        return fork_runtime_state()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        logger.error("Failed to fork isolated ViolaWake spoke runtime: %s", exc)
        return None


async def _get_engine():
    """Lazy-load the ViolaWake template and return an isolated runtime fork."""
    global _engine, _CLIP_SAMPLES

    async with _engine_lock:
        if _engine is not None:
            return _fork_wake_engine_runtime(_engine)

        try:
            from config.wake_config import get_violawake_model_path
            from violawake import CLIP_SAMPLES as CS, DEFAULT_THRESHOLD, ViolaWake

            _CLIP_SAMPLES = CS

            model_path = get_violawake_model_path(None)
            if model_path is None:
                model_path = Path("violawake_data/trained_models/temporal_cnn.onnx")

            if not Path(model_path).exists():
                logger.warning(
                    "ViolaWake model not found at %s — spoke wake detection disabled",
                    model_path,
                )
                return None

            engine = ViolaWake(
                model_path=str(model_path),
                threshold=DEFAULT_THRESHOLD,
                debounce_seconds=2.0,
            )
            _engine = engine
            logger.info("ViolaWake engine loaded for spoke streams: %s", model_path)
            return _fork_wake_engine_runtime(engine)

        except Exception:
            logger.exception("Failed to load ViolaWake engine for spoke streams")
            return None


async def _get_ws_user_context(ws: WebSocket) -> tuple[str | None, str | None]:
    """Resolve the authenticated user_id and session token for a voice socket."""
    from ui.api.routes.websocket_auth import get_websocket_session_context

    session_context = await get_websocket_session_context(ws)
    if session_context is None:
        # Worker-thread hop: spoke verification reads the secret file and can
        # harden the secret dir (icacls subprocess) on first use — never on
        # the event loop (2026-07-01 starvation conviction).
        spoke_credential = await asyncio.to_thread(get_websocket_spoke_credential, ws)
        if spoke_credential is not None:
            return spoke_credential.hub_user_id, None
        return None, None

    return session_context.user_id, session_context.session_token


def _normalize_required_user_id(user_id: str | None) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("Voice stream requires an authenticated user_id")
    return user_id.strip()


def _wake_audio_int16(audio_buffer: np.ndarray) -> np.ndarray:
    clipped = np.clip(audio_buffer, -1.0, 1.0)
    return np.clip(clipped * AUDIO_INT16_SCALE, -AUDIO_INT16_SCALE, AUDIO_INT16_SCALE - 1).astype(np.int16)


def _wake_audio_rms(audio_int16: np.ndarray) -> float:
    if audio_int16.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio_int16.astype(np.float32) ** 2)))


def _evaluate_spoke_wake_decision(
    score: float,
    audio_buffer: np.ndarray,
    *,
    frame_count: int,
    wake_policy: WakeDecisionPolicy | None = None,
) -> WakeDecisionResult:
    policy = wake_policy or get_wake_policy()
    audio_int16 = _wake_audio_int16(audio_buffer)
    context = WakeContext(
        wake_score=float(score),
        mic_rms=_wake_audio_rms(audio_int16),
        vad_confidence=policy.estimate_vad_from_audio(audio_int16=audio_int16),
        timestamp=time.time(),
        frame_count=frame_count,
    )
    return policy.final_trigger_decision(context)


def _resolve_voice_stream_tts(app: FastAPI) -> object | None:
    """Best-effort lookup for the app's TTS engine or proxy."""
    candidates: list[object | None] = [
        getattr(app.state, "tts", None),
        getattr(app.state, "intent_pipeline", None),
    ]

    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        candidates.append(getattr(scheduler, "_intent_pipeline", None))

    watchdog = getattr(app.state, "health_watchdog", None)
    if watchdog is not None:
        candidates.append(getattr(watchdog, "_intent", None))

    api_context = getattr(app.state, "api_context", None)
    if api_context is not None:
        bindings = getattr(api_context, "bindings", None)
        if bindings is not None:
            candidates.append(getattr(bindings, "intent", None))

    for candidate in candidates:
        if candidate is None:
            continue

        pipeline = getattr(candidate, "_pipeline", None)
        if pipeline is not None:
            tts = getattr(pipeline, "tts", None)
            if tts is not None:
                return tts

        tts_engine = getattr(candidate, "tts_engine", None)
        if tts_engine is not None:
            return tts_engine

        tts = getattr(candidate, "tts", None)
        if tts is not None:
            return tts

        if hasattr(candidate, "synthesize"):
            return candidate

    return None


def _resolve_voice_stream_asr(app: FastAPI) -> object | None:
    """Return the in-process ASR engine attached to app.state, if any.

    Cloud attaches this in the fail-closed startup block (backend/cloud_app.py)
    behind the voice-stream route flag. When present, the command handler
    transcribes in-process instead of looping back to /v1/transcribe, which is
    not registered on the cloud surface.
    """
    return getattr(app.state, "asr", None)


def _resolve_voice_stream_command(app: FastAPI) -> object | None:
    """Return the in-process command dispatcher on app.state, if any.

    Cloud attaches ``cloud_intent_dispatcher`` in startup. When present, the
    command handler dispatches in-process (with the WS-authenticated user_id)
    instead of looping back to /v1/command, which cannot be authenticated from
    the WS context (the session token is a one-shot ws-auth ticket, not a
    session cookie, so the loopback 403s on CSRF).
    """
    dispatcher = getattr(app.state, "cloud_intent_dispatcher", None)
    if dispatcher is not None and callable(getattr(dispatcher, "dispatch", None)):
        return dispatcher
    return None


async def _transcribe_in_process(asr: object, wav_bytes: bytes) -> str:
    """Transcribe a WAV payload with an in-process ASR engine (no HTTP loopback).

    The ASR port takes a file path, so the in-memory WAV is written to a
    short-lived temp file the engine reads and we delete immediately after.
    """
    import os
    import tempfile

    transcribe = getattr(asr, "transcribe", None)
    if not callable(transcribe):
        # Never fail silently: an app.state.asr without a callable transcribe
        # (e.g. a stub/SimpleNamespace) previously produced an empty transcript
        # with no trace, surfacing to users as "Could not understand speech".
        logger.error(
            "Voice stream: in-process ASR (type=%s) has no callable transcribe(); returning empty transcript",
            type(asr).__name__,
        )
        return ""

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(wav_bytes)
        tmp.flush()
        tmp.close()
        result = transcribe(tmp.name)
        text = await result if inspect.isawaitable(result) else result
        return (text or "").strip()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            logger.debug("Voice stream: temp transcribe file cleanup failed")


async def _synthesize_tts_bytes(app: FastAPI, text: str, room: str) -> tuple[bytes, int]:
    """Synthesize text to int16 PCM bytes when a byte-capable engine exists."""
    if not text or not text.strip():
        return b"", SAMPLE_RATE_16K

    tts = _resolve_voice_stream_tts(app)
    if tts is None:
        logger.debug("Voice stream: no TTS engine available for room=%s", room)
        return b"", SAMPLE_RATE_16K

    synthesize = getattr(tts, "synthesize", None)
    if not callable(synthesize):
        logger.debug(
            "Voice stream: TTS engine has no synthesize() for room=%s (type=%s)",
            room,
            type(tts).__name__,
        )
        return b"", SAMPLE_RATE_16K

    try:
        result = synthesize(text)
        pcm = await result if inspect.isawaitable(result) else result
    except Exception:
        logger.exception("Voice stream: TTS synthesis failed (room=%s)", room)
        return b"", SAMPLE_RATE_16K

    if not pcm:
        logger.debug("Voice stream: synthesize() returned no PCM for room=%s", room)
        return b"", SAMPLE_RATE_16K

    sample_rate = getattr(tts, "last_sample_rate", SAMPLE_RATE_16K)
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        sample_rate = SAMPLE_RATE_16K
    if isinstance(pcm, bytearray):
        return bytes(pcm), sample_rate
    if isinstance(pcm, bytes):
        return pcm, sample_rate

    logger.warning(
        "Voice stream: synthesize() returned unsupported payload type=%s (room=%s)",
        type(pcm).__name__,
        room,
    )
    return b"", SAMPLE_RATE_16K


def _is_no_speech_response(resp: object) -> bool:
    """True when the transcribe route said "there were no words in this audio".

    That is an ordinary outcome, not a fault, and it is the one case where
    telling the user to say it again is the honest answer. Everything else the
    route can refuse with is our problem, not theirs.

    Read from the envelope's error CODE rather than the HTTP status, so the
    meaning travels with the response instead of being inferred from a status
    that is shared with any other client-error the route may grow.
    """
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001, RUF100 - an unparseable body is not a no-speech answer
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return str(error.get("code") or "") == "no_speech_detected"


async def _send_command_result(
    session: _SpokeVoiceSession,
    *,
    transcript: str,
    response_text: str,
    cap_state: dict[str, object] | None = None,
) -> None:
    """Send JSON command feedback followed by optional TTS PCM."""
    frame: dict[str, object] = {
        "type": "command_result",
        "transcript": transcript,
        "response": response_text,
    }
    # A voice turn denied for spending the managed-AI plan allowance is spoken
    # aloud, and audio has nothing to tap. Carrying the cap state to the client
    # lets the browser and spoke surfaces render the upgrade route beside the
    # spoken reply (candidate C-077); without it the voice denial dead-ends.
    if cap_state:
        frame["cap_state"] = cap_state
    try:
        await session.ws.send_text(json.dumps(frame))
    except Exception:
        logger.debug("Failed to send command result to voice stream client")
        return

    from voice.synthesis.spoke_tts_broadcast import suppress_spoke_tts_broadcast

    with suppress_spoke_tts_broadcast():
        pcm, sample_rate = await _synthesize_tts_bytes(session.app, response_text, session.room)
    if not pcm:
        return

    try:
        await session.ws.send_bytes(encode_tts_frame(pcm, sample_rate))
        logger.info(
            "Voice stream: sent TTS response (room=%s, user_id=%s, bytes=%d, sample_rate=%d)",
            session.room,
            session.user_id or "none",
            len(pcm),
            sample_rate,
        )
    except Exception:
        logger.exception("Voice stream: failed to send TTS response (room=%s)", session.room)


# ---------------------------------------------------------------------------
# Per-connection state
# ---------------------------------------------------------------------------


class _SpokeVoiceSession:
    """Manages audio buffering and wake detection for one spoke connection."""

    # Process inference every N frames (~400 ms at 80 ms/chunk)
    INFER_INTERVAL = 5

    def __init__(
        self,
        room: str,
        ws: WebSocket,
        *,
        app: FastAPI,
        user_id: str,
        session_token: str | None,
        wake_engine: object | None = None,
        wake_policy: WakeDecisionPolicy | None = None,
    ):
        self.app = app
        self.room = room
        self.ws = ws
        self.user_id = user_id
        self.session_token = session_token
        self.wake_engine = wake_engine
        self.wake_policy = wake_policy or WakeDecisionPolicy()
        self.listening = False
        self.total_bytes = 0
        # Sample rate reported by client in start_listening.  Defaults to
        # 16000 Hz — the only rate we process.  Updated when client sends
        # { "type": "start_listening", "sample_rate": N }.  Used for WAV
        # creation in _handle_command so STT receives audio at the correct
        # declared rate.
        self.pcm_sample_rate: int = 16000
        self._frame_count = 0
        self._last_log = time.monotonic()

        # Ring buffer (float32, normalised)
        self._buf = np.zeros(_CLIP_SAMPLES, dtype=np.float32)
        self._write_idx = 0

        # Post-wake command capture state
        self._capturing_command = False
        self._command_chunks: list[bytes] = []
        self._command_start: float = 0.0
        self._command_timeout: float = 7.0
        self._silence_frames: int = 0
        self._silence_threshold: float = 500.0  # int16 RMS
        self._silence_limit: int = 18  # ~1.5 s at 80 ms/frame
        # PTT capture flag. While True, incoming PCM is treated as command
        # audio (same path as post-wake capture) and wake inference is
        # suppressed to avoid simultaneous wake/PTT triggers.
        self._ptt_active: bool = False
        # Guard against firing _handle_command twice when both server-side
        # silence detection and an explicit ptt_stop race to end capture.
        self._command_handled: bool = False

        # Spoke diagnostics (zero-cost when VIOLA_SPOKE_WAKE_DIAG != 1)
        # Import deferred to avoid pulling in ONNX runtime at module load
        self.diag: SpokeDiagnostics | None = None
        try:
            from voice.wake_detector.spoke_wake_diag import (
                DIAG_ENABLED,
                SpokeDiagnostics as _SpokeDiag,
            )

            if DIAG_ENABLED:
                self.diag = _SpokeDiag(room)
        except ImportError:
            pass

    # -- audio buffering -------------------------------------------------- #

    def feed_pcm(self, pcm_bytes: bytes) -> None:
        """Append raw int16 PCM into the ring buffer."""
        self.total_bytes += len(pcm_bytes)
        int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        # Normalise to [-1, 1] float32 (same as ViolaWakeListener)
        chunk = int16.astype(np.float32) * (1.0 / AUDIO_INT16_SCALE)
        n = len(chunk)
        end = self._write_idx + n
        if end <= _CLIP_SAMPLES:
            self._buf[self._write_idx : end] = chunk
        else:
            first = _CLIP_SAMPLES - self._write_idx
            self._buf[self._write_idx :] = chunk[:first]
            self._buf[: n - first] = chunk[first:]
        self._write_idx = end % _CLIP_SAMPLES
        self._frame_count += 1

    def should_infer(self) -> bool:
        return self._frame_count > 0 and self._frame_count % self.INFER_INTERVAL == 0

    def contiguous_buffer(self) -> np.ndarray:
        """Return the ring buffer in oldest→newest order."""
        if self._write_idx == 0:
            return self._buf.copy()
        return np.concatenate([self._buf[self._write_idx :], self._buf[: self._write_idx]])

    # -- command capture -------------------------------------------------- #

    def start_command_capture(self) -> None:
        self._capturing_command = True
        self._command_chunks = []
        self._command_start = time.monotonic()
        self._silence_frames = 0
        self._command_handled = False

    def start_ptt_capture(self) -> None:
        """Begin a PTT command capture turn (skips wake detection)."""
        self._ptt_active = True
        self.start_command_capture()

    def force_end_capture(self) -> bytes:
        """Force-end the current command capture (e.g. on ptt_stop)."""
        self._ptt_active = False
        self._capturing_command = False
        return self.get_command_audio()

    def feed_command_pcm(self, pcm_bytes: bytes) -> bool:
        """Feed PCM during command capture. Returns True while still capturing."""
        if not self._capturing_command:
            return False

        self._command_chunks.append(pcm_bytes)

        # Check timeout
        if time.monotonic() - self._command_start > self._command_timeout:
            self._capturing_command = False
            return False

        # Silence endpoint detection
        int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        rms = float(np.sqrt(np.mean(int16.astype(np.float32) ** 2)))
        if rms < self._silence_threshold:
            self._silence_frames += 1
            if self._silence_frames >= self._silence_limit:
                self._capturing_command = False
                return False
        else:
            self._silence_frames = 0

        return True

    def get_command_audio(self) -> bytes:
        """Return captured command audio as raw int16 PCM."""
        self._capturing_command = False
        return b"".join(self._command_chunks)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


def register_voice_stream_ws(app: FastAPI) -> None:
    """Register the /ws/voice-stream WebSocket endpoint."""

    @app.websocket("/ws/voice-stream")
    async def ws_voice_stream(ws: WebSocket) -> None:
        """Accept voice audio streams from browser spokes."""
        if not get_store().is_enabled("voice"):
            await reject_websocket(ws, code=status.WS_1013_TRY_AGAIN_LATER, reason="voice subsystem disabled")
            return

        # Pass client_host so a no-Origin connection from loopback (Qt webview,
        # local tools) is allowed, matching /ws/events (ui/websocket/routes.py)
        # and the documented check_websocket_origin contract. Without it,
        # client_host defaults to None and every no-Origin handshake -- even a
        # genuinely local one -- is rejected. Behind Caddy/Cloudflare the raw
        # socket peer is the proxy (often loopback on the same host), which
        # would misclassify remote no-Origin clients as local, so resolve the
        # real client through the trusted-proxy chain.
        from auth.ip_utils import extract_client_ip

        if not check_websocket_origin(ws, client_host=extract_client_ip(ws)):
            await reject_websocket(ws, code=1008, reason="Origin not allowed")
            return
        # Auth rules:
        # - If VIOLA_SPOKE_TOKEN is unset, preserve single-device/dev behavior.
        # - If it is set, allow either a verified hub/browser session or a
        #   verified spoke credential for documented room microphone streams.
        # - Rejected before the NORMAL accept() below, via reject_websocket
        #   (which accepts-then-closes so the client's onclose still gets the
        #   real code/reason -- see ui/core/security.py, issue #1166).
        if not await websocket_is_authorized(ws, allow_spoke_token=True):
            await reject_websocket(ws, code=4401, reason="Unauthorized")
            return

        room = ws.query_params.get("room", "unknown")
        raw_user_id, session_token = await _get_ws_user_context(ws)
        try:
            user_id = _normalize_required_user_id(raw_user_id)
        except ValueError:
            await reject_websocket(ws, code=4401, reason="User identity required")
            return

        # CONFIRM-6 warm-keeping: the client now keeps this connection open
        # across multiple turns instead of one-per-turn, so bound how many a
        # single user can hold concurrently before accepting (see the
        # _VoiceStreamConnectionLimiter docstring above).
        if not _connection_limiter.try_acquire(user_id):
            logger.warning(
                "Voice stream: user_id=%s at concurrent-connection cap (%d); refusing new connection",
                user_id,
                MAX_CONCURRENT_VOICE_STREAM_PER_USER,
            )
            await reject_websocket(ws, code=4429, reason="Too many active voice connections")
            return

        try:
            await _run_voice_stream_connection(
                ws,
                app=app,
                room=room,
                user_id=user_id,
                session_token=session_token,
            )
        finally:
            # The release must cover EVERY exit after try_acquire — including
            # an accept() or engine-load failure — or a crashed handshake
            # permanently burns one of the user's capped slots.
            _connection_limiter.release(user_id)


async def _run_voice_stream_connection(
    ws: WebSocket,
    *,
    app: FastAPI,
    room: str,
    user_id: str,
    session_token: str | None,
) -> None:
    """Accept and drive one authorized, slot-acquired voice-stream connection."""
    await ws.accept()
    logger.info(
        "Voice stream: client connected (room=%s, user_id=%s)",
        room,
        user_id,
    )

    # Load a per-session runtime engine (lazy shared model template)
    wake_engine = await _get_engine()
    if wake_engine is None:
        # Expected on the cloud/browser (PTT-only) surface: there is no wake
        # engine and PTT turns never use one. Do NOT send a client-facing
        # message of type "error" here (useVoiceWs treats any error message
        # as a fatal turn abort, _finishWithResult -> teardown, and closes
        # the socket before audio streams, killing every cloud voice turn).
        # Log it and keep accepting audio (PTT works without wake).
        # NOTE: this comment must not begin with the token `type:` -- a
        # comment line starting `# type:` is a PEP 484 type comment, and
        # vulture parses with ast type_comments=True, so it becomes a
        # SyntaxError that silently skips this ENTIRE file from the
        # dead-code scan (#313 follow-up).
        logger.info(
            "Voice stream: no wake engine on this hub (PTT-only); continuing (room=%s)",
            room,
        )

    session = _SpokeVoiceSession(
        room,
        ws,
        app=app,
        user_id=user_id,
        session_token=session_token,
        wake_engine=wake_engine,
    )

    from core.user_context import reset_current_user_id, set_current_user_id

    user_context_token = set_current_user_id(user_id)
    try:
        while True:
            # CONFIRM-6 warm-keeping: this connection may now sit idle
            # between turns (the client keeps it open with a ~20s keepalive
            # ping instead of reconnecting every turn). Bound how long an
            # idle connection can be held open server-side — see
            # VOICE_STREAM_IDLE_TIMEOUT_S above.
            msg = await _receive_or_idle_timeout(ws, VOICE_STREAM_IDLE_TIMEOUT_S)
            msg_type = msg.get("type", "")

            if msg_type == "websocket.disconnect":
                break

            if "bytes" in msg:
                pcm = msg["bytes"]

                # Command capture mode — forward audio to STT path
                if session._capturing_command:
                    still_going = session.feed_command_pcm(pcm)
                    if not still_going and not session._command_handled:
                        session._command_handled = True
                        session._ptt_active = False
                        # Command capture finished (server-side silence
                        # endpointing or the 7s command timeout, NOT a
                        # client-sent ptt_stop). A tap-mode PTT turn (client
                        # never sends ptt_stop -- see SmartDisplay.jsx's
                        # TAP_THRESHOLD_MS handling) has no other way to learn
                        # capture ended: without this message the client sat
                        # with isRecording stuck true and no dropped-turn
                        # backstop armed until command_result eventually
                        # arrived, or forever if it never did (#2769). Mirrors
                        # the explicit ptt_stop branch's ptt_status message
                        # below; useVoiceWs.js's onmessage 'capture_ended'
                        # handler stops streaming mic frames client-side and
                        # arms its own response timeout.
                        await ws.send_text(json.dumps({"type": "capture_ended"}))
                        # Command capture finished — transcribe + execute
                        asyncio.create_task(_handle_command(session, session.wake_engine))
                    continue

                # PTT active but capture already ended — drop until ptt_stop
                if session._ptt_active:
                    continue

                # Normal wake detection path
                session.feed_pcm(pcm)

                # Diagnostic: record per-frame RMS of incoming spoke audio
                if session.diag is not None:
                    int16_diag = np.frombuffer(pcm, dtype=np.int16)
                    rms = float(np.sqrt(np.mean(int16_diag.astype(np.float32) ** 2))) / AUDIO_INT16_SCALE
                    session.diag.record_frame_rms(rms)

                if session.wake_engine and session.listening and not session._ptt_active and session.should_infer():
                    buf_snapshot = session.contiguous_buffer()
                    score = await asyncio.to_thread(
                        session.wake_engine.process_audio,
                        buf_snapshot,
                    )
                    decision = _evaluate_spoke_wake_decision(
                        score,
                        buf_snapshot,
                        frame_count=session._frame_count,
                        wake_policy=session.wake_policy,
                    )
                    now = time.monotonic()
                    if now - session._last_log >= 10.0:
                        logger.debug(
                            "Voice stream: room=%s frame=%d score=%.3f threshold=%.3f bytes=%d",
                            room,
                            session._frame_count,
                            score,
                            decision.effective_threshold,
                            session.total_bytes,
                        )
                        session._last_log = now

                    triggered = decision.should_trigger

                    # Diagnostic: record inference result + save WAV if needed
                    if session.diag is not None:
                        session.diag.record_inference(score, buf_snapshot, triggered)

                    if triggered:
                        wake_ts = int(time.time() * 1000)
                        logger.info(
                            "Voice stream: WAKE DETECTED room=%s score=%.3f",
                            room,
                            score,
                        )
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "wake_detected",
                                    "timestamp": wake_ts,
                                    "score": round(score, 3),
                                    "room": room,
                                }
                            )
                        )
                        # Start capturing command audio
                        session.start_command_capture()

            elif "text" in msg:
                try:
                    data = json.loads(msg["text"])
                except (json.JSONDecodeError, TypeError):
                    continue

                kind = data.get("type")
                if kind == "start_listening":
                    session.listening = True
                    # Read and store the PCM sample rate reported by the
                    # client.  Clients should send the rate of the PCM
                    # data they are streaming (typically 16000 Hz after
                    # any browser-side resampling).  Defaults to 16000.
                    reported_rate = data.get("sample_rate", 16000)
                    if isinstance(reported_rate, int) and reported_rate > 0:
                        session.pcm_sample_rate = reported_rate
                        if reported_rate != 16000:
                            logger.warning(
                                "Voice stream: unexpected client sample rate %d Hz "
                                "(room=%s) — expected 16000 after browser resampling",
                                reported_rate,
                                room,
                            )
                    await ws.send_text(json.dumps({"type": "listening_status", "active": True}))
                    logger.info(
                        "Voice stream: listening started (room=%s, pcm_rate=%d)",
                        room,
                        session.pcm_sample_rate,
                    )
                elif kind == "stop_listening":
                    session.listening = False
                    await ws.send_text(json.dumps({"type": "listening_status", "active": False}))
                    logger.info("Voice stream: listening stopped (room=%s)", room)
                elif kind == "ptt_start":
                    session.start_ptt_capture()
                    await ws.send_text(json.dumps({"type": "ptt_status", "active": True}))
                    logger.info("Voice stream: PTT started (room=%s)", room)
                elif kind == "ptt_stop":
                    if not session._command_handled and (session._ptt_active or session._capturing_command):
                        session._command_handled = True
                        session.force_end_capture()
                        await ws.send_text(json.dumps({"type": "ptt_status", "active": False}))
                        asyncio.create_task(_handle_command(session, session.wake_engine))
                        logger.info(
                            "Voice stream: PTT stopped, dispatching command (room=%s)",
                            room,
                        )
                    else:
                        session._ptt_active = False
                        await ws.send_text(json.dumps({"type": "ptt_status", "active": False}))
                elif kind == "ping":
                    # CONFIRM-6 warm-keeping: client-side idle keepalive
                    # (~20s cadence) so this connection stays alive between
                    # turns without transiting audio. Cheap: one small text
                    # frame, no session-state changes.
                    await _handle_ping(session)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Voice stream WebSocket error (room=%s)", room)
    finally:
        # NOTE: the connection-limiter slot is released by the ENDPOINT's own
        # finally (which wraps this whole function), never here — one owner,
        # so a slot can neither leak on a pre-loop crash nor double-release.
        reset_current_user_id(user_context_token)
        if session.diag is not None:
            stats = session.diag.get_cumulative_stats()
            logger.info(
                "[SPOKE_DIAG] Session summary room=%s: inferences=%d triggers=%d near_misses=%d",
                room,
                stats["total_inferences"],
                stats["total_triggers"],
                stats["total_near_misses"],
            )
        logger.info(
            "Voice stream: client disconnected (room=%s, user_id=%s, total_bytes=%d)",
            room,
            user_id,
            session.total_bytes,
        )


async def _handle_command(session: _SpokeVoiceSession, _engine) -> None:
    """Transcribe captured command audio and execute via internal HTTP API."""
    import io
    import wave

    try:
        user_id = _normalize_required_user_id(session.user_id)
    except ValueError:
        logger.error(
            "Voice stream: refusing to dispatch command without user_id (room=%s)",
            session.room,
        )
        await _send_command_result(
            session,
            transcript="",
            response_text="Authentication required",
        )
        return

    raw_pcm = session.get_command_audio()
    if not raw_pcm or len(raw_pcm) < 3200:  # < 100 ms of audio
        logger.info(
            "Voice stream: command capture too short, ignoring (room=%s)",
            session.room,
        )
        await _send_command_result(
            session,
            transcript="",
            response_text="No speech detected",
        )
        return

    # Build WAV in memory for the transcription endpoint.
    # Use the sample rate the client reported in start_listening so the WAV
    # header matches the actual PCM encoding.  Clients resample to their
    # reported rate before streaming, so this is always consistent.
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(session.pcm_sample_rate)
        wf.writeframes(raw_pcm)
    wav_bytes = wav_buf.getvalue()

    # Use internal HTTP calls to /v1/transcribe then /v1/command
    # This reuses the existing auth-gated, fully-tested pipeline.
    import httpx

    from config.settings import settings

    base = settings.base_url
    headers: dict[str, str] = {}
    try:
        from ui.security.bootstrap import load_bootstrap_api_key

        # Worker-thread hop: the key path ensures/hardens the secret dir
        # (icacls subprocess on first use) — keep it off the event loop.
        api_key = await asyncio.to_thread(load_bootstrap_api_key)
        if api_key:
            headers["X-API-Key"] = api_key
    except Exception:
        logger.debug("Bootstrap API key unavailable for voice stream request")

    verify = not settings.ssl_enabled
    cookies: dict[str, str] | None = None
    if session.session_token:
        from auth.middleware import SESSION_COOKIE_NAME

        cookies = {SESSION_COOKIE_NAME: session.session_token}

    # 1. Transcribe. Prefer an in-process ASR engine (cloud-native path — the
    # /v1/transcribe route is not registered on the cloud surface, so the HTTP
    # loopback 404s there and the turn goes silent). Fall back to the internal
    # transcribe endpoint on surfaces that expose it (desktop).
    transcript = ""
    # Distinguishes "we heard you but the words were unclear" from "speech
    # recognition never ran at all". Both used to reach the user as "Could not
    # understand speech", which blames the speaker for our outage.
    transcription_broke = False
    asr = _resolve_voice_stream_asr(session.app)
    if asr is not None:
        try:
            transcript = await _transcribe_in_process(asr, wav_bytes)
        except Exception:
            transcription_broke = True
            logger.exception("Voice stream: in-process transcription failed (room=%s)", session.room)
    else:
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=verify, cookies=cookies) as client:
                resp = await client.post(
                    f"{base}/v1/transcribe",
                    files={TRANSCRIBE_FILE_FIELD: ("command.wav", wav_bytes, "audio/wav")},
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    transcript = data.get("data", {}).get("text", "") or data.get("data", {}).get("transcript", "")
                elif _is_no_speech_response(resp):
                    # The route's own "no speech in this audio" answer. Genuine
                    # silence, not a fault — leave transcription_broke False.
                    # Keyed on the envelope's error CODE rather than on the bare
                    # 400, because 400 is not reserved for this: any future
                    # client-error the route adds would otherwise be silently
                    # reclassified as the user having said nothing, which is the
                    # exact mislabelling the rest of this change removes.
                    logger.info(
                        "Voice stream: loopback /v1/transcribe found no speech (room=%s)",
                        session.room,
                    )
                else:
                    # Never fail silently: a non-200 loopback (401 for spoke-token
                    # sessions, 404 where the route is unregistered) previously
                    # died as an empty transcript with no trace.
                    transcription_broke = True
                    logger.error(
                        "Voice stream: loopback /v1/transcribe returned %d (room=%s); transcript will be empty",
                        resp.status_code,
                        session.room,
                    )
        except Exception:
            transcription_broke = True
            logger.exception("Voice stream: transcription request failed (room=%s)", session.room)

    if not transcript.strip():
        await _send_command_result(
            session,
            transcript="",
            response_text=(
                "Speech recognition isn't responding right now. Please try again in a moment."
                if transcription_broke
                else "I didn't catch that. Try speaking again, a little closer to the mic."
            ),
        )
        return

    logger.info(
        "Voice stream: transcript_length=%d (room=%s, user_id=%s)",
        len(transcript),
        session.room,
        user_id,
    )

    # 2. Execute command. Prefer an in-process command dispatcher (cloud-native
    # path). The /v1/command HTTP loopback cannot authenticate from the WS
    # context: the session token here is a one-shot ws-auth ticket, not a
    # session cookie, so the loopback 403s on CSRF. Fall back to the internal
    # HTTP endpoint on surfaces that expose it (desktop).
    response_text = ""
    cap_state: dict[str, object] = {}
    dispatcher = _resolve_voice_stream_command(session.app)
    if dispatcher is not None:
        try:
            result = await dispatcher.dispatch(
                transcript,
                user_id=user_id,
                device_id="browser",
                origin_channel="voice",
                companion_online=False,
            )
            if isinstance(result, dict):
                response_text = result.get("message", "") or ""
                cap_state = cap_state_from_response(result)
        except Exception:
            logger.exception("Voice stream: in-process command dispatch failed (room=%s)", session.room)
            response_text = "Command failed"
    else:
        try:
            request_body: dict[str, str] = {
                "text": transcript,
                "channel": "voice-stream",
                "user_id": user_id,
            }

            async with httpx.AsyncClient(timeout=30.0, verify=verify, cookies=cookies) as client:
                resp = await client.post(
                    f"{base}/v1/command",
                    json=request_body,
                    headers={**headers, "Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    envelope = data.get("data", {}) if isinstance(data, dict) else {}
                    response_text = envelope.get("message", "") or data.get("message", "")
                    cap_state = cap_state_from_response(data)
                else:
                    # A non-200 here used to fall through with response_text
                    # still "", and an empty reply is not nothing on the client:
                    # the spoke's display falls back to `data.text`, so the user
                    # saw their OWN words quoted back as if Viola had answered
                    # them, with silent TTS and no error anywhere.
                    logger.error(
                        "Voice stream: loopback /v1/command returned %d (room=%s)",
                        resp.status_code,
                        session.room,
                    )
                    response_text = "I heard you, but that command didn't go through. Try again?"
        except Exception:
            logger.exception("Voice stream: command execution failed (room=%s)", session.room)
            response_text = "I heard you, but that command didn't go through. Try again?"

    if not response_text.strip():
        # Same trap as above, reached whenever any branch produces an empty
        # reply: never hand the client an empty response for a turn that did
        # happen.
        logger.error(
            "Voice stream: command produced an empty reply (room=%s); sending a spoken fallback",
            session.room,
        )
        response_text = "I heard you, but I don't have an answer for that. Try again?"

    # Send result back to spoke
    await _send_command_result(
        session,
        transcript=transcript,
        response_text=response_text,
        cap_state=cap_state,
    )
