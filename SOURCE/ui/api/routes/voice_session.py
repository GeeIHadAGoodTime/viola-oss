"""WebSocket endpoint for browser-based bidirectional voice conversations.

Creates a Pipecat pipeline per WebSocket connection:
    BrowserInput -> VADProcessor (Silero) -> WhisperSTT ->
    ViolaAgentProcessor (real cloud agent) -> TTS (local Kokoro) -> BrowserOutput

The browser sends 16 kHz mono Int16 PCM as binary frames and receives
TTS audio as binary frames + JSON state updates as text frames.

Unlike a bare LLM chatbot, the user's transcript is routed through Viola's
real cloud agent path -- the SAME ``CloudIntentDispatcher`` that text
commands on the cloud surface use. That path carries the full toolset,
cloud capability gating, managed-LLM spend caps, consent gating, and
per-(user, device) conversation state. The agent's textual answer is then
spoken back through the existing local Kokoro TTS stage.

Protocol -- messages from client:
    Binary: Raw Int16 PCM audio (16 kHz, mono)
    Text:   {"type": "end"} -> graceful disconnect

Protocol -- messages to client:
    Binary: TTS Int16 PCM audio (16 kHz, mono)
    Text:   {"type": "state",   "state": "listening|thinking|speaking"}
    Text:   {"type": "transcript", "role": "user|assistant", "text": "..."}
    Text:   {"type": "error",   "message": "..."}
    Text:   {"type": "session", "session_id": "...", "usage": {...}}
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from core.logging_config import get_logger
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from ui.api.routes.websocket_auth import websocket_is_authorized
from ui.core.security import check_websocket_origin, reject_websocket

logger = get_logger(__name__)

# Marker placed on a TextFrame's ``metadata`` to flag it as a JSON control
# message (transcript update) bound for the browser rather than TTS-bound
# speech text. The transcript relay at the head of the pipeline forwards
# such frames to the WebSocket and consumes them.
_VOICE_TRANSCRIPT_META_KEY = "viola_voice_transcript"


async def _cloud_llm_consent_error(user_id: str | None = None) -> str | None:
    """Return a user-safe error string when cloud voice-session consent is missing."""
    cloud_user_checked = False
    try:
        from config.settings import settings as app_settings

        if getattr(app_settings, "app_surface", "desktop") == "cloud" and user_id:
            cloud_user_checked = True
            from services.cloud_consent import get_cloud_consent_service

            result = await get_cloud_consent_service().can_start_voice_session(user_id)
            if result.allowed:
                return None
            logger.warning("Cloud voice consent missing for user=%s: %s", user_id, result.missing)
            return result.message or "Cloud consent is required before starting browser voice sessions."
    except Exception as exc:
        if cloud_user_checked:
            logger.warning("Cloud voice consent lookup failed for user=%s: %s", user_id, exc)
            return "Cloud consent checks are temporarily unavailable. Try again in a moment."
        logger.debug("Cloud voice consent lookup failed: %s", exc)

    from core.privacy_consent import is_cloud_llm_consented

    if is_cloud_llm_consented():
        return None
    logger.warning("Cloud LLM consent not given - browser voice session disabled")
    return "Cloud AI consent is required before starting browser voice sessions."


# ---------------------------------------------------------------------------
# Session auth helper — extract user_id from WebSocket session cookie
# ---------------------------------------------------------------------------


async def _get_ws_user_id(ws: WebSocket) -> str | None:
    """Extract user_id from the WebSocket session cookie.

    Returns None if session is invalid or auth infrastructure unavailable.
    """
    from ui.api.routes.websocket_auth import verify_websocket_auth

    auth_result = await verify_websocket_auth(ws, required=False)
    if auth_result is None:
        return None
    user, _session = auth_result
    return user.id


async def _get_user_plan_family(user_id: str) -> str:
    """Look up the canonical commercial plan family for browser voice billing."""
    from auth.models import resolve_user_entitlement
    from core.product import PlanFamily

    try:
        from auth.database import get_auth_db

        db = get_auth_db()
        user = await db.users.find_by_id(user_id)
        if user:
            subscription = await db.subscriptions.get_subscription(user_id)
            entitlement = resolve_user_entitlement(user, subscription)
            if entitlement.has_paid_access:
                return entitlement.plan_family.value
    except Exception:
        pass
    return PlanFamily.FREE.value


# ---------------------------------------------------------------------------
# Viola agent processor — routes a voice transcript through the real cloud
# agent and emits the textual answer for the TTS stage to speak.
# ---------------------------------------------------------------------------


def _build_viola_agent_processor(
    ws: WebSocket,
    user_id: str,
    device_id: str,
    session_id: str,
) -> Any:
    """Construct the Pipecat processor that bridges STT transcripts to Viola's agent.

    Lazily imports pipecat so the module stays importable without it (the
    WS endpoint and its auth/billing gates must work even when pipecat is
    not installed). Returns a configured ``ViolaAgentProcessor`` instance.
    """
    from pipecat.frames.frames import (
        CancelFrame,
        EndFrame,
        Frame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        StartFrame,
        StartInterruptionFrame,
        TextFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    def _make_transcript_frame(payload: str) -> TextFrame:
        """Build a TextFrame carrying a JSON control message for the browser.

        ``TextFrame.__init__`` only accepts ``text``; the transcript marker
        and ``skip_tts`` are set on the constructed instance so the TTS
        service ignores it and the head-of-pipeline relay can detect it.
        """
        frame = TextFrame(payload)
        frame.skip_tts = True
        frame.metadata = {_VOICE_TRANSCRIPT_META_KEY: True}
        return frame

    class ViolaAgentProcessor(FrameProcessor):  # type: ignore[misc]
        """Routes finalized STT transcripts through Viola's real cloud agent.

        Sits in the pipeline where a bare LLM service would normally live:
            ... -> WhisperSTT -> ViolaAgentProcessor -> TTS -> ...

        On a final ``TranscriptionFrame`` it dispatches the transcript through
        ``CloudIntentDispatcher.dispatch()`` -- the exact same agent path that
        cloud text commands use, including the full toolset, cloud capability
        gating, managed-LLM spend caps and per-(user, device) conversation
        state. The dispatcher's textual answer is emitted downstream as a
        ``TextFrame`` bracketed by ``LLMFullResponseStartFrame`` /
        ``LLMFullResponseEndFrame`` so the existing TTS stage speaks it.

        TranscriptionFrames are NOT forwarded downstream -- the TTS service
        explicitly ignores them, and there is no LLM context aggregator in
        this pipeline (the cloud agent owns conversation state itself).
        """

        def __init__(self, dispatcher: Any) -> None:
            super().__init__()
            self._dispatcher = dispatcher
            self._user_id = user_id
            self._device_id = device_id
            self._session_id = session_id
            self._dispatch_task: asyncio.Task[None] | None = None
            self._cancelled = False

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:  # type: ignore[override]
            await super().process_frame(frame, direction)

            if isinstance(frame, StartFrame):
                self._cancelled = False
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, StartInterruptionFrame):
                # User barged in mid-answer: abandon the in-flight dispatch so
                # we do not speak a stale response over the new turn.
                self._cancelled = True
                if self._dispatch_task is not None and not self._dispatch_task.done():
                    self._dispatch_task.cancel()
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, (EndFrame, CancelFrame)):
                if self._dispatch_task is not None and not self._dispatch_task.done():
                    self._dispatch_task.cancel()
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, TranscriptionFrame):
                if getattr(frame, "finalized", True) is False:
                    # Interim transcript — wait for the finalized turn.
                    return
                text = (getattr(frame, "text", "") or "").strip()
                if not text:
                    return
                self._cancelled = False
                self._dispatch_task = self.create_task(self._dispatch_turn(text))
                return

            # Anything else (audio frames already consumed upstream, control
            # frames, etc.) passes straight through.
            await self.push_frame(frame, direction)

        async def _emit_transcript(self, role: str, text: str) -> None:
            """Push a transcript update upstream so the relay forwards it to the browser."""
            payload = {"type": "transcript", "role": role, "text": text}
            try:
                await self.push_frame(
                    _make_transcript_frame(json.dumps(payload)),
                    FrameDirection.UPSTREAM,
                )
            except Exception:
                logger.debug("Failed to emit %s transcript frame", role, exc_info=True)

        async def _dispatch_turn(self, text: str) -> None:
            """Run one voice turn through Viola's real cloud agent."""
            await self._emit_transcript("user", text)
            try:
                result = await self._dispatcher.dispatch(
                    text,
                    user_id=self._user_id,
                    device_id=self._device_id,
                    origin_channel="voice",
                    companion_online=False,
                    include_manifest=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Voice session %s: agent dispatch failed for user=%s",
                    self._session_id,
                    self._user_id,
                )
                await self._speak("Sorry, something went wrong. Please try again.")
                return

            if self._cancelled:
                return

            message = ""
            if isinstance(result, dict):
                raw = result.get("message")
                if isinstance(raw, str):
                    message = raw.strip()
            if not message:
                message = "Sorry, I could not complete that request."

            await self._emit_transcript("assistant", message)
            await self._speak(message)

        async def _speak(self, message: str) -> None:
            """Emit a textual answer for the downstream TTS stage to speak."""
            if self._cancelled:
                return
            await self.push_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
            await self.push_frame(TextFrame(message), FrameDirection.DOWNSTREAM)
            await self.push_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    return ViolaAgentProcessor(_get_cloud_intent_dispatcher(ws))


