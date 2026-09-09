"""API Registry -- tracks which services have direct API integrations.

The registry returns neutral facts about known integrations. The model decides
whether those facts are useful for the current task.

Registry entries are either:
- BUILT_IN: Viola has a native integration (Gmail, Weather, Calendar)
- DISCOVERED: Agent found and validated an API at runtime
- MANUAL: User configured an API key for a service
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

REGISTRY_PATH = get_data_dir() / "api_registry.json"
_LEGACY_REGISTRY_PATH = Path.home().joinpath(".viola", "api_registry.json")


def _migrate_legacy_registry_if_needed() -> None:
    if REGISTRY_PATH.exists() or not _LEGACY_REGISTRY_PATH.exists():
        return
    try:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_REGISTRY_PATH, REGISTRY_PATH)
        logger.info(
            "Migrated legacy API registry from %s to %s",
            _LEGACY_REGISTRY_PATH,
            REGISTRY_PATH,
        )
    except OSError as exc:
        logger.warning(
            "Could not migrate legacy API registry from %s to %s: %s",
            _LEGACY_REGISTRY_PATH,
            REGISTRY_PATH,
            exc,
        )


class ApiSource(str, Enum):
    """How this API entry was discovered."""

    BUILT_IN = "built_in"
    DISCOVERED = "discovered"
    MANUAL = "manual"


class ApiStatus(str, Enum):
    """Current operational status of the API."""

    ACTIVE = "active"
    NEEDS_AUTH = "needs_auth"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass
class ApiEntry:
    """A registered API integration."""

    service_name: str
    display_name: str
    source: str  # ApiSource value
    status: str  # ApiStatus value
    tools: list[str]
    description: str
    domains: list[str]
    success_count: int = 0
    failure_count: int = 0
    last_used: str | None = None
    auth_type: str | None = None
    notes: str = ""


class ApiRegistry:
    """Registry of available API integrations."""

    def __init__(self) -> None:
        self._entries: dict[str, ApiEntry] = {}
        self._load()

    def _load(self) -> None:
        """Load registry from disk and register builtins."""
        _migrate_legacy_registry_if_needed()
        if REGISTRY_PATH.exists():
            try:
                data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
                for name, entry_data in data.items():
                    self._entries[name] = ApiEntry(**entry_data)
                logger.debug(
                    "API registry loaded: %d entries from %s",
                    len(self._entries),
                    REGISTRY_PATH,
                )
            except Exception:
                logger.debug("Failed to load API registry, starting fresh")
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register built-in API integrations."""
        calendar_status = ApiStatus.ACTIVE.value

        builtins = [
            ApiEntry(
                service_name="weather_nws",
                display_name="National Weather Service",
                source=ApiSource.BUILT_IN.value,
                status=ApiStatus.ACTIVE.value,
                tools=["weather"],
                description=(
                    "Structured US weather forecasts and conditions; historical weather, climate research, "
                    "and weather news need outside context"
                ),
                domains=["weather.gov", "forecast.weather.gov"],
                auth_type="none",
            ),
            ApiEntry(
                service_name="web_search",
                display_name="Web Search",
                source=ApiSource.BUILT_IN.value,
                status=ApiStatus.ACTIVE.value,
                tools=["web_search"],
                description=(
                    "Public web snippets for current events, prices, reviews, regulations, and questions "
                    "without a matching structured integration"
                ),
                domains=[],
                auth_type="api_key",
            ),
            ApiEntry(
                service_name="google_calendar",
                display_name="Calendar",
                source=ApiSource.BUILT_IN.value,
                status=calendar_status,
                tools=[
                    "calendar",
                ],
                description=(
                    "Create, list, update, and delete local-primary calendar events; countdown timers and recurring "
                    "Viola automations are separate capabilities"
                ),
                domains=[
                    "calendar.google.com",
                    "graph.microsoft.com",
                    "outlook.live.com",
                    "outlook.office.com",
                ],
                auth_type="oauth_or_password",
                notes="Local calendar is always available; Google, Microsoft 365/Outlook, and CalDAV add sync",
            ),
            # -- Services with known APIs but no native tools yet --
            # Registered as neutral facts; the model decides how to use them.
            ApiEntry(
                service_name="dominos",
                display_name="Domino's Pizza",
                source=ApiSource.BUILT_IN.value,
                status=ApiStatus.NEEDS_AUTH.value,
                tools=[],
                description="Order pizza from Domino's via their ordering API",
                domains=["dominos.com", "pizza.dominos.com"],
                notes="pizzapi PyPI package wraps ordering API",
            ),
            ApiEntry(
                service_name="uber",
                display_name="Uber",
                source=ApiSource.BUILT_IN.value,
                status=ApiStatus.NEEDS_AUTH.value,
                tools=[],
                description="Request rides via Uber",
                domains=["uber.com", "m.uber.com"],
                notes="Uber API requires OAuth2 app registration",
            ),
            ApiEntry(
                service_name="opentable",
                display_name="OpenTable",
                source=ApiSource.BUILT_IN.value,
                status=ApiStatus.NEEDS_AUTH.value,
                tools=[],
                description="Make restaurant reservations via OpenTable",
                domains=["opentable.com"],
                notes="OpenTable API available for restaurant reservation booking",
            ),
            ApiEntry(
                service_name="yelp",
                display_name="Yelp",
                source=ApiSource.BUILT_IN.value,
                status=ApiStatus.NEEDS_AUTH.value,
                tools=[],
                description="Search for businesses and read reviews via Yelp Fusion API",
                domains=["yelp.com"],
                notes="Yelp Fusion API provides business search and reviews",
            ),
            ApiEntry(
                service_name="instacart",
                display_name="Instacart",
                source=ApiSource.BUILT_IN.value,
                status=ApiStatus.NEEDS_AUTH.value,
                tools=[],
                description="Order grocery delivery via Instacart",
                domains=["instacart.com"],
                notes="Instacart Connect API available for grocery ordering",
            ),
        ]

        for entry in builtins:
            if entry.service_name not in self._entries:
                self._entries[entry.service_name] = entry
            else:
                # Update tools list from builtins
                existing = self._entries[entry.service_name]
                existing.tools = entry.tools

    def save(self) -> None:
        """Persist registry to disk."""
        try:
            REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, Any] = {name: asdict(entry) for name, entry in self._entries.items()}
            REGISTRY_PATH.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Failed to save API registry")

    def lookup_by_domain(self, url: str) -> ApiEntry | None:
        """Check if a URL has a registered API."""
        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            return None

        _matchable = {ApiStatus.ACTIVE.value, ApiStatus.NEEDS_AUTH.value}
        for entry in self._entries.values():
            if entry.status in _matchable:
                for d in entry.domains:
                    if d in domain or domain.endswith(d):
                        return entry
        return None

    def lookup_by_service(self, service_name: str) -> ApiEntry | None:
        """Look up API by service name."""
        return self._entries.get(service_name.lower())

    def record_success(self, service_name: str) -> None:
        """Record a successful API use."""
        entry = self._entries.get(service_name)
        if entry:
            entry.success_count += 1
            entry.last_used = datetime.now(UTC).isoformat()
            self.save()

    def record_failure(self, service_name: str) -> None:
        """Record a failed API use."""
        entry = self._entries.get(service_name)
        if entry:
            entry.failure_count += 1
            self.save()

    def register_discovered(
        self,
        service_name: str,
        display_name: str,
        description: str,
        domains: list[str],
        tools: list[str] | None = None,
    ) -> None:
        """Register an API discovered at runtime by the agent."""
        self._entries[service_name] = ApiEntry(
            service_name=service_name,
            display_name=display_name,
            source=ApiSource.DISCOVERED.value,
            status=ApiStatus.ACTIVE.value,
            tools=tools or [],
            description=description,
            domains=domains,
        )
        self.save()
        logger.info(
            "Discovered API registered: %s (%s)",
            service_name,
            display_name,
        )

    def list_entries(self) -> list[ApiEntry]:
        """Return all registry entries."""
        return list(self._entries.values())


# Singleton
_registry: ApiRegistry | None = None


def get_api_registry() -> ApiRegistry:
    """Get or create the global API registry singleton."""
    global _registry
    if _registry is None:
        _registry = ApiRegistry()
    return _registry
