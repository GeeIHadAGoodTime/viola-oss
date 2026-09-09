"""
Third-Party Plugin System for Viola

Sandboxed plugin architecture for user-created and community plugins.
Separate from bootstrap internal enhancement system.
"""

from __future__ import annotations

from .api import (
    Capability,
    PluginContext,
    PluginResponse,
    ThirdPartyPlugin,
)
from .loader import PluginLoader
from .manager import PluginManager
from .manifest import PluginManifest, PluginManifestValidationError
from .singleton import get_plugin_manager

__all__ = [
    "Capability",
    "PluginContext",
    "PluginLoader",
    "PluginManager",
    "PluginManifest",
    "PluginManifestValidationError",
    "PluginResponse",
    "ThirdPartyPlugin",
    "get_plugin_manager",
]
