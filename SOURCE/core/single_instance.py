"""Single-instance enforcement for Viola desktop app.

Goal (safety-critical launch path): a real installed user must ALWAYS be able
to start Viola. The failure mode this guards against is a *zombie* holder — a
ViolaApp.exe process that is still alive but not a usable app (its Qt event loop
froze, or a crash left it running with no window, or a dying launcher orphaned
it). A naive single-instance lock keeps blocking every new launch because the
zombie still "holds" the lock, and the user has to hunt the ghost process in
Task Manager. This module reclaims the lock when — and ONLY when — there is
unambiguous evidence the holder is dead or non-functional.

The catastrophic *wrong* outcome is the opposite: two live Viola instances
(double audio capture, double wake word, split state). So reclaim is
deliberately conservative — during a generous startup grace window we NEVER
reclaim, and after it we reclaim only on a frozen event loop or a
window-less-but-alive holder.

Three cooperating primitives, one file namespace per API port:

1. ``QLockFile`` (``viola-instance-<port>.lock``) — the atomic cross-process
   gate. ``tryLock()`` is atomic on Windows (a plain ``QLocalServer.listen()``
   is NOT: two racing starts can both ``listen()`` the same pipe name), and Qt
   auto-reclaims a lock whose owning PID is dead. This closes the
   double-acquire race and handles the force-killed/dead holder for free.

2. A JSON state file (``viola-instance-<port>.state.json``) — lets a challenger
   that lost ``tryLock`` (a holder is alive) classify that holder as functional
   vs. zombie. Holds ``pid``/``create_time`` (PID-reuse safe), ``acquired_at``
   (startup-grace clock), ``heartbeat_at`` (event-loop-alive clock, bumped by a
   QTimer so it only advances while the event loop runs), ``ready`` (has a
   raisable window), and the unique ``pipe_name`` to talk to.

3. ``QLocalServer`` (``viola-instance-<port>-<token>``, unique per acquisition)
   — best-effort raise-the-existing-window IPC. Correctness never depends on it;
   the unique name means a lingering zombie's stale pipe can't be mistaken for
   the current holder's.

A holder that discovers it has been reclaimed (its token is no longer in the
state file) stands itself down on the next heartbeat, so at most one process
ever believes it is the instance.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
from PySide6.QtCore import QCoreApplication, QLockFile, QObject, QTimer, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from core.logging_config import get_logger

if TYPE_CHECKING:
    from PySide6.QtWidgets import QMainWindow

logger = get_logger(__name__)

# Include port in the resource names so multiple instances on different ports
# can coexist without the single-instance guard blocking the second one.
_instance_port = os.environ.get("VIOLA_API_PORT", "8756")

RAISE_CMD = b"RAISE_WINDOW"
_ALIVE_REPLY = b"ALIVE"
_NOWINDOW_REPLY = b"NOWINDOW"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


# --- Conservative timing knobs (all biased toward NOT reclaiming) ------------
# Heartbeat cadence: the holder's QTimer bumps ``heartbeat_at`` this often. It
# only fires while the Qt event loop is actually running, so a fresh heartbeat
# is positive proof the event loop is alive.
HEARTBEAT_INTERVAL_S = _env_float("VIOLA_INSTANCE_HEARTBEAT_S", 2.0)
# A holder whose event loop hasn't ticked within this window is treated as
# frozen. Must comfortably exceed any plausible momentary main-thread block of a
# *healthy* app — a 30s UI freeze is already pathological. Bigger = safer
# (slower to reclaim a truly-hung holder, but never falsely reclaims a busy one).
HEARTBEAT_STALE_LIMIT_S = _env_float("VIOLA_INSTANCE_HEARTBEAT_STALE_S", 30.0)
# From lock acquisition until this many seconds elapse, a holder is NEVER
# reclaimed — this is the window where a healthy instance is still importing /
# starting its backend / building its window and legitimately has no event loop
# or window yet. Must exceed worst-case cold startup to the first window: a
# measured cold start to FULLY-verified on a fast dev machine is ~43s
# (2026-07-06, viola_control.py start); real user machines (first run, AV
# scans, spinning disks) can be far slower, so the default carries wide margin.
# An oversized grace merely delays reclaiming a fresh zombie; an undersized one
# kills a healthy starting instance — always err large.
STARTUP_GRACE_S = _env_float("VIOLA_INSTANCE_STARTUP_GRACE_S", 120.0)
# How long a challenger waits for the raise/health reply from a holder before
# falling back to state-file classification.
_HANDSHAKE_WAIT_MS = 1000
# Bounded retries for the acquire loop (each iteration may reclaim one zombie).
_MAX_ACQUIRE_ATTEMPTS = 5


def _runtime_dir() -> Path:
    """Directory for the lock + state files. Viola-owned temp, with a safe
    fallback so a mis-set env can never prevent launch."""
    try:
        from core.platform import get_temp_dir

        d = get_temp_dir()
    except (ImportError, OSError, ValueError):  # pragma: no cover - defensive
        d = Path(tempfile.gettempdir()) / "viola"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive
        d = Path(tempfile.gettempdir())
    return d


def _lock_path(port: str) -> str:
    return str(_runtime_dir() / f"viola-instance-{port}.lock")


def _state_path(port: str) -> Path:
    return _runtime_dir() / f"viola-instance-{port}.state.json"


def _proc_create_time(pid: int) -> float | None:
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        return psutil.pid_exists(pid)
    except psutil.Error:  # pragma: no cover - psutil should be present
        return True  # unknown -> assume alive (conservative: don't reclaim)


# Backwards/coexistence: the deterministic pipe name older builds listened on.
def _legacy_socket_name(port: str) -> str:
    return f"viola-instance-lock-{port}"


class SingleInstanceGuard(QObject):
    """Manages single-instance enforcement.

    Usage::

        guard = SingleInstanceGuard(app)
        if not guard.try_acquire():
            sys.exit(0)  # another live, functioning instance was focused
        ...
        guard.set_window(window)  # enable RAISE_WINDOW + mark 'ready'
    """

    def __init__(self, parent: QObject | None = None, *, port: str | None = None) -> None:
        super().__init__(parent)
        self._port = str(port) if port is not None else _instance_port
        self._server: QLocalServer | None = None
        self._window: QMainWindow | None = None
        self._lock: QLockFile | None = None
        self._token: str = ""
        self._pipe_name: str = ""
        self._acquired_at: float = 0.0
        self._create_time: float | None = None
        self._heartbeat: QTimer | None = None
        self._stood_down = False

    # -- public API -----------------------------------------------------------
    def try_acquire(self) -> bool:
        """Try to become the single instance.

        Returns True if we are (now) the instance — either no live holder
        existed, or the previous holder was a dead/non-functional zombie whose
        lock we reclaimed. Returns False if a live, *functioning* instance
        already holds the lock (we asked it to raise its window).
        """
        for attempt in range(_MAX_ACQUIRE_ATTEMPTS):
            lock = QLockFile(_lock_path(self._port))
            # We do our own liveness reasoning; disable Qt's blunt time-based
            # staleness so it can't reclaim a slow-but-healthy startup out from
            # under us. Qt still auto-reclaims when the holder PID is DEAD.
            lock.setStaleLockTime(0)

            if lock.tryLock(100):
                # No process holds the QLockFile. But a PRE-UPGRADE build guards
                # only with QLocalServer (no QLockFile), so it would not have
                # failed our tryLock. Probe its legacy socket before we claim the
                # instance, so we never double-launch across an upgrade.
                if self._legacy_instance_alive():
                    lock.unlock()
                    self._raise_existing_window()
                    return False
                self._become_holder(lock)
                return True

            # A process holds the lock. Is it a functioning instance?
            holder_pid = self._holder_pid(lock)
            if self._holder_is_functional(holder_pid):
                self._raise_existing_window()
                return False

            # Holder is alive but non-functional (frozen / window-less), or
            # provably gone via PID reuse. Reclaim conservatively and retry.
            logger.warning(
                "Single-instance: holder pid=%s appears non-functional; reclaiming lock " "(attempt %d/%d)",
                holder_pid,
                attempt + 1,
                _MAX_ACQUIRE_ATTEMPTS,
            )
            self._force_reclaim(lock, holder_pid)
            # loop and re-attempt the atomic tryLock

        # Could not acquire after bounded retries (persistent contention). Do
        # NOT block the user's launch on an unexplained lock failure.
        logger.error(
            "Single-instance: could not acquire lock after %d attempts; "
            "starting without the lock to avoid blocking launch",
            _MAX_ACQUIRE_ATTEMPTS,
        )
        return True

    def set_window(self, window: QMainWindow) -> None:
        """Register the main window: enables RAISE_WINDOW handling and marks
        this instance 'ready' (has a raisable window) in the state file."""
        self._window = window
        self._write_state(ready=True)

    # -- becoming / holding the lock -----------------------------------------
    def _become_holder(self, lock: QLockFile) -> None:
        self._lock = lock
        self._token = uuid.uuid4().hex
        self._pipe_name = f"viola-instance-{self._port}-{self._token}"
        self._acquired_at = time.time()
        self._create_time = _proc_create_time(os.getpid())

        # Write state BEFORE listening so any challenger that connects can
        # always resolve our identity + pipe.
        self._write_state(ready=False)

        # Best-effort raise IPC on a per-acquisition unique name. Also listen on
        # the legacy deterministic name so a pre-upgrade challenger can still
        # raise us.
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        QLocalServer.removeServer(self._pipe_name)
        if not self._server.listen(self._pipe_name):
            logger.error(
                "Single-instance: raise-IPC listen failed on %s: %s",
                self._pipe_name,
                self._server.errorString(),
            )
        self._legacy_server = QLocalServer(self)
        self._legacy_server.newConnection.connect(self._on_new_connection)
        QLocalServer.removeServer(_legacy_socket_name(self._port))
        self._legacy_server.listen(_legacy_socket_name(self._port))

        # Heartbeat: proves the event loop is alive, and self-evicts if we were
        # reclaimed. Fires only once app.exec() runs.
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(int(HEARTBEAT_INTERVAL_S * 1000))
        self._heartbeat.timeout.connect(self._on_heartbeat)
        self._heartbeat.start()

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cleanup)

        logger.info("Single-instance lock acquired (port=%s token=%s)", self._port, self._token)

    def _state_dict(self, *, ready: bool) -> dict:
        return {
            "pid": os.getpid(),
            "create_time": self._create_time,
            "acquired_at": self._acquired_at,
            "heartbeat_at": time.time(),
            "ready": bool(ready),
            "token": self._token,
            "pipe_name": self._pipe_name,
        }

    def _write_state(self, *, ready: bool) -> None:
        if not self._token:
            return
        path = _state_path(self._port)
        payload = json.dumps(self._state_dict(ready=ready))
        try:
            # Atomic replace so a reader never sees a half-written file.
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError:
            logger.exception("Single-instance: failed to write state file")

    @Slot()
    def _on_heartbeat(self) -> None:
        # Self-eviction backstop: if our token is no longer the one in the state
        # file, another launch reclaimed us (judged us a zombie). Stand down so
        # there is never a second live instance.
        if self._stood_down:
            return
        current = _read_state(self._port)
        if current is not None and current.get("token") not in (None, self._token):
            logger.warning(
                "Single-instance: our lock was reclaimed by another instance " "(token %s -> %s); standing down",
                self._token,
                current.get("token"),
            )
            self._stood_down = True
            app = QCoreApplication.instance()
            if app is not None:
                app.quit()
            return

        ready = self._window_is_raisable()
        self._write_state(ready=ready)

    def _window_is_raisable(self) -> bool:
        w = self._window
        if w is None:
            return False
        try:
            # Touch the C++ object; a destroyed/deleted window raises RuntimeError
            # ("Internal C++ object already deleted"), which is exactly the
            # window-less-zombie signal we want.
            _ = w.isVisible()
        except RuntimeError:
            return False
        return True

    # -- classifying an existing holder --------------------------------------
    def _holder_pid(self, lock: QLockFile) -> int:
        try:
            info = lock.getLockInfo()
            # PySide6 returns (pid, hostname, appname).
            if info and len(info) >= 1:
                return int(info[0])
        except (RuntimeError, ValueError, TypeError):
            pass
        st = _read_state(self._port)
        if st:
            try:
                return int(st.get("pid", -1))
            except (TypeError, ValueError):
                return -1
        return -1

    def _holder_is_functional(self, holder_pid: int) -> bool:
        """Conservative: return True (defer, do NOT reclaim) unless there is
        unambiguous evidence the holder is dead or non-functional."""
        now = time.time()
        state = _read_state(self._port)

        if state is None:
            # No state file. If a live process is behind the lock (holder wrote
            # the QLockFile but not yet the state file, in the microsecond window
            # inside _become_holder), defer — reclaiming would double-launch a
            # holder that just acquired. If we could NOT identify any live holder
            # (holder_pid<=0, e.g. an unwritable lock dir), there is no evidence a
            # holder exists at all: do NOT defer (proceed to acquire / fail open).
            if holder_pid > 0 and _pid_alive(holder_pid):
                return True
            return False

        try:
            st_pid = int(state.get("pid", -1))
        except (TypeError, ValueError):
            return True

        if holder_pid > 0 and st_pid != holder_pid:
            # State file is about a different process than the lock's owner ->
            # uncertain -> defer.
            return True

        if not _pid_alive(st_pid):
            return False  # holder gone

        # PID-reuse guard: same PID number, different process.
        st_ct = state.get("create_time")
        if st_ct is not None:
            live_ct = _proc_create_time(st_pid)
            if live_ct is not None and abs(float(st_ct) - live_ct) > 1.0:
                return False  # original holder dead, PID recycled

        acquired_at = _as_float(state.get("acquired_at"))
        heartbeat_at = _as_float(state.get("heartbeat_at"))
        ready = bool(state.get("ready"))

        # Inside the startup grace window we NEVER reclaim — a healthy instance
        # may still be importing / starting its backend / building its window.
        if acquired_at is None or (now - acquired_at) < STARTUP_GRACE_S:
            return True

        # Past startup grace. Require a live event loop AND a raisable window.
        hb_fresh = heartbeat_at is not None and (now - heartbeat_at) <= HEARTBEAT_STALE_LIMIT_S
        if not hb_fresh:
            return False  # event loop frozen -> hung zombie
        if not ready:
            return False  # event loop alive but no window -> window-less zombie
        return True

    def _force_reclaim(self, lock: QLockFile, holder_pid: int) -> None:
        """Free the lock held by a non-functional zombie so the next atomic
        tryLock can win.

        On Windows a live process keeps its ``QLockFile`` file open, so the file
        cannot simply be unlinked (WinError 32) — and a frozen holder's event
        loop is dead, so it cannot cooperatively stand down either. The only
        decisive, correct reclaim is to TERMINATE the zombie. That also frees
        the resources a fresh launch actually needs: the raise-IPC pipe AND the
        API port the zombie still binds. We only reach here after classifying
        the holder non-functional, and we additionally verify the PID's process
        creation time matches the state file so we can NEVER kill an innocent
        PID-reused process.
        """
        killed = False
        if holder_pid > 0 and _pid_alive(holder_pid):
            state = _read_state(self._port)
            st_ct = state.get("create_time") if state else None
            live_ct = _proc_create_time(holder_pid)
            same_process = st_ct is not None and live_ct is not None and abs(float(st_ct) - live_ct) <= 1.0
            if same_process:
                try:
                    proc = psutil.Process(holder_pid)
                    logger.warning(
                        "Single-instance: terminating non-functional holder pid=%s " "to reclaim lock/pipe/port",
                        holder_pid,
                    )
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        proc.kill()  # escalate if terminate didn't take
                        try:
                            proc.wait(timeout=5)
                        except psutil.Error:
                            pass
                    killed = True
                except psutil.Error:
                    logger.exception("Single-instance: failed to terminate holder pid=%s", holder_pid)
            else:
                # PID reused by a different process -> original holder already
                # gone; do NOT touch the innocent process. Fall through to file
                # cleanup + Qt stale removal.
                logger.info("Single-instance: holder pid=%s is a reused PID; original " "holder is gone", holder_pid)

        # Holder is now dead (killed, or already gone). Clean up leftover files.
        try:
            lock.removeStaleLockFile()
        except (RuntimeError, OSError):  # pragma: no cover - defensive
            pass
        for p in (Path(_lock_path(self._port)), _state_path(self._port)):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                # A just-terminated process can leave the handle briefly open;
                # the next tryLock will still succeed via Qt stale detection.
                logger.debug("Single-instance: could not unlink %s yet", p)
        if killed:
            time.sleep(0.3)  # let the OS release the pipe/port handles

    def _legacy_instance_alive(self) -> bool:
        """True if a PRE-UPGRADE build (QLocalServer-only, no QLockFile) is
        listening on the legacy deterministic socket. Used only on the
        tryLock-SUCCESS path (no new-build holder exists) so it can never shadow
        the zombie classification of a new-build holder."""
        client = QLocalSocket(self)
        client.connectToServer(_legacy_socket_name(self._port))
        alive = client.waitForConnected(300)
        if alive:
            client.abort()
        return alive

    # -- raise-existing-window client ----------------------------------------
    def _raise_existing_window(self) -> None:
        state = _read_state(self._port)
        pipe = None
        if state:
            pipe = state.get("pipe_name")
        for name in filter(None, (pipe, _legacy_socket_name(self._port))):
            client = QLocalSocket(self)
            client.connectToServer(name)
            if client.waitForConnected(500):
                logger.info("Viola already running. Focusing existing window.")
                client.write(RAISE_CMD)
                client.waitForBytesWritten(500)
                client.waitForReadyRead(_HANDSHAKE_WAIT_MS)
                client.disconnectFromServer()
                return
        logger.info("Viola already running (functioning holder); deferring.")

    # -- server side: handle an incoming raise request -----------------------
    @Slot()
    def _on_new_connection(self) -> None:
        server = self.sender() if isinstance(self.sender(), QLocalServer) else self._server
        if server is None:
            return
        conn = server.nextPendingConnection()
        if conn is None:
            return
        conn.waitForReadyRead(500)
        data = bytes(conn.readAll())
        if data == RAISE_CMD:
            raised = False
            if self._window_is_raisable() and self._window is not None:
                logger.info("Received RAISE_WINDOW from second instance")
                try:
                    self._window.showNormal()
                    self._window.raise_()
                    self._window.activateWindow()
                    raised = True
                except RuntimeError:
                    logger.exception("Failed to raise window on RAISE_WINDOW")
            try:
                conn.write(_ALIVE_REPLY if raised else _NOWINDOW_REPLY)
                conn.flush()
                conn.waitForBytesWritten(500)
            except RuntimeError:  # pragma: no cover - defensive
                pass
        conn.close()

    # -- cleanup --------------------------------------------------------------
    @Slot()
    def _cleanup(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.stop()
        # Only remove the state file if it is still OURS (don't clobber a holder
        # that reclaimed us).
        try:
            st = _read_state(self._port)
            if st and st.get("token") == self._token:
                _state_path(self._port).unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive
            pass
        if self._lock is not None:
            try:
                self._lock.unlock()
            except (RuntimeError, OSError):  # pragma: no cover - defensive
                pass


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_state(port: str) -> dict | None:
    path = _state_path(port)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        # missing, unreadable, or malformed JSON -> treat as no state
        return None
