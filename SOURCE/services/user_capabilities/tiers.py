"""Autonomy-tier validation for user-authored routines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mcp_hub.client_hub import MCPClientHub

from .schema import CallCapabilityAction, CapabilityAction, Tier, normalize_tier

TIER_ORDER: dict[Tier, int] = {"solo": 0, "ensemble": 1, "symphony": 2}
KNOWN_SURFACES: frozenset[str] = frozenset({"web", "desktop", "phone", "voice", "voice-stream", "sms", "email"})
TIER_DESCRIPTIONS: dict[Tier, str] = {
    "solo": "Solo can use memory, web, weather, music, timers, reminders, settings, and safe Viola panels.",
    "ensemble": "Ensemble adds read-only files, email, calendar, scheduling, browser, system, and account tools.",
    "symphony": "Symphony can use the full tool surface, including high-autonomy computer and write actions.",
}
# Canonical phone tool blocklist — single source of truth at
# telephony/call_manager.py:_PHONE_DESKTOP_TOOL_BLOCKLIST = frozenset({"phone", "ask_user"}).
# Duplicated here (rather than imported) to keep this module free of the heavy
# telephony import surface. If the canon expands, update this list in sync.
#
# Rationale for matching canon (not over-restricting):
# - Phone agent CAN call media/browser/computer when relevant; the production
#   phone pipeline doesn't block them. Over-restricting user_capabilities here
#   would create inconsistent UX where the agent uses a tool the routine can't.
# - `phone` is blocked to prevent call-in-call recursion.
# - `ask_user` is blocked because there's no interactive surface mid-call.
_PHONE_CAPABILITY_BLOCKED_TOOLS: frozenset[str] = frozenset({"phone", "ask_user"})
_SURFACE_BLOCKED_TOOLS: dict[str, set[str]] = {
    "phone": set(_PHONE_CAPABILITY_BLOCKED_TOOLS),
}


@dataclass(frozen=True)
class ToolTierRequirement:
    """Required tier for one call_capability action."""

    tool_name: str
    required_tier: Tier
    defaulted_to_symphony: bool = False


@dataclass(frozen=True)
class TierComputation:
    """Computed routine tier plus per-tool evidence."""

    required_tier: Tier
    tool_requirements: tuple[ToolTierRequirement, ...]


class CapabilityTierError(ValueError):
    """Raised when a routine exceeds the user's selected autonomy tier."""

    def __init__(
        self,
        *,
        required_tier: Tier,
        user_tier: Tier,
        requirements: Iterable[ToolTierRequirement],
    ) -> None:
        self.required_tier = required_tier
        self.user_tier = user_tier
        self.requirements = tuple(requirements)
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        defaulted = [item.tool_name for item in self.requirements if item.defaulted_to_symphony]
        if defaulted:
            reason = " Tool(s) not listed in Solo or Ensemble default to Symphony: %s." % ", ".join(sorted(defaulted))
        else:
            reason = ""
        return "This routine requires %s, but the current autonomy tier is %s.%s" % (
            self.required_tier.capitalize(),
            self.user_tier.capitalize(),
            reason,
        )


class CapabilitySurfaceError(ValueError):
    """Raised when a routine uses tools blocked on the current surface."""

    def __init__(self, *, surface: str, blocked_tools: Iterable[str]) -> None:
        self.surface = surface
        self.blocked_tools = tuple(sorted(set(blocked_tools)))
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        if not self.blocked_tools:
            return "This routine is not available on %s." % self.surface
        return "This routine is not available on %s because this surface blocks: %s." % (
            self.surface,
            ", ".join(self.blocked_tools),
        )


def max_tier(left: Tier, right: Tier) -> Tier:
    """Return the higher autonomy tier."""
    return left if TIER_ORDER[left] >= TIER_ORDER[right] else right


def required_tier_for_tool(tool_name: str) -> ToolTierRequirement:
    """Return the minimum tier required for an MCP tool name."""
    normalized = str(tool_name or "").strip()
    hidden_map = MCPClientHub._TIER_HIDDEN_TOOL_MAP
    for hidden_tools in hidden_map.values():
        if normalized in hidden_tools:
            return ToolTierRequirement(normalized, "symphony")

    tier_map = MCPClientHub._TIER_TOOL_MAP
    solo_tools = tier_map.get("solo") or set()
    if normalized in solo_tools:
        return ToolTierRequirement(normalized, "solo")

    ensemble_tools = tier_map.get("ensemble") or set()
    if normalized in ensemble_tools:
        return ToolTierRequirement(normalized, "ensemble")

    return ToolTierRequirement(normalized, "symphony", defaulted_to_symphony=True)


def compute_required_tier(actions: Iterable[CapabilityAction]) -> TierComputation:
    """Compute the highest tier required by a routine's actions."""
    required: Tier = "solo"
    requirements: list[ToolTierRequirement] = []
    for action in actions:
        if not isinstance(action, CallCapabilityAction):
            continue
        requirement = required_tier_for_tool(action.name)
        requirements.append(requirement)
        required = max_tier(required, requirement.required_tier)
    return TierComputation(required_tier=required, tool_requirements=tuple(requirements))


def ensure_tier_allowed(
    *,
    required_tier: Tier,
    user_tier: object,
    requirements: Iterable[ToolTierRequirement],
) -> Tier:
    """Validate that the selected user tier can save or run the routine."""
    normalized_user_tier = normalize_tier(user_tier)
    if TIER_ORDER[required_tier] > TIER_ORDER[normalized_user_tier]:
        raise CapabilityTierError(
            required_tier=required_tier,
            user_tier=normalized_user_tier,
            requirements=requirements,
        )
    return normalized_user_tier


def _normalize_surface(surface: object) -> str | None:
    normalized = str(surface or "").strip().lower()
    if not normalized:
        return None
    if normalized == "http":
        return "web"
    return normalized


def _tool_blocked_on_surface(tool_name: str, surface: str | None) -> bool:
    normalized_surface = _normalize_surface(surface)
    if normalized_surface is None:
        return False
    normalized_tool = str(tool_name or "").strip()
    if not normalized_tool:
        return False
    blocked = _SURFACE_BLOCKED_TOOLS.get(normalized_surface, set())
    for blocked_name in blocked:
        if blocked_name.endswith("*") and normalized_tool.startswith(blocked_name[:-1]):
            return True
        if blocked_name == "browser" and normalized_tool.startswith("browser_"):
            return True
        if normalized_tool == blocked_name:
            return True
    return False


def required_surfaces_for_tool(tool_name: str) -> set[str]:
    """Return known surfaces where an MCP tool can run."""
    return {surface for surface in KNOWN_SURFACES if not _tool_blocked_on_surface(tool_name, surface)}


def blocked_tools_for_surface(actions: Iterable[CapabilityAction], surface: object) -> list[str]:
    """Return routine tool names blocked on the given surface."""
    normalized_surface = _normalize_surface(surface)
    if normalized_surface is None:
        return []
    blocked: list[str] = []
    for action in actions:
        if isinstance(action, CallCapabilityAction) and _tool_blocked_on_surface(action.name, normalized_surface):
            blocked.append(action.name)
    return sorted(set(blocked))


def ensure_surface_allowed(actions: Iterable[CapabilityAction], surface: object) -> str | None:
    """Validate that a routine can run on the current channel/surface."""
    normalized_surface = _normalize_surface(surface)
    if normalized_surface is None:
        return None
    blocked = blocked_tools_for_surface(actions, normalized_surface)
    if blocked:
        raise CapabilitySurfaceError(surface=normalized_surface, blocked_tools=blocked)
    return normalized_surface
