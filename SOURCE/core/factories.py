"""
Generic factory infrastructure for NOVVIOLA.

This module provides reusable factory patterns to eliminate duplication across
provider registries, engine managers, and backend loaders.

Patterns provided:
- Registry[T]: Thread-safe registry for managing named instances
- ClassRegistry[T]: Registry specialized for class types with instantiation
- ConfigurableFactory[ConfigT, T]: Abstract factory creating products from config
- LazyClassLoader[T]: Deferred class loader to avoid circular imports
- decorator_register(): Decorator for auto-registration
"""

from __future__ import annotations

import importlib
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from core.logging_config import get_logger

if TYPE_CHECKING:
    pass

T = TypeVar("T")
ConfigT = TypeVar("ConfigT")


class RegistryKeyError(KeyError):
    """Raised when a registry lookup fails."""

    def __init__(self, key: str, registry_name: str) -> None:
        self.key = key
        self.registry_name = registry_name
        super().__init__(f"'{key}' not found in {registry_name} registry")


class Registry(Generic[T]):
    """
    Generic thread-safe registry for managing named instances.

    This provides a consistent pattern for registering and retrieving items
    by string key, with proper thread-safety and error handling.

    Example:
        >>> registry: Registry[SomeType] = Registry("my_registry")
        >>> registry.register("key1", instance1)
        >>> registry.get("key1")
        instance1
    """

    def __init__(self, name: str = "registry") -> None:
        """
        Initialize the registry.

        Args:
            name: Human-readable name for error messages and logging
        """
        self._name = name
        self._items: dict[str, T] = {}
        self._lock = threading.Lock()
        self._logger = get_logger(f"viola.registry.{name}")

    @property
    def name(self) -> str:
        """Return the registry name."""
        return self._name

    def register(self, key: str, item: T, *, override: bool = False) -> None:
        """
        Register an item with the given key.

        Args:
            key: Unique identifier for the item
            item: The item to register
            override: If True, replace existing registration; otherwise raise error

        Raises:
            ValueError: If key already exists and override is False
        """
        with self._lock:
            if not override and key in self._items:
                raise ValueError(f"'{key}' already registered in {self._name}. " "Pass override=True to replace.")
            self._items[key] = item
            self._logger.debug("Registered '%s'", key)

    def unregister(self, key: str) -> T | None:
        """
        Remove and return an item from the registry.

        Args:
            key: The key to remove

        Returns:
            The removed item, or None if not found
        """
        with self._lock:
            item = self._items.pop(key, None)
            if item is not None:
                self._logger.debug("Unregistered '%s'", key)
            return item

    def get(self, key: str) -> T | None:
        """
        Get an item by key.

        Args:
            key: The key to look up

        Returns:
            The item if found, None otherwise
        """
        with self._lock:
            return self._items.get(key)

    def get_or_raise(self, key: str) -> T:
        """
        Get an item by key, raising an error if not found.

        Args:
            key: The key to look up

        Returns:
            The registered item

        Raises:
            RegistryKeyError: If key is not registered
        """
        with self._lock:
            try:
                return self._items[key]
            except KeyError as exc:
                raise RegistryKeyError(key, self._name) from exc

    def keys(self) -> list[str]:
        """Return a list of all registered keys."""
        with self._lock:
            return list(self._items.keys())

    def values(self) -> list[T]:
        """Return a list of all registered items."""
        with self._lock:
            return list(self._items.values())

    def items(self) -> list[tuple[str, T]]:
        """Return a list of (key, item) pairs."""
        with self._lock:
            return list(self._items.items())

    def clear(self) -> None:
        """Remove all items from the registry."""
        with self._lock:
            self._items.clear()
            self._logger.debug("Cleared registry")

    def __len__(self) -> int:
        """Return the number of registered items."""
        with self._lock:
            return len(self._items)

    def __contains__(self, key: str) -> bool:
        """Check if a key is registered."""
        with self._lock:
            return key in self._items

    def __iter__(self) -> Iterator[str]:
        """Iterate over registered keys."""
        with self._lock:
            return iter(list(self._items.keys()))


