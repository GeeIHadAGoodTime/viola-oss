"""Meta-analysis service -- weekly review of accumulated data.

Runs weekly (or on-demand) to analyze task logs, user patterns, error
registries, and the user model to produce actionable insights.
"""

from __future__ import annotations

from services.meta_analysis.weekly_review import (
    WeeklyReviewService,
    get_weekly_review_service,
)

__all__ = [
    "WeeklyReviewService",
    "get_weekly_review_service",
]
