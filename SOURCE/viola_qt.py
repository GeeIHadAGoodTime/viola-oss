#!/usr/bin/env python3

# Venv check — warn early if running with wrong Python
import os
import sys as _sys


def _install_frozen_safe_stdio() -> None:
    """Install discard streams for frozen windowed builds with no console."""
    if not getattr(_sys, "frozen", False):
        return

    def _replace_stream(stream_name: str) -> bool:
        try:
            if getattr(_sys, stream_name, None) is None:
                setattr(
                    _sys,
                    stream_name,
                    open(os.devnull, "w", encoding="utf-8"),
                )
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        return True

    for stream_name in ("stdout", "stderr", "__stdout__", "__stderr__"):
        _replace_stream(stream_name)


_install_frozen_safe_stdio()


def _boot_checkpoint(name: str) -> None:
    """Append a breadcrumb marking a boot stage reached, independent of
    stdout/stderr state.

    #1500: FIVE consecutive diagnostic fixes (_fatal_boot's crash-log file +
    stderr echo, the smoke script's native-crash-visibility env vars, the
    top-level SystemExit catcher, the config/sentry _fatal_boot wrap) all
    landed, were each individually confirmed reached in isolation, and STILL
    the Linux AppImage headless smoke's boot-survive step showed the exact
    same "a few early prints then silent rc=1" signature every single time --
    proving the crash happens somewhere in this module's large linear
    execution that none of those specific wraps happen to cover, AND that
    something about this frozen build's stdio may not be trustworthy for
    diagnostics (or the exit mechanism bypasses exception handling entirely).
    Rather than guess again which one specific line to wrap next, this
    breadcrumb trail is dropped at every major boot-stage boundary through to
    app.exec(): whichever one is LAST in the file on a failed run pins the
    crash to a narrow span deterministically, no guessing required. Uses only
    a plain file append (like _fatal_boot's crash log) -- no dependency on
    sys.stdout/stderr being valid, so it survives even if THOSE are broken.

    Each line carries a UTC wall clock and the pid (#4650). Without those the
    trail could say WHERE boot got to but never WHEN or in which process, so it
    was useless for timing an incident or for telling two overlapping runs
    apart -- the same undated-artifact gap that made the 2026-08-01/02 crash
    dumps unattributable. Stdlib-only and inside the same blanket swallow, so a
    breadcrumb still cannot crash the app.
    """
    try:
        import tempfile as _tf
        import time as _time

        stamp = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime()) + ".%03dZ" % (int(_time.time() * 1000) % 1000)
        with open(os.path.join(_tf.gettempdir(), "viola_boot_checkpoints.log"), "a", encoding="utf-8") as _f:
            _f.write("%s pid=%d %s\n" % (stamp, os.getpid(), name))
    except Exception:  # noqa: BLE001, S110, RUF100 - a breadcrumb must never itself crash the app
        pass


_boot_checkpoint("01-frozen-safe-stdio-installed")


def _run_frozen_dash_c_and_exit() -> None:
    """Frozen builds have no real ``python -c`` mode -- emulate one and exit.

    PyInstaller's bootloader ignores a literal ``-c`` in argv: it always
    re-runs the packaged entry script (this file) rather than executing the
    snippet, so any stdlib code path that shells out to "the current
    interpreter" with ``-c CODE`` and no ``--multiprocessing-fork`` marker
    boots an entire second Viola instance on a frozen build.

    Confirmed instance (#691, live-reproduced on the signed macOS bundle):
    ``multiprocessing.resource_tracker.ResourceTracker._launch`` execs
    ``[sys.executable, *interpreter_flags, "-c",
    "from multiprocessing.resource_tracker import main;main(N)"]`` with no
    fork marker at all -- neither of this module's other multiprocessing
    guards below (``__mp_main__`` / ``--multiprocessing-fork``) catch it, so
    every time the resource tracker (re)spawns, a frozen macOS bundle
    silently boots a whole new Viola.app instead of just running the
    tracker. That reproduces the ticket's exact signature: a graceful
    ``osascript`` quit AND a ``kill -TERM`` both leave the resource
    tracker's child alive long enough to hit this path and relaunch;
    ``kill -9`` kills that child outright before it gets the chance, which
    is why only ``-9`` stuck in manual testing. Ratchet:
    check-viola-qt-frozen-dash-c-guard.

    Must run before any Velopack/telemetry/Qt import: a literal ``-c``
    invocation has to behave like a real interpreter (execute the snippet,
    then exit) instead of booting the product.
    """
    if not getattr(_sys, "frozen", False):
        return
    argv = _sys.argv
    if "-c" not in argv:
        return
    index = argv.index("-c")
    if index + 1 >= len(argv):
        return
    code = argv[index + 1]
    _sys.argv = [argv[0], *argv[index + 2 :]]
    # nosec B102 - this is the frozen-build emulation of the real interpreter's
    # own -c handling: argv[index + 1] is never attacker/network-controlled,
    # it is always a snippet the CURRENT process's own stdlib generated for
    # itself (e.g. multiprocessing.resource_tracker._launch's hardcoded
    # "from multiprocessing.resource_tracker import main;main(N)"), exactly
    # the code the standard `python -c` entry point would execute if this
    # were an unfrozen interpreter.
    exec(compile(code, "<-c>", "exec"), {"__name__": "__main__"})  # nosec B102
    _sys.exit(0)


_run_frozen_dash_c_and_exit()
_boot_checkpoint("02-dash-c-guard-passed")


def _is_velopack_hook_invocation(argv: list[str] | None = None) -> bool:
    args = argv if argv is not None else _sys.argv
    return any(str(arg).startswith("--veloapp-") for arg in args[1:])


def _run_velopack_startup_hook_first() -> bool:
    """Run Velopack before any Viola imports or startup side effects."""
    if __name__ == "__mp_main__" or "--multiprocessing-fork" in _sys.argv:
        return False
    hook_invocation = _is_velopack_hook_invocation()
    if not hook_invocation and not getattr(_sys, "frozen", False):
        return False
    if not hook_invocation and _sys.platform.startswith("linux"):
        # Decision D2 (requirements_linux.txt): the Linux update path does NOT
        # use Velopack's native apply -- Linux ships via download-and-replace
        # AppImage instead (scripts/check_platform_download_wiring.py
        # proves that path end to end). Calling into the native extension here
        # anyway is a GUARANTEED failure on every single Linux launch: Velopack's
        # Rust locator (src/lib-rust/src/locator.rs in the velopack crate,
        # confirmed live against the pinned velopack==0.0.1589.dev41669 wheel)
        # hard-requires the resolved executable path to contain a '/usr/bin/'
        # segment, but our AppImage places the frozen binary at
        # usr/lib/viola/ViolaApp (build_appimage.sh's AppDir assembly +
        # deploy/appimage/AppRun's EXE path), never usr/bin/ -- so
        # velopack.App().run() prints/raises NotInstalled("Could not locate
        # '/usr/bin/' in executable path ...") on EVERY Linux launch, previously
        # swallowed below with only a `.debug()` breadcrumb. That is exactly the
        # "silent permanent failure" smell #1539/#1541 called out, and the
        # misleading "VelopackApp: Error..." print briefly misdirected the
        # #1571 boot-crash investigation toward a Velopack theory that live CI
        # then disproved. Skip the doomed call outright and log it loudly once,
        # instead of attempting-and-swallowing it on every boot (#1541).
        try:
            import logging

            logging.getLogger(__name__).info(
                "Velopack startup hook skipped on Linux: decision D2 -- Linux "
                "auto-update is download-and-replace, not native Velopack "
                "apply (see requirements_linux.txt, #1541)"
            )
        except Exception:  # noqa: BLE001, S110, RUF100 - logging must never crash this fail-open path
            pass
        return False

    try:
        import velopack  # type: ignore[import-untyped]  # VIOLA-000: velopack ships no type stubs
    except Exception:
        if hook_invocation:
            raise
        return False

    try:
        app = velopack.App()
        if callable(getattr(app, "set_auto_apply_on_startup", None)):
            app.set_auto_apply_on_startup(False)
        app.run()
    except BaseException as exc:  # noqa: BLE001, RUF100 - see below; must never kill a non-hook launch
        # velopack's native App.run() binding does not always signal "not a
        # Velopack-installed layout" as a catchable Exception subclass -- on at
        # least one platform binding it raises/propagates SystemExit (a
        # BaseException, deliberately NOT caught by `except Exception:`), which
        # this function's own contract already promises never happens: it must
        # "run Velopack before any Viola imports" and fail open into a normal
        # launch when App.run() cannot resolve an install (every non-Velopack-
        # installer launch -- every dev run, every Inno Setup install, every
        # Linux AppImage, since this product ships neither a Velopack Setup.exe
        # nor a Velopack Linux installer). `except Exception:` silently violated
        # that contract whenever the failure surfaced as SystemExit, killing the
        # WHOLE app before Qt ever starts with no traceback (viola_boot_crash.log
        # is never reached because SystemExit isn't routed through the normal
        # except-and-log path either) -- confirmed live on the Linux AppImage
        # headless smoke (main run 29332068457/29336136287, #1518): the process
        # exits rc=1 with only Velopack's own "NotInstalled" diagnostic print and
        # nothing else, immediately after `app.run()`.
        if hook_invocation:
            raise
        if not isinstance(exc, Exception):
            # Diagnostic only (never fails hook_invocation=False open-ness): log
            # the actual BaseException subclass so a future regression here is
            # provable from a real log line instead of re-derived from scratch.
            try:
                import logging

                logging.getLogger(__name__).debug(
                    "Velopack startup hook raised a non-Exception BaseException (%s: %s); "
                    "treated as fail-open, not a Velopack-installed layout",
                    type(exc).__name__,
                    exc,
                )
            except Exception:  # noqa: BLE001, S110, RUF100 - logging must never itself crash this fail-open path
                pass
        return False
    return True


