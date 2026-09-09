#!/usr/bin/env python3
"""Tree-killing, pipe-safe subprocess runner (#1824).

THE DISEASE (CL-20260714-1ed9 / CL-20260715 class)
--------------------------------------------------
On Windows, ``subprocess.run(argv, capture_output=True, timeout=T)`` -- and its
``Popen`` + ``communicate()`` equivalents -- kills only the DIRECT child when the
timeout fires. Grandchildren the child spawned (batch scripts, runner
registration ``config.cmd`` -> ``dotnet Runner.Listener``, docker/pg clients,
``signtool``/R2/``curl``/``powershell`` publish trees) inherit the stdout/stderr
PIPE write handles and survive. The stdlib's post-kill ``communicate()`` then
drains those pipes to EOF -- an EOF that never comes while a grandchild holds a
write handle -- so the call blocks FOREVER. A fired timeout becomes a permanent
wedge. Measured on this box: a raw ``capture_output`` call whose grandchild slept
8 s wedged 8.23 s PAST its 3.0 s timeout; an immortal grandchild = a permanent
wedge (the 5 autoscaler wedges, the 5.5 h battery-lock holder, and the silent
publish lanes on 2026-07-14/15).

THE FIX -- two independent guarantees
-------------------------------------
1. **No pipe to drain.** Output goes to TEMP FILES, never OS pipes. A leaked
   grandchild that inherits a file write handle blocks nothing -- there is no
   drain-to-EOF step at all. This alone eliminates the wedge (measured: the same
   forking command redirected to a file returned in 0.12 s).
2. **Race-free tree-wide termination.** On Windows with pywin32 the child is
   created SUSPENDED, assigned to a Job Object (``KILL_ON_JOB_CLOSE``) while still
   suspended, then resumed -- so every descendant is inside the job from birth and
   ``TerminateJobObject`` takes the WHOLE tree atomically, including grandchildren
   that reparented or detached (which ``taskkill``/``psutil`` snapshots miss).
   Assign-AFTER-spawn is NOT race-free (a fast-forking child spawns a grandchild in
   the microsecond before assignment and it escapes -- verified), which is why the
   suspend/assign/resume order matters. Fallbacks, in order, when pywin32 is
   unavailable or the job path errors: ``taskkill /T /F`` on the tree, then a
   ``psutil`` recursive-children sweep. POSIX uses a new session + ``killpg`` +
   ``psutil`` sweep.

TREE-KILL HAPPENS BEFORE TEMP-FILE CLEANUP. A grandchild inherits the temp FILE
handles too, so on Windows the files cannot be unlinked while a grandchild holds
them (``WinError 32``). Terminating the tree first releases those handles; reads of
the file content succeed regardless (a shared read never blocks).

DROP-IN
-------
``run(argv, timeout=..., ...)`` returns a :class:`TreeResult` shaped like
``subprocess.CompletedProcess`` (``returncode`` / ``stdout`` / ``stderr``), plus a
``timed_out`` flag. By default a timeout does NOT raise (returns ``timed_out=True``,
``returncode=124``) so a caller's ``if code != 0`` fails the step LOUDLY instead of
wedging; pass ``raise_on_timeout=True`` for stdlib-compatible ``TimeoutExpired``.

This module is the ONE shared primitive for forking spawn sites. Do not reintroduce
raw ``subprocess.run(capture_output=True, timeout=...)`` at a forking call site --
the ``no-raw-forking-subprocess`` Ratchet gate reds on it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

_IS_WINDOWS = os.name == "nt"

# Conventional exit code for a timed-out command (GNU ``timeout``). A NON-zero value
# so ``if returncode != 0`` fails the step loudly rather than silently succeeding.
TIMEOUT_RETURNCODE = 124
DEFAULT_KILL_GRACE_S = 5.0

try:
    import psutil  # cross-platform tree sweep + pid liveness
except ImportError:  # pragma: no cover - psutil is a hard dep here, defensive only
    psutil = None  # type: ignore[assignment]

_HAVE_WIN32JOB = False
if _IS_WINDOWS:
    try:
        import msvcrt

        import win32api
        import win32con
        import win32event
        import win32job
        import win32process

        _HAVE_WIN32JOB = True
    except ImportError:  # pragma: no cover - pywin32 present in this env; defensive
        _HAVE_WIN32JOB = False


@dataclass
class TreeResult:
    """Result of :func:`run`. Shaped like ``subprocess.CompletedProcess`` with a
    ``timed_out`` flag and the same ``returncode`` / ``stdout`` / ``stderr`` fields.

    ``stdout``/``stderr`` are ``str`` by default (decoded per ``run(encoding=...)``), or
    raw ``bytes`` when the caller passed ``encoding=None`` (see ``run`` docstring --
    needed for a command whose output encoding is not knowable/uniform up front, e.g.
    ``wsl.exe``'s UTF-16LE-vs-UTF-8 split)."""

    args: list[str]
    returncode: int | None
    stdout: str | bytes
    stderr: str | bytes
    timed_out: bool = False
    kill_method: str = ""  # which mechanism reaped the tree (observability)
    duration_s: float = 0.0

    def check_returncode(self) -> None:
        if self.timed_out:
            raise subprocess.TimeoutExpired(self.args, self.duration_s, self.stdout, self.stderr)
        if self.returncode not in (0, None):
            raise subprocess.CalledProcessError(self.returncode, self.args, self.stdout, self.stderr)


# --------------------------------------------------------------------------- #
# PID liveness (holder-validated locking, #1824 leg 3)
# --------------------------------------------------------------------------- #
def pid_alive(pid: int | None, *, create_time: float | None = None, create_time_tol_s: float = 2.0) -> bool:
    """True iff ``pid`` names a live, non-zombie process.

    PID-REUSE SAFE when ``create_time`` (a ``psutil.Process.create_time()`` epoch
    seconds) is supplied: liveness additionally requires the running process's
    creation time to match within ``create_time_tol_s`` -- so a recycled PID owned
    by an unrelated process reads as DEAD, which is exactly what a stale-lock
    reclaim must conclude. Without ``create_time`` this is a plain liveness check
    (use it only where PID reuse is not a concern).

    Conservative on ambiguity: a process that exists but cannot be inspected
    (``AccessDenied``) reads ALIVE, so we never break a lock we cannot prove dead.
    """
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if create_time is not None:
                try:
                    return abs(proc.create_time() - create_time) <= create_time_tol_s
                except (psutil.AccessDenied, OSError):
                    return True  # exists, cannot compare -> conservative alive
            return True
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True  # exists, owned by another principal -> conservative alive
        except (OSError, ValueError):
            return False
    # No psutil: best-effort. POSIX signal-0 probe; Windows assumes alive (safe).
    if _IS_WINDOWS:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def process_create_time(pid: int | None) -> float | None:
    """Creation-time epoch of ``pid`` (for recording a PID-reuse-safe lock holder)."""
    if not pid or psutil is None:
        return None
    with contextlib.suppress(Exception):
        return psutil.Process(int(pid)).create_time()
    return None


# --------------------------------------------------------------------------- #
# Tree termination for a bare PID (stale-lock reclaim, wedged-holder kill)
# --------------------------------------------------------------------------- #
def terminate_tree(pid: int, *, grace_s: float = DEFAULT_KILL_GRACE_S) -> tuple[list[int], list[int]]:
    """Terminate ``pid`` and every descendant. Returns ``(reaped, still_alive)`` pids.

    Cross-platform, snapshot-based (``psutil`` recursive children). For a live-tree
    kill this is complete; for a process that already reparented/detached a
    grandchild, use :func:`run` (Job Object) which contains descendants from birth.
    """
    reaped: list[int] = []
    if pid <= 0 or pid == os.getpid():
        return reaped, []
    if psutil is None:
        _taskkill_tree(pid)
        return reaped, []
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return reaped, []
    try:
        procs = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        procs = []
    procs.append(parent)
    for p in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            p.kill()
    gone, alive = psutil.wait_procs(procs, timeout=grace_s)
    reaped = [p.pid for p in gone]
    if alive and _IS_WINDOWS:
        _taskkill_tree(pid)  # belt-and-suspenders for anything psutil could not signal
        gone2, alive = psutil.wait_procs(alive, timeout=grace_s)
        reaped += [p.pid for p in gone2]
    return reaped, [p.pid for p in alive]


def _taskkill_tree(pid: int) -> None:
    """``taskkill /T /F /PID`` -- output to DEVNULL (no pipe to drain), short timeout."""
    if not _IS_WINDOWS:
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
@dataclass
class _TempCapture:
    out_path: str
    err_path: str | None
    _out = None
    _err = None
    _handles: list = field(default_factory=list)

    def read_text(self, encoding: str | None, errors: str) -> tuple[str | bytes, str | bytes]:
        """``encoding=None`` returns raw ``bytes`` (no decode) -- for a command whose
        output encoding is caller-determined after the fact (e.g. ``wsl.exe``'s
        UTF-16LE-vs-raw-UTF-8 split, picked by a null-byte heuristic)."""
        if encoding is None:
            out = _read_file_bytes(self.out_path)
            err = b"" if self.err_path is None else _read_file_bytes(self.err_path)
            return out, err
        out = _read_file_text(self.out_path, encoding, errors)
        err = "" if self.err_path is None else _read_file_text(self.err_path, encoding, errors)
        return out, err

    def cleanup(self) -> None:
        for h in self._handles:
            with contextlib.suppress(Exception):
                h.close()
        for path in (self.out_path, self.err_path):
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)


