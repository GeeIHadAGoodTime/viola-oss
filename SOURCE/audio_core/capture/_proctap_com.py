"""
Reusable COM layer for Windows Process Audio Tap (ProcTap).

Provides ctypes structures, GUID utilities, and COM activation functions
for per-process audio loopback capture via ActivateAudioInterfaceAsync.

This module is Windows-only and requires the ``mmdevapi`` system DLL.
"""

from __future__ import annotations

import ctypes
import struct
import threading
import uuid
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# ctypes structures                                                    #
# ------------------------------------------------------------------ #


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("blob", BLOB),
    ]


class FakeCOM(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.c_void_p)]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)

# COM IIDs
IID_IUNKNOWN = "{00000000-0000-0000-C000-000000000046}"
IID_IAGILE = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"
IID_HANDLER = "{41D949AB-9862-444A-80F6-C261334DA5EB}"
IID_IAUDIOCLIENT = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
IID_IAUDIOCAPTURECLIENT = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"
IID_IMMDEVICEENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"

# AUDCLNT flags
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x00000001

# HRESULT codes
E_NOINTERFACE = 0x80004002

# MMDevice COM class / interface IDs
CLSID_MMDEVICEENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

# COM / MMDevice constants
CLSCTX_INPROC_SERVER = 0x1
E_RENDER = 0
E_CONSOLE = 0


# ------------------------------------------------------------------ #
# Utility functions                                                    #
# ------------------------------------------------------------------ #


def make_guid(s: str) -> GUID:
    """Convert a GUID string to a ctypes GUID structure."""
    u = uuid.UUID(s)
    g = GUID()
    ctypes.memmove(ctypes.byref(g), u.bytes_le, 16)
    return g


def _guid_eq(a: GUID, b: GUID) -> bool:
    """Compare two GUID structures for equality."""
    return a.Data1 == b.Data1 and a.Data2 == b.Data2 and a.Data3 == b.Data3 and bytes(a.Data4) == bytes(b.Data4)


def call_com(ptr: int, idx: int, restype: type, *args: tuple[type, Any]) -> Any:
    """Call a COM vtable method at the given index.

    Args:
        ptr: Pointer to the COM object.
        idx: Vtable index of the method.
        restype: Return type of the method.
        *args: Pairs of (ctype, value) for each argument.

    Returns:
        The HRESULT or other return value from the COM method.
    """
    argtypes = [ctypes.c_void_p] + [a[0] for a in args]
    argvals = [a[1] for a in args]
    ft = ctypes.WINFUNCTYPE(restype, *argtypes)
    vt = ctypes.c_void_p()
    ctypes.memmove(ctypes.byref(vt), ptr, PTR_SIZE)
    fp = ctypes.c_void_p()
    ctypes.memmove(ctypes.byref(fp), vt.value + idx * PTR_SIZE, PTR_SIZE)
    return ft(fp.value)(ptr, *argvals)


# ------------------------------------------------------------------ #
# COM activation                                                       #
# ------------------------------------------------------------------ #


def activate_proctap(pid: int) -> tuple[int, tuple]:
    """Activate process loopback IAudioClient for the given PID.

    Must be called from an MTA-initialised thread (or a thread where
    COM has been initialised in any apartment mode).

    Args:
        pid: Process ID to capture audio from.

    Returns:
        Tuple of (client_ptr, gc_refs) where client_ptr is the raw
        pointer to the IAudioClient COM interface and gc_refs is a
        tuple of prevent-GC references that must be kept alive while
        client_ptr is in use.

    Raises:
        RuntimeError: If activation fails or times out.
    """
    # Build activation parameters
    params = struct.pack("III", 1, pid, 0)
    buf = ctypes.create_string_buffer(params)
    pv = PROPVARIANT()
    pv.vt = 0x41  # VT_BLOB
    pv.blob.cbSize = len(params)
    pv.blob.pBlobData = ctypes.addressof(buf)

    done = threading.Event()
    result: list[int | None] = [None]

    # Build IActivateAudioInterfaceCompletionHandler callback vtable
    iid_unknown = make_guid(IID_IUNKNOWN)
    iid_agile = make_guid(IID_IAGILE)
    iid_handler = make_guid(IID_HANDLER)

    QI_T = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    AR_T = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    RL_T = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    AC_T = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

    def qi(this: int, riid: Any, ppv: Any) -> int:
        g = riid.contents
        if _guid_eq(g, iid_unknown) or _guid_eq(g, iid_handler) or _guid_eq(g, iid_agile):
            ppv.contents = ctypes.c_void_p(this)
            return 0
        return E_NOINTERFACE

    def completed(this: int, op: int) -> int:
        result[0] = op
        done.set()
        return 0

    qi_f = QI_T(qi)
    ar_f = AR_T(lambda t: 2)
    rl_f = RL_T(lambda t: 1)
    ac_f = AC_T(completed)

    vtbl = (ctypes.c_void_p * 4)(
        ctypes.cast(qi_f, ctypes.c_void_p),
        ctypes.cast(ar_f, ctypes.c_void_p),
        ctypes.cast(rl_f, ctypes.c_void_p),
        ctypes.cast(ac_f, ctypes.c_void_p),
    )
    handler = FakeCOM()
    handler.lpVtbl = ctypes.addressof(vtbl)

    func = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    func.restype = ctypes.c_long
    func.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(PROPVARIANT),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    iid = make_guid(IID_IAUDIOCLIENT)
    op_ptr = ctypes.c_void_p()

    hr = func(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        ctypes.byref(iid),
        ctypes.byref(pv),
        ctypes.byref(handler),
        ctypes.byref(op_ptr),
    )
    if hr != 0:
        raise RuntimeError("ActivateAudioInterfaceAsync: 0x%08X" % (hr & 0xFFFFFFFF))

    if not done.wait(10):
        raise RuntimeError("Timeout waiting for ProcTap activation")

    # GetActivateResult (vtbl index 3 on IActivateAudioInterfaceAsyncOperation)
    act_hr = ctypes.c_long()
    act_intf = ctypes.c_void_p()
    hr2 = call_com(
        result[0],
        3,
        ctypes.c_long,
        (ctypes.POINTER(ctypes.c_long), ctypes.byref(act_hr)),
        (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(act_intf)),
    )
    if hr2 != 0:
        raise RuntimeError("GetActivateResult call: 0x%08X" % (hr2 & 0xFFFFFFFF))
    if act_hr.value != 0:
        raise RuntimeError("Activation result: 0x%08X" % (act_hr.value & 0xFFFFFFFF))

    gc_refs = (qi_f, ar_f, rl_f, ac_f, vtbl, handler, buf, pv)
    return act_intf.value, gc_refs