_VELOPACK_STARTUP_HOOK_RAN = _run_velopack_startup_hook_first()
_boot_checkpoint("03-velopack-hook-returned")


def _mark_release_health_clean_exit(exit_code: int = 0) -> None:
    return None


def _start_sentry_session_for_active() -> None:
    return None


_RELEASE_HEALTH_SESSION = None
if not (_is_velopack_hook_invocation() or __name__ == "__mp_main__" or "--multiprocessing-fork" in _sys.argv):
    # Release-health is non-essential telemetry that runs here BEFORE the
    # single-instance lock, so on first run two processes can reach it at once and
    # race on the marker file. It must never crash Viola's launch, so the whole
    # init is guarded: any failure leaves telemetry inert and lets the app boot.
    try:
        from telemetry.release_health_session import (
            mark_release_health_clean_exit as _real_mark_release_health_clean_exit,
            start_release_health_session as _start_release_health_session,
            start_sentry_session_for_active as _real_start_sentry_session_for_active,
        )

        from core.constants import VIOLA_VERSION as _VIOLA_RELEASE_HEALTH_VERSION

        _RELEASE_HEALTH_SESSION = _start_release_health_session(_VIOLA_RELEASE_HEALTH_VERSION)
        _mark_release_health_clean_exit = _real_mark_release_health_clean_exit
        _start_sentry_session_for_active = _real_start_sentry_session_for_active
    except Exception:  # noqa: BLE001, RUF100 - telemetry must never crash Viola's launch
        _RELEASE_HEALTH_SESSION = None

import pathlib as _pathlib
import threading as _obs_threading
from ipaddress import ip_address


def _is_windows_multiprocessing_child_import() -> bool:
    """Return True for Windows multiprocessing child-import contexts."""
    if os.name != "nt":
        return False
    return __name__ == "__mp_main__" or "--multiprocessing-fork" in _sys.argv


def _detach_windows_child_console() -> None:
    """Hide/free the transient console allocated for Windows child imports."""
    if not _is_windows_multiprocessing_child_import():
        return

    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
            ctypes.windll.kernel32.FreeConsole()
    except (AttributeError, OSError):
        import logging

        logging.getLogger(__name__).debug("child console detach failed (harmless)", exc_info=True)

    try:
        _sys.stdin = open(os.devnull)
        _sys.stdout = open(os.devnull, "w")
        _sys.stderr = open(os.devnull, "w")
    except OSError:
        import logging

        logging.getLogger(__name__).debug("child stdio redirect to devnull failed (harmless)", exc_info=True)


_detach_windows_child_console()
_suppress_import_console_output = _is_windows_multiprocessing_child_import()

from core.platform import (
    configure_environment as _configure_viola_environment,
    get_logs_dir as _get_viola_logs_dir,
)

_boot_checkpoint("04-core-platform-imported")


def _fatal_boot(stage: str, exc: BaseException) -> None:
    """Surface a pre-UI startup failure instead of dying silently.

    The app is frozen with ``console=False``, so any unhandled exception before
    the Qt window exists produces no window and no error — the user just sees
    "nothing happens" (the clean-machine launch bug). This last-resort guard
    writes the traceback to a guaranteed-writable temp file, ALWAYS echoes it
    to stderr, and, on Windows, also shows a native message box, then exits
    non-zero. It depends only on the stdlib so it still works when Viola's own
    state/paths are broken.

    The unconditional stderr echo (added 2026-07-14, ticket #1500) matters on
    every non-Windows platform too: ``console=False`` means no real end user
    has a terminal watching that fd, so it is harmless there (same as before),
    but any process supervisor that DOES capture stdout/stderr -- CI's
    xvfb-run-redirected boot.log, systemd/journald, `nohup`, Docker logs --
    gets the real traceback immediately instead of a bare non-zero exit with
    zero diagnostic content. That silence is exactly what turned the Linux
    AppImage headless smoke's boot-crash failures into an unreadable "rc=1,
    no further output" for every layer of this chain so far -- the crash log
    file existed all along, just never surfaced anywhere the CI log captures.
    """
    import sys as _local_sys
    import tempfile
    import traceback

    # Local `import sys` (not the module-level `_sys` alias): this function is
    # also called AFTER `del _sys` runs further down the module, so binding to
    # the deleted module global would raise NameError on that later call path.
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    crash_path = None
    try:
        crash_path = os.path.join(tempfile.gettempdir(), "viola_boot_crash.log")
        with open(crash_path, "a", encoding="utf-8") as fh:
            fh.write(f"--- boot failure during {stage} ---\n{detail}\n")
    except OSError as log_exc:
        detail += f"\n(could not write crash log: {log_exc})"
        crash_path = None
    if not _suppress_import_console_output:
        try:
            _local_sys.stderr.write(f"FATAL BOOT FAILURE during {stage}:\n{detail}\n")
            if crash_path:
                _local_sys.stderr.write(f"(also written to {crash_path})\n")
            _local_sys.stderr.flush()
        except (OSError, ValueError):
            # stderr may already be closed/redirected to devnull (e.g. a detached
            # Windows multiprocessing child) -- the crash log file above is still
            # the durable record; nothing further to do here.
            pass
    if os.name == "nt" and not _suppress_import_console_output:
        try:
            import ctypes

            msg = f"Viola failed to start during {stage}:\n\n{exc}"
            if crash_path:
                msg += f"\n\nDetails written to:\n{crash_path}"
            ctypes.windll.user32.MessageBoxW(None, msg, "Viola — startup error", 0x10)
        except (AttributeError, OSError) as ui_exc:
            # No user32/display surface — the crash log written above is the only
            # channel; record that the dialog could not be shown and move on.
            detail += f"\n(could not show startup error dialog: {ui_exc})"
    raise SystemExit(1)


def _fatal_boot_unless_clean_exit(stage: str, exc: BaseException) -> None:
    """Route any exception from a wrapped boot step to _fatal_boot() -- EXCEPT
    a clean/intentional SystemExit(0 or None), which is left to propagate
    normally.

    #1500 root cause: every ``except Exception as _boot_exc:`` guard around a
    boot step (this one included, before this fix) let a bare
    ``SystemExit`` sail straight past it -- ``SystemExit`` is a
    ``BaseException`` subclass, NOT an ``Exception`` subclass, by design (so
    that a plain ``except Exception:`` never accidentally swallows an
    intentional exit). That is exactly why FIVE consecutive fixes to
    _fatal_boot's own diagnostics (stderr echo, crash-log file, the
    native-crash env vars, the top-level SystemExit catcher, wrapping the
    config/console/sentry block with a plain ``except Exception:``) never
    produced a single byte of output: the real failure IS a SystemExit
    raised from inside a wrapped block (traced to the checkpoint trail
    stopping dead between "08-faulthandler-enabled" and
    "09-config-console-sentry-done" with no crash-log file written -- the
    signature of an escaped BaseException, not a caught Exception), and
    ``except Exception:`` was structurally incapable of ever catching it.

    Call sites use ``except BaseException as _boot_exc:`` and delegate here
    so this exact class of gap cannot reopen at a fourth wrapped step.
    """
    if isinstance(exc, SystemExit) and exc.code in (0, None):
        raise exc
    _fatal_boot(stage, exc)


try:
    _configure_viola_environment()
except BaseException as _boot_exc:  # noqa: BLE001, RUF100 - last-resort boot guard; surface, never silently die
    _fatal_boot_unless_clean_exit("environment setup", _boot_exc)
_boot_checkpoint("05-environment-configured")

_expected_venv = _pathlib.Path(__file__).parent / ".venv"
if not _suppress_import_console_output and not _sys.executable.startswith(str(_expected_venv)):
    print(f"WARNING: Running with {_sys.executable}")  # noqa: T201
    print(f"Expected venv: {_expected_venv}")  # noqa: T201
    print(r"Activate with: .\.venv\Scripts\Activate.ps1")  # noqa: T201

