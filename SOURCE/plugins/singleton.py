"""Plugin Manager Singleton

Provides a global plugin manager instance with lazy initialization.
"""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)

_instance = None


def get_plugin_manager():
    """Get or create the global PluginManager instance.

    On first call, discovers and loads all plugins from builtin/ and user/ dirs.
    """
    global _instance
    if _instance is None:
        from .manager import PluginManager

        _instance = PluginManager()
        results = _instance.discover_and_load_all()
        loaded = sum(1 for v in results.values() if v == "loaded")
        failed = sum(1 for v in results.values() if v not in ("loaded", "already_loaded"))
        logger.info(
            "Plugin system initialized: %d loaded, %d failed",
            loaded,
            failed,
        )
    return _instance


def reset_plugin_manager() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None
