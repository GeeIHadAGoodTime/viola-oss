"""
ProcTap capture subprocess — runs in a child process to avoid in-process COM issues.

When ProcTap is activated from within the Viola process (which has many COM
objects via pycaw, comtypes, Qt, etc.), the capture client receives near-zero
audio data even though the target process IS playing audio.  A standalone
process using the identical COM activation code captures real audio.

This module provides a subprocess-based capture that:
1. Spawns a child Python process running _proctap_subprocess_worker
2. The worker does COM init, ProcTap activation, capture loop
3. PCM data is sent back via a pipe (multiprocessing.Connection)
4. The parent reads frames and delivers them to the callback

The subprocess approach has been proven to work in standalone tests.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import multiprocessing.connection
import os
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

# NOTE: core.logging_config may not be importable in the subprocess
# because the project root isn't in sys.path at module load time.
# We do a lazy import with fallback.
try:
    from core.logging_config import get_logger

    logger = get_logger(__name__)
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

# Protocol: each message on the pipe is:
#   4 bytes: message type (b'PCM\x00' = audio data, b'LOG\x00' = log, b'ERR\x00' = error)
#   4 bytes: payload length (little-endian uint32)
#   N bytes: payload
MSG_PCM = b"PCM\x00"
MSG_LOG = b"LOG\x00"
MSG_ERR = b"ERR\x00"
MSG_RMS = b"RMS\x00"  # RMS metric update
MSG_DIE = b"DIE\x00"  # Shutdown signal (parent -> child)
MSG_FMT = b"FMT\x00"  # Format info (child -> parent): uint16 bps
MSG_DISCONT = b"DSC\x00"  # WASAPI data discontinuity count (child -> parent): uint32

_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_PROC_THREAD_ATTRIBUTE_PARENT_PROCESS = 0x00020000
_PROCESS_CREATE_PROCESS = 0x0080
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF

# Capture parameters — defaults; actual format determined at runtime
PROCTAP_RATE = 44100
PROCTAP_CHANNELS = 2
PROCTAP_BPS = 16  # default; may be 32 (float32) at runtime
PROCTAP_BLOCK_ALIGN = PROCTAP_CHANNELS * (PROCTAP_BPS // 8)

# How many 44100-Hz frames to accumulate before sending (≈20ms worth)
# 882 frames at 44100 Hz = ~20ms = one output chunk after resampling to 960@48k
FRAMES_PER_SEND = 882

_SENTRY_INIT_STARTED = False


def _start_sentry_init_background(entry_point: str) -> None:
    """Initialize crash reporting WITHOUT ever delaying worker readiness.

    Sentry init must not be able to block ANY worker deadline. With the local
    viola-sentry Docker stack wedged, a synchronous ``init_sentry`` blocked
    22.4s (measured 2026-07-02) — past the launcher's 15s ready window — so
    every capture launch timed out and the hub broadcast silence-flagged
    frames to all spokes forever. This is the same disease the 2026-07-01
    connect-first fix cured one stage earlier (imports before dial-back).

    The init (including its import chain) runs on a daemon thread: the
    capture path proceeds straight to COM activation and the ready signal.
    Coverage trade-off: exceptions raised before the background init finishes
    (sub-second on a healthy stack) are not captured by Sentry, but every
    worker failure is already relayed to the parent hub over the pipe as
    MSG_ERR and logged/reported there — and when Sentry is wedged, a blocking
    init would not have reported anything either. Idempotent per process.
    """
    global _SENTRY_INIT_STARTED
    if _SENTRY_INIT_STARTED:
        return
    _SENTRY_INIT_STARTED = True

    def _init() -> None:
        try:
            # Import inside the thread: the sentry import chain itself has
            # been measured in the seconds range on a loaded machine.
            from core.exceptions import ViolaError
            from services.sentry_init import init_sentry
        except ImportError:
            logger.debug("ProcTap: background Sentry init imports unavailable", exc_info=True)
            return
        try:
            init_sentry(entry_point)
        except (ViolaError, ImportError, OSError, RuntimeError, TypeError, ValueError, AttributeError):
            logger.debug("ProcTap: background Sentry init failed", exc_info=True)

    threading.Thread(target=_init, name="proctap-sentry-init", daemon=True).start()


def _configure_windows_no_console_spawn() -> None:
    """Use pythonw.exe for multiprocessing children so ProcTap has no console."""
    if sys.platform != "win32":
        return

    from pathlib import Path

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        logger.warning("ProcTap: pythonw.exe not found next to %s; child may allocate a console", sys.executable)
        return

    multiprocessing.set_executable(str(pythonw))


class _Win32ProcTapWorker:
    """Minimal process wrapper for a worker launched through CreateProcessW."""

    def __init__(self, pid: int, process_handle: int) -> None:
        self.pid = pid
        self._process_handle = process_handle

    def is_alive(self) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(self._process_handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE

    def join(self, timeout: float | None = None) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        timeout_ms = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        kernel32.WaitForSingleObject(self._process_handle, timeout_ms)

    def kill(self) -> None:
        if self.is_alive():
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateProcess(self._process_handle, 1)

    def close(self) -> None:
        if self._process_handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle(self._process_handle)
            self._process_handle = 0


if sys.platform == "win32":
    from ctypes import wintypes

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", _STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]


def _raise_last_win32_error(action: str) -> None:
    err = ctypes.get_last_error()
    raise OSError(err, "%s failed with Win32 error %d" % (action, err))


def _pythonw_executable() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    logger.warning("ProcTap: pythonw.exe not found next to %s; worker may allocate a console", sys.executable)
    return sys.executable


# The installed (PyInstaller) app has no python/pythonw.exe and cannot run a
# .py file: ``ViolaApp.exe <script.py>`` just boots a second Viola. Its entry
# script emulates ``python -c CODE`` instead (viola_qt._run_frozen_dash_c_and_exit),
# so the worker is started as ``ViolaApp.exe -c <this> --port N --pid P``.
_FROZEN_WORKER_CODE = "from audio_core.capture._proctap_worker_entry import main; main()"


def _worker_command(*, listener_port: int, target_pid: int, use_system_loopback: bool) -> list[str]:
    """Command line that starts the ProcTap worker in this kind of install."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "-c", _FROZEN_WORKER_CODE]
    else:
        cmd = [_pythonw_executable(), str(Path(__file__).with_name("_proctap_worker_entry.py"))]
    cmd += ["--port", str(listener_port), "--pid", str(target_pid)]
    if use_system_loopback:
        cmd.append("--system-loopback")
    return cmd


