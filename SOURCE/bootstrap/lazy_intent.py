"""
Lazy proxy for IntentBridge.

Defers both the import of backend.intent_bridge (which pulls in the entire
LLM SDK chain: torch, openai, ctranslate2) AND the IntentBridge constructor
(which initializes LLM providers and loads prompt templates) until the first
actual method call.

This removes ~3s of imports and ~1.6s of construction from the critical
startup path.  The proxy satisfies the Bindings.intent contract (typed as
Any) and transparently forwards all attribute access to the real object.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger
from diagnostics.startup_telemetry import subsystem_timer

logger = get_logger(__name__)


class LazyIntentBridge:
    """Thread-safe lazy proxy that defers IntentBridge creation until first use."""

    __slots__ = ("_factory", "_initialized_at", "_lock", "_real")

    def __init__(self, factory: Callable[[], Any]) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_initialized_at", None)
        object.__setattr__(self, "_real", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def _ensure(self) -> Any:
        real = object.__getattribute__(self, "_real")
        if real is not None:
            return real
        lock = object.__getattribute__(self, "_lock")
        with lock:
            # Double-check after acquiring lock
            real = object.__getattribute__(self, "_real")
            if real is not None:
                return real
            factory = object.__getattribute__(self, "_factory")
            logger.info("LazyIntentBridge: materializing IntentBridge on first access...")
            start = time.perf_counter()
            with subsystem_timer("llm"):
                real = factory()
            object.__setattr__(self, "_real", real)
            elapsed = time.perf_counter() - start
            object.__setattr__(self, "_initialized_at", time.time())
            logger.info("LazyIntentBridge: IntentBridge ready in %.3fs", elapsed)
            return real

    def materialize(self) -> Any:
        """Create and return the real IntentBridge if it is still lazy."""
        return self._ensure()

    def is_materialized(self) -> bool:
        """Return whether the real IntentBridge has been created."""
        return object.__getattribute__(self, "_real") is not None

    def __getattr__(self, name: str) -> Any:
        real = object.__getattribute__(self, "_ensure")()
        return getattr(real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        real = object.__getattribute__(self, "_ensure")()
        setattr(real, name, value)

    def __repr__(self) -> str:
        real = object.__getattribute__(self, "_real")
        if real is not None:
            return repr(real)
        return "<LazyIntentBridge (not yet materialized)>"

    def __bool__(self) -> bool:
        # Always truthy so `if intent:` checks pass without materializing
        return True
