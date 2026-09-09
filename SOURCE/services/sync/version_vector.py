"""Version vector primitives for the Tier-2 sync engine.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
See ``docs/architecture/TIER2_CLOUD_LAUNCH_SPEC.md`` for the locked contract.
"""

from __future__ import annotations

from typing import TypeAlias

VersionVector: TypeAlias = dict[str, int]


def vv_merge(local: VersionVector, remote: VersionVector) -> VersionVector:
    """Return the field-wise maximum of *local* and *remote*."""
    merged: VersionVector = {}
    for key in set(local) | set(remote):
        merged[str(key)] = max(int(local.get(key, 0)), int(remote.get(key, 0)))
    return merged


def vv_dominates(a: VersionVector, b: VersionVector) -> bool:
    """Return True if a[k] >= b[k] for every key, AND strict in at least one."""
    strict = False
    for key in set(a) | set(b):
        left = int(a.get(key, 0))
        right = int(b.get(key, 0))
        if left < right:
            return False
        if left > right:
            strict = True
    return strict


def vv_concurrent(a: VersionVector, b: VersionVector) -> bool:
    """Return True if neither vector dominates the other."""
    return not vv_dominates(a, b) and not vv_dominates(b, a)


def vv_bump(vv: VersionVector, actor_id: str) -> VersionVector:
    """Return a new vector with vv[actor_id] += 1. Pure (no mutation of input)."""
    actor = str(actor_id or "").strip()
    if not actor:
        raise ValueError("actor_id must be non-empty")
    bumped = {str(key): int(value) for key, value in vv.items()}
    bumped[actor] = int(bumped.get(actor, 0)) + 1
    return bumped