def _get_cloud_intent_dispatcher(ws: WebSocket) -> Any:
    """Return the process-wide ``CloudIntentDispatcher`` for this surface.

    The cloud agent path (full toolset, cloud capability gating, managed-LLM
    spend caps, consent gating, per-(user, device) conversation state) lives
    behind ``CloudIntentDispatcher``. Browser voice reuses the SAME dispatcher
    instance as cloud text commands (``/v1/command``) so voice turns and text
    turns share conversation state and the per-(user, device) pipeline cache.

    Resolution order:
      1. ``app.state.cloud_intent_dispatcher`` — the canonical singleton the
         cloud app constructs at startup and ``/v1/command`` dispatches to.
      2. The chat service's process-wide singleton (shared with channels).
      3. A freshly constructed dispatcher (desktop / test fallback).
    """
    app = getattr(ws, "app", None)
    state = getattr(app, "state", None)
    dispatcher = getattr(state, "cloud_intent_dispatcher", None)
    if dispatcher is not None:
        return dispatcher

    try:
        from chat.service import _get_canonical_dispatcher

        shared = _get_canonical_dispatcher()
        if shared is not None:
            return shared
    except Exception:
        logger.debug("Chat-service dispatcher unavailable for voice session", exc_info=True)

    from services.cloud_intent.dispatch import CloudIntentDispatcher

    return CloudIntentDispatcher()


