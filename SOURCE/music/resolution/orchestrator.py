"""
music/resolution/orchestrator.py

Parallel resolution orchestrator for efficient batch operations.
Handles rate limiting, concurrency control, and error recovery.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from core.logging_config import get_logger
from music.compliance.services import get_services

logger = get_logger(__name__)


@dataclass
class ResolutionResult:
    """Result of a resolution attempt"""

    success: bool
    data: Any  # QueueItem on success, None on failure
    error: str | None = None
    query: str | None = None


class ResolutionFunction(Protocol):
    """Protocol for resolution functions"""

    async def __call__(self, query: str, source: str | None = None, emit: bool = True) -> Any:
        """Resolve a query to a QueueItem"""
        ...


class ResolutionOrchestrator:
    """
    Orchestrates parallel resolution of multiple queries.

    Features:
    - Configurable concurrency (rate limiting)
    - Automatic retry on transient failures
    - Progress callbacks
    - Error aggregation

    Use cases:
    - Playlist loading (resolve 10 songs in parallel)
    - Autoplay queue building (batch resolution)
    - Search results expansion
    """

    def __init__(
        self,
        resolver: ResolutionFunction,
        max_concurrent: int = 5,
        retry_count: int = 1,
        timeout_per_item: float = 30.0,
        provider: str | None = None,
    ):
        """
        Initialize orchestrator.

        Args:
            resolver: Async function to resolve a single query
            max_concurrent: Maximum parallel resolutions (default 5)
            retry_count: Number of retries on failure (default 1)
            timeout_per_item: Timeout per item in seconds (default 30)
            provider: Provider identifier for compliance telemetry
        """
        self._resolver = resolver
        self._max_concurrent = max_concurrent
        self._retry_count = retry_count
        self._timeout = timeout_per_item
        self._provider = provider

        # Stats
        self._total_resolved = 0
        self._total_failed = 0
        self._total_retries = 0

    async def resolve_batch(
        self,
        queries: list[str],
        source: str | None = None,
        emit: bool = False,
        provider: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[ResolutionResult]:
        """
        Resolve multiple queries in parallel with rate limiting.

        Args:
            queries: List of queries to resolve
            source: Source type (ytsearch1, url, local)
            emit: Whether to emit state changes (typically False for batch)
            progress_callback: Optional callback(completed, total)

        Returns:
            List of ResolutionResults (includes both successes and failures)
        """
        if not queries:
            return []

        logger.info(
            "🔄 Resolving %s queries in parallel (max concurrent: %s)",
            len(queries),
            self._max_concurrent,
        )

        # Create semaphore for rate limiting
        semaphore = asyncio.Semaphore(self._max_concurrent)
        completed = 0
        services = get_services()
        provider_name = (provider or self._provider or "unknown").lower()

        async def resolve_one(query: str, index: int) -> ResolutionResult:
            """Resolve a single query with retry logic"""
            nonlocal completed

            if not services.resilience.allow_request(provider_name):
                logger.warning(
                    "⛔ Circuit breaker open for provider=%s; skipping query",
                    provider_name,
                )
                error_msg = "provider_circuit_open"
                services.audit.record_resolution(
                    provider_name,
                    query,
                    status="blocked",
                    source=source,
                    error=error_msg,
                )
                services.telemetry.record_error(provider_name, error_msg)
                return ResolutionResult(success=False, data=None, error=error_msg, query=query)

            async with semaphore:
                start_time: float = time.time()  # Initialize before loop
                for attempt in range(self._retry_count + 1):
                    try:
                        start_time = time.time()
                        # Resolve with timeout
                        result = await asyncio.wait_for(
                            self._resolver(query, source, emit=emit),
                            timeout=self._timeout,
                        )

                        # Success
                        completed += 1
                        if progress_callback:
                            progress_callback(completed, len(queries))

                        self._total_resolved += 1
                        logger.debug(
                            "✅ Resolved [%s/%s]: %s",
                            index + 1,
                            len(queries),
                            query[:50],
                        )
                        duration_ms = (time.time() - start_time) * 1000
                        services.telemetry.record_resolution(provider_name, duration_ms, True, source)
                        services.resilience.record_success(provider_name)
                        services.audit.record_resolution(
                            provider_name,
                            query,
                            status="success",
                            latency_ms=duration_ms,
                            source=source,
                        )

                        return ResolutionResult(success=True, data=result, query=query)

                    except TimeoutError:
                        error_msg = f"Timeout after {self._timeout}s"
                        logger.warning(
                            "⏱️ Timeout [%s/%s]: %s",
                            index + 1,
                            len(queries),
                            query[:50],
                        )

                        if attempt < self._retry_count:
                            self._total_retries += 1
                            logger.debug(
                                "🔄 Retrying [%s/%s] (attempt %s/%s)",
                                index + 1,
                                len(queries),
                                attempt + 2,
                                self._retry_count + 1,
                            )
                            await asyncio.sleep(0.5)  # Brief delay before retry
                            continue

                        duration_ms = (time.time() - start_time) * 1000 if start_time is not None else 0.0
                        completed += 1
                        if progress_callback:
                            progress_callback(completed, len(queries))

                        self._total_failed += 1
                        services.telemetry.record_resolution(provider_name, duration_ms, False, source)
                        services.telemetry.record_error(provider_name, "timeout")
                        services.resilience.record_failure(provider_name)
                        services.audit.record_resolution(
                            provider_name,
                            query,
                            status="failed",
                            latency_ms=duration_ms,
                            source=source,
                            error=error_msg,
                        )
                        return ResolutionResult(success=False, data=None, error=error_msg, query=query)

                    except Exception as e:
                        error_msg = str(e)

                        # Check if error is retryable
                        is_retryable = self._is_retryable_error(error_msg)

                        if is_retryable and attempt < self._retry_count:
                            self._total_retries += 1
                            logger.debug(
                                "🔄 Retrying [%s/%s] after error: %s",
                                index + 1,
                                len(queries),
                                error_msg[:100],
                            )
                            await asyncio.sleep(0.5)
                            continue

                        duration_ms = (time.time() - start_time) * 1000 if start_time is not None else 0.0
                        # Final failure
                        logger.warning(
                            "❌ Failed [%s/%s]: %s - %s",
                            index + 1,
                            len(queries),
                            query[:50],
                            error_msg[:100],
                        )

                        completed += 1
                        if progress_callback:
                            progress_callback(completed, len(queries))

                        self._total_failed += 1
                        services.telemetry.record_resolution(provider_name, duration_ms, False, source)
                        services.telemetry.record_error(provider_name, self._normalize_error_type(error_msg))
                        services.resilience.record_failure(provider_name)
                        services.audit.record_resolution(
                            provider_name,
                            query,
                            status="failed",
                            latency_ms=duration_ms,
                            source=source,
                            error=error_msg,
                        )
                        return ResolutionResult(success=False, data=None, error=error_msg, query=query)

                # Should never reach here
                return ResolutionResult(success=False, data=None, error="Max retries exceeded", query=query)

        # Launch all resolutions concurrently (semaphore limits actual parallelism)
        tasks = [resolve_one(query, i) for i, query in enumerate(queries)]
        results = await asyncio.gather(*tasks)

        # Log summary
        successes = sum(1 for r in results if r.success)
        failures = len(results) - successes
        logger.info(
            "✅ Batch complete: %s/%s succeeded, %s failed, %s retries",
            successes,
            len(queries),
            failures,
            self._total_retries,
        )

        return results

    async def resolve_batch_sequential(
        self,
        queries: list[str],
        source: str | None = None,
        emit: bool = False,
        provider: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[ResolutionResult]:
        """
        Resolve queries sequentially (no parallelism).
        Useful when strict ordering is required or to debug issues.

        Args:
            queries: List of queries to resolve
            source: Source type
            emit: Whether to emit state changes
            progress_callback: Optional callback(completed, total)

        Returns:
            List of ResolutionResults
        """
        services = get_services()
        provider_name = (provider or self._provider or "unknown").lower()
        results = []

        for i, query in enumerate(queries):
            start_time: float | None = None
            try:
                start_time = time.time()
                if not services.resilience.allow_request(provider_name):
                    raise RuntimeError("provider_circuit_open")
                result = await asyncio.wait_for(self._resolver(query, source, emit=emit), timeout=self._timeout)
                results.append(ResolutionResult(success=True, data=result, query=query))
                self._total_resolved += 1
                duration_ms = (time.time() - start_time) * 1000
                services.telemetry.record_resolution(provider_name, duration_ms, True, source)
                services.resilience.record_success(provider_name)
                services.audit.record_resolution(
                    provider_name,
                    query,
                    status="success",
                    latency_ms=duration_ms,
                    source=source,
                )
            except Exception as e:
                logger.warning("Failed to resolve %s: %s", query, e)
                results.append(ResolutionResult(success=False, data=None, error=str(e), query=query))
                self._total_failed += 1
                duration_ms = (time.time() - start_time) * 1000 if start_time is not None else 0.0
                services.telemetry.record_resolution(provider_name, duration_ms, False, source)
                services.telemetry.record_error(provider_name, self._normalize_error_type(str(e)))
                services.resilience.record_failure(provider_name)
                services.audit.record_resolution(
                    provider_name,
                    query,
                    status="failed",
                    latency_ms=None,
                    source=source,
                    error=str(e),
                )

            if progress_callback:
                progress_callback(i + 1, len(queries))

        return results

    def stats(self) -> dict[str, Any]:
        """Get orchestrator statistics"""
        total = self._total_resolved + self._total_failed
        success_rate = (self._total_resolved / total * 100) if total > 0 else 0

        return {
            "total_resolved": self._total_resolved,
            "total_failed": self._total_failed,
            "total_retries": self._total_retries,
            "success_rate_percent": round(success_rate, 2),
            "max_concurrent": self._max_concurrent,
        }

    @staticmethod
    def _is_retryable_error(error_msg: str) -> bool:
        """Determine if an error is worth retrying"""
        # Network/timeout errors are retryable
        retryable_keywords = [
            "timeout",
            "connection",
            "network",
            "temporary",
            "503",
            "429",  # Rate limit
            "socket",
        ]

        error_lower = error_msg.lower()
        if any(keyword in error_lower for keyword in retryable_keywords):
            return True

        # Non-retryable errors (permanent failures)
        non_retryable_keywords = [
            "private video",
            "video unavailable",
            "not found",
            "deleted",
            "copyright",
            "blocked",
        ]

        if any(keyword in error_lower for keyword in non_retryable_keywords):
            return False

        # Default: retry (conservative approach)
        return True

    @staticmethod
    def _normalize_error_type(message: str) -> str:
        cleaned = message.lower()
        if "timeout" in cleaned:
            return "timeout"
        if "circuit" in cleaned:
            return "circuit_open"
        if "403" in cleaned or "expired" in cleaned:
            return "expired"
        if "private" in cleaned or "blocked" in cleaned:
            return "restricted"
        return "unknown"