def activate_system_loopback() -> tuple[int, tuple]:
    """Activate standard WASAPI loopback on the default render endpoint.

    Returns:
        Tuple of (client_ptr, gc_refs). System loopback does not need extra
        GC roots, so ``gc_refs`` is an empty tuple for API compatibility.

    Raises:
        RuntimeError: If endpoint enumeration or IAudioClient activation fails.
    """
    co_create_instance = ctypes.windll.ole32.CoCreateInstance
    co_create_instance.restype = ctypes.c_long
    co_create_instance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]

    clsid = make_guid(CLSID_MMDEVICEENUMERATOR)
    enum_iid = make_guid(IID_IMMDEVICEENUMERATOR)
    client_iid = make_guid(IID_IAUDIOCLIENT)

    enum_ptr = ctypes.c_void_p()
    device_ptr = ctypes.c_void_p()
    client_ptr = ctypes.c_void_p()

    hr = co_create_instance(
        ctypes.byref(clsid),
        None,
        CLSCTX_INPROC_SERVER,
        ctypes.byref(enum_iid),
        ctypes.byref(enum_ptr),
    )
    if hr != 0:
        raise RuntimeError("CoCreateInstance(MMDeviceEnumerator): 0x%08X" % (hr & 0xFFFFFFFF))

    try:
        hr = call_com(
            enum_ptr.value,
            4,
            ctypes.c_long,
            (ctypes.c_int, E_RENDER),
            (ctypes.c_int, E_CONSOLE),
            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(device_ptr)),
        )
        if hr != 0:
            raise RuntimeError("IMMDeviceEnumerator.GetDefaultAudioEndpoint: 0x%08X" % (hr & 0xFFFFFFFF))

        hr = call_com(
            device_ptr.value,
            3,
            ctypes.c_long,
            (ctypes.POINTER(GUID), ctypes.byref(client_iid)),
            (ctypes.c_uint, CLSCTX_INPROC_SERVER),
            (ctypes.c_void_p, None),
            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(client_ptr)),
        )
        if hr != 0:
            raise RuntimeError("IMMDevice.Activate(IAudioClient): 0x%08X" % (hr & 0xFFFFFFFF))
    finally:
        if device_ptr.value:
            try:
                call_com(device_ptr.value, 2, ctypes.c_ulong)
            except Exception:
                logger.debug("COM Release(device_ptr) failed during cleanup")
        if enum_ptr.value:
            try:
                call_com(enum_ptr.value, 2, ctypes.c_ulong)
            except Exception:
                logger.debug("COM Release(enum_ptr) failed during cleanup")

    return client_ptr.value, ()


