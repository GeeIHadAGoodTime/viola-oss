"""
PortAudio lifecycle guard — process-wide serialization of Pa_Initialize / Pa_Terminate.
======================================================================================

Why this exists
---------------
PortAudio (the C library behind ``pyaudio``) keeps a single, process-global,
reference-counted table of host-API descriptors. Each descriptor holds the
**function pointers** PortAudio calls to enumerate devices, open streams, etc.
``Pa_Initialize`` builds/ref-counts that table; ``Pa_Terminate`` decrements it
and, when the count reaches zero, frees the descriptors and zeroes their
function pointers.

That table is **not protected by any lock inside PortAudio** — the library is
explicitly documented as not thread-safe for initialize/terminate. So when two
threads touch PortAudio at the same time (one constructing ``pyaudio.PyAudio()``
== ``Pa_Initialize`` while another calls ``.terminate()`` == ``Pa_Terminate``),
one thread can free a host-API descriptor while the other is mid-walk through
its function-pointer table. The walking thread then calls through a freed /
NULL function pointer and the CPU jumps to address ``0x0`` and tries to execute
there — a native access violation that Windows reports as:

    python.exe - Application Error
    The instruction at 0x0000000000000000 referenced memory at 0x0...
    (exception 0xC0000005, WER bucket "BEX64" = execute non-executable memory)

There is no Python traceback because it is a crash *inside the C library*, on a
background thread. It is intermittent because it only fires when two PortAudio
lifecycle calls genuinely overlap in time.

The observed crash (2026-06-30): the wake-detector startup thread fingerprinting
the input device (``Pa_Initialize``) raced the ``GET /rooms`` API handler
fingerprinting the output device (``Pa_Terminate``) — both via
``voice.wake_detector.device_profile_manager.get_device_fingerprint``.

The fix
-------
Serialize every **pyaudio-family** PortAudio create/terminate (``pyaudio`` and
the ``pyaudiowpatch`` WASAPI-loopback fork) behind one process-wide lock so two
threads can never be inside ``Pa_Initialize`` / ``Pa_Terminate`` at once.

Scope: ``sounddevice`` is guarded too — through :func:`sounddevice_guard`, which
takes the **same** lock (#1663). The old rationale ("sounddevice loads its own
bundled ``libportaudio`` DLL with its own global table, so it cannot cross-race
pyaudio") holds only on Windows/macOS, where the sounddevice wheel ships a bundled
PortAudio binary. On **Linux** it is false: ``sounddevice`` binds the *system*
``libportaudio.so.2`` and ``pyaudio`` links the *same* system PortAudio
(``requirements_linux.txt`` — both need ``libportaudio2`` / ``portaudio19-dev``),
so the dynamic linker maps ONE library with ONE global host-API table shared by
both bindings. On top of that, PortAudio's ALSA host API drives the process-global
``libasound`` config tree (``snd_config``), whose enumeration walk is not
thread-safe and is shared by *every* audio client in the process — including an
in-process Chromium. So a sounddevice ``query_devices()`` on one boot thread
genuinely races a pyaudio ``Pa_Initialize`` / ``Pa_Terminate`` on another, and a
second sounddevice enumeration on a third, over that shared global state — the same
crash class this module already fixed for pyaudio, reached via a different door.

A *separate* sounddevice-only lock would NOT close the sounddevice-vs-pyaudio
cross-race on the shared Linux library, which is exactly why sounddevice routes
through THIS lock. The guard is a no-op cost when uncontended and correct on every
OS. What is serialized is the full-device **enumeration** (``query_devices()`` with
no device selector — the ``libasound`` config walk) and the one-time
``import sounddevice`` (its ``Pa_Initialize``); a single-device ``query_devices(idx)``
info lookup and long-lived stream lifetimes are out of scope (a stream held under
the lock would serialize unrelated playback).

Two usage shapes:

* **Transient** (create -> query -> terminate inside one function): use the
  :func:`portaudio_instance` context manager. It holds the lock for the whole
  short-lived instance, which also prevents any other thread from tearing the
  table down while you enumerate devices.

* **Persistent** (a long-lived ``self._audio = pyaudio.PyAudio()`` opened in one
  place and ``.terminate()``-d on shutdown): use :func:`open_portaudio` /
  :func:`terminate_portaudio`, which take the lock only around the
  initialize / terminate moments (never for the whole stream lifetime, which
  would serialize unrelated work and risk stalls).

A reentrant lock is used so a guarded helper may call another guarded helper on
the same thread without self-deadlock.

The second door: a live stream freed under a reader (found investigating #4650)
------------------------------------------------------------------------------
Serializing ``Pa_Initialize`` / ``Pa_Terminate`` against each other closes only
one of the two doors. The other one is a stream's own lifetime.

``Pa_CloseStream`` frees the host-API stream object (on Windows the
``PaWinWasapiStream``) and joins its servicing thread. ``pyaudio``'s
``Stream.close()`` calls it and then leaves ``self._stream`` pointing at the
freed capsule — it sets no "is closed" flag, and ``Stream.read()`` /
``Stream.write()`` check none, they hand that capsule straight back to
``Pa_ReadStream`` / ``Pa_WriteStream``. So when one thread closes a stream while
another thread is inside a read on it, the reader (and PortAudio's own
processing thread for that stream) dereferences freed memory.
``PyAudio.terminate()`` has the same shape one level up: it closes every stream
it owns, from the terminating thread, before ``Pa_Terminate``.

This is reachable, not hypothetical, and the proof is in the source rather than
in any crash dump: ``voice.wake_detector.facade.WakeWordDetector.stop()`` calls
``cleanup()`` on the CALLER's thread and deliberately does not join the
wake-detector thread ("the daemon thread will exit on its own"), while
``ViolaWakeListener``'s read loop sits in ``self._stream.read(...)`` holding no
lock that ``cleanup()`` takes. Mute, a settings change, and shutdown all reach
``stop()`` from other threads (``voice/pipeline.py``).

The same bug class was already found and fixed on the sounddevice OUTPUT side
(#4572, see :mod:`audio_core.output.sounddevice_output` — "freeing memory that
thread is still walking"). This module closes the pyaudio INPUT sibling, with
the same deliberate trade: leak a handle rather than free a live stream.

:data:`PORTAUDIO_LOCK` cannot fix this, and must not try: a read blocks for a
whole buffer period and holding the process-wide lifecycle lock across every
mic read would serialize all unrelated audio. The fix is per-stream instead —
:class:`GuardedStream`, returned by :func:`open_stream`, owns one lock per
stream and takes it around **every** PortAudio call on that stream, so a close
can never overlap a read/write, and a use-after-close raises
:class:`PortAudioStreamClosed` (a ``RuntimeError``) instead of touching freed
memory.

When a close cannot prove the stream is idle it **leaks the stream rather than
freeing it** — see :meth:`GuardedStream.close`. A leaked stream costs a device
handle; freeing one out from under a live reader costs the whole process.

Lock ordering (the invariant that keeps this deadlock-free)
-----------------------------------------------------------
:data:`PORTAUDIO_LOCK` may be taken *before* a per-stream lock (that is what
:func:`open_stream` and :func:`terminate_portaudio` do), never after. No
:class:`GuardedStream` method takes :data:`PORTAUDIO_LOCK`, so the reverse edge
does not exist and the two lock levels cannot form a cycle.
"""