del _sys, _pathlib, _expected_venv
_boot_checkpoint("06-venv-check-done")


# Must stay module-level: DLL load order conflict with Qt C++ runtime.
# Importing onnxruntime first claims the native DLLs before Qt/config loads
# conflicting runtimes.
try:
    import onnxruntime
except ImportError:
    import logging

    logging.getLogger(__name__).debug("onnxruntime preload skipped (not installed in this build)", exc_info=True)
_boot_checkpoint("07-onnxruntime-preload-done")


# Enable faulthandler so segfaults / access violations produce a Python
# traceback instead of silent death.  Must run before Qt or any C extension
# that could crash.  Output to a file since the runner discards stderr.
import faulthandler

# core.crash_forensics owns the writable-logs-dir resolution (per-user on
# installed builds, NOT the install directory — writing the crash log under
# Program Files was itself a silent-boot-death cause), plus the three things a
# bare faulthandler.enable() could not give us when three hard crashes landed on
# 2026-08-01/02 and none of them could be root-caused (#4650):
#
#   * a RUN-SCOPED log with a dated header, so a dump is tied to a wall clock
#     and a build instead of joining 89 undated dumps in one shared file;
#   * a loaded-module map, so a bare faulting address resolves to module+offset;
#   * on Windows, silent-queued WER instead of a modal hard-error dialog that
#     SUSPENDS the process (which is why WER never completed a bucket for any of
#     the three, and why the app sat there looking half-alive).
#
# enable() stays HERE rather than moving inside that module on purpose: the Qt
# crash handler must remain visibly stdlib faulthandler — stack frames only,
# never locals — because payment data lives in this process
# (tests/security/test_payment_pan_never_in_exceptions.py).
#
# Multiprocessing children re-import this module and so reach this line too.
# They get their OWN component name deliberately: retention is per-component,
# so if children shared "viola_qt" a burst of them could evict the desktop
# process's own crash log — silently destroying the artifact this whole change
# exists to preserve. Separate bucket, same diagnostics, no eviction.
try:
    from core.crash_forensics import install_crash_forensics as _install_crash_forensics

    _crash_forensics = _install_crash_forensics("viola_qt_mp_child" if _suppress_import_console_output else "viola_qt")
    faulthandler.enable(file=_crash_forensics.stream, all_threads=True)
except BaseException as _boot_exc:  # noqa: BLE001, RUF100 - last-resort boot guard; surface, never silently die
    _fatal_boot_unless_clean_exit("crash-handler setup", _boot_exc)
_boot_checkpoint("08-faulthandler-enabled")

# CRITICAL: Load .env file FIRST before any other imports
# This ensures OPENAI_API_KEY and other env vars are available
#
# This block (config.settings / core.console / sentry_init / the module-level
# init_sentry() call) sits between two _fatal_boot-wrapped steps
# (_configure_viola_environment, faulthandler setup) but was itself entirely
# UNGUARDED -- any exception here propagated straight to the top of the
# module's linear execution, never reaching `if __name__ == "__main__":` at
# all (so _run_entrypoint_with_top_level_diagnostics() never got a chance to
# run either). #1500: the Linux AppImage headless smoke's "exited early
# (rc=1), zero diagnostic output anywhere" kept recurring after FOUR
# consecutive fixes each closed one specific diagnostic gap (_fatal_boot's
# stderr echo, the smoke script's native-crash-visibility env vars, the
# top-level SystemExit catcher) -- this module-level gap between two ALREADY
# -wrapped _fatal_boot steps is the remaining one. Wrap it the same way.
try:
    import logging as _bootstrap_logging

    import config.settings
    from core.console import console
    from services.sentry_init import init_sentry

    if not _suppress_import_console_output:
        init_sentry("viola_qt")
        _start_sentry_session_for_active()
except BaseException as _boot_exc:  # noqa: BLE001, RUF100 - last-resort boot guard; surface, never silently die
    _fatal_boot_unless_clean_exit("config/console/sentry init", _boot_exc)
_boot_checkpoint("09-config-console-sentry-done")

# SELF-HEALING: Clear stale bytecode caches at startup (gated, rare)
if os.environ.get("VIOLA_CLEAN_CACHE") == "1":
    try:
        from bootstrap.cache_cleaner import startup_cache_clean

        startup_cache_clean()
    except Exception as e:
        _bootstrap_logging.debug("Cache cleaner failed (non-critical): %s", e)
_boot_checkpoint("10-self-healing-cache-clean-done")


"""
Viola Native Qt - Entry Point
Beautiful, modular, zero-flickering native desktop application

This is an alternative to viola_desktop.py that uses native Qt widgets
instead of embedding the web UI. Benefits:
- ZERO flickering (no compositor sync issues)
- Lower memory usage (~100-150MB less)
- Faster startup (~2-3 seconds faster)
- Easier debugging (pure Python, no JavaScript)
- Better system integration

The backend (FastAPI) remains the same - this just replaces the UI layer.
"""

import sys

_HEADLESS_FLAG = "--headless"
_QT_ONLY_FLAGS_WITH_VALUE = {"--remote-debugging-port"}
_QT_ONLY_FLAGS = {_HEADLESS_FLAG, "--no-wake"}
_CDP_LOOPBACK_BIND_HOST = "127.0.0.1"


def _headless_requested(argv: list[str] | None = None) -> bool:
    args = sys.argv[1:] if argv is None else argv[1:]
    return _HEADLESS_FLAG in args


def _daemon_argv_from_qt_args(argv: list[str]) -> list[str]:
    daemon_argv = [argv[0]]
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in _QT_ONLY_FLAGS:
            continue
        if arg in _QT_ONLY_FLAGS_WITH_VALUE:
            skip_next = True
            continue
        if any(arg.startswith("%s=" % flag) for flag in _QT_ONLY_FLAGS_WITH_VALUE):
            continue
        daemon_argv.append(arg)
    return daemon_argv


def _run_headless_daemon() -> None:
    from services.daemon.viola_daemon import main as daemon_main

    sys.argv = _daemon_argv_from_qt_args(sys.argv)
    daemon_main()


