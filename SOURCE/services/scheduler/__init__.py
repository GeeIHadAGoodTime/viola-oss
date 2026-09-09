"""Persistent scheduled automation service.

Provides a SchedulerService backed by SQLite for scheduling recurring
and one-shot actions that execute as sub-agents through the intent pipeline.
"""

from __future__ import annotations

from services.scheduler.service import SchedulerService, get_scheduler_service

__all__ = ["SchedulerService", "get_scheduler_service"]
