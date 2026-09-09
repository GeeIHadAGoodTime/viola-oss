"""
Plugin Loader

Load third-party plugins from filesystem.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from core.logging_config import get_logger

from .api import ThirdPartyPlugin
from .manifest import PluginManifest
from .trust import PluginTrustError

logger = get_logger(__name__)


class IncompatibleAPIError(Exception):
    """Raised when plugin API version is incompatible"""

    pass


class PluginLoader:
    """Load third-party plugins from manifest"""

    def __init__(self):
        """Initialize plugin loader"""
        self.loaded_modules = {}

    def load_plugin_class(self, plugin_path: Path, manifest: PluginManifest, *, trusted: bool) -> type:
        """
        Load plugin class from module

        Args:
            plugin_path: Path to plugin directory
            manifest: Plugin manifest
            trusted: Whether this plugin has cleared the trust gate (built-in
                location or a valid signature). REQUIRED, keyword-only, and
                fail-closed: an untrusted plugin is never ``exec_module``-ed.
                See ``plugins/trust.py`` (SEC-031/SEC-034, sweep 2026-06-09).

        Returns:
            Plugin class
        """
        # Fail-closed trust gate. Module top-level code executes *during*
        # exec_module() below — before any sandbox wraps the instance — so the
        # trust check MUST happen here, at the exec chokepoint, not only in the
        # caller. A caller that forgets to verify trust cannot reach exec.
        if not trusted:
            raise PluginTrustError(
                manifest.name,
                "Refusing to execute untrusted plugin %s (no signature trusted by "
                "this install and not a built-in plugin)." % manifest.name,
            )

        # Load the Python module
        module_path = plugin_path / manifest.entry_point

        if not module_path.exists():
            raise FileNotFoundError(f"Entry point not found: {module_path}")

        # Generate unique module name (detect builtin vs user)
        if "builtin" in str(plugin_path):
            module_name = "plugins.builtin.%s" % manifest.name
        else:
            module_name = "plugins.user.%s" % manifest.name

        # Load module
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Failed to create spec for {module_name}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # Find plugin class — try multiple naming conventions for hyphenated names
        # e.g. "system-info" → "System-InfoPlugin", "System_InfoPlugin", "SystemInfoPlugin"
        candidates = [
            manifest.name.title() + "Plugin",
            manifest.name.replace("-", "_").title() + "Plugin",
            manifest.name.replace("-", " ").title().replace(" ", "_") + "Plugin",
            manifest.name.replace("-", " ").title().replace(" ", "") + "Plugin",
        ]
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_candidates: list[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique_candidates.append(c)

        plugin_class_name = ""
        for candidate in unique_candidates:
            if hasattr(module, candidate):
                plugin_class_name = candidate
                break

        if not plugin_class_name:
            raise ImportError(
                "Plugin class not found in %s. Tried: %s" % (manifest.entry_point, ", ".join(unique_candidates))
            )

        plugin_class = getattr(module, plugin_class_name)

        # Verify it's a ThirdPartyPlugin
        if not issubclass(plugin_class, ThirdPartyPlugin):
            raise TypeError(f"{plugin_class_name} must inherit from ThirdPartyPlugin")

        logger.info("📦 Loaded plugin class: %s", plugin_class_name)
        return plugin_class