def _read_file_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:  # read tolerates a still-open (grandchild) handle
            return fh.read()
    except OSError:
        return b""


def _read_file_text(path: str, encoding: str, errors: str) -> str:
    return _read_file_bytes(path).decode(encoding, errors)


def run(
    argv: list[str],
    *,
    cwd: str | os.PathLike | None = None,
    env: dict | None = None,
    timeout: float | None = None,
    encoding: str | None = "utf-8",
    errors: str = "replace",
    combine_stderr: bool = False,
    kill_grace_s: float = DEFAULT_KILL_GRACE_S,
    raise_on_timeout: bool = False,
    check: bool = False,
    terminate_tree_on_exit: bool = True,
    creationflags: int = 0,
) -> TreeResult:
    """Run ``argv`` capturing output to temp FILES (never OS pipes) and terminating
    the whole descendant tree on timeout -- the wedge-proof, tree-killing drop-in for
    ``subprocess.run(argv, capture_output=True, timeout=timeout)``.

    ``combine_stderr`` merges stderr into stdout (single file); leave False to keep
    them separate (stdout-only fact parsing needs this). On timeout: returns
    ``timed_out=True`` and ``returncode=124`` (a loud non-zero) unless
    ``raise_on_timeout`` is set. ``terminate_tree_on_exit`` (default True) reaps any
    grandchildren the process leaves behind on NORMAL exit too -- correct for the
    publish/gate/ops call sites that must leave nothing running; set False for a
    caller that intentionally spawns a persistent daemon (e.g. runner registration).
    ``creationflags`` (Windows only) is OR-ed into the process creation flags -- pass
    ``CREATE_NO_WINDOW`` for a console child under a windowless (pythonw) parent so it
    does not flash a window (autoscaler ``config.cmd``, gh api; issue #261).
    ``encoding=None`` skips decoding entirely -- ``stdout``/``stderr`` come back as raw
    ``bytes`` -- for a command whose encoding isn't uniform/knowable up front (#1826:
    ``wsl.exe`` emits UTF-16LE for its own text but raw UTF-8 for a piped Linux
    program's stdout; the caller picks by a null-byte heuristic after the fact).
    """
    argv = [str(a) for a in argv]
    cwd = None if cwd is None else str(cwd)
    started = time.monotonic()

    cap = _make_temp_capture(combine_stderr)
    try:
        if _IS_WINDOWS and _HAVE_WIN32JOB:
            try:
                rc, timed_out, method = _run_windows_job(
                    argv,
                    cwd,
                    env,
                    timeout,
                    cap,
                    kill_grace_s,
                    terminate_tree_on_exit,
                    creationflags,
                )
            # any win32 / Job-Object failure -> degrade to the portable popen path
            except Exception:  # noqa: BLE001, RUF100  # pragma: no cover
                rc, timed_out, method = _run_popen(
                    argv,
                    cwd,
                    env,
                    timeout,
                    cap,
                    kill_grace_s,
                    terminate_tree_on_exit,
                    creationflags,
                )
                method = "popen-fallback:" + method
        else:
            rc, timed_out, method = _run_popen(
                argv,
                cwd,
                env,
                timeout,
                cap,
                kill_grace_s,
                terminate_tree_on_exit,
                creationflags,
            )

        out, err = cap.read_text(encoding, errors)
    finally:
        cap.cleanup()

    duration = time.monotonic() - started
    if timed_out:
        note = "\n[proc_tree] TIMEOUT after %.0fs; process tree terminated (%s)." % (
            timeout or 0,
            method,
        )
        note_bytes = note.encode("utf-8") if encoding is None else note
        if combine_stderr:
            out += note_bytes
        else:
            err += note_bytes
        rc = TIMEOUT_RETURNCODE

    result = TreeResult(
        args=argv,
        returncode=rc,
        stdout=out,
        stderr="" if combine_stderr else err,
        timed_out=timed_out,
        kill_method=method,
        duration_s=duration,
    )
    if timed_out and raise_on_timeout:
        raise subprocess.TimeoutExpired(argv, timeout or duration, out, None if combine_stderr else err)
    if check:
        result.check_returncode()
    return result


