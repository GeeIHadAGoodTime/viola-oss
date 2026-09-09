"""
Dictation controller -- orchestrates the live dictation session.

Coordinates the streaming STT engine, text injector, command parser,
wake detector pause/resume, and audio capture into a single cohesive
dictation mode that the user can activate and deactivate by voice.
"""

from __future__ import annotations

import asyncio
import threading
from enum import Enum, auto
from typing import TYPE_CHECKING

from audio_core.portaudio_guard import open_portaudio, open_stream, terminate_portaudio
from core.constants import AUDIO_CHANNELS_MONO, AUDIO_CHUNK_SIZE, SAMPLE_RATE_16K
from core.events.types import BaseEvent
from core.logging_config import get_logger
from voice.dictation.command_parser import (
    DictationAction,
    parse_dictation,
    reset_parser_state,
    set_capitalize_next,
)

if TYPE_CHECKING:
    from core.events.bus import EventBus
    from voice.dictation.streaming_stt import StreamingSTTEngine
    from voice.dictation.text_injector import TextInjectorPort

logger = get_logger(__name__)


# ======================================================================== #
# Dictation events                                                         #
# ======================================================================== #

# These are lightweight BaseEvent subclasses so the rest of the application
# can observe dictation lifecycle transitions without coupling to the
# dictation module directly.

from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True, slots=True)
class DictationStartedEvent(BaseEvent):
    """Emitted when dictation mode is activated."""

    source: str = "voice.dictation"


@_dataclass(frozen=True, slots=True)
class DictationStoppedEvent(BaseEvent):
    """Emitted when dictation mode is deactivated."""

    source: str = "voice.dictation"


@_dataclass(frozen=True, slots=True)
class DictationTextEvent(BaseEvent):
    """Emitted when dictated text was accepted by the OS input queue.

    The event asserts exactly that much and no more. Keystroke injection
    has no channel back from the application receiving them, so this is
    never a claim that the text appeared on screen. It IS a claim the OS
    took the keystrokes -- and when it did not (the focused window outranks
    Viola, or input is blocked) no event is published at all, rather than
    telling the user "I typed this" on the strength of having asked.
    """

    text: str = ""
    source: str = "voice.dictation"


# ======================================================================== #
# State enum                                                               #
# ======================================================================== #


class DictationState(Enum):
    """Lifecycle states of the dictation controller."""

    IDLE = auto()
    ACTIVE = auto()
    PAUSED = auto()


# ======================================================================== #
# Controller                                                               #
# ======================================================================== #


