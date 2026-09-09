"""Viola Daemon -- headless API server + messaging channels.

Runs the FastAPI server, messaging hub, scheduler, and optional audio
pipeline WITHOUT the Qt UI. Designed for always-on server/service mode.

Usage::

    python -m services.daemon.viola_daemon          # foreground
    python tools/viola_service.py start             # background (via CLI)

Handles SIGTERM / SIGINT for graceful shutdown. Writes PID to
``.viola/daemon.pid`` for management by ``tools/viola_service.py``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import settings
from core.constants import LOCALHOST
from core.logging_config import get_logger
from core.platform import get_data_dir
from core.sentry_integration import install_process_exception_hooks
from services.sentry_init import init_sentry

logger = get_logger(__name__)
init_sentry("services.daemon.viola_daemon")

# #1419: the daemon is a non-GUI desktop process (headless local backend) that
# previously had no crash sink at all -- init_sentry() alone no-ops on desktop
# (the legacy Sentry DSN is empty there), and nothing routed an unhandled
# exception to the anonymized diagnostic relay/spool the way
# core.sentry_integration.capture_qt_python_exception does for the Qt GUI
# process. Installed at import time (mirrors init_sentry's own module-level
# placement above) so it is active for the whole daemon lifetime, including
# both real launch paths: `python -m services.daemon.viola_daemon` and
# `viola_qt.py --headless` (which imports this module and calls main()
# directly, never touching the Qt-only install_qt_exception_hooks()).
install_process_exception_hooks("desktop_daemon", entry_point="services.daemon.viola_daemon")

# Segfault/native-crash visibility (portaudio, onnxruntime, native audio libs
# the daemon's optional --audio pipeline loads): faulthandler dumps a Python
# traceback to a file on SIGSEGV/SIGABRT/SIGFPE/SIGBUS/SIGILL instead of the
# process vanishing with zero trace. Mirrors viola_qt.py's own boot-time
# faulthandler.enable() (added for the same reason: silent native death is
# undiagnosable otherwise). This writes a LOCAL file only -- it is not, by
# itself, relayed to GlitchTip (see the crash-telemetry report for #1419).
try:
    import faulthandler

    from core.crash_forensics import install_crash_forensics

    # Run-scoped + dated + module-mapped, and (on Windows) it dies promptly
    # instead of suspending behind an unattended modal dialog. See #4650 and
    # core/crash_forensics.py for why a bare append-only log was undiagnosable.
    _crash_forensics = install_crash_forensics("viola_daemon")
    faulthandler.enable(file=_crash_forensics.stream, all_threads=True)
except (OSError, ValueError, RuntimeError, ImportError):
    logger.debug("Daemon faulthandler setup failed (non-critical)", exc_info=True)


def _project_data_dir() -> Path:
    data_dir = get_data_dir()
    return data_dir if data_dir.is_absolute() else _PROJECT_ROOT / data_dir


_PID_DIR = _project_data_dir()
_PID_FILE = _PID_DIR / "daemon.pid"

_DEFAULT_HOST = LOCALHOST
_HEADLESS_ROUTE_INITIALIZERS = frozenset(
    {
        "api_core_routes",
        "api_feature_routes",
        "auth_routes",
        "backend_optional_routes",
        "settings_router",
        "websocket_command_handlers",
    }
)


class ViolaDaemon:
    """Headless Viola server that runs API + messaging without Qt UI.

    Lifecycle:
        1. ``start()`` writes PID, boots the FastAPI app via uvicorn,
           starts messaging router + scheduler.
        2. ``stop()`` shuts down all subsystems and removes PID file.
        3. Signal handlers (SIGTERM, SIGINT) call ``stop()`` automatically.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        enable_audio: bool = False,
    ) -> None:
        configured_host = host if host is not None else getattr(settings, "api_host", None)
        self._host = configured_host or _DEFAULT_HOST
        self._port = port or getattr(settings, "api_port", 8756)
        self._enable_audio = enable_audio
        self._server: Any = None
        self._shutdown_event = asyncio.Event()
        self._messaging_router: Any = None
        self._scheduler_service: Any = None
        self._weekly_review_service: Any = None
        self._calendar_reminder_service: Any = None
        self._intent_pipeline: Any = None

    def _register_headless_route_initializers(self, app: Any) -> None:
        """Register route-only initializers that daemon mode otherwise skips."""
        try:
            initializers = list(getattr(app.state, "startup_background_initializers", []) or [])
        except Exception as exc:
            logger.debug("Could not inspect daemon route initializers: %s", exc)
            return

        for name, initializer in initializers:
            if name not in _HEADLESS_ROUTE_INITIALIZERS:
                continue
            try:
                initializer()
                logger.info("Daemon route initializer registered: %s", name)
            except Exception:
                logger.exception("Daemon route initializer failed: %s", name)

    # ------------------------------------------------------------------ PID

    @staticmethod
    def write_pid() -> None:
        """Write the current process PID to the PID file."""
        _PID_DIR.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        logger.info("Daemon PID %d written to %s", os.getpid(), _PID_FILE)

    @staticmethod
    def remove_pid() -> None:
        """Remove the PID file if it exists."""
        try:
            _PID_FILE.unlink(missing_ok=True)
            logger.debug("PID file removed")
        except Exception as exc:
            logger.warning("Failed to remove PID file: %s", exc)

    @staticmethod
    def read_pid() -> int | None:
        """Read the daemon PID from file. Returns None if not running."""
        try:
            if _PID_FILE.exists():
                pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
                return pid
        except (ValueError, OSError) as exc:
            logger.debug("Could not read PID file: %s", exc)
        return None

    @staticmethod
    def is_running() -> bool:
        """Check whether a daemon process is currently running."""
        pid = ViolaDaemon.read_pid()
        if pid is None:
            return False
        # Check if process exists
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            # Process not found -- stale PID file
            ViolaDaemon.remove_pid()
            return False

    # ------------------------------------------------------------------ bootstrap

    def _build_app(self) -> Any:
        """Build the FastAPI application using the standard factory."""
        from backend.runtime import bootstrap

        boot = bootstrap(
            host=self._host,
            port=self._port,
            voice=self._enable_audio,
            wake=self._enable_audio,
            desktop_mode=False,
        )
        self._intent_pipeline = boot.intent
        logger.info("Daemon using backend.runtime bootstrap facades")
        return boot.app

    async def _start_messaging(self) -> None:
        """Start messaging channels (Telegram, Discord, etc.)."""
        try:
            from messaging.hub import register_messaging_hub
            from messaging.router import MessageRouter

            self._messaging_router = MessageRouter()
            if self._intent_pipeline is not None:
                await self._messaging_router.start(self._intent_pipeline)
                register_messaging_hub(self._messaging_router)
                logger.info("Daemon messaging channels started")
            else:
                logger.info("Daemon messaging skipped (no intent pipeline)")
        except Exception as exc:
            logger.warning("Daemon messaging startup failed: %s", exc)

    async def _start_scheduler(self) -> None:
        """Start the scheduler service."""
        try:
            from services.scheduler.service import get_scheduler_service

            self._scheduler_service = get_scheduler_service()
            if self._intent_pipeline is not None:
                self._scheduler_service._intent_pipeline = self._intent_pipeline
            await self._scheduler_service.start()
            logger.info("Daemon scheduler started")
        except Exception as exc:
            logger.warning("Daemon scheduler startup failed: %s", exc)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the daemon: API server + messaging + scheduler."""
        # #1419: sys.excepthook/threading.excepthook (installed at import time,
        # above) never fire for an exception that only ever lives inside an
        # asyncio Task nobody awaited -- asyncio's default behavior there is to
        # log "Task exception was never retrieved" and move on. Must be called
        # from inside the running loop, hence here rather than at import time.
        from core.sentry_integration import install_asyncio_exception_handler

        install_asyncio_exception_handler("desktop_daemon", entry_point="services.daemon.viola_daemon")

        if self.is_running():
            logger.warning("Daemon already running (PID %s)", self.read_pid())
            return

        self.write_pid()

        # Install signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except NotImplementedError:
                # Windows doesn't support add_signal_handler for all signals
                signal.signal(sig, lambda s, f: asyncio.create_task(self.stop()))

        logger.info("Viola daemon starting on %s:%d", self._host, self._port)

        # Build app
        app = self._build_app()
        self._register_headless_route_initializers(app)

        # Start subsystems
        await self._start_messaging()
        await self._start_scheduler()

        # Start proactive scheduler if available
        try:
            from services.scheduler.proactive_scheduler import get_proactive_scheduler

            proactive = get_proactive_scheduler()
            if self._intent_pipeline is not None:
                proactive.set_intent_pipeline(self._intent_pipeline)
            await proactive.start()
            logger.info("Daemon proactive scheduler started")
        except Exception as exc:
            logger.debug("Proactive scheduler not available: %s", exc)

        # Start calendar reminder sweep (voices pre-event alerts; covers events
        # created via any path -- calendar panel/REST, agent tool, external sync)
        try:
            from services.calendar.reminders import get_calendar_reminder_service

            self._calendar_reminder_service = get_calendar_reminder_service()
            await self._calendar_reminder_service.start()
            logger.info("Daemon calendar reminder service started")
        except Exception as exc:  # noqa: BLE001, RUF100 -- optional service wiring must fail open
            logger.debug("Calendar reminder service not available: %s", exc)

        # Start weekly-review service if opt-in via Settings
        try:
            from ui.settings_manager import get_settings_manager

            if get_settings_manager().get("weekly_review_enabled", False):
                from services.meta_analysis.weekly_review import (
                    get_weekly_review_service,
                )

                self._weekly_review_service = get_weekly_review_service()
                await self._weekly_review_service.start()
                logger.info("Daemon weekly-review service started (opt-in)")
            else:
                logger.debug("Weekly-review service skipped (not enabled in settings)")
        except Exception as exc:
            logger.debug("Weekly-review service not available: %s", exc)

        # Start uvicorn
        import uvicorn

        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        logger.info("Viola daemon ready on http://%s:%d", self._host, self._port)

        # Run server (blocks until shutdown)
        await self._server.serve()

    async def stop(self) -> None:
        """Gracefully shut down all daemon subsystems."""
        logger.info("Viola daemon shutting down...")

        # Stop scheduler
        if self._scheduler_service is not None:
            try:
                await self._scheduler_service.stop()
            except Exception as exc:
                logger.debug("Scheduler stop error: %s", exc)

        # Stop calendar reminder service
        if self._calendar_reminder_service is not None:
            try:
                await self._calendar_reminder_service.stop()
            except Exception as exc:  # noqa: BLE001, RUF100 -- best-effort shutdown must not raise
                logger.debug("Calendar reminder stop error: %s", exc)

        # Stop weekly-review service
        if self._weekly_review_service is not None:
            try:
                await self._weekly_review_service.stop()
            except Exception as exc:
                logger.debug("Weekly-review stop error: %s", exc)

        # Stop messaging
        if self._messaging_router is not None:
            try:
                await self._messaging_router.shutdown()
            except Exception as exc:
                logger.debug("Messaging shutdown error: %s", exc)

        # Stop uvicorn
        if self._server is not None:
            self._server.should_exit = True

        self.remove_pid()
        logger.info("Viola daemon stopped")


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for ``python -m services.daemon.viola_daemon``."""
    import argparse

    parser = argparse.ArgumentParser(description="Viola headless daemon")
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8756)")
    parser.add_argument("--audio", action="store_true", help="Enable audio pipeline")
    args = parser.parse_args()

    daemon = ViolaDaemon(
        host=args.host,
        port=args.port,
        enable_audio=args.audio,
    )

    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user")
    finally:
        ViolaDaemon.remove_pid()


if __name__ == "__main__":
    main()
