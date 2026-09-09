"""Agent tool handlers for user-authored routines."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.logging_config import get_logger
from core.user_context import get_current_user_id
from intent.tool_types import ToolResult
from services.user_capabilities import (
    CapabilityArgsValidationError,
    build_run_plan,
    create_for_user,
    delete_for_user,
    list_for_user,
    set_disabled_for_user,
    update_for_user,
)
from services.user_capabilities.tiers import CapabilitySurfaceError, CapabilityTierError

logger = get_logger(__name__)


def _resolve_current_user_id() -> str:
    return get_current_user_id()


def _validation_error(prefix: str, exc: ValidationError) -> ToolResult:
    return ToolResult(ok=False, error="%s: %s" % (prefix, exc))


async def create_user_capability(spec: dict[str, Any] | None, *, root: Path | None = None) -> ToolResult:
    """Create a user-authored phrase-triggered routine."""
    try:
        user_id = _resolve_current_user_id()
    except LookupError:
        return ToolResult(ok=False, error="Authenticated user context is required for routines.")

    if not isinstance(spec, dict):
        return ToolResult(ok=False, error="spec must be a JSON object.")

    try:
        result = create_for_user(spec, user_id=user_id, root=root)
        return ToolResult(
            ok=True,
            data={
                "capability": result.capability.model_dump(mode="json"),
                "collisions": result.collisions,
                "instant_collisions": result.instant_collisions,
            },
        )
    except ValidationError as exc:
        return _validation_error("Invalid routine spec", exc)
    except CapabilityArgsValidationError as exc:
        return ToolResult(ok=False, error=str(exc), data={"validation_errors": exc.errors})
    except CapabilityTierError as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            data={"required_tier": exc.required_tier, "current_tier": exc.user_tier},
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    except OSError as exc:
        logger.exception("Failed to create user capability")
        return ToolResult(ok=False, error="Failed to save routine: %s" % exc)


async def list_user_capabilities(*, root: Path | None = None) -> ToolResult:
    """List the current user's saved routines."""
    try:
        user_id = _resolve_current_user_id()
    except LookupError:
        return ToolResult(ok=False, error="Authenticated user context is required for routines.")

    try:
        capabilities = list_for_user(user_id, root=root)
        return ToolResult(
            ok=True,
            data={
                "capabilities": capabilities,
                "count": len(capabilities),
            },
        )
    except OSError as exc:
        logger.exception("Failed to list user capabilities")
        return ToolResult(ok=False, error="Failed to list routines: %s" % exc)


async def delete_user_capability(capability_id: str, *, root: Path | None = None) -> ToolResult:
    """Delete the current user's routine by id."""
    try:
        user_id = _resolve_current_user_id()
    except LookupError:
        return ToolResult(ok=False, error="Authenticated user context is required for routines.")

    try:
        result = delete_for_user(capability_id, user_id=user_id, root=root)
        return ToolResult(ok=True, data=result)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    except OSError as exc:
        logger.exception("Failed to delete user capability")
        return ToolResult(ok=False, error="Failed to delete routine: %s" % exc)


async def update_user_capability(
    capability_id: str,
    spec: dict[str, Any] | None,
    *,
    root: Path | None = None,
) -> ToolResult:
    """Update the current user's routine by id."""
    try:
        user_id = _resolve_current_user_id()
    except LookupError:
        return ToolResult(ok=False, error="Authenticated user context is required for routines.")

    if not isinstance(spec, dict):
        return ToolResult(ok=False, error="spec must be a JSON object.")

    try:
        result = update_for_user(capability_id, spec, user_id=user_id, root=root)
        return ToolResult(
            ok=True,
            data={
                "capability": result.capability.model_dump(mode="json"),
                "collisions": result.collisions,
                "instant_collisions": result.instant_collisions,
            },
        )
    except FileNotFoundError as exc:
        return ToolResult(ok=False, error=str(exc))
    except ValidationError as exc:
        return _validation_error("Invalid routine spec", exc)
    except CapabilityArgsValidationError as exc:
        return ToolResult(ok=False, error=str(exc), data={"validation_errors": exc.errors})
    except CapabilityTierError as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            data={"required_tier": exc.required_tier, "current_tier": exc.user_tier},
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    except OSError as exc:
        logger.exception("Failed to update user capability")
        return ToolResult(ok=False, error="Failed to update routine: %s" % exc)


async def toggle_user_capability(
    capability_id: str,
    disabled: bool,
    *,
    root: Path | None = None,
) -> ToolResult:
    """Enable or disable the current user's routine by id."""
    try:
        user_id = _resolve_current_user_id()
    except LookupError:
        return ToolResult(ok=False, error="Authenticated user context is required for routines.")

    if not isinstance(disabled, bool):
        return ToolResult(ok=False, error="disabled must be a boolean.")

    try:
        capability = set_disabled_for_user(capability_id, disabled, user_id=user_id, root=root)
        return ToolResult(ok=True, data={"capability": capability.model_dump(mode="json")})
    except FileNotFoundError as exc:
        return ToolResult(ok=False, error=str(exc))
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    except OSError as exc:
        logger.exception("Failed to toggle user capability")
        return ToolResult(ok=False, error="Failed to toggle routine: %s" % exc)


async def run_user_capability(capability_id: str, *, root: Path | None = None) -> ToolResult:
    """Return a structured plan for the current user's routine."""
    try:
        user_id = _resolve_current_user_id()
    except LookupError:
        return ToolResult(ok=False, error="Authenticated user context is required for routines.")

    try:
        return ToolResult(ok=True, data=build_run_plan(capability_id, user_id=user_id, root=root))
    except FileNotFoundError as exc:
        return ToolResult(ok=False, error=str(exc))
    except CapabilitySurfaceError as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            data={"surface": exc.surface, "blocked_tools": list(exc.blocked_tools)},
        )
    except CapabilityTierError as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            data={"required_tier": exc.required_tier, "current_tier": exc.user_tier},
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    except OSError as exc:
        logger.exception("Failed to build user capability run plan")
        return ToolResult(ok=False, error="Failed to run routine: %s" % exc)
