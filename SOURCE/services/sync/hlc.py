"""Hybrid Logical Clock — Tier-2 sync engine timestamping primitive.

Format: ``<ms_since_epoch>:<logical_counter>:<actor_id>``.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
See ``docs/architecture/TIER2_CLOUD_LAUNCH_SPEC.md`` for the locked contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

_LAST_BY_ACTOR: dict[str, str] = {}


def _parse_hlc(value: str) -> tuple[int, int, str]:
    try:
        ms_raw, counter_raw, actor_id = value.split(":", 2)
        ms = int(ms_raw)
        counter = int(counter_raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid HLC value: %r" % value) from exc
    if ms < 0 or counter < 0 or not actor_id:
        raise ValueError("invalid HLC value: %r" % value)
    return ms, counter, actor_id


def _format_hlc(ms: int, counter: int, actor_id: str) -> str:
    actor = str(actor_id or "").strip()
    if not actor:
        raise ValueError("actor_id must be non-empty")
    if ms < 0 or counter < 0:
        raise ValueError("HLC ms and counter must be non-negative")
    return "%d:%d:%s" % (int(ms), int(counter), actor)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def hlc_now(actor_id: str, observed: str | None = None) -> str:
    """Return a fresh HLC stamped by *actor_id*; ratchets forward past *observed* if needed."""
    actor = str(actor_id or "").strip()
    if not actor:
        raise ValueError("actor_id must be non-empty")

    candidates: list[tuple[int, int, str]] = [(_now_ms(), 0, actor)]
    if observed:
        candidates.append(_parse_hlc(observed))
    last = _LAST_BY_ACTOR.get(actor)
    if last:
        candidates.append(_parse_hlc(last))

    max_ms = max(candidate[0] for candidate in candidates)
    counter = 0
    observed_counter = max((candidate[1] for candidate in candidates if candidate[0] == max_ms), default=-1)
    if observed_counter >= 0 and any(candidate[0] == max_ms and candidate[1] > 0 for candidate in candidates):
        counter = observed_counter + 1
    elif observed and _parse_hlc(observed)[0] == max_ms:
        counter = _parse_hlc(observed)[1] + 1
    elif last and _parse_hlc(last)[0] == max_ms:
        counter = _parse_hlc(last)[1] + 1

    value = _format_hlc(max_ms, counter, actor)
    _LAST_BY_ACTOR[actor] = value
    return value


def hlc_compare(a: str, b: str) -> int:
    """Return -1 if a < b, 0 if equal, +1 if a > b. Lexicographic on (ms, counter, actor_id)."""
    left = _parse_hlc(a)
    right = _parse_hlc(b)
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def hlc_merge(local: str, remote: str) -> str:
    """Return the max of *local* and *remote* with the counter bumped."""
    winner = local if hlc_compare(local, remote) >= 0 else remote
    ms, counter, actor_id = _parse_hlc(winner)
    value = _format_hlc(ms, counter + 1, actor_id)
    _LAST_BY_ACTOR[actor_id] = value
    return value
