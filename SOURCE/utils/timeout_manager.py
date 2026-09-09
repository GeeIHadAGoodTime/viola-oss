"""
Timeout Manager Service
========================
Centralized timeout management for long-running operations (GPT, API calls, etc).

Key Features:
- Configurable timeouts per operation type
- Graceful timeout handling with fallback
- Timeout metrics and monitoring
- Circuit breaker pattern for failing services

Part of the unified service architecture for NOVVIOLA.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, TypeVar

from core.exceptions import CircuitOpenError
from core.logging_config import get_logger
from utils.circuit_breaker import CircuitBreaker

logger = get_logger(__name__)

T = TypeVar("T")


# ---------- Timeout Configuration ----------


@dataclass
class TimeoutConfig:
    """Configuration for timeout management."""

    # Operation timeouts (seconds)
    gpt_api_call: float = 20.0
    youtube_resolution: float = 15.0
    transcription: float = 30.0
    tts_synthesis: float = 10.0
    playlist_load: float = 30.0
    default: float = 10.0

    # Circuit breaker settings
    failure_threshold: int = 5  # Consecutive failures before circuit opens
    recovery_timeout: float = 60.0  # Seconds before trying again


@dataclass
class TimeoutMetrics:
    """Metrics for timeout monitoring."""

    operation_name: str
    total_calls: int = 0
    timeouts: int = 0
    errors: int = 0
    successes: int = 0
    total_duration: float = 0.0
    avg_duration: float = 0.0

    def update_success(self, duration: float) -> None:
        """Record a successful operation."""
        self.total_calls += 1
        self.successes += 1
        self.total_duration += duration
        self.avg_duration = self.total_duration / self.total_calls

    def update_timeout(self) -> None:
        """Record a timeout."""
        self.total_calls += 1
        self.timeouts += 1

    def update_error(self) -> None:
        """Record an error."""
        self.total_calls += 1
        self.errors += 1

    @property
    def timeout_rate(self) -> float:
        """Calculate timeout rate (0.0 to 1.0)."""
        return self.timeouts / self.total_calls if self.total_calls > 0 else 0.0

    @property
    def success_rate(self) -> float:
        """Calculate success rate (0.0 to 1.0)."""
        return self.successes / self.total_calls if self.total_calls > 0 else 0.0


# ---------- Timeout Manager ----------


class TimeoutManager:
    """
    Centralized timeout management for all long-running operations.

    Features:
    - Per-operation timeout configuration
    - Automatic retry with backoff
    - Circuit breaker for failing services
    - Metrics and monitoring
    - Fallback value support
    """

    def __init__(self, config: TimeoutConfig | None = None):
        """Initialize timeout manager."""
        self.config = config or TimeoutConfig()
        self._metrics: dict[str, TimeoutMetrics] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._executor = ThreadPoolExecutor(max_workers=8)
        self._closed = False

        logger.info("⏱️ TimeoutManager initialized with config: %s", self.config)

    async def run_with_timeout(
        self,
        operation: Callable[[], Awaitable[T]],
        operation_name: str,
        timeout: float | None = None,
        fallback: T | None = None,
        use_circuit_breaker: bool = True,
        max_attempts: int = 1,
        backoff_initial: float = 0.5,
        backoff_multiplier: float = 2.0,
    ) -> T:
        """
        Run an async operation with timeout protection.

        Args:
            operation: Async callable to execute
            operation_name: Name for logging and metrics
            timeout: Timeout in seconds (None = use default for operation)
            fallback: Value to return on timeout/error
            use_circuit_breaker: Whether to use circuit breaker pattern

        Returns:
            Result of operation or fallback value

        Raises:
            asyncio.TimeoutError: If no fallback provided and timeout occurs
            Exception: If operation fails and no fallback provided
        """
        # Get timeout for this operation
        if timeout is None:
            timeout = self._get_timeout_for_operation(operation_name)

        # Check circuit breaker
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        breaker: CircuitBreaker | None = None
        if use_circuit_breaker:
            breaker = self._get_circuit_breaker(operation_name)
            if breaker.is_open:
                logger.warning("⚡ Circuit breaker open for %s, fast-failing", operation_name)
                if fallback is not None:
                    return fallback
                lft = breaker.last_failure_time or 0.0
                remaining = max(0.0, breaker.recovery_timeout - (time.monotonic() - lft))
                raise CircuitOpenError(operation_name, remaining)

        # Get or create metrics
        metrics = self._get_metrics(operation_name)

        attempt = 1
        delay = backoff_initial
        while True:
            start_time = time.time()
            try:
                result = await asyncio.wait_for(operation(), timeout=timeout)

                duration = time.time() - start_time
                metrics.update_success(duration)
                if breaker:
                    breaker.record_success()

                logger.debug(
                    "✅ %s completed in %.2fs (attempt %s/%s)",
                    operation_name,
                    duration,
                    attempt,
                    max_attempts,
                )
                return result
            except TimeoutError:
                metrics.update_timeout()
                if breaker:
                    breaker.record_failure()
                logger.warning(
                    "⏱️ %s timed out after %ss (attempt %s/%s)",
                    operation_name,
                    timeout,
                    attempt,
                    max_attempts,
                )
                if attempt >= max_attempts:
                    if fallback is not None:
                        return fallback
                    raise
            except Exception as exc:
                metrics.update_error()
                if breaker:
                    breaker.record_failure()
                logger.error(
                    "❌ %s failed on attempt %s/%s: %s",
                    operation_name,
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt >= max_attempts:
                    if fallback is not None:
                        return fallback
                    raise
            attempt += 1
            await asyncio.sleep(delay)
            delay *= backoff_multiplier

    def run_sync_with_timeout(
        self,
        operation: Callable[[], T],
        operation_name: str,
        timeout: float | None = None,
        fallback: T | None = None,
        use_circuit_breaker: bool = True,
        max_attempts: int = 1,
        backoff_initial: float = 0.5,
        backoff_multiplier: float = 2.0,
    ) -> T:
        """
        Run a synchronous operation with timeout protection and retries.
        """

        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        if timeout is None:
            timeout = self._get_timeout_for_operation(operation_name)

        breaker: CircuitBreaker | None = None
        if use_circuit_breaker:
            breaker = self._get_circuit_breaker(operation_name)
            if breaker.is_open:
                logger.warning("⚡ Circuit breaker open for %s, fast-failing", operation_name)
                if fallback is not None:
                    return fallback
                lft = breaker.last_failure_time or 0.0
                remaining = max(0.0, breaker.recovery_timeout - (time.monotonic() - lft))
                raise CircuitOpenError(operation_name, remaining)

        metrics = self._get_metrics(operation_name)
        attempt = 1
        delay = backoff_initial

        while True:
            start_time = time.time()
            future = self._executor.submit(operation)
            try:
                result = future.result(timeout=timeout)
                duration = time.time() - start_time
                metrics.update_success(duration)
                if breaker:
                    breaker.record_success()
                logger.debug(
                    "✅ %s completed in %.2fs (attempt %s/%s)",
                    operation_name,
                    duration,
                    attempt,
                    max_attempts,
                )
                return result
            except FuturesTimeout:
                future.cancel()
                metrics.update_timeout()
                if breaker:
                    breaker.record_failure()
                logger.warning(
                    "⏱️ %s timed out after %ss (attempt %s/%s)",
                    operation_name,
                    timeout,
                    attempt,
                    max_attempts,
                )
                if attempt >= max_attempts:
                    if fallback is not None:
                        return fallback
                    raise TimeoutError(f"{operation_name} timed out after {timeout}s") from None
            except Exception as exc:
                metrics.update_error()
                if breaker:
                    breaker.record_failure()
                logger.error(
                    "❌ %s failed on attempt %s/%s: %s",
                    operation_name,
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt >= max_attempts:
                    if fallback is not None:
                        return fallback
                    raise
            attempt += 1
            time.sleep(delay)
            delay *= backoff_multiplier

    def _get_timeout_for_operation(self, operation_name: str) -> float:
        """Get configured timeout for an operation."""
        # Map operation names to config attributes
        timeout_map = {
            "gpt": self.config.gpt_api_call,
            "gpt_api": self.config.gpt_api_call,
            "openai": self.config.gpt_api_call,
            "youtube": self.config.youtube_resolution,
            "youtube_api": self.config.youtube_resolution,
            "resolve": self.config.youtube_resolution,
            "transcribe": self.config.transcription,
            "stt": self.config.transcription,
            "whisper": self.config.transcription,
            "tts": self.config.tts_synthesis,
            "speak": self.config.tts_synthesis,
            "playlist": self.config.playlist_load,
        }

        # Check for matching keyword in operation name
        operation_lower = operation_name.lower()
        for keyword, timeout in timeout_map.items():
            if keyword in operation_lower:
                return timeout

        return self.config.default

    def _get_metrics(self, operation_name: str) -> TimeoutMetrics:
        """Get or create metrics for an operation."""
        if operation_name not in self._metrics:
            self._metrics[operation_name] = TimeoutMetrics(operation_name=operation_name)
        return self._metrics[operation_name]

    def _get_circuit_breaker(self, operation_name: str) -> CircuitBreaker:
        """Get or create circuit breaker for an operation."""
        if operation_name not in self._circuit_breakers:
            self._circuit_breakers[operation_name] = CircuitBreaker(
                failure_threshold=self.config.failure_threshold,
                recovery_timeout=self.config.recovery_timeout,
            )
        return self._circuit_breakers[operation_name]

    def get_metrics(self) -> dict[str, TimeoutMetrics]:
        """Get all operation metrics."""
        return dict(self._metrics)

    def get_circuit_breaker_states(self) -> dict[str, str]:
        """Get state of all circuit breakers."""
        return {name: breaker.state.value for name, breaker in self._circuit_breakers.items()}

    def reset_metrics(self) -> None:
        """Reset all metrics."""
        self._metrics.clear()
        logger.info("📊 Timeout metrics reset")

    def reset_circuit_breakers(self) -> None:
        """Reset all circuit breakers to CLOSED state."""
        for breaker in self._circuit_breakers.values():
            breaker.reset()
        logger.info("⚡ Circuit breakers reset")

    def close(self) -> None:
        """Shut down internal resources to avoid leaking threads."""
        if self._closed:
            return
        self._executor.shutdown(wait=True)
        self._closed = True

    def __enter__(self) -> TimeoutManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None:
        self.close()


# Module-level singleton
_timeout_manager: TimeoutManager | None = None
_timeout_manager_lock = __import__("threading").Lock()


def get_timeout_manager(config: TimeoutConfig | None = None) -> TimeoutManager:
    """
    Get or create the timeout manager singleton.

    Args:
        config: Optional configuration used only when creating a new instance.
    """
    global _timeout_manager
    if _timeout_manager is not None:
        if config is not None:
            logger.warning("TimeoutManager already initialized; ignoring subsequent config override.")
        return _timeout_manager

    with _timeout_manager_lock:
        if _timeout_manager is not None:
            if config is not None:
                logger.warning("TimeoutManager already initialized; ignoring subsequent config override.")
            return _timeout_manager
        _timeout_manager = TimeoutManager(config)
        return _timeout_manager


def reset_timeout_manager() -> None:
    """Remove the cached TimeoutManager instance (for tests)."""
    global _timeout_manager
    with _timeout_manager_lock:
        _timeout_manager = None