# ---------------------------------------------------------------------------
# Transcript relay — forwards processor transcript frames to the browser
# ---------------------------------------------------------------------------


def _build_transcript_relay(ws: WebSocket) -> Any:
    """Build a Pipecat processor that relays transcript JSON frames to the browser.

    The ``ViolaAgentProcessor`` pushes transcript metadata upstream as a
    ``TextFrame`` tagged with ``_VOICE_TRANSCRIPT_META_KEY``; this relay sits
    at the head of the pipeline, catches those frames, forwards their JSON
    payload to the WebSocket as a text message, and consumes them. Audio and
    all other frames pass through untouched.
    """
    from pipecat.frames.frames import Frame, TextFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    class TranscriptRelay(FrameProcessor):  # type: ignore[misc]
        """Relays JSON-carrying text frames to the browser WebSocket."""

        def __init__(self) -> None:
            super().__init__()
            self._ws = ws

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:  # type: ignore[override]
            await super().process_frame(frame, direction)

            metadata = getattr(frame, "metadata", None)
            is_transcript = isinstance(metadata, dict) and metadata.get(_VOICE_TRANSCRIPT_META_KEY)
            if direction == FrameDirection.UPSTREAM and isinstance(frame, TextFrame) and is_transcript:
                text = getattr(frame, "text", "")
                if isinstance(text, str) and text:
                    try:
                        await self._ws.send_text(text)
                    except Exception:
                        logger.debug("Failed to relay transcript to browser", exc_info=True)
                # Consume — do not propagate the control frame further.
                return

            await self.push_frame(frame, direction)

    return TranscriptRelay()