class DictationController:
    """Orchestrator for a live dictation session.

    The controller owns the lifecycle of audio capture, streaming STT, and
    text injection.  It pauses the wake detector while active so that
    spoken dictation text is not mistaken for wake word triggers.

    Args:
        stt_engine: A :class:`StreamingSTTEngine` instance (not yet started).
        text_injector: A :class:`TextInjectorPort` implementation for typing text.
        event_bus: Optional :class:`EventBus` for publishing dictation events.
    """

    def __init__(
        self,
        stt_engine: StreamingSTTEngine,
        text_injector: TextInjectorPort,
        event_bus: EventBus | None = None,
    ) -> None:
        self._stt = stt_engine
        self._injector = text_injector
        self._event_bus = event_bus

        self._state = DictationState.IDLE
        self._state_lock = threading.Lock()

        # Undo buffer: last injected text for "scratch that"
        self._last_text: str = ""
        self._capitalize_next: bool = True

        # Audio capture thread management
        self._audio_thread: threading.Thread | None = None
        self._audio_stop_event = threading.Event()
        self._pyaudio_instance: object | None = None
        self._audio_stream: object | None = None

    # -- properties -------------------------------------------------------- #

    @property
    def is_active(self) -> bool:
        """Return ``True`` if dictation is in ACTIVE state."""
        with self._state_lock:
            return self._state == DictationState.ACTIVE

    @property
    def state(self) -> DictationState:
        """Return the current dictation state."""
        with self._state_lock:
            return self._state

    # -- public lifecycle -------------------------------------------------- #

    async def start(self) -> str:
        """Enter dictation mode.

        1. Pauses the wake detector so speech is not mistaken for a wake word.
        2. Resets the command parser auto-capitalisation state.
        3. Starts the streaming STT engine.
        4. Launches the audio capture thread.
        5. Publishes a ``DictationStartedEvent``.

        Returns:
            Human-readable confirmation string.

        Raises:
            RuntimeError: If dictation is already active.
        """
        with self._state_lock:
            if self._state != DictationState.IDLE:
                raise RuntimeError("Cannot start dictation: current state is %s" % self._state.name)
            self._state = DictationState.ACTIVE

        logger.info("Dictation session starting")

        # Pause wake detector
        self._pause_wake_detector()

        # Reset parser state for fresh session
        reset_parser_state()
        self._capitalize_next = True
        self._last_text = ""

        # Wire callbacks into the STT engine
        self._stt._on_partial = self._handle_partial  # type: ignore[attr-defined]
        self._stt._on_final = self._handle_transcription  # type: ignore[attr-defined]
        # For Deepgram the attribute names differ
        if hasattr(self._stt, "_on_interim"):
            self._stt._on_interim = self._handle_partial  # type: ignore[attr-defined]

        # Start the STT engine
        self._stt.start()

        # Start audio capture
        self._audio_stop_event.clear()
        self._audio_thread = threading.Thread(
            target=self._audio_feed_loop,
            daemon=True,
            name="dictation-audio-feed",
        )
        self._audio_thread.start()

        # Publish event
        self._publish(DictationStartedEvent())

        logger.info("Dictation session started")
        return "Dictation mode is now active. Start speaking."

    async def stop(self) -> str:
        """Exit dictation mode.

        1. Stops audio capture.
        2. Stops the STT engine.
        3. Resumes the wake detector.
        4. Publishes a ``DictationStoppedEvent``.

        Returns:
            Human-readable confirmation string.
        """
        with self._state_lock:
            if self._state == DictationState.IDLE:
                return "Dictation is not active."
            self._state = DictationState.IDLE

        logger.info("Dictation session stopping")

        # Stop audio capture
        self._audio_stop_event.set()
        if self._audio_thread is not None and self._audio_thread.is_alive():
            self._audio_thread.join(timeout=3.0)
        self._audio_thread = None
        self._close_audio_stream()

        # Stop STT
        self._stt.stop()

        # Resume wake detector
        self._resume_wake_detector()

        # Publish event
        self._publish(DictationStoppedEvent())

        logger.info("Dictation session stopped")
        return "Dictation mode stopped."

    async def pause(self) -> str:
        """Temporarily pause dictation without tearing down the session.

        Returns:
            Confirmation string.
        """
        with self._state_lock:
            if self._state != DictationState.ACTIVE:
                return "Dictation is not active."
            self._state = DictationState.PAUSED

        logger.info("Dictation session paused")
        return "Dictation paused."

    async def resume(self) -> str:
        """Resume a previously paused dictation session.

        Returns:
            Confirmation string.
        """
        with self._state_lock:
            if self._state != DictationState.PAUSED:
                return "Dictation is not paused."
            self._state = DictationState.ACTIVE

        logger.info("Dictation session resumed")
        return "Dictation resumed."

    # -- transcription handling -------------------------------------------- #

    def _handle_partial(self, text: str) -> None:
        """Handle interim/partial transcription (informational only).

        Partial results are logged but not injected -- only final results
        drive text injection to avoid flickering/doubling.

        Args:
            text: Interim transcription text.
        """
        logger.debug("Dictation partial length=%d", len(text))

    def _handle_transcription(self, text: str) -> None:
        """Handle a finalised transcription from the STT engine.

        The text is parsed into a :class:`DictationCommand` and the
        appropriate action is executed (type text, insert punctuation,
        undo, stop, etc.).

        Args:
            text: Final transcription text.
        """
        with self._state_lock:
            if self._state != DictationState.ACTIVE:
                return

        if not text or not text.strip():
            return

        command = parse_dictation(text)
        logger.debug(
            "Dictation command: action=%s text_length=%d",
            command.action.name,
            len(command.text),
        )

        try:
            if command.action == DictationAction.TEXT:
                self._inject_text(command.text)

            elif command.action == DictationAction.PUNCTUATION:
                self._inject_punctuation(command.text)

            elif command.action == DictationAction.NEW_LINE:
                if self._accepted(self._injector.press_key("return"), "new line"):
                    self._last_text = "\n"

            elif command.action == DictationAction.NEW_PARAGRAPH:
                accepted = self._accepted(self._injector.press_key("return"), "new paragraph")
                accepted = self._accepted(self._injector.press_key("return"), "new paragraph") and accepted
                if accepted:
                    self._last_text = "\n\n"

            elif command.action == DictationAction.UNDO:
                self._undo_last()

            elif command.action == DictationAction.STOP:
                # Stop dictation asynchronously
                asyncio.get_event_loop().create_task(self.stop())

            elif command.action == DictationAction.SELECT_ALL:
                self._accepted(self._injector.combo_key("ctrl", "a"), "select all")

            elif command.action == DictationAction.COPY:
                self._accepted(self._injector.combo_key("ctrl", "c"), "copy")

            elif command.action == DictationAction.PASTE:
                self._accepted(self._injector.combo_key("ctrl", "v"), "paste")

            elif command.action == DictationAction.CAPITALIZE:
                set_capitalize_next(True)

            elif command.action == DictationAction.TAB:
                self._accepted(self._injector.press_key("tab"), "tab")

        except Exception:
            logger.exception("Failed to execute dictation action %s", command.action.name)

    def _accepted(self, outcome: object, what: str) -> bool:
        """Whether the OS took the keystrokes, logging the reason when it did not.

        An injector that reports nothing (an older or third-party
        implementation of the port) leaves us with no evidence either way;
        that is treated as accepted so behaviour is unchanged for it, and
        the real Win32/pynput implementations both report.
        """
        submitted = getattr(outcome, "submitted", None)
        if submitted is None or submitted:
            return True
        reason = outcome.reason() if hasattr(outcome, "reason") else "the OS refused the keystrokes"
        logger.warning("Dictation injection refused for %s: %s", what, reason)
        return False

    def _inject_text(self, text: str) -> None:
        """Type text at the cursor and update the undo buffer.

        Args:
            text: Text to inject (already formatted by the parser).
        """
        if not self._accepted(self._injector.type_text(text), "text"):
            # Two things must NOT happen when the OS refused the keystrokes.
            # Publishing the event would tell the UI "I typed this" about
            # text that does not exist. Recording it as _last_text would be
            # worse: "undo" sends len(_last_text) backspaces, so an undo
            # after a refused injection would delete that many characters
            # of the user's OWN text.
            return
        self._last_text = text
        self._publish(DictationTextEvent(text=text))

    def _inject_punctuation(self, punct: str) -> None:
        """Inject a punctuation character, removing any trailing space first.

        Many STT engines and the parser add trailing spaces after words.
        When punctuation follows, we backspace over the trailing space to
        avoid ``"word ."`` instead of ``"word."``.

        Args:
            punct: The punctuation string to inject.
        """
        # Remove trailing space before punctuation (if last injection ended with one)
        if self._last_text.endswith(" "):
            # A refused backspace leaves the space in place, so the result is
            # "word ." instead of "word.". Cosmetic rather than a false claim,
            # but it belongs in the log next to everything else the OS refused.
            self._accepted(self._injector.backspace(1), "trailing-space backspace")

        if not self._accepted(self._injector.type_text(punct), "punctuation"):
            # Same reasoning as _inject_text: an undo buffer holding
            # characters that were never typed turns "undo" into a
            # backspace over the user's own words.
            return
        self._last_text = punct

        # Add a space after punctuation for natural flow (except open brackets/quotes)
        if punct not in ("(", "\u201c", '"', "'"):
            if self._accepted(self._injector.type_text(" "), "trailing space"):
                self._last_text = punct + " "

    def _undo_last(self) -> None:
        """Delete the last injected text by sending backspace presses."""
        if not self._last_text:
            logger.debug("Undo: nothing to undo")
            return
        count = len(self._last_text)
        if not self._accepted(self._injector.backspace(count), "undo"):
            # The backspaces never reached the OS, so the text is still on
            # screen. Clearing _last_text here would tell the next undo there
            # is nothing left to remove and strand it.
            logger.warning("Undo: backspaces were refused; the text is still there")
            return
        logger.debug("Undo: removed %d characters", count)
        self._last_text = ""

    # -- audio capture ----------------------------------------------------- #

    def _audio_feed_loop(self) -> None:
        """Capture microphone audio and feed it to the STT engine.

        Runs in a dedicated daemon thread.  Uses PyAudio to open a 16 kHz
        mono int16 input stream and reads 1024-sample frames, forwarding
        each to :meth:`StreamingSTTEngine.feed_audio`.
        """
        import numpy as np

        try:
            import pyaudio
        except ImportError:
            logger.error("PyAudio is required for dictation audio capture")
            return

        pa: object | None = None
        stream: object | None = None

        try:
            pa = open_portaudio()
            self._pyaudio_instance = pa

            stream = open_stream(
                pa,
                format=pyaudio.paInt16,
                channels=AUDIO_CHANNELS_MONO,
                rate=SAMPLE_RATE_16K,
                input=True,
                frames_per_buffer=AUDIO_CHUNK_SIZE,
            )
            self._audio_stream = stream
            logger.info(
                "Dictation audio capture started (rate=%d, channels=%d, buffer=%d)",
                SAMPLE_RATE_16K,
                AUDIO_CHANNELS_MONO,
                AUDIO_CHUNK_SIZE,
            )

            # VP-2/VP-7: Noise suppression for cleaner STT input
            _noise_gate = None
            try:
                from audio_core.noise_gate import get_noise_gate

                _noise_gate = get_noise_gate()
                if _noise_gate.enabled:
                    logger.debug("Dictation noise suppression active")
            except Exception:
                logger.debug("Noise suppression unavailable for dictation")

            while not self._audio_stop_event.is_set():
                # Check pause state
                with self._state_lock:
                    if self._state == DictationState.PAUSED:
                        self._audio_stop_event.wait(timeout=0.1)
                        continue

                try:
                    raw = stream.read(AUDIO_CHUNK_SIZE, exception_on_overflow=False)
                    chunk = np.frombuffer(raw, dtype=np.int16)
                    # Apply noise suppression before STT if available
                    if _noise_gate is not None:
                        chunk = _noise_gate.process(chunk, SAMPLE_RATE_16K)
                    self._stt.feed_audio(chunk)
                except OSError:
                    logger.warning("Audio read error in dictation capture", exc_info=True)
                    break
                except Exception:
                    logger.debug("Dictation audio feed error", exc_info=True)
                    break

        except Exception:
            logger.exception("Failed to start dictation audio capture")
        finally:
            self._close_audio_stream()
            logger.info("Dictation audio capture stopped")

    def _close_audio_stream(self) -> None:
        """Safely close the PyAudio stream and terminate the instance."""
        stream = self._audio_stream
        self._audio_stream = None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                logger.debug("Error closing audio stream", exc_info=True)

        pa = self._pyaudio_instance
        self._pyaudio_instance = None
        if pa is not None:
            try:
                terminate_portaudio(pa)
            except Exception:
                logger.debug("Error terminating PyAudio", exc_info=True)

    # -- wake detector integration ----------------------------------------- #

    def _pause_wake_detector(self) -> None:
        """Pause the wake detector to prevent false triggers during dictation."""
        try:
            from voice.wake_detector.facade import WakeDetectorFacade

            instance = WakeDetectorFacade.get_instance()
            if instance is not None:
                instance.pause()
                logger.debug("Wake detector paused for dictation")
            else:
                logger.debug("No wake detector instance to pause")
        except Exception:
            logger.debug("Could not pause wake detector", exc_info=True)

    def _resume_wake_detector(self) -> None:
        """Resume the wake detector after dictation ends."""
        try:
            from voice.wake_detector.facade import WakeDetectorFacade

            instance = WakeDetectorFacade.get_instance()
            if instance is not None:
                instance.resume()
                logger.debug("Wake detector resumed after dictation")
            else:
                logger.debug("No wake detector instance to resume")
        except Exception:
            logger.debug("Could not resume wake detector", exc_info=True)

    # -- event publishing -------------------------------------------------- #

    def _publish(self, event: BaseEvent) -> None:
        """Publish an event on the event bus if one is configured.

        Args:
            event: The event to publish.
        """
        if self._event_bus is not None:
            try:
                self._event_bus.publish(event)
            except Exception:
                logger.debug("Failed to publish dictation event", exc_info=True)


__all__ = [
    "DictationController",
    "DictationStartedEvent",
    "DictationState",
    "DictationStoppedEvent",
    "DictationTextEvent",
]
