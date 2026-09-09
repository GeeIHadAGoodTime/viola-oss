"""CoreAudio system-audio capture provider for macOS (Core Audio process taps).

Captures the system audio *output* mix as 48 kHz / stereo / signed-16-bit PCM
and delivers 20 ms chunks through the registered callback — matching the
Windows WASAPI (:mod:`audio_core.capture.wasapi_loopback`) and Linux PulseAudio
providers so the multi-room fanout pipeline is platform-agnostic.

Mechanism (macOS 14.4+, no virtual audio device, no screen-recording perm):
    1. ``CATapDescription`` global tap (stereo mixdown of all processes,
       excluding none).
    2. ``AudioHardwareCreateProcessTap`` -> a tap AudioObject.
    3. A private aggregate device wrapping the tap
       (``AudioHardwareCreateAggregateDevice``).
    4. ``AudioDeviceCreateIOProcID`` + ``AudioDeviceStart`` — the IOProc
       receives the tapped float32 buffers on Core Audio's realtime thread.
    5. A worker thread resamples dev-rate -> 48 kHz, converts float32 -> int16,
       and emits 3840-byte (20 ms) stereo chunks via the callback.

Permission: capturing the *system* mix on macOS 14.4+ requires the process to
hold the OS audio-capture (TCC) grant. Without it Core Audio creates the tap
successfully but delivers **silence** (Apple privacy behaviour). A signed app
bundle with ``NSAudioCaptureUsageDescription`` + the audio-input entitlement
obtains the grant via a one-time user prompt. ``request_permission`` triggers
that prompt; ``get_metrics`` reports the live authorization state.

This provider is desktop-local (Tier-3): captured audio / AEC reference material
never leaves the machine.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys
import threading

from core.constants import AUDIO_CHANNELS_STEREO, SAMPLE_RATE_48K
from core.logging_config import get_logger

from .base import AudioCaptureProvider

logger = get_logger(__name__)

IS_MACOS = sys.platform == "darwin"

_BYTES_PER_SAMPLE = 2
_CHUNK_DURATION_MS = 20
_SAMPLES_PER_CHUNK = (SAMPLE_RATE_48K * _CHUNK_DURATION_MS) // 1000  # 960
_CHUNK_SIZE_BYTES = _SAMPLES_PER_CHUNK * AUDIO_CHANNELS_STEREO * _BYTES_PER_SAMPLE  # 3840

__all__ = ["CoreAudioCaptureProvider"]


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


if IS_MACOS:

    class _PropAddr(ctypes.Structure):
        _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]

    class _ASBD(ctypes.Structure):
        _fields_ = [
            ("mSampleRate", ctypes.c_double),
            ("mFormatID", ctypes.c_uint32),
            ("mFormatFlags", ctypes.c_uint32),
            ("mBytesPerPacket", ctypes.c_uint32),
            ("mFramesPerPacket", ctypes.c_uint32),
            ("mBytesPerFrame", ctypes.c_uint32),
            ("mChannelsPerFrame", ctypes.c_uint32),
            ("mBitsPerChannel", ctypes.c_uint32),
            ("mReserved", ctypes.c_uint32),
        ]

    class _AudioBuffer(ctypes.Structure):
        _fields_ = [
            ("mNumberChannels", ctypes.c_uint32),
            ("mDataByteSize", ctypes.c_uint32),
            ("mData", ctypes.c_void_p),
        ]

    class _AudioBufferList(ctypes.Structure):
        _fields_ = [("mNumberBuffers", ctypes.c_uint32), ("mBuffers", _AudioBuffer * 8)]

    _IOPROC = ctypes.CFUNCTYPE(
        ctypes.c_int32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(_AudioBufferList),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    def _load_coreaudio():
        path = ctypes.util.find_library("CoreAudio") or "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        ca = ctypes.CDLL(path)
        ca.AudioHardwareCreateProcessTap.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioHardwareCreateProcessTap.restype = ctypes.c_int32
        ca.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
        ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
        ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropAddr),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        ca.AudioDeviceCreateIOProcID.argtypes = [
            ctypes.c_uint32,
            _IOPROC,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ca.AudioDeviceCreateIOProcID.restype = ctypes.c_int32
        ca.AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        ca.AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        ca.AudioDeviceStart.restype = ctypes.c_int32
        ca.AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        return ca


def _audio_authorization_status() -> str:
    """Return the process's audio-capture TCC status (notDetermined/denied/authorized)."""
    if not IS_MACOS:
        return "n/a"
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        st = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        return {0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized"}.get(int(st), str(st))
    except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        return "unknown"


class CoreAudioCaptureProvider(AudioCaptureProvider):
    """Capture macOS system audio output via a Core Audio process tap."""

    def __init__(self) -> None:
        self._callback = None
        self._running = False
        self._lock = threading.Lock()
        self._raw = bytearray()  # float32 interleaved from the tap
        self._raw_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._ca = None
        self._tap_id = ctypes.c_uint32(0) if IS_MACOS else None
        self._agg_id = ctypes.c_uint32(0) if IS_MACOS else None
        self._proc_id = ctypes.c_void_p(0) if IS_MACOS else None
        self._ioproc_ref = None  # keep CFUNCTYPE alive
        self._dev_rate = 0.0
        self._dev_channels = 2
        self._chunks_produced = 0
        self._last_rms = 0.0

    def set_callback(self, fn) -> None:
        self._callback = fn

    @classmethod
    def is_available(cls) -> bool:
        """True on macOS 14.4+ where the Core Audio process-tap API exists."""
        if not IS_MACOS:
            return False
        try:
            import objc

            objc.lookUpClass("CATapDescription")  # only present on macOS 14.4+
            ca_path = (
                ctypes.util.find_library("CoreAudio") or "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
            )
            return hasattr(ctypes.CDLL(ca_path), "AudioHardwareCreateProcessTap")
        except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
            return False

    @staticmethod
    def request_permission() -> str:
        """Trigger the OS audio-capture permission prompt; return the resulting status."""
        if not IS_MACOS:
            return "n/a"
        try:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

            done = threading.Event()
            AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, lambda granted: done.set())
            done.wait(timeout=60)
        except Exception as exc:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
            logger.warning("audio permission request failed: %s", exc)
        return _audio_authorization_status()

    def start(self) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("CoreAudio capture already started")
            if not self.is_available():
                raise RuntimeError("CoreAudio process-tap API unavailable (requires macOS 14.4+)")
            import objc
            from Foundation import NSArray, NSMutableDictionary, NSString

            self._ca = _load_coreaudio()
            ca = self._ca

            # 1) global system-output tap (exclude no processes)
            cad = objc.lookUpClass("CATapDescription")
            desc = cad.alloc().initStereoGlobalTapButExcludeProcesses_([])
            st = ca.AudioHardwareCreateProcessTap(ctypes.c_void_p(objc.pyobjc_id(desc)), ctypes.byref(self._tap_id))
            if st != 0 or self._tap_id.value == 0:
                raise RuntimeError("AudioHardwareCreateProcessTap failed (OSStatus=%d)" % st)

            # tap UID
            addr = _PropAddr(_fourcc("tuid"), _fourcc("glob"), 0)
            uid_ptr = ctypes.c_void_p(0)
            sz = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
            ca.AudioObjectGetPropertyData(
                self._tap_id, ctypes.byref(addr), 0, None, ctypes.byref(sz), ctypes.byref(uid_ptr)
            )
            tap_uid = objc.objc_object(c_void_p=uid_ptr.value)

            # 2) private aggregate device wrapping the tap
            agg = NSMutableDictionary.dictionary()
            agg[NSString.stringWithString_("uid")] = NSString.stringWithString_("viola-systemaudio-tap")
            agg[NSString.stringWithString_("name")] = NSString.stringWithString_("Viola System Audio Tap")
            agg[NSString.stringWithString_("private")] = True
            agg[NSString.stringWithString_("tapautostart")] = True
            subtap = NSMutableDictionary.dictionary()
            subtap[NSString.stringWithString_("uid")] = tap_uid
            agg[NSString.stringWithString_("taps")] = NSArray.arrayWithObject_(subtap)
            st = ca.AudioHardwareCreateAggregateDevice(ctypes.c_void_p(objc.pyobjc_id(agg)), ctypes.byref(self._agg_id))
            if st != 0 or self._agg_id.value == 0:
                ca.AudioHardwareDestroyProcessTap(self._tap_id)
                raise RuntimeError("AudioHardwareCreateAggregateDevice failed (OSStatus=%d)" % st)

            # device stream format (rate/channels the tap delivers)
            fmt = _ASBD()
            faddr = _PropAddr(_fourcc("sfmt"), _fourcc("inpt"), 0)
            fsz = ctypes.c_uint32(ctypes.sizeof(_ASBD))
            if (
                ca.AudioObjectGetPropertyData(
                    self._agg_id, ctypes.byref(faddr), 0, None, ctypes.byref(fsz), ctypes.byref(fmt)
                )
                == 0
            ):
                self._dev_rate = fmt.mSampleRate or 48000.0
                self._dev_channels = fmt.mChannelsPerFrame or 2
            else:
                self._dev_rate, self._dev_channels = 48000.0, 2

            # 3) IOProc — copy tapped float32 into the raw buffer (realtime thread; keep light)
            # ctypes invokes this through _IOPROC positionally, so the arity must stay 7 and
            # match CoreAudio's AudioDeviceIOProc exactly: (inDevice, inNow, inInputData,
            # inInputTime, outOutputData, inOutputTime, inClientData). This tap is input-only,
            # so every parameter except the input buffer list is deliberately unread and is
            # underscore-named to say so rather than to look like an oversight.
            def _io(_dev, _now, inbl, _intime, _outbl, _outtime, _client):
                try:
                    bl = inbl[0]
                    for i in range(min(bl.mNumberBuffers, 8)):
                        b = bl.mBuffers[i]
                        if b.mData and b.mDataByteSize:
                            with self._raw_lock:
                                self._raw += ctypes.string_at(b.mData, b.mDataByteSize)
                except Exception:  # noqa: BLE001, S110, RUF100 - best-effort platform guard; must not raise
                    pass
                return 0

            self._ioproc_ref = _IOPROC(_io)
            st = ca.AudioDeviceCreateIOProcID(self._agg_id, self._ioproc_ref, None, ctypes.byref(self._proc_id))
            if st != 0:
                self._cleanup_devices()
                raise RuntimeError("AudioDeviceCreateIOProcID failed (OSStatus=%d)" % st)

            self._stop_evt.clear()
            self._worker = threading.Thread(target=self._run_worker, name="coreaudio-tap", daemon=True)
            self._running = True
            self._worker.start()

            st = ca.AudioDeviceStart(self._agg_id, self._proc_id)
            if st != 0:
                self._running = False
                self._stop_evt.set()
                self._cleanup_devices()
                raise RuntimeError("AudioDeviceStart failed (OSStatus=%d)" % st)
            logger.info(
                "CoreAudio tap capture started: dev_rate=%.0f ch=%d -> 48k int16 (audio_auth=%s)",
                self._dev_rate,
                self._dev_channels,
                _audio_authorization_status(),
            )

    def _run_worker(self) -> None:
        import numpy as np

        out = bytearray()
        ratio = SAMPLE_RATE_48K / float(self._dev_rate or 48000.0)
        while not self._stop_evt.is_set():
            with self._raw_lock:
                buf = bytes(self._raw)
                self._raw.clear()
            if not buf:
                self._stop_evt.wait(0.005)
                continue
            frames = np.frombuffer(buf, dtype=np.float32)
            ch = self._dev_channels
            if ch >= 2:
                usable = (frames.size // ch) * ch
                frames = frames[:usable].reshape(-1, ch)[:, :2]
            else:
                frames = np.repeat(frames.reshape(-1, 1), 2, axis=1)
            if frames.shape[0] == 0:
                continue
            if abs(ratio - 1.0) > 1e-6:
                n_in = frames.shape[0]
                n_out = max(1, int(n_in * ratio))
                xi = np.linspace(0, n_in - 1, n_out)
                idx = np.arange(n_in)
                frames = np.stack([np.interp(xi, idx, frames[:, 0]), np.interp(xi, idx, frames[:, 1])], axis=1)
            self._last_rms = float(np.sqrt(np.mean(frames**2))) if frames.size else 0.0
            i16 = np.clip(frames * 32767.0, -32768, 32767).astype("<i2")
            out += i16.tobytes()
            while len(out) >= _CHUNK_SIZE_BYTES:
                chunk = bytes(out[:_CHUNK_SIZE_BYTES])
                del out[:_CHUNK_SIZE_BYTES]
                self._chunks_produced += 1
                cb = self._callback
                if cb:
                    try:
                        cb(chunk, SAMPLE_RATE_48K, AUDIO_CHANNELS_STEREO, _BYTES_PER_SAMPLE)
                    except Exception as exc:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
                        logger.debug("capture callback error: %s", exc)

    def _cleanup_devices(self) -> None:
        ca = self._ca
        if ca is None:
            return
        try:
            if self._proc_id and self._proc_id.value and self._agg_id.value:
                ca.AudioDeviceDestroyIOProcID(self._agg_id, self._proc_id)
        except Exception:  # noqa: BLE001, S110, RUF100 - best-effort platform guard; must not raise
            pass
        try:
            if self._agg_id.value:
                ca.AudioHardwareDestroyAggregateDevice(self._agg_id)
        except Exception:  # noqa: BLE001, S110, RUF100 - best-effort platform guard; must not raise
            pass
        try:
            if self._tap_id.value:
                ca.AudioHardwareDestroyProcessTap(self._tap_id)
        except Exception:  # noqa: BLE001, S110, RUF100 - best-effort platform guard; must not raise
            pass
        self._agg_id = ctypes.c_uint32(0)
        self._tap_id = ctypes.c_uint32(0)
        self._proc_id = ctypes.c_void_p(0)

    def stop(self) -> None:
        with self._lock:
            if not self._running and (self._agg_id is None or self._agg_id.value == 0):
                return
            self._stop_evt.set()
            if self._ca and self._agg_id.value and self._proc_id and self._proc_id.value:
                try:
                    self._ca.AudioDeviceStop(self._agg_id, self._proc_id)
                except Exception:  # noqa: BLE001, S110, RUF100 - best-effort device stop during capture teardown
                    pass
            self._cleanup_devices()
            self._running = False
        if self._worker:
            self._worker.join(timeout=2.0)
            self._worker = None
        self._ioproc_ref = None

    def get_metrics(self) -> dict:
        return {
            "provider": "coreaudio_tap",
            "rms": round(self._last_rms, 6),
            "chunks_produced": self._chunks_produced,
            "running": self._running,
            "device_sample_rate": self._dev_rate,
            "device_channels": self._dev_channels,
            "audio_authorization": _audio_authorization_status(),
            "chunk_size_bytes": _CHUNK_SIZE_BYTES,
        }