from __future__ import annotations

import logging
import threading
import weakref
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pyaudio

logger = logging.getLogger(__name__)

# One process-wide lock for ALL PortAudio lifecycle calls. Reentrant so nested
# guarded calls on one thread don't deadlock. Importing this name and taking it
# around a raw PyAudio()/terminate() is the sanctioned escape hatch for call
# shapes the helpers below don't cover (the ratchet gate allows it).
PORTAUDIO_LOCK = threading.RLock()

# How long a teardown waits for an in-flight read/write to finish before it
# gives up and leaks the stream instead of freeing it under the reader. A
# blocking read returns after one buffer period (tens of milliseconds for every
# capture stream in this app), so a healthy stream is always well inside this;
# only a wedged device driver can reach the timeout, and in that case leaking is
# the correct trade (see the module docstring).
STREAM_QUIESCE_TIMEOUT = 5.0


class PortAudioStreamClosed(RuntimeError):
    """Raised when a guarded PortAudio stream is used after it was closed.

    Subclasses ``RuntimeError`` deliberately: every capture/playback path in this
    codebase already treats ``RuntimeError`` as a recoverable device failure, so a
    stream that gets closed mid-read surfaces as an ordinary reopen-and-retry
    instead of a native crash.
    """


# Guarded streams, per owning PyAudio instance, so terminate_portaudio() can
# close them under their own locks BEFORE Pa_Terminate (pyaudio's own
# terminate() would otherwise close them from the terminating thread). Weak
# keys so a dropped PyAudio instance does not pin this registry.
_STREAM_REGISTRY: weakref.WeakKeyDictionary[Any, set[GuardedStream]] = weakref.WeakKeyDictionary()
_REGISTRY_LOCK = threading.Lock()


