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


# What the user says right after a wake to take it back ("never mind",
# "cancel", "nothing", "not you"), optionally wrapped in filler. Deliberately
# narrower than _DISMISS_RE: "stop", "skip" and "quiet" are real commands on a
# wake turn ("Viola, stop" stops the music), so they are not undo phrases here.
_WAKE_UNDO_RE = re.compile(
    r"^\s*"
    r"(?:(?:oh|oops|sorry|um+|uh+|no|nah|okay|ok|hey|viola)\s*[,.!]?\s*)*"
    r"(?:"
    r"never\s*mind"
    r"|cancel(?:\s+(?:that|it))?"
    r"|nothing"
    r"|forget\s+(?:it|about\s+it)"
    r"|not\s+(?:you|now)"
    r"|false\s+alarm"
    r"|(?:I\s+)?(?:wasn'?t|was\s+not)\s+talking\s+to\s+you"
    r"|I\s+(?:didn'?t|did\s+not)\s+(?:call|mean|say|need)\s+(?:you|viola|that|anything)"
    r")"
    r"(?:\s*[,.!]?\s*(?:sorry|thanks|thank\s+you|viola))*"
    r"[.!,?\s]*$",
    re.IGNORECASE,
)
# A transcript with no words in it: punctuation, or only hesitation sounds.
_NO_COMMAND_RE = re.compile(r"^[\s.,!?\u2026-]*(?:(?:h?m+|hmm+|um+|uh+|ah+|er+m?)[\s.,!?\u2026-]*)*$", re.IGNORECASE)


def _is_wake_undo(transcript: str) -> bool:
    """True when what followed a wake means "no command": an undo phrase or no words at all."""
    text = (transcript or "").strip()
    return bool(_NO_COMMAND_RE.match(text) or _WAKE_UNDO_RE.match(text))


_ECHO_WORD_RE = re.compile(r"[a-z0-9']+")
# A transcript this short is too easy to match by accident ("jazz" after
# "jazz or rock?"), so it is never called an echo.
_ECHO_MIN_WORDS = 3
# Share of the transcript's words that must line up, in order, with the reply.
_ECHO_MATCH_RATIO = 0.75
# How long after a reply its echo can still turn up in a recording.
_ECHO_WINDOW_SEC = 30.0
# Two words this similar are the same word misheard ("playin"/"playing",
# "relaxin"/"relaxing"); "toe"/"tokyo" is not.
_FUZZY_WORD_RATIO = 0.8


def _echo_words(text: str) -> list[str]:
    return _ECHO_WORD_RE.findall((text or "").lower().replace("’", "'"))


def _same_word(heard: str, spoken: str) -> bool:
    """True when ``heard`` is plausibly ``spoken`` as Whisper heard it back."""
    if heard == spoken:
        return True
    a, b = heard.replace("'", ""), spoken.replace("'", "")
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    if a.startswith(b) or b.startswith(a):
        return True
    from difflib import SequenceMatcher

    return SequenceMatcher(a=a, b=b, autojunk=False).ratio() >= _FUZZY_WORD_RATIO


def _canonical_words(heard: list[str], spoken: list[str]) -> list[str]:
    """Rewrite each heard word as the spoken word it is a mishearing of, if any."""
    vocabulary = list(dict.fromkeys(spoken))
    return [next((w for w in vocabulary if _same_word(word, w)), word) for word in heard]


def _matched_in_order(heard: list[str], spoken: list[str]) -> list[tuple[int, int]]:
    """Matching blocks (start in ``heard``, size) of ``heard`` against ``spoken``, in order."""
    from difflib import SequenceMatcher

    matcher = SequenceMatcher(a=_canonical_words(heard, spoken), b=spoken, autojunk=False)
    return [(block.a, block.size) for block in matcher.get_matching_blocks() if block.size]


def _is_echo_of_reply(transcript: str, reply: str) -> bool:
    """True when a "user" transcript is really Viola's own reply heard back.

    1.0.5 laptop: with echo cancellation not removing Viola's voice, barge-in
    recorded the reply "That one tripped me up, try again in a sec?", Whisper
    returned "That one tripped me up." and Viola answered it as a command. A
    person's words almost never repeat most of the reply in the same order.
    Words are compared loosely ("playin" for "playing", "4.55pm" for "4:55
    PM"), because Whisper rarely hears a speaker's echo word for word.
    """
    heard = _echo_words(transcript)
    if len(heard) < _ECHO_MIN_WORDS:
        return False
    spoken = _echo_words(reply)
    if not spoken:
        return False
    matched = sum(size for _start, size in _matched_in_order(heard, spoken))
    return matched / len(heard) >= _ECHO_MATCH_RATIO


