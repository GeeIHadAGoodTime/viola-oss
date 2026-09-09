"""
Resilient HTTP client with automatic retry and exponential backoff.

Designed for reliability in poor network conditions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum
from functools import wraps
from typing import ParamSpec, TypeVar

import httpx

from core.constants import RETRY_COUNT_DEFAULT, TIMEOUT_VERY_LONG
from core.logging_config import get_logger

logger = get_logger(__name__)

_TENACITY_AVAILABLE = False

P = ParamSpec("P")
R = TypeVar("R")


class RetryStrategy(Enum):
    """Retry strategy types"""

    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    FIXED = "fixed"


class ResilientClient:
    """
    HTTP client with automatic retry and exponential backoff.

    Features:
    - Exponential backoff (1s, 2s, 4s, 8s, 10s max)
    - Configurable retry attempts
    - Network quality detection
    - Graceful degradation
    """

    def __init__(
        self,
        max_retries: int = RETRY_COUNT_DEFAULT,
        initial_delay: float = 1.0,
        max_delay: float = 10.0,
        timeout: float = TIMEOUT_VERY_LONG,
        strategy: RetryStrategy = RetryStrategy.EXPONENTIAL,
    ):
        """
        Initialize resilient client.

        Args:
            max_retries: Maximum retry attempts
            initial_delay: Initial delay in seconds
            max_delay: Maximum delay in seconds
            timeout: Request timeout in seconds
            strategy: Retry strategy
        """
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.strategy = strategy
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        """Async context manager entry"""
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self._client:
            await self._client.aclose()

    def _get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt"""
        if self.strategy == RetryStrategy.EXPONENTIAL:
            delay: float = self.initial_delay * (2**attempt)
        elif self.strategy == RetryStrategy.LINEAR:
            delay = self.initial_delay * float(attempt + 1)
        else:  # FIXED
            delay = self.initial_delay

        return min(delay, self.max_delay)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        """
        GET request with retry logic.

        Args:
            url: URL to fetch
            **kwargs: Additional httpx arguments

        Returns:
            HTTP response

        Raises:
            httpx.HTTPError: If all retries fail
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async context manager.")

        last_exception: httpx.HTTPError | httpx.TimeoutException | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                last_exception = e

                if attempt >= self.max_retries:
                    logger.warning("All retries failed for %s after %s attempts", url, attempt + 1)
                    break

                delay = self._get_delay(attempt)
                logger.debug(
                    "Request failed (attempt %s/%s), retrying in %ss: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                    str(e)[:100],
                )
                await asyncio.sleep(delay)

        # All retries failed - raise the last exception
        # Loop always runs at least once (max_retries >= 0), so last_exception is set
        assert last_exception is not None, "Loop invariant: at least one attempt made"
        raise last_exception

    async def post(self, url: str, **kwargs) -> httpx.Response:
        """
        POST request with retry logic.

        Args:
            url: URL to post to
            **kwargs: Additional httpx arguments

        Returns:
            HTTP response
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async context manager.")

        last_exception: httpx.HTTPError | httpx.TimeoutException | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                last_exception = e

                if attempt >= self.max_retries:
                    logger.warning("All retries failed for %s after %s attempts", url, attempt + 1)
                    break

                delay = self._get_delay(attempt)
                logger.debug(
                    "Request failed (attempt %s/%s), retrying in %ss: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                    str(e)[:100],
                )
                await asyncio.sleep(delay)

        # All retries failed - raise the last exception
        # Loop always runs at least once (max_retries >= 0), so last_exception is set
        assert last_exception is not None, "Loop invariant: at least one attempt made"
        raise last_exception


async def _resilient_fetch_impl(
    url: str,
    *,
    timeout: float = TIMEOUT_VERY_LONG,
    params: httpx.QueryParams | dict[str, str | int | float | bool | None] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> httpx.Response:
    """
    Fetch URL with auto-retry and exponential backoff.

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds
        params: Query parameters
        headers: HTTP headers
        cookies: Cookies to send
        follow_redirects: Whether to follow redirects

    Returns:
        HTTP response

    Example:
        async with resilient_fetch("https://api.example.com/data") as response:
            data = response.json()
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            follow_redirects=follow_redirects,
        )
        response.raise_for_status()
        return response


# Decorator-based API for simple use cases.
# Tenacity is optional, so define `resilient_fetch` in one place to avoid
# reassigning imported types when the dependency is missing.
resilient_fetch = _resilient_fetch_impl
try:  # pragma: no cover - optional dependency
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )
except ImportError:  # pragma: no cover
    _TENACITY_AVAILABLE = False
else:
    _TENACITY_AVAILABLE = True
    resilient_fetch = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        reraise=True,
    )(_resilient_fetch_impl)