def start_capture(client_ptr: int) -> tuple[int, int, int]:
    """Initialize IAudioClient for capture and return IAudioCaptureClient pointer.

    For system loopback, WASAPI shared mode requires the audio engine's
    exact mix format.  GetMixFormat typically returns WAVEFORMATEXTENSIBLE
    (tag 0xFFFE) which includes a SubFormat GUID and channel mask.  We
    must pass that structure *as-is* to Initialize — creating a plain
    WAVEFORMATEX from the fields loses the extensible data and causes
    Initialize to succeed but capture to return silence.

    ProcTap's per-process loopback returns E_NOTIMPL for GetMixFormat,
    so we fall back to probing 44100/48000 Hz formats directly.

    Args:
        client_ptr: Pointer to the activated IAudioClient.

    Returns:
        Tuple of (capture_client_ptr, bits_per_sample, sample_rate).

    Raises:
        RuntimeError: If Initialize or GetService fails for all formats.
    """
    WAVE_FORMAT_IEEE_FLOAT = 0x0003
    WAVE_FORMAT_PCM = 0x0001

    # Step 1: Try GetMixFormat and pass the pointer directly to Initialize.
    # This preserves WAVEFORMATEXTENSIBLE (tag 0xFFFE) with its SubFormat
    # GUID and channel mask, which WASAPI shared-mode loopback requires.
    mix_fmt_ptr = ctypes.c_void_p()
    mix_hr = call_com(
        client_ptr,
        8,  # IAudioClient::GetMixFormat (vtbl 8)
        ctypes.c_long,
        (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(mix_fmt_ptr)),
    )

    actual_bps = 0
    actual_sr = 0

    if mix_hr == 0 and mix_fmt_ptr.value:
        # GetMixFormat succeeded — pass the original pointer to Initialize.
        # Read fields for return values BEFORE Initialize (pointer stays valid).
        mix_fmt = ctypes.cast(mix_fmt_ptr.value, ctypes.POINTER(WAVEFORMATEX)).contents
        actual_sr = mix_fmt.nSamplesPerSec
        actual_bps = mix_fmt.wBitsPerSample

        hr = call_com(
            client_ptr,
            3,  # IAudioClient::Initialize (vtbl 3)
            ctypes.c_long,
            (ctypes.c_uint, 0),  # AUDCLNT_SHAREMODE_SHARED
            (ctypes.c_uint, AUDCLNT_STREAMFLAGS_LOOPBACK),
            (ctypes.c_longlong, 10_000_000),  # 1 second buffer
            (ctypes.c_longlong, 0),
            (ctypes.c_void_p, mix_fmt_ptr.value),  # pass original pointer
            (ctypes.POINTER(GUID), None),
        )

        # Free COM-allocated memory (safe after Initialize)
        try:
            ctypes.windll.ole32.CoTaskMemFree(mix_fmt_ptr.value)
        except Exception:
            logger.debug("CoTaskMemFree failed during mix format cleanup")

        if hr == 0:
            # GetMixFormat + Initialize succeeded — skip probe loop
            pass
        else:
            # Initialize failed with GetMixFormat result — fall through to probes
            actual_bps = 0
            actual_sr = 0

    # Step 2: Probe formats (fallback for ProcTap where GetMixFormat returns
    # E_NOTIMPL, or if GetMixFormat Initialize failed above)
    if actual_bps == 0:
        probe_formats = [
            (44100, 2, 32, WAVE_FORMAT_IEEE_FLOAT),
            (44100, 2, 16, WAVE_FORMAT_PCM),
            (48000, 2, 32, WAVE_FORMAT_IEEE_FLOAT),
            (48000, 2, 16, WAVE_FORMAT_PCM),
        ]

        last_hr = 0
        for sr, ch, bps, tag in probe_formats:
            ba = ch * (bps // 8)

            fmt = WAVEFORMATEX()
            fmt.wFormatTag = tag
            fmt.nChannels = ch
            fmt.nSamplesPerSec = sr
            fmt.wBitsPerSample = bps
            fmt.nBlockAlign = ba
            fmt.nAvgBytesPerSec = sr * ba
            fmt.cbSize = 0

            hr = call_com(
                client_ptr,
                3,
                ctypes.c_long,
                (ctypes.c_uint, 0),  # AUDCLNT_SHAREMODE_SHARED
                (ctypes.c_uint, AUDCLNT_STREAMFLAGS_LOOPBACK),
                (ctypes.c_longlong, 10_000_000),  # 1 second buffer
                (ctypes.c_longlong, 0),
                (ctypes.POINTER(WAVEFORMATEX), ctypes.pointer(fmt)),
                (ctypes.POINTER(GUID), None),
            )
            if hr == 0:
                actual_bps = bps
                actual_sr = sr
                break
            last_hr = hr
        else:
            raise RuntimeError("IAudioClient.Initialize failed for all formats: 0x%08X" % (last_hr & 0xFFFFFFFF))

    # GetService for IAudioCaptureClient (vtbl 14)
    cap_iid = make_guid(IID_IAUDIOCAPTURECLIENT)
    cap = ctypes.c_void_p()
    hr = call_com(
        client_ptr,
        14,
        ctypes.c_long,
        (ctypes.POINTER(GUID), ctypes.byref(cap_iid)),
        (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(cap)),
    )
    if hr != 0:
        raise RuntimeError("IAudioClient.GetService: 0x%08X" % (hr & 0xFFFFFFFF))

    # Start (vtbl 10)
    call_com(client_ptr, 10, ctypes.c_long)

    return cap.value, actual_bps, actual_sr


__all__ = [
    "AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY",
    "AUDCLNT_BUFFERFLAGS_SILENT",
    "GUID",
    "WAVEFORMATEX",
    "activate_proctap",
    "activate_system_loopback",
    "call_com",
    "make_guid",
    "start_capture",
]