def _cdp_host_is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_cdp_debugging_bind(raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    host = _CDP_LOOPBACK_BIND_HOST
    port_text = value
    if ":" in value:
        if value.startswith("[") and "]:" in value:
            host, port_text = value[1:].split("]:", 1)
        else:
            host, port_text = value.rsplit(":", 1)
    if not _cdp_host_is_loopback(host):
        raise ValueError("CDP remote debugging must bind to loopback, got %s" % host)
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("CDP remote debugging port must be an integer") from exc
    if port <= 0 or port > 65535:
        raise ValueError("CDP remote debugging port is out of range")
    return "%s:%d" % (_CDP_LOOPBACK_BIND_HOST, port)


# Parse --remote-debugging-port BEFORE any Qt imports.
# QTWEBENGINE_REMOTE_DEBUGGING must be set before QApplication is created.
_cdp_port = None
for _arg in sys.argv[1:]:
    if _arg.startswith("--remote-debugging-port="):
        _cdp_port = _arg.split("=", 1)[1]
    elif _arg == "--remote-debugging-port" and sys.argv.index(_arg) + 1 < len(sys.argv):
        _cdp_port = sys.argv[sys.argv.index(_arg) + 1]

# Also enable CDP from explicit config if not set via CLI flag.
# Chromium DevTools Protocol has no Viola auth layer, so default settings keep
# it disabled unless the operator explicitly sets VIOLA_CDP_PORT.
if not _cdp_port:
    try:
        from config.settings import settings as _cfg

        if getattr(_cfg, "cdp_port", 0):
            _cdp_port = str(_cfg.cdp_port)
    except Exception:
        _bootstrap_logging.debug("Could not read cdp_port from settings, CDP disabled")

# -- Chromium stability flags (MUST be set before QApplication creation) -------
# AudioServiceOutOfProcess: Keeps Chromium's audio in Viola's main process.
# WINDOWS-ONLY (#2423). The only reason for this flag is ProcTap per-process
# WASAPI loopback capture (single PID target instead of hunting child renderer
# PIDs) -- ProcTap is a Windows-only capability (pycaw/WASAPI COM APIs, see
# audio_core/capture/hub_audio_controller.py), so nothing on Linux/macOS
# depends on Chromium's audio staying in-process. Forcing it in-process on
# those platforms only creates a race: Chromium's own ALSA usage shares
# libasound's global config with our sounddevice/pyaudio boot threads -- the
# leg #1663 (sounddevice boot-serialization) could not close, because a
# Python-side lock cannot reach Chromium's native audio thread. Leaving the
# flag unset on Linux/macOS lets Chromium use its upstream-default
# out-of-process audio service, which removes that race structurally
# (separate process = separate libasound state).
# Originally added to fix alleged crashes ~40s into YouTube playback, but
# those crashes were likely false flags caused by agents restarting Viola
# concurrently (no isolated reproduction evidence exists; Falcon's 2026-03-04
# test ran WITH this flag for 12+ seconds of mute/unmute cycles with zero
# crashes). Kept on Windows for ProcTap simplicity.
# QT_OPENGL=software forces Qt's rendering to use Mesa llvmpipe instead of the
# GPU driver, preventing native access violations in OpenGL threads that
# --disable-gpu alone does not prevent (Chromium flag vs Qt rendering pipeline).
os.environ.setdefault("QT_OPENGL", "software")
_chromium_stability_flags = "--autoplay-policy=no-user-gesture-required --disable-gpu"
if sys.platform == "win32":
    # ProcTap needs Chromium's audio kept in-process to target a single PID;
    # Linux/macOS have no ProcTap consumer, so they keep Chromium's default
    # out-of-process audio service instead (see comment block above).
    _chromium_stability_flags = "--disable-features=AudioServiceOutOfProcess " + _chromium_stability_flags
# Linux: QtWebEngine's Chromium render process uses a SUID sandbox helper
# (chrome-sandbox) that is frequently unavailable or mis-permissioned inside
# VMs, containers, and AppImage bundles. When it can't initialize, the render
# process dies on launch and the React UI never paints (the QMainWindow still
# shows, but its WebEngine surface is blank). Disabling the Chromium sandbox is
# the standard desktop-AppImage workaround; the app already runs with the
# invoking user's full privileges, so the sandbox adds little for a local
# single-user desktop process. Gated to Linux only so Windows/macOS keep the
# sandbox. Operators who ship a correctly-SUID chrome-sandbox can opt back in by
# pre-setting QTWEBENGINE_DISABLE_SANDBOX=0 in the environment.
# NOTE (cross-lane / L3 packaging): the durable fix is to bundle a correctly
# permissioned chrome-sandbox in the AppImage; this flag is the safe default
# until that lands and must be re-verified on a real Ubuntu VM.
if sys.platform.startswith("linux") and os.environ.get("QTWEBENGINE_DISABLE_SANDBOX") != "0":
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    if "--no-sandbox" not in _chromium_stability_flags:
        _chromium_stability_flags = _chromium_stability_flags + " --no-sandbox"
_existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
# Merge: keep any flags already set (e.g. by caller or .env) and append ours
for _flag in _chromium_stability_flags.split():
    if _flag.split("=")[0] not in _existing_flags:
        _existing_flags = (_existing_flags + " " + _flag).strip()
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _existing_flags

_raw_cdp_debugging = _cdp_port or os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING", "")
try:
    _cdp_debugging_bind = _normalize_cdp_debugging_bind(_raw_cdp_debugging)
except ValueError as _cdp_error:
    _bootstrap_logging.error("Refusing unsafe Qt WebEngine CDP configuration: %s", _cdp_error)
    sys.stderr.write("Unsafe Qt WebEngine CDP configuration: %s\n" % _cdp_error)
    sys.exit(2)
if _cdp_debugging_bind:
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = _cdp_debugging_bind


logger = _bootstrap_logging.getLogger(__name__)


def _resolve_configured_log_level() -> int:
    """Return the numeric logging level from AppConfig.log_level (#4863).

    Best-effort: falls back to INFO (the AppConfig default, config/settings.py)
    if settings aren't importable yet at this point in boot. Mirrors the
    established early-bootstrap pattern above (``from config.settings import
    settings as _cfg``) rather than ``get_settings()``, which rebuilds
    AppConfig from scratch and runs one-time side effects (secret-presence
    logging) best left to their normal call site.
    """
    import logging as _logging

    try:
        from config.settings import settings as _cfg

        level_name = str(getattr(_cfg, "log_level", "INFO") or "INFO").upper()
    except (ImportError, AttributeError):
        return _logging.INFO
    resolved = getattr(_logging, level_name, None)
    return resolved if isinstance(resolved, int) else _logging.INFO


def _ensure_qt_file_logging() -> None:
    """Add a file handler so Qt process logs survive DEVNULL subprocess redirect.

    viola_control.py launches this process with stdout/stderr=DEVNULL because
    QtWebEngine audio breaks when stdout/stderr are redirected to files (Chromium
    child processes inherit the file handles).  Without an explicit file handler,
    all Python logging from this process is lost.

    This adds a RotatingFileHandler to the ``viola`` root logger writing to
    ``logs/structured/viola-qt.log`` (50 MB per file, 3 backups).  If the
    observability module already attached a file handler, this is a no-op.
    """
    import logging as _logging
    from logging.handlers import RotatingFileHandler as _RotatingFileHandler

    root_viola = _logging.getLogger("viola")

    # Skip if a file handler already exists (observability module set one up)
    for h in root_viola.handlers:
        if isinstance(h, _logging.FileHandler):
            return

    try:
        # Writable per-user logs dir on frozen installs — NOT dirname(__file__),
        # which is the read-only _internal install dir for a frozen build.
        log_dir = str(_get_viola_logs_dir() / "structured")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "viola-qt.log")

        # One-time recovery: if the log file exceeds 100MB, rename it so
        # RotatingFileHandler starts fresh.  Rotation will prevent recurrence.
        _MAX_RECOVERY_SIZE = 100 * 1024 * 1024  # 100 MB
        try:
            if os.path.isfile(log_file) and os.path.getsize(log_file) > _MAX_RECOVERY_SIZE:
                old_file = log_file + ".old"
                if os.path.isfile(old_file):
                    os.remove(old_file)
                os.rename(log_file, old_file)
        except OSError:
            logger.debug("recovery log rotation failed (best-effort; rotation caps growth regardless)", exc_info=True)

        handler = _RotatingFileHandler(
            log_file,
            maxBytes=50 * 1024 * 1024,  # 50 MB per file
            backupCount=3,  # Keep 3 backups (200 MB total cap)
            encoding="utf-8",
        )
        # Handler stays at DEBUG so it captures everything the logger's
        # effective level lets through (including a submodule that opts
        # itself into DEBUG for targeted debugging) — this does NOT force
        # DEBUG output on its own; only the logger's own level does that.
        handler.setLevel(_logging.DEBUG)
        handler.setFormatter(
            _logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root_viola.addHandler(handler)
        # Only set a level if nothing has configured one yet (#4863): the
        # previous logic force-set DEBUG whenever the level was NOTSET *or*
        # coarser than DEBUG, which is true on every shipped install (nothing
        # else sets the "viola" logger's level), so it silently overrode
        # config/settings.py's log_level=INFO default on every run and spun
        # short-poll loops (e.g. music/runtime/worker_manager.py's
        # _wait_for_work_item) into a 10Hz DEBUG log flood while idle. Honor
        # the configured level instead of hardcoding DEBUG.
        if root_viola.level == _logging.NOTSET:
            root_viola.setLevel(_resolve_configured_log_level())
    except Exception as _log_setup_err:
        # Cannot use logger here — logging setup itself failed. Write to stderr
        # so the error is visible in development but never crashes the app.
        import sys as _sys_log

        _sys_log.stderr.write(f"WARNING: Qt file logging setup failed: {_log_setup_err}\n")


_ensure_qt_file_logging()
if not _suppress_import_console_output:
    logger.info("Qt process file logging initialized (viola-qt.log)")
_boot_checkpoint("11-qt-file-logging-done")


from typing import Callable

# Optional dependency type definitions
_GetDebugBusFn = Callable[[], object]
_ConfigureObservabilityFn = Callable[..., object]
_InstallDebugEventMirrorFn = Callable[..., Callable[[], None] | None]

_get_debug_bus: _GetDebugBusFn | None = None
_configure_observability: _ConfigureObservabilityFn | None = None
_install_debug_event_mirror: _InstallDebugEventMirrorFn | None = None

# Ensure Qt uses software rendering if needed (uncomment for GPU issues)
# os.environ['QT_QPA_PLATFORM'] = 'windows:darkmode=1'


def _emit_debug(signal_name: str, payload: dict) -> None:
    if _get_debug_bus is None:
        return
    try:
        bus = _get_debug_bus()
        signal = getattr(bus, signal_name, None)
        if signal is not None:
            signal.emit(payload)
    except Exception as e:
        # Debug instrumentation must never crash startup
        logger.debug("Failed to emit debug signal %s: %s", signal_name, e)


def _wire_video_widget(window: "ViolaWebViewWindow", bootstrap) -> None:
    """Connect the window's QVideoWidget to the music backend.

    Enables local video file rendering in the QVideoWidget. The backend
    remains UI-agnostic — it receives the widget via dependency injection.
    """
    video_widget = window.get_video_widget()
    if video_widget is None:
        logger.debug("No video widget available, skipping video wiring")
        return

    try:
        music_service = getattr(bootstrap, "music", None)
        if music_service is None:
            return

        player = getattr(music_service, "player", None)
        if player is None:
            return

        # Store video widget ref on the player for re-wiring on backend changes
        player._video_widget_ref = video_widget

        backend = getattr(player, "_backend", None)
        if backend is not None and hasattr(backend, "set_video_output"):
            backend.set_video_output(video_widget)
            logger.info(
                "Video widget wired to backend: %s",
                type(backend).__name__,
            )

            # Connect video_available_changed signal to UI
            bridge = getattr(backend, "video_signal_bridge", None)
            if bridge is not None:
                bridge.video_available_changed.connect(
                    lambda has_video: (window.show_video_surface() if has_video else window.show_webview_surface())
                )
                logger.info("Video signal bridge connected to window")
        else:
            logger.debug(
                "Backend does not support video output: %s",
                type(backend).__name__ if backend else "None",
            )
    except Exception as e:
        logger.warning("Failed to wire video widget: %s", e)


def _wire_browser_webview(window: "ViolaWebViewWindow", bootstrap) -> None:
    """Store the dedicated browser QWebEngineView reference for browser-native playback.

    The browser provider's BrowserPlaybackController needs a QWebEngineView
    to navigate to music service pages.  We use the dedicated browser webview
    (stack index 2) — NOT the main React webview (stack index 0).

    The webview reference is stored on the player so the embedded_playback
    code can wire it when the browser engine is first attached.
    """
    webview = getattr(window, "browser_webview", None)
    if webview is None:
        logger.debug("No dedicated browser webview available, skipping browser provider wiring")
        return

    from config.settings import settings as _cfg

    if not _cfg.browser_provider_enabled:
        return

    try:
        music_service = getattr(bootstrap, "music", None)
        if music_service is None:
            return

        player = getattr(music_service, "player", None)
        if player is None:
            return

        # Store dedicated browser webview ref for lazy wiring
        player._browser_webview_ref = webview
        logger.info("Dedicated browser webview reference stored on player for browser-native provider")

        # Attach the auth/login controller before the user clicks Connect Spotify.
        from music.runtime.embedded_playback import (
            wire_browser_auth_controller_at_bootstrap,
        )

        wire_browser_auth_controller_at_bootstrap(player)

        # Store CEF widget reference for CefBrowserManager windowed rendering
        cef_widget = getattr(window, "cef_widget", None)
        if cef_widget is not None:
            player._cef_widget_ref = cef_widget
            logger.info("CEF video container reference stored on player")
    except Exception as e:
        logger.warning("Failed to wire browser webview: %s", e)


def _wire_browser_overlay_controller(window: "ViolaWebViewWindow", bootstrap) -> None:
    """Create and wire the BrowserOverlayController to the auth manager.

    The overlay controller bridges the Qt show/hide overlay methods with
    the auth manager login flow and the React UI via WebSocket broadcast.
    """
    if getattr(window, "_browser_webview", None) is None:
        logger.debug("No browser webview, skipping overlay controller wiring")
        return

    try:
        from services.browser_overlay_controller import (
            BrowserOverlayController,
            set_overlay_controller,
        )

        # WebSocket broadcast function (sends overlay state to React). The
        # desktop backend exposes EventHub on app.state, not bootstrap.ws_hub.
        broadcast_fn = None
        app = getattr(bootstrap, "app", None)
        event_hub = getattr(getattr(app, "state", None), "event_hub", None) if app is not None else None
        default_user_id = None
        if event_hub is not None and hasattr(event_hub, "broadcast"):
            import asyncio

            from core.asyncio_safe import get_main_loop
            from core.user_context import get_current_or_device_user_id

            default_user_id = get_current_or_device_user_id()

            def _broadcast_overlay(payload: dict, *, user_id: str | None = None) -> None:
                target_user_id = user_id or default_user_id
                send_payload = dict(payload)
                send_payload.pop("type", None)
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    running_loop = None
                if running_loop is not None:
                    running_loop.create_task(
                        event_hub.broadcast(
                            "browser_overlay_state",
                            send_payload,
                            user_id=target_user_id,
                            force=True,
                        )
                    )
                    return
                loop = get_main_loop()
                if loop is not None and loop.is_running() and not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        event_hub.broadcast(
                            "browser_overlay_state",
                            send_payload,
                            user_id=target_user_id,
                            force=True,
                        ),
                        loop,
                    )
                    return
                logger.debug("No running EventHub loop for browser overlay broadcast")

            broadcast_fn = _broadcast_overlay

        overlay = BrowserOverlayController(
            show_fn=window.show_browser_overlay,
            hide_fn=window.hide_browser_overlay,
            broadcast_fn=broadcast_fn,
            default_user_id=default_user_id,
        )

        # Register as module-level singleton (used by AgentExecutor)
        set_overlay_controller(overlay)

        # Wire to auth manager so initiate_login() shows the overlay
        from music.providers.browser.auth_manager import get_browser_auth_manager

        auth_mgr = get_browser_auth_manager()
        auth_mgr.set_overlay_controller(overlay)

        # Store on window for access from ViolaBridge (close button)
        window._overlay_controller = overlay

        logger.info("BrowserOverlayController created and wired to auth manager")
    except Exception as exc:
        logger.warning("Failed to wire browser overlay controller: %s", exc)