class GuardedStream:
    """A pyaudio stream whose every PortAudio call is serialized against close.

    Wraps one ``pyaudio.PyAudio.Stream`` (or the ``pyaudiowpatch`` equivalent)
    and holds a per-stream reentrant lock across every call that reaches the C
    library. Two properties follow, and they are the whole point:

    * A ``close()`` can never overlap an in-flight ``read()`` / ``write()``, so
      ``Pa_CloseStream`` never frees a stream another thread is inside.
    * After the close, any further call raises :class:`PortAudioStreamClosed`
      rather than handing a dangling capsule back to PortAudio.

    The lock is reentrant so a caller may drive the stream from inside another
    guarded call on the same thread (e.g. stop-then-close) without deadlock.
    """

    __slots__ = ("__weakref__", "_closed", "_lock", "_owner_ref", "_stream")

    def __init__(self, stream: Any, owner: Any) -> None:
        self._stream = stream
        self._owner_ref = weakref.ref(owner)
        self._lock = threading.RLock()
        self._closed = False

    # -- state ------------------------------------------------------------- #

    @property
    def closed(self) -> bool:
        """True once this stream has been closed (or abandoned as un-quiescable)."""
        return self._closed

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Run one PortAudio stream call under this stream's lock."""
        with self._lock:
            if self._closed:
                raise PortAudioStreamClosed("PortAudio stream is closed (%s)" % name)
            return getattr(self._stream, name)(*args, **kwargs)

    # -- the PortAudio surface --------------------------------------------- #

    def read(self, num_frames: int, exception_on_overflow: bool = True) -> bytes:
        return self._call("read", num_frames, exception_on_overflow=exception_on_overflow)

    def write(self, frames: bytes, num_frames: int | None = None, exception_on_underflow: bool = False) -> None:
        return self._call("write", frames, num_frames=num_frames, exception_on_underflow=exception_on_underflow)

    def start_stream(self) -> None:
        return self._call("start_stream")

    def stop_stream(self) -> None:
        return self._call("stop_stream")

    def is_active(self) -> bool:
        return self._call("is_active")

    def is_stopped(self) -> bool:
        return self._call("is_stopped")

    def get_read_available(self) -> int:
        return self._call("get_read_available")

    def get_write_available(self) -> int:
        return self._call("get_write_available")

    def get_input_latency(self) -> float:
        return self._call("get_input_latency")

    def get_output_latency(self) -> float:
        return self._call("get_output_latency")

    def get_time(self) -> float:
        return self._call("get_time")

    def get_cpu_load(self) -> float:
        return self._call("get_cpu_load")

    # -- teardown ----------------------------------------------------------- #

    def close(self, timeout: float = STREAM_QUIESCE_TIMEOUT) -> bool:
        """Close the stream once no other thread is inside a call on it.

        Returns ``True`` when the stream was genuinely closed (or was already
        closed), ``False`` when the wait for an in-flight call timed out.

        On timeout the stream is marked closed — so nothing new can touch it —
        but ``Pa_CloseStream`` is deliberately **not** called: freeing a stream a
        live thread is reading is the exact use-after-free this class exists to
        prevent, and a leaked device handle is the cheaper failure. A ``False``
        return also tells :func:`terminate_portaudio` not to run ``Pa_Terminate``,
        which would free the same memory by another route.
        """
        if not self._lock.acquire(timeout=timeout):
            self._closed = True
            logger.warning(
                "PortAudio stream still in use after %.1fs; leaving it open rather than "
                "freeing it under an in-flight call (a leaked handle beats a native crash)",
                timeout,
            )
            return False
        try:
            if self._closed:
                return True
            try:
                self._stream.close()
            finally:
                self._closed = True
                self._unregister()
        finally:
            self._lock.release()
        return True

    def _unregister(self) -> None:
        owner = self._owner_ref()
        if owner is None:
            return
        with _REGISTRY_LOCK:
            streams = _STREAM_REGISTRY.get(owner)
            if streams is not None:
                streams.discard(self)


