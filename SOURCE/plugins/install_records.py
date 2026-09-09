"""Plugin install records (F-023, F-055).

Claude tracks marketplace sources and install records by plugin id,
scope, install path, project path, version, timestamps, and git commit.
We mirror that with a ``version: 2`` installed-plugin file.

Marketplace fetch / unblock / cache logic is NOT implemented here —
remote install still returns a typed ``marketplace-not-implemented``
PluginError. This module is the metadata substrate the marketplace
layer will hang off when it lands.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)


# Install scopes (Claude TS reference: utils/plugins/schemas.ts:1506-1568)
SCOPE_BUILTIN = "builtin"
SCOPE_LOCAL = "local"
SCOPE_USER = "user"
SCOPE_PROJECT = "project"
SCOPE_MANAGED = "managed"

VALID_SCOPES: frozenset[str] = frozenset({SCOPE_BUILTIN, SCOPE_LOCAL, SCOPE_USER, SCOPE_PROJECT, SCOPE_MANAGED})


@dataclass
class InstallEntry:
    """One install record for a plugin under a particular scope."""

    plugin_id: str
    scope: str
    install_path: str
    version: str
    source: str = "local"  # "local" | "marketplace:<id>" | "builtin"
    project_path: str | None = None
    git_commit: str | None = None
    installed_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def record_key(self) -> str:
        return plugin_record_key(self.plugin_id, self.source)

    def to_file_entry(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": file_scope(self.scope),
            "installPath": self.install_path,
            "version": self.version,
            "installedAt": timestamp_to_iso(self.installed_at),
            "lastUpdated": timestamp_to_iso(self.updated_at),
        }
        if self.project_path:
            payload["projectPath"] = self.project_path
        if self.git_commit:
            payload["gitCommitSha"] = self.git_commit
        return payload


@dataclass
class InstalledPluginsFile:
    version: int = 2
    entries: list[InstallEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        plugins: dict[str, list[dict[str, Any]]] = {}
        for entry in self.entries:
            plugins.setdefault(entry.record_key(), []).append(entry.to_file_entry())
        return {"version": self.version, "plugins": plugins}


def file_scope(scope: str) -> str:
    if scope == SCOPE_BUILTIN:
        return SCOPE_MANAGED
    return scope


def timestamp_to_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return time.time()
    return time.time()


def marketplace_from_source(source: str) -> str | None:
    if source.startswith("marketplace:"):
        marketplace = source.split(":", 1)[1].strip()
        return marketplace or None
    if source not in {"", "local", "builtin"} and ":" not in source:
        return source
    return None


def plugin_record_key(plugin_id: str, source: str = "local") -> str:
    plugin_name, key_marketplace = split_plugin_record_key(plugin_id)
    marketplace = marketplace_from_source(source) or key_marketplace
    return "%s@%s" % (plugin_name, marketplace) if marketplace else plugin_name


def split_plugin_record_key(record_key: str) -> tuple[str, str | None]:
    text = str(record_key or "").strip()
    if "@" not in text:
        return text, None
    plugin_name, marketplace = text.split("@", 1)
    return plugin_name.strip(), marketplace.strip() or None


class PluginInstallRegistry:
    """Persistent record of plugin installs keyed by (plugin_id, scope)."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = get_data_dir() / "installed_plugins.json"
        self._path = path
        self._file = InstalledPluginsFile()
        self.reload()

    def reload(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read installed_plugins.json: %s", exc)
            return
        version = int(data.get("version") or 1)
        entries: list[InstallEntry] = []
        if isinstance(data.get("plugins"), dict):
            entries.extend(self._parse_v2_plugins(data["plugins"]))
        else:
            entries.extend(self._parse_legacy_entries(data.get("entries") or []))
        self._file = InstalledPluginsFile(version=version, entries=entries)

    @staticmethod
    def _parse_v2_plugins(plugins: dict[str, Any]) -> list[InstallEntry]:
        entries: list[InstallEntry] = []
        for record_key, scoped_entries in plugins.items():
            plugin_name, marketplace = split_plugin_record_key(str(record_key))
            if not plugin_name or not isinstance(scoped_entries, list):
                logger.warning("Skipping malformed plugin install record %s", record_key)
                continue
            source = "marketplace:%s" % marketplace if marketplace else "local"
            for raw in scoped_entries:
                if not isinstance(raw, dict):
                    logger.warning("Skipping malformed install entry %s", raw)
                    continue
                try:
                    entries.append(
                        InstallEntry(
                            plugin_id=plugin_name,
                            scope=str(raw.get("scope") or SCOPE_USER),
                            install_path=str(raw.get("installPath") or ""),
                            version=str(raw.get("version") or "0.0.0"),
                            source=source,
                            project_path=raw.get("projectPath"),
                            git_commit=raw.get("gitCommitSha"),
                            installed_at=parse_timestamp(raw.get("installedAt")),
                            updated_at=parse_timestamp(raw.get("lastUpdated")),
                            enabled=bool(raw.get("enabled", True)),
                        )
                    )
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Skipping malformed install entry %s: %s", raw, exc)
        return entries

    @staticmethod
    def _parse_legacy_entries(entries_raw: list[Any]) -> list[InstallEntry]:
        entries: list[InstallEntry] = []
        for raw in entries_raw:
            if not isinstance(raw, dict):
                logger.warning("Skipping malformed install entry %s", raw)
                continue
            try:
                entries.append(
                    InstallEntry(
                        plugin_id=str(raw["plugin_id"]),
                        scope=str(raw.get("scope") or SCOPE_USER),
                        install_path=str(raw.get("install_path") or ""),
                        version=str(raw.get("version") or "0.0.0"),
                        source=str(raw.get("source") or "local"),
                        project_path=raw.get("project_path"),
                        git_commit=raw.get("git_commit"),
                        installed_at=parse_timestamp(raw.get("installed_at")),
                        updated_at=parse_timestamp(raw.get("updated_at")),
                        enabled=bool(raw.get("enabled", True)),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed install entry %s: %s", raw, exc)
        return entries

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._file.to_dict(), fh, indent=2, sort_keys=True)
        except OSError as exc:
            logger.warning("Failed to write installed_plugins.json: %s", exc)

    def list(self, *, scope: str | None = None) -> list[InstallEntry]:
        if scope is None:
            return list(self._file.entries)
        return [e for e in self._file.entries if e.scope == scope]

    def find(self, plugin_id: str, *, scope: str | None = None) -> InstallEntry | None:
        for entry in self._file.entries:
            if entry.plugin_id in {plugin_id, split_plugin_record_key(plugin_id)[0]} and (
                scope is None or entry.scope == scope
            ):
                return entry
            if entry.record_key() == plugin_id and (scope is None or entry.scope == scope):
                return entry
        return None

    def upsert(self, entry: InstallEntry) -> None:
        if entry.scope not in VALID_SCOPES:
            raise ValueError("install scope must be one of %s" % sorted(VALID_SCOPES))
        for idx, existing in enumerate(self._file.entries):
            if existing.record_key() == entry.record_key() and existing.scope == entry.scope:
                entry.installed_at = existing.installed_at  # preserve original
                entry.updated_at = time.time()
                self._file.entries[idx] = entry
                self.save()
                return
        self._file.entries.append(entry)
        self.save()

    def remove(self, plugin_id: str, *, scope: str | None = None) -> int:
        before = len(self._file.entries)
        bare_name = split_plugin_record_key(plugin_id)[0]
        if scope is None:
            self._file.entries = [
                e for e in self._file.entries if e.plugin_id != bare_name and e.record_key() != plugin_id
            ]
        else:
            self._file.entries = [
                e
                for e in self._file.entries
                if not ((e.plugin_id == bare_name or e.record_key() == plugin_id) and e.scope == scope)
            ]
        removed = before - len(self._file.entries)
        if removed:
            self.save()
        return removed


def canonical_dependency_name(raw: Any) -> str:
    """Normalize a Claude-style dependency declaration to a plugin id.

    Claude accepts bare names, ``plugin@marketplace`` strings, or object
    form with ``{"name": ..., "marketplace": ...}``. Marketplace identity
    is preserved so cross-marketplace dependency gaps fail closed.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        return text
    if isinstance(raw, dict):
        name = str(raw.get("name") or raw.get("id") or "").strip()
        marketplace = str(raw.get("marketplace") or "").strip()
        if name and marketplace and "@" not in name:
            return "%s@%s" % (name, marketplace)
        return name
    return ""


__all__ = [
    "SCOPE_BUILTIN",
    "SCOPE_LOCAL",
    "SCOPE_MANAGED",
    "SCOPE_PROJECT",
    "SCOPE_USER",
    "VALID_SCOPES",
    "InstallEntry",
    "InstalledPluginsFile",
    "PluginInstallRegistry",
    "canonical_dependency_name",
]
