"""
Voice Command Handler for processing individual voice commands.

Extracted from voice_orchestrator.py to comply with code constraints.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.constants import SAMPLE_RATE_16K, TIMEOUT_GRACE, TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from core.platform import get_temp_dir
from core.voice_canonical_carrier import VoiceTurnMetadata, use_voice_turn_context

if TYPE_CHECKING:
    # numpy is imported locally inside _get_interrupted_audio_buffer at runtime;
    # the class-level annotation needs the name available to mypy without
    # forcing a top-level import (keeps numpy an optional runtime dep).
    import numpy as np

logger = get_logger(__name__)

try:  # pragma: no cover - optional in non-Qt contexts
    from ui.qt_native.debug_events import emit_debug_event
except Exception:
    emit_debug_event = None


# Voice command timeout in seconds
VOICE_COMMAND_TIMEOUT = TIMEOUT_VERY_LONG

# Guardrail: Maximum time allowed between wake detection and STT start (in milliseconds)
STT_START_TIMEOUT_MS = int(TIMEOUT_GRACE * 1000)  # 3000ms = 3 seconds


def _sanitize_voice_temp_user_id(user_id: str) -> str:
    """Return a filesystem-safe user_id segment for voice temp files."""
    sanitized = re.sub(r"[^0-9A-Za-z]", "_", user_id)
    return sanitized or "default"


def _get_voice_temp_dir() -> Path:
    """Return the per-user temp directory for transient voice recordings."""
    app_surface = "desktop"
    try:
        from config.settings import settings

        app_surface = getattr(settings, "app_surface", "desktop")
    except Exception:
        app_surface = "desktop"

    try:
        from core.user_context import get_current_user_id

        user_id = get_current_user_id()
    except LookupError:
        if app_surface == "cloud":
            raise PermissionError("Authenticated user_id required for cloud voice temp files")
        try:
            from core.user_context import get_device_user_id

            user_id = get_device_user_id()
        except Exception:
            user_id = "default"  # mt-ok: fallback for temp dir naming when user_context unavailable

    temp_dir = get_temp_dir() / "viola_commands" / _sanitize_voice_temp_user_id(user_id)
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


_DISMISS_RE = re.compile(
    r"^\s*"
    # Optional polite/filler prefix (Whisper often adds these)
    r"(?:(?:okay|ok|hey|hey viola|viola|please|uh|um|so|well|alright)\s*[,.]?\s*)*"
    # Core dismiss verbs/phrases
    r"(?:"
    r"stop(?:\s+(?:it|that|talking|speaking|please))?"
    r"|cancel"
    r"|shut\s+up"
    r"|be\s+quiet"
    r"|quiet"
    r"|enough"
    r"|that'?s\s+enough"
    r"|never\s*mind"
    r"|forget\s+(?:it|about\s+it)"
    r"|no(?:pe)?"
    r"|not\s+now"
    r"|go\s+away"
    r"|skip"
    r"|dismiss"
    r"|I\s+(?:didn'?t|did\s+not)\s+(?:say|ask|call|need)\s+(?:that|you|anything|viola)"
    r"|(?:I\s+)?(?:wasn'?t|was\s+not)\s+talking\s+to\s+you"
    r")"
    # Optional trailing filler
    r"[.!,\s]*$",
    re.IGNORECASE,
)


def _is_dismiss_phrase(transcript: str) -> bool:
    """Check if a transcript is a dismiss/stop phrase.

    Used after TTS barge-in to detect when the user wants Viola to
    stop rather than process their interruption as a new command.
    """
    return bool(_DISMISS_RE.match(transcript.strip()))


def _update_state_hub_voice_mode(mode_name: str, *, user_id: str) -> None:
    """
    Update the central StateHub with voice mode change.

    This ensures the centralized state reflects the current voice processing stage.
    The StateHub is the single source of truth for state across the application.

    Args:
        mode_name: One of "idle", "listening", "processing", "speaking"
    """
    try:
        from core.state_hub import UpdateVoiceMode, get_state_hub
        from core.unified_state import VoiceMode

        hub = get_state_hub(user_id=user_id)
        if hub is None:
            return

        mode_map = {
            "idle": VoiceMode.IDLE,
            "listening": VoiceMode.LISTENING,
            "processing": VoiceMode.PROCESSING,
            "speaking": VoiceMode.SPEAKING,
            "conversing": VoiceMode.CONVERSING,
        }
        mode = mode_map.get(mode_name, VoiceMode.IDLE)
        hub.dispatch(UpdateVoiceMode(mode=mode, user_id=user_id))
        logger.debug("StateHub voice mode updated: %s", mode_name)
    except Exception as e:
        logger.debug("StateHub voice mode update failed (may not be initialized): %s", e)


def _update_state_hub_last_transcript(transcript: str, *, user_id: str, confidence: float = 0.0) -> None:
    """Update centralized voice transcript state without touching legacy globals."""
    try:
        from core.state_hub import SetLastTranscript, get_state_hub

        hub = get_state_hub(user_id=user_id)
        if hub is None:
            return
        hub.dispatch(SetLastTranscript(transcript=transcript, confidence=confidence, user_id=user_id))
        logger.debug("StateHub voice transcript updated")
    except (AttributeError, ImportError, LookupError, RuntimeError, ValueError) as e:
        logger.debug("StateHub transcript update failed (may not be initialized): %s", e)


def _broadcast_chat_response(result: dict[str, Any]) -> None:
    """Broadcast command response text to WebSocket clients for UI display."""
    message = result.get("message", "")
    if not message:
        return
    try:
        import time

        from ui.websocket.event_hub import get_event_hub

        hub = get_event_hub()
        if hub is None:
            return
        import asyncio

        # Resolve user_id from ContextVar for broadcast scoping
        try:
            from core.user_context import get_current_user_id

            _uid = get_current_user_id()
        except (ImportError, LookupError):
            _uid = None

        payload = {
            "text": message,
            "intent": result.get("intent", ""),
            "timestamp": time.time(),
        }
        # Pass through content card if the LLM/pipeline included one
        card = result.get("card") or (result.get("data", {}) or {}).get("card")
        if card and isinstance(card, dict):
            payload["card"] = card
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(hub.broadcast("chat_response", payload, user_id=_uid, force=True))
        else:
            asyncio.run(hub.broadcast("chat_response", payload, user_id=_uid, force=True))
    except Exception as e:
        logger.debug("Chat response broadcast failed: %s", e)


def _voice_account_required(*, user_id: str | None = None) -> bool:
    """Return True when the current voice command must pause for sign-in."""
    try:
        from config.defaults import DEFAULT_AI_SOURCE
        from core.account_gate import requires_account_for_command
        from core.user_context import get_current_user_id
        from ui.settings_manager import get_settings_manager

        resolved_user_id = user_id
        if resolved_user_id is None:
            try:
                resolved_user_id = get_current_user_id()
            except LookupError:
                resolved_user_id = None

        ai_source = get_settings_manager().get("ai_source", DEFAULT_AI_SOURCE, user_id=resolved_user_id)
        return requires_account_for_command(resolved_user_id, ai_source if isinstance(ai_source, str) else None)
    except (
        AttributeError,
        ImportError,
        LookupError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        logger.debug("Voice account preflight skipped after error: %s", exc)
        return False


# Import validation utilities
try:
    from core.validation import sanitize_error_message, validate_command_text
except ImportError:
    # Fallback if validation module not available
    def validate_command_text(text: str) -> tuple[bool, str]:
        return True, ""

    def sanitize_error_message(error: Exception) -> str:
        return str(error)


def _notify_data_collector(outcome: str, clip_id: int | None) -> None:
    """Fire-and-forget notification to data collection classifier."""
    if clip_id is None:
        return
    try:
        from voice.wake_detector.data_collection.classifier import notify_data_collector

        notify_data_collector(outcome, clip_id)
    except Exception:
        logger.debug(
            "Data collection notification skipped (optional subsystem)"
        )  # Data collection is optional; never disrupt voice pipeline


@dataclass
class CommandContext:
    """Context for a single voice command processing cycle."""

    user_id: str = ""
    audio_file: str | None = None
    audio_buffer: Any = None  # np.ndarray (int16) or None — buffer-based recording
    audio_sample_rate: int = 0  # Sample rate for audio_buffer (e.g. 16000)
    provider_name: str = "unknown"
    stt_event_emitted: bool = False
    ducking_cleaned_up: bool = False
    wake_detector: Any = None
    data_clip_id: int | None = None  # Wake data collection clip ID

    def has_audio(self) -> bool:
        """Return True if any audio source (buffer or file) is available."""
        return self.audio_buffer is not None or bool(self.audio_file)

    def clear_audio_buffer(self) -> None:
        """Zero out and discard the audio buffer for privacy."""
        if self.audio_buffer is not None:
            try:
                self.audio_buffer[:] = 0  # Overwrite memory
            except Exception:
                pass  # ndarray may be read-only or already freed
            self.audio_buffer = None
            self.audio_sample_rate = 0


class VoiceCommandHandler:
    """Handles processing of individual voice commands."""

    def __init__(
        self,
        orchestrator_state: Any,
        intent: Any,
        music: Any | None,
        tts: Any | None,
        voice_pipeline: Any,
        metrics: Any,
        async_bridge: Any,
        stop_event: Any,
        capabilities: dict,
    ) -> None:
        self.state = orchestrator_state
        self.intent = intent
        self.music = music
        self.tts = tts
        self.voice_pipeline = voice_pipeline
        self._metrics = metrics
        self._async_bridge = async_bridge
        self._stop_event = stop_event
        self.capabilities = capabilities
        self._last_stt_activity = time.time()
        self._wake_timestamp: float | None = None  # Track when wake was detected
        self._wake_perf_counter: float | None = None  # High-precision wake timing
        self._continuous_capture: Any | None = None  # ContinuousMicCapture instance

    def record_wake_timestamp(self) -> None:
        """Record timestamp when wake word was detected for guardrail checks."""
        self._wake_timestamp = time.time()
        self._wake_perf_counter = time.perf_counter()
        logger.debug("Wake timestamp recorded: %.3f", self._wake_timestamp)

    def _check_stt_start_guardrail(self) -> bool:
        """
        Guardrail: Check if STT started within acceptable time after wake.

        Returns True if within bounds, False if guardrail violation.
        """
        if self._wake_timestamp is None:
            return True

        elapsed_ms = (time.time() - self._wake_timestamp) * 1000
        if elapsed_ms > STT_START_TIMEOUT_MS:
            logger.error(
                "STT start timeout! Wake fired %.0fms ago but STT not started. "
                "Expected: <%dms. This indicates a pipeline bottleneck.",
                elapsed_ms,
                STT_START_TIMEOUT_MS,
            )
            self._emit_guardrail_violation(elapsed_ms)
            return False

        logger.debug("STT start timing OK: %.0fms after wake", elapsed_ms)
        return True

    def _emit_guardrail_violation(self, elapsed_ms: float) -> None:
        """Emit debug event for guardrail violation."""
        if emit_debug_event is not None:
            emit_debug_event(
                "stt_guardrail_violation",
                {
                    "elapsed_ms": elapsed_ms,
                    "threshold_ms": STT_START_TIMEOUT_MS,
                    "reason": "stt_start_timeout",
                },
                source="voice_command_handler",
            )

    # -------------------------------------------------------------------------
    # Main Entry Point
    # -------------------------------------------------------------------------

    async def handle_once(self) -> None:
        """Handle a single voice command."""
        logger.info("handle_once() started - entering voice command handler")

        ctx = CommandContext()
        ctx.wake_detector = getattr(self.voice_pipeline, "wake_detector", None)

        # Capture data collection clip ID from wake detector
        if ctx.wake_detector:
            ctx.data_clip_id = getattr(ctx.wake_detector, "_last_data_clip_id", None)

        # Pause wake detection while listening
        self._pause_wake_detector(ctx)

        # Guardrail check
        if not self._check_stt_start_guardrail():
            logger.warning("Aborting due to STT start timeout guardrail")

        try:
            ctx.user_id = self._resolve_voice_user_id()
            from core.user_context import user_scope

            with user_scope(ctx.user_id):
                await self._process_voice_command(ctx)
        except TimeoutError:
            logger.error("Voice command processing timed out")
        except Exception as e:
            error_msg = sanitize_error_message(e)
            logger.exception("Voice handling error: %s", error_msg)
        finally:
            if ctx.user_id:
                from core.user_context import user_scope

                with user_scope(ctx.user_id):
                    self._cleanup_after_command(ctx)
            else:
                self._cleanup_after_command(ctx)

    # -------------------------------------------------------------------------
    # Command Processing Phases
    # -------------------------------------------------------------------------

    async def _process_voice_command(self, ctx: CommandContext) -> None:
        """Process a voice command, optionally looping for conversational turns."""
        from config.settings import settings

        if not ctx.user_id:
            ctx.user_id = self._resolve_voice_user_id()
        conversation_enabled = settings.conversational_mode_enabled
        max_turns = settings.conversation_max_turns
        follow_up_timeout = settings.conversation_follow_up_timeout

        turn = 0
        exit_reason = "complete"

        while True:
            turn += 1

            # --- Setup listening state ---
            if turn == 1:
                self._setup_listening_state(ctx)
            else:
                _update_state_hub_voice_mode("listening", user_id=ctx.user_id)

            # --- Record audio (buffer-based, no disk I/O) ---
            if turn == 1:
                ctx.audio_file = await self._record_audio_with_timeout(ctx)
            else:
                ctx.audio_file = await self._record_follow_up_audio(follow_up_timeout, ctx)

            if not ctx.has_audio():
                exit_reason = "no_audio" if turn == 1 else "follow_up_timeout"
                break

            # --- Transcribe ---
            transcript = await self._transcribe_audio_with_timeout(ctx)
            if not transcript:
                exit_reason = "empty_transcript"
                break

            # Heuristic FP detection: wake fired but STT returned only filler
            if transcript.strip() in ("", ".", "...", "hmm", "um", "uh"):
                try:
                    from admin.instrumentation import record_wake_false_positive

                    record_wake_false_positive()
                except Exception:
                    logger.debug("Wake false positive instrumentation skipped")

            # Check Whisper's no_speech_prob
            no_speech_prob = self._get_no_speech_prob()
            if no_speech_prob > 0.6:
                logger.info(
                    "Whisper no_speech_prob %.2f > 0.6 — treating as non-speech, silent abort",
                    no_speech_prob,
                )
                if turn == 1:
                    detector = getattr(self.voice_pipeline, "wake_detector", None)
                    if detector:
                        detector.mark_detection_as_false_positive()
                    _notify_data_collector("no_speech", ctx.data_clip_id)
                    self._unduck_audio()
                    ctx.ducking_cleaned_up = True
                exit_reason = "no_speech"
                break

            # --- Validate transcript ---
            if not self._validate_transcript(transcript):
                exit_reason = "invalid_transcript"
                break

            # --- Interpret and dispatch ---
            turn_source = "follow_up" if turn > 1 else "wake"
            with use_voice_turn_context(
                self._voice_turn_metadata(ctx, source=turn_source),
                transcript=transcript,
            ):
                result = await self._interpret_and_dispatch(
                    transcript,
                    is_follow_up=(turn > 1),
                    user_id=ctx.user_id,
                )
            if result is None:
                if turn == 1:
                    _notify_data_collector("nlu_fail", ctx.data_clip_id)
                exit_reason = "interpret_failed"
                break

            # Classify for data collection (first turn only)
            if turn == 1:
                intent = result.get("intent", "")
                if intent == "ignore":
                    logger.info("GPT classified as ignore — marking as false positive for retraining")
                    detector = getattr(self.voice_pipeline, "wake_detector", None)
                    if detector:
                        detector.mark_detection_as_false_positive()
                    _notify_data_collector("ignored", ctx.data_clip_id)
                    self._unduck_audio()
                    ctx.ducking_cleaned_up = True
                elif intent in ("cancel", "stop", "nevermind"):
                    _notify_data_collector("cancel", ctx.data_clip_id)
                elif result.get("success"):
                    _notify_data_collector("nlu_success", ctx.data_clip_id)
                else:
                    _notify_data_collector("nlu_fail", ctx.data_clip_id)

            # Check for ignore intent — conversation over
            if result.get("intent") == "ignore":
                exit_reason = "ignore"
                break

            # --- Speak response ---
            await self._speak_response(result, user_id=ctx.user_id)

            # --- Broadcast response text to WS clients (GAP-1 fix) ---
            _broadcast_chat_response(result)

            # --- Handle TTS interruption ---
            # If user interrupted TTS, save captured audio as next turn's input
            if result.get("_interrupted"):
                # User speech is in the continuous capture buffer — get as buffer
                interrupted_buf = self._get_interrupted_audio_buffer()
                if interrupted_buf is not None and conversation_enabled:
                    # Skip follow-up recording: we already have the audio
                    self._cleanup_audio_file(ctx)
                    ctx.clear_audio_buffer()
                    ctx.audio_buffer = interrupted_buf
                    ctx.audio_sample_rate = SAMPLE_RATE_16K
                    ctx.audio_file = "buffer"
                    await self._record_voice_event(
                        "barge_in_resume",
                        {
                            "source": "barge_in",
                            "barge_in": True,
                            "resume_target": "captured_audio",
                            "sample_rate": SAMPLE_RATE_16K,
                        },
                        user_id=ctx.user_id,
                    )
                    logger.info("TTS interrupted — using captured speech for next turn")
                    # Continue loop: skip recording, go straight to transcription
                    turn += 1
                    if turn > max_turns:
                        exit_reason = "max_turns"
                        break
                    _update_state_hub_voice_mode("conversing", user_id=ctx.user_id)
                    # Transcribe the interrupted audio directly
                    transcript = await self._transcribe_audio_with_timeout(ctx)
                    if not transcript:
                        exit_reason = "empty_transcript"
                        break

                    # Dismiss detection: if the user interrupted with a
                    # stop/cancel/dismiss phrase, silently exit instead of
                    # processing it as a new command.  This makes
                    # interrupting feel natural — like telling a real
                    # assistant "stop" or "never mind".
                    if _is_dismiss_phrase(transcript):
                        logger.info("Interrupt dismiss detected - silently exiting")
                        exit_reason = "dismissed"
                        break

                    if not self._validate_transcript(transcript):
                        exit_reason = "invalid_transcript"
                        break
                    with use_voice_turn_context(
                        self._voice_turn_metadata(ctx, source="barge_in", barge_in=True),
                        transcript=transcript,
                    ):
                        result = await self._interpret_and_dispatch(
                            transcript,
                            is_follow_up=True,
                            user_id=ctx.user_id,
                        )
                    if result is None:
                        exit_reason = "interpret_failed"
                        break
                    if result.get("intent") == "ignore":
                        exit_reason = "ignore"
                        break
                    await self._speak_response(result, user_id=ctx.user_id)
                    _broadcast_chat_response(result)
                    # Fall through to continuation logic below
                else:
                    # Interrupt fired but no captured audio — just exit
                    # cleanly (TTS was already stopped).
                    logger.info("TTS interrupted but no audio captured — exiting")
                    exit_reason = "interrupted_no_audio"
                    break

            # --- Decide whether to continue ---
            if not conversation_enabled:
                break

            if not result.get("continue_listening", False):
                exit_reason = "llm_done"
                break

            if turn >= max_turns:
                logger.info("Conversation reached max turns (%d), ending", max_turns)
                exit_reason = "max_turns"
                break

            # Transition to CONVERSING — clean up previous audio file
            self._cleanup_audio_file(ctx)
            _update_state_hub_voice_mode("conversing", user_id=ctx.user_id)

            # GAP-6: Subtle audio cue + WS event to signal Viola is listening
            await self._play_follow_up_cue()
            self._broadcast_follow_up_listening()
            await self._record_voice_event(
                "follow_up_listening_resumed",
                {
                    "source": "follow_up",
                    "turn": turn,
                    "reason": "continue_listening",
                },
                user_id=ctx.user_id,
            )

            logger.info("Conversation turn %d complete, waiting for follow-up...", turn)

        if turn > 1 or conversation_enabled:
            logger.info("Conversation ended: %s (after %d turn(s))", exit_reason, turn)

    async def _record_follow_up_audio(
        self,
        follow_up_timeout: float,
        ctx: CommandContext,
    ) -> str | None:
        """Record follow-up audio with onset timeout for conversational mode.

        Uses buffer-based recording (no disk I/O) when available, falling
        back to file-based recording. Stores buffer in ctx.audio_buffer.

        Returns sentinel "buffer" when buffer stored in ctx, a file path,
        or None if no speech detected within the onset window.
        """
        logger.debug("Waiting for follow-up speech (onset_timeout=%.1fs)", follow_up_timeout)
        try:
            audio_buf, sample_rate = await asyncio.wait_for(
                asyncio.to_thread(
                    self.voice_pipeline.listen_and_record_buffer,
                    500,  # silence_threshold (RMS pre-filter)
                    1.0,  # silence_duration (VAD endpoint, matches SmartTurn)
                    7.0,  # timeout = max recording after speech starts
                    follow_up_timeout,  # onset_timeout = wait for speech to begin
                ),
                timeout=follow_up_timeout + 10.0,  # safety net
            )
            if audio_buf is not None and len(audio_buf) > 0:
                ctx.audio_buffer = audio_buf
                ctx.audio_sample_rate = sample_rate
                return "buffer"
            logger.info("No follow-up speech detected, ending conversation")
            return None
        except TimeoutError:
            logger.info(
                "Follow-up timeout (%.1fs), ending conversation",
                follow_up_timeout,
            )
            return None

    def _setup_listening_state(self, ctx: CommandContext) -> None:
        """Setup listening state and emit events."""
        logger.debug("Setting is_listening=True")
        self.state.set_listening(True, user_id=ctx.user_id)
        _update_state_hub_voice_mode("listening", user_id=ctx.user_id)
        self._metrics.heartbeat("voice.stt", status="active", state="listening")

        logger.debug("Emitting voice_listening_started event")
        if emit_debug_event is not None:
            emit_debug_event(
                "voice_listening_started",
                {"state": "listening", "source": "wake_word"},
                source="voice_orchestrator",
            )
            logger.debug("voice_listening_started event emitted")
        else:
            logger.warning("emit_debug_event is None - cannot emit events")
        logger.info("Listening for command (audio ducked)...")

    async def _record_audio_with_timeout(self, ctx: CommandContext) -> str | None:
        """Record audio with timeout.

        Uses buffer-based recording (no disk I/O) for lower latency.
        Falls back to file-based recording if buffer mode is unavailable.
        Returns a sentinel string "buffer" when buffer is stored in ctx,
        or a real file path, or None if no audio was recorded.
        """
        logger.debug("Recording audio (buffer mode preferred)")

        try:
            audio_buf, sample_rate = await asyncio.wait_for(
                asyncio.to_thread(self.voice_pipeline.listen_and_record_buffer),
                timeout=VOICE_COMMAND_TIMEOUT,
            )

            if audio_buf is not None and len(audio_buf) > 0:
                ctx.audio_buffer = audio_buf
                ctx.audio_sample_rate = sample_rate
                logger.debug(
                    "Buffer recording complete: %d samples at %dHz",
                    len(audio_buf),
                    sample_rate,
                )
                # Return sentinel — callers check ctx.has_audio() or truthiness
                return "buffer"

            self._handle_no_audio_recorded(ctx)
            return None

        except TimeoutError:
            logger.error(
                "Voice command recording timed out after %ds",
                VOICE_COMMAND_TIMEOUT,
            )
            self._handle_recording_timeout(ctx)
            return None

    def _handle_no_audio_recorded(self, ctx: CommandContext) -> None:
        """Handle case where no audio was recorded."""
        logger.warning("No command recorded after wake - listen_and_record returned None")
        logger.debug(
            "This often happens when: 1) Silence detected immediately, 2) Audio device issue, 3) Wake detector not initialized"
        )
        detector = getattr(self.voice_pipeline, "wake_detector", None)
        if detector:
            detector.mark_detection_as_false_positive()
        _notify_data_collector("silence", ctx.data_clip_id)
        self._unduck_audio()
        ctx.ducking_cleaned_up = True
        return None

    def _handle_recording_timeout(self, ctx: CommandContext) -> None:
        """Handle recording timeout."""
        detector = getattr(self.voice_pipeline, "wake_detector", None)
        if detector:
            detector.mark_detection_as_false_positive()
        _notify_data_collector("silence", ctx.data_clip_id)
        self._unduck_audio()
        ctx.ducking_cleaned_up = True

    async def _transcribe_audio_with_timeout(self, ctx: CommandContext) -> str | None:
        """Transcribe audio with timeout. Returns transcript or None.

        Prefers buffer-based transcription (no file I/O) when ctx.audio_buffer
        is available. Falls back to file-based transcription via ctx.audio_file.
        """
        # Determine audio source: prefer buffer (no disk I/O)
        use_buffer = ctx.audio_buffer is not None
        if use_buffer:
            logger.debug(
                "Transcribing from buffer (%d samples)",
                len(ctx.audio_buffer),
            )
        else:
            logger.debug("Transcribing audio file: %s", ctx.audio_file)

        stt_start = time.perf_counter()
        ctx.provider_name = type(self.voice_pipeline.transcriber).__name__

        # Record wake-to-STT latency (high-precision)
        if self._wake_perf_counter is not None:
            wake_to_stt_ms = (stt_start - self._wake_perf_counter) * 1000
            try:
                from admin.instrumentation import record_latency

                record_latency("latency_wake_to_stt", wake_to_stt_ms)
            except Exception:
                logger.debug("Wake-to-STT latency instrumentation skipped")

        self._emit_stt_started(ctx)

        # Choose audio source for transcription
        audio_source = ctx.audio_buffer if use_buffer else ctx.audio_file

        try:
            transcript = await asyncio.wait_for(
                asyncio.to_thread(
                    self.voice_pipeline.transcribe_audio,
                    audio_source,
                    False,  # preprocess
                    False,  # duck_audio
                ),
                timeout=VOICE_COMMAND_TIMEOUT,
            )
            return self._handle_transcription_result(ctx, transcript, stt_start)

        except TimeoutError:
            self._handle_transcription_timeout(ctx)
            return None
        finally:
            # Privacy: zero out buffer memory immediately after transcription
            ctx.clear_audio_buffer()
            # Also delete any temporary WAV file if file-based path was used
            if not use_buffer:
                self._cleanup_audio_file_path(ctx.audio_file)
                ctx.audio_file = None

    def _cleanup_audio_file_path(self, audio_file: Path | str | None) -> None:
        """Delete temporary audio file after transcription to protect user privacy."""
        if audio_file is None:
            return
        try:
            audio_path = Path(audio_file)
            if audio_path.exists() and audio_path.suffix == ".wav":
                audio_path.unlink()
                logger.debug("Deleted temporary audio file: %s", audio_path)
        except Exception as e:
            logger.warning("Failed to delete temporary audio file %s: %s", audio_file, e)

    def _emit_stt_started(self, ctx: CommandContext) -> None:
        """Emit STT started event."""
        if emit_debug_event is not None:
            emit_debug_event(
                "stt_started",
                {"provider": ctx.provider_name},
                source="voice_orchestrator",
            )

    def _handle_transcription_result(
        self,
        ctx: CommandContext,
        transcript: str | None,
        stt_start: float,
    ) -> str | None:
        """Handle transcription result."""
        stt_duration = time.perf_counter() - stt_start
        self._metrics.record_stt_round_trip(stt_duration, provider=ctx.provider_name)
        self._last_stt_activity = time.time()
        self._metrics.record_stt_idle(0.0)

        self._emit_stt_finished(ctx, stt_duration, transcript)

        if not transcript:
            self._handle_empty_transcript(ctx)
            return None

        logger.debug("Transcript received (%d chars)", len(transcript))
        self.state.set_last_transcript(transcript, user_id=ctx.user_id)
        _update_state_hub_last_transcript(transcript, user_id=ctx.user_id)
        return transcript

    def _emit_stt_finished(
        self,
        ctx: CommandContext,
        duration: float,
        transcript: str | None,
    ) -> None:
        """Emit STT finished event."""
        if emit_debug_event is not None:
            emit_debug_event(
                "stt_finished",
                {
                    "provider": ctx.provider_name,
                    "duration_s": duration,
                    "transcript": transcript,
                },
                source="voice_orchestrator",
            )
            ctx.stt_event_emitted = True

    def _handle_empty_transcript(self, ctx: CommandContext) -> None:
        """Handle empty transcript from STT."""
        logger.warning("STT returned empty transcript")
        detector = getattr(self.voice_pipeline, "wake_detector", None)
        if detector:
            detector.record_missed_command()
        _notify_data_collector("silence", ctx.data_clip_id)
        # Heuristic FP: wake fired but STT returned nothing
        try:
            from admin.instrumentation import record_wake_false_positive

            record_wake_false_positive()
        except Exception:
            logger.debug("Wake false positive instrumentation skipped (empty transcript)")
        self._unduck_audio()
        ctx.ducking_cleaned_up = True
        self._metrics.heartbeat(
            "voice.stt",
            status="error",
            reason="empty_transcript",
            provider=ctx.provider_name,
        )
        if emit_debug_event is not None:
            emit_debug_event(
                "stt_finished",
                {"provider": ctx.provider_name, "status": "empty_transcript"},
                source="voice_orchestrator",
            )
            ctx.stt_event_emitted = True
        return None

    def _handle_transcription_timeout(self, ctx: CommandContext) -> None:
        """Handle STT transcription timeout."""
        logger.error("STT transcription timed out after %ds", VOICE_COMMAND_TIMEOUT)
        detector = getattr(self.voice_pipeline, "wake_detector", None)
        if detector:
            detector.record_missed_command()
        _notify_data_collector("timeout", ctx.data_clip_id)
        self._unduck_audio()
        ctx.ducking_cleaned_up = True
        self._metrics.heartbeat(
            "voice.stt",
            status="error",
            reason="timeout",
            provider=ctx.provider_name,
        )
        if emit_debug_event is not None:
            emit_debug_event(
                "stt_finished",
                {
                    "provider": ctx.provider_name,
                    "status": "timeout",
                    "duration_s": VOICE_COMMAND_TIMEOUT,
                },
                source="voice_orchestrator",
            )
            ctx.stt_event_emitted = True

    def _validate_transcript(self, transcript: str) -> bool:
        """Validate transcript before processing."""
        is_valid, error_msg = validate_command_text(transcript)
        if not is_valid:
            logger.warning("Invalid transcript ignored: %s", error_msg)
            return False
        return True

    @staticmethod
    def _unwrap_envelope(envelope: dict) -> dict:
        """Unwrap a ResponseEnvelope to its data dict.

        IntentBridge.interpret() and .dispatch() return ResponseEnvelope
        (``{"ok": ..., "error": ..., "data": {...}}``).  Callers need the
        inner ``data`` dict which contains the actual intent/message/spoken
        fields.
        """
        if isinstance(envelope, dict) and "ok" in envelope and "data" in envelope:
            data = envelope.get("data")
            if isinstance(data, dict):
                return data
        return envelope

    @staticmethod
    def _is_agent_tier_interpretation(interpretation: dict) -> bool:
        """Check if interpretation indicates a long-running agent/AI task.

        Agent-tier tasks are identified by:
        - type == "answer" with spoken == True (AI already answered via pipeline)
        - type == "delegated" with spoken == True (AI executed and spoke result)
        - source field indicating AI routing (knowledge, ai, agent, llm)

        These tasks have already been executed during interpret() by the
        IntentPipeline's AI controller.  dispatch() for these is fast
        (just returns the already-computed result), so the timeout concern
        is actually on the interpret() side.
        """
        intent_type = str(interpretation.get("type", ""))
        params = interpretation.get("params", {})
        if not isinstance(params, dict):
            params = {}

        # Already-spoken AI answers — these were handled during interpret()
        if intent_type in ("answer", "delegated") and params.get("spoken"):
            return True

        # Source-based detection (pipeline annotates source on results)
        source = str(params.get("source", "") or interpretation.get("source", ""))
        if source in ("ai", "agent", "llm_route"):
            return True

        return False

    async def _try_command_registry(self, transcript: str, *, user_id: str) -> dict | None:
        pipeline = getattr(self.intent, "_pipeline", None)
        resolver = getattr(pipeline, "try_command_registry", None)
        if not callable(resolver):
            return None
        try:
            result = await resolver(transcript, user_key=user_id)
        except LookupError as exc:
            return {
                "ok": False,
                "success": False,
                "intent": "auth_required",
                "message": str(exc),
                "spoken": False,
                "continue_listening": False,
                "command_registry": True,
            }
        except Exception as exc:
            logger.debug("Voice command registry resolution failed: %s", exc)
            return None
        if result is None:
            return None
        data = dict(result.data) if getattr(result, "data", None) else {}
        message = data.get("message", "")
        return {
            "ok": bool(result.ok),
            "success": bool(result.ok),
            "intent": str(result.intent or "command_registry"),
            "message": message if isinstance(message, str) else str(message),
            "spoken": False,
            "continue_listening": False,
            "command_registry": True,
        }

    def _resolve_voice_user_id(self) -> str:
        try:
            from core.user_context import (
                get_current_user_id,
                get_desktop_authenticated_user_id,
                is_desktop_local_principal,
            )
        except ImportError as exc:
            raise LookupError("user_id is required for voice command handling") from exc

        try:
            user_id = str(get_current_user_id() or "").strip()
        except LookupError:
            try:
                user_id = str(get_desktop_authenticated_user_id() or "").strip()
            except LookupError as desktop_exc:
                raise LookupError("user_id is required for voice command handling") from desktop_exc
        if not user_id or is_desktop_local_principal(user_id):
            try:
                user_id = str(get_desktop_authenticated_user_id() or "").strip()
            except LookupError as exc:
                raise LookupError("user_id is required for voice command handling") from exc
        if not user_id:
            raise LookupError("user_id is required for voice command handling")
        return user_id

    def _voice_ai_controller(self) -> Any | None:
        pipeline = getattr(self.intent, "_pipeline", None)
        if pipeline is None:
            return None
        return getattr(pipeline, "ai_controller", None)

    def _voice_turn_metadata(
        self,
        ctx: CommandContext,
        *,
        source: str,
        barge_in: bool = False,
    ) -> VoiceTurnMetadata:
        transcriber = getattr(self.voice_pipeline, "transcriber", None)
        no_speech_prob = getattr(transcriber, "last_no_speech_prob", None)
        confidence: float | None = None
        if isinstance(no_speech_prob, (int, float)):
            confidence = 1.0 - float(no_speech_prob)

        device_id: str | None = None
        for holder in (
            self.voice_pipeline,
            getattr(self.voice_pipeline, "recorder", None),
            getattr(self.voice_pipeline, "capture", None),
        ):
            if holder is None:
                continue
            raw_device_id = getattr(holder, "device_id", None) or getattr(holder, "input_device_id", None)
            if raw_device_id:
                device_id = str(raw_device_id)
                break

        wake_event_id = str(ctx.data_clip_id) if ctx.data_clip_id is not None else None
        audio_session_id = getattr(self.state, "voice_session_id", None) or wake_event_id
        return VoiceTurnMetadata(
            source=source,
            confidence=confidence,
            device_id=device_id,
            wake_event_id=wake_event_id,
            barge_in=barge_in,
            audio_session_id=str(audio_session_id) if audio_session_id else None,
            channel="voice",
        )

    async def _record_voice_event(
        self,
        event_type: str,
        metadata: dict[str, object],
        *,
        user_id: str | None = None,
    ) -> None:
        ai_ctrl = self._voice_ai_controller()
        record_voice_event = getattr(ai_ctrl, "record_voice_event", None)
        if not callable(record_voice_event):
            return
        try:
            resolved_user_id = user_id or self._resolve_voice_user_id()
            result = record_voice_event(
                event_type,
                metadata,
                user_id=resolved_user_id,
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Voice event frame write failed for %s", event_type)

    async def _interpret_and_dispatch(
        self,
        transcript: str,
        *,
        is_follow_up: bool = False,
        user_id: str,
    ) -> dict | None:
        """Interpret and dispatch the command.

        Args:
            transcript: User's transcribed speech.
            is_follow_up: True for turn > 1 in conversational mode.
                When True, uses a fast path that skips intent classification
                and routes directly to the LLM with conversation history.

        Uses a two-tier timeout strategy:
        - interpret() runs with a soft deadline: if it takes longer than
          VOICE_COMMAND_TIMEOUT (30s), the user hears "Working on it" but
          the interpretation continues in the foreground until the pipeline's
          own timeout fires (up to 600s for agent tasks).
        - dispatch() keeps the 30s hard timeout for simple commands.  For
          agent-tier results (already executed during interpret()), dispatch
          is just envelope wrapping and finishes instantly.
        """
        _update_state_hub_voice_mode("processing", user_id=user_id)

        if _voice_account_required(user_id=user_id):
            from core.account_gate import account_required_envelope_data

            return account_required_envelope_data()

        registry_result = await self._try_command_registry(transcript, user_id=user_id)
        if registry_result is not None:
            return registry_result

        # GAP-7: Fast path for conversational follow-ups.
        # Skip the full IntentBridge interpret/dispatch cycle and route
        # directly to the IntentPipeline with force_ai=True so the LLM
        # receives conversation history and handles short answers like
        # "yes", "the large one", "tomorrow" without re-classification.
        if is_follow_up:
            _lower = transcript.strip().lower()
            _INSTANT_FOLLOW_UP = frozenset(
                {
                    "stop",
                    "cancel",
                    "nevermind",
                    "never mind",
                    "quit",
                    "shut up",
                    "pause",
                }
            )
            if _lower not in _INSTANT_FOLLOW_UP:
                fast_result = await self._interpret_follow_up_direct(
                    transcript,
                    user_id=user_id,
                )
                if fast_result is not None:
                    return fast_result
                # Fast path failed — fall through to normal path
                logger.debug("Follow-up fast path returned None, using normal path")

        # ---- Phase 0: Try streaming answer path (LLM + TTS overlap) ----
        # For simple chat/answer queries, stream LLM tokens directly to TTS
        # so the user hears the first sentence while the LLM is still
        # generating.  Falls back to the batch path for agent tasks, commands,
        # and unsupported providers.
        streaming_result = await self._try_streaming_answer(transcript, user_id=user_id)
        if streaming_result is not None:
            return streaming_result

        # ---- Phase 1: Interpretation (with soft "working on it" notification) ----
        interpretation = await self._interpret_with_progress(transcript)
        if interpretation is None:
            return None

        # ---- Phase 2: Dispatch ----
        # Agent-tier results were already executed during interpret() — the
        # AI controller ran tools, generated a response, and spoke via TTS.
        # dispatch() for these just wraps the result and returns instantly.
        # Simple commands (play, pause, etc.) still get the 30s hard timeout.
        try:
            if self._is_agent_tier_interpretation(interpretation):
                # Fast path — dispatch is just envelope wrapping for agent results
                logger.debug(
                    "Agent-tier result detected (type=%s), dispatching without timeout",
                    interpretation.get("type"),
                )
                result_envelope = await self.intent.dispatch(interpretation)
            else:
                result_envelope = await asyncio.wait_for(
                    self.intent.dispatch(interpretation),
                    timeout=VOICE_COMMAND_TIMEOUT,
                )
            return self._unwrap_envelope(result_envelope)
        except TimeoutError:
            logger.error(
                "Intent dispatch timed out after %ds transcript_length=%d",
                VOICE_COMMAND_TIMEOUT,
                len(transcript),
            )
            return None

    async def _interpret_follow_up_direct(
        self,
        transcript: str,
        *,
        user_id: str | None = None,
    ) -> dict | None:
        """Fast-path interpretation for conversational follow-ups.

        Bypasses the IntentBridge interpret/dispatch cycle and routes
        directly through the IntentPipeline with ``force_ai=True``.
        The pipeline skips Phase 1 (instant commands / regex) and sends
        the transcript straight to the AI controller, which already has
        conversation history from prior turns.

        Returns the unwrapped result dict, or None on failure (caller
        should fall back to the normal path).
        """
        pipeline = getattr(self.intent, "_pipeline", None)
        if pipeline is None:
            return None
        voice_user_key = str(user_id or "").strip()
        if not voice_user_key:
            # Resolve user identity so the pipeline binds the correct per-user
            # conversation manager. Without this, a shared pipeline on desktop
            # daemon mode would write voice turns under the wrong tenant when
            # multiple users are linked to the same process (CHAN-R1).
            try:
                voice_user_key = self._resolve_voice_user_id()
            except LookupError:
                voice_user_key = ""
        if not voice_user_key:
            return {
                "ok": False,
                "intent": "auth_required",
                "message": "user_id is required for voice command handling",
                "spoken": False,
                "continue_listening": False,
                "success": False,
            }
        try:
            pipeline_result = await pipeline.process(
                transcript,
                force_ai=True,
                user_key=voice_user_key,
            )
            # Convert PipelineResult to the dict shape the voice handler expects
            data = dict(pipeline_result.data) if pipeline_result.data else {}
            result: dict[str, Any] = {
                "ok": pipeline_result.ok,
                "intent": pipeline_result.intent,
                "message": data.get("message", ""),
                "spoken": data.get("spoken", False),
                "continue_listening": data.get("continue_listening", False),
                "success": pipeline_result.ok,
            }
            card = data.get("card")
            if card and isinstance(card, dict):
                result["card"] = card
            return result
        except Exception:
            logger.warning(
                "Follow-up fast path failed, falling back: %s",
                transcript[:60],
                exc_info=True,
            )
            return None

    async def _try_streaming_answer(self, transcript: str, *, user_id: str) -> dict | None:
        """Try to stream LLM answer tokens to TTS concurrently.

        When the LLM provider supports streaming and the request produces a
        simple text answer (not a command or agent task), this method runs
        LLM token generation and TTS playback concurrently so the user hears
        the first sentence while the LLM is still generating.

        Returns the completed result dict on success, or ``None`` to signal
        the caller to fall back to the batch path.
        """
        # Need the AI controller for streaming
        pipeline = getattr(self.intent, "_pipeline", None)
        if pipeline is None:
            return None
        ai_ctrl = getattr(pipeline, "ai_controller", None)
        if ai_ctrl is None or not hasattr(ai_ctrl, "stream_answer_tokens"):
            return None

        # Need TTS for streaming playback
        tts_enabled = self.capabilities.get("enable_tts", True)
        if not self.tts or not tts_enabled:
            return None

        # Check if TTS has streaming support
        if not hasattr(self.tts, "speak_streaming"):
            return None

        _update_state_hub_voice_mode("processing", user_id=user_id)

        token_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _token_generator():
            """Async generator that yields tokens from the queue."""
            while True:
                token = await token_queue.get()
                if token is None:
                    break
                yield token

        # Run LLM streaming and TTS concurrently
        llm_task = asyncio.ensure_future(ai_ctrl.stream_answer_tokens(transcript, token_queue))

        # Start TTS immediately — it reads from the queue as tokens arrive
        _update_state_hub_voice_mode("speaking", user_id=user_id)
        tts_task = asyncio.ensure_future(self.tts.speak_streaming(_token_generator()))

        try:
            # Wait for both to complete
            stream_result, spoken_text = await asyncio.gather(llm_task, tts_task)
        except Exception:
            logger.debug(
                "Streaming answer failed, falling back to batch: %s",
                transcript[:60],
                exc_info=True,
            )
            # Cancel any pending tasks
            for task in (llm_task, tts_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            return None

        if stream_result is None:
            # Streaming not applicable (agent mode, command, etc.)
            # TTS may have consumed some tokens — but since stream_result
            # is None, the queue should have received sentinel immediately.
            return None

        # Streaming succeeded — result already has spoken=True
        # Update message with the full spoken text (for logging/broadcast)
        if spoken_text and isinstance(spoken_text, str):
            if not stream_result.get("message"):
                stream_result["message"] = spoken_text

        await self._record_voice_event(
            "streaming_tts_finalized",
            {
                "source": "streaming_tts",
                "spoken_chars": len(spoken_text) if isinstance(spoken_text, str) else 0,
                "response_intent": str(stream_result.get("intent", "")),
            },
            user_id=user_id,
        )

        logger.info(
            "Streaming answer complete: %d chars spoken",
            len(spoken_text) if spoken_text else 0,
        )
        return stream_result

    async def _play_follow_up_cue(self) -> None:
        """Play a brief audio cue to signal Viola is listening for follow-up.

        Uses a very short TTS utterance. Non-critical — failures are
        swallowed so the conversation loop is never disrupted.
        """
        if not self.tts:
            return
        try:
            speak_coro = self.tts.speak("Mm-hmm?")
            if asyncio.iscoroutine(speak_coro):
                await speak_coro
        except Exception as exc:
            logger.debug("Follow-up cue failed (non-critical): %s", exc)

    @staticmethod
    def _broadcast_follow_up_listening() -> None:
        """Broadcast a WS event so the React UI can show a listening indicator."""
        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is None:
                return
            import asyncio as _aio

            # Resolve user_id from ContextVar for broadcast scoping
            try:
                from core.user_context import get_current_user_id

                _uid = get_current_user_id()
            except (ImportError, LookupError):
                _uid = None

            loop = _aio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    hub.broadcast(
                        "voice_mode",
                        {"mode": "conversing"},
                        user_id=_uid,
                        force=True,
                    )
                )
        except Exception:
            pass

    def _interpret_hard_timeout(self) -> float:
        """Hard outer bound (seconds) for a single interpret() call (WL-1).

        A hung downstream await under interpret() must never leave the wake
        detector paused forever — handle_once() pauses it at entry and only
        resumes it once handle_once returns, so an unbounded interpret bricks
        the voice session (Viola cannot even hear "stop"). This bound is the
        backstop that guarantees the turn always ends.

        It must sit ABOVE the agent's legitimate maximum wall-clock so it never
        strangles a real long-running agent task. The agent budget
        (settings.agent_timeout_seconds, the source of truth passed to the
        executor at ai_controller.py) auto-extends x1.5 once (agent_loop.py),
        so the legitimate ceiling is budget * 1.5. We use budget * 2.0 (with a
        1800s floor for small budgets) to clear that ceiling with margin for
        intent classification, tool wrap-up and TTS inside the interpret path.
        """
        agent_budget = 900.0
        try:
            from config.settings import settings

            agent_budget = float(getattr(settings, "agent_timeout_seconds", 900.0) or 900.0)
        except (ImportError, AttributeError, TypeError, ValueError):
            logger.debug("Could not read agent_timeout_seconds; using default for interpret bound")
        return max(agent_budget * 2.0, 1800.0)

    async def _interpret_with_progress(self, transcript: str) -> dict | None:
        """Run interpret() without automatic progress speech.

        Returns:
            Unwrapped interpretation dict, or None on failure.
        """
        interpret_task = asyncio.ensure_future(self.intent.interpret(transcript))

        try:
            # Wait up to VOICE_COMMAND_TIMEOUT for fast interpretation
            done, _pending = await asyncio.wait(
                {interpret_task},
                timeout=VOICE_COMMAND_TIMEOUT,
            )

            if done:
                # Interpretation completed within the timeout — normal path
                interpretation_envelope = interpret_task.result()
                return self._unwrap_envelope(interpretation_envelope)

            # Interpretation is still running — this is likely an agent task.
            # Keep waiting silently; final answers and user interruptions still work.
            logger.info(
                "Interpretation exceeding %ds, likely agent task",
                VOICE_COMMAND_TIMEOUT,
            )

            # Keep waiting, but under a generous HARD outer timeout so a single
            # unbounded await anywhere under interpret() can never hang the turn
            # forever with the mic paused (WL-1). The bound sits well above the
            # agent's legitimate max wall-clock (see _interpret_hard_timeout).
            # Uses asyncio.wait (not wait_for) to mirror the soft-deadline idiom
            # above and to keep cancellation handling explicit and local.
            hard_timeout = self._interpret_hard_timeout()
            done, _pending = await asyncio.wait(
                {interpret_task},
                timeout=hard_timeout,
            )
            if done:
                interpretation_envelope = interpret_task.result()
                return self._unwrap_envelope(interpretation_envelope)

            # Hard outer timeout fired: a downstream await did not return within
            # the generous bound. Cancel and end the turn so handle_once()'s
            # finally resumes the wake detector — Viola stays able to hear "stop".
            logger.error(
                "Interpretation exceeded hard outer timeout %.0fs; cancelling to "
                "release the wake detector transcript_length=%d",
                hard_timeout,
                len(transcript),
            )
            interpret_task.cancel()
            # Give the cancellation a bounded chance to settle so we don't leak
            # the task; asyncio.wait never raises on task cancellation/error.
            await asyncio.wait({interpret_task}, timeout=TIMEOUT_GRACE)
            return None

        except Exception:
            logger.exception("Interpretation failed transcript_length=%d", len(transcript))
            if not interpret_task.done():
                interpret_task.cancel()
            return None

    @staticmethod
    def _voice_output_muted(user_id: str) -> bool:
        """Whether the user has muted Viola's voice output.

        Read fresh from SettingsManager on every reply so flipping the mute
        takes effect on the next thing Viola would have said, rather than at
        the next restart. Fails open (unmuted) if settings cannot be read: a
        settings hiccup should not silently make Viola stop talking.
        """
        try:
            from ui.settings_manager import get_settings_manager

            return bool(get_settings_manager().get("voice_muted", False, user_id=user_id))
        except Exception:  # noqa: BLE001, RUF100 - a settings hiccup must not silently mute Viola
            logger.debug("Could not read voice_muted; assuming not muted", exc_info=True)
            return False

    async def _speak_response(self, result: dict, *, user_id: str) -> None:
        """Speak the response if appropriate. Awaits TTS directly so ducking stays active.

        When continuous capture is available and conversational mode is active,
        uses interruptible TTS that monitors for user speech via VAD.
        """
        message = result.get("message", "")
        already_spoken = result.get("spoken", False)
        tts_enabled = self.capabilities.get("enable_tts", True)

        # voice_muted is the user's "stop talking" switch. It was only ever
        # consulted for replies to typed commands (ui/api/routes/command.py),
        # so muting Viola's voice left spoken turns — the ones the setting is
        # actually about — still speaking. Checked here, on the path that
        # speaks a reply to a voice command, it means what its name says.
        if self._voice_output_muted(user_id):
            logger.debug("voice_muted is on; skipping spoken reply")
            return

        if not tts_enabled and self.tts:
            logger.debug(
                "Runtime profile '%s' disabled local TTS; skipping speech output",
                getattr(self.state, "runtime_profile", "unknown"),
            )
            return

        if self.tts and message and not already_spoken and tts_enabled:
            try:
                logger.info("Speaking response: %s...", message[:100])
                _update_state_hub_voice_mode("speaking", user_id=user_id)

                # Try interruptible TTS if continuous capture is available
                capture = self._get_continuous_capture()
                if capture is not None and capture.is_running:
                    interrupted = await self._speak_response_interruptible(
                        message,
                        capture,
                        user_id=user_id,
                    )
                    if interrupted:
                        # Signal that user interrupted — caller should
                        # immediately use captured audio for next turn
                        result["_interrupted"] = True
                        return
                else:
                    # Standard non-interruptible TTS
                    speak_coro = self.tts.speak(message)
                    if asyncio.iscoroutine(speak_coro):
                        await speak_coro

                try:
                    from admin.instrumentation import record_feature_used

                    record_feature_used("tts")
                except Exception:
                    logger.debug("TTS feature usage instrumentation skipped")
            except Exception as e:
                logger.warning("TTS failed to speak response: %s", e)
        elif already_spoken:
            logger.debug("Response already spoken by interpreter")

    async def _speak_response_interruptible(
        self,
        message: str,
        capture: Any,
        *,
        user_id: str,
    ) -> bool:
        """Speak with interrupt monitoring via continuous capture VAD.

        Sets the continuous capture to vad_monitor mode during TTS playback.
        If the user speaks for >300ms (detected by VAD on AEC'd audio), TTS
        is cancelled and the method returns True.

        The captured speech audio is preserved in the capture's recording
        buffer for immediate transcription.

        Args:
            message: Text to speak via TTS.
            capture: Active ContinuousMicCapture instance.

        Returns:
            True if user interrupted, False if TTS completed normally.
        """
        from voice.continuous_capture import MODE_RECORD

        interrupt_event = asyncio.Event()

        def _on_interrupt() -> None:
            """Called from capture thread when VAD detects sustained speech."""
            # Thread-safe: asyncio.Event.set() is safe to call from any thread
            interrupt_event.set()

        # Start VAD monitoring (also accumulates audio in buffer)
        capture.set_mode("vad_monitor", on_interrupt=_on_interrupt)

        try:
            # Race TTS against interrupt
            tts_task = asyncio.ensure_future(self._do_speak(message))
            interrupt_task = asyncio.ensure_future(interrupt_event.wait())

            done, pending = await asyncio.wait(
                {tts_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if interrupt_task in done:
                # Stop the TTS engine immediately so audio output ceases
                # without waiting for the async task to cancel gracefully.
                if self.tts and hasattr(self.tts, "stop"):
                    try:
                        self.tts.stop()
                    except Exception:
                        pass

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if interrupt_task in done:
                # User interrupted — preserve the accumulated VAD buffer
                # (it contains the speech that triggered the interrupt).
                # Do NOT call set_mode(MODE_RECORD) here because that
                # clears the recording buffer.  Instead switch to paused
                # so no more frames are appended, and let the caller
                # retrieve the buffer via get_recording().
                capture.set_mode("paused")
                await self._record_voice_event(
                    "tts_interrupted",
                    {
                        "source": "barge_in",
                        "barge_in": True,
                        "message_chars": len(message),
                    },
                    user_id=user_id,
                )
                logger.info("TTS interrupted by user speech (buffer preserved)")
                return True

            return False

        except Exception as exc:
            logger.warning("Interruptible TTS error: %s", exc)
            return False
        finally:
            # If still in vad_monitor mode (e.g., TTS completed normally),
            # switch to paused to stop VAD processing
            if capture.mode == "vad_monitor":
                capture.set_mode("paused")

    async def _do_speak(self, message: str) -> None:
        """Execute TTS speak (wrapper for cancellation support)."""
        speak_coro = self.tts.speak(message)
        if asyncio.iscoroutine(speak_coro):
            await speak_coro

    def _get_continuous_capture(self) -> Any | None:
        """Get the ContinuousMicCapture instance if available.

        Checks the voice pipeline for a continuous capture instance.
        Returns None if not configured.
        """
        # Check if already cached
        if self._continuous_capture is not None:
            return self._continuous_capture

        # Check if the voice pipeline has one
        capture = getattr(self.voice_pipeline, "_continuous_capture", None)
        if capture is not None:
            self._continuous_capture = capture
            return capture

        return None

    def _save_interrupted_audio(self) -> str | None:
        """Save audio from the continuous capture buffer to a temp WAV file.

        Called when TTS is interrupted by user speech. The capture buffer
        contains the speech that triggered the interrupt.

        Returns:
            Path to the saved WAV file, or None if no audio available.
        """
        import wave

        capture = self._get_continuous_capture()
        if capture is None:
            return None

        pcm_bytes = capture.get_recording()
        if not pcm_bytes or len(pcm_bytes) < 3200:  # < 100ms at 16kHz
            logger.debug("Interrupted audio too short (%d bytes), discarding", len(pcm_bytes))
            return None

        try:
            temp_dir = _get_voice_temp_dir()
            output_path = temp_dir / ("interrupt_%s.wav" % secrets.token_hex(8))

            with wave.open(str(output_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(SAMPLE_RATE_16K)
                wf.writeframes(pcm_bytes)

            duration_ms = len(pcm_bytes) / (SAMPLE_RATE_16K * 2) * 1000
            logger.info(
                "Saved interrupted speech: %s (%.0fms)",
                output_path,
                duration_ms,
            )
            return str(output_path)

        except Exception as exc:
            logger.warning("Failed to save interrupted audio: %s", exc)
            return None

    def _get_interrupted_audio_buffer(self) -> np.ndarray | None:
        """Get interrupted speech audio as a numpy int16 buffer (no disk I/O).

        Returns the continuous capture buffer as a numpy array, or None if
        no usable audio is available. This avoids writing a temp WAV file.
        """
        import numpy as np

        capture = self._get_continuous_capture()
        if capture is None:
            return None

        pcm_bytes = capture.get_recording()
        if not pcm_bytes or len(pcm_bytes) < 3200:  # < 100ms at 16kHz
            logger.debug(
                "Interrupted audio too short (%d bytes), discarding",
                len(pcm_bytes) if pcm_bytes else 0,
            )
            return None

        try:
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).copy()
            duration_ms = len(audio) / SAMPLE_RATE_16K * 1000
            logger.info(
                "Captured interrupted speech buffer: %d samples (%.0fms)",
                len(audio),
                duration_ms,
            )
            return audio
        except Exception as exc:
            logger.warning("Failed to create interrupted audio buffer: %s", exc)
            return None

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def _get_no_speech_prob(self) -> float:
        """Get no_speech_prob from the last Whisper transcription."""
        try:
            transcriber = getattr(self.voice_pipeline, "transcriber", None)
            if transcriber is None:
                return 0.0
            return getattr(transcriber, "last_no_speech_prob", 0.0)
        except Exception:
            return 0.0

    def _pause_wake_detector(self, ctx: CommandContext) -> None:
        """Pause wake detector during voice command processing."""
        if ctx.wake_detector and hasattr(ctx.wake_detector, "pause"):
            ctx.wake_detector.pause()

    def _resume_wake_detector(self, ctx: CommandContext) -> None:
        """Resume wake detector after voice command processing."""
        if ctx.wake_detector and hasattr(ctx.wake_detector, "resume"):
            ctx.wake_detector.resume()

    def _cleanup_after_command(self, ctx: CommandContext) -> None:
        """Cleanup after voice command processing."""
        logger.debug("handle_once() finally block - cleaning up")

        try:
            user_id = ctx.user_id or self._resolve_voice_user_id()
        except LookupError as exc:
            logger.warning("Voice cleanup continuing without user_id: %s", exc)
            user_id = ""
        if user_id:
            _update_state_hub_voice_mode("idle", user_id=user_id)
        self._resume_wake_detector(ctx)
        if user_id:
            self.state.set_listening(False, user_id=user_id)
        else:
            try:
                self.state.set_listening(False)
            except (TypeError, ValueError, RuntimeError) as exc:
                logger.debug("Voice cleanup skipped userless listening reset: %s", exc)

        self._emit_listening_stopped()
        self._update_final_metrics(ctx)
        self._cleanup_ducking(ctx)
        # Privacy: zero out any remaining audio buffer and delete temp files
        ctx.clear_audio_buffer()
        self._cleanup_audio_file(ctx)

    def _emit_listening_stopped(self) -> None:
        """Emit listening stopped event."""
        logger.debug("Emitting voice_listening_stopped event")
        if emit_debug_event is not None:
            emit_debug_event(
                "voice_listening_stopped",
                {"state": "idle", "source": "voice_orchestrator"},
                source="voice_orchestrator",
            )
            logger.debug("voice_listening_stopped event emitted")
        else:
            logger.warning("emit_debug_event is None - cannot emit stop event")
        logger.info("UI: Voice state -> IDLE (listening ended)")

    def _update_final_metrics(self, ctx: CommandContext) -> None:
        """Update metrics after command processing."""
        self._last_stt_activity = time.time()
        self._metrics.record_stt_idle(0.0)
        self._metrics.heartbeat("voice.stt", status="idle", state="idle")

        if emit_debug_event is not None and not ctx.stt_event_emitted:
            emit_debug_event(
                "stt_finished",
                {"provider": ctx.provider_name, "status": "completed"},
                source="voice_orchestrator",
            )

    def _cleanup_ducking(self, ctx: CommandContext) -> None:
        """Cleanup audio ducking if not already done."""
        if not ctx.ducking_cleaned_up:
            self._unduck_audio()
            logger.debug("Audio unducked in finally block")

    def _cleanup_audio_file(self, ctx: CommandContext) -> None:
        """Cleanup temporary audio file (skipped for buffer-based recordings)."""
        if not ctx.audio_file or ctx.audio_file == "buffer":
            return

        try:
            from core.validation import validate_file_path

            is_valid, error_msg = validate_file_path(ctx.audio_file, expected_base=str(get_temp_dir()))
            if is_valid:
                os.remove(ctx.audio_file)
                ctx.audio_file = None
            else:
                logger.warning("Skipped deletion of invalid audio file path: %s", error_msg)
        except Exception as e:
            error_msg = sanitize_error_message(e)
            logger.warning("Failed to remove audio file: %s", error_msg)

    def _unduck_audio(self) -> None:
        """Unduck audio after voice command processing."""
        try:
            from utils.audio_ducking import get_global_ducker

            ducker = get_global_ducker()
            if ducker:
                ducker.unduck()
                logger.debug("Audio unducked after voice command")
        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
