"""Self-documenting native-crash artifacts (#4650).

WHY THIS EXISTS
---------------
Three Viola desktop hard crashes on 2026-08-01/02 (access violations at
``0x00007FFF5CF02A5A`` reading ``0xEC``, the same address again, and
``0x00007FFECC937AA1`` reading ``0x140``) could not be root-caused from the
artifacts they left behind. Three concrete gaps did that, and this module
closes all three:

1. **Nothing died cleanly.** The faults raised a modal "Viola: python.exe -
   Application Error" hard-error dialog. That dialog SUSPENDS the process, so
   the app sat there looking half-alive, and because nobody acknowledged it the
   Windows Error Reporting bucket never completed -- WER has no record of any
   of the three crashes and no minidump was ever written.
2. **Nothing was dated.** ``faulthandler`` writes its dump with no timestamp
   whatsoever, and every process appended to one shared
   ``logs/faulthandler.log``. 89 access violations accumulated in one file with
   no way to date any of them, so nobody could say which dump belonged to which
   incident or whether a dump predated the PortAudio guard that was supposed to
   have fixed it (docs/INTAKE_CANDIDATES.md, C-801). That single missing fact
   is what made attribution inconclusive.
3. **Nothing was attributable.** A faulting address is a bare number. Without
   the base address of every loaded module, working out which DLL crashed is a
   research project instead of a lookup.

WHAT THIS MODULE DOES
---------------------
``install_crash_forensics(component)`` is called by every Viola process
entrypoint immediately before it enables ``faulthandler``. It:

* opens a **run-scoped** crash log --
  ``<logs>/crash/<component>-<UTC timestamp>-<pid>.faulthandler.log`` -- so the
  filename carries the wall clock, the file's mtime carries the moment of the
  last dump written into it, and no two incidents can ever share a file again;
* writes a **run header** into it (UTC start, component, pid, session id, app
  version, git sha, python, platform, frozen flag) so a dump can always be tied
  to a specific build and a specific run;
* writes a **module map sidecar** (``.modules.json``) listing every loaded
  module's base address, size and path, so a raw faulting address resolves to
  module + offset with ``scripts/diagnostics/resolve_crash_address.py`` -- and
  re-samples it a few times early on, because Qt and the audio stack load after
  boot and are where this app actually crashes;
* stamps a **clean-exit marker** at normal shutdown, which is what separates a
  fatal fault from a survived first-chance one: on Windows faulthandler is a
  vectored handler, so a dump in the file does NOT by itself mean the process
  died (the shared log holds 190 survived ``RPC_E_DISCONNECTED`` blocks);
* on Windows, **stops the crash from suspending the process behind an
  unattended modal dialog**, while keeping the crash buckettable: WER is put in
  silent queued mode (``WerSetFlags``) so the OS still produces its own
  timestamped report with the faulting module and offset in it, and the
  legacy hard-error message boxes are suppressed via ``SetErrorMode``. If WER
  cannot be reached at all we degrade to ``SEM_NOGPFAULTERRORBOX``, which
  trades the WER bucket for a guaranteed prompt death -- never a hang;
* prunes old run files so the crash directory cannot grow without bound.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It never captures **locals, stack memory, or heap contents**. Payment data
(PAN/CVC), BYOK API keys and OAuth tokens live in this process, so a
conventional minidump-based crash reporter is not an option here. That is a
load-bearing constraint asserted by
``tests/security/test_payment_pan_never_in_exceptions.py`` and by the
``crash-forensics-attributable`` gate: the stack artifact stays stdlib
``faulthandler`` (file/line/function frames only), and the WER flags we set
include ``WER_FAULT_REPORTING_FLAG_NOHEAP`` so even an OS-side report excludes
the heap.

BUY VS BUILD
------------
Google Breakpad / Crashpad are the mature answer to native crash reporting and
were the first thing considered. Both are rejected here for two independent
reasons: they are out-of-process C++ components that would have to be built and
code-signed into the installer for three platforms, and -- decisively -- a
minidump captures thread stack memory by design, which is exactly the
card-data-bearing memory this process is forbidden from writing to disk. The
stdlib ``faulthandler`` already produces the stack artifact we are allowed to
keep; what it lacked was attribution, and attribution is what this module adds.

Windows **LocalDumps** (the ``HKLM\\...\\Windows Error Reporting\\LocalDumps``
registry key, which makes WER drop a minidump per crash) was raised as an
alternative and is declined, for three reasons in increasing order of weight:

* It is HKLM, so it needs admin the fleet does not have, and it is keyed by
  EXECUTABLE NAME. Viola runs as ``python.exe``, so switching it on would
  silently start collecting dumps from every unrelated Python process on the
  user's machine. A shipped desktop app has no business writing machine-wide
  crash policy.
* A minidump carries thread stack memory, i.e. locals, i.e. potentially the
  PAN. ``CustomDumpFlags`` with ``MiniDumpFilterMemory`` could in principle
  strip it, but that would make a PCI-adjacent guarantee depend on a registry
  value any tool or admin can flip to a full dump -- with no code change for a
  gate to catch. A constraint this load-bearing does not get to live in the
  registry.
* It is not needed for the goal. LocalDumps was wanted for module attribution,
  and the module map above delivers exactly that, in-process, on all three
  platforms, with no admin and no memory captured.

The concern behind the LocalDumps suggestion is real and IS addressed, just not
that way: a mechanism that waits for a WER bucket to complete inherits the
original failure, because what blocks completion is a human not clicking a
dialog. Measured on the dev box: the machine-level WER archive holds 121
reports including 18 ``AppCrash_python.exe`` ones, all from 2026-05-11..19, and
86 further reports from other processes arrived between then and 2026-08-04 --
so WER was healthy the whole time and still captured NOTHING for the August
crashes. This module removes the human from that loop entirely (the modal never
appears, so the bucket completes unattended), and, more importantly, its
primary artifacts -- the dated run log and the module map -- are written by us
and do not depend on WER at all. WER is a bonus channel here, never the
load-bearing one.

Everything here is defensive by construction: no import of any Viola subsystem
at module scope beyond ``core.platform``, no threads, no network, and every
optional step wrapped so a failure degrades to a recorded note rather than
taking the boot down. It runs on the startup path of a shipped desktop app.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

__all__ = [
    "CRASH_ARTIFACT_SCHEMA_VERSION",
    "CrashForensics",
    "install_crash_forensics",
    "snapshot_loaded_modules",
    "suppress_native_crash_dialogs",
]

# Bump when the header / sidecar layout changes in a way readers must notice.
CRASH_ARTIFACT_SCHEMA_VERSION = 1

_CRASH_DIR_NAME = "crash"
_LOG_SUFFIX = ".faulthandler.log"
_MODULES_SUFFIX = ".modules.json"

# Header sentinels. The resolver and the gate both key off these exact strings,
# so they are module constants rather than inline literals.
_HEADER_OPEN = "==== VIOLA CRASH RUN (schema %d) ===="
_HEADER_CLOSE = "==== END VIOLA CRASH RUN HEADER ===="
# Written by an atexit hook. Its PRESENCE proves the interpreter shut down
# normally, which in turn proves every dump above it was a SURVIVED
# (first-chance) exception rather than the fault that killed the process. See
# _register_clean_exit_marker.
_CLEAN_EXIT_MARK = "==== VIOLA CRASH RUN ENDED CLEANLY ===="

# How many run files (log + sidecar pairs) to keep per component. A crash loop
# that restarts the app repeatedly must not fill the user's disk, and the
# oldest runs are the least useful.
_DEFAULT_RETENTION = 40
_RETENTION_ENV = "VIOLA_CRASH_RETENTION"

# Escape hatch for a developer who WANTS the modal dialog back (e.g. to attach
# a debugger at the moment of the fault). Off by default: the shipped product
# must never suspend behind a dialog nobody is there to click.
_KEEP_DIALOG_ENV = "VIOLA_CRASH_KEEP_NATIVE_DIALOG"

# Correlates the crash artifacts with the app's other per-run logs/traces when
# the launcher sets it; otherwise a per-process id is generated.
_SESSION_ENV = "VIOLA_SESSION_ID"

# Module-map re-sampling (see _start_module_map_refresher for why it exists).
_REFRESH_DISABLE_ENV = "VIOLA_CRASH_DISABLE_MODULE_REFRESH"
_REFRESH_SCHEDULE_ENV = "VIOLA_CRASH_MODULE_REFRESH_SCHEDULE"
# Lets tests (and an orderly shutdown) end the sampler early. Never set on the
# app's own path -- the sampler retires on its own after the last delay.
_REFRESH_STOP = threading.Event()

# --- Win32 constants (documented values, mirrored so no import is needed) ---
# errhandlingapi.h
_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002
_SEM_NOOPENFILEERRORBOX = 0x8000
# werapi.h
_WER_FAULT_REPORTING_FLAG_NOHEAP = 0x0001
_WER_FAULT_REPORTING_FLAG_QUEUE = 0x0002
_WER_FAULT_REPORTING_FLAG_DISABLE_THREAD_SUSPENSION = 0x0004
_WER_FAULT_REPORTING_ALWAYS_SHOW_UI = 0x0010


@dataclass
class CrashForensics:
    """Everything the caller needs after ``install_crash_forensics``.

    ``stream`` is the file object to hand to ``faulthandler.enable(file=...)``.
    It is opened line-buffered-ish (``buffering=1`` is text-mode line
    buffering) but faulthandler writes to the raw fd anyway, bypassing Python
    buffering entirely -- which is precisely why it survives a hard crash.
    """

    component: str
    stream: IO[str]
    log_path: Path
    modules_path: Path | None
    session_id: str
    started_at: str
    dialog_suppression: str
    module_count: int
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One-line, log-safe description (no %-formatting surprises)."""
        return "crash forensics: log=%s modules=%d dialogs=%s session=%s" % (
            self.log_path,
            self.module_count,
            self.dialog_suppression,
            self.session_id,
        )


