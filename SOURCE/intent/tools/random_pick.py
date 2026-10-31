"""Real randomness for dice, coin flips, random numbers and random choices.

A language model has no random source: asked to "roll a dice" it said "You rolled
a 4" every time (1.0.5 round-7 latency measurements). This tool draws from the
operating system's CSPRNG (``random.SystemRandom``) so the result is genuinely
random. Read-only, no side effects, available at every autonomy level and loaded
on the first turn so it costs one tool step, not a ToolSearch round trip too.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

from intent.tool_types import ToolResult

_RNG = random.SystemRandom()

MAX_COUNT = 100
MAX_SIDES = 1000
MAX_OPTIONS = 100
MAX_RANGE_SPAN = 10**12

_KINDS = ("dice", "coin", "number", "choose")


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _error(message: str) -> ToolResult:
    return ToolResult(ok=False, error=message, error_category="INVALID_INPUT")


def random_pick(
    kind: str = "dice",
    *,
    count: int = 1,
    sides: int = 6,
    low: int = 1,
    high: int = 100,
    options: Sequence[str] | None = None,
) -> ToolResult:
    """Draw ``count`` results of ``kind`` (dice, coin, number, choose)."""
    kind = str(kind or "dice").strip().lower()
    if kind in {"die", "roll"}:
        kind = "dice"
    if kind in {"flip", "coin_flip"}:
        kind = "coin"
    if kind not in _KINDS:
        return _error("kind must be one of: %s" % ", ".join(_KINDS))
    count = _bounded(count, 1, 1, MAX_COUNT)

    if kind == "dice":
        sides = _bounded(sides, 6, 2, MAX_SIDES)
        results: list[Any] = [_RNG.randint(1, sides) for _ in range(count)]
        label = "d%d" % sides if count == 1 else "%dd%d" % (count, sides)
        data: dict[str, Any] = {"kind": "dice", "dice": label, "sides": sides, "results": results}
        if count > 1:
            data["total"] = sum(results)
        return ToolResult(ok=True, data=data)

    if kind == "coin":
        results = [_RNG.choice(("heads", "tails")) for _ in range(count)]
        return ToolResult(ok=True, data={"kind": "coin", "results": results})

    if kind == "number":
        try:
            lo, hi = int(low), int(high)
        except (TypeError, ValueError):
            return _error("low and high must be whole numbers")
        if lo > hi:
            lo, hi = hi, lo
        if hi - lo > MAX_RANGE_SPAN:
            return _error("the range is too large")
        results = [_RNG.randint(lo, hi) for _ in range(count)]
        return ToolResult(ok=True, data={"kind": "number", "low": lo, "high": hi, "results": results})

    choices = [str(item).strip() for item in (options or []) if str(item).strip()][:MAX_OPTIONS]
    if len(choices) < 2:
        return _error("choose needs at least two options")
    results = [_RNG.choice(choices) for _ in range(count)]
    return ToolResult(ok=True, data={"kind": "choose", "options": choices, "results": results})