def open_stream(pa: Any, **kwargs: Any) -> GuardedStream:
    """Open a capture/playback stream on ``pa`` and return it guarded.

    This is the ONLY sanctioned way for production code to open a pyaudio-family
    stream. A raw ``pa.open(...)`` hands back a stream whose ``read`` can be
    freed mid-call by any other thread's ``close`` / ``terminate`` — the #4650
    crash — so the ratchet gate rejects it.

    ``Pa_OpenStream`` itself walks the host-API table, so the open is taken under
    :data:`PORTAUDIO_LOCK` (never held for the stream's lifetime — only for the
    open call).
    """
    with PORTAUDIO_LOCK:
        raw = pa.open(**kwargs)
    guarded = GuardedStream(raw, pa)
    with _REGISTRY_LOCK:
        _STREAM_REGISTRY.setdefault(pa, set()).add(guarded)
    return guarded


def _close_streams_for(pa: Any, timeout: float) -> bool:
    """Close every guarded stream opened on ``pa``. False if any would not quiesce."""
    with _REGISTRY_LOCK:
        streams = list(_STREAM_REGISTRY.get(pa, ()))
    return all([stream.close(timeout=timeout) for stream in streams])


@contextmanager
def portaudio_instance() -> Iterator[pyaudio.PyAudio]:
    """Yield a PyAudio instance with init+terminate serialized under the lock.

    Use for transient create -> query -> terminate sequences::

        from audio_core.portaudio_guard import portaudio_instance

        with portaudio_instance() as pa:
            info = pa.get_default_input_device_info()

    The lock is held for the entire ``with`` block, guaranteeing no other thread
    can ``Pa_Initialize`` / ``Pa_Terminate`` (and thus free the host-API table)
    while this instance is alive and being queried.
    """
    import pyaudio

    with PORTAUDIO_LOCK:
        pa = pyaudio.PyAudio()
        try:
            yield pa
        finally:
            # Drain any guarded stream opened on this instance first: pyaudio's
            # own terminate() closes the streams it owns from THIS thread, which
            # would free a stream another thread is reading (#4650).
            if _close_streams_for(pa, timeout=STREAM_QUIESCE_TIMEOUT):
                pa.terminate()
            else:
                logger.warning(
                    "Skipping Pa_Terminate: a stream on this PortAudio instance did not "
                    "quiesce, and terminating would free it under an in-flight call"
                )


def open_portaudio() -> pyaudio.PyAudio:
    """Construct a persistent PyAudio instance with ``Pa_Initialize`` serialized.

    For long-lived instances (e.g. ``self._audio = open_portaudio()``) that are
    terminated later via :func:`terminate_portaudio`. Only the initialize call is
    serialized; the caller owns the instance afterward.
    """
    import pyaudio

    with PORTAUDIO_LOCK:
        return pyaudio.PyAudio()


def terminate_portaudio(pa: pyaudio.PyAudio | None, timeout: float = STREAM_QUIESCE_TIMEOUT) -> None:
    """Terminate a persistent PyAudio instance with ``Pa_Terminate`` serialized.

    No-op if ``pa`` is None. Safe to call from shutdown paths, and safe to call
    from a thread other than the one driving the streams: every guarded stream
    on this instance is closed under its own lock FIRST, so ``Pa_Terminate``
    never frees a stream another thread is inside.

    That ordering is load-bearing. ``pyaudio.PyAudio.terminate()`` closes the
    streams it owns itself, from the calling thread, with no synchronization —
    which is how a mute toggle or a settings change on the UI thread used to free
    the wake-capture stream out from under the wake-detector thread's blocking
    read (#4650). If a stream will not quiesce, ``Pa_Terminate`` is skipped
    entirely rather than run over a live reader.
    """
    if pa is None:
        return
    if not _close_streams_for(pa, timeout=timeout):
        logger.warning(
            "Skipping Pa_Terminate: a stream on this PortAudio instance did not quiesce "
            "within %.1fs, and terminating would free it under an in-flight call",
            timeout,
        )
        return
    with PORTAUDIO_LOCK:
        pa.terminate()


