"""Deterministic staged-rollout cohort helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

COHORT_ALGORITHM = "sha256-install-id-release-id-mod100-v1"
DEFAULT_ROLLOUT_STEPS = (1, 5, 25, 50, 100)


def cohort_bucket(install_id: str, release_id: str) -> int:
    """Return the stable 0-99 rollout bucket for an install and release."""
    normalized_install = str(install_id or "").strip()
    normalized_release = str(release_id or "").strip()
    if not normalized_install:
        raise ValueError("install_id is required for rollout cohort hashing")
    if not normalized_release:
        raise ValueError("release_id is required for rollout cohort hashing")

    digest = hashlib.sha256(f"{normalized_install}:{normalized_release}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def normalize_rollout_percent(value: Any) -> int:
    """Clamp an arbitrary rollout percentage to an integer in [0, 100]."""
    if isinstance(value, bool):
        return 100 if value else 0
    try:
        pct = int(float(value))
    except (TypeError, ValueError):
        pct = 100
    return max(0, min(100, pct))


def parse_rollout_steps(value: Any = None) -> tuple[int, ...]:
    """Return a validated, strictly increasing rollout ladder ending at 100."""
    if value is None or value == "":
        return DEFAULT_ROLLOUT_STEPS
    if isinstance(value, str):
        raw_items: Iterable[Any] = (item.strip() for item in value.split(","))
    elif isinstance(value, Iterable):
        raw_items = value
    else:
        raise ValueError("rollout steps must be a comma-separated string or iterable")

    steps: list[int] = []
    for item in raw_items:
        if item == "":
            continue
        if isinstance(item, bool):
            raise ValueError("rollout steps must be integer percentages")
        try:
            step = int(float(item))
        except (TypeError, ValueError) as exc:
            raise ValueError("rollout steps must be integer percentages") from exc
        if step <= 0 or step > 100:
            raise ValueError("rollout steps must be between 1 and 100")
        steps.append(step)

    if len(steps) < 2:
        raise ValueError("rollout steps must include at least one cohort and 100")
    if steps[-1] != 100:
        raise ValueError("rollout steps must end at 100")
    if any(left >= right for left, right in pairwise(steps)):
        raise ValueError("rollout steps must be strictly increasing")
    return tuple(steps)


def format_rollout_steps(steps: Iterable[int]) -> str:
    return " -> ".join(str(step) for step in steps)


def next_rollout_step(current_percent: Any, steps: Iterable[int] = DEFAULT_ROLLOUT_STEPS) -> int | None:
    current = normalize_rollout_percent(current_percent)
    parsed_steps = parse_rollout_steps(tuple(steps))
    if current <= 0:
        return parsed_steps[0]
    for step in parsed_steps:
        if step > current:
            return step
    return None


def is_in_rollout(
    install_id: str,
    release_id: str,
    rollout_percent: Any,
    *,
    frozen: bool = False,
) -> bool:
    """Return True when an install is eligible for a release candidate."""
    if frozen:
        return False
    pct = normalize_rollout_percent(rollout_percent)
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    return cohort_bucket(install_id, release_id) < pct
