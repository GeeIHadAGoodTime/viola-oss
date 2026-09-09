"""
ProcTap Capture Provider — per-process audio loopback on Windows.

Captures audio from a specific child process (e.g. YouTube renderer)
rather than the entire system audio mix.  This isolates Viola's music
from other applications and avoids capturing system notification sounds,
Teams calls, etc.

Architecture:
    ProcTap COM capture runs in a **child process** to isolate it from
    the Viola process's COM environment (pycaw, comtypes, Qt STA, etc.).
    In-process ProcTap activation consistently yields near-zero audio
    even though standalone processes using the same code capture real
    audio from the same PID (see docs/PROCTAP_FIX_LOG.md).

    The subprocess sends raw 44100 Hz PCM via a pipe.  A daemon thread
    in the provider reads the pipe, resamples to 48000 Hz, and delivers
    20ms chunks to the callback.
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
import threading
import time
from collections.abc import Callable

import numpy as np
import soxr

from core.logging_config import get_logger

from .base import AudioCaptureProvider

logger = get_logger(__name__)

# Output format matching WASAPI provider and ChunkStamper expectations
_OUTPUT_RATE = 48000
_OUTPUT_CHANNELS = 2
_OUTPUT_FRAMES_PER_CHUNK = 960

# ProcTap native sample rate (GetMixFormat returns E_NOTIMPL)
_PROCTAP_RATE = 44100
_PROCTAP_CHANNELS = 2

# PID polling interval (how often to check for audio child)
_PID_POLL_INTERVAL_SEC = 5.0

# Auto-reconnect: if subprocess reports no meaningful audio for this long,
# tear down and restart the subprocess.
_SILENCE_RECONNECT_SEC = 8.0
_SILENCE_RECONNECT_SEC_FAST = 3.0  # used after a rescan interrupt
_MAX_RECONNECT_ATTEMPTS = 3

# Post-audio silence: how often to re-scan for a different audio PID
# when the current PID goes silent after having produced audio.
# Handles provider transitions (YouTube -> Spotify CDP, etc.).
_POST_AUDIO_RESCAN_SEC = 3.0

# Hard timeout: give up on a silent PID entirely after this long.
_POST_AUDIO_HARD_TIMEOUT_SEC = 15.0


def find_audio_child_pid() -> int | None:
    """Find a child process of the current process that has an active audio session.

    Delegates to source_audio_controller.find_audio_child_pid() which is the
    canonical implementation shared by both ProcTap and Source mute logic.

    Returns:
        PID of the child with an active audio session, or None.
    """
    try:
        from .source_audio_controller import find_audio_child_pid as _find_pid

        return _find_pid()
    except ImportError:
        logger.debug("source_audio_controller not available for PID discovery")
        return None
    except Exception:
        logger.exception("ProcTap: unexpected error in PID discovery")
        return None


class ProcTapProvider(AudioCaptureProvider):
    """Audio capture provider using Windows Process Audio Tap.

    Captures audio from a specific child process via ProcTap in a
    subprocess, resamples from 44100 to 48000 Hz, and delivers 20ms
    chunks matching the ChunkStamper format.
    """

    def __init__(self) -> None:
        self._callback: Callable[[bytes, int, int, int], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._target_pid: int | None = None

        # PID notification from external components (e.g. mute thread)
        self._notified_pid: int | None = None
        self._last_notified_pid: int | None = None
        self._pid_notify_event = threading.Event()
        # Set by _wait_for_audio_pid; read by _capture_from_pid to decide timeout
        self._last_pid_was_notified: bool = False

        # Rescan interrupt: breaks _capture_from_pid() out of its retry loop
        # immediately so ProcTap can re-discover the correct PID.  Set by
        # request_rescan() when a new play command fires.
        self._rescan_event = threading.Event()

        # Subprocess capture handle
        self._subprocess = None

        # Runtime capture format (set after subprocess starts)
        self._capture_sw: int = 2  # sample width in bytes (2=int16, 4=float32)
        self._capture_sr: int = _PROCTAP_RATE  # actual capture sample rate
        self._use_system_loopback = False
        self._active_capture_mode: str | None = None

        # Metrics
        self._chunks_produced = 0
        self._last_rms: float = 0.0
        self._lock = threading.Lock()

        # Boundary continuity tracking (Task 3)
        self._prev_last_L: float | None = None
        self._prev_last_R: float | None = None
        self._boundary_discontinuities: int = 0
        self._resample_stream = None
        self._resample_stream_key: tuple[int, int] | None = None
        self._resample_output_pending = np.empty((0, _OUTPUT_CHANNELS), dtype=np.float32)

        # NaN detection counter (Task 4)
        self._nan_chunks: int = 0

    def set_callback(self, fn: Callable[[bytes, int, int, int], None]) -> None:
        self._callback = fn

    def notify_pid(self, pid: int) -> None:
        """Accept an externally-discovered audio child PID.

        Called by the mute thread (or any other component) when it discovers
        the YouTube renderer PID.  This wakes up ``_wait_for_audio_pid``
        immediately so ProcTap can begin capturing without waiting for its
        own next polling cycle.  Also checked inside ``_capture_from_pid``
        to allow mid-capture PID switches (Bug #29 Fix 2).
        """
        with self._lock:
            self._notified_pid = pid
            self._last_notified_pid = pid
        self._use_system_loopback = False
        logger.info("ProcTap: notify_pid(%d) — clearing system loopback fallback", pid)
        self._pid_notify_event.set()
        logger.debug("ProcTap: notified of audio child PID %d", pid)

    def request_rescan(self) -> None:
        """Wake ProcTap from any blocking state on a new play event.

        Called when a new play command fires.  Two effects:
        1. Wakes ``_wait_for_audio_pid`` poll sleep (via _pid_notify_event).
        2. Interrupts ``_capture_from_pid`` retry loop (via _rescan_event)
           so ProcTap abandons a stale PID immediately instead of burning
           3 × 8 s = 24 s of retries on a dead process.

        If the capture thread has died (e.g. unhandled exception in
        ``_capture_loop``), restarts it entirely via :meth:`start`.
        """
        if not (self._thread and self._thread.is_alive()):
            logger.warning("ProcTap: capture thread dead, restarting via start()")
            self.start()
        self._rescan_event.set()
        self._pid_notify_event.set()
        logger.debug("ProcTap: rescan requested (play event)")

    def _resolve_rescan_pid(self, current_pid: int) -> int:
        """Determine the target PID for a rescan event.

        Checks (in order):
        1. ``_notified_pid`` — explicitly set by ``notify_pid()``
        2. pycaw discovery — ``find_audio_child_pid()``
        3. Fall back to ``current_pid`` (the existing capture target)

        Used by rescan checks to decide whether to keep the current
        subprocess (same PID) or abandon and re-discover (different PID).

        Pre-2026-05-07: returned ``None`` when neither notify nor discovery
        produced a PID, and the caller treated ``None`` as "abandon current
        PID". This caused the iPhone-cut-out bug at 15:35:32 — a rescan
        fired during Spotify track-change while the audio renderer briefly
        had no detectable session, find_audio_child_pid() returned None,
        and ProcTap abandoned a perfectly working Chrome PID 57252 that
        was still rendering audio. The current PID is then never
        re-acquired and ProcTap captures silence indefinitely.

        Defensive new behavior: if discovery fails, fall back to
        ``current_pid``. The caller's ``rescan_pid == pid`` branch now
        treats "no new info" as "keep current". If the current PID is
        actually dead, the next capture attempt will fail naturally and
        the inner restart loop will re-discover then.
        """
        with self._lock:
            notified = self._notified_pid
        if notified is not None:
            return notified

        # No explicit notification — do a quick pycaw discovery.
        # This handles YouTube stop→play where the renderer PID
        # stays the same but no notify_pid() was called.
        discovered = find_audio_child_pid()
        if discovered is not None:
            return discovered

        # Discovery returned nothing — keep the current PID. Don't
        # abandon a known-good capture target on a transient discovery
        # miss (Chrome briefly between renderer sessions during track
        # change, audio service restart, etc.).
        return current_pid

    def start(self) -> None:
        if self._running:
            return

        self._stop_event.clear()
        self._pid_notify_event.clear()
        self._rescan_event.clear()
        self._notified_pid = None
        self._use_system_loopback = False
        self._reset_resampler_state()
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            name="proctap-capture",
            daemon=True,
        )
        self._thread.start()
        logger.info("ProcTapProvider started (subprocess mode)")

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        self._pid_notify_event.set()  # Unblock _wait_for_audio_pid
        self._rescan_event.set()  # Unblock _capture_from_pid

        # Stop subprocess
        if self._subprocess is not None:
            self._subprocess.stop()
            with self._lock:
                self._subprocess = None
                self._active_capture_mode = None

        if self._thread is not None:
            self._thread.join(timeout=8.0)
            self._thread = None
        self._running = False
        with self._lock:
            self._active_capture_mode = None
        self._reset_resampler_state()
        logger.info(
            "ProcTapProvider stopped: chunks_produced=%d",
            self._chunks_produced,
        )

    @classmethod
    def is_available(cls) -> bool:
        """ProcTap is only available on Windows 10+."""
        if sys.platform != "win32":
            return False
        try:
            # Verify mmdevapi DLL is accessible
            ctypes.windll.mmdevapi  # noqa: B018
            return True
        except (OSError, AttributeError):
            return False

    def get_metrics(self) -> dict:
        with self._lock:
            sub = self._subprocess
            return {
                "provider": "proctap",
                "mode": "subprocess",
                "capture_mode": self._active_capture_mode
                or (
                    "system-loopback"
                    if self._target_pid == os.getpid() and self._use_system_loopback
                    else "per-process"
                ),
                "pid": self._target_pid,
                "last_notified_pid": self._last_notified_pid,
                "rms": self._last_rms,
                "chunks_produced": self._chunks_produced,
                "running": self._running,
                "thread_alive": self._thread.is_alive() if self._thread else False,
                "subprocess_alive": (sub.is_alive() if sub is not None else False),
                "wasapi_discont_count": sub._discont_count if sub is not None else 0,
                "boundary_discontinuities": self._boundary_discontinuities,
                "nan_chunks": self._nan_chunks,
            }

    def _reset_resampler_state(self) -> None:
        """Clear streaming resampler state and any partial output frame."""
        self._resample_stream = None
        self._resample_stream_key = None
        self._resample_output_pending = np.empty((0, _OUTPUT_CHANNELS), dtype=np.float32)

    # ------------------------------------------------------------------ #
    # Main loop (runs on daemon thread)                                    #
    # ------------------------------------------------------------------ #

    def _capture_loop(self) -> None:
        """Main capture loop: discover PID, start subprocess, read PCM.

        Retries indefinitely if the target process dies or is not yet available.
        Only exits when self._stop_event is set (i.e. stop() is called).
        """
        after_rescan = False
        try:
            while not self._stop_event.is_set():
                pid = self._wait_for_audio_pid()
                if pid is None:
                    break  # stop_event was set

                if not self._capture_from_pid(pid, after_rescan=after_rescan, after_notify=self._last_pid_was_notified):
                    # Capture ended (process died, error, etc.) -- retry
                    # If a rescan event is still set, pass that hint to the
                    # next _capture_from_pid call for faster timeout.
                    after_rescan = self._rescan_event.is_set()
                    logger.info(
                        "ProcTap: capture ended for PID %d, will retry discovery" " (after_rescan=%s)",
                        pid,
                        after_rescan,
                    )
                    with self._lock:
                        self._target_pid = None
                    # Skip backoff delay after a rescan — play command just
                    # fired so re-discovery should happen immediately.
                    if not after_rescan:
                        self._stop_event.wait(1.0)
                else:
                    after_rescan = False
        except Exception:
            logger.exception("ProcTap capture loop failed")
        finally:
            self._running = False

    def _wait_for_audio_pid(self) -> int | None:
        """Wait for a child process with an active audio session.

        Checks two sources:
        1. An externally-notified PID (set by :meth:`notify_pid`, typically
           from the mute thread which discovers the PID independently).
        2. Own polling via ``find_audio_child_pid()`` every
           ``_PID_POLL_INTERVAL_SEC`` seconds.

        Returns the PID when found, or None if stop_event is set.
        """
        logged_waiting = False
        self._pid_notify_event.clear()

        while not self._stop_event.is_set():
            # 1. Check for externally-notified PID (mute thread coordination)
            with self._lock:
                notified = self._notified_pid
                if notified is not None:
                    self._notified_pid = None
                    self._last_pid_was_notified = True
                    logger.info(
                        "ProcTap: using notified PID %d from mute thread",
                        notified,
                    )
                    return notified

            # 2. Try own discovery via pycaw
            pid = find_audio_child_pid()
            if pid is not None:
                self._last_pid_was_notified = False
                logger.info("ProcTap: own discovery found PID %d", pid)
                return pid

            if not logged_waiting:
                logger.info("ProcTap: waiting for audio child process...")
                logged_waiting = True

            # Wait with early wake-up on PID notification
            self._pid_notify_event.wait(_PID_POLL_INTERVAL_SEC)
            self._pid_notify_event.clear()

        return None

    def _capture_from_pid(self, pid: int, *, after_rescan: bool = False, after_notify: bool = False) -> bool:
        """Start subprocess ProcTap on *pid* and read PCM from it.

        Returns True if stopped cleanly (stop_event), False if the capture
        failed or the process died (caller should retry).

        *after_rescan*: if True, use a shorter silence timeout on the first
        attempt (a play command just fired, so audio should appear quickly).
        """
        from ._proctap_subprocess import SubprocessProcTap
        from .source_audio_controller import _excluded_pids

        # If the PID was excluded between discovery and here (race with source-local
        # subprocess spawn), abort immediately so ProcTap re-discovers the correct PID.
        if pid in _excluded_pids:
            logger.warning("ProcTap: PID %d is excluded — abandoning capture (race with exclude_pid)", pid)
            return False

        # Clear rescan flag at entry — we're starting fresh on this PID.
        self._rescan_event.clear()

        with self._lock:
            self._target_pid = pid

        self_pid = os.getpid()
        use_system_loopback = pid == self_pid and self._use_system_loopback

        def _desired_system_loopback() -> bool:
            return pid == self_pid and self._use_system_loopback

        def _capture_mode_label(loopback: bool) -> str:
            return "system-loopback" if loopback else "per-process"

        accumulator = bytearray()
        accum_lock = threading.Lock()
        # Track whether a play-command rescan happened mid-capture
        # (Fix 6 may set this to True to enable fast timeout).
        use_fast_timeout = after_rescan
        _sub_proc_pid: int | None = None  # PID of capture subprocess (excluded from discovery)

        for attempt in range(_MAX_RECONNECT_ATTEMPTS):
            if self._stop_event.is_set():
                return True

            # Bug #29 Fix 4+7: if a rescan was requested (play command fired),
            # check whether the target PID matches the current PID.
            # Same PID → just use fast timeout for this attempt (avoid 6s
            # subprocess restart penalty by not going through re-discovery).
            # Different PID → abandon and re-discover as before.
            if self._rescan_event.is_set():
                rescan_pid = self._resolve_rescan_pid(pid)
                if rescan_pid == pid:
                    desired_loopback = _desired_system_loopback()
                    if desired_loopback != use_system_loopback:
                        self._rescan_event.clear()
                        with self._lock:
                            self._notified_pid = None
                        logger.info(
                            "ProcTap: same-PID capture mode switch for PID %d (%s -> %s) before retry",
                            pid,
                            _capture_mode_label(use_system_loopback),
                            _capture_mode_label(desired_loopback),
                        )
                        use_system_loopback = desired_loopback
                        use_fast_timeout = True
                        continue
                    # Same PID — clear events and continue with fast timeout.
                    self._rescan_event.clear()
                    with self._lock:
                        self._notified_pid = None
                    use_fast_timeout = True
                    logger.debug(
                        "ProcTap: rescan for same PID %d between retries "
                        "— continuing with fast timeout (attempt %d)",
                        pid,
                        attempt,
                    )
                else:
                    logger.warning(
                        "ProcTap: rescan interrupt — abandoning PID %d " "(attempt %d, rescan_pid=%s) to re-discover",
                        pid,
                        attempt,
                        rescan_pid,
                    )
                    return False

            label = "ProcTap" if attempt == 0 else "ProcTap[retry %d]" % attempt
            logger.info(
                "%s: starting subprocess for PID %d (%s)",
                label,
                pid,
                "system loopback" if use_system_loopback else "per-process",
            )
            if attempt == 0 and after_notify:
                logger.info(
                    "ProcTap: using extended silence timeout (60s) for notified PID %d",
                    pid,
                )

            # Create and start the subprocess — clear stale data first
            with accum_lock:
                accumulator.clear()
            sub = SubprocessProcTap()

            # Wire subprocess callback to do resampling + delivery
            def _on_raw_pcm(
                data: bytes,
                sr: int,
                ch: int,
                sw: int,
            ) -> None:
                with accum_lock:
                    accumulator.extend(data)

            sub.set_callback(_on_raw_pcm)
            self._subprocess = sub

            if not sub.start(pid, use_system_loopback=use_system_loopback):
                logger.error(
                    "%s: subprocess failed to start for PID %d (%s)",
                    label,
                    pid,
                    "system loopback" if use_system_loopback else "per-process",
                )
                self._subprocess = None
                return False
            with self._lock:
                self._active_capture_mode = _capture_mode_label(use_system_loopback)

            # Exclude the capture subprocess from audio PID discovery.
            # WASAPI capture clients register audio sessions; without this,
            # find_audio_child_pid() can select the capture subprocess itself
            # as the next capture target, creating a circular loop (Bug #29 Fix 8).
            sub_process = getattr(sub, "_process", None)
            if sub_process is not None:
                _sub_proc_pid = sub_process.pid
                try:
                    from .source_audio_controller import exclude_pid as _excl

                    _excl(_sub_proc_pid)
                except Exception:
                    logger.debug("source_audio_controller.exclude_pid unavailable, skipping")

            # Read actual capture format from subprocess
            self._capture_sw = sub.sample_width
            self._capture_sr = sub.sample_rate
            self._reset_resampler_state()
            logger.info(
                "%s: subprocess capture started for PID %d (%s, sw=%d, sr=%d)",
                label,
                pid,
                "system loopback" if use_system_loopback else "per-process",
                self._capture_sw,
                self._capture_sr,
            )

            # Read and process data while subprocess is alive
            activation_time = time.monotonic()
            got_meaningful_audio = False
            # Bug #29 Fix 1: separate hard-timeout clock from rescan throttle.
            # post_audio_silence_start — set once when silence begins, NEVER
            # reset.  Used only for the hard timeout comparison.
            # post_audio_last_rescan — tracks when the last rescan happened.
            # Reset after each rescan to throttle the poll frequency.
            post_audio_silence_start: float | None = None
            post_audio_last_rescan: float | None = None
            switch_pid: int | None = None

            while not self._stop_event.is_set() and sub.is_alive():
                # Process accumulated raw 44100 Hz PCM into 48000 Hz chunks
                self._process_accumulated(accumulator, accum_lock)

                # Check subprocess RMS
                sub_rms = sub.get_rms()
                if sub_rms > 0.0005 and not got_meaningful_audio:
                    got_meaningful_audio = True
                    elapsed = time.monotonic() - activation_time
                    logger.info(
                        "%s: meaningful audio from subprocess at +%.1fs " "(rms=%.6f, pid=%d)",
                        label,
                        elapsed,
                        sub_rms,
                        pid,
                    )

                # Check for silence timeout (pre-audio: never got audio)
                if not got_meaningful_audio:
                    # Skip silence restart when pycaw muting is active.
                    # Embedded sources (YouTube via QtWebEngine) are muted to
                    # 1% by pycaw — captured RMS (~0.0003) is below the
                    # meaningful-audio threshold (0.0005) by design, not
                    # because the subprocess is dead.  Restarting here wastes
                    # 8s and causes audible snippets on reconnect.
                    from audio_core.streaming.pipeline_wiring import (
                        is_embedded_source_active,
                    )

                    if not is_embedded_source_active():
                        elapsed = time.monotonic() - activation_time
                        # Notified PIDs are confirmed correct — wait patiently for
                        # audio to start (YouTube ~16s).  Discovered PIDs use the
                        # normal short/fast timeout to abandon wrong PIDs quickly.
                        if after_notify:
                            silence_limit = 60.0
                        elif use_fast_timeout:
                            silence_limit = _SILENCE_RECONNECT_SEC_FAST
                        else:
                            silence_limit = _SILENCE_RECONNECT_SEC
                        if elapsed > silence_limit:
                            logger.debug(
                                "%s: %.1fs without meaningful audio from PID %d " "-- will restart subprocess",
                                label,
                                elapsed,
                                pid,
                            )
                            break

                # Post-audio silence detection: if the current PID goes
                # silent after producing audio, periodically check if a
                # DIFFERENT audio PID is now active (provider switch).
                if got_meaningful_audio and sub_rms < 0.0005:
                    now = time.monotonic()
                    if post_audio_silence_start is None:
                        post_audio_silence_start = now
                        post_audio_last_rescan = now
                    else:
                        silence_duration = now - post_audio_silence_start

                        # Hard timeout: give up on this PID entirely.
                        # Uses post_audio_silence_start which is set once
                        # and never reset (Bug #29 Fix 1).
                        # Skip for embedded sources (YouTube in QtWebEngine):
                        # quiet content is normal and the subprocess should
                        # stay alive — the audio comes from Viola's own
                        # Chromium process which doesn't change PID.
                        from audio_core.streaming.pipeline_wiring import (
                            is_embedded_source_active,
                        )

                        if silence_duration > _POST_AUDIO_HARD_TIMEOUT_SEC and not is_embedded_source_active():
                            logger.info(
                                "%s: %.1fs post-audio silence — " "abandoning PID %d",
                                label,
                                silence_duration,
                                pid,
                            )
                            break

                        # Periodic re-scan: check for a different audio PID.
                        # Uses post_audio_last_rescan for throttling only.
                        # Skip rescan for embedded sources (YouTube): silence
                        # is from pycaw muting to 1%, not a real source change.
                        rescan_elapsed = now - (post_audio_last_rescan or now)
                        if rescan_elapsed > _POST_AUDIO_RESCAN_SEC and not is_embedded_source_active():
                            from .source_audio_controller import (
                                _external_audio_pids,
                                find_all_audio_pids,
                            )

                            all_pids = find_all_audio_pids()
                            other_pids = [p for p in all_pids if p != pid]
                            if pid in _external_audio_pids:
                                # A registered external source (e.g. Spotify
                                # CDP Chrome) that goes quiet is usually just
                                # PAUSED — never steal its capture for Viola's
                                # own process on a silence rescan (2026-07-02:
                                # a >15s Spotify pause retargeted capture to
                                # Viola's python PID, costing spokes 24-79s of
                                # silence after resume). Self-PID capture is
                                # entered only via an explicit notify_pid()
                                # (local-file and embedded playback both send
                                # one, playback_executor.py).
                                self_pid = os.getpid()
                                if self_pid in other_pids:
                                    logger.info(
                                        "%s: ignoring own PID %d during silence of "
                                        "registered external PID %d (pause, not a source switch)",
                                        label,
                                        self_pid,
                                        pid,
                                    )
                                other_pids = [p for p in other_pids if p != self_pid]
                            if other_pids:
                                switch_pid = other_pids[0]
                                logger.info(
                                    "%s: found new audio PID %d " "(was %d) — switching",
                                    label,
                                    switch_pid,
                                    pid,
                                )
                                break
                            # Reset rescan throttle only (NOT the hard
                            # timeout clock — Bug #29 Fix 1).
                            post_audio_last_rescan = now
                elif got_meaningful_audio:
                    post_audio_silence_start = None
                    post_audio_last_rescan = None

                # Bug #29 Fix 2: check for externally-notified PID during
                # active capture.  This lets the mute worker's PID discovery
                # trigger an immediate switch instead of waiting for our own
                # rescan poll.
                with self._lock:
                    notified = self._notified_pid
                    if notified is not None and notified != pid:
                        self._notified_pid = None
                        switch_pid = notified
                        logger.info(
                            "ProcTap: received notify_pid(%d) during " "capture of PID %d — switching",
                            notified,
                            pid,
                        )
                        break

                # Bug #29 Fix 4+6+7: check rescan event inside inner loop.
                # If a play command fired and the target PID is the SAME,
                # keep the subprocess alive — restarting would waste ~6s
                # of COM startup.  Just reset the silence timer.
                # If a DIFFERENT PID, break to re-discover.
                if self._rescan_event.is_set():
                    rescan_pid = self._resolve_rescan_pid(pid)
                    if rescan_pid == pid:
                        desired_loopback = _desired_system_loopback()
                        if desired_loopback != use_system_loopback:
                            self._rescan_event.clear()
                            with self._lock:
                                self._notified_pid = None
                            switch_pid = pid
                            logger.info(
                                "ProcTap: same-PID capture mode switch for PID %d (%s -> %s); restarting subprocess",
                                pid,
                                _capture_mode_label(use_system_loopback),
                                _capture_mode_label(desired_loopback),
                            )
                            break
                        # Same PID — keep subprocess, reset silence timer.
                        self._rescan_event.clear()
                        with self._lock:
                            self._notified_pid = None
                        activation_time = time.monotonic()
                        got_meaningful_audio = False
                        post_audio_silence_start = None
                        use_fast_timeout = True
                        logger.debug(
                            "ProcTap: rescan for same PID %d — keeping " "subprocess, reset silence timer (fast)",
                            pid,
                        )
                    else:
                        logger.warning(
                            "ProcTap: rescan interrupt during capture of "
                            "PID %d (rescan_pid=%s) — breaking inner loop",
                            pid,
                            rescan_pid,
                        )
                        break

                self._stop_event.wait(0.010)  # 10ms poll

            # Process any remaining data
            self._process_accumulated(accumulator, accum_lock)

            # Re-include subprocess PID before stopping (Bug #29 Fix 8 cleanup)
            if _sub_proc_pid is not None:
                try:
                    from .source_audio_controller import include_pid as _incl

                    _incl(_sub_proc_pid)
                except Exception:
                    logger.debug("source_audio_controller.include_pid unavailable, skipping")
                _sub_proc_pid = None

            # Clean up subprocess
            sub.stop()
            with self._lock:
                self._subprocess = None
                self._active_capture_mode = None

            if self._stop_event.is_set():
                return True

            # If we found a different PID during post-audio silence,
            # notify ourselves so _wait_for_audio_pid picks it up immediately.
            if switch_pid is not None:
                with self._lock:
                    self._notified_pid = switch_pid
                self._pid_notify_event.set()
                return False

            if got_meaningful_audio:
                # Subprocess died after receiving audio — process likely exited
                logger.info(
                    "%s: subprocess ended after successful capture (pid=%d)",
                    label,
                    pid,
                )
                return False

            # Bug #29 Fix 4+7: check rescan between retries too.
            # Same-PID rescan → continue loop with fast timeout (skip
            # the 2s backoff below and start new subprocess immediately).
            if self._rescan_event.is_set():
                rescan_pid = self._resolve_rescan_pid(pid)
                if rescan_pid == pid:
                    desired_loopback = _desired_system_loopback()
                    if desired_loopback != use_system_loopback:
                        self._rescan_event.clear()
                        with self._lock:
                            self._notified_pid = None
                        logger.info(
                            "ProcTap: same-PID capture mode switch for PID %d (%s -> %s) between retries",
                            pid,
                            _capture_mode_label(use_system_loopback),
                            _capture_mode_label(desired_loopback),
                        )
                        use_system_loopback = desired_loopback
                        use_fast_timeout = True
                        continue
                    self._rescan_event.clear()
                    with self._lock:
                        self._notified_pid = None
                    use_fast_timeout = True
                    logger.debug(
                        "ProcTap: rescan for same PID %d between retries " "— skipping backoff, fast timeout",
                        pid,
                    )
                    continue  # skip 2s backoff, start new subprocess now
                else:
                    logger.warning(
                        "ProcTap: rescan interrupt between retries — "
                        "abandoning PID %d (rescan_pid=%s) to re-discover",
                        pid,
                        rescan_pid,
                    )
                    return False

            # No meaningful audio — retry with new subprocess
            logger.info(
                "%s: retrying subprocess capture (attempt %d/%d)",
                label,
                attempt + 1,
                _MAX_RECONNECT_ATTEMPTS,
            )
            self._stop_event.wait(2.0)

        logger.warning(
            "ProcTap: exhausted %d subprocess attempts for PID %d; capture will re-discover or fall back when possible",
            _MAX_RECONNECT_ATTEMPTS,
            pid,
        )
        if pid == self_pid and not use_system_loopback:
            self._use_system_loopback = True
            logger.info(
                "ProcTap: per-process silence on self-PID %d — enabling " "system loopback fallback for the next retry",
                pid,
            )
        return False

    def _track_boundary(self, stereo_in: np.ndarray) -> None:
        """Track inter-chunk boundary continuity for resampler diagnostics.

        Compares the first sample of the current chunk with the last sample
        of the previous chunk.  A jump > 0.3 (normalized) indicates a
        discontinuity that may cause an audible click.

        Args:
            stereo_in: (N, 2) array of samples in [-1, 1] float range.
        """
        if self._prev_last_L is not None:
            delta_L = abs(float(stereo_in[0, 0]) - self._prev_last_L)
            delta_R = abs(float(stereo_in[0, 1]) - self._prev_last_R)
            if max(delta_L, delta_R) > 0.3:
                self._boundary_discontinuities += 1
        self._prev_last_L = float(stereo_in[-1, 0])
        self._prev_last_R = float(stereo_in[-1, 1])

    def _get_resample_stream(self, capture_sr: int, sw: int):
        """Return the persistent soxr stream for the current ProcTap format."""
        key = (int(capture_sr), int(sw))
        if self._resample_stream is None or self._resample_stream_key != key:
            self._resample_stream = soxr.ResampleStream(
                float(capture_sr),
                float(_OUTPUT_RATE),
                _OUTPUT_CHANNELS,
                dtype="float32",
                quality="HQ",
            )
            self._resample_stream_key = key
            self._resample_output_pending = np.empty((0, _OUTPUT_CHANNELS), dtype=np.float32)
        return self._resample_stream

    def _resample_stereo_stream(
        self,
        stereo_in: np.ndarray,
        capture_sr: int,
        sw: int,
    ) -> list[np.ndarray]:
        """Resample continuous stereo ProcTap blocks and return 960-frame output chunks."""
        if stereo_in.ndim != 2 or stereo_in.shape[1] != _OUTPUT_CHANNELS:
            raise ValueError("stereo_in must be stereo frames")
        if len(stereo_in) == 0:
            return []

        stream = self._get_resample_stream(capture_sr, sw)
        output = stream.resample_chunk(np.ascontiguousarray(stereo_in.astype(np.float32, copy=False)))
        if output.size == 0:
            return []

        if len(self._resample_output_pending):
            output = np.vstack((self._resample_output_pending, output))

        chunks: list[np.ndarray] = []
        cursor = 0
        while len(output) - cursor >= _OUTPUT_FRAMES_PER_CHUNK:
            end = cursor + _OUTPUT_FRAMES_PER_CHUNK
            chunks.append(output[cursor:end].copy())
            cursor = end

        self._resample_output_pending = output[cursor:].copy()
        return chunks

    def _process_raw_capture_chunk(self, raw: bytes, capture_sr: int, sw: int) -> None:
        """Convert one raw capture block into one 48 kHz callback chunk when possible."""
        callback = self._callback
        if callback is None:
            return

        needs_resample = capture_sr != _OUTPUT_RATE
        if sw == 4:
            samples_in = np.frombuffer(raw, dtype=np.float32)
            if np.any(np.isnan(samples_in)):
                self._nan_chunks += 1
                samples_in = np.zeros_like(samples_in)
                if self._nan_chunks <= 10:
                    logger.error("NaN detected in ProcTap chunk %d", self._chunks_produced)
            stereo_in = samples_in.reshape(-1, _OUTPUT_CHANNELS)
            if needs_resample:
                output_chunks = self._resample_stereo_stream(stereo_in, capture_sr, sw)
            else:
                self._reset_resampler_state()
                output_chunks = [stereo_in[:_OUTPUT_FRAMES_PER_CHUNK].copy()]

            for resampled_stereo in output_chunks:
                resampled = resampled_stereo.reshape(-1).astype(np.float32, copy=False)
                chunk_bytes = resampled.tobytes()
                self._track_boundary(resampled_stereo)
                with self._lock:
                    self._chunks_produced += 1
                    if self._chunks_produced % 50 == 0:
                        self._last_rms = float(math.sqrt(np.mean(resampled**2)))
                try:
                    callback(chunk_bytes, _OUTPUT_RATE, _OUTPUT_CHANNELS, sw)
                except Exception:
                    logger.exception("ProcTap: callback failed, continuing capture")
        else:
            samples_in = np.frombuffer(raw, dtype=np.int16)
            stereo_in = samples_in.reshape(-1, _OUTPUT_CHANNELS)
            stereo_float = stereo_in.astype(np.float32) / 32768.0
            if needs_resample:
                output_chunks = self._resample_stereo_stream(stereo_float, capture_sr, sw)
            else:
                self._reset_resampler_state()
                output_chunks = [stereo_float[:_OUTPUT_FRAMES_PER_CHUNK].copy()]

            for resampled_stereo_f in output_chunks:
                resampled_stereo = np.clip(
                    np.rint(resampled_stereo_f * 32768.0),
                    -32768,
                    32767,
                ).astype(np.int16)
                resampled = resampled_stereo.reshape(-1)
                chunk_bytes = resampled.tobytes()
                self._track_boundary(resampled_stereo_f)
                with self._lock:
                    self._chunks_produced += 1
                    if self._chunks_produced % 50 == 0:
                        self._last_rms = float(math.sqrt(np.mean(resampled.astype(np.float32) ** 2)) / 32767.0)
                try:
                    callback(chunk_bytes, _OUTPUT_RATE, _OUTPUT_CHANNELS, sw)
                except Exception:
                    logger.exception("ProcTap: callback failed, continuing capture")

    def _process_accumulated(
        self,
        accumulator: bytearray,
        accum_lock: threading.Lock | None = None,
    ) -> None:
        """Resample capture rate -> 48000 Hz and deliver 20ms chunks to callback.

        Handles both int16 (sw=2) and float32 (sw=4) input from the
        subprocess.  Output format matches input: float32 in → float32 out,
        int16 in → int16 out.

        When capture rate matches output rate (48000 Hz, common with system
        loopback), no resampling is needed — chunks pass through directly.

        Args:
            accumulator: Raw PCM byte buffer from subprocess.
            accum_lock: Lock protecting accumulator access (if provided).
        """
        if self._callback is None:
            if accum_lock is not None:
                with accum_lock:
                    accumulator.clear()
            else:
                accumulator.clear()
            return

        sw = self._capture_sw
        capture_sr = self._capture_sr
        block_align = _PROCTAP_CHANNELS * sw

        # Input frames per 20ms output chunk (960 frames at 48kHz).
        # 44100 Hz -> 882 frames, 48000 Hz -> 960 frames (passthrough).
        input_frames_per_chunk = round(960 * capture_sr / _OUTPUT_RATE)
        input_bytes_per_chunk = input_frames_per_chunk * block_align

        # Drain chunks from the accumulator under lock, process outside lock
        while True:
            if accum_lock is not None:
                with accum_lock:
                    if len(accumulator) < input_bytes_per_chunk:
                        break
                    raw = bytes(accumulator[:input_bytes_per_chunk])
                    del accumulator[:input_bytes_per_chunk]
            else:
                if len(accumulator) < input_bytes_per_chunk:
                    break
                raw = bytes(accumulator[:input_bytes_per_chunk])
                del accumulator[:input_bytes_per_chunk]

            self._process_raw_capture_chunk(raw, capture_sr, sw)


__all__ = [
    "ProcTapProvider",
    "find_audio_child_pid",
]
