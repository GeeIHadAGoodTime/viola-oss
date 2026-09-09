#!/usr/bin/env python3
# Crash instrumentation — writes traceback to crash_diag.log on any unhandled exit
import atexit as _at
import faulthandler as _fh
import sys as _sys
import traceback as _tb

from core.platform import (
    configure_environment as _configure_viola_environment,
    get_project_root as _get_viola_project_root,
)

_configure_viola_environment()
# Run-scoped, dated, module-mapped crash artifacts (#4650). This used to open
# logs/crash_diag.log in "w" mode, which avoided the shared-file conflation
# problem only by DESTROYING the previous run's crash evidence on every start --
# so a crash that took two attempts to notice had already erased itself.
from core.crash_forensics import install_crash_forensics as _install_crash_forensics

_crash_forensics = _install_crash_forensics("web_interface")
_crash_log_path = _crash_forensics.log_path
_fh.enable(file=_crash_forensics.stream, all_threads=True)


def _crash_dump():
    with open(_crash_log_path, "a", encoding="utf-8") as f:
        f.write(f"\n=== atexit dump ===\nexc: {_sys.exc_info()[1]}\n")
        _tb.print_exc(file=f)


_at.register(_crash_dump)
# CRITICAL: Load .env file FIRST before any other imports
# This ensures OPENAI_API_KEY and other env vars are available
import config.settings
from core.console import console
from services.sentry_init import init_sentry

init_sentry("START_WEB_INTERFACE")

"""
Viola Web Interface Startup Script (Python version for cross-platform)
This provides better error messages than the batch file
"""

import signal
import socket
import subprocess
import sys
import time

from backend import PortSelectionError, resolve_listen_port
from config import get_settings
from core.constants import DEFAULT_API_PORT, LOCALHOST

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    # Set UTF-8 encoding for stdout/stderr
    # Use getattr for dynamic attribute access on TextIO streams
    _stdout_reconf = getattr(sys.stdout, "reconfigure", None)
    _stderr_reconf = getattr(sys.stderr, "reconfigure", None)
    if callable(_stdout_reconf) and callable(_stderr_reconf):
        _stdout_reconf(encoding="utf-8")
        _stderr_reconf(encoding="utf-8")
    else:
        # Fallback for older Python versions or non-standard streams
        import io

        _stdout_buffer = getattr(sys.stdout, "buffer", None)
        _stderr_buffer = getattr(sys.stderr, "buffer", None)
        if isinstance(_stdout_buffer, io.BufferedIOBase):
            sys.stdout = io.TextIOWrapper(_stdout_buffer, encoding="utf-8", errors="strict")
        if isinstance(_stderr_buffer, io.BufferedIOBase):
            sys.stderr = io.TextIOWrapper(_stderr_buffer, encoding="utf-8", errors="strict")


def print_header(text):
    """Print a formatted header"""
    console("\n" + "=" * 60)
    console(f"  {text}")
    console("=" * 60)


APP_SETTINGS = get_settings()


def print_step(num, total, text):
    """Print a step indicator"""
    console(f"\n[{num}/{total}] {text}...")


def check_python_version():
    """Check if Python version is adequate"""
    print_step(1, 5, "Checking Python version")
    version = sys.version_info
    console(f"    Python {version.major}.{version.minor}.{version.micro}")

    if version.major < 3 or (version.major == 3 and version.minor < 9):
        console("    [ERROR] Python 3.9 or higher required")
        return False
    console("    [OK] Python version is adequate")
    return True


def check_dependencies():
    """Check if required packages are installed"""
    print_step(2, 5, "Checking dependencies")

    required = ["fastapi", "uvicorn", "pydantic", "python-vlc"]
    missing = []

    for pkg in required:
        try:
            __import__(pkg.replace("-", "_"))
            console(f"    [OK] {pkg}")
        except ImportError:
            console(f"    [MISS] {pkg} (missing)")
            missing.append(pkg)

    if missing:
        console(f"\n    [WARN] Missing packages: {', '.join(missing)}")
        console("    Installing...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q"] + missing,
                cwd=str(_get_viola_project_root()),
            )
            console("    [OK] Packages installed")
            return True
        except subprocess.CalledProcessError:
            console("    [ERROR] Failed to install packages")
            return False

    console("    [OK] All dependencies present")
    return True