# --------------------------------------------------------------------------
# Small helpers. Each one is total: it returns a fallback rather than raising.
# --------------------------------------------------------------------------


def _utc_now() -> tuple[str, str]:
    """Return ``(compact_stamp, iso_stamp)`` in UTC.

    The compact form goes in the filename (filesystem-safe on all three
    platforms); the ISO form goes in the header for humans and parsers.
    """
    now = datetime.now(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _resolve_session_id() -> str:
    configured = os.environ.get(_SESSION_ENV, "").strip()
    if configured:
        # Keep it filename-safe and bounded; the raw value still lands in the
        # header verbatim below.
        return "".join(ch for ch in configured if ch.isalnum() or ch in "-_")[:64] or _random_id()
    return _random_id()


def _random_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _app_version() -> str:
    try:
        from core.constants import VIOLA_VERSION

        return str(VIOLA_VERSION)
    except Exception:  # noqa: BLE001, RUF100 - version is a nicety; never fail boot for it
        return "unknown"


def _git_sha() -> str:
    """Best-effort build identity WITHOUT shelling out to git.

    A subprocess on the startup path is exactly the kind of thing that turns a
    diagnostic into a boot hang, so this only reads files that a build step may
    have left behind. Returns ``"unknown"`` when there is nothing to read --
    that is fine, ``app_version`` already separates release builds.
    """
    try:
        root = Path(__file__).resolve().parents[1]
        stamp = root / "build_sha.txt"
        if stamp.is_file():
            return stamp.read_text(encoding="utf-8", errors="replace").strip()[:64] or "unknown"
        git_dir = root / ".git"
        if git_dir.is_file():
            # A git worktree stores "gitdir: <path>" in a .git FILE rather than
            # a directory. Dev builds run from worktrees, so following it is
            # what makes git_sha resolve at all outside the main checkout.
            pointer = git_dir.read_text(encoding="utf-8", errors="replace").strip()
            if pointer.startswith("gitdir:"):
                git_dir = Path(pointer.split(":", 1)[1].strip())
        head = git_dir / "HEAD"
        if head.is_file():
            ref = head.read_text(encoding="utf-8", errors="replace").strip()
            if ref.startswith("ref: "):
                name = ref[5:].strip()
                # In a worktree, refs/heads/* live in the COMMON dir, not the
                # per-worktree gitdir; try both before giving up.
                for base in (git_dir, git_dir.parent.parent):
                    target = base / name
                    if target.is_file():
                        return target.read_text(encoding="utf-8", errors="replace").strip()[:40]
            elif ref:
                return ref[:40]
    except Exception:  # noqa: BLE001, RUF100 - build identity is a nicety
        return "unknown"
    return "unknown"


# --------------------------------------------------------------------------
# Loaded-module map. This is what turns a bare faulting address into a lookup.
# --------------------------------------------------------------------------


def _windows_modules() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        # Unreachable in practice -- the only caller (snapshot_loaded_modules)
        # already gates on sys.platform before calling in. The guard is
        # repeated HERE, local to this function, because a type checker
        # evaluates a function body on its own: a caller's platform check two
        # frames up does not make ctypes.WinDLL below narrow to "exists" for a
        # checker running on Linux (CI's mypy leg), only a check in the SAME
        # function does. Mirrors the identical pattern already used in
        # suppress_native_crash_dialogs() below.
        return []

    import ctypes
    from ctypes import wintypes

    class _ModuleInfo(ctypes.Structure):
        _fields_ = (
            ("lpBaseOfDll", ctypes.c_void_p),
            ("SizeOfImage", wintypes.DWORD),
            ("EntryPoint", ctypes.c_void_p),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # psapi's EnumProcessModules/GetModuleInformation are forwarded from
    # kernel32 as K32* on Win7+, but psapi.dll is present everywhere Viola runs
    # and keeps the names conventional.
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    proc = kernel32.GetCurrentProcess()

    psapi.EnumProcessModules.restype = wintypes.BOOL
    psapi.EnumProcessModules.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD
    psapi.GetModuleFileNameExW.argtypes = (
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    psapi.GetModuleInformation.restype = wintypes.BOOL
    psapi.GetModuleInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.HMODULE,
        ctypes.POINTER(_ModuleInfo),
        wintypes.DWORD,
    )

    # Two-pass sizing: a Viola process loads a few hundred modules (Qt, ONNX
    # Runtime, PortAudio, the CRT, every Windows shell extension the file
    # dialogs drag in), so a fixed guess would silently truncate the map and
    # produce exactly the "address does not resolve" hole this closes.
    capacity = 512
    for _ in range(4):
        buffer = (wintypes.HMODULE * capacity)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModules(proc, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            return []
        wanted = needed.value // ctypes.sizeof(wintypes.HMODULE)
        if wanted <= capacity:
            handles = list(buffer[:wanted])
            break
        capacity = wanted + 64
    else:
        return []

    name_buffer = ctypes.create_unicode_buffer(32768)
    modules: list[dict[str, Any]] = []
    for handle in handles:
        if not handle:
            continue
        info = _ModuleInfo()
        if not psapi.GetModuleInformation(proc, handle, ctypes.byref(info), ctypes.sizeof(info)):
            continue
        path = ""
        if psapi.GetModuleFileNameExW(proc, handle, name_buffer, len(name_buffer)):
            path = name_buffer.value
        modules.append(
            {
                "base": int(info.lpBaseOfDll or 0),
                "size": int(info.SizeOfImage),
                "path": path,
            }
        )
    return modules


def _linux_modules() -> list[dict[str, Any]]:
    """Collapse ``/proc/self/maps`` to one entry per backing file.

    ``maps`` lists every segment separately (``.text``/``.rodata``/``.data``
    each get a line); the module map wants the load base and the total span, so
    segments are merged per path.
    """
    spans: dict[str, list[int]] = {}
    with open("/proc/self/maps", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split(maxsplit=5)
            if len(parts) < 6:
                continue
            path = parts[5].strip()
            if not path or path.startswith("["):
                continue
            bounds, _, _ = parts[0].partition(" ")
            start_text, _, end_text = bounds.partition("-")
            try:
                start = int(start_text, 16)
                end = int(end_text, 16)
            except ValueError:
                continue
            span = spans.get(path)
            if span is None:
                spans[path] = [start, end]
            else:
                span[0] = min(span[0], start)
                span[1] = max(span[1], end)
    return [{"base": start, "size": end - start, "path": path} for path, (start, end) in spans.items()]


def _macos_modules() -> list[dict[str, Any]]:
    """Walk dyld's image table.

    dyld exposes no image SIZE, so ``size`` stays 0 and the resolver falls back
    to nearest-base attribution (correct in practice: images are laid out in
    ascending order and an address below the next image's base belongs to the
    previous one).
    """
    import ctypes

    libc = ctypes.CDLL(None)
    libc._dyld_image_count.restype = ctypes.c_uint32
    libc._dyld_image_count.argtypes = ()
    libc._dyld_get_image_name.restype = ctypes.c_char_p
    libc._dyld_get_image_name.argtypes = (ctypes.c_uint32,)
    libc._dyld_get_image_header.restype = ctypes.c_void_p
    libc._dyld_get_image_header.argtypes = (ctypes.c_uint32,)

    modules: list[dict[str, Any]] = []
    for index in range(libc._dyld_image_count()):
        header = libc._dyld_get_image_header(index)
        raw_name = libc._dyld_get_image_name(index)
        modules.append(
            {
                "base": int(header or 0),
                "size": 0,
                "path": raw_name.decode("utf-8", "replace") if raw_name else "",
            }
        )
    return modules


def _write_module_map(
    path: Path,
    *,
    component: str,
    session_id: str,
    captured_at: str,
    modules: list[dict[str, Any]],
) -> None:
    """Write the sidecar ATOMICALLY.

    Non-negotiable: this file is rewritten while the process is running, and
    the process may be killed by a native fault at any instant. A partial JSON
    document would destroy the very artifact the crash needs, so the new map is
    written to a temp file and ``os.replace``-d in -- a reader always sees one
    complete map, either the previous one or the new one.
    """
    from datetime import datetime

    payload = {
        "schema_version": CRASH_ARTIFACT_SCHEMA_VERSION,
        "component": component,
        "pid": os.getpid(),
        "session_id": session_id,
        "captured_utc": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "captured_at": captured_at,
        "platform": sys.platform,
        "modules": modules,
    }
    temp_path = path.with_name(path.name + ".tmp%d" % os.getpid())
    temp_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(temp_path, path)


def _start_module_map_refresher(
    path: Path,
    *,
    component: str,
    session_id: str,
    initial_count: int,
) -> bool:
    """Re-snapshot the module map a few times early in the process's life.

    WHY THIS IS NOT OPTIONAL. The map taken at install time is captured before
    Qt, PortAudio and the audio stack have loaded -- and those are precisely
    the DLLs this app crashes in (CL-20260707-1f8a; the C-801 census found 35
    of the 89 dumps inside pyaudio's ``terminate`` and 31 more in the
    wake-detector's device-table walks). A map that omits them cannot resolve
    the addresses that actually matter, which would leave the headline promise
    -- a raw address becomes a module+offset lookup -- unfulfilled for the
    known crash class.

    Modules are essentially never unloaded and never move once loaded, so a
    LATER snapshot is a superset of an earlier one: re-sampling strictly
    improves attribution and can never invalidate it.

    Shape chosen for safety on a shipped app's startup path: a single daemon
    thread that takes a fixed, BOUNDED number of samples and then exits, so
    there is no permanent background work, nothing to leak, and nothing that
    can hold the process open. It never touches shared state, never takes a
    lock the app uses, and its whole body is wrapped -- the worst case is a
    stale-but-complete map, never a hang and never a crash.
    """
    if os.environ.get(_REFRESH_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return False

    delays = _refresh_schedule()
    if not delays:
        return False

    import threading

    def _refresh() -> None:
        previous = initial_count
        for index, delay in enumerate(delays):
            # Event.wait rather than sleep so the interpreter can shut down
            # promptly; the thread is a daemon either way.
            if _REFRESH_STOP.wait(delay):
                return
            try:
                modules = snapshot_loaded_modules()
                if len(modules) <= previous:
                    # Nothing new loaded; the existing map is already a
                    # superset. Skip the write rather than churn the disk.
                    continue
                _write_module_map(
                    path,
                    component=component,
                    session_id=session_id,
                    captured_at="refresh-%d" % (index + 1),
                    modules=modules,
                )
                previous = len(modules)
            except Exception:  # noqa: BLE001, RUF100 - a diagnostic refresh must never disturb the app
                return

    thread = threading.Thread(target=_refresh, name="viola-crash-module-map", daemon=True)
    thread.start()
    return True


def _refresh_schedule() -> list[float]:
    """Delays (seconds) between module-map samples.

    Front-loaded because the DLL set churns hardest during boot and is
    essentially settled after the first few minutes. Empty list disables.
    """
    raw = os.environ.get(_REFRESH_SCHEDULE_ENV, "").strip()
    if not raw:
        return [5.0, 15.0, 40.0, 120.0]
    delays: list[float] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = float(chunk)
        except ValueError:
            return [5.0, 15.0, 40.0, 120.0]
        if value > 0:
            delays.append(value)
    return delays


def snapshot_loaded_modules() -> list[dict[str, Any]]:
    """Return ``[{base, size, path}, ...]`` for every currently loaded module.

    Sorted by base address so the resolver can bisect. Returns ``[]`` -- never
    raises -- on any platform or condition where the walk is unavailable; an
    empty map degrades attribution, it must never degrade the boot.
    """
    try:
        if sys.platform == "win32":
            modules = _windows_modules()
        elif sys.platform == "darwin":
            modules = _macos_modules()
        elif sys.platform.startswith("linux"):
            modules = _linux_modules()
        else:
            return []
    except Exception:  # noqa: BLE001, RUF100 - a diagnostic snapshot must never fail the caller
        return []
    modules.sort(key=lambda entry: entry["base"])
    return modules


# --------------------------------------------------------------------------
# Making the crash die promptly instead of suspending behind a modal dialog.
# --------------------------------------------------------------------------


def suppress_native_crash_dialogs() -> str:
    """Stop a native fault from hanging the process behind an unattended dialog.

    Returns a short machine-readable status describing what was actually
    applied, which is recorded in the crash header so a future reader knows
    which regime produced the artifact:

    ``wer-silent+seterrormode``
        Best case. ``WerSetFlags`` puts Windows Error Reporting into queued
        (no-UI), no-heap, no-thread-suspension mode, so the crash still
        produces a real WER bucket -- with the faulting module and offset the
        OS resolves for us, and a LocalDumps minidump if the machine is
        configured for one -- but no dialog and no suspension.
        ``SetErrorMode`` additionally suppresses the legacy critical-error and
        open-file message boxes, which suspend the process the same way.
    ``seterrormode-nogpfault``
        Degraded fallback used when WER cannot be reached at all. Adds
        ``SEM_NOGPFAULTERRORBOX``, which guarantees the process terminates
        immediately -- at the cost of WER not running, so the local
        faulthandler artifact becomes the only record. A guaranteed clean death
        beats a suspended process every time; that suspension is the whole
        reason the 2026-08-01/02 crashes have no records at all.
    ``disabled-by-env``
        ``VIOLA_CRASH_KEEP_NATIVE_DIALOG=1`` -- a developer asked for the modal
        back so they can attach a debugger at the fault.
    ``not-applicable-<platform>``
        macOS and Linux have no equivalent modal: a fatal signal terminates the
        process and the OS crash reporter (``ReportCrash`` / core dump / the
        journal) records it without human interaction. Nothing to do, and
        nothing raised.
    """
    if os.environ.get(_KEEP_DIALOG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return "disabled-by-env"
    if sys.platform != "win32":
        return "not-applicable-%s" % sys.platform

    import ctypes
    from ctypes import wintypes

    # Suppress the legacy hard-error message boxes ("There is no disk in the
    # drive", a missing-DLL box). These are raised through csrss and suspend
    # the process exactly like the crash dialog does.
    quiet_mode = _SEM_FAILCRITICALERRORS | _SEM_NOOPENFILEERRORBOX
    wer_ok = False
    try:
        # WerSetFlags is exported from kernel32, NOT from wer.dll -- wer.dll
        # carries only the WerReport* family. Loading "wer" here would raise
        # AttributeError and silently drop us into the degraded path, so the
        # DLL choice is load-bearing and verified by the readback below.
        werlib = ctypes.WinDLL("kernel32", use_last_error=True)
        werlib.WerSetFlags.restype = ctypes.HRESULT
        werlib.WerSetFlags.argtypes = (wintypes.DWORD,)
        # NOHEAP keeps card data / tokens out of any OS-side report; QUEUE is
        # the documented "no UI" mode; DISABLE_THREAD_SUSPENSION stops WER
        # freezing the remaining threads while it works.
        flags = (
            _WER_FAULT_REPORTING_FLAG_NOHEAP
            | _WER_FAULT_REPORTING_FLAG_QUEUE
            | _WER_FAULT_REPORTING_FLAG_DISABLE_THREAD_SUSPENSION
        )
        # ctypes raises OSError for a failing HRESULT, so reaching the readback
        # means the call reported success. Trust the readback, not the call:
        # WerGetFlags proves the process really is in silent queued mode, so a
        # future Windows build that quietly ignores a flag downgrades us to the
        # guaranteed-terminate path instead of leaving us believing a lie.
        werlib.WerSetFlags(flags)
        werlib.WerGetFlags.restype = ctypes.HRESULT
        werlib.WerGetFlags.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        werlib.GetCurrentProcess.restype = wintypes.HANDLE
        werlib.GetCurrentProcess.argtypes = ()
        observed = wintypes.DWORD()
        werlib.WerGetFlags(werlib.GetCurrentProcess(), ctypes.byref(observed))
        wer_ok = bool(observed.value & _WER_FAULT_REPORTING_FLAG_QUEUE) and not (
            observed.value & _WER_FAULT_REPORTING_ALWAYS_SHOW_UI
        )
    except Exception:  # noqa: BLE001, RUF100 - degrade to SetErrorMode, never fail boot
        wer_ok = False

    if not wer_ok:
        quiet_mode |= _SEM_NOGPFAULTERRORBOX

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetErrorMode.restype = wintypes.UINT
        kernel32.SetErrorMode.argtypes = (wintypes.UINT,)
        # SetErrorMode REPLACES the process error mode rather than OR-ing into
        # it, so read the current value first and never clear a bit some other
        # component (PyInstaller's bootloader, a vendored native lib) set.
        previous = kernel32.SetErrorMode(0)
        kernel32.SetErrorMode(previous | quiet_mode)
    except Exception:  # noqa: BLE001, RUF100 - diagnostics must never fail boot
        return "wer-silent-only" if wer_ok else "unavailable"

    return "wer-silent+seterrormode" if wer_ok else "seterrormode-nogpfault"


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def _retention_limit() -> int:
    raw = os.environ.get(_RETENTION_ENV, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return _DEFAULT_RETENTION
        # 0 disables pruning entirely (a support engineer collecting a long
        # crash loop); negatives are nonsense and fall back to the default.
        if parsed >= 0:
            return parsed
    return _DEFAULT_RETENTION


def _sidecar_for(log_path: Path) -> Path:
    """Map ``<stem>.faulthandler.log`` to its ``<stem>.modules.json`` sidecar.

    Done by explicit string surgery rather than ``Path.with_suffix`` so a
    component or timestamp containing a dot can never silently retarget the
    sidecar at a different file (a pruning bug that deletes the wrong artifact
    is worse than no pruning).
    """
    name = log_path.name
    stem = name[: -len(_LOG_SUFFIX)] if name.endswith(_LOG_SUFFIX) else name
    return log_path.with_name(stem + _MODULES_SUFFIX)


def _prune_old_runs(crash_dir: Path, component: str, keep: int) -> int:
    """Delete all but the newest ``keep`` run files for ``component``.

    stat-only (no file reads), bounded by the directory listing, and every
    individual deletion is independently guarded -- a locked file (another
    Viola process still holding its own log open) is skipped, not fatal.
    """
    if keep <= 0:
        return 0
    removed = 0
    try:
        candidates = sorted(
            (path for path in crash_dir.glob(component + "-*" + _LOG_SUFFIX) if path.is_file()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return 0
    for stale in candidates[keep:]:
        for victim in (stale, _sidecar_for(stale)):
            try:
                victim.unlink()
                removed += 1
            except OSError:
                continue
    return removed


# --------------------------------------------------------------------------
# The entrypoint
# --------------------------------------------------------------------------


def _open_run_log(component: str, stamp: str) -> tuple[IO[str], Path, list[str]]:
    """Open the run-scoped crash log, degrading through fallback locations.

    Raises only if EVERY location fails, which preserves the caller's existing
    "crash-handler setup" fatal-boot semantics for a genuinely unwritable box.
    """
    notes: list[str] = []
    name = "%s-%s-%d%s" % (component, stamp, os.getpid(), _LOG_SUFFIX)

    candidates: list[Path] = []
    try:
        from core.platform import get_logs_dir

        logs_dir = get_logs_dir()
        candidates.append(logs_dir / _CRASH_DIR_NAME)
        candidates.append(logs_dir)
    except Exception as exc:  # noqa: BLE001, RUF100 - fall through to the temp dir
        notes.append("logs-dir-unavailable:%s" % type(exc).__name__)
    try:
        import tempfile

        candidates.append(Path(tempfile.gettempdir()) / "viola-crash")
    except Exception as exc:  # noqa: BLE001, RUF100 - the loop below reports the real failure
        notes.append("tempdir-unavailable:%s" % type(exc).__name__)

    last_error: Exception | None = None
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / name
            # Text mode: faulthandler writes bytes to the raw fd, but the
            # header below is written through Python, and an explicit encoding
            # keeps a non-ASCII module path from exploding on cp1252.
            stream = open(path, "a", encoding="utf-8", errors="replace")
        except OSError as exc:
            last_error = exc
            notes.append("unwritable:%s" % directory.name)
            continue
        return stream, path, notes

    raise OSError("no writable crash-log location (last error: %r)" % (last_error,))


def install_crash_forensics(component: str) -> CrashForensics:
    """Prepare run-scoped, attributable crash artifacts for this process.

    The caller enables faulthandler on the returned stream::

        forensics = install_crash_forensics("viola_qt")
        faulthandler.enable(file=forensics.stream, all_threads=True)

    The enable() call stays at the call site on purpose: the crash handler is
    stdlib ``faulthandler`` and nothing else, and keeping that visible in each
    entrypoint is what
    ``tests/security/test_payment_pan_never_in_exceptions.py`` asserts.

    Everything except opening the log file is best-effort: a failure is
    recorded in ``notes`` and boot continues. Only a box where NO location at
    all is writable raises, which is the pre-existing behaviour of the code
    this replaces.
    """
    stamp, iso = _utc_now()
    stream, log_path, notes = _open_run_log(component, stamp)

    session_id = _resolve_session_id()
    dialog_suppression = "unattempted"
    try:
        dialog_suppression = suppress_native_crash_dialogs()
    except Exception as exc:  # noqa: BLE001, RUF100 - belt on top of the function's own guards
        dialog_suppression = "error:%s" % type(exc).__name__
        notes.append("dialog-suppression-failed")

    modules = snapshot_loaded_modules()
    modules_path: Path | None = None
    if modules:
        try:
            modules_path = _sidecar_for(log_path)
            _write_module_map(
                modules_path,
                component=component,
                session_id=session_id,
                captured_at="install",
                modules=modules,
            )
        except (OSError, TypeError, ValueError) as exc:
            notes.append("module-map-unwritten:%s" % type(exc).__name__)
            modules_path = None

    # Re-sample so Qt / PortAudio / the audio stack -- which load AFTER this
    # point and are where this app actually crashes -- end up in the map.
    if modules_path is not None:
        try:
            if not _start_module_map_refresher(
                modules_path,
                component=component,
                session_id=session_id,
                initial_count=len(modules),
            ):
                notes.append("module-refresh-off")
        except Exception:  # noqa: BLE001, RUF100 - never let a diagnostic thread fail the boot
            notes.append("module-refresh-unstarted")

    try:
        keep = _retention_limit()
        _prune_old_runs(log_path.parent, component, keep)
    except Exception:  # noqa: BLE001, RUF100 - housekeeping is never worth a failed boot
        notes.append("prune-failed")

    try:
        _register_clean_exit_marker(stream)
    except Exception:  # noqa: BLE001, RUF100 - never let a diagnostic hook fail the boot
        notes.append("clean-exit-marker-unregistered")

    _write_header(
        stream,
        component=component,
        iso=iso,
        session_id=session_id,
        dialog_suppression=dialog_suppression,
        modules_path=modules_path,
        module_count=len(modules),
        notes=notes,
    )

    return CrashForensics(
        component=component,
        stream=stream,
        log_path=log_path,
        modules_path=modules_path,
        session_id=session_id,
        started_at=iso,
        dialog_suppression=dialog_suppression,
        module_count=len(modules),
        notes=notes,
    )


def _register_clean_exit_marker(stream: IO[str]) -> None:
    """Stamp the log at normal interpreter exit so fatal dumps are separable.

    THE PROBLEM THIS SOLVES. On Windows, CPython's faulthandler is a VECTORED
    exception handler (``AddVectoredExceptionHandler``) that always returns
    EXCEPTION_CONTINUE_SEARCH, so it logs FIRST-CHANCE exceptions -- including
    ones something downstream catches and the process survives. The shared
    ``logs/faulthandler.log`` proves this empirically: it holds 190 blocks of
    ``0x80010108`` (RPC_E_DISCONNECTED, a COM control-flow exception the COM
    runtime converts to an HRESULT) and instances that logged an access
    violation and then kept running
    (_diag/2026-08-04/4650_crash_forensics_module_attribution.md, section 6.3).
    So "there is a dump in the file" does NOT mean "the process died", and a
    reader who assumes it does miscounts crashes roughly two-fold.

    Run-scoped files alone do not fix that -- they separate runs, not fatal
    faults from survived ones WITHIN a run. This marker does: if it is present,
    the interpreter reached a normal shutdown, so every dump above it was
    survived. If it is absent, the run ended abnormally.

    Honest limit, stated here so a reader is not misled: absence means "did not
    exit cleanly", which covers a hard kill (``taskkill /F``, TerminateProcess)
    as well as a crash, because neither runs atexit. Presence is the strong,
    unambiguous direction, and it is the one that was missing.
    """
    import atexit
    from datetime import datetime

    def _mark() -> None:
        try:
            stream.write(
                "\n%s\nrun_ended_utc: %s\n"
                % (
                    _CLEAN_EXIT_MARK,
                    datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                )
            )
            stream.flush()
        except (OSError, ValueError):
            # Closed/rotated stream at shutdown is not worth a noisy exit.
            return

    atexit.register(_mark)


def _write_header(
    stream: IO[str],
    *,
    component: str,
    iso: str,
    session_id: str,
    dialog_suppression: str,
    modules_path: Path | None,
    module_count: int,
    notes: list[str],
) -> None:
    """Stamp the run header so every dump below it is dated and attributable.

    This is THE fix for the "89 undated access violations in one file" problem:
    the header names the wall clock, the build and the run, and because the log
    is run-scoped nothing else can ever be appended under a different header.
    """
    fields = [
        ("run_started_utc", iso),
        ("component", component),
        ("pid", str(os.getpid())),
        ("session_id", session_id),
        ("app_version", _app_version()),
        ("git_sha", _git_sha()),
        ("python", sys.version.split()[0]),
        ("implementation", sys.implementation.name),
        ("platform", sys.platform),
        ("frozen", str(bool(getattr(sys, "frozen", False))).lower()),
        ("executable", sys.executable or "unknown"),
        ("native_dialog_suppression", dialog_suppression),
        ("module_map", str(modules_path) if modules_path else "none"),
        ("module_count", str(module_count)),
        ("notes", ",".join(notes) if notes else "none"),
    ]
    try:
        stream.write("\n" + (_HEADER_OPEN % CRASH_ARTIFACT_SCHEMA_VERSION) + "\n")
        for key, value in fields:
            stream.write("%s: %s\n" % (key, value))
        stream.write(_HEADER_CLOSE + "\n")
        stream.flush()
    except (OSError, ValueError):
        # A header we could not write is a worse artifact, not a failed boot.
        notes.append("header-unwritten")