def _make_temp_capture(combine: bool) -> _TempCapture:
    out_fd, out_path = tempfile.mkstemp(prefix="proctree-", suffix=".out")
    os.close(out_fd)
    if combine:
        return _TempCapture(out_path=out_path, err_path=None)
    err_fd, err_path = tempfile.mkstemp(prefix="proctree-", suffix=".err")
    os.close(err_fd)
    return _TempCapture(out_path=out_path, err_path=err_path)


def _run_windows_job(argv, cwd, env, timeout, cap, kill_grace_s, terminate_on_exit, creationflags=0):
    """Race-free Windows path: CREATE_SUSPENDED -> assign to Job Object -> resume."""
    of = open(cap.out_path, "wb")
    ef = of if cap.err_path is None else open(cap.err_path, "wb")
    devnull_in = open(os.devnull, "rb")
    cap._handles.extend([of] if ef is of else [of, ef])
    cap._handles.append(devnull_in)

    for handle_file in {id(of): of, id(ef): ef, id(devnull_in): devnull_in}.values():
        os.set_handle_inheritable(msvcrt.get_osfhandle(handle_file.fileno()), True)

    job = win32job.CreateJobObject(None, "")
    if terminate_on_exit:
        # KILL_ON_JOB_CLOSE is what makes the tree kill total: it reaps every process
        # in the job the instant the LAST handle to the job closes, including a
        # grandchild that already reparented or detached. It must NOT be set when the
        # caller asked to keep the tree alive (terminate_tree_on_exit=False) -- the
        # job handle is closed in this function's `finally`, so with the flag set the
        # tree dies there no matter what the caller asked for, and the documented
        # "set False for a caller that intentionally spawns a persistent daemon"
        # contract was silently a no-op on Windows.
        #
        # That is exactly how it broke installer_smoke's product-mode launch: the
        # smoke starts the desktop app through the native launcher, which spawns
        # ViolaApp.exe and exits within a second BY DESIGN (Plan B, 9951da91e), so
        # the PowerShell returned at ~0.8s, this handle closed, and the freshly
        # spawned app was killed about a second after launch. The smoke then polled
        # a dead port for its full 180s launch budget and reported a health timeout
        # with no crash and an empty runtime data dir -- the app looked like it never
        # started, and nothing pointed back here. viola_launcher.c's own header warns
        # about precisely this ("KILL_ON_JOB_CLOSE ... would kill ViolaApp.exe moments
        # after launch") for a job owned by the launcher; the harness reintroduced the
        # same shape from the other side.
        #
        # Timeout tree-kill does NOT depend on this flag: the timeout path below calls
        # TerminateJobObject explicitly while the handle is still open, so a wedged
        # tree is still reaped whole either way.
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)

    si = win32process.STARTUPINFO()
    si.dwFlags |= win32con.STARTF_USESTDHANDLES
    si.hStdInput = msvcrt.get_osfhandle(devnull_in.fileno())
    si.hStdOutput = msvcrt.get_osfhandle(of.fileno())
    si.hStdError = msvcrt.get_osfhandle(ef.fileno())

    flags = win32con.CREATE_SUSPENDED | int(creationflags)
    if env is not None:
        flags |= win32con.CREATE_UNICODE_ENVIRONMENT
    cmdline = subprocess.list2cmdline(argv)

    h_proc = h_thread = None
    assigned = False  # True once the child is inside the job (KILL_ON_JOB_CLOSE can reap it)
    method = "win32job"
    try:
        h_proc, h_thread, _pid, _tid = win32process.CreateProcess(None, cmdline, None, None, True, flags, env, cwd, si)
        win32job.AssignProcessToJobObject(job, h_proc)  # while suspended -> race-free
        assigned = True
        win32process.ResumeThread(h_thread)

        timed_out = False
        wait_ms = win32event.INFINITE if timeout is None else int(timeout * 1000)
        rc_wait = win32event.WaitForSingleObject(h_proc, wait_ms)
        if rc_wait == win32event.WAIT_TIMEOUT:
            timed_out = True
            win32job.TerminateJobObject(job, TIMEOUT_RETURNCODE)
            win32event.WaitForSingleObject(h_proc, int(kill_grace_s * 1000))
            rc = TIMEOUT_RETURNCODE
        else:
            rc = win32process.GetExitCodeProcess(h_proc)
            if terminate_on_exit:
                # reap any grandchildren the process left behind (KILL_ON_JOB_CLOSE also
                # covers this when the handle closes, but be explicit + deterministic).
                with contextlib.suppress(Exception):
                    win32job.TerminateJobObject(job, rc)
        return rc, timed_out, method
    finally:
        # If the child was created SUSPENDED but AssignProcessToJobObject failed (e.g. an
        # outer job that forbids nesting), it is NOT in the job -- so closing the job
        # handle below will NOT reap it, and the raiser degrades to _run_popen which spawns
        # a fresh process. Kill the orphan explicitly so it can never leak as a wedged
        # suspended process. (Assign succeeded -> KILL_ON_JOB_CLOSE handles it.)
        if h_proc is not None and not assigned:
            with contextlib.suppress(Exception):
                win32process.TerminateProcess(h_proc, TIMEOUT_RETURNCODE)
        for h in (h_thread, h_proc):
            if h is not None:
                with contextlib.suppress(Exception):
                    win32api.CloseHandle(h)
        with contextlib.suppress(Exception):
            # With terminate_on_exit, KILL_ON_JOB_CLOSE is set and this close is the
            # final safety net for the tree. Without it the flag is deliberately not
            # set, so closing here just releases the job and leaves the tree running.
            win32api.CloseHandle(job)