def check_port(port=DEFAULT_API_PORT, host=LOCALHOST):
    """Check if port is available"""
    print_step(3, 5, f"Checking port {port} on {host}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
        console(f"    [OK] Port {port} is available")
        return True
    except OSError:
        console(f"    [WARN] Port {port} is in use on {host}")
        console("    Another process may be running. Viola will attempt to select a free port automatically.")
        return True


def check_config():
    """Check if configuration is valid"""
    print_step(4, 5, "Checking configuration")

    try:
        from config import settings

        console("    [OK] Configuration loaded")

        # Check for API key (optional but good to know)
        if not getattr(settings, "openai_api_key", None):
            console("    [WARN] No OpenAI API key configured")
            console("    AI features will be limited")
        else:
            console("    [OK] OpenAI API key configured")

        console("    [OK] Configuration valid")
        return True

    except Exception as e:
        console(f"    [ERROR] Configuration error: {e}")
        return False


def start_backend(host: str, port: int):
    """Start the Viola backend"""
    print_step(5, 5, "Starting Viola backend")

    console("\n" + "=" * 60)
    console("  SERVER STARTING")
    console(f"  URL: http://{host}:{port}")
    console("  Press Ctrl+C to stop")
    console("=" * 60 + "\n")

    from backend import start_backend_server

    boot = None
    shutdown_requested = {"flag": False}

    def _handle_shutdown(signum, frame):
        shutdown_requested["flag"] = True
        # Attempt to close WebSocket connections gracefully during shutdown.
        # EventHub.disconnect() is async; we schedule it from the sync handler.
        # NOTE: EventHub does not currently have a close_all() method — when
        # added, call it here instead of iterating clients manually.
        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub and hasattr(hub, "_clients"):
                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    for ws in list(hub._clients):
                        try:
                            loop.call_soon_threadsafe(
                                asyncio.ensure_future,
                                ws.close(code=1001, reason="server_shutdown"),
                            )
                        except Exception:
                            pass
        except Exception:
            pass  # Best-effort cleanup during shutdown

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_shutdown)
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_shutdown)

    try:
        boot = start_backend_server(host=host, port=port, voice=False, wake=False)
        console("  Press Ctrl+C to stop the server\n")
        while not shutdown_requested["flag"]:
            time.sleep(1)
        console("\n\n[INFO] Shutdown signal received. Stopping server...")
    except KeyboardInterrupt:
        shutdown_requested["flag"] = True
        console("\n\n[INFO] Shutting down gracefully...")
        return 0
    except Exception as e:
        console(f"\n\n[ERROR] Backend failed to start: {e}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        if boot:
            boot.cleanup()


def main():
    """Main entry point"""
    print_header("VIOLA WEB INTERFACE STARTUP")

    settings = APP_SETTINGS
    host = getattr(settings, "api_host", LOCALHOST)
    requested_port = int(getattr(settings, "api_port", DEFAULT_API_PORT))

    try:
        port = resolve_listen_port(host, requested_port)
    except PortSelectionError as exc:
        console(f"\n❌ {exc}")
        console("\nPlease free the requested port or set VIOLA_API_PORT to an available value.")
        return 1

    console(f"\n[INFO] Backend will listen on http://{host}:{port}")

    # Use new startup validation system
    try:
        from utils.startup_validation import StartupValidationError, validate_startup

        try:
            validate_startup(strict=False, check_port=True, port=port)
            console("\n✅ All startup checks passed")
        except StartupValidationError as e:
            console(f"\n❌ Startup validation failed:\n{e}")
            console("\nPlease fix the issues above and try again")
            return 1
    except ImportError:
        # Fallback to old checks if new system unavailable
        console("\n⚠️ New validation system unavailable, using legacy checks...")
        checks = [
            ("Python version", check_python_version),
            ("Dependencies", check_dependencies),
            ("Port availability", lambda: check_port(port=port, host=host)),
            ("Configuration", check_config),
        ]
        for name, check_func in checks:
            if not check_func():
                console(f"\n[FATAL] {name} check failed")
                console("Please fix the issues above and try again")
                return 1

    # All checks passed, start backend
    return start_backend(host, port)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        console(f"\n[FATAL] Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
