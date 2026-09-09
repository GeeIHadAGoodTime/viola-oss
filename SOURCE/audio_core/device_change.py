"""Route audio device changes to the components that actually hold streams.

The problem this solves
======================
Viola picked its microphone and its speaker once, at boot, and never looked
again. Connect a Bluetooth headset mid-session, plug in a USB interface, or
change the Windows default endpoint, and capture and playback both stayed on
whatever was there at startup. If the boot device *disappeared*, the failure
landed in a swallowed exception and the user simply got silence.

Detecting the change is only a third of the job. Three things have to happen,
in this order, or nothing moves:

1. **Something notices.** :class:`~voice.wake_detector.device_profile_manager.DeviceChangeDetector`
   already did this correctly and was never called by anything. It polls
   ``get_device_fingerprint()``, which enumerates through
   :func:`audio_core.portaudio_guard.portaudio_instance` — a fresh
   ``Pa_Initialize`` / ``Pa_Terminate`` per query, so it genuinely re-reads the
   hardware and a poll loop can actually observe a change. This module starts it
   rather than reimplementing it.

2. **The stale enumeration is rebuilt.** ``sounddevice`` initializes PortAudio
   once at import and never re-initializes, so its device table is frozen at
   first import for the life of the process. Telling a sounddevice-backed
   consumer to "re-resolve" without rebuilding that table just re-resolves
   against the same stale snapshot. :func:`~audio_core.portaudio_guard.refresh_sounddevice_devices`
   is what makes re-resolution mean anything, and this module is its only
   sanctioned caller because the refresh is only safe with every sounddevice
   stream closed.

3. **The stream owners re-open.** A component holding an open stream has to be
   told, and has to re-open on the *new* device.

Why polling and not ``IMMNotificationClient``
=============================================
Windows can report endpoint changes immediately through the COM interface
``IMMNotificationClient`` (via ``pycaw``/``comtypes``), which would cut the
worst-case latency from one poll interval to ~instant. It is deliberately not
used here:

* It is Windows-only, and Viola ships Linux (AppImage) and macOS builds, so the
  poller would still have to exist as the fallback — the COM path would be a
  second mechanism, not a replacement.
* It needs a COM apartment plus a dedicated message-pump thread, and its
  callbacks arrive on an OS-owned thread with real reentrancy rules.
* This codebase already has a logged incident from that exact surface:
  ``docs/DECISIONS_REGISTRY.md`` records ``set_default_endpoint()`` firing an
  ``IMMNotificationClient`` callback inside the in-process Chromium and crashing
  its audio session.
* It adds a dependency, against the standing zero-new-machinery preference.

A few seconds to notice a headset is well inside what a human reads as
"immediate" for this action, and the cross-platform poller already existed. If
sub-second response is ever wanted on Windows, the right shape is an *additional*
trigger that calls :func:`notify_device_change_now` — the routing below does not
care what noticed.

The race this module has to not lose
====================================
A device-change callback that tears down and rebuilds audio streams runs on the
detector's poll thread, while the wake-detection loop is concurrently blocked
inside ``stream.read()`` on the very stream being torn down. Closing a PortAudio
stream under a reader is a use-after-free in C, not a Python exception.

So no listener here is asked to close a stream from *this* thread. The contract
is a **handoff**: :meth:`AudioStreamOwner.suspend_for_device_change` must make the
owner's stream quiescent *by its own thread's rules* and only return once that is
true. The wake listener implements it by setting a flag its read loop checks at a
safe point and waiting for that loop to acknowledge, so the close always happens
on the thread that does the reading. Owners whose writes are already serialized
by their own lock (the sounddevice output driver) can close inline under that
lock. Either way the router only sequences; it never touches a stream itself.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Protocol, runtime_checkable

from core.logging_config import get_logger

logger = get_logger(__name__)

# How long to wait for in-flight transient playbacks to drain before rebuilding
# the device table. Generous enough for a normal TTS utterance to finish, short
# enough that a wedged stream cannot block device recovery forever.
_PLAYBACK_DRAIN_TIMEOUT_SECONDS = 10.0

# Default poll cadence for the change detector, in seconds.
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


@runtime_checkable
class AudioStreamOwner(Protocol):
    """A component holding a long-lived audio stream across a device change.

    Both methods are called from the device-watch thread and MUST NOT raise; the
    router isolates exceptions anyway so one broken owner cannot strand the rest,
    but an owner that throws will not be re-resumed correctly.
    """

    def suspend_for_device_change(self) -> None:
        """Make this owner's stream quiescent and return only once it is closed.

        Must be safe against the owner's own reader/writer threads — see the
        module docstring on the handoff contract. Returning while another thread
        can still be inside a read/write on the old stream is the bug this whole
        module is built to avoid.
        """
        ...

    def resume_after_device_change(self) -> None:
        """Re-open on whatever device is correct now."""
        ...


class _PlaybackGate:
    """Reader/writer gate: many transient playbacks, or one device-table refresh.

    ``sounddevice``'s table can only be rebuilt with every stream closed, but
    Viola's TTS opens short-lived streams through ``sd.play()`` without
    registering as a long-lived owner. Those spans take the reader side; the
    refresh takes the writer side and waits for readers to drain.

    Writer-preferring: once a refresh is waiting, new readers block instead of
    starving it behind a stream of back-to-back utterances.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers = 0
        self._writer_waiting = 0
        self._writer_active = False

    @contextmanager
    def reader(self) -> Iterator[None]:
        with self._condition:
            while self._writer_active or self._writer_waiting > 0:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def writer(self, timeout: float) -> Iterator[bool]:
        """Acquire the exclusive side. Yields True if readers actually drained."""
        with self._condition:
            self._writer_waiting += 1
            try:
                drained = self._condition.wait_for(lambda: self._readers == 0 and not self._writer_active, timeout)
                self._writer_active = True
            finally:
                self._writer_waiting -= 1
        try:
            yield drained
        finally:
            with self._condition:
                self._writer_active = False
                self._condition.notify_all()


