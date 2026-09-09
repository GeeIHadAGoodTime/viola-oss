"""
Capability Provider base protocol.

Defines the CapabilityProvider protocol that all providers must implement,
and the SmartDevice dataclass for normalized device representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from services.capability_registry import HealthStatus, ProviderType

# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CapabilityProvider(Protocol):
    """Interface for capability providers.

    Every provider that plugs into a capability domain must implement
    this protocol. The registry uses it to query health, tools, and
    setup guidance.
    """

    @property
    def provider_id(self) -> str:
        """Unique identifier: 'home_assistant', 'philips_hue', etc."""
        ...

    @property
    def domain_id(self) -> str:
        """Which domain this serves: 'smart_home', 'music', etc."""
        ...

    @property
    def provider_type(self) -> ProviderType:
        """MCP, native, plugin, or agent_tool."""
        ...

    async def check_health(self) -> HealthStatus:
        """Verify the provider is reachable and functional."""
        ...

    def get_tools(self) -> list[str]:
        """Return MCP tool names this provider exposes."""
        ...

    def get_setup_guide(self) -> dict[str, Any]:
        """Return setup instructions for connecting this provider."""
        ...

    async def auto_setup(self, params: dict[str, Any]) -> bool:
        """Attempt automatic setup. Returns True if successful."""
        ...


# ---------------------------------------------------------------------------
# SmartDevice — Provider-Agnostic Device Representation
# ---------------------------------------------------------------------------


@dataclass
class SmartDevice:
    """Normalized device representation across all smart home providers.

    No provider-specific fields — the ``provider_entity_id`` is the only
    field that maps back to the provider's native identifier.
    """

    device_id: str
    device_type: str  # light, climate, lock, sensor, switch, cover, media_player
    display_name: str
    room: str
    aliases: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    provider: str = ""
    provider_entity_id: str = ""
    state: dict[str, object] | None = None
