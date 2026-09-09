"""
Plugin Manager

Central manager for third-party plugins.
Handles loading, unloading, lifecycle, discovery, install, remove, and reload.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.constants import PLUGIN_BUILTIN_DIR, PLUGIN_USER_DIR
from core.logging_config import get_logger

from .api import PluginContext, ThirdPartyPlugin
from .config_manager import PluginConfigManager
from .errors import (
    DEPENDENCY_UNSATISFIED,
    LOAD_ERROR,
    PERMISSION_DENIED,
    UNSUPPORTED_COMPONENT,
    PluginError,
)
from .loader import IncompatibleAPIError, PluginLoader
from .manifest import PluginManifest
from .permissions import Permission, PermissionDeniedError, PermissionManager
from .sandbox import PluginSandbox

# F-020: Plugin components that Claude manifests can declare. Each component
# type may be present without an `entry_point` — those plugins load as
# component-only records and register into their respective subsystems.
CLAUDE_COMPONENT_FIELDS: tuple[str, ...] = (
    "hooks",
    "commands",
    "agents",
    "skills",
    "output_styles",
    "channels",
    "mcp_servers",
    "lsp_servers",
)


@dataclass
class ComponentRegistration:
    """Single Claude-style component registered from a manifest."""

    kind: str  # "hooks" | "commands" | "agents" | "skills" | ...
    name: str
    payload: Any
    plugin_name: str


@dataclass
class LoadedPlugin:
    """A loaded plugin record.

    Wraps either a Python-runtime `ThirdPartyPlugin` instance or a
    component-only Claude-style plugin (no Python entry point). Callers
    that need the Python plugin object access ``.runtime``; component
    consumers iterate ``.components``.
    """

    manifest: PluginManifest
    plugin_path: Path
    runtime: ThirdPartyPlugin | None = None
    components: list[ComponentRegistration] = field(default_factory=list)
    unsupported_components: list[PluginError] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str | None:
        return self.manifest.version

    @property
    def is_runtime(self) -> bool:
        return self.runtime is not None


logger = get_logger(__name__)


class PluginDisabledError(RuntimeError):
    """Raised when a disabled plugin is asked to load or reload."""


class PluginManager:
    """Manage third-party plugin loading, unloading, lifecycle"""

    def __init__(
        self,
        user_dir: Path | None = None,
        builtin_dir: Path | None = None,
        tts_callback: Callable[[str], None] | None = None,
    ):
        if user_dir is None:
            user_dir = Path(PLUGIN_USER_DIR)
        if builtin_dir is None:
            builtin_dir = Path(PLUGIN_BUILTIN_DIR)

        self.user_dir = user_dir
        self.builtin_dir = builtin_dir
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.builtin_dir.mkdir(parents=True, exist_ok=True)
        self._tts_callback = tts_callback

        # Legacy compat: keep plugin_dir pointing to user_dir
        self.plugin_dir = self.user_dir

        self.loaded_plugins: dict[str, ThirdPartyPlugin] = {}
        # F-020: Component-only and runtime plugins both live here. The
        # legacy ``loaded_plugins`` dict above keeps only runtime plugins
        # for backward compat with callers that expect a ThirdPartyPlugin.
        self.loaded_records: dict[str, LoadedPlugin] = {}
        self.plugin_sandboxes: dict[str, PluginSandbox] = {}
        self.permission_manager = PermissionManager()
        self.loader = PluginLoader()
        self.config_manager = PluginConfigManager([self.builtin_dir, self.user_dir])
        # F-022: per-plugin install-time options. Non-sensitive values
        # live alongside the YAML config; sensitive values are kept in
        # the desktop-only secret store and substituted into MCP/LSP
        # configs at access time.
        from .options_storage import PluginOptionsStorage

        self.options_storage = PluginOptionsStorage([self.builtin_dir, self.user_dir])
        # F-023: marketplace install records. Mirrors Claude's
        # version-2 installed-plugin file, keyed by plugin id and scope.
        from .install_records import PluginInstallRegistry

        self.install_registry = PluginInstallRegistry()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_plugins(self) -> list[Path]:
        """Discover plugins in both builtin and user directories."""
        plugins: list[Path] = []
        for search_dir in [self.builtin_dir, self.user_dir]:
            if not search_dir.exists():
                continue
            for item in search_dir.iterdir():
                if item.is_dir():
                    manifest_path = item / "plugin.json"
                    if manifest_path.exists():
                        plugins.append(item)
        return plugins

    def discover_and_load_all(self) -> dict[str, str]:
        """Discover and load all plugins from builtin and user dirs.

        Returns dict mapping plugin_name -> status ("loaded" or error string).

        Each value is a string for backward compat. The structured tagged
        errors (F-056) are available via :meth:`discover_and_load_all_tagged`.
        """
        return {name: self._status_str(status) for name, status in self.discover_and_load_all_tagged().items()}

    def discover_and_load_all_tagged(self) -> dict[str, str | PluginError]:
        """F-056: tagged discovery results.

        Returns dict mapping plugin_name -> "loaded" | "already_loaded" |
        PluginError. ``PluginError`` carries kind, message, plugin name,
        and per-kind details.
        """
        # F-055: build a dependency-ordered list before materializing.
        manifests = self._scan_manifests()
        ordered_names, depend_errors = self._resolve_dependency_name_order(manifests)
        results: dict[str, str | PluginError] = {}
        results.update(depend_errors)

        for name in ordered_names:
            plugin_path = manifests[name][0]
            if not self.is_plugin_enabled(name):
                if name in self.loaded_records:
                    self.unload_plugin(name)
                results[name] = "disabled"
                continue
            if name in self.loaded_records:
                results[name] = "already_loaded"
                continue
            try:
                self.load_plugin(plugin_path)
                results[name] = "loaded"
            except PluginError as exc:
                logger.warning("Failed to load plugin %s: %s", name, exc)
                results[name] = exc
            except IncompatibleAPIError as exc:
                results[name] = PluginError(
                    kind="incompatible-api",
                    message=str(exc),
                    plugin_name=name,
                )
            except PermissionDeniedError as exc:
                results[name] = PluginError(
                    kind=PERMISSION_DENIED,
                    message=str(exc),
                    plugin_name=name,
                )
            except FileNotFoundError as exc:
                results[name] = PluginError(
                    kind="not-found",
                    message=str(exc),
                    plugin_name=name,
                )
            except (
                ImportError,
                TypeError,
                AttributeError,
                RuntimeError,
                ValueError,
                OSError,
                AssertionError,
            ) as exc:
                logger.warning("Failed to load plugin %s: %s", name, exc)
                results[name] = PluginError(
                    kind=LOAD_ERROR,
                    message=str(exc),
                    plugin_name=name,
                )
        return results

    @staticmethod
    def _status_str(value: str | PluginError) -> str:
        if isinstance(value, PluginError):
            return value.message
        return value

    def _scan_manifests(self) -> dict[str, tuple[Path, PluginManifest]]:
        """Parse every plugin.json that discovery finds.

        Returns mapping name -> (path, manifest). Unparseable manifests
        log a warning and are skipped — they surface as PluginError via
        ``load_plugin`` if a caller tries to load them by path.
        """
        import json as _json

        manifests: dict[str, tuple[Path, PluginManifest]] = {}
        for plugin_path in self.discover_plugins():
            try:
                manifest = PluginManifest.from_file(plugin_path / "plugin.json")
            except (OSError, _json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("Failed to parse manifest %s: %s", plugin_path, exc)
                continue
            manifests[manifest.name] = (plugin_path, manifest)
        return manifests

    @staticmethod
    def _path_is_under(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _resolve_dependency_order(
        self,
        manifests: dict[str, tuple[Path, PluginManifest]],
    ) -> tuple[list[Path], dict[str, PluginError]]:
        """F-055: topological sort by declared dependencies.

        Returns (ordered_paths, errors). Plugins with unsatisfied or
        cyclic dependencies appear in ``errors`` and are NOT in the
        ordered list.
        """
        ordered_names, errors = self._resolve_dependency_name_order(manifests)
        return [manifests[name][0] for name in ordered_names], errors

    def _resolve_dependency_name_order(
        self,
        manifests: dict[str, tuple[Path, PluginManifest]],
        *,
        roots: list[str] | None = None,
    ) -> tuple[list[str], dict[str, PluginError]]:
        """Return dependency-first manifest names while preserving marketplace ids."""
        from .install_records import canonical_dependency_name

        ordered: list[str] = []
        errors: dict[str, PluginError] = {}
        visiting: set[str] = set()
        visited: set[str] = set()

        def _visit(name: str) -> bool:
            if name in visited:
                return True
            if name in visiting:
                errors[name] = PluginError(
                    kind=DEPENDENCY_UNSATISFIED,
                    message="Cyclic dependency detected involving %s" % name,
                    plugin_name=name,
                    details={"cycle": sorted(visiting)},
                )
                return False
            if name not in manifests:
                return False
            visiting.add(name)
            _path, manifest = manifests[name]
            for raw_dep in manifest.dependencies or []:
                dep_name = canonical_dependency_name(raw_dep)
                if not dep_name:
                    continue
                if dep_name not in manifests:
                    errors[name] = PluginError(
                        kind=DEPENDENCY_UNSATISFIED,
                        message="Dependency %s for plugin %s is not installed" % (dep_name, name),
                        plugin_name=name,
                        details={"missing": [dep_name]},
                    )
                    visiting.discard(name)
                    return False
                if not _visit(dep_name):
                    errors[name] = PluginError(
                        kind=DEPENDENCY_UNSATISFIED,
                        message="Dependency %s for plugin %s failed to resolve" % (dep_name, name),
                        plugin_name=name,
                        details={"failed_dep": dep_name},
                    )
                    visiting.discard(name)
                    return False
            visiting.discard(name)
            visited.add(name)
            ordered.append(name)
            return True

        for name in roots or list(manifests):
            _visit(name)

        return ordered, errors

    # ------------------------------------------------------------------
    # Load / Unload
    # ------------------------------------------------------------------

    def load_plugin(self, plugin_path: Path, *, force: bool = False) -> ThirdPartyPlugin | LoadedPlugin:
        """Load plugin from path.

        F-020: When the manifest has no ``entry_point`` but declares
        Claude components (``skills``, ``commands``, ``mcp_servers``, ...),
        we materialize a component-only :class:`LoadedPlugin` record
        and register each component into its registry, without invoking
        the Python loader. Viola runtime plugins (with ``entry_point``)
        return the ``ThirdPartyPlugin`` instance as before. Disabled plugins
        fail closed unless ``force`` is explicitly set by an enable flow.
        """
        manifest = PluginManifest.from_file(plugin_path / "plugin.json")
        manifest.validate()

        # Fail-closed trust gate (SEC-031/SEC-034, sweep 2026-06-09). Built-in
        # plugins are trusted by location; everything else (e.g. sideloaded
        # plugins/user/*) must carry a signature trusted by this install. This
        # covers BOTH the runtime exec path and the component-only path
        # (hooks/commands a manifest registers are also attacker-controlled).
        from .trust import assert_plugin_trusted

        assert_plugin_trusted(plugin_path, manifest, self.builtin_dir)

        if not force and not self.is_plugin_enabled(manifest.name):
            raise PluginDisabledError("Plugin disabled: %s" % manifest.name)

        if not self._check_api_compatibility(manifest.api_version):
            raise IncompatibleAPIError(
                "Plugin %s requires API %s, but this system only supports 1.0" % (manifest.name, manifest.api_version)
            )

        # Request permissions
        for permission in manifest.permissions:
            try:
                perm = Permission(permission)
            except ValueError:
                # F-021: scoped permission rules are accepted as strings;
                # if it isn't one of the legacy enum buckets, route it
                # through the scoped rule store instead of failing closed.
                self.permission_manager.upsert_rule(manifest.name, str(permission))
                continue
            if not self.permission_manager.request_permission(manifest.name, perm):
                raise PermissionDeniedError("Permission denied: %s for %s" % (permission, manifest.name))

        # F-022: seed user_config defaults from the manifest schema
        # (sensitive entries are skipped — users install secrets at
        # enable time, not from a hard-coded default).
        if manifest.user_config:
            from .options_storage import parse_user_config_schema

            schema = parse_user_config_schema(manifest.user_config)
            self.options_storage.ensure_defaults(manifest.name, schema)

        # F-020: component-only plugin path.
        if not manifest.entry_point:
            if not manifest.has_claude_components():
                raise PluginError(
                    kind=LOAD_ERROR,
                    message="Plugin %s declares neither entry_point nor Claude components" % manifest.name,
                    plugin_name=manifest.name,
                )
            record = self._register_component_only(manifest, plugin_path)
            self.loaded_records[manifest.name] = record
            logger.info(
                "Plugin loaded (component-only): %s v%s",
                manifest.name,
                manifest.version,
            )
            return record

        # Runtime Python plugin path. Trust was asserted above; pass it through
        # so the loader's exec chokepoint stays fail-closed even if a future
        # caller reaches it directly.
        from .trust import is_trusted_plugin

        plugin_class = self.loader.load_plugin_class(
            plugin_path,
            manifest,
            trusted=is_trusted_plugin(plugin_path, manifest, self.builtin_dir),
        )

        # Create plugin context
        granted = self.permission_manager.grants.get(manifest.name, set())
        context = PluginContext(self, [p.value for p in granted], tts_callback=self._tts_callback)

        # Instantiate plugin
        plugin = plugin_class(context)

        # Ensure config defaults if plugin defines a schema
        schema = plugin.get_config_schema()
        if schema:
            self.config_manager.ensure_defaults(manifest.name, schema)

        # Initialize plugin (with sandbox)
        sandbox = PluginSandbox(plugin, tts_callback=self._tts_callback)

        try:
            sandbox.execute_with_isolation(plugin.on_init)
            plugin.on_start()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("Failed to start plugin %s", manifest.name)
            raise

        self.plugin_sandboxes[manifest.name] = sandbox
        self.loaded_plugins[manifest.name] = plugin
        record = LoadedPlugin(manifest=manifest, plugin_path=plugin_path, runtime=plugin)
        # Component-side: a runtime plugin may still declare Claude
        # components (e.g. accompanying skill files). Register them too.
        self._register_components_into(record, manifest)
        self.loaded_records[manifest.name] = record
        logger.info("Plugin loaded: %s v%s", manifest.name, manifest.version)

        return plugin

    def _register_component_only(self, manifest: PluginManifest, plugin_path: Path) -> LoadedPlugin:
        record = LoadedPlugin(manifest=manifest, plugin_path=plugin_path)
        self._register_components_into(record, manifest)
        return record

    def _register_components_into(self, record: LoadedPlugin, manifest: PluginManifest) -> None:
        """Materialize Claude-style components into typed registrations.

        For now we surface them as a list on the LoadedPlugin so callers
        (skill/hook/mcp registries) can iterate. Concrete wiring into
        Viola's skill/agent/MCP registries is incremental — unsupported
        component types collect a typed unsupported error rather than
        silently dropping.
        """
        component_map = {
            "hooks": manifest.hooks,
            "commands": manifest.commands,
            "agents": manifest.agents,
            "skills": manifest.skills,
            "output_styles": manifest.output_styles,
            "channels": manifest.channels,
            "mcp_servers": manifest.mcp_servers,
            "lsp_servers": manifest.lsp_servers,
        }
        for kind, value in component_map.items():
            if value in (None, "", [], {}):
                continue
            entries = self._normalize_component_entries(value)
            for name, payload in entries:
                record.components.append(
                    ComponentRegistration(
                        kind=kind,
                        name=name,
                        payload=payload,
                        plugin_name=manifest.name,
                    )
                )
            # Mark MCP / LSP components as unsupported runtime — they
            # parse cleanly but Viola does not yet provision external
            # servers from a manifest. Skills / commands / hooks /
            # output_styles surface to component registries as data
            # only; runtime wiring is done by callers.
            if kind in {"mcp_servers", "lsp_servers"} and entries:
                record.unsupported_components.append(
                    PluginError(
                        kind=UNSUPPORTED_COMPONENT,
                        message="%s components are parsed but not yet provisioned" % kind,
                        plugin_name=manifest.name,
                        details={"component_kind": kind, "count": len(entries)},
                    )
                )

    @staticmethod
    def _normalize_component_entries(value: Any) -> list[tuple[str, Any]]:
        """Normalize Claude component declarations to (name, payload) pairs."""
        if isinstance(value, str):
            return [(value, value)]
        if isinstance(value, list):
            out: list[tuple[str, Any]] = []
            for item in value:
                if isinstance(item, str):
                    out.append((item, item))
                elif isinstance(item, dict):
                    name = str(item.get("name") or item.get("id") or "")
                    if name:
                        out.append((name, item))
            return out
        if isinstance(value, dict):
            return [(str(k), v) for k, v in value.items()]
        return []

    def unload_plugin(self, plugin_name: str) -> None:
        """Unload plugin by name."""
        record = self.loaded_records.pop(plugin_name, None)
        plugin = self.loaded_plugins.pop(plugin_name, None)
        if plugin is None and record is None:
            logger.warning("Plugin not loaded: %s", plugin_name)
            return

        if plugin is not None:
            sandbox = self.plugin_sandboxes.get(plugin_name)
            self._run_unload_hooks(plugin_name, plugin, sandbox)

            if plugin_name in self.plugin_sandboxes:
                del self.plugin_sandboxes[plugin_name]

        logger.info("Plugin unloaded: %s", plugin_name)

    @staticmethod
    def _run_unload_hooks(
        plugin_name: str,
        plugin: ThirdPartyPlugin,
        sandbox: PluginSandbox | None,
    ) -> None:
        """Run on_stop/on_cleanup, logging any failure.

        Plugin-author lifecycle code is arbitrary, so we trap on the
        base ``Exception``. Specific failure modes (RuntimeError,
        AttributeError, OSError, etc.) all surface via the logger.
        """
        try:
            if sandbox:
                sandbox.execute_with_isolation(plugin.on_stop, allow_disabled=True)
                sandbox.execute_with_isolation(plugin.on_cleanup, allow_disabled=True)
            else:
                plugin.on_stop()
                plugin.on_cleanup()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("Error unloading plugin %s", plugin_name)

    # ------------------------------------------------------------------
    # Install / Remove / Update / Reload
    # ------------------------------------------------------------------

    def install(self, name: str, *, scope: str | None = None) -> ThirdPartyPlugin | LoadedPlugin:
        """Install a plugin by name from registry.

        For builtin plugins (``repo_url`` starts with ``builtin:``), copies
        from builtin dir and records the install under SCOPE_BUILTIN.
        Remote plugins are not yet implemented — they raise a tagged
        ``marketplace-not-implemented`` PluginError (F-023 / F-056).
        """
        from .install_records import (
            SCOPE_BUILTIN,
            SCOPE_USER,
            InstallEntry,
        )
        from .registry import RegistryClient

        if name in self.loaded_plugins:
            logger.info("Plugin %s is already installed", name)
            return self.loaded_plugins[name]
        if name in self.loaded_records:
            logger.info("Plugin %s is already installed (component-only)", name)
            return self.loaded_records[name]

        registry = RegistryClient()
        entry = registry.find_by_name(name)
        if entry is None:
            from core.exceptions import PluginNotFoundError

            raise PluginNotFoundError(name)

        repo_url = entry.repo_url

        if repo_url.startswith("builtin:"):
            # Builtin plugin — load from builtin dir
            plugin_path = self.builtin_dir / name
            if not plugin_path.exists():
                from core.exceptions import PluginInstallError

                raise PluginInstallError(name, "Builtin plugin directory not found")
            manifests = self._scan_manifests()
            ordered_names, dependency_errors = self._resolve_dependency_name_order(manifests, roots=[name])
            if dependency_errors:
                raise dependency_errors.get(name) or next(iter(dependency_errors.values()))

            loaded_by_name: dict[str, ThirdPartyPlugin | LoadedPlugin] = {}
            for plugin_name in ordered_names:
                dependency_path, dependency_manifest = manifests[plugin_name]
                loaded = self.loaded_records.get(plugin_name)
                if loaded is None:
                    loaded = self.load_plugin(dependency_path)
                loaded_by_name[plugin_name] = loaded
                source = "builtin" if self._path_is_under(dependency_path, self.builtin_dir) else "local"
                install_scope = (
                    scope
                    if plugin_name == name and scope is not None
                    else (SCOPE_BUILTIN if source == "builtin" else SCOPE_USER)
                )
                self.install_registry.upsert(
                    InstallEntry(
                        plugin_id=plugin_name,
                        scope=install_scope,
                        install_path=str(dependency_path),
                        version=dependency_manifest.version or entry.version,
                        source=source,
                    )
                )
            return loaded_by_name[name]

        # Remote plugin — typed unsupported until marketplace lands.
        raise PluginError(
            kind="marketplace-not-implemented",
            message=(
                "Remote plugin installation is not yet implemented. "
                "Place the plugin at plugins/user/%s/ manually." % name
            ),
            plugin_name=name,
            details={"repo_url": repo_url, "requested_scope": scope or SCOPE_USER},
        )

    def remove(self, name: str) -> None:
        """Remove a plugin (unload + delete user directory)."""
        self.unload_plugin(name)

        # Revoke all permissions
        if name in self.permission_manager.grants:
            del self.permission_manager.grants[name]
            self.permission_manager.save_grants()

        # Remove from user dir (never delete builtins)
        user_plugin_dir = self.user_dir / name
        if user_plugin_dir.exists():
            shutil.rmtree(user_plugin_dir, ignore_errors=True)
            logger.info("Removed plugin directory: %s", user_plugin_dir)

        # Clear from sys.modules
        mod_prefix = "plugins.user.%s" % name
        to_remove = [k for k in sys.modules if k.startswith(mod_prefix)]
        for k in to_remove:
            del sys.modules[k]

        # F-023: drop install records for the plugin (all scopes).
        try:
            self.install_registry.remove(name)
        except OSError as exc:
            logger.warning("Failed to remove install records for %s: %s", name, exc)

    def reload(self, name: str | None = None) -> dict[str, str]:
        """Hot-reload one or all plugins.

        Returns dict mapping plugin_name -> status string. Use
        :meth:`reload_tagged` for the F-056 PluginError shape.
        """
        return {n: self._status_str(v) for n, v in self.reload_tagged(name).items()}

    def reload_tagged(self, name: str | None = None) -> dict[str, str | PluginError]:
        results: dict[str, str | PluginError] = {}

        if name:
            names_to_reload = [name] if name in self.loaded_records else []
            if not names_to_reload:
                # Try discovering it
                for plugin_path in self.discover_plugins():
                    try:
                        manifest = PluginManifest.from_file(plugin_path / "plugin.json")
                        discovered_name = manifest.name
                    except (OSError, TypeError, ValueError):
                        discovered_name = plugin_path.name
                    if discovered_name == name:
                        names_to_reload = [name]
                        break
            if not names_to_reload:
                # F-056: callers reloading an unknown plugin get a typed
                # not-found error rather than an empty dict.
                results[name] = PluginError(
                    kind="not-found",
                    message="Plugin %s is not loaded and not on disk" % name,
                    plugin_name=name,
                )
                return results
        else:
            names_to_reload = sorted({*self.loaded_records.keys(), *self._discover_plugin_names()})

        for plugin_name in names_to_reload:
            try:
                # Find plugin path
                plugin_path = self._find_plugin_path(plugin_name)

                if not plugin_path:
                    results[plugin_name] = PluginError(
                        kind="not-found",
                        message="Plugin %s not found in builtin/ or user/" % plugin_name,
                        plugin_name=plugin_name,
                    )
                    continue

                if not self.is_plugin_enabled(plugin_name):
                    if plugin_name in self.loaded_records:
                        self.unload_plugin(plugin_name)
                    results[plugin_name] = "disabled"
                    continue

                old_record = self.loaded_records.pop(plugin_name, None)
                old_plugin = self.loaded_plugins.pop(plugin_name, None)
                old_sandbox = self.plugin_sandboxes.pop(plugin_name, None)

                # Unload
                if old_plugin is not None:
                    try:
                        if old_sandbox:
                            old_sandbox.execute_with_isolation(old_plugin.on_stop, allow_disabled=True)
                        else:
                            old_plugin.on_stop()
                    except (
                        AttributeError,
                        OSError,
                        RuntimeError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        logger.warning("Plugin %s stop during reload failed: %s", plugin_name, exc)

                # Clear cached modules
                mod_prefix = "plugins.user.%s" % plugin_name
                mod_prefix_builtin = "plugins.builtin.%s" % plugin_name
                to_remove = [k for k in sys.modules if k.startswith(mod_prefix) or k.startswith(mod_prefix_builtin)]
                for k in to_remove:
                    del sys.modules[k]

                # Delete .pyc files so Python doesn't serve stale bytecode
                self._purge_pycache(plugin_path)

                # Invalidate import caches so finder re-reads from disk
                importlib.invalidate_caches()

                # Reload
                try:
                    self.load_plugin(plugin_path)
                except (
                    PluginError,
                    IncompatibleAPIError,
                    PermissionDeniedError,
                    ImportError,
                    TypeError,
                    AttributeError,
                    RuntimeError,
                    ValueError,
                    OSError,
                    AssertionError,
                ):
                    self.loaded_records.pop(plugin_name, None)
                    self.loaded_plugins.pop(plugin_name, None)
                    self.plugin_sandboxes.pop(plugin_name, None)
                    if old_record is not None:
                        self.loaded_records[plugin_name] = old_record
                    if old_plugin is not None:
                        self.loaded_plugins[plugin_name] = old_plugin
                        if old_sandbox is not None:
                            self.plugin_sandboxes[plugin_name] = old_sandbox
                        try:
                            if old_sandbox:
                                old_sandbox.reset()
                                old_sandbox.execute_with_isolation(old_plugin.on_start, allow_disabled=True)
                            else:
                                old_plugin.on_start()
                        except (
                            AttributeError,
                            OSError,
                            RuntimeError,
                            TypeError,
                            ValueError,
                        ) as restart_exc:
                            logger.warning(
                                "Plugin %s rollback start failed: %s",
                                plugin_name,
                                restart_exc,
                            )
                    raise

                # Verify the module was loaded from the expected source file
                self._verify_module_source(plugin_name, plugin_path)

                if old_plugin is not None:
                    try:
                        if old_sandbox:
                            old_sandbox.execute_with_isolation(old_plugin.on_cleanup, allow_disabled=True)
                        else:
                            old_plugin.on_cleanup()
                    except (
                        AttributeError,
                        OSError,
                        RuntimeError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        logger.warning(
                            "Plugin %s cleanup after reload failed: %s",
                            plugin_name,
                            exc,
                        )

                results[plugin_name] = "reloaded"

            except PluginError as exc:
                logger.warning("Failed to reload plugin %s: %s", plugin_name, exc)
                results[plugin_name] = exc
            except (
                ImportError,
                TypeError,
                AttributeError,
                RuntimeError,
                ValueError,
                OSError,
                AssertionError,
            ) as exc:
                logger.warning("Failed to reload plugin %s: %s", plugin_name, exc)
                results[plugin_name] = PluginError(
                    kind="reload-error",
                    message=str(exc),
                    plugin_name=plugin_name,
                )

        return results

    def get_plugin_api_routes(
        self,
    ) -> list[tuple[str, str, str, Any]]:
        """Get all API routes from all loaded plugins.

        Returns list of (plugin_name, method, path, handler).
        """
        result: list[tuple[str, str, str, Any]] = []
        for name, plugin in self.loaded_plugins.items():
            sandbox = self.plugin_sandboxes.get(name)
            if not self.is_plugin_enabled(name) or (sandbox and sandbox.is_disabled):
                continue
            try:
                routes = plugin.get_api_routes()
                if routes:
                    for method, path, handler in routes:
                        result.append((name, method, path, handler))
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning("Failed to get routes from plugin %s: %s", name, exc)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _purge_pycache(plugin_path: Path) -> None:
        """Delete __pycache__ directories under plugin directory to prevent stale bytecode."""
        # Collect first, then delete (avoid modifying tree during iteration)
        pycache_dirs = [d for d in plugin_path.rglob("__pycache__") if d.is_dir()]
        for pycache_dir in pycache_dirs:
            shutil.rmtree(pycache_dir, ignore_errors=True)

    def _discover_plugin_names(self) -> list[str]:
        """Return manifest names for discoverable plugins."""
        names: list[str] = []
        for plugin_path in self.discover_plugins():
            try:
                manifest = PluginManifest.from_file(plugin_path / "plugin.json")
                names.append(manifest.name)
            except (OSError, TypeError, ValueError):
                names.append(plugin_path.name)
        return names

    def _find_plugin_path(self, plugin_name: str) -> Path | None:
        """Find a plugin directory by manifest name or directory name."""
        for plugin_path in self.discover_plugins():
            try:
                manifest = PluginManifest.from_file(plugin_path / "plugin.json")
                if manifest.name == plugin_name:
                    return plugin_path
            except (OSError, TypeError, ValueError):
                if plugin_path.name == plugin_name:
                    return plugin_path
        return None

    def _verify_module_source(self, plugin_name: str, plugin_path: Path) -> None:
        """Verify the loaded module's __file__ points to the expected source."""
        for prefix in ("plugins.user.", "plugins.builtin."):
            mod_name = prefix + plugin_name
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "__file__") and mod.__file__:
                expected_dir = str(plugin_path.resolve())
                actual_file = str(Path(mod.__file__).resolve())
                if not actual_file.startswith(expected_dir):
                    logger.warning(
                        "Plugin %s loaded from unexpected path: %s (expected under %s)",
                        plugin_name,
                        actual_file,
                        expected_dir,
                    )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _check_api_compatibility(self, api_version: str) -> bool:
        """Check if plugin API version is compatible."""
        return api_version == "1.0"

    def get_plugin(self, name: str) -> ThirdPartyPlugin | None:
        """Get plugin by name."""
        return self.loaded_plugins.get(name)

    def is_plugin_enabled(self, name: str) -> bool:
        """Return whether plugin config permits loading/execution."""
        return bool(self.config_manager.get_config(name).get("enabled", True))

    def set_plugin_enabled(self, name: str, enabled: bool) -> bool:
        """Persist plugin enabled state and apply it to the live manager."""
        plugin_path = self._find_plugin_path(name)
        if plugin_path is None and name not in self.loaded_records:
            return False

        config = self.config_manager.get_config(name)
        config["enabled"] = enabled
        if not self.config_manager.set_config(name, config):
            return False

        if not enabled:
            if name in self.loaded_records:
                self.unload_plugin(name)
            return True

        if name in self.loaded_records:
            sandbox = self.plugin_sandboxes.get(name)
            if sandbox is not None:
                sandbox.reset()
            return True

        if plugin_path is None:
            return False
        self.load_plugin(plugin_path, force=True)
        return True

    def list_plugins(self) -> list[str]:
        """List all loaded plugin names."""
        return list(self.loaded_plugins.keys())

    def get_plugin_info(self, name: str) -> dict[str, Any] | None:
        """Get plugin info dict."""
        plugin = self.loaded_plugins.get(name)
        if not plugin:
            return None
        sandbox = self.plugin_sandboxes.get(name)
        config_enabled = self.is_plugin_enabled(name)
        return {
            "name": plugin.name,
            "version": plugin.version,
            "description": plugin.description,
            "author": plugin.author,
            "enabled": config_enabled and not (sandbox and sandbox.is_disabled),
            "capabilities": [c.name for c in plugin.get_capabilities()],
        }