_gate = _PlaybackGate()

_registry_lock = threading.RLock()
_owners: list[AudioStreamOwner] = []

_watch_lock = threading.RLock()
_watch_started = False
_last_known_good: tuple[bool, bool] = (True, True)


@contextmanager
def playback_stream_guard() -> Iterator[None]:
    """Mark a transient sounddevice playback so a refresh cannot cut it off.

    Wrap the whole span in which a sounddevice stream is open, including the
    blocking wait — closing over only the call that *starts* playback would leave
    the stream open and unprotected for the entire utterance.
    """
    with _gate.reader():
        yield


def register_stream_owner(owner: AudioStreamOwner) -> None:
    """Register a long-lived stream owner to be moved on a device change.

    Starts the device watch as a side effect. That coupling is deliberate: the
    watch exists only to move registered owners, so tying its lifetime to the
    existence of an owner means it cannot be left un-started by an unrelated
    refactor of some startup sequence.

    The predecessor of this module was a complete, correct ``DeviceChangeDetector``
    that nothing ever instantiated, so device changes went unnoticed for the life
    of the product. Requiring a separate "and also start it" call somewhere in
    bootstrap is exactly how that happened; self-wiring here is what stops it
    from happening again.
    """
    started_now = False
    with _registry_lock:
        if owner not in _owners:
            _owners.append(owner)
            started_now = True
            logger.debug("Registered audio stream owner: %s", type(owner).__name__)
    if started_now:
        start_device_watch()


def unregister_stream_owner(owner: AudioStreamOwner) -> None:
    """Remove a stream owner (on its cleanup/shutdown)."""
    with _registry_lock:
        if owner in _owners:
            _owners.remove(owner)
            logger.debug("Unregistered audio stream owner: %s", type(owner).__name__)


def _snapshot_owners() -> tuple[AudioStreamOwner, ...]:
    with _registry_lock:
        return tuple(_owners)