def _wire_frame_streamer(window: "ViolaWebViewWindow", bootstrap) -> None:
    """Create and wire the AgentFrameStreamer for spoke viewport streaming.

    The frame streamer captures JPEG frames from the browser QWebEngineView
    and streams them to spokes via binary WebSocket messages so spokes can
    see the agent working in real-time.
    """
    if getattr(window, "_browser_webview", None) is None:
        logger.debug("No browser webview, skipping frame streamer wiring")
        return

    try:
        from services.agent_frame_streamer import (
            AgentFrameStreamer,
            set_frame_streamer,
        )

        streamer = AgentFrameStreamer()
        streamer.set_webview(window._browser_webview)

        # Wire broadcast function: sends binary JPEG to all room-subscribed spokes
        # EventHub lives on the FastAPI app state, accessible via bootstrap.app
        event_hub = None
        app = getattr(bootstrap, "app", None)
        if app is not None:
            state = getattr(app, "state", None)
            if state is not None:
                event_hub = getattr(state, "event_hub", None)
        if event_hub is not None and hasattr(event_hub, "broadcast_binary_to_rooms"):
            streamer.set_broadcast_fn(event_hub.broadcast_binary_to_rooms)

            def _count_spokes(*, user_id: str | None = None) -> int:
                room_clients: set[object] = set()
                for clients in getattr(event_hub, "_room_clients", {}).values():
                    room_clients.update(clients)
                if not user_id:
                    return len(room_clients)
                user_clients = getattr(event_hub, "_user_clients", {}).get(user_id, set())
                return sum(1 for ws in room_clients if ws in user_clients)

            streamer.set_spoke_count_fn(_count_spokes)

        # Give the streamer the event loop for scheduling async broadcasts
        import asyncio

        from core.asyncio_safe import get_main_loop

        try:
            loop = get_main_loop()
            if loop is None or loop.is_closed():
                loop = asyncio.get_event_loop()
            if loop is not None:
                streamer.set_event_loop(loop)
        except RuntimeError:
            logger.debug("No event loop for frame streamer, will set later")

        set_frame_streamer(streamer)
        logger.info("AgentFrameStreamer created and wired")
    except Exception as exc:
        logger.warning("Failed to wire frame streamer: %s", exc)


def _wire_cdp_browser_server(window: "ViolaWebViewWindow") -> None:
    """Pass the browser webview to the CDP browser MCP server.

    Desktop agentic browsing always uses the embedded CDP browser, so the
    CDP browser tools need a reference to the Qt ``QWebEngineView`` for
    OS-level screenshots via ``AgentPerception``.
    """
    from config.settings import settings as _cfg

    if not _cfg.cdp_port:
        return

    webview = getattr(window, "browser_webview", None)
    if webview is None:
        logger.debug("No browser webview for CDP server wiring")
        return

    try:
        from mcp_servers.browser_cdp.server import set_webview

        set_webview(webview)
        logger.info("CDP browser MCP server: webview reference wired")
    except Exception as exc:
        logger.warning("Failed to wire CDP browser server webview: %s", exc)


def _preload_phone_stt_at_startup() -> None:
    """Warm phone Whisper before the desktop can initiate a call."""
    try:
        from telephony.call_manager import preload_phone_stt

        preload_phone_stt()
    except Exception as exc:
        logger.warning("Failed to preload phone STT at startup: %s", exc)


