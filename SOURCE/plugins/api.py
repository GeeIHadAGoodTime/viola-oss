"""
Third-Party Plugin API

Base API for user-created and community plugins.
Plugins are sandboxed and isolated from core functionality.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class Capability:
    """Plugin capability (what it can do)"""

    name: str
    description: str
    permissions: list[str]  # ["files", "network", "audio", "settings", "system"]


@dataclass
class PluginResponse:
    """Structured response from a plugin handler."""

    speech: str  # TTS output
    display: dict[str, Any] | None = None  # Optional UI payload
    state_updates: list[dict[str, Any]] | None = None  # StateHub commands
    error: str | None = None  # Error (triggers error handling)


class PluginContext:
    """Context provided to plugins with capabilities"""

    def __init__(
        self,
        manager: Any,
        granted_permissions: list[str],
        tts_callback: Callable[[str], None] | None = None,
    ):
        self.manager = manager
        self.granted_permissions = granted_permissions
        self.tts_callback = tts_callback

    def has_permission(self, permission: str) -> bool:
        """Check if plugin has permission"""
        return permission in self.granted_permissions


class ThirdPartyPlugin(ABC):
    """
    Base class for all third-party plugins

    Lifecycle:
    1. on_init() - Plugin loaded, can setup resources
    2. on_start() - Plugin activated, can begin operation
    3. on_stop() - Plugin deactivated, should cleanup
    4. on_cleanup() - Plugin unloaded, final cleanup
    """

    # Plugin metadata (override in subclass)
    name: str = "unnamed_plugin"
    version: str = "1.0.0"
    api_version: str = "1.0"  # Plugin API version
    description: str = "No description"
    author: str = "Unknown"

    # Permissions required (override in subclass)
    required_permissions: list[str] = []

    def __init__(self, context: PluginContext):
        """
        Initialize plugin

        Args:
            context: Plugin context with access to capabilities
        """
        self.context = context
        logger.info("Plugin initialized: %s", self.name)

    @abstractmethod
    def get_capabilities(self) -> list[Capability]:
        """Return list of capabilities this plugin provides"""
        pass

    def get_config_schema(self) -> dict[str, Any] | None:
        """Return JSON Schema for plugin configuration, or None if no config needed."""
        return None

    def get_api_routes(self) -> list[tuple[str, str, Callable[..., Any]]] | None:
        """Return list of (method, path, handler) tuples for REST API routes.

        Routes are mounted under /v1/plugins/{plugin_name}/...
        """
        return None

    def on_init(self) -> None:
        """Called when plugin is initialized"""
        pass

    def on_start(self) -> None:
        """Called when plugin is started"""
        pass

    def on_stop(self) -> None:
        """Called when plugin is stopped"""
        pass

    def on_cleanup(self) -> None:
        """Called when plugin is unloaded"""
        pass
