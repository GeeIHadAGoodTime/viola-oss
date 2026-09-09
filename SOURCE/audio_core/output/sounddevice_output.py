"""Sounddevice-based audio output driver.

Uses the ``sounddevice`` library to play raw PCM via an
:class:`~sounddevice.RawOutputStream`.  Samples are written as
``int16`` numpy arrays (16-bit signed PCM).

Close safety (device removal / device wedge)
--------------------------------------------
``write()`` blocks inside PortAudio's ``Pa_WriteStream`` until the device has
consumed the samples.  When a device disappears or wedges mid-playback (USB
headset unplugged, Bluetooth link dropped) that call can stay parked for far
longer than any teardown is willing to wait.

The dangerous shape -- and the one this module used to have -- is a ``stop()``
that swaps the Python attribute under a lock and then calls
``stream.close()`` *outside* it.  ``sounddevice.Stream.close()`` is
``Pa_CloseStream``, which frees the stream's internal buffers.  Doing that
while another thread is parked inside ``Pa_WriteStream`` on the same stream
frees memory that thread is still walking: a native access violation with no
Python traceback, the same crash class documented in
:mod:`audio_core.portaudio_guard`.

The rule here is therefore **exactly one thread ever closes a stream, and only
when no thread can be inside it**:

* Each opened stream gets a :class:`_StreamSession` carrying its own in-flight
  writer count.  Bookkeeping is per session, so a writer wedged on an old
  device can never corrupt the accounting of a stream opened after it.
* ``write()`` registers itself on the session *under the lock*, writes outside
  the lock (a blocking device call must never hold a lock teardown needs), then
  deregisters under the lock.
* ``stop()`` detaches the session under the lock -- after which no new writer
  can enter it -- waits a bounded moment for any in-flight writer to come back,
  and closes the stream itself only if none is inside.  If a writer is still
  parked in PortAudio, ``stop()`` hands that writer the close and returns
  immediately, so teardown is never held hostage by a dead device.

The trade is deliberate: if the device never returns, one stream handle stays
open for the life of the process.  A leaked handle on an already-dead device is
strictly better than freeing memory out from under a live PortAudio call.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from core.logging_config import get_logger

from .base import AudioOutputDriver

logger = get_logger(__name__)

__all__ = [
    "SounddeviceAudioOutput",
]

#: How long :meth:`SounddeviceAudioOutput.stop` waits for an in-flight
#: ``write()`` to return before handing that writer the close and giving up on
#: closing inline.  A healthy device consumes a chunk in tens of milliseconds,
#: so this is only ever reached when the device is gone or wedged -- and when it
#: is reached, teardown continues rather than blocking.
_STOP_DRAIN_TIMEOUT_SEC: float = 0.5


class _StreamSession:
    """One opened PortAudio stream plus the bookkeeping that decides who closes it.

    Every field is read and written only while the owning driver's ``_lock`` is
    held, except :attr:`idle`, which is a :class:`threading.Event` and is safe to
    wait on without the lock.

    A session is per *stream object*, not per driver, so that a writer still
    parked in a stream opened before a device change cannot decrement the
    in-flight count of a stream opened after it.
    """

    __slots__ = ("close_pending", "closed", "idle", "stream", "writers")

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        #: Number of threads currently inside ``stream.write()``.
        self.writers = 0
        #: Set whenever :attr:`writers` is zero.
        self.idle = threading.Event()
        self.idle.set()
        #: ``stop()`` wanted to close but a writer was inside; the last writer
        #: out performs the close instead.
        self.close_pending = False
        #: Guards against a double close when ``stop()`` and a departing writer
        #: race for ownership.
        self.closed = False


class SounddeviceAudioOutput(AudioOutputDriver):
    """Audio output driver backed by ``sounddevice.RawOutputStream``.

    The stream is opened on :meth:`start` and closed on :meth:`stop`.
    :meth:`write` converts the incoming PCM bytes into a numpy ``int16``
    array and writes them to the stream; the call blocks until the
    device has consumed the data.

    Thread safety follows the contract on :class:`~audio_core.output.base.AudioOutputDriver`:
    ``write()`` runs on a background playback loop while ``start()`` / ``stop()``
    run on the application lifecycle thread.  See the module docstring for how a
    close is kept off a live write.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: _StreamSession | None = None
        self._running = False
        self._channels: int = 1
        self._sample_width: int = 2

    # -- AudioOutputDriver interface ----------------------------------------

    def start(self, sample_rate: int, channels: int, sample_width: int) -> None:
        import sounddevice as sd  # deferred to avoid import failure at module level

        with self._lock:
            if self._running:
                return

            self._channels = channels
            self._sample_width = sample_width

            stream = sd.RawOutputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
            )
            stream.start()
            # A session from a previous stop() may still be alive with a wedged
            # writer inside it; it owns its own close and is deliberately not
            # touched here.  This driver simply moves on to the new stream.
            self._session = _StreamSession(stream)
            self._running = True

        logger.info(
            "SounddeviceAudioOutput started: rate=%d channels=%d width=%d",
            sample_rate,
            channels,
            sample_width,
        )

    def write(self, data: bytes) -> None:
        with self._lock:
            session = self._session
            if not self._running or session is None:
                return
            # Registering under the lock is what makes stop() correct: once
            # stop() has detached the session, no writer can get here, and any
            # writer already counted is one stop() knows to wait for.
            session.writers += 1
            session.idle.clear()

        try:
            # The blocking device call is deliberately made OUTSIDE the lock --
            # holding it here would make every teardown wait on the device.
            samples = np.frombuffer(data, dtype=np.int16)
            session.stream.write(samples)
        except Exception:
            logger.exception("SounddeviceAudioOutput write error")
        finally:
            self._writer_finished(session)

    def stop(self) -> None:
        with self._lock:
            session = self._session
            if not self._running or session is None:
                return
            # Detach first: from here on no new write() can enter this session.
            self._session = None
            self._running = False

        # Give any writer already inside PortAudio a bounded chance to come
        # back, so the healthy case still closes inline and in order.
        session.idle.wait(timeout=_STOP_DRAIN_TIMEOUT_SEC)

        stream_to_close: Any | None = None
        deferred = False
        with self._lock:
            if session.writers == 0:
                if not session.closed:
                    session.closed = True
                    stream_to_close = session.stream
            else:
                # A writer is still parked in the device call.  Closing now
                # would free buffers it is walking, so hand it the close and
                # let teardown continue.
                session.close_pending = True
                deferred = True

        if stream_to_close is not None:
            self._close_stream(stream_to_close)

        if deferred:
            logger.warning(
                "SounddeviceAudioOutput stop: a write is still inside PortAudio "
                "(device removed or wedged); the stream was detached and its "
                "close handed to that writer. Teardown is not waiting for it."
            )
        logger.info("SounddeviceAudioOutput stopped")

    # -- internal -----------------------------------------------------------

    def _writer_finished(self, session: _StreamSession) -> None:
        """Deregister a writer and close the stream if ``stop()`` left it to us."""
        stream_to_close: Any | None = None
        with self._lock:
            session.writers -= 1
            if session.writers <= 0:
                session.writers = 0
                session.idle.set()
                if session.close_pending and not session.closed:
                    session.closed = True
                    stream_to_close = session.stream

        if stream_to_close is not None:
            logger.info(
                "SounddeviceAudioOutput: deferred stream close performed by the "
                "writer that was inside PortAudio at stop() time"
            )
            self._close_stream(stream_to_close)

    def _close_stream(self, stream: Any) -> None:
        """Abort and close a stream that no thread can be inside.

        ``abort()`` is used rather than ``stop()`` because ``stop()`` waits for
        pending buffers to drain, which on a removed device is another place
        teardown can hang.  ``close()`` discards pending buffers anyway, so the
        drain buys nothing here.
        """
        try:
            abort = getattr(stream, "abort", None)
            if callable(abort):
                abort()
        except Exception:
            logger.exception("SounddeviceAudioOutput stream abort error")

        try:
            stream.close()
        except Exception:
            logger.exception("SounddeviceAudioOutput stream close error")