# --------------------------------------------------------------------------- #
# Barge-in while Viola is speaking                                             #
# --------------------------------------------------------------------------- #
#
# Build-7 laptop (1.0.5): 4 of 4 spoken replies were cut off 2-5 s in by "TTS
# interrupt detected" on Viola's OWN voice, and the partial transcript ran as a
# command ("Playing relaxing." -> a different song, "tomorrow" -> the weather
# again, "Now play great." -> a second skip, a second phone call). The echo
# canceller reported 3-19 dB of echo reduction: working, but with the speaker a
# few centimetres from the microphone the rest of Viola's voice is still
# "speech" to the VAD. No canceller setting makes that safe on every laptop, so
# the VAD only raises a CANDIDATE. Viola keeps talking while the last few
# seconds are transcribed, and she stops only when that transcript contains the
# wake word ("Viola, stop", "Viola, play rock instead") and what follows it is
# not Viola's own sentence. Anything else (her own words, however garbled,
# music, a TV) is ignored, the monitor re-arms, and the reply plays to the end.
#
# A bare "stop" without the wake word does not interrupt: replayed through the
# real models (Kokoro reply, laptop echo path, Silero VAD, faster-whisper
# tiny.en), a check window that ended mid-word turned "Tokyo, Sa-" into
# "Tokyo. Stop." For the same reason a wake word that is the LAST word of a
# window is checked once more on a later window before Viola stops, and once
# she has stopped, the user's whole request is transcribed again after they
# finish, so the command is never a half-heard phrase cut at a window edge.

# After the VAD's 0.3 s of speech, how long to let the phrase finish before
# checking it ("Viola, stop" takes about a second to say).
_BARGE_IN_SETTLE_SEC = 0.8
# How much of the most recent audio is transcribed per check.
_BARGE_IN_WINDOW_SEC = 3.0
# Checks per reply. A canceller that leaves all of Viola's voice in raises a
# candidate every couple of seconds; this bounds the speech-to-text work.
_BARGE_IN_MAX_CHECKS = 8
# A check that takes longer than this counts as "not the user".
_BARGE_IN_STT_TIMEOUT_SEC = 5.0
# A run of this many consecutive words of the reply, in order, inside the
# transcript is Viola's voice, not the user's.
_BARGE_IN_ECHO_RUN = 2
# Once Viola has stopped for the user: how long without speech means they
# finished their request, and the longest she waits for that.
_BARGE_IN_USER_DONE_SEC = 0.7
_BARGE_IN_USER_MAX_SEC = 5.0

# The wake word as Whisper writes it. Stricter than the wake Stage 2 list on
# purpose: here a false match stops Viola mid-sentence.
_BARGE_IN_WAKE_WORDS = frozenset({"viola", "violas", "vyola", "viyola", "voila", "fiola", "biola"})
_BARGE_IN_STOP_RE = re.compile(
    r"^(?:(?:ok|okay|hey|please|no)\s+)*"
    r"(?:stop(?:\s+(?:it|talking|now|please))*|be\s+quiet|quiet|shut\s+up|enough|that'?s\s+enough)"
    r"(?:\s+(?:please|now|thanks|thank\s+you))*$"
)
_BARGE_IN_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


@dataclass(frozen=True)
class BargeInDecision:
    """What a transcript heard while Viola was speaking means.

    ``kind`` is "ignore" (Viola keeps talking), "stop" (stop talking, nothing
    else), "listen" (the user said only the wake word: stop and listen) or
    "command" (stop and run ``command``).
    """

    kind: str
    reason: str
    command: str = ""
    # The wake word was the last word heard: the window may have cut a word of
    # Viola's own ("Vio-lin") or the user may still be talking. Check again.
    wake_at_end: bool = False

    @property
    def interrupts(self) -> bool:
        return self.kind != "ignore"