def _run_popen(argv, cwd, env, timeout, cap, kill_grace_s, terminate_on_exit, creationflags=0):
    """Portable path (POSIX, or Windows without pywin32): temp-file output + tree
    kill via new-session killpg / taskkill / psutil. No pipe -> no drain wedge."""
    of = open(cap.out_path, "wb")
    ef = of if cap.err_path is None else open(cap.err_path, "wb")
    cap._handles.extend([of] if ef is of else [of, ef])

    kwargs: dict = {
        "cwd": cwd,
        "env": env,
        "stdout": of,
        "stderr": ef,
        "stdin": subprocess.DEVNULL,
    }
    if not _IS_WINDOWS:
        kwargs["start_new_session"] = True  # own process group for killpg
    elif creationflags:
        kwargs["creationflags"] = int(creationflags)

    proc = subprocess.Popen(argv, **kwargs)  # trusted argv list, shell=False
    timed_out = False
    method = "killpg" if not _IS_WINDOWS else "taskkill"
    try:
        proc.wait(timeout=timeout)
        rc = proc.returncode
        if terminate_on_exit:
            _kill_process_tree(proc, kill_grace_s)  # reap leaked grandchildren
        return rc, False, method
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc, kill_grace_s)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=kill_grace_s)
        return TIMEOUT_RETURNCODE, True, method


