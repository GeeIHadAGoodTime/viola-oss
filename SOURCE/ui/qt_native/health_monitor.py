"""
Backend Health Monitor with Exponential Backoff

Monitors backend health without blocking the UI thread.
Uses exponential backoff on failures to reduce load on failing services.

Integrates with StartupCoordinator and CircuitBreaker patterns.

Usage:
    from config.settings import get_settings
    monitor = HealthMonitor(base_url=get_settings().base_url)
    monitor.state_changed.connect(on_state_change)
    monitor.start()
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from core.constants import TIMEOUT_SHUTDOWN, TIMEOUT_VERY_LONG, Status
from core.logging_config import get_logger
from utils.circuit_breaker import CircuitBreaker, CircuitState

logger = get_logger(__name__)


class BackendState(Enum):
    """Backend connection state."""

    UNKNOWN = "unknown"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


class BackoffStrategy:
    """Exponential backoff strategy with jitter."""

    def __init__(
        self,
        initial_interval: float = 1.0,
        max_interval: float = 30.0,
        multiplier: float = 2.0,
        jitter: float = 0.1,
    ):
        self.initial_interval = initial_interval
        self.max_interval = max_interval
        self.multiplier = multiplier
        self.jitter = jitter
        self._current = initial_interval
        self._failures = 0

    def next_interval(self) -> float:
        """Get next backoff interval and increment failure count."""
        import random

        self._failures += 1
        # Apply jitter: +/- jitter%
        jitter_factor = 1 + (random.random() * 2 - 1) * self.jitter
        interval = self._current * jitter_factor

        # Increase for next failure
        self._current = min(self._current * self.multiplier, self.max_interval)

        return interval

    def reset(self) -> None:
        """Reset backoff to initial state."""
        self._current = self.initial_interval
        self._failures = 0

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def current_interval(self) -> float:
        return self._current


class HealthCheckResult:
    """Result of a health check probe."""

    def __init__(
        self,
        success: bool,
        latency_ms: float | None = None,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ):
        self.success = success
        self.latency_ms = latency_ms
        self.details = details or {}
        self.error = error
        self.timestamp = time.time()

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAILED"
        latency = f"{self.latency_ms:.1f}ms" if self.latency_ms else "N/A"
        return f"HealthCheckResult({status}, latency={latency})"


class HealthMonitor(QObject):
    """
    Non-blocking health monitor with exponential backoff.

    Runs health checks in a background thread, emits signals for
    state changes and health updates.

    Signals:
        state_changed(BackendState): Backend state changed
        health_check_completed(dict): Successful health check with details
        health_check_failed(str): Failed health check with error message
        circuit_state_changed(CircuitState): Circuit breaker state changed

    Configuration:
        INITIAL_INTERVAL: Starting check interval (1 second)
        MAX_INTERVAL: Maximum backoff interval (30 seconds)
        SUCCESS_INTERVAL: Interval when healthy (5 seconds)
        BACKOFF_MULTIPLIER: How quickly to back off (2x)
    """

    # Qt signals
    state_changed = Signal(object)  # BackendState
    health_check_completed = Signal(dict)
    health_check_failed = Signal(str)
    circuit_state_changed = Signal(object)  # CircuitState

    # Backoff configuration
    INITIAL_INTERVAL = 1.0
    MAX_INTERVAL = TIMEOUT_VERY_LONG
    BACKOFF_MULTIPLIER = 2.0
    SUCCESS_INTERVAL = 5.0
    HEALTH_CHECK_TIMEOUT = TIMEOUT_SHUTDOWN

    def __init__(
        self,
        base_url: str | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        health_check_fn: Callable[[], HealthCheckResult] | None = None,
    ):
        super().__init__()
        if base_url is None:
            from config.settings import get_settings

            base_url = get_settings().base_url
        self._base_url = base_url.rstrip("/")
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=TIMEOUT_VERY_LONG,
            on_state_change=self._on_circuit_state_change,
        )
        self._custom_health_check = health_check_fn

        self._state = BackendState.UNKNOWN
        self._backoff = BackoffStrategy(
            initial_interval=self.INITIAL_INTERVAL,
            max_interval=self.MAX_INTERVAL,
            multiplier=self.BACKOFF_MULTIPLIER,
        )
        self._current_interval = self.INITIAL_INTERVAL

        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._force_check_event = threading.Event()

        # Last check result for diagnostics
        self._last_result: HealthCheckResult | None = None

    @property
    def state(self) -> BackendState:
        """Current backend connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if backend is currently connected."""
        return self._state == BackendState.CONNECTED

    @property
    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running

    @property
    def last_result(self) -> HealthCheckResult | None:
        """Get last health check result."""
        return self._last_result

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get circuit breaker instance."""
        return self._circuit_breaker

    def start(self) -> None:
        """Start health monitoring (non-blocking)."""
        if self._running:
            logger.debug("HealthMonitor already running")
            return

        self._running = True
        self._stop_event.clear()
        self._update_state(BackendState.CONNECTING)

        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="HealthMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("Health monitor started")

    def stop(self) -> None:
        """Stop health monitoring."""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=TIMEOUT_SHUTDOWN)

        logger.info("Health monitor stopped")

    def force_check(self) -> None:
        """Trigger an immediate health check."""
        self._force_check_event.set()

    def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        import requests

        while self._running:
            self._stop_event.clear()
            self._force_check_event.clear()

            # Perform health check
            result = self._perform_health_check(requests)

            if result.success:
                self._handle_success(result)
            else:
                self._handle_failure(result)

            # Wait for next check (interruptible by stop or force_check)
            self._wait_for_next_check()

    def _perform_health_check(self, requests_module: Any) -> HealthCheckResult:
        """Perform a single health check probe."""
        # Check circuit breaker first
        if not self._circuit_breaker.allow_request():
            return HealthCheckResult(
                success=False,
                error="Circuit breaker open",
                details={"circuit_state": self._circuit_breaker.state.value},
            )

        # Use custom health check if provided
        if self._custom_health_check:
            try:
                return self._custom_health_check()
            except Exception as e:
                logger.exception("Custom health check failed: %s", e)
                return HealthCheckResult(success=False, error=str(e))

        # Default HTTP health check
        start_time = time.perf_counter()
        try:
            response = requests_module.get(
                f"{self._base_url}/health",
                timeout=self.HEALTH_CHECK_TIMEOUT,
            )
            latency_ms = (time.perf_counter() - start_time) * 1000

            if response.status_code == 200:
                try:
                    details = response.json()
                except ValueError:
                    details = {"status": Status.OK}

                return HealthCheckResult(
                    success=True,
                    latency_ms=latency_ms,
                    details=details,
                )
            else:
                return HealthCheckResult(
                    success=False,
                    latency_ms=latency_ms,
                    error=f"HTTP {response.status_code}",
                    details={"status_code": response.status_code},
                )

        except requests_module.Timeout:
            return HealthCheckResult(
                success=False,
                error="Health check timed out",
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )
        except requests_module.ConnectionError:
            return HealthCheckResult(
                success=False,
                error="Cannot connect to backend",
            )
        except Exception as e:
            logger.exception("Health check failed: %s", e)
            return HealthCheckResult(
                success=False,
                error=str(e),
            )

    def _handle_success(self, result: HealthCheckResult) -> None:
        """Handle successful health check."""
        self._circuit_breaker.record_success()
        self._backoff.reset()
        self._current_interval = self.SUCCESS_INTERVAL
        self._last_result = result

        new_state = BackendState.CONNECTED
        self._update_state(new_state)

        # Emit success signal
        self.health_check_completed.emit(result.details)

        logger.debug("Health check passed: latency=%.1fms", result.latency_ms)

    def _handle_failure(self, result: HealthCheckResult) -> None:
        """Handle failed health check with exponential backoff."""
        self._circuit_breaker.record_failure()
        self._current_interval = self._backoff.next_interval()
        self._last_result = result

        # Determine new state based on circuit breaker
        if self._circuit_breaker.state == CircuitState.OPEN:
            new_state = BackendState.DISCONNECTED
        else:
            new_state = BackendState.DEGRADED

        self._update_state(new_state)

        # Emit failure signal
        self.health_check_failed.emit(result.error or "Unknown error")

        logger.debug(
            "Health check failed (attempt %s, next in %ss): %s",
            self._backoff.failure_count,
            format(self._current_interval, ".1f"),
            result.error,
        )

    def _wait_for_next_check(self) -> None:
        """Wait for next check interval or until interrupted."""
        # Use shorter sleeps to be more responsive to stop/force events
        remaining = self._current_interval
        check_interval = 0.1

        while remaining > 0 and self._running:
            if self._stop_event.is_set() or self._force_check_event.is_set():
                break
            wait_time = min(check_interval, remaining)
            time.sleep(wait_time)
            remaining -= wait_time

    def _update_state(self, new_state: BackendState) -> None:
        """Update state and emit signal if changed."""
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            logger.info("Backend state: %s -> %s", old_state.value, new_state.value)
            self.state_changed.emit(new_state)

    def _on_circuit_state_change(self, old_state: CircuitState, new_state: CircuitState) -> None:
        """Handle circuit breaker state changes."""
        self.circuit_state_changed.emit(new_state)

        # Update backend state based on circuit
        if new_state == CircuitState.OPEN:
            self._update_state(BackendState.DISCONNECTED)
        elif new_state == CircuitState.CLOSED:
            # Don't immediately mark as connected - wait for next health check
            pass