def _is_barge_in_wake_word(word: str) -> bool:
    w = word.lower().replace("'", "")
    if w in _BARGE_IN_WAKE_WORDS:
        return True
    if not 4 <= len(w) <= 6 or w[0] not in "vbf":
        return False
    from difflib import SequenceMatcher

    # One letter away from "viola" (vila, vioala, violah).
    return SequenceMatcher(a=w, b="viola", autojunk=False).ratio() >= 0.8


def _drop_reply_runs(tokens: list[str], spoken: list[str]) -> list[str]:
    """Remove every run of >= 2 consecutive reply words (Viola's voice) from ``tokens``."""
    if not tokens or not spoken:
        return list(tokens)
    lowered = [t.lower() for t in tokens]
    drop: set[int] = set()
    for start, size in _matched_in_order(lowered, spoken):
        if size >= _BARGE_IN_ECHO_RUN:
            drop.update(range(start, start + size))
    return [t for i, t in enumerate(tokens) if i not in drop]


def classify_barge_in_transcript(transcript: str, reply: str) -> BargeInDecision:
    """Decide whether what was heard while Viola spoke ``reply`` should stop her."""
    tokens = _BARGE_IN_TOKEN_RE.findall((transcript or "").replace("’", "'"))
    if not tokens:
        return BargeInDecision("ignore", "nothing_heard")
    spoken = _echo_words(reply)
    wake_at = max((i for i, t in enumerate(tokens) if _is_barge_in_wake_word(t)), default=None)

    if wake_at is None:
        rest = _drop_reply_runs(tokens, spoken)
        if len(rest) < len(tokens) or _is_echo_of_reply(transcript, reply):
            return BargeInDecision("ignore", "viola_own_words")
        if rest and _BARGE_IN_STOP_RE.match(" ".join(t.lower() for t in rest)):
            return BargeInDecision("ignore", "stop_without_wake_word")
        return BargeInDecision("ignore", "no_wake_word")

    after = _drop_reply_runs(tokens[wake_at + 1 :], spoken)
    if any(_is_barge_in_wake_word(w) for w in spoken):
        # Viola says her own name in this reply ("I'm Viola, ..."): the name in
        # the transcript may be hers. Only a clear request after it counts.
        if not after or _is_echo_of_reply(" ".join(after), reply):
            return BargeInDecision("ignore", "reply_says_viola")
    if not after:
        return BargeInDecision("listen", "wake_word", wake_at_end=wake_at == len(tokens) - 1)
    command = " ".join(after)
    if _BARGE_IN_STOP_RE.match(command.lower()):
        return BargeInDecision("stop", "wake_word_stop", command)
    return BargeInDecision("command", "wake_word_command", command)


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


