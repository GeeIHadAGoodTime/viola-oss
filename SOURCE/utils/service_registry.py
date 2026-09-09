"""
Service Registry
================
Unified dependency injection and service locator for NOVVIOLA.

Key Features:
- Centralized service registration and lookup
- Lazy initialization with factory functions
- Singleton and transient service lifetimes
- Service health checking and monitoring
- Clean shutdown coordination

Part of the unified/modular architecture for NOVVIOLA.
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar, cast

from core.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


# ---------- Service Lifetime ----------


class ServiceLifetime(Enum):
    """Service lifetime management strategies."""

    SINGLETON = "singleton"  # One instance shared globally
    TRANSIENT = "transient"  # New instance every time
    SCOPED = "scoped"  # One instance per scope (future feature)


# ---------- Service Descriptor ----------


@dataclass
class ServiceDescriptor(Generic[T]):
    """Descriptor for a registered service."""

    service_type: type[T]
    factory: Callable[[], T]
    lifetime: ServiceLifetime
    instance: T | None = None
    health_check: Callable[[], bool] | None = None


# ---------- Service Health Protocol ----------


class HealthCheckable(Protocol):
    """Protocol for services that support health checking."""

    def is_healthy(self) -> bool:
        """Check if service is healthy."""
        ...


class Shutdownable(Protocol):
    """Protocol for services that support graceful shutdown."""

    def shutdown(self) -> None:
        """Shutdown the service gracefully."""
        ...


# ---------- Service Registry ----------


class ServiceRegistry:
    """
    Centralized service registry for dependency injection.

    Usage:
        # Register services
        registry = ServiceRegistry()
        registry.register(BackgroundResolver, lambda: BackgroundResolver(), ServiceLifetime.SINGLETON)

        # Get services
        resolver = registry.get(BackgroundResolver)

        # Shutdown all services
        registry.shutdown_all()
    """

    def __init__(self, auto_shutdown: bool = True):
        """
        Initialize service registry.

        Args:
            auto_shutdown: Automatically shutdown services on program exit
        """
        self._services: dict[type[Any], ServiceDescriptor[Any]] = {}
        self._shutdown_hooks: list[Callable[[], None]] = []

        if auto_shutdown:
            atexit.register(self.shutdown_all)

        logger.info("🔧 ServiceRegistry initialized")

    def register(
        self,
        service_type: type[T],
        factory: Callable[[], T],
        lifetime: ServiceLifetime = ServiceLifetime.SINGLETON,
        health_check: Callable[[], bool] | None = None,
    ) -> None:
        """
        Register a service with the registry.

        Args:
            service_type: Type/class of the service
            factory: Factory function to create instances
            lifetime: Service lifetime (singleton, transient, scoped)
            health_check: Optional health check function
        """
        if service_type in self._services:
            logger.warning("⚠️ Service %s already registered, replacing", service_type.__name__)

        descriptor: ServiceDescriptor[T] = ServiceDescriptor(
            service_type=service_type,
            factory=factory,
            lifetime=lifetime,
            health_check=health_check,
        )

        self._services[service_type] = cast(ServiceDescriptor[Any], descriptor)
        logger.debug("✓ Registered %s (%s)", service_type.__name__, lifetime.value)

    def register_instance(self, service_type: type[T], instance: T) -> None:
        """
        Register an existing service instance (always singleton).

        Args:
            service_type: Type/class of the service
            instance: Pre-created instance
        """
        descriptor: ServiceDescriptor[T] = ServiceDescriptor(
            service_type=service_type,
            factory=lambda: instance,
            lifetime=ServiceLifetime.SINGLETON,
            instance=instance,
        )

        self._services[service_type] = cast(ServiceDescriptor[Any], descriptor)
        logger.debug("✓ Registered instance %s", service_type.__name__)

    def get(self, service_type: type[T], required: bool = True) -> T | None:
        """
        Get a service instance from the registry.

        Args:
            service_type: Type/class of the service to retrieve
            required: If True, raise error if service not found

        Returns:
            Service instance or None if not found and not required

        Raises:
            KeyError: If service not found and required=True
        """
        if service_type not in self._services:
            if required:
                raise KeyError(f"Service {service_type.__name__} not registered")
            return None

        descriptor = cast(ServiceDescriptor[T], self._services[service_type])

        # Singleton: return cached instance or create and cache
        if descriptor.lifetime == ServiceLifetime.SINGLETON:
            if descriptor.instance is None:
                logger.debug("Creating singleton instance of %s", service_type.__name__)
                descriptor.instance = descriptor.factory()
            return descriptor.instance

        # Transient: always create new instance
        elif descriptor.lifetime == ServiceLifetime.TRANSIENT:
            logger.debug("Creating transient instance of %s", service_type.__name__)
            return descriptor.factory()

        # Scoped: FUTURE FEATURE - not yet implemented
        # INTENTIONAL STUB: This is a placeholder for planned functionality.
        # SCOPED lifetime (one instance per scope/request) is planned but not implemented yet.
        # This raises a clear error if accessed before implementation to prevent silent failures.
        else:
            raise NotImplementedError(
                f"Service lifetime {descriptor.lifetime} not yet implemented. "
                "Currently supported: SINGLETON, TRANSIENT. "
                "SCOPED lifetime is planned for future release."
            )

    def has(self, service_type: type) -> bool:
        """Check if a service is registered."""
        return service_type in self._services

    def check_health(self, service_type: type | None = None) -> dict[str, bool]:
        """
        Check health of registered services.

        Args:
            service_type: Check specific service, or None for all

        Returns:
            Dict mapping service names to health status
        """
        results = {}

        services_to_check: dict[type[Any], ServiceDescriptor[Any]] = (
            {service_type: self._services[service_type]} if service_type else self._services
        )

        for svc_type, descriptor in services_to_check.items():
            name = svc_type.__name__

            # Use custom health check if provided
            if descriptor.health_check:
                try:
                    results[name] = descriptor.health_check()
                except Exception as e:
                    logger.warning("Health check failed for %s: %s", name, e)
                    results[name] = False

            # Use protocol health check if available
            elif descriptor.instance and hasattr(descriptor.instance, "is_healthy"):
                try:
                    results[name] = descriptor.instance.is_healthy()
                except Exception as e:
                    logger.warning("Health check failed for %s: %s", name, e)
                    results[name] = False

            # Default: healthy if instance exists
            else:
                results[name] = descriptor.instance is not None

        return results

    def shutdown_all(self) -> None:
        """Shutdown all services gracefully."""
        logger.info("🛑 Shutting down all services...")

        # Call custom shutdown hooks first
        for hook in reversed(self._shutdown_hooks):
            try:
                hook()
            except Exception as e:
                logger.warning("Shutdown hook failed: %s", e)

        # Shutdown singleton instances
        for service_type, descriptor in self._services.items():
            if descriptor.instance is None:
                continue

            name = service_type.__name__

            # Try Shutdownable protocol
            if hasattr(descriptor.instance, "shutdown"):
                try:
                    logger.debug("Shutting down %s...", name)
                    descriptor.instance.shutdown()
                except Exception as e:
                    logger.warning("Failed to shutdown %s: %s", name, e)

            # Clear instance
            descriptor.instance = None

        logger.info("✅ All services shut down")

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        """Add a custom shutdown hook (called before service shutdown)."""
        self._shutdown_hooks.append(hook)

    def get_service_info(self) -> dict[str, dict[str, Any]]:
        """Get information about all registered services."""
        return {
            svc_type.__name__: {
                "lifetime": descriptor.lifetime.value,
                "instantiated": descriptor.instance is not None,
                "has_health_check": descriptor.health_check is not None
                or (descriptor.instance and hasattr(descriptor.instance, "is_healthy")),
            }
            for svc_type, descriptor in self._services.items()
        }


# ---------- Global registry instance ----------

_global_registry: ServiceRegistry | None = None


def get_service_registry() -> ServiceRegistry:
    """
    Get the global service registry instance.

    This is the primary way to access services in NOVVIOLA.
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = ServiceRegistry(auto_shutdown=True)
    return _global_registry


def reset_service_registry() -> None:
    """Reset the global service registry (primarily for testing)."""
    global _global_registry
    if _global_registry:
        _global_registry.shutdown_all()
    _global_registry = None