def _kill_process_tree(proc: subprocess.Popen, grace_s: float) -> None:
    """Kill the whole descendant tree of ``proc``, including the NORMAL-exit case.

    ``_run_popen`` calls this both on timeout (``proc`` still alive) AND on normal
    exit (``proc.wait()`` already succeeded, so the OS has fully reaped ``proc.pid``
    -- it no longer names any process, zombie or otherwise). ``os.getpgid(pid)``
    on an already-reaped pid raises ``ProcessLookupError`` unconditionally (verified:
    100% reproduction, not a race), so re-querying the pgid via the dead leader's pid
    silently no-ops every normal-exit call -- the #1826 regression this fixes
    (test_forking_child_that_exits_is_not_wedged). ``_run_popen`` always spawns with
    ``start_new_session=True`` on POSIX, which makes the child's own pid its process
    GROUP id at creation time (POSIX: a new session's pgid == its leader's pid) --
    that pgid stays a valid killpg target as long as ANY member (e.g. a lingering
    grandchild) is still in the group, independent of whether the leader itself has
    already exited and been reaped. So use ``pid`` directly as the pgid instead of
    re-deriving it through the (possibly already-gone) leader process.
    """
    pid = proc.pid
    if not _IS_WINDOWS:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, _sigkill())
    terminate_tree(pid, grace_s=grace_s)