# ---------------------------------------------------------------------------
# Pipeline builder — STT -> Viola agent -> TTS
# ---------------------------------------------------------------------------


async def _build_pipeline(ws: WebSocket, session_id: str, user_id: str):
    """Build a Pipecat pipeline for a browser voice session.

    The pipeline runs the user's speech through Viola's real cloud agent:

        BrowserInput -> TranscriptRelay -> VADProcessor (Silero) ->
        WhisperSTT -> ViolaAgentProcessor -> TTS (Kokoro) -> BrowserOutput

    There is NO bare LLM and NO LLM context aggregator: the transcript is
    dispatched through ``CloudIntentDispatcher`` (the same agent path used by
    cloud text commands) and the agent's textual answer is spoken back.

    Returns (runner, task, transport) or raises ImportError / PermissionError.
    """
    consent_error = await _cloud_llm_consent_error(user_id)
    if consent_error is not None:
        raise PermissionError(consent_error)

    import os

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.audio.vad_processor import VADProcessor

    from voice.browser_transport import WebSocketVoiceTransport

    # --- Transport ---
    transport = WebSocketVoiceTransport(ws)

    # --- STT (local faster-whisper) ---
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")

    from telephony.multilingual_whisper import MultilingualWhisperSTTService

    stt = MultilingualWhisperSTTService(
        settings=MultilingualWhisperSTTService.Settings(
            model="small",
            no_speech_prob=0.9,
        ),
        device="cpu",
    )

    # --- TTS (local Kokoro; fail closed if unavailable) ---
    from telephony.tts_normalizer import SpeechTextFilter

    speech_filter = SpeechTextFilter()

    try:
        from pipecat.services.kokoro.tts import KokoroTTSService

        tts = KokoroTTSService(
            voice_id="af_heart",
            stop_frame_timeout_s=0.5,
            text_filters=[speech_filter],
        )
        # Bind so the corruption guard reads the active TTS language.
        speech_filter.bind_tts(tts)
    except (ImportError, Exception) as exc:
        logger.error("Kokoro TTS not available; refusing paid cloud TTS fallback: %s", exc)
        raise RuntimeError("Browser voice session requires local Kokoro TTS") from exc

    # --- VAD: standalone processor that emits VADUser{Started,Stopped}Speaking
    # frames so the SegmentedSTTService can carve speech turns. (In pipecat
    # 0.0.107 the transport-level vad_analyzer is deprecated in favour of a
    # VADProcessor when no LLMUserAggregator is present.) ---
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())

    # --- Viola agent: routes the transcript through the real cloud agent ---
    transcript_relay = _build_transcript_relay(ws)
    viola_agent = _build_viola_agent_processor(
        ws,
        user_id=user_id,
        device_id="browser",
        session_id=session_id,
    )

    # --- Assemble pipeline ---
    pipeline = Pipeline(
        [
            transport.input(),
            transcript_relay,
            vad,
            stt,
            viola_agent,
            tts,
            transport.output(),
        ]
    )

    params = PipelineParams(
        allow_interruptions=True,
        enable_metrics=True,
    )

    runner = PipelineRunner(handle_sigint=False)
    task = PipelineTask(pipeline, params=params)

    return runner, task, transport


