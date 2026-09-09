"""
Core Security Plugin System

Provides unified interface for all security plugins.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any

from core.logging_config import get_logger
from fastapi import FastAPI

log = get_logger(__name__)


class SecurityPlugin(ABC):
    """Base class for all security plugins."""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self._initialized = False

    @abstractmethod
    def initialize(self, app: FastAPI) -> None:
        """Initialize plugin with FastAPI app.

        Must be implemented by concrete plugin classes.
        """
        pass  # Abstract method stub - must be implemented by subclasses

    @abstractmethod
    def cleanup(self) -> None:
        """Cleanup resources on shutdown.

        Must be implemented by concrete plugin classes.
        """
        pass  # Abstract method stub - must be implemented by subclasses

    def is_enabled(self) -> bool:
        """Check if plugin is enabled."""
        return self.enabled

    def is_initialized(self) -> bool:
        """Check if plugin has been initialized."""
        return self._initialized


class SecurityManager:
    """
    Unified security manager for all security plugins.

    Provides single interface to:
    - Register plugins
    - Enable/disable features
    - Apply security middleware
    - Get plugin instances
    """

    def __init__(self):
        self._plugins: dict[str, SecurityPlugin] = {}
        self._app: FastAPI | None = None
        self._initialized = False
        self._plugin_dependencies: dict[str, list[str]] = {}  # plugin -> [dependencies]

    def register_plugin(self, plugin: SecurityPlugin, dependencies: list[str] | None = None) -> None:
        """
        Register a security plugin with optional dependencies.

        Args:
            plugin: Security plugin to register
            dependencies: List of plugin names this plugin depends on
        """
        if not isinstance(plugin, SecurityPlugin):
            raise TypeError(f"Plugin must be instance of SecurityPlugin, got {type(plugin)}")

        if plugin.name in self._plugins:
            log.warning("Plugin '%s' already registered, replacing", plugin.name)

        self._plugins[plugin.name] = plugin
        if dependencies:
            self._plugin_dependencies[plugin.name] = dependencies

        log.info(
            "🔐 Registered security plugin: %s (enabled=%s, dependencies=%s)",
            plugin.name,
            plugin.enabled,
            dependencies or [],
        )

    def initialize(self, app: FastAPI) -> None:
        """Initialize all registered plugins in dependency order."""
        if self._initialized:
            log.warning("SecurityManager already initialized")
            return

        self._app = app

        # Sort plugins by dependencies (topological sort)
        initialized_plugins = set()
        remaining_plugins = list(self._plugins.items())

        # Initialize plugins with no dependencies first
        while remaining_plugins:
            progress = False
            for name, plugin in list(remaining_plugins):
                # Check if dependencies are satisfied
                deps = self._plugin_dependencies.get(name, [])
                if all(dep in initialized_plugins for dep in deps):
                    # All dependencies initialized, can initialize this plugin
                    if plugin.is_enabled():
                        try:
                            plugin.initialize(app)
                            plugin._initialized = True
                            log.info("✅ Initialized security plugin: %s", name)
                            progress = True
                        except Exception as e:
                            log.exception(
                                "❌ Failed to initialize plugin '%s': %s",
                                name,
                                e,
                            )
                    else:
                        log.debug("⏭️ Skipping disabled plugin: %s", name)

                    initialized_plugins.add(name)
                    remaining_plugins.remove((name, plugin))
                    progress = True

            if not progress and remaining_plugins:
                # Circular dependency or missing dependency
                remaining_names = [name for name, _ in remaining_plugins]
                log.error(
                    "❌ Circular dependency or missing dependencies detected: %s",
                    remaining_names,
                )
                # Initialize remaining plugins anyway (might work)
                for name, plugin in remaining_plugins:
                    if plugin.is_enabled():
                        try:
                            plugin.initialize(app)
                            plugin._initialized = True
                            log.warning(
                                "⚠️ Initialized plugin '%s' despite dependency issues",
                                name,
                            )
                        except Exception as e:
                            log.exception(
                                "❌ Failed to initialize plugin '%s': %s",
                                name,
                                e,
                            )
                break

        self._initialized = True
        log.info("🔐 SecurityManager initialized with %s plugins", len(self._plugins))

    def cleanup(self) -> None:
        """Cleanup all plugins."""
        for name, plugin in self._plugins.items():
            if plugin.is_initialized():
                try:
                    plugin.cleanup()
                    log.info("🧹 Cleaned up plugin: %s", name)
                except Exception as e:
                    log.error("❌ Failed to cleanup plugin '%s': %s", name, e)

    def get_plugin(self, name: str) -> SecurityPlugin | None:
        """Get plugin by name."""
        return self._plugins.get(name)

    def is_plugin_enabled(self, name: str) -> bool:
        """Check if plugin is enabled."""
        plugin = self._plugins.get(name)
        return plugin.is_enabled() if plugin else False

    def list_plugins(self) -> list[str]:
        """List all registered plugin names."""
        return list(self._plugins.keys())

    def get_status(self) -> dict[str, Any]:
        """Get status of all plugins."""
        return {
            name: {
                "enabled": plugin.is_enabled(),
                "initialized": plugin.is_initialized(),
            }
            for name, plugin in self._plugins.items()
        }


# Global security manager instance (thread-safe)
_security_manager: SecurityManager | None = None
_security_manager_lock = threading.Lock()


def get_security_manager() -> SecurityManager:
    """Get global security manager instance (thread-safe)."""
    global _security_manager
    if _security_manager is None:
        with _security_manager_lock:
            # Double-check pattern
            if _security_manager is None:
                _security_manager = SecurityManager()
    return _security_manager
