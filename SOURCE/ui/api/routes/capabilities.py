"""
Capability Registry API — exposes domain connection state.

GET /v1/capabilities returns all domains with their current state,
provider info, health status, and setup guides for available domains.
"""

from __future__ import annotations

from typing import Any

from contracts.api_response import success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, Request
from services.capability_registry import (
    CapabilityRegistry,
    DomainState,
)
from ui.api.routes.auth_dependencies import require_auth

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["capabilities"])

_PUBLIC_SERVICES_TAB_DOMAINS = frozenset(
    {
        "music",
        "calendar",
        "email",
        "telegram",
        "multiroom",
        "smart_home",
    }
)


@router.get("/capabilities", dependencies=[Depends(require_auth)])
async def get_capabilities(request: Request) -> dict[str, Any]:
    """Return all capability domains with state, provider, and setup info."""
    registry: CapabilityRegistry | None = getattr(request.app.state, "capability_registry", None)
    if registry is None:
        return success_response(
            {
                "domains": [],
                "summary": {
                    "total": 0,
                    "connected": 0,
                    "available": 0,
                    "discovered": 0,
                    "unknown": 0,
                },
            }
        )

    domains: list[dict[str, Any]] = []
    for domain in registry._domains.values():
        if domain.domain_id not in _PUBLIC_SERVICES_TAB_DOMAINS:
            continue
        entry: dict[str, Any] = {
            "domain_id": domain.domain_id,
            "display_name": domain.display_name,
            "state": domain.state.value,
            "provider": domain.provider,
            "provider_type": domain.provider_type.value,
            "health": domain.health.value,
            "tools_count": len(domain.tools),
            "setup_guide": None,
        }
        if domain.setup_guide is not None:
            entry["setup_guide"] = {
                "orthodox_path": domain.setup_guide.orthodox_path,
                "auto_setup_possible": domain.setup_guide.auto_setup_possible,
                "requirements": domain.setup_guide.requirements,
                "estimated_effort": domain.setup_guide.estimated_effort,
            }
        domains.append(entry)

    summary = {
        "total": len(domains),
        "connected": sum(1 for d in domains if d["state"] == DomainState.CONNECTED.value),
        "available": sum(1 for d in domains if d["state"] == DomainState.AVAILABLE.value),
        "discovered": sum(1 for d in domains if d["state"] == DomainState.DISCOVERED.value),
        "unknown": sum(1 for d in domains if d["state"] == DomainState.UNKNOWN.value),
    }

    return success_response({"domains": domains, "summary": summary})
