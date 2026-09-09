"""
music/backends/simple.py

FFmpeg-powered streaming backend that provides pause/resume/seek without
pre-downloading entire tracks. Uses PortAudio (via sounddevice) for playback.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any

from config import settings
from core.constants import (
    SAMPLE_RATE_48K,
    TIMEOUT_DEFAULT,
    TIMEOUT_EXTENDED,
    TIMEOUT_MEDIUM,
)
from core.logging_config import get_logger
from core.subprocess_utils import popen_silent, run_silent
from music.backends.base import BackendCapabilities, BackendProgress, BaseBackend
from music.exceptions import BackendError
from music.log_utils import log_kv
from music.player_config import BackendStreamingConfig
from utils.optional_imports import sounddevice as sd

from .simple_audio import SimpleBackendAudioOutput
from .simple_av import AVDecodeSession, av_available, probe_duration_seconds
from .simple_stream import SimpleBackendStreamManager


@dataclass
class _PlaybackContext:
    source: str
    start_position_sec: float = 0.0


class SimpleBackend(BaseBackend):
    """
    Streaming backend that pipes FFmpeg decoded PCM into PortAudio.
    """

    def __init__(
        self,
        logger=None,
        config: BackendStreamingConfig | None = None,
    ):
        super().__init__()
        self._logger = logger or get_logger(__name__)
        self._config = config or BackendStreamingConfig()

        self._ffmpeg_path = self._config.ffmpeg_path or shutil.which("ffmpeg")
        self._ffprobe_path = self._config.ffprobe_path or shutil.which("ffprobe")
        self._simulate_only = False
        self._use_av = self._resolve_decode_engine()
        if self._ffmpeg_path is None and not self._use_av:
            raise RuntimeError(
                "no audio decoder available: ffmpeg CLI not on PATH and PyAV (av) is "
                "not importable; install FFmpeg, install the 'av' package, or "
                "configure player_config.backend.ffmpeg_path"
            )
        log_kv(
            self._logger,
            "info",
            "decode_engine",
            engine="av" if self._use_av else "ffmpeg_cli",
            frozen=bool(getattr(sys, "frozen", False)),
        )
        if sd is None:  # pragma: no cover - dependency gating
            if getattr(settings, "test_mode", False):
                log_kv(
                    self._logger,
                    "warning",
                    "sounddevice_missing",
                    mode="simulation",
                )
                self._simulate_only = True
            else:
                raise RuntimeError(
                    "sounddevice/PortAudio not available; " "install sounddevice and PortAudio libraries"
                )

        self._lock = threading.RLock()
        # Dedicated lock for the playback position counter.
        #
        # The playback thread must NEVER take self._lock: _stop_locked() runs
        # with self._lock held (every caller -- play/stop/seek -- takes it
        # first) and joins the playback thread, so a playback thread blocking
        # on self._lock cannot exit and the join burns its whole timeout on a
        # perfectly healthy device. The position counter is the one piece of
        # shared state the playback thread mutates, so it gets its own lock.
        #
        # Lock order is self._lock -> self._pos_lock, never the reverse, and
        # this lock is never held across a blocking call.
        self._pos_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()  # Initially playing state

        self._play_thread: threading.Thread | None = None
        # Playback threads that were told to stop but were still inside a
        # blocking audio write when the join timed out. Each still owns (and
        # will close) its own stream, so they are tracked rather than presumed
        # gone. Pruned on every _stop_locked().
        self._wedged_play_threads: list[threading.Thread] = []
        self._stderr_thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stream: object | None = None

        self._volume = 50
        self._context: _PlaybackContext | None = None
        self._is_playing = False
        self._paused = False
        self._play_generation = 0
        # Honest-playback state (#2754): set when a stream fails to produce
        # any audio (missing/corrupt/undecodable source). Surfaced via
        # health_check() so the runtime never reports "now playing" for a
        # stream that yielded zero audio. Cleared at the start of each attempt.
        self._playback_error: str | None = None

        self._sample_rate = SAMPLE_RATE_48K
        self._channels = 2
        self._bytes_per_frame = self._channels * 2  # int16

        self._position_frames = 0
        self._duration_frames: int | None = None

        # Simulation fields
        self._simulate_thread: threading.Thread | None = None
        self._simulate_stop = threading.Event()
        self._simulate_duration_sec: float = 0.0
        self._expected_duration_sec: float = max(5.0, float(self._config.simulate_track_duration_sec or 45.0))

        # Multi-room streaming: optional AudioTee for PCM broadcast
        self._audio_tee: object | None = None

        # Initialize extracted managers
        self._stream_manager = SimpleBackendStreamManager(self)
        self._audio_output = SimpleBackendAudioOutput(self, sounddevice_module=sd)

    # ---------- BaseBackend API ----------

    def play(self, source: str) -> None:
        initial_progress = None
        with self._lock:
            log_kv(self._logger, "info", "stream_start", source=source)
            self._stop_locked()
            self._context = _PlaybackContext(source=source, start_position_sec=0.0)
            if self._simulate_only:
                self._start_simulated_playback()
            else:
                self._start_stream(source, offset=0.0)
            # Capture progress data inside lock, emit AFTER releasing to avoid
            # lock ordering deadlock (simple._lock → player._lock vs reverse).
            initial_progress = BackendProgress(
                position_ms=int((self._position_frames / self._sample_rate) * 1000),
                duration_ms=(
                    int((self._duration_frames / self._sample_rate) * 1000)
                    if self._duration_frames is not None
                    else None
                ),
            )
        # Emit initial progress outside the lock — the callback chain may
        # acquire player._lock, which would deadlock if simple._lock is held.
        if initial_progress is not None:
            self._emit_progress(initial_progress)

    def pause(self) -> None:
        with self._lock:
            if not self._is_playing or self._paused:
                return
            self._paused = True
            if self._simulate_only:
                return
            # LP-8 fix: Do NOT call stream.stop() or clear _resume_event.
            # The playback loop keeps draining FFmpeg stdout during pause
            # (preventing pipe starvation) and writes silence to the
            # sounddevice stream (keeping the audio session alive).
            # The loop checks _paused directly to decide whether to
            # discard audio data or write it to the output.
            log_kv(self._logger, "debug", "paused", note="drain-mode")

    def resume(self) -> None:
        with self._lock:
            if not self._is_playing or not self._paused:
                return
            self._paused = False
            if self._simulate_only:
                return
            # LP-8 fix: No stream.start() needed -- the sounddevice stream
            # never stopped.  Clearing _paused is sufficient; the playback
            # loop will switch from silence back to real audio on the next
            # iteration.
            log_kv(self._logger, "debug", "resumed", note="drain-mode")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def is_playing(self) -> bool:
        with self._lock:
            return self._is_playing and not self._paused

    def health_check(self) -> dict[str, Any]:
        """Report backend health so the runtime can catch a silent start failure.

        #2754: SimpleBackend previously had no health_check, so
        ``BackendLifecycleManager.evaluate_backend_health`` always reported
        healthy — even when a stream produced zero audio. Now, if the last
        stream failed to produce any audio (missing/corrupt/undecodable
        source) and nothing is playing, report an error so
        ``_handle_playback_start_timeout`` records an honest failure instead
        of accepting a fake "now playing".
        """
        with self._lock:
            if self._playback_error is not None and not self._is_playing:
                return {
                    "status": "error",
                    "reason": "playback_no_audio",
                    "message": self._playback_error,
                }
            return {"status": "ok"}

    def set_volume(self, level: int) -> int:
        """Set volume level (0-100)."""
        with self._lock:
            self._volume = max(0, min(100, int(level)))
        return self._volume

    def seek(self, position_seconds: float) -> None:
        seek_progress = None
        with self._lock:
            if self._context is None:
                return
            log_kv(
                self._logger,
                "info",
                "stream_seek",
                position_seconds=round(float(position_seconds), 3),
            )
            self._stop_locked()
            self._context.start_position_sec = max(0.0, float(position_seconds))
            if self._simulate_only:
                self._start_simulated_playback()
            else:
                self._start_stream(self._context.source, offset=self._context.start_position_sec)
            seek_progress = BackendProgress(
                position_ms=int((self._position_frames / self._sample_rate) * 1000),
                duration_ms=(
                    int((self._duration_frames / self._sample_rate) * 1000)
                    if self._duration_frames is not None
                    else None
                ),
            )
        if seek_progress is not None:
            self._emit_progress(seek_progress)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=True,
            pause=True,
            resume=True,
            seek=True,
            volume=True,
            position=True,
            duration=True,
        )

    def current_position_ms(self) -> int | None:
        """Get current playback position in milliseconds."""
        with self._lock:
            if not self._is_playing:
                return None
            return int((self._position_frames / self._sample_rate) * 1000)

    def current_duration_ms(self) -> int | None:
        """Get current track duration in milliseconds."""
        with self._lock:
            if self._duration_frames is None:
                return None
            return int((self._duration_frames / self._sample_rate) * 1000)

    # ---------- multi-room streaming ----------

    def set_audio_tee(self, tee: object | None) -> None:
        """
        Attach or detach an AudioTee for multi-room PCM broadcast.

        When set, the playback loop feeds raw PCM data into the tee
        after each read from FFmpeg stdout, enabling HubAudioBroadcaster
        to timestamp and broadcast chunks to spoke devices.

        Args:
            tee: AudioTee instance (must have a ``write(bytes)`` method),
                 or None to detach.
        """
        self._audio_tee = tee
        if tee is not None:
            self._logger.info("AudioTee attached to SimpleBackend for multi-room streaming")
        else:
            self._logger.info("AudioTee detached from SimpleBackend")

    # ---------- gapless playback (stub implementation) ----------

    def preload_next_track(self, source: str) -> bool:
        """
        Preload next track for gapless playback.

        SimpleBackend uses FFmpeg piped to PortAudio. For preloading,
        we would need to start a second FFmpeg process which consumes
        too many resources. Return False to indicate preloading is
        not supported for this backend.
        """
        self._logger.debug("SimpleBackend does not support preloading (FFmpeg-based)")
        return False

    def has_preloaded_track(self) -> bool:
        """SimpleBackend doesn't support preloading."""
        return False

    def play_preloaded(self) -> bool:
        """SimpleBackend doesn't support preloading."""
        return False

    def cancel_preload(self) -> None:
        """No-op for SimpleBackend."""
        pass

    # ---------- internal helpers ----------

    def _resolve_decode_engine(self) -> bool:
        """
        True → decode in-process via PyAV; False → shell out to the ffmpeg CLI.

        Resolution order (requal-M1: clean machines have no ffmpeg.exe, but the
        frozen bundle already ships PyAV + ffmpeg's shared libraries):
        - An explicit operator-configured ``ffmpeg_path`` always wins (CLI).
        - Frozen builds prefer the packaged PyAV decoder — the installer ships
          av + av.libs and must never depend on whatever ffmpeg.exe a user's
          PATH happens to contain.
        - Dev/non-frozen keeps the historical CLI-first behavior, with PyAV as
          the fallback when ffmpeg is not on PATH.
        """
        if self._config.ffmpeg_path:
            return False
        if getattr(sys, "frozen", False) and av_available():
            return True
        if self._ffmpeg_path is None and av_available():
            return True
        return False

    @staticmethod
    def _is_local_file_source(source: str) -> bool:
        """True when ``source`` is a filesystem path rather than a network URL.

        A URL scheme (``http://``, ``https://``, ``file://`` ...) means the
        source is not a plain local path we can stat. A Windows drive path
        (``C:/Music/x.mp3``) has ``:/`` but no ``://`` and is treated as local.
        """
        if not source:
            return False
        return re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", source) is None

    def _start_stream(self, source: str, *, offset: float) -> None:
        """Start stream using FFmpeg (CLI or in-process PyAV) and PortAudio output."""
        # #2754: honesty gate. A local file that does not exist (deleted or
        # moved after the library was scanned, or a stale index entry) can
        # never produce audio. Refuse to launch FFmpeg and fake a play-started
        # signal — raise so the executor records a real playback error and
        # never reports "now playing" for a track it cannot play. Skipped for
        # network URLs (reachability is FFmpeg's job, not a local stat).
        self._playback_error = None
        if self._is_local_file_source(source) and not os.path.exists(source):
            raise BackendError(f"Audio file not found: {source}")

        # Auto-discover AudioTee from the streaming pipeline if not set explicitly
        if self._audio_tee is None:
            try:
                from audio_core.streaming.pipeline_wiring import get_active_audio_tee

                tee = get_active_audio_tee()
                if tee is not None:
                    self._audio_tee = tee
                    self._logger.info("AudioTee auto-discovered from streaming pipeline")
            except Exception:
                pass  # Multi-room streaming not available; silent

        self._stop_event.clear()
        self._resume_event.set()
        with self._pos_lock:
            self._position_frames = int(offset * self._sample_rate)
        self._duration_frames = self._probe_duration_frames(source)

        if self._use_av:
            log_kv(
                self._logger,
                "debug",
                "av_decode_launch",
                source=source,
                offset_seconds=round(offset, 3),
            )
            proc = AVDecodeSession(
                source,
                offset=offset,
                sample_rate=self._sample_rate,
                channels=self._channels,
                logger=self._logger,
            )
        else:
            cmd = [
                self._ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
            ]
            if offset > 0:
                cmd.extend(["-ss", f"{offset:.3f}"])
            cmd.extend(
                [
                    "-i",
                    source,
                    "-ac",
                    str(self._channels),
                    "-ar",
                    str(self._sample_rate),
                    "-f",
                    "s16le",
                    "pipe:1",
                ]
            )

            log_kv(
                self._logger,
                "debug",
                "ffmpeg_launch",
                command=" ".join(cmd),
                offset_seconds=round(offset, 3),
            )
            proc = popen_silent(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        self._proc = proc
        self._play_generation += 1
        # #2754: do NOT report playing yet. The old code set _is_playing = True
        # here, before a single audio byte was read, so is_playing() returned
        # True synchronously even when FFmpeg immediately error-exited on a
        # corrupt/undecodable file (zero audio). The playback loop now flips
        # _is_playing to True only after the first successful non-empty read —
        # i.e. only once audio genuinely started.
        self._is_playing = False
        self._paused = False

        # DI track-change fix: re-enable Direct Injection synchronously
        # BEFORE starting the playback thread.  _stop_locked() disables DI
        # to clean up; we must re-enable it here so there is no window
        # where DI=False while a local file is about to play.  The
        # playback thread also enables DI (idempotent), but relying solely
        # on the thread created a race where the stamper saw DI=False for
        # the entire track if the thread's try/except silently failed or
        # was delayed.
        try:
            from audio_core.streaming.pipeline_wiring import (
                get_active_chunk_stamper,
                set_direct_injection,
            )

            stamper = get_active_chunk_stamper()
            if stamper is not None:
                set_direct_injection(True)
                self._logger.info(
                    "DI re-enabled in _start_stream (gen=%d, stamper=%s)",
                    self._play_generation,
                    type(stamper).__name__,
                )
            else:
                self._logger.warning(
                    "DI NOT re-enabled in _start_stream: stamper is None (gen=%d)",
                    self._play_generation,
                )
        except Exception as exc:
            self._logger.warning(
                "DI re-enable FAILED in _start_stream: %s (gen=%d)",
                exc,
                self._play_generation,
            )

        generation = self._play_generation

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(proc.stderr,),
            daemon=True,
            name="StreamingBackendStderr",
        )
        self._stderr_thread.start()

        self._play_thread = threading.Thread(
            target=self._playback_loop,
            args=(proc.stdout, generation),
            name="StreamingBackend",
            daemon=True,
        )
        self._play_thread.start()
        # NOTE: Initial progress emission moved to play() — emitting here
        # while simple._lock is held causes AB-BA deadlock with player._lock.

    def _start_simulated_playback(self) -> None:
        """Start simulated playback in a background thread with progress updates."""
        # Don't call start_simulated_playback() - it blocks and prevents the simulation thread from starting!
        # Instead, initialize state and start the simulation loop thread which emits progress
        self._simulate_stop.clear()
        self._play_generation += 1
        self._is_playing = True
        self._paused = False
        start_seconds = self._context.start_position_sec if self._context else 0.0
        self._position_frames = int(start_seconds * self._sample_rate)
        target_duration = max(5.0, self._expected_duration_sec)
        if start_seconds >= target_duration:
            target_duration = start_seconds + 5.0
        self._simulate_duration_sec = target_duration
        self._duration_frames = int(self._simulate_duration_sec * self._sample_rate)
        generation = self._play_generation
        self._simulate_thread = threading.Thread(
            target=self._simulate_loop,
            args=(generation,),
            name="StreamingBackendSim",
            daemon=True,
        )
        self._simulate_thread.start()

    def _playback_loop(self, stdout, generation: int) -> None:
        """Playback loop using the audio output handler."""
        try:
            self._audio_output.playback_loop(stdout, generation=generation)
        except Exception:
            self._logger.exception("StreamingBackend playback thread crashed")

    def _simulate_loop(self, generation: int) -> None:
        """Simulation loop using the audio output handler."""
        try:
            self._audio_output.simulate_loop(generation=generation)
        except Exception:
            self._logger.exception("StreamingBackend simulation thread crashed")

    def _drain_stderr(self, pipe) -> None:
        """
        Drain stderr from FFmpeg process in a separate thread.

        This function reads stderr lines until EOF or stop_event is set.
        When _stop_locked() closes the pipe, readline() will return EOF (b"")
        and the loop will exit gracefully.
        """
        if not pipe:
            return
        try:
            # Read lines until EOF (pipe closed) or stop_event is set
            # When pipe is closed by _stop_locked(), readline() returns b"" (EOF)
            for line in iter(pipe.readline, b""):
                # Check stop_event periodically - if set, exit immediately
                if self._stop_event.is_set():
                    break
                try:
                    log_kv(
                        self._logger,
                        "debug",
                        "ffmpeg_stderr",
                        line=line.decode(errors="ignore").strip(),
                    )
                except Exception as e:  # Silent OK: decode errors in stderr are expected during shutdown
                    self._logger.exception("FFmpeg stderr decode failed: %s", e)
                    pass
        except (
            OSError,
            ValueError,
            BrokenPipeError,
        ):  # Silent OK: pipe closure expected during shutdown
            pass
        except Exception as e:  # pragma: no cover - defensive
            # Any other exception - log and exit
            self._logger.exception("Unexpected error in stderr drain: %s", e)
            return

    def _stop_locked(self) -> None:
        if self._simulate_only:
            self._simulate_stop_locked()
            return

        # Increment generation FIRST so any orphaned play thread's finally
        # block will see a stale generation and skip clobbering _is_playing.
        self._play_generation += 1

        self._stop_event.set()
        self._resume_event.set()

        # Terminate process first to signal EOF to readers
        if self._proc is not None:
            try:
                # Close pipes to signal EOF to reader threads
                if self._proc.stdout:
                    try:
                        self._proc.stdout.close()
                    except Exception as e:  # Silent OK: pipe already closed during shutdown
                        self._logger.exception("Failed to close stdout pipe: %s", e)
                        pass
                if self._proc.stderr:
                    try:
                        self._proc.stderr.close()
                    except Exception as e:  # Silent OK: pipe already closed during shutdown
                        self._logger.exception("Failed to close stderr pipe: %s", e)
                        pass
                self._proc.terminate()
                self._proc.wait(timeout=self._config.shutdown_timeout_sec)
            except Exception as e:  # Silent OK: process may already be dead
                self._logger.exception("Failed to terminate FFmpeg process: %s", e)
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=TIMEOUT_MEDIUM)
                except Exception as e2:  # Silent OK: process cleanup failure during shutdown
                    self._logger.exception("Failed to kill FFmpeg process: %s", e2)
                    pass
            finally:
                self._proc = None

        # Join stderr thread before play thread to ensure cleanup order
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=TIMEOUT_DEFAULT)
            self._stderr_thread = None

        play_thread_wedged = False
        if self._play_thread is not None:
            play_thread = self._play_thread
            self._play_thread = None
            play_thread.join(timeout=TIMEOUT_DEFAULT)
            play_thread_wedged = play_thread.is_alive()
            if play_thread_wedged:
                # The thread is parked in a blocking PortAudio write on a device
                # that stopped consuming samples. Record it rather than assume it
                # exited: it still owns its stream (see the stream handling
                # below) and it still counts as playing.
                self._wedged_play_threads = [t for t in self._wedged_play_threads if t.is_alive()]
                self._wedged_play_threads.append(play_thread)
                self._logger.warning(
                    "Playback thread did not exit within %.1fs -- it is still inside "
                    "a blocking audio write (output device removed or wedged). Its "
                    "stream is left for it to close; teardown continues without it.",
                    TIMEOUT_DEFAULT,
                )

        # Explicitly disable DI after the play thread is joined.
        # The play thread's finally block skips set_direct_injection(False)
        # when the generation is stale (incremented above).  This ensures
        # DI is disabled when stop() is called directly (no new play()
        # follows).  When play() calls _stop_locked() followed by
        # _start_stream(), DI is re-enabled synchronously in
        # _start_stream() before the new thread starts.
        try:
            from audio_core.streaming.pipeline_wiring import (
                _direct_injection_active,
                set_direct_injection,
            )

            if _direct_injection_active:
                set_direct_injection(False)
                self._logger.info("DI disabled by _stop_locked (gen=%d)", self._play_generation)
        except ImportError:
            pass  # multi-room not active

        if self._stream is not None:
            stream = self._stream
            self._stream = None
            if play_thread_wedged:
                # Closing is Pa_CloseStream, which frees the buffers the wedged
                # playback thread is still walking inside Pa_WriteStream -- a
                # native access violation with no Python traceback (the crash
                # class in audio_core.portaudio_guard). The stream is detached
                # here but NOT closed: its playback thread closes it in its own
                # finally block when the device call returns.
                self._logger.warning(
                    "Audio stream detached without closing: its playback thread is "
                    "still inside a blocking write. That thread owns the close."
                )
            else:
                try:
                    # abort() rather than stop(): stop() waits for pending buffers
                    # to drain, which hangs on a device that stopped consuming.
                    abort = getattr(stream, "abort", None)
                    if callable(abort):
                        abort()
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                except Exception as e:  # Silent OK: audio stream cleanup during shutdown
                    self._logger.exception("Failed to abort/close audio stream: %s", e)
                    pass

        self._is_playing = False
        self._paused = False

    def _probe_duration_frames(self, source: str) -> int | None:
        # In-process probe first when decoding via PyAV (or when ffprobe is
        # absent — clean machines have neither CLI binary).
        if self._use_av or self._ffprobe_path is None:
            duration_sec = probe_duration_seconds(source)
            if duration_sec is not None:
                return int(duration_sec * self._sample_rate)

        if self._ffprobe_path is None:
            return None

        try:
            cmd = [
                self._ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                source,
            ]
            log_kv(self._logger, "debug", "ffprobe_probe_duration", source=source)
            result = run_silent(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=TIMEOUT_EXTENDED,
            )
            if result.returncode != 0:
                return None
            duration_sec = float(result.stdout.strip())
            return int(duration_sec * self._sample_rate)
        except Exception as exc:
            self._logger.debug("SimpleBackend: ffprobe duration query failed: %r", exc)
            return None

    def _simulate_stop_locked(self) -> None:
        # Increment generation so orphaned simulate thread won't clobber state.
        self._play_generation += 1
        self._simulate_stop.set()
        if self._simulate_thread is not None:
            self._simulate_thread.join(timeout=TIMEOUT_DEFAULT)
            self._simulate_thread = None
        self._is_playing = False
        self._paused = False

    # ---------- extended helpers ----------

    def update_expected_duration(self, duration: int | float | None) -> None:
        """
        Hint the backend about upcoming track length. Used in simulation mode
        to avoid unrealistically long placeholder playback.
        """
        with self._lock:
            if duration is None:
                self._expected_duration_sec = max(5.0, float(self._config.simulate_track_duration_sec or 45.0))
                return

            if isinstance(duration, int):
                seconds = float(duration) / 1000.0
            else:
                seconds = float(duration)

            if seconds <= 0:
                self._expected_duration_sec = max(5.0, float(self._config.simulate_track_duration_sec or 45.0))
                return

            self._expected_duration_sec = max(5.0, seconds)
