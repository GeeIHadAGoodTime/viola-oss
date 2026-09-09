"""Utilities for async/sync interop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def wait_for_condition(
    predicate: Callable[[], bool],
    timeout: float = 5.0,
    *,
    poll_interval: float = 0.2,
) -> bool:
    """
    Block until predicate returns True or timeout expires.

    This is a synchronous polling utility for waiting on conditions
    in non-async code. For async code, use asyncio.wait_for() instead.

    Args:
        predicate: Function that returns True when condition is met
        timeout: Maximum time to wait in seconds
        poll_interval: How often to check the predicate (default 0.2s)

    Returns:
        True if condition was met, False if timeout expired

    Example:
        # Wait up to 5 seconds for a flag to be set
        result = wait_for_condition(lambda: some_flag, timeout=5.0)
        if not result:
            raise TimeoutError("Flag not set in time")
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        if predicate():
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            return predicate()  # Final check
        time.sleep(min(poll_interval, remaining))


async def run_sync_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Run a synchronous function in a thread pool.

    This is a thin wrapper around asyncio.to_thread() that provides:
    - Consistent naming
    - Type hints
    - Documentation

    Args:
        func: Synchronous function to run
        *args: Positional arguments to pass to func
        **kwargs: Keyword arguments to pass to func

    Returns:
        Result from func

    Example:
        result = await run_sync_in_thread(sync_function, arg1, arg2, key=value)
    """
    return await asyncio.to_thread(func, *args, **kwargs)


async def run_sync_with_timeout(
    func: Callable[..., T],
    timeout: float,
    *args: Any,
    **kwargs: Any,
) -> T:
    """
    Run a synchronous function in a thread pool with a timeout.

    Args:
        func: Synchronous function to run
        timeout: Timeout in seconds
        *args: Positional arguments to pass to func
        **kwargs: Keyword arguments to pass to func

    Returns:
        Result from func

    Raises:
        asyncio.TimeoutError: If function doesn't complete within timeout

    Example:
        result = await run_sync_with_timeout(sync_function, 5.0, arg1, arg2)
    """
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=timeout,
    )
