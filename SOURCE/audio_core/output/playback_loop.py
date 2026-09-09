"""Background playback loop for Spoke audio output.

Pulls chunks from a :class:`~audio_core.streaming.playback_scheduler.PlaybackScheduler`
and writes their PCM data to an :class:`~audio_core.output.base.AudioOutputDriver`
in a dedicated daemon thread.

Usage::

    from audio_core.output.playback_loop import SpokePlaybackLoop

    loop = SpokePlaybackLoop(scheduler, driver)
    loop.start()
    # ...
    loop.stop()

Stopping when the output device is gone
---------------------------------------
``AudioOutputDriver.write()`` blocks until the device consumes the samples, so
when a device is unplugged or wedges mid-playback the loop thread can be parked
inside the driver for longer than :data:`_STOP_JOIN_TIMEOUT_SEC`.  Two things
follow, and both are handled here rather than assumed away:

* :meth:`SpokePlaybackLoop.stop` reports whether the thread *actually* exited.
  Clearing the thread reference on a timed-out join would make
  :attr:`is_running` report "stopped" for a thread that is still writing --
  a health signal describing a state nobody measured.  A thread that did not
  exit is kept as an orphan so :attr:`is_running` keeps telling the truth.
* Each run owns its own stop flag.  A single shared, re-cleared
  :class:`threading.Event` would let a later :meth:`start` un-signal a thread
  that had been told to stop but had not yet returned from the device call, and
  that resurrected thread would then write to the same driver alongside the new
  one.  A per-run event cannot be un-signalled, so an orphan always exits the
  moment its device call returns.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from core.constants import TIMEOUT_SHORT
from core.logging_config import get_logger

if TYPE_CHECKING:
    from audio_core.output.base import AudioOutputDriver
    from audio_core.streaming.playback_scheduler import PlaybackScheduler

logger = get_logger(__name__)

# How long the loop sleeps when no chunk is ready (seconds).
_POLL_INTERVAL: float = TIMEOUT_SHORT  # 0.1 s  -- 5x per chunk at 20 ms

#: How long :meth:`SpokePlaybackLoop.stop` waits for the loop thread to exit.
_STOP_JOIN_TIMEOUT_SEC: float = 2.0

__all__ = [
    "SpokePlaybackLoop",
]


class SpokePlaybackLoop:
    """Background thread that drains the PlaybackScheduler into an output driver.

    The loop calls :meth:`PlaybackScheduler.get_next_chunk` on each
    iteration.  When a chunk is due, its ``pcm_data`` is forwarded to
    :meth:`AudioOutputDriver.write`.  When the queue is empty or no chunk
    is due yet, the loop sleeps briefly to avoid busy-waiting.

    The thread is created as a daemon so it will not prevent interpreter
    shutdown.
    """

    def __init__(
        self,
        scheduler: PlaybackScheduler,
        driver: AudioOutputDriver,
        *,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._scheduler = scheduler
        self._driver = driver
        self._poll_interval = poll_interval
        # Guards the thread/event bookkeeping below.  Never held across a join
        # or any driver call -- doing so would put teardown behind the device.
        self._lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        # Threads that were told to stop but had not returned from the driver
        # by the time the join timed out.  Tracked so is_running() stays honest.
        self._orphans: list[threading.Thread] = []

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Start the background playback thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.debug("SpokePlaybackLoop already running")
                return

            self._prune_orphans_locked()
            orphan_count = len(self._orphans)

            # A fresh event per run. The previous run's event stays set forever,
            # so a thread still parked in the driver can never be resurrected
            # into writing alongside this new one.
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(stop_event,),
                name="spoke-playback-loop",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread

        if orphan_count:
            logger.warning(
                "SpokePlaybackLoop starting while %d earlier thread(s) are still "
                "parked in the output driver; they are stop-flagged and will exit "
                "when their device call returns",
                orphan_count,
            )
        thread.start()
        logger.info("SpokePlaybackLoop started")

    def stop(self) -> bool:
        """Signal the playback thread to stop and wait for it to exit.

        Returns:
            ``True`` if the thread exited (or none was running), ``False`` if it
            was still inside the output driver when the join timed out.  A
            ``False`` return means the output device is gone or wedged: the
            thread is stop-flagged and will exit on its own when the device call
            returns, and :attr:`is_running` keeps reporting ``True`` until then.
        """
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
            self._thread = None
            self._stop_event = None

        if stop_event is not None:
            stop_event.set()

        if thread is None:
            logger.info("SpokePlaybackLoop stopped (no thread running)")
            return True

        thread.join(timeout=_STOP_JOIN_TIMEOUT_SEC)

        if thread.is_alive():
            with self._lock:
                self._orphans.append(thread)
                self._prune_orphans_locked()
            logger.warning(
                "SpokePlaybackLoop thread did not exit within %.1fs -- it is still "
                "inside AudioOutputDriver.write() (output device removed or "
                "wedged). The thread is stop-flagged and orphaned; it will exit "
                "when the device call returns. Teardown continues without it.",
                _STOP_JOIN_TIMEOUT_SEC,
            )
            return False

        logger.info("SpokePlaybackLoop stopped")
        return True

    @property
    def is_running(self) -> bool:
        """Return True if any playback thread of this loop is still alive.

        Includes threads orphaned by a timed-out :meth:`stop`: while one is
        still inside the driver it is still writing to the device, and reporting
        "not running" for it would be a health signal for a state nobody
        measured.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._prune_orphans_locked()
            return bool(self._orphans)

    # -- internal -----------------------------------------------------------

    def _prune_orphans_locked(self) -> None:
        """Drop orphaned threads that have since exited. Caller holds ``_lock``."""
        if self._orphans:
            self._orphans = [t for t in self._orphans if t.is_alive()]

    def _run(self, stop_event: threading.Event) -> None:
        """Main loop executed in the background thread."""
        logger.debug("SpokePlaybackLoop thread entered")
        try:
            while not stop_event.is_set():
                chunk = self._scheduler.get_next_chunk()
                if chunk is not None:
                    try:
                        self._driver.write(chunk.pcm_data)
                    except Exception:
                        logger.exception("Error writing chunk to output driver")
                else:
                    # Nothing due -- sleep briefly to avoid busy-waiting
                    time.sleep(self._poll_interval)
        except Exception:
            logger.exception("SpokePlaybackLoop thread crashed")
        finally:
            logger.debug("SpokePlaybackLoop thread exiting")
