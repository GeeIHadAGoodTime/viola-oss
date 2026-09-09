"""
Pure-Python timer service with persistence and event listeners.

This module contains the timer logic shared by desktop and headless/cloud
environments. It does not import Qt.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from core.platform import get_data_dir

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)
_MAX_TIMERS_PER_USER = 50
_MISSED_TIMER_DELIVERY_GRACE_SECONDS = 24 * 60 * 60


@dataclass
class Timer:
    """Represents a single timer."""

    timer_id: str
    label: str
    duration_seconds: int
    end_time: datetime
    created_at: datetime = field(default_factory=datetime.now)
    paused: bool = False
    remaining_when_paused: float | None = None

    @property
    def remaining_seconds(self) -> float:
        """Get remaining seconds (can be negative if expired)."""
        if self.paused and self.remaining_when_paused is not None:
            return self.remaining_when_paused
        return (self.end_time - datetime.now()).total_seconds()

    @property
    def is_expired(self) -> bool:
        """Check if timer has expired."""
        return self.remaining_seconds <= 0

    @property
    def progress(self) -> float:
        """Get progress as 0.0-1.0 (1.0 = complete)."""
        if self.duration_seconds <= 0:
            return 1.0
        elapsed = self.duration_seconds - self.remaining_seconds
        return min(1.0, max(0.0, elapsed / self.duration_seconds))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for persistence."""
        return {
            "timer_id": self.timer_id,
            "label": self.label,
            "duration_seconds": self.duration_seconds,
            "end_time": self.end_time.isoformat(),
            "created_at": self.created_at.isoformat(),
            "paused": self.paused,
            "remaining_when_paused": self.remaining_when_paused,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Timer:
        """Deserialize from dictionary."""
        return cls(
            timer_id=data["timer_id"],
            label=data["label"],
            duration_seconds=data["duration_seconds"],
            end_time=datetime.fromisoformat(data["end_time"]),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.now().isoformat())),
            paused=data.get("paused", False),
            remaining_when_paused=data.get("remaining_when_paused"),
        )


class TimerEventListener:
    """Optional event sink for timer lifecycle notifications."""

    def timer_added(self, user_id: str, timer: Timer) -> None:
        """Called after a timer is added."""

    def timer_cancelled(self, user_id: str, timer_id: str, timer: Timer | None) -> None:
        """Called after a timer is cancelled."""

    def timer_completed(self, user_id: str, timer_id: str, label: str) -> None:
        """Called after a timer completes."""

    def timer_updated(self, user_id: str, timer: Timer) -> None:
        """Called after a timer is paused or resumed."""

    def timers_changed(self, user_id: str | None = None) -> None:
        """Called after the active timer list changes."""