# ---------------------------------------------------------------------------
# WebSocket endpoint registration
# ---------------------------------------------------------------------------


def register_voice_session_ws(app: FastAPI) -> None:
    """Register the /ws/voice-session WebSocket endpoint."""

    @app.websocket("/ws/voice-session")
    async def voice_session_ws(ws: WebSocket) -> None:
        """Bidirectional voice conversation over WebSocket.

        Lifecycle:
        1. Auth check (session cookie)
        2. Billing gate check
        3. Accept WebSocket
        4. Build and run Pipecat pipeline (STT -> Viola agent -> TTS)
        5. Pipeline runs until client disconnects or error
        6. Cleanup: record billing, destroy pipeline
        """
        # --- Origin check ---
        if not check_websocket_origin(ws):
            await reject_websocket(ws, code=4403, reason="Origin not allowed")
            return

        authorized = await websocket_is_authorized(ws, allow_spoke_token=False)
        if not authorized:
            await reject_websocket(ws, code=4401, reason="Unauthorized")
            return

        # --- Identify user ---
        user_id = await _get_ws_user_id(ws)
        if not user_id:
            await reject_websocket(ws, code=4401, reason="Authenticated user required")
            return

        # --- Auth ---
        await ws.accept()

        user_plan_family = await _get_user_plan_family(user_id)

        # --- Billing gate ---
        from voice.browser_billing import get_browser_voice_billing

        consent_error = await _cloud_llm_consent_error(user_id)
        if consent_error is not None:
            await ws.send_text(json.dumps({"type": "error", "message": consent_error}))
            await ws.close(code=4403, reason="Cloud AI consent required")
            return

        billing = get_browser_voice_billing()
        gate = await billing.check_can_start_session(user_id, plan_family=user_plan_family)
        if not gate.allowed:
            await ws.send_text(json.dumps({"type": "error", "message": gate.reason}))
            await ws.close(code=4429, reason="Rate limited")
            return

        # --- Session setup ---
        session_id = uuid.uuid4().hex[:12]
        started_at = datetime.now(tz=UTC)

        await billing.record_session_start(user_id, session_id)

        await ws.send_text(
            json.dumps(
                {
                    "type": "session",
                    "session_id": session_id,
                    "usage": gate.usage,
                }
            )
        )

        logger.info(
            "Voice session %s started for user=%s plan_family=%s",
            session_id,
            user_id,
            user_plan_family,
        )

        # --- Build and run pipeline ---
        try:
            runner, task, _transport = await _build_pipeline(ws, session_id, user_id)

            # Run the pipeline (blocks until client disconnects or pipeline ends)
            await runner.run(task)

        except WebSocketDisconnect:
            logger.info("Voice session %s: client disconnected", session_id)

        except PermissionError as exc:
            logger.warning("Voice session %s blocked: %s", session_id, exc)
            try:
                await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            except Exception:
                pass

        except ImportError as exc:
            error_msg = "Voice session requires pipecat-ai: %s" % exc
            logger.error(error_msg)
            try:
                await ws.send_text(json.dumps({"type": "error", "message": error_msg}))
            except Exception:
                pass

        except Exception:
            logger.exception("Voice session %s error", session_id)
            try:
                await ws.send_text(json.dumps({"type": "error", "message": "Voice session error"}))
            except Exception:
                pass

        finally:
            # --- Cleanup: record billing ---
            ended_at = datetime.now(tz=UTC)
            duration = (ended_at - started_at).total_seconds()

            try:
                await billing.record_session_end(user_id, session_id, duration)
            except Exception:
                logger.warning("Failed to record voice session end for billing")

            logger.info(
                "Voice session %s ended: duration=%.1fs user=%s",
                session_id,
                duration,
                user_id,
            )

            # Close WebSocket if still open
            try:
                await ws.close()
            except Exception:
                pass