def refresh_sounddevice_devices() -> bool:
    """Rebuild ``sounddevice``'s PortAudio device table so it sees hot-plugged devices.

    Why this is needed
    ------------------
    ``sounddevice`` calls ``_initialize()`` (``Pa_Initialize``) exactly once, at
    ``import sounddevice`` (module level in the vendored ``sounddevice.py``), and
    only ever terminates via ``atexit``. PortAudio builds its host-API **device
    table** during ``Pa_Initialize`` and never refreshes it afterwards.

    ``sd.query_devices()`` and ``sd.default.device`` are not Python-cached — they
    call ``Pa_GetDeviceCount`` / ``Pa_GetDefaultOutputDevice`` fresh every time —
    but those C functions read that one frozen table. So for the whole life of the
    process, sounddevice reports the devices that existed at first import: a
    Bluetooth headset connected later is invisible, and a changed Windows default
    endpoint is never observed. Re-resolving a device through sounddevice without
    this refresh re-resolves against the same stale snapshot and changes nothing.

    The pyaudio side does not have this problem: :func:`portaudio_instance` and
    :func:`open_portaudio` construct a *new* ``PyAudio()`` each time, and because
    the transient form terminates back down to a zero refcount, the next
    ``Pa_Initialize`` genuinely rebuilds the table. (On Windows the two bindings
    are separate binaries with separate tables — ``sounddevice`` loads its bundled
    ``libportaudio64bit.dll``, ``pyaudio`` links its own ``_portaudio`` extension —
    so refreshing one does not refresh the other.)

    Safety contract — READ BEFORE CALLING
    -------------------------------------
    This runs ``Pa_Terminate`` followed by ``Pa_Initialize``. ``Pa_Terminate``
    tears down the host-API descriptors that every open sounddevice stream is
    still holding function pointers into — exactly the free-while-walking crash
    this module exists to prevent. **Every sounddevice stream must be stopped and
    closed before calling this**, which is why the only sanctioned caller is
    :mod:`audio_core.device_change`, whose router suspends all registered stream
    owners first and resumes them afterwards.

    The whole terminate+initialize pair is held under :data:`PORTAUDIO_LOCK`, so
    no pyaudio ``Pa_Initialize`` / ``Pa_Terminate`` (and no guarded sounddevice
    enumeration) can interleave with it — on Linux that matters especially, since
    both bindings share the system ``libportaudio.so.2`` and the process-global
    ``libasound`` config tree.

    Returns
    -------
    ``True`` when the table was rebuilt, ``False`` when sounddevice is not
    installed. Raises if the re-initialize fails, because a failed re-init leaves
    sounddevice unusable and that must not be swallowed into silent no-audio.
    """
    with PORTAUDIO_LOCK:
        try:
            import sounddevice as sd
        except ImportError:
            return False

        sd._terminate()
        try:
            sd._initialize()
        except Exception:  # noqa: BLE001, RUF100 - re-init errors vary by host API
            # A failed re-init leaves the module with PortAudio down, so every
            # later query/stream raises. Retry once before giving up — a
            # transient re-enumeration during a hot-plug is the likely cause.
            sd._initialize()
        return True


@contextmanager
def sounddevice_playback_guard() -> Iterator[None]:
    """Hold off a device-table refresh for the duration of a transient playback.

    For the short-lived ``sd.play()`` / ``sd.wait()`` shape (Viola's TTS) that
    opens a stream, plays, and closes it without registering a long-lived owner
    with :mod:`audio_core.device_change`. Wrapping that span in this guard makes
    :func:`refresh_sounddevice_devices` wait for it rather than calling
    ``Pa_Terminate`` out from under an open stream.

    Deliberately NOT :data:`PORTAUDIO_LOCK`: playback lasts seconds, and holding
    the process-wide lifecycle lock that long would stall every unrelated device
    query. This is the reader side of the refresh's reader/writer gate, so any
    number of playbacks may overlap each other and only a refresh is excluded.
    """
    from audio_core.device_change import playback_stream_guard

    with playback_stream_guard():
        yield


@contextmanager
def sounddevice_guard() -> Iterator[None]:
    """Serialize a ``sounddevice`` PortAudio/ALSA call under the process-wide lock.

    Wrap any full-device enumeration (``sounddevice.query_devices()`` with no device
    selector — the call that walks the process-global ``libasound`` config tree) and
    the one-time ``import sounddevice`` (its ``Pa_Initialize``) in this guard so it
    cannot run concurrently with pyaudio's guarded lifecycle, an in-process
    Chromium's audio thread, or another sounddevice enumeration.

    It deliberately takes the SAME :data:`PORTAUDIO_LOCK` as the pyaudio helpers:
    on Linux both bindings share the system ``libportaudio.so.2`` and the
    process-global ``libasound`` config, so a private lock would leave the
    sounddevice-vs-pyaudio cross-race open (see the module docstring, #1663). The
    lock is reentrant, so a guarded call may nest inside another guarded call on the
    same thread without deadlock.
    """
    with PORTAUDIO_LOCK:
        yield


__all__ = [
    "PORTAUDIO_LOCK",
    "STREAM_QUIESCE_TIMEOUT",
    "GuardedStream",
    "PortAudioStreamClosed",
    "open_portaudio",
    "open_stream",
    "portaudio_instance",
    "sounddevice_guard",
    "terminate_portaudio",
]
