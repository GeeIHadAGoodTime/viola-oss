"""Conflict resolution for incoming Tier-2 sync mutations.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
See ``docs/architecture/TIER2_CLOUD_LAUNCH_SPEC.md`` for the locked contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from services.sync.hlc import hlc_compare
from services.sync.version_vector import VersionVector, vv_concurrent, vv_dominates


class ResolutionOutcome(str, Enum):
    ACCEPT = "accept"
    IGNORE = "ignore"
    MERGE = "merge"


@dataclass(frozen=True)
class IncomingMutation:
    surface: str
    op: Literal["upsert", "delete"]
    row: dict[str, Any]
    version_vector: VersionVector
    lww_hlc: str
    lww_actor_id: str
    last_mutation_id: str
    field_versions: dict[str, str]


@dataclass(frozen=True)
class ServerRowSnapshot:
    version_vector: VersionVector
    lww_hlc: str
    lww_actor_id: str
    last_mutation_id: str
    field_versions: dict[str, str]
    deleted_at: datetime | None


def resolve(server: ServerRowSnapshot | None, incoming: IncomingMutation) -> ResolutionOutcome:
    """Decide whether to apply *incoming* on top of *server*."""
    if server is None:
        return ResolutionOutcome.ACCEPT

    if server.last_mutation_id == incoming.last_mutation_id and server.version_vector == incoming.version_vector:
        return ResolutionOutcome.IGNORE

    if vv_dominates(incoming.version_vector, server.version_vector):
        return ResolutionOutcome.ACCEPT

    if server.deleted_at is not None and incoming.op != "delete":
        return ResolutionOutcome.IGNORE

    if vv_dominates(server.version_vector, incoming.version_vector):
        return ResolutionOutcome.IGNORE

    if vv_concurrent(server.version_vector, incoming.version_vector):
        if incoming.op == "delete" and server.deleted_at is None:
            return ResolutionOutcome.ACCEPT
        hlc_order = hlc_compare(incoming.lww_hlc, server.lww_hlc)
        if hlc_order > 0:
            return ResolutionOutcome.MERGE
        if hlc_order < 0:
            return ResolutionOutcome.IGNORE
        if incoming.lww_actor_id > server.lww_actor_id:
            return ResolutionOutcome.MERGE
        return ResolutionOutcome.IGNORE

    return ResolutionOutcome.IGNORE