class HealthMonitorService(QObject):
    """
    Qt service wrapper for HealthMonitor with QTimer-based scheduling.

    Use this when you want health monitoring integrated with Qt's event loop
    rather than a background thread.

    Usage:
        service = HealthMonitorService()
        service.state_changed.connect(update_ui)
        service.start()
    """

    state_changed = Signal(object)  # BackendState
    health_result = Signal(object)  # HealthCheckResult

    def __init__(
        self,
        base_url: str | None = None,
        check_interval_ms: int = 5000,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        if base_url is None:
            from config.settings import get_settings

            base_url = get_settings().base_url
        self._base_url = base_url.rstrip("/")
        self._check_interval_ms = check_interval_ms

        self._state = BackendState.UNKNOWN
        self._backoff = BackoffStrategy()
        self._circuit_breaker = CircuitBreaker()

        self._timer: QTimer | None = None
        self._worker_active = False

    @property
    def state(self) -> BackendState:
        return self._state

    def start(self) -> None:
        """Start health monitoring with QTimer."""
        if self._timer is not None:
            return

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._schedule_check)
        self._timer.start(self._check_interval_ms)

        # Do initial check
        self._schedule_check()

    def stop(self) -> None:
        """Stop health monitoring."""
        if self._timer:
            self._timer.stop()
            self._timer = None

    def _schedule_check(self) -> None:
        """Schedule a health check on worker thread."""
        if self._worker_active:
            return

        self._worker_active = True

        # Import worker infrastructure
        from PySide6.QtCore import QRunnable, QThreadPool

        from .api_base import _APICallRunnableCls

        worker = _APICallRunnableCls(self._perform_check)
        worker.signals.result.connect(self._on_check_result)
        worker.signals.completed.connect(self._on_check_complete)

        # _APICallRunnableCls is _QtAPICallRunnable at runtime which extends QRunnable
        thread_pool = QThreadPool.globalInstance()
        if thread_pool is not None and isinstance(worker, QRunnable):
            thread_pool.start(worker)

    def _perform_check(self) -> HealthCheckResult:
        """Perform health check (runs on worker thread)."""
        import requests

        if not self._circuit_breaker.allow_request():
            return HealthCheckResult(
                success=False,
                error="Circuit breaker open",
            )

        try:
            start = time.perf_counter()
            response = requests.get(f"{self._base_url}/health", timeout=TIMEOUT_SHUTDOWN)
            latency_ms = (time.perf_counter() - start) * 1000

            if response.status_code == 200:
                self._circuit_breaker.record_success()
                return HealthCheckResult(
                    success=True,
                    latency_ms=latency_ms,
                    details=response.json() if response.content else {},
                )
            else:
                self._circuit_breaker.record_failure()
                return HealthCheckResult(
                    success=False,
                    latency_ms=latency_ms,
                    error=f"HTTP {response.status_code}",
                )

        except Exception as e:
            self._circuit_breaker.record_failure()
            return HealthCheckResult(success=False, error=str(e))

    def _on_check_result(self, result: HealthCheckResult) -> None:
        """Handle health check result (runs on Qt main thread)."""
        self.health_result.emit(result)

        if result.success:
            self._backoff.reset()
            new_state = BackendState.CONNECTED
            if self._timer:
                self._timer.setInterval(self._check_interval_ms)
        else:
            interval = int(self._backoff.next_interval() * 1000)
            if self._timer:
                self._timer.setInterval(interval)
            new_state = BackendState.DISCONNECTED

        if self._state != new_state:
            self._state = new_state
            self.state_changed.emit(new_state)

    def _on_check_complete(self) -> None:
        """Mark worker as inactive."""
        self._worker_active = False


__all__ = [
    "BackendState",
    "BackoffStrategy",
    "HealthCheckResult",
    "HealthMonitor",
    "HealthMonitorService",
]