# F-010: keep the coordinator-mode rejection list in one place so the guard
# is easy to extend if Round-4 buddy/swarm support lands or new env vars are
# added.
_CLAUDE_COORDINATOR_MODE_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_COORDINATOR_MODE",
    "CLAUDE_COORDINATOR_MODE",
    "ANTHROPIC_COORDINATOR_MODE",
)


def _reject_coordinator_mode_invocation() -> None:
    """Fail closed when an environment requests Claude coordinator mode.

    Claude's coordinator-mode (coordinator/coordinatorMode.ts) changes
    prompt/tool context and resume behavior in a way Viola does not
    implement. Booting silently as a normal session would hand the caller
    a control plane it didn't ask for. Reject before any Qt/services init.
    """

    import os

    enabled = []
    for env_name in _CLAUDE_COORDINATOR_MODE_ENV_VARS:
        value = os.environ.get(env_name, "")
        if value and value.strip().lower() not in {"", "0", "false", "no", "off"}:
            enabled.append((env_name, value))
    if not enabled:
        return
    message = (
        "Claude coordinator-mode is not implemented in Viola. "
        "Refusing to boot with %s set; unset the variable(s) to launch a "
        "normal Viola session." % ", ".join(name for name, _ in enabled)
    )
    try:
        logger.error("❌ %s", message)
    except (RuntimeError, AttributeError) as log_err:
        # logger may be unavailable in some env-only contexts.
        sys.stderr.write("logger unavailable: %s\n" % log_err)
    sys.stderr.write(message + "\n")
    sys.exit(2)


_boot_checkpoint("12-module-level-complete")