class ClassRegistry(Registry[type[T]]):
    """
    Registry specialized for class types with instantiation support.

    Extends Registry to provide a create() method that instantiates
    registered classes with provided arguments.

    Example:
        >>> registry: ClassRegistry[BaseEngine] = ClassRegistry("engines")
        >>> registry.register("youtube", YouTubeEngine)
        >>> engine = registry.create("youtube", controller=my_controller)
    """

    def create(self, key: str, *args: Any, **kwargs: Any) -> T:
        """
        Create an instance of the registered class.

        Args:
            key: The key of the class to instantiate
            *args: Positional arguments to pass to the constructor
            **kwargs: Keyword arguments to pass to the constructor

        Returns:
            A new instance of the registered class

        Raises:
            RegistryKeyError: If key is not registered
        """
        cls = self.get_or_raise(key)
        return cls(*args, **kwargs)


class ConfigurableFactory(ABC, Generic[ConfigT, T]):
    """
    Abstract factory that creates products from configuration objects.

    Subclasses implement create() to produce instances based on config.

    Example:
        >>> class LLMFactory(ConfigurableFactory[LLMConfig, BaseLLMProvider]):
        ...     def create(self, config: LLMConfig) -> BaseLLMProvider:
        ...         return self._providers[config.provider_type](config)
    """

    @abstractmethod
    def create(self, config: ConfigT) -> T:
        """
        Create a product from the given configuration.

        Args:
            config: Configuration object describing what to create

        Returns:
            The created product instance
        """
        ...

    def create_or_none(self, config: ConfigT) -> T | None:
        """
        Create a product, returning None on failure instead of raising.

        Args:
            config: Configuration object

        Returns:
            The created product, or None if creation failed
        """
        try:
            return self.create(config)
        except Exception:
            return None


class LazyClassLoader(Generic[T]):
    """
    Deferred class loader to avoid circular imports.

    Delays importing a class until it's actually needed, which is useful
    when dealing with circular dependency situations.

    Example:
        >>> loader = LazyClassLoader[AudioPlayer](
        ...     "audio_core.player", "AudioPlayer"
        ... )
        >>> player_class = loader.get_class()  # Import happens here
        >>> player = loader.create(config=my_config)
    """

    def __init__(
        self,
        module_path: str,
        class_name: str,
        *,
        fallback: type[T] | None = None,
    ) -> None:
        """
        Initialize the lazy loader.

        Args:
            module_path: Full dotted path to the module
            class_name: Name of the class within the module
            fallback: Optional fallback class to use if import fails
        """
        self._module_path = module_path
        self._class_name = class_name
        self._fallback = fallback
        self._loaded_class: type[T] | None = None
        self._lock = threading.Lock()

    def get_class(self) -> type[T]:
        """
        Get the class, importing it if necessary.

        Returns:
            The loaded class

        Raises:
            ImportError: If the class cannot be imported and no fallback exists
        """
        with self._lock:
            if self._loaded_class is not None:
                return self._loaded_class

            try:
                module = importlib.import_module(self._module_path)
                self._loaded_class = getattr(module, self._class_name)
            except (ImportError, AttributeError) as exc:
                if self._fallback is not None:
                    self._loaded_class = self._fallback
                else:
                    raise ImportError(f"Cannot import {self._class_name} from {self._module_path}") from exc

            return self._loaded_class

    def create(self, *args: Any, **kwargs: Any) -> T:
        """
        Create an instance of the loaded class.

        Args:
            *args: Positional arguments for the constructor
            **kwargs: Keyword arguments for the constructor

        Returns:
            A new instance of the class
        """
        cls = self.get_class()
        return cls(*args, **kwargs)


def decorator_register(
    registry: Registry[T],
    key: str | None = None,
    *,
    override: bool = False,
):
    """
    Decorator factory for auto-registering items at import time.

    Args:
        registry: The registry to register with
        key: The key to use (defaults to class __name__ or function __name__)
        override: Whether to override existing registrations

    Returns:
        A decorator that registers the decorated item

    Example:
        >>> engine_registry: Registry[type[BaseEngine]] = Registry("engines")
        >>>
        >>> @decorator_register(engine_registry, "youtube")
        ... class YouTubeEngine(BaseEngine):
        ...     pass
    """

    def decorator(item: T) -> T:
        registration_key = key
        if registration_key is None:
            # Try to get a name from the item
            registration_key = getattr(item, "__name__", None)
            if registration_key is None:
                raise ValueError("Cannot determine registration key. Provide explicit key parameter.")
        registry.register(registration_key, item, override=override)
        return item

    return decorator


__all__ = [
    "ClassRegistry",
    "ConfigurableFactory",
    "LazyClassLoader",
    "Registry",
    "RegistryKeyError",
    "decorator_register",
]
