"""
Server factory for Uvicorn server lifecycle management.

Construct the application server and its dependencies.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any, Protocol

from core.constants import (
    LOCALHOST,
    TIMEOUT_DEFAULT,
    TIMEOUT_LONG,
    TIMEOUT_MINUTE,
    TIMEOUT_SHORT,
    TIMEOUT_SHUTDOWN,
)
from core.logging_config import get_logger

logger = get_logger(__name__)


class StoppableServer(Protocol):
    """Protocol for servers that can be stopped gracefully."""

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the server gracefully."""
        ...


# Import validation utilities
try:
    from core.validation import validate_host, validate_port
except ImportError as e:
    # Fallback if validation module not available
    logger.debug("Failed to import validation utilities: %s", e, exc_info=True)

    def validate_host(host: str) -> tuple[bool, str]:
        return True, ""

    def validate_port(port: Any) -> tuple[bool, str]:
        return True, ""


class UvicornServer:
    """Wrapper for Uvicorn server that can be stopped gracefully"""

    def __init__(self, app: Any, host: str, port: int) -> None:
        try:
            import uvicorn
        except ImportError as e:
            raise ImportError("uvicorn is required for web server. Install: pip install uvicorn") from e

        # HTTPS test: serve over TLS with self-signed cert (temporary)
        import os

        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _cert = os.path.join(_base, "data", "secrets", "viola_cert.pem")
        _key = os.path.join(_base, "data", "secrets", "viola_key.pem")
        _ssl_kwargs: dict[str, str] = {}
        if os.path.exists(_cert) and os.path.exists(_key):
            _ssl_kwargs = {"ssl_certfile": _cert, "ssl_keyfile": _key}
            logger.info("HTTPS enabled with self-signed cert: %s", _cert)

        self.config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="info",  # Changed from "warning" to see startup logs
            loop="asyncio",
            timeout_keep_alive=30,
            limit_concurrency=200,
            timeout_notify=30,
            timeout_graceful_shutdown=30,
            proxy_headers=False,
            forwarded_allow_ips="",
            **_ssl_kwargs,
        )
        self.server = uvicorn.Server(self.config)
        self.thread: threading.Thread | None = None
        self.should_exit = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.started = threading.Event()  # Signal when server has actually started
        self.startup_error: Exception | None = None  # Store startup errors

    def _wait_for_server_ready(self, host: str, port: int, timeout: float = 10.0) -> bool:
        """
        Check if server is ready to accept connections.

        Strategy:
        1) Verify TCP port is listening
        2) Probe canonical health endpoints from cheapest to most expensive:
           - /                (may be protected; any HTTP response means server is up)
           - /health          (200 when ready; 503 when not ready)
           - /health/ready    (200 when ready; 503 when not ready)
           - /health/details  (always 200; includes ready flag)

        Any HTTP response (2xx/3xx/4xx/5xx) indicates the HTTP server stack is responding,
        which is sufficient for “server up” readiness at this layer.
        """
        try:
            # Check if port is listening
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(min(0.2, timeout))
            result = sock.connect_ex((host, port))
            sock.close()

            if result == 0:
                # Port is open - this is a good sign the server is at least bound
                logger.debug("Port %d is open, verifying HTTP response", port)

                # Try to verify it's actually serving HTTP(S).
                # Probe the cheapest endpoints first to minimize startup latency.
                import ssl
                import urllib.error
                import urllib.request

                # If SSL is configured, probe via HTTPS with unverified context
                _use_https = hasattr(self, "config") and getattr(self.config, "ssl_certfile", None)
                _scheme = "https" if _use_https else "http"
                _ssl_ctx = None
                if _use_https:
                    _ssl_ctx = ssl.create_default_context()
                    _ssl_ctx.check_hostname = False
                    _ssl_ctx.verify_mode = ssl.CERT_NONE

                endpoints_to_try = ["/health/live", "/health/ready", "/health", "/"]
                for endpoint in endpoints_to_try:
                    probe_url = f"{_scheme}://{host}:{port}{endpoint}"
                    try:
                        req = urllib.request.Request(probe_url)
                        req.add_header("Connection", "close")
                        response = urllib.request.urlopen(
                            req, timeout=min(0.3, timeout), context=_ssl_ctx
                        )  # nosec B310
                        body = response.read()
                        response.close()
                        logger.debug(
                            "[DIAG_STARTUP] transport_probe url=%s status=200 body=%s",
                            probe_url,
                            body[:200],
                        )
                        logger.info(
                            "Server is ready and responding at %s://%s:%d (verified via %s)",
                            _scheme,
                            host,
                            port,
                            endpoint,
                        )
                        return True
                    except urllib.error.HTTPError as http_err:
                        # Any HTTP status confirms the HTTP stack is responding.
                        code = getattr(http_err, "code", 0)
                        logger.debug(
                            "[DIAG_STARTUP] transport_probe url=%s status=%d (HTTPError, treating as up)",
                            probe_url,
                            code,
                        )
                        if endpoint in ("/health/ready", "/health"):
                            logger.debug(
                                "Health probe %s responded with HTTP %d; treating as server up at transport layer",
                                endpoint,
                                code,
                            )
                        else:
                            logger.debug(
                                "Endpoint %s responded with HTTP %d; treating as server up at transport layer",
                                endpoint,
                                code,
                            )
                        logger.info(
                            "Server is up at %s://%s:%d (HTTP %d on %s)",
                            _scheme,
                            host,
                            port,
                            code,
                            endpoint,
                        )
                        return True
                    except Exception as http_err:
                        # Other errors - might still be starting
                        logger.debug(
                            "[DIAG_STARTUP] transport_probe url=%s error=%s: %s",
                            probe_url,
                            type(http_err).__name__,
                            http_err,
                        )
                        if endpoint == endpoints_to_try[-1]:
                            # Last endpoint failed - port is open but not serving HTTP yet
                            logger.debug("All endpoints failed, but port is open - server may still be initializing")
                            return False
                        # Try next endpoint
                        continue

                # If we get here, port is open but no HTTP response
                logger.debug("Port %d is open but not responding to HTTP requests yet", port)
                return False
            else:
                # Port not yet open
                logger.debug("Port %d not yet open (connect_ex result: %d)", port, result)
                return False
        except Exception as e:
            logger.debug("Error checking server readiness: %s", e)
            return False

    def start(self, wait_for_ready: bool = True, ready_timeout: float = 10.0) -> None:
        """Start the server in a background thread and wait for it to be ready."""

        def run_server() -> None:
            try:
                logger.debug("[THREAD] Server thread function started")
                # Windows-specific fix: Set event loop policy for thread
                import asyncio
                import sys

                logger.debug("[THREAD] Platform: %s", sys.platform)
                if sys.platform == "win32":
                    # Use WindowsSelectorEventLoopPolicy for better compatibility
                    logger.debug("[THREAD] Setting Windows event loop policy")
                    try:
                        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                        logger.debug("[THREAD] Event loop policy set successfully")
                    except Exception as policy_err:
                        logger.warning("[THREAD] Failed to set event loop policy: %s", policy_err)

                # Create new event loop for this thread
                logger.debug("[THREAD] Creating new event loop")
                loop = asyncio.new_event_loop()
                self.loop = loop
                asyncio.set_event_loop(loop)
                logger.debug("[THREAD] Event loop created and set")

                # Signal that server thread has started (thread is running, server may still be binding)
                logger.debug("[THREAD] Server thread initialized, setting started event")
                self.started.set()
                logger.debug("[THREAD] Started event set, about to call server.serve()")

                # Run server - this will block until server is stopped
                logger.info(
                    "Starting Uvicorn server on %s:%d",
                    self.config.host,
                    self.config.port,
                )
                try:
                    # Run the server - this blocks until server.should_exit is set
                    # This is where uvicorn actually binds to the port and starts listening
                    logger.debug("Calling loop.run_until_complete(self.server.serve())")
                    loop.run_until_complete(self.server.serve())
                    logger.debug("server.serve() completed (server stopped)")
                except OSError as bind_err:
                    # Port binding errors - most common failure
                    if not self.should_exit.is_set():
                        self.startup_error = bind_err
                        logger.error(
                            "Uvicorn server failed to bind to %s:%d: %s",
                            self.config.host,
                            self.config.port,
                            bind_err,
                        )
                        import traceback

                        logger.error(traceback.format_exc())
                except Exception as serve_err:
                    if not self.should_exit.is_set():
                        self.startup_error = serve_err
                        logger.error("Uvicorn server serve() error: %s", serve_err)
                        import traceback

                        logger.error(traceback.format_exc())

            except Exception as e:
                if not self.should_exit.is_set():
                    self.startup_error = e
                    logger.error("Uvicorn server thread error: %s", e)
                    import traceback

                    logger.error(traceback.format_exc())
                if not self.started.is_set():
                    logger.debug("Setting started event due to error (so caller doesn't hang)")
                    self.started.set()  # Signal even on error so caller doesn't hang
            finally:
                try:
                    import asyncio

                    if isinstance(self.loop, asyncio.AbstractEventLoop):
                        # Don't try to stop a running loop from here
                        if not self.loop.is_closed():
                            try:
                                # Cancel all pending tasks
                                pending = asyncio.all_tasks(self.loop)
                                for task in pending:
                                    task.cancel()
                                # Give tasks a chance to clean up
                                if pending:
                                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                            except Exception:
                                logger.debug(
                                    "Error cancelling pending tasks during cleanup, continuing",
                                    exc_info=True,
                                )
                            try:
                                self.loop.close()
                            except Exception:
                                logger.debug(
                                    "Error closing event loop during cleanup, continuing",
                                    exc_info=True,
                                )
                except Exception:
                    logger.debug("Error while closing Uvicorn server loop", exc_info=True)
                finally:
                    self.loop = None

        logger.debug("Creating server thread")
        self.thread = threading.Thread(
            target=run_server,
            daemon=True,  # Daemon to avoid leaking non-daemon threads in tests
            name="uvicorn-thread",
        )
        logger.debug("Starting server thread...")
        self.thread.start()
        logger.debug("Server thread started, waiting for initialization signal...")

        # Wait for server thread to start (thread initialization)
        if not self.started.wait(timeout=TIMEOUT_LONG):
            logger.error("Server thread failed to signal start within %ds timeout", TIMEOUT_LONG)
            logger.error("Thread alive: %s", self.thread.is_alive())
            logger.error("Thread name: %s", self.thread.name)
            if self.startup_error:
                logger.error("Startup error detected: %s", self.startup_error)
            raise RuntimeError("Server thread failed to start within timeout")
        logger.debug("Server thread signaled start, proceeding with readiness check")

        # Wait for server to actually be ready to accept connections
        if wait_for_ready:
            logger.debug("Waiting for server to be ready (timeout=%ds)...", ready_timeout)
            # Poll for server readiness, checking for errors periodically
            start_time = time.time()
            check_interval = TIMEOUT_SHORT
            probe_timeout = 0.3
            last_error_check = 0.0
            last_progress_log = 0.0
            progress_interval = 2.0  # Log progress every 2 seconds

            while time.time() - start_time < ready_timeout:
                elapsed = time.time() - start_time

                # Log progress periodically
                if elapsed - last_progress_log >= progress_interval:
                    logger.debug(
                        "Still waiting for server... (%.1fs / %ds)",
                        elapsed,
                        ready_timeout,
                    )
                    last_progress_log = elapsed

                # Check for errors every 0.1 seconds
                if elapsed - last_error_check >= check_interval:
                    if self.startup_error:
                        logger.error("Startup error detected during wait: %s", self.startup_error)
                        raise RuntimeError(f"Server failed to start: {self.startup_error}") from self.startup_error
                    last_error_check = elapsed

                # Check if server is ready (with short timeout to allow polling)
                # Wildcard bind addresses (0.0.0.0, ::) are not routable on Windows;
                # use loopback for the readiness probe, matching settings.base_url.
                poll_host = LOCALHOST if self.config.host in ("0.0.0.0", "::") else self.config.host  # nosec B104
                if type(self).__name__ == "UvicornServer" and bool(getattr(self.server, "started", False)):
                    logger.debug("Uvicorn reported server.started after %.2fs", elapsed)
                    return
                if self._wait_for_server_ready(poll_host, self.config.port, timeout=probe_timeout):
                    # Server is ready!
                    logger.debug("Server readiness confirmed after %.2fs", elapsed)
                    return

                # Brief sleep before next check (polling interval)
                time.sleep(TIMEOUT_SHORT)

            # Timeout reached - check one last time for errors
            elapsed = time.time() - start_time
            logger.error("Server readiness timeout after %.2fs", elapsed)
            if self.startup_error:
                logger.error("Startup error: %s", self.startup_error)
                raise RuntimeError(f"Server failed to start: {self.startup_error}") from self.startup_error

            # Server didn't become ready - provide diagnostic info
            logger.error("Thread alive: %s", self.thread.is_alive() if self.thread else "N/A")
            logger.error("Thread name: %s", self.thread.name if self.thread else "N/A")
            # Attempt cleanup before raising to avoid thread leak in tests
            try:
                self.stop(timeout=TIMEOUT_DEFAULT)
            except Exception:
                logger.debug(
                    "Error during cleanup after server timeout, continuing",
                    exc_info=True,
                )
            # Disambiguate likely causes in error message
            raise RuntimeError(
                "Server did not become ready within "
                f"{ready_timeout}s. Transport status: "
                f"thread_alive={self.thread.is_alive() if self.thread else False}. "
                "If binding errors occurred, you'll see them above. "
                "If transport is listening but health endpoints failed, "
                "verify /health/details or /health readiness and security middleware configuration."
            )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the server gracefully with hard timeout cap."""
        import time

        stop_start = time.time()
        max_stop_time = timeout  # Use provided timeout as hard cap

        if self.server:
            logger.info("Stopping Uvicorn server...")
            self.should_exit.set()
            if hasattr(self.server, "should_exit"):
                self.server.should_exit = True

            # Stop thread with timeout
            if self.thread and self.thread.is_alive():
                remaining_time = max_stop_time - (time.time() - stop_start)
                if remaining_time > 0.1:
                    self.thread.join(timeout=min(timeout, remaining_time))
                else:
                    logger.warning("Insufficient time remaining for thread join")

            # Stop event loop if still running
            if self.loop and not self.loop.is_closed():
                try:
                    remaining_time = max_stop_time - (time.time() - stop_start)
                    if remaining_time > 0.1:
                        if self.loop.is_running():
                            self.loop.call_soon_threadsafe(self.loop.stop)
                    else:
                        logger.warning("Insufficient time remaining for loop stop")
                except Exception:
                    logger.debug("Error stopping Uvicorn loop during stop()", exc_info=True)

            stop_duration = time.time() - stop_start
            if stop_duration > timeout * 0.8:
                logger.warning(
                    "Server stop took %.2fs (close to timeout %ds)",
                    stop_duration,
                    timeout,
                )
            else:
                logger.info("Uvicorn server stopped in %.2fs", stop_duration)


def _verify_viola_server(host: str, port: int) -> bool:
    """Verify that a server on host:port is actually a Viola server."""
    try:
        import ssl
        import urllib.request

        from config.settings import settings

        scheme = "https" if settings.ssl_enabled else "http"
        req = urllib.request.Request(f"{scheme}://{host}:{port}/health")
        req.add_header("Connection", "close")
        ssl_ctx = None
        if scheme == "https":
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        response = urllib.request.urlopen(req, timeout=TIMEOUT_SHUTDOWN, context=ssl_ctx)  # nosec B310
        data = response.read().decode("utf-8", errors="ignore")
        response.close()
        # Viola servers should respond with JSON containing status info
        # Basic check: if it's JSON and has common Viola fields, it's likely our server
        if "status" in data.lower() or "ok" in data.lower() or "health" in data.lower():
            return True
    except Exception as e:
        logger.debug("Failed to verify Viola server (non-critical): %s", e, exc_info=True)
    return False


def _is_port_available(host: str, port: int) -> bool:
    """Check if a port is available for binding (not just if it's open)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            # Try to bind to the port - this is the real test
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
                return True
            except OSError:
                # Port is in use
                return False
    except Exception as e:
        logger.debug("Port availability check failed (non-critical): %s", e, exc_info=True)
        return False


def create_server(app: Any, host: str, port: int) -> StoppableServer:
    """
    Create and start Uvicorn server with validation and safety checks.

    Args:
        app: FastAPI application instance
        host: Server host address
        port: Server port

    Returns:
        StoppableServer instance (UvicornServer or ExternalServer)

    Raises:
        ValueError: If host/port validation fails
        RuntimeError: If server startup fails or port is not available
    """
    logger.info("create_server called: host=%s, port=%d, app=%s", host, port, type(app).__name__)
    try:
        # NETWORK SECURITY: Validate host/port before binding
        logger.debug("Validating host=%s, port=%d", host, port)
        is_valid_host, host_error = validate_host(host)
        if not is_valid_host:
            logger.error("Host validation failed: %s", host_error)
            raise ValueError(f"Invalid host for server binding: {host_error}")

        is_valid_port, port_error = validate_port(port)
        if not is_valid_port:
            logger.error("Port validation failed: %s", port_error)
            raise ValueError(f"Invalid port for server binding: {port_error}")

        logger.debug("Host/port validation passed")

        # Security warning for non-localhost binding
        if host in ["0.0.0.0"]:  # nosec B104
            logger.warning(
                "SECURITY WARNING: Binding to all interfaces (0.0.0.0) on port %s. This exposes the server to your network. Ensure firewall is configured.",
                port,
            )
        elif host not in [LOCALHOST, "localhost"]:
            logger.warning(
                "SECURITY WARNING: Binding to external host '%s' on port %s. Ensure proper authentication and firewall rules are in place.",
                host,
                port,
            )

        # Check if port is available for binding (right before we try to use it)
        logger.debug("Checking if port %d is available on %s", port, host)
        port_available = _is_port_available(host, port)
        logger.debug("Port availability check result: %s", port_available)

        if not port_available:
            # Port is in use - check if it's actually a Viola server
            logger.warning("Port %d is already in use on %s", port, host)

            # Try to verify if it's a Viola server
            # Use loopback for probing — wildcard addresses aren't routable on Windows.
            probe_host = LOCALHOST if host in ("0.0.0.0", "::") else host  # nosec B104
            logger.debug(
                "Verifying if existing server on %s:%d is a Viola server",
                probe_host,
                port,
            )
            is_viola = _verify_viola_server(probe_host, port)
            logger.debug("Viola server verification result: %s", is_viola)

            if is_viola:
                logger.info("Detected existing Viola server on %s:%d; reusing it", host, port)

                class ExternalServer:
                    def stop(self, timeout: float = 5.0) -> None:
                        # Do not attempt to stop externally managed server
                        logger.info("External server in use; skip stop()")

                from config.settings import settings as _cfg

                _scheme = "https" if _cfg.ssl_enabled else "http"
                _ws_scheme = "wss" if _cfg.ssl_enabled else "ws"
                logger.info("UI server available at %s://%s:%d", _scheme, host, port)
                logger.info("WebSocket endpoint: %s://%s:%d/ws/events", _ws_scheme, host, port)
                logger.info("To play music, send a command to /v1/command")
                logger.info(
                    "Example: python -c \"import json,sys,requests; sys.stdout.write(json.dumps(requests.post('%s://%s:%d/v1/command', json={'text':'play never gonna give you up'}, verify=False).json())+'\\\\n')\"",
                    _scheme,
                    host,
                    port,
                )
                logger.info("Change volume: 'volume 80'")
                logger.info("Push-to-talk: User can say 'enable push-to-talk' to configure")
                return ExternalServer()
            else:
                # Port is in use by something else
                error_msg = (
                    f"Port {port} on {host} is already in use by another application. "
                    f"Please free the port or set VIOLA_API_PORT to a different port."
                )
                logger.error("%s", error_msg)
                raise RuntimeError(error_msg)

        logger.info("Starting Uvicorn server on %s:%d...", host, port)
        logger.debug("Creating UvicornServer instance")
        server = UvicornServer(app, host, port)
        logger.debug("UvicornServer instance created, calling start()")
        try:
            # Start server and wait for it to be ready
            server.start(wait_for_ready=True, ready_timeout=TIMEOUT_MINUTE)
            from diagnostics.startup_telemetry import record_port_bound, run_post_bind_initializers

            record_port_bound()
            run_post_bind_initializers(app)
            logger.debug("server.start() completed successfully")
        except RuntimeError as start_err:
            # Server failed to start - provide detailed error message
            logger.exception("Server startup failed: %s", start_err)
            # Ensure cleanup to avoid thread leaks in tests
            try:
                server.stop(timeout=TIMEOUT_DEFAULT)
            except Exception:
                logger.debug(
                    "Error during cleanup after startup failure, continuing",
                    exc_info=True,
                )
            raise RuntimeError(
                f"Failed to start server on {host}:{port}. "
                f"Error: {start_err}. "
                f"Check if another process is using the port or if there are firewall issues."
            ) from start_err
        except Exception as start_err:
            logger.exception("Unexpected error during server.start(): %s", start_err)
            try:
                server.stop(timeout=TIMEOUT_DEFAULT)
            except Exception:
                logger.debug(
                    "Error during cleanup after unexpected startup error, continuing",
                    exc_info=True,
                )
            raise RuntimeError(f"Unexpected error starting server on {host}:{port}: {start_err}") from start_err

        from config.settings import settings as _cfg

        _scheme = "https" if _cfg.ssl_enabled else "http"
        _ws_scheme = "wss" if _cfg.ssl_enabled else "ws"
        logger.info("UI server started successfully at %s://%s:%d", _scheme, host, port)
        logger.info("WebSocket endpoint: %s://%s:%d/ws/events", _ws_scheme, host, port)
        logger.info("To play music, send a command to /v1/command")
        logger.info(
            "Example: python -c \"import json,sys,requests; sys.stdout.write(json.dumps(requests.post('%s://%s:%d/v1/command', json={'text':'play never gonna give you up'}, verify=False).json())+'\\\\n')\"",
            _scheme,
            host,
            port,
        )
        logger.info("Change volume: 'volume 80'")
        logger.info("Push-to-talk: User can say 'enable push-to-talk' to configure")
        return server
    except (ValueError, RuntimeError):
        # Re-raise validation and runtime errors as-is
        logger.debug("Re-raising ValueError/RuntimeError from create_server")
        raise
    except Exception as e:
        logger.exception("Failed to start Uvicorn server: %s", e)
        raise RuntimeError(f"Unexpected error starting server on {host}:{port}: {e}") from e
