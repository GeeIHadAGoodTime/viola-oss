"""
Thread-safe Singleton Pattern Utility
Standardizes singleton management across the codebase.

Replaces copy-pasted singleton implementations in:
- music/playlist_manager.py
- ui/settings_manager.py
- music/rating_system.py (implied)
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar, cast

T = TypeVar("T")


class SingletonManager:
    """
    Thread-safe singleton manager.

    Provides centralized singleton instance management with:
    - Thread safety via locks
    - Type safety via generics
    - Lazy initialization
    - Instance lifecycle management

    Usage:
        # In module with singleton:
        _instance = None

        def get_my_singleton():
            global _instance
            if _instance is None:
                _instance = SingletonManager.get_or_create(
                    'my_singleton',
                    lambda: MySingleton()
                )
            return _instance

        # Or simpler:
        from utils.singleton import singleton

        @singleton
        class MySingleton:
            ...
    """

    _instances: ClassVar[dict[str, object]] = {}
    _lock: ClassVar[threading.RLock] = threading.RLock()  # Reentrant lock for nested calls

    @classmethod
    def get_or_create(cls, key: str, factory: Callable[[], T]) -> T:
        """
        Get existing singleton instance or create new one.

        Thread-safe with double-checked locking pattern.

        Args:
            key: Unique identifier for this singleton
            factory: Callable that creates the instance

        Returns:
            Singleton instance
        """
        # Fast path: instance already exists (no lock needed)
        if key in cls._instances:
            return cast(T, cls._instances[key])

        # Slow path: need to create instance (acquire lock)
        with cls._lock:
            # Double-check: another thread may have created it
            if key not in cls._instances:
                cls._instances[key] = factory()
            return cast(T, cls._instances[key])

    @classmethod
    def get(cls, key: str) -> T | None:
        """
        Get existing singleton instance without creating.

        Args:
            key: Unique identifier for singleton

        Returns:
            Singleton instance or None if not exists
        """
        return cast(T | None, cls._instances.get(key))

    @classmethod
    def reset(cls, key: str | None = None):
        """
        Reset singleton instance(s).

        Useful for testing.

        Args:
            key: Specific singleton to reset, or None to reset all
        """
        with cls._lock:
            if key is None:
                cls._instances.clear()
            else:
                cls._instances.pop(key, None)

    @classmethod
    def exists(cls, key: str) -> bool:
        """Check if singleton instance exists."""
        return key in cls._instances


def singleton(cls: type[T]) -> type[T]:
    """
    Decorator to make a class a singleton.

    Usage:
        @singleton
        class MyClass:
            def __init__(self):
                ...

        # Later:
        instance1 = MyClass()
        instance2 = MyClass()
        assert instance1 is instance2  # Same instance

    Args:
        cls: Class to make singleton

    Returns:
        Wrapper class that returns singleton instance
    """
    key = f"{cls.__module__}.{cls.__name__}"

    base_cls: type[Any] = cast(type[Any], cls)

    class SingletonWrapper(base_cls):
        """Runtime wrapper that enforces singleton semantics."""

        def __new__(cls, *args: Any, **kwargs: Any) -> SingletonWrapper:
            def _create() -> SingletonWrapper:
                instance = cast(
                    "SingletonWrapper",
                    base_cls.__new__(cls, *args, **kwargs),
                )
                base_cls.__init__(instance, *args, **kwargs)
                return instance

            return SingletonManager.get_or_create(key, _create)

        def __repr__(self) -> str:
            return f"<Singleton {cls.__name__}>"

    SingletonWrapper.__name__ = cls.__name__
    SingletonWrapper.__module__ = cls.__module__

    return cast(type[T], SingletonWrapper)


# Helper for creating simple getter functions
def create_singleton_getter(key: str, factory: Callable[[], T]) -> Callable[[], T]:
    """
    Create a standardized singleton getter function.

    Replaces pattern:
        _instance = None
        def get_instance():
            global _instance
            if _instance is None:
                _instance = MyClass()
            return _instance

    With:
        get_instance = create_singleton_getter('my_instance', MyClass)

    Args:
        key: Unique key for singleton
        factory: Factory function to create instance

    Returns:
        Getter function
    """

    def getter() -> T:
        return SingletonManager.get_or_create(key, factory)

    return getter