def main():
    """Main entry point"""
    global _configure_observability, _get_debug_bus, _install_debug_event_mirror, logger
    _boot_checkpoint("13-main-entered")

    from diagnostics.startup_telemetry import record_process_start, run_in_background

    record_process_start()

    # F-010: fail closed for Claude's coordinator-mode invocation surface.
    # Claude's coordinator-mode (coordinator/coordinatorMode.ts) changes
    # prompt/tool context and resume behavior in a way Viola does not
    # currently implement. Until R9 / Round-4 buddy-swarm work lands, an
    # environment requesting coordinator mode must NOT silently boot as a
    # normal Viola session — that would hand the caller a tool surface and
    # control plane it didn't ask for. Reject loudly at the bootstrap
    # boundary instead.
    _reject_coordinator_mode_invocation()

    # CRITICAL: WebEngine must be imported BEFORE QApplication is created
    # Otherwise YouTube playback will not work (QWebEngineView becomes None)
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView

        logger.info("WebEngine pre-imported successfully for YouTube playback")
    except ImportError as e:
        logger.warning("WebEngine not available: %s", e)

    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication

    from config import get_settings
    from core.constants import DEFAULT_API_PORT, LOCALHOST
    from core.logging_config import get_logger
    from core.sentry_integration import (
        configure_sentry,
        install_qt_exception_hooks,
    )
    from ui.core.security import get_security_posture_snapshot
    from ui.qt_native.startup_controller import StartupCoordinator
    from ui.qt_native.startup_types import StartupPolicy

    logger = get_logger(__name__)
    configure_sentry()
    install_qt_exception_hooks()

    try:
        from ui.qt_native.debug_events import get_debug_bus as _imported_debug_bus

        _get_debug_bus = _imported_debug_bus
    except Exception:  # pragma: no cover - Qt-less environments
        logger.debug("Qt debug bus unavailable, debug events disabled")

    try:
        from diagnostics.debug_events import (
            install_debug_event_mirror as _imported_mirror,
        )
        from diagnostics.observability_logging import (
            configure_observability as _imported_configure,
        )

        _configure_observability = _imported_configure
        _install_debug_event_mirror = _imported_mirror
    except Exception:  # pragma: no cover - observability optional in constrained envs
        logger.debug("Observability modules unavailable in this environment")

    # VB-CABLE routing removed — ProcTap captures source audio directly,
    # no system-default device switching needed.

    settings = get_settings()
    security_posture = get_security_posture_snapshot(settings_instance=settings)

    # Cloud boot guard — hard-stop if security invariants are violated
    from core.boot_guard import validate_cloud_deployment

    validate_cloud_deployment(settings)

    api_host = getattr(settings, "api_host", LOCALHOST)
    requested_port = int(getattr(settings, "api_port", DEFAULT_API_PORT))
    debug_unsubscribe = None

    def _configure_observability_background() -> None:
        nonlocal debug_unsubscribe
        try:
            config = _configure_observability(telemetry_enabled=telemetry_opt_in)
            if telemetry_opt_in and _install_debug_event_mirror is not None and _get_debug_bus is not None:
                # rationale: persist DebugEventBus traffic for Batch A observability.
                debug_path = config.log_root / "debug_events" / "debug_events.jsonl"
                debug_unsubscribe = _install_debug_event_mirror(destination=debug_path)
                logger.info("✅ DebugEventBus mirror active at %s", debug_path)
            elif not telemetry_opt_in:
                logger.info("🛑 Telemetry opt-in disabled; structured logging and debug mirroring are inactive.")
        except Exception as obs_exc:  # pragma: no cover - defensive observability setup
            logger.warning("⚠️ Observability bootstrap failed: %s", obs_exc)

    # No Python-level QApplication.notify override here (see #1500/#1674 and
    # CL-20260714-c6b9). Overriding notify() in Python forces Shiboken's
    # virtual-dispatch machinery to materialize a Python wrapper for EVERY
    # event receiver in the process — including QtWebEngine/Chromium's
    # short-lived internal C++ widgets. A queued event for a delegate widget
    # Chromium already destroyed then dereferences a freed QObject inside
    # PySide::getWrapperForQObject, SIGSEGVing the main thread (gdb-proven on
    # the Linux AppImage headless smoke, run 29381837697). It also taxed
    # every event in the app — including every mouse move — with a Python
    # round-trip for no behavioral benefit: PySide6 already routes any
    # exception that escapes a Qt-invoked Python slot through sys.excepthook,
    # which install_qt_exception_hooks() (above) already wires to
    # capture_qt_python_exception — so a redundant notify() override bought
    # no additional exception coverage, only the crash.
    #
    # Create Qt application early — needed for SingleInstanceGuard (QLocalServer)
    logger.info("🎨 Creating Qt application...")
    # QtWebEngine (QWebEngineView) requires shared OpenGL contexts to be enabled
    # BEFORE the QApplication is constructed. Windows tolerates its absence, but
    # macOS's stricter GL/Metal context sharing use-after-frees the Chromium
    # compositor during the first WebEngine page load (EXC_BAD_ACCESS in
    # PySide::getWrapperForQObject). Must be set before QApplication(sys.argv).
    from PySide6.QtCore import Qt as _Qt_gl

    QApplication.setAttribute(_Qt_gl.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    app.setApplicationName("Viola")
    app.setOrganizationName("Viola")

    # From here on this process owes the world a living message loop, so a
    # silent one is a fault rather than "no GUI here" (#4650). Declared at
    # construction, separately from the beat installed just before app.exec(),
    # so a GUI that dies between the two reads as stalled instead of as
    # unmonitored -- the health surface fails closed.
    from services.qt_loop_liveness import declare_gui_process as _declare_gui_process

    _declare_gui_process()

    _emit_debug("app_starting", {"client": "qt", "phase": "qt_app"})

    from config.facade import get_settings_facade
    from ui.qt_native.api_client import ViolaAPIClient

    settings_facade = get_settings_facade()
    telemetry_opt_in = bool(settings_facade.get("telemetry_opt_in", False))

    if _configure_observability is not None:
        _obs_threading.Thread(
            target=_configure_observability_background,
            name="observability-bootstrap",
            daemon=True,
        ).start()

    # Single-instance check — must happen after QApplication, before port resolution
    from core.single_instance import SingleInstanceGuard

    guard = SingleInstanceGuard(app)
    if not guard.try_acquire():
        _mark_release_health_clean_exit(0)
        sys.exit(0)

    try:
        from backend.ports import PortSelectionError, resolve_listen_port

        logger.info(
            "🔍 Resolving listen port: requested_port=%s on host=%s",
            requested_port,
            api_host,
        )
        api_port = resolve_listen_port(api_host, requested_port)
        logger.info("✅ Port resolved: api_port=%s (requested=%s)", api_port, requested_port)
    except PortSelectionError as exc:
        logger.error("❌ %s", exc)
        _emit_debug(
            "backend_connection_result",
            {
                "client": "qt",
                "phase": "port_selection",
                "success": False,
                "error": exc.__class__.__name__,
                "message": str(exc),
            },
        )
        console(f"\n❌ {exc}")
        console("\nPlease free the port or set VIOLA_API_PORT to an available value.")
        sys.exit(1)

    _emit_debug(
        "app_starting",
        {
            "client": "qt",
            "phase": "preflight",
            "requested_port": requested_port,
            "selected_port": api_port,
        },
    )

    # Run startup validation first
    if os.environ.get("VIOLA_VALIDATE_STARTUP") == "1":
        try:
            from utils.startup_validation import (
                StartupValidationError,
                validate_startup,
            )

            try:
                # Gated: duplicates bootstrap checks. Set VIOLA_VALIDATE_STARTUP=1 to force.
                validate_startup(strict=False, check_port=True, port=api_port)
                logger.info("? Startup validation passed")
            except StartupValidationError as e:
                _emit_debug(
                    "app_starting",
                    {
                        "client": "qt",
                        "phase": "validation_failed",
                        "success": False,
                        "message": str(e),
                    },
                )
                console(f"\n? Startup validation failed:\n{e}")
                console("\nPlease fix the issues above and try again")
                sys.exit(1)
        except ImportError:
            logger.warning("Startup validation unavailable, continuing anyway...")

    # Pretty startup banner
    console("\n" + "=" * 70)
    console("🎧 VIOLA NATIVE QT - DESKTOP APPLICATION")
    console("=" * 70)
    console("✨ Beautiful native UI with zero flickering")
    console("🚀 Fast, modular, and easy to debug")
    console(f"ðŸ” {security_posture.summary}")
    if security_posture.warning:
        console(f"âš ï¸ {security_posture.warning}")
    console("=" * 70 + "\n")

    # Set dark fusion style
    app.setStyle("Fusion")

    # Warm device fingerprint cache from main thread — PyAudio() crashes
    # when instantiated from asyncio/worker threads on Windows.
    try:
        from voice.wake_detector.device_profile_manager import warm_device_cache

        warm_device_cache()
    except Exception as exc:
        logger.warning("Failed to warm device cache at startup: %s", exc)

    # Check WebEngine capability — deferred 1s via QTimer (must stay on Qt main thread)
    def _deferred_webengine_check() -> None:
        try:
            from ui.qt_native.webengine_capability import (
                assert_available_or_explain,
                explain_missing,
            )

            webengine_available = assert_available_or_explain(logger=logger)
            if not webengine_available:
                logger.warning(explain_missing())
        except Exception as exc:
            logger.warning("Failed to check WebEngine capability: %s", exc)

    QTimer.singleShot(1000, _deferred_webengine_check)

    # Create and show main window (React UI only)
    logger.info("🪟 Creating main window...")
    # Wildcard bind addresses (0.0.0.0, ::) are not routable on Windows;
    # use loopback for the API client, matching settings.base_url.
    probe_host = LOCALHOST if api_host in ("0.0.0.0", "::") else api_host  # nosec B104
    from config.settings import settings as _cfg

    _scheme = "https" if _cfg.ssl_enabled else "http"
    api_base_url = f"{_scheme}://{probe_host}:{api_port}"
    api_client = ViolaAPIClient(base_url=api_base_url, run_initial_probe=False)
    startup_policy = StartupPolicy()
    logger.info("🔧 Creating StartupCoordinator with host=%s, port=%s", api_host, api_port)
    coordinator = StartupCoordinator(
        api_client=api_client,  # Needed for health probing
        host=api_host,
        port=api_port,
        policy=startup_policy,
    )

    # Use WebView with React UI
    logger.info("🌐 Using React UI in WebView mode")
    window_holder: dict[str, object | None] = {"window": None}
    pending_ready: dict[str, object | None] = {"bootstrap": None}
    startup_attached = {"value": False}
    # Long-lived periodic checker; installs are manual reinstall only.
    update_scheduler_holder: dict[str, object | None] = {"scheduler": None}
    # Native-apply adapter — constructed only when NATIVE_SELF_UPDATE_APPLY_SUPPORTED
    # is True (currently True; see utils.update_checker.NATIVE_SELF_UPDATE_APPLY_SUPPORTED).
    # Holds a VelopackUpdater on Windows or a MacUpdater on macOS (#2634).
    velopack_updater_holder: dict[str, object | None] = {"updater": None}
    # Phase 1: force-update gate is independent of the routine scheduler — it ignores
    # the user opt-out because min_supported is the CVE response lever.
    update_gate_started = {"value": False}

    class _UpdateGateBridge(QObject):
        required_update = Signal(dict)

    update_gate_bridge = _UpdateGateBridge()

    def _show_required_update_dialog(result: dict) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QMessageBox

        from utils.update_checker import safe_update_download_url

        # SEC-016: the manifest URL is untrusted (TLS-only, unsigned) — enforce
        # https-only here so a malicious manifest can't redirect the forced
        # update click to an attacker URL/scheme.
        download_url = safe_update_download_url(result.get("url"))
        min_supported = str(result.get("min_supported") or result.get("latest_version") or "the latest version")
        parent = window_holder.get("window")

        try:
            box = QMessageBox(parent)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Viola Update Required")
            box.setText("This version of Viola is no longer supported.")
            box.setInformativeText(
                "Viola %s or newer is required. Download the latest update to continue." % min_supported
            )
            box.addButton("Download Update", QMessageBox.ButtonRole.AcceptRole)
            box.setModal(True)
            box.exec()
            QDesktopServices.openUrl(QUrl(download_url))
        except Exception as exc:
            logger.warning("Required update dialog failed: %s", exc)
        finally:
            app.quit()

    update_gate_bridge.required_update.connect(_show_required_update_dialog)

    def _start_min_supported_gate() -> None:
        if update_gate_started["value"]:
            return
        update_gate_started["value"] = True
        try:
            from utils.update_checker import schedule_min_supported_gate

            schedule_min_supported_gate(update_gate_bridge.required_update.emit)
        except Exception as exc:
            update_gate_started["value"] = False
            logger.debug("Minimum-supported update gate could not start: %s", exc)

    QTimer.singleShot(0, _start_min_supported_gate)

    def _ensure_window():
        window = window_holder.get("window")
        if window is not None:
            return window
        from ui.qt_native.webview_window import ViolaWebViewWindow

        logger.info("ðŸŒ Using React UI in WebView mode")
        window = ViolaWebViewWindow(port=api_port)
        window_holder["window"] = window
        guard.set_window(window)
        return window

    def _attach_window_startup(window) -> None:
        if startup_attached["value"]:
            return
        window.attach_startup_coordinator(coordinator)
        startup_attached["value"] = True

    def _attach_update_scheduler(window) -> None:
        if update_scheduler_holder["scheduler"] is not None:
            return
        try:
            from utils.update_checker import (
                NATIVE_SELF_UPDATE_APPLY_SUPPORTED,
                schedule_update_check,
            )

            # Wire the window's on_update_event method as the event callback so
            # the scheduler drives the update banner.  The window marshals the
            # callback through a Qt signal (queued connection) so banner widget
            # updates happen on the main thread even though the scheduler fires
            # from a daemon thread.
            _event_cb = getattr(window, "on_update_event", None)
            scheduler = schedule_update_check(
                tray_icon=getattr(window, "tray_icon", None),
                event_callback=_event_cb,
            )
            if hasattr(window, "set_update_scheduler"):
                window.set_update_scheduler(scheduler)
            update_scheduler_holder["scheduler"] = scheduler
            app.aboutToQuit.connect(scheduler.stop)
            logger.info("Desktop update scheduler attached")

            # Native-apply adapter — the object the banner's "Restart now" (and a
            # forced auto-apply) calls apply_and_restart() on. Constructed when
            # NATIVE_SELF_UPDATE_APPLY_SUPPORTED is True. The apply engine is
            # platform-specific: Windows applies via Velopack; Velopack has NO
            # macOS runtime, so on macOS we construct utils.macos_updater.MacUpdater
            # instead (#2634 — wire the Mac apply leg into the running app). Both
            # expose the same apply_and_restart(manifest, user_consent=...) contract,
            # so the banner/window wiring below is identical for either platform.
            if NATIVE_SELF_UPDATE_APPLY_SUPPORTED and velopack_updater_holder["updater"] is None:
                import sys as _platform_sys

                if _platform_sys.platform == "darwin":
                    try:
                        from utils.macos_updater import MacUpdater

                        # MacUpdater reads its swap target from sys.executable and its
                        # artifact (url/sha256/team-id/notarized) from the manifest the
                        # scheduler hands to apply_and_restart — no feed URL to wire.
                        _mac_updater = MacUpdater()
                        velopack_updater_holder["updater"] = _mac_updater
                        logger.info("MacUpdater constructed for macOS native apply")
                        # The window setter is Velopack-named but platform-agnostic:
                        # it injects whatever apply object the banner should call.
                        if hasattr(window, "set_velopack_updater"):
                            window.set_velopack_updater(_mac_updater)
                    except Exception as _mac_exc:  # noqa: BLE001, RUF100
                        logger.warning("MacUpdater could not be constructed: %s", _mac_exc)
                else:
                    try:
                        from utils.update_checker import cached_velopack_feed_url
                        from utils.velopack_updater import VelopackUpdater

                        # The feed URL comes from the live manifest, but it is only
                        # needed when the user actually applies an update. Pass the
                        # resolver itself so VelopackUpdater looks it up on first use
                        # (banner "Restart now"), by which time the background
                        # checkers have long since cached a manifest. Reading it here
                        # used to mean a synchronous, 10-second-timeout httpx.get on
                        # the MAIN THREAD before window.show() -- so a brand-new
                        # install on a dead or proxy-blocked network waited on the
                        # network for a string, just to construct an object that
                        # makes no network call of its own. cached_velopack_feed_url
                        # never fetches; a cold cache yields the canonical default.
                        velopack_updater_holder["updater"] = VelopackUpdater(cached_velopack_feed_url)
                        logger.info("VelopackUpdater constructed (feed resolved lazily on first apply)")
                        # Wire the updater to the banner so "Restart now" can apply.
                        if hasattr(window, "set_velopack_updater"):
                            window.set_velopack_updater(velopack_updater_holder["updater"])
                    except Exception as _vpk_exc:  # noqa: BLE001, RUF100
                        logger.warning("VelopackUpdater could not be constructed: %s", _vpk_exc)
        except Exception as exc:
            logger.warning("Desktop update scheduler unavailable: %s", exc)

    def _on_backend_ready(bootstrap):
        """Handle backend bootstrap completion."""
        if bootstrap is None:
            return
        window = window_holder.get("window")
        if window is None:
            window = _ensure_window()
        _attach_window_startup(window)
        # NOTE: update wiring deliberately runs at the END of this slot, after
        # show(). This is the main thread, and nothing about checking for a new
        # version is on the path to what the user actually asked for.
        # React UI WebView handles its own loading via _on_backend_ready
        logger.info("✅ Backend ready - React UI WebView will load now")
        # Wire video widget to QtMediaBackend if available
        _wire_video_widget(window, bootstrap)
        # Store webview reference on player for browser-native provider
        _wire_browser_webview(window, bootstrap)
        # Wire browser overlay controller for login flows
        _wire_browser_overlay_controller(window, bootstrap)
        # Wire agent frame streamer for spoke viewport streaming
        _wire_frame_streamer(window, bootstrap)
        # Wire browser webview into CDP browser MCP server (visible browser mode)
        _wire_cdp_browser_server(window)
        show = getattr(window, "show", None)
        if callable(show):
            show()
        run_in_background("phone_stt", _preload_phone_stt_at_startup)
        # Update wiring is the LAST thing this slot does: the window is already
        # on screen, so nothing here can delay first paint. It only starts
        # background daemon threads and must never make a network call inline.
        _attach_update_scheduler(window)

    coordinator.ready.connect(_on_backend_ready)

    def _on_backend_failed(phase: str, payload: dict):
        logger.error(
            "Backend failed during %s: %s",
            phase,
            payload.get("detail") or payload.get("error"),
        )

    coordinator.failed.connect(_on_backend_failed)

    def _on_backend_degraded(payload: dict):
        elapsed = payload.get("elapsed_s")
        timeout = payload.get("timeout_s")
        details = []
        if isinstance(elapsed, (int, float)):
            details.append(f"elapsed={elapsed:.1f}s")
        if isinstance(timeout, (int, float)):
            details.append(f"timeout={timeout:.1f}s")
        suffix = f" ({', '.join(details)})" if details else ""
        console(f"\n[WARN] Backend still starting...{suffix}")
        console("You can keep using offline features while Viola reconnects.\n")

    coordinator.degraded.connect(_on_backend_degraded)

    # Materialize the WebEngine window before backend readiness. The embedded
    # CDP browser target lives in ViolaWebViewWindow, and browser tools must be
    # attachable even when /health/ready is slow to report ready.
    early_window = _ensure_window()
    _attach_window_startup(early_window)

    # Phase 3B: Start backend BEFORE window.show() so bootstrap runs
    # in parallel with Qt's first-paint and WebEngine initialization.
    coordinator.start()

    if pending_ready.get("bootstrap") is not None:
        _on_backend_ready(pending_ready.pop("bootstrap"))

    console("\n" + "=" * 70)
    console("✅ VIOLA READY!")
    console("=" * 70)
    console("🌐 Running React Smart Display UI")
    console("🎵 Voice commands and playback controls ready")
    console(f"🌐 Web UI also available at http://{api_host}:{api_port}")
    console("=" * 70 + "\n")

    # -- Crash diagnostics: log exit path for exit-code-15 investigation ------
    import atexit as _atexit
    import signal as _signal

    _crash_log = _get_viola_logs_dir() / "exit_diagnostic.log"
    _crash_log.parent.mkdir(parents=True, exist_ok=True)

    def _on_exit():
        with open(_crash_log, "a") as f:
            import datetime

            f.write("[%s] atexit handler fired\n" % datetime.datetime.now(datetime.UTC).isoformat())

        # Hub-local playback subprocess removed — no cleanup needed.

    _atexit.register(_on_exit)

    def _on_sigterm(signum, frame):
        with open(_crash_log, "a") as f:
            import datetime
            import traceback as _tb

            f.write("[%s] SIGTERM (signal %d) received\n" % (datetime.datetime.now(datetime.UTC).isoformat(), signum))
            _tb.print_stack(frame, file=f)
        sys.exit(128 + signum)

    if hasattr(_signal, "SIGTERM"):
        _signal.signal(_signal.SIGTERM, _on_sigterm)

    # Signal that the event loop is about to start — plugin permission
    # dialogs are now safe to show (QMessageBox requires a pumping loop).
    try:
        import plugins.permissions as _perms

        _perms.EVENT_LOOP_RUNNING = True
    except Exception:  # noqa: BLE001, RUF100 - best-effort plugin flag; never block startup
        logger.debug("plugins.permissions event-loop flag not set", exc_info=True)

    # The message loop's own heartbeat. A queued QTimer event can only fire
    # because this loop dequeued and dispatched it, so the beat stops the
    # instant the loop does -- which is what /health, /health/live and
    # viola_control.py health now answer from instead of from "the HTTP thread
    # replied" (#4650). Installed last, so the very next thing that happens is
    # the loop that carries it.
    from services.qt_loop_liveness import install_event_loop_probe as _install_qt_loop_probe

    _install_qt_loop_probe(app)

    # Run application
    _boot_checkpoint("14-about-to-call-app-exec")
    try:
        _exit_code = app.exec()
        with open(_crash_log, "a") as f:
            import datetime

            f.write(
                "[%s] app.exec() returned exit_code=%d\n"
                % (datetime.datetime.now(datetime.UTC).isoformat(), _exit_code)
            )
        if _exit_code == 0:
            _mark_release_health_clean_exit(_exit_code)
        sys.exit(_exit_code)
    finally:
        if debug_unsubscribe:
            try:
                debug_unsubscribe()
            except Exception as e:
                logger.debug("Debug unsubscribe cleanup failed: %s", e)


def _run_entrypoint_with_top_level_diagnostics() -> None:
    """Run the real entrypoint, guaranteeing ANY exit path is diagnosable.

    ``_fatal_boot()`` only wraps a handful of EARLY, specific startup steps
    (environment config, faulthandler setup) -- it says nothing about a
    ``sys.exit(N)`` called from deeper in ``main()`` (e.g. the port-selection
    or startup-validation failure paths), and Python's default top-level
    handling of an uncaught ``SystemExit`` prints NOTHING regardless of
    platform (unlike a regular exception, which prints a traceback by
    default) -- that silence is itself the trap. #1500's chain already fixed
    two other silent-exit blind spots (_fatal_boot's file-only crash log, the
    smoke script's native-crash visibility); this closes the last one: ANY
    non-zero/uncaught exit from the real entrypoint, from ANY call site, now
    prints its origin here before the process actually exits, instead of
    requiring a fix per call site as each one is discovered one CI run at a
    time.
    """
    import sys as _local_sys
    import traceback

    # Local `import sys` (not the module-level `_sys` alias, which is deleted
    # earlier in this module -- referencing it here would raise NameError).
    try:
        if _headless_requested():
            _run_headless_daemon()
        else:
            main()
    except SystemExit as exc:
        code = exc.code
        # code is 0/None on a clean, expected exit (e.g. main()'s own
        # app.exec()-returned-0 path) -- nothing to diagnose there.
        if code not in (0, None) and not _suppress_import_console_output:
            try:
                _local_sys.stderr.write(
                    "UNCAUGHT SystemExit(%r) reached the top-level entrypoint "
                    "-- not routed through _fatal_boot(). Origin:\n" % (code,)
                )
                traceback.print_exc(file=_local_sys.stderr)
                _local_sys.stderr.flush()
            except (OSError, ValueError):
                pass
        raise


if __name__ == "__main__":
    _run_entrypoint_with_top_level_diagnostics()
