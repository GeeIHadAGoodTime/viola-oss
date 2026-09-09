"""Lazy proxy for TTS engine — defers 2.5s import until first use."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class LazyTTSEngine:
    """Thread-safe lazy proxy that defers TTS engine creation until first method call.

    The real TTS engine is created on first attribute access (speak, is_available, etc.).
    This saves ~2.5s of cold-start time (Kokoro/pyttsx3 import chain).
    """

    _PROXY_METHODS = frozenset({"say", "speak", "is_available", "stop", "cleanup", "shutdown"})
    __slots__ = ("_factory", "_instance", "_lock")

    def __init__(self, factory: Callable[[], Any]) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_instance", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def _materialize(self) -> Any:
        instance = object.__getattribute__(self, "_instance")
        if instance is not None:
            return instance
        lock = object.__getattribute__(self, "_lock")
        with lock:
            # Double-check after acquiring lock
            instance = object.__getattribute__(self, "_instance")
            if instance is not None:
                return instance
            factory = object.__getattribute__(self, "_factory")
            logger.info("LazyTTSEngine: materializing TTS engine on first access...")
            instance = factory()
            object.__setattr__(self, "_instance", instance)
            logger.info("LazyTTSEngine: TTS engine ready")
            return instance

    def __getattr__(self, name: str) -> Any:
        instance = object.__getattribute__(self, "_instance")
        if instance is None and name in self._PROXY_METHODS:

            def _deferred(*a: Any, **kw: Any) -> Any:
                return getattr(self._materialize(), name)(*a, **kw)

            return _deferred
        return getattr(self._materialize(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        real = self._materialize()
        setattr(real, name, value)

    def __repr__(self) -> str:
        instance = object.__getattribute__(self, "_instance")
        if instance is None:
            return "<LazyTTSEngine (not yet materialized)>"
        return repr(instance)

    def __bool__(self) -> bool:
        # Always truthy so `if tts:` checks pass without materializing
        return True