def probe_default_devices() -> tuple[bool, bool, str, str]:
    """Report whether a default input and output currently resolve.

    Enumerates through the pyaudio guard (fresh ``Pa_Initialize`` per call), so
    unlike a sounddevice query this reflects the hardware as it is right now.

    Returns ``(input_ok, output_ok, input_name, output_name)``.
    """
    input_ok = False
    output_ok = False
    input_name = "(none)"
    output_name = "(none)"
    try:
        from audio_core.portaudio_guard import portaudio_instance

        with portaudio_instance() as pa:
            try:
                info = pa.get_default_input_device_info()
                input_name = str(info.get("name", "(unnamed)"))
                input_ok = int(info.get("maxInputChannels", 0)) > 0
            except Exception:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
                logger.debug("No default input device resolved", exc_info=True)
            try:
                info = pa.get_default_output_device_info()
                output_name = str(info.get("name", "(unnamed)"))
                output_ok = int(info.get("maxOutputChannels", 0)) > 0
            except Exception:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
                logger.debug("No default output device resolved", exc_info=True)
    except Exception:
        logger.exception("Could not probe default audio devices")
    return input_ok, output_ok, input_name, output_name


def _report_device_availability() -> None:
    """Log loudly when audio hardware disappears, and when it comes back.

    A vanished device used to surface only as a swallowed exception and silence.
    This is the one place that says so at a level a user-visible log will show,
    and it only fires on a transition so a permanently headless box does not spam.
    """
    global _last_known_good

    input_ok, output_ok, input_name, output_name = probe_default_devices()
    previous_input_ok, previous_output_ok = _last_known_good
    _last_known_good = (input_ok, output_ok)

    if previous_input_ok and not input_ok:
        logger.error(
            "AUDIO DEVICE LOST: no usable microphone is available after a device change. "
            "Voice input is down until a microphone is connected or selected."
        )
    elif input_ok and not previous_input_ok:
        logger.info("AUDIO DEVICE RESTORED: microphone available again (%s)", input_name)

    if previous_output_ok and not output_ok:
        logger.error(
            "AUDIO DEVICE LOST: no usable playback device is available after a device change. "
            "Viola cannot be heard until a speaker or headset is connected."
        )
    elif output_ok and not previous_output_ok:
        logger.info("AUDIO DEVICE RESTORED: playback device available again (%s)", output_name)


def _emit_device_change_event(old_id: str | None, new_id: str, input_ok: bool, output_ok: bool) -> None:
    """Surface the change to the UI so a lost device is visible, not just silent."""
    try:
        from ui.qt_native.debug_events import emit_debug_event

        if emit_debug_event is not None:
            emit_debug_event(
                "audio_device_changed",
                {
                    "old_device_id": old_id,
                    "new_device_id": new_id,
                    "input_available": input_ok,
                    "output_available": output_ok,
                },
                source="backend",
            )
    except Exception:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
        logger.debug("Could not emit audio_device_changed event", exc_info=True)


