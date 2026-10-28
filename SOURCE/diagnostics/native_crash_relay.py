"""Send last run's native crash to GlitchTip on the next launch.

A native crash (an access violation inside python311.dll, Qt or a driver)
kills the process before any Python crash hook can run, so the only record is
the faulthandler dump in the run-scoped log that ``core.crash_forensics``
opens for every run. Nothing read those logs back: the two 1.0.5 build-5
laptop crashes left a dump on the laptop and nothing in GlitchTip, so the only
evidence that reached us was the Windows Application log's faulting offset.

On launch this looks at earlier runs of the same component, picks the ones that
did NOT end cleanly (no clean-exit marker) and whose last dump is a real fatal
fault or a quit that the exit watchdog had to end, and sends one anonymized
minimum per run through the crash baseline
(``diagnostics.diagnostic_dispatch.dispatch_native_crash_diagnostic``): same
opt-out, same "queue until the disclosure was shown" rule, same allowlist. Only
the fault label and the crashing thread's ``module / function / line`` frames
leave the machine; file paths are reduced exactly like a Python traceback.
Each run is reported once: a marker line is appended to its log.

Benign first-chance exceptions are not crashes. On Windows faulthandler also
logs exceptions that are raised and then handled (COM's RPC_E_DISCONNECTED
``0x80010108`` is routine), so a dump whose code is not an error-severity
NTSTATUS (``0xC...``) is skipped.

Never raises; meant to run on a background thread at startup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_config import get_logger

log = get_logger(__name__)

RELAYED_MARK = "==== VIOLA NATIVE CRASH RELAYED ===="

# Mirrors core.crash_forensics (kept literal so this module has no import cost
# on the crash path and stays readable on its own).
_CLEAN_EXIT_MARK = "==== VIOLA CRASH RUN ENDED CLEANLY ===="
_EXIT_WATCHDOG_MARK = "==== VIOLA EXIT WATCHDOG ARMED ===="
_LOG_SUFFIX = ".faulthandler.log"

# Reading back more than a few runs only matters after a crash loop, and the
# baseline groups duplicates anyway.
_MAX_RUNS_PER_LAUNCH = 3
# A run log larger than this is read from its tail only.
_MAX_READ_BYTES = 2 * 1024 * 1024

# faulthandler's headers. A nested fault can start mid-line (the dumper itself
# faulted while writing ``  File "...``), so these are not anchored to a line
# start.
_FATAL_HEADER_RE = re.compile(
    r"(Windows fatal exception: [^\r\n]+|Fatal Python error: [^\r\n]+|Timeout \(\d+:\d\d:\d\d\)!)"
)
_FRAME_RE = re.compile(r'^\s+File "(?P<file>[^"\r\n]*)", line (?P<line>\d+|\?\?\?) in (?P<func>[^\r\n]+)$')
_FRAME_UNKNOWN_RE = re.compile(r"^\s+File \?\?\?, line \?\?\? in \?\?\?$")
_BLOCK_START_RE = re.compile(
    r"^(Current thread 0x[0-9a-fA-F]+|Thread 0x[0-9a-fA-F]+|Stack) \(most recent call first\):$"
)
_CODE_RE = re.compile(r"code (0x[0-9a-fA-F]{1,8})")
_PID_RE = re.compile(r"^pid: (\d+)\r?$", re.MULTILINE)


@dataclass
class NativeCrash:
    """What one unclean run's log says about how it ended."""

    error_type: str
    error_value: str
    # (filename, lineno or None, function), OLDEST first.
    frames: list[tuple[str, int | None, str]] = field(default_factory=list)


def _is_fatal_windows_label(label: str) -> bool:
    """True for faults that end a process, False for routine first-chance codes."""
    match = _CODE_RE.search(label)
    if match is None:
        # Named labels: access violation, stack overflow, illegal instruction,
        # in page error, divide by zero, ... -- all error severity.
        return True
    try:
        code = int(match.group(1), 16)
    except ValueError:
        return False
    # NTSTATUS severity bits 0b11 = error. 0x8... (warning: RPC/COM control
    # flow), 0x4... (informational) and 0xE... (customer: C++ / CLR) are not
    # process-ending by themselves.
    return (code >> 28) == 0xC


def _thread_blocks(lines: list[str], start: int) -> list[tuple[bool, list[tuple[str, int | None, str]]]]:
    """The thread blocks of the dump whose header is ``lines[start]``.

    Each block is ``(is_faulting_thread, frames)`` with frames most recent
    first, as faulthandler prints them. ``all_threads=False`` writes one
    "Stack" block; ``all_threads=True`` marks the faulting thread "Current
    thread". The dump ends at the next fatal header.
    """
    blocks: list[tuple[bool, list[tuple[str, int | None, str]]]] = []
    for line in lines[start + 1 :]:
        stripped = line.rstrip("\r\n")
        if _FATAL_HEADER_RE.search(stripped):
            break
        block = _BLOCK_START_RE.match(stripped)
        if block is not None:
            label = block.group(1)
            blocks.append((label == "Stack" or label.startswith("Current thread"), []))
            continue
        if not blocks:
            continue
        frame = _FRAME_RE.match(stripped)
        if frame is not None:
            line_text = frame.group("line")
            blocks[-1][1].append(
                (frame.group("file"), int(line_text) if line_text.isdigit() else None, frame.group("func"))
            )
        elif _FRAME_UNKNOWN_RE.match(stripped):
            blocks[-1][1].append(("???", None, "???"))
    return blocks