def _process_creation_time(kernel32: ctypes.WinDLL, pid: int) -> int | None:
    """Creation time (FILETIME ticks) of a RUNNING process, or None.

    None when the process is gone, cannot be opened, or has already exited.
    """
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != _STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = times[0]
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def _shell_process_id() -> int | None:
    """PID of the interactive desktop shell (explorer.exe), or None."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetShellWindow.restype = wintypes.HWND
        hwnd = user32.GetShellWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except (AttributeError, OSError):
        return None


def _reparent_candidates(kernel32: ctypes.WinDLL, own_pid: int) -> list[tuple[int, str]]:
    """Processes the worker may be re-parented under, best first.

    The worker must sit OUTSIDE the tapped process tree (see
    _proctap_worker_entry). Our own parent is the natural choice from a dev
    launcher, but an installed build is started by the tiny Viola.exe shim (or
    Velopack), which exits right after starting ViolaApp.exe. Its PID is then
    stale (gone, or reused by an unrelated process) and
    OpenProcess(PROCESS_CREATE_PROCESS) on it failed on every attempt (1.0.5
    laptop: 159 failures, multiroom speakers got only silence). So our parent
    is used only while it is still running and older than us, and the user's
    desktop shell is the fallback.
    """
    candidates: list[tuple[int, str]] = []
    own_created = _process_creation_time(kernel32, own_pid)
    parent_pid = os.getppid()
    if parent_pid and parent_pid != own_pid:
        parent_created = _process_creation_time(kernel32, parent_pid)
        if parent_created is not None and (own_created is None or parent_created <= own_created):
            candidates.append((parent_pid, "launcher"))
        else:
            logger.debug("ProcTap: launcher PID %d has exited (or was reused); not re-parenting under it", parent_pid)
    shell_pid = _shell_process_id()
    if shell_pid and shell_pid != own_pid and all(pid != shell_pid for pid, _ in candidates):
        candidates.append((shell_pid, "desktop shell"))
    return candidates


def _open_reparent_target(kernel32: ctypes.WinDLL) -> tuple[int, int, str]:
    """Open a parent for the worker: ``(handle, pid, label)``. Raises OSError if none."""
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    failures: list[str] = []
    for pid, label in _reparent_candidates(kernel32, os.getpid()):
        handle = kernel32.OpenProcess(_PROCESS_CREATE_PROCESS, False, pid)
        if handle:
            return int(handle), pid, label
        failures.append("%s pid %d: Win32 error %d" % (label, pid, ctypes.get_last_error()))
    detail = "; ".join(failures) if failures else "no running launcher or desktop shell"
    raise OSError(0, "OpenProcess(PROCESS_CREATE_PROCESS) found no usable parent for the ProcTap worker (%s)" % detail)


def _accept_worker_connection(
    listener: multiprocessing.connection.Listener,
    timeout_sec: float,
) -> multiprocessing.connection.Connection:
    raw_listener = getattr(listener, "_listener", None)
    listener_socket = getattr(raw_listener, "_socket", None)
    if listener_socket is not None:
        listener_socket.settimeout(timeout_sec)
    try:
        return listener.accept()
    except TimeoutError as exc:
        raise TimeoutError("timed out waiting for ProcTap worker to connect") from exc


def _create_reparented_worker_process(
    *,
    target_pid: int,
    listener_port: int,
    use_system_loopback: bool,
) -> _Win32ProcTapWorker:
    if sys.platform != "win32":
        raise RuntimeError("reparented ProcTap worker launch is Windows-only")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    parent_handle, parent_pid, parent_label = _open_reparent_target(kernel32)

    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    attr_size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
    attr_buf = ctypes.create_string_buffer(attr_size.value)
    attr_ptr = ctypes.cast(attr_buf, ctypes.c_void_p)
    startup = _STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(startup)
    startup.lpAttributeList = attr_ptr
    proc_info = _PROCESS_INFORMATION()

    try:
        if not kernel32.InitializeProcThreadAttributeList(attr_ptr, 1, 0, ctypes.byref(attr_size)):
            _raise_last_win32_error("InitializeProcThreadAttributeList")

        parent_handle_value = wintypes.HANDLE(parent_handle)
        if not kernel32.UpdateProcThreadAttribute(
            attr_ptr,
            0,
            _PROC_THREAD_ATTRIBUTE_PARENT_PROCESS,
            ctypes.byref(parent_handle_value),
            ctypes.sizeof(parent_handle_value),
            None,
            None,
        ):
            _raise_last_win32_error("UpdateProcThreadAttribute(PARENT_PROCESS)")

        cmd = _worker_command(
            listener_port=listener_port,
            target_pid=target_pid,
            use_system_loopback=use_system_loopback,
        )
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
        cwd = str(Path(__file__).resolve().parents[2])

        ok = kernel32.CreateProcessW(
            None,
            cmdline,
            None,
            None,
            False,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW,
            None,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(proc_info),
        )
        if not ok:
            _raise_last_win32_error("CreateProcessW")

        kernel32.CloseHandle(proc_info.hThread)
        logger.info(
            "ProcTap: launched reparented worker pid=%d target_pid=%d parent_pid=%d (%s)",
            proc_info.dwProcessId,
            target_pid,
            parent_pid,
            parent_label,
        )
        return _Win32ProcTapWorker(int(proc_info.dwProcessId), int(proc_info.hProcess))
    finally:
        if startup.lpAttributeList:
            kernel32.DeleteProcThreadAttributeList(startup.lpAttributeList)
        kernel32.CloseHandle(parent_handle)


def _start_worker_process(
    pid: int,
    use_system_loopback: bool,
) -> tuple[
    _Win32ProcTapWorker | multiprocessing.Process,
    multiprocessing.connection.Connection,
]:
    if sys.platform != "win32":
        parent_conn, child_conn = multiprocessing.Pipe(duplex=True)
        process = multiprocessing.Process(
            target=_worker_main,
            args=(child_conn, pid, use_system_loopback),
            daemon=True,
            name="proctap-subprocess",
        )
        process.start()
        child_conn.close()
        return process, parent_conn

    listener = multiprocessing.connection.Listener(("127.0.0.1", 0), authkey=None)
    worker: _Win32ProcTapWorker | None = None
    try:
        listener_port = int(listener.address[1])
        worker = _create_reparented_worker_process(
            target_pid=pid,
            listener_port=listener_port,
            use_system_loopback=use_system_loopback,
        )
        try:
            from core.win32_job import assign_to_lifetime_job

            assign_to_lifetime_job(worker.pid)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.debug("ProcTap: lifetime job assignment unavailable for worker pid=%d: %s", worker.pid, exc)
        # 30s margin: the worker dials back before its heavy imports, so it
        # normally connects in <2s — but a cold venv on a loaded machine has
        # been measured at 13.8s end-to-end (2026-07-01). 10s made every
        # launch time out and the hub broadcast silence to all spokes.
        conn = _accept_worker_connection(listener, timeout_sec=30.0)
        return worker, conn
    except (EOFError, OSError, RuntimeError, TimeoutError, ValueError):
        if worker is not None:
            worker.kill()
            worker.join(timeout=2.0)
            worker.close()
        raise
    finally:
        listener.close()


_LAUNCH_FAILURES_LOGGED = 0


def _log_launch_failure(exc: BaseException) -> None:
    """Log a worker launch refusal: in full the 1st, 2nd, 4th, 8th ... time, quietly otherwise."""
    global _LAUNCH_FAILURES_LOGGED
    _LAUNCH_FAILURES_LOGGED += 1
    count = _LAUNCH_FAILURES_LOGGED
    if count & (count - 1) == 0:
        logger.error("ProcTap subprocess launch failed (%d so far): %s", count, exc)
    else:
        logger.debug("ProcTap subprocess launch failed (%d so far): %s", count, exc)


def _worker_main(
    conn: multiprocessing.connection.Connection,
    pid: int,
    use_system_loopback: bool = False,
) -> None:
    """ProcTap capture worker — runs in a child process.

    Captures audio from *pid* and sends PCM frames over *conn*.
    Exits when *conn* is closed by the parent or on error.

    Args:
        conn: Pipe connection for sending data back to parent.
        pid: Target process ID to capture audio from.
    """
    import pathlib

    project_root = str(pathlib.Path(__file__).resolve().parent.parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Crash reporting runs on a side thread — a wedged Sentry backend must
    # not delay activation or the ready signal (see _start_sentry_init_background).
    _start_sentry_init_background("audio_core.capture._proctap_subprocess")

    import ctypes
    import math

    import numpy as np

    # COM must be initialized in this fresh process
    ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED

    # Import the COM layer — runs in this clean process
    # We need to add the project root to sys.path
    try:
        from audio_core.capture._proctap_com import (
            AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY,
            AUDCLNT_BUFFERFLAGS_SILENT,
            activate_proctap,
            activate_system_loopback,
            call_com,
            start_capture,
        )
    except Exception as e:
        conn.send_bytes(MSG_ERR + struct.pack("<I", len(str(e).encode())) + str(e).encode())
        conn.close()
        return

    capture_mode = "system-loopback" if use_system_loopback else "per-process"

    # Activate capture
    try:
        if use_system_loopback:
            client_ptr, gc_refs = activate_system_loopback()
        else:
            client_ptr, gc_refs = activate_proctap(pid)
        cap_ptr, actual_bps, actual_sr = start_capture(client_ptr)
    except RuntimeError as e:
        msg = "Activation failed for PID %d (%s): %s" % (pid, capture_mode, e)
        conn.send_bytes(MSG_ERR + struct.pack("<I", len(msg.encode())) + msg.encode())
        conn.close()
        return

    def _log(msg: str, *args: object) -> None:
        """Send a log message to the parent process over the pipe."""
        text = msg % args if args else msg
        payload = text.encode()
        try:
            conn.send_bytes(MSG_LOG + struct.pack("<I", len(payload)) + payload)
        except OSError:
            return

    # Send format info (bps + sample rate) THEN ready signal
    fmt_payload = struct.pack("<HI", actual_bps, actual_sr)
    conn.send_bytes(MSG_FMT + struct.pack("<I", len(fmt_payload)) + fmt_payload)
    ready_payload = capture_mode.encode()
    conn.send_bytes(MSG_LOG + struct.pack("<I", len(ready_payload)) + ready_payload)

    # Compute send threshold using actual sample rate
    frames_per_send = round(960 * actual_sr / 48000)  # 882@44100, 960@48000
    accumulator = bytearray()
    block_align = PROCTAP_CHANNELS * (actual_bps // 8)
    send_threshold = frames_per_send * block_align
    rms_counter = 0
    is_float32 = actual_bps == 32
    discont_count = 0

    try:
        while True:
            # Check for shutdown signal from parent (non-blocking)
            if conn.poll(0):
                try:
                    data = conn.recv_bytes()
                    if data[:4] == MSG_DIE:
                        break
                except (EOFError, OSError):
                    break

            try:
                # GetNextPacketSize (vtbl 5)
                ps = ctypes.c_uint32()
                call_com(
                    cap_ptr,
                    5,
                    ctypes.c_long,
                    (ctypes.POINTER(ctypes.c_uint32), ctypes.byref(ps)),
                )

                while ps.value > 0:
                    dp = ctypes.c_void_p()
                    fa = ctypes.c_uint32()
                    fl = ctypes.c_uint32()
                    dv = ctypes.c_uint64()
                    qp = ctypes.c_uint64()

                    call_com(
                        cap_ptr,
                        3,
                        ctypes.c_long,
                        (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(dp)),
                        (ctypes.POINTER(ctypes.c_uint32), ctypes.byref(fa)),
                        (ctypes.POINTER(ctypes.c_uint32), ctypes.byref(fl)),
                        (ctypes.POINTER(ctypes.c_uint64), ctypes.byref(dv)),
                        (ctypes.POINTER(ctypes.c_uint64), ctypes.byref(qp)),
                    )

                    nf = fa.value

                    # Check for WASAPI data discontinuity (buffer overrun)
                    if fl.value & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                        discont_count += 1
                        if discont_count <= 10 or discont_count % 100 == 0:
                            _log(
                                "DISCONT#%d: nf=%d flags=0x%x",
                                discont_count,
                                nf,
                                fl.value,
                            )

                    if nf > 0:
                        if fl.value & AUDCLNT_BUFFERFLAGS_SILENT:
                            accumulator.extend(b"\x00" * (nf * block_align))
                        else:
                            accumulator.extend(ctypes.string_at(dp.value, nf * block_align))

                    # ReleaseBuffer (vtbl 4)
                    call_com(cap_ptr, 4, ctypes.c_long, (ctypes.c_uint32, nf))

                    # Next packet
                    call_com(
                        cap_ptr,
                        5,
                        ctypes.c_long,
                        (ctypes.POINTER(ctypes.c_uint32), ctypes.byref(ps)),
                    )

                # Send accumulated chunks
                while len(accumulator) >= send_threshold:
                    chunk = bytes(accumulator[:send_threshold])
                    del accumulator[:send_threshold]

                    # Send PCM chunk: type + length + data
                    header = MSG_PCM + struct.pack("<I", len(chunk))
                    conn.send_bytes(header + chunk)

                    # Periodic RMS reporting (every ~20 chunks = ~400ms)
                    rms_counter += 1
                    if rms_counter % 20 == 0:
                        if is_float32:
                            samples = np.frombuffer(chunk, dtype=np.float32)
                            rms = float(math.sqrt(np.mean(samples**2)))
                        else:
                            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                            rms = float(math.sqrt(np.mean(samples**2)) / 32767.0)
                        rms_bytes = struct.pack("<f", rms)
                        conn.send_bytes(MSG_RMS + struct.pack("<I", 4) + rms_bytes)

                        # Send discontinuity count alongside RMS
                        discont_bytes = struct.pack("<I", discont_count)
                        conn.send_bytes(MSG_DISCONT + struct.pack("<I", 4) + discont_bytes)

            except OSError:
                # Pipe broken — parent closed
                break
            except Exception as e:
                msg = "Capture error: %s" % e
                try:
                    conn.send_bytes(MSG_ERR + struct.pack("<I", len(msg.encode())) + msg.encode())
                except OSError:
                    return
                break

            time.sleep(0.005)  # 5ms poll interval

    finally:
        # Stop capture
        try:
            call_com(client_ptr, 11, ctypes.c_long)
        except Exception:
            logger.debug("COM Stop(client_ptr) failed during cleanup")
        _ = gc_refs  # prevent GC

        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            logger.debug("CoUninitialize failed during cleanup")

        try:
            conn.close()
        except Exception:
            logger.debug("Pipe connection close failed during cleanup")


class SubprocessProcTap:
    """Manages a ProcTap capture subprocess.

    Spawns a child process for ProcTap capture and reads PCM data
    from it via a pipe.  Delivers chunks to a registered callback.
    """

    def __init__(self) -> None:
        self._process: _Win32ProcTapWorker | multiprocessing.Process | None = None
        self._parent_conn: multiprocessing.connection.Connection | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._callback: Callable[[bytes, int, int, int], None] | None = None
        self._last_rms: float = 0.0
        self._chunks_received: int = 0
        self._actual_bps: int = PROCTAP_BPS  # updated by MSG_FMT from child
        self._actual_sr: int = PROCTAP_RATE  # updated by MSG_FMT from child
        self._discont_count: int = 0
        self._lock = threading.Lock()
        # Why the last start() returned False: "launch" (the OS would not start
        # the worker) or "ready" (it started but never became ready).
        self.last_start_failure: str | None = None

    def set_callback(self, fn: Callable[[bytes, int, int, int], None]) -> None:
        self._callback = fn

    def start(self, pid: int, *, use_system_loopback: bool = False) -> bool:
        """Start capture subprocess for the given PID.

        Returns True if the subprocess started and sent the ready signal.
        """
        if self._process is not None:
            self.stop()

        self._stop_event.clear()
        self.last_start_failure = None
        try:
            if sys.platform != "win32":
                _configure_windows_no_console_spawn()
            self._process, parent_conn = _start_worker_process(pid, use_system_loopback)
            self._parent_conn = parent_conn
        except TimeoutError as exc:
            # Started but never dialled back: a readiness problem, not a refusal.
            self.last_start_failure = "ready"
            logger.error("ProcTap subprocess launch failed: %s", exc)
            return False
        except (EOFError, OSError, RuntimeError, ValueError) as exc:
            self.last_start_failure = "launch"
            # The provider rate-limits the repeat of this; log the cause in full.
            _log_launch_failure(exc)
            return False

        # Wait for the ready signals: MSG_FMT (format) then MSG_LOG (ready)
        try:
            # Read up to 2 messages within the timeout
            deadline = 15.0
            got_ready = False
            while not got_ready:
                if not parent_conn.poll(timeout=deadline):
                    logger.error("ProcTap subprocess: timeout waiting for ready signal")
                    self.last_start_failure = "ready"
                    self.stop()
                    return False
                data = parent_conn.recv_bytes()
                msg_type = data[:4]
                if msg_type == MSG_ERR:
                    payload_len = struct.unpack("<I", data[4:8])[0]
                    err_msg = data[8 : 8 + payload_len].decode()
                    logger.error("ProcTap subprocess failed: %s", err_msg)
                    self.stop()
                    return False
                if msg_type == MSG_FMT:
                    if len(data) >= 14:
                        # Extended format: bps (uint16) + sample_rate (uint32)
                        self._actual_bps, self._actual_sr = struct.unpack("<HI", data[8:14])
                    elif len(data) >= 10:
                        self._actual_bps = struct.unpack("<H", data[8:10])[0]
                    deadline = 5.0  # shorter timeout for LOG after FMT
                elif msg_type == MSG_LOG:
                    got_ready = True
                else:
                    logger.warning("ProcTap subprocess: unexpected message type during startup")
            fmt_label = "float32" if self._actual_bps == 32 else "int16"
            mode_label = "system loopback" if use_system_loopback else "per-process"
            logger.info(
                "ProcTap subprocess started for PID %d (%s, capture: %s)",
                pid,
                mode_label,
                fmt_label,
            )
        except (EOFError, OSError) as e:
            logger.error("ProcTap subprocess pipe error: %s", e)
            self.stop()
            return False

        # Start reader thread
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name="proctap-pipe-reader",
            daemon=True,
        )
        self._reader_thread.start()
        return True

    def stop(self) -> None:
        """Stop the capture subprocess and reader thread."""
        self._stop_event.set()

        # Signal child to exit
        if self._parent_conn is not None:
            try:
                self._parent_conn.send_bytes(MSG_DIE + struct.pack("<I", 0))
            except (OSError, EOFError) as exc:
                logger.debug("ProcTap subprocess shutdown signal skipped: %s", exc)

        # Wait for process to exit
        if self._process is not None:
            self._process.join(timeout=5.0)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=2.0)
            if hasattr(self._process, "close"):
                self._process.close()
            self._process = None

        # Close pipe
        if self._parent_conn is not None:
            try:
                self._parent_conn.close()
            except OSError as exc:
                logger.debug("ProcTap subprocess pipe close failed: %s", exc)
            self._parent_conn = None

        # Wait for reader thread
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3.0)
            self._reader_thread = None

        with self._lock:
            logger.info(
                "ProcTap subprocess stopped: chunks_received=%d",
                self._chunks_received,
            )

    @property
    def sample_width(self) -> int:
        """Actual sample width in bytes (2 for int16, 4 for float32)."""
        return self._actual_bps // 8

    @property
    def sample_rate(self) -> int:
        """Actual capture sample rate (44100 or 48000)."""
        return self._actual_sr

    def get_rms(self) -> float:
        with self._lock:
            return self._last_rms

    def get_chunks(self) -> int:
        with self._lock:
            return self._chunks_received

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _read_loop(self) -> None:
        """Read PCM frames from the subprocess pipe and deliver to callback."""
        conn = self._parent_conn
        if conn is None:
            return

        while not self._stop_event.is_set():
            try:
                if not conn.poll(timeout=0.5):
                    # Check if subprocess died
                    if self._process is not None and not self._process.is_alive():
                        logger.warning("ProcTap subprocess exited unexpectedly")
                        break
                    continue

                data = conn.recv_bytes()
                if len(data) < 8:
                    continue

                msg_type = data[:4]
                payload_len = struct.unpack("<I", data[4:8])[0]
                payload = data[8 : 8 + payload_len]

                if msg_type == MSG_PCM:
                    with self._lock:
                        self._chunks_received += 1
                    if self._callback is not None:
                        self._callback(
                            payload,
                            self._actual_sr,
                            PROCTAP_CHANNELS,
                            self._actual_bps // 8,
                        )

                elif msg_type == MSG_RMS:
                    if len(payload) >= 4:
                        rms = struct.unpack("<f", payload[:4])[0]
                        with self._lock:
                            self._last_rms = rms

                elif msg_type == MSG_DISCONT:
                    if len(payload) >= 4:
                        count = struct.unpack("<I", payload[:4])[0]
                        with self._lock:
                            self._discont_count = count

                elif msg_type == MSG_LOG:
                    if payload:
                        logger.info("ProcTap subprocess: %s", payload.decode(errors="replace"))

                elif msg_type == MSG_ERR:
                    logger.error(
                        "ProcTap subprocess error: %s",
                        payload.decode(errors="replace"),
                    )
                    break

            except (EOFError, OSError):
                logger.info("ProcTap subprocess pipe closed")
                break
            except Exception:
                logger.exception("ProcTap pipe reader error")
                break


__all__ = ["SubprocessProcTap"]
