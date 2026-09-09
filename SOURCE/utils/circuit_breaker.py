"""
Circuit Breaker Pattern Implementation
=======================================

Prevents cascade failures by stopping requests to a failing service.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Service failing, requests rejected immediately
- HALF_OPEN: Testing if service recovered

Usage:
    from core.constants import TIMEOUT_VERY_LONG
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=TIMEOUT_VERY_LONG)

    if cb.allow_request():
        try:
            result = make_request()
            cb.record_success()
        except Exception as e:
            logger.debug("Request failed, recording circuit breaker failure: %s", e)
            cb.record_failure()
    else:
        raise CircuitOpenError("Service unavailable")

Thread Safety:
    All operations are thread-safe via RLock.

This is the canonical circuit breaker for the entire NOVVIOLA project.
All other modules should import from here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.constants import TIMEOUT_VERY_LONG
from core.exceptions import CircuitOpenError
from core.logging_config import get_logger

logger = get_logger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitMetrics:
    """Metrics for monitoring circuit breaker behavior."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rejected_requests: int = 0
    state_changes: int = 0
    current_state: CircuitState = CircuitState.CLOSED
    last_failure_time: float | None = None
    last_success_time: float | None = None
    consecutive_failures: int = 0


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""

    failure_threshold: int = 3
    """Consecutive failures before opening circuit."""

    recovery_timeout: float = TIMEOUT_VERY_LONG
    """Seconds before attempting recovery (half-open state)."""

    success_threshold: int = 1
    """Successful requests in half-open before closing."""

    on_state_change: Callable[[CircuitState, CircuitState], None] | None = None
    """Optional callback when state changes."""


class CircuitBreaker:
    """
    Thread-safe circuit breaker implementation.

    Protects services from cascade failures by tracking failures and
    temporarily blocking requests when a threshold is exceeded.

    Args:
        failure_threshold: Consecutive failures to open circuit (default 3)
        recovery_timeout: Seconds before testing recovery (default 30)
        success_threshold: Successes in half-open before closing (default 1)
        on_state_change: Optional callback for state transitions
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = TIMEOUT_VERY_LONG,
        success_threshold: int = 1,
        on_state_change: Callable[[CircuitState, CircuitState], None] | None = None,
    ):
        self._config = CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            success_threshold=success_threshold,
            on_state_change=on_state_change,
        )

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._half_open_successes = 0
        self._last_failure_time: float | None = None
        self._last_success_time: float | None = None
        self._lock = threading.RLock()

        # Metrics
        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
        self._rejected_requests = 0
        self._state_changes = 0

    @property
    def state(self) -> CircuitState:
        """Current circuit state (thread-safe)."""
        with self._lock:
            return self._state

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)."""
        with self._lock:
            return self._state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        with self._lock:
            return self._state == CircuitState.CLOSED

    @property
    def failures(self) -> int:
        """Current consecutive failure count (thread-safe)."""
        with self._lock:
            return self._consecutive_failures

    @property
    def recovery_timeout(self) -> float:
        """Recovery timeout in seconds."""
        return self._config.recovery_timeout

    @property
    def last_failure_time(self) -> float | None:
        """Monotonic timestamp of last recorded failure."""
        with self._lock:
            return self._last_failure_time

    @property
    def metrics(self) -> CircuitMetrics:
        """Get current metrics snapshot (thread-safe)."""
        with self._lock:
            return CircuitMetrics(
                total_requests=self._total_requests,
                successful_requests=self._successful_requests,
                failed_requests=self._failed_requests,
                rejected_requests=self._rejected_requests,
                state_changes=self._state_changes,
                current_state=self._state,
                last_failure_time=self._last_failure_time,
                last_success_time=self._last_success_time,
                consecutive_failures=self._consecutive_failures,
            )

    def allow_request(self) -> bool:
        """
        Check if a request should be allowed.

        Returns:
            True if request should proceed, False if circuit is open

        Note:
            Call record_success() or record_failure() after the request completes.
        """
        with self._lock:
            self._total_requests += 1

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                if self._should_attempt_recovery():
                    self._transition_to(CircuitState.HALF_OPEN)
                    logger.debug("Circuit breaker entering half-open state for recovery test")
                    return True
                self._rejected_requests += 1
                return False

            if self._state == CircuitState.HALF_OPEN:
                # Allow limited requests in half-open for testing
                return True

            return False

    def record_success(self) -> None:
        """Record a successful request."""
        with self._lock:
            self._successful_requests += 1
            self._last_success_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self._config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
                    self._consecutive_failures = 0
                    self._half_open_successes = 0
                    logger.info("Circuit breaker closed - service recovered")
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._consecutive_failures = 0

    def record_failure(self, error: Exception | None = None) -> None:
        """
        Record a failed request.

        Args:
            error: Optional exception for logging
        """
        with self._lock:
            self._failed_requests += 1
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()

            if error:
                logger.debug("Circuit breaker recorded failure: %s", error)

            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
                self._half_open_successes = 0
                logger.warning("Circuit breaker reopened - service still failing")

            elif self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self._config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)
                    logger.warning(
                        "Circuit breaker opened after %s consecutive failures",
                        self._consecutive_failures,
                    )

    def reset(self) -> None:
        """Manually reset circuit to closed state."""
        with self._lock:
            old_state = self._state
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._half_open_successes = 0

            if old_state != CircuitState.CLOSED:
                self._state_changes += 1
                logger.info("Circuit breaker manually reset to closed")

                if self._config.on_state_change:
                    try:
                        self._config.on_state_change(old_state, CircuitState.CLOSED)
                    except Exception as e:
                        logger.debug("State change callback failed: %s", e)

    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if self._last_failure_time is None:
            return True
        elapsed = time.monotonic() - self._last_failure_time
        return elapsed >= self._config.recovery_timeout

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state (caller must hold lock)."""
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            self._state_changes += 1

            logger.debug("Circuit breaker: %s -> %s", old_state.value, new_state.value)

            if self._config.on_state_change:
                try:
                    self._config.on_state_change(old_state, new_state)
                except Exception as e:
                    logger.debug("State change callback failed: %s", e)


# Convenience decorator for circuit breaker protection
def with_circuit_breaker(
    circuit_breaker: CircuitBreaker,
    fallback: Callable[[], Any] | None = None,
):
    """
    Decorator to protect a function with circuit breaker.

    Args:
        circuit_breaker: CircuitBreaker instance to use
        fallback: Optional fallback function when circuit is open

    Usage:
        cb = CircuitBreaker()

        @with_circuit_breaker(cb, fallback=lambda: {"cached": True})
        def fetch_data():
            return requests.get(url).json()
    """

    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            if not circuit_breaker.allow_request():
                if fallback:
                    return fallback()
                raise CircuitOpenError(func.__name__, circuit_breaker._config.recovery_timeout)

            try:
                result = func(*args, **kwargs)
                circuit_breaker.record_success()
                return result
            except Exception as e:
                logger.exception("Circuit breaker intercepted failure: %s", e)
                circuit_breaker.record_failure(e)
                raise

        return wrapper

    return decorator


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitMetrics",
    "CircuitOpenError",
    "CircuitState",
    "with_circuit_breaker",
]
