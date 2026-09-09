"""
Command client for sending commands to the Viola backend.

Provides async command execution with:
- Circuit breaker protection
- Request timeout handling
- Progress feedback via signals
- Request cancellation support

This client is designed to NEVER block the Qt main thread.

Usage:
    client = CommandClient(session)
    client.command_progress.connect(show_progress)
    client.command_completed.connect(handle_result)
    client.command_failed.connect(handle_error)

    handle = client.send_command_async("play some music")
    # Later: handle.cancel() to abort
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import cast

from PySide6.QtCore import QObject, Signal

from contracts.api_response import ensure_envelope
from core.constants import TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from utils.circuit_breaker import CircuitBreaker, CircuitState

from .api_client_mixin import APIClientMixin

logger = get_logger(__name__)


@dataclass
class RequestHandle:
    """Handle for tracking and cancelling async requests."""

    request_id: int
    future: Future[CommandResult]
    started_at: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    command_text: str = ""

    def cancel(self) -> bool:
        """Cancel the request if not already completed."""
        if self.cancelled:
            return False
        if self.future.done():
            return False
        self.cancelled = True
        self.future.cancel()
        return True

    @property
    def elapsed_ms(self) -> float:
        """Time elapsed since request started."""
        return (time.monotonic() - self.started_at) * 1000

    @property
    def is_done(self) -> bool:
        """Check if request is completed or cancelled."""
        return self.cancelled or self.future.done()


@dataclass
class CommandResult:
    """Result of a command execution."""

    success: bool
    data: dict[str, object] = field(default_factory=dict)
    message: str = ""
    error_code: str | None = None
    intent: str | None = None
    latency_ms: float = 0.0

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, object], latency_ms: float = 0.0) -> CommandResult:
        """Create from API response envelope."""
        data_raw = envelope.get("data")
        if isinstance(data_raw, dict):
            data = {key: value for key, value in data_raw.items() if isinstance(key, str)}
        else:
            data = {}
        error = envelope.get("error")
        message_raw = envelope.get("message", "")
        message = message_raw if isinstance(message_raw, str) else ""

        intent: str | None = None
        intent_raw = data.get("intent")
        if isinstance(intent_raw, str):
            intent = intent_raw

        return cls(
            success=bool(envelope.get("ok", False)),
            data=data,
            message=message,
            error_code=error.get("code") if isinstance(error, dict) else None,
            intent=intent,
            latency_ms=latency_ms,
        )

    @classmethod
    def error(cls, code: str, message: str) -> CommandResult:
        """Create error result."""
        return cls(
            success=False,
            error_code=code,
            message=message,
        )


class CommandClient(QObject, APIClientMixin):
    """
    Async command client with circuit breaker protection.

    All command execution happens on background threads, ensuring
    the Qt main thread is never blocked.

    Signals:
        command_progress(str, str): (request_id, status) - Progress updates
        command_completed(str, CommandResult): (request_id, result) - Success
        command_failed(str, str): (request_id, error) - Failure
        command_timeout(str): (request_id) - Request timed out
        circuit_changed(CircuitState): Circuit breaker state changed
    """

    # Signals for command lifecycle
    command_progress = Signal(str, str)  # request_id, status
    command_completed = Signal(str, object)  # request_id, CommandResult
    command_failed = Signal(str, str)  # request_id, error message
    command_timeout = Signal(str)  # request_id

    # Circuit breaker signal
    circuit_changed = Signal(object)  # CircuitState

    # Configuration
    DEFAULT_TIMEOUT = 5.0  # 5 seconds (reduced from typical 10s)
    PROGRESS_UPDATE_INTERVAL = 1.0  # Show "still working" after 1 second
    MAX_WORKERS = 4

    def __init__(
        self,
        session,
        base_url: str | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        super().__init__()
        self.session = session
        self.base_url = base_url  # Set by parent client

        # Create or use provided circuit breaker
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=TIMEOUT_VERY_LONG,
            on_state_change=self._on_circuit_change,
        )

        # Thread pool for async execution
        self._executor = ThreadPoolExecutor(
            max_workers=self.MAX_WORKERS,
            thread_name_prefix="command-client",
        )

        # Request tracking
        self._active_requests: dict[int, RequestHandle] = {}
        self._request_counter = 0
        self._lock = threading.Lock()

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get circuit breaker instance."""
        return self._circuit_breaker

    @property
    def is_available(self) -> bool:
        """Check if commands can be sent (circuit not open)."""
        return not self._circuit_breaker.is_open

    @property
    def active_request_count(self) -> int:
        """Number of currently active requests."""
        with self._lock:
            return len(self._active_requests)

    def send_command(
        self,
        text: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> CommandResult:
        """
        Send command synchronously. USE WITH CAUTION - blocks the calling thread.

        Prefer send_command_async() to avoid blocking the Qt main thread.

        Args:
            text: Command text to execute
            timeout: Request timeout in seconds

        Returns:
            CommandResult with success/failure status
        """
        # Reuse _execute_command with a non-existent request_id (-1)
        # This avoids duplicating the circuit breaker + request logic
        return self._execute_command(-1, text, timeout)

    def send_command_async(
        self,
        text: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_success: Callable[[CommandResult], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_timeout: Callable[[], None] | None = None,
    ) -> RequestHandle:
        """
        Send command asynchronously. Never blocks the Qt main thread.

        Args:
            text: Command text to execute
            timeout: Request timeout in seconds
            on_success: Optional callback on success
            on_error: Optional callback on error
            on_timeout: Optional callback on timeout

        Returns:
            RequestHandle for tracking/cancelling the request
        """
        with self._lock:
            request_id = self._request_counter
            self._request_counter += 1

        # Create future
        future = self._executor.submit(
            self._execute_command,
            request_id,
            text,
            timeout,
        )

        # Create handle
        handle = RequestHandle(
            request_id=request_id,
            future=future,
            command_text=text,
        )

        with self._lock:
            self._active_requests[request_id] = handle

        # Emit progress signal
        self.command_progress.emit(str(request_id), "Processing...")

        # Set up completion callback
        def on_done(f: Future) -> None:
            self._handle_completion(
                request_id,
                handle,
                f,
                on_success=on_success,
                on_error=on_error,
                on_timeout=on_timeout,
            )

        future.add_done_callback(on_done)

        # Start progress timer for long-running requests
        self._start_progress_timer(request_id, handle)

        return handle

    def cancel_request(self, request_id: int | str) -> bool:
        """Cancel a pending request by ID."""
        rid = int(request_id) if isinstance(request_id, str) else request_id
        with self._lock:
            handle = self._active_requests.get(rid)
            if handle:
                return handle.cancel()
        return False

    def cancel_all_requests(self) -> int:
        """Cancel all pending requests. Returns count of cancelled."""
        with self._lock:
            cancelled = 0
            for handle in self._active_requests.values():
                if handle.cancel():
                    cancelled += 1
            return cancelled

    def shutdown(self) -> None:
        """Shutdown client and cleanup resources."""
        self.cancel_all_requests()
        self._executor.shutdown(wait=False)

    def _execute_command(
        self,
        request_id: int,
        text: str,
        timeout: float,
    ) -> CommandResult:
        """Execute command on worker thread."""
        import requests as req_lib

        # Check if cancelled before starting
        with self._lock:
            handle = self._active_requests.get(request_id)
            if handle and handle.cancelled:
                return CommandResult.error("cancelled", "Request cancelled")

        # Check circuit breaker
        if not self._circuit_breaker.allow_request():
            return CommandResult.error(
                "circuit_open",
                "Service temporarily unavailable. Please try again.",
            )

        start_time = time.perf_counter()
        try:
            response = self.session.post(
                f"{self.base_url}/v1/command",
                json={"text": text},
                timeout=timeout,
            )
            response.raise_for_status()
            latency_ms = (time.perf_counter() - start_time) * 1000

            envelope = ensure_envelope(response.json())
            result = CommandResult.from_envelope(cast(dict[str, object], envelope), latency_ms)

            if result.success:
                self._circuit_breaker.record_success()
            else:
                self._circuit_breaker.record_failure()

            return result

        except req_lib.Timeout:
            self._circuit_breaker.record_failure()
            return CommandResult.error("timeout", "Request timed out")

        except req_lib.ConnectionError:
            self._circuit_breaker.record_failure()
            return CommandResult.error("connection_failed", "Cannot connect to backend")

        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.debug("Command execution failed: %s", e)
            return CommandResult.error("request_failed", str(e))

    def _handle_completion(
        self,
        request_id: int,
        handle: RequestHandle,
        future: Future,
        *,
        on_success: Callable[[CommandResult], None] | None,
        on_error: Callable[[str], None] | None,
        on_timeout: Callable[[], None] | None,
    ) -> None:
        """Handle request completion (runs on worker thread)."""
        # Clean up tracking
        with self._lock:
            self._active_requests.pop(request_id, None)

        # Check if cancelled
        if handle.cancelled:
            return

        try:
            result = future.result(timeout=0)

            if result.error_code == "timeout":
                self.command_timeout.emit(str(request_id))
                if on_timeout:
                    on_timeout()
            elif result.success:
                self.command_completed.emit(str(request_id), result)
                if on_success:
                    on_success(result)
            else:
                error_msg = result.message or result.error_code or "Unknown error"
                self.command_failed.emit(str(request_id), error_msg)
                if on_error:
                    on_error(error_msg)

        except Exception as e:
            error_msg = str(e)
            self.command_failed.emit(str(request_id), error_msg)
            if on_error:
                on_error(error_msg)

    def _start_progress_timer(self, request_id: int, handle: RequestHandle) -> None:
        """Start timer to emit progress updates for long-running requests."""
        import threading

        def check_progress() -> None:
            # Wait for progress interval
            time.sleep(self.PROGRESS_UPDATE_INTERVAL)

            # Check if still active
            if handle.is_done:
                return

            # Emit "still working" message
            elapsed = handle.elapsed_ms / 1000
            command_len = len(handle.command_text)
            self.command_progress.emit(
                str(request_id),
                f"Still working... ({elapsed:.1f}s, cmd_len={command_len})",
            )

        thread = threading.Thread(target=check_progress, daemon=True)
        thread.start()

    def _on_circuit_change(
        self,
        old_state: CircuitState,
        new_state: CircuitState,
    ) -> None:
        """Handle circuit breaker state changes."""
        self.circuit_changed.emit(new_state)

        if new_state == CircuitState.OPEN:
            logger.warning("Command circuit breaker opened - requests will be rejected")
        elif new_state == CircuitState.CLOSED:
            logger.info("Command circuit breaker closed - service recovered")


__all__ = [
    "CommandClient",
    "CommandResult",
    "RequestHandle",
]
