"""Tier-2 sync engine — central stamping + apply logic.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Sequence
from uuid import uuid4

import asyncpg

from services.sync.conflict import IncomingMutation, ServerRowSnapshot
from services.sync.consent import current_consent_generation
from services.sync.hlc import hlc_now
from services.sync.version_vector import VersionVector, vv_bump, vv_merge

CLOUD_ACTOR_ID = "cloud"


@dataclass(frozen=True)
class CloudStamp:
    """The full set of CRDT columns the cloud writes on every mutation."""

    lww_hlc: str
    lww_actor_id: str
    version_vector: VersionVector
    field_versions: dict[str, str]
    updated_by_device_id: str
    last_mutation_id: str
    consent_generation: int
    updated_at: datetime


async def stamp(
    conn: asyncpg.Connection,
    user_id: str,
    surface: str,
    incoming: IncomingMutation | None,
    fields_touched: Sequence[str],
    device_id_header: str | None = None,
    server_row: ServerRowSnapshot | None = None,
) -> CloudStamp:
    """Compute the CloudStamp for a single mutation."""
    _ = surface
    actor_id = incoming.lww_actor_id if incoming is not None else CLOUD_ACTOR_ID
    observed_hlc = server_row.lww_hlc if server_row is not None else None
    lww_hlc = hlc_now(actor_id, observed=observed_hlc)

    server_vector = server_row.version_vector if server_row is not None else {}
    if incoming is not None:
        version_vector = vv_bump(vv_merge(server_vector, incoming.version_vector), actor_id)
        last_mutation_id = incoming.last_mutation_id
    else:
        version_vector = vv_bump(server_vector, CLOUD_ACTOR_ID)
        last_mutation_id = uuid4().hex

    field_versions = dict(server_row.field_versions if server_row is not None else {})
    for field_name in fields_touched:
        field_versions[str(field_name)] = lww_hlc

    device_id = str(device_id_header or "").strip()
    if not device_id:
        device_id = "cloud-%s" % uuid4().hex[:8]

    return CloudStamp(
        lww_hlc=lww_hlc,
        lww_actor_id=actor_id,
        version_vector=version_vector,
        field_versions=field_versions,
        updated_by_device_id=device_id,
        last_mutation_id=last_mutation_id,
        consent_generation=await current_consent_generation(conn, user_id),
        updated_at=datetime.now(UTC),
    )