def handle_device_change(old_id: str | None, new_id: str) -> None:
    """Move every registered stream owner onto the now-current device.

    Ordering is the whole point and is not rearrangeable:

    1. Suspend every owner. Each returns only once its stream is genuinely
       closed and no thread of its own can still be reading or writing it.
    2. Rebuild sounddevice's PortAudio device table, with the transient-playback
       gate held exclusively so no ``sd.play()`` is mid-flight. Only now is
       ``Pa_Terminate`` safe, and only after it does a sounddevice consumer
       resolve anything other than the boot-time snapshot.
    3. Resume every owner, which re-resolves and re-opens.

    Every owner is resumed even if its suspend or the refresh failed: leaving a
    component suspended would turn a recoverable device change into permanent
    silence, which is the failure mode being fixed.
    """
    logger.info("Audio device change detected (%s -> %s); moving capture and playback", old_id, new_id)

    owners = _snapshot_owners()
    suspended: list[AudioStreamOwner] = []
    for owner in owners:
        try:
            owner.suspend_for_device_change()
            suspended.append(owner)
        except Exception:
            logger.exception("Audio stream owner %s failed to suspend for device change", type(owner).__name__)

    try:
        with _gate.writer(_PLAYBACK_DRAIN_TIMEOUT_SECONDS) as drained:
            if not drained:
                logger.warning(
                    "Transient playback did not drain within %.0fs; "
                    "rebuilding the audio device table anyway so a device change is not ignored",
                    _PLAYBACK_DRAIN_TIMEOUT_SECONDS,
                )
            from audio_core.portaudio_guard import refresh_sounddevice_devices

            if refresh_sounddevice_devices():
                logger.info("Rebuilt sounddevice device table; playback will re-resolve")
    except Exception:
        logger.exception(
            "Failed to rebuild the sounddevice device table; " "playback may stay on the previous device until restart"
        )

    for owner in owners:
        try:
            owner.resume_after_device_change()
        except Exception:
            logger.exception("Audio stream owner %s failed to resume after device change", type(owner).__name__)

    _report_device_availability()
    input_ok, output_ok = _last_known_good
    _emit_device_change_event(old_id, new_id, input_ok, output_ok)


def notify_device_change_now(reason: str = "manual") -> None:
    """Run the device-change routing immediately, without waiting for a poll.

    The seam for any faster trigger (a Windows endpoint notification, a settings
    change that repoints a device, a test). The routing does not care what
    noticed the change.
    """
    logger.info("Audio device re-resolution requested (%s)", reason)
    handle_device_change(None, reason)


def start_device_watch(poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS) -> bool:
    """Start watching for audio device changes. Idempotent.

    Reuses the existing :class:`DeviceChangeDetector` singleton rather than
    adding a second polling mechanism.
    """
    global _watch_started
    with _watch_lock:
        if _watch_started:
            return True
        try:
            from voice.wake_detector.device_profile_manager import get_device_change_detector

            detector = get_device_change_detector()
            detector.set_poll_interval(poll_interval_seconds)
            detector.on_device_change(handle_device_change)
            detector.start()
            _watch_started = True
            logger.info("Audio device watch started (poll interval %.1fs)", poll_interval_seconds)
            return True
        except Exception:
            logger.exception("Could not start the audio device watch; hot-plug changes will not be followed")
            return False


def stop_device_watch() -> None:
    """Stop watching for audio device changes. Idempotent."""
    global _watch_started
    with _watch_lock:
        if not _watch_started:
            return
        try:
            from voice.wake_detector.device_profile_manager import get_device_change_detector

            detector = get_device_change_detector()
            detector.remove_callback(handle_device_change)
            detector.stop()
        except Exception:
            logger.exception("Error stopping the audio device watch")
        finally:
            _watch_started = False


def is_device_watch_running() -> bool:
    """True when the device watch is active."""
    with _watch_lock:
        return _watch_started


def _reset_for_tests() -> None:
    """Clear module state between tests."""
    global _watch_started, _last_known_good, _gate
    with _registry_lock:
        _owners.clear()
    with _watch_lock:
        _watch_started = False
    _last_known_good = (True, True)
    _gate = _PlaybackGate()


def _current_owner_count() -> int:
    """Number of registered stream owners (diagnostics/tests)."""
    with _registry_lock:
        return len(_owners)


def _describe_state() -> dict[str, Any]:
    """Snapshot for /health style reporting."""
    return {
        "watch_running": is_device_watch_running(),
        "registered_owners": _current_owner_count(),
        "input_available": _last_known_good[0],
        "output_available": _last_known_good[1],
    }


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "AudioStreamOwner",
    "handle_device_change",
    "is_device_watch_running",
    "notify_device_change_now",
    "playback_stream_guard",
    "probe_default_devices",
    "register_stream_owner",
    "start_device_watch",
    "stop_device_watch",
    "unregister_stream_owner",
]