class TimerService:
    """
    Centralized timer management service.

    Multi-tenant: timers are keyed by ``user_id`` and persisted per user.
    """

    _instance: TimerService | None = None

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._timers: dict[str, dict[str, Timer]] = {}
        self._timers_lock = threading.RLock()
        self._check_timer: threading.Thread | None = None
        self._check_stop = threading.Event()
        self._completion_callbacks: list[Callable[[str, str, str], None]] = []
        self._listeners: list[TimerEventListener] = []

        self._load_all_user_timers()
        self._start_check_timer()

        with self._timers_lock:
            total = sum(len(v) for v in self._timers.values())
            user_count = len(self._timers)
        logger.info("TimerService initialized with %s active timers across %s users", total, user_count)

    @staticmethod
    def _resolve_user_id(user_id: str | None = None) -> str:
        """Resolve user_id from an explicit arg or the request-scoped ContextVar.

        F-050: timer mutations must NOT silently fall back to a device id
        or to a retired desktop pseudo-user. Cross-user leakage is the
        documented hazard in ``core/user_context.py``. Callers must pass
        ``user_id`` or be in a request whose middleware has set the
        ambient user context.
        """
        if user_id:
            return user_id
        from core.user_context import get_current_user_id

        try:
            return get_current_user_id()
        except LookupError as exc:
            raise PermissionError(
                "Timer operation requires an explicit user_id or an authenticated request context"
            ) from exc

    @classmethod
    def get_instance(cls, *args: Any, **kwargs: Any) -> TimerService:
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls(*args, **kwargs)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        if cls._instance is not None:
            cls._instance._stop_check_timer()
            cls._instance = None

    def add_listener(self, listener: TimerEventListener) -> None:
        """Register a timer lifecycle listener."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: TimerEventListener) -> None:
        """Remove a timer lifecycle listener."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify_listeners(self, event_name: str, *args: Any) -> None:
        for listener in list(self._listeners):
            callback = getattr(listener, event_name, None)
            if not callable(callback):
                continue
            try:
                callback(*args)
            except Exception:
                logger.exception("Timer listener error during %s", event_name)

    def _start_check_timer(self) -> None:
        """Start the background expiration checker."""
        if self._check_timer is not None and self._check_timer.is_alive():
            return

        self._check_stop.clear()

        def _loop() -> None:
            while not self._check_stop.wait(1.0):
                self._check_expirations()

        # mt-ok: shared expiration checker iterates per-user dicts inside
        # _check_expirations and notifies listeners with the resolved uid;
        # the thread itself owns no user-specific state.
        self._check_timer = threading.Thread(target=_loop, daemon=True, name="timer-service-checker")
        self._check_timer.start()

    def _stop_check_timer(self) -> None:
        """Stop the background expiration checker."""
        self._check_stop.set()
        thread = self._check_timer
        self._check_timer = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def ensure_checker_running(self) -> None:
        """Restart the background expiration checker if it is not alive.

        ``_stop_check_timer`` is called on cloud lifespan shutdown, so a process
        that re-enters lifespan (test harnesses, in-process restarts) would
        otherwise come back up with a live singleton whose checker thread is
        dead -- timers armed, nobody watching, which is the whole defect class.
        Idempotent: a no-op when the thread is already alive.
        """
        self._start_check_timer()

    def check_expirations_now(self) -> int:
        """Run one expiration sweep immediately and return how many fired.

        The background checker thread only wakes once a second, so a caller
        that has just registered its listeners (cloud startup: see
        ``backend/cloud_timer_expiry.register_cloud_timer_expiry``) uses this
        to deliver timers that came due while the process was down without
        racing the thread's first tick.
        """
        return self._check_expirations()

    def _check_expirations(self) -> int:
        """Check for and handle expired timers across all users.

        Returns the number of timers that completed in this sweep.
        """
        any_expired = False
        completed_timers: list[tuple[str, str, str]] = []
        users_to_save: list[str] = []

        with self._timers_lock:
            for uid in list(self._timers.keys()):
                user_timers = self._timers.get(uid, {})
                expired_ids = []

                for timer_id, timer in user_timers.items():
                    if timer.is_expired and not timer.paused:
                        expired_ids.append(timer_id)

                for timer_id in expired_ids:
                    timer = user_timers.pop(timer_id, None)
                    if timer is not None:
                        completed_timers.append((uid, timer_id, timer.label))

                if expired_ids:
                    any_expired = True
                    users_to_save.append(uid)
                    if not user_timers:
                        self._timers.pop(uid, None)

        for uid, timer_id, label in completed_timers:
            logger.info("Timer completed: %s (user=%s)", label, uid)
            self._notify_listeners("timer_completed", uid, timer_id, label)

            for callback in list(self._completion_callbacks):
                try:
                    callback(uid, timer_id, label)
                except Exception:
                    logger.exception("Timer completion callback error")

        for uid in users_to_save:
            self._save_timers(uid)
            self._notify_listeners("timers_changed", uid)

        if any_expired:
            self._notify_listeners("timers_changed", None)

        return len(completed_timers)

    def add_timer(
        self,
        duration_seconds: int,
        label: str = "",
        timer_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """Add a new timer and return its ID."""
        uid = self._resolve_user_id(user_id)

        if duration_seconds <= 0:
            raise ValueError("Timer duration must be positive")

        if timer_id is None:
            timer_id = "timer_%s" % datetime.now().timestamp()

        if not label:
            if duration_seconds >= 3600:
                hours = duration_seconds // 3600
                label = f"{hours} hour timer"
            elif duration_seconds >= 60:
                minutes = duration_seconds // 60
                label = f"{minutes} min timer"
            else:
                label = f"{duration_seconds}s timer"

        timer = Timer(
            timer_id=timer_id,
            label=label,
            duration_seconds=duration_seconds,
            end_time=datetime.now() + timedelta(seconds=duration_seconds),
        )

        with self._timers_lock:
            user_timers = self._timers.setdefault(uid, {})
            if len(user_timers) >= _MAX_TIMERS_PER_USER:
                raise ValueError("Timer limit reached for user")
            user_timers[timer_id] = timer

        self._save_timers(uid)
        logger.info("Timer added: %s (%ss) for user=%s", label, duration_seconds, uid)
        self._notify_listeners("timer_added", uid, timer)
        self._notify_listeners("timers_changed", uid)
        self._notify_listeners("timers_changed", None)
        return timer_id

    def cancel_timer(self, timer_id: str, user_id: str | None = None) -> bool:
        """Cancel a timer."""
        uid = self._resolve_user_id(user_id)
        timer: Timer | None = None
        with self._timers_lock:
            user_timers = self._timers.get(uid, {})
            timer = user_timers.pop(timer_id, None)
            if timer is not None and not user_timers:
                self._timers.pop(uid, None)
        if timer is None:
            return False

        self._save_timers(uid)
        logger.info("Timer cancelled: %s (user=%s)", timer.label, uid)
        self._notify_listeners("timer_cancelled", uid, timer_id, timer)
        self._notify_listeners("timers_changed", uid)
        self._notify_listeners("timers_changed", None)
        return True

    def pause_timer(self, timer_id: str, user_id: str | None = None) -> bool:
        """Pause a timer."""
        uid = self._resolve_user_id(user_id)
        timer: Timer | None = None
        with self._timers_lock:
            timer = self._timers.get(uid, {}).get(timer_id)
            should_pause = timer is not None and not timer.paused
            if should_pause and timer is not None:
                remaining = max(0.0, timer.remaining_seconds)
                timer.paused = True
                timer.remaining_when_paused = remaining
        if not should_pause or timer is None:
            return False

        self._save_timers(uid)
        self._notify_listeners("timer_updated", uid, timer)
        return True

    def resume_timer(self, timer_id: str, user_id: str | None = None) -> bool:
        """Resume a paused timer."""
        uid = self._resolve_user_id(user_id)
        timer: Timer | None = None
        with self._timers_lock:
            timer = self._timers.get(uid, {}).get(timer_id)
            should_resume = timer is not None and timer.paused and timer.remaining_when_paused is not None
            if should_resume and timer is not None and timer.remaining_when_paused is not None:
                timer.end_time = datetime.now() + timedelta(seconds=timer.remaining_when_paused)
                timer.paused = False
                timer.remaining_when_paused = None
        if not should_resume or timer is None:
            return False

        self._save_timers(uid)
        self._notify_listeners("timer_updated", uid, timer)
        return True

    def get_timer(self, timer_id: str, user_id: str | None = None) -> Timer | None:
        """Get a timer by ID."""
        uid = self._resolve_user_id(user_id)
        with self._timers_lock:
            return self._timers.get(uid, {}).get(timer_id)

    def get_all_timers(self, user_id: str | None = None) -> list[Timer]:
        """Get all active timers for a user, sorted by end time."""
        uid = self._resolve_user_id(user_id)
        with self._timers_lock:
            user_timers = list(self._timers.get(uid, {}).values())
        return sorted(user_timers, key=lambda timer: timer.end_time)

    def get_timers(self, user_id: str | None = None) -> list[Timer]:
        """Backward-compatible alias for listing active timers."""
        return self.get_all_timers(user_id=user_id)

    def get_timer_count(self, user_id: str | None = None) -> int:
        """Get count of active timers for a user."""
        uid = self._resolve_user_id(user_id)
        with self._timers_lock:
            return len(self._timers.get(uid, {}))

    def cancel_most_recent(self, user_id: str | None = None) -> bool:
        """Cancel the most recently created timer for a user."""
        uid = self._resolve_user_id(user_id)
        with self._timers_lock:
            user_timers = list(self._timers.get(uid, {}).values())
        if not user_timers:
            return False

        most_recent = max(user_timers, key=lambda timer: timer.created_at)
        return self.cancel_timer(most_recent.timer_id, user_id=uid)

    def cancel_all(self, user_id: str | None = None) -> int:
        """Cancel all timers for a user. Returns count of cancelled timers."""
        uid = self._resolve_user_id(user_id)
        with self._timers_lock:
            timer_ids = list(self._timers.get(uid, {}).keys())
        for timer_id in timer_ids:
            self.cancel_timer(timer_id, user_id=uid)
        return len(timer_ids)

    def on_completion(self, callback: Callable[[str, str, str], None]) -> None:
        """Register a callback for timer completion as (user_id, timer_id, label)."""
        self._completion_callbacks.append(callback)

    def _get_storage_path(self, user_id: str | None = None) -> str:
        """Get the path to the timers storage file for a user."""
        config_dir = str(get_data_dir())
        os.makedirs(config_dir, exist_ok=True)
        if user_id:
            safe_id = user_id.replace("/", "_").replace("\\", "_").replace("..", "_")
            return os.path.join(config_dir, "timers_%s.json" % safe_id)
        return os.path.join(config_dir, "timers.json")

    def _load_all_user_timers(self) -> None:
        """Scan the config dir for per-user timer files and load them all."""
        config_dir = str(get_data_dir())
        if not os.path.isdir(config_dir):
            return

        for file_name in os.listdir(config_dir):
            if file_name.startswith("timers_") and file_name.endswith(".json"):
                uid = file_name[len("timers_") : -len(".json")]
                self._load_timers(uid)

        legacy_path = os.path.join(config_dir, "timers.json")
        if os.path.exists(legacy_path):
            self._migrate_legacy_timers(legacy_path)

    def _migrate_legacy_timers(self, legacy_path: str) -> None:
        """Migrate timers from shared timers.json into per-user storage."""
        try:
            with open(legacy_path, encoding="utf-8") as handle:
                data = json.load(handle)

            timers_data = data.get("timers", [])
            if not timers_data:
                os.rename(legacy_path, legacy_path + ".migrated")
                return

            from core.user_context import get_device_user_id

            # Legacy shared timers.json predates per-user keying; assign its
            # rows to the device user so desktop upgrades retain timers.
            uid = get_device_user_id()  # mt-ok: one-time legacy migration target
            loaded = 0
            for timer_data in timers_data:
                try:
                    timer = Timer.from_dict(timer_data)
                    if timer.is_expired and not timer.paused:
                        continue
                    with self._timers_lock:
                        user_timers = self._timers.setdefault(uid, {})
                        if len(user_timers) >= _MAX_TIMERS_PER_USER:
                            logger.warning("Skipping migrated timer for user=%s because the cap was reached", uid)
                            break
                        user_timers[timer.timer_id] = timer
                        loaded += 1
                except Exception:
                    logger.exception("Failed to migrate legacy timer")

            if loaded:
                self._save_timers(uid)

            os.rename(legacy_path, legacy_path + ".migrated")
            logger.info("Migrated %s legacy timers to user=%s", loaded, uid)
        except Exception:
            logger.exception("Failed to migrate legacy timers")

    def _load_timers_for_user(self, user_id: str) -> None:
        """Load timers from persistent storage for a single user."""
        try:
            path = self._get_storage_path(user_id)
            if not os.path.exists(path):
                return

            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)

            loaded = 0
            now = datetime.now()
            for timer_data in data.get("timers", []):
                try:
                    timer = Timer.from_dict(timer_data)
                    if timer.is_expired and not timer.paused:
                        overdue_seconds = (now - timer.end_time).total_seconds()
                        if overdue_seconds > _MISSED_TIMER_DELIVERY_GRACE_SECONDS:
                            logger.info(
                                "Skipping stale expired timer for user=%s; overdue by %.0fs",
                                user_id,
                                overdue_seconds,
                            )
                            continue
                    with self._timers_lock:
                        user_timers = self._timers.setdefault(user_id, {})
                        if len(user_timers) >= _MAX_TIMERS_PER_USER:
                            logger.warning(
                                "Skipping persisted timer for user=%s because the cap was reached",
                                user_id,
                            )
                            break
                        user_timers[timer.timer_id] = timer
                        loaded += 1
                except Exception:
                    logger.exception("Failed to load timer for user=%s", user_id)

            if loaded:
                logger.info("Loaded %s timers from storage for user=%s", loaded, user_id)
        except Exception:
            logger.exception("Failed to load timers for user=%s", user_id)

    def _load_timers(self, user_id: str) -> None:
        """Backward-compatible wrapper for loading persisted user timers."""
        self._load_timers_for_user(user_id)

    def _save_timers(self, user_id: str | None = None) -> None:
        """Save timers to persistent storage for a user."""
        uid = user_id or self._resolve_user_id()
        try:
            path = self._get_storage_path(uid)
            with self._timers_lock:
                user_timers = list(self._timers.get(uid, {}).values())

            data = {
                "version": 2,
                "user_id": uid,
                "timers": [timer.to_dict() for timer in user_timers],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        except Exception:
            logger.exception("Failed to save timers for user=%s", uid)


def get_timer_service() -> TimerService:
    """Get the global TimerService instance."""
    return TimerService.get_instance()


def parse_duration(text: str) -> int | None:
    """
    Parse a duration string into seconds.

    Supports formats like:
    - "5 minutes", "5 mins", "5m", "five minutes"
    - "1 hour", "1h", "one hour"
    - "30 seconds", "30s", "30 sec"
    - "1 hour 30 minutes", "1h30m"
    - "90" (assumed minutes)
    """
    import re

    text = text.lower().strip()

    text = re.sub(
        r"\b(?:a|an)\s+(?=(?:second|minute|hour|day|week|sec|min|hr)s?\b)",
        "1 ",
        text,
    )

    word_numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "fifteen": 15,
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "forty-five": 45,
        "forty five": 45,
        "fifty": 50,
        "sixty": 60,
        "ninety": 90,
        "half": 0.5,
        "quarter": 0.25,
    }

    for word, number in word_numbers.items():
        text = text.replace(word, str(number))

    total_seconds = 0
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*h(?:ours?)?", 3600),
        (r"(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?", 60),
        (r"(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?", 1),
    ]

    found_any = False
    for pattern, multiplier in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            total_seconds += int(float(match) * multiplier)
            found_any = True

    if found_any:
        return total_seconds

    plain_match = re.match(r"^(\d+)$", text)
    if plain_match:
        return int(plain_match.group(1)) * 60

    return None


__all__ = [
    "Timer",
    "TimerEventListener",
    "TimerService",
    "get_timer_service",
    "parse_duration",
]
