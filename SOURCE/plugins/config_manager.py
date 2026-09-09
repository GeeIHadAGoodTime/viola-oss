"""Plugin Configuration Manager

Handles per-plugin configuration storage and validation.
Config files are stored as YAML alongside each plugin.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from core.constants import PLUGIN_BUILTIN_DIR, PLUGIN_CONFIG_FILENAME, PLUGIN_USER_DIR
from core.logging_config import get_logger

from .manifest import PluginManifest

logger = get_logger(__name__)

PLUGIN_SETTINGS_FILENAME = "settings.json"

try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


class PluginConfigManager:
    """Manage per-plugin configuration (load, save, validate)."""

    def __init__(self, plugin_base_dirs: list[Path] | None = None):
        if plugin_base_dirs is None:
            plugin_base_dirs = [Path("plugins/builtin"), Path("plugins/user")]
        self._base_dirs = plugin_base_dirs

    def _find_plugin_dir(self, plugin_name: str) -> Path | None:
        """Find the directory for a plugin by name."""
        for base in self._base_dirs:
            candidate = base / plugin_name
            if candidate.is_dir():
                return candidate
        return None

    def _config_path(self, plugin_name: str) -> Path | None:
        """Return the config path for a plugin, or None when missing."""
        plugin_dir = self._find_plugin_dir(plugin_name)
        if not plugin_dir:
            return None
        return plugin_dir / PLUGIN_CONFIG_FILENAME

    @staticmethod
    def _write_bytes_atomic(path: Path, payload: bytes) -> None:
        """Write bytes using same-directory replace so failed writes keep the old file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        fd, tmp_name = tempfile.mkstemp(
            prefix=".%s." % path.name,
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except OSError:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                logger.debug("Failed to remove temporary config file %s", tmp_path)
            raise

    def get_config(self, plugin_name: str) -> dict[str, Any]:
        """Load plugin config from YAML file.

        Returns empty dict if no config file exists or YAML unavailable.
        """
        config_path = self._config_path(plugin_name)
        if not config_path:
            return {}

        if not config_path.exists():
            return {}

        if not _YAML_AVAILABLE:
            logger.debug("YAML not available, returning empty config for %s", plugin_name)
            return {}

        try:
            with open(config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Failed to load config for plugin %s: %s", plugin_name, exc)
            return {}

    def set_config(self, plugin_name: str, config: dict[str, Any]) -> bool:
        """Save plugin config to YAML file.

        Returns True on success.
        """
        config_path = self._config_path(plugin_name)
        if not config_path:
            logger.warning("Plugin directory not found for %s", plugin_name)
            return False

        if not _YAML_AVAILABLE:
            logger.warning("YAML not available, cannot save config for %s", plugin_name)
            return False

        try:
            rendered = yaml.safe_dump(config, default_flow_style=False)
            self._write_bytes_atomic(config_path, rendered.encode("utf-8"))
            return True
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Failed to save config for plugin %s: %s", plugin_name, exc)
            return False

    def set_configs_atomic(self, configs_by_plugin: dict[str, dict[str, Any]]) -> bool:
        """Save multiple plugin configs as one all-or-nothing filesystem update."""
        if not _YAML_AVAILABLE:
            logger.warning("YAML not available, cannot save plugin configs")
            return False

        rendered_by_path: dict[Path, bytes] = {}
        for plugin_name, config in configs_by_plugin.items():
            config_path = self._config_path(plugin_name)
            if not config_path:
                logger.warning("Plugin directory not found for %s", plugin_name)
                return False
            try:
                rendered_by_path[config_path] = yaml.safe_dump(config, default_flow_style=False).encode("utf-8")
            except (TypeError, ValueError, yaml.YAMLError) as exc:
                logger.warning("Failed to render config for plugin %s: %s", plugin_name, exc)
                return False

        snapshots: dict[Path, bytes | None] = {
            path: path.read_bytes() if path.exists() else None for path in rendered_by_path
        }
        try:
            for config_path, payload in rendered_by_path.items():
                self._write_bytes_atomic(config_path, payload)
            return True
        except OSError as exc:
            logger.warning("Atomic plugin config update failed, rolling back: %s", exc)
            for config_path, snapshot in snapshots.items():
                try:
                    if snapshot is None:
                        config_path.unlink(missing_ok=True)
                    else:
                        self._write_bytes_atomic(config_path, snapshot)
                except OSError as rollback_exc:
                    logger.error(
                        "Failed to roll back config file %s: %s",
                        config_path,
                        rollback_exc,
                    )
            return False

    def validate_config(
        self,
        plugin_name: str,
        schema: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        """Validate plugin config against a JSON Schema.

        Returns (is_valid, error_message).
        Falls back to True if jsonschema is not installed.
        """
        if config is None:
            config = self.get_config(plugin_name)

        try:
            import jsonschema

            jsonschema.validate(config, schema)
            return True, None
        except ImportError:
            # jsonschema not installed — skip validation
            return True, None
        except (jsonschema.SchemaError, jsonschema.ValidationError) as exc:
            return False, str(exc)

    def ensure_defaults(self, plugin_name: str, schema: dict[str, Any]) -> bool:
        """Ensure config file exists with defaults from schema.

        If config file doesn't exist, creates it with defaults from the schema's
        "properties" section. Returns True if defaults were written.
        """
        plugin_dir = self._find_plugin_dir(plugin_name)
        if not plugin_dir:
            return False

        config_path = plugin_dir / PLUGIN_CONFIG_FILENAME
        if config_path.exists():
            return False  # Config already exists

        # Extract defaults from schema properties
        properties = schema.get("properties", {})
        defaults: dict[str, Any] = {}
        for key, prop in properties.items():
            if "default" in prop:
                defaults[key] = prop["default"]

        if defaults:
            return self.set_config(plugin_name, defaults)
        return False


def load_plugin_settings_defaults(
    plugin_base_dirs: list[Path] | None = None,
    *,
    allowed_keys: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Load allowlisted plugin default settings from settings.json and plugin.json."""
    base_dirs = plugin_base_dirs or [Path(PLUGIN_BUILTIN_DIR), Path(PLUGIN_USER_DIR)]
    merged: dict[str, Any] = {}
    for base_dir in base_dirs:
        if not base_dir.is_dir():
            continue
        for plugin_dir in sorted((path for path in base_dir.iterdir() if path.is_dir()), key=lambda path: path.name):
            plugin_settings = _load_plugin_settings(plugin_dir)
            if not plugin_settings:
                continue
            for key, value in _flatten_plugin_settings(plugin_settings).items():
                if not isinstance(key, str) or not key:
                    continue
                if allowed_keys is not None and key not in allowed_keys:
                    continue
                if _is_json_compatible(value):
                    merged[key] = value
    return merged


def _load_plugin_settings(plugin_dir: Path) -> dict[str, Any]:
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.exists():
        return {}

    try:
        manifest = PluginManifest.from_file(manifest_path)
        manifest.validate()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Skipping plugin settings for %s: %s", plugin_dir, exc)
        return {}

    settings: dict[str, Any] = {}
    settings_path = plugin_dir / PLUGIN_SETTINGS_FILENAME
    if settings_path.exists():
        try:
            with settings_path.open(encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                _merge_plugin_settings(settings, loaded)
            else:
                logger.warning("Ignoring non-object plugin settings at %s", settings_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to load plugin settings from %s: %s", settings_path, exc)

    if isinstance(manifest.settings, dict):
        _merge_plugin_settings(settings, manifest.settings)
    return settings


def _merge_plugin_settings(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged = dict(existing)
            _merge_plugin_settings(merged, value)
            target[key] = merged
        else:
            target[key] = value


def _flatten_plugin_settings(settings: dict[str, Any]) -> dict[str, Any]:
    flattened = {key: value for key, value in settings.items() if key != "agent"}
    agent_settings = settings.get("agent")
    if isinstance(agent_settings, dict):
        aliases = {
            "enabled": "agent_enabled",
            "autonomy": "agent_autonomy",
            "model": "agent_model",
            "reasoning_effort": "agent_reasoning_effort",
            "browser_session_mode": "browser_session_mode",
            "auto_propose_shortcuts": "auto_propose_shortcuts",
        }
        for source_key, target_key in aliases.items():
            if source_key in agent_settings:
                flattened[target_key] = agent_settings[source_key]
    return flattened


def _is_json_compatible(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_compatible(item) for key, item in value.items())
    return False
