"""Win32 Job Object helper — bind subprocesses to Viola's process lifetime.

Solves the orphaned-Chrome bug: Spotify CDP launches Chrome via ``subprocess.Popen``
which has no inherent tie to Viola's lifetime. If Viola crashes or its API
wedges, Chrome keeps rendering audio forever, the user has no UI to stop it,
and ProcTap subprocess plus other workers go zombie. The user discovered this
the hard way at 15:47 on 2026-05-07 — Viola died, music played for 12 more
minutes from an orphan Chrome (PID 57252) before being noticed.

The fix: create a Win32 Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``,
keep the handle open for the lifetime of the Viola process, and assign each
spawned subprocess to it. When Viola exits (clean OR crash), the OS closes
its handle, the Job's last reference drops, the Job is destroyed, and all
processes assigned to it are forcibly terminated by the kernel.

Usage::

    from core.win32_job import get_lifetime_job, assign_to_lifetime_job

    proc = subprocess.Popen([...])
    assign_to_lifetime_job(proc.pid)

The helper is a no-op on non-Windows platforms.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ``ctypes.wintypes`` only exists on Windows and raises ImportError on Linux/macOS,
# so importing it at module top would break ``import core.win32_job`` on those
# platforms even though every public function below already no-ops when
# ``sys.platform != "win32"``. Bind it lazily on Windows only; non-Windows keeps
# a ``None`` placeholder that the (unreachable-off-Windows) call sites never use.
if sys.platform == "win32":
    from ctypes import wintypes
else:  # pragma: no cover - exercised on Linux/macOS only
    wintypes = None  # type: ignore[assignment]

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_lifetime_job_handle: int | None = None
_lifetime_job_lock = threading.Lock()


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_lifetime_job() -> int | None:
    """Create the Job Object once per Viola process."""
    if sys.platform != "win32":
        return None

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        err = ctypes.GetLastError()
        logger.warning("win32_job: CreateJobObjectW failed err=%d — orphan-cleanup will not work", err)
        return None

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]

    ok = kernel32.SetInformationJobObject(
        job,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        err = ctypes.GetLastError()
        logger.warning("win32_job: SetInformationJobObject failed err=%d — closing handle", err)
        kernel32.CloseHandle(job)
        return None

    logger.info("win32_job: lifetime job created (handle=%d, KILL_ON_JOB_CLOSE armed)", int(job))
    return int(job)


def get_lifetime_job() -> int | None:
    """Return the Viola-lifetime Job Object handle, creating it on first call."""
    global _lifetime_job_handle
    if sys.platform != "win32":
        return None
    with _lifetime_job_lock:
        if _lifetime_job_handle is None:
            _lifetime_job_handle = _create_lifetime_job()
        return _lifetime_job_handle


def assign_to_lifetime_job(pid: int) -> bool:
    """Bind a subprocess PID to the Viola-lifetime job.

    Returns True if the assignment succeeded. False on non-Windows or any
    failure — calling code should treat failure as non-fatal (the subprocess
    will simply not auto-terminate when Viola dies, regressing to legacy
    orphan behavior; better than refusing to launch the subprocess).
    """
    if sys.platform != "win32" or pid <= 0:
        return False

    job = get_lifetime_job()
    if job is None:
        return False

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    proc_handle = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
        False,
        pid,
    )
    if not proc_handle:
        err = ctypes.GetLastError()
        logger.warning("win32_job: OpenProcess(pid=%d) failed err=%d", pid, err)
        return False

    try:
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        ok = kernel32.AssignProcessToJobObject(job, proc_handle)
        if not ok:
            err = ctypes.GetLastError()
            # Common: ERROR_ACCESS_DENIED (5) when the target is already in
            # a Job that doesn't allow nesting. Modern Windows allows nested
            # jobs, but third-party software can still create un-nestable jobs.
            logger.warning(
                "win32_job: AssignProcessToJobObject(pid=%d) failed err=%d — subprocess will orphan if Viola dies",
                pid,
                err,
            )
            return False
        logger.info("win32_job: bound pid=%d to lifetime job — will die with Viola", pid)
        return True
    finally:
        kernel32.CloseHandle(proc_handle)
