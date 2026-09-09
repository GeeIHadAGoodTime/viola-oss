"""Lightweight activity recency tracker.

Tracks when activities (music, agent_task) start and stop, so the
system can determine which activity is "most recent" for smart-stop
behaviour.

Multi-user safe: activities are keyed by (user_id, activity) so that
User A's "stop" command cannot affect User B's music.

Usage::

    from core.activity_tracker import get_activity_tracker

    tracker = get_activity_tracker()
    tracker.record_start("music")
    tracker.record_start("agent_task")
    tracker.most_recent_active()  # -> "agent_task"
    tracker.stop_most_recent()    # -> "agent_task"
    tracker.most_recent_active()  # -> "music"
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Valid activity names
ACTIVITY_MUSIC = "music"
ACTIVITY_AGENT = "agent_task"
_VALID_ACTIVITIES = frozenset({ACTIVITY_MUSIC, ACTIVITY_AGENT})


def _resolve_user_id(user_id: str | None = None) -> str:
    """Resolve user_id from an explicit value or authenticated context."""
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except LookupError as exc:
        raise ValueError("user_id is required for activity tracking") from exc


class ActivityTracker:
    """Records activity start/stop events with timestamps.

    Thread-safe via the GIL for dict operations (sufficient for our use case).

    Activities are stored per-user: ``{user_id: {activity: start_time}}``.
    The user_id is resolved automatically from the ambient ContextVar.
    """

    def __init__(self) -> None:
        # user_id -> {activity_name -> start_time (monotonic)}
        self._active: dict[str, dict[str, float]] = {}

    def _get_user_activities(self, user_id: str) -> dict[str, float]:
        """Return the activity dict for a user, creating on first access."""
        if user_id not in self._active:
            self._active[user_id] = {}
        return self._active[user_id]

    def record_start(self, activity: str, *, user_id: str | None = None) -> None:
        """Record that an activity has started."""
        if activity not in _VALID_ACTIVITIES:
            logger.warning("Unknown activity: %s", activity)
            return
        uid = _resolve_user_id(user_id)
        user_acts = self._get_user_activities(uid)
        user_acts[activity] = time.monotonic()
        logger.info(
            "Activity started: %s for user %s (active=%s)",
            activity,
            uid,
            list(user_acts.keys()),
        )

    def record_stop(self, activity: str, *, user_id: str | None = None) -> None:
        """Record that an activity has stopped."""
        uid = _resolve_user_id(user_id)
        user_acts = self._get_user_activities(uid)
        removed = user_acts.pop(activity, None)
        if removed is not None:
            logger.info(
                "Activity stopped: %s for user %s (active=%s)",
                activity,
                uid,
                list(user_acts.keys()),
            )

    def is_active(self, activity: str, *, user_id: str | None = None) -> bool:
        """Check if a specific activity is currently active for the user."""
        uid = _resolve_user_id(user_id)
        user_acts = self._active.get(uid, {})
        return activity in user_acts

    def most_recent_active(self, *, user_id: str | None = None) -> str | None:
        """Return the activity that started most recently for the user, or None."""
        uid = _resolve_user_id(user_id)
        user_acts = self._active.get(uid, {})
        if not user_acts:
            return None
        return max(user_acts, key=user_acts.__getitem__)

    def stop_most_recent(self, *, user_id: str | None = None) -> str | None:
        """Remove and return the most recently started activity for the user.

        Returns the activity name so the caller knows what to stop.
        """
        uid = _resolve_user_id(user_id)
        name = self.most_recent_active(user_id=uid)
        if name is not None:
            user_acts = self._get_user_activities(uid)
            user_acts.pop(name, None)
            logger.info(
                "Stopped most recent: %s for user %s (remaining=%s)",
                name,
                uid,
                list(user_acts.keys()),
            )
        return name

    def stop_all(self, *, user_id: str | None = None) -> list[str]:
        """Stop all active activities for the user. Returns list of stopped names."""
        uid = _resolve_user_id(user_id)
        user_acts = self._get_user_activities(uid)
        stopped = list(user_acts.keys())
        user_acts.clear()
        if stopped:
            logger.info("Stopped all activities for user %s: %s", uid, stopped)
        return stopped

    def active_activities(self, *, user_id: str | None = None) -> list[str]:
        """Return list of currently active activity names for the user, ordered by start time."""
        uid = _resolve_user_id(user_id)
        user_acts = self._active.get(uid, {})
        return sorted(user_acts, key=user_acts.__getitem__)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: ActivityTracker | None = None


def get_activity_tracker() -> ActivityTracker:
    """Return the global ActivityTracker singleton (created on first call)."""
    global _instance
    if _instance is None:
        _instance = ActivityTracker()
    return _instance