def _frames_after(lines: list[str], start: int, *, main_thread: bool = False) -> list[tuple[str, int | None, str]]:
    """Frames, OLDEST first, of the faulting thread in the dump at ``lines[start]``.

    ``main_thread`` picks the last block instead: the exit watchdog's dump has
    no faulting thread, and faulthandler lists the oldest thread (the main,
    Qt thread) last.
    """
    blocks = _thread_blocks(lines, start)
    chosen: list[tuple[str, int | None, str]] = []
    if main_thread:
        if blocks:
            chosen = blocks[-1][1]
    else:
        for is_faulting, frames in blocks:
            if is_faulting:
                chosen = frames
                break
    return list(reversed(chosen))


def _line_index(lines: list[str], position: int) -> int:
    """Index of the line holding character ``position`` of the joined text."""
    offset = 0
    for index, line in enumerate(lines):
        if offset + len(line) >= position:
            return index
        offset += len(line) + 1
    return max(0, len(lines) - 1)


def parse_native_crash(text: str) -> NativeCrash | None:
    """Return the fatal ending recorded in a run log, or None if there is none.

    Only the LAST dump in the file can be the one that ended the run.
    """
    if _CLEAN_EXIT_MARK in text or RELAYED_MARK in text:
        return None
    headers = list(_FATAL_HEADER_RE.finditer(text))
    if not headers:
        return None
    last = headers[-1]
    label = last.group(1)
    if label.startswith("Timeout ("):
        if _EXIT_WATCHDOG_MARK not in text[: last.start()]:
            # A debug dump_traceback_later, not our quit watchdog.
            return None
        error_type, error_value = "ExitHang", "Quit did not finish; the exit watchdog ended the process"
    elif label.startswith("Fatal Python error: "):
        error_type, error_value = "FatalPythonError", label
    else:
        if not _is_fatal_windows_label(label):
            return None
        error_type, error_value = "NativeCrash", label

    lines = text.split("\n")
    header_line = _line_index(lines, last.start())
    frames = _frames_after(lines, header_line, main_thread=error_type == "ExitHang")
    if not frames and len(headers) > 1 and lines[header_line].lstrip().startswith("File "):
        # The dumper itself faulted mid-frame (see
        # core.crash_forensics.faulthandler_all_threads): report the fault and
        # the stack of the exception it was dumping.
        previous = headers[-2]
        error_value = "%s while dumping %s" % (label, previous.group(1))
        frames = _frames_after(lines, _line_index(lines, previous.start()))
    return NativeCrash(error_type=error_type, error_value=error_value, frames=frames)


def _read_tail(path: Path) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - _MAX_READ_BYTES))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _run_is_alive(text: str) -> bool:
    """True when the process that owns this run log is still running.

    A live run has no clean-exit marker yet, so without this check a second
    launch could report (and mark) a run that has not ended.
    """
    match = _PID_RE.search(text)
    if match is None:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(int(match.group(1))))
    except Exception:  # noqa: BLE001, RUF100 - unknown means "leave it for a later launch"
        return True


def _mark_relayed(path: Path, outcome: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n%s\noutcome: %s\n" % (RELAYED_MARK, outcome))
    except OSError:
        log.debug("Could not mark %s as relayed", path.name, exc_info=True)


def relay_previous_native_crashes(
    component: str,
    *,
    current_log: Path | None,
    crash_dir: Path | None = None,
    surface: str = "desktop_qt",
) -> list[str]:
    """Report earlier unclean runs of ``component``; return the dispatch outcomes.

    ``current_log`` is this run's own log, which is never read. Never raises.
    """
    outcomes: list[str] = []
    try:
        directory = crash_dir or (current_log.parent if current_log is not None else None)
        if directory is None or not directory.is_dir():
            return outcomes
        current = current_log.resolve() if current_log is not None else None
        candidates = sorted(
            (p for p in directory.glob("%s-*%s" % (component, _LOG_SUFFIX)) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        checked = 0
        for path in candidates:
            if current is not None and path.resolve() == current:
                continue
            if checked >= _MAX_RUNS_PER_LAUNCH * 4:
                break
            checked += 1
            try:
                text = _read_tail(path)
            except OSError:
                continue
            if RELAYED_MARK in text or _CLEAN_EXIT_MARK in text or _run_is_alive(text):
                continue
            crash = parse_native_crash(text)
            if crash is None:
                _mark_relayed(path, "no-fatal-dump")
                continue
            from diagnostics.diagnostic_dispatch import dispatch_native_crash_diagnostic

            outcome = dispatch_native_crash_diagnostic(
                error_type=crash.error_type,
                error_value=crash.error_value,
                frames=crash.frames,
                surface=surface,
            )
            _mark_relayed(path, outcome)
            log.warning(
                "Previous %s run ended in a native fault (%s: %s); crash report %s",
                component,
                crash.error_type,
                crash.error_value,
                outcome,
            )
            outcomes.append(outcome)
            if len(outcomes) >= _MAX_RUNS_PER_LAUNCH:
                break
    except Exception:  # noqa: BLE001, RUF100 - a crash relay must never break startup
        log.debug("Native crash relay failed", exc_info=True)
    return outcomes


__all__ = ["RELAYED_MARK", "NativeCrash", "parse_native_crash", "relay_previous_native_crashes"]
