"""
Simple Backend Audio Output.

This module contains PortAudio/sounddevice audio output logic
extracted from the main SimpleBackend class to comply with code constraints.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from core.logging_config import get_logger
from music.backends.base import BackendProgress

logger = get_logger(__name__)


class SimpleBackendAudioOutput:
    """Handles PortAudio/sounddevice audio output for the simple backend."""

    def __init__(self, backend_instance, *, sounddevice_module: object | None):
        """
        Initialize audio output handler.

        Args:
            backend_instance: The SimpleBackend instance
            sounddevice_module: Optional injected sounddevice module (used for testing and optional dependencies)
        """
        self.backend = backend_instance
        self._sounddevice = sounddevice_module

    def playback_loop(self, stdout, *, generation: int) -> None:
        """
        Main playback loop that reads from FFmpeg stdout and plays via PortAudio.

        During pause the loop keeps draining FFmpeg's stdout to prevent pipe
        starvation (which would freeze FFmpeg and corrupt resume).  Drained
        audio is discarded and silence is written to the sounddevice stream
        so the audio session stays alive.  Position is only advanced while
        *not* paused.

        Args:
            stdout: FFmpeg stdout pipe
            generation: Playback generation counter. Used to guard against
                stale threads clobbering _is_playing for a newer session.
        """
        if self._sounddevice is None:
            logger.error("sounddevice not available - cannot start playback loop")
            return

        # Bound before the try so the finally can always ask whether this thread
        # got as far as opening a stream it now owns closing.
        stream: object | None = None

        try:
            import numpy as np

            # Bug #29 fix: grab ChunkStamper once for direct PCM injection.
            # This bypasses ProcTap (which can't capture sounddevice/PortAudio
            # output via per-process WASAPI loopback) and provides instant
            # audio to multi-room spokes for local file playback.
            _stamper = None
            _stamper_pack_fn = None
            _set_direct_injection = None
            try:
                from audio_core.streaming.pipeline_wiring import (
                    get_active_chunk_stamper,
                    set_direct_injection,
                )

                _stamper = get_active_chunk_stamper()
                if _stamper is not None:
                    _set_direct_injection = set_direct_injection
                    # Bug #29 Fix 9: block ProcTap → stamper path to prevent
                    # dual-source garbling (ProcTap silence + injected audio).
                    _set_direct_injection(True)
                    if getattr(_stamper, "_bit_depth", 16) == 24:
                        from audio_core.streaming.chunk_protocol import (
                            pack_float32_to_int24,
                        )

                        _stamper_pack_fn = pack_float32_to_int24
            except Exception:
                pass  # multi-room not active — no injection needed

            output_stream_factory = getattr(self._sounddevice, "OutputStream", None)
            if not callable(output_stream_factory):
                logger.error("sounddevice.OutputStream unavailable - cannot start playback loop")
                return

            # Read exactly 3840 bytes = 960 stereo int16 samples = 20ms at
            # 48kHz = one DI chunk.  Aligning the read size with the DI chunk
            # size ensures exactly one DI injection per loop iteration.  With
            # the muted branch paced at 50 iter/sec, this gives exactly
            # 50 DI chunks/sec.  The old default (4096) caused 4096/3840 =
            # 1.067 DI chunks per iteration → 53.3 DI/sec → 7% speed-up.
            read_chunk_size = 3840
            progress_interval = max(
                0.01,
                float(getattr(self.backend._config, "progress_interval_sec", 0.1) or 0.1),
            )
            last_progress_at = time.monotonic() - progress_interval

            stream = output_stream_factory(
                samplerate=float(self.backend._sample_rate),
                channels=int(self.backend._channels),
                dtype=np.int16,
                blocksize=1024,
            )
            self.backend._stream = stream

            start = getattr(stream, "start", None)
            if callable(start):
                start()

            logger.debug("Audio stream opened")

            # Pre-allocate a silence frame for pause/mute: 960 stereo samples
            # = 20ms at 48kHz = one stamper chunk.  Previously this was derived
            # from read_chunk_size (65536 / 4 = 16384 samples = 341ms), which
            # caused the muted-sounddevice write to block for 341ms per
            # iteration — starving the DI injection to ~3/sec instead of ~50/sec
            # (Bug #32 root cause: hub-local received 93% silence).
            silence_samples = 960  # 20ms at 48kHz — matches ChunkStamper tick
            if self.backend._channels > 1:
                silence_array = np.zeros((silence_samples, int(self.backend._channels)), dtype=np.int16)
            else:
                silence_array = np.zeros(silence_samples, dtype=np.int16)

            _di_count = 0
            _di_next_log = 250
            _di_start = time.monotonic()
            _di_buf = bytearray()
            # Stamper-aligned chunk sizes: inject exactly one 20ms chunk at a
            # time so the stamper capture buffer never falls below one chunk.
            _di_chunk_size = 5760  # 960 stereo samples × 3 bytes (int24)
            _di_chunk_size_16 = 3840  # 960 stereo samples × 2 bytes (int16)

            # #2754: track whether any audio has actually been read. Until the
            # first non-empty read, the backend must NOT report "now playing";
            # and an empty first read means FFmpeg produced zero audio (a
            # decode/open failure), which is an ERROR, not a clean end-of-track.
            first_read = True

            # The generation check is load-bearing, not belt-and-braces:
            # _start_stream() CLEARS the shared _stop_event, so a thread that
            # was told to stop but was still parked in a blocking write on a
            # wedged device would find the flag un-set when it finally returned
            # and carry on -- writing audio and advancing the NEW track's
            # position alongside the new thread. The generation is only ever
            # incremented, so a superseded thread exits on its next iteration.
            while not self.backend._stop_event.is_set() and self.backend._play_generation == generation:
                paused = self.backend._paused

                # Always read from FFmpeg stdout to prevent pipe starvation.
                # During pause this drains the pipe so FFmpeg never blocks.
                data = stdout.read(read_chunk_size)
                if not data:
                    if first_read:
                        # Zero audio ever produced: FFmpeg could not open/decode
                        # the source (missing, corrupt, unsupported codec) and
                        # exited immediately. This is NOT a clean end-of-track.
                        self._record_start_failure(generation)
                    else:
                        logger.debug("End of audio stream reached")
                    break

                if first_read:
                    first_read = False
                    # Audio genuinely started — only now is it honest to report
                    # playing. Guarded by generation so a stale thread from a
                    # superseded play/seek cannot resurrect _is_playing.
                    if self.backend._play_generation == generation:
                        self.backend._is_playing = True

                if paused:
                    # Discard the audio data and write silence to keep the
                    # sounddevice stream alive (avoids stop/start fragility).
                    write_fn = getattr(stream, "write", None)
                    if callable(write_fn):
                        write_fn(silence_array)
                    continue

                # Feed raw PCM into AudioTee for multi-room broadcast
                audio_tee = self.backend._audio_tee
                if audio_tee is not None:
                    try:
                        audio_tee.write(data)
                    except Exception:
                        logger.exception("AudioTee write failed; playback continues")

                # Bug #29: inject PCM directly into ChunkStamper for binary
                # WS broadcast to spokes (bypasses ProcTap entirely).
                # Bug #32: buffer and inject in aligned chunks matching
                # ChunkStamper's drain size (5760 bytes int24 / 3840 bytes
                # int16 = 960 stereo samples = 20ms).  Without alignment,
                # FFmpeg's variable pipe-read sizes cause the average buffer
                # level to fall below one chunk, producing ~25% silence.
                if _stamper is not None:
                    try:
                        if _stamper_pack_fn is not None:
                            # Lossless int16 → int24 conversion via bit-shift.
                            # Previous code: int16 → float32/32768 → int24 (lossy
                            # due to asymmetric division + double quantisation).
                            # New: shift int16 left by 8 bits to fill 24-bit range,
                            # then pack the low 3 bytes of each int32.
                            int16_samples = np.frombuffer(data, dtype=np.int16)
                            int32_shifted = int16_samples.astype(np.int32) << 8
                            raw = int32_shifted.view(np.uint8).reshape(-1, 4)
                            packed_24 = raw[:, :3].tobytes()
                            _di_buf.extend(packed_24)
                            while len(_di_buf) >= _di_chunk_size:
                                _stamper.on_capture_data(
                                    bytes(_di_buf[:_di_chunk_size]),
                                    48000,
                                    2,
                                    3,
                                )
                                del _di_buf[:_di_chunk_size]
                                _di_count += 1
                        else:
                            _di_buf.extend(data)
                            while len(_di_buf) >= _di_chunk_size_16:
                                _stamper.on_capture_data(
                                    bytes(_di_buf[:_di_chunk_size_16]),
                                    48000,
                                    2,
                                    2,
                                )
                                del _di_buf[:_di_chunk_size_16]
                                _di_count += 1
                        # DI injection rate diagnostic (log every 250th chunk)
                        if _di_count >= _di_next_log:
                            _di_elapsed = time.monotonic() - _di_start
                            logger.info(
                                "DI injection: count=%d rate=%.1f/sec read=%d elapsed=%.1fs",
                                _di_count,
                                _di_count / max(_di_elapsed, 0.001),
                                len(data),
                                _di_elapsed,
                            )
                            _di_next_log = _di_count + 250
                    except Exception:
                        logger.exception("ChunkStamper injection failed; playback continues")

                audio_array = np.frombuffer(data, dtype=np.int16)
                if self.backend._channels > 1:
                    remainder = int(audio_array.size) % int(self.backend._channels)
                    if remainder:
                        audio_array = audio_array[:-remainder]
                    audio_array = audio_array.reshape(-1, int(self.backend._channels))

                # Source-and-spoke architecture: sounddevice always writes
                # PCM.  (Pre-2026-04-12 hub-and-spoke had a "muted" branch
                # that stopped writes while a subprocess played delayed
                # audio; that subprocess no longer exists — the local
                # spoke plays through sounddevice like any other spoke.)
                write = getattr(stream, "write", None)
                if callable(write):
                    write(audio_array)

                if self.backend._play_generation != generation:
                    # Superseded while parked in the blocking write above. The
                    # samples just written belong to the previous track, so they
                    # must not advance the new track's position or emit its
                    # progress. Leave now rather than at the next loop check.
                    break

                frames_written = int(audio_array.shape[0])
                # _pos_lock, NOT _lock: _stop_locked() holds _lock while joining
                # this thread, so blocking on _lock here would stall that join
                # for its whole timeout (and does so on a healthy device, not
                # just a wedged one). See SimpleBackend.__init__ for the rule.
                with self.backend._pos_lock:
                    self.backend._position_frames += frames_written
                    position_ms = int((self.backend._position_frames / self.backend._sample_rate) * 1000)
                    duration_ms = (
                        int((self.backend._duration_frames / self.backend._sample_rate) * 1000)
                        if self.backend._duration_frames is not None
                        else None
                    )

                now = time.monotonic()
                if now - last_progress_at >= progress_interval:
                    last_progress_at = now
                    self.backend._emit_progress(BackendProgress(position_ms=position_ms, duration_ms=duration_ms))

            logger.debug("Playback loop finished")

        except Exception as e:
            logger.exception("Error in playback loop: %s", e)
        finally:
            # Only clear state if this is still the active generation.
            # A newer _start_stream() call increments _play_generation and
            # sets _is_playing = True; a stale thread must not clobber that.
            # NOTE: Do NOT acquire self.backend._lock here — _stop_locked()
            # holds the lock while joining this thread, so acquiring it here
            # would deadlock.  Instead, _stop_locked() increments
            # _play_generation to invalidate this generation check.
            is_current = self.backend._play_generation == generation
            # Bug #29 Fix 9: re-enable ProcTap → stamper path.
            # ONLY disable DI if this is the current generation (natural
            # track completion).  If a newer _start_stream() has already
            # incremented _play_generation, the new playback thread will
            # call set_direct_injection(True) — disabling DI here would
            # race with that and leave DI=False, causing spoke silence
            # after track changes.
            if _set_direct_injection is not None and is_current:
                try:
                    _set_direct_injection(False)
                    logger.info(
                        "DI disabled (natural track completion, gen=%d)",
                        generation,
                    )
                except Exception:
                    pass
            elif _set_direct_injection is not None:
                logger.info(
                    "DI disable SKIPPED (stale gen=%d, current=%d)",
                    generation,
                    self.backend._play_generation,
                )
            if is_current:
                self.backend._is_playing = False

            # This thread opened `stream`, so this thread closes it -- always,
            # current generation or not. Closing a PortAudio stream is
            # Pa_CloseStream, which frees the buffers a blocking write walks, so
            # it is only ever safe on the thread that could be inside the write.
            # _stop_locked() therefore never closes a stream whose playback
            # thread is still alive, and would otherwise leave this one open.
            # Identity, not generation, decides whether the backend's handle
            # still refers to our stream: a newer _start_stream() has already
            # published its own object and must not have it cleared here.
            if stream is not None:
                if self.backend._stream is stream:
                    self.backend._stream = None
                try:
                    # abort() before close(): stop() waits for pending buffers to
                    # drain, which is another place a removed device can hang, and
                    # close() discards them anyway.
                    abort = getattr(stream, "abort", None)
                    if callable(abort):
                        abort()
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                except Exception as exc:
                    logger.debug("Error aborting/closing audio stream: %s", exc)

    def _record_start_failure(self, generation: int) -> None:
        """Record that a stream produced zero audio — an honest start failure.

        #2754: an empty first read means FFmpeg could not open/decode the
        source and exited, as opposed to a clean end-of-track. Store an error
        on the backend (surfaced via ``health_check``) and leave ``_is_playing``
        False so the runtime never reports "now playing" for a stream that
        rendered nothing. Guarded by generation so a stale thread from a
        superseded play/seek cannot clobber a newer session's state.
        """
        backend = self.backend
        proc = getattr(backend, "_proc", None)
        returncode: object | None = None
        try:
            if proc is not None:
                poll = getattr(proc, "poll", None)
                returncode = poll() if callable(poll) else getattr(proc, "returncode", None)
        except Exception:  # noqa: BLE001, RUF100 - diagnostic probe on possibly-dead proc
            returncode = getattr(proc, "returncode", None)
        message = f"Playback produced no audio (source could not be decoded; ffmpeg returncode={returncode})"
        logger.error("SimpleBackend start failure: %s", message)
        if backend._play_generation == generation:
            backend._playback_error = message
            backend._is_playing = False

    def simulate_loop(self, *, generation: int) -> None:
        """Simulation loop for progress updates when sounddevice is unavailable.

        Args:
            generation: Playback generation counter. Used to guard against
                stale threads clobbering _is_playing for a newer session.
        """
        try:
            progress_interval = max(0.01, float(self.backend._config.progress_interval_sec))

            while not self.backend._simulate_stop.is_set():
                time.sleep(progress_interval)
                with self.backend._lock:
                    if not self.backend._is_playing:
                        break
                    if self.backend._paused:
                        continue
                    self.backend._position_frames += int(progress_interval * self.backend._sample_rate)
                    if (
                        self.backend._duration_frames is not None
                        and self.backend._position_frames >= self.backend._duration_frames
                    ):
                        self.backend._position_frames = self.backend._duration_frames
                        self.backend._simulate_stop.set()

                    position_ms = int((self.backend._position_frames / self.backend._sample_rate) * 1000)
                    duration_ms = (
                        int((self.backend._duration_frames / self.backend._sample_rate) * 1000)
                        if self.backend._duration_frames
                        else None
                    )

                # Emit progress
                self.backend._emit_progress(BackendProgress(position_ms=position_ms, duration_ms=duration_ms))

        except Exception as e:
            logger.exception("Error in simulation loop: %s", e)
        finally:
            # Only clear _is_playing if this is still the active generation.
            # NOTE: Do NOT acquire self.backend._lock here — see playback_loop.
            if self.backend._play_generation == generation:
                self.backend._is_playing = False

    def start_simulated_playback(self) -> None:
        """Start simulated playback for testing when sounddevice is unavailable."""
        try:
            logger.info("Starting simulated playback (sounddevice unavailable)")

            # Simulate playback duration
            simulated_duration = 30.0  # 30 seconds
            start_time = time.time()

            while not self.backend._simulate_stop.is_set() and (time.time() - start_time) < simulated_duration:
                time.sleep(0.1)

                # Update progress
                elapsed = time.time() - start_time
                self.backend._emit_progress(
                    BackendProgress(
                        position_ms=int(elapsed * 1000),
                        duration_ms=int(simulated_duration * 1000),
                    )
                )

            # Signal completion
            self.backend._is_playing = False

            logger.info("Simulated playback finished")

        except Exception as e:
            logger.exception("Error in simulated playback: %s", e)

    def set_volume_level(self, level: int) -> int:
        """
        Set volume level.

        Args:
            level: Volume level (0-100)

        Returns:
            Actual volume level set
        """
        try:
            # Clamp to valid range
            level = max(0, min(100, level))

            # Update volume (will be applied on next stream start)
            self.backend._volume = level

            logger.debug("Volume set to %s%%", level)
            return level

        except Exception as e:
            logger.exception("Error setting volume to %s: %s", level, e)
            return self.backend._volume

    def get_current_position_ms(self) -> int | None:
        """
        Get current playback position in milliseconds.

        Returns:
            Position in milliseconds or None if not playing
        """
        try:
            if not self.backend._is_playing or self.backend._stream_start_time is None:
                return None

            position_sec = time.time() - self.backend._stream_start_time
            return int(position_sec * 1000)

        except Exception as e:
            logger.debug("Error getting current position: %s", e)
            return None

    def get_current_duration_ms(self) -> int | None:
        """
        Get current track duration in milliseconds.

        Returns:
            Duration in milliseconds or None if unknown
        """
        # For streaming, we don't know the duration in advance
        # Could potentially probe the source, but that's complex
        return None

    def check_sounddevice_available(self) -> bool:
        """
        Check if sounddevice is available.

        Returns:
            True if sounddevice is available, False otherwise
        """
        sd_module = self._sounddevice
        if sd_module is None:
            return False
        has_output_stream = callable(getattr(sd_module, "OutputStream", None))
        has_query_devices = callable(getattr(sd_module, "query_devices", None))
        return has_output_stream and has_query_devices

    def get_audio_devices(self) -> list[dict[str, object]]:
        """
        Get available audio output devices.

        Returns:
            List of device info dictionaries
        """
        sd_module = self._sounddevice
        query_devices = getattr(sd_module, "query_devices", None) if sd_module is not None else None
        if not callable(query_devices):
            return []

        try:
            from audio_core.portaudio_guard import sounddevice_guard

            devices = []
            with sounddevice_guard():
                device_list_obj = query_devices()
            if not isinstance(device_list_obj, list):
                return []
            device_list: list[object] = device_list_obj

            for i, device in enumerate(device_list):
                # sounddevice returns dict-like objects with string keys
                if not isinstance(device, Mapping):
                    continue
                device_dict: dict[str, object] = {key: value for key, value in device.items() if isinstance(key, str)}

                max_channels_obj = device_dict.get("max_output_channels")
                max_channels = int(max_channels_obj) if isinstance(max_channels_obj, (int, float)) else 0
                if max_channels > 0:  # Output device
                    default_samplerate_obj = device_dict.get("default_samplerate")
                    default_samplerate = (
                        float(default_samplerate_obj) if isinstance(default_samplerate_obj, (int, float)) else 0.0
                    )
                    devices.append(
                        {
                            "id": i,
                            "name": str(device_dict.get("name", "Unknown")),
                            "channels": max_channels,
                            "default_sample_rate": default_samplerate,
                        }
                    )

            return devices

        except Exception as e:
            logger.debug("Error querying audio devices: %s", e)
            return []
