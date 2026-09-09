# voice/synthesis/engine.py

from __future__ import annotations

"""
Thread-Safe TTS Engine (pyttsx3 fallback)

Implements a queue-based TTS engine that handles pyttsx3's single-threaded
event loop limitations. All TTS operations are serialized through a dedicated
worker thread to prevent "run loop already started" errors.

Root Cause of Original Bug:
- pyttsx3.runAndWait() starts an internal event loop
- Multiple threads calling runAndWait() concurrently causes conflict
- Even with a lock, the internal state can become corrupted

Fix:
- Dedicated TTS worker thread owns the pyttsx3 engine
- All speak requests are queued to this thread
- Engine recreation happens entirely within the worker thread
"""
import asyncio
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from config.settings import settings
from core.constants import TIMEOUT_MEDIUM, TIMEOUT_SHUTDOWN
from core.logging_config import get_logger
from core.quiet_hours import tts_volume_for_now

logger = get_logger(__name__)


@dataclass
class TTSRequest:
    """Request to speak text."""

    text: str
    completion_event: threading.Event
    success: bool = False


class TTSWorker(threading.Thread):
    """
    Dedicated worker thread for TTS operations.

    This thread owns the pyttsx3 engine and processes all speak requests
    sequentially. This prevents the "run loop already started" error by
    ensuring only one thread ever interacts with the engine.
    """

    def __init__(self, config: Any):
        super().__init__(daemon=True, name="TTS-Worker")
        self.config = config
        self._request_queue: queue.Queue[TTSRequest | None] = queue.Queue()
        self._running = False
        self._engine: Any = None
        self._engine_failure_count = 0
        self._max_engine_failures = 3  # Max consecutive failures before disabling

    def run(self) -> None:
        """Worker thread main loop."""
        self._running = True
        self._init_engine()

        logger.info("TTS worker thread started")

        while self._running:
            try:
                # Wait for a request with timeout (allows clean shutdown)
                try:
                    request = self._request_queue.get(timeout=TIMEOUT_MEDIUM)
                except queue.Empty:
                    continue

                if request is None:  # Shutdown signal
                    break

                self._process_request(request)

            except Exception as e:
                logger.exception("TTS worker loop error: %s", e)

        self._cleanup_engine()
        logger.info("TTS worker thread stopped")

    def _init_engine(self) -> None:
        """Initialize the pyttsx3 engine within the worker thread."""
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            self._set_female_voice()
            # Use config values if available, otherwise sensible defaults
            rate = getattr(self.config, "tts_rate", 155)
            volume_pct = getattr(self.config, "tts_volume", 100)
            self._engine.setProperty("rate", rate)
            self._engine.setProperty("volume", tts_volume_for_now(volume_pct))
            self._engine_failure_count = 0
            logger.info("TTS engine initialized in worker thread")
        except Exception as e:
            logger.error("Failed to initialize TTS engine: %s", e)
            self._engine = None

    def _set_female_voice(self) -> None:
        """Select a female voice if available."""
        if self._engine is None:
            return

        try:
            voices = self._engine.getProperty("voices")
            if not voices:
                return

            female_keywords = [
                "zira",
                "hazel",
                "hannah",
                "female",
                "woman",
                "susan",
                "linda",
            ]

            for voice in voices:
                voice_id_lower = str(voice.id).lower()
                voice_name_lower = str(getattr(voice, "name", "")).lower()

                for keyword in female_keywords:
                    if keyword in voice_id_lower or keyword in voice_name_lower:
                        self._engine.setProperty("voice", voice.id)
                        logger.info("TTS voice set to: %s", getattr(voice, "name", voice.id))
                        return

            # Fallback: try second voice (often female)
            if len(voices) > 1:
                self._engine.setProperty("voice", voices[1].id)

        except Exception as e:
            logger.warning("Could not set female voice: %s", e)

    def _process_request(self, request: TTSRequest) -> None:
        """Process a single TTS request."""
        try:
            if self._engine is None:
                if self._engine_failure_count < self._max_engine_failures:
                    logger.warning(
                        "TTS engine is None, attempting re-init (attempt %s/%s)",
                        self._engine_failure_count + 1,
                        self._max_engine_failures,
                    )
                    self._init_engine()
                if self._engine is None:
                    logger.warning("TTS engine not available after re-init, skipping speech")
                    return

            if self._engine_failure_count >= self._max_engine_failures:
                logger.critical(
                    "TTS engine disabled due to %s repeated failures",
                    self._max_engine_failures,
                )
                return

            self._speak_internal(request.text)
            self._engine_failure_count = 0  # Reset on success
            request.success = True

        except Exception as e:
            self._engine_failure_count += 1
            logger.error(
                "TTS speak failed (failure %s/%s): %s",
                self._engine_failure_count,
                self._max_engine_failures,
                e,
            )

            if "run loop already started" in str(e):
                # This shouldn't happen with our queue-based approach
                # but if it does, recreate the engine
                logger.warning("Unexpected 'run loop already started' - recreating engine")
                self._recreate_engine()
            elif self._engine_failure_count >= self._max_engine_failures:
                logger.error("TTS engine disabled after too many failures")
                self._engine = None

        finally:
            # Always signal completion, even on failure
            request.completion_event.set()

    def _speak_internal(self, text: str) -> None:
        """Actually speak the text. ONLY called from worker thread."""
        if self._engine is None:
            return

        volume_pct = getattr(self.config, "tts_volume", 100)
        self._engine.setProperty("volume", tts_volume_for_now(volume_pct))
        self._engine.say(text)
        self._engine.runAndWait()

    def _recreate_engine(self) -> None:
        """Recreate the engine after a failure."""
        try:
            if self._engine is not None:
                try:
                    self._engine.stop()
                except Exception as e:
                    logger.debug("Engine stop failed during recreation: %s", e)
                self._engine = None

            # Brief delay before recreation
            time.sleep(0.2)
            self._init_engine()

        except Exception as e:
            logger.error("Failed to recreate TTS engine: %s", e)
            self._engine = None

    def _cleanup_engine(self) -> None:
        """Clean up engine resources."""
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception as e:
                logger.debug("Engine cleanup failed: %s", e)
            self._engine = None

    def queue_speak(self, text: str, timeout: float = 30.0) -> bool:
        """
        Queue a speak request to the worker thread.

        Args:
            text: Text to speak
            timeout: Maximum time to wait for completion

        Returns:
            True if speech completed, False if timed out or failed
        """
        if not self._running:
            logger.warning("TTS worker not running")
            return False

        completion_event = threading.Event()
        request = TTSRequest(text=text, completion_event=completion_event)

        self._request_queue.put(request)
        completed = completion_event.wait(timeout=timeout)
        return completed and request.success

    def stop(self) -> None:
        """Stop the worker thread."""
        self._running = False
        self._request_queue.put(None)  # Send shutdown signal