_UI_ACTION_BROADCAST_KEYS = (
    "ui_action",
    "rooms_modal_tab",
    "path_identifier",
    "panel_id",
    "tab",
    "sub_tab",
    "section",
    "initial_section",
    "prefill",
    "room_name",
    "target_room",
)


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
        # A spoken panel or full-screen request carries a ui_action (as the
        # /v1/command broadcast does); without it the app shows the reply but
        # never opens the panel or goes full screen.
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        action_source = result if result.get("ui_action") else data
        if isinstance(action_source.get("ui_action"), str) and action_source["ui_action"]:
            for key in _UI_ACTION_BROADCAST_KEYS:
                if key in action_source:
                    payload[key] = action_source[key]
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
        # The last reply spoken aloud and when (time.monotonic()), so a
        # recording of Viola's own voice is not run as the user's next command.
        self._last_spoken_reply: tuple[str, float] | None = None
        # What the user said over Viola's last reply, once it was checked to be
        # the user (see _speak_response_interruptible).
        self._barge_in_decision: BargeInDecision | None = None
        # time.monotonic() when the last barge-in check took its audio window.
        self._barge_in_window_end: float | None = None
        # Same-class fallback when no running app holds the desktop's command service.
        self._local_command_service: Any | None = None

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
            if turn > 1 and self._transcript_is_own_reply(transcript):
                exit_reason = "self_echo"
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

            # Wake undo: right after a wake, "never mind" / "cancel" / "nothing",
            # or no words at all, ends the turn silently. No LLM call, nothing
            # spoken, ducked music restored. The wake is logged as a false one.
            if turn == 1 and _is_wake_undo(transcript):
                logger.info("Wake taken back or no command after wake; ending the turn silently")
                detector = getattr(self.voice_pipeline, "wake_detector", None)
                if detector:
                    detector.mark_detection_as_false_positive()
                _notify_data_collector("cancel", ctx.data_clip_id)
                self._unduck_audio()
                ctx.ducking_cleaned_up = True
                exit_reason = "wake_undo"
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
            # The user talked over the reply and the check in
            # _speak_response_interruptible found it was really the user (the
            # wake word or a bare "stop"); Viola's own voice never gets here.
            # The checked transcript is used as is; nothing is re-transcribed.
            barge_in_exit: str | None = None
            listen_after_barge_in = False
            while result.get("_interrupted"):
                decision = self._take_barge_in_decision()
                if decision is None or not conversation_enabled:
                    logger.info("TTS interrupted; ending the conversation")
                    barge_in_exit = "interrupted_no_audio" if decision is None else "interrupted"
                    break
                if decision.kind == "listen":
                    # "Viola" alone over the reply: stop talking and listen.
                    listen_after_barge_in = True
                    break
                turn += 1
                if turn > max_turns:
                    barge_in_exit = "max_turns"
                    break
                _update_state_hub_voice_mode("conversing", user_id=ctx.user_id)
                transcript = decision.command
                # Dismiss detection: "Viola, stop" / "stop" / "never mind"
                # over the reply ends the turn silently instead of running as
                # a new command, like telling a real assistant to stop.
                if decision.kind == "stop" or _is_dismiss_phrase(transcript):
                    logger.info("Interrupt dismiss detected - silently exiting")
                    barge_in_exit = "dismissed"
                    break
                await self._record_voice_event(
                    "barge_in_resume",
                    {
                        "source": "barge_in",
                        "barge_in": True,
                        "resume_target": "checked_transcript",
                        "sample_rate": SAMPLE_RATE_16K,
                    },
                    user_id=ctx.user_id,
                )
                logger.info("TTS interrupted by a request after the wake word; running it as the next turn")
                if not self._validate_transcript(transcript):
                    barge_in_exit = "invalid_transcript"
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
                    barge_in_exit = "interpret_failed"
                    break
                if result.get("intent") == "ignore":
                    barge_in_exit = "ignore"
                    break
                await self._speak_response(result, user_id=ctx.user_id)
                _broadcast_chat_response(result)
                # That reply can be interrupted too: the loop checks again.
            if barge_in_exit is not None:
                exit_reason = barge_in_exit
                break

            # --- Decide whether to continue ---
            if not conversation_enabled:
                break

            if not listen_after_barge_in and not result.get("continue_listening", False):
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
        """Run a spoken command through the same command path as push-to-talk.

        Push-to-talk and typed commands go ``POST /v1/command`` ->
        ``CommandService.execute`` -> the bridge's canonical
        ``IntentPipeline.process(user_key=<signed-in user>)``. A wake turn, and
        every follow-up or barge-in turn after it, takes exactly that path with
        the same ``user_id``: same account, same provider router, same
        conversation, same agent loop.

        Wake turns used to take a separate legacy route instead
        (``IntentBridge.interpret`` -> ``IntentInterpreter`` ->
        ``route_command``). That route ran on a second pipeline built at
        startup for the anonymous ``device-*`` identity and never passed the
        speaker's ``user_key``, so the pipeline rebound the turn to the device
        identity and the managed-AI account gate saw nobody signed in. Every
        wake turn that needed the AI failed with "Managed LLM provider blocked
        until account login" while push-to-talk on the same account worked
        (1.0.5 build-4 laptop test). Turns that never reached the AI (a music
        skill, "never mind") still worked, which is why it looked like it broke
        "after a while".

        Returns the result dict the rest of the voice turn reads (``message``,
        ``spoken``, ``continue_listening``, ``card``...), or ``None`` when the
        command failed or hit the hard outer timeout.
        """
        _update_state_hub_voice_mode("processing", user_id=user_id)

        if _voice_account_required(user_id=user_id):
            from core.account_gate import account_required_envelope_data

            return account_required_envelope_data()

        from services.command import CommandRequest

        request = CommandRequest(
            text=transcript,
            channel="voice",
            origin="voice_wake",
            user_id=user_id,
        )
        logger.debug("Voice turn via command service (follow_up=%s)", is_follow_up)
        command_result = await self._run_command_bounded(
            self._command_service().execute(request),
            transcript,
        )
        if command_result is None:
            return None
        return self._voice_result_from_command(command_result)

    def _command_service(self) -> Any:
        """Return the command service push-to-talk and typed commands use.

        The desktop builds one ``CommandService`` per process and keeps it on
        ``app.state.command_service`` (``ui/api/routes/command.py``); the
        companion relay reaches it the same way. Only when no app is running
        (headless runs, tests) is the same class built here around this
        handler's intent bridge, which owns the same canonical pipeline.
        """
        try:
            from ui.server import get_app

            app = get_app()
        except Exception:  # noqa: BLE001, RUF100 - no running app means the local fallback below
            app = None
        service = getattr(getattr(app, "state", None), "command_service", None)
        if service is not None and callable(getattr(service, "execute", None)):
            return service
        if self._local_command_service is None:
            from services.command import CommandService, CommandServiceContext

            self._local_command_service = CommandService(
                context=CommandServiceContext(state=None, music=self.music, intent=self.intent)
            )
        return self._local_command_service

    @staticmethod
    def _voice_result_from_command(command_result: Any) -> dict[str, Any]:
        """Shape a ``CommandResult`` the way the rest of the voice turn reads it.

        Uses the same envelope ``POST /v1/command`` returns, so a wake reply
        and a push-to-talk reply carry the same text and fields.
        """
        envelope = command_result.to_envelope()
        raw_data = envelope.get("data") if isinstance(envelope, dict) else None
        result: dict[str, Any] = dict(raw_data) if isinstance(raw_data, dict) else {}
        ok = bool(getattr(command_result, "ok", False))
        message = result.get("message")
        if not isinstance(message, str) or not message:
            message = ""
            error = envelope.get("error") if isinstance(envelope, dict) else None
            if not ok and isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
        result.update(
            ok=ok,
            success=ok,
            intent=str(getattr(command_result, "intent", "") or result.get("intent") or "unknown"),
            message=message,
            spoken=bool(result.get("spoken", False)),
            continue_listening=bool(result.get("continue_listening", False)),
        )
        return result

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

    def _command_hard_timeout(self) -> float:
        """Hard outer bound (seconds) for a single voice command (WL-1).

        A hung downstream await under the command must never leave the wake
        detector paused forever — handle_once() pauses it at entry and only
        resumes it once handle_once returns, so an unbounded command bricks
        the voice session (Viola cannot even hear "stop"). This bound is the
        backstop that guarantees the turn always ends.

        It must sit ABOVE the agent's legitimate maximum wall-clock so it never
        strangles a real long-running agent task. The agent budget
        (settings.agent_timeout_seconds, the source of truth passed to the
        executor at ai_controller.py) auto-extends x1.5 once (agent_loop.py),
        so the legitimate ceiling is budget * 1.5. We use budget * 2.0 (with a
        1800s floor for small budgets) to clear that ceiling with margin for
        intent classification, tool wrap-up and TTS inside the command path.
        """
        agent_budget = 900.0
        try:
            from config.settings import settings

            agent_budget = float(getattr(settings, "agent_timeout_seconds", 900.0) or 900.0)
        except (ImportError, AttributeError, TypeError, ValueError):
            logger.debug("Could not read agent_timeout_seconds; using default for command bound")
        return max(agent_budget * 2.0, 1800.0)

    async def _run_command_bounded(self, command: Any, transcript: str) -> Any | None:
        """Await one voice command under the WL-1 hard outer timeout.

        No progress speech: long agent tasks keep running silently and the
        final answer or a user interruption still works.

        Returns the command's result, or ``None`` if it raised or exceeded the
        hard outer timeout (it is cancelled then, so the turn ends and the wake
        detector resumes).
        """
        command_task = asyncio.ensure_future(command)

        try:
            done, _pending = await asyncio.wait({command_task}, timeout=VOICE_COMMAND_TIMEOUT)
            if done:
                return command_task.result()

            logger.info("Voice command exceeding %ds, likely agent task", VOICE_COMMAND_TIMEOUT)

            # Keep waiting, but under a generous HARD outer timeout so a single
            # unbounded await anywhere under the command can never hang the
            # turn forever with the mic paused (WL-1). The bound sits well above
            # the agent's legitimate max wall-clock (see _command_hard_timeout).
            hard_timeout = self._command_hard_timeout()
            done, _pending = await asyncio.wait({command_task}, timeout=hard_timeout)
            if done:
                return command_task.result()

            # Hard outer timeout fired: a downstream await did not return within
            # the generous bound. Cancel and end the turn so handle_once()'s
            # finally resumes the wake detector — Viola stays able to hear "stop".
            logger.error(
                "Voice command exceeded hard outer timeout %.0fs; cancelling to "
                "release the wake detector transcript_length=%d",
                hard_timeout,
                len(transcript),
            )
            command_task.cancel()
            # Give the cancellation a bounded chance to settle so we don't leak
            # the task; asyncio.wait never raises on task cancellation/error.
            await asyncio.wait({command_task}, timeout=TIMEOUT_GRACE)
            return None

        except Exception:
            logger.exception("Voice command failed transcript_length=%d", len(transcript))
            if not command_task.done():
                command_task.cancel()
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
                self._last_spoken_reply = (message, time.monotonic())
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
        """Speak with barge-in monitoring via the continuous capture's VAD.

        Sets the capture to vad_monitor mode while ``message`` plays. Sustained
        speech on the echo-cancelled microphone is only a CANDIDATE: Viola keeps
        talking while the last few seconds are transcribed, and she stops only
        when :func:`classify_barge_in_transcript` finds the user's wake word.
        Her own voice, music or a TV re-arm the monitor and the reply plays to
        the end.

        The accepted decision is left in ``self._barge_in_decision`` for the
        conversation loop, so the audio is not transcribed a second time.

        Returns:
            True if the user interrupted, False if TTS completed normally.
        """
        self._barge_in_decision = None
        interrupt_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_interrupt() -> None:
            """Called from capture thread when VAD detects sustained speech."""
            # asyncio.Event belongs to this loop, never the capture thread.
            try:
                loop.call_soon_threadsafe(interrupt_event.set)
            except RuntimeError:
                # A late capture callback may outlive the caller's event loop.
                logger.debug("Ignored TTS interrupt after event-loop shutdown")

        # Start VAD monitoring (also accumulates audio in buffer)
        capture.set_mode("vad_monitor", on_interrupt=_on_interrupt)

        tts_task: asyncio.Future | None = None
        try:
            tts_task = asyncio.ensure_future(self._do_speak(message))
            checks = 0
            ignored: dict[str, int] = {}
            while True:
                interrupt_task = asyncio.ensure_future(interrupt_event.wait())
                done, _pending = await asyncio.wait(
                    {tts_task, interrupt_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if tts_task in done:
                    interrupt_task.cancel()
                    try:
                        await interrupt_task
                    except asyncio.CancelledError:
                        pass
                    if ignored:
                        logger.info(
                            "Viola finished her reply; %d sound(s) heard while she spoke were not the user (%s)",
                            sum(ignored.values()),
                            ", ".join(f"{reason} x{count}" for reason, count in sorted(ignored.items())),
                        )
                    tts_task.result()
                    return False

                interrupt_event.clear()
                checks += 1
                decision = await self._check_barge_in(capture, message, tts_task)
                if decision.wake_at_end:
                    # "...Viola" at the very end of the window: a word of her
                    # own cut in half, or the user still talking. Look again.
                    checks += 1
                    decision = await self._check_barge_in(capture, message, tts_task)
                if decision.interrupts:
                    # Stop the TTS engine immediately so audio output ceases
                    # without waiting for the async task to cancel gracefully.
                    if self.tts and hasattr(self.tts, "stop"):
                        try:
                            self.tts.stop()
                        except Exception:
                            pass
                    tts_task.cancel()
                    try:
                        await tts_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001, RUF100 - TTS is being cut off
                        pass
                    decision = await self._complete_barge_in(capture, message, decision)
                    capture.set_mode("paused")
                    self._barge_in_decision = decision
                    await self._record_voice_event(
                        "tts_interrupted",
                        {
                            "source": "barge_in",
                            "barge_in": True,
                            "message_chars": len(message),
                            "reason": decision.reason,
                        },
                        user_id=user_id,
                    )
                    logger.info(
                        "TTS interrupted by the user (%s, check %d of this reply)",
                        decision.reason,
                        checks,
                    )
                    return True

                ignored[decision.reason] = ignored.get(decision.reason, 0) + 1
                if checks >= _BARGE_IN_MAX_CHECKS:
                    logger.info(
                        "Barge-in: %d checks this reply were all Viola's own voice or other sound; "
                        "not checking again until she finishes",
                        checks,
                    )
                    continue  # not re-armed: only tts_task can finish the wait now
                if not tts_task.done():
                    capture.rearm_interrupt()

        except asyncio.CancelledError:
            if tts_task is not None and not tts_task.done():
                tts_task.cancel()
            raise
        except Exception as exc:
            logger.warning("Interruptible TTS error: %s", exc)
            return False
        finally:
            # If still in vad_monitor mode (e.g., TTS completed normally),
            # switch to paused to stop VAD processing
            if capture.mode == "vad_monitor":
                capture.set_mode("paused")

    async def _check_barge_in(
        self,
        capture: Any,
        message: str,
        tts_task: asyncio.Future,
    ) -> BargeInDecision:
        """Transcribe the last few seconds heard while Viola speaks and classify them.

        Viola keeps talking during the check (a "Viola, ..." said just as she
        finishes is still heard).
        """
        try:
            await asyncio.wait_for(asyncio.shield(tts_task), timeout=_BARGE_IN_SETTLE_SEC)
        except TimeoutError:
            pass
        except (asyncio.CancelledError, Exception):  # noqa: BLE001, RUF100 - the speak loop reads tts_task itself
            pass
        self._barge_in_window_end = time.monotonic()
        transcript = await self._transcribe_recent(capture, _BARGE_IN_WINDOW_SEC)
        if transcript is None:
            return BargeInDecision("ignore", "check_failed")
        return classify_barge_in_transcript(transcript, message)

    async def _transcribe_recent(self, capture: Any, seconds: float) -> str | None:
        """Transcribe the last ``seconds`` the barge-in monitor heard; None if that failed."""
        try:
            import numpy as np

            pcm = capture.get_recent_recording(seconds)
            if not pcm or len(pcm) < 3200:  # < 100 ms
                return ""
            audio = np.frombuffer(pcm, dtype=np.int16).copy()
            transcript = await asyncio.wait_for(
                asyncio.to_thread(self.voice_pipeline.transcribe_audio, audio, False, False),
                timeout=_BARGE_IN_STT_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001, RUF100 - a failed check must never cut Viola off
            logger.debug("Barge-in transcription failed: %s", exc)
            return None
        return transcript or ""

    async def _complete_barge_in(
        self,
        capture: Any,
        message: str,
        decision: BargeInDecision,
    ) -> BargeInDecision:
        """Viola has stopped for the user: let them finish, then hear the whole request.

        The check window can end in the middle of the request ("Viola, play
        so-"). With Viola quiet the VAD is trustworthy again, so wait until it
        hears no speech, then transcribe from the start of the check window to
        now. If that somehow loses the wake word, the checked decision stands.
        """
        started = time.monotonic()
        while time.monotonic() - started < _BARGE_IN_USER_MAX_SEC:
            await asyncio.sleep(0.1)
            if (
                time.monotonic() - started >= _BARGE_IN_USER_DONE_SEC
                and capture.seconds_since_speech() >= _BARGE_IN_USER_DONE_SEC
            ):
                break
        window_end = getattr(self, "_barge_in_window_end", None) or started
        seconds = min(_BARGE_IN_WINDOW_SEC + (time.monotonic() - window_end), 30.0)
        transcript = await self._transcribe_recent(capture, seconds)
        if not transcript:
            return decision
        full = classify_barge_in_transcript(transcript, message)
        return full if full.interrupts else decision

    def _take_barge_in_decision(self) -> BargeInDecision | None:
        decision = getattr(self, "_barge_in_decision", None)
        self._barge_in_decision = None
        return decision

    def _transcript_is_own_reply(self, transcript: str) -> bool:
        """True (and logged) when ``transcript`` is Viola's last reply heard back."""
        last = self._last_spoken_reply
        if last is None:
            return False
        reply, spoken_at = last
        if time.monotonic() - spoken_at > _ECHO_WINDOW_SEC:
            return False
        if not _is_echo_of_reply(transcript, reply):
            return False
        logger.warning(
            "Ignored a recording of Viola's own reply (%d words matched the reply just spoken); "
            "not running it as a command",
            len(_echo_words(transcript)),
        )
        return True

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
