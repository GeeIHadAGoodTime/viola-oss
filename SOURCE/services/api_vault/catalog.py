"""API Catalog — unencrypted registry of what each API does.

LLM-readable catalog of available API integrations. Stored as plain JSON
since it contains no secrets — only service descriptions, capabilities,
and health status.

Storage: .viola/api_vault/catalog.json
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir
from core.url_validation import validate_external_url

logger = get_logger(__name__)

_CATALOG_FILE = get_data_dir() / "api_vault" / "catalog.json"


@dataclass
class ApiCatalogEntry:
    """A registered API with its capabilities and health."""

    service_name: str
    description: str
    base_url: str
    docs_url: str
    capabilities: list[str]
    auth_type: str  # "bearer_token", "api_key", "oauth2", "header"
    rate_limits: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "unknown",
            "last_check": None,
            "last_error": None,
        }
    )
    added_date: str = ""
    usage_count: int = 0
    header_name: str = "Authorization"  # Custom header name for auth
    header_prefix: str = "Bearer"  # e.g. "Bearer", "Token", ""


class ApiCatalog:
    """Registry of API capabilities available to the LLM.

    This is the LLM-facing catalog. It tells the LLM what APIs are available,
    what they can do, and how healthy they are — but never exposes credentials.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ApiCatalogEntry] = {}
        self._load()

    def _load(self) -> None:
        """Load catalog from disk."""
        if not _CATALOG_FILE.exists():
            return
        try:
            data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
            for name, entry_data in data.items():
                self._entries[name] = ApiCatalogEntry(**entry_data)
            logger.debug("API catalog loaded: %d entries", len(self._entries))
        except Exception:
            logger.exception("Failed to load API catalog")

    def save(self) -> None:
        """Persist catalog to disk."""
        try:
            _CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {name: asdict(entry) for name, entry in self._entries.items()}
            _CATALOG_FILE.write_text(
                json.dumps(data, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to save API catalog")

    def register_api(
        self,
        service_name: str,
        description: str,
        base_url: str,
        docs_url: str = "",
        capabilities: list[str] | None = None,
        auth_type: str = "bearer_token",
        rate_limits: dict[str, Any] | None = None,
        header_name: str = "Authorization",
        header_prefix: str = "Bearer",
    ) -> ApiCatalogEntry:
        """Register a new API in the catalog.

        Args:
            service_name: Unique identifier (e.g., "notion", "github")
            description: Human-readable description of what the API does
            base_url: Base URL for API requests
            docs_url: URL to API documentation
            capabilities: List of capability strings
            auth_type: Authentication type
            rate_limits: Rate limit configuration
            header_name: HTTP header name for auth
            header_prefix: Prefix before the token in the header

        Returns:
            The created catalog entry
        """
        safe_base_url = validate_external_url(base_url.strip())

        entry = ApiCatalogEntry(
            service_name=service_name,
            description=description,
            base_url=safe_base_url,
            docs_url=docs_url,
            capabilities=capabilities or [],
            auth_type=auth_type,
            rate_limits=rate_limits or {},
            added_date=datetime.now(UTC).isoformat(),
            header_name=header_name,
            header_prefix=header_prefix,
        )
        self._entries[service_name] = entry
        self.save()
        logger.info("API registered: %s", service_name)
        return entry

    def get_entry(self, service_name: str) -> ApiCatalogEntry | None:
        """Get a catalog entry by service name."""
        return self._entries.get(service_name)

    def remove_api(self, service_name: str) -> bool:
        """Remove an API from the catalog."""
        if service_name in self._entries:
            del self._entries[service_name]
            self.save()
            logger.info("API removed from catalog: %s", service_name)
            return True
        return False

    def get_catalog(self) -> list[dict[str, Any]]:
        """Get the full catalog (LLM-safe, no secrets)."""
        return [asdict(entry) for entry in self._entries.values()]

    def get_catalog_summary(self) -> str:
        """Get a compact catalog summary for LLM context injection (<200 tokens).

        Returns:
            Human-readable summary of available APIs
        """
        if not self._entries:
            return "No dynamic APIs configured."

        lines = ["Available APIs:"]
        for entry in self._entries.values():
            status = entry.health.get("status", "unknown")
            caps = ", ".join(entry.capabilities[:3])
            if len(entry.capabilities) > 3:
                caps += ", ..."
            lines.append(
                "- %s (%s): %s [%s]"
                % (
                    entry.service_name,
                    status,
                    entry.description[:60],
                    caps,
                )
            )
        return "\n".join(lines)

    def find_api_for_capability(self, description: str) -> list[ApiCatalogEntry]:
        """Search for APIs that match a capability description.

        Args:
            description: Natural language description of needed capability

        Returns:
            Matching catalog entries, sorted by relevance
        """
        desc_lower = description.lower()
        matches = []
        for entry in self._entries.values():
            score = 0
            # Check capabilities
            for cap in entry.capabilities:
                if cap.lower() in desc_lower or desc_lower in cap.lower():
                    score += 2
            # Check description
            for word in desc_lower.split():
                if len(word) > 3 and word in entry.description.lower():
                    score += 1
            if score > 0:
                matches.append((score, entry))
        matches.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in matches]

    def update_health(
        self,
        service_name: str,
        status: str,
        last_error: str | None = None,
    ) -> None:
        """Update the health status of an API.

        Args:
            service_name: The service to update
            status: New status ("healthy", "degraded", "unhealthy", "unknown")
            last_error: Optional error message from last failure
        """
        entry = self._entries.get(service_name)
        if entry is None:
            return
        entry.health = {
            "status": status,
            "last_check": datetime.now(UTC).isoformat(),
            "last_error": last_error,
        }
        self.save()

    def increment_usage(self, service_name: str) -> None:
        """Increment the usage counter for an API."""
        entry = self._entries.get(service_name)
        if entry:
            entry.usage_count += 1
            self.save()

    def load_template(self, template_path: Path) -> ApiCatalogEntry | None:
        """Load an API template and register it (without credentials).

        Templates contain everything except the API key. When a user
        provides a key for a known service, the template auto-populates.

        Args:
            template_path: Path to the template JSON file

        Returns:
            The registered entry, or None on failure
        """
        try:
            data = json.loads(template_path.read_text(encoding="utf-8"))
            return self.register_api(**data)
        except Exception:
            logger.exception("Failed to load API template: %s", template_path)
            return None


# Singleton
_catalog: ApiCatalog | None = None


def get_api_catalog() -> ApiCatalog:
    """Get or create the global API catalog singleton."""
    global _catalog
    if _catalog is None:
        _catalog = ApiCatalog()
    return _catalog