def _sigkill() -> int:
    import signal

    return getattr(signal, "SIGKILL", signal.SIGTERM)


# --------------------------------------------------------------------------- #
# Tuple adapter for call sites that want (returncode, stdout, stderr)
# --------------------------------------------------------------------------- #
def run_tuple(argv: list[str], **kwargs) -> tuple[int, str, str]:
    """``(returncode, stdout, stderr)`` convenience; a timeout maps to
    ``returncode=124`` with the timeout note appended to stderr."""
    res = run(argv, **kwargs)
    rc = res.returncode if res.returncode is not None else TIMEOUT_RETURNCODE
    return rc, res.stdout, res.stderr


if __name__ == "__main__":  # tiny CLI: python -m scripts.proc_tree -- <argv...> [--timeout N]
    import argparse

    ap = argparse.ArgumentParser(description="tree-killing pipe-safe runner (#1824)")
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--combine", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    cmd = ns.cmd[1:] if ns.cmd and ns.cmd[0] == "--" else ns.cmd
    if not cmd:
        print(
            "usage: python -m scripts.proc_tree --timeout N -- <argv...>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    r = run(cmd, timeout=ns.timeout, combine_stderr=ns.combine)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    raise SystemExit(r.returncode if r.returncode is not None else TIMEOUT_RETURNCODE)