class TTSEngine:
    """
    Thread-safe TTS Engine using a dedicated worker thread.

    All speak operations are serialized through a worker thread that
    owns the pyttsx3 engine, preventing "run loop already started" errors.
    """

    def __init__(self, config=settings):
        self.config = config
        self._worker: TTSWorker | None = None

        # In test/pytest mode, avoid initializing system TTS engine
        if config.test_mode or settings.pytest_in_progress:
            logger.debug("TTSEngine disabled in test mode")
            return

        # Start the worker thread
        self._worker = TTSWorker(config)
        self._worker.start()
        logger.info("TTSEngine initialized with worker thread")

    async def say(self, text: str, **kwargs: object) -> None:
        """Alias for speak() to satisfy IntentTTSPort protocol."""
        await self.speak(text)

    async def speak(self, text: str) -> None:
        """
        Speak text using the worker thread.

        This method is thread-safe and can be called from any context.
        The actual speech is processed by the dedicated TTS worker thread.

        Args:
            text: Text to speak
        """
        from voice.synthesis.text_normalizer import normalize_for_speech

        text = normalize_for_speech(text)

        # Honour the tts_enabled config flag
        if not getattr(self.config, "tts_enabled", True):
            return

        # Test mode check
        if self.config.test_mode or settings.pytest_in_progress or self._worker is None:
            logger.info("TTS test mode text_length=%d", len(text))
            return

        logger.info("TTS text_length=%d", len(text))

        # Update TTS state for diagnostics tracking
        try:
            from diagnostics.wake_state_sync import get_state_sync_monitor

            monitor = get_state_sync_monitor()
            monitor.update_tts_state(is_speaking=True)
        except Exception as e:
            logger.debug("TTS state tracking unavailable: %s", e)
            monitor = None

        # Duck audio during speech for better clarity
        from contextlib import AbstractContextManager, nullcontext

        duck_ctx: AbstractContextManager[object]
        try:
            from utils.audio_ducking import duck_context

            duck_ctx = duck_context()
        except Exception as e:
            logger.debug("Audio ducking unavailable, continuing without ducking: %s", e)
            duck_ctx = nullcontext()

        try:
            with duck_ctx:
                # Queue the speech to the worker thread (non-blocking from async perspective)
                try:
                    # Compute timeout based on text length so that long
                    # paragraphs don't hit the hard 30 s wall.
                    # Empirical: pyttsx3 speaks ~15 chars/s → budget 0.12s/char
                    # plus a 10 s fixed head-start; floor at 30 s.
                    _tts_timeout = max(30.0, len(text) * 0.12 + 10.0)
                    # Run the blocking queue_speak in a thread to not block the event loop
                    success = await asyncio.to_thread(self._worker.queue_speak, text, _tts_timeout)
                    if not success:
                        logger.warning(
                            "TTS speak timed out or failed (text_len=%d, timeout=%.1f)", len(text), _tts_timeout
                        )
                except Exception as e:
                    logger.error("TTS speak failed: %s", e)
        finally:
            # Update TTS state when speech ends
            if monitor is not None:
                try:
                    monitor.update_tts_state(is_speaking=False)
                except Exception as e:
                    logger.debug("TTS state tracking update failed: %s", e)

    def speak_sync(self, text: str, timeout: float = 30.0) -> bool:
        """
        Synchronous speak method for non-async contexts.

        Args:
            text: Text to speak
            timeout: Maximum time to wait for speech completion

        Returns:
            True if speech completed, False if failed or timed out
        """
        if self._worker is None:
            logger.warning("TTS worker not available")
            return False

        # Update TTS state for diagnostics tracking
        monitor = None
        try:
            from diagnostics.wake_state_sync import get_state_sync_monitor

            monitor = get_state_sync_monitor()
            monitor.update_tts_state(is_speaking=True)
        except Exception as e:
            logger.debug("TTS state tracking unavailable: %s", e)

        try:
            return self._worker.queue_speak(text, timeout)
        finally:
            # Update TTS state when speech ends
            if monitor is not None:
                try:
                    monitor.update_tts_state(is_speaking=False)
                except Exception as e:
                    logger.debug("TTS state tracking update failed: %s", e)

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to PCM bytes.

        pyttsx3 does not support byte-level synthesis; it can only play
        audio through the system speakers.  This method returns empty bytes.
        Use KokoroTTSEngine for byte-level synthesis.
        """
        logger.debug(
            "TTSEngine.synthesize — pyttsx3 cannot produce bytes (text length: %s)",
            len(text),
        )
        return b""

    def is_available(self) -> bool:
        """Check if TTS is available and functional."""
        return self._worker is not None and self._worker.is_alive()

    def stop(self) -> None:
        """Stop the TTS engine and worker thread."""
        if self._worker is not None:
            self._worker.stop()
            self._worker.join(timeout=TIMEOUT_SHUTDOWN)
            self._worker = None
            logger.info("TTS engine stopped")
