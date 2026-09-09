"""Aggregate cost tracking across all call history.

Reads call history metadata.json files and computes summary statistics.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from core.logging_config import get_logger
from telephony.call_history import list_all_call_history

logger = get_logger(__name__)


class CostAggregator:
    """Reads all call history and computes aggregate cost stats."""

    def get_summary(self, since_days: int = 30) -> dict:
        """Compute cost summary across all calls.

        Args:
            since_days: Only include calls from the last N days.

        Returns:
            Dict with totals, averages, and per-model breakdown.
        """
        cutoff = (datetime.now(tz=UTC) - timedelta(days=since_days)).isoformat()
        entries = list_all_call_history(limit=1000)

        total_calls = 0
        total_duration = 0.0
        total_cost = 0.0
        breakdown = {"telnyx": 0.0, "llm": 0.0, "stt": 0.0, "tts": 0.0}
        by_model: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
        )

        for entry in entries:
            # Filter by date
            if entry.started_at and entry.started_at < cutoff:
                continue

            total_calls += 1
            total_duration += entry.duration_seconds

            cost = entry.cost_breakdown
            if not cost:
                continue

            call_total = cost.get("total_cost_usd", 0)
            total_cost += call_total

            telnyx = cost.get("telnyx", {})
            llm = cost.get("llm", {})
            stt = cost.get("stt", {})
            tts = cost.get("tts", {})

            breakdown["telnyx"] += telnyx.get("cost_usd", 0)
            breakdown["llm"] += llm.get("cost_usd", 0)
            breakdown["stt"] += stt.get("cost_usd", 0)
            breakdown["tts"] += tts.get("cost_usd", 0)

            model = llm.get("model", "unknown")
            by_model[model]["calls"] += 1
            by_model[model]["prompt_tokens"] += llm.get("prompt_tokens", 0)
            by_model[model]["completion_tokens"] += llm.get("completion_tokens", 0)
            by_model[model]["cost_usd"] += llm.get("cost_usd", 0)

        total_minutes = total_duration / 60.0

        summary = {
            "period_days": since_days,
            "total_calls": total_calls,
            "total_duration_minutes": round(total_minutes, 1),
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_call_usd": round(total_cost / total_calls, 4) if total_calls else 0,
            "avg_cost_per_minute_usd": round(total_cost / total_minutes, 4) if total_minutes else 0,
            "breakdown": {k: round(v, 4) for k, v in breakdown.items()},
            "by_model": {
                model: {
                    "calls": data["calls"],
                    "prompt_tokens": data["prompt_tokens"],
                    "completion_tokens": data["completion_tokens"],
                    "cost_usd": round(data["cost_usd"], 4),
                }
                for model, data in by_model.items()
            },
        }
        summary["fixed_costs"] = {
            "number_rental_usd": 1.00,
            "regulatory_surcharge_pct": 10,
        }
        return summary
