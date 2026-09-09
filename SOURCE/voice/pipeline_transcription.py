"""
Voice pipeline transcription utilities.

Extracted from voice/pipeline.py to reduce method complexity.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from core.constants import TIMEOUT_SHORT
from core.logging_config import get_logger

logger = get_logger(__name__)


class VoiceTranscriptionManager:
    """
    Manages voice transcription operations.

    Handles audio transcription, latency tracking, and voice command processing.
    """

    def __init__(self, pipeline_instance: Any):
        self.pipeline = pipeline_instance

    async def transcribe_audio(
        self,
        audio_source: str | Path | np.ndarray,
    ) -> str | None:
        """
        Transcribe audio to text with timeout and cancellation support.

        Accepts a file path or a numpy int16 buffer (mono, 16kHz).
        Buffer mode skips disk I/O for lower latency.
        """
        import numpy as np

        if self.pipeline._stt_cancelled:
            logger.debug("STT cancelled, skipping transcription")
            return None

        is_buffer = isinstance(audio_source, np.ndarray)
        if not is_buffer:
            audio_source = Path(audio_source)
            if not audio_source.exists():
                logger.warning("Audio file does not exist: %s", audio_source)
                return None

        start_time = time.perf_counter()

        try:
            # Check if we have an active STT task to cancel
            if self.pipeline._active_stt_task and not self.pipeline._active_stt_task.done():
                logger.debug("Cancelling previous STT task")
                self.pipeline._stt_cancellation_event.set()
                try:
                    await asyncio.wait_for(self.pipeline._active_stt_task, timeout=TIMEOUT_SHORT)
                except TimeoutError:
                    logger.debug("Previous STT task did not cancel promptly")
                except Exception as exc:
                    logger.debug("Previous STT task cancellation error: %s", exc)

            # Clear cancellation for new transcription
            self.pipeline._stt_cancellation_event.clear()
            self.pipeline._stt_cancelled = False

            # Create new STT task
            self.pipeline._active_stt_task = asyncio.create_task(self._perform_transcription(audio_source))

            # Wait for transcription with timeout
            try:
                result = await asyncio.wait_for(
                    self.pipeline._active_stt_task,
                    timeout=self.pipeline._stt_latency_alert,
                )
                return result
            except TimeoutError:
                logger.warning(
                    "STT transcription timed out after %ss",
                    self.pipeline._stt_latency_alert,
                )
                self.pipeline._stt_cancelled = True
                return None

        except Exception as exc:
            duration = time.perf_counter() - start_time
            logger.warning("STT transcription failed after %.2fs: %s", duration, exc)
            return None
        finally:
            # Record latency metrics
            duration = time.perf_counter() - start_time
            self._record_stt_latency(duration)

    async def _perform_transcription(
        self,
        audio_source: Path | np.ndarray,
    ) -> str | None:
        """Perform the actual transcription operation.

        Args:
            audio_source: File path or int16 numpy array.
        """
        import numpy as np

        try:
            # Check for cancellation
            if self.pipeline._stt_cancellation_event.is_set():
                logger.debug("STT cancelled during transcription")
                return None

            is_buffer = isinstance(audio_source, np.ndarray)
            if is_buffer:
                logger.info(
                    "[STT] Starting transcription from buffer (%d samples)",
                    len(audio_source),
                )
            else:
                logger.info("[STT] Starting transcription from file input")

            # Perform transcription - use asyncio.to_thread for sync transcriber
            # WhisperTranscriber.transcribe() is synchronous, so we run it in a thread
            transcriber = self.pipeline.transcriber
            if transcriber is None:
                logger.error("[STT] Transcriber is None - cannot transcribe")
                return None

            # Determine the input to pass to the transcriber
            transcribe_input = audio_source if is_buffer else str(audio_source)

            # Check for transcribe_async first (future-proofing), fallback to sync transcribe
            if hasattr(transcriber, "transcribe_async") and callable(transcriber.transcribe_async):
                logger.debug("[STT] Using async transcribe method")
                result = await transcriber.transcribe_async(transcribe_input)
            elif hasattr(transcriber, "transcribe") and callable(transcriber.transcribe):
                logger.debug("[STT] Using sync transcribe via to_thread")
                result = await asyncio.to_thread(transcriber.transcribe, transcribe_input)
            else:
                logger.error("[STT] Transcriber has no transcribe method!")
                return None

            # Check again for cancellation after transcription
            if self.pipeline._stt_cancellation_event.is_set():
                logger.debug("STT result discarded due to cancellation")
                return None

            if result and result.strip():
                clean_result = result.strip()
                logger.info("[STT] Transcription complete length=%d", len(clean_result))
                return clean_result

            logger.debug("STT returned empty result")
            return None

        except Exception as exc:
            logger.warning("Transcription error: %s", exc)
            raise

    def _record_stt_latency(self, duration: float) -> None:
        """
        Record STT latency metrics and emit warnings if needed.

        This implements the latency recording logic that was previously
        in VoicePipeline._record_stt_latency.
        """
        # Record in samples
        self.pipeline._stt_latency_samples.append(duration)
        self.pipeline._last_transcription_latency = duration

        # Calculate jitter if we have previous samples
        if len(self.pipeline._stt_latency_samples) >= 2:
            prev_samples = list(self.pipeline._stt_latency_samples)[-10:]  # Last 10 samples
            if len(prev_samples) >= 2:
                mean_latency = sum(prev_samples) / len(prev_samples)
                jitter = abs(duration - mean_latency)
                self.pipeline._stt_jitter_samples.append(jitter)

        # Emit warnings for slow transcription
        if duration >= self.pipeline._stt_latency_alert:
            logger.warning(
                "STT latency alert: %.2fs >= %ss",
                duration,
                self.pipeline._stt_latency_alert,
            )
        elif duration >= self.pipeline._stt_latency_warn:
            logger.warning(
                "STT latency warning: %.2fs >= %ss",
                duration,
                self.pipeline._stt_latency_warn,
            )

        # Emit debug event if available
        if emit_debug_event := getattr(self.pipeline, "_emit_debug_event", None):
            try:
                emit_debug_event(
                    "stt_latency",
                    {
                        "duration_seconds": duration,
                        "latency_warn_threshold": self.pipeline._stt_latency_warn,
                        "latency_alert_threshold": self.pipeline._stt_latency_alert,
                        "samples_count": len(self.pipeline._stt_latency_samples),
                    },
                )
            except Exception as exc:
                logger.debug("STT latency debug event failed: %s", exc)

    async def handle_voice_command(self, text: str) -> bool:
        """
        Handle a voice command by routing it through the intent system.

        This implements the command handling logic that was previously
        in VoicePipeline.handle_voice_command.
        """
        if not text or not text.strip():
            logger.debug("Empty voice command, ignoring")
            return False

        logger.info("Processing voice command length=%d", len(text.strip()))

        # Cancel any active command
        if self.pipeline._active_command_task and not self.pipeline._active_command_task.done():
            logger.debug("Cancelling active command")
            self.pipeline._cancellation_event.set()
            try:
                await asyncio.wait_for(self.pipeline._active_command_task, timeout=TIMEOUT_SHORT)
            except TimeoutError:
                logger.debug("Active command did not cancel promptly")
            except Exception as exc:
                logger.debug("Active command cancellation error: %s", exc)

        # Clear cancellation for new command
        self.pipeline._cancellation_event.clear()

        # Create and track new command task
        self.pipeline._active_command_task = asyncio.create_task(self._process_voice_command(text))

        # Track active commands
        self.pipeline._active_commands += 1
        self.pipeline._max_backlog = max(self.pipeline._max_backlog, self.pipeline._active_commands)

        try:
            await self.pipeline._active_command_task
            return True
        except Exception as exc:
            logger.error("Voice command processing failed: %s", exc)
            return False
        finally:
            self.pipeline._active_commands -= 1

    async def _process_voice_command(self, text: str) -> None:
        """Process the actual voice command."""
        try:
            # Voice command routing is handled by the voice orchestrator.
            # This method is a placeholder for direct pipeline command handling.
            # The actual routing goes through core.voice_orchestrator -> intent.pipeline
            logger.debug("Voice command received for processing length=%d", len(text.strip()))

            # Note: IntentPipeline requires dependencies to be properly initialized
            # In production, commands flow through voice_orchestrator which has these deps
            logger.info("Processing voice command via intent pipeline length=%d", len(text.strip()))

        except Exception as exc:
            logger.error("Voice command routing failed: %s", exc)
            # Don't re-raise - command failures shouldn't crash the pipeline
