"""Plugin Registry

Local registry for discovering available plugins.
Reads from registry/registry.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.constants import PLUGIN_REGISTRY_PATH
from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class RegistryEntry:
    """A plugin available in the registry."""

    name: str
    description: str
    author: str
    repo_url: str
    version: str
    min_viola_version: str = "1.0"
    tags: list[str] = field(default_factory=list)
    intent_keywords: list[str] = field(default_factory=list)


class RegistryClient:
    """Read-only client for the local plugin registry."""

    def __init__(self, registry_path: str | Path | None = None):
        if registry_path is None:
            registry_path = PLUGIN_REGISTRY_PATH
        self._path = Path(registry_path)
        self._entries: list[RegistryEntry] | None = None

    def _load(self) -> list[RegistryEntry]:
        if self._entries is not None:
            return self._entries

        if not self._path.exists():
            logger.debug("Registry file not found: %s", self._path)
            self._entries = []
            return self._entries

        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)

            self._entries = []
            for item in data:
                self._entries.append(
                    RegistryEntry(
                        name=item["name"],
                        description=item.get("description", ""),
                        author=item.get("author", "Unknown"),
                        repo_url=item.get("repo_url", ""),
                        version=item.get("version", "0.1.0"),
                        min_viola_version=item.get("min_viola_version", "1.0"),
                        tags=item.get("tags", []),
                        intent_keywords=item.get("intent_keywords", []),
                    )
                )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load registry: %s", exc)
            self._entries = []

        return self._entries

    def list_all(self) -> list[RegistryEntry]:
        """List all entries in the registry."""
        return list(self._load())

    def find_by_name(self, name: str) -> RegistryEntry | None:
        """Find a registry entry by plugin name."""
        for entry in self._load():
            if entry.name == name:
                return entry
        return None

    def search(self, query: str) -> list[RegistryEntry]:
        """Full-text search across name, description, and tags."""
        query_lower = query.lower()
        results: list[RegistryEntry] = []
        for entry in self._load():
            text = " ".join([entry.name, entry.description, entry.author] + entry.tags).lower()
            if query_lower in text:
                results.append(entry)
        return results
